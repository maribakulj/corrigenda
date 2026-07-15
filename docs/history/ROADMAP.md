# ROADMAP — alto-llm-corrector

> **Audience :** mainteneurs, contributeurs externes, sponsors/financeurs.
> **Document de référence :** [ARCHITECTURE.md](ARCHITECTURE.md) (cible) et [MIGRATION.md](MIGRATION.md) (comment passer de l'actuel au cible).

---

## Vue d'ensemble

L'objectif est d'amener alto-llm-corrector d'**app monolithique fonctionnelle** à **brique open-source de référence** pour la correction post-OCR ALTO. Le chantier est découpé en 6 phases sur ~6 mois (effort temps-partiel) ou ~3 mois (effort plein-temps).

| Phase | Nom | Effort estimé | Statut |
|---|---|---|---|
| 0 | Stabilisation (bugs critiques) | 2 semaines | ✅ DONE (PR #43) |
| 1 | Outillage qualité + refactoring extraction-ready | 2 semaines | À démarrer |
| 2 | Extraction `alto-core` + `alto-providers` (PyPI) | 2 semaines | Bloqué par Phase 1 |
| 3 | `alto-server` + `alto-cli` | 2 semaines | Bloqué par Phase 2 |
| 4 | Documentation + benchmarks + écosystème | 3 semaines | Bloqué par Phase 3 |
| 5 | Gouvernance + conformité institutionnelle | continu | Démarre en parallèle de la Phase 4 |

---

## Phase 0 — Stabilisation (✅ COMPLÉTÉE)

**Livré dans PR #43 (merge `d2ddd57`) :**

- ✅ XXE protection (`parser.py:250`)
- ✅ Temperature escalation (`orchestrator.py:217-224`)
- ✅ Anthropic structured output via `tools` + `tool_choice`
- ✅ Cross-page hyphen resolution (`page_id` propagé)
- ✅ ZIP bomb mitigation (500 MB / 1000 membres)
- ✅ CORS configurable
- ✅ Sanitisation API keys dans les logs
- ✅ SSE leak frontend corrigé
- ✅ Tests étoffés (+2 390 lignes)
- ✅ Nginx security headers

**Reste explicitement repoussé :** refactoring orchestrator, extraction PyPI, frontend tests — tous traités dans les phases suivantes.

---

## Phase 1 — Outillage qualité + refactoring extraction-ready

**Durée :** 2 semaines · **Bloqueur de :** Phase 2

### 1.1 Outillage qualité (2 jours)

| Tâche | Critère de done |
|---|---|
| Ajouter `ruff` (lint + format) | `ruff check .` et `ruff format --check .` passent sur tout le backend |
| Ajouter `mypy --strict` sur `app/alto/` et `app/jobs/` | Zéro erreur sur ces modules |
| Ajouter `pre-commit` avec hooks ruff/mypy/end-of-file-fixer/trailing-whitespace | Pre-commit installé par défaut via `make setup` ou équivalent |
| Ajouter `pytest-cov` avec seuil 80% | CI échoue si coverage < 80% |
| Ajouter `bandit` (security lint) + `pip-audit` (dépendances) | Zéro finding HIGH/CRITICAL |
| Étendre `.github/workflows/ci.yml` | Étapes : lint, types, tests+coverage, security |
| Ajouter `eslint` + `prettier` côté frontend | `npm run lint` passe |
| Ajouter `vitest` avec 1-2 tests d'exemple sur `useJobStream` | Setup complet pour étoffer plus tard |

### 1.2 Refactoring orchestrator (4 jours)

Découper `backend/app/jobs/orchestrator.py` (788 LOC) en trois unités :

| Nouvelle classe | Responsabilité | Localisation cible | Dépendances |
|---|---|---|---|
| `CorrectionPipeline` | Pipeline pur : reçoit un `DocumentManifest` + `Provider`, retourne un `CorrectedDocument`. Aucun I/O, aucun job store, aucun filesystem. | `app/jobs/correction_pipeline.py` (futur `alto_core/pipeline/`) | `Provider`, `PipelineObserver` (optionnel) |
| `JobRunner` | Orchestre `CorrectionPipeline` + `JobStore` + `OutputWriter`. C'est lui qui émet les SSE events et écrit les sorties. | `app/jobs/runner.py` (futur `alto_server/runner.py`) | `CorrectionPipeline`, `JobStore`, `OutputWriter` |
| `FilesystemOutputWriter` | Implémentation actuelle (`_write_outputs`) extraite proprement. | `app/storage/output_writer.py` (futur `alto_server/adapters/output_writer/filesystem.py`) | aucune |

**Critères de done :**
- `orchestrator.py` n'existe plus, remplacé par les trois unités ci-dessus.
- `CorrectionPipeline` n'importe ni `app.jobs.store` ni `pathlib.Path`.
- Tous les tests existants passent sans modification de leur logique.
- Nouveau test : `test_correction_pipeline.py` qui instancie le pipeline en isolation avec un `MockProvider` et un `MockObserver`.

### 1.3 Injection du JobStore (2 jours)

Faire disparaître le singleton global `job_store = JobStore()` :

- API routes (`backend/app/api/jobs.py`) reçoivent `JobStore` via `Depends(get_job_store)`.
- `JobRunner` reçoit `JobStore` en argument du constructeur.
- `app/main.py` instancie le `JobStore` une seule fois et le passe via `app.state`.
- Tests : remplacer les patches `monkeypatch.setattr(...)` par injection directe.

**Critères de done :**
- `grep "from app.jobs.store import job_store"` retourne 0 résultats hors `main.py`.
- Tests passent.

### 1.4 Protocols formalisés (2 jours)

Créer `app/protocols/` (futur `alto_core/protocols/`) avec :

- `BaseProvider` (déplacement de `providers/base.py`, déjà un Protocol)
- `PipelineObserver` (nouveau)
- `OutputWriter` (nouveau)
- `JobStore` (extrait du module concret, devient un Protocol que `InMemoryJobStore` implémente)

**Critères de done :**
- Chaque implémentation concrète est testée pour conformité au Protocol via un test générique paramétré.

### Métriques de succès Phase 1

- `orchestrator.py` n'existe plus.
- Aucun singleton global (sauf le registry providers, acceptable).
- `mypy --strict` passe sur tout `app/alto/`, `app/jobs/`, `app/providers/`.
- Coverage ≥ 80% backend.
- CI avec 5 étapes : lint, types, tests+coverage, security, build.

---

## Phase 2 — Extraction `alto-core` + `alto-providers` (PyPI)

**Durée :** 2 semaines · **Bloque :** Phase 3

### 2.1 Mise en place du monorepo (1 jour)

- Créer la structure `packages/alto-core/`, `packages/alto-providers/`, `packages/alto-server/`, `packages/alto-cli/`.
- `pyproject.toml` racine en mode workspace `uv`.
- Migration des dépendances dev dans `pyproject.toml` racine (ruff, mypy, pytest, pre-commit).
- Workflow CI mis à jour pour exécuter `uv sync` puis tester chaque package.

### 2.2 `alto-core` (5 jours)

**Déplacements :**

| Source actuelle | Destination |
|---|---|
| `backend/app/alto/parser.py` | `packages/alto-core/src/alto_core/alto/parser.py` |
| `backend/app/alto/rewriter.py` | `packages/alto-core/src/alto_core/alto/rewriter.py` |
| `backend/app/alto/hyphenation.py` | `packages/alto-core/src/alto_core/alto/hyphenation.py` |
| `backend/app/alto/_norm.py` ⚠ | `packages/alto-core/src/alto_core/alto/_norm.py` |
| `backend/app/alto/_ns.py` ⚠ | `packages/alto-core/src/alto_core/alto/_ns.py` |

> ⚠ Shim backend supprimé après extraction (`618be08`, L8 corrective wave). Seule la version alto-core subsiste — cf. REMEDIATION_STATUS.md / S6.
| `backend/app/jobs/chunk_planner.py` | `packages/alto-core/src/alto_core/pipeline/chunk_planner.py` |
| `backend/app/jobs/validator.py` | `packages/alto-core/src/alto_core/pipeline/validator.py` |
| `backend/app/jobs/line_acceptance.py` | `packages/alto-core/src/alto_core/pipeline/line_acceptance.py` |
| `backend/app/jobs/correction_pipeline.py` (Phase 1) | `packages/alto-core/src/alto_core/pipeline/correction_pipeline.py` |
| Modèles purs de `backend/app/schemas/__init__.py` | `packages/alto-core/src/alto_core/schemas/__init__.py` |
| Protocols (Phase 1) | `packages/alto-core/src/alto_core/protocols/` |

**Tests :** copier les tests pertinents dans `packages/alto-core/tests/`. Tous doivent passer sans modification.

**API publique :** voir [ARCHITECTURE.md §4.1](ARCHITECTURE.md#41-alto-core).

**Publication :**
- Version `0.1.0a1` sur **TestPyPI** d'abord.
- Validation manuelle par 1-2 early adopters (eScriptorium, BnF Labs, équipe Transkribus si contact possible).
- Si OK : `0.1.0` sur PyPI via Trusted Publishing (OIDC).

### 2.3 `alto-providers` (4 jours)

**Déplacements :**

| Source | Destination |
|---|---|
| `backend/app/providers/base.py` (helpers HTTP) | `packages/alto-providers/src/alto_providers/base.py` |
| `backend/app/providers/openai_provider.py` | `packages/alto-providers/src/alto_providers/openai.py` |
| `backend/app/providers/anthropic_provider.py` | `packages/alto-providers/src/alto_providers/anthropic.py` |
| `backend/app/providers/google_provider.py` | `packages/alto-providers/src/alto_providers/google.py` |
| `backend/app/providers/mistral_provider.py` | `packages/alto-providers/src/alto_providers/mistral.py` |
| Mocks de test | `packages/alto-providers/src/alto_providers/mock.py` |

**Améliorations à inclure :**
- Retry HTTP avec backoff exponentiel (tenacity ou impl. maison).
- `httpx.AsyncClient` partagé via context manager.
- Tests de conformité paramétrés (`pytest.mark.parametrize` sur chaque provider).

**Publication :** même processus que `alto-core`, `0.1.0a1` → `0.1.0`.

### Métriques de succès Phase 2

- `alto-core 0.1.0` publié sur PyPI.
- `alto-providers 0.1.0` publié sur PyPI.
- `pip install alto-core` fonctionne sur Python 3.11, 3.12, 3.13.
- Notebook quickstart démontrant l'usage en bibliothèque (sans alto-server).
- Au moins une intégration tierce documentée (un script d'exemple qui utilise `alto-core` dans un autre projet).

---

## Phase 3 — `alto-server` + `alto-cli`

**Durée :** 2 semaines · **Bloque :** Phase 4

### 3.1 `alto-server` (5 jours)

**Refactoring :**

| Source | Destination |
|---|---|
| `backend/app/main.py` | `packages/alto-server/src/alto_server/app.py` (`create_app`) |
| `backend/app/api/jobs.py` | `packages/alto-server/src/alto_server/api/jobs.py` |
| `backend/app/api/providers.py` | `packages/alto-server/src/alto_server/api/providers.py` |
| `backend/app/jobs/store.py` (`InMemoryJobStore`) | `packages/alto-server/src/alto_server/adapters/job_store/memory.py` |
| `backend/app/storage/` (filesystem) | `packages/alto-server/src/alto_server/adapters/storage/filesystem.py` |
| `backend/app/jobs/runner.py` (Phase 1) | `packages/alto-server/src/alto_server/runner.py` |

**Nouveautés :**

- `create_router()` factory : permet de monter alto-server comme sous-router d'une app FastAPI tierce.
- Endpoint `/metrics` Prometheus (opt-in via env var).
- Middleware OpenTelemetry (opt-in).
- Middleware Sentry (opt-in).
- Rate limiting via `slowapi` (opt-in, configurable par endpoint).
- Endpoint `/healthz` et `/readyz` séparés (k8s standard).

**Critères de done :**
- App tierce peut faire `app.include_router(create_router(...), prefix="/ocr")`.
- Tests d'intégration de mount dans une app FastAPI vide.
- OpenAPI 3.1 exposée et validée.

### 3.2 `alto-cli` (4 jours)

Implémenter les commandes listées dans [ARCHITECTURE.md §4.3](ARCHITECTURE.md#43-alto-cli) avec Typer + Rich.

**Critères de done :**
- `altocorrect run <file> --provider mistral --api-key-env MISTRAL_API_KEY` fonctionne.
- `altocorrect batch ./scans/ --concurrency 8` fonctionne avec barre de progression.
- Aide complète sur `altocorrect --help` et chaque sous-commande.
- Tests CLI avec `typer.testing.CliRunner`.

### 3.3 Frontend tests (3 jours)

- Vitest pour `useJobStream`, `DiffViewer`, `LayoutViewer` (les composants à logique).
- Playwright E2E sur le flux principal (upload → progression → download) avec un backend mocké.
- i18n setup avec `react-i18next` (au moins FR + EN).
- Génération du client API depuis l'OpenAPI de `alto-server` via `openapi-typescript`.

### Métriques de succès Phase 3

- `alto-server 0.1.0` publié sur PyPI et image Docker sur GHCR.
- `alto-cli 0.1.0` publié sur PyPI (`pip install alto-cli`).
- Image Docker signée Sigstore/cosign.
- Tests frontend : ≥ 1 test par composant à logique, 1 E2E nominal.
- App tierce qui mount alto-server : exemple complet dans `examples/`.

---

## Phase 4 — Documentation + benchmarks + écosystème

**Durée :** 3 semaines · **Bloque :** rien (mais conditionne l'adoption)

### 4.1 Documentation (1 semaine)

- Migration de tout le contenu pertinent depuis `SPECS_*.md` vers `docs/` (MkDocs Material).
- Structure :
  - `Getting started` (3 quickstarts : bibliothèque, CLI, serveur)
  - `Tutorials` (intégration FastAPI tierce, intégration Airflow, écriture d'un provider custom)
  - `Architecture` (ce document + ADRs)
  - `Reference` (mkdocstrings auto)
  - `Benchmarks` (résultats publiés)
  - `Contributing`
- Déploiement automatique sur GitHub Pages via workflow.
- Versioning des docs (mike) : `latest`, `stable`, `v0.1`, etc.

### 4.2 Benchmarks reproductibles (1 semaine)

- Constitution d'un dataset de 100 pages ALTO annotées manuellement :
  - Sources libres : Gallica, Wikisource, Library of Congress, Europeana.
  - Annotations : texte ground-truth par ligne.
- Scripts dans `benchmarks/runners/` qui :
  - Lancent la correction pour chaque (provider, model) configuré.
  - Calculent CER, WER, taux de hyphen pairs correctement réconciliés.
  - Génèrent un rapport HTML reproductible.
- Workflow CI hebdomadaire qui publie les résultats dans `docs/benchmarks/`.
- Comparatif provider × model avec coûts estimés (€/1000 pages).

### 4.3 Écosystème (1 semaine)

- **Notebook quickstart Colab** : "Corriger un OCR Gallica en 5 minutes" avec lien direct.
- **Bridge eScriptorium** : script Python qui prend un export eScriptorium, le corrige, le réimporte. PR à proposer dans leur repo si possible.
- **Bridge Transkribus** : pareil pour les exports Transkribus (PAGE XML → conversion ALTO → correction → reconversion).
- **Exemple DAG Airflow** : pipeline complet (download depuis IA Archive → correction → upload S3).
- **Plugin OCR4all** : si l'API le permet.

### Métriques de succès Phase 4

- Documentation déployée et indexée par Google.
- Au moins 3 sources de trafic externes (eScriptorium, blog post DH, conférence DH).
- Benchmark hebdomadaire qui tourne et publie.
- ≥ 1 intégration tierce documentée et validée par son mainteneur.

---

## Phase 5 — Gouvernance + conformité institutionnelle

**Durée :** continue, démarre en parallèle de la Phase 4

### 5.1 Gouvernance OSS

- `CONTRIBUTING.md` complet : workflow PR, conventions de commit (Conventional Commits), tests requis, signature DCO.
- `CODE_OF_CONDUCT.md` (Contributor Covenant v2.1).
- `GOVERNANCE.md` : qui décide, comment, processus pour devenir mainteneur.
- `SECURITY.md` : processus de divulgation responsable, GPG key, SLA réponse.
- Templates issues : bug report, feature request, question, security (privé).
- Template PR : checklist tests, docs, changelog.
- Labels normalisés (`good-first-issue`, `help-wanted`, `breaking-change`, etc.).
- Stale bot configuration.

### 5.2 Conformité institutionnelle

- **CITATION.cff** : permet aux chercheurs de citer l'outil (intégration GitHub native).
- **DOI Zenodo** : à chaque release, archivage automatique avec DOI permanent.
- **SBOM CycloneDX** : généré à chaque release, attaché aux artefacts GitHub.
- **OpenSSF Scorecard** : viser ≥ 7/10, badge dans le README.
- **OpenSSF Best Practices badge** : viser le niveau "passing" puis "silver".
- **REUSE compliance** : tous les fichiers ont une licence claire (`.reuse/dep5` ou en-têtes SPDX).
- **CHANGELOG.md** : tenu manuellement (ou via release-drafter), suit Keep a Changelog.
- **Releases GitHub** : notes de release rédigées, artefacts signés, lien Zenodo.

### 5.3 Modèle économique soutenable (optionnel mais recommandé)

- Version OSS toujours 100% libre, AGPLv3 ou Apache 2.0 (à arbitrer).
- Offre **support pro** pour institutions : SLA, prioritisation features, audit conformité.
- Offre **SaaS managé** : pour ceux qui ne veulent pas auto-héberger.
- Sponsoring GitHub / Open Collective / NumFOCUS Fiscal Sponsorship pour collecter des dons institutionnels.
- Demandes de financement : NLnet Foundation (NGI Zero), Sloan Foundation, Mellon Foundation (très actif sur DH/GLAM).

### Métriques de succès Phase 5

- OpenSSF Scorecard ≥ 7/10.
- DOI Zenodo permanent pour chaque release.
- ≥ 3 contributeurs externes ayant mergé au moins 1 PR.
- Mentionné dans ≥ 1 publication académique ou ≥ 1 documentation institutionnelle (BnF, Library of Congress, etc.).

---

## Timeline visuelle

```
Mois 1               Mois 2               Mois 3               Mois 4-6
├── Phase 1 ─────────┤
                     ├── Phase 2 ─────────┤
                                          ├── Phase 3 ─────────┤
                                                               ├── Phase 4 ──────────┤
                     ├── Phase 5 (continu) ──────────────────────────────────────────►
```

---

## Métriques de succès globales (à 6 mois)

| Métrique | Cible |
|---|---|
| `alto-core` téléchargements PyPI / mois | 1 000+ |
| Étoiles GitHub | 200+ |
| Contributeurs externes (commits mergés) | 5+ |
| Pull requests externes mergées | 20+ |
| Intégrations tierces documentées | 3+ |
| Couverture tests `alto-core` | ≥ 90% |
| OpenSSF Scorecard | ≥ 7/10 |
| Présence dans ≥ 1 doc institutionnelle | oui |
| Présence dans ≥ 1 publication académique | oui |

---

## Risques & mitigations

| Risque | Probabilité | Impact | Mitigation |
|---|---|---|---|
| Refactoring orchestrator casse des tests subtils | Moyenne | Moyen | Faire le refactoring par petites étapes, tests après chaque étape. Coverage 90% avant de commencer. |
| Adoption PyPI lente | Élevée | Moyen | Investir dans la Phase 4 (doc, benchmarks, écosystème). Aller chercher activement les early adopters. |
| Changements d'API LLM cassent les providers | Continue | Faible | Tests d'intégration avec mocks réalistes, suivi des changelogs vendors, retry/fallback robustes. |
| Conflit de versions Python (3.11 vs 3.13) | Faible | Moyen | Matrix CI sur 3.11, 3.12, 3.13. Pin lxml à des versions stables. |
| Sécurité : faille découverte après publication PyPI | Moyenne | Élevé | SECURITY.md avec processus rapide. Pre-release sur TestPyPI. Audit interne avant chaque MAJOR. |
| Burnout maintenance solo | Élevée | Critique | Recruter mainteneurs tôt (Phase 4). Modèle économique soutenable (Phase 5.3). |

---

## Décisions à arbitrer rapidement

1. **Licence finale** : Apache 2.0 (permissive, adoption maximale) ou AGPLv3 (protège contre SaaS forks non-contributifs) ? **Recommandation : Apache 2.0** sauf si offre SaaS commerciale prévue.
2. **Gestion monorepo** : `uv workspaces` (recommandé) ou `hatch` ou `poetry` ? **Recommandation : uv**.
3. **Nom PyPI** : `alto-core` est-il disponible ? Sinon : `altoxml-core`, `alto-corrector-core`, `pyalto-core`. À vérifier dès maintenant.
4. **API publique sync ou async only** ? **Recommandation : async primaire + wrappers sync** dans `alto_core.sync` pour scripts simples.
5. **Compatibilité Python** : 3.11+ (recommandé, moderne) ou 3.10+ (plus large) ? **Recommandation : 3.11+** (déjà la base actuelle).
