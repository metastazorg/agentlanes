"""Slots: an N-slot FIFO queue for a shared resource (heavy builds, browsers, a GPU, a test device).

`slot NAME N -- CMD` waits for one of N slots in arrival order, runs CMD holding it and releases it on exit.
The slot is a flock on <state>/slots/NAME/<i>.lock; the lock fd is inherited by CMD, so the slot stays busy
as long as CMD runs, even if this wrapper is killed, and is freed by the kernel when CMD dies (also on SIGKILL).
A holder record (<i>.who) whose lock is free is reported and cleaned as stale.
Optional memory cap: systemd scope (MemoryMax) when a systemd user session exists, else an RSS watchdog.
"""
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

from . import core, procs

NAME_RE = re.compile(r'^[A-Za-z0-9._-]{1,40}$')
POLL = 0.25


def slot_dir(name):
    d = os.path.join(core.state_dir(), 'slots', name)
    os.makedirs(d, exist_ok=True)
    return d


def _try_lock(p):
    fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except OSError:
        os.close(fd)
        return None


def _lock_free(p):
    fd = _try_lock(p)
    if fd is None:
        return False
    os.close(fd)
    return True


def status(name, clean=True):
    """[(slot_no, state, holder_record_or_None)]; state is free / busy / stale. Stale records are cleaned."""
    d = slot_dir(name)
    out = []
    nums = sorted(int(f[:-5]) for f in os.listdir(d) if f.endswith('.lock') and f[:-5].isdigit())
    for i in nums:
        who = core.load_json(os.path.join(d, '%d.who' % i), None)
        if not _lock_free(os.path.join(d, '%d.lock' % i)):
            out.append((i, 'busy', who or {}))
        elif not who:
            out.append((i, 'free', None))
        else:
            out.append((i, 'stale', who))
            if clean:
                try:
                    os.remove(os.path.join(d, '%d.who' % i))
                except OSError:
                    pass
                core.emit('slot.stale', by=who.get('by'), topic='%s/%d' % (name, i), msg=who.get('cmd'),
                          pid=who.get('pid'))
    return out


def waiters(name):
    q = core.load_json(os.path.join(slot_dir(name), 'queue.json'), [])
    return [w for w in q if procs.same_process(w['pid'], w.get('pstart'))]


def all_names():
    base = os.path.join(core.state_dir(), 'slots')
    try:
        return sorted(n for n in os.listdir(base) if os.path.isdir(os.path.join(base, n)))
    except OSError:
        return []


def _queue(name, fn):
    """Run fn(list) under the queue lock with dead waiters pruned; saves the list fn leaves behind."""
    qf = os.path.join(slot_dir(name), 'queue.json')
    with core.locked(qf):
        q = [w for w in core.load_json(qf, []) if procs.same_process(w['pid'], w.get('pstart'))]
        res = fn(q)
        core.save_json(qf, q)
        return res


def acquire(name, n, wait_s, by):
    """Block in FIFO order until a slot is free. Returns (slot_no, fd) or (None, None) on timeout."""
    me = {'pid': os.getpid(), 'pstart': procs.start_token(os.getpid()), 'by': by, 't': time.time()}
    _queue(name, lambda q: q.append(me))
    d = slot_dir(name)
    deadline = time.time() + wait_s

    def attempt(q):
        if not q or q[0]['pid'] != me['pid']:
            if not any(w['pid'] == me['pid'] for w in q):   # pruned by mistake (pid check race): rejoin at the end
                q.append(me)
            return None
        for i in range(1, n + 1):
            fd = _try_lock(os.path.join(d, '%d.lock' % i))
            if fd is not None:
                q.pop(0)
                return i, fd
        return None

    def leave(q):
        q[:] = [w for w in q if w['pid'] != me['pid']]
    try:
        while True:
            got = _queue(name, attempt)
            if got:
                return got
            if time.time() > deadline:
                _queue(name, leave)
                return None, None
            time.sleep(POLL)
    except BaseException:
        _queue(name, leave)
        raise


def _systemd_scope_ok():
    if core.env('NO_SYSTEMD') or not sys.platform.startswith('linux') or not shutil.which('systemd-run'):
        return False
    return os.path.exists('/run/user/%d/systemd/private' % os.getuid())


def _watch_rss(proc, cap_mb, name, slot_no, by, killed):
    while proc.poll() is None:
        table = procs.process_table()
        if procs.tree_rss_kb(proc.pid, table) // 1024 > cap_mb:
            killed.append(True)
            core.emit('slot.oom', by=by, topic='%s/%d' % (name, slot_no), cap_mb=cap_mb)
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            return
        time.sleep(1)


