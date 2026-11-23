"""`agentlanes run` (Landlock). Kernel tests run only where Landlock works; the ABI logic is unit-tested everywhere."""
import errno
import io
import json
import os
import socket
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), 'src'))
from agentlanes import landlock, run  # noqa: E402

from test_agentlanes import CLI, Base  # noqa: E402

try:
    ABI = landlock.abi_version()
except landlock.Unsupported:
    ABI = 0
need_landlock = unittest.skipUnless(ABI >= 1, 'Landlock not available on this kernel')
DENIED = ('Permission denied', 'Operation not permitted')


@need_landlock
class KernelLanes(Base):
    def setUp(self):
        super().setUp()
        self.a = os.path.join(self.tmp, 'lane')
        self.b = os.path.join(self.tmp, 'outside')
        os.makedirs(self.a)
        os.makedirs(self.b)
        with open(os.path.join(self.b, 'keep.txt'), 'w') as fh:
            fh.write('original\n')
        with open(os.path.join(self.a, 'mine.txt'), 'w') as fh:
            fh.write('mine\n')

    def run_in(self, script, *opts, env=None):
        return self.ak('run', '-q', *opts, '--', 'sh', '-c', script, env=env)

    def keep(self):
        with open(os.path.join(self.b, 'keep.txt')) as fh:
            return fh.read()

    def assert_denied(self, r):
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(any(d in r.stderr for d in DENIED), r.stderr)

    def test_write_inside_claim_ok_outside_denied(self):
        self.assertEqual(self.ak('claim', 'feat', '--paths', self.a).returncode, 0)
        r = self.run_in('echo x > "$0/new.txt" && sed -i s/mine/ours/ "$0/mine.txt" && mkdir "$0/d" && '
                        'mv "$0/new.txt" "$0/d/n.txt" && rm "$0/d/n.txt" && rmdir "$0/d" && cat "$0/mine.txt"'
                        .replace('$0', self.a))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, 'ours\n')
        for script in ('echo x > {b}/f', 'echo x >> {b}/keep.txt', 'cp /etc/hostname {b}/c',
                       'sed -i s/original/changed/ {b}/keep.txt', 'rm {b}/keep.txt', 'mkdir {b}/d',
                       'mv {a}/mine.txt {b}/moved', 'mv {b}/keep.txt {a}/stolen', 'ln {a}/mine.txt {b}/hard',
                       'ln -s /etc/passwd {b}/sym', 'touch {b}/t', 'mkfifo {b}/p'):
            with self.subTest(script=script):
                self.assert_denied(self.run_in(script.format(a=self.a, b=self.b)))
        self.assertEqual(sorted(os.listdir(self.b)), ['keep.txt'])
        self.assertEqual(self.keep(), 'original\n')
        self.assertTrue(os.path.exists(os.path.join(self.a, 'mine.txt')))

    @unittest.skipUnless(ABI >= 3, 'truncate is restricted from Landlock ABI 3')
    def test_truncate_denied(self):
        self.assert_denied(self.run_in('truncate -s 0 %s/keep.txt' % self.b, '--write', self.a))
        self.assertEqual(self.keep(), 'original\n')

    @unittest.skipUnless(ABI >= 2, 'REFER needs ABI 2')
    def test_refer_rename_and_link_between_two_lanes(self):
        c = os.path.join(self.tmp, 'lane2')
        os.makedirs(c)
        r = self.run_in('mv {a}/mine.txt {c}/m && ln {c}/m {a}/back && cat {a}/back'.format(a=self.a, c=c),
                        '--write', self.a, '--write', c)
        self.assertEqual((r.returncode, r.stdout), (0, 'mine\n'), r.stderr)

    def test_claims_from_inside_and_state_dir_writable(self):
        self.ak('claim', 'feat', '--paths', self.a)
        r = self.ak('run', '-q', '--', *CLI, 'claim', 'inner', '--paths', os.path.join(self.tmp, 'other'),
                    '--desc', 'from inside')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.ak('check', 'inner').returncode, 1)
        r = self.ak('run', '-q', '--claim', 'feat', '--', *CLI, 'release', 'inner')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.ak('check', 'inner').returncode, 0)
        # a claim taken inside does NOT widen the running sandbox
        other = os.path.join(self.tmp, 'other')
        os.makedirs(other)
        r = self.run_in('{cli} claim late --paths {o} && echo x > {o}/f'.format(
            cli=' '.join(CLI), o=other), '--claim', 'feat')
        self.assert_denied(r)

    def test_claim_selection(self):
        self.ak('claim', 'feat', '--paths', self.a)
        self.ak('claim', 'theirs', '--paths', self.b, '--by', 'someone-else')
        self.assert_denied(self.run_in('echo x > %s/f' % self.b))           # default: only my claims
        r = self.run_in('echo x > %s/f' % self.b, '--claim', 'theirs')     # explicitly named claim
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('held by someone-else', self.ak('run', '--claim', 'theirs', '--', 'true').stderr)
        self.assertEqual(self.ak('run', '--claim', 'nope', '--', 'true').returncode, 1)

    def test_nothing_writable_refused(self):
        r = self.ak('run', '--', 'true')
        self.assertEqual(r.returncode, 2)
        self.assertIn('nothing to make writable', r.stderr)
        self.assertEqual(self.ak('run', '--write', os.path.join(self.tmp, 'missing'), '--', 'true').returncode, 2)

    def test_allow_tmp(self):
        r = self.run_in('echo t > "$TMPDIR/x" && cat "$TMPDIR/x" && echo "$TMPDIR"', '--write', self.a, '--allow-tmp')
        self.assertEqual(r.returncode, 0, r.stderr)
        out, tmpdir = r.stdout.split('\n')[:2]
        self.assertEqual(out, 't')
        self.assertIn('agentlanes-run-', tmpdir)
        self.assertFalse(os.path.exists(tmpdir), 'private temp dir is removed afterwards')

    def test_events_and_exit_code(self):
        r = self.run_in('exit 7', '--write', self.a)
        self.assertEqual(r.returncode, 7)
        start, stop = self.events('run.start'), self.events('run.stop')
        self.assertEqual(len(start), 1)
        self.assertIn(self.a, start[0]['write'])
        self.assertEqual(start[0]['abi'], ABI)
        self.assertEqual(stop[0]['rc'], 7)

    def test_nested_run_only_narrows(self):
        c = os.path.join(self.a, 'sub')
        os.makedirs(c)
        r = self.ak('run', '-q', '--write', self.a, '--', *CLI, 'run', '-q', '--write', self.b, '--write', c, '--',
                    'sh', '-c', 'echo x > %s/f; echo y > %s/f; echo z > %s/../f2' % (self.b, c, c))
        self.assertTrue(os.path.exists(os.path.join(c, 'f')), 'allowed by both layers')
        self.assertFalse(os.path.exists(os.path.join(self.b, 'f')), 'inner --write cannot exceed the outer lane')
        self.assertFalse(os.path.exists(os.path.join(self.a, 'f2')), 'inner lane excludes the rest of the outer one')
        self.assertTrue(any(d in r.stderr for d in DENIED), r.stderr)

    def test_dry_run_prints_rules(self):
        r = self.ak('run', '--dry-run', '--write', self.a, '--', 'true')
        self.assertEqual(r.returncode, 0, r.stderr)
        d = json.loads(r.stdout)
        self.assertEqual(d['abi'], ABI)
        self.assertIn(self.a, d['write'])

    @unittest.skipUnless(ABI >= 4, 'TCP restriction needs ABI 4')
    def test_no_net_and_allow_port(self):
        srv = socket.socket()
        srv.bind(('127.0.0.1', 0))
        srv.listen(5)
        port = srv.getsockname()[1]
        try:
            code = ('import socket,sys\ntry:\n socket.create_connection(("127.0.0.1",%d),timeout=5); print("connected")\n'
                    'except OSError as e: print("err", e.errno)' % port)
            r = self.ak('run', '-q', '--write', self.a, '--no-net', '--', sys.executable, '-c', code)
            self.assertEqual(r.stdout.strip(), 'err %d' % errno.EACCES, r.stderr)
            r = self.ak('run', '-q', '--write', self.a, '--allow-port', str(port), '--', sys.executable, '-c', code)
            self.assertEqual(r.stdout.strip(), 'connected', r.stderr)
            r = self.ak('run', '-q', '--write', self.a, '--', sys.executable, '-c', code)
            self.assertEqual(r.stdout.strip(), 'connected', r.stderr)
        finally:
            srv.close()


