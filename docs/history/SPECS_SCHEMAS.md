# SPECS_SCHEMAS — Modèles Pydantic

Fichier cible : `backend/app/schemas/__init__.py`

---

## Enums

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

---

## Coords

```python
class Coords(BaseModel):
    hpos: int; vpos: int; width: int; height: int
```

---

## LineManifest

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

---

## Autres modèles

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

---

## Payload LLM enrichi

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
