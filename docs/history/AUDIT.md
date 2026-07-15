# Audit complet — alto-llm-corrector

**Date:** 2026-04-13
**68 problèmes identifiés** : 8 CRITICAL, 17 HIGH, 27 MEDIUM, 16 LOW

---

## 1. SECURITE (CRITICAL / HIGH)

### 1.1 Vulnérabilité XXE (XML External Entity) — CRITICAL

**Fichier:** `backend/app/alto/parser.py:267`

```python
tree = etree.parse(str(xml_path))  # Aucune protection XXE
```

Les fichiers ALTO XML sont uploadés par l'utilisateur et parsés sans désactiver la résolution d'entités externes. Un attaquant peut injecter un DOCTYPE malveillant pour lire des fichiers locaux (`/etc/passwd`), déclencher une attaque "billion laughs" (DoS), ou faire du SSRF.

**Fix:** `parser = etree.XMLParser(resolve_entities=False, no_network=True); tree = etree.parse(str(xml_path), parser)`

---

### 1.2 Path Traversal dans l'API images — HIGH

**Fichier:** `backend/app/api/jobs.py:387-402`

```python
if "/" in image_name or "\\" in image_name or image_name.startswith("."):
    raise HTTPException(status_code=400, detail="Invalid image name.")
img_path = images_dir(job_id) / image_name
return Response(content=img_path.read_bytes(), media_type=mime)
```

La validation est insuffisante : pas de `.resolve()` ni `.is_relative_to()`. Les symlinks ne sont pas vérifiés. Un fichier symlink avec un nom anodin pointant vers `/etc/passwd` permet une lecture arbitraire du filesystem.

---

### 1.3 ZIP Bomb sans limite de taille — HIGH

**Fichier:** `backend/app/storage/__init__.py:67-90`

Aucune vérification de taille sur les fichiers extraits du ZIP. Un ZIP de 1 Ko peut se décompresser en plusieurs Go (zip bomb), épuisant le disque.

---

### 1.4 CORS grand ouvert — HIGH

**Fichier:** `backend/app/main.py:47-52`

```python
allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
```

Tout domaine peut faire des requêtes cross-origin vers l'API. La variable `CORS_ORIGINS` dans `.env.example` n'est **jamais lue** par le code — c'est du code mort.

---

### 1.5 Credentials exposées dans les logs CI/CD — CRITICAL

**Fichier:** `.github/workflows/hf-sync.yml:18-19`

```yaml
git remote add hf https://$HF_USERNAME:$HF_TOKEN@huggingface.co/spaces/...
git push hf main --force
```

Le token HF est intégré dans l'URL git (visible dans les logs). Le `--force` push écrase l'historique sans approbation.

---

### 1.6 Clés API exposées dans les erreurs/logs — CRITICAL

**Fichier:** `backend/app/jobs/orchestrator.py:549`

Les clés API sont passées à travers tout le pipeline et peuvent apparaître dans `str(exc)` quand un provider HTTP échoue. Aucune sanitisation des credentials avant logging.

---

### 1.7 Headers de sécurité manquants — HIGH

**Fichier:** `frontend/nginx.conf` (fichier entier)

Aucun header de sécurité : pas de `X-Frame-Options`, `X-Content-Type-Options`, `Content-Security-Policy`, `Strict-Transport-Security`. Vulnérable au clickjacking, MIME sniffing, et XSS.

---

### 1.8 Pas de rate limiting — HIGH

**Fichier:** `backend/app/main.py` (global)

Aucune limitation de débit sur les endpoints. L'upload de fichiers, la création de jobs, et les appels modèles sont tous illimités. Vecteur de DoS trivial.

---

## 2. BUGS LOGIQUES (CRITICAL / HIGH)

### 2.1 Temperature toujours à 0.0 — CRITICAL

**Fichier:** `backend/app/jobs/orchestrator.py:107`

```python
temperature = 0.0 if (attempt > 1 or hyphen_violation) else 0.0
```

Les deux branches du ternaire retournent `0.0`. Le mécanisme de retry avec température variable est **complètement inopérant**. La diversité des réponses LLM est perdue.

---

### 2.2 Forward SUBS perdus sur les lignes BOTH avec un seul String — CRITICAL

**Fichier:** `backend/app/alto/rewriter.py:271-276`

```python
if manifest.hyphen_role == HyphenRole.BOTH:
    strings = _get_string_children(el, ns)
    if strings and len(strings) > 1:  # <-- BUG: devrait être >= 1
        last = strings[-1]
        fw_type, fw_content = _desired_forward_subs(manifest)
```

La condition `len(strings) > 1` empêche l'application du forward SUBS quand la ligne n'a qu'un seul mot. Résultat : les attributs `SUBS_TYPE`/`SUBS_CONTENT` sont silencieusement perdus dans le XML de sortie.

