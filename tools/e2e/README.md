# Harnais E2E local (mock vendeur)

Teste le **vrai backend** de bout en bout (upload → pipeline → SSE →
download) contre un mock vendeur local parlant le dialecte API Mistral.
Aucune clé, aucun réseau externe. Prouvé le 2026-07-13 : corrections
honnêtes appliquées, invariants géométrie/césure intacts, et le mock
*saboteur* (fusion de césure, absorption de ligne, ligne vidée) est
intégralement intercepté par les gardes (7 retries, 2 fallbacks,
`completed_with_fallbacks`, lignes sabotées revenues à l'OCR).

**Vague 0 exécutée** : le gate permanent vit dans
`backend/tests/e2e/` (scénarios pytest, marqueur `e2e`) et tourne en CI
dans le job `backend-e2e` (`pytest tests/e2e` depuis `backend/`). Les
scripts de ce dossier restent pour l'exploration manuelle :

```bash
# 1. Mock vendeur (honnête) — ou mock_vendor_sabotage:app pour l'adversarial
python3 -m uvicorn mock_vendor:app --host 127.0.0.1 --port 9611 &

# 2. Vrai backend, URL Mistral patchée vers le mock (runtime, dépôt intact)
python3 run_backend.py &   # écoute sur 127.0.0.1:8642

# 3. Job E2E
curl -sS -X POST http://127.0.0.1:8642/api/jobs \
  -F "files=@examples/sample.xml" -F "provider=mistral" \
  -F "model=mock-mistral-small" -F "api_key=dummy"
# → {job_id, job_token} ; ensuite :
#   GET /api/jobs/{id}/events?token=...   (SSE)
#   GET /api/jobs/{id}/download?token=... (XML corrigé)
```

Scénarios du saboteur (`mock_vendor_sabotage.py`), sur `examples/sample.xml` :
TL4 = paire de césure fusionnée, TL7 = absorption de la ligne suivante,
TL10 = ligne vidée. Attendu : aucune de ces corruptions dans la sortie,
fallback OCR tracé dans `trace.json`.
