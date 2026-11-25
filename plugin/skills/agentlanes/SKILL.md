---
name: agentlanes
description: Use when other coding agents may be working on this machine or repository at the same time — before editing files, when an edit is blocked because another agent holds the file, or when picking the next piece of work from a shared queue.
---

# Working next to other agents

Other agents (Claude Code, Codex, Cursor, scripts) may be editing this machine right now. agentlanes keeps you out of each other's way.

1. **Before you edit**, call the `claim` tool with a short topic and the paths you will change. The claim ends by itself when your session exits.
2. **If the answer is `HELD by …`**, someone else has it. Do not work around it: pick other work (`jobs` with `action: list`), or tell the user who holds it.
3. **Before touching a file you did not claim**, call `who` with its path.
4. **When done**, call `release`. For work longer than a few hours call `renew` now and then. Add follow-up work for others with `jobs` `action: add`.
5. `status` shows everything on one screen: running agents, claims, slots, jobs, recent events.

The edit hook enforces step 2 even if you forget: an edit to a file another agent holds is refused with the holder's name.

Requires the CLI: `pip install agentlanes-cli` (Linux or macOS, Python 3.9+, no dependencies).
