"""init, doctor, strict mode and the Codex/Cursor hook adapters (real CLI in subprocesses)."""
import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from test_agentlanes import CLI, ROOT, Base  # noqa: E402

BIN = os.path.join(ROOT, 'bin')


class InitBase(Base):
    def setUp(self):
        super().setUp()
        self.proj = os.path.join(self.tmp, 'proj')
        os.makedirs(self.proj)
        self.env.pop('AGENTLANES_STATE_DIR')
        self.env['PATH'] = BIN + os.pathsep + self.env.get('PATH', '/usr/bin:/bin')
        self.env['HOME'] = self.tmp          # keep the user's real ~/.claude out of it
        self.env.pop('XDG_STATE_HOME', None)

    def init(self, *args, env=None):
        return subprocess.run(CLI + ['init'] + list(args), cwd=self.proj, capture_output=True, text=True,
                              env=env or self.env, timeout=60)

    def read(self, *parts):
        with open(os.path.join(self.proj, *parts)) as fh:
            return fh.read()

    def backups(self):
        d = os.path.join(self.proj, '.claude')
        return sorted(f for f in os.listdir(d) if '-backup-' in f) if os.path.isdir(d) else []


class InitTest(InitBase):

    def test_fresh_project_and_idempotent(self):
        r = self.init('--claude-code', '--agents-md', '--claude-md', '--cursor')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.isdir(os.path.join(self.proj, '.agentlanes')))
        self.assertIn('*', self.read('.agentlanes', '.gitignore'))
        s = json.loads(self.read('.claude', 'settings.json'))
        h = s['hooks']['PreToolUse'][0]
        self.assertEqual(h['matcher'], 'Edit|Write|MultiEdit|NotebookEdit')
        self.assertEqual(h['hooks'][0]['command'], 'agentlanes hook claude-code')
        self.assertIn('agentlanes claim', self.read('AGENTS.md'))
        self.assertIn('agentlanes claim', self.read('CLAUDE.md'))
        self.assertIn('alwaysApply: true', self.read('.cursor', 'rules', 'agentlanes.mdc'))
        before = {f: self.read(*f.split('/')) for f in ('.claude/settings.json', 'AGENTS.md', 'CLAUDE.md')}
        r = self.init('--claude-code', '--agents-md', '--claude-md', '--cursor')
        self.assertEqual(r.returncode, 0)
        for f, txt in before.items():
            self.assertEqual(self.read(*f.split('/')), txt, f)
        self.assertEqual(self.read('AGENTS.md').count('agentlanes:begin'), 1)
        self.assertEqual(self.backups(), [])          # nothing pre-existed, nothing changed: no backup

    def test_mcp_json_merged_and_idempotent(self):
        with open(os.path.join(self.proj, '.mcp.json'), 'w') as fh:
            json.dump({'mcpServers': {'other': {'command': 'x'}}}, fh)
        r = self.init('--mcp')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        m = json.loads(self.read('.mcp.json'))['mcpServers']
        self.assertEqual(m['other'], {'command': 'x'})
        self.assertEqual(m['agentlanes']['args'][-1], 'mcp')
        self.assertEqual(m['agentlanes']['type'], 'stdio')
        before = self.read('.mcp.json')
        self.assertEqual(self.init('--mcp').returncode, 0)
        self.assertEqual(self.read('.mcp.json'), before)
        self.assertEqual(len([f for f in os.listdir(self.proj) if '.mcp.json.' in f]), 1)   # one backup, first run

    def test_state_dir_marker_is_used(self):
        self.init()
        sub = os.path.join(self.proj, 'src', 'deep')
        os.makedirs(sub)
        r = subprocess.run(CLI + ['claim', 't', '--desc', 'x'], cwd=sub, env=self.env, capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue(os.path.exists(os.path.join(self.proj, '.agentlanes', 'claims.json')))

    def test_merges_into_existing_settings_with_other_hooks(self):
        os.makedirs(os.path.join(self.proj, '.claude'))
        orig = {
            'permissions': {'allow': ['Bash(npm test)']},
            'env': {'FOO': '1'},
            'hooks': {
                'PreToolUse': [{'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': 'guard.sh'}]}],
                'PostToolUse': [{'matcher': 'Edit', 'hooks': [{'type': 'command', 'command': 'fmt.sh'}]}],
            },
        }
        raw = json.dumps(orig, indent=4)
        with open(os.path.join(self.proj, '.claude', 'settings.json'), 'w') as fh:
            fh.write(raw)
        r = self.init('--claude-code')
        self.assertEqual(r.returncode, 0, r.stdout)
        s = json.loads(self.read('.claude', 'settings.json'))
        self.assertEqual(s['permissions'], orig['permissions'])
        self.assertEqual(s['env'], orig['env'])
        self.assertEqual(s['hooks']['PostToolUse'], orig['hooks']['PostToolUse'])
        pre = s['hooks']['PreToolUse']
        self.assertEqual(pre[0], orig['hooks']['PreToolUse'][0])
        self.assertEqual(len(pre), 2)
        self.assertIn('hook claude-code', pre[1]['hooks'][0]['command'])
        bak = self.backups()
        self.assertEqual(len(bak), 1)
        self.assertEqual(self.read('.claude', bak[0]), raw)      # byte-for-byte original
        self.init('--claude-code')
        self.assertEqual(len(json.loads(self.read('.claude', 'settings.json'))['hooks']['PreToolUse']), 2)
        self.assertEqual(len(self.backups()), 1)
        # switching to strict updates the same entry in place instead of adding a second one
        r = self.init('--claude-code', '--strict')
        pre = json.loads(self.read('.claude', 'settings.json'))['hooks']['PreToolUse']
        self.assertEqual(len(pre), 2)
        self.assertIn('AGENTLANES_STRICT=1', pre[1]['hooks'][0]['command'])

    def test_invalid_settings_left_untouched(self):
        os.makedirs(os.path.join(self.proj, '.claude'))
        with open(os.path.join(self.proj, '.claude', 'settings.json'), 'w') as fh:
            fh.write('{ "hooks": [ broken')
        r = self.init('--claude-code')
        self.assertEqual(r.returncode, 1)
        self.assertIn('not valid JSON', r.stdout)
        self.assertEqual(self.read('.claude', 'settings.json'), '{ "hooks": [ broken')
        self.assertEqual(self.backups(), [])

    def test_snippet_appended_once_after_existing_text(self):
        with open(os.path.join(self.proj, 'AGENTS.md'), 'w') as fh:
            fh.write('# House rules\nUse tabs.')
        self.init('--agents-md')
        self.init('--agents-md')
        txt = self.read('AGENTS.md')
        self.assertTrue(txt.startswith('# House rules\nUse tabs.\n\n<!-- agentlanes:begin -->'), txt[:80])
        self.assertEqual(txt.count('Working alongside other agents'), 1)

    def test_strict_hook_command_blocks_when_tool_missing(self):
        self.init('--claude-code', '--strict')
        cmd = json.loads(self.read('.claude', 'settings.json'))['hooks']['PreToolUse'][0]['hooks'][0]['command']
        probe = json.dumps({'tool_name': 'Edit', 'tool_input': {'file_path': os.path.join(self.proj, 'a.py')}})
        env = dict(self.env, PATH='/usr/bin:/bin')
        r = subprocess.run(['/bin/sh', '-c', cmd], input=probe, env=env, capture_output=True, text=True,
                           cwd=self.proj, timeout=30)
        self.assertEqual(r.returncode, 2)                      # Claude Code: exit 2 = block, stderr to the model
        self.assertIn('not installed or not on PATH', r.stderr)
        r = subprocess.run(['/bin/sh', '-c', cmd], input=probe, env=self.env, capture_output=True, text=True,
                           cwd=self.proj, timeout=30)
        self.assertEqual((r.returncode, r.stdout), (0, ''))

    def test_examples_match_packaged_snippet(self):
        sys.path.insert(0, os.path.join(ROOT, 'src'))
        from agentlanes import onboard
        with open(os.path.join(ROOT, 'examples', 'AGENTS-snippet.md')) as fh:
            self.assertEqual(fh.read(), onboard.SNIPPET)
        with open(os.path.join(ROOT, 'examples', 'cursor', 'agentlanes.mdc')) as fh:
            self.assertEqual(fh.read(), onboard.CURSOR_RULE)


