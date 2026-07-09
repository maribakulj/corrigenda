# Guide complet — alto-llm-corrector avec Claude Code

> ⚠️ **DOCUMENT HISTORIQUE (non normatif).** Ces specs décrivent l'ancienne
> *application web* ALTO. La bibliothèque `corrigenda` (le cœur de correction
> extrait et publié) fait désormais autorité via **`SPECS_LIB_V2.md`** ; le
> backend/frontend sont couverts par `SPECS_API.md` / `SPECS_FRONTEND.md` /
> `SPECS_JOBS.md`. Conservé pour la traçabilité de la conception initiale ;
> plusieurs modules qu'il cite (`orchestrator.py`, `chunk_planner.py`,
> `line_acceptance.py`, `correction_pipeline.py`) n'existent plus.

---

## PARTIE 1 — SPECS

> Ces specs sont rédigées pour être données directement en contexte à Claude Code.
> Chaque section est autonome et actionnable. Sauvegarde ce fichier sous `SPECS.md` à la racine du repo.

---

### Vue d'ensemble

Construire `alto-llm-corrector` : une application web de post-correction OCR text-only pour fichiers ALTO XML.

**Ce que fait l'app :**
1. L'utilisateur uploade des fichiers ALTO XML (ou un ZIP)
2. Il choisit un fournisseur LLM (OpenAI / Anthropic / Mistral / Google)
3. Il saisit sa clé API
4. Il charge la liste réelle des modèles disponibles
5. Il choisit un modèle et lance le traitement
6. Le backend orchestre la correction page par page
7. L'ALTO corrigé est téléchargeable

**Ce que l'app ne fait PAS :**
- Pas d'OCR image
- Pas de resegmentation
- Pas de fusion/scission de lignes
- Pas de traduction
- Pas de modernisation du texte

**Contrainte de déploiement :** fonctionne en local (docker-compose) ET sur Hugging Face Spaces (Dockerfile racine, port 7860, un seul conteneur servant le frontend buildé comme fichiers statiques via FastAPI).

---

### Stack technique

```
Backend    : Python 3.11+, FastAPI, Pydantic v2, httpx, lxml, uvicorn, sse-starlette
Frontend   : React + TypeScript + Vite + Tailwind CSS
Conteneurs : Dockerfile backend, Dockerfile frontend, docker-compose.yml
             + Dockerfile racine pour HF Spaces (build frontend → sert via FastAPI)
Storage    : /tmp/app-jobs/{job_id}/ sur disque local, état jobs en mémoire
DB         : aucune
```

---

### Arborescence cible

```
alto-llm-corrector/
├── Dockerfile                    ← HF Spaces (build tout-en-un, port 7860)
├── docker-compose.yml            ← dev local (backend:8000 + frontend:5173)
├── .env.example
├── README.md
├── SPECS.md                      ← ce fichier (index)
├── CLAUDE.md
├── examples/
│   └── sample.xml                ← ALTO v3 minimal avec césures pour tests
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── app/
│   │   ├── main.py
│   │   ├── schemas/
│   │   │   └── __init__.py       ← tous les modèles Pydantic
│   │   ├── alto/
│   │   │   ├── __init__.py
│   │   │   ├── parser.py         ← parsing ALTO v2/v3/v4 + détection césures
│   │   │   ├── hyphenation.py    ← Hyphenation Reconciler
│   │   │   └── rewriter.py       ← réécriture ALTO avec HYP/SUBS_*
│   │   ├── providers/
│   │   │   ├── __init__.py       ← registry + get_provider()
│   │   │   ├── base.py           ← Protocol + SYSTEM_PROMPT + JSON_SCHEMA
│   │   │   ├── openai_provider.py
│   │   │   ├── anthropic_provider.py
│   │   │   ├── mistral_provider.py
│   │   │   └── google_provider.py
│   │   ├── jobs/
│   │   │   ├── __init__.py
│   │   │   ├── store.py          ← JobStore en mémoire + queues SSE
│   │   │   ├── chunk_planner.py  ← planificateur adaptatif (hyphen-aware)
│   │   │   ├── validator.py      ← validation réponses LLM
│   │   │   └── orchestrator.py   ← moteur principal + intégration reconciler
│   │   ├── api/
│   │   │   ├── __init__.py
│   │   │   ├── providers.py      ← POST /api/providers/models
│   │   │   └── jobs.py           ← POST/GET /api/jobs + SSE + download
│   │   └── storage/
│   │       └── __init__.py       ← gestion fichiers disque
│   └── tests/
│       ├── test_parser.py
│       ├── test_hyphenation.py
│       ├── test_rewriter.py
│       ├── test_chunk_planner.py
│       ├── test_validator.py
│       └── test_integration.py
└── frontend/
    ├── Dockerfile
    ├── package.json
    ├── vite.config.ts
    ├── tailwind.config.ts
    └── src/
        ├── main.tsx
        ├── App.tsx
        ├── types/
        │   └── index.ts
        ├── api/
        │   └── client.ts
        ├── hooks/
        │   ├── useJobStream.ts
        │   └── useModels.ts
        └── components/
            ├── FileUpload.tsx
            ├── ProviderSelector.tsx
            ├── ModelSelector.tsx
            ├── ApiKeyInput.tsx
            ├── JobProgress.tsx
            ├── LogPanel.tsx
            └── DownloadButton.tsx
```

---

### Modèles Pydantic (schemas/__init__.py)

#### Enums

```python
class JobStatus(str, Enum): QUEUED / STARTED / RUNNING / COMPLETED / FAILED
class LineStatus(str, Enum): PENDING / CORRECTED / FALLBACK / FAILED
class ChunkGranularity(str, Enum): PAGE / BLOCK / WINDOW / LINE
class Provider(str, Enum): OPENAI / ANTHROPIC / MISTRAL / GOOGLE
class HyphenRole(str, Enum):
    NONE = "none"
    PART1 = "HypPart1"    # dernière ligne de la paire : porte le fragment gauche
    PART2 = "HypPart2"    # première ligne de la paire : porte le fragment droit
```

#### Coords

```python
class Coords(BaseModel):
    hpos: int; vpos: int; width: int; height: int
```

#### LineManifest — champs de césure ajoutés

```python
class LineManifest(BaseModel):
    line_id: str
    page_id: str
    block_id: str
    line_order_global: int
    line_order_in_block: int
    coords: Coords
    ocr_text: str
    prev_line_id: Optional[str] = None
    next_line_id: Optional[str] = None
    expected: bool = True
    received: bool = False
    corrected_text: Optional[str] = None
    status: LineStatus = LineStatus.PENDING

    # ── Champs de césure ─────────────────────────────────────────────
    hyphen_role: HyphenRole = HyphenRole.NONE
    # PART1 : cette ligne se termine par la première partie d'un mot coupé
    # PART2 : cette ligne commence par la deuxième partie d'un mot coupé

    hyphen_pair_line_id: Optional[str] = None
    # ID de la ligne jumelle dans la paire (PART1 → pointe vers PART2 et vice-versa)

    hyphen_subs_content: Optional[str] = None
    # Mot logique complet si présent dans SUBS_CONTENT de l'ALTO source
    # Exemple : "porte" pour une paire (por- / te)

    hyphen_source_explicit: bool = False
    # True si la césure provient de SUBS_TYPE ou HYP dans l'ALTO source
    # False si elle a été détectée heuristiquement (dernier token finissant par -)
```

#### Autres modèles (inchangés)

