"""Command-line interface. Exit codes: 0 ok, 1 held/conflict/not found, 2 usage, 75 slot timeout, 124 wait timeout."""
import argparse
import collections
import json
import os
import sys
import time

from . import NAME, __version__, claims, core, hook, jobs, onboard, procs, slots

USAGE = """{n} {v} -- coordination for several coding agents on one machine

  {n} claim TOPIC [--desc TEXT] [--paths a,b] [--pid self|PID] [--ttl MIN] [--share] [--by WHO]
  {n} release TOPIC [--by WHO]          {n} renew TOPIC [--ttl MIN]
  {n} who PATH...                       which claim covers these paths (exit 1 if any)
  {n} check TOPIC | list [--json] | reap
  {n} slot NAME N [--wait SEC] [--mem-free MB] [--max-mem MB] -- COMMAND...
  {n} slots [NAME] [--json]             holders, stale holders, waiters
  {n} events emit KIND [--topic T] [--msg TEXT] [key=value...] | tail [-n N] [--kind K] [-f] | stats | wait --kind K
  {n} jobs add TITLE [--prio N] | list [--all] | take [ID] | done ID [--result T] | drop ID [--why T]
  {n} agents [--all] [--json]           running coding-agent processes
  {n} status                            one screen: agents, claims, slots, jobs, recent events
  {n} init [--claude-code [--strict]] [--agents-md] [--claude-md] [--cursor]
                                        set this project up (idempotent; merges, never overwrites)
  {n} run [--claim NAME | --write PATH]... [--allow-tmp] [--allow-common] [--no-net] [--allow-port N] -- CMD...
                                        Linux: start CMD with writes confined to its lanes by the kernel (Landlock)
  {n} mcp                               MCP server on stdio (claude mcp add agentlanes -- agentlanes mcp)
  {n} doctor                            check PATH, state dir, flock, hooks, Landlock; prints fixes
  {n} hook claude-code|codex|cursor     PreToolUse hook for that agent (see examples/)

State: {state}   (override: --state-dir or ${e}_STATE_DIR)
"""


def _by(o):
    return o.by or procs.default_by()


def p_claims(sub):
    for name in ('claim', 'renew'):
        a = sub.add_parser(name)
        a.add_argument('topic')
        a.add_argument('--by')
        a.add_argument('--desc', default='')
        a.add_argument('--ttl', type=int, default=120, help='minutes (default 120)')
        a.add_argument('--pid', help="'self' (enclosing agent) or a pid: claim dies with that process")
        a.add_argument('--paths', default='', help='comma-separated files/dirs')
        a.add_argument('--share', action='store_true')
    a = sub.add_parser('release')
    a.add_argument('topic')
    a.add_argument('--by')
    a.add_argument('--force', action='store_true', help='release even if held by someone else')
    a = sub.add_parser('who')
    a.add_argument('paths', nargs='+')
    a = sub.add_parser('check')
    a.add_argument('topic')
    a = sub.add_parser('list')
    a.add_argument('--json', action='store_true')
    sub.add_parser('reap')


def p_events():
    a = argparse.ArgumentParser(prog='%s events' % NAME)
    a.add_argument('action', choices=['emit', 'tail', 'stats', 'wait'])
    a.add_argument('rest', nargs='*')
    a.add_argument('--by')
    a.add_argument('--topic')
    a.add_argument('--msg')
    a.add_argument('--kind')
    a.add_argument('--since', help='e.g. 30m, 2h, 1d')
    a.add_argument('--timeout', type=float, default=600)
    a.add_argument('-n', type=int, default=30)
    a.add_argument('-f', '--follow', action='store_true')
    a.add_argument('--json', action='store_true')
    return a


