# SPECS_INFRA — Docker, HF Spaces, Sécurité

---

## Déploiement Hugging Face Spaces (`Dockerfile` racine)

- Base : `python:3.11-slim`
- Build frontend React (`npm run build`) dans `/app/static`
- FastAPI sert `/app/static` comme `StaticFiles` sur `/`
- **Port obligatoire : 7860**
- `CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7860"]`

---

## Développement local (`docker-compose.yml`)

- `backend` : port 8000
- `frontend` : port 5173 avec proxy vers backend

---

## Sécurité

- Ne jamais logger la clé API
- Ne jamais écrire la clé API sur disque
- Ne jamais renvoyer la clé au frontend
- Whitelist extensions uploadées : `.xml`, `.alto`, `.zip`
- Nettoyer les fichiers temporaires après téléchargement