```python
class BlockManifest(BaseModel):
    block_id: str; page_id: str; block_order: int
    coords: Coords; line_ids: list[str]

class PageManifest(BaseModel):
    page_id: str; source_file: str; page_index: int
    page_width: int; page_height: int
    blocks: list[BlockManifest]; lines: list[LineManifest]
    status: JobStatus = JobStatus.QUEUED

class DocumentManifest(BaseModel):
    document_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source_files: list[str]; pages: list[PageManifest]
    total_pages: int; total_blocks: int; total_lines: int
    status: JobStatus = JobStatus.QUEUED

class ChunkPlannerConfig(BaseModel):
    max_input_chars_per_request: int = 12000
    max_lines_per_request: int = 80
    line_window_size: int = 12
    line_window_overlap: int = 1

class ChunkRequest(BaseModel):
    chunk_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    document_id: str; page_id: str; block_id: Optional[str]
    granularity: ChunkGranularity; line_ids: list[str]; attempt: int = 0

class ChunkPlan(BaseModel):
    page_id: str; chunks: list[ChunkRequest]; granularity: ChunkGranularity

class JobManifest(BaseModel):
    job_id: str; provider: Provider; model: str
    status: JobStatus = JobStatus.QUEUED
    document_manifest: Optional[DocumentManifest] = None
    total_lines: int = 0; lines_modified: int = 0
    chunks_total: int = 0; retries: int = 0; fallbacks: int = 0
    duration_seconds: Optional[float] = None; error: Optional[str] = None
```

#### Payload LLM enrichi

```python
class LLMLineInput(BaseModel):
    line_id: str
    prev_text: Optional[str] = None
    ocr_text: str
    next_text: Optional[str] = None

    # Champs de césure — absents si hyphen_role == NONE
    hyphenation_role: Optional[str] = None          # "HypPart1" | "HypPart2"
    hyphen_candidate: Optional[bool] = None
    hyphen_join_with_next: Optional[bool] = None    # présent sur PART1
    hyphen_join_with_prev: Optional[bool] = None    # présent sur PART2
    logical_join_candidate: Optional[str] = None    # mot logique si connu

class LLMUserPayload(BaseModel):
    task: str = "correct_ocr_lines"
    granularity: ChunkGranularity
    document_id: str; page_id: str; block_id: Optional[str]
    lines: list[LLMLineInput]
```

---

### Parser ALTO (alto/parser.py)

**Responsabilité :** lire un fichier ALTO XML, extraire pages/blocs/lignes, retourner des PageManifest. Détecter et annoter les césures interlignes.

#### Règles générales

- Détecter automatiquement le namespace depuis le tag racine
- Supporter ALTO v2, v3, v4, et sans namespace
- Pour chaque `TextLine`, extraire : ID, HPOS, VPOS, WIDTH, HEIGHT
- Reconstruire `ocr_text` :
  - `String` → append `CONTENT`
  - `SP` → append `" "`
  - `HYP` → append `CONTENT` si présent, sinon `"-"`
- Normaliser en Unicode NFC, supprimer `\r`, strip bords
- Lier `prev_line_id` / `next_line_id` entre lignes consécutives

#### Détection des césures — règles de priorité

**Cas 1 — Césure explicite (source_explicit = True) :**

Lors du parcours des enfants d'une TextLine, détecter :
- Un élément `HYP` présent en dernière position (= PART1)
- Un attribut `SUBS_TYPE="HypPart1"` sur le dernier `String` (= PART1)
- Un attribut `SUBS_TYPE="HypPart2"` sur le premier `String` (= PART2)
- Extraire `SUBS_CONTENT` s'il est présent sur l'un ou l'autre

**Cas 2 — Césure heuristique (source_explicit = False) :**

Si aucun marquage SUBS_TYPE/HYP n'est présent mais que le dernier token non-espace de la ligne se termine par `-` : marquer comme candidate heuristique. Mode conservateur : ne pas inventer de `SUBS_CONTENT`.

**Liaison des paires :**

Après avoir parcouru toutes les lignes de la page, faire un second pass :
- Pour chaque ligne marquée PART1, la ligne suivante dans l'ordre global est candidate PART2
- Si la ligne suivante porte déjà PART2 ou est une candidate heuristique cohérente → créer le lien bidirectionnel via `hyphen_pair_line_id`
- Si `SUBS_CONTENT` est présent sur PART1 et absent sur PART2 (ou vice-versa), propager la valeur sur les deux

**Signatures principales :**

```python
def build_document_manifest(files: list[tuple[Path, str]]) -> DocumentManifest
def parse_alto_file(xml_path, source_name, page_index_offset, global_line_offset)
    -> tuple[list[PageManifest], etree._Element]
def _detect_hyphenation(lines: list[LineManifest]) -> None
    # Mutates lines in-place : remplit hyphen_role, hyphen_pair_line_id, hyphen_subs_content
```

---

### Hyphenation Reconciler (alto/hyphenation.py)

C'est le module central ajouté par rapport à la V1. Son rôle est d'orchestrer la gestion des mots cassés entre deux lignes : **l'application décide, le LLM informe**.

**Principe fondamental :**

> Les césures interlignes ne doivent pas être laissées à la seule initiative du LLM. L'application détecte les paires de lignes liées par césure, transmet cette information au modèle, puis réinscrit la sortie sur les deux lignes physiques. En cas d'ambiguïté, la forme source est préservée.

#### Responsabilités du module

1. **`enrich_chunk_lines()`** — préparer les `LLMLineInput` enrichis avec métadonnées de césure
2. **`reconcile_hyphen_pair()`** — après réponse LLM, réinscrire la correction sur la paire physique
3. **`should_stay_in_same_chunk()`** — prédicat pour le chunk planner

#### Fonction `enrich_chunk_lines()`

```python
def enrich_chunk_lines(
    line_manifests: list[LineManifest],
    all_lines_by_id: dict[str, LineManifest],
) -> list[LLMLineInput]:
```

Pour chaque ligne, construire le `LLMLineInput` avec :
- `prev_text` / `next_text` comme d'habitude
- Si `hyphen_role != NONE` :
  - Renseigner `hyphenation_role`, `hyphen_candidate = True`
  - Sur PART1 : `hyphen_join_with_next = True`
  - Sur PART2 : `hyphen_join_with_prev = True`
  - Si `hyphen_subs_content` connu : `logical_join_candidate = hyphen_subs_content`

#### Fonction `reconcile_hyphen_pair()`

```python
def reconcile_hyphen_pair(
    part1: LineManifest,
    part2: LineManifest,
    corrected_part1: str,
    corrected_part2: str,
) -> tuple[str, str, Optional[str]]:
    """
    Retourne (final_text_part1, final_text_part2, resolved_subs_content).

    Garantit :
    - Les deux lignes physiques restent distinctes
    - Aucun texte ne migre d'une ligne à l'autre
    - Si la correction est ambiguë, retourner les textes source
    """
```

**Algorithme :**

```
1. Isoler le dernier token non-espace de corrected_part1 (candidat fragment gauche)
2. Isoler le premier token non-espace de corrected_part2 (candidat fragment droit)
3. Si source_explicit == True (césure encodée dans l'ALTO source) :
   a. Si hyphen_subs_content connu → utiliser comme référence pour valider
   b. Vérifier que la concaténation (fragment_gauche + fragment_droit) est cohérente
      avec le mot logique attendu (si connu)
   c. Conserver les frontières physiques : part1 garde son texte, part2 garde le sien
   d. resolved_subs_content = mot logique déterminé avec confiance
4. Si source_explicit == False (heuristique) :
   a. Mode conservateur : ne rien reconstruire agressivement
   b. Retourner corrected_part1, corrected_part2 tels quels
   c. resolved_subs_content = None
5. En cas de doute à n'importe quelle étape : retourner les textes OCR source
```

**Ce que cette fonction ne fait JAMAIS :**
- Fusionner les deux lignes en une
- Déplacer "porte" sur la ligne 1 et vider la ligne 2
- Inventer un SUBS_CONTENT sans base dans la source

#### Fonction `should_stay_in_same_chunk()`

