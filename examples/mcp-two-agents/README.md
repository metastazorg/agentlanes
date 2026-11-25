# Two real Claude Code agents, one file, coordinated through the MCP server

Recorded 2026-11-25 with `agentlanes-cli` 0.3.2 from PyPI, Claude Code 2.1 (`claude -p`, model haiku), in a fresh repo set up with
`agentlanes init --claude-code --mcp`. Nothing below is scripted: each agent only got a task and "coordinate with the agentlanes MCP tools".

- **Agent A**: add a rate limit to `login()` in `src/auth.py`, then wait 40 s for a "review" before releasing.
- **Agent B** (started 15 s later): make the password check constant-time in the same function; if the file is held, wait and retry.

What happened (`timeline.txt`, times from A's start):
1. A asks `who src/auth.py` -> free, claims `auth-login-rate-limit` for `src/auth.py`, writes its change.
2. B asks `who src/auth.py` -> **HELD by A**. B does not touch the file; it sleeps 10 s and asks again (HELD again).
3. A's session ends without calling `release` (its 40 s wait was blocked by the environment). Its claim was a lease on A's process,
   so agentlanes reaps it the moment A exits (`claim.reap` in `events.txt`). No stale lock, no human needed.
4. B's next `who` -> free. B claims, makes its edit on top of A's, releases.

Result (`auth.py.result`): both changes are in the file — the rate limit from A and `hmac.compare_digest` from B. Nothing was
overwritten. Total cost of both sessions: about $0.025.

Files: `timeline.txt` (tool calls and the coordination answers they got), `events.txt` (agentlanes event log), `auth.py.result`.
Paths are shortened to repo-relative; the agents also had unrelated user-level skills/MCP servers loaded, which they ignored.
