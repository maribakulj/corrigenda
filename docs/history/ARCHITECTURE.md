# ARCHITECTURE — alto-llm-corrector (cible)

> **Statut :** document cible, valide pour la roadmap d'extraction PyPI.
> Document vivant : à mettre à jour au fil des refactorings (voir [ROADMAP.md](ROADMAP.md) et [MIGRATION.md](MIGRATION.md)).
>
> **Audience :** mainteneurs, contributeurs externes, partenaires institutionnels, intégrateurs tiers.

---

## 1. Vision

Faire d'alto-llm-corrector la **brique de référence open-source** pour la correction post-OCR de fichiers ALTO XML par LLM, intégrable dans :

- une bibliothèque Python embarquée dans n'importe quel pipeline data (Airflow, Prefect, scripts ad-hoc) ;
- un microservice HTTP déployable seul ou comme sous-module d'une app plus large ;
- une CLI utilisable directement par des bibliothécaires sans compétences dev ;
- un plugin / bridge pour les écosystèmes existants (eScriptorium, Transkribus, OCR4all).

La valeur unique du projet — le **Hyphenation Reconciler** et l'invariant "l'app décide, le LLM informe" — doit être encapsulée dans un package pur, sans dépendance à FastAPI, au système de fichiers, ou à un job store particulier.

---

## 2. Principes architecturaux

### 2.1 Ports & Adapters (architecture hexagonale)

Le **domaine** (parsing ALTO, chunking, validation, reconciliation, rewriting) est isolé de l'**infrastructure** (HTTP, filesystem, job store, providers LLM). Toute interaction avec le monde extérieur passe par un **Protocol** (port) injecté à l'instanciation.

### 2.2 Pipeline pur

Le cœur du pipeline de correction est une fonction asynchrone sans effet de bord caché :

```
pipeline.run(document, *, observer=None) -> CorrectedDocument
```

