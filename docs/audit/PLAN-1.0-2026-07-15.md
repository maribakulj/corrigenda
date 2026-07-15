# Plan 1.0 — de la remédiation à la maturité (15 juillet 2026)

Statut : **proposé**. Ce plan succède à `PLAN-REMEDIATION-2026-07-15.md`
(Vagues 1–4 livrées ; V4.5 — revue externe — reste ouvert ; Vague 5
institutional inchangée, sur déclencheur uniquement). Il couvre le chemin
entre l'état actuel (`main` @ `2fd1334`) et une bibliothèque `corrigenda`
1.0 réellement mature : correctifs résiduels vérifiés, refonte du cœur,
preuve de qualité mesurée, release.

Chaque défaut cité en Phase 1 a été **vérifié dans le code** (fichier:ligne
dans les fiches). Les items non vérifiables (refonte, corpus) sont des
décisions de conception, marquées comme telles.

Principe directeur, hérité du tri des audits externes de juillet :

> Corrigenda ne doit pas devenir plus sophistiquée ; elle doit devenir
> plus petite, plus déterministe et plus démontrable. Chaque chantier
> répond à une seule question : est-ce que cela rend le cœur plus petit
> ou plus démontrable ? Si la réponse est « cela ajoute de la capacité »,
> l'item va en 1.x ou dans `integrations`, pas dans le cœur.

## Méthode (invariante sur tout le plan)

- **Test d'abord** : chaque correctif ship avec le test qui échoue avant
  (règle existante du dépôt). Pour la refonte : la suite métamorphique
  (P0) doit être verte avant ET après chaque étape.
- **1 fiche = 1 PR** en Phase 1 ; en Phase 3 une étape peut prendre
  plusieurs PR mais chaque PR laisse `main` vert (pytest, mypy --strict,
  couverture ≥ 85 %).
- **ADR pour chaque décision structurante** (la refonte en produit
  quatre : ADR-009 à ADR-012 ; la révision de la taxonomie d'erreurs met
  à jour ADR-008).
- Les références d'audit (`P1.x`, phases) restent dans les PR et ce
  document — **jamais dans les commentaires du code** (règle V4.4).

## Vue d'ensemble et dépendances

```
P0  Filet de sécurité (propriétés métamorphiques)     ──┐
P1  Correctifs vérifiés (13 fiches, indépendantes)      │ P0 requis avant P3
P2  Honnêteté de version (trivial, immédiat)            │
P3  Refonte du cœur (ordre strict 3.1 → 3.4, puis libre)◄┘
P4  Preuve de qualité (corpus, CER/WER, benchmark)    — en parallèle de P3
P5  RC → revue externe (V4.5) → 1.0                   — après P3 + P4
```

P1 et P2 ne dépendent de rien et peuvent démarrer immédiatement, en
parallèle de P0. P3 ne démarre qu'avec P0 vert (ou ses échecs documentés
en xfail avec ticket). P5 est bloqué par V4.5 (revue humaine externe),
qui ne peut porter que sur l'API **post-refonte** — la faire avant P3
serait la faire deux fois.

---

## Phase 0 — Filet de sécurité : propriétés métamorphiques

Objectif : avant de toucher au cœur, encoder son comportement observable
dans des propriétés qui survivront à la refonte. Les tests actuels
(`test_properties_hypothesis.py`) couvrent géométrie, atomicité des
paires et no-op ; il manque les propriétés qui contraignent réellement
la partie que P3 va réécrire.

### P0.1 Invariance au découpage en chunks (la propriété maîtresse)
- **Propriété** : producteur déterministe + même document + partitions de
  chunks différentes (granularités forcées PAGE/BLOCK/WINDOW/LINE) ⇒
  textes finaux identiques et décisions de rapport identiques.
- **Pourquoi d'abord** : c'est exactement ce que les trois passes de
  doublons (`pipeline.py:848`, `pipeline.py:1103`, `pipeline.py:1121`),
  `finalized_owner` et `accepted_snapshot` essaient de garantir à la
  main. Si la propriété échoue sur le code actuel, c'est une découverte
  (xfail + ticket) ; si elle passe, elle devient le portique de P3.3.
- **Fichiers** : `packages/corrigenda/tests/test_metamorphic.py` (nouveau).
- **Taille** : M.

### P0.2 Autres propriétés du filet
- Producteur identité ⇒ sortie XML octet-identique à la source (plus fort
  que la propriété 3 actuelle, qui ne compare que les `CONTENT`).
- Replay de l'`EditScript` retourné ⇒ même résultat que le run
  (équivalence script/moteur).
