"""Lanes: a git worktree per agent, claimed; other agents are kept out; done refuses to drop unsaved work."""
import json
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from test_agentlanes import Base  # noqa: E402


@unittest.skipUnless(shutil.which('git'), 'git not installed')
class Lanes(Base):
    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, 'repo')
        os.makedirs(self.repo)
        self.env.pop('AGENTLANES_STATE_DIR')          # use the project's .agentlanes/, as real users do
        g = dict(GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t')
        self.env.update(g)
        for c in (['init', '-q'], ['commit', '-q', '--allow-empty', '-m', 'init']):
            subprocess.run(['git', *c], cwd=self.repo, env=self.env, check=True)
        self.lane = os.path.join(self.repo, '.agentlanes', 'lanes', 'auth')

    def ak(self, *args, cwd=None, agent='alice', **kw):
        return subprocess.run(self.cli_argv(*args), capture_output=True, text=True, cwd=cwd or self.repo,
                              env=self.env_for(AGENTLANES_AGENT=agent), timeout=60)

    def cli_argv(self, *args):
        from test_agentlanes import CLI
        return CLI + list(args)

    def test_new_list_done(self):
        r = self.ak('lane', 'new', 'auth', '--desc', 'OAuth')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.isdir(self.lane))
        rows = json.loads(self.ak('lane', 'list', '--json').stdout)
        self.assertEqual([(x['name'], x['branch'], x['holder']) for x in rows], [('auth', 'lane/auth', 'alice')])
        # the main tree does not see the lane as a change
        self.assertEqual(subprocess.run(['git', 'status', '--porcelain'], cwd=self.repo, capture_output=True,
                                        text=True).stdout, '')
        # from inside the lane, the project's claims are visible (shared state)
        r = self.ak('who', 'x.py', cwd=self.lane, agent='bob')
        self.assertEqual(r.returncode, 1)
        self.assertIn('alice', r.stdout)
        r = self.ak('lane', 'done', 'auth')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertFalse(os.path.exists(self.lane))
        self.assertIn('lane/auth', subprocess.run(['git', 'branch'], cwd=self.repo, capture_output=True,
                                                  text=True).stdout)
        self.assertEqual(json.loads(self.ak('list', '--json').stdout), {})

    def test_other_agent_is_kept_out(self):
        self.ak('lane', 'new', 'auth')
        r = self.ak('lane', 'new', 'auth', agent='bob')
        self.assertEqual(r.returncode, 1)
        self.assertIn('HELD by alice', r.stdout)
        r = self.ak('lane', 'done', 'auth', agent='bob')
        self.assertEqual(r.returncode, 1)
        self.assertTrue(os.path.isdir(self.lane))

    def test_done_keeps_unsaved_work(self):
        self.ak('lane', 'new', 'auth')
        with open(os.path.join(self.lane, 'new.py'), 'w') as fh:
            fh.write('x = 1\n')
        r = self.ak('lane', 'done', 'auth')
        self.assertEqual(r.returncode, 1)
        self.assertTrue(os.path.exists(os.path.join(self.lane, 'new.py')))
        self.assertIn('lane:auth', self.ak('list').stdout)       # still claimed

    def test_existing_branch_and_bad_names(self):
        subprocess.run(['git', 'branch', 'lane/old'], cwd=self.repo, check=True)
        r = self.ak('lane', 'new', 'old')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('existing', r.stdout)
        for bad in ('../x', '-x', 'a/b', ''):
            self.assertEqual(self.ak('lane', 'new', bad).returncode, 2, bad)
        r = self.ak('lane', 'new', 'x', cwd=self.tmp)          # not a repository
        self.assertEqual(r.returncode, 2)


if __name__ == '__main__':
    unittest.main()
