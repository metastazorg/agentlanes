"""`agentlanes run`: start a command whose writes the Linux kernel confines to the agent's lanes (Landlock).

Writable: paths of the agent's claims (or --claim NAMEs), the agentlanes state directory, an optional private temp
dir and explicit --write paths. Everything else stays readable and executable, so tools keep working.

The ruleset is fixed when the command starts. A claim taken later does not widen it: restart the agent, or run the
sub-task under a new `agentlanes run` (nesting only ever narrows).
"""
import argparse
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from . import ENV, NAME, claims, core, landlock, procs

# --allow-common: per-user places coding agents and build tools write to (caches, their own config/session state).
# Only the paths that exist are added. Extend with AGENTLANES_RUN_COMMON=path1:path2.
COMMON = ['~/.cache', '~/.claude', '~/.claude.json', '~/.codex', '~/.cursor', '~/.gemini', '~/.aider',
          '~/.config/claude', '~/.config/gh', '~/.npm', '~/.local/state', '~/.local/share/claude']

USAGE = '%s run [--claim NAME]... [--write PATH]... [--allow-tmp] [--allow-common] [--no-net] [--allow-port N]... ' \
        '[--isolate] [--best-effort] [--by WHO] [--dry-run] -- COMMAND...' % NAME


def parser():
    a = argparse.ArgumentParser(prog='%s run' % NAME, usage=USAGE, description=__doc__.split('\n\n')[0])
    a.add_argument('--claim', action='append', default=[], metavar='NAME',
                   help='make this claim\'s paths writable (repeatable). Default: every claim held by --by')
    a.add_argument('--write', action='append', default=[], metavar='PATH', help='extra writable path (repeatable)')
    a.add_argument('--allow-tmp', action='store_true', help='create a private temp dir, export TMPDIR, delete it after')
    a.add_argument('--allow-common', action='store_true',
                   help='also allow ~/.cache, agent config/session dirs (~/.claude, ~/.codex, ...) that exist')
    a.add_argument('--no-net', action='store_true', help='deny TCP connect/bind (Landlock ABI 4+)')
    a.add_argument('--allow-port', action='append', default=[], type=int, metavar='N',
                   help='with network restriction, allow TCP connect to port N (repeatable; implies --no-net)')
    a.add_argument('--allow-bind', action='append', default=[], type=int, metavar='N',
                   help='allow listening on TCP port N (repeatable; implies --no-net)')
    a.add_argument('--isolate', action='store_true',
                   help='block signals and abstract unix sockets to processes outside the run (Landlock ABI 6+)')
    a.add_argument('--best-effort', action='store_true',
                   help='if Landlock is unavailable, run the command unconfined (with a warning) instead of refusing')
    a.add_argument('--by', help='whose claims to use (default: $%s_AGENT / enclosing agent)' % ENV)
    a.add_argument('--dry-run', action='store_true', help='print the ruleset and exit')
    a.add_argument('-q', '--quiet', action='store_true', help='no summary line on stderr')
    return a


def _err(msg):
    print('%s run: %s' % (NAME, msg), file=sys.stderr)


def claim_paths(names, by):
    """(paths, used_claim_names, error)."""
    live = claims.snapshot()
    if names:
        missing = [n for n in names if n not in live]
        if missing:
            return None, None, 'no live claim named %s (see `%s list`)' % (', '.join(missing), NAME)
        chosen = {n: live[n] for n in names}
        for n, c in chosen.items():
            if c.get('by') != by:
                _err('note: claim %s is held by %s, not %s' % (n, c.get('by'), by))
    else:
        chosen = {k: c for k, c in live.items() if c.get('by') == by}
    paths = []
    for n, c in chosen.items():
        if not c.get('paths'):
            _err('note: claim %s has no paths, so it adds nothing writable' % n)
        paths.extend(c.get('paths', []))
    return paths, sorted(chosen), None