- Deux runs sur deux copies profondes du même manifest ⇒ résultats
  indépendants (documente la mutation actuelle ; P3.4 transformera ce
  test en « deux runs sur le MÊME document » sans copie).
- Corrections arbitraires sur des lignes **avec** césure (les propriétés
  actuelles les excluent — `test_properties_hypothesis.py:136`).
- **Taille** : M. Total P0 : ~1 semaine.

---

## Phase 1 — Correctifs vérifiés (chacun : test qui échoue d'abord)

Ordre interne indifférent sauf mention. Tous sont petits et sans risque
architectural ; aucun n'attend la refonte.

### P1.1 Sémantique des fallbacks : compter des lignes, pas des chunks
- **Défaut** : `ctx.fallback_count += 1` par **chunk** tombé en fallback
  (`pipeline.py:1306`, `pipeline.py:1327`) ; le backend en dérive le
  statut `COMPLETED_WITH_FALLBACKS` (`runner.py:143-147`) et l'événement
  terminal le transporte (`runner.py:185`) ; le frontend l'affiche comme
  « N line(s) fell back » (`useJobStream.ts:298`, `useJobStream.ts:469`).
  Un chunk de 20 lignes rejeté s'affiche « 1 ligne ».
- **Correctif** : le rapport connaît déjà les lignes en fallback
  (`schemas.py:818`, propriété `fallback_lines`) — la source de vérité
  existe. Exposer sur le résultat `fallback_lines: int` (dérivé du
  rapport), `fallback_chunks: int` (l'actuel compteur, renommé) et les
  motifs agrégés. `COMPLETED_WITH_FALLBACKS` se fonde sur
  `fallback_lines > 0`. Le payload SSE et le frontend affichent
  `fallback_lines`.
- **Test d'acceptation** : chunk de 20 lignes rejeté ⇒
  `fallback_lines == 20`, jamais 1. Contrat SSE mis à jour
  (`test_sse_event_contract.py`).
- **Taille** : S–M. **Fichiers** : `core/pipeline.py`, `core/schemas.py`,
  `backend/app/jobs/runner.py`, `frontend/src/hooks/useJobStream.ts`,
  `frontend/src/types/`.

### P1.2 Réservation d'upload au niveau ASGI, avant le parsing multipart
- **Défaut** : le check-and-increment est dans la fonction de route
  (`jobs.py:259-268`), donc **après** que FastAPI a construit
  `files: list[UploadFile]` (`jobs.py:242`) — le corps multipart complet
  est déjà reçu et spoolé quand le 503 part. Le commentaire
  (`jobs.py:253`) et le README (ligne 123, « reserved before reading any
  body byte ») promettent l'inverse.
- **Correctif** : middleware ASGI `UploadAdmissionMiddleware` monté avec
  `UploadSizeLimitMiddleware`, scopé sur `POST /api/jobs`. Slots épuisés
  ⇒ répondre 503 + `Retry-After` + `Connection: close` **sans jamais
  appeler `receive()`** ; slot libéré dans un `finally`. Le check de
  route disparaît (ou reste en ceinture-bretelles documentée).
- **Test d'acceptation** : `receive()` factice qui lève s'il est appelé ;
  slots saturés ⇒ 503 émis sans déclencher le piège. Test de libération
  du slot sur exception aval.
- **Taille** : M. **Fichiers** : `backend/app/middleware.py`,
  `backend/app/api/jobs.py`, `backend/app/main.py`, `README.md`,
  `backend/tests/test_upload_admission.py`.

### P1.3 Lier le format au document, supprimer le défaut ALTO implicite
- **Défaut** : le pipeline retombe sur `AltoFormatAdapter()` quand aucun
  adaptateur n'est injecté (`pipeline.py:142-154`, `pipeline.py:2007`).
  Le quickstart (`docs/quickstart.md:61`) suggère de permuter l'import du
  parser PAGE **sans** mentionner l'adaptateur : l'utilisateur qui suit
  la doc réécrit du PAGE avec le rewriter ALTO.
- **Correctif** : le manifest porte son format (détecté par namespace au
  parsing) ; le pipeline en dérive l'adaptateur ; adaptateur explicite
  incompatible avec le format du manifest ⇒ erreur immédiate ;
  `_default_format_adapter` supprimé. Quickstart corrigé.
- **Test d'acceptation** : manifest PAGE sans adaptateur explicite ⇒
  sortie PAGE correcte ; adaptateur ALTO + manifest PAGE ⇒ erreur
  explicite au démarrage du run, pas à l'écriture.
  `examples/quickstart.py` exécuté en CI sur ALTO **et** PAGE.
- **Taille** : M. **Fichiers** : `core/schemas.py`, `core/pipeline.py`,
  `formats/*/parser.py`, `docs/quickstart.md`, `examples/quickstart.py`,
  `.github/workflows/ci.yml`.

### P1.4 La ré-extraction devient un invariant exécuté, pas une donnée de trace
- **Défaut** : le pipeline re-parse le XML produit et stocke le texte
  ré-extrait (`pipeline.py:2061-2068`, `output_alto_text`,
  `schemas.py:777`) mais **ne le compare jamais** au texte décidé : une
  divergence de projection serait enregistrée silencieusement.
- **Correctif** : comparaison systématique texte ré-extrait ↔ texte final
  décidé, par ligne. Divergence ⇒ échec du run (les octets écrits ne
  correspondent pas aux décisions — c'est une corruption, pas une
  dégradation).
- **Test d'acceptation** : rewriter saboté (monkeypatch qui altère une
  ligne) ⇒ le run échoue avec une erreur de projection nommant la ligne ;
  jamais de sortie divergente promue.
