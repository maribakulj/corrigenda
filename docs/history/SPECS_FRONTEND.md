# SPECS_FRONTEND — Interface React

Fichiers cibles : `frontend/src/`

---

## Écran unique

1. **Header** — titre + sous-titre
2. **Upload** — drag & drop, liste ordonnée des fichiers + nb de paires de césure détectées
3. **Configuration** — sélecteur fournisseur + clé API masquée + bouton "Charger les modèles" + sélecteur modèle
4. **Contrôles** — bouton Play (disabled si config incomplète)
5. **Progression** — barre globale + compteur pages/lignes/paires césure réconciliées
6. **Logs** — panel scrollable SSE en temps réel, code couleur par type
7. **Résultats** — bouton télécharger + stats (lignes modifiées, paires réconciliées, durée)

---

## Règles UX

- Play activé uniquement si : fichier(s) + fournisseur + clé API + modèle
- Clé API jamais loguée, jamais renvoyée au frontend

---

## Composants (`components/`)

| Composant | Rôle |
|-----------|------|
| `FileUpload.tsx` | Drag & drop, liste fichiers, nb paires césure |
| `ProviderSelector.tsx` | Sélecteur OpenAI / Anthropic / Mistral / Google |
| `ApiKeyInput.tsx` | Saisie clé masquée |
| `ModelSelector.tsx` | Liste modèles chargés depuis l'API |
| `JobProgress.tsx` | Barre progression globale + compteurs |
| `LogPanel.tsx` | Flux SSE scrollable, code couleur par type d'événement |
| `DownloadButton.tsx` | Téléchargement XML/ZIP + stats finales |

---

## Hooks (`hooks/`)

- `useJobStream.ts` — consomme le flux SSE `GET /api/jobs/{job_id}/events`
- `useModels.ts` — appelle `POST /api/providers/models` pour charger la liste

---

## Client API (`api/client.ts`)

Encapsule tous les appels HTTP vers le backend FastAPI.
