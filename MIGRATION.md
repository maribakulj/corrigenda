# MIGRATION — de l'app actuelle à l'architecture cible

> **Audience :** mainteneurs et contributeurs qui vont exécuter les phases 1 à 3 de la [ROADMAP.md](ROADMAP.md).
> **Référence :** [ARCHITECTURE.md](ARCHITECTURE.md) (cible).

Ce document décrit **précisément** comment passer de l'arborescence actuelle à l'arborescence cible, sans casser l'app existante, sans rupture de fonctionnalité pour les utilisateurs.

---

## 1. Stratégie générale

### 1.1 Trois principes directeurs

1. **Aucun big bang.** Chaque étape produit un commit qui compile, passe les tests, et déploie. Pas de PR de 5 000 lignes.
2. **Tests d'abord.** Avant chaque déplacement, s'assurer que la couverture du module à déplacer est ≥ 90%. Si non, écrire les tests manquants en premier.
3. **Compatibilité ascendante stricte pendant la transition.** Tant que `alto-server` n'est pas en `2.0`, les imports `from app.X` continuent de marcher (via réexports), les routes HTTP ne bougent pas, le format des fichiers de sortie est inchangé.

### 1.2 Outillage à mettre en place avant tout (étape 0)

- `ruff` + `mypy --strict` + `pre-commit` + `pytest-cov`.
- Coverage gate à 80% qui fail la CI (sera relevé à 90% sur `alto-core` après extraction).
- Branche `main` protégée : pas de push direct, PR obligatoire, CI obligatoire avant merge.

---

## 2. Inventaire de l'état actuel

### 2.1 Structure backend actuelle

```
backend/
├── app/
│   ├── alto/                       1 470 LOC — domaine pur, déjà presque PyPI-ready
│   │   ├── parser.py               446 LOC
│   │   ├── rewriter.py             622 LOC
│   │   ├── hyphenation.py          343 LOC
│   │   ├── _norm.py                35 LOC   ← snapshot d'origine ; shim supprimé post-extraction (cf. REMEDIATION_STATUS.md L8/S6)
│   │   └── _ns.py                  24 LOC   ← idem
│   ├── api/                        432 LOC — FastAPI routes
│   │   ├── jobs.py                 404 LOC — couplé à job_store global
│   │   └── providers.py            28 LOC
│   ├── jobs/                       1 474 LOC — mélange domaine + I/O
│   │   ├── orchestrator.py         788 LOC — gros morceau, à découper
│   │   ├── chunk_planner.py        330 LOC — pur, prêt à extraire
│   │   ├── validator.py            200 LOC — pur, prêt à extraire
│   │   ├── line_acceptance.py      204 LOC — pur, prêt à extraire
│   │   └── store.py                152 LOC — InMemoryJobStore + singleton
│   ├── providers/                  421 LOC — abstraction propre
│   │   ├── base.py                 131 LOC — Protocol + helpers HTTP
│   │   ├── openai_provider.py      82 LOC
│   │   ├── anthropic_provider.py   138 LOC
│   │   ├── google_provider.py      98 LOC
│   │   ├── mistral_provider.py     72 LOC
│   │   └── __init__.py             30 LOC — registry
│   ├── schemas/                    290 LOC — Pydantic models
│   ├── storage/                    250 LOC — filesystem I/O
│   └── main.py                     100 LOC — FastAPI app factory
├── tests/                          7 746 LOC — 18 fichiers
└── requirements.txt                déps non lockées
```

### 2.2 Couplages problématiques recensés

| # | Fichier:ligne | Symptôme | Cible |
|---|---|---|---|
| C1 | `app/jobs/store.py:152` | `job_store = JobStore()` singleton | Injection via FastAPI Depends + ctor |
| C2 | `app/jobs/orchestrator.py:16` | `from app.jobs.store import job_store` | Receveur via argument |
| C3 | `app/jobs/orchestrator.py:545` | `_write_outputs()` écrit directement sur disque | Extraire en `OutputWriter` injectable |
| C4 | `app/api/jobs.py:18` | `from app.jobs.store import job_store` (9 usages) | `Depends(get_job_store)` |
| C5 | `app/storage/__init__.py:14` | `_BASE_DIR = Path(os.environ.get("JOB_STORAGE_DIR", "/tmp/app-jobs"))` | Config injectée dans le `FilesystemStorage` |
| C6 | `app/jobs/orchestrator.py` | 788 LOC mélangent pipeline pur, traces, SSE, I/O | Découpe en `CorrectionPipeline` / `JobRunner` / `OutputWriter` |
| C7 | `app/providers/__init__.py:11` | Registry singleton statique | Acceptable, mais permettre `register_provider()` runtime |

