# PROGRESS_V1 — alto-core v1.0

État de la livraison v1.0 de `packages/alto-core/` selon `SPECS_LIB_V2.md`.
Point de reprise pour toute session fraîche.

## Statut : v1.0 COMPLÈTE (F1–F14 + surface §8/§9/§11) + rounds correctifs post-audit

Vérification finale (voir « Preuves » plus bas) :
- **lib** : 298 tests, couverture ~86 % (gate 85 %), `mypy --strict` 0 erreur, ruff clean
- **backend** : 265 tests, couverture ~84 % (gate 80 %), mypy clean, ruff clean
- **frontend** : `tsc --noEmit` clean, vitest 12/12, eslint clean, prettier clean
- **sécurité** : bandit clean, `pip-audit --strict` 0 vulnérabilité
- **byte-parity DoD** : démontrée vs le commit baseline `8c4789c` (voir ci-dessous)

Version : reste `0.1.0a1`, tout sous `[Unreleased]`. Le bump 1.0.0, le nom
PyPI (`corrigenda` ? §14) et la publication sont des décisions produit
réservées à l'utilisateur. Packaging prêt (py.typed dans le wheel), NON publié.

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

## Décisions prises à ratifier par l'utilisateur

1. **Liste blanche §6.1 étendue à `STYLE`** (slow path). La lettre de la
   spec ne cite que `ID`+`STYLEREFS`, mais sa doctrine vise les données
   *périmées* par le changement de texte — le stylage (bold/italics) ne
   l'est pas, et le supprimer détruisait du formatage réel sur le corpus
   (30+ String stylés dans X0000002). Revenir à la lettre = retirer
   `"STYLE"` du tuple dans `rewriter._emit_string`.
2. **F8 validateur** : implémenté à la lettre (comptage 1:1 sur les
   cibles ; sortie contexte optionnelle, mais strictement vérifiée quand
   présente). L'alternative « exiger toutes les lignes » a été abandonnée.
3. **`CorrectionPipeline(pairing_policy=…)`** : paramètre AJOUTÉ, à des fins
   de provenance uniquement (l'appariement se fait au parse) — pour que
   `config_fingerprint()` couvre les quatre politiques §8.2.

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
- **JobTrace vs CorrectionReport** : deux artefacts quasi identiques
  (trace.json persistée vs rapport public §9). Unification différée à la
  v2.0 (changer le schéma de trace.json casserait les consommateurs
  backend) — candidate à une dépréciation documentée.

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

- lib : `cd packages/alto-core && python -m pytest tests/ --cov=alto_core`
- lib types : `python -m mypy --strict src/alto_core`
- lib lint : `ruff check src/ tests/ && ruff format --check src/ tests/`
- backend : `cd backend && PYTHONPATH=. python -m pytest --cov=app`
- backend types : `mypy --explicit-package-bases app`
- frontend : `cd frontend && npx tsc --noEmit && npm run test && npm run lint`
- sécurité : `bandit -r app -c pyproject.toml && pip-audit -r requirements.txt --strict`

## Reste (produit, à décider par l'utilisateur)

- Ratifier les 3 décisions ci-dessus (surtout STYLE §6.1).
- Bump `1.0.0` + entrée CHANGELOG datée, nom PyPI (§14), publication.
- Optionnel : consommer `target_count` côté frontend (progression exacte).
