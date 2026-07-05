# PROGRESS_V1 — alto-core v1.0

Suivi de la livraison v1.0 de `packages/alto-core/` selon `SPECS_LIB_V2.md`
(§7 F1–F14, §8, §9, §11, §13 ligne v1.0). Ce fichier est le point de reprise
pour une session fraîche : il dit ce qui est fait, ce qui reste, et les
décisions prises.

## Contrat / docs
- `SPECS_LIB_V2.md` (la spec) a été **rajoutée à la racine cette session**
  (elle n'existait ni dans le working tree ni dans l'historique git ; fournie
  par l'utilisateur). C'est le contrat.
- Branche de travail : `claude/alto-core-v1-release-jgj5wl`.

## Baseline (avant toute modif, commit de départ)
- Lib : `cd packages/alto-core && python -m pytest tests/` → **70 passed**.
- Backend : `cd backend && PYTHONPATH=. python -m pytest` → **424 passed**.
- Lib ruff : `ruff check src/` + `ruff format --check src/` → clean.
- Lib `python -m mypy --strict src/alto_core` → **2 erreurs seulement** :
  - `pipeline/validator.py:23` `dict` sans paramètres de type (`hyphen_pairs: dict`).
  - `alto/rewriter.py:398` `dict(el.attrib)` — `_Attrib` incompatible `dict[str,str]`.
  (mypy/lxml-stubs/pydantic installés dans l'env du paquet ; utiliser
  `python -m mypy`, pas le `mypy` global qui n'a pas pydantic.)

## Commandes (découvertes depuis pyproject/CI, ne pas inventer)
- Lib tests : `cd packages/alto-core && python -m pytest tests/`
- Lib lint : `cd packages/alto-core && ruff check src/ && ruff format --check src/`
- Lib types : `cd packages/alto-core && python -m mypy --strict src/alto_core`
- Backend tests : `cd backend && PYTHONPATH=. python -m pytest`
- Backend types : `cd backend && mypy --explicit-package-bases app`
- Frontend contrat SSE : couvert par `backend/tests/test_sse_event_contract.py`
  (compare `frontend/src/hooks/useJobStream.ts::EVENTS`).

## Ordre des tranches (sûr → risqué), 1+ commits par tranche
1. Robustesse additive : **F3, F5, F6, F13 (GuardConfig), F7 (PairingPolicy)**.
2. Impact snapshot : **F2, F4** (snapshots mis à jour délibérément).
3. Pipeline : **F1** (descente granularité + `chunk_downgraded` + EVENTS front),
   **F8** (cibles vs contexte), **F9** (RetryPolicy), **F10** (should_abort).
4. Surface API & packaging : **F14** (tuple + Usage), hiérarchie erreurs §8.4,
   `CorrectionReport` public + dry-run `apply=False`, **F12** (déplacer enums
   applicatives vers backend — LE PLUS RISQUÉ, en dernier), `py.typed` +
   `mypy --strict` CI, provenance §11.
5. **F11** : rapatrier les tests d'algo dans `packages/alto-core/tests/`.

## Localisation des F-items dans le code actuel (re-vérifiée)
- **F1** desc. granularité : `chunk_planner.downgrade_granularity` existe mais
  jamais appelé ; `correction_pipeline._apply_chunk_fallback` (≈L651) reverte
  tout le chunk. Ajouter re-planif au grain inférieur + événement
  `chunk_downgraded` + budget `RetryPolicy.per_chunk_budget` (défaut 6).
- **F2** `rewriter._emit_string` (slow, ≈L329) recopie TOUS les attrs sauf SUBS
  → limiter à `ID`+`STYLEREFS` ; `_update_content_in_place` (fast, ≈L272) →
  supprimer `WC`/`CC` quand CONTENT change. **Bouge des snapshots.**
- **F3** `parser._parse_textline_hyphen_info` `etree.QName(last_child.tag)`
  (≈L143) lève sur commentaire/PI → filtrer enfants dont `tag` non `str`.
- **F4** `rewriter._line_text_unchanged` (≈L117) compare non-strippé vs
  `ocr_text` strippé → stripper les deux côtés. **Bouge des snapshots.**
- **F5** `_ns._int_attr` (L46) `int(raw)` lève sur `"123.0"` → `int(float(raw))`
  trunc ; non-numérique lève toujours.
- **F6** `rewriter._compute_geometry` (≈L72) : espaces pondérés 0.6 hors du
  total → dernier token absorbe le déficit. Faire entrer 0.6 dans total_weight
  + répartir l'arrondi. **Bouge des snapshots.**
- **F7** `parser._link_hyphen_pairs` séquentiel → `PairingPolicy` injectable,
  défaut = comportement actuel.
- **F8** fenêtres : lignes cibles vs contexte. Touche `chunk_planner` (window),
  `ChunkRequest` (champ cibles), `enrich_chunk_lines`, `validator` (compte sur
  cibles), pipeline.
- **F9** `correction_pipeline._call_with_retry` rampe temp 0.0/0.3/0.5 codée en
  dur (≈L725) + `DEFAULT_MAX_ATTEMPTS=3` → `RetryPolicy` injectable.
  `default()` = actuel à l'octet ; `deterministic()` = temps tous 0.
- **F10** aucun point d'annulation → `should_abort` sur `run()`, sondé entre
  chunks/pages → `CorrectionAborted`, sorties non écrites.
- **F11** tests d'algo dans `backend/tests/` → rapatrier.
- **F12** `schemas`: sortir `Provider`, `JobManifest`, `JobStatus` (+ `images`)
  vers backend. **Piège** : `PageManifest.status`/`DocumentManifest.status` sont
  typés `JobStatus` et `_process_page` fait `page.status = JobStatus.COMPLETED`.
  À retyper/retirer. `LineStatus`, `PipelineEventType` RESTENT.
- **F13** seuils dispersés (`line_acceptance` L37-51, `migration_guards`,
  `validator._check_pair_drift`) → `GuardConfig` frozen, défauts = actuels.
- **F14** `provider.complete_structured` → renvoyer `(dict, Usage | None)`.
  Touche 4 providers backend + site d'appel pipeline + ~10 tests backend.

## Notes / pièges confirmés
- `test_rewriter_byte_stability.py` ne pin QUE UNTOUCHED/SUBS_ONLY → F2/F4/F6
  n'y touchent pas. Les snapshots qui bougent sont côté backend
  (`test_orchestrator_snapshot.py`, `test_rewriter.py`) — à re-vérifier.
- `chunk_downgraded` (F1) : ajouter à `PipelineEventType` + `EVENTS` front +
  `_KNOWN_BACKEND_EVENTS` dans `test_sse_event_contract.py`.
- Byte-parity `RetryPolicy.default()` : la rampe actuelle est
  attempt1=0.0, attempt2=0.3, attempt3=0.5, pinned 0.0 après hyphen violation,
  max_attempts=3. `default()` doit reproduire EXACTEMENT ça.
- Providers backend concrets restent hors lib (§12) : F14 change leur signature
  chez eux, pas d'ajout de provider dans la lib.

## État d'avancement
- [x] **Tranche 1 — F3, F5, F6, F13, F7** (commits sur `claude/alto-core-v1-release-jgj5wl`)
  - F3/F5 : parser tolère commentaires/PI + coords flottantes (`c9…`).
  - F6 : géométrie slow-path rééquilibrée (arrondi cumulatif). Byte change
    délibéré sur lignes slow-path avec espaces intérieurs ; snapshots backend
    non impactés (ne pinnent pas la largeur token slow-path).
  - F13 : `GuardConfig` (frozen, `FrozenPolicy` + `policy_fingerprint()`),
    seuils des 3 étages, défauts = valeurs actuelles, threadé partout.
    mypy --strict : 2→1 erreur (reste `rewriter.py` `_Attrib`).
  - F7 : `PairingPolicy` injectable (défaut = séquentiel actuel), threadé dans
    `parse_alto_file`/`build_document_manifest`/`_link_hyphen_pairs`.
  - Baseline maintenu vert : lib 86, backend 424, ruff clean.
- [x] **Tranche 2 — F2, F4** (impact snapshot délibéré)
  - F4 : `_line_text_unchanged` strippe les deux côtés (SP de queue → UNTOUCHED).
  - F2 : fast-path retire WC/CC sur String au CONTENT changé ; slow-path
    `_emit_string` ne recycle que ID+STYLEREFS, VPOS/HEIGHT hérités ligne.
  - Snapshots backend bougés délibérément : `test_rewriter.py`
    (test_fast_path_only_content_changes, test_slow_path_preserves_original_attributes)
    et `test_corpus_validation.py` (TestFastPathPreservation) mis à jour vers
    le nouveau contrat (assertions WC/CC → None). Distinction voulu/régression :
    tous les échecs portaient sur WC/CC, aucune régression fonctionnelle.
  - lib 88, backend 424, ruff clean.
- [~] **Tranche 3 — F9, F10, F1 faits ; F8 RESTE**
  - F9 : `RetryPolicy` (frozen) — max_attempts, temperatures (clampées),
    per_chunk_budget, transient/output backoff base. `.default()` = actuel,
    `.deterministic()` = temps 0. Threadé dans `__init__` + `_classify_retry`.
  - F10 : `should_abort` sur `run()`, sondé entre pages/chunks → `CorrectionAborted`
    (module `alto_core/errors.py` : `CorrectionError` base + `CorrectionAborted`).
    Sorties non écrites sur abort.
  - F1 : descente de granularité. `_call_with_retry` scindé en `_attempt_chunk`
    (pur, renvoie (response, attempts_used, can_downgrade, last_msg), NE fallback pas)
    + driver `_run_chunk` (décide downgrade vs fallback, récursif, budget partagé).
    `_subpage_for_lines` re-planifie via le planner normal (force_granularity).
    Événement `chunk_downgraded` → PipelineEventType + EVENTS front +
    `_KNOWN_BACKEND_EVENTS`. Comportement changé : échec transitoire RÉCUPÈRE
    via downgrade (au lieu de fallback). 4 tests backend basculés sur échec
    persistant (test_fallback_on_persistent_failure, test_fallback_on_invalid_json,
    test_fallback_warning_message_*, test_drift_fallback_distinguished élargi).
  - **F8 RESTE À FAIRE** (lignes cibles vs contexte). Le plus complexe de la
    tranche. Touche : chunk_planner (window overlap → marquer contexte),
    ChunkRequest (champ target_line_ids ou context_line_ids), enrich_chunk_lines,
    validator (compte 1:1 sur cibles seulement), pipeline (n'émettre/accepter que
    pour les cibles). Voir §7 F8 + §5.2 dernier point.
  - lib 101, backend 424, ruff clean, mypy --strict = 1 (rewriter `_Attrib`).
- [ ] Tranche 4 — F14, erreurs §8.4 (base déjà posée dans errors.py — reste
      ParseError, ValidationError, reparent HyphenIntegrityError sous
      CorrectionError+ValueError), CorrectionReport public + dry-run `apply=False`,
      F12 (bouger Provider/JobManifest/JobStatus+images vers backend ; PIÈGE
      PageManifest.status/DocumentManifest.status typés JobStatus + _process_page
      fait page.status=JobStatus.COMPLETED), py.typed + `mypy --strict` en CI +
      corriger la dernière erreur mypy (dict(_Attrib) rewriter L~427/454),
      provenance §11 (version lib + policy_fingerprint dans processingStep).
- [ ] Tranche 5 — F11 (rapatrier tests d'algo dans packages/alto-core/tests)
- [ ] CHANGELOG + packaging (py.typed, métadonnées) prêt, NON publié

## Nouveaux symboles publics ajoutés (top-level __all__)
GuardConfig, PairingPolicy, RetryPolicy, CorrectionError, CorrectionAborted.
(schemas expose aussi FrozenPolicy, DEFAULT_GUARD_CONFIG, DEFAULT_PAIRING_POLICY,
DEFAULT_RETRY_POLICY ; CHUNK_DOWNGRADED sur PipelineEventType.)

## Rappel commandes de validation (à relancer après chaque tranche)
- `cd packages/alto-core && python -m pytest tests/` (101 actuellement)
- `cd packages/alto-core && ruff check src/ tests/ && ruff format --check src/ tests/`
- `cd packages/alto-core && python -m mypy --strict src/alto_core` (1 err restante)
- `cd backend && PYTHONPATH=. python -m pytest` (424)

## À remonter à l'utilisateur (ne pas décider seul)
- Renommage paquet `alto-core → corrigenda` (§14) : NON décidé, garder
  `alto_core`/`alto-core` cette session.
- Ne pas toucher au frontend au-delà d'ajouter `chunk_downgraded` à EVENTS.