```python
def should_stay_in_same_chunk(
    line_a: LineManifest,
    line_b: LineManifest,
) -> bool:
    """
    Retourne True si line_a et line_b doivent impérativement être
    dans le même chunk LLM (paire liée par césure).
    """
    return (
        line_a.hyphen_role == HyphenRole.PART1
        and line_a.hyphen_pair_line_id == line_b.line_id
    ) or (
        line_b.hyphen_role == HyphenRole.PART1
        and line_b.hyphen_pair_line_id == line_a.line_id
    )
```

---

### Rewriter ALTO (alto/rewriter.py)

**Responsabilité :** réécrire un fichier ALTO en remplaçant les enfants textuels des TextLine, en reconstituant HYP et SUBS_* pour les paires de césure.

#### Invariants absolus à respecter

- Ne jamais modifier `TextLine/@ID`, `/@HPOS`, `/@VPOS`, `/@WIDTH`, `/@HEIGHT`
- Ne jamais changer l'ordre XML des `TextLine`
- Ne jamais fusionner deux TextLine

#### Algorithme par TextLine — cas sans césure

1. Supprimer tous les enfants `String`, `SP`, `HYP` existants
2. Supprimer attributs `WC`, `CC` de la TextLine
3. Tokeniser `corrected_text` avec `re.split(r'(\s+)', text)`
4. Segments espace → élément `SP`
5. Segments non-espace → élément `String` avec ID `{line_id}_STR_{n:04d}`
6. Géométrie heuristique : redistribuer `TextLine.WIDTH` proportionnellement à `len(token)`
7. Tous les nouveaux `String` héritent de `VPOS` et `HEIGHT` de la TextLine

**Géométrie proportionnelle :**
- Poids mot = `len(mot)`
- Poids espace = `max(1, round(len(espace) * 0.6 * unit))`
- `unit = TextLine.WIDTH / total_poids`
- Corriger l'arrondi sur le dernier token pour que la somme = `TextLine.WIDTH` exact

#### Algorithme par TextLine — cas PART1 (ligne terminée par césure)

Condition : `line_manifest.hyphen_role == HyphenRole.PART1`

1. Supprimer les enfants existants
2. Construire les `String` pour tous les tokens jusqu'à l'avant-dernier mot inclus
3. Pour le dernier mot (fragment gauche) :
   - Créer un `String` avec son `CONTENT` (ex: `"por"`)
   - Si `hyphen_subs_content` est connu : ajouter `SUBS_TYPE="HypPart1"` et `SUBS_CONTENT=hyphen_subs_content`
4. Créer un élément `HYP` après ce dernier `String` :
   - `CONTENT="-"`, `HPOS/VPOS/WIDTH/HEIGHT` heuristiques en fin de ligne

#### Algorithme par TextLine — cas PART2 (ligne commençant par suite de césure)

Condition : `line_manifest.hyphen_role == HyphenRole.PART2`

1. Supprimer les enfants existants
2. Pour le premier mot (fragment droit) :
   - Créer un `String` avec son `CONTENT` (ex: `"te"`)
   - Si `hyphen_subs_content` est connu : ajouter `SUBS_TYPE="HypPart2"` et `SUBS_CONTENT=hyphen_subs_content`
3. Construire les `String` + `SP` pour les tokens suivants normalement

#### Politique de confiance pour SUBS_CONTENT

| Condition | Action |
|-----------|--------|
| `source_explicit=True` et `hyphen_subs_content` fourni par source | Écrire SUBS_CONTENT tel quel |
| `source_explicit=True` et SUBS_CONTENT résolu par reconciler avec confiance | Écrire SUBS_CONTENT résolu |
| `source_explicit=False` (heuristique) | Ne pas écrire SUBS_CONTENT |
| Ambiguïté ou incertitude | Ne pas écrire SUBS_CONTENT |

Ajouter une entrée de processing dans `Description/Processing` si la section existe.

---

### Chunk Planner (jobs/chunk_planner.py)

**Règle additionnelle : les paires de césure ne peuvent pas être séparées.**

Le planner doit intégrer la contrainte `should_stay_in_same_chunk()` du Hyphenation Reconciler à chaque niveau de découpage.

#### Hiérarchie de décision

```
1. PAGE ENTIÈRE
   Condition : total chars ≤ 12000 ET total lignes ≤ 80
   → 1 seul chunk contenant toutes les lignes de la page

2. BLOC PAR BLOC
   Condition : chaque bloc tient dans les budgets
   MAIS : si une paire de césure est à cheval sur deux blocs,
          les deux blocs concernés doivent être regroupés dans un seul chunk.
   → Si un regroupement dépasse le budget → invalide, passer à WINDOW

3. FENÊTRES DE LIGNES
   window_size=12, overlap=1, step=11
   MAIS : aucune fenêtre ne peut couper une paire de césure en deux.
   Règle : si la ligne N est PART1 et que la ligne N+1 est sa PART2,
           et que N est le dernier index d'une fenêtre,
           étendre la fenêtre d'une ligne pour inclure N+1.
   → Chevauchement possible : ajuster le step pour ne pas laisser de paire orpheline.

4. LIGNE PAR LIGNE (dernier recours)
   Si une ligne fait partie d'une paire de césure,
   traiter la paire comme un bloc atomique inséparable :
   → le "chunk ligne" contient en réalité 2 lignes liées.
```

**Fonction `downgrade_granularity(current)` :** inchangée — retourne le niveau suivant ou None.

---

### Prompt système (providers/base.py)

Le prompt système est enrichi avec une règle explicite sur les césures :

```
Tu es un moteur de correction post-OCR spécialisé dans les documents patrimoniaux.

Règles absolues :
1. Corrige uniquement les erreurs manifestes d'OCR.
2. Conserve la langue source.
3. Conserve l'orthographe historique quand elle semble intentionnelle.
4. Ne traduis rien.
5. Ne modernise pas volontairement le texte.
6. Ne fusionne jamais deux lignes.
7. Ne scinde jamais une ligne.
8. Ne déplace jamais du texte d'une ligne à l'autre.
9. Chaque entrée line_id doit produire exactement une sortie avec le même line_id.
10. corrected_text doit contenir une seule ligne, sans caractère de saut de ligne.
11. Retourne uniquement un JSON valide conforme au schéma fourni.
12. En cas d'incertitude, fais la correction minimale.
13. Quand une ligne porte hyphenation_role="HypPart1" ou "HypPart2",
    tu dois corriger chaque ligne individuellement sans déplacer de texte
    entre elles. Le mot logique (logical_join_candidate) t'est fourni
    à titre indicatif uniquement pour le contexte.
```

#### Payload user enrichi — exemple avec césure

```json
{
  "task": "correct_ocr_lines",
  "granularity": "window",
  "document_id": "DOC_001",
  "page_id": "P_001",
  "lines": [
    {
      "line_id": "TL_101",
      "prev_text": "Il marchait vite.",
      "ocr_text": "Il s'approcha de la por-",
      "next_text": "te du palais",
      "hyphenation_role": "HypPart1",
      "hyphen_candidate": true,
      "hyphen_join_with_next": true,
      "logical_join_candidate": "porte"
    },
    {
      "line_id": "TL_102",
      "prev_text": "Il s'approcha de la por-",
      "ocr_text": "te du palais",
      "next_text": "La garde était présente.",
      "hyphenation_role": "HypPart2",
      "hyphen_candidate": true,
      "hyphen_join_with_prev": true,
      "logical_join_candidate": "porte"
    }
  ]
}
```

Le LLM corrige chaque ligne pour ses erreurs OCR éventuelles, mais ne déplace aucun fragment d'une ligne à l'autre. C'est le Hyphenation Reconciler qui gère ensuite la reconstruction ALTO.

