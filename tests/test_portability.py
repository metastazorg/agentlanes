"""The non-/proc (macOS) code paths.

Two layers:
1. Real execution on Linux with AGENTLANES_NO_PROC=1: leases, pid-reuse detection and agent discovery run through
   `ps` instead of /proc (procps `ps`, which takes the same `-o field=` options as BSD/macOS `ps`).
2. In-process tests that fake macOS: sys.platform='darwin', canned BSD `ps`, `vm_stat` and `lsof` output.
What this cannot prove: that real macOS `ps`/`vm_stat`/`lsof` print exactly what the canned samples show.
"""
import os
import subprocess
import sys
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'src'))
from test_agentlanes import Base  # noqa: E402

from agentlanes import onboard, procs, slots  # noqa: E402

VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                3842.
Pages active:                            201033.
Pages inactive:                          195318.
Pages speculative:                         2210.
Pages throttled:                              0.
Pages wired down:                         98760.
"""


class PsPathOnLinux(Base):
    """Same scenarios as the main suite, forced through the `ps` code path."""

    def setUp(self):
        super().setUp()
        self.env['AGENTLANES_NO_PROC'] = '1'

    def test_lease_and_reaping_via_ps(self):
        holder = self.spawn(['sleep', '60'])
        f = os.path.join(self.tmp, 'a.py')
        self.assertEqual(self.ak('claim', 't', '--paths', f, '--pid', str(holder.pid)).returncode, 0)
        self.assertEqual(self.ak('who', f).returncode, 1)
        holder.kill()
        holder.wait()
        self.assertEqual(self.ak('who', f).returncode, 0)
        self.assertEqual(len(self.events('claim.reap')), 1)

    def test_recycled_pid_detected_via_lstart(self):
        p = self.spawn(['sleep', '60'])
        self.write_claims({'t': {'by': 'x', 'since': 1, 'until': 9e9, 'pid': p.pid,
                                 'pstart': 'Thu Jan  1 00:00:00 1970', 'paths': ['/x']}})
        self.assertIn('no active claims', self.ak('list').stdout)

    def test_pid_self_finds_agent_via_ps(self):
        # a process named like an agent CLI, running a shell that calls us: `--pid self` must find it
        agent_dir = os.path.join(self.tmp, 'agentbin')
        os.makedirs(agent_dir)
        fake = os.path.join(agent_dir, 'codex')
        os.symlink('/bin/sh', fake)
        cli = ' '.join(['"%s"' % a for a in [sys.executable, os.path.join(os.path.dirname(HERE), 'bin',
                                                                          'agentlanes')]])
        r = subprocess.run([fake, '-c', '%s claim t --pid self; echo pid=$$' % cli], env=self.env,
                           capture_output=True, text=True, timeout=60)
        self.assertIn('claimed t', r.stdout, r.stdout + r.stderr)
        sh_pid = int(r.stdout.split('pid=')[1])
        import json
        with open(os.path.join(self.state, 'claims.json')) as fh:
            c = json.load(fh)['t']
        self.assertEqual(c['apid'], sh_pid)
        self.assertEqual(c['agent'], 'codex')


class FakeMac(unittest.TestCase):
    def setUp(self):
        p = [mock.patch.object(procs, 'LINUX', False), mock.patch.object(sys, 'platform', 'darwin')]
        for x in p:
            x.start()
            self.addCleanup(x.stop)

    def fake_run(self, table):
        """subprocess.run stub: table maps argv[0] -> stdout (or a callable(argv) -> stdout)."""
        calls = []

        def run(argv, **kw):
            calls.append((list(argv), kw))
            out = table.get(argv[0], '')
            out = out(argv) if callable(out) else out
            return types.SimpleNamespace(returncode=0 if out else 1, stdout=out, stderr='')
        return run, calls

    def test_ps_argv_is_bsd_compatible_and_locale_fixed(self):
        run, calls = self.fake_run({'ps': ' 4242 node /opt/homebrew/bin/claude --resume\n'})
        with mock.patch.object(procs.subprocess, 'run', run):
            self.assertEqual(procs.parent_and_argv(777), (4242, ['node', '/opt/homebrew/bin/claude', '--resume']))
        argv, kw = calls[0]
        self.assertEqual(argv, ['ps', '-o', 'ppid=', '-o', 'args=', '-p', '777'])
        self.assertEqual(kw['env']['LC_ALL'], 'C')

    def test_parent_and_argv_survives_garbage(self):
        for out in ('', '   PPID\n', 'x y'):
            run, _ = self.fake_run({'ps': out})
            with mock.patch.object(procs.subprocess, 'run', run):
                self.assertEqual(procs.parent_and_argv(1), (None, []))

    def test_start_token_uses_lstart(self):
        run, calls = self.fake_run({'ps': 'Mon Nov 23 22:13:53 2026\n'})
        with mock.patch.object(procs.subprocess, 'run', run):
            self.assertEqual(procs.start_token(os.getpid()), 'Mon Nov 23 22:13:53 2026')
        self.assertIn('lstart=', calls[0][0])

    def test_same_process_keeps_lease_if_ps_fails(self):
        run, _ = self.fake_run({})   # ps prints nothing
        with mock.patch.object(procs.subprocess, 'run', run):
            self.assertTrue(procs.pid_alive(os.getpid()))
            self.assertTrue(procs.same_process(os.getpid(), 'Mon Nov 23 22:13:53 2026'))

    def test_zombie_and_recycled(self):
        run, _ = self.fake_run({'ps': lambda a: 'Z+\n' if 'stat=' in a else 'Tue Nov 24 01:00:00 2026\n'})
        with mock.patch.object(procs.subprocess, 'run', run):
            self.assertFalse(procs.pid_alive(os.getpid()))
        run, _ = self.fake_run({'ps': lambda a: 'S\n' if 'stat=' in a else 'Tue Nov 24 01:00:00 2026\n'})
        with mock.patch.object(procs.subprocess, 'run', run):
            self.assertFalse(procs.same_process(os.getpid(), 'Mon Nov 23 22:13:53 2026'))
            self.assertTrue(procs.same_process(os.getpid(), 'Tue Nov 24 01:00:00 2026'))

    def test_agent_self_walks_ps(self):
        tree = {os.getppid(): (500, ['/bin/zsh', '-c', 'agentlanes claim x']),
                500: (400, ['node', '/opt/homebrew/lib/node_modules/@anthropic-ai/claude-code/cli.js']),
                400: (1, ['login'])}
        with mock.patch.object(procs, 'parent_and_argv', lambda p: tree.get(p, (None, []))), \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('AGENTLANES_AGENT_PID', None)
            self.assertEqual(procs.agent_self(), (500, 'claude'))

    def test_process_table_bsd_etime(self):
        out = ('  1     0  12000 10-02:03:04 /sbin/launchd\n'
               '500     1 300000    01:02 node /usr/local/bin/codex exec fix\n')
        run, calls = self.fake_run({'ps': out})
        with mock.patch.object(procs.subprocess, 'run', run):
            t = procs.process_table()
        self.assertEqual(t[1]['age_s'], 10 * 86400 + 2 * 3600 + 3 * 60 + 4)
        self.assertEqual(t[500]['age_s'], 62)
        self.assertEqual(t[500]['argv'][1], '/usr/local/bin/codex')
        self.assertEqual(calls[0][0], ['ps', '-ax', '-o', 'pid=', '-o', 'ppid=', '-o', 'rss=', '-o', 'etime=',
                                       '-o', 'args='])

    def test_vm_stat(self):
        run, _ = self.fake_run({'vm_stat': VM_STAT})
        with mock.patch.object(procs.subprocess, 'run', run):
            self.assertEqual(procs.mem_available_mb(), (3842 + 195318 + 2210) * 16384 // 1048576)
        self.assertIsNone(procs.parse_vm_stat('garbage'))

    def test_cwd_via_lsof(self):
        run, calls = self.fake_run({'lsof': 'p500\nfcwd\nn/Users/dev/project\n'})
        with mock.patch.object(procs.subprocess, 'run', run):
            self.assertEqual(procs.process_cwd(500), '/Users/dev/project')
        self.assertEqual(calls[0][0][:5], ['lsof', '-a', '-p', '500', '-d'])
        run, _ = self.fake_run({})
        with mock.patch.object(procs.subprocess, 'run', run):
            self.assertIsNone(procs.process_cwd(500))

    def test_no_systemd_scope_and_no_proc_mounts(self):
        self.assertFalse(slots._systemd_scope_ok())
        self.assertIsNone(onboard.fs_type('/'))


if __name__ == '__main__':
    unittest.main()
