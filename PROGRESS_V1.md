# PROGRESS_V1 — corrigenda 1.0.0

État de la livraison de `packages/corrigenda/` selon `SPECS_LIB_V2.md`.
Point de reprise pour toute session fraîche. Le détail des phases P0–P5
vit dans `PLAN_V2.md` (toutes cochées).

## Statut : 1.0.0 PRÊTE À PUBLIER (P0–P5 complètes)

Périmètre livré : F1–F14 + surface §8/§9/§11 (ex-v1.0), **PAGE XML**
(§6.2 P1–P7 + parité §6.3), **protocole d'édition** (§4/§5 complet, y
compris les deux ruptures approuvées : unification
JobTrace→CorrectionReport et résorption §5.1 du run() legacy en
`EditProducer`), docs + snapshot d'API + exemple exécutable, version
**1.0.0** avec CHANGELOG daté 2026-07-06.

Vérification finale (2026-07-06) :
- **lib** : 389 tests verts, `mypy --strict` 0 erreur (31 fichiers),
  contrats exécutables verts (imports §3, sécurité XML, I4 zéro lib
  image, snapshot d'API publique, quickstart exécuté en subprocess)
- **backend** : 265 tests verts ; **frontend** : `tsc --noEmit` clean
- **byte-parity DoD** : goldens sha256 INTACTS depuis la baseline
  `8c4789c` à travers P3+P4+P5 (rewrite sans arguments de provenance ⇒
  hashes indépendants de la version)
- **wheel** : `python -m build` OK, smoke-install en venv isolé OK
  (54 symboles publics, `import corrigenda` sans lxml), version 1.0.0

Publication : NON effectuée (action mainteneur — tag + workflow, voir
« Restes manuels »).

## Byte-parity (DoD §13) — démonstration formelle

Harnais : deux scénarios déterministes (identité ; scripted = fast path sur
1 ligne/3, slow path sur 1 ligne/7) sur `examples/{sample,X0000002}.xml`,
exécutés sur le code baseline (worktree `8c4789c`) ET le code v1.0, puis
diff classifié par TextLine :
- **identité : BYTE-IDENTICAL** sur les deux fichiers ;
- **scripted : uniquement** F2 (WC/CC retirés : 179+5 lignes) et géométrie
  F6/§6.1 (exactement les 81+2 lignes slow-path). Zéro dérive de texte,
  zéro dérive de structure.
Pérennisé par `tests/test_byte_parity_corpus.py` (hashes golden sha256,
indépendants de la version — rewrite sans arguments de provenance).

## Décisions ratifiées par le mainteneur (2026-07-07)

Les trois décisions ci-dessous ont été **ratifiées** après analyse sur
corpus ; la spec (`SPECS_LIB_V2.md` §6.1, F2, F8, §8.2) a été mise à jour
en conséquence.

1. **Liste blanche §6.1 étendue à `STYLE`** (slow path) — RATIFIÉE. La
   lettre initiale ne citait que `ID`+`STYLEREFS`, mais la doctrine F2
   vise les données *périmées* par le changement de texte — le stylage
   (bold/italics) ne l'est pas. Analyse chiffrée (run contrefactuel,
   rewriter avec vs sans l'entrée, lignes stylées forcées en slow path) :
   **45 des 47 `String` stylés de X0000002 détruits** sans l'entrée
   (27 bold, 6 underline, 5 italics, 5 smallcaps, 4 composés), répartis
   sur 25 lignes — presque toutes des manchettes de presse à l'OCR abîmé,
   c.-à-d. précisément les lignes où le LLM change le nombre de mots.
   `STYLE` est désormais normatif dans §6.1.
2. **F8 validateur** — RATIFIÉE (option « cibles uniquement »). Comptage
   1:1 sur les cibles ; sortie contexte optionnelle mais strictement
   vérifiée quand présente, puis écartée. Invariant vérifié sur corpus
   (page de 566 lignes, 52 fenêtres) : chaque ligne est cible dans
   **exactement un** chunk. L'alternative « exiger toutes les lignes »
   forçait des corrections jetées et des retries inutiles — abandonnée.
3. **`CorrectionPipeline(pairing_policy=…)`** — RATIFIÉE. Paramètre de
   provenance uniquement (l'appariement se fait au parse) pour que
   `config_fingerprint()` couvre les quatre politiques §8.2. Contrat
   appelant documenté dans la spec : passer la même `PairingPolicy` qu'au
   parse, sinon l'empreinte estampillée ment.

## Signalé, volontairement NON corrigé (hors périmètre autorisé)

- **Progression frontend** : `lines_done += line_count` sur
  `chunk_completed` surcompte les lignes de recouvrement des fenêtres
  (préexistant, PAS introduit par v1.0). L'événement expose désormais
  `target_count` (le compte exact) — le fix frontend est trivial mais la
  consigne interdit de toucher au frontend au-delà d'EVENTS.
- **Erratum historique git** : le corps du message du commit F12
  (`f7f6904`) a été partiellement mangé par des backticks interprétés par
  le shell. Contenu réel documenté ici et dans le CHANGELOG ; l'historique
  poussé n'a pas été réécrit.
- ~~**JobTrace vs CorrectionReport**~~ : **RÉSOLU en P4** (option A
  validée par le mainteneur) — `JobTrace` supprimé, `trace.json` et
  l'endpoint `/trace` portent le `CorrectionReport` versionné verbatim,
  backend + frontend ajustés.

## Ce qui est livré (résumé par tranche)

1. **F3, F5, F6, F13, F7** — robustesse parser, géométrie slow-path
   (arrondi cumulatif + plancher dégénéré multi-donneurs), `GuardConfig`,
   `PairingPolicy` (gap ignoré inter-pages).
2. **F2, F4** — WC/CC supprimés sur contenu changé ; liste blanche slow
   path `ID`/`STYLEREFS`/`STYLE` ; SP recalculés (layout contigu) ;
   comparaison UNTOUCHED strippée.
3. **F1, F8, F9, F10** — descente de granularité (sur CIBLES uniquement,
   budget `per_chunk_budget`, événement `chunk_downgraded` dans l'enum +
   EVENTS front + contrat SSE), cibles vs contexte (planner, pipeline ET
   validateur), `RetryPolicy` (`default()` byte-identique,
   `deterministic()`), `should_abort` (sondé entre pages, chunks ET
   sous-chunks de descente ; jamais avalé en `chunk_error`).
4. **F14, §8.4, §9, §11, F12, py.typed** — `complete_structured →
   (dict, Usage|None)` (usage par chunk cumulé sur les retries),
   hiérarchie `CorrectionError`, `CorrectionReport` versionné + dry-run
   `apply=False`, provenance (version lib + `config_fingerprint()` public
   couvrant les 4 politiques), enums applicatives déplacées vers
   `backend/app/schemas/job.py`, `mypy --strict` + job CI, `run_sync()`,
   `ChunkPlannerConfig` frozen.
5. **F11** — 8 fichiers de tests d'algo rapatriés (159 tests) ; couverture
   séparée paquet (85 %) / backend (`source=["app"]`, 80 %).

## Commandes de validation

- lib : `cd packages/corrigenda && python -m pytest tests/ --cov=corrigenda`
- lib types : `python -m mypy --strict src/corrigenda`
- lib lint : `ruff check src/ tests/ && ruff format --check src/ tests/`
- backend : `cd backend && PYTHONPATH=. python -m pytest --cov=app`
- backend types : `mypy --explicit-package-bases app`
- frontend : `cd frontend && npx tsc --noEmit && npm run test && npm run lint`
- sécurité : `bandit -r app -c pyproject.toml && pip-audit -r requirements.txt --strict`

## Renommage §14 — DÉCIDÉ ET EXÉCUTÉ : **corrigenda**

Validé par le mainteneur (juillet 2026). Stratégie B (avant toute
publication → zéro alias) : distribution `alto-core` → `corrigenda`,
import `alto_core` → `corrigenda`, répertoire `packages/corrigenda/`,
jobs CI `corrigenda-*`, workflow `publish-corrigenda.yml`, script
`release-corrigenda.sh`, marque frontend « Corrigenda » (App.tsx,
index.html, smoke test), README racine/HF, marque de provenance
`processingStep` → `corrigenda` (sans effet sur les goldens byte-parity :
le corpus n'a pas d'élément Processing). Les docs historiques
(SPECS*/AUDIT/LEDGER/ROADMAP/MIGRATION/ARCHITECTURE) gardent l'ancien nom
comme trace d'époque.

**Restes manuels (actions mainteneur, hors conteneur) :**
- Renommer le dépôt GitHub `alto-llm-corrector` → `corrigenda`
  (Settings → Rename ; GitHub redirige les anciennes URLs), puis mettre à
  jour `[project.urls]` du pyproject et le slug HF Spaces.
- Sur PyPI : le nom `corrigenda` était libre au 5 juillet 2026 (vérifié) —
  le réserver vite via une première publication TestPyPI→PyPI.

## Reste (actions mainteneur, hors conteneur)

- ~~Ratifier les 3 décisions~~ — **fait le 2026-07-07** (voir section
  « Décisions ratifiées » ; spec mise à jour).
- **Publier 1.0.0** (le bump + CHANGELOG daté + build vérifié sont faits) :
  1. `git tag corrigenda-v1.0.0` sur le commit de release, `git push origin corrigenda-v1.0.0` ;
  2. GitHub UI → Actions → « Publish corrigenda » → `testpypi`, vérifier, puis `pypi`
     (Trusted Publishing OIDC — configurer d'abord le *pending publisher*
     sur PyPI/TestPyPI : projet `corrigenda`, repo, workflow
     `publish-corrigenda.yml`, environnements `testpypi`/`pypi`) ;
- Renommer le dépôt GitHub → `corrigenda`, mettre à jour `[project.urls]`
  et le slug HF Spaces (redirections GitHub automatiques).
- Optionnel : publier le site mkdocs (`packages/corrigenda/mkdocs.yml`).
- Optionnel : consommer `target_count` côté frontend (progression exacte).
