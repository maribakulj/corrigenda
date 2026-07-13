# Plan de correction complet — 37 findings confirmés

Source : audit exhaustif (72 fichiers, 152 agents, vérification adversariale) — rapport détaillé dans
`docs/audit/AUDIT-2026-07-13.md` (scénario + raisonnement de vérification pour chaque finding).
Harnais E2E local (mock vendeur honnête + saboteur) : `tools/e2e/` (voir son README).
Chaque finding référencé `fichier:ligne`.
Périmètre : branche `claude/corrigenda-audit-check-w1m6fy` (PR #54, puis PR #55 après merge de #54). Aucun merge sur main.
Statut : **EXÉCUTÉ intégralement (2026-07-13)** — les 37 corrections F1-F37 sont appliquées, chacune
avec test rouge préalable, sur les commits `987c47d..6035260` (voir `git log` de la branche).
Chaque vague a reçu sa revue adversariale par sous-agent, et **tous les findings confirmés par les
revues ont été traités** avant la vague suivante :

- Vague 0 (E2E) : revue → 5 durcissements (`ef2c075`).
- Vague 1 (F1-F12) : revue → 1 majeur (OverflowError HYP WIDTH dans le rewriter, twin F7) +
  2 mineurs (attribution `page_id` des ops d'edit_script ; garde anti-doublons sur les coutures
  de descente de granularité) — traités dans `8d0add9`.
- Vague 2 (F13-F17) : revue → 2 majeurs (caps de sortie gén. 5 absents de la table F14 → 8192 ;
  pagination `list_models` Anthropic ignorée, twin F16) + 4 mineurs (log de rampe neutralisée,
  troncature de pagination silencieuse, alias camelCase du strip-fallback, famille gpt-5) —
  traités dans `f01aaee`.
- Vague 3 (F18-F23) : revue → 2 majeurs (fuite de secrets via le fallback repr du formatter JSON,
  après le filtre de redaction ; `/api/providers/models` hors du garde-fou de taille F18) +
  4 mineurs (rollback rmtree offloadé+shieldé, stream de job évincé terminé proprement, tests AST
  durcis, twins bloquants /diff /layout /trace et `_write_outputs` du pipeline offloadés) —
  traités dans `25076a6`.
- Vague 4 (F24-F33) : revue → 2 majeurs (promesses `retryFetch` périmées traversant un reset ;
  `traceError` latché jamais rendu) + 3 mineurs (payloads réels des événements diagnostiques,
  spam du default arm, skip StrictMode de FileUpload) — traités dans `62788c4`.
- Vague 5 (F33-F37) : rework couverture (seuils vitest **70/70/70/70** gatés sur ~95/91/93/97 mesurés,
  suite frontend 31→118 tests ; `npm audit` bloquant ; script de génération des types réparé —
  son étape prettier était un no-op `npx --no`) ; revue → 4 durcissements (`frontend/.dockerignore`,
  `timeout-minutes` sur tous les jobs, retry npm audit, lint des tests lib) — `536d469` + `6035260`.
- Vague 6 : revue adversariale cumulée finale + CHANGELOG + ce statut.

## Principes d'exécution (leçons de la session)

1. **Repro avant fix** : tout finding dynamique est reproduit en live (harnais E2E) ou par un test qui échoue AVANT la correction. Pas de fix « sur plan ».
2. **Branches symétriques** : chaque fix énumère explicitement les branches/call-sites jumeaux (mode explicite ↔ heuristique, `_apply_subs` ↔ `_subs_need_update`, ALTO ↔ PAGE…). C'est l'erreur qui a rendu mes fixes précédents partiels.
3. **Une vague = un lot atomique** : fixes + tests + suites complètes + mypy/ruff + CI verte + **revue adversariale du diff de la vague** avant de passer à la suivante.
4. **Byte-parity** : tout changement rewriter est classifié par TextLine avant re-pin des goldens (jamais de régénération aveugle).
5. Chaque commit référence son finding (`Audit-F<n>`).

---

## Vague 0 — Harnais E2E intégré aux tests

Le harnais (mock vendeur honnête + saboteur, lancement backend patché) est déjà committé dans `tools/e2e/`. La vague 0 le transforme en gate permanent :

- `backend/tests/e2e/` : mock vendeur (honnête + sabotage), fixture de lancement uvicorn+mock, scénarios pytest : job honnête complet (upload→SSE→download, invariants géométrie/césure), job saboté (fusion, absorption, ligne vide → fallbacks + `completed_with_fallbacks`), token capability (404 sans token).
- Nouveau job CI `backend-e2e` (rapide : mock local, ~10 s).
- **Livrable** : le comportement observé aujourd'hui devient un test automatique et permanent.

---

## Vague 1 — Bibliothèque : intégrité du texte (12 findings)

La priorité que tu as fixée (library-integrity-first) : ces findings corrompent ou dégradent silencieusement la sortie.

| # | Finding | Sév. | Correction | Test |
|---|---------|------|------------|------|
| F1 | `hyphenation.py:376` — PART2 heuristique peut absorber les mots de la ligne suivante | P2 | Appliquer à la branche **heuristique** le même garde de croissance que la branche explicite (`len(tokens2) > len(part2.ocr_text.split())` → fallback), en préservant la sémantique subs de la branche | Test miroir du test explicite existant, en mode heuristique (échoue avant fix) |
| F2 | `pipeline.py:1749` — extension de revert à un seul saut : chaîne 3+ lignes → paire mixte | P2 | Remplacer la passe unique par un **point fixe/worklist** : les partenaires enrôlés sont eux-mêmes parcourus jusqu'à stabilité (chaînes PART1→BOTH→…→PART2 entières) | Chaîne a-b-c reconciliée, flag sur a → a, b **et c** revert (échoue avant fix) ; idem flag sur c |
| F3 | `pipeline.py:1011` — passe frontière inter-chunks lit le texte post-revert (3-run masqué) | P3 | Snapshoter les corrections **pré-revert** par ligne (comme le fait déjà la passe intra-chunk via `accepted_lines`) et comparer la frontière sur ce snapshot | Run de 3 corrections identiques à cheval sur 2 chunks → la 3ᵉ revert aussi |
| F4 | `pipeline.py:859` — `_producer_ops` indexé par line_id nu (collision inter-fichiers) | P3 | Clé composite `(page_id, line_id)` (pattern `_trace_key` existant) pour la capture et la relecture | Deux fichiers réutilisant `L1` avec corrections différentes → edit_script correct pour chacun |
| F5 | `rewriter.py:265` — `_subs_need_update` sans le garde single-String BOTH | P3 | Extraire le garde en helper partagé et l'appliquer aux **deux** fonctions (`_apply_subs` + `_subs_need_update`) | Ligne BOTH single-String en correction identité → chemin UNTOUCHED (aujourd'hui : jamais) |
| F6 | `rewriter.py:613` — HYP placé après le dernier **mot**, ignore un SP final | P3 | Positionner le HYP après le dernier **enfant émis** (mot ou SP) ; ou trimmer le texte avant `_tokenize` (choix : trim, plus simple, cohérent avec F4/UNTOUCHED strip) | corrected_text à espace final sur PART1 explicite → géométrie sans chevauchement, somme = WIDTH |
| F7 | `core/_parse.py:35` — `int(float("inf"))` → OverflowError non attrapé | P2 | `except (ValueError, OverflowError)` + même traitement (strict → ParseError, sinon défaut). **Helper partagé** pour les 3 sites | `"inf"`, `"1e999"`, `"-1e400"`, `"nan"` → défaut en mode tolérant, ParseError en strict |
| F8 | `alto/_ns.py:38` — `_int_attr` même crash inf/overflow | P2 | Utiliser le helper F7 | Coordonnée ALTO `HPOS="1e999"` → politique tolérante, pas de crash |
| F9 | `page/_ns.py:69` — `polygon_to_bbox` même crash | P2 | Utiliser le helper F7 (skip atomique du couple, déjà en place pour ValueError) | Polygone `"10,10 20,inf"` → paire ignorée, bbox correcte |
| F10 | `validator.py:149` — U+2028/U+2029 échappent au check newline | P3 | Étendre le check aux séparateurs Unicode (`  `, et `\x0b\x0c\x85` par cohérence `str.splitlines`) — même extension côté `editing._has_newline` (branche jumelle) | Correction contenant U+2028 → rejetée des deux côtés |
| F11 | `rules.py:126` — garde lexique non re-validé quand plusieurs édits composent dans un même token | P3 | Après sélection gloutonne, **re-valider le token composé** (tous les édits d'un même token appliqués ensemble) contre le lexique ; rejeter le lot sinon | Deux règles guardées touchant le même mot dont la composition sort du lexique → aucune n'est émise |
| F12 | `page/_custom.py:53` — groupes structurels reconstruits (espaces normalisés), pas « verbatim » | P3 | Préserver la sous-chaîne source réelle des groupes conservés (slice de l'original au lieu de reconstruction) ; sinon corriger la doc — décision : préserver (cohérent avec la promesse) | Round-trip d'un `custom` à espaces multiples → byte-identique |

**Gate vague 1** : suite lib complète + byte-parity (reclassifié si F6 bouge des octets) + mypy --strict + ruff + revue adversariale du diff.

---

## Vague 2 — Providers (5 findings, dont le P1)

| # | Finding | Sév. | Correction | Test |
|---|---------|------|------------|------|
| F13 | `anthropic_provider.py:141` — `temperature` toujours envoyée → 400 dur sur Opus 4.7/4.8, Sonnet 5, Fable 5 | **P1** | Double défense : (a) table de capacités par famille — ne pas envoyer `temperature` aux modèles qui la refusent ; (b) **fallback générique** : sur 400 dont le message cite un paramètre non supporté, retirer le paramètre et rejouer (couvre les futurs modèles inconnus) | Unit : body sans `temperature` pour les familles récentes ; fallback rejoue sans le param sur 400 simulé (mock). **Validation vendeur réelle si clé Anthropic fournie** |
| F14 | `anthropic_provider.py:43` — ordre des branches : `claude-3-7` inatteignable (cap 4096 au lieu de 64000) | P2 | Réordonner : tester `claude-3-7`/`3.7` **avant** `claude-3` (+ test exhaustif de la table) | `_model_output_cap("claude-3-7-sonnet")==64000` (échoue avant fix) |
| F15 | `openai_provider.py:65` — `temperature=0.0` → 400 sur o1/o3/o4, fallback json_object ne récupère pas | P2 | Même stratégie que F13 : omission par famille (`o1*`, `o3*`, `o4*`) + fallback strip-param sur 400 | Unit par famille + fallback |
| F16 | `google_provider.py:50` — `nextPageToken` ignoré → modèles silencieusement tronqués | P3 | Boucle de pagination (borne de sécurité ~10 pages) | Mock 2 pages → liste complète |
| F17 | `api/providers.py:59` — detail d'erreur affiche `Provider.OPENAI` au lieu de `openai` | P3 | `provider.value` dans le message | Assert sur le detail |

**Gate vague 2** : suites backend + E2E mock + (optionnel) test vendeur Anthropic réel.

---

## Vague 3 — Backend : robustesse serveur (6 findings)

| # | Finding | Sév. | Correction | Test / Repro live |
|---|---------|------|------------|-------------------|
| F18 | `api/jobs.py:197` — caps d'upload APRÈS le spool multipart complet de Starlette → DoS disque | P2 | Middleware ASGI **avant** le parse : rejeter si `Content-Length` absent/excessif sur `/api/jobs`, + garde streaming (compteur de bytes reçus, coupe au-delà du plafond) — les caps in-handler restent en défense en profondeur | Repro live (harnais) : POST body énorme borné → 413 immédiat, disque intact ; test unitaire middleware |
| F19 | `api/jobs.py:250,418` — parse/extract/zip synchrones sur l'event-loop | P2 | `asyncio.to_thread` autour de `save_uploaded_files`, `build_document_manifest`, et la construction du ZIP de download | Repro live : upload d'un gros fichier pendant un `GET /health` chronométré → la latence /health reste plate après fix |
| F20 | `store.py:327` — événement terminal perdu sous backpressure → stream SSE infini | P2 | Double défense : (a) sur le timeout keepalive, **re-vérifier `job.status`** et émettre le terminal synthétique si l'état est terminal ; (b) dans `emit()`, garantir la livraison du terminal (drainer une place / file dédiée aux terminaux) | Repro live : consommateur lent + file réduite (patch runtime) → le stream se termine quand même ; test unitaire sur le keepalive |
| F21 | `store.py:364` — `shutil.rmtree` d'éviction sur l'event-loop dans `create_job` | P3 | `asyncio.to_thread` (pattern du sweep périodique déjà en place) | Test : éviction opportuniste ne bloque pas la boucle (chrono) |
| F22 | `logging_config.py:138` — sonde `json.dumps` n'attrape que TypeError (ValueError/RecursionError font sauter le record) | P2 | `except Exception` → `repr()` de repli (un log ne doit jamais tuer un record) | Extra avec objet à `__repr__` récursif / float inf → record émis avec repli |
| F23 | `read_models.py:42` — `hyphen_pairs` ignore `HyphenRole.BOTH` (sous-compte des chaînes) | P3 | Compter PART1 **et** BOTH (côtés forward), aligné sur le comptage pipeline | Doc à chaîne 3 lignes → compte exact |

**Gate vague 3** : suites backend + scénarios E2E dédiés (slow-consumer, gros upload) ajoutés au harnais.

---

## Vague 4 — Frontend (10 findings)

| # | Finding | Sév. | Correction | Test |
|---|---------|------|------------|------|
| F24 | `JobProgress.tsx:8-29` — `completed_with_fallbacks` : badge vide + classe `undefined` | P2 | Ajouter label (« Terminé (avec replis) ») + couleur (ambre) ; **fallback défensif** pour tout statut inconnu | Test composant : badge rendu pour les 6 statuts + un statut inconnu |
| F25 | `useJobStream.ts:153` — `lines_done += line_count` (contexte inclus) → barre >100 % | P2 | Utiliser `target_count` (déjà émis par le backend), fallback `line_count` si absent ; clamp à `lines_total` | Événements chunk avec contexte → jamais >100 % |
| F26 | `useJobStream.ts:264` — `chunk_error`/`hyphen_partner_missing` avalés | P2 | Cases dédiées (log warning visible) + ajout à l'union `SSEEventData` + **case `default`** qui logue les événements inconnus | Événement injecté → visible dans le log panel |
| F27 | `App.tsx:63` — fetch diff/layout en boucle infinie sur échec persistant | P2 | Retry borné (3) avec backoff, puis état d'erreur affiché | Mock fetch qui échoue → 3 tentatives puis stop + message |
| F28 | `App.tsx:86` — idem fetch trace | P2 | Même mécanisme (helper partagé avec F27) | Idem |
| F29 | `useModels.ts:23` — réponses out-of-order → modèles du mauvais provider affichés | P2 | Jeton de staleness (id de requête / AbortController) : seule la dernière requête écrit l'état | Deux réponses inversées → état = dernière requête |
| F30 | `useJobStream.ts:251` — `subscriber_cap_reached` : reconnexion toutes les 2 s pour toujours (churn) | P3 | Backoff progressif plafonné (2 s → 30 s) sur cette raison précise, **sans** marquer failed (la récupération auto est conservée — le réfuteur a montré que couper la reconnexion serait pire) | Simulé : cadence de reconnexion décroît |
| F31 | `FileUpload.tsx:43` — `onFilesChange` appelé dans l'updater setState (render phase) | P3 | Sortir l'effet dans un `useEffect` sur l'état fichiers | Test lint/React strict-mode sans warning |
| F32 | `FileUpload.tsx:39` — dédup par nom seul → fichiers distincts silencieusement perdus | P3 | Dédup par (nom, taille, lastModified) + message visible quand un doublon est écarté | Deux fichiers même nom, contenus ≠ → les deux gardés (ou avertissement explicite) |
| F33 | `api.generated.ts` — types OpenAPI périmés (statut, job_token, geometric_pairing) | P3 | Régénérer depuis l'OpenAPI backend + **step CI de drift** (régénère et diff) ; sinon supprimer le fichier s'il reste inutilisé — décision : régénérer + gate | CI échoue si drift |

**Gate vague 4** : vitest + tsc + eslint + build ; test manuel visuel via le harnais (frontend build servi par le backend local).

---

## Vague 5 — CI / Infra (4 findings)

| # | Finding | Sév. | Correction |
|---|---------|------|------------|
| F34 | `ci.yml:21` — aucun build Docker en CI | P2 | Job `docker-build` (root + backend + frontend, `push: false`) + smoke `/health` sur l'image root |
| F35 | `frontend/Dockerfile:8` — `nginx:alpine` non épinglé | P3 | Épingler par digest sha256 (cohérent avec les autres images) |
| F36 | `ci.yml:266` — pas d'audit CVE npm | P3 | Step `npm audit --audit-level=high` (non bloquant d'abord, bloquant après triage) |
| F37 | `vitest.config.ts:14` — couverture frontend non gâtée | P3 | Seuils vitest (départ 70 %, à monter) + `--coverage` en CI |

---

## Vague 6 — Validation finale

1. Suites complètes (lib + backend + frontend) + mypy --strict + ruff + byte-parity.
2. Harnais E2E : scénario honnête + scénario sabotage verts.
3. CI verte sur PR #54 (avec les nouveaux jobs docker-build/e2e).
4. **Revue adversariale du diff cumulé des 6 vagues** (workflow multi-agents, comme l'audit) — c'est le filet qui aurait attrapé mes fixes partiels.
5. Optionnel (si tu fournis une clé Anthropic temporaire) : test vendeur réel — `list_models` + un job sur `claude-haiku` récent, validant F13/F14 contre l'API réelle. Clé en env uniquement, jamais loggée, à révoquer après.
6. CHANGELOG + mise à jour du rapport d'audit (findings → fixed).

---

## Non retenus (7 réfutés à la vérification — pour trace)

| Finding | Raison de la réfutation |
|---|---|
| Multi-worker (main.py:142) | Documenté single-worker ; mécanique exacte mais choix d'architecture assumé, pas un défaut |
| Fuite tempfile download (jobs.py:413) | Impossible sur la stack épinglée (uvicorn exécute la BackgroundTask même sur déconnexion) |
| Clés Gemini `AIza` non redigées | Aucun chemin de log actuel ne porte la clé (header, jamais loggé) — défense en profondeur possible, non nécessaire |
| Spoofing X-Forwarded-For | Prémisse fausse : le Dockerfile par défaut est `TRUSTED_PROXIES=127.0.0.1`, pas `*` |
| `subscriber_cap` → faux « failed » | Le reset `onopen` (fix Audit-C) rend le mislabel inatteignable ; seul le churn subsiste (retenu en F30) |
| Types générés « risque runtime » | Fichier non importé — retenu seulement comme drift d'artefact (F33) |
| bandit sur la lib | Chaque élément du scénario de risque réfuté (parser déjà durci, XXE déjà neutralisé) |

---

## Ordre, dépendances, volume

- **Ordre** : 0 → 1 → 2 → 3 → 4 → 5 → 6. La vague 1 d'abord (ta priorité intégrité-lib : corruption silencieuse > panne bruyante) ; la vague 2 contient le P1 (panne dure mais visible et contournable en choisissant un autre modèle).
- **Dépendances** : le harnais (vague 0) est requis par les repros des vagues 2-4. F7 (helper overflow) précède F8/F9. F27/F28 partagent un helper.
- **Volume estimé** : vague 1 la plus lourde (12 fixes cœur + goldens) ; vagues 2-3 moyennes ; 4-5 légères. ~25-35 commits au total.
- **Réversibilité** : tout sur la branche PR #54, une vague = un lot de commits identifiables, revert possible par vague.
