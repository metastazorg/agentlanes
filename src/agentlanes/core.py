"""Shared plumbing: state directory, file locks, JSON files, the event log, notify hook, durations."""
import contextlib
import fcntl
import json
import os
import re
import subprocess
import time

from . import ENV, NAME, PROJECT_MARKER

DEFAULT_NOTIFY_KINDS = ('claim.conflict', 'claim.blocked', 'claim.reap', 'slot.stale', 'slot.timeout', 'slot.oom',
                        'job.requeue')


def env(key, default=None):
    return os.environ.get('%s_%s' % (ENV, key), default)


def state_dir():
    """--state-dir / $AGENTLANES_STATE_DIR > nearest .agentlanes/ above cwd > $XDG_STATE_HOME/agentlanes."""
    d = env('STATE_DIR')
    if not d:
        cur = os.getcwd()
        while True:
            if os.path.isdir(os.path.join(cur, PROJECT_MARKER)):
                d = os.path.join(cur, PROJECT_MARKER)
                break
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
    if not d:
        base = os.environ.get('XDG_STATE_HOME') or os.path.expanduser('~/.local/state')
        d = os.path.join(base, NAME)
    d = os.path.abspath(os.path.expanduser(d))
    os.makedirs(d, exist_ok=True)
    return d


def path(*parts):
    p = os.path.join(state_dir(), *parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


@contextlib.contextmanager
def locked(target):
    """Exclusive flock on TARGET.lock for the duration of the block (data files are replaced atomically)."""
    with open(target + '.lock', 'a') as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def load_json(p, default):
    try:
        with open(p, encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def save_json(p, obj):
    tmp = '%s.%d.tmp' % (p, os.getpid())
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, p)


def config():
    c = load_json(os.path.join(state_dir(), 'config.json'), {})
    return c if isinstance(c, dict) else {}


def parse_age(s):
    """'90m' / '2h' / '1d' / '300' (seconds) -> seconds."""
    m = re.fullmatch(r'(\d+(?:\.\d+)?)([smhd]?)', str(s).strip())
    if not m:
        raise ValueError('bad duration: %s' % s)
    return float(m.group(1)) * {'': 1, 's': 1, 'm': 60, 'h': 3600, 'd': 86400}[m.group(2)]


def hm(t):
    return time.strftime('%H:%M:%S', time.localtime(t))


def dur(s):
    if s is None:
        return '-'
    s = int(s)
    if s < 60:
        return '%ds' % s
    if s < 3600:
        return '%dm' % (s // 60)
    if s < 86400:
        return '%dh%02d' % (s // 3600, s % 3600 // 60)
    return '%dd%02dh' % (s // 86400, s % 86400 // 3600)


# ------------------------------------------------------------------ event log

def events_file():
    return path('events.jsonl')


def emit(kind, **fields):
    """Append one event to the log. Never raises: coordination must not break the caller. Returns the record or None."""
    try:
        rec = {'t': round(time.time(), 3), 'kind': str(kind)}
        rec.update({k: v for k, v in fields.items() if v is not None and v != ''})
        line = json.dumps(rec, ensure_ascii=False, default=str) + '\n'
        ev = events_file()
        max_bytes = int(env('EVENTS_MAX_BYTES', 4 << 20))
        with locked(ev):
            try:
                if os.path.getsize(ev) > max_bytes:
                    os.replace(ev, ev + '.1')
            except OSError:
                pass
            with open(ev, 'a', encoding='utf-8') as fh:
                fh.write(line)
        _notify(rec)
        return rec
    except Exception:
        return None


def _notify(rec):
    """Run the user's notify command (event JSON on stdin) for selected kinds. Fire and forget."""
    cfg = config()
    cmd = env('NOTIFY_CMD') or cfg.get('notify_cmd')
    if not cmd:
        return
    kinds = env('NOTIFY_KINDS')
    kinds = kinds.split(',') if kinds else cfg.get('notify_kinds', DEFAULT_NOTIFY_KINDS)
    if not any(rec['kind'].startswith(k.strip()) for k in kinds if k.strip()):
        return
    try:
        p = subprocess.Popen(cmd, shell=True, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        p.stdin.write(json.dumps(rec, ensure_ascii=False).encode())
        p.stdin.close()
    except Exception:
        pass


def read_events(since=None, kind=None, by=None):
    ev = events_file()
    for p in (ev + '.1', ev):
        try:
            fh = open(p, encoding='utf-8')
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if since and e.get('t', 0) < since:
                    continue
                if kind and not str(e.get('kind', '')).startswith(kind):
                    continue
                if by and e.get('by') != by:
                    continue
                yield e


def fmt_event(e):
    t = time.strftime('%m-%d %H:%M:%S', time.localtime(e.get('t', 0)))
    extra = {k: v for k, v in e.items() if k not in ('t', 'kind', 'by', 'topic', 'msg')}
    s = '%s %-16s %-14s' % (t, e.get('kind', ''), str(e.get('by', '-'))[:14])
    if e.get('topic'):
        s += ' [%s]' % e['topic']
    if e.get('msg'):
        s += ' ' + str(e['msg'])
    if extra:
        s += ' ' + ' '.join('%s=%s' % kv for kv in extra.items())
    return s
