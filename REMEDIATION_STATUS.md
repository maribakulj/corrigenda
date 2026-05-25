# Remediation status — alto-llm-corrector

Last updated: 2026-05-25 (session L1)
Branch: `claude/vibrant-pascal-STfnR`

Roadmap reference: voir conversation (sections 5 et 6 du plan validé).
Convention : 1 session = 1 lot, même identifiant (L1 → L8).

## Progress

| Lot/Session | Statut       | Commits     | Notes |
|-------------|--------------|-------------|-------|
| L1          | done         | (this push) | size-limit + preset-app added |
| L2          | not started  | —           | health endpoints (B2, B3) |
| L3          | not started  | —           | proxy headers + middleware (R1, R2) |
| L4          | not started  | —           | pipeline tests P0 (B4, T0a-d) |
| L5          | not started  | —           | alto-core release readiness (B5, A5, B6, P3, P8) |
| L6          | not started  | —           | architecture cleanup (A1, A2, A3, A9) |
| L7          | not started  | —           | release pipeline (P5, P6, P7) |
| L8          | not started  | —           | backlog (T1a-d, R3, R4, R5, A4, A6) — optional |

## Done

- **B1** — `size-limit` + `@size-limit/preset-app` added to `frontend/devDependencies` (L1). `npx size-limit` exits 0, reports JS 49.77 KB / 75 KB budget, CSS 3.88 KB / 10 KB budget.

## In progress

- (none)

## Blocked

- (none)

## Remaining

- B2, B3, R1, R2, B4, A5, B5, B6, P3, P8, T0a, T0b, T0c, T0d, A1, A2, A3, A9, P5, P6, P7, T1a, T1b, T1c, T1d, R3, R4, R5, A4, A6.

## New bugs discovered

- **NB1** — Pre-existing devDeps vulnerabilities surfaced during L1 install:
  - `postcss` < safe: XSS via unescaped `</style>` ([GHSA-qx2v-qp2m-jg93](https://github.com/advisories/GHSA-qx2v-qp2m-jg93)), moderate.
  - `vite` ≤ 6.4.1: path traversal + arbitrary file read via dev server WebSocket ([GHSA-4w7w-66w2-5vf9](https://github.com/advisories/GHSA-4w7w-66w2-5vf9), [GHSA-p9ff-h696-f583](https://github.com/advisories/GHSA-p9ff-h696-f583)), high.
  - These existed before L1 — not introduced by size-limit. Both are dev-only (`npm audit --omit=dev` reports 0). Proposed lot: new mini-lot **L9** (security bumps) after L8, or fold into L8 if scope permits. Decision pending.

## Tests added

- (none — L1 is a packaging fix, no test required per roadmap §5/L1)

## Tests count evolution

- Baseline (avant L1): 329 backend + 4 alto-core + 12 frontend = 345 total.
- Après L1: unchanged (345). L1 ne touche pas de tests.

## Coverage evolution

- Baseline `observers.py`: 0% (audit).
- Baseline `correction_pipeline.py`: ~70% (audit).
- Cible post-L4: `observers.py` ≥ 60%, `correction_pipeline.py` ≥ 85%.
- Cible post-L8: `observers.py` ≥ 90%, `store.py` ≥ 95%, `health.py` ≥ 95%.

## Risks remaining

- L6 refactor pourrait casser des tests qui patchent `run_job` — mitigation prévue : grep préalable.
- L7 check tag peut bloquer une release légitime si workflow lancé avant push tag — mitigation : doc CONTRIBUTING.
- NB1 (vite/postcss) : dev-only mais à traiter avant fin de remédiation pour ne pas laisser de signal rouge sur `npm audit`.

## Decisions pending (utilisateur)

- A4: garder ou supprimer le legacy `/health` endpoint ? (revue en L8)
- A6: déplacer les 6 tests backend qui importent privés alto-core vers `packages/alto-core/tests/`, ou promouvoir les privés ? (revue en L8)
- L8: faire ou reporter ?
- NB1: créer un lot L9 dédié ou folder dans L8 ?