---

## 3. Mapping actuel → cible

### 3.1 Table de correspondance complète

| Source actuelle | Destination cible | Package | Phase |
|---|---|---|---|
| `backend/app/alto/parser.py` | `packages/alto-core/src/alto_core/alto/parser.py` | alto-core | 2 |
| `backend/app/alto/rewriter.py` | `packages/alto-core/src/alto_core/alto/rewriter.py` | alto-core | 2 |
| `backend/app/alto/hyphenation.py` | `packages/alto-core/src/alto_core/alto/hyphenation.py` | alto-core | 2 |
| `backend/app/alto/_norm.py` ⚠ | `packages/alto-core/src/alto_core/alto/_norm.py` | alto-core | 2 |
| `backend/app/alto/_ns.py` ⚠ | `packages/alto-core/src/alto_core/alto/_ns.py` | alto-core | 2 |

> ⚠ Le shim côté `backend/` a été **supprimé** après extraction (commit `618be08`, L8 corrective wave) : zéro consumer interne. La version alto-core reste la seule source.
| `backend/app/jobs/chunk_planner.py` | `packages/alto-core/src/alto_core/pipeline/chunk_planner.py` | alto-core | 2 |
| `backend/app/jobs/validator.py` | `packages/alto-core/src/alto_core/pipeline/validator.py` | alto-core | 2 |
| `backend/app/jobs/line_acceptance.py` | `packages/alto-core/src/alto_core/pipeline/line_acceptance.py` | alto-core | 2 |
| `backend/app/jobs/orchestrator.py` (extraction pure) | `packages/alto-core/src/alto_core/pipeline/correction_pipeline.py` | alto-core | 1+2 |
| `backend/app/jobs/orchestrator.py` (orchestration I/O) | `packages/alto-server/src/alto_server/runner.py` | alto-server | 1+3 |
| `backend/app/jobs/store.py` | `packages/alto-server/src/alto_server/adapters/job_store/memory.py` | alto-server | 3 |
| `backend/app/schemas/__init__.py` (modèles purs) | `packages/alto-core/src/alto_core/schemas/__init__.py` | alto-core | 2 |
| `backend/app/schemas/__init__.py` (modèles HTTP) | `packages/alto-server/src/alto_server/schemas.py` | alto-server | 3 |
| `backend/app/providers/base.py` | `packages/alto-providers/src/alto_providers/base.py` (split: Protocol → alto-core) | alto-core + alto-providers | 2 |
| `backend/app/providers/openai_provider.py` | `packages/alto-providers/src/alto_providers/openai.py` | alto-providers | 2 |
| `backend/app/providers/anthropic_provider.py` | `packages/alto-providers/src/alto_providers/anthropic.py` | alto-providers | 2 |
| `backend/app/providers/google_provider.py` | `packages/alto-providers/src/alto_providers/google.py` | alto-providers | 2 |
| `backend/app/providers/mistral_provider.py` | `packages/alto-providers/src/alto_providers/mistral.py` | alto-providers | 2 |
| `backend/app/providers/__init__.py` (registry) | `packages/alto-providers/src/alto_providers/__init__.py` | alto-providers | 2 |
| `backend/app/api/jobs.py` | `packages/alto-server/src/alto_server/api/jobs.py` | alto-server | 3 |
| `backend/app/api/providers.py` | `packages/alto-server/src/alto_server/api/providers.py` | alto-server | 3 |
| `backend/app/storage/__init__.py` | `packages/alto-server/src/alto_server/adapters/storage/filesystem.py` | alto-server | 3 |
| `backend/app/main.py` | `packages/alto-server/src/alto_server/app.py` (create_app) + `entry.py` | alto-server | 3 |
| `frontend/` | `packages/alto-web/` (déplacement simple, structure interne conservée) | alto-web | 3 |