class AbiLogic(unittest.TestCase):
    """Best-effort degradation, tested without touching the kernel."""

    def plan(self, abi, **kw):
        pol = landlock.Policy(['/work'], **kw)
        return landlock.plan(pol, abi, listdir=lambda d: ['null', 'pts', 'shm', 'stdin'], isdir=lambda p: p != '/dev/null',
                             exists=lambda p: True, islink=lambda p: p.endswith('stdin'))

    def test_fs_masks_per_abi(self):
        L = landlock
        self.assertEqual(L.fs_rights(0), 0)
        self.assertEqual(L.fs_rights(1), L.FS_V1)
        self.assertEqual(L.fs_rights(2), L.FS_V1 | L.FS_REFER)
        self.assertEqual(L.fs_rights(3), L.FS_V1 | L.FS_REFER | L.FS_TRUNCATE)
        self.assertEqual(L.fs_rights(4), L.fs_rights(3))
        self.assertEqual(L.fs_rights(5), L.fs_rights(3) | L.FS_IOCTL_DEV)
        self.assertEqual(L.fs_rights(9), L.fs_rights(5))   # unknown future ABI: what we know
        for abi in range(1, 8):
            p = self.plan(abi)
            for path, rights in p['rules']:
                self.assertEqual(rights & ~p['handled_fs'], 0, 'rule rights must be a subset of handled (%d)' % abi)

    def test_rules(self):
        p = self.plan(5)
        rules = dict(p['rules'])
        L = landlock
        self.assertEqual(rules['/'], L.FS_READ)
        self.assertEqual(rules['/work'], L.fs_rights(5))
        self.assertNotIn('/dev/shm', rules)
        self.assertNotIn('/dev/stdin', rules)
        self.assertEqual(rules['/dev/null'] & ~L.FS_FILE_ONLY, 0, 'file rules get file-only rights')
        self.assertFalse(rules['/dev/pts'] & L.FS_MAKE_REG, 'devices: write, never create')

    def test_net_and_scope_degrade_with_warning(self):
        for abi in (1, 2, 3):
            p = self.plan(abi, no_net=True)
            self.assertEqual(p['handled_net'], 0)
            self.assertTrue(any('ABI 4' in w for w in p['warnings']))
        p = self.plan(4, connect_ports=[443])
        self.assertEqual(p['handled_net'], landlock.NET_BIND_TCP | landlock.NET_CONNECT_TCP)
        self.assertEqual(p['net_rules'], [(443, landlock.NET_CONNECT_TCP)])
        self.assertTrue(any('ABI 6' in w for w in self.plan(5, isolate=True)['warnings']))
        self.assertEqual(self.plan(6, isolate=True)['scoped'], landlock.SCOPE_SIGNAL | landlock.SCOPE_ABSTRACT_UNIX_SOCKET)
        self.assertTrue(any('EXDEV' in w for w in self.plan(1)['warnings']))
        self.assertTrue(any('truncate' in w for w in self.plan(2)['warnings']))
        self.assertEqual(self.plan(3)['warnings'], [])

    def test_missing_paths_skipped(self):
        pol = landlock.Policy(['/nope'])
        p = landlock.plan(pol, 3, listdir=lambda d: [], exists=lambda p: p != '/nope')
        self.assertIn('/nope', p['skipped'])

    def test_attr_size_per_abi(self):
        sizes = []

        def fake_call(nr, *args):
            if nr == landlock.SYS_CREATE_RULESET:
                sizes.append(args[1].value)
                return 99
            return 0
        with mock.patch.object(landlock, '_call', fake_call), mock.patch.object(os, 'close'):
            for abi in (1, 3, 4, 5, 6):
                landlock.build({'abi': abi, 'handled_fs': landlock.fs_rights(abi), 'handled_net': 0, 'scoped': 0,
                                'rules': [], 'net_rules': []})
        self.assertEqual(sizes, [8, 8, 16, 16, 24])

    def test_abi_version_errors(self):
        for err, text in ((errno.ENOSYS, 'no Landlock'), (errno.EOPNOTSUPP, 'disabled')):
            with mock.patch.object(landlock, '_call', side_effect=OSError(err, 'x')):
                with self.assertRaises(landlock.Unsupported) as cm:
                    landlock.abi_version()
                self.assertIn(text, str(cm.exception))
        with mock.patch.object(landlock.sys, 'platform', 'darwin'):
            with self.assertRaises(landlock.Unsupported) as cm:
                landlock.abi_version()
            self.assertIn('Linux feature', str(cm.exception))
            self.assertEqual(landlock.describe_abi()[0], 'info')


class Unsupported(Base):
    """No Landlock: refuse by default, run unconfined only with --best-effort."""

    def call(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(landlock, 'abi_version', side_effect=landlock.Unsupported('old kernel')), \
                redirect_stdout(out), redirect_stderr(err):
            rc = run.main(list(argv))
        return rc, err.getvalue()

    def test_refuse_then_best_effort(self):
        target = os.path.join(self.tmp, 'f')
        rc, err = self.call('--write', self.tmp, '--', 'sh', '-c', 'echo x > %s' % target)
        self.assertEqual(rc, 3)
        self.assertIn('refusing to run unconfined: old kernel', err)
        self.assertFalse(os.path.exists(target))
        rc, err = self.call('--best-effort', '--write', self.tmp, '--', 'sh', '-c', 'echo x > %s' % target)
        self.assertEqual(rc, 0)
        self.assertIn('UNCONFINED', err)
        self.assertTrue(os.path.exists(target))

    def test_doctor_reports_landlock(self):
        r = subprocess.run(CLI + ['doctor'], capture_output=True, text=True, env=self.env)
        self.assertIn('Landlock', r.stdout)


if __name__ == '__main__':
    unittest.main()