#### Schéma JSON de sortie (inchangé)

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["lines"],
  "properties": {
    "lines": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["line_id", "corrected_text"],
        "properties": {
          "line_id": {"type": "string"},
          "corrected_text": {"type": "string"}
        }
      }
    }
  }
}
```

---

### Validateur (jobs/validator.py)

Après chaque réponse LLM, valider :
1. Présence de la clé `"lines"`
2. Nombre d'entrées = nombre attendu
3. Tous les `line_id` attendus présents
4. Aucun `line_id` doublon ou inconnu
5. Chaque `corrected_text` : string non vide, sans `\n` ni `\r`

**Validation additionnelle pour les paires de césure :**

Si le chunk contient une paire PART1/PART2, vérifier que :
- `corrected_text` de PART1 ne contient pas le texte logique entier du mot coupé (ce serait une fusion interdite)
- `corrected_text` de PART2 n'est pas vide (la suite de la césure ne doit pas avoir disparu)

En cas de violation sur une paire de césure : lever `ValueError` avec motif `"hyphen_integrity_violation"`.

---

### Orchestrateur (jobs/orchestrator.py)

L'orchestrateur intègre le Hyphenation Reconciler **avant** et **après** chaque appel LLM.

#### Pipeline par chunk

```
AVANT l'appel LLM :
  1. Récupérer les LineManifest du chunk
  2. Appeler enrich_chunk_lines() → LLMLineInput enrichis avec métadonnées césure
  3. Construire le payload user

APPEL LLM (inchangé)

APRÈS l'appel LLM :
  4. Valider la réponse (validator.py)
  5. Pour chaque paire PART1/PART2 présente dans le chunk :
     a. Extraire corrected_part1 et corrected_part2 depuis la réponse
     b. Appeler reconcile_hyphen_pair(part1, part2, corrected_part1, corrected_part2)
     c. Remplacer les corrected_text dans le résultat par les textes réconciliés
     d. Stocker resolved_subs_content sur les deux LineManifest
  6. Appliquer les corrections finales aux LineManifest
```

#### Politique de retry — cas spécifique aux paires de césure

Si la validation échoue avec `"hyphen_integrity_violation"` :
- Ne pas downgrader la granularité
- Retry immédiat avec temperature=0 et prompt plus explicite sur la règle 13
- Si second échec : conserver les textes OCR source pour les deux lignes de la paire

#### Politique générale de retry (inchangée)

| Tentative | Action |
|-----------|--------|
| 1 | Appel normal |
| 2 | Retry même chunk, temperature=0 |
| 3 | Retry encore |
| Après 3 échecs | Downgrade granularité |
| Plus de granularité | Conserver texte OCR source, logger warning |

---

### Fournisseurs LLM (providers/)

**Protocole commun :**
```python
class BaseProvider(Protocol):
    async def list_models(self, api_key: str) -> list[ModelInfo]: ...
    async def complete_structured(
        self, api_key, model, system_prompt, user_payload, json_schema, temperature=0.0
    ) -> dict: ...
```

**OpenAI :**
- Lister : `GET /v1/models` + allowlist préfixes (`gpt-4`, `gpt-3.5`, `o1`, `o3`, `o4`)
- Exclure : `instruct`, `embedding`, `whisper`, `tts`, `dall-e`, `moderation`, `realtime`, `audio`
- Générer : `POST /v1/chat/completions` avec `response_format.type = "json_schema"`

**Anthropic :**
- Lister : `GET /v1/models` (headers: `x-api-key`, `anthropic-version: 2023-06-01`)
- Générer : `POST /v1/messages` avec `output_config.format.type = "json_schema"`
- Fallback si 400/422 : plain JSON

**Mistral :**
- Lister : `GET /v1/models`, filtrer `capabilities.completion_chat == true`
- Générer : `POST /v1/chat/completions` avec `response_format.type = "json_schema"`
- Fallback si 400/422 : `response_format.type = "json_object"`

**Google Gemini :**
- Lister : `GET .../v1beta/models?key={api_key}`, filtrer `generateContent` dans `supportedGenerationMethods`
- Exclure : `embed`, `aqa`, `attribute`
- Générer : `POST .../models/{model}:generateContent` avec `responseMimeType: "application/json"` et `responseSchema`

---

### Routes API (api/)

```
POST /api/providers/models
  Body: {provider, api_key}
  Response: {provider, models: [{id, label, supports_structured_output, context_window}]}

POST /api/jobs
  multipart/form-data: files[], provider, api_key, model
  Response: {job_id}

GET /api/jobs/{job_id}
  Response: JobStatusResponse

GET /api/jobs/{job_id}/events
  SSE stream

GET /api/jobs/{job_id}/download
  Response: XML (1 fichier) ou ZIP (plusieurs fichiers)
```

#### SSE Events

| Événement | Données clés |
|-----------|-------------|
| `queued` | job_id |
| `started` | job_id |
| `document_parsed` | total_pages, total_blocks, total_lines, hyphen_pairs |
| `page_started` | page_id, page_index, line_count, hyphen_pair_count |
| `chunk_planned` | page_id, granularity, chunk_count |
| `chunk_started` | chunk_id, granularity, line_count, attempt |
| `chunk_completed` | chunk_id, line_count, hyphen_pairs_reconciled, attempt |
| `retry` | chunk_id, attempt, error |
| `warning` | message |
| `page_completed` | page_id, page_index, corrections |
| `completed` | total_lines, lines_modified, hyphen_pairs_total, duration_seconds |
| `failed` | error |
| `keepalive` | {} |

---

### Stockage (storage/__init__.py)

```
/tmp/app-jobs/{job_id}/
  input/          ← fichiers uploadés (XML extraits)
  outputs/        ← fichiers ALTO corrigés (*_corrected.xml)
```

- Accepter `.xml`, `.alto.xml`, `.zip`
- Si ZIP : extraire tous les XML, flatten les chemins (basename seulement)
- Multi-fichiers : document multi-pages, ordre = ordre d'upload

---

### Interface utilisateur

**Écran unique :**

1. **Header** — titre + sous-titre
2. **Upload** — drag & drop, liste ordonnée des fichiers + nb de paires de césure détectées
3. **Configuration** — sélecteur fournisseur + clé API masquée + bouton "Charger les modèles" + sélecteur modèle
4. **Contrôles** — bouton Play (disabled si config incomplète)
5. **Progression** — barre globale + compteur pages/lignes/paires césure réconciliées
6. **Logs** — panel scrollable SSE en temps réel, code couleur par type
7. **Résultats** — bouton télécharger + stats (lignes modifiées, paires réconciliées, durée)

**Règles UX :**
- Play activé uniquement si : fichier(s) + fournisseur + clé API + modèle
- Clé API jamais loguée, jamais renvoyée au frontend

---

### Déploiement Hugging Face Spaces

**Dockerfile racine :**
- Base : `python:3.11-slim`
- Build frontend React (`npm run build`) dans `/app/static`
- FastAPI sert `/app/static` comme `StaticFiles` sur `/`
- **Port obligatoire : 7860**
- `CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7860"]`

**docker-compose.yml (dev local) :**
- `backend` : port 8000
- `frontend` : port 5173 avec proxy vers backend

---

### Sécurité

- Ne jamais logger la clé API
- Ne jamais écrire la clé API sur disque
- Ne jamais renvoyer la clé au frontend
- Whitelist extensions uploadées : `.xml`, `.alto`, `.zip`
- Nettoyer les fichiers temporaires après téléchargement

---

### Tests obligatoires

**Unit tests :**

`test_parser.py` :
- Détection namespace v2/v3/v4/sans ns
- Reconstruction ocr_text (String, SP, HYP)
- Construction PageManifest (nb pages, blocs, lignes)
- Liens prev/next
- Détection césure explicite (SUBS_TYPE + SUBS_CONTENT)
- Détection césure heuristique (dernier token en `-`)
- Liaison bidirectionnelle des paires