### 3.2 Schemas : split entre core et server

| Schéma | Destination |
|---|---|
| `DocumentManifest`, `PageManifest`, `BlockManifest`, `LineManifest` | `alto-core` |
| `HyphenRole`, `LLMLineInput`, `LLMLineOutput` | `alto-core` |
| `ChunkPlan`, `ChunkGranularity` | `alto-core` |
| `PipelineConfig`, `PipelineEvent` | `alto-core` |
| `JobManifest`, `JobStatus`, `Provider` (enum) | ~~`alto-core`~~ → **backend `app.schemas.job`** (révisé par SPECS_LIB_V2 F12 : concepts serveur, sortis du cœur en v1.0) |
| `JobCreateRequest`, `JobCreateResponse`, `ModelsRequest`, etc. (HTTP payloads) | `alto-server` |
| `SSEEvent`, `LayoutData`, `DiffData`, `TraceData` (réponses API) | `alto-server` |

### 3.3 Tests : redistribution

| Tests actuels | Destination |
|---|---|
| `test_parser.py`, `test_hyphenation.py`, `test_rewriter.py` | `packages/alto-core/tests/` |
| `test_chunk_planner.py`, `test_validator.py`, `test_line_acceptance.py` | `packages/alto-core/tests/` |
| `test_chained_hyphenation.py`, `test_double_dash.py`, `test_x0000002.py` | `packages/alto-core/tests/` |
| `test_corpus_validation.py`, `test_corpus_present.py` | `packages/alto-core/tests/` |
| `test_providers.py`, `test_sanitize_error.py` | `packages/alto-providers/tests/` |
| `test_orchestrator.py` | split : pipeline pur → `alto-core`, runner → `alto-server` |
| `test_store.py` | `packages/alto-server/tests/` |
| `test_api.py`, `test_integration.py` | `packages/alto-server/tests/` |
| `test_trace.py` | split : trace metadata → `alto-core`, persistance → `alto-server` |

---

## 4. Plan d'exécution étape par étape

Chaque étape est un commit indépendant, mergeable seul, qui passe les tests.

### Phase 1 — Refactoring extraction-ready (avant tout déplacement)

#### Étape 1.1 — Outillage qualité

Commit : `chore: setup ruff, mypy, pre-commit, coverage`

Fichiers ajoutés :
- `pyproject.toml` racine (ou `backend/pyproject.toml`) avec config ruff + mypy + pytest + coverage.
- `.pre-commit-config.yaml` avec hooks ruff, mypy, end-of-file-fixer, trailing-whitespace.
- `.github/workflows/ci.yml` étendu : lint → types → tests+coverage → security.

Coverage gate : 80% backend.

#### Étape 1.2 — Protocols formalisés

Commit : `refactor: introduce protocols for JobStore, Observer, OutputWriter`

Fichier nouveau : `backend/app/protocols/__init__.py`

```python
from typing import Protocol, runtime_checkable, AsyncIterator
from app.schemas import (
    JobManifest, JobStatus, CorrectedDocument,
    PipelineEvent, DocumentManifest, LLMLineInput, LLMLineOutput,
)

@runtime_checkable
class BaseProvider(Protocol):
    name: str
    async def list_models(self, api_key: str) -> list[str]: ...
    async def complete_structured(self, *, api_key: str, model: str,
                                  system_prompt: str, lines: list[LLMLineInput],
                                  json_schema: dict, temperature: float = 0.0
                                  ) -> list[LLMLineOutput]: ...

@runtime_checkable
class PipelineObserver(Protocol):
    async def on_event(self, event: PipelineEvent) -> None: ...

@runtime_checkable
class OutputWriter(Protocol):
    async def write(self, document: CorrectedDocument, *, job_id: str) -> str: ...

@runtime_checkable
class JobStore(Protocol):
    async def create_job(self, ...) -> str: ...
    async def get_job(self, job_id: str) -> JobManifest | None: ...
    async def update_job(self, job_id: str, **fields) -> None: ...
    async def emit(self, job_id: str, event_type: str, payload: dict) -> None: ...
    async def stream_events(self, job_id: str) -> AsyncIterator[dict]: ...
    async def cleanup_job(self, job_id: str) -> None: ...
```

