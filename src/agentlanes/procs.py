"""Process helpers that work on Linux (/proc) and macOS (ps). No daemon, no state."""
import os
import re
import subprocess
import sys

from . import core

# $AGENTLANES_NO_PROC=1 makes Linux use the portable `ps` code paths that macOS uses (for tests and odd systems).
LINUX = (sys.platform.startswith('linux') and os.path.isdir('/proc/self')
         and not os.environ.get(core.ENV + '_NO_PROC'))

# Executable names of common coding-agent CLIs. Extend with AGENTLANES_AGENT_NAMES=name1,name2.
KNOWN_AGENTS = {'claude', 'codex', 'cursor-agent', 'aider', 'gemini', 'opencode', 'goose', 'amp', 'crush', 'qwen',
                'copilot', 'cline', 'kiro'}
ALIASES = {'claude-code': 'claude', 'gemini-cli': 'gemini', 'qwen-code': 'qwen'}
INTERPRETERS = re.compile(r'^(node|nodejs|bun|deno|python[0-9.]*)$')


def _read(p):
    try:
        with open(p, 'rb') as fh:
            return fh.read().decode('utf-8', 'replace')
    except OSError:
        return ''


# `ps` output must not depend on the caller's locale: start tokens written by one agent are compared by another.
PS_ENV = dict(os.environ, LC_ALL='C', LANG='C')


def _ps(pid, *fields):
    """Run `ps -o f1= -o f2= ... -p PID` (one -o per field: portable to BSD/macOS and procps, never a header)."""
    argv = ['ps']
    for f in fields:
        argv += ['-o', f + '=']
    try:
        r = subprocess.run(argv + ['-p', str(int(pid))], capture_output=True, text=True, timeout=5, env=PS_ENV)
        return r.stdout.strip() if r.returncode == 0 else ''
    except Exception:
        return ''


def pid_alive(pid):
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        os.kill(pid, 0)
    except (ValueError, TypeError, ProcessLookupError, OverflowError):
        return False
    except PermissionError:
        return True
    if LINUX:
        st = _read('/proc/%d/stat' % pid)
        return bool(st) and st.rsplit(')', 1)[-1].split()[:1] != ['Z']
    return not _ps(pid, 'stat').startswith('Z')   # kill(0) said it exists; an empty answer (ps failed) is not death


def start_token(pid):
    """Opaque process start time, used to notice a recycled pid."""
    if LINUX:
        st = _read('/proc/%d/stat' % int(pid))
        return st.rsplit(')', 1)[1].split()[19] if st else None
    return _ps(pid, 'lstart') or None


def same_process(pid, token):
    if not pid_alive(pid):
        return False
    if not token:
        return True
    now = start_token(pid)
    return now is None or now == token   # unknown (ps/proc read failed): keep the lease rather than drop a live one


def parent_and_argv(pid):
    if LINUX:
        st = _read('/proc/%d/stat' % pid)
        if not st:
            return None, []
        ppid = int(st.rsplit(')', 1)[1].split()[1])
        argv = [a for a in _read('/proc/%d/cmdline' % pid).split('\0') if a]
        if not argv:
            argv = [st[st.find('(') + 1:st.rfind(')')]]
        return ppid, argv
    out = _ps(pid, 'ppid', 'args')
    f = out.split(None, 1)
    try:
        ppid = int(f[0])
    except (IndexError, ValueError):
        return None, []
    return ppid, f[1].split() if len(f) > 1 else []


def known_agents():
    extra = core.env('AGENT_NAMES', '')
    return KNOWN_AGENTS | {x.strip() for x in extra.split(',') if x.strip()}


def agent_kind(argv, names=None):
    """'claude' / 'codex' / ... if argv looks like a coding-agent CLI, else None."""
    if not argv:
        return None
    names = names or known_agents()

    def match(word):
        word = re.sub(r'\.(js|mjs|cjs|py|exe)$', '', os.path.basename(word))
        word = ALIASES.get(word, word)
        return word if word in names else None
    hit = match(argv[0])
    if hit:
        return hit
    if INTERPRETERS.match(os.path.basename(argv[0])) and len(argv) > 1:
        script = next((a for a in argv[1:] if not a.startswith('-')), '')
        for comp in reversed(script.split('/')):
            hit = match(comp.lstrip('@'))
            if hit:
                return hit
    return None