def events_cmd(o):
    if o.action == 'emit':
        if not o.rest:
            print('usage: %s events emit KIND [--topic T] [--msg TEXT] [key=value ...]' % NAME, file=sys.stderr)
            return 2
        kv = dict(x.split('=', 1) for x in o.rest[1:] if '=' in x)
        return 0 if core.emit(o.rest[0], by=_by(o), topic=o.topic, msg=o.msg, **kv) else 1
    show = (lambda e: json.dumps(e, ensure_ascii=False)) if o.json else core.fmt_event

    def match(e):
        return ((not o.kind or str(e.get('kind', '')).startswith(o.kind)) and (not o.topic or e.get('topic') == o.topic)
                and (not o.by or e.get('by') == o.by))

    def follow(until=None, first_only=False):
        ev = core.events_file()
        pos = os.path.getsize(ev) if os.path.exists(ev) else 0
        while until is None or time.time() < until:
            try:
                size = os.path.getsize(ev)
            except OSError:
                size = 0
            if size < pos:   # rotated
                pos = 0
            if size > pos:
                with open(ev, encoding='utf-8') as fh:
                    fh.seek(pos)
                    for line in fh:
                        if not line.endswith('\n'):
                            break
                        pos += len(line.encode('utf-8'))
                        try:
                            e = json.loads(line)
                        except ValueError:
                            continue
                        if match(e):
                            print(show(e), flush=True)
                            if first_only:
                                return 0
            time.sleep(0.2)
        return 124

    if o.action == 'wait':
        if not o.kind:
            print('events wait needs --kind', file=sys.stderr)
            return 2
        return follow(time.time() + o.timeout, first_only=True)
    since = time.time() - core.parse_age(o.since) if o.since else None
    if o.action == 'stats':
        since = since or time.time() - 86400
        kinds, actors = collections.Counter(), collections.Counter()
        for e in core.read_events(since=since, kind=o.kind, by=o.by):
            kinds[e.get('kind')] += 1
            actors[e.get('by', '-')] += 1
        if o.json:
            print(json.dumps({'kinds': kinds, 'actors': actors}))
            return 0
        for k, n in kinds.most_common():
            print('%6d  %s' % (n, k))
        print('by: ' + (', '.join('%s %d' % x for x in actors.most_common(12)) or '-'))
        return 0
    for e in [e for e in core.read_events(since=since) if match(e)][-o.n:]:
        print(show(e))
    if o.follow:
        try:
            follow()
        except KeyboardInterrupt:
            pass
    return 0


def p_jobs(sub):
    a = sub.add_parser('jobs')
    a.add_argument('action', choices=['add', 'list', 'take', 'done', 'drop'])
    a.add_argument('arg', nargs='?')
    a.add_argument('--by')
    a.add_argument('--prio', type=int, default=50, help='lower runs sooner (default 50)')
    a.add_argument('--note')
    a.add_argument('--pid', default='self', help="lease for a taken job: 'self', a pid, or 'none'")
    a.add_argument('--result')
    a.add_argument('--why')
    a.add_argument('--all', action='store_true')
    a.add_argument('--json', action='store_true')


def jobs_cmd(o):
    if o.action == 'list':
        return jobs.cmd_list(o.all, o.json)
    if o.action == 'add':
        if not o.arg:
            print('usage: %s jobs add TITLE' % NAME, file=sys.stderr)
            return 2
        return jobs.add(o.arg, o.prio, o.note, o.by)
    jid = int(o.arg.lstrip('#')) if o.arg else None
    if o.action == 'take':
        return jobs.take(jid, o.by, o.pid)
    if jid is None:
        print('usage: %s jobs %s ID' % (NAME, o.action), file=sys.stderr)
        return 2
    return jobs.finish(jid, 'done' if o.action == 'done' else 'dropped', o.by, o.result or o.why)


def agents_rows(include_sub=False):
    rows = procs.list_agents(include_sub)
    cl = claims.snapshot()
    for a in rows:
        a['claims'] = sorted(k for k, c in cl.items() if a['pid'] in (c.get('apid'), c.get('pid')))
    return rows


