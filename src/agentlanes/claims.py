"""Claims: named work leases with optional path guards.

A claim says "agent X is working on TOPIC (and these paths) until T". With --pid it is a lease: it disappears
as soon as that process exits, so a crashed agent never blocks the others. Overlapping path claims are refused
unless --share is given. State: <state>/claims.json, guarded by flock; every change goes to the event log.
"""
import json
import os
import time

from . import core, procs


def claims_file():
    return core.path('claims.json')


def norm(p):
    return os.path.realpath(os.path.expanduser(p.strip())).rstrip('/') or '/'


def overlaps(a, b):
    return a == b or a.startswith(b.rstrip('/') + '/') or b.startswith(a.rstrip('/') + '/')


def _dead(c):
    return bool(c.get('pid')) and not procs.same_process(c['pid'], c.get('pstart'))


def _split(d):
    now = time.time()
    live, dropped = {}, {}
    for k, c in d.items():
        if not isinstance(c, dict):
            continue
        if c.get('until', 0) <= now:
            dropped[k] = (c, 'expire')
        elif _dead(c):
            dropped[k] = (c, 'reap')
        else:
            live[k] = c
    return live, dropped


class Store:
    """with Store() as s: s.claims is the live dict; changes are saved and events emitted on exit."""

    def __enter__(self):
        self.file = claims_file()
        self._lock = core.locked(self.file)
        self._lock.__enter__()
        raw = core.load_json(self.file, {})
        self.claims, self.dropped = _split(raw if isinstance(raw, dict) else {})
        self.changed = bool(self.dropped)
        self.events = [('claim.' + why, dict(by=c.get('by'), topic=k, msg=c.get('desc', '')[:120], pid=c.get('pid')))
                       for k, (c, why) in self.dropped.items()]
        return self

    def __exit__(self, *exc):
        try:
            if self.changed and exc[0] is None:
                core.save_json(self.file, self.claims)
        finally:
            self._lock.__exit__(*exc)
        for kind, kw in self.events:
            core.emit(kind, **kw)
        return False


def snapshot():
    """Live claims without taking the lock for long (read-only helpers: status, hook, agents)."""
    with Store() as s:
        return dict(s.claims)


def holders_of(path, claims=None):
    p = norm(path)
    claims = snapshot() if claims is None else claims
    return [(k, c, q) for k, c in claims.items() for q in c.get('paths', []) if overlaps(p, q)]


def resolve_pid(spec):
    """'self' -> enclosing agent pid; number -> that pid. Returns (pid, error)."""
    if spec == 'self':
        pid, _ = procs.agent_self()
        if not pid:
            return None, ('--pid self: no coding-agent process found above this shell; pass --pid PID '
                          'or set %s_AGENT_PID' % core.ENV)
        return pid, None
    try:
        pid = int(spec)
    except ValueError:
        return None, 'bad --pid: %s' % spec
    if not procs.pid_alive(pid):
        return None, 'no such process: %s' % spec
    return pid, None


def add(topic, by, desc='', ttl_min=120, pid=None, paths=(), share=False, renew=False, out=print):
    """Returns exit code: 0 claimed, 1 held/conflict, 2 usage error."""
    lease_pid = None
    if pid:
        lease_pid, err = resolve_pid(pid)
        if err:
            out(err)
            return 2
    with Store() as s:
        cur = s.claims.get(topic)
        if cur and cur['by'] != by:
            out('HELD by %s since %s: %s' % (cur['by'], core.hm(cur['since']), cur.get('desc', '')))
            s.events.append(('claim.conflict', dict(by=by, topic=topic, holder=cur['by'])))
            return 1
        if renew and not cur:
            out('not held by you')
            return 1
        paths = [norm(p) for p in paths if p.strip()] or (cur or {}).get('paths', [])
        if paths and not share:
            for k, c in s.claims.items():
                if k == topic or c.get('by') == by:
                    continue
                for p in paths:
                    for q in c.get('paths', []):
                        if overlaps(p, q):
                            out('PATH HELD by %s (%s, since %s): %s -- use --share to work there anyway'
                                % (c['by'], k, core.hm(c['since']), q))
                            s.events.append(('claim.conflict', dict(by=by, topic=topic, holder=c['by'],
                                                                    holder_topic=k, path=q)))
                            return 1
        now = time.time()
        n = dict(cur or {})
        n.update(by=by, desc=desc or (cur or {}).get('desc', ''), since=(cur or {}).get('since', now),
                 until=now + ttl_min * 60)
        if paths:
            n['paths'] = paths
        if lease_pid:
            n['pid'], n['pstart'] = lease_pid, procs.start_token(lease_pid)
        apid, kind = procs.agent_self()
        if apid:
            n['apid'], n['agent'] = apid, kind
        sid = core.env('SESSION')
        if sid:
            n['sid'] = sid
        s.claims[topic] = n
        s.changed = True
        s.events.append(('claim.renew' if cur else 'claim.add',
                         dict(by=by, topic=topic, msg=n['desc'][:120], ttl=ttl_min, pid=n.get('pid'),
                              paths=','.join(paths) or None)))
        out('claimed %s' % topic)
        return 0


def release(topic, by=None, out=print):
    with Store() as s:
        c = s.claims.get(topic)
        if c and (not by or c['by'] == by):
            del s.claims[topic]
            s.changed = True
            s.events.append(('claim.done', dict(by=c['by'], topic=topic, held_s=int(time.time() - c['since']))))
            out('released %s' % topic)
            return 0
        out('not held by you' if c else 'not held')
        return 1


def fmt(k, c):
    extra = ''
    if c.get('paths'):
        extra += ' [paths: %s]' % ', '.join(c['paths'])
    if c.get('pid'):
        extra += ' [lease pid %s]' % c['pid']
    return '%s -- %s since %s, %s left: %s%s' % (k, c['by'], core.hm(c['since']), core.dur(c['until'] - time.time()),
                                                c.get('desc', ''), extra)


def cmd_list(as_json=False, out=print):
    with Store() as s:
        d = dict(s.claims)
    if as_json:
        out(json.dumps(d, ensure_ascii=False, indent=1))
    elif not d:
        out('no active claims')
    else:
        for k, c in sorted(d.items(), key=lambda x: x[1]['since']):
            out(fmt(k, c))
    return 0


def cmd_check(topic, out=print):
    c = snapshot().get(topic)
    if c:
        out('HELD by %s: %s' % (c['by'], c.get('desc', '')))
        return 1
    out('free')
    return 0


def cmd_who(paths, out=print):
    d = snapshot()
    rc = 0
    for p in paths:
        hits = holders_of(p, d)
        for k, c, q in hits:
            out('HELD by %s (%s, since %s): %s -- %s' % (c['by'], k, core.hm(c['since']), q, c.get('desc', '')))
        if hits:
            rc = 1
        else:
            out('free: %s' % norm(p))
    return rc


def cmd_reap(out=print):
    with Store() as s:
        n = sum(1 for _, why in s.dropped.values() if why == 'reap')
        out('reaped %d dead lease(s), expired %d' % (n, len(s.dropped) - n))
    return 0