`test_hyphenation.py` :
- `enrich_chunk_lines` : PART1 reçoit `hyphen_join_with_next`, PART2 reçoit `hyphen_join_with_prev`
- `enrich_chunk_lines` : `logical_join_candidate` présent si `hyphen_subs_content` connu
- `reconcile_hyphen_pair` : textes non fusionnés, frontière physique préservée
- `reconcile_hyphen_pair` : source_explicit=True + subs_content connu → résolution avec confiance
- `reconcile_hyphen_pair` : source_explicit=False → mode conservateur, pas de SUBS_CONTENT
- `reconcile_hyphen_pair` : cas ambigu → retour des textes source
- `should_stay_in_same_chunk` : vrai pour PART1/PART2 liés, faux pour lignes normales

`test_rewriter.py` :
- Préservation TextLine ID/coords
- Tokenisation et géométrie proportionnelle (sum widths == TextLine.WIDTH)
- Reconstruction HYP sur PART1
- Reconstruction SUBS_TYPE/SUBS_CONTENT sur PART1 et PART2 (quand confiance suffisante)
- Pas de SUBS_CONTENT sur césure heuristique
- Round-trip (parse → rewrite sans correction → re-parse → mêmes IDs)

`test_chunk_planner.py` :
- Cas page, bloc, fenêtre, ligne
- Une paire PART1/PART2 n'est jamais séparée par une frontière de fenêtre
- Downgrade granularité

`test_validator.py` :
- Réponse valide
- Missing/doublon/inconnu line_id
- Newline dans text
- `hyphen_integrity_violation` : PART2 vide ou PART1 contient tout le mot

**Integration tests :**
- Upload XML simple → job → download ALTO valide
- Upload ZIP → extraction → job
- Document avec paires de césure → ALTO de sortie avec HYP/SUBS_* corrects
- Fallback JSON invalide → retry → downgrade

---

## PARTIE 2 — SPRINTS DE DÉVELOPPEMENT

### Vue d'ensemble des sprints

| Sprint | Nom | Durée est. | Dépend de |
|--------|-----|-----------|-----------|
| 0 | Bootstrap & infrastructure | 1-2h | — |
| 1 | Schemas + Parser ALTO (avec détection césures) | 2-3h | Sprint 0 |
| 2 | Hyphenation Reconciler | 2-3h | Sprint 1 |
| 3 | Rewriter ALTO (avec HYP/SUBS_* ) | 2h | Sprint 1, 2 |
| 4 | Providers LLM | 2-3h | Sprint 1 |
| 5 | Chunk Planner + Validateur (hyphen-aware) | 2h | Sprint 1, 2 |
| 6 | Orchestrateur + Job Store | 2-3h | Sprint 2, 3, 4, 5 |
| 7 | Routes API FastAPI | 2h | Sprint 6 |
| 8 | Frontend React | 3-4h | Sprint 7 |
| 9 | Docker + HF Spaces | 1-2h | Sprint 8 |
| 10 | Tests d'intégration + polish | 2h | Sprint 9 |

---

### SPRINT 0 — Bootstrap & infrastructure

**Objectif :** structure de repo opérationnelle, environnements configurés.

**Tâches pour Claude Code :**
```
1. Crée l'arborescence complète selon SPECS.md section "Arborescence cible"

2. Crée backend/requirements.txt :
   fastapi, uvicorn[standard], pydantic[v2], httpx, lxml,
   python-multipart, aiofiles, sse-starlette, pytest, pytest-asyncio

3. Crée frontend/package.json :
   react, typescript, vite, tailwindcss, @types/react, autoprefixer

4. Crée .env.example :
   JOB_STORAGE_DIR=/tmp/app-jobs
   CORS_ORIGINS=*

5. Crée examples/sample.xml : ALTO v3 minimal avec :
   - 2 pages
   - 3 blocs, 10 lignes au total
   - Texte français avec quelques erreurs OCR manifestes
   - AU MOINS une paire de lignes avec césure explicite (SUBS_TYPE + SUBS_CONTENT)
   - AU MOINS une ligne finissant par un tiret sans SUBS_TYPE (cas heuristique)

6. Initialise git, crée .gitignore
7. Lance /init → CLAUDE.md
```

**Critères d'acceptation :**
- `pip install -r requirements.txt` passe
- `npm install` passe
- `examples/sample.xml` est un ALTO v3 valide parseable par lxml
- Il contient une paire avec SUBS_TYPE="HypPart1" et une avec tiret heuristique
- CLAUDE.md généré

---

### SPRINT 1 — Schemas + Parser ALTO

**Objectif :** modèles Pydantic complets incluant les champs de césure, parser détectant les paires.

**Prompt de lancement :**
```
Lis SPECS.md sections "Modèles Pydantic" et "Parser ALTO".
Implémente dans l'ordre :
1. app/schemas/__init__.py — tous les modèles dont HyphenRole et les champs de césure de LineManifest
2. app/alto/parser.py — parsing complet avec _detect_hyphenation()
3. backend/tests/test_parser.py — tests incluant les cas de césure
Lance pytest, corrige, committe "feat: schemas + alto parser with hyphenation detection"
```

**Tâches :**
```
1. Implémente app/schemas/__init__.py avec :
   - HyphenRole enum : NONE / PART1 / PART2
   - LineManifest avec les 4 champs de césure :
     hyphen_role, hyphen_pair_line_id, hyphen_subs_content, hyphen_source_explicit
   - LLMLineInput avec les champs hyphenation_role, hyphen_candidate,
     hyphen_join_with_next, hyphen_join_with_prev, logical_join_candidate
   - Tous les autres modèles (BlockManifest, PageManifest, DocumentManifest,
     ChunkPlannerConfig, ChunkRequest, ChunkPlan, JobManifest, etc.)

2. Implémente app/alto/parser.py :
   - Détection namespace automatique
   - Reconstruction ocr_text via String/SP/HYP
   - Normalisation NFC + strip
   - Liens prev_line_id / next_line_id
   - _detect_hyphenation(lines) qui détecte :
     a. SUBS_TYPE="HypPart1" sur dernier String d'une ligne → PART1, source_explicit=True
     b. SUBS_TYPE="HypPart2" sur premier String d'une ligne → PART2, source_explicit=True
     c. HYP élément présent en fin de ligne → PART1, source_explicit=True
     d. dernier token finissant par "-" sans marquage SUBS → PART1, source_explicit=False
     e. Propagation SUBS_CONTENT sur les deux lignes de la paire
     f. Liaison bidirectionnelle hyphen_pair_line_id
   - build_document_manifest(files) -> DocumentManifest

3. Écris tests/test_parser.py :
   - test_namespace_v2_v3_v4_none
   - test_ocr_text_string_sp_hyp
   - test_page_manifest_counts
   - test_prev_next_links
   - test_hyphen_explicit_subs_type : détecter PART1/PART2 + SUBS_CONTENT depuis sample.xml
   - test_hyphen_explicit_hyp_element : détecter PART1 depuis un élément HYP
   - test_hyphen_heuristic : détecter candidat heuristique (tiret sans SUBS_TYPE)
   - test_hyphen_pair_bidirectional : vérifier que PART1.pair_id == PART2.line_id et vice-versa
   - test_hyphen_subs_content_propagated : même valeur sur les deux lignes de la paire
   - test_multi_file : build_document_manifest avec 2 fichiers

4. pytest -v tests/test_parser.py → 100% vert
5. Committe : "feat: schemas + alto parser with hyphenation detection"
```

**Critères d'acceptation :**
- 100% vert
- Sur sample.xml : paire explicite détectée avec `source_explicit=True`
- Sur sample.xml : candidat heuristique détecté avec `source_explicit=False`
- Aucun import circulaire

---

### SPRINT 2 — Hyphenation Reconciler

