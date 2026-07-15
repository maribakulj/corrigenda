# SPECS_SPRINTS — Sprints de développement

---

## Vue d'ensemble des sprints

| Sprint | Nom | Durée est. | Dépend de |
|--------|-----|-----------|-----------|
| 0 | Bootstrap & infrastructure | 1-2h | — |
| 1 | Schemas + Parser ALTO (avec détection césures) | 2-3h | Sprint 0 |
| 2 | Hyphenation Reconciler | 2-3h | Sprint 1 |
| 3 | Rewriter ALTO (avec HYP/SUBS_*) | 2h | Sprint 1, 2 |
| 4 | Providers LLM | 2-3h | Sprint 1 |
| 5 | Chunk Planner + Validateur (hyphen-aware) | 2h | Sprint 1, 2 |
| 6 | Orchestrateur + Job Store | 2-3h | Sprint 2, 3, 4, 5 |
| 7 | Routes API FastAPI | 2h | Sprint 6 |
| 8 | Frontend React | 3-4h | Sprint 7 |
| 9 | Docker + HF Spaces | 1-2h | Sprint 8 |
| 10 | Tests d'intégration + polish | 2h | Sprint 9 |

---

## Tests obligatoires par sprint

### Sprint 1 — `test_parser.py`
- Détection namespace v2/v3/v4/sans ns
- Reconstruction ocr_text (String, SP, HYP)
- Construction PageManifest (nb pages, blocs, lignes)
- Liens prev/next
- Détection césure explicite (SUBS_TYPE + SUBS_CONTENT)
- Détection césure heuristique (dernier token en `-`)
- Liaison bidirectionnelle des paires

### Sprint 2 — `test_hyphenation.py`
- `enrich_chunk_lines` : PART1 reçoit `hyphen_join_with_next`, PART2 reçoit `hyphen_join_with_prev`
- `enrich_chunk_lines` : `logical_join_candidate` présent si `hyphen_subs_content` connu
- `reconcile_hyphen_pair` : textes non fusionnés, frontière physique préservée
- `reconcile_hyphen_pair` : source_explicit=True + subs_content connu → résolution avec confiance
- `reconcile_hyphen_pair` : source_explicit=False → mode conservateur, pas de SUBS_CONTENT
- `reconcile_hyphen_pair` : cas ambigu → retour des textes source
- `should_stay_in_same_chunk` : vrai pour PART1/PART2 liés, faux pour lignes normales

### Sprint 3 — `test_rewriter.py`
- Préservation TextLine ID/coords
- Tokenisation et géométrie proportionnelle (sum widths == TextLine.WIDTH)
- Reconstruction HYP sur PART1
- Reconstruction SUBS_TYPE/SUBS_CONTENT sur PART1 et PART2 (quand confiance suffisante)
- Pas de SUBS_CONTENT sur césure heuristique
- Round-trip (parse → rewrite sans correction → re-parse → mêmes IDs)

### Sprint 5 — `test_chunk_planner.py` et `test_validator.py`
- Voir [SPECS_JOBS.md](SPECS_JOBS.md)

### Sprint 10 — `test_integration.py`
- Voir [SPECS_API.md](SPECS_API.md)
