"""MCP server (stdio, JSON-RPC 2.0, newline-delimited). Standard library only.

Lets any MCP-capable coding agent (Claude Code, Cursor, Codex, ...) claim work, check who holds a path,
see the whole machine and use the shared job queue as native tools, without shell calls.

Each tool runs the same code path as the CLI, so behaviour and exit codes match `agentlanes ...` exactly.
Register:  claude mcp add agentlanes -- agentlanes mcp
"""
import contextlib
import io
import json
import os
import sys

from . import NAME, __version__

PROTOCOL = '2025-06-18'

_S = lambda **p: {'type': 'object', 'properties': p, 'additionalProperties': False}
_str = lambda d: {'type': 'string', 'description': d}

TOOLS = [
    ('claim', 'Claim a topic (a unit of work) and optionally the files/dirs it covers before editing them. '
              'Fails with HELD if another agent holds it: then pick other work or wait. The claim ends when you release it, '
              'when the TTL runs out, or when your agent process exits.',
     dict(_S(topic=_str('short name of the work, e.g. "auth-refactor"'),
             desc=_str('one line: what you are doing'),
             paths={'type': 'array', 'items': {'type': 'string'}, 'description': 'files or directories this work will edit'},
             ttl_minutes={'type': 'integer', 'description': 'lease length, default 120'},
             share={'type': 'boolean', 'description': 'allow others to claim the same paths (read-mostly work)'}),
          required=['topic'])),
    ('release', 'Release a topic you claimed, as soon as the work is done.',
     dict(_S(topic=_str('the topic to release')), required=['topic'])),
    ('who', 'Which claims cover these paths. Call before editing a file you did not claim.',
     dict(_S(paths={'type': 'array', 'items': {'type': 'string'}}), required=['paths'])),
    ('list_claims', 'All active claims on this machine (JSON).', _S()),
    ('status', 'One screen: running agents, claims, slots, jobs and recent events.', _S()),
    ('jobs', 'The shared job queue. action=list shows open jobs; add/take/done/drop change it.',
     dict(_S(action={'type': 'string', 'enum': ['list', 'add', 'take', 'done', 'drop']},
             title=_str('job title (add)'), id=_str('job id (take/done/drop)'),
             note=_str('result (done) or reason (drop)')), required=['action'])),
    ('events', 'Recent coordination events (claims, conflicts, slots, jobs, edits), newest last.',
     _S(n={'type': 'integer', 'description': 'how many, default 30'}, kind=_str('only this kind, e.g. claim.conflict'))),
    ('blame', 'Which agents edited a path (from hook events), newest last.',
     dict(_S(path=_str('file or directory')), required=['path'])),
]


def _lease_pid():
    """The claim lives as long as the agent: the enclosing coding agent if we can see it, else the MCP client
    that started this server (our parent), which is that agent or its launcher."""
    from . import procs
    pid, _ = procs.agent_self()
    return str(pid or os.getppid())


def _argv(name, a):
    if name == 'claim':
        v = ['claim', a['topic'], '--pid', _lease_pid()]
        if a.get('desc'):
            v += ['--desc', a['desc']]
        if a.get('paths'):
            v += ['--paths', ','.join(a['paths'])]
        if a.get('ttl_minutes'):
            v += ['--ttl', str(int(a['ttl_minutes']))]
        if a.get('share'):
            v.append('--share')
        return v
    if name == 'release':
        return ['release', a['topic']]
    if name == 'who':
        return ['who'] + list(a['paths'])
    if name == 'list_claims':
        return ['list', '--json']
    if name == 'status':
        return ['status']
    if name == 'events':
        v = ['events', 'tail', '-n', str(int(a.get('n') or 30))]
        return v + (['--kind', a['kind']] if a.get('kind') else [])
    if name == 'blame':
        return ['blame', a['path']]
    if name == 'jobs':
        act = a['action']
        if act == 'list':
            return ['jobs', 'list']
        if act == 'add':
            return ['jobs', 'add', a.get('title') or '']
        if act == 'take':
            return ['jobs', 'take'] + ([a['id']] if a.get('id') else [])
        if act == 'done':
            return ['jobs', 'done', a.get('id') or ''] + (['--result', a['note']] if a.get('note') else [])
        if act == 'drop':
            return ['jobs', 'drop', a.get('id') or ''] + (['--why', a['note']] if a.get('note') else [])
    raise ValueError('unknown tool or action')


def call(name, args):
    """Run one tool. Returns (text, is_error)."""
    from . import cli
    buf = io.StringIO()
    try:
        argv = _argv(name, args or {})
    except (KeyError, ValueError) as e:
        return 'bad arguments: %s' % e, True
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            rc = cli.main(argv)
        except SystemExit as e:          # argparse usage errors
            rc = e.code if isinstance(e.code, int) else 2
    text = buf.getvalue().strip() or ('ok' if not rc else 'exit %s' % rc)
    # 1 = held / conflict / not found: a normal answer the agent must act on, not a tool failure
    return text, rc not in (0, 1, None)


def handle(msg):
    mid, method = msg.get('id'), msg.get('method')
    if mid is None:                      # notification (initialized, cancelled, ...): no reply
        return None
    if method == 'initialize':
        res = {'protocolVersion': (msg.get('params') or {}).get('protocolVersion') or PROTOCOL,
               'capabilities': {'tools': {}},
               'serverInfo': {'name': NAME, 'version': __version__},
               'instructions': 'Several agents share this machine. Before editing files, call claim with the paths; '
                               'on HELD pick other work. Release when done. Use who before touching unclaimed files.'}
    elif method == 'ping':
        res = {}
    elif method == 'tools/list':
        res = {'tools': [{'name': n, 'description': d, 'inputSchema': s} for n, d, s in TOOLS]}
    elif method == 'tools/call':
        p = msg.get('params') or {}
        if p.get('name') not in {t[0] for t in TOOLS}:
            return {'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32602, 'message': 'unknown tool: %s' % p.get('name')}}
        text, err = call(p['name'], p.get('arguments') or {})
        res = {'content': [{'type': 'text', 'text': text}], 'isError': err}
    else:
        return {'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32601, 'message': 'method not found: %s' % method}}
    return {'jsonrpc': '2.0', 'id': mid, 'result': res}


def serve(inp=None, out=None):
    inp, out = inp or sys.stdin, out or sys.stdout
    for line in inp:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            rep = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'parse error'}}
        else:
            rep = handle(msg) if isinstance(msg, dict) else None
        if rep is not None:
            out.write(json.dumps(rep) + '\n')
            out.flush()
    return 0