**Objectif :** le module central de gestion des paires de césure est opérationnel et testable indépendamment.

**Prompt de lancement :**
```
Lis SPECS.md section "Hyphenation Reconciler".
Implémente app/alto/hyphenation.py avec ses 3 fonctions.
Principe : l'app orchestre, le LLM informe seulement.
Les frontières physiques ne bougent jamais.
Lance les tests, committe "feat: hyphenation reconciler"
```

**Tâches :**
```
1. Implémente app/alto/hyphenation.py :

   def enrich_chunk_lines(
       line_manifests: list[LineManifest],
       all_lines_by_id: dict[str, LineManifest],
   ) -> list[LLMLineInput]:
   # Construit les payloads enrichis avec métadonnées de césure
   # Les champs hyphenation_role, hyphen_candidate, etc. sont absents
   # (None) si hyphen_role == NONE — pas de bruit inutile pour le LLM

   def reconcile_hyphen_pair(
       part1: LineManifest,
       part2: LineManifest,
       corrected_part1: str,
       corrected_part2: str,
   ) -> tuple[str, str, Optional[str]]:
   # Retourne (final_text_part1, final_text_part2, resolved_subs_content)
   # Algorithme décrit dans SPECS.md
   # NE JAMAIS fusionner les deux lignes
   # En cas de doute → retourner les textes OCR source

   def should_stay_in_same_chunk(
       line_a: LineManifest,
       line_b: LineManifest,
   ) -> bool:
   # True si la paire est liée par hyphen_pair_line_id

2. Écris tests/test_hyphenation.py :
   - test_enrich_part1_has_join_with_next
   - test_enrich_part2_has_join_with_prev
   - test_enrich_logical_candidate_present_when_known
   - test_enrich_no_hyphen_fields_on_normal_line
   - test_reconcile_explicit_preserves_boundaries :
       part1="Il s'approcha de la por-", part2="te du palais"
       → résultat : part1 inchangé, part2 inchangé, subs_content="porte"
   - test_reconcile_heuristic_conservative :
       source_explicit=False → subs_content=None, textes inchangés
   - test_reconcile_ambiguous_returns_source :
       LLM retourne quelque chose d'incohérent → retour OCR source
   - test_reconcile_no_line_fusion :
       vérifier que len(part1) + len(part2) > 0 et les deux lignes existent toujours
   - test_should_stay_linked_pair : retourne True
   - test_should_stay_unrelated_lines : retourne False

3. pytest -v tests/test_hyphenation.py → 100% vert
4. Committe : "feat: hyphenation reconciler"
```

**Critères d'acceptation :**
- `reconcile_hyphen_pair` ne fusionne jamais deux lignes en une
- Le mode conservateur (source_explicit=False) ne produit jamais de SUBS_CONTENT
- 100% vert

---

### SPRINT 3 — Rewriter ALTO

**Objectif :** l'ALTO rewriter reconstruit des TextLine correctes, gère HYP et SUBS_* pour les paires.

**Prompt de lancement :**
```
Lis SPECS.md section "Rewriter ALTO".
Implémente app/alto/rewriter.py.
Il doit gérer 3 cas : ligne normale, PART1 (avec HYP), PART2 (avec SUBS_TYPE).
Lance les tests, committe "feat: alto rewriter with hyphenation"
```

**Tâches :**
```
1. Implémente app/alto/rewriter.py :
   - _tokenize(text) -> list[str]
   - _compute_geometry(hpos, width, tokens) -> list[tuple[str, int, int]]
     Algorithme proportionnel, correction arrondi sur dernier token
   - _rebuild_normal_line(line_el, corrected_text, manifest, ns)
     Supprime String/SP/HYP, reconstruit String + SP
   - _rebuild_hyp_part1(line_el, corrected_text, manifest, ns)
     Reconstruit les mots normaux, puis dernier mot avec SUBS_TYPE="HypPart1"
     si source_explicit et subs_content connu, puis élément HYP
   - _rebuild_hyp_part2(line_el, corrected_text, manifest, ns)
     Premier mot avec SUBS_TYPE="HypPart2" et SUBS_CONTENT si connu,
     puis mots suivants normalement
   - rewrite_alto_file(xml_path, page_manifests, provider, model) -> bytes
     Dispatcher vers la bonne fonction selon hyphen_role
     Ajouter entrée Processing si Description existe
   - Politique SUBS_CONTENT : écrire uniquement si source_explicit=True
     et hyphen_subs_content non None

2. Écris tests/test_rewriter.py :
   - test_normal_line_tokenize
   - test_geometry_sum_equals_width (invariant critique)
   - test_line_id_preserved
   - test_coords_preserved (HPOS/VPOS/WIDTH/HEIGHT de TextLine)
   - test_string_ids_pattern ({line_id}_STR_{n:04d})
   - test_no_newline_in_content
   - test_part1_has_hyp_element : la ligne PART1 se termine par un élément HYP
   - test_part1_subs_type : String final de PART1 porte SUBS_TYPE="HypPart1"
     quand source_explicit=True et subs_content connu
   - test_part2_subs_type : String initial de PART2 porte SUBS_TYPE="HypPart2"
   - test_subs_content_written_explicit : SUBS_CONTENT écrit si source_explicit
   - test_subs_content_absent_heuristic : pas de SUBS_CONTENT si source_explicit=False
   - test_round_trip_normal : parse → rewrite sans correction → re-parse → mêmes IDs
   - test_round_trip_with_hyphen : idem sur paire de césure → HYP reconstruit

3. pytest -v tests/test_rewriter.py → 100% vert
4. Committe : "feat: alto rewriter with hyphenation"
```

**Critères d'acceptation :**
- Round-trip conserve exactement les mêmes line_ids
- Les paires de césure produisent un ALTO valide avec HYP et SUBS_* corrects
- SUBS_CONTENT absent sur les cas heuristiques

---

### SPRINT 4 — Providers LLM

**Objectif :** les 4 adaptateurs fournisseurs fonctionnent et listent réellement les modèles.

**Prompt de lancement :**
```
Lis SPECS.md section "Fournisseurs LLM".
Implémente les 4 providers avec leur protocole commun.
Le prompt système doit inclure la règle 13 sur les césures.
Tests avec mocks httpx, committe "feat: llm providers"
```

**Tâches :**
```
1. Implémente app/providers/base.py :
   - BaseProvider Protocol
   - OUTPUT_JSON_SCHEMA
   - SYSTEM_PROMPT avec la règle 13 (césures)

2. Implémente les 4 providers (openai, anthropic, mistral, google)
   selon les specs section "Fournisseurs LLM"
   Chaque provider : list_models() + complete_structured() + gestion fallback

3. Implémente app/providers/__init__.py : registry + get_provider()

4. Tests avec mocks httpx :
   - test_openai_model_filter
   - test_mistral_capability_filter
   - test_google_generate_content_filter
   - test_anthropic_parse_response
   - test_system_prompt_contains_hyphen_rule

5. pytest -v tests/test_providers.py → 100% vert
6. Committe : "feat: llm providers"
```

**Critères d'acceptation :**
- Le SYSTEM_PROMPT contient bien la règle 13
- get_provider() retourne le bon provider pour chaque enum
- 100% vert avec mocks

---

### SPRINT 5 — Chunk Planner + Validateur (hyphen-aware)

**Objectif :** le planificateur ne sépare jamais une paire de césure ; le validateur détecte les violations d'intégrité.

**Prompt de lancement :**
```
Lis SPECS.md sections "Chunk Planner" et "Validateur".
Point critique : les paires PART1/PART2 ne peuvent jamais être
séparées en deux chunks. Intègre should_stay_in_same_chunk()
dans la logique de fenêtrage.
Lance les tests, committe "feat: chunk planner + validator hyphen-aware"
```

