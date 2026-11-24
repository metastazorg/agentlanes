"""Lanes: one git worktree per agent, on its own branch, covered by a claim.

Two agents in two lanes never touch the same working files, so they cannot overwrite each other, and the edit hook
keeps every other agent out of a lane's directory. Lanes live inside the project at .agentlanes/lanes/NAME (ignored by
git), so the lane shares the project's claims, slots, jobs and event log.

  agentlanes lane new NAME [--base REF] [--desc TEXT] [--pid self|PID] [--ttl MIN] [--by WHO]
  agentlanes lane list [--json]
  agentlanes lane done NAME [--force] [--by WHO]      remove the worktree, keep the branch, release the claim
"""
import json
import os
import re
import subprocess

from . import PROJECT_MARKER, claims, core

NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
BRANCH_PREFIX = 'lane/'


def _git(*args, cwd=None):
    return subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True)


def repo_root(cwd=None):
    """Top of the main working tree, also when called from inside a lane (a linked worktree)."""
    r = _git('rev-parse', '--path-format=absolute', '--git-common-dir', cwd=cwd)
    if r.returncode != 0:
        return None
    common = r.stdout.strip()
    if os.path.basename(common) == '.git':
        return os.path.dirname(common)
    r = _git('rev-parse', '--show-toplevel', cwd=cwd)          # bare or unusual layout
    return r.stdout.strip() if r.returncode == 0 else None


def lanes_dir(root):
    d = os.path.join(root, PROJECT_MARKER)
    os.makedirs(d, exist_ok=True)
    gi = os.path.join(d, '.gitignore')
    if not os.path.exists(gi):
        with open(gi, 'w', encoding='utf-8') as fh:
            fh.write('# runtime coordination state, never commit\n*\n')
    return os.path.join(d, 'lanes')


def topic(name):
    return 'lane:' + name


def new(name, by, base=None, desc='', pid=None, ttl_min=24 * 60, out=print):
    if not NAME_RE.match(name or ''):
        out('bad lane name %r: letters, digits, . _ - (max 64), not starting with a symbol' % name)
        return 2
    root = repo_root()
    if not root:
        out('not inside a git repository')
        return 2
    path = os.path.join(lanes_dir(root), name)
    if os.path.exists(path):
        cur = claims.snapshot().get(topic(name))
        out('lane %s already exists%s: %s' % (name, ' (HELD by %s)' % cur['by'] if cur else '', path))
        return 1
    # Claim first: if someone else holds the lane, nothing is created.
    rc = claims.add(topic(name), by, desc or 'lane %s' % name, ttl_min, pid, (path,), out=lambda m: None)
    if rc:
        cur = claims.snapshot().get(topic(name), {})
        out('HELD by %s: lane %s' % (cur.get('by', '?'), name) if rc == 1 else 'could not claim lane %s' % name)
        return rc
    branch = BRANCH_PREFIX + name
    exists = _git('rev-parse', '--verify', '--quiet', 'refs/heads/' + branch, cwd=root).returncode == 0
    args = ['worktree', 'add', path, branch] if exists else \
           ['worktree', 'add', '-b', branch, path] + ([base] if base else [])
    r = _git(*args, cwd=root)
    if r.returncode != 0:
        claims.release(topic(name), by, out=lambda m: None)
        out('git worktree add failed: %s' % (r.stderr.strip() or r.stdout.strip()))
        return 1
    core.emit('lane.new', by=by, topic=topic(name), path=path, branch=branch, base=base)
    out('lane %s ready (branch %s%s)' % (name, branch, ', existing' if exists else ''))
    out(path)
    return 0


def _worktrees(root):
    r = _git('worktree', 'list', '--porcelain', cwd=root)
    wts, cur = [], {}
    for line in r.stdout.splitlines() + ['']:
        if not line:
            if cur:
                wts.append(cur)
            cur = {}
            continue
        k, _, v = line.partition(' ')
        cur[k] = v or True
    return wts


def listing(root=None):
    root = root or repo_root()
    if not root:
        return None
    base = os.path.join(root, PROJECT_MARKER, 'lanes') + os.sep
    held = claims.snapshot()
    rows = []
    for w in _worktrees(root):
        p = w.get('worktree', '')
        if not p.startswith(base):
            continue
        name = os.path.basename(p)
        c = held.get(topic(name))
        dirty = bool(_git('status', '--porcelain', cwd=p).stdout.strip()) if os.path.isdir(p) else None
        rows.append({'name': name, 'path': p, 'branch': (w.get('branch') or '').replace('refs/heads/', ''),
                     'holder': c['by'] if c else None, 'desc': c.get('desc', '') if c else '',
                     'dirty': dirty, 'missing': not os.path.isdir(p)})
    return rows


def cmd_list(as_json=False, out=print):
    rows = listing()
    if rows is None:
        out('not inside a git repository')
        return 2
    if as_json:
        out(json.dumps(rows, indent=1))
        return 0
    if not rows:
        out('no lanes (agentlanes lane new NAME)')
    for r in rows:
        out('%-20s %-24s %-18s %s%s' % (r['name'], r['branch'], r['holder'] or '(unclaimed)',
                                        'dirty ' if r['dirty'] else '', r['desc']))
    return 0


def done(name, by, force=False, out=print):
    root = repo_root()
    if not root:
        out('not inside a git repository')
        return 2
    path = os.path.join(root, PROJECT_MARKER, 'lanes', name)
    c = claims.snapshot().get(topic(name))
    if c and c['by'] != by and not force:
        out('HELD by %s: lane %s (use --force to remove anyway)' % (c['by'], name))
        return 1
    if os.path.realpath(os.getcwd()).startswith(os.path.realpath(path) + os.sep) or \
            os.path.realpath(os.getcwd()) == os.path.realpath(path):
        out('you are inside the lane; cd out of it first')
        return 2
    if os.path.isdir(path):
        r = _git('worktree', 'remove', *(['--force'] if force else []), path, cwd=root)
        if r.returncode != 0:
            out('git worktree remove failed: %s' % (r.stderr.strip() or r.stdout.strip()))
            out('commit or discard the changes in %s, or pass --force' % path)
            return 1
    else:
        _git('worktree', 'prune', cwd=root)
    if c:
        claims.release(topic(name), None, out=lambda m: None)
    core.emit('lane.done', by=by, topic=topic(name), branch=BRANCH_PREFIX + name)
    out('lane %s removed; branch %s kept (merge it, or: git branch -D %s)' % (name, BRANCH_PREFIX + name,
                                                                         BRANCH_PREFIX + name))
    return 0
