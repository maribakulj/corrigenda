# Plan de remédiation — 15 juillet 2026

Statut : **Vagues 1 et 2 livrées** (V1.1 `bb8b49c`, V1.2 `149f7c1`, V1.3 `84a2d16`, V2.5 `f90e9f1`, V2.3 `b613045`, V2.2 `0038cf4`, V2.1 `2374b92`, V2.4 `bb14a28`) · Vagues 3–5 : proposées.
Périmètre : totalité des défauts confirmés par la contre-vérification du 15/07 (audit externe + vérification ligne-à-ligne dans le code).

Note V2.4 (choix d'implémentation) : plutôt que le fetch-streaming SSE, les surfaces sans headers utilisent des crédentiels signés HMAC scopés (job + usage + expiration) en `?sig=` — `events_url` minté à la création (durée = budget du run), `?sig=` images 15 min apposé par `/layout`. Le token de capacité ne circule plus jamais en URL (le transport `?token=` est refusé partout) ; le download passe par header + blob. Au passage, ce correctif répare les images du layout, qui ne portaient aucun crédential et étaient donc cassées par le gating.

Chaque défaut cité ici a été **vérifié dans le code** (fichier:ligne dans les fiches ci-dessous). Ce plan remplace toute liste de correctifs antérieure pour les sujets qu'il couvre.

---

## 0. Décision structurante (avant toute ligne de code)

Tout le reste du plan découle d'une décision de produit unique :

> **Corrigenda est deux choses : une bibliothèque (`packages/corrigenda`) qui vise une vraie 1.0, et une application de démonstration qui ne vise PAS l'exploitation institutionnelle aujourd'hui.**

Conséquences immédiates :

1. **Deux profils de déploiement explicites et documentés** :
   - `demo` — HF Space, mono-process, in-memory, sans auth, CORS ouvert, jobs éphémères. C'est ce qui existe. On le dit.
   - `institutional` — derrière SSO/reverse-proxy, persistance réelle, quotas, annulation. C'est la Vague 5. Tant qu'elle n'est pas livrée, **aucun document ne prétend que ce profil existe**.
2. **L'étiquette de maturité suit le profil** : la *bibliothèque* peut afficher 1.0 (après Vague 4) ; l'*application* est étiquetée `beta / demo` partout (README front-matter, UI, package.json).
3. **Ordre des vagues** : on corrige d'abord ce qui **ment silencieusement à l'utilisateur** (V1), puis ce qui **expose le serveur** (V2), puis on **rend le dépôt honnête** (V3), puis on **consolide la bibliothèque** (V4). La Vague 5 (institutional) n'est lancée que s'il existe un déploiement cible réel.

Justification de l'ordre : un bug qui affiche la trace d'une autre page est plus grave qu'une absence de haute disponibilité — le premier corrompt silencieusement le travail de l'utilisateur, le second échoue bruyamment.

---

## Vague 1 — Bugs silencieux côté utilisateur (P1, ~1 PR chacun)

### V1.1 Identité de ligne frontend : `(page_id, line_id)`
- **Défaut** : `App.tsx:129` fait `map.set(lt.line_id, lt)` ; `DiffLine` (`types/index.ts:249`) ne porte pas de `page_id` ; `DiffViewer.tsx:202` remonte `line.line_id` seul. Deux fichiers contenant tous deux `L1` ⇒ la dernière trace écrase la première, clic sur une ligne ⇒ trace d'une autre page possible.
- **Correctif** :
  - `type LineKey = \`${string}:${string}\`` (`page_id:line_id`) + helper `lineKey(pageId, lineId)` unique, utilisé partout (map de traces, sélection, DiffViewer).
  - Ajouter `page_id` à `DiffLine` côté backend si absent du payload, sinon propager celui de `DiffPage` dans le composant.
  - `onSelectLine(pageId, lineId)` au lieu de `onSelectLine(lineId)`.
- **Test d'acceptation** : test vitest avec **deux fichiers ayant volontairement les mêmes `TextLine@ID`** (`L1`, `L2`) ; sélectionner `L1` du fichier 2 doit afficher la trace du fichier 2. Ce test échoue sur le code actuel.
- **Taille** : S–M. **Fichiers** : `frontend/src/App.tsx`, `types/index.ts`, `components/DiffViewer.tsx`, test nouveau.

### V1.2 Panne SSE ≠ échec du job (+ stats structurées, même hook)
- **Défaut** : `useJobStream.ts:391-397` — après 3 échecs consécutifs, `setStatus('failed')` sans consulter le serveur ; le client n'a même **aucune** fonction `GET /api/jobs/{id}`. Résultat réel inaccessible alors que le job a pu réussir. En prime, `App.tsx:195` reconstruit les stats terminales par **regex sur la phrase de log** `"Completed …"` alors que l'événement SSE `completed` porte `lines_modified`, `hyphen_pairs_total`, `duration_seconds` (`useJobStream.ts:220-223` les lit… pour formater la phrase).
- **Correctif** (une seule PR, même hook) :
  1. Séparer deux états : `streamState: 'connected' | 'reconnecting' | 'lost'` et `jobStatus` (autoritatif serveur).
  2. Ajouter `fetchJobStatus(jobId, token)` dans `client.ts`.
  3. À l'épuisement des reconnexions SSE : passer en **polling** (`GET /api/jobs/{id}` toutes les 3–5 s) au lieu de `failed`. N'afficher `failed` que si le **serveur** le dit.
  4. Bandeau UI « connexion au flux perdue — suivi par sondage » + bouton reconnexion manuelle.
  5. Stocker le payload terminal SSE dans un état structuré `finalStats` ; supprimer la regex de `App.tsx`.
- **Test d'acceptation** : test simulant une coupure EventSource pendant un job qui se termine côté serveur ⇒ l'UI atteint `completed` et le téléchargement est accessible. Test que les stats survivent à un changement de formulation du message de log.
- **Taille** : M. **Fichiers** : `useJobStream.ts`, `client.ts`, `App.tsx`, backend inchangé.

### V1.3 Smoke test Docker qui teste vraiment le frontend
- **Défaut** : `ci.yml:425` ne fait qu'un `curl /health`, alors que `Dockerfile:29-37` documente que la régression historique visée laissait précisément `/health` à 200. Pire : `main.py:233-237` fait retourner `{"status":"ok"}` 200 à `/` même quand `index.html` est absent.
- **Correctif** :
  1. Backend : `/health/live` = ping process (inchangé) ; **`/health/ready` échoue (503) si `SERVE_FRONTEND=1` et `index.html` absent**.
  2. Backend : quand `index.html` manque et que le frontend est attendu, `/` retourne 503 explicite, pas `{"status":"ok"}`.
  3. CI : le smoke test vérifie que `/` retourne `Content-Type: text/html` **et** contient un marqueur (`<div id="root">` ou titre « Corrigenda ») ; extrait une URL d'asset `/assets/*.js` du HTML et vérifie qu'elle répond 200.
- **Test d'acceptation** : construire volontairement une image avec la copie statique au mauvais endroit ⇒ le job CI échoue.
- **Taille** : S. **Fichiers** : `backend/app/main.py`, `backend/app/api/health.py`, `.github/workflows/ci.yml`.

---

## Vague 2 — Sécurité des ressources et contrôle (P1)

### V2.1 Réservation de capacité avant lecture des uploads
- **Défaut** : `jobs.py:158` (check fail-fast, aucune réservation) → lecture jusqu'à 200 MiB en mémoire (`jobs.py:196-218`) → check autoritatif `jobs.py:293` **après** l'allocation. N requêtes concurrentes peuvent toutes bufferiser avant que 4 seulement ne démarrent. Le middleware (`upload_guard.py`) ne borne qu'une requête isolée.
- **Correctif** :
  1. `asyncio.Semaphore(MAX_CONCURRENT_UPLOADS)` (par défaut = `MAX_ACTIVE_JOBS`) acquis **avant** toute lecture du corps ; 503 + `Retry-After` immédiat si non disponible (acquisition non bloquante).
  2. **Streamer les fichiers vers le disque** (`input/` du job, déjà existant) par blocs de 1 MiB au lieu de constituer `file_tuples` en bytes — le plafond mémoire par upload tombe à ~1 MiB.
  3. Compter séparément `uploads_in_progress` et `jobs_running` ; exposer les deux dans `/health/ready`.
  4. Documenter dans README que la limite doit être **répétée au niveau du reverse proxy** en profil institutional.
- **Test d'acceptation** : test de charge léger (8 uploads concurrents de la taille max) ⇒ pic RSS borné ; 4 acceptés, 4 refusés en 503 avant consommation du corps.
- **Taille** : M–L. **Fichiers** : `backend/app/api/jobs.py`, `storage/__init__.py`, éventuellement `upload_guard.py`.

### V2.2 Annulation de job (backend + UI)
- **Défaut** : la lib expose `should_abort` (`pipeline.py:618-627`, réellement câblé en interne) mais `runner.py:317-321` ne le passe jamais. Aucune route cancel/DELETE, aucun état `CANCELLING/CANCELLED`. Seuls arrêts : timeout 1800 s, extinction, exception. Un job erroné consomme quota fournisseur pendant 30 min.
- **Correctif** :
  1. Registre `cancellation_events: dict[job_id, asyncio.Event]` dans le `JobStore` (ou le task registry).
  2. `POST /api/jobs/{job_id}/cancel` (idempotent, protégé par le job token, 202) ; positionne l'event.
  3. `runner.py` passe `should_abort=event.is_set` à `pipeline.run(...)` ; capture `CorrectionAborted` ⇒ statut `cancelled`.
  4. Nouveaux statuts `cancel_requested` / `cancelled` dans `schemas/job.py` + événement SSE.
  5. UI : bouton « Annuler » pendant `running`, avec confirmation.
  6. (Optionnel, si le provider le permet) : propager un `asyncio.CancelledError` aux appels httpx en cours pour couper immédiatement au lieu d'attendre la fin du chunk.
- **Test d'acceptation** : E2E avec le faux fournisseur lent : cancel à mi-parcours ⇒ statut `cancelled` < 5 s, aucun output promu, répertoire nettoyable.
- **Taille** : M. **Fichiers** : `runner.py`, `store.py` ou `task_registry.py`, `api/jobs.py`, `schemas/job.py`, frontend.

### V2.3 Le guard de taille rend son 413 lui-même
- **Défaut** : `upload_guard.py:124-136` — sur Content-Length menteur, le middleware fabrique un corps vide terminal et laisse le parseur multipart produire un 400/422 arbitraire au lieu d'un rejet de taille.
- **Correctif** : le middleware envoie lui-même le 413 (ou ferme la connexion proprement après drainage borné du flux restant), et le documente. Harmoniser la docstring (« aborts » → comportement exact).
- **Test d'acceptation** : requête avec Content-Length sous-déclaré ⇒ 413, pas 400/422.
- **Taille** : S. **Fichiers** : `upload_guard.py` + test.

### V2.4 Jetons hors des URLs (ou signés courts)
- **Défaut** : `jobs.py:112` accepte `?token=` pour SSE/images/download ; ces URLs fuient dans les logs de proxy/ingress/APM — précisément la couche derrière laquelle l'app dit être déployée. Aggravant vérifié : la redaction applicative (`logging_config.py:99-146`) cible des motifs de clés API et **ne masque probablement pas** un `token_urlsafe(32)` en query string.
- **Correctif** (pragmatique, sans SSO) :
  1. **Download** : passer par `fetch` + header `X-Job-Token` + blob download côté client — supprime le cas le plus sensible (URL copiable).
  2. **SSE** : remplacer `EventSource` par **fetch streaming** (`ReadableStream`) avec header — supprime `?token=` du flux. (Alternative moindre : garder EventSource mais avec un jeton d'URL distinct, à usage « events seulement », durée 5 min, renouvelable via header.)
  3. **Images** : URLs signées courtes (HMAC du chemin + exp ≤ 5 min), limitées à la ressource ; le token principal ne circule plus jamais en URL.
  4. Ajouter le motif du token de capacité aux règles de redaction, en défense en profondeur.
- **Test d'acceptation** : grep des logs d'accès d'un run E2E ⇒ zéro occurrence du token principal.
- **Taille** : M–L (le fetch-streaming SSE est le gros morceau). Peut être découpé : (1)+(4) d'abord, (2)(3) ensuite.

### V2.5 Volume Compose nommé
- **Défaut** : `docker-compose.yml:12-13` bind-mount `/tmp/app-jobs:/tmp/app-jobs` masque le `chown appuser` du build (`backend/Dockerfile:29-30`) ⇒ conteneur non-root incapable d'écrire sur un hôte Linux standard.
- **Correctif** : volume nommé `jobs-data:/tmp/app-jobs` + note README pour qui veut un bind mount (préparer l'UID).
- **Taille** : XS. **Fichiers** : `docker-compose.yml`, README.

---

## Vague 3 — Honnêteté du dépôt (docs, versions, sécurité déclarative)

### V3.1 Consolidation documentaire : UNE spec courante
- **Défaut vérifié** : le README déclare « authoritative » des documents qui se contredisent — `CONTRIBUTING.md:5-8` (« deux distributions Python », « publié sur PyPI ») vs `backend/pyproject.toml:9-12` (« NOT a build config yet ») ; `SPECS_API.md:19-20` (réponse sans `job_token`, routes trace/diff/layout/images absentes, `outputs/` vs `output/`) ; `SPECS_JOBS.md:3` (planner/validator/orchestrator localisés dans `backend/app/jobs` alors qu'ils sont dans `packages/corrigenda`).
- **Correctif** :
  1. **Une seule spec normative** : `SPECS_LIB_V2.md` (bibliothèque, versionnée avec le package) + un `docs/API.md` régénérable depuis OpenAPI pour le backend.
  2. Déplacer `SPECS.md`, `SPECS_API.md`, `SPECS_JOBS.md`, `SPECS_FRONTEND.md`, `SPECS_ALTO.md`, `SPECS_PROVIDERS.md`, `SPECS_SCHEMAS.md`, `SPECS_SPRINTS.md`, `SPECS_INFRA.md`, `PLAN_V2.md`, `PROGRESS_V1.md`, `MIGRATION.md`, `REMEDIATION_STATUS.md`, `ISSUE_LEDGER.md` → `docs/history/` avec bannière « historique, non normatif ».
  3. Réécrire `CONTRIBUTING.md` : une distribution Python (corrigenda), backend = app non packagée, liste CI à jour, coverage réelle (85 % lib / backend séparé — pas « combinée 80 % »).
  4. Le README ne garde que : quickstart, profils demo/institutional, table env, pointeurs.
- **Taille** : M (mécanique mais volumineux). Aucun code.

### V3.2 README « persistance » honnête
- **Défaut** : `README.md:101-104` propose volume persistant + `JOB_STORAGE_DIR=/data/app-jobs`, mais `store.py:69-73` est intégralement in-memory ; au restart, fichiers orphelins inaccessibles **et jamais nettoyés** (le sweep n'itère que `_completed_at`, `store.py:423`).
- **Correctif immédiat** (cette vague) :
  1. Remplacer la section README par la vérité : « les jobs sont en mémoire ; un volume ne conserve que des fichiers que l'API ne saura plus servir ».
  2. **Sweep de démarrage** : au boot, scanner `JOB_STORAGE_DIR` et supprimer tout répertoire sans enregistrement en mémoire (= tous, après restart) au-delà d'un âge de grâce. Petit, ferme la fuite disque réelle.
- **La vraie persistance (SQLite) part en Vague 5** — pas de demi-implémentation ici.
- **Taille** : S. **Fichiers** : README, `store.py` (sweep boot).

### V3.3 SECURITY.md + profil de sécurité explicite
- **Défaut** : pas de `SECURITY.md` ; CORS par défaut `*` (`main.py:197`) ; aucune notion de propriétaire de job ; le modèle repose sur un SSO externe non fourni, tandis que le déploiement documenté est un Space public.
- **Correctif** :
  1. `SECURITY.md` : modèle de menace par profil (demo : « toute personne ayant l'URL du Space peut soumettre des jobs ; ne pas y envoyer de documents sensibles ; les clés API transitent par le serveur »), processus de signalement, périmètre supporté.
  2. Profil `demo` : CORS peut rester `*` mais **documenté comme choix** ; profil `institutional` : `CORS_ORIGINS` obligatoire (le serveur refuse de démarrer en profil institutional avec `*`).
  3. Variable `DEPLOYMENT_PROFILE=demo|institutional` qui gouverne ces gardes.
- **Taille** : S–M.

### V3.4 Cohérence de version et de nom
- **Défaut** : `frontend/package.json` = `alto-llm-corrector-frontend@0.1.0` vs « Corrigenda 1.0.0 ». Symptôme d'un 1.0 posé par décision, pas par convergence.
- **Correctif** : renommer `corrigenda-frontend`, versionner avec l'app (pas avec la lib) ; l'app passe en `0.9.x-beta` tant que V1+V2 ne sont pas fusionnées. **La lib garde son versionnement SemVer propre.**
- **Taille** : XS.

### V3.5 Le client TS consomme les types générés
- **Défaut** : la CI vérifie le drift de `api.generated.ts`, mais `client.ts` importe ses types de `../types` (interfaces manuelles) — le contrôle protège un artefact adjacent.
- **Correctif** : `client.ts` dérive ses types REST de `api.generated.ts` (`components['schemas'][…]`) ; `types/index.ts` ne garde que les modèles UI et le protocole SSE (non exposé par OpenAPI) en les définissant comme alias/extensions des types générés quand ils existent.
- **Test d'acceptation** : changer un champ dans un schéma Pydantic ⇒ la CI frontend échoue à la compilation, pas seulement au diff du fichier généré.
- **Taille** : M.

---

## Vague 4 — Consolidation de la bibliothèque (la vraie 1.0)

### V4.1 `RunContext` : pipeline sans état par exécution
- **Défaut** : `pipeline.py:497-527` — compteurs, ops produites, snapshots, owners de finalisation sur `self`, reset au début de `run()` (`:664-671`) ; `run()` **mute** `document_manifest.pages` (docstring `:628`). Deux `run()` concurrents sur la même instance se contaminent.
- **Correctif en deux temps** :
  1. **Tout de suite (S)** : documenter le contrat — « une instance = un run à la fois ; le manifest est consommé/muté ; non thread-safe » — dans la docstring classe, le README du package et la spec. + un garde runtime : `RuntimeError` si `run()` est appelé alors qu'un run est en cours (`self._running` flag).
  2. **Avant le gel SemVer (L)** : extraire un `RunContext` dataclass portant tout l'état par exécution ; `CorrectionPipeline` devient configuration immutable réutilisable ; les méthodes internes reçoivent `ctx`. Décider et documenter si `run()` continue de muter le manifest (acceptable si explicite) ou retourne une copie (coût mémoire à évaluer sur gros corpus).
- **Test d'acceptation** : deux `run()` concurrents sur la même instance ⇒ soit résultats indépendants corrects (post-refactor), soit `RuntimeError` immédiate (garde).
- **Taille** : S puis L.

### V4.2 Tests par propriétés et fuzzing
- **Défaut structurel** : les tests couvrent surtout les invariants internes et les bugs déjà identifiés — mêmes hypothèses que le code, même processus générateur.
- **Correctif** :
  1. **Hypothesis** sur les invariants clés : round-trip parse→rewrite (géométrie inchangée, IDs stables), atomicité des paires de césure sous tout plan de chunking, réconciliation (aucun texte ne migre entre lignes quel que soit le retour LLM simulé).
  2. **Fuzzing XML** (via Hypothesis ou atheris) : ALTO/PAGE malformés, encodages, polygones dégénérés, SUBS_* incohérents ⇒ jamais de crash non-typé, toujours une erreur classée.
  3. **Corpus externe** : un jeu de fichiers ALTO/PAGE réels (Gallica/Transkribus) **non utilisés pendant le développement**, en job CI séparé non bloquant d'abord, bloquant ensuite.
- **Taille** : L, incrémental. Prioriser (1) — c'est le meilleur ratio effort/assurance.

### V4.3 Chaîne de build reproductible et distribuable
- **Défauts** : pins directs sans lock des transitives ; `pip install -e` dans les deux Dockerfiles de prod (`Dockerfile:23`, `backend/Dockerfile:11`).
- **Correctif** :
  1. `uv lock` (ou `pip-compile --generate-hashes`) pour backend + lib ; installation `--require-hashes` en CI et Docker.
  2. Dockerfiles : `pip wheel packages/corrigenda -w /wheels && pip install /wheels/corrigenda-*.whl` — on shippe l'artefact qu'on distribue.
  3. Workflow de publication : vérifier que le commit tagué a une **CI complète verte** (API checks) et publier l'artefact attesté de la CI (upload/download d'artefact + attestation GitHub) au lieu de rebuilder. SBOM (`pip-audit --format cyclonedx` existe déjà en germe) + attestations `actions/attest-build-provenance`.
- **Taille** : M.

### V4.4 Archéologie de commentaires → ADRs
- **Défaut** : code saturé de `Audit-Fxx`, `Wave-N review`, généalogies de bugs.
- **Correctif** : passe unique — chaque commentaire d'audit devient soit (a) un commentaire d'**invariant** (« les paires de césure ne traversent jamais une frontière de chunk car… »), soit (b) un ADR dans `docs/adr/NNN-*.md`, soit (c) supprimé. Règle CONTRIBUTING : les références d'audit vivent dans les PR/issues, pas dans le code.
- **Taille** : M, mécanique, idéal en fin de vague (après les refactors qui touchent les mêmes fichiers).

### V4.5 Revue indépendante avant gel SemVer
- Avant de taguer `corrigenda-v1.0.0` : une **revue humaine externe de l'API publique** (surface d'export, noms, contrats de mutation, exceptions) + les résultats V4.2 sur corpus externe. Le gel SemVer est un engagement ; il se prend sur preuve indépendante, pas sur auto-audit.

---

## Vague 5 — Profil institutional (uniquement si déploiement cible réel)

Déclencheur : une institution identifiée veut déployer. **Ne pas construire à vide.**

1. **Persistance SQLite** (mono-instance suffit largement) : table jobs (id, statut, token hashé, timestamps, chemins d'artefacts), écrite à chaque transition ; scan de récupération au démarrage (jobs `running` orphelins → `failed:interrupted`) ; sweep basé sur la base, plus sur la mémoire. PostgreSQL **seulement** si multi-instance devient réel.
2. **Auth + ownership** : OIDC (l'institution a déjà un IdP) ; `owner_sub` sur les jobs ; listing « mes jobs » ; téléchargement différé.
3. **Quotas** : par utilisateur — jobs actifs, pages/jour, plafond de coût estimé avant lancement (le comptage de tokens existe déjà dans `_usage`).
4. **Worker séparé + queue** : uniquement si la charge le justifie ; l'étape intermédiaire honnête est « single worker + SQLite + reprise au boot », qui couvre l'essentiel des besoins patrimoniaux.
5. **Observabilité** : compteurs Prometheus (latence chunk, taux de fallback, erreurs fournisseur, coût par job) — les métriques existent déjà dans le rapport, il faut les exposer.
6. **RGPD** : politique de rétention (le sweep a déjà la mécanique), page de confidentialité (documents et clés transitent par le serveur), suppression sur demande = la route DELETE de V2.2 étendue.
7. **Test de charge documenté** : corpus de plusieurs centaines de pages, profil mémoire (streaming des traces si nécessaire).

---

## Ordonnancement et parallélisation

```
V1.1  V1.2  V1.3        ← indépendants, parallélisables, à faire EN PREMIER
  └──────┬──────┘
V2.1  V2.2  V2.3  V2.5  ← indépendants entre eux
        V2.4            ← après V1.2 (touche le même hook SSE)
  └──────┬──────┘
V3.1 → V3.2, V3.3, V3.4, V3.5   ← V3.1 d'abord (les autres docs s'y raccrochent)
  └──────┬──────┘
V4.1(S) immédiat · V4.1(L) → V4.4 · V4.2 en continu · V4.3 avant tout tag
  └──────┬──────┘
V4.5 → tag corrigenda-v1.0.0
V5 : sur déclencheur uniquement
```

Règles de fusion :
- **1 fiche = 1 PR** (V2.4 peut en faire deux). Pas de PR fourre-tout : c'est ce qui a produit la PR de 6 767 lignes sans reviewer.
- Chaque PR embarque **son test d'acceptation** (celui qui échoue avant, passe après).
- Tant que V1+V2 ne sont pas fusionnées : README et UI affichent `beta`.

## Critère de sortie global

- **App demo honnête** : V1 + V2 + V3 fusionnées ⇒ l'app peut rester une démo publique sans mentir à personne.
- **Lib 1.0** : V4 complète + revue indépendante ⇒ tag et publication PyPI réels.
- **App institutional** : V5, sur besoin réel uniquement.
