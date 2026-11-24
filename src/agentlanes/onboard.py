"""`init` (set a project up) and `doctor` (check an install). Both are safe to run again and again."""
import json
import os
import shlex
import shutil
import subprocess
import sys
import time

from . import ENV, NAME, PROJECT_MARKER, __version__, core, procs

BEGIN = '<!-- %s:begin -->' % NAME
END = '<!-- %s:end -->' % NAME

SNIPPET = """## Working alongside other agents

Other coding agents may be working on this machine at the same time. Coordinate through `{n}`:

1. Before editing, run `{n} list` to see what others hold.
2. Claim what you will change:
   `{n} claim <short-topic> --paths <files-or-dirs> --pid self --desc "<one line: what you are doing>"`
   If it prints `HELD` or `PATH HELD`, do not edit those paths. Pick other work, or wait with
   `{n} events wait --kind claim.done --timeout 600` and try again.
3. When finished (or when you give up), run `{n} release <short-topic>`.
   If you exit or crash, the claim ends by itself because of `--pid self`.
4. Run heavy commands (full builds, full test suites, browsers, model runs) through a slot:
   `{n} slot heavy 1 -- <command>`. Never start a second copy of a heavy command in parallel.
5. Claims expire after 120 minutes; run `{n} renew <short-topic>` if you need longer.
6. To wait for another agent, use `{n} events wait --kind <kind>` instead of sleep loops.
""".format(n=NAME)

CURSOR_RULE = """---
description: Coordinate with other coding agents on this machine through {n}
alwaysApply: true
---
""".format(n=NAME) + SNIPPET

HOOK_MATCHER = 'Edit|Write|MultiEdit|NotebookEdit'


def self_command():
    """How a hook should call us: the bare name if it is on PATH, else the absolute path of this checkout."""
    if shutil.which(NAME):
        return NAME
    argv0 = os.path.realpath(sys.argv[0]) if sys.argv and sys.argv[0] else ''
    if argv0 and os.path.basename(argv0) == NAME and os.path.isfile(argv0):
        return '%s %s' % (shlex.quote(sys.executable), shlex.quote(argv0))
    return '%s -m %s' % (shlex.quote(sys.executable), NAME)


def hook_command(strict=False, cmd=None):
    cmd = cmd or self_command()
    if not strict:
        return '%s hook claude-code' % cmd
    first = shlex.split(cmd)[0]
    # Exit 2 makes Claude Code block the tool call and show stderr to the model, even when the tool is missing.
    return ('command -v %s >/dev/null 2>&1 || { echo "%s is not installed or not on PATH; edits are blocked '
            '(strict mode). Install it or remove the hook." >&2; exit 2; }; %s_STRICT=1 %s hook claude-code'
            % (shlex.quote(first), NAME, ENV, cmd))


def _is_our_hook(h):
    return isinstance(h, dict) and ' hook claude-code' in str(h.get('command', '')) and NAME in str(h.get('command'))


def merge_settings(settings, command):
    """Add our PreToolUse hook to a Claude Code settings dict in place. Returns 'added', 'updated' or 'present'."""
    hooks = settings.setdefault('hooks', {})
    if not isinstance(hooks, dict):
        raise ValueError('"hooks" in settings is not an object')
    pre = hooks.setdefault('PreToolUse', [])
    if not isinstance(pre, list):
        raise ValueError('"hooks.PreToolUse" in settings is not a list')
    for group in pre:
        for h in (group.get('hooks') or []) if isinstance(group, dict) else []:
            if _is_our_hook(h):
                if h.get('command') == command:
                    return 'present'
                h['command'] = command
                return 'updated'
    pre.append({'matcher': HOOK_MATCHER, 'hooks': [{'type': 'command', 'command': command, 'timeout': 10}]})
    return 'added'


def _write(p, text):
    tmp = '%s.%d.tmp' % (p, os.getpid())
    with open(tmp, 'w', encoding='utf-8') as fh:
        fh.write(text)
    os.replace(tmp, p)


def wire_claude_code(root, strict=False, out=print, command=None):
    """Merge the hook into ROOT/.claude/settings.json; back the old file up first. Returns exit code."""
    d = os.path.join(root, '.claude')
    p = os.path.join(d, 'settings.json')
    settings, original = {}, None
    if os.path.exists(p):
        with open(p, encoding='utf-8') as fh:
            original = fh.read()
        try:
            settings = json.loads(original) if original.strip() else {}
        except ValueError as e:
            out('error: %s is not valid JSON (%s); left untouched. Fix it and run init again.' % (p, e))
            return 1
        if not isinstance(settings, dict):
            out('error: %s is not a JSON object; left untouched.' % p)
            return 1
    try:
        what = merge_settings(settings, hook_command(strict, command))
    except ValueError as e:
        out('error: %s: %s; left untouched.' % (p, e))
        return 1
    if what == 'present':
        out('ok    Claude Code hook already in %s' % p)
        return 0
    os.makedirs(d, exist_ok=True)
    if original is not None:
        bak = '%s.%s-backup-%s' % (p, NAME, time.strftime('%Y%m%d-%H%M%S'))
        _write(bak, original)
        out('      backup of the previous settings: %s' % bak)
    _write(p, json.dumps(settings, indent=2, ensure_ascii=False) + '\n')
    out('%s Claude Code hook in %s%s' % ('added ' if what == 'added' else 'update', p, ' (strict)' if strict else ''))
    return 0