def main(argv):
    if '--' not in argv:
        print('usage: ' + USAGE, file=sys.stderr)
        return 2
    i = argv.index('--')
    o = parser().parse_args(argv[:i])
    cmd = argv[i + 1:]
    if not cmd:
        print('usage: ' + USAGE, file=sys.stderr)
        return 2
    by = o.by or procs.default_by()
    state = core.state_dir()

    paths, used, err = claim_paths(o.claim, by)
    if err:
        _err(err)
        return 1
    for p in o.write:
        if not os.path.exists(os.path.expanduser(p)):
            _err('--write path does not exist: %s (create it first; a rule needs an existing file or directory)' % p)
            return 2
    if not paths and not o.write:
        _err('nothing to make writable: %s holds no claims with paths and no --write was given '
             '(claim first: `%s claim TOPIC --paths DIR`, or pass --write DIR)' % (by, NAME))
        return 2

    tmp = None
    if o.allow_tmp:
        base = os.environ.get('XDG_RUNTIME_DIR') if os.path.isdir(os.environ.get('XDG_RUNTIME_DIR', '')) else None
        tmp = tempfile.mkdtemp(prefix='%s-run-' % NAME, dir=base)
    common = []
    if o.allow_common:
        extra = [x for x in core.env('RUN_COMMON', '').split(':') if x]
        common = [os.path.expanduser(p) for p in COMMON + extra if os.path.exists(os.path.expanduser(p))]

    write = paths + list(o.write) + [state] + ([tmp] if tmp else []) + common
    policy = landlock.Policy(write, no_net=o.no_net, connect_ports=o.allow_port, bind_ports=o.allow_bind,
                             isolate=o.isolate)

    try:
        abi = landlock.abi_version()
    except landlock.Unsupported as e:
        abi = 0
        if not o.best_effort:
            _err('refusing to run unconfined: %s. Use --best-effort to run anyway (hooks still apply), or use '
                 'a Linux 5.13+ kernel with Landlock enabled.' % e)
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
            return 3
        _err('WARNING: %s; running UNCONFINED (--best-effort)' % e)

    plan = landlock.plan(policy, abi) if abi else None
    summ = landlock.summary(plan) if plan else {'abi': 0, 'write': 'unrestricted (no Landlock)'}
    summ['claims'] = used
    for w in (plan or {}).get('warnings', []):
        _err('warning: ' + w)
    if o.dry_run:
        import json
        print(json.dumps(dict(summ, rules=[(p, hex(r)) for p, r in plan['rules']] if plan else []), indent=1))
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
        return 0

    fd = None
    if plan:
        try:
            fd = landlock.build(plan)
        except OSError as e:
            _err('could not build the Landlock ruleset: %s' % e)
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
            return 3

    env = dict(os.environ)
    env['%s_STATE_DIR' % ENV] = state          # the child must find the same (writable) state dir
    env.setdefault('%s_AGENT' % ENV, by)
    env['%s_RUN' % ENV] = '1'
    if tmp:
        env['TMPDIR'] = tmp

    def confine():
        if fd is not None:
            landlock.restrict_self(fd)

    if not o.quiet:
        _err('Landlock ABI %s; writable: %s%s' % (
            abi or 'none', ', '.join(summ['write']) if isinstance(summ['write'], list) else summ['write'],
            '; tcp: ' + summ['tcp'] if summ.get('tcp', 'unrestricted') != 'unrestricted' else ''))
    t0 = time.time()
    try:
        child = subprocess.Popen(cmd, env=env, preexec_fn=confine)
    except OSError as e:
        _err('cannot start %s: %s' % (cmd[0], e))
        rc = 127
        child = None
    except subprocess.SubprocessError as e:
        _err('could not apply Landlock in the child: %s' % e)
        rc = 3
        child = None
    finally:
        if fd is not None:
            os.close(fd)
    if child:
        core.emit('run.start', by=by, pid=child.pid, cmd=shlex.join(cmd)[:200], **{
            k: (','.join(v) if isinstance(v, list) else v) for k, v in summ.items()})
        old = {s: signal.signal(s, signal.SIG_IGN) for s in (signal.SIGINT, signal.SIGQUIT)}

        def forward(sig, _frame):
            try:
                child.send_signal(sig)
            except OSError:
                pass
        old[signal.SIGTERM] = signal.signal(signal.SIGTERM, forward)
        old[signal.SIGHUP] = signal.signal(signal.SIGHUP, forward)
        try:
            rc = child.wait()
        finally:
            for s, h in old.items():
                signal.signal(s, h)
        rc = 128 - rc if rc < 0 else rc
        core.emit('run.stop', by=by, pid=child.pid, rc=rc, dur_s=round(time.time() - t0, 1))
    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)
    return rc