**Tâches :**
```
1. Implémente app/jobs/chunk_planner.py :
   - plan_page(page, document_id, config, force_granularity=None) -> ChunkPlan
   - _plan_blocks() : regrouper blocs si paire à cheval sur deux blocs
   - _plan_windows() : ajuster les frontières pour ne pas couper une paire
     Règle : si ligne[i] est PART1 et ligne[i+1] est sa PART2,
             et que i est le dernier index d'une fenêtre → étendre d'une ligne
   - _plan_line_by_line() : traiter les paires comme unité atomique (2 lignes)
   - downgrade_granularity(current) -> ChunkGranularity | None

2. Implémente app/jobs/validator.py :
   - validate_llm_response(raw, expected_line_ids, hyphen_pairs=None) -> LLMResponse
   - hyphen_pairs : dict[str, str] = {part1_id: part2_id}
   - Validation de base (présence, count, IDs, no newline)
   - Validation additionelle si hyphen_pairs fourni :
     a. corrected_text de PART2 non vide
     b. corrected_text de PART1 ne contient pas le mot logique entier
        (détection heuristique : si le mot logique == corrected_part1.rstrip('-') → violation)

3. Écris tests/test_chunk_planner.py :
   - test_small_page_single_chunk
   - test_large_page_block_granularity
   - test_block_too_large_window_fallback
   - test_window_coverage_complete (toutes lignes couvertes)
   - test_hyphen_pair_not_split_by_window :
     créer une page avec paire en position i / i+1 là où une fenêtre couperait
     → vérifier qu'aucun chunk n'a seulement PART1 ou seulement PART2
   - test_hyphen_pair_atomic_in_line_mode
   - test_downgrade_sequence

4. Écris tests/test_validator.py :
   - test_valid_response
   - test_missing_lines_key / missing_line_id / duplicate / unknown / newline / empty
   - test_hyphen_part2_empty_violation
   - test_hyphen_part1_fusion_violation

5. pytest -v tests/test_chunk_planner.py tests/test_validator.py → 100% vert
6. Committe : "feat: chunk planner + validator hyphen-aware"
```

**Critères d'acceptation :**
- Aucune paire de césure n'est jamais séparée par une frontière de chunk
- Violation d'intégrité hyphen correctement détectée par le validateur

---

### SPRINT 6 — Orchestrateur + Job Store

**Objectif :** le moteur principal traite un document complet avec retry, fallback, et réconciliation des paires de césure.

**Prompt de lancement :**
```
Lis SPECS.md sections "Orchestrateur" et "Stockage".
L'orchestrateur doit intégrer enrich_chunk_lines() avant chaque appel LLM
et reconcile_hyphen_pair() après. Implémente aussi le job store
et le storage. Test avec mock provider sur sample.xml.
Committe "feat: orchestrator + job store"
```

**Tâches :**
```
1. Implémente app/jobs/store.py :
   - JobStore avec _jobs: dict et _subscribers: dict[str, list[asyncio.Queue]]
   - create_job(), get_job(), update_job()
   - emit(job_id, event, data) → distribue aux abonnés
   - subscribe/unsubscribe, stream_events() avec keepalive 30s
   - Singleton job_store = JobStore()

2. Implémente app/storage/__init__.py :
   - job_dir/input_dir/output_dir/init_job_dirs
   - save_uploaded_files : gère ZIP (zipfile), flatten paths
   - get_output_files, cleanup_job

3. Implémente app/jobs/orchestrator.py :
   Pipeline par chunk (voir SPECS.md section "Orchestrateur") :
   a. enrich_chunk_lines() → LLMLineInput enrichis
   b. Appel LLM
   c. Validation (avec hyphen_pairs si paire présente dans chunk)
   d. reconcile_hyphen_pair() pour chaque paire dans le chunk
   e. Stocker resolved_subs_content sur les LineManifest
   f. Politique retry spécifique si hyphen_integrity_violation
   g. Fallback général (downgrade granularité)
   h. rewrite_alto_file() avec les manifests enrichis
   i. Émettre tous les événements SSE (dont hyphen_pairs_reconciled)

4. Test avec MockProvider :
   - Créer un MockProvider qui retourne des corrections fixes
   - test_orchestrator_full_run_with_hyphens :
     charger sample.xml, run_job avec mock
     → vérifier output_dir contient *_corrected.xml avec HYP/SUBS_* corrects
     → vérifier invariants TextLine (IDs, coords)
     → vérifier que les paires de césure sont réconciliées

5. pytest → 100% vert
6. Committe : "feat: orchestrator + job store"
```

**Critères d'acceptation :**
- reconcile_hyphen_pair est appelé pour chaque paire dans chaque chunk
- resolved_subs_content stocké sur les LineManifest avant rewrite
- Le fichier de sortie est un XML valide avec HYP/SUBS_* corrects sur les paires

---

### SPRINT 7 — Routes API FastAPI

**Objectif :** l'API backend est complète et testable.

**Tâches :**
```
1. Implémente app/api/providers.py :
   POST /api/providers/models → ListModelsResponse
   HTTPException 400 si erreur provider

2. Implémente app/api/jobs.py :
   POST /api/jobs (multipart) :
   - Accepter files[], provider, api_key, model
   - Valider extensions
   - init_job_dirs + save_uploaded_files
   - build_document_manifest
   - Lancer run_job en background (asyncio.create_task)
   - Retourner {job_id}

   GET /api/jobs/{job_id} → JobStatusResponse
   GET /api/jobs/{job_id}/events → SSE (sse-starlette)
   GET /api/jobs/{job_id}/download → XML ou ZIP

3. Implémente app/main.py :
   - FastAPI avec lifespan
   - CORS depuis env CORS_ORIGINS
   - Inclure les deux routers
   - Si ./static existe → StaticFiles sur "/" pour HF Spaces
   - Catch-all → index.html pour le SPA

4. Test manuel :
   uvicorn app.main:app --reload
   curl -X POST http://localhost:8000/api/providers/models \
     -H "Content-Type: application/json" \
     -d '{"provider":"anthropic","api_key":"test"}'
   → doit retourner 400 clair (pas de crash)

5. Committe : "feat: fastapi routes"
```

**Critères d'acceptation :**
- Tous les endpoints répondent
- SSE envoie des keepalives toutes les 30s
- L'événement `document_parsed` inclut `hyphen_pairs` (nb de paires détectées)

---

### SPRINT 8 — Frontend React

**Objectif :** interface complète fonctionnelle connectée au backend.

**Design :** aesthetic archival/industriel — fond ardoise sombre (`slate-900`), accents ambre (`amber-500`), typographie monospace pour les données, serif pour les titres. Écran unique.

**Tâches :**
```
1. Configure Vite + Tailwind :
   vite.config.ts : proxy /api → http://localhost:8000
   Thème custom ambre/ardoise

2. Implémente src/types/index.ts :
   Provider, ModelInfo, JobStatus, HyphenRole (optionnel, pour les stats)
   SSEEvent (union discriminée par event)
   LogEntry : {id, type: 'info'|'warning'|'error'|'success', message, timestamp}

3. Implémente src/api/client.ts :
   listModels, createJob, getJob, downloadJob

4. Implémente src/hooks/useJobStream.ts :
   - EventSource sur /api/jobs/{jobId}/events
   - Accumuler LogEntry
   - Extraire progress : pages, lignes, hyphen_pairs_reconciled
   - Retourner {logs, progress, status, cleanup}

5. Implémente src/hooks/useModels.ts

6. Implémente les composants :
   - FileUpload.tsx : drag & drop, liste ordonnée
   - ProviderSelector.tsx : dropdown fixe 4 fournisseurs
   - ModelSelector.tsx : dropdown dynamique + bouton "Charger les modèles"
   - ApiKeyInput.tsx : password + toggle afficher/masquer
   - JobProgress.tsx : barre + compteur lignes + compteur "paires réconciliées : N"
   - LogPanel.tsx : scrollable auto, couleur par type
   - DownloadButton.tsx : déclenche téléchargement

7. Implémente src/App.tsx :
   - Orchestrer tous les composants
   - Play disabled si config incomplète
   - Une fois completed : afficher stats (lignes modifiées, paires réconciliées, durée)
   - Bouton "Nouvelle correction" pour reset l'état

8. Vérifie :
   - Upload → liste affichée
   - "Charger les modèles" → dropdown se remplit
   - Play enabled/disabled correct
   - Logs SSE en temps réel
   - Stats de paires césure visibles dans les résultats

9. Committe : "feat: frontend react"
```