---

### 2.3 Perte de subs_content en mode heuristique — CRITICAL

**Fichier:** `backend/app/alto/hyphenation.py:269-275`

```python
return corrected_part1, corrected_part2, None  # subs_content = None
```

Quand l'hyphénation est détectée heuristiquement (pas de markers SUBS_TYPE explicites), le reconciler retourne `None` pour `subs_content` même si une valeur inférée existait. Le mot complet reconstruit est perdu.

---

### 2.4 Extension de fenêtre non bornée dans le chunk planner — CRITICAL

**Fichier:** `backend/app/jobs/chunk_planner.py:205-211`

```python
while end < n:
    if should_stay_in_same_chunk(last_in_window, next_line):
        end += 1  # AUCUNE BORNE
    else:
        break
```

Une chaîne de lignes hyphenées peut étendre le chunk sans limite, dépassant `max_lines_per_request` et le budget tokens du LLM.

---

### 2.5 Paires hyphenées cross-page jamais réconciliées — CRITICAL

**Fichier:** `backend/app/jobs/orchestrator.py:220-288`

```python
part2 = line_by_id.get(part2_id)  # line_by_id est scopé par page
if part2 is None:
    continue  # Paire silencieusement ignorée
```

Si PART1 est en page N et PART2 en page N+1, la paire n'est jamais réconciliée. Les deux lignes sont traitées indépendamment, brisant le mot hyphenné.

---

### 2.6 Accès tableau non vérifié dans tous les providers — HIGH

**Fichiers:** `openai_provider.py:88`, `anthropic_provider.py:93`, `google_provider.py:106`, `mistral_provider.py:85`

```python
content = data["choices"][0]["message"]["content"]      # OpenAI
text = data["candidates"][0]["content"]["parts"][0]["text"]  # Google (3 niveaux!)
```

Si l'API retourne un tableau vide, `IndexError` non attrapé. Aucune validation de la structure JSON avant accès.

---

### 2.7 Fast-path ne nettoie pas les attributs SUBS périmés — HIGH

**Fichier:** `backend/app/alto/rewriter.py:283-300`

`_update_content_in_place()` ne met à jour que `CONTENT` sans toucher `SUBS_TYPE`/`SUBS_CONTENT`. Si la réconciliation a neutralisé le subs_content, les anciens attributs persistent dans le XML de sortie.

---

## 3. CONCURRENCE & FUITES MEMOIRE

### 3.1 Job Store sans éviction — CRITICAL

**Fichier:** `backend/app/jobs/store.py:13-14`

```python
self._jobs: dict[str, JobManifest] = {}
self._subscribers: dict[str, list[asyncio.Queue]] = {}
```

Tous les jobs terminés restent en mémoire indéfiniment. Pas de TTL, pas d'éviction. Avec les `line_traces` (des Mo par job), la mémoire croît sans limite.

---

### 3.2 Queues SSE zombies — HIGH

**Fichier:** `backend/app/jobs/store.py:50-51`

```python
except asyncio.QueueFull:
    pass  # slow consumer — drop
```

Les événements sont silencieusement perdus. Si un client se déconnecte sans `unsubscribe()`, la queue reste allouée pour toujours.

---

### 3.3 Accumulation mémoire des traces — HIGH

**Fichier:** `backend/app/jobs/orchestrator.py:399-407`

Un `LineTrace` par ligne du document stocké en mémoire. Pour 100k+ lignes, centaines de Mo sans flush intermédiaire.

---

### 3.4 Race condition TOCTOU sur les fichiers — MEDIUM

**Fichier:** `backend/app/api/jobs.py:398-402`

`img_path.exists()` puis `img_path.read_bytes()` — le fichier peut disparaître entre les deux appels.

---

### 3.5 Race condition get/update sur le job store — MEDIUM

**Fichier:** `backend/app/jobs/orchestrator.py:183, 195, 214`

`getattr(job_store.get_job(job_id), "retries", 0)` — le job peut être supprimé entre get et update.

---

## 4. GESTION D'ERREURS

### 4.1 Exception avalée silencieusement — MEDIUM

**Fichier:** `backend/app/storage/__init__.py:159-160`

```python
except Exception:
    pass
```

Toutes les erreurs de parsing ALTO sont ignorées sans log.

---

### 4.2 Exceptions trop larges converties en 400 — MEDIUM

**Fichier:** `backend/app/api/jobs.py:88-91`, `backend/app/api/providers.py:20-26`

Un OOM est signalé comme erreur client (400) au lieu d'erreur serveur (500). Fuite potentielle de détails internes.

---

### 4.3 Échecs partiels de chunks non rollbackés — MEDIUM

**Fichier:** `backend/app/jobs/orchestrator.py:442-455`

