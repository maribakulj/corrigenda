# ADR-010 — Hyphen groups: one derivation of "these lines travel together"

Status: accepted (2026-07) — landing in slices; see Staging.

## Context
A hyphenated word split across lines is ONE thing to correct, but the
engine encoded that fact as per-line pointer fields
(`hyphen_pair_line_id`, `hyphen_forward_pair_id`, roles PART1/BOTH/
PART2) and re-derived the grouping ad hoc at every site: a union-find
in the chunk planner's window pinning, another in its block packing,
transitive revert worklists in the pipeline's duplicate pass, pairwise
checks in the validator. Every ad-hoc derivation is an opportunity to
disagree with the others — the audit's P0 finding (a 3-line chain split
across chunks by a pairwise last-write-wins pin) was exactly such a
disagreement.

## Decision
1. `corrigenda.core.units.HyphenGroup` — a frozen value: the maximal
   hyphen-linked component, members as `LineRef`s in reading order,
   with `spans_pages` (cross-page joins are reconciled with cross-page
   context, never planned into one chunk) and `explicit` (conservative
   heuristic mode hangs off it: a heuristic group never invents
   SUBS_CONTENT).
2. `derive_hyphen_groups(lines)` is THE derivation. It accepts any line
   set, so document-wide consumers (reconciler, reverts) and page-scoped
   ones (the planner) use the same function — a severed cross-page pair
   simply degenerates to no group page-locally.
3. Grouping is cross-validated against the generated corpus
   (`tests/_alto_gen.py`): every generated pair/chain/seam must surface
   as exactly one group with the right members in the right order.

## Staging
- **Slice 1 (landed)**: the model + derivation; the planner's window
  pinning consumes it (its local union-find is gone).
- **Slice 2** (fallback half LANDED): a fallback covers the whole unit
  — the chunk-fallback and absorb paths extend to cross-page members
  through the shared `_hyphen_closure`, and the reconcile/acceptance
  paths refuse to correct a member whose partner already fell back
  (this closed a REAL mixed-pair bug: one-sided chunk failure on a
  cross-page pair left the joined word rewritten on one line and
  verbatim on the other), and the duplicate-revert pass walks that same
  closure instead of its own inline worklist. Remaining: one reconcile
  call per unit.
- **Slice 3**: the planner's block packing joins; `BOTH` becomes a
  derived detail of group membership rather than a load-bearing state.

### Design constraint discovered while scoping slice 2
The LINE-granularity planner UNLINKS over-cap chains mid-run (it cuts a
chain longer than `max_lines_per_request` and rewrites the members'
pointer fields — `_try_line`). Consequently a group set derived once at
run start goes stale, and the revert pass's fixed-point worklist —
which follows the CURRENT pointers — is not a naive duplicate of the
derivation but the semantically correct traversal under mutation.
Slice 2 therefore starts by making the cut a UNIT operation (a group
SPLIT recorded in the unit model) instead of a planner side effect on
pointer fields; only then can reverts become a group lookup. This is
the same lesson as the rest of the plan: mutation of the record of
truth is what forces every downstream pass to re-derive it.

Postscript on the invariance gate: scoping it to chain-safe
partitions (the first remedy tried) was an over-correction. Every
falsifying example the gate produced traced to a VALIDATOR false
positive — the fusion check flagged identity proposals whenever the
source's own last word already equalled the logical word (one-letter
fragments: 'A'+'A' → word 'AA' on a line reading 'AA-') — and the
hard chunk failure that followed made the fallback blast radius
partition-visible. With the check made source-relative, the gate runs
over-cap partitions too: the unlink only executes on failure-driven
descent to LINE granularity, which the gate's deterministic producer
can no longer trigger, and it stays pinned at planner level
(``test_line_mode_cap_unlinks_the_cut_pair``). The design constraint
above stands unchanged for slice 2.

## Consequences
Atomicity claims become checkable against one definition. The pinning
logic in the planner is shorter and provably order-independent. Until
slice 2 lands, the pointer fields remain the storage of record — the
derivation reads them; nothing writes groups back.
