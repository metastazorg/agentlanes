"""Editor-tool hooks for coding agents.

`agentlanes hook claude-code` is a Claude Code PreToolUse hook: it reads the hook JSON on stdin and denies an
Edit/Write/MultiEdit/NotebookEdit whose file is covered by a claim held by a *different* agent. Paths that nobody
claimed, and paths claimed by this same agent, pass through untouched (no output = normal permission flow).
A claim counts as "mine" when its agent pid is this session's agent process, or its `by` equals $AGENTLANES_AGENT.

`agentlanes hook codex` does the same for Codex PreToolUse on apply_patch (file paths are read from the patch).
`agentlanes hook cursor` does the same for Cursor preToolUse (experimental; Cursor expects an explicit answer).

Fail-open by default: if the hook itself cannot work (bad input, unreadable or corrupt state), the edit is allowed,
because a coordination tool must not stop all work when it breaks. With $AGENTLANES_STRICT=1 those cases deny the
edit with a message that says what is broken. Note that if the `agentlanes` command is missing from PATH, the hook
never runs at all and the agent's harness decides; `agentlanes init --claude-code --strict` writes a hook command
that denies in that case too.
"""
import json
import os
import re
import sys

from . import claims, core, procs

STRICT_HINT = ('Coordination state is broken, so edits are blocked (strict mode, %s_STRICT=1). Run `%s doctor` '
               'to see why. A human can unset %s_STRICT to fall back to fail-open.')


def strict():
    return (core.env('STRICT') or '').strip().lower() not in ('', '0', 'false', 'no', 'off')


def _mine(c, my_pid, my_name):
    if my_pid and c.get('apid') == my_pid:
        return True
    if my_pid and c.get('pid') == my_pid:
        return True
    return bool(my_name) and c.get('by') == my_name


def _check_state():
    """Raise if the state directory or claims.json cannot be trusted (used in strict mode only)."""
    f = claims.claims_file()
    if os.path.exists(f):
        with open(f, encoding='utf-8') as fh:
            d = json.load(fh)          # ValueError on a corrupt file
        if not isinstance(d, dict):
            raise ValueError('%s is not a JSON object' % f)
    if not os.access(os.path.dirname(f), os.W_OK):
        raise OSError('state directory %s is not writable' % os.path.dirname(f))


def blocking_claim(targets, tool=None):
    """Return a deny reason if any of TARGETS is covered by another agent's claim, else None."""
    my_pid, _ = procs.agent_self()
    my_name = core.env('AGENT')
    for target in targets:
        hits = [(k, c, q) for k, c, q in claims.holders_of(target) if not _mine(c, my_pid, my_name)]
        if not hits:
            continue
        k, c, q = hits[0]
        core.emit('claim.blocked', by=my_name or ('agent-%s' % my_pid if my_pid else None), topic=k,
                  holder=c['by'], path=target, tool=tool)
        return ('%s is claimed by %s (claim "%s": %s, since %s). Do not edit it. Pick other work, or ask that agent '
                'to release it; check with `%s who %s`.' % (q, c['by'], k, c.get('desc', ''), core.hm(c['since']),
                                                             core.NAME, target))
    for target in targets:   # allowed: record who is about to edit what (`agentlanes blame PATH`)
        try:
            core.emit('edit', by=my_name or ('agent-%s' % my_pid if my_pid else None), path=target, tool=tool)
        except Exception:
            pass
    return None


def _abs(p, cwd):
    p = os.path.expanduser(p.strip())
    return p if os.path.isabs(p) else os.path.join(cwd or os.getcwd(), p)


PATCH_FILE_RE = re.compile(r'^\*\*\* (?:Add|Update|Delete) File: (.+?)\s*$|^\*\*\* Move to: (.+?)\s*$', re.M)


def patch_paths(patch):
    """File paths named in a Codex apply_patch body (`*** Update File: x`, `*** Move to: y`, ...)."""
    return [a or b for a, b in PATCH_FILE_RE.findall(patch or '')]


def targets_of(data):
    ti = data.get('tool_input') or {}
    if not isinstance(ti, dict):
        return []
    cwd = data.get('cwd')
    out = []
    for key in ('file_path', 'notebook_path', 'path', 'target_file', 'filePath'):
        v = ti.get(key)
        if isinstance(v, str) and v.strip():
            out.append(_abs(v, cwd))
    if data.get('tool_name') in ('apply_patch', 'Edit', 'Write') or '*** Begin Patch' in str(ti.get('command', '')):
        out += [_abs(p, cwd) for p in patch_paths(str(ti.get('command') or ti.get('input') or ''))]
    return list(dict.fromkeys(out))


def decide(stdin):
    """(reason_or_None, tool). Raises only in strict mode."""
    try:
        data = json.load(stdin)
        if not isinstance(data, dict):
            raise ValueError('hook input is not a JSON object')
    except ValueError:
        if strict():
            raise
        return None, None   # never break the agent on malformed input
    targets = targets_of(data)
    if not targets:
        return None, data.get('tool_name')
    if strict():
        _check_state()
    try:
        return blocking_claim(targets, data.get('tool_name')), data.get('tool_name')
    except Exception:
        if strict():
            raise
        return None, data.get('tool_name')


def _run(stdin, emit_deny, emit_allow=None, emit_error=None):
    try:
        reason, _ = decide(stdin)
    except Exception as e:
        msg = STRICT_HINT % (core.ENV, core.NAME, core.ENV) + ' Error: %s: %s' % (type(e).__name__, e)
        (emit_error or emit_deny)(msg)
        return 0
    if reason:
        emit_deny(reason)
    elif emit_allow:
        emit_allow()
    return 0


def _pretool_deny(out):
    def f(reason):
        json.dump({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                          'permissionDecisionReason': reason}}, out)
        out.write('\n')
    return f


def claude_code(stdin=None, out=None):
    """Claude Code PreToolUse. No output = no opinion (normal permission flow)."""
    out = out or sys.stdout
    return _run(stdin or sys.stdin, _pretool_deny(out))


def codex(stdin=None, out=None):
    """Codex PreToolUse (apply_patch / Edit / Write matchers). Same JSON answer as Claude Code."""
    out = out or sys.stdout
    return _run(stdin or sys.stdin, _pretool_deny(out))


def cursor(stdin=None, out=None):
    """Cursor preToolUse (matcher Write). Cursor treats empty output as invalid, so always answer."""
    out = out or sys.stdout

    def answer(obj):
        json.dump(obj, out)
        out.write('\n')
    return _run(stdin or sys.stdin,
                lambda reason: answer({'permission': 'deny', 'agent_message': reason, 'user_message': reason}),
                lambda: answer({'permission': 'allow'}))


ADAPTERS = {'claude-code': claude_code, 'codex': codex, 'cursor': cursor}