Existing classes deviennent des implémentations de ces Protocols (rien à changer côté code grâce au duck typing).

#### Étape 1.3 — Découpe orchestrator (5 commits successifs)

**Commit 1.3.a — Extraire `_write_outputs` → `FilesystemOutputWriter`**

- Créer `backend/app/storage/output_writer.py` :

```python
from pathlib import Path
from app.schemas import CorrectedDocument

class FilesystemOutputWriter:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir

    async def write(self, document: CorrectedDocument, *, job_id: str) -> str:
        # Logique de _write_outputs() actuelle
        ...
```

- Remplacer dans `orchestrator.py:545` les appels par `await output_writer.write(...)`.
- Tests : nouveau `test_output_writer.py` avec un `tmp_path`.

**Commit 1.3.b — Extraire `CorrectionPipeline` (pipeline pur)**

- Créer `backend/app/jobs/correction_pipeline.py` qui ne contient QUE la logique pure de :
  - Planification chunks (déjà dans `chunk_planner`, juste orchestration)
  - Enrichissement hyphenation
  - Appel provider
  - Validation
  - Réconciliation
  - Application des corrections au manifest

- Cette classe :
  - **N'importe pas** `app.jobs.store`.
  - **N'importe pas** `pathlib.Path` ni `aiofiles`.
  - Reçoit un `BaseProvider` et un `PipelineObserver` optionnel.
  - Émet tous les événements via l'observer.

```python
class CorrectionPipeline:
    def __init__(self, provider: BaseProvider, config: PipelineConfig,
                 observer: PipelineObserver | None = None):
        self.provider = provider
        self.config = config
        self.observer = observer or NoOpObserver()

    async def run(self, document: DocumentManifest, *,
                  api_key: str, model: str) -> CorrectedDocument:
        ...
```

- Tests : `test_correction_pipeline.py` avec `MockProvider` et `RecordingObserver`.

**Commit 1.3.c — Extraire `JobRunner`**

- Créer `backend/app/jobs/runner.py` qui :
  - Orchestre `CorrectionPipeline` + `JobStore` + `OutputWriter`.
  - Émet les SSE events via `JobStore`.
  - Écrit les sorties via `OutputWriter`.
  - Gère le cycle de vie du job (created → running → completed/failed).

```python
class JobRunner:
    def __init__(self, pipeline: CorrectionPipeline,
                 job_store: JobStore, output_writer: OutputWriter):
        ...

    async def run(self, job_id: str, ...) -> None:
        ...
```

- Cette classe **est** le pont entre le pipeline pur et l'infrastructure.

**Commit 1.3.d — Supprimer `orchestrator.py`**

- L'ancien fichier est vidé, remplacé par un alias d'import pour compatibilité ascendante :

```python
# backend/app/jobs/orchestrator.py
from app.jobs.runner import JobRunner
from app.jobs.correction_pipeline import CorrectionPipeline

# Compatibilité : ancien `run_job(job_id, ...)` redevient une fonction qui
# instancie le runner avec le job_store global et délègue.
async def run_job(job_id: str, ...) -> None:
    runner = JobRunner(...)
    await runner.run(job_id, ...)
```

- Tous les tests passent toujours sans modification.

**Commit 1.3.e — Test de conformité Protocol**

- Ajouter `test_protocols_conformance.py` qui vérifie :
  - `OpenAIProvider`, `AnthropicProvider`, etc. implémentent `BaseProvider`.
  - `InMemoryJobStore` implémente `JobStore`.
  - `FilesystemOutputWriter` implémente `OutputWriter`.

#### Étape 1.4 — Injection JobStore (3 commits)

**Commit 1.4.a — JobStore via FastAPI Depends**

- Ajouter `backend/app/api/deps.py` :