def agent_self():
    """(pid, kind) of the agent process this command runs under: $AGENTLANES_AGENT_PID, else nearest agent ancestor."""
    forced = core.env('AGENT_PID')
    if forced:
        return int(forced), core.env('AGENT') or 'agent'
    names = known_agents()
    pid, hops = os.getppid(), 0
    while pid and pid > 1 and hops < 64:
        ppid, argv = parent_and_argv(pid)
        if ppid is None:
            return None, None
        k = agent_kind(argv, names)
        if k:
            return pid, k
        pid, hops = ppid, hops + 1
    return None, None


def default_by():
    """Who am I: $AGENTLANES_AGENT, else '<agent>-<pid>' of the enclosing agent, else $USER."""
    name = core.env('AGENT')
    if name:
        return name
    pid, kind = agent_self()
    if pid:
        return '%s-%d' % (kind, pid)
    return os.environ.get('USER') or 'user'


def _etime(s):
    """ps etime '[[dd-]hh:]mm:ss' -> seconds."""
    days, _, rest = s.rpartition('-')
    parts = [int(x) for x in rest.split(':')]
    while len(parts) < 3:
        parts.insert(0, 0)
    return (int(days) if days else 0) * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]


def process_table():
    """{pid: {'ppid', 'rss_kb', 'age_s', 'argv'}} for every process (one `ps` call)."""
    try:
        argv = ['ps', '-ax']
        for f in ('pid', 'ppid', 'rss', 'etime', 'args'):
            argv += ['-o', f + '=']
        out = subprocess.run(argv, capture_output=True, text=True, timeout=10, env=PS_ENV).stdout
    except Exception:
        return {}
    table = {}
    for line in out.splitlines():
        f = line.split(None, 4)
        if len(f) < 4:
            continue
        try:
            table[int(f[0])] = {'ppid': int(f[1]), 'rss_kb': int(f[2]), 'age_s': _etime(f[3]),
                                'argv': f[4].split() if len(f) > 4 else []}
        except ValueError:
            continue
    return table


def tree_rss_kb(root, table):
    kids = {}
    for p, i in table.items():
        kids.setdefault(i['ppid'], []).append(p)
    tot, stack, seen = 0, [root], set()
    while stack:
        p = stack.pop()
        if p in seen:
            continue
        seen.add(p)
        tot += table.get(p, {}).get('rss_kb', 0)
        stack.extend(kids.get(p, ()))
    return tot


def list_agents(include_sub=False):
    table = process_table()
    names = known_agents()
    agents = {p: agent_kind(i['argv'], names) for p, i in table.items()}
    agents = {p: k for p, k in agents.items() if k}
    out = []
    for p, k in sorted(agents.items()):
        anc, parent_agent = table[p]['ppid'], None
        hops = 0
        while anc in table and anc > 1 and hops < 64:
            if anc in agents:
                parent_agent = anc
                break
            anc, hops = table[anc]['ppid'], hops + 1
        if parent_agent and not include_sub:
            continue
        cwd = process_cwd(p)
        out.append({'pid': p, 'kind': k, 'parent_agent': parent_agent, 'age_s': table[p]['age_s'],
                    'mem_mb': tree_rss_kb(p, table) // 1024, 'cwd': cwd,
                    'headless': any(a in ('-p', '--print', 'exec') for a in table[p]['argv'][1:3])})
    return out


def process_cwd(pid):
    """Working directory of PID: /proc on Linux, `lsof` elsewhere (macOS). None if unknown."""
    if LINUX:
        try:
            return os.readlink('/proc/%d/cwd' % pid)
        except OSError:
            return None
    try:
        out = subprocess.run(['lsof', '-a', '-p', str(int(pid)), '-d', 'cwd', '-Fn'], capture_output=True, text=True,
                             timeout=5, env=PS_ENV).stdout
    except Exception:
        return None
    return next((ln[1:] for ln in out.splitlines() if ln.startswith('n')), None)


def parse_vm_stat(out):
    """macOS `vm_stat` text -> available MB (free + inactive + speculative pages), or None."""
    try:
        page = int(re.search(r'page size of (\d+)', out).group(1))
        pages = sum(int(re.search(r'%s:\s+(\d+)' % k, out).group(1))
                    for k in ('Pages free', 'Pages inactive', 'Pages speculative'))
        return pages * page // 1048576
    except (AttributeError, ValueError):
        return None


def mem_available_mb():
    if LINUX:
        m = re.search(r'MemAvailable:\s+(\d+)', _read('/proc/meminfo'))
        return int(m.group(1)) // 1024 if m else None
    try:
        out = subprocess.run(['vm_stat'], capture_output=True, text=True, timeout=5, env=PS_ENV).stdout
    except Exception:
        return None
    return parse_vm_stat(out)