def wire_mcp(root, out=print, command=None):
    """Register our MCP server in ROOT/.mcp.json (Claude Code project scope; Cursor reads the same shape from
    .cursor/mcp.json). Merges, never drops other servers; backs the old file up. Returns exit code."""
    p = os.path.join(root, '.mcp.json')
    argv = shlex.split(command or self_command()) + ['mcp']
    want = {'type': 'stdio', 'command': argv[0], 'args': argv[1:]}
    cfg, original = {}, None
    if os.path.exists(p):
        with open(p, encoding='utf-8') as fh:
            original = fh.read()
        try:
            cfg = json.loads(original) if original.strip() else {}
        except ValueError as e:
            out('error: %s is not valid JSON (%s); left untouched.' % (p, e))
            return 1
        if not isinstance(cfg, dict) or not isinstance(cfg.get('mcpServers', {}), dict):
            out('error: %s has an unexpected shape; left untouched.' % p)
            return 1
    servers = cfg.setdefault('mcpServers', {})
    if servers.get(NAME) == want:
        out('ok    MCP server already in %s' % p)
        return 0
    what = 'update' if NAME in servers else 'added '
    servers[NAME] = want
    if original is not None:
        bak = '%s.%s-backup-%s' % (p, NAME, time.strftime('%Y%m%d-%H%M%S'))
        _write(bak, original)
        out('      backup of the previous file: %s' % bak)
    _write(p, json.dumps(cfg, indent=2, ensure_ascii=False) + '\n')
    out('%s MCP server in %s (Claude Code asks once to trust project servers)' % (what, p))
    return 0


def append_snippet(p, body=SNIPPET, out=print):
    """Append the agent instructions to P between markers, once."""
    old = ''
    if os.path.exists(p):
        with open(p, encoding='utf-8') as fh:
            old = fh.read()
    if BEGIN in old:
        out('ok    instructions already in %s' % p)
        return 0
    sep = ''
    if old:   # exactly one blank line between the existing text and ours
        sep = ('' if old.endswith('\n') else '\n') + ('' if old.endswith('\n\n') else '\n')
    os.makedirs(os.path.dirname(p) or '.', exist_ok=True)
    _write(p, old + sep + BEGIN + '\n' + body.rstrip('\n') + '\n' + END + '\n')
    out('added instructions to %s' % p)
    return 0


def init(root=None, claude_code=False, agents_md=False, claude_md=False, cursor=False, strict=False, out=print,
         mcp=False):
    root = os.path.abspath(root or os.getcwd())
    d = os.path.join(root, PROJECT_MARKER)
    os.makedirs(d, exist_ok=True)
    gi = os.path.join(d, '.gitignore')
    if not os.path.exists(gi):
        _write(gi, '# runtime coordination state, never commit\n*\n')
    out('ok    state directory %s (ignored by git)' % d)
    rc = 0
    if claude_code:
        rc |= wire_claude_code(root, strict, out)
    if mcp:
        rc |= wire_mcp(root, out)
    if agents_md:
        rc |= append_snippet(os.path.join(root, 'AGENTS.md'), out=out)
    if claude_md:
        rc |= append_snippet(os.path.join(root, 'CLAUDE.md'), out=out)
    if cursor:
        p = os.path.join(root, '.cursor', 'rules', NAME + '.mdc')
        if os.path.exists(p):
            out('ok    Cursor rule already at %s' % p)
        else:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            _write(p, CURSOR_RULE)
            out('added Cursor rule %s' % p)
    if not (claude_code or agents_md or claude_md or cursor or mcp):
        out('next: `%s init --claude-code --mcp --agents-md` wires the Claude Code hook, MCP tools and agent instructions' % NAME)
    if not shutil.which(NAME):
        out('warn  `%s` is not on PATH; agents and hooks need it there (pip install .)' % NAME)
    return rc


# ------------------------------------------------------------------ doctor