```python
from fastapi import Request
from app.protocols import JobStore as JobStoreProtocol

def get_job_store(request: Request) -> JobStoreProtocol:
    return request.app.state.job_store
```

- Modifier `main.py` pour stocker l'instance dans `app.state.job_store`.
- Modifier `api/jobs.py` pour utiliser `Depends(get_job_store)` au lieu de l'import global.

**Commit 1.4.b — JobStore dans JobRunner**

- `JobRunner.__init__` accepte le `JobStore` en argument.
- `run_job()` (le wrapper de compatibilité) le récupère depuis `app.state` ou utilise toujours l'ancien singleton si appelé hors d'un contexte FastAPI.

**Commit 1.4.c — Supprimer le singleton global**

- `backend/app/jobs/store.py` : retirer la dernière ligne `job_store = JobStore()`.
- Tous les imports `from app.jobs.store import job_store` doivent avoir disparu (vérifié par grep).
- L'instanciation se fait uniquement dans `main.py:create_app()`.

### Phase 2 — Extraction `alto-core` + `alto-providers`

#### Étape 2.1 — Mise en place workspace

Commit : `chore: setup uv workspace with empty packages`

- `pyproject.toml` racine :

```toml
[tool.uv.workspace]
members = [
    "packages/alto-core",
    "packages/alto-providers",
    "packages/alto-cli",
    "packages/alto-server",
]
```

- Créer la structure de dossiers vides avec `pyproject.toml` minimal pour chaque package.
- Ajuster CI pour `uv sync` puis tester chaque package.
- À ce stade `backend/` existe toujours et fonctionne. Les `packages/` sont vides.

#### Étape 2.2 — Déplacement alto-core en deux temps

**Commit 2.2.a — Copie des fichiers purs (pas de suppression)**

- Copier tous les fichiers vers `packages/alto-core/src/alto_core/`.
- Adapter les imports internes (`from app.schemas` → `from alto_core.schemas`).
- Faire passer les tests dans `packages/alto-core/tests/`.
- À ce stade : le code existe en double, c'est OK temporairement.

**Commit 2.2.b — Backend délègue à alto-core**

- Dans `backend/app/`, remplacer chaque fichier déplacé par un réexport :

```python
# backend/app/alto/parser.py
from alto_core.alto.parser import *  # noqa
from alto_core.alto.parser import parse_alto, parse_alto_bytes  # explicite
```

- Ajouter `alto-core` (via path = `packages/alto-core`) aux dépendances de `backend/`.
- Tous les tests `backend/` passent toujours.
- Suppression effective des duplications à la prochaine étape.

**Commit 2.2.c — Suppression des copies dans backend**

- Supprimer le contenu des modules déplacés dans `backend/app/`, ne garder que les réexports.

#### Étape 2.3 — Idem pour alto-providers

Même séquence : copie → délégation → suppression.

#### Étape 2.4 — Publication alpha sur TestPyPI

- Workflow `.github/workflows/publish-alpha.yml` qui sur tag `vX.Y.Z-alphaN` :
  - Build chaque package
  - Publie sur TestPyPI via Trusted Publishing OIDC

- Validation manuelle : `pip install --index-url https://test.pypi.org/simple/ alto-core` fonctionne sur un environnement vierge.

#### Étape 2.5 — Publication 0.1.0 sur PyPI

- Tag `v0.1.0` déclenche la publication officielle.
- DOI Zenodo généré automatiquement (intégration GitHub native une fois `CITATION.cff` en place).

### Phase 3 — Extraction `alto-server` + création `alto-cli`

#### Étape 3.1 — Déplacement alto-server

Même méthode (copie → délégation → suppression).

Particularité : `backend/app/main.py` devient `packages/alto-server/src/alto_server/app.py` avec une fonction `create_app()` factory.

Le `backend/` racine devient un simple `entry.py` :

```python
# backend/entry.py
from alto_server import create_app
app = create_app()
```

Ou mieux : `backend/` disparaît complètement, le Dockerfile pointe directement sur `packages/alto-server/`.

#### Étape 3.2 — `create_router()` montable

