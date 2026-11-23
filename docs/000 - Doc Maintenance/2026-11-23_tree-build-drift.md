# Docs-tree gaps — 2026-11-23

**Status**: Observational note
**Date**: 2026-11-23
**Applies to**: `docs/` as a tree, compared with the code on `main` at `5e3dd0a`

*Observational note. Not a roadmap, not a plan.* Written while the full folder tree was laid down, so the next reader sees a marker on each doc that has drifted instead of acting on stale prose. Open engineering work is in `docs/06 - Roadmaps/`; this note is about the **tree itself**.

## The tree today

```
docs/
├── README.md                index + governance                         ✅ new 2026-11-23
├── 000 - Doc Maintenance/   this note                                  ✅ new
├── 00 - Theory/             1 doc — windows, transport, control        ✅ new (DRAFT)
├── 01 - Design/             3 docs — chat-to-API, signature lab,       🟡 service doc new; the two older ones dated 2026-08-27
│                            UC as a service
├── 02 - Implementation/     2 docs — setup/operations, service spec    🟡 setup doc stale (Gaps 2, 3)
├── 03 - Architecture/       1 doc — system overview                    🟡 stale in three places (Gap 1)
├── 04 - Reference/          1 doc — glossary                           ✅ new
├── 05 - Standards/          1 doc — docs style (UC-STD-DOCS v0)        ✅ moved in from the repo root
├── 06 - Roadmaps/           2 docs — engine gaps, signature lab        🟡 one stale marker (Gap 4)
├── 07 - Status Reports/     1 doc — 2026-08-27 handoff                 🟡 nothing since; 10 commits later (Gap 5)
├── 08 - Analysis/           1 doc — service prototype measurements     ✅ new
├── 09 - Archived/           ARCHIVE-INDEX only                         ✅ new, empty by design
└── 10 - Blueprints/         README only                                ✅ new, no blueprints yet
```

## Gap 0 — the tree was written in a checkout that does not contain the older docs

The nine files added on 2026-11-23 were written in `event_tool`'s submodule checkout at `99390a5`, which branches from a commit older than the docs on `main`. In that checkout `01 - Design/00`, `01 - Design/01`, `02 - Implementation/00`, `03 - Architecture/00`, `06 - Roadmaps/*` and `07 - Status Reports/*` are absent, and so are the modules they describe (`status_server.py`, `registry.py`, `health.py`, `sites/generic.py`). The new docs cite both.

→ **Cheap fix (minutes):** land the new files on `main`, where the cited docs and code exist. On `main`, also delete the repo-root `Agentic Technique Master.md`: `docs/05 - Standards/UC-STD-DOCS-v0 — Agentic Technique Master.md` is the same text with a header and replaces it.

## Gap 1 — System Overview is stale in three places

`docs/03 - Architecture/00 - System Overview.md` (2026-08-27):

| Claim | Now |
|---|---|
| "much of the Python driver layer is UNCOMMITTED in the working tree" (status block) | committed; every file the overview names exists on `main` |
| provider: "No streaming" (§8) | `uc_browser/llm_providers/uc.py` has `streaming()` fed by an `on_delta` sink |
| the grok adapter is described only as a "fast path" | the adapter now tries the extension toolkit first (added after the doc was written) and otherwise, since `5e3dd0a`, reads the reply through Grok's REST read endpoints, because Grok sends chats over a WebSocket. No doc in this repo says how replies are captured; the only description is `event_tool`'s `docs/02 - Implementation/03 - UC Grok Proxy.md` §6 |

→ **Cheap fix (under an hour):** correct the three rows in place and re-date the doc. **Alternative:** leave it and rely on this note, which makes the single trusted map untrusted.

## Gap 2 — Setup and Operations quotes an old test count

`docs/02 - Implementation/00 - Setup and Operations.md` §3 says **64 passed + 1 skipped** (2026-08-27). On `main` at `5e3dd0a` the suite is **100 passed, 4 skipped** (run 2026-11-23, Linux, inside `event_tool`'s pixi environment). The 2026-08-27 handoff repeats the old figure, correctly for its date.

→ **Cheap fix (minutes):** update the number and its date.

## Gap 3 — no Linux path in Setup and Operations

The doc assumes the repo's own pixi workspace, which lists `win-64` only, and its zombie-Chromium cleanup is PowerShell. UC runs daily on Linux as a submodule inside `event_tool`'s environment. Two Linux facts are recorded only in the new service spec (`02 - Implementation/01`, gotchas 4 and 7).

→ **Cheap fix (an hour):** add a Linux section to the setup doc, or add `linux-64` to the workspace and document that. The second is a code decision, not a doc fix.

## Gap 4 — a stale UNCOMMITTED marker in the Signature Lab roadmap

`docs/06 - Roadmaps/01 - Signature Lab Roadmap.md` marks `uc_browser/site_profiles.py` as UNCOMMITTED. It is committed on `main`.

→ **Cheap fix (minutes).**

## Gap 5 — ten commits with one doc update

Since the docs were dated (2026-08-27; last doc commit `5e1c389`, 2026-08-28) `main` gained: auto-bootstrap with an API backup, the analyst agent, vendor signatures and the in-page scanner, the reverse loop, the timing estimator, the cross-engine browser matrix, and the Grok REST reply read. A search of `docs/` finds no mention of the toolkit path, the browser matrix or the reverse loop.

→ **Fix (a session):** a reconciliation pass over `03 - Architecture/00` and `06 - Roadmaps/00`, and a status report for the September–November work. Not cheap; listed so it is visible.

## Why this note exists

The tree now has every folder, which makes the older docs look more current than they are. Each claim listed above was checked against `main` on 2026-11-23 and found out of date. A reader should take the 2026-08-27 docs as accurate for that date and check this note before acting on them.

## Suggested cadence

One note per session that changes code without updating the matching doc. The Grok REST change (`5e3dd0a`) should have produced one.

## Decision log

| Date | Decision |
|---|---|
| 2026-11-23 | Tree laid down to match `event_tool`'s. Gaps 1–5 recorded, not fixed: the session's scope was the tree and the three service docs. |