def _flock_excludes(state):
    """True if a second process really cannot take a lock we hold (catches filesystems where flock is a no-op)."""
    import fcntl
    p = os.path.join(state, '.doctor.lock')
    with open(p, 'a') as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        code = ('import fcntl,sys\nf=open(sys.argv[1],"a")\ntry:\n fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)\n'
                ' sys.exit(1)\nexcept OSError:\n sys.exit(0)\n')
        r = subprocess.run([sys.executable, '-c', code, p], timeout=20)
        fcntl.flock(fh, fcntl.LOCK_UN)
    try:
        os.remove(p)
    except OSError:
        pass
    return r.returncode == 0


def fs_type(path):
    """Filesystem type of PATH on Linux (from /proc/mounts), else None."""
    if not procs.LINUX:
        return None
    best, typ = '', None
    real = os.path.realpath(path)
    try:
        with open('/proc/mounts') as fh:
            for line in fh:
                f = line.split()
                if len(f) >= 3:
                    mnt = f[1].replace('\\040', ' ')
                    if (real == mnt or real.startswith(mnt.rstrip('/') + '/')) and len(mnt) > len(best):
                        best, typ = mnt, f[2]
    except OSError:
        return None
    return typ


def _state_source():
    if core.env('STATE_DIR'):
        return '$%s_STATE_DIR' % ENV
    cur = os.getcwd()
    while True:
        if os.path.isdir(os.path.join(cur, PROJECT_MARKER)):
            return 'project marker'
        parent = os.path.dirname(cur)
        if parent == cur:
            return 'user default'
        cur = parent


def _project_root():
    d = core.state_dir()
    if os.path.basename(d) == PROJECT_MARKER:
        return os.path.dirname(d)
    return os.getcwd()


def _hook_files(root):
    return [os.path.join(root, '.claude', 'settings.json'), os.path.join(root, '.claude', 'settings.local.json'),
            os.path.expanduser('~/.claude/settings.json')]


def find_hooks(root):
    """[(settings_file, command)] for every agentlanes Claude Code hook found."""
    found = []
    for p in _hook_files(root):
        s = core.load_json(p, None)
        if not isinstance(s, dict):
            continue
        for group in ((s.get('hooks') or {}).get('PreToolUse') or []) if isinstance(s.get('hooks'), dict) else []:
            for h in (group.get('hooks') or []) if isinstance(group, dict) else []:
                if _is_our_hook(h):
                    found.append((p, h['command']))
    return found


def _hook_runs(command):
    """Feed the hook a harmless Edit on an unclaimed path; it must exit 0 with no deny."""
    probe = json.dumps({'hook_event_name': 'PreToolUse', 'tool_name': 'Edit',
                        'tool_input': {'file_path': os.path.join(core.state_dir(), '.doctor-probe')}})
    try:
        r = subprocess.run(command, shell=True, input=probe, capture_output=True, text=True, timeout=20)
    except Exception as e:
        return False, str(e)
    if r.returncode != 0:
        return False, 'exit %d: %s' % (r.returncode, (r.stderr or r.stdout).strip()[:200])
    if 'deny' in r.stdout:
        return False, 'denied an unclaimed path: %s' % r.stdout.strip()[:200]
    return True, ''


def _mcp_answers(entry):
    """Start a registered MCP server entry and ask it `initialize`. Returns (ok, why)."""
    argv = [entry.get('command', '')] + list(entry.get('args') or [])
    if not shutil.which(argv[0]):
        return False, '%s not found' % argv[0]
    req = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                      'params': {'protocolVersion': '2025-06-18', 'capabilities': {},
                                 'clientInfo': {'name': 'doctor', 'version': __version__}}}) + '\n'
    try:
        r = subprocess.run(argv, input=req, capture_output=True, text=True, timeout=10,
                           env=dict(os.environ, **(entry.get('env') or {})))
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    try:
        rep = json.loads(r.stdout.splitlines()[0])
        return rep['result']['serverInfo']['name'] == NAME, ''
    except (ValueError, IndexError, KeyError, TypeError):
        return False, (r.stderr.strip().splitlines() or ['no answer'])[-1]


