# universal_controller — Documentation Tree

**Status**: CURRENT
**Date**: 2026-11-23
**Applies to**: all documentation under `docs/`
**Depends on**: `docs/05 - Standards/UC-STD-DOCS-v0 — Agentic Technique Master.md`

Every folder is a prerequisite for the ones after it: theory grounds design, design precedes implementation, implementation produces the architecture that reference and standards describe. Find a doc by asking *what kind of claim am I making* — a rationale, a procedure, a map, a rule, a plan, a snapshot, or a measurement — and the folder follows.

The layout is the one `event_tool` (the main consumer of this repo) uses, so a reader moving between the two finds the same kind of claim in the same folder.

## Hard rules (non-negotiable)

1. **A doc's folder is determined by its lane, not its topic.** The service appears in Theory, Design, Implementation and Analysis; each copy makes a different kind of claim. Do not merge them.
2. **`05 - Standards` is frozen after release.** Change it by versioned amendment (`v0` → `v1`), never by edit.
3. **`09 - Archived` is append-only.** An archived doc is a snapshot; never edit one in place.
4. **Dated docs carry `YYYY-MM-DD_` prefixes and the date is load-bearing** — it is how a reader judges staleness.
5. **A doc is trustworthy only if it has been reconciled since the last substantial change to what it describes.** Drift you cannot fix now goes in `000 - Doc Maintenance` as a note, not in your head.

## The tree

| Folder | Purpose | Freeze policy |
|---|---|---|
| `000 - Doc Maintenance` | Drift / tree-health notes | Append-only; action or explicitly defer |
| `00 - Theory` | The models the design rests on: what a controlled browser is, what is shared, what is isolated | Stable; corrections only. A `DRAFT` here is an argument, not a plan |
| `01 - Design` | Why the system is shaped this way | Archive when superseded |
| `02 - Implementation` | Setup, operations, and the build specs code directly realizes | Versioned with the code |
| `03 - Architecture` | The as-built system map | Keep current; single trusted map |
| `04 - Reference` | Vocabulary and external material | Living |
| `05 - Standards` | Normative rules (`UC-STD-*`) | **Frozen after release** |
| `06 - Roadmaps` | Open work, gap analysis | Living |
| `07 - Status Reports` | Dated session/agent handoffs | Archive when complete |
| `08 - Analysis` | Measured deep-dives | Living |
| `09 - Archived` | Superseded material | Append-only; never edit |
| `10 - Blueprints` | Repeatable operator procedures — *what do I do, in order, and where does it usually go wrong* | Living; each cites a real run |

## Where to start

| You are… | Read |
|---|---|
| new to the codebase | `03 - Architecture/00 - System Overview.md` |
| standing the repo up | `02 - Implementation/00 - Setup and Operations.md` |
| asking why chat UIs are driven structurally | `01 - Design/00 - Chat-Window-to-API Design.md` |
| working on the service (UC as one long-running browser owner) | `00 - Theory/00 - Windows, Transport and Control.md`, then `01 - Design/02 - UC as a Local Service.md`, then `02 - Implementation/01 - UC Service — Build Spec.md` |
| checking a number the service docs quote | `08 - Analysis/00 - Service Prototype Measurements.md` |
| unsure what a term means | `04 - Reference/Glossary.md` |
| picking up work cold | the newest file in `07 - Status Reports/`, then `06 - Roadmaps/` |
| about to trust an older doc | `000 - Doc Maintenance/` — the newest note lists what has drifted |

## Adding a doc

1. Pick the lane, then the folder, using the table above.
2. Use the matching template in `05 - Standards/UC-STD-DOCS-v0 — Agentic Technique Master.md` (§ Document-type templates).
3. Open with a status block. Never with "This document describes…".
4. Mark what is unproven — `BUG:`, `unverified`, `not implemented`, `placeholder` — wherever a reader might assume more certainty than exists.
5. Close actionable docs with a **Verification** block: a command to run and a decision to make from its output.
6. Numeric prefixes order *sequential* docs; date prefixes order *dated* docs; descriptive names cover the rest.

## Verification

```bash
find docs -name '*.md' | sort
```

Every file listed should sit in a folder whose purpose matches the kind of claim the file makes. One that does not is either misfiled or is two docs wearing one filename.