- **Taille** : S. **Fichiers** : `core/pipeline.py`, test nouveau.

### P1.5 Invariant « toute ligne a exactement une décision terminale »
- **Défaut** : une `CorrectionError` échappant à `_run_chunk` est
  absorbée en `chunk_error` + continue (`pipeline.py:1084-1101`) sans
  garantie que les lignes du chunk soient passées en état terminal ;
  aucun check runtime de `PENDING` en fin de run (le corpus externe
  l'affirme dans un test, le moteur ne l'exige pas).
- **Correctif** : (a) dans la branche d'absorption, basculer atomiquement
  toutes les lignes cibles du chunk non terminales en fallback avant de
  continuer ; (b) invariant de fin de run : aucune ligne `PENDING`,
  aucune ligne décidée deux fois — violation ⇒ échec du run.
- **Test d'acceptation** : d'abord tenter d'exhiber le trou (producteur
  levant `CorrectionError` en pleine réconciliation) — si le test
  démontre des lignes non terminales, il devient le test du fix ; sinon
  l'invariant reste et le test documente qu'il tient.
- **Taille** : S–M. **Fichiers** : `core/pipeline.py`, test nouveau.

### P1.6 Hiérarchie d'erreurs : les erreurs provider sous la racine commune
- **Défaut** : `errors.py:3-4` promet « une racine unique au-dessus de
  toute erreur levée par la bibliothèque », mais `ProviderTransientError`
  et `ProviderPermanentError` héritent d'`Exception`
  (`core/protocols.py:29`, `core/protocols.py:56`) et
  `ProviderPermanentError` propage hors de `run()` (`pipeline.py:1076`).
