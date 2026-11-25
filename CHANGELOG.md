# Changelog

## Unreleased
- `agentlanes board` shows lanes (holder, branch, unsaved changes).

## 0.3.1 (2026-11-25)
- `agentlanes doctor` starts the project MCP server and checks it answers `initialize`.
- `agentlanes init --mcp`: registers the MCP server in the project's `.mcp.json` (merged with other servers, old file backed up, idempotent), so everyone who clones the repo gets the tools.

## 0.3.0 (2026-11-24)
- `agentlanes board`: read-only live web page (localhost) of agents, claims, slots, jobs and events.
- `agentlanes lane new|list|done`: one git worktree per agent on branch `lane/NAME`, claimed so other agents are kept out; `done` refuses to drop unsaved work and keeps the branch. MCP tool `lane`.

## 0.2.0 (2026-11-24)
- `agentlanes mcp`: MCP server on stdio (standard library only). Tools: claim, release, who, list_claims, status, jobs, events, blame.
- Claude Code plugin (`plugin/`) and marketplace (`.claude-plugin/marketplace.json`): MCP tools + edit hook + skill.
- `agentlanes blame PATH`: the editor hook now records allowed edits (`edit` events) next to refusals; `blame` lists who edited what.
- Example: two real Claude Code agents coordinating on one file (`examples/two-agents/real-session.txt`).
- `agentlanes status --watch [SEC]`: live view that redraws until Ctrl-C.

## 0.1.1 (2026-11-24)
- CI on every push: unit tests, `agentlanes doctor` and `demo.sh` on Linux (Python 3.9–3.12) and macOS (3.10, 3.12).
- macOS: first real runs; a platform-dependent test fixed.
- README: test badge, macOS status, absolute links so they also work on PyPI.
- Packaging: the PyPI name is `agentlanes-cli` (`agentlanes` was too close to an existing project); command and import stay `agentlanes`.

## 0.1.0 (2026-11-23)
- First release: claims with path leases (released when the holder process dies), N-slot job queues with memory caps,
  an append-only event log, a job queue, `status`, `init`, `doctor`.
- Hooks: Claude Code (tested in a real session), Codex, Cursor (experimental).
- `agentlanes run`: kernel-enforced write lanes on Linux via Landlock (tested in a real Claude Code session).