**Critères d'acceptation :**
- Interface utilisable sans erreur console
- Stats de réconciliation visibles dans les résultats
- Clé API ne fuite pas

---

### SPRINT 9 — Docker + HF Spaces

**Objectif :** l'application tourne en local via docker-compose ET sur HF Spaces.

**Tâches :**
```
1. Crée backend/Dockerfile :
   FROM python:3.11-slim
   WORKDIR /app
   COPY requirements.txt .
   RUN pip install --no-cache-dir -r requirements.txt
   COPY app/ ./app/
   EXPOSE 8000
   CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

2. Crée frontend/Dockerfile :
   FROM node:20-alpine AS builder / npm ci / npm run build
   FROM nginx:alpine + dist + nginx.conf (proxy /api → backend:8000)
   EXPOSE 5173

3. Crée docker-compose.yml :
   backend: build ./backend, port 8000, env_file .env
   frontend: build ./frontend, port 5173:80, depends_on backend

4. Crée Dockerfile RACINE pour HF Spaces :
   Stage 1 : node:20-alpine → npm ci + npm run build → /frontend/dist
   Stage 2 : python:3.11-slim → pip install + COPY app/ + COPY dist → ./static/
   ENV JOB_STORAGE_DIR=/tmp/app-jobs
   EXPOSE 7860
   CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7860"]

5. Crée .env.example avec toutes les variables

6. Test local : docker compose up --build → http://localhost:5173

7. Crée README.md :
   - Description du projet
   - Installation locale (docker-compose)
   - Déploiement HF Spaces
   - Variables d'environnement
   - Note sur la gestion des césures (Hyphenation Reconciler)

8. Committe : "feat: docker + hf spaces"
```

**Critères d'acceptation :**
- `docker compose up --build` → app sur localhost:5173
- Dockerfile racine build sans erreur
- Port 7860 pour HF Spaces

---

### SPRINT 10 — Tests d'intégration + polish

**Objectif :** couverture complète, fichier exemple vérifié avec césures, polish UI.

**Tâches :**
```
1. Complète tests/test_integration.py :
   - test_upload_single_xml (mock provider)
   - test_upload_zip
   - test_sse_events_order
   - test_download_single_xml_valid
   - test_download_multi_zip
   - test_fallback_invalid_json → retry → downgrade
   - test_output_preserves_textline_invariants :
     mêmes IDs, mêmes HPOS/VPOS/WIDTH/HEIGHT
   - test_hyphen_pairs_reconciled_in_output :
     sur sample.xml, vérifier que la paire explicite produit
     un HYP + SUBS_TYPE="HypPart1"/"HypPart2" + SUBS_CONTENT dans l'ALTO de sortie
   - test_hyphen_heuristic_no_subs_content :
     sur le candidat heuristique, vérifier que SUBS_CONTENT absent

2. Vérifie examples/sample.xml :
   - ALTO v3 valide
   - ≥ 2 pages, ≥ 10 lignes
   - Erreurs OCR manifestes corrigeables
   - 1 paire avec SUBS_TYPE explicite (source_explicit=True)
   - 1 ligne avec tiret heuristique sans SUBS_TYPE (source_explicit=False)

3. Lance la suite complète :
   pytest -v tests/ --tb=short → 100% vert

4. Polish frontend :
   - Stats résultats : lignes modifiées / paires réconciliées / durée
   - Bouton "Nouvelle correction"
   - Badge discret sur chaque fichier de la liste : "N paires césure détectées"

5. Committe final : "chore: integration tests + polish"
6. Tag : git tag v1.0.0
```

**Critères d'acceptation finaux (checklist complète) :**

- [ ] Upload XML unique → correction → download ALTO valide
- [ ] Upload plusieurs XML → traitement comme multi-pages
- [ ] Upload ZIP → extraction → correction
- [ ] Chaque fournisseur liste ses modèles réels
- [ ] Play activé uniquement si config complète
- [ ] SSE logs en temps réel
- [ ] L'ALTO de sortie conserve : même nb TextLine, mêmes IDs, mêmes coords
- [ ] Aucune ligne de sortie ne contient de saut de ligne
- [ ] Les paires de césure explicites produisent HYP + SUBS_TYPE + SUBS_CONTENT
- [ ] Les césures heuristiques ne produisent pas de SUBS_CONTENT inventé
- [ ] Aucune paire de césure n'est jamais séparée en deux chunks
- [ ] `reconcile_hyphen_pair` ne fusionne jamais deux lignes physiques
- [ ] Si partie échoue → job continue avec fallback
- [ ] Résultat toujours un ALTO valide
- [ ] docker-compose up fonctionne
- [ ] Dockerfile racine HF Spaces fonctionne (port 7860)
- [ ] Clé API jamais loguée
- [ ] pytest 100% vert

---

## ANNEXE — Commandes de référence pour Claude Code

### Lancer une session type

```bash
cd alto-llm-corrector
claude
/clear
"Lis SPECS.md et TODO.md. On travaille sur le Sprint X aujourd'hui."
```

### Vérifier l'état entre sessions

```bash
git log --oneline -10
pytest tests/ -v
uvicorn app.main:app --reload
```

### Fichier TODO.md à maintenir

```markdown
# TODO

## En cours : Sprint X — Nom

### Fait
- [x] Tâche complète

### À faire
- [ ] Prochaine tâche

## Bugs connus
- Description → à traiter en Sprint Y
```

### Prompt de démarrage de sprint type

```
Je travaille sur alto-llm-corrector.
Les specs complètes sont dans SPECS.md.

Aujourd'hui : Sprint 2 — Hyphenation Reconciler.

Avant de coder :
1. Lis SPECS.md section "Hyphenation Reconciler"
2. Lis les fichiers existants : app/schemas/__init__.py, app/alto/parser.py
3. Explique comment tu vas implémenter hyphenation.py et ses 3 fonctions
4. Insiste sur la règle centrale : l'app orchestre, le LLM informe seulement
5. Une fois validé, implémente avec les tests
6. Lance pytest, corrige si nécessaire
7. Committe "feat: hyphenation reconciler"
```

### Décisions d'architecture clés à retenir

1. **Le LLM informe, l'app décide** : pour les césures, le LLM reçoit des métadonnées de contexte mais ne reconstruit jamais les frontières physiques lui-même.

2. **Trois niveaux de confiance pour SUBS_CONTENT** :
   - Source ALTO explicite (SUBS_TYPE + SUBS_CONTENT dans l'original) → confiance totale → écrire
   - Source ALTO partielle (HYP présent, SUBS_CONTENT absent) → confiance partielle → écrire si reconciler confirme
   - Heuristique (tiret terminal, sans marquage SUBS) → pas de confiance → ne pas écrire

3. **Les paires sont atomiques** : du parsing au chunk planner à l'orchestrateur au rewriter, une paire PART1/PART2 est toujours traitée ensemble. Jamais séparée.

4. **En cas de doute, conserver la source** : à chaque étape (reconciler, validator, orchestrator), le fallback ultime est de garder les textes OCR source. Un ALTO source intact vaut mieux qu'un ALTO inventé.