Si un chunk raise une exception après traitement partiel, les `LineManifest` restent incohérents. La page est marquée COMPLETED malgré les échecs.

---

### 4.4 Pas de retry HTTP dans les providers — HIGH

**Fichiers:** Tous les providers

`resp.raise_for_status()` échoue immédiatement. Pas de retry avec backoff pour les erreurs 5xx ou les timeouts.

---

## 5. PROBLEMES FRONTEND

### 5.1 Null reference sur pages vides — HIGH

**Fichiers:** `frontend/src/components/DiffViewer.tsx:145`, `LayoutViewer.tsx:217-218`

```typescript
const currentPage = data.pages[pageIdx] ?? data.pages[0]
// Si data.pages est vide → undefined → crash sur currentPage.lines
```

---

### 5.2 Aucun AbortController / timeout sur les fetch — HIGH

**Fichier:** `frontend/src/api/client.ts` (tous les appels fetch)

Tous les `fetch()` n'ont ni timeout ni mécanisme d'annulation.

---

### 5.3 Erreurs API silencieusement avalées — HIGH

**Fichier:** `frontend/src/App.tsx:57-73`

```typescript
fetchDiff(jobId).then(setDiffData).catch(() => { /* non-critical */ })
```

Les erreurs sont ignorées. L'utilisateur ne sait jamais si le chargement a échoué.

---

### 5.4 Type assertions masquant des erreurs runtime — MEDIUM

**Fichier:** `frontend/src/api/client.ts:63, 76, 89`

```typescript
return resp.json() as Promise<LayoutData>
```

Aucune validation runtime que le JSON correspond au type attendu.

---

### 5.5 Index comme key React — MEDIUM

**Fichier:** `frontend/src/components/DiffViewer.tsx:120, 126`

```typescript
<TokenSpan key={idx} token={t} />
```

Anti-pattern : artefacts visuels possibles quand la liste de tokens change.

---

### 5.6 EventSource pas correctement nettoyé — MEDIUM

**Fichier:** `frontend/src/hooks/useJobStream.ts:170`

`addEventListener` sans `removeEventListener` dans le cleanup.

---

### 5.7 Pas d'état d'erreur pour les chargements — MEDIUM

**Fichier:** `frontend/src/App.tsx:32-35`

Aucun `diffError` / `layoutError` correspondant aux states de loading. Si le fetch échoue, le spinner tourne indéfiniment.

---

### 5.8 Regex fragile pour parser les stats — MEDIUM

**Fichier:** `frontend/src/App.tsx:149`

```typescript
const m = completedLog.message.match(/(\d+) line\(s\) modified.*?(\d+) hyphen.*?([\d.]+)s/)
```

Dépend du format exact du message backend. Aucune vérification de null avant `m[1]`.

---

### 5.9 Accessibilité manquante — LOW

Pas d'`aria-label` sur les boutons toggle, selects, et le bouton download.

---

## 6. PIPELINE — EDGE CASES LOGIQUES

### 6.1 Pas de normalisation Unicode dans le validateur — MEDIUM

**Fichier:** `backend/app/jobs/validator.py:165`

```python
if part1_last_word.lower() == subs_content.lower():
```

Comparaison sans `unicodedata.normalize()`. NFC vs NFD bypass la détection de fusion.

---

### 6.2 Caractères zero-width non détectés — MEDIUM

**Fichier:** `backend/app/jobs/validator.py:80-81`

Un texte composé uniquement de `\u200b` passe la validation mais est fonctionnellement vide.

---

### 6.3 _detect_namespace() crash sur XML malformé — MEDIUM

**Fichiers:** `backend/app/alto/parser.py:29-34`, `backend/app/alto/rewriter.py:38-42`

```python
return tag[1: tag.index("}")]  # ValueError si "}" absent
```

---

### 6.4 Layout manquant = document vide sans erreur — MEDIUM

**Fichier:** `backend/app/alto/parser.py:273-276`

Un ALTO invalide (sans Layout) produit un manifest vide. Aucune erreur, le job "réussit" avec 0 corrections.

---

### 6.5 Collision d'IDs sur fichiers sans IDs explicites — MEDIUM

**Fichier:** `backend/app/alto/parser.py:279, 291, 302`

Les IDs générés (`PAGE_0`, `TB_PAGE_0_0`) peuvent collisionner si plusieurs fichiers ALTO sans IDs sont traités dans le même job.

---

### 6.6 Seuils magiques non documentés — LOW

**Fichier:** `backend/app/jobs/line_acceptance.py:38, 42`

`MIN_SOURCE_SIMILARITY = 0.35`, `NEIGHBOUR_MARGIN = 0.15` — seuils arbitraires sans justification. Le seuil de 35% est très permissif.

---