def run(name, n, cmd, wait_s=1800, mem_free_mb=0, max_mem_mb=0, by=None, out=None):
    out = out or (lambda s: print(s, file=sys.stderr))
    if not NAME_RE.match(name or '') or name in ('status', 'help'):
        out('slot: bad name %r (letters, digits . _ -)' % name)
        return 2
    if n < 1 or not cmd:
        out('usage: slot NAME N [--wait SEC] [--mem-free MB] [--max-mem MB] -- COMMAND...')
        return 2
    by = by or procs.default_by()
    status(name)   # clean stale holder records first
    t_start = time.time()
    slot_no, fd = acquire(name, n, wait_s, by)
    cmd_s = ' '.join(cmd)[:80]
    if slot_no is None:
        out('slot %s: no free slot after %d s' % (name, wait_s))
        core.emit('slot.timeout', by=by, topic=name, msg=cmd_s, wait_s=wait_s)
        return 75
    try:
        warned = False
        while mem_free_mb:
            avail = procs.mem_available_mb()
            if avail is None or avail >= mem_free_mb:
                break
            if not warned:
                out('slot %s/%d: waiting for %d MB free memory (now %d MB)' % (name, slot_no, mem_free_mb, avail))
                core.emit('slot.mem_wait', by=by, topic='%s/%d' % (name, slot_no), msg=cmd_s, mem_mb=mem_free_mb)
                warned = True
            time.sleep(2)
        argv, watchdog = list(cmd), False
        if max_mem_mb:
            if _systemd_scope_ok():
                argv = ['systemd-run', '--user', '--scope', '-q', '-p', 'MemoryMax=%dM' % max_mem_mb,
                        '-p', 'MemorySwapMax=0', '--'] + argv
            else:
                watchdog = True
        envv = dict(os.environ)
        envv['%s_SLOT' % core.ENV] = str(slot_no)
        wf = os.path.join(slot_dir(name), '%d.who' % slot_no)
        t0 = time.time()
        core.save_json(wf, {'pid': None, 'wrapper': os.getpid(), 'by': by, 'cmd': cmd_s, 'since': t0})
        try:
            proc = subprocess.Popen(argv, pass_fds=(fd,), env=envv, start_new_session=watchdog)
        except OSError as e:
            out('slot: cannot run %s: %s' % (cmd[0], e))
            os.remove(wf)
            return 127
        core.save_json(wf, {'pid': proc.pid, 'wrapper': os.getpid(), 'by': by, 'cmd': cmd_s, 'since': t0})
        core.emit('slot.acquire', by=by, topic='%s/%d' % (name, slot_no), msg=cmd_s, pid=proc.pid,
                  wait_s=round(t0 - t_start, 1))
        killed = []
        if watchdog:
            threading.Thread(target=_watch_rss, args=(proc, max_mem_mb, name, slot_no, by, killed),
                             daemon=True).start()

        def forward(sig, _frame):
            if sig == signal.SIGINT and not watchdog:
                return   # same process group: the terminal already delivered it to CMD
            try:
                (os.killpg if watchdog else os.kill)(proc.pid, sig)
            except OSError:
                pass
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, forward)
        rc = proc.wait()
        if rc < 0:
            rc = 128 - rc
        try:
            os.remove(wf)
        except OSError:
            pass
        core.emit('slot.release', by=by, topic='%s/%d' % (name, slot_no), pid=proc.pid, rc=rc,
                  held_s=round(time.time() - t0, 1), oom=bool(killed) or None)
        return rc
    finally:
        os.close(fd)


def cmd_status(name=None, as_json=False, out=print):
    names = [name] if name else all_names()
    data = {}
    for nm in names:
        data[nm] = {'slots': [{'slot': i, 'state': st, 'holder': who} for i, st, who in status(nm)],
                    'waiting': waiters(nm)}
    if as_json:
        out(json.dumps(data, ensure_ascii=False, indent=1))
        return 0
    if not data:
        out('no slots used yet')
    for nm, v in data.items():
        for s in v['slots']:
            who = s['holder']
            if s['state'] == 'busy' and not who:
                out('%s slot %d: busy (starting)' % (nm, s['slot']))
            elif s['state'] == 'busy':
                out('%s slot %d: busy -- %s pid %s for %s: %s' % (nm, s['slot'], who.get('by'), who.get('pid'),
                                                                  core.dur(time.time() - who.get('since', time.time())), who.get('cmd')))
            elif s['state'] == 'stale':
                out('%s slot %d: free (stale holder cleaned: %s pid %s)' % (nm, s['slot'], who.get('by'), who.get('pid')))
            else:
                out('%s slot %d: free' % (nm, s['slot']))
        if v['waiting']:
            out('%s waiting: %s' % (nm, ', '.join('%s (pid %d)' % (w['by'], w['pid']) for w in v['waiting'])))
    return 0