def agents_cmd(o, out=print):
    rows = agents_rows(o.all)
    if getattr(o, 'json', False):
        out(json.dumps(rows, indent=1))
        return 0
    if not rows:
        out('no coding-agent processes found (known: %s)' % ', '.join(sorted(procs.known_agents())))
        return 0
    out('%-13s %8s %7s %7s  %-6s %s' % ('AGENT', 'PID', 'MEM', 'AGE', 'MODE', 'CLAIMS'))
    for a in rows:
        out('%-13s %8d %6dM %7s  %-6s %s' % (a['kind'], a['pid'], a['mem_mb'], core.dur(a['age_s']),
                                            'batch' if a['headless'] else 'chat', ','.join(a['claims']) or '-'))
    return 0


def _rp(x):
    return os.path.realpath(x).rstrip("/") or "/"


def blame_cmd(o):
    p = os.path.realpath(os.path.expanduser(o.path)).rstrip('/') or '/'
    rows = [e for e in core.read_events(since=time.time() - o.days * 86400)
            if e.get('kind') in ('edit', 'claim.blocked') and e.get('path')
            and (_rp(e['path']) == p or _rp(e['path']).startswith(p + '/'))]
    if not rows:
        print('no recorded edits under %s in the last %g days (edits are recorded by the editor hook)' % (p, o.days))
        return 1
    for e in rows:
        what = 'edit   ' if e['kind'] == 'edit' else 'BLOCKED'
        print('%s  %s  %-14s %-10s %s' % (time.strftime('%m-%d %H:%M:%S', time.localtime(e.get('t', 0))), what,
                                          e.get('by') or '?', e.get('tool') or '', e['path']))
    return 0


def status_cmd(o):
    every = getattr(o, 'watch', None)
    if not every:
        return _status_once()
    every = max(0.5, every)
    try:
        while True:
            sys.stdout.write('\x1b[H\x1b[2J')   # home + clear
            print('%s status  %s  (every %gs, Ctrl-C to stop)' % (NAME, time.strftime('%H:%M:%S'), every))
            _status_once()
            sys.stdout.flush()
            time.sleep(every)
    except KeyboardInterrupt:
        return 0


def _status_once():
    print('== agents')
    agents_cmd(argparse.Namespace(all=False, json=False), out=lambda s: print('  ' + s))
    print('== claims')
    claims.cmd_list(out=lambda s: print('  ' + s))
    print('== slots')
    slots.cmd_status(out=lambda s: print('  ' + s))
    print('== jobs')
    jobs.cmd_list(out=lambda s: print('  ' + s))
    print('== recent events')
    evs = list(core.read_events(since=time.time() - 86400))[-8:]
    for e in evs:
        print('  ' + core.fmt_event(e))
    if not evs:
        print('  none in the last 24 h')
    m = procs.mem_available_mb()
    print('== memory available: %s' % ('%d MB' % m if m is not None else 'unknown'))
    return 0