### 6.7 Vérification de drift sautée si OCR manquant — MEDIUM

**Fichier:** `backend/app/jobs/validator.py:149-153`

Si un des textes OCR est absent, toute la vérification de migration est silencieusement sautée.

---

### 6.8 Backward réconciliation des lignes BOTH non traitée — MEDIUM

**Fichier:** `backend/app/jobs/orchestrator.py:220-288`

Les lignes BOTH ne passent pas par `reconcile_hyphen_pair()` pour leur côté backward.

---

### 6.9 Stale forward_subs_content dans l'enrichissement BOTH — HIGH

**Fichier:** `backend/app/alto/hyphenation.py:71-72`

Pour les lignes BOTH, `hyphen_forward_subs_content` est lu directement du manifest sans vérifier s'il a été mis à jour depuis le linking. Le LLM reçoit un candidat de jointure obsolète.

---

### 6.10 Boundary check ne gère pas la normalisation Unicode — MEDIUM

**Fichier:** `backend/app/alto/hyphenation.py:176-179`

`_part2_boundary_word_diverged()` compare avec `.lower()` mais pas `normalize()`. Faux positifs sur les corrections avec accents décomposés.

---

## 7. DOCKER & DEPLOIEMENT

### 7.1 Containers exécutés en root — HIGH

**Fichiers:** Tous les Dockerfiles. Aucune directive `USER`.

---

### 7.2 Images de base non pinées au digest — HIGH

**Fichiers:** Tous les Dockerfiles. `FROM python:3.11-slim` sans `@sha256:...`.

---

### 7.3 Pas de healthcheck Docker — MEDIUM

**Fichier:** `docker-compose.yml`. Aucun `healthcheck` sur les services.

---

### 7.4 Pas de limites de ressources — HIGH

**Fichier:** `docker-compose.yml`. Aucune contrainte CPU/mémoire.

---

### 7.5 Dépendances Python non pinées — HIGH

**Fichier:** `backend/requirements.txt`

6 packages sur 10 n'ont aucune version. Pas de lock file.

---

### 7.6 `actions/checkout@v3` obsolète — MEDIUM

**Fichier:** `.github/workflows/hf-sync.yml:9`

---

### 7.7 Stockage jobs dans /tmp sans nettoyage — MEDIUM

`JOB_STORAGE_DIR=/tmp/app-jobs` — effacé au reboot, aucun garbage collection.

---

## 8. COUVERTURE DE TESTS

### 8.1 Modules sans tests directs

| Module | Test dédié |
|--------|-----------|
| `storage/__init__.py` | Aucun (indirect seulement) |
| `jobs/store.py` | Aucun (queue overflow jamais testé) |
| `providers/base.py` | Aucun |
| `main.py` | Aucun (CORS, SPA fallback non testés) |
| `schemas/__init__.py` | Aucun |

### 8.2 Edge cases non testés

- XML malformé (attributs manquants, coordonnées négatives, TextLine sans String)
- Caractères Unicode exotiques (zero-width, RTL marks, control chars)
- Réponses LLM géantes (>100Ko par ligne)
- Jobs concurrents (10 jobs simultanés)
- Déconnexion client pendant le streaming SSE
- ZIP contenant des millions de petits fichiers
- `cleanup_job()` jamais appelé dans aucun test

### 8.3 Tests fragiles

- 15+ tests sautés silencieusement si `examples/*.xml` absents
- Tests corpus avec comptages hardcodés (`assert total == 566`)
- `MockProvider` retourne toujours le texte OCR inchangé

---

## Synthèse par priorité d'action

| Priorité | Action | Issues |
|----------|--------|--------|
| **P0** | Fix XXE, path traversal, zip bomb | 1.1, 1.2, 1.3 |
| **P0** | Fix temperature bug | 2.1 |
| **P0** | Ajouter éviction au job store | 3.1 |
| **P1** | Sanitiser les API keys dans les logs | 1.6 |
| **P1** | Borner l'extension de fenêtre chunk_planner | 2.4 |
| **P1** | Fix forward SUBS lignes BOTH (`> 1` → `>= 1`) | 2.2 |
| **P1** | Retry HTTP avec backoff dans les providers | 4.4 |
| **P1** | Piner les dépendances Python + lock file | 7.5 |
| **P2** | CORS configurable, headers sécurité nginx | 1.4, 1.7 |
| **P2** | AbortController + timeouts frontend | 5.2 |
| **P2** | Null checks pages vides | 5.1 |
| **P2** | USER non-root dans Dockerfiles | 7.1 |
| **P3** | Normalisation Unicode validateur | 6.1 |
| **P3** | Tests storage, store, edge cases XML | 8.1, 8.2 |
| **P3** | Rate limiting, healthchecks, resource limits | 1.8, 7.3, 7.4 |
