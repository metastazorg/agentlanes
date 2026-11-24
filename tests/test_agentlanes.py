"""End-to-end tests: every test drives the real CLI in subprocesses against a throwaway state directory.
    python3 -m unittest discover -s tests -v
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
CLI = [sys.executable, os.path.join(ROOT, 'bin', 'agentlanes')]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='agentlanes-test-')
        self.state = os.path.join(self.tmp, 'state')
        self.env = {k: v for k, v in os.environ.items() if not k.startswith('AGENTLANES_')}
        self.env.update(AGENTLANES_STATE_DIR=self.state, AGENTLANES_AGENT='tester')
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    p.kill()
                p.wait()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def env_for(self, **kw):
        e = dict(self.env)
        e.update({k: str(v) for k, v in kw.items()})
        return e

    def ak(self, *args, env=None, inp=None, timeout=60):
        return subprocess.run(CLI + list(args), capture_output=True, text=True, env=env or self.env, input=inp,
                              timeout=timeout)

    def spawn(self, argv, env=None, **kw):
        p = subprocess.Popen(argv, env=env or self.env, start_new_session=True, **kw)
        self.procs.append(p)
        return p

    def events(self, kind=''):
        p = os.path.join(self.state, 'events.jsonl')
        if not os.path.exists(p):
            return []
        with open(p) as fh:
            return [e for e in map(json.loads, fh) if e['kind'].startswith(kind)]

    def write_claims(self, d):
        os.makedirs(self.state, exist_ok=True)
        with open(os.path.join(self.state, 'claims.json'), 'w') as fh:
            json.dump(d, fh)

    def read_claims(self):
        with open(os.path.join(self.state, 'claims.json')) as fh:
            return json.load(fh)

    def wait_for(self, cond, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.1)
        return False


class ClaimsTest(Base):
    def test_basic_contract(self):
        r = self.ak('claim', 't1', '--desc', 'work')
        self.assertEqual((r.returncode, r.stdout.strip()), (0, 'claimed t1'))
        r = self.ak('claim', 't1', '--by', 'other')
        self.assertEqual(r.returncode, 1)
        self.assertTrue(r.stdout.startswith('HELD by tester'))
        self.assertEqual(self.ak('check', 't1').returncode, 1)
        self.assertEqual(self.ak('release', 't1', '--by', 'other').returncode, 1)
        self.assertEqual(self.ak('release', 't1').stdout.strip(), 'released t1')
        self.assertEqual(self.ak('list').stdout.strip(), 'no active claims')

    def test_concurrent_claims_same_path(self):
        """16 processes race for the same file under 16 different topic names: exactly one wins."""
        target = os.path.join(self.tmp, 'src', 'main.c')
        n = 16
        ps = [subprocess.Popen(CLI + ['claim', 'edit-%d' % i, '--paths', target], env=self.env_for(AGENTLANES_AGENT='a%d' % i),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i in range(n)]
        rcs = [p.wait(timeout=60) for p in ps]
        outs = [p.stdout.read() for p in ps]
        for p in ps:
            p.stdout.close()
            p.stderr.close()
        self.assertEqual(rcs.count(0), 1, outs)
        self.assertEqual(rcs.count(1), n - 1, outs)
        d = self.read_claims()
        self.assertEqual(len(d), 1)
        self.assertEqual(len(self.events('claim.conflict')), n - 1)

    def test_concurrent_claims_same_topic(self):
        n = 16
        ps = [subprocess.Popen(CLI + ['claim', 'release-notes'], env=self.env_for(AGENTLANES_AGENT='a%d' % i),
                               stdout=subprocess.DEVNULL) for i in range(n)]
        rcs = [p.wait(timeout=60) for p in ps]
        self.assertEqual(rcs.count(0), 1)

    def test_paths_overlap_dir_and_share(self):
        d = os.path.join(self.tmp, 'pkg')
        self.assertEqual(self.ak('claim', 'refactor', '--paths', d).returncode, 0)
        other = self.env_for(AGENTLANES_AGENT='b')
        self.assertEqual(self.ak('claim', 'fix', '--paths', d + '/x.py', env=other).returncode, 1)
        self.assertEqual(self.ak('claim', 'fix', '--paths', d + '-other/x.py', env=other).returncode, 0)
        self.assertEqual(self.ak('claim', 'fix2', '--paths', d + '/x.py', '--share', env=other).returncode, 0)
        r = self.ak('who', d + '/deep/file.txt')
        self.assertEqual(r.returncode, 1)
        self.assertIn('HELD by tester (refactor', r.stdout)

    def test_lease_dies_with_holder_process(self):
        holder = self.spawn(['sleep', '60'])
        f = os.path.join(self.tmp, 'a.txt')
        self.assertEqual(self.ak('claim', 'x', '--paths', f, '--pid', str(holder.pid)).returncode, 0)
        other = self.env_for(AGENTLANES_AGENT='b')
        self.assertEqual(self.ak('claim', 'y', '--paths', f, env=other).returncode, 1)
        holder.kill()
        holder.wait()
        r = self.ak('claim', 'y', '--paths', f, env=other)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual([e['topic'] for e in self.events('claim.reap')], ['x'])

    def test_recycled_pid_is_not_the_holder(self):
        now = time.time()
        self.write_claims({'old': {'by': 'ghost', 'since': now, 'until': now + 600, 'pid': os.getpid(),
                                   'pstart': 'not-me', 'paths': ['/p']}})
        self.assertEqual(self.ak('who', '/p').returncode, 0)
        self.assertEqual(len(self.events('claim.reap')), 1)

    def test_ttl_expiry(self):
        now = time.time()
        self.write_claims({'old': {'by': 'x', 'since': now - 100, 'until': now - 1}})
        self.assertEqual(self.ak('list').stdout.strip(), 'no active claims')
        self.assertEqual(len(self.events('claim.expire')), 1)

    def test_bad_lease_pid_is_an_error(self):
        env = self.env_for(AGENTLANES_AGENT_NAMES='', PATH=os.environ['PATH'])
        r = subprocess.run(CLI + ['claim', 't', '--pid', '999999999'], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 2)

    def test_notify_cmd_gets_conflict(self):
        out = os.path.join(self.tmp, 'notified.json')
        env = self.env_for(AGENTLANES_NOTIFY_CMD='cat > %s' % out)
        self.ak('claim', 't', env=env)
        self.ak('claim', 't', env=self.env_for(AGENTLANES_AGENT='b', AGENTLANES_NOTIFY_CMD='cat > %s' % out))
        self.assertTrue(self.wait_for(lambda: os.path.exists(out) and os.path.getsize(out) > 0))
        with open(out) as fh:
            self.assertEqual(json.load(fh)['kind'], 'claim.conflict')


class SlotTest(Base):
    def slot(self, *a, **kw):
        return self.ak('slot', *a, **kw)

    def test_runs_and_returns_exit_code(self):
        r = self.slot('s', '1', '--', sys.executable, '-c', 'import os,sys; print(os.environ["AGENTLANES_SLOT"]); sys.exit(3)')
        self.assertEqual((r.returncode, r.stdout.strip()), (3, '1'))
        self.assertEqual(self.events('slot.release')[0]['rc'], 3)

    def test_capacity_never_exceeded(self):
        log = os.path.join(self.tmp, 'log')
        code = ('import sys,time; f=open(sys.argv[1],"a"); f.write("s %f\\n" % time.time()); f.flush(); '
                'time.sleep(0.6); f.write("e %f\\n" % time.time())')
        ps = [self.spawn(CLI + ['slot', 'cap', '2', '--', sys.executable, '-c', code, log]) for _ in range(5)]
        for p in ps:
            self.assertEqual(p.wait(timeout=60), 0)
        with open(log) as fh:
            marks = sorted((float(t), k) for k, t in (l.split() for l in fh))
        cur = peak = 0
        for _, k in marks:
            cur += 1 if k == 's' else -1
            peak = max(peak, cur)
        self.assertEqual(peak, 2)

    def test_fifo_fairness(self):
        log = os.path.join(self.tmp, 'order')
        holder = self.spawn(CLI + ['slot', 'f', '1', '--', 'sleep', '2'])
        self.assertTrue(self.wait_for(lambda: self.events('slot.acquire')))
        ws = []
        for i in range(5):
            ws.append(self.spawn(CLI + ['slot', 'f', '1', '--', 'sh', '-c', 'echo w%d >> %s' % (i, log)]))
            time.sleep(0.4)
        for p in [holder] + ws:
            self.assertEqual(p.wait(timeout=60), 0)
        with open(log) as fh:
            self.assertEqual(fh.read().split(), ['w0', 'w1', 'w2', 'w3', 'w4'])

    def test_timeout_exit_75(self):
        self.spawn(CLI + ['slot', 'h', '1', '--', 'sleep', '5'])
        self.assertTrue(self.wait_for(lambda: self.events('slot.acquire')))
        self.assertEqual(self.slot('h', '1', '--wait', '1', '--', 'true').returncode, 75)
        self.assertEqual(len(self.events('slot.timeout')), 1)
        self.assertEqual(json.loads(self.ak('slots', 'h', '--json').stdout)['h']['waiting'], [])

    def test_stale_holder_reclaimed(self):
        """Holder and its command are SIGKILLed (e.g. by the OOM killer): the slot is free at once, the record is stale."""
        p = self.spawn(CLI + ['slot', 'st', '1', '--', 'sleep', '60'])
        self.assertTrue(self.wait_for(lambda: self.events('slot.acquire')))
        self.assertIn('busy', self.ak('slots', 'st').stdout)
        os.killpg(p.pid, signal.SIGKILL)
        p.wait()
        self.assertTrue(self.wait_for(lambda: 'stale' in self.ak('slots', 'st').stdout or
                                      not os.path.exists(os.path.join(self.state, 'slots/st/1.who'))))
        self.assertEqual(len(self.events('slot.stale')), 1)
        self.assertEqual(self.slot('st', '1', '--wait', '3', '--', 'true').returncode, 0)

    def test_killed_wrapper_keeps_slot_while_command_runs(self):
        p = self.spawn(CLI + ['slot', 'k', '1', '--', 'sleep', '3'])
        self.assertTrue(self.wait_for(lambda: self.events('slot.acquire')))
        os.kill(p.pid, signal.SIGKILL)   # wrapper only; `sleep` keeps the inherited lock
        p.wait()
        self.assertEqual(self.slot('k', '1', '--wait', '1', '--', 'true').returncode, 75)
        self.assertEqual(self.slot('k', '1', '--wait', '10', '--', 'true').returncode, 0)

    def test_dead_waiter_does_not_block_queue(self):
        self.spawn(CLI + ['slot', 'q', '1', '--', 'sleep', '2'])
        self.assertTrue(self.wait_for(lambda: self.events('slot.acquire')))
        w = self.spawn(CLI + ['slot', 'q', '1', '--', 'true'])
        self.assertTrue(self.wait_for(lambda: json.loads(self.ak('slots', 'q', '--json').stdout)['q']['waiting']))
        os.killpg(w.pid, signal.SIGKILL)
        w.wait()
        self.assertEqual(self.slot('q', '1', '--wait', '10', '--', 'true').returncode, 0)

    def test_memory_watchdog_without_systemd(self):
        env = self.env_for(AGENTLANES_NO_SYSTEMD=1)
        code = 'import time; b = b"x" * (120 << 20); time.sleep(30)'
        r = self.slot('m', '1', '--max-mem', '60', '--', sys.executable, '-c', code, env=env)
        self.assertEqual(r.returncode, 128 + signal.SIGKILL)
        self.assertEqual(len(self.events('slot.oom')), 1)

    def test_bad_names_rejected(self):
        for n in ('a b', '../x', 'status'):
            self.assertEqual(self.slot(n, '1', '--', 'true').returncode, 2, n)


class EventsTest(Base):
    def test_wait_ignores_old_and_returns_on_match(self):
        self.ak('events', 'emit', 'build.done', '--topic', 'old')
        self.assertEqual(self.ak('events', 'wait', '--kind', 'build.done', '--timeout', '1').returncode, 124)
        w = subprocess.Popen(CLI + ['events', 'wait', '--kind', 'build.done', '--topic', 'web', '--timeout', '20'],
                             env=self.env, stdout=subprocess.PIPE, text=True)
        time.sleep(1)
        self.ak('events', 'emit', 'build.done', '--topic', 'api')
        self.ak('events', 'emit', 'build.done', '--topic', 'web', '--msg', 'ok')
        out, _ = w.communicate(timeout=20)
        self.assertEqual(w.returncode, 0)
        self.assertIn('[web] ok', out)

    def test_concurrent_emitters_no_torn_lines(self):
        code = ('import sys; sys.path.insert(0, %r); from agentlanes import core\n'
                'for i in range(50): core.emit("load.test", msg="x" * 200, i=i)') % os.path.join(ROOT, 'src')
        ps = [subprocess.Popen([sys.executable, '-c', code], env=self.env) for _ in range(8)]
        for p in ps:
            self.assertEqual(p.wait(timeout=60), 0)
        self.assertEqual(len(self.events('load.test')), 400)

    def test_rotation_and_tail(self):
        env = self.env_for(AGENTLANES_EVENTS_MAX_BYTES=300)
        for i in range(10):
            self.ak('events', 'emit', 'x.many', '--msg', 'y' * 50, 'n=%d' % i, env=env)
        self.assertTrue(os.path.exists(os.path.join(self.state, 'events.jsonl.1')))
        self.assertIn('n=9', self.ak('events', 'tail', '-n', '1').stdout)

    def test_emit_never_raises(self):
        sys.path.insert(0, os.path.join(ROOT, 'src'))
        from agentlanes import core
        old = os.environ.get('AGENTLANES_STATE_DIR')
        os.environ['AGENTLANES_STATE_DIR'] = '/proc/forbidden/state'
        try:
            self.assertIsNone(core.emit('x'))
        finally:
            if old is None:
                os.environ.pop('AGENTLANES_STATE_DIR')
            else:
                os.environ['AGENTLANES_STATE_DIR'] = old


class JobsTest(Base):
    def test_take_order_done_and_requeue_on_death(self):
        self.ak('jobs', 'add', 'low', '--prio', '90')
        self.ak('jobs', 'add', 'high', '--prio', '10')
        taker = self.spawn(['sleep', '60'])
        r = self.ak('jobs', 'take', '--pid', str(taker.pid))
        self.assertIn('took #2: high', r.stdout)
        taker.kill()
        taker.wait()
        self.assertIn('waiting  high', self.ak('jobs', 'list').stdout)
        self.assertEqual(len(self.events('job.requeue')), 1)
        self.ak('jobs', 'take', '2', '--pid', 'none')
        self.assertEqual(self.ak('jobs', 'done', '2', '--result', 'shipped').returncode, 0)
        self.assertNotIn('high', self.ak('jobs', 'list').stdout)
        self.assertIn('done', self.ak('jobs', 'list', '--all').stdout)


class HookTest(Base):
    def run_hook(self, agent_pid, path, tool='Edit'):
        inp = json.dumps({'session_id': 's', 'hook_event_name': 'PreToolUse', 'tool_name': tool,
                          'tool_input': {'file_path': path, 'old_string': 'a', 'new_string': 'b'}})
        return self.ak('hook', 'claude-code', inp=inp, env=self.env_for(AGENTLANES_AGENT_PID=agent_pid,
                                                                         AGENTLANES_AGENT='agent-%d' % agent_pid))

    def test_denies_other_agents_claim_only(self):
        a, b = self.spawn(['sleep', '60']), self.spawn(['sleep', '60'])
        f = os.path.join(self.tmp, 'app.py')
        r = self.ak('claim', 'feature', '--paths', f, '--pid', 'self',
                    env=self.env_for(AGENTLANES_AGENT_PID=a.pid, AGENTLANES_AGENT='agent-%d' % a.pid))
        self.assertEqual(r.returncode, 0, r.stdout)
        r = self.run_hook(b.pid, f)
        self.assertEqual(r.returncode, 0)
        out = json.loads(r.stdout)['hookSpecificOutput']
        self.assertEqual(out['permissionDecision'], 'deny')
        self.assertIn('claimed by agent-%d' % a.pid, out['permissionDecisionReason'])
        self.assertEqual(self.run_hook(a.pid, f).stdout, '')                       # own claim
        self.assertEqual(self.run_hook(b.pid, os.path.join(self.tmp, 'free.py')).stdout, '')   # unclaimed
        a.kill()
        a.wait()
        self.assertEqual(self.run_hook(b.pid, f).stdout, '')                       # holder died: lease gone
        self.assertEqual(self.ak('hook', 'claude-code', inp='not json').returncode, 0)


if __name__ == '__main__':
    unittest.main()


class StatusWatch(Base):
    """status --watch redraws until interrupted; one-shot status still works."""

    def test_status_once_and_watch_parses(self):
        import subprocess, signal, os, sys, time
        r = self.ak('status')
        self.assertEqual(r.returncode, 0)
        self.assertIn('== claims', r.stdout)
        p = subprocess.Popen(CLI + ['status', '--watch', '0.5'], stdout=subprocess.PIPE, text=True, env=self.env)
        time.sleep(1.6)
        p.send_signal(signal.SIGINT)
        out, _ = p.communicate(timeout=10)
        self.assertEqual(p.returncode, 0)
        self.assertGreaterEqual(out.count('== claims'), 2)


class Blame(HookTest):
    """Allowed edits are recorded by the hook; `blame` lists who edited a path."""

    def test_blame_records_allowed_and_blocked_edits(self):
        a, b = self.spawn(['sleep', '60']), self.spawn(['sleep', '60'])
        target = os.path.join(self.tmp, 'app.py')
        self.assertEqual(self.run_hook(b.pid, target).stdout, '')          # allowed -> recorded as edit
        r = self.ak('claim', 'mine', '--paths', target, '--pid', 'self',
                    env=self.env_for(AGENTLANES_AGENT_PID=a.pid, AGENTLANES_AGENT='agent-%d' % a.pid))
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('deny', self.run_hook(b.pid, target).stdout)         # blocked -> recorded as BLOCKED
        out = self.ak('blame', target)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn('agent-%d' % b.pid, out.stdout)
        self.assertIn('edit', out.stdout)
        self.assertIn('BLOCKED', out.stdout)
        self.assertEqual(self.ak('blame', os.path.join(self.tmp, 'nothing.py')).returncode, 1)
