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
- **Slice 2**: reconciliation and fallback operate per group (one
  reconcile call per unit, a fallback covers the whole unit); the
  pipeline's transitive revert propagation collapses into "revert the
  group".
- **Slice 3**: the planner's block packing joins; `BOTH` becomes a
  derived detail of group membership rather than a load-bearing state.

## Consequences
Atomicity claims become checkable against one definition. The pinning
logic in the planner is shorter and provably order-independent. Until
slice 2 lands, the pointer fields remain the storage of record — the
derivation reads them; nothing writes groups back.
