# ISSUE_LEDGER — Senior Review (2026-05-23)

Branche : `claude/dreamy-cray-kc47X`
Reviewer : Claude (Opus 4.7, 1M context)
Méthodologie : 5 passes (Inventory → File review → Cross-file → Issue graph → Action plan)

Note : un `AUDIT.md` antérieur (2026-04-13) existait avec 68 problèmes ; ~40 % ont
été corrigés depuis. Ce ledger ne re-référence un point de l'audit que s'il est
encore exploitable, et ajoute les problèmes nouveaux ou non couverts.

Légende :
- **B-NNN** : bug réel (comportement incorrect aujourd'hui)
- **R-NNN** : risque (vulnérabilité latente, fragilité)
- **D-NNN** : dette (architecture, lisibilité)
- **T-NNN** : test manquant
- **S-NNN** : style

Sévérité : 🔴 CRITICAL · 🟠 HIGH · 🟡 MEDIUM · 🟢 LOW

---

## BUGS RÉELS

### B-001 🔴 — Anthropic provider : structured output cassé
**Fichier :** `backend/app/providers/anthropic_provider.py:53-71`
**Symptôme :** Le payload contient `"output_config": {"format": {"type": "json_schema", ...}}`.
Ce champ n'existe **pas** dans l'API Messages d'Anthropic (https://api.anthropic.com/v1/messages).
L'API renvoie 400, le `call_llm` enclenche le `fallback_body` qui retire `output_config` →
on envoie une requête plain text sans contrainte de schéma → `json.loads(text)` échoue
souvent, le validateur lève `ValueError`, et `_run_chunk` itère 3× avec backoff.
**Impact :** Anthropic est de facto inutilisable en production. Chaque chunk
consomme 3 appels API payants avant fallback OCR.
**Fix :** utiliser le mécanisme officiel `tools` + `tool_choice` pour JSON schema, ou
basculer sur la nouvelle API `/v1/messages` avec `response_format` (vérifier la doc actuelle).

### B-002 🔴 — DiffRow viole les Rules of Hooks
**Fichier :** `frontend/src/components/DiffViewer.tsx:77-134`
**Symptôme :** `useMemo` est appelé à la ligne 102 **après** un `return` conditionnel
ligne 87-99 (`if (!isModified) return ...`). Le nombre de hooks varie entre renders
selon `line.modified`. Si une ligne passe de `modified=false` à `modified=true`
(ou inversement) entre deux renders, React lève
`"Rendered more hooks than during the previous render"`.
**Impact :** Crash du composant DiffViewer dès que l'utilisateur sélectionne une
ligne ou que les données diff sont rechargées avec un statut changé.
**Fix :** déplacer `useMemo` au-dessus du `if (!isModified)`, ou extraire la
branche modified dans un sous-composant.

### B-003 🟠 — useJobStream : reconnect leak + StrictMode double-increment
**Fichier :** `frontend/src/hooks/useJobStream.ts:178-211`
**Symptôme :**
1. Dans `es.onerror`, on `setStatus((s) => { ... retryCount++ ... setTimeout(...) ...; return s })`.
   Or, en `React.StrictMode` (actif dans `main.tsx:8`), les updaters sont invoqués 2× en dev →
   `retryCount` double-incrémente et `setTimeout` est appelé 2× → 2 nouveaux EventSource créés
   pour la même tentative.
2. Le `setTimeout` créé dans `onerror` n'est pas conservé ni annulé dans le cleanup.
   Si l'utilisateur démonte le composant pendant la fenêtre 2s/4s/6s, le timeout
   crée un `newEs`, qui se branche sur des setters d'un composant démonté
   (warning React, et leak réel de l'EventSource).
**Impact :** dev : doublement des connexions et logs. prod : leak EventSource si
unmount pendant retry.
**Fix :** sortir `retryCount` et `setTimeout` du setter (les déplacer dans un
`useRef` et un `timeoutRef`), `clearTimeout` dans le cleanup, et tracker `newEs`
dans `esRef`.

### B-004 🟠 — Pretty-print casse l'identité byte du XML inchangé
**Fichier :** `backend/app/alto/rewriter.py:594`
**Symptôme :** `etree.tostring(root, ..., pretty_print=True)` reformate
intégralement le XML, même quand `metrics.untouched == total_lines`.
Les espaces, indentations et retours à la ligne diffèrent du source.
**Impact :** Les utilisateurs qui comparent visuellement source/corrigé voient
un diff massif sur des fichiers où *rien n'a été corrigé*. Casse aussi tout
processus aval qui dépend de la stabilité byte (signatures, diff utilities).
**Fix :** `pretty_print=False`. Si l'esthétique est requise, le faire seulement
quand `metrics.total_processed > 0`.

### B-005 🟠 — Cross-page hyphen resolution silencieuse en cas de collision d'IDs
**Fichier :** `backend/app/jobs/orchestrator.py:418-425`
```python
if cross_page_partners:
    for partner_id, partner_lm in cross_page_partners.items():
        if partner_id not in line_by_id:    # ← collision masquée
            line_by_id[partner_id] = partner_lm
```
**Symptôme :** Si page N a une ligne `TL1` et page N+1 aussi (cas extrêmement
fréquent quand chaque ALTO est généré par scan), le partenaire cross-page
n'est jamais injecté (collision avec un local). Le reconciler appelé en
ligne 267-274 trouve le **mauvais** `TL1` (celui de la page courante), et
brise la paire silencieusement.
**Impact :** Sur un corpus multi-page sans IDs uniques, les paires césurées
cross-page sont mal réconciliées sans warning.
**Fix :** garantir l'unicité des IDs au parsing (préfixer par `source_file`),
ou clé composite `(page_id, line_id)` partout dans `line_by_id`.

### B-006 🟠 — Parser génère des IDs collisionnables entre fichiers sans ID
**Fichier :** `backend/app/alto/parser.py:280, 292, 303`
```python
page_id  = page_el.get("ID", f"PAGE_{page_index_offset + page_idx}")
block_id = tb.get("ID", f"TB_{page_id}_{block_order}")
line_id  = tl.get("ID", f"TL_{block_id}_{line_order_in_block}")
```
**Symptôme :** `page_index_offset` est cumulatif inter-fichiers donc PAGE_N
est unique. **Mais** un fichier ALTO peut déclarer `Page ID="Page1"` (très
courant). Deux fichiers contenant tous deux `Page1/TextBlock1/Line1`
produisent les mêmes line_id → collision dans `line_by_id` global.
**Impact :** Le storage layer (`link_alto_to_images`) gère déjà la collision
de Page/@ID (commentaire ligne 139-141), mais le parser ne suffixe pas
les IDs explicites. Cause directe de B-005.
**Fix :** appliquer un préfixe `{source_file}::` ou `{file_index}_` à TOUS
les IDs (explicites ou synthétisés), ou maintenir un sous-namespace par
source_file dans les structures de lookup.

### B-007 🟠 — `_detect_namespace` peut lever ValueError sur XML malformé
**Fichier :** `backend/app/alto/parser.py:29-34`, `backend/app/alto/rewriter.py:38-42`
```python
def _detect_namespace(root):
    tag = root.tag
    if tag.startswith("{"):
        return tag[1: tag.index("}")]   # ← ValueError si pas de "}"
    return ""
```
**Symptôme :** Si une racine a un tag malformé `"{noclosingbrace"`,
`str.index` lève `ValueError` non capturée. Remonte jusqu'à l'API
`POST /api/jobs` qui le transforme en `400 "Failed to parse files"`,
mais le message expose le détail technique.
**Impact :** Faible (XML lxml normalement bien formé), mais code fragile.
**Fix :** `if "}" not in tag: return ""` avant l'index.

### B-008 🟠 — `_run_chunk` : double comptage des retries pour `hyphen_violation`
**Fichier :** `backend/app/jobs/orchestrator.py:207-228`
**Symptôme :** Pour une violation hyphen :
1. ligne 215 : `increment_counter("retries")` puis `continue`
2. Au prochain tour, si l'erreur est encore une exception (autre que hyphen),
   ligne 227 : `increment_counter("retries")` à nouveau.
Pas grave fonctionnellement, mais `job.retries` ne reflète pas le nombre
réel de retries — il compte les tentatives extra.
**Impact :** statistique trompeuse dans `JobStatusResponse.retries`.
**Fix :** ne comptabiliser que le retry effectivement utilisé (déplacer
l'increment hors du `if is_hyphen_violation`).

### B-009 🟠 — `except (ValueError, Exception)` : redondance + masque les erreurs système
**Fichier :** `backend/app/jobs/orchestrator.py:199`
```python
except (ValueError, Exception) as exc:
```
**Symptôme :** `Exception` englobe déjà `ValueError`. Plus grave, ce catch-all
attrape `asyncio.CancelledError` (qui hérite de `BaseException` en 3.8+ donc
pas dans Exception — mais hérite de Exception en 3.7 et avant). En 3.11+
elle est sortie de Exception, donc OK ; mais il attrape aussi
`KeyboardInterrupt`'s parent ? Non — KI hérite de BaseException. Donc le
risque réel est : ne distingue plus erreurs HTTP, validation, JSON parsing,
etc. → toutes traitées avec la même backoff.
**Impact :** comportement uniforme face à des erreurs très différentes.
Une erreur permanente (clé invalide) est retryée 3× avec backoff,
gaspillant 6s + appels API.
**Fix :** catch ciblé `except (ValueError, httpx.HTTPError, json.JSONDecodeError)`
+ classification de l'erreur (permanente vs transitoire).

### B-010 🟡 — `_sanitize_error` : truncation **avant** sanitization
**Fichier :** `backend/app/jobs/orchestrator.py:703`
```python
safe_error = _sanitize_error(str(exc)[:500], api_key)
```
**Symptôme :** Si la clé API est positionnée entre les caractères 495-505 du
message d'erreur, la troncation la coupe en 2 et le regex ne la matche plus
→ la moitié de la clé fuit dans le log.
**Impact :** fuite partielle possible. Faible probabilité mais réelle.
**Fix :** sanitize d'abord, truncate ensuite : `_sanitize_error(str(exc), api_key)[:500]`.

### B-011 🟡 — Stale `forward_subs_content` dans l'enrichissement BOTH
**Fichier :** `backend/app/alto/hyphenation.py:71-72`
```python
backward_join_candidate=lm.hyphen_subs_content or None,
forward_join_candidate=lm.hyphen_forward_subs_content or None,
```
**Symptôme :** Pour une ligne BOTH, le `hyphen_forward_subs_content` est lu
directement du manifest. Or, après la Pass 1 du reconciler (orchestrator
ligne 285), `lm.hyphen_subs_content` peut avoir été mis à None
(neutralisé) ; mais c'est uniquement la backward subs, donc le forward
reste OK. ✅ Pas de bug ici en réalité — cas couvert. **Cet item est
un faux positif de l'audit antérieur (6.9). Marqué pour archivage.**

### B-012 🟡 — Job timeout : message d'erreur incorrect si `JOB_TIMEOUT_SECONDS=0`
**Fichier :** `backend/app/jobs/orchestrator.py:692-694`
**Symptôme :** Si `JOB_TIMEOUT_SECONDS=0`, le `timeout=None` n'enclenche jamais
`TimeoutError`. Mais le message d'erreur dans le `except` est figé à
`f"Job timed out after {_JOB_TIMEOUT_SECONDS}s"` = `"timed out after 0s"`.
**Impact :** Bug théorique : ne peut pas être déclenché. Mais le code est
trompeur. Faible priorité.
**Fix :** sortir le timeout dans une variable, message dépendant.

### B-013 🟡 — Validator : pas de normalisation Unicode (NFC/NFD)
**Fichier :** `backend/app/jobs/validator.py:165`, `backend/app/alto/hyphenation.py:176-179, 259`
```python
if part1_last_word.lower() == subs_content.lower():
if joined.lower() == effective_subs.lower():
```
**Symptôme :** "café" en NFC (1 codepoint `é`) ≠ "café" en NFD (`e` + combining
acute). Le `==` échoue. La comparaison de fusion et de divergence du
boundary word peuvent rater des cas, et inversement accepter des cas qu'elles
devraient rejeter.
**Impact :** Faux négatifs sur fusion detection ; faux positifs sur boundary
divergence pour les textes avec accents décomposés.
**Fix :** `unicodedata.normalize("NFC", x).lower()` partout, ou définir un
helper `_normalize(s)` centralisé.

### B-014 🟡 — `text_changed` ne tient pas compte de la normalisation
**Fichier :** `backend/app/alto/rewriter.py:151-152`
```python
def _line_text_unchanged(el, corrected, ns):
    return _extract_text_from_line(el, ns) == corrected
```
**Symptôme :** Le parser normalise en NFC (`parser.py:67`) mais le rewriter
`_extract_text_from_line` ne normalise pas. Un round-trip "untouched" pour
une ligne contenant des caractères composés peut être faussement détecté
comme `text_changed` → bascule en fast/slow path inutile.
**Impact :** mineur, mais gonfle les métriques de rewrite et peut casser
le test `test_unchanged_rewrite_metrics` sur corpus contenant des caractères NFD.
**Fix :** appliquer la même `unicodedata.normalize("NFC", ...)` dans
`_extract_text_from_line`, ou comparer après normalisation.

### B-015 🟡 — `_update_content_in_place` perd les caractères non-word
**Fichier :** `backend/app/alto/rewriter.py:283-300`
```python
words = [t for t in _tokenize(corrected) if t.strip()]
if len(words) != len(orig_strings):
    return False
for string_el, word in zip(orig_strings, words):
    string_el.set("CONTENT", word.replace("­", ""))
```
**Symptôme :** Le fast-path ne crée jamais d'éléments SP/HYP supplémentaires.
Si le corrected a le même nombre de mots mais avec une ponctuation différente
qui modifie l'espace (rare), le fast-path met juste à jour les CONTENT —
acceptable. Mais : il replace `­` seulement ici, pas dans le slow-path
de manière exhaustive. Légère asymétrie, voir B-019.

### B-016 🟡 — `_run_chunk` : si toutes les attempts échouent en hyphen_violation, fallback silencieux
**Fichier :** `backend/app/jobs/orchestrator.py:202-216`
**Symptôme :** Si `hyphen_violation=True`, on retry une seule fois (le `if not
hyphen_violation` empêche d'incrémenter à nouveau). Au prochain hyphen_violation,
on tombe dans le `if attempt < max_attempts` à 219 et retry comme erreur générale,
mais le contexte de hyphen est perdu. Au final tous les retries restants sont
considérés non-hyphen. Logique d'escalade peu claire.
**Impact :** Pas d'incorrect behavior mais difficile à diagnostiquer en
production.
**Fix :** documenter le state machine ou utiliser un compteur séparé.

### B-017 🟡 — Sample.xml utilise des coordonnées et SP sans HPOS → coordonnées générées potentiellement invalides
**Fichier :** `backend/app/alto/rewriter.py:347-355` (slow-path SP fallback)
**Symptôme :** Quand `sp_n >= len(orig_sp_attribs)`, le code génère un nouveau
SP avec `WIDTH/HPOS/VPOS`. Mais le `tok_hpos` calculé peut chevaucher les
coordonnées du String précédent si le tokenizer ajoute plus de tokens que
prévu. Pas de vérification de cohérence des coordonnées.
**Impact :** Coordonnées potentiellement chevauchantes en sortie. Visible
seulement à la visionneuse layout, pas dans le texte corrigé.
**Fix :** vérification min-max contre les voisins, ou cursor strict.

### B-018 🟢 — `useJobStream` : reset state quand `jobId === null` peut causer double effect
**Fichier :** `frontend/src/hooks/useJobStream.ts:44-51`
**Symptôme :** Au démontage `jobId=null` reset le state ET le `return` est
un cleanup pour le useEffect précédent. En StrictMode dev les effects sont
double-invoqués. Le reset peut écraser un state mis à jour entre les deux.
Cas edge.
**Fix :** vérifier avec `useEffect` cleanup pattern correct.

### B-019 🟢 — Asymétrie `­` stripping entre fast-path et slow-path
**Fichier :** `backend/app/alto/rewriter.py:299, 362, 367, 436, 441, 505, 510`
**Symptôme :** `.replace("­", "")` est répété dans 7 endroits. Si une
nouvelle path est ajoutée et oublie le strip, des soft-hyphens fuient.
**Impact :** dette / cohérence.
**Fix :** helper `_clean_content(s)` centralisé.

---

## RISQUES / VULNÉRABILITÉS LATENTES

### R-001 🔴 — HF_TOKEN dans URL git en clair (CI/CD)
**Fichier :** `.github/workflows/hf-sync.yml:18`
```yaml
git remote add hf https://$HF_USERNAME:$HF_TOKEN@huggingface.co/spaces/$HF_USERNAME/$HF_SPACE_NAME
```
**Symptôme :** Token intégré dans l'URL git. Affichage masqué par Actions mais :
- visible dans tout `set -x` involontaire
- visible dans tout debugger ou outil qui dumpe environnement git
- `git push hf main --force` écrase l'historique sans audit
**Fix :** utiliser `git -c credential.helper=... push` avec config éphémère,
ou un script auth via `git push https://oauth2:<token>@...` puis nettoyer
la config. Retirer le `--force` ou le confiner à une branche dédiée.

### R-002 🟠 — Pas de rate limiting
**Fichier :** `backend/app/main.py` (global)
**Symptôme :** Aucun middleware de throttling. `POST /api/jobs` accepte
des uploads illimités, `POST /api/providers/models` peut être martelé.
**Impact :** DoS trivial, surcoût LLM si la clé API d'un utilisateur fuit.
**Fix :** `slowapi` ou `limits` middleware avec quota par IP ou par job_id.

### R-003 🟠 — Pas de headers de sécurité nginx
**Fichier :** `frontend/nginx.conf`
**Symptôme :** Pas de CSP, X-Frame-Options, X-Content-Type-Options,
Referrer-Policy. Vulnérable à clickjacking, MIME-sniffing, XSS reflété.
**Fix :** ajouter
```nginx
add_header X-Frame-Options "DENY" always;
add_header X-Content-Type-Options "nosniff" always;
add_header Referrer-Policy "no-referrer" always;
add_header Content-Security-Policy "default-src 'self'; img-src 'self' data:; ..." always;
```

### R-004 🟠 — Job IDs UUIDv4 sans auth → download par devinette
**Fichier :** `backend/app/api/jobs.py:183-218`
**Symptôme :** `GET /api/jobs/{job_id}/download` ne vérifie aucune
authentification. Un attaquant qui devine un job_id (UUIDv4 → 122 bits
d'entropie, infaisable en force brute) ne peut rien faire, mais :
- En cas de fuite via referer header, log, screenshot → accès immédiat
au contenu corrigé (potentiellement PII).
- Si CORS est laissé à `*`, n'importe quel site peut déclencher un
download via XHR depuis l'origine d'un utilisateur connecté.
**Impact :** Faible probabilité mais haute gravité.
**Fix :** ajouter un token éphémère (signé) couplé au job_id, ou un
cookie de session.

### R-005 🟠 — API key acceptée en multipart Form
**Fichier :** `backend/app/api/jobs.py:48`
```python
api_key: str = Form(...)
```
**Symptôme :** Les clés API sont transmises dans le corps d'une requête
multipart. La plupart des reverse-proxies (nginx, ALB) logguent les
URL+headers mais pas le body. Mais :
- Tout middleware FastAPI tiers qui logge le body (request logging) la capture.
- Les caches HTTP intermédiaires peuvent stocker temporairement le body.
- Pas standard : les credentials doivent être en `Authorization` header
ou `X-API-Key`.
**Fix :** déplacer la clé dans un header dédié, ou la passer dans une session
chiffrée côté backend.

### R-006 🟠 — Trace exposée publiquement (PII)
**Fichier :** `backend/app/api/jobs.py:226-243`
**Symptôme :** `GET /api/jobs/{job_id}/trace` retourne tout le texte source,
toutes les corrections, et toutes les versions intermédiaires. Pour des
documents patrimoniaux c'est OK ; pour des documents privés, c'est une
fuite massive si la connaissance du job_id est compromise (cf. R-004).
**Fix :** lié à R-004. Auth + redaction optionnelle.

### R-007 🟠 — ZIP bomb : guard incomplet
**Fichier :** `backend/app/storage/__init__.py:71-77`
```python
total_uncompressed = sum(m.file_size for m in zf.infolist())
if total_uncompressed > _MAX_ZIP_EXTRACTED_BYTES:
    raise ValueError(...)
```
**Symptôme :** `m.file_size` est la taille *déclarée* dans l'en-tête ZIP. Un
attaquant peut mentir : déclarer 1 KB, contenir 1 GB. La vérification passe,
puis `zf.read(member.filename)` extrait le vrai contenu. Pas non plus de
limite sur le nombre de fichiers (1 million de petits fichiers → exhaustion
inodes).
**Fix :** vérifier la taille réelle à l'extraction (lire en chunks et
abort si dépassement), et limiter le nombre de membres (e.g. 1000).

### R-008 🟡 — Pas de digest pinning sur les images Docker
**Fichier :** `Dockerfile`, `backend/Dockerfile`, `frontend/Dockerfile`
**Symptôme :** `FROM python:3.11-slim`, `FROM node:20-alpine` — sans `@sha256:...`.
Le contenu de l'image peut changer (CVE patch, malicious push de l'auteur,
typosquatting des registries).
**Fix :** pin par digest pour les builds reproductibles.

### R-009 🟡 — Pas de healthcheck, pas de limites de ressources
**Fichier :** `docker-compose.yml`
**Symptôme :** Pas de `healthcheck`, pas de `mem_limit`, `cpus`,
`pids_limit`. Un job pathologique peut consommer toute la RAM/CPU et
faire OOM-killer le container.
**Fix :** ajouter limites dans compose.

### R-010 🟡 — `actions/checkout@v3` obsolète
**Fichier :** `.github/workflows/hf-sync.yml:9`
**Symptôme :** v3 utilise Node 16 (deprecated), v4 utilise Node 20.
**Fix :** `actions/checkout@v4`.

### R-011 🟡 — `httpx.AsyncClient` recréé à chaque appel
**Fichier :** tous les providers
**Symptôme :** `async with httpx.AsyncClient() as client` dans chaque
`list_models` et chaque `complete_structured`. Nouveau handshake TLS à
chaque chunk. Pour un document de 50 chunks, c'est 50 handshakes au lieu
de 1.
**Impact :** latence et coût CPU.
**Fix :** singleton client géré par `lifespan` ou cache module-level.

### R-012 🟡 — `_REGISTRY` instancié à l'import du module
**Fichier :** `backend/app/providers/__init__.py:11-15`
**Symptôme :** Les providers sont instanciés tout de suite. Tests doivent
patcher le dict, mais si du code stocke une référence locale au provider
avant patch, le patch est sans effet.
**Fix :** factory function lazy, ou DI.

### R-013 🟡 — Anthropic : `blocks[0]["text"]` ne gère pas tool_use/thinking
**Fichier :** `backend/app/providers/anthropic_provider.py:80-86`
**Symptôme :** `data["content"]` peut contenir des blocs de type `tool_use`,
`thinking`, ou autres avant le `text`. Accéder à `blocks[0]["text"]` lève
`KeyError` ou récupère le mauvais bloc.
**Fix :** filtrer par `type == "text"` :
```python
text_blocks = [b for b in blocks if b.get("type") == "text"]
if not text_blocks: raise ValueError(...)
text = text_blocks[0]["text"]
```

### R-014 🟡 — SSE queue overflow silencieux
**Fichier :** `backend/app/jobs/store.py:70-73`
```python
except asyncio.QueueFull:
    pass  # slow consumer — drop
```
**Symptôme :** Si un consommateur SSE est lent (réseau saturé), les events
sont silencieusement perdus. Pas de signal côté client pour savoir que des
events ont été dropped.
**Fix :** envoyer un event `dropped` distinct, ou closure du subscriber.

### R-015 🟢 — Storage `JOB_STORAGE_DIR` = `/tmp/app-jobs` par défaut
**Fichier :** `backend/app/storage/__init__.py:9`
**Symptôme :** `/tmp` peut être nettoyé par tmpwatch / systemd-tmpfiles
pendant l'exécution d'un job → fichiers de sortie disparaissent avant
le download.
**Fix :** valeur par défaut hors /tmp (e.g. `/var/lib/alto-corrector/jobs`),
ou doc explicite.

### R-016 🟢 — `frontend/src/components/FileUpload.tsx:18-19` — MIME validation faible
**Fichier :** `frontend/src/components/FileUpload.tsx:9, 17-20`
```typescript
const ACCEPTED_MIME = ['application/xml', 'text/xml', 'application/zip']
return ACCEPTED.some((ext) => name.endsWith(ext)) || ACCEPTED_MIME.includes(file.type)
```
**Symptôme :** Validation OR — un fichier `.exe` avec MIME `application/zip`
passe. Ou un `.xml` avec MIME image. Combiné avec une faille XXE, attack
surface élevée. Heureusement la validation backend est plus stricte.
**Fix :** AND au lieu de OR, ou ne se reposer que sur l'extension côté UI.

---

## DETTE / REFACTOR

### D-001 🟡 — `_detect_namespace` dupliqué
**Fichiers :** `backend/app/alto/parser.py:29-34`, `backend/app/alto/rewriter.py:38-42`
**Fix :** module `backend/app/alto/_ns.py` partagé.

### D-002 🟡 — `lifespan` vide alors qu'on a des ressources à initialiser
**Fichier :** `backend/app/main.py:26-27`
**Fix :** y initialiser le httpx.AsyncClient partagé, fermer proprement.

### D-003 🟡 — Pass 1 / Pass 2 dans `_run_chunk` ont du code dupliqué massif
**Fichier :** `backend/app/jobs/orchestrator.py:251-329`
**Fix :** extraire `_reconcile_pair(lm, part2, ..., is_forward_of_both: bool)`.

### D-004 🟡 — Trois endpoints API répètent le même preamble
**Fichier :** `backend/app/api/jobs.py:226-302`
```python
job = job_store.get_job(job_id)
if job is None: raise HTTPException(...)
if job.status != JobStatus.COMPLETED: raise HTTPException(...)
```
**Fix :** dependency injection : `Depends(get_completed_job)`.

### D-005 🟡 — `frontend/src/api/client.ts` : 3 fetch identiques
**Fichier :** `frontend/src/api/client.ts:57-90`
**Fix :** `apiGet<T>(path: string): Promise<T>` générique.

### D-006 🟡 — Tests utilisent `asyncio.get_event_loop()` (deprecated)
**Fichiers :** `backend/tests/test_api.py:255`,
`backend/tests/test_line_acceptance.py:276, 346`,
`backend/tests/test_integration.py:72`,
`backend/tests/test_trace.py:109`
**Symptôme :** Python 3.10+ deprecation warning. 3.12+ supprime le fallback.
**Fix :** `pytest.mark.asyncio` partout, ou `asyncio.run()`.

### D-007 🟡 — `line_traces: dict[str, LineTrace]` dans `JobManifest`
**Fichier :** `backend/app/schemas/__init__.py:169`
**Symptôme :** Pour un corpus de 100k lignes, plusieurs Mo de RAM par job
restent attachés au manifest en mémoire. La cap à 200 jobs (cf. store.py:16)
peut alors représenter centaines de Mo.
**Fix :** déplacer les traces sur disque dans `output_dir(job_id)/trace.json`
seul, et exposer l'endpoint via file streaming.

### D-008 🟡 — `_JOB_TIMEOUT_SECONDS = int(os.environ.get(...))` à l'import
**Fichier :** `backend/app/jobs/orchestrator.py:38`
**Symptôme :** valeur invalide → `ValueError` à l'import, app crash sans
diagnostic clair.
**Fix :** try/except + default + log warning.

### D-009 🟡 — `max_attempts = 3` hardcodé
**Fichier :** `backend/app/jobs/orchestrator.py:122`
**Fix :** déplacer dans `ChunkPlannerConfig` ou nouvelle `JobRetryConfig`.

### D-010 🟢 — `frontend/src/index.css` n'a que 3 lignes (`@tailwind` directives)
**Fix :** rester comme tel — ou inliner dans `main.tsx` via `?inline`. Sans urgence.

### D-011 🟢 — Magic numbers dans `line_acceptance.py`
**Fichier :** `backend/app/jobs/line_acceptance.py:38-52`
**Symptôme :** seuils 0.35, 0.15, 0.85, 0.70, 1.2, 0.8 sans benchmark
documenté.
**Fix :** ajouter un test de calibration sur corpus de référence, ou
documentation justifiant.

### D-012 🟢 — Spec docs `SPECS_*.md` peuvent diverger du code
**Fichier :** 10 fichiers `SPECS_*.md` en racine, total ~80 Ko.
**Fix :** consolider ou indiquer "specs historiques" + dates de
last-sync-with-code.

---

## TESTS MANQUANTS

### T-001 🟠 — Anthropic provider : pas de test d'intégration HTTP mock
**Fichier :** `backend/tests/test_providers.py`
**Symptôme :** Seuls `list_models` et le keep-model filter sont testés. La
fonction `complete_structured` n'a aucun test → c'est pour cela que B-001
(provider Anthropic cassé) n'est pas détecté par CI.
**Fix :** test avec `httpx.MockTransport` qui simule la réponse 400
puis vérifie le fallback.

### T-002 🟠 — Pas de test pour `app.jobs.store.JobStore._evict_stale`
**Fichier :** `backend/app/jobs/store.py`
**Fix :** test avec `JobStore(ttl_seconds=0)` + `time.sleep(0)` mock.

### T-003 🟠 — Pas de test SSE queue overflow / disconnect
**Fix :** simuler un consommateur lent, vérifier que les autres reçoivent.

### T-004 🟠 — Pas de test cross-page hyphen reconciliation dans l'orchestrator
**Fichiers :** `backend/tests/test_orchestrator.py`
**Symptôme :** Le parser linke bien (`test_cross_page_hyphen_pair_linked`),
mais l'orchestrateur n'est pas testé end-to-end sur ce cas. B-005 et B-006
pourraient passer inaperçus.
**Fix :** test d'intégration sur 2 fichiers + paire césurée.

### T-005 🟠 — Pas de test pour `_sanitize_error`
**Fichier :** `backend/app/jobs/orchestrator.py:49`
**Fix :** test des différents formats (Bearer, sk-, key-) et de la
non-double-troncation (B-010).

### T-006 🟠 — Pas de test pour la normalisation Unicode NFC/NFD
**Fix :** corpus avec é décomposé vs précomposé, vérifier le validateur
et `reconcile_hyphen_pair`.

### T-007 🟠 — Pas de test pour XML malformé (sans `}` dans le tag)
**Fix :** appeler `_detect_namespace` avec un mock root malformé.

### T-008 🟡 — Pas de test pour le ZIP bomb réel (mensonge sur file_size)
**Fix :** créer un ZIP qui déclare 1 KB mais contient 100 MB en zlib.

### T-009 🟡 — Pas de test pour jobs concurrents
**Fix :** asyncio.gather(run_job_1, run_job_2, ...) et vérifier
qu'ils ne s'écrasent pas mutuellement dans `job_store`.

### T-010 🟡 — Pas de test pour client disconnect pendant SSE
**Fix :** simuler la fermeture du queue side-channel.

### T-011 🟡 — Pas de test pour DiffViewer/LayoutViewer avec `data.pages = []`
**Fix :** test composant React (RTL) avec données vides.

### T-012 🟡 — Pas de test pour `pretty_print=True` impact (B-004)
**Fix :** comparer source/output bytewise pour un cas untouched.

### T-013 🟢 — Pas de test pour `JOB_TIMEOUT_SECONDS` trigger
**Fix :** mock asyncio.wait_for ou patcher le timeout à 0.

### T-014 🟢 — Tests avec hardcoded counts (566, 26, 100…)
**Fichiers :** `test_x0000002.py`, `test_corpus_validation.py`
**Symptôme :** Fragile : tout changement dans la heuristique de
détection casse le test sans diagnostic clair.
**Fix :** réduire à des invariants (e.g. "≥ 100 PART1 explicits") ou
documenter la source du nombre.

### T-015 🟢 — 15+ tests skip silencieusement si `examples/*.xml` absent
**Symptôme :** Les vrais corpus sont en `examples/`. Si déplacés, les
tests passent vert sans rien tester. Aucun warning visible.
**Fix :** transformer en `pytest.fail()` si la couverture exige le corpus,
ou marquer explicitement `@pytest.mark.corpus` exclu par défaut.

---

## STYLE

### S-001 🟢 — `except (ValueError, Exception)` redondant
Voir B-009.

### S-002 🟢 — Modules vides `__init__.py` sans docstring
**Fichiers :** `backend/app/alto/__init__.py`, `api/__init__.py`, `jobs/__init__.py`
**Fix :** soit ajouter un module-level docstring, soit supprimer si
namespace package suffit (mais on est en src layout).

### S-003 🟢 — Régex précompilé pour secrets (orchestrator.py:41-46) bien fait
Pas un problème — note positive.

### S-004 🟢 — Tests : f-strings imbriqués dans XML templates difficiles à lire
**Fichiers :** `test_parser.py`, `test_corpus_validation.py`, etc.
**Fix :** extraire en fixtures dataclass ou fichiers `.xml` dédiés.

---

## RÉCAP PAR SÉVÉRITÉ

| 🔴 CRITICAL | 🟠 HIGH | 🟡 MEDIUM | 🟢 LOW | Total |
|---|---|---|---|---|
| Bugs : 2 (B-001, B-002) | Bugs : 7 (B-003 à B-009) | Bugs : 7 (B-010 à B-017) | Bugs : 2 | 18 |
| Risques : 1 (R-001) | Risques : 5 (R-002 à R-006, R-013/14 partagé) | Risques : 6 | 2 | 16 |
| Dette : 0 | Dette : 0 | Dette : 7 | 5 | 12 |
| Tests : 0 | Tests : 5 (T-001 à T-007) | Tests : 5 | 5 | 15 |
| Style : 0 | Style : 0 | Style : 0 | 4 | 4 |
| **3** | **17** | **25** | **20** | **65** |

---

## ORDRE DE CORRECTION RECOMMANDÉ

1. **B-001** (Anthropic cassé) — fix immédiat, sinon le provider est inutilisable
2. **B-002** (DiffViewer Rules of Hooks) — crash UI imminent
3. **R-001** (HF_TOKEN en clair) — sécurité CI
4. **B-005 + B-006** (collision IDs) — corruption silencieuse de paires cross-page
5. **B-004** (pretty_print) — quick win, énorme impact perçu
6. **B-013/B-014** (Unicode NFC) — défense fondamentale corpus français
7. **B-003** (useJobStream leak) — leaks ressources prod
8. **R-002, R-003** (rate limit, headers nginx) — sécurité base
9. **R-004, R-005, R-006** (auth + clés) — sécurité fonctionnelle
10. Le reste par ordre de sévérité
