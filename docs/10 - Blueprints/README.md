# Blueprints

**Status**: CURRENT — the folder's rules; it holds no blueprints yet
**Date**: 2026-11-23
**Applies to**: repeatable operator procedures

A **blueprint** is a procedure you run more than once, written so the next run is identical and so the *judgement* in it is explicit rather than tacit.

## What separates a blueprint from the neighbouring folders

| Folder | Answers |
|---|---|
| `02 - Implementation` | *How do I stand this up?* — one-time or occasional setup |
| `04 - Reference` | *What does this term or flag mean?* |
| **`10 - Blueprints`** | *What do I do, in order, and where does it usually go wrong?* |

## Rules

1. **Every step is a command or a decision, never "check that it looks right".** If a step needs judgement, name the signal and the threshold.
2. **Record the failure the step exists to prevent.** A step with no failure behind it is ceremony and will be skipped.
3. **Cite real runs.** A blueprint claiming a procedure works must name a case where it did, with numbers.
4. **Cheap checks before expensive ones.** Anything that avoids launching a browser goes earlier.

## Candidates

Procedures that have been run once, will recur, and are not yet written down. Each becomes a blueprint the second time it is run, with both runs as its citations.

| Procedure | Run | Where the steps live today |
|---|---|---|
| Find out why a site adapter stopped returning replies (compare what the page calls with what the adapter waits for) | 2026-11-17, Grok moved chats to a WebSocket | commit `99390a5` message; `event_tool` `docs/02 - Implementation/03 - UC Grok Proxy.md` §8 |
| Seed a browser profile with a session from the user's normal Chrome | 2026-11-17, Grok | `event_tool` `docs/02 - Implementation/03 - UC Grok Proxy.md` §1 |
| Port a fix from an old base onto `main` and re-verify it live | 2026-11-17, `5e3dd0a` | commit `5e3dd0a` message |