Les seuls effets de bord sont :
- l'appel HTTP au LLM (via le `Provider` injecté) ;
- l'émission d'événements (via l'`Observer` injecté, facultatif).

Aucun accès disque, aucun mutation d'état global, aucun appel à un singleton.

### 2.3 Configuration explicite

Plus de variables globales (`_DEFAULT_CONFIG`, `job_store = JobStore()`). Toute configuration est un objet Pydantic instancié explicitement et passé en argument.

### 2.4 Dépendances minimales par couche

| Package | Dépendances runtime |
|---|---|
| `alto-core` | `lxml`, `pydantic` |
| `alto-providers` | `alto-core`, `httpx` |
| `alto-cli` | `alto-core`, `alto-providers`, `typer`, `rich` |
| `alto-server` | `alto-core`, `alto-providers`, `fastapi`, `uvicorn`, `sse-starlette`, `aiofiles`, `python-multipart` |
| `alto-web` | — (artefact frontend, embarqué dans l'image Docker de `alto-server`) |

Un consommateur qui n'a besoin que du parsing/rewriting installe `alto-core` seul (3 dépendances transitives).

### 2.5 Compatibilité et versioning

- **SemVer strict** pour `alto-core` et `alto-providers` (API publique stable, breaking changes = MAJOR).
- **CalVer** pour `alto-server` et `alto-cli` (apps déployables, cadence trimestrielle).
- Le frontend `alto-web` suit la version de `alto-server` qui l'embarque.

---

## 3. Vue d'ensemble du monorepo cible

```
alto-llm-corrector/                       monorepo géré par uv workspaces
├── packages/
│   ├── alto-core/                        PyPI: pur, zéro I/O réseau, zéro filesystem
│   │   ├── pyproject.toml
│   │   ├── src/alto_core/
│   │   │   ├── __init__.py               API publique (re-exports)
│   │   │   ├── alto/                     parsing/rewriting ALTO XML
│   │   │   │   ├── parser.py
│   │   │   │   ├── rewriter.py
│   │   │   │   ├── hyphenation.py
│   │   │   │   ├── _norm.py
│   │   │   │   └── _ns.py
│   │   │   ├── pipeline/                 pipeline de correction pur
│   │   │   │   ├── chunk_planner.py
│   │   │   │   ├── validator.py
│   │   │   │   ├── line_acceptance.py
│   │   │   │   └── correction_pipeline.py   ← NOUVEAU (extrait d'orchestrator)
│   │   │   ├── schemas/                  modèles Pydantic du domaine
│   │   │   │   └── __init__.py
│   │   │   └── protocols/                Protocols injectables (ports)
│   │   │       ├── provider.py           BaseProvider
│   │   │       ├── observer.py           PipelineObserver
│   │   │       └── output_writer.py      OutputWriter
│   │   └── tests/
│   │
│   ├── alto-providers/                   PyPI: 4 implémentations + extras
│   │   ├── pyproject.toml                [openai], [anthropic], [google], [mistral], [all]
│   │   ├── src/alto_providers/
│   │   │   ├── __init__.py               registry + factory
│   │   │   ├── base.py                   helpers HTTP partagés
│   │   │   ├── openai_provider.py
│   │   │   ├── anthropic_provider.py
│   │   │   ├── google_provider.py
│   │   │   ├── mistral_provider.py
│   │   │   └── mock_provider.py          utile pour tests des consommateurs
│   │   └── tests/
│   │
│   ├── alto-cli/                         PyPI: `pip install alto-cli`
│   │   ├── pyproject.toml
│   │   ├── src/alto_cli/
│   │   │   ├── __main__.py               entrée Typer
│   │   │   ├── commands/
│   │   │   │   ├── run.py                altocorrect run <file>
│   │   │   │   ├── batch.py              altocorrect batch <dir>
│   │   │   │   ├── validate.py           altocorrect validate
│   │   │   │   ├── diff.py               altocorrect diff
│   │   │   │   └── serve.py              altocorrect serve (lance alto-server)
│   │   │   └── output/                   formatters: text, json, rich
│   │   └── tests/
│   │
│   ├── alto-server/                      Docker + PyPI: FastAPI app
│   │   ├── pyproject.toml
│   │   ├── src/alto_server/
│   │   │   ├── __init__.py
│   │   │   ├── app.py                    create_app(config) factory
│   │   │   ├── router.py                 create_router(...) — montable dans une app tierce
│   │   │   ├── api/
│   │   │   │   ├── jobs.py
│   │   │   │   ├── providers.py
│   │   │   │   ├── health.py
│   │   │   │   └── deps.py               FastAPI dependencies
│   │   │   ├── adapters/                 implémentations concrètes des Protocols
│   │   │   │   ├── job_store/
│   │   │   │   │   ├── memory.py         InMemoryJobStore
│   │   │   │   │   └── redis.py          RedisJobStore (extra)
│   │   │   │   ├── storage/
│   │   │   │   │   ├── filesystem.py     FilesystemStorage
│   │   │   │   │   └── s3.py             S3Storage (extra)
│   │   │   │   └── output_writer/
│   │   │   │       ├── filesystem.py
│   │   │   │       └── s3.py
│   │   │   ├── observability/
│   │   │   │   ├── otel.py               OpenTelemetry traces
│   │   │   │   ├── prometheus.py         metrics
│   │   │   │   └── sentry.py             error tracking
│   │   │   ├── security/
│   │   │   │   ├── rate_limit.py
│   │   │   │   └── auth.py               JWT/OAuth optionnel
│   │   │   └── runner.py                 JobRunner (orchestre Pipeline + JobStore + I/O)
│   │   └── tests/
│   │
│   └── alto-web/                         apps/frontend
│       ├── package.json
│       ├── src/                          (structure actuelle conservée, refactorée)
│       └── tests/                        Vitest + Playwright (manquant aujourd'hui)
│
├── docs/                                 MkDocs Material, déployé sur GitHub Pages
│   ├── index.md
│   ├── tutorials/
│   │   ├── quickstart-library.md
│   │   ├── quickstart-cli.md
│   │   ├── quickstart-server.md
│   │   └── integration-fastapi-app.md
│   ├── reference/                        mkdocstrings auto-généré
│   ├── architecture/                     ce document + ADRs
│   └── benchmarks/                       résultats reproductibles
│
├── benchmarks/                           datasets ALTO publics + scripts CER/WER
│   ├── datasets/                         (sous-modules git ou téléchargement)
│   ├── runners/
│   └── results/                          historique versionné
│
├── examples/
│   ├── notebook_quickstart.ipynb
│   ├── escriptorium_bridge/
│   ├── transkribus_export/
│   └── airflow_dag.py
│
├── .github/
│   ├── workflows/
│   │   ├── ci.yml                        tests, lint, types, coverage, security
│   │   ├── publish.yml                   trusted publishing PyPI (OIDC)
│   │   ├── docker.yml                    build + push GHCR + signature cosign
│   │   ├── docs.yml                      build + deploy MkDocs
│   │   └── benchmark.yml                 run benchmarks weekly
│   ├── ISSUE_TEMPLATE/
│   └── PULL_REQUEST_TEMPLATE.md
│
├── pyproject.toml                        racine: uv workspace, dev tools
├── README.md
├── CONTRIBUTING.md
├── CODE_OF_CONDUCT.md
├── GOVERNANCE.md
├── SECURITY.md
├── CITATION.cff                          DOI Zenodo
├── CHANGELOG.md
├── LICENSE
└── ARCHITECTURE.md                       ce document
```

---

## 4. Description détaillée des packages

### 4.1 `alto-core`

**Responsabilité :** logique métier pure de correction ALTO.

**Surface publique stable :**

```python
# Parsing
from alto_core import parse_alto, parse_alto_bytes
# → DocumentManifest

# Pipeline
from alto_core import CorrectionPipeline, PipelineConfig, CorrectedDocument
# → instanciable avec provider + observer + config

# Reconciliation / validation (utilisables seuls)
from alto_core import enrich_chunk_lines, reconcile_hyphen_pair, validate_response
from alto_core import plan_chunks, ChunkPlanner

# Rewriting
from alto_core import rewrite_alto, rewrite_alto_bytes

# Schémas du domaine
from alto_core.schemas import (
    DocumentManifest, PageManifest, BlockManifest, LineManifest,
    HyphenRole, LLMLineInput, LLMLineOutput,
)

# Protocols (ports) à implémenter par les consommateurs
from alto_core.protocols import BaseProvider, PipelineObserver, OutputWriter
```

**Garanties :**
- Aucun import de `fastapi`, `aiofiles`, `httpx` (`httpx` est dans `alto-providers`).
- Aucun accès filesystem (`pathlib.Path` accepté en argument, mais `parse_alto` accepte aussi `bytes` et `BinaryIO`).
- 100% type-checked en `mypy --strict`.
- Couverture de tests ≥ 90%, 100% sur les invariants hyphenation.

**Non-objectifs :**
- N'embarque aucun provider concret (passer par `alto-providers`).
- N'embarque aucune logique de job persistence ou de SSE.
- Ne fait pas d'OCR, de resegmentation, de translation.

### 4.2 `alto-providers`

**Responsabilité :** implémentations concrètes du `BaseProvider` pour les principaux fournisseurs LLM.

**Installation modulaire :**

```bash
pip install alto-providers                # juste le protocol + mock
pip install alto-providers[openai]        # + OpenAI
pip install alto-providers[anthropic]     # + Anthropic
pip install alto-providers[all]           # tous
```

**Surface publique :**

```python
from alto_providers import get_provider, list_providers, register_provider
from alto_providers.openai import OpenAIProvider
from alto_providers.anthropic import AnthropicProvider
from alto_providers.mock import MockProvider  # pour les tests des consommateurs
```

**Garanties :**
- Tous implémentent strictement `alto_core.protocols.BaseProvider`.
- Retry HTTP avec backoff exponentiel (5xx, timeouts).
- Sanitisation des clés API dans les messages d'erreur (jamais loggées en clair).
- Connection pooling via `httpx.AsyncClient` partagé.

**Extension points :**
- `register_provider("ollama", OllamaProvider)` pour brancher des providers tiers (Ollama, vLLM, Bedrock, Azure OpenAI…) sans toucher au package.

### 4.3 `alto-cli`

**Responsabilité :** interface ligne de commande pour usage direct et scripting.

**Commandes :**

| Commande | Usage |
|---|---|
| `altocorrect run <file>` | Corrige un fichier ALTO |
| `altocorrect batch <dir>` | Traite un répertoire en parallèle |
| `altocorrect validate <files...>` | Vérifie qu'un ALTO est conforme |
| `altocorrect diff <a> <b>` | Diff visuel entre deux ALTO (text / json / html) |
| `altocorrect serve` | Lance `alto-server` localement |
| `altocorrect providers list` | Liste les providers disponibles et leurs modèles |
| `altocorrect benchmark <dataset>` | Lance un benchmark CER/WER |

**Conventions :**
- Toutes les commandes acceptent `--json` pour sortie machine-readable.
- Codes de sortie POSIX (`0` succès, `1` erreur métier, `2` erreur usage).
- Clés API via `--api-key-env VAR` (jamais en argument direct).

### 4.4 `alto-server`

**Responsabilité :** application HTTP autonome ou montable.

**Deux modes d'usage :**

**Mode 1 — App autonome (cas actuel) :**

```python
from alto_server import create_app
app = create_app()  # lit les env vars, config par défaut
```

**Mode 2 — Router montable dans une app tierce :**

```python
from fastapi import FastAPI
from alto_server import create_router
from alto_server.adapters import InMemoryJobStore, FilesystemStorage

my_app = FastAPI()
my_app.include_router(
    create_router(
        job_store=InMemoryJobStore(),
        storage=FilesystemStorage("/var/lib/alto"),
        auth_dependency=my_auth,
    ),
    prefix="/ocr",
)
```

**Adapters fournis :**

| Protocol | In-tree | Extras (séparés) |
|---|---|---|
| `JobStore` | `InMemoryJobStore` | `RedisJobStore`, `PostgresJobStore` |
| `Storage` | `FilesystemStorage` | `S3Storage`, `AzureBlobStorage` |
| `OutputWriter` | `FilesystemOutputWriter` | `S3OutputWriter` |
| `RateLimiter` | `InMemoryRateLimiter` | `RedisRateLimiter` |

**Observabilité (opt-in) :**
- OpenTelemetry traces sur chaque chunk et chaque appel LLM.
- Métriques Prometheus exposées sur `/metrics` : `alto_corrections_total`, `alto_llm_latency_seconds`, `alto_hyphen_pairs_reconciled_total`, `alto_chunk_retries_total`, etc.
- Sentry pour error tracking (DSN via env var).

### 4.5 `alto-web`

**Responsabilité :** interface utilisateur web.

**Évolutions vs aujourd'hui :**
- Tests Vitest pour les composants critiques (`DiffViewer`, `LayoutViewer`, `useJobStream`).
- Tests end-to-end Playwright sur les flux principaux (upload → progression → download).
- i18n via `react-i18next` (FR, EN, DE, IT au minimum).
- Génération du client API depuis l'OpenAPI de `alto-server` (élimine la duplication `types/index.ts`).
- Validation runtime des réponses API via `zod` ou `valibot` (élimine les type assertions silencieuses).

---

## 5. Flux d'exécution (séquences clés)

### 5.1 Correction d'un document (intégration bibliothèque)

```
Consommateur                CorrectionPipeline           Provider         Observer
     │                            │                         │                │
     │ run(document, observer) ──▶│                         │                │
     │                            │                         │                │
     │                            │── plan_chunks() ────▶ChunkPlanner       │
     │                            │◀───────────────────────                  │
     │                            │                         │                │
     │                            │── on_event(chunk_started) ──────────────▶│
     │                            │                         │                │
     │                            │── enrich_chunk_lines()                   │
     │                            │                         │                │
     │                            │── complete_structured()─▶│                │
     │                            │◀────────────────────────│                │
     │                            │                         │                │
     │                            │── validate_response()                    │
     │                            │── reconcile_hyphen_pair() (par paire)   │
     │                            │                         │                │
     │                            │── on_event(chunk_completed) ────────────▶│
     │                            │                         │                │
     │                            │   (boucle sur chunks)                    │
     │                            │                         │                │
     │◀── CorrectedDocument ──────│                         │                │
```

### 5.2 Intégration dans une app FastAPI tierce

```
HTTP Client          Tiers App         alto-server router    JobRunner        Pipeline
     │                  │                     │                  │                │
     │── POST /ocr/jobs ▶│                     │                  │                │
     │                  │── auth_dependency ──▶                  │                │
     │                  │── proxy ───────────▶│                  │                │
     │                  │                     │── create_job() ─▶│                │
     │                  │                     │                  │── pipeline.run│
     │                  │                     │                  │    (async bg) │
     │◀── {job_id} ─────│◀────────────────────│                  │                │
     │                  │                     │                  │                │
     │── GET /ocr/events▶│                    │                  │                │
     │                  │── SSE stream ──────▶│                  │                │
     │◀══ event stream ═│════════════════════│                  │                │
     │                  │                     │                  │   (events…)    │
```

L'app tierce voit `alto-server` comme un router noir. Toute la mécanique (JobRunner, JobStore, OutputWriter) reste interne au package mais peut être customisée via injection.

---

## 6. Décisions architecturales (ADRs résumés)

### ADR-001 : monorepo avec uv workspaces (vs poly-repo)
**Choix :** monorepo. **Raisons :** refactorings cross-package atomiques, CI unique, versioning coordonné. **Coût :** outillage workspace nécessaire (uv) mais standard 2025+.

### ADR-002 : Pydantic v2 pour tous les modèles publics
**Choix :** Pydantic. **Raisons :** déjà en place, typé, sérialisation gratuite, schéma JSON exportable. **Alternative rejetée :** dataclasses (pas de validation, pas de schema export natif).

### ADR-003 : Protocol vs ABC pour les ports
**Choix :** `typing.Protocol` (structural typing). **Raisons :** consommateurs peuvent fournir des duck types sans hériter, plus pythonique. **Coût :** moins explicite, mitigé par tests de conformité.

### ADR-004 : SemVer strict pour les packages PyPI
**Choix :** SemVer. **Raisons :** confiance des consommateurs, breaking changes signalés clairement. **Engagement :** zéro breaking change en minor/patch sur `alto-core` ≥ 1.0.

### ADR-005 : Async par défaut
**Choix :** API publique async (`async def`). **Raisons :** providers HTTP, SSE, gros documents. **Versions sync :** wrappers `alto_core.sync` fournis pour scripts simples.

### ADR-006 : Pipeline pur sans logger interne
**Choix :** le pipeline n'utilise pas `logging`, il émet des `PipelineEvent` à l'observer. **Raisons :** consommateurs choisissent leur stack de logging (structlog, loguru, stdlib). **Coût :** un peu plus verbeux côté observer par défaut.

### ADR-007 : pas de plugin system custom
**Choix :** extension via Python imports et `register_provider`, pas via entry_points. **Raisons :** explicite, debuggable, suffisant à cette échelle. **Réévaluable :** à 10+ providers communautaires.

### ADR-008 : Trusted Publishing PyPI (OIDC)
**Choix :** publication PyPI via GitHub Actions OIDC, pas de token long-lived. **Raisons :** standard sécurité 2025+, recommandé PyPA.

---

## 7. Stack technique cible

| Domaine | Choix | Justification |
|---|---|---|
| Gestion monorepo Python | **uv workspaces** | Performances, standard 2025+, gère lockfile cross-package |
| Build backend | **hatchling** | Standard PyPA, simple, versionning dynamique via hatch-vcs |
| Lint Python | **ruff** | Tout-en-un (flake8/isort/pydocstyle/etc.), ultra-rapide |
| Format Python | **ruff format** | Cohérent avec le lint |
| Types Python | **mypy --strict** | Strict obligatoire sur alto-core et alto-providers |
| Tests | **pytest + pytest-asyncio + pytest-cov** | Standard, déjà en place |
| Pre-commit | **pre-commit** | Lint, types, format, security avant commit |
| Security scan | **bandit + pip-audit** | Code + dépendances |
| SBOM | **CycloneDX (cyclonedx-py)** | Standard, accepté par auditeurs |
| Docs | **MkDocs Material + mkdocstrings** | Magnifique, hébergeable GitHub Pages |
| Frontend lint | **eslint + prettier** | Standard |
| Frontend tests | **Vitest + Playwright** | Unit + E2E |
| API contract | **OpenAPI 3.1** | Auto-généré par FastAPI |
| Client TS | **openapi-typescript** | Codegen depuis OpenAPI, élimine la duplication |
| Conteneurs | **Multi-stage Dockerfile** + **distroless** | Réduit la surface d'attaque |
| Signature images | **cosign (Sigstore)** | Supply chain security |
| Observabilité | **OpenTelemetry** | Standard CNCF, vendor-neutral |

---

## 8. Points d'extension publics

Pour qu'un consommateur tiers étende ou remplace un comportement, il implémente l'un de ces `Protocol` :

### 8.1 `BaseProvider`

> **Contrat v1.0 effectif** (SPECS_LIB_V2 §5.1/F14) — `complete_structured`
> reçoit le payload utilisateur en dict et renvoie `(dict, Usage | None)` :
> le JSON conforme à `OUTPUT_JSON_SCHEMA` plus la consommation de tokens
> (ou `None` si le provider ne la rapporte pas).

```python
from typing import Any

from alto_core.protocols import BaseProvider
from alto_core.schemas import ModelInfo, Usage

class MyCustomProvider(BaseProvider):
    async def list_models(self, api_key: str) -> list[ModelInfo]:
        ...

    async def complete_structured(
        self,
        api_key: str,
        model: str,
        system_prompt: str,
        user_payload: dict[str, Any],
        json_schema: dict[str, Any],
        temperature: float = 0.0,
    ) -> tuple[dict[str, Any], Usage | None]:
        ...
```

### 8.2 `PipelineObserver`

```python
from alto_core.protocols import PipelineObserver, PipelineEvent

class MyObserver(PipelineObserver):
    async def on_event(self, event: PipelineEvent) -> None:
        # event.type, event.payload, event.timestamp, event.context
        ...
```

### 8.3 `OutputWriter`

```python
from alto_core.protocols import OutputWriter
from alto_core.schemas import CorrectedDocument

class MyWriter(OutputWriter):
    async def write(self, document: CorrectedDocument, *, job_id: str) -> str:
        # retourne une URI accessible (file://, s3://, https://…)
        ...
```

### 8.4 `JobStore` (côté `alto-server` uniquement)

> **v1.0 (F12)** — `JobManifest`, `JobStatus` et `Provider` ne vivent plus
> dans `alto_core.schemas` : ce sont des concepts serveur, déplacés chez le
> consommateur (`app.schemas.job` dans le backend actuel, `alto_server`
> à terme). Le cœur n'énumère pas de vendeurs LLM et ne suit pas le cycle
> de vie d'un job.

```python
from alto_server.protocols import JobStore
from alto_server.schemas import JobManifest

class MyJobStore(JobStore):
    async def create_job(self, ...) -> str: ...
    async def get_job(self, job_id: str) -> JobManifest | None: ...
    async def update_job(self, job_id: str, **fields) -> None: ...
    async def stream_events(self, job_id: str) -> AsyncIterator[Event]: ...
    async def cleanup_job(self, job_id: str) -> None: ...
```

---

## 9. Sécurité

| Couche | Mesures |
|---|---|
| Parsing XML | XXE protection (`resolve_entities=False, no_network=True`) ✅ déjà en place |
| ZIP extraction | Limites taille + nombre de membres ✅ déjà en place |
| Path traversal | `pathlib.Path.resolve().is_relative_to()` à appliquer systématiquement |
| API keys | Sanitisation dans tous les messages d'erreur ✅ déjà en place |
| CORS | Configurable, jamais `*` en prod ✅ déjà en place |
| Rate limiting | À ajouter (slowapi ou implémentation maison) |
| Containers | Non-root user ✅, distroless cible, pinning par digest |
| Supply chain | Trusted Publishing PyPI, Sigstore pour images Docker, SBOM CycloneDX |
| Dépendances | Lock file `uv.lock`, Dependabot, pip-audit en CI |
| Divulgation | `SECURITY.md` avec processus + GPG key |

---

## 10. Compatibilité ascendante pendant la migration

Voir [MIGRATION.md](MIGRATION.md) pour les détails. Principes :

1. Aucun breaking change visible pour l'utilisateur final tant que `alto-server 1.x` est en place.
2. Les imports internes (`from app.X`) restent valides pendant toute la transition via réexports.
3. Les routes HTTP existantes (`/api/jobs`, `/api/providers/models`) ne bougent ni de chemin ni de payload.
4. Le format des fichiers d'output (XML corrigé, trace.json) est inchangé.