- Ajouter `packages/alto-server/src/alto_server/router.py` :

```python
from fastapi import APIRouter, Depends
from alto_server.api import jobs_router, providers_router

def create_router(*, job_store, storage, output_writer, provider_registry,
                  auth_dependency=None) -> APIRouter:
    router = APIRouter()
    # Construire les sous-routers avec leurs dépendances injectées
    router.include_router(jobs_router, prefix="/jobs",
                         dependencies=[Depends(auth_dependency)] if auth_dependency else [])
    router.include_router(providers_router, prefix="/providers")
    return router
```

- Test d'intégration : `tests/test_mount_in_third_party_app.py` qui mount le router dans une FastAPI vide et vérifie que tout marche.

#### Étape 3.3 — `alto-cli`

- Nouveau package `packages/alto-cli/`.
- Commandes Typer décrites dans [ARCHITECTURE.md §4.3](ARCHITECTURE.md#43-alto-cli).
- Tests `CliRunner`.

---

## 5. Compatibilité ascendante

### 5.1 Pour les utilisateurs HTTP (frontend, API tierces)

**Garantie absolue jusqu'à `alto-server 2.0` :**
- Chemins de routes inchangés : `/api/jobs`, `/api/jobs/{id}`, `/api/jobs/{id}/events`, etc.
- Payloads inchangés (requêtes et réponses).
- Format SSE inchangé.
- Format des fichiers de sortie inchangé.

Toute évolution d'API se fait par **ajout** (nouvelles routes, nouveaux champs optionnels). Aucune suppression, aucun renommage avant `2.0`.

### 5.2 Pour les utilisateurs Python (rare aujourd'hui mais à anticiper)

Pendant la transition (entre Phase 2 et 3), `from app.X` continue de marcher via les réexports. Une fois `backend/` supprimé, les utilisateurs doivent migrer vers `from alto_core.X` ou `from alto_server.X`.

**Plan de communication :**
- Annonce dans `CHANGELOG.md` dès Phase 2.
- Warning de dépréciation dans `from app.X` au début de Phase 3.
- Suppression effective au début de Phase 4.

### 5.3 Pour le frontend

Le frontend continue d'appeler `/api/*` sur le même backend. Aucune modification de son côté pendant les Phases 1-3.

L'évolution du frontend (génération client TS depuis OpenAPI, tests, i18n) est planifiée en Phase 3.3, indépendamment.

### 5.4 Pour Docker / docker-compose

`docker-compose.yml` continue de fonctionner tant que le `Dockerfile` racine pointe sur quelque chose qui sert FastAPI sur port 7860. Pendant la transition :
- Phase 1 : aucun changement Dockerfile.
- Phase 2 : Dockerfile mis à jour pour installer les packages depuis le workspace (`uv sync`).
- Phase 3 : Dockerfile pointe directement sur `packages/alto-server/`. Le `backend/` racine n'existe plus.

---

## 6. Gestion des breaking changes (post-1.0)

### 6.1 Politique générale

Une fois `alto-core 1.0` publié :
- **MAJOR** : breaking changes API publique (signatures, comportements observables). Migration guide obligatoire.
- **MINOR** : ajouts (nouvelles fonctions, nouveaux paramètres optionnels). Pas de breaking.
- **PATCH** : bugfix only. Pas de nouvelle feature.

### 6.2 Période de dépréciation

Une API publique dépréciée :
1. Émet un `DeprecationWarning` à l'exécution avec message clair et URL doc.
2. Reste fonctionnelle pendant au minimum **un cycle MAJOR** (donc 6+ mois typiquement).
3. Documentation marquée "deprecated" avec date de suppression.

### 6.3 Anticipation des breaking changes connus

| Changement attendu | Version cible | Raison |
|---|---|---|
| Signature de `parse_alto()` accepte `bytes`/`BinaryIO` | 0.2.0 (additif, pas breaking) | Permet usage sans filesystem |
| Suppression de `from app.X` réexports | 0.x → 1.0 | Nettoyage |
| Renommage `JobManifest.line_traces` → `traces` (uniformisation) | 1.0 | Cohérence |

---

## 7. Validation à chaque étape

### 7.1 Critères de done par commit

Tout commit doit satisfaire :
- [ ] `ruff check .` passe
- [ ] `ruff format --check .` passe
- [ ] `mypy --strict` passe (modules cibles)
- [ ] `pytest` passe (coverage ≥ seuil défini)
- [ ] `bandit` passe (zéro HIGH/CRITICAL)
- [ ] Pas de régression frontend (build OK, tests OK)
- [ ] Docker compose démarre et endpoint `/healthz` répond 200

### 7.2 Validation manuelle après chaque phase

Phase 1 :
- [ ] Lancer une correction réelle via UI avec OpenAI / Anthropic / Mistral / Google
- [ ] Vérifier que les fichiers de sortie sont identiques à avant le refactoring (diff bytewise)
- [ ] SSE stream fonctionne sans déconnexion
- [ ] Tests passent en 100% (pas de skip silencieux)

Phase 2 :
- [ ] `pip install alto-core` fonctionne sur un venv vierge
- [ ] Notebook quickstart fonctionne
- [ ] Pas de régression d'app entière

Phase 3 :
- [ ] `pip install alto-server` fonctionne
- [ ] `altocorrect run example.xml --provider mock` fonctionne
- [ ] `from fastapi import FastAPI; app.include_router(create_router(...))` fonctionne dans un projet vierge

---

## 8. Points d'attention

### 8.1 Sur la découpe d'`orchestrator.py`

C'est l'étape la plus risquée. Recommandations :

1. **Avant tout** : monter la couverture de `test_orchestrator.py` à ≥ 95%. Aujourd'hui 430 lignes de tests pour 788 lignes de code, c'est insuffisant pour un refactoring de cette ampleur.
2. **Snapshot tests** : sur un corpus de référence, capturer les fichiers de sortie actuels. Après refactoring, comparer byte à byte. Si différence : analyser, corriger ou justifier.
3. **Profiling** : mesurer le throughput avant/après. Le découpage ne doit pas dégrader les performances (idéalement les améliorer).

### 8.2 Sur les schemas partagés

`backend/app/schemas/__init__.py` (290 LOC) mélange modèles purs du domaine et payloads HTTP. Le split en Phase 2/3 est délicat car les imports croisés peuvent créer des cycles. Stratégie :

1. D'abord séparer en deux fichiers dans `backend/` : `schemas/domain.py` (pur) et `schemas/http.py` (HTTP).
2. Les imports existants restent dans `schemas/__init__.py` qui réexporte les deux.
3. Au déplacement vers les packages, `domain.py` → `alto-core`, `http.py` → `alto-server`.

### 8.3 Sur les noms PyPI

À vérifier dès maintenant :
- `alto-core` disponible ? Sinon `altoxml-core`, `alto-corrector-core`.
- `alto-providers` disponible ? Sinon `alto-llm-providers`.
- `alto-server` disponible ? Sinon `alto-corrector-server`.
- `alto-cli` disponible ? Sinon `alto-corrector`, `altocorrect-cli`.

**Action immédiate :** réserver les noms sur TestPyPI puis PyPI dès la Phase 1 pour éviter les squatters.

### 8.4 Sur la communication

Chaque release majeure devrait être accompagnée :
- D'un post sur le blog du projet (à créer, MkDocs supporte les blogs nativement).
- D'une annonce sur les listes pertinentes : OCR-D, IIIF, Digital Humanities, eScriptorium users.
- D'une note dans `CHANGELOG.md` et dans les release notes GitHub.

---

## 9. Estimation totale

| Phase | Effort temps-partiel | Effort plein-temps |
|---|---|---|
| 1 (refactoring + outillage) | 2 semaines | 1 semaine |
| 2 (alto-core + alto-providers) | 3 semaines | 1.5 semaine |
| 3 (alto-server + alto-cli + frontend tests) | 3 semaines | 1.5 semaine |
| **Total migration technique** | **8 semaines** | **4 semaines** |

Les Phases 4 (doc + benchmarks + écosystème) et 5 (gouvernance) sont continues et indépendantes de l'effort de migration.