class DoctorTest(InitBase):
    def doctor(self, env=None):
        return subprocess.run(CLI + ['doctor'], cwd=self.proj, capture_output=True, text=True, env=env or self.env,
                              timeout=120)

    def test_healthy_project(self):
        self.init('--claude-code')
        r = self.doctor()
        self.assertEqual(r.returncode, 0, r.stdout)
        for s in ('on PATH', 'state directory is writable', 'flock excludes a second process',
                  'Claude Code hook in', 'all checks passed'):
            self.assertIn(s, r.stdout)

    def test_reports_missing_path_and_broken_hook(self):
        self.init('--claude-code')
        r = self.doctor(env=dict(self.env, PATH='/usr/bin:/bin'))
        self.assertEqual(r.returncode, 1)
        self.assertIn('FAIL  `agentlanes` is not on PATH', r.stdout)
        self.assertIn('does not run', r.stdout)
        self.assertIn('fix:', r.stdout)

    def test_reports_corrupt_claims(self):
        self.init()
        with open(os.path.join(self.proj, '.agentlanes', 'claims.json'), 'w') as fh:
            fh.write('{oops')
        r = self.doctor()
        self.assertEqual(r.returncode, 1)
        self.assertIn('claims.json is corrupt', r.stdout)


class StrictAndAdaptersTest(Base):
    def hook(self, kind, payload, **env):
        inp = payload if isinstance(payload, str) else json.dumps(payload)
        return self.ak('hook', kind, inp=inp, env=self.env_for(**env))

    def claim_as_other(self, path):
        holder = self.spawn(['sleep', '60'])
        r = self.ak('claim', 'feat', '--paths', path, '--pid', str(holder.pid),
                    env=self.env_for(AGENTLANES_AGENT='other', AGENTLANES_AGENT_PID=holder.pid))
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_fail_open_by_default_and_closed_in_strict(self):
        os.makedirs(self.state)
        with open(os.path.join(self.state, 'claims.json'), 'w') as fh:
            fh.write('{corrupt')
        edit = {'tool_name': 'Edit', 'tool_input': {'file_path': os.path.join(self.tmp, 'x.py')}}
        self.assertEqual(self.hook('claude-code', edit).stdout, '')
        r = self.hook('claude-code', edit, AGENTLANES_STRICT='1')
        out = json.loads(r.stdout)['hookSpecificOutput']
        self.assertEqual(out['permissionDecision'], 'deny')
        self.assertIn('strict mode', out['permissionDecisionReason'])
        self.assertEqual(self.hook('claude-code', 'not json').stdout, '')
        self.assertIn('deny', self.hook('claude-code', 'not json', AGENTLANES_STRICT='1').stdout)
        self.assertEqual(self.hook('claude-code', edit, AGENTLANES_STRICT='0').stdout, '')

    def test_strict_allows_healthy_unclaimed_edit(self):
        edit = {'tool_name': 'Write', 'tool_input': {'file_path': os.path.join(self.tmp, 'x.py')}}
        r = self.hook('claude-code', edit, AGENTLANES_STRICT='1')
        self.assertEqual((r.returncode, r.stdout), (0, ''))

    def test_codex_apply_patch(self):
        f = os.path.join(self.tmp, 'src', 'app.py')
        self.claim_as_other(f)
        patch = ('*** Begin Patch\n*** Update File: src/other.py\n@@\n-a\n+b\n*** Update File: src/app.py\n'
                 '@@\n-a\n+b\n*** End Patch\n')
        payload = {'hook_event_name': 'PreToolUse', 'tool_name': 'apply_patch', 'cwd': self.tmp,
                   'tool_input': {'command': patch}}
        out = json.loads(self.hook('codex', payload, AGENTLANES_AGENT='me').stdout)['hookSpecificOutput']
        self.assertEqual(out['permissionDecision'], 'deny')
        self.assertIn('claimed by other', out['permissionDecisionReason'])
        payload['tool_input']['command'] = patch.replace('src/app.py', 'src/free.py')
        self.assertEqual(self.hook('codex', payload, AGENTLANES_AGENT='me').stdout, '')
        bash = {'tool_name': 'Bash', 'cwd': self.tmp, 'tool_input': {'command': 'ls'}}
        self.assertEqual(self.hook('codex', bash, AGENTLANES_AGENT='me').stdout, '')

    def test_cursor_always_answers(self):
        f = os.path.join(self.tmp, 'app.py')
        self.claim_as_other(f)
        r = self.hook('cursor', {'tool_name': 'Write', 'tool_input': {'file_path': f}}, AGENTLANES_AGENT='me')
        out = json.loads(r.stdout)
        self.assertEqual(out['permission'], 'deny')
        self.assertIn('claimed by other', out['agent_message'])
        r = self.hook('cursor', {'tool_name': 'Write', 'tool_input': {'file_path': f + '.free'}},
                      AGENTLANES_AGENT='me')
        self.assertEqual(json.loads(r.stdout), {'permission': 'allow'})

    def test_unknown_adapter_is_usage_error(self):
        self.assertEqual(self.ak('hook', 'nope', inp='{}').returncode, 2)


if __name__ == '__main__':
    unittest.main()
