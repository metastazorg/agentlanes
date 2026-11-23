"""Jobs: a small shared work queue. Agents add work, take the next item, and mark it done.

A taken job is tied to the taker's process (--pid, default: the enclosing agent). If that process dies the job
goes back to the queue (event job.requeue), so work is never lost with a crashed agent.
State: <state>/jobs.json under flock.
"""
import json
import time

from . import core, procs


def jobs_file():
    return core.path('jobs.json')


def _load():
    q = core.load_json(jobs_file(), {})
    if not isinstance(q, dict) or 'items' not in q:
        q = {'next': 1, 'items': []}
    events = []
    for j in q['items']:
        if j['state'] == 'taken' and j.get('pid') and not procs.same_process(j['pid'], j.get('pstart')):
            events.append(('job.requeue', dict(by=j.get('taken_by'), topic='#%d' % j['id'], msg=j['title'])))
            j.update(state='waiting', requeued=j.get('requeued', 0) + 1)
            for k in ('pid', 'pstart', 'taken_by', 'taken_at'):
                j.pop(k, None)
    return q, events


def _locked(fn):
    with core.locked(jobs_file()):
        q, events = _load()
        res, changed = fn(q)
        if changed or events:
            core.save_json(jobs_file(), q)
    for kind, kw in events:
        core.emit(kind, **kw)
    return res


def _waiting(q):
    return sorted((j for j in q['items'] if j['state'] == 'waiting'), key=lambda j: (j['prio'], j['id']))


def add(title, prio=50, note=None, by=None, out=print):
    by = by or procs.default_by()

    def fn(q):
        j = {'id': q['next'], 'title': title, 'prio': prio, 'note': note, 'state': 'waiting', 'added': time.time(),
             'by': by}
        q['next'] += 1
        q['items'].append(j)
        return j, True
    j = _locked(fn)
    core.emit('job.add', by=by, topic='#%d' % j['id'], msg=title, prio=prio)
    out('queued #%d' % j['id'])
    return 0


def take(job_id=None, by=None, pid='self', out=print):
    by = by or procs.default_by()
    lease_pid = None
    if pid and pid != 'none':
        from .claims import resolve_pid
        lease_pid, err = resolve_pid(pid)
        if err:
            out(err)
            return 2

    def fn(q):
        cand = [j for j in _waiting(q) if job_id is None or j['id'] == job_id]
        if not cand:
            return None, False
        j = cand[0]
        j.update(state='taken', taken_by=by, taken_at=time.time())
        if lease_pid:
            j.update(pid=lease_pid, pstart=procs.start_token(lease_pid))
        return dict(j), True
    j = _locked(fn)
    if not j:
        out('nothing to take')
        return 1
    core.emit('job.take', by=by, topic='#%d' % j['id'], msg=j['title'])
    out('took #%d: %s' % (j['id'], j['title']))
    return 0


def finish(job_id, state, by=None, text=None, out=print):
    """state: done | dropped."""
    by = by or procs.default_by()

    def fn(q):
        j = next((x for x in q['items'] if x['id'] == job_id), None)
        if not j or j['state'] in ('done', 'dropped'):
            return None, False
        j.update(state=state, finished_by=by, finished_at=time.time(), result=text)
        j.pop('pid', None)
        return dict(j), True
    j = _locked(fn)
    if not j:
        out('no open job #%d' % job_id)
        return 1
    core.emit('job.' + ('done' if state == 'done' else 'drop'), by=by, topic='#%d' % job_id, msg=text or j['title'])
    out('%s #%d' % (state, job_id))
    return 0


def items(include_all=False):
    q = _locked(lambda q: (q, False))
    its = q['items'] if include_all else [j for j in q['items'] if j['state'] in ('waiting', 'taken')]
    order = {'taken': 0, 'waiting': 1, 'done': 2, 'dropped': 3}
    return sorted(its, key=lambda j: (order.get(j['state'], 9), j['prio'], j['id']))


def cmd_list(include_all=False, as_json=False, out=print):
    its = items(include_all)
    if as_json:
        out(json.dumps(its, ensure_ascii=False, indent=1))
        return 0
    if not its:
        out('queue empty')
    for j in its:
        who = ' (%s)' % j.get('taken_by') if j['state'] == 'taken' else ''
        out('#%-4d p%-3d %-8s %s%s' % (j['id'], j['prio'], j['state'], j['title'], who))
    return 0
