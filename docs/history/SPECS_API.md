# SPECS_API — Routes FastAPI, SSE events, Stockage

Fichiers cibles : `backend/app/api/`, `backend/app/storage/`

---

## Routes FastAPI (`api/`)

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

---

## SSE Events

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

## Stockage (`storage/__init__.py`)

```
/tmp/app-jobs/{job_id}/
  input/          ← fichiers uploadés (XML extraits)
  outputs/        ← fichiers ALTO corrigés (*_corrected.xml)
```

- Accepter `.xml`, `.alto.xml`, `.zip`
- Si ZIP : extraire tous les XML, flatten les chemins (basename seulement)
- Multi-fichiers : document multi-pages, ordre = ordre d'upload

---

## Tests obligatoires

### `test_integration.py`
- Upload XML simple → job → download ALTO valide
- Upload ZIP → extraction → job
- Document avec paires de césure → ALTO de sortie avec HYP/SUBS_* corrects
- Fallback JSON invalide → retry → downgrade