def slot_cmd(argv):
    """slot NAME N [opts] -- CMD...   (parsed by hand so CMD flags are never touched)."""
    if '--' not in argv:
        print('usage: %s slot NAME N [--wait SEC] [--mem-free MB] [--max-mem MB] [--by WHO] -- COMMAND...' % NAME,
              file=sys.stderr)
        return 2
    i = argv.index('--')
    head, cmd = argv[:i], argv[i + 1:]
    a = argparse.ArgumentParser(prog='%s slot' % NAME)
    a.add_argument('name')
    a.add_argument('n', type=int)
    a.add_argument('--wait', type=float, default=1800, help='seconds (default 1800); exit 75 on timeout')
    a.add_argument('--mem-free', type=int, default=0, help='wait until this many MB are available')
    a.add_argument('--max-mem', type=int, default=0, help='kill CMD above this many MB (systemd scope or watchdog)')
    a.add_argument('--by')
    o = a.parse_args(head)
    return slots.run(o.name, o.n, cmd, o.wait, o.mem_free, o.max_mem, o.by)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    while argv[:1] and argv[0] in ('--state-dir', '--notify-cmd'):
        if len(argv) < 2:
            print('%s needs a value' % argv[0], file=sys.stderr)
            return 2
        key = 'STATE_DIR' if argv[0] == '--state-dir' else 'NOTIFY_CMD'
        os.environ['%s_%s' % (core.ENV, key)] = argv[1]
        argv = argv[2:]
    if not argv or argv[0] in ('-h', '--help', 'help'):
        print(USAGE.format(n=NAME, v=__version__, e=core.ENV, state=core.state_dir()))
        return 0
    if argv[0] in ('-V', '--version'):
        print(NAME, __version__)
        return 0
    if argv[0] == 'slot':
        return slot_cmd(argv[1:])
    if argv[0] == 'mcp':
        from . import mcp
        return mcp.serve()
    if argv[0] == 'run':
        from . import run
        return run.main(argv[1:])
    if argv[0] == 'init':
        a = argparse.ArgumentParser(prog='%s init' % NAME, description='Set up the current directory (idempotent).')
        a.add_argument('--claude-code', action='store_true', help='merge the PreToolUse hook into .claude/settings.json')
        a.add_argument('--strict', action='store_true', help='hook denies edits when %s is missing or broken' % NAME)
        a.add_argument('--agents-md', action='store_true', help='append agent instructions to AGENTS.md')
        a.add_argument('--claude-md', action='store_true', help='append agent instructions to CLAUDE.md')
        a.add_argument('--cursor', action='store_true', help='write .cursor/rules/%s.mdc' % NAME)
        o = a.parse_args(argv[1:])
        return onboard.init(None, o.claude_code, o.agents_md, o.claude_md, o.cursor, o.strict)
    if argv[0] == 'doctor':
        return onboard.doctor()
    if argv[0] == 'events':
        return events_cmd(p_events().parse_intermixed_args(argv[1:]))
    if argv[0] == 'hook':
        fn = hook.ADAPTERS.get(argv[1] if len(argv) > 1 else '')
        if not fn:
            print('usage: %s hook %s' % (NAME, '|'.join(hook.ADAPTERS)), file=sys.stderr)
            return 2
        return fn()

    ap = argparse.ArgumentParser(prog=NAME, usage=USAGE.format(n=NAME, v=__version__, e=core.ENV, state='...'))
    sub = ap.add_subparsers(dest='cmd')
    p_claims(sub)
    p_jobs(sub)
    a = sub.add_parser('slots')
    a.add_argument('name', nargs='?')
    a.add_argument('--json', action='store_true')
    a = sub.add_parser('agents')
    a.add_argument('--all', action='store_true')
    a.add_argument('--json', action='store_true')
    a = sub.add_parser('blame', help='which agents edited PATH (from hook events), newest last')
    a.add_argument('path')
    a.add_argument('--days', type=float, default=30)
    a = sub.add_parser('status')
    a.add_argument('--watch', nargs='?', type=float, const=2.0, default=None, metavar='SEC',
                   help='redraw every SEC seconds (default 2) until Ctrl-C')
    o = ap.parse_args(argv)
    c = o.cmd
    if c in ('claim', 'renew'):
        return claims.add(o.topic, _by(o), o.desc, o.ttl, o.pid, o.paths.split(','), o.share, renew=(c == 'renew'))
    if c == 'release':
        return claims.release(o.topic, None if o.force else _by(o))
    if c == 'who':
        return claims.cmd_who(o.paths)
    if c == 'check':
        return claims.cmd_check(o.topic)
    if c == 'list':
        return claims.cmd_list(o.json)
    if c == 'reap':
        return claims.cmd_reap()
    if c == 'jobs':
        return jobs_cmd(o)
    if c == 'slots':
        return slots.cmd_status(o.name, o.json)
    if c == 'agents':
        return agents_cmd(o)
    if c == 'status':
        return status_cmd(o)
    if c == 'blame':
        return blame_cmd(o)
    ap.print_usage()
    return 2


def entry():
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except BrokenPipeError:
        sys.exit(0)