- **Correctif** : reparenter les deux erreurs provider sous
  `CorrectionError` (branche `ProviderError`). Ajouter à chaque erreur un
  code machine stable et `retryable: bool`. Documenter dans `errors.py`
  la hiérarchie complète réelle. (Le renommage éventuel de
  `ValidationError` — collision mentale avec Pydantic — attend P3.11,
  avec les autres ruptures d'API.)
- **Test d'acceptation** : test structurel — toute exception publique est
  sous la racine ; `run()` ne peut lever que des membres de la taxonomie
  (+ `CorrectionAborted`).
- **Taille** : S. **Fichiers** : `errors.py`, `core/protocols.py`,
  `core/pipeline.py`, ADR-008 (mise à jour).

### P1.7 Liste blanche des erreurs récupérables (révision ADR-008)
- **Défaut** : `_PROGRAMMING_ERROR_TYPES` (`pipeline.py:96-105`) est une
  liste **noire** de 8 types ; tout le reste — y compris un
  `RuntimeError` inattendu d'un producteur — dégrade en
  retry-puis-fallback OCR (`pipeline.py:1616`). Le choix est documenté
  (le cœur provider-agnostique ne peut pas nommer les exceptions
  httpx/SDK) mais une exception inconnue ne doit jamais devenir un succès
  dégradé.
- **Correctif** : inverser en liste **blanche** : seuls
  `ProviderTransientError`, `ValidationError` (+ sous-classes, dont
  `HyphenIntegrityError`) et `json.JSONDecodeError` sont récupérables ;
  tout le reste est fatal. **Conséquence contractuelle** : chaque
  provider doit encapsuler ses erreurs de transport en
  `ProviderTransientError` — les providers du backend
  (`backend/app/providers/`) sont mis à jour dans la même PR. Dépend de
  P1.6 (la taxonomie doit exister avant d'être exigée).
- **Test d'acceptation** : producteur levant `RuntimeError` ⇒ le run
  échoue ; provider levant une transiente encapsulée ⇒ retry puis
  fallback comme avant. Matrice d'erreurs provider (401/404/429/5xx/
  timeout/JSON tronqué) vérifiée par provider.
- **Taille** : M. **Fichiers** : `core/pipeline.py`,
  `backend/app/providers/*`, ADR-008 (révision), tests providers.

### P1.8 Renouvellement de l'URL SSE (et cas `JOB_TIMEOUT_SECONDS=0`)
- **Défaut** : la signature `events` est mintée une fois pour
  `DEFAULT_JOB_TIMEOUT_SECONDS + 600` (`jobs.py:471-472`) ; quand le
  timeout vaut 0 (= désactivé, `runner.py:109`) la signature ne dure que
  600 s — reconnexion SSE impossible après 10 min, le polling prend le
  relais mais le flux live n'est plus restaurable.
- **Correctif** : route authentifiée par `X-Job-Token`
  `GET /api/jobs/{id}/events-url` qui re-minte une signature courte à la
  demande. Le frontend la rappelle avant chaque (re)connexion SSE au lieu
  de réutiliser l'URL de création. Les signatures restent courtes dans
  tous les cas — plus de dépendance au budget du run.
- **Test d'acceptation** : signature expirée + token valide ⇒ nouvelle
  URL ⇒ flux rouvert. Token invalide ⇒ 403.
- **Taille** : S–M. **Fichiers** : `backend/app/api/jobs.py`,
  `backend/app/api/signed_urls.py`, `frontend/src/api/client.ts`,
  `frontend/src/hooks/useJobStream.ts`.

### P1.9 Course polling/SSE : identifiant de génération
- **Défaut** : le reconnect manuel annule le *timer* de polling
  (`useJobStream.ts:590-593`) mais pas la requête `fetchJobStatus` en
  vol ; à sa résolution, `useJobStream.ts:487` re-arme le polling sans
  vérifier qu'un flux live a repris — deux transports tournent en même
  temps.
- **Correctif** : compteur de génération (`genRef`) incrémenté à chaque
  transition de transport (nouveau job, reconnect, cleanup) ; chaque
  continuation de `poll()` vérifie sa génération après chaque `await` et
  s'arrête si elle est périmée.
- **Test d'acceptation** : vitest, timers factices — poll en vol +
  reconnect manuel ⇒ aucun poll re-programmé après le retour du flux.
- **Taille** : S. **Fichiers** : `frontend/src/hooks/useJobStream.ts`,
  `useJobStream.test.tsx`.

### P1.10 Téléchargement en streaming côté navigateur
- **Défaut** : `client.ts:169` fait `await resp.blob()` — le fichier
  complet repasse en mémoire navigateur, annulant le streaming du
  backend pour les gros résultats.
- **Correctif** : route token-gated qui minte une URL signée de
  téléchargement à usage court (purpose `download`, TTL court, artefact
  inclus dans le HMAC — même mécanique que `events`/`images`) ; le
  navigateur navigue dessus et streame nativement. `resp.blob()`
  disparaît. Les jetons ne circulent toujours jamais en URL (le `?sig=`
  scopé n'est pas le token de capacité — règle existante).
- **Test d'acceptation** : e2e — téléchargement via URL signée, header
  `Content-Disposition` correct, signature expirée ⇒ 403.
- **Taille** : M. **Fichiers** : `backend/app/api/jobs.py`,
  `backend/app/api/signed_urls.py`, `frontend/src/api/client.ts`,
  `docs/API.md` + snapshot OpenAPI.

### P1.11 SBOM depuis un environnement propre
- **Défaut** : `publish-corrigenda.yml:144-146` installe `pip-audit`
  **dans** le venv de smoke-install puis génère la SBOM de ce même venv :
  la SBOM contient pip-audit et ses dépendances, qui ne font pas partie
  de corrigenda. Le commentaire du workflow prétend un « CLEAN venv ».
- **Correctif** : outil SBOM exécuté depuis son propre venv, inspectant
  le venv cible de l'extérieur (ex. `cyclonedx-py environment
  /tmp/wheel-smoke`). Vérification automatique post-génération : la SBOM
  contient `corrigenda` et ne contient **pas** l'outil SBOM.
- **Test d'acceptation** : assertion `jq` dans le workflow (présence /
  absence de composants attendus).
- **Taille** : S. **Fichiers** : `.github/workflows/publish-corrigenda.yml`.

### P1.12 Publier exactement l'artefact testé par la CI
- **Défaut** : la CI verte est vérifiée pour le SHA
  (`publish-corrigenda.yml:100-116`) mais le workflow **reconstruit** la
  wheel (`publish-corrigenda.yml:121-122`) : octets testés ≠ octets
  publiés.
- **Correctif** : la CI du commit construit wheel + sdist, les teste
  (smoke-install existant), publie leurs SHA-256 et les stocke en
  artefacts GitHub. Le workflow de publication télécharge l'artefact du
  run vert exact, vérifie les checksums, publie **sans rebuild**.
  Garantie : octets testés = octets attestés = octets publiés.
- **Test d'acceptation** : le workflow échoue si aucun artefact n'existe
  pour le SHA tagué ou si les checksums divergent.
- **Taille** : M. **Fichiers** : `.github/workflows/ci.yml`,
  `.github/workflows/publish-corrigenda.yml`.

### P1.13 Corpus externe Gallica : exigences minimales
- **Défaut** : `tests/external_corpus/fetch.py:75` retourne succès dès
  qu'**une** page sur sept est récupérée ; fichiers manquants ignorés ;
  aucune vérification d'intégrité — le job valide surtout « rien n'a
  grossièrement cassé ».
- **Correctif** : checksums attendus dans `manifest.json` ; échec si
  moins de N pages (N = total − tolérance explicite) ; sous-ensemble
  épinglé **hors réseau** (2-3 fichiers committés) qui parse
  obligatoirement et bloque les merges ; un changement de checksum
  distant (ré-OCR Gallica) produit une alerte explicite, pas un faux
  vert.
- **Taille** : S–M. **Fichiers** : `tests/external_corpus/fetch.py`,
  `manifest.json`, `.github/workflows/ci.yml`.

---

## Phase 2 — Honnêteté de version (immédiat)

- **Défaut** : `__init__.py:105` affiche `1.0.0` et `pyproject.toml:20`
  le classifier `Development Status :: 5 - Production/Stable`, alors que
  V4.5 (revue externe) est explicitement requis avant le tag et que P3
  va casser l'API délibérément.
- **Correctif** : version `0.9.0`, classifier
  `Development Status :: 4 - Beta`. La série 0.9.x porte P1 + P3 (ruptures
  libres, CHANGELOG discipliné) ; `1.0.0rc1` n'apparaît qu'à l'entrée de
  P5, l'API gelée ; `1.0.0` après revue externe. Rien n'ayant été publié
  sur PyPI, la rupture est sans victime — c'est maintenant qu'elle est
  gratuite.
- **Taille** : XS. **Fichiers** : `src/corrigenda/__init__.py`,
  `pyproject.toml`, `CHANGELOG.md`.

---

## Phase 3 — Refonte du cœur (ordre strict pour 3.1 → 3.4)

Objectif : supprimer les **causes** qui rendent les protections actuelles
nécessaires — mutation du manifest, identités partielles, césures par
pointeurs, validations dispersées, effets de bord dans le moteur. Chaque
étape laisse la suite complète verte (dont P0) ; une étape qui exige de
désactiver des tests pour passer est une étape mal découpée.

### P3.1 `LineRef` (et `PageRef`) obligatoires partout — ADR-009
- Dataclass frozen/slots `(source_id, page_id, line_id)` ; toutes les
  opérations, traces, partenaires de césure, voisins et erreurs portent
  un `LineRef` complet ; plus aucun dict indexé par `line_id` nu, plus
  aucune chaîne composite `"page:line"` fabriquée à la main.
- **Inclut la correction des images multipages** : `source_images` est
  aujourd'hui indexé par fichier source (`core/protocols.py:138-159`) —
  un XML multipage partage une image pour toutes ses pages. Ré-indexer
  par `PageRef` ; `require_source_images` vérifie la couverture par page.
- Mécanique, gros diff, zéro changement de comportement. Propriété
  nouvelle : deux fichiers aux IDs identiques traversent tout le pipeline
  sans collision (le frontend l'a déjà, la lib doit l'avoir par types).
- **Taille** : L (mécanique).

### P3.2 Unités de correction atomiques : `HyphenGroup` — ADR-010
- `CorrectionUnit = SingleLineUnit | HyphenGroup` (membres ordonnés,
  caractère de césure source, origine explicite/heuristique, contraintes
  de projection). Une chaîne PART1→BOTH→PART2 est UNE unité, pas trois
  lignes reliées par quatre pointeurs.
- Le planner planifie des **unités** : l'atomicité des paires devient
  vraie par construction — les vérifications de non-séparation dans le
  planner et le validator disparaissent au lieu d'être testées. La
  réconciliation est appelée une fois par unité ; un fallback couvre
  toute l'unité ; la propagation des reverts « jusqu'au point fixe »
  disparaît ; `BOTH` devient un détail dérivé.
- **Portique** : propriétés césure de P0 + tests Hypothesis renforcés
  (chaînes, inter-pages) écrits AVANT l'étape.
- **Taille** : L.

### P3.3 Passe globale unique de cohérence — ADR-010 (suite)
- Collecter toutes les décisions provisoires ⇒ séquence globale en ordre
  de lecture (fournie par l'adaptateur de format — pas de moteur de
  reading-order générique, la séquence canonique de `LineRef` suffit) ⇒
  une seule passe O(n) d'adjacence ⇒ rejets propagés par identifiant
  d'unité ⇒ décisions finales.
- Supprime : `accepted_snapshot`, `finalized_owner`, `finalize_seq`, la
  passe intra-chunk dupliquée, la passe de frontières de chunks
  (`pipeline.py:1103-1131`) et la passe de coutures de pages
  (`pipeline.py:848-863`).
- **Portique** : P0.1 (invariance au chunking) est LE test de cette
  étape — la passe globale doit la rendre trivialement vraie.
- **Taille** : L.

### P3.4 Source immuable, `DecisionSet`, moteur sans effets de bord — ADR-011
- `SourceDocument`/`SourcePage`/`SourceLine` immuables ; le moteur
  produit `ProposalSet` → `DecisionSet` (immuable) → `CorrectionResult`
  (artefacts calculés : XML par fichier, rapport, EditScript, métriques).
  Le document source n'est **jamais** modifié ; `run()` ne mute plus son
  entrée.
- `OutputWriter`, `apply=` et la promotion staging quittent le cœur : le
  moteur retourne des octets, `result.write(dir)` est un helper hors
  moteur ; la transaction de fichiers reste au serveur
  (`backend/app/jobs/runner.py`), qui la possède déjà.
- Supprime : `_running` (le moteur devient frozen et réentrant — deux
  runs concurrents sur la même instance fonctionnent), le re-parse final
  du XML réécrit (le rewriter retourne `RewriteResult(xml_bytes,
  texts_by_ref, losses)` — l'invariant P1.4 se vérifie sans seconde
  analyse), les compteurs redondants du manifest (`total_pages/blocks/
  lines` deviennent des propriétés calculées).
- Le test « deux runs sur copies » de P0 devient « deux runs sur le même
  document » ; ADR-005 (one run per instance) est retiré/remplacé.
- **Taille** : XL — l'étape la plus invasive, rendue beaucoup plus petite
  par 3.1–3.3.

### P3.5 Rapport v2 : source → proposal → decision → projection
- `LineOutcome` par unité : texte source, proposition, décision (statut,
  motifs structurés, métriques calculées une fois — `ProposalFeatures`),
  texte final, projection (statut, pertes attribuées, texte ré-extrait).
  Remplace les ~5 copies de texte par ligne de `LineTrace` ; renomme
  `output_alto_text` (faux dans une lib ALTO+PAGE, `schemas.py:777`).
- Schéma JSON du rapport **versionné indépendamment** de l'API Python ;
  règles de compatibilité documentées. Les niveaux de rapport
  (SUMMARY/DECISIONS/FULL) attendent une mesure mémoire réelle — pas
  dans cette étape.
- **Taille** : M.

### P3.6 Événements typés ; séparation moteur/serveur
- `EngineEvent` (dataclasses typées : `ChunkStarted`, `UnitRejected`,
  `RunCompleted`…) dans la lib ; `PipelineEventType`
  (`core/schemas.py:57`) perd les valeurs serveur (`QUEUED`, `KEEPALIVE`,
  `STARTED`/`FAILED`/`CANCELLED` de job) qui migrent dans
  `backend/app/jobs/` ; la couche SSE possède ses événements de
  transport. Le contrat de câblage (`test_sse_event_contract.py`) est
  mis à jour et reste le garde-fou frontend.
- **Taille** : M.

### P3.7 Contrat producteur minimal
- `produce()` reçoit `ProducerOptions(attempt, temperature, deadline,
  cancellation)` — plus de `RetryPolicy` complet (le moteur possède la
  stratégie de retry/downgrade). Le token d'annulation permet enfin de
  couper un appel HTTP en vol (aujourd'hui `should_abort` n'est observé
  qu'entre chunks — limitation documentée de `errors.py:70-71`).
- Scission `StructuredCompletionClient` / `ModelCatalog` : le cœur ne
  dépend que du premier ; `list_models()` appartient à l'application.
- Renommages génériques : `LLMUserPayload`/`LLMLineInput`/`LLMLineOutput`/
  `LLMResponse` → `CorrectionRequest`/`LineContext`/`LineProposal`/
  `ProposalBatch` ; `ProducerMetadata(name, version, implementation,
  configuration_fingerprint)` remplace `provider_name`/`model` nus (un
  producteur de règles n'a pas de « modèle »). Les schémas purement LLM
  vivent dans `corrigenda.integrations.llm`.
- **Taille** : M–L.

### P3.8 `LossPolicy` : strict d'abord, configurable ensuite — ADR-012
- Le rewriter PAGE **compte déjà** ses pertes (`formats/page/rewriter.py:
  70-94` → `format_losses`) : l'état actuel ≈ mode REPORT. Ajouter :
  `strict: bool` (STRICT : une correction improjetable sans perte est
  rejetée — l'unité retombe au texte source avec motif, cohérent avec la
  philosophie fallback existante) et **l'attribution par décision** (quels
  mots perdus, pour quelle ligne) au lieu des compteurs agrégés.
  Défaut : REPORT (comportement actuel, désormais explicite). Pas de
  troisième mode tant qu'un besoin réel ne l'exige pas.
- **Taille** : M.

### P3.9 Provenance complète
- Étendre le fingerprint existant (4 policies) : digest du document
  source, digest du prompt/configuration producteur,
  `ProducerMetadata`, paramètres de génération, version de l'adaptateur
  de format, version du schéma de rapport, versions des dépendances
  critiques (lxml, pydantic). Provenance générique — pas de champs
  `provider`/`model` artificiels pour un producteur de règles.
- **Taille** : S–M.

### P3.10 Préconditions des `EditScript`
- Le script enregistre : digest du document source, digest de chaque
  ligne ciblée, version du protocole, `LineRef` complets. `apply()` sur
  un document qui ne correspond pas ⇒ échec explicite, jamais de
  modification d'une ligne au même ID mais au contenu différent.
- **Taille** : S.

### P3.11 API publique minimale
- `corrigenda.__init__` réduit à ~8 symboles : `load`, `correct`,
  `correct_sync`, `CorrectionEngine`, `CorrectionResult`,
  `CorrectionPolicy`, `CorrigendaError` (racine renommée — c'est ici que
  `CorrectionError`→`CorrigendaError` et le renommage de
  `ValidationError` ambigu se font, avec alias de dépréciation le temps
  de la 0.9.x), `__version__`. Tout le reste par sous-modules
  (`formats.alto`, `formats.page`, `editing`, `policies`, `producers`,
  `reporting`, `integrations.llm`). Chaque export de premier niveau est
  un engagement SemVer — le tri se fait maintenant, avant le gel.
- **Taille** : M (mécanique + migration des tests).

### P3.12 Chemin heureux en trois lignes
- ```python
  document = corrigenda.load("page.xml")        # format par namespace
  result = await corrigenda.correct(document, producer=producer)
  result.write("out/")
  ```
  Observer, writer, manifest, adaptateur : optionnels, plus jamais requis
  pour le cas simple. Quickstart réécrit autour de ce chemin ; exécuté en
  CI sur ALTO et PAGE (P1.3 a posé la base).
- **Taille** : S–M (tout le travail est fait par 3.4/3.11).

### Simplifications de policies (fil rouge de P3, pas une étape)
- `AcceptancePolicy.decide(unit, proposal, context) -> Decision` unifie
  les gardes dispersées ; `ProposalFeatures` immuable calcule chaque
  métrique (similarités, croissance, risques) une seule fois.
- `GuardConfig` : trois profils nommés (`strict`/`balanced`/
  `permissive`), seuils fins relégués en configuration avancée. La
  **calibration** des profils et la remise en cause de la rampe de
  température des retries (0.0→0.3→0.5 — de l'aléa en plus après un
  échec est une hypothèse, pas un fait) attendent P4 : on ne change pas
  les défauts sans benchmark.

---

## Phase 4 — Preuve de qualité (en parallèle de P3)

Sans cette phase, la seule chose démontrée est « corrigenda ne casse
rien ». C'est elle qui change le statut du projet.

### P4.1 Corpus vérité terrain
- 10–20 pages stratifiées : ALTO + PAGE ; livre + presse ; une et
  plusieurs colonnes ; césures explicites, heuristiques, inter-pages, en
  chaîne ; français ancien et moderne. Transcription de référence relue
  humainement. Source naturelle : les pages Gallica déjà épinglées +
  transcriptions manuelles versionnées (`tests/corpus_gt/`).
- **Taille** : M–L (le coût est humain, pas technique).

### P4.2 Métriques et benchmark versionné
- `scripts/benchmark.py` : CER/WER avant/après, taux d'amélioration ET de
  dégradation, faux positifs (lignes correctes modifiées), exactitude des
  césures, pertes structurelles, latence/page, mémoire max. Producteur
  LLM rejoué depuis des cassettes enregistrées (déterministe, exécutable
  en CI) + producteur de règles.
- Rapport comparable par release : version lib, version corpus, policy,
  producteur, métriques. **Règle : aucun défaut (seuils de gardes,
  rampe de température) ne change sans amélioration mesurée ici.**
- **Taille** : M.

### P4.3 Renforcement Hypothesis (avant/pendant P3.2)
- Chaînes PART1→BOTH→PART2 ; césures inter-pages ; plusieurs pages par
  fichier ; IDs identiques entre fichiers ; corrections sur lignes avec
  césure ; variations d'encodage ; documents vides/partiels ; équivalence
  `DecisionSet` ↔ `EditScript` ↔ texte ré-extrait (post-P3.4).
- **Taille** : M, étalée.

### P4.4 Mesure Pydantic (décision, pas migration)
- Benchmark mémoire/temps par tranche de 100 000 lignes sur les modèles
  runtime. Migration éventuelle (dataclasses frozen+slots en interne,
  Pydantic aux frontières JSON) **uniquement si la mesure l'exige** —
  sinon l'item est clos par le chiffre.
- **Taille** : S (mesure) ; migration hors plan tant que non justifiée.

---

## Phase 5 — RC, revue externe, 1.0

1. **Documentation générée** (mkdocs + mkdocstrings, exécutée en CI) :
   API publique, erreurs, policies, formats ; page « garanties et
   non-garanties » (ce qui ne change jamais / peut être approximé / peut
   être supprimé selon la policy / constitue un fallback vs un échec) ;
   matrice de conservation ALTO/PAGE par version ; recettes : ALTO
   simple, PAGE simple, multipage, règles, LLM custom, dry-run, replay
   d'EditScript, mode strict, annulation.
2. **Gel candidat** : `1.0.0rc1` sur TestPyPI via le workflow durci
   (P1.11/P1.12).
3. **V4.5 — revue humaine externe** (bloquant, inchangé depuis le plan de
   remédiation) : un profil Python/packaging, un profil OCR/ALTO/PAGE, au
   moins un utilisateur hors développement. Corrections → `rc2` si
   nécessaire.
4. **`corrigenda-v1.0.0`** : classifier repasse à Production/Stable, tag,
   publication PyPI, benchmark P4.2 joint à la release.

---

## Hors périmètre 1.0 (délibérément)

Reportés en 1.x, **sur besoin mesuré uniquement** — chacun ajoute de la
capacité, aucun ne rend le cœur plus petit ou plus démontrable :
chunking token-aware, budgets de coût, concurrence bornée
(`max_in_flight`), cache de corrections, resolvers IIIF, mutation
testing systématique (une passe ponctuelle sur `hyphenation`/`editing`/
`guards` peut informer P4.3, sans gate CI), niveaux de rapport
(attendent la mesure P4.4), formats supplémentaires, typage strict
complet du backend (piste continue côté app, jamais bloquante pour la
lib).

Inchangé : **Vague 5 institutional** (SQLite → ownership → quotas →
OIDC…) reste sur déclencheur réel, hors de la bibliothèque. Les
améliorations « app de démo » au-delà des fiches P1.8–P1.10 (suppression
explicite des jobs, flux upload→aperçu→lancement, i18n/a11y frontend)
suivent le même régime : sur besoin, jamais en prérequis de la 1.0 de la
bibliothèque.

## Définition de « terminé »

La 1.0 peut être taguée quand :
1. aucune exception inconnue ne peut produire un succès (P1.7) ;
2. chaque ligne reçoit exactement une décision terminale, vérifiée à
   l'exécution (P1.5) ;
3. le texte ré-extrait de la sortie correspond aux décisions (P1.4) ;
4. le document source est immuable et le moteur réentrant (P3.4) ;
5. toute perte structurelle est refusée ou explicitement rapportée et
   attribuée (P3.8) ;
6. le résultat est indépendant du découpage en chunks (P0.1, rendu
   trivial par P3.3) ;
7. la qualité est mesurée sur vérité terrain et publiée (P4.2) ;
8. l'artefact publié est octet-pour-octet celui testé (P1.12) ;
9. l'API publique tient en ~8 exports et a été revue par des personnes
   externes (P3.11, V4.5) ;
10. l'application de démonstration reste clairement séparée de la
    bibliothèque (acquis des Vagues 1–4, préservé par P3.6).