def doctor(out=print):
    """Print one line per check. Exit 1 if any check FAILs (warnings do not fail)."""
    results = []

    def rep(level, what, fix=None):
        results.append(level)
        out('%-5s %s' % (level, what))
        if fix:
            out('      fix: %s' % fix)

    rep('info', '%s %s, Python %s, %s' % (NAME, __version__, sys.version.split()[0], sys.platform))
    exe = shutil.which(NAME)
    if exe:
        rep('ok', 'on PATH: %s' % exe)
    else:
        rep('FAIL', '`%s` is not on PATH, so agents and hooks cannot call it' % NAME,
            'pip install . (or pipx install .), or add the bin/ directory of this checkout to PATH')
    # state dir
    try:
        state = core.state_dir()
        rep('ok', 'state directory %s (%s)' % (state, _state_source()))
        probe = os.path.join(state, '.doctor-write')
        with open(probe, 'w') as fh:
            fh.write('x')
        os.remove(probe)
        rep('ok', 'state directory is writable')
    except OSError as e:
        state = None
        rep('FAIL', 'state directory not usable: %s' % e,
            'set %s_STATE_DIR to a writable local directory, or run `%s init` in the project' % (ENV, NAME))
    if state:
        typ = fs_type(state)
        if typ and (typ.startswith('nfs') or typ in ('cifs', 'smb3', 'smbfs', 'fuse.sshfs', '9p', 'virtiofs')):
            rep('warn', 'state directory is on %s; flock there may not exclude other machines or may not work' % typ,
                'use a local disk for %s_STATE_DIR' % ENV)
        try:
            if _flock_excludes(state):
                rep('ok', 'flock excludes a second process')
            else:
                rep('FAIL', 'flock does not exclude a second process on this filesystem',
                    'use a local disk for %s_STATE_DIR' % ENV)
        except Exception as e:
            rep('FAIL', 'flock test failed: %s' % e, 'use a local disk for %s_STATE_DIR' % ENV)
        claims_f = os.path.join(state, 'claims.json')
        if os.path.exists(claims_f):
            try:
                with open(claims_f, encoding='utf-8') as fh:
                    json.load(fh)
                rep('ok', 'claims.json parses')
            except ValueError as e:
                rep('FAIL', 'claims.json is corrupt (%s); claims in it are ignored' % e,
                    'move it aside: mv %s %s.bad' % (shlex.quote(claims_f), shlex.quote(claims_f)))
    # process inspection
    if procs.LINUX:
        rep('ok', 'process inspection via /proc')
    elif shutil.which('ps'):
        tok = procs.start_token(os.getpid())
        if tok:
            rep('ok', 'process inspection via ps (start time: %s)' % tok)
        else:
            rep('FAIL', '`ps -o lstart=` gave nothing; leases cannot detect recycled pids')
    else:
        rep('FAIL', 'no /proc and no `ps`: leases (--pid) cannot work')
    apid, kind = procs.agent_self()
    rep('info', 'enclosing coding agent: %s' % ('%s pid %d' % (kind, apid) if apid else
                                              'none found (fine in a plain shell; `--pid self` needs one)'))
    # kernel-enforced write lanes
    from . import landlock
    lvl, txt = landlock.describe_abi()
    rep(lvl, txt, None if lvl != 'warn' else '`%s run` refuses without Landlock (or runs unconfined with '
        '--best-effort); hooks still apply' % NAME)
    # memory caps
    from . import slots
    if slots._systemd_scope_ok():
        rep('ok', 'slot --max-mem uses a systemd user scope (MemoryMax)')
    else:
        rep('info', 'slot --max-mem uses the RSS watchdog (no systemd user session%s)'
            % ('' if sys.platform.startswith('linux') else ' on this OS'))
    m = procs.mem_available_mb()
    rep('ok' if m is not None else 'warn', 'available memory: %s' % ('%d MB' % m if m is not None else 'unknown'),
        None if m is not None else 'slot --mem-free will not wait')
    # hook
    root = _project_root()
    hooks = find_hooks(root)
    if not hooks:
        rep('warn', 'no Claude Code hook found in %s' % ', '.join(_hook_files(root)),
            '`%s init --claude-code` (only needed if you use Claude Code)' % NAME)
    for p, cmd in hooks:
        ok, why = _hook_runs(cmd)
        if ok:
            rep('ok', 'Claude Code hook in %s runs%s' % (p, ' (strict)' if '_STRICT=1' in cmd else ' (fail-open)'))
        else:
            rep('FAIL', 'Claude Code hook in %s does not run: %s' % (p, why),
                'make sure `%s` is on PATH for Claude Code, or rerun `%s init --claude-code`' % (NAME, NAME))
    # MCP (project scope)
    mp = os.path.join(root, '.mcp.json')
    entry = None
    try:
        with open(mp, encoding='utf-8') as fh:
            entry = (json.load(fh).get('mcpServers') or {}).get(NAME)
    except (OSError, ValueError, AttributeError):
        pass
    if entry is None:
        rep('info', 'no %s MCP server in %s (`%s init --mcp`, or the Claude Code plugin)' % (NAME, mp, NAME))
    else:
        ok, why = _mcp_answers(entry if isinstance(entry, dict) else {})
        if ok:
            rep('ok', 'MCP server in %s answers initialize' % mp)
        else:
            rep('FAIL', 'MCP server in %s does not answer: %s' % (mp, why), 'rerun `%s init --mcp`' % NAME)
    from .hook import strict
    rep('info', 'strict mode in this shell (%s_STRICT): %s' % (ENV, 'on' if strict() else 'off, hooks fail open'))
    bad = results.count('FAIL')
    out('%d problem(s)' % bad if bad else 'all checks passed')
    return 1 if bad else 0
