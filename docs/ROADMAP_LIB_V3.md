# ROADMAP V3 — corrigenda, bibliothèque de post-correction OCR patrimoniale

> Document de travail issu de la revue externe de juillet 2026 (verdict « NO-GO
> production ») et de sa contre-vérification sur le code. Il consolide ce qui a
> survécu à la vérification, écarte ce qui était périmé ou mal attribué, et
> intègre les décisions d'architecture prises depuis (confiances, QE local,
> routage hybride, aligneur de tokens).
>
> Statut : proposition de feuille de route. Les documents normatifs restent
> `README.md`, `SPECS_LIB_V2.md`, `packages/corrigenda/docs/`, `docs/API.md`.

## Décision de produit

Définition retenue de « bibliothèque complète » : *corrigenda fournit toute la
chaîne de post-correction — règles, LLM texte, VLM, hybride — depuis l'XML
(+ image) jusqu'à une projection sûre et auditable vers ALTO/PAGE, avec
confiances, pertes comptées et états de revue.*

- Le **cœur** reste pixel-blind et léger (pydantic + lxml + httpx).
- Tout le reste vit dans des **extras officiels** (`corrigenda[qe]`,
  `corrigenda[vision]`, `corrigenda[<provider>]`).
- L'**application web reste une démo** : elle illustre la bibliothèque, elle
  n'est pas jugée comme un service de production. Les exigences
  d'infrastructure (base durable, queues, OIDC, quotas, RGPD, SLO) sont hors
  périmètre bibliothèque.

## Règles transversales

Valables pour chaque phase, sans exception :

1. **Additif** : tout ajout au `CorrectionReport` est optionnel et
   rétro-compatible (pas de bump de `report_version` pour un champ optionnel).
2. **Fingerprinté** : toute option de comportement est une `FrozenPolicy`
   intégrée au fingerprint composite (§8.2) — la provenance est structurelle.
3. **Extras** : toute dépendance lourde va dans un extra ; le cœur ne grossit
   pas.
4. **Défauts conservateurs** : les défauts reproduisent le comportement
   actuel ; tout ce qui est nouveau est opt-in.

S'y ajoute la règle déjà en vigueur : chaque correctif est livré avec le test
qui échoue avant lui.

---

## Phase 0 — Assainissement et crédibilité

Objectif : plus aucune affirmation du projet n'est contredite par le code.

- [x] **Docs normatives ↔ code** : `SPECS_LIB_V2.md` et
      `packages/corrigenda/docs/edit-protocol.md` passent à
      `page_images` / `require_page_images` (le code porte le contrat corrigé
      — une image par *page*, keyée par `page_id` unique au document ; ce sont
      les docs qui sont en retard). Balayage des autres dérives.
- [x] **Chemin PAGE du backend** : `corrigenda.formats.loader` (entrée
      générique `(path, name)` + `pairing_policy`, sniff par namespace,
      refus des lots mixtes) remplace l'import direct du parseur ALTO dans
      `backend/app/api/jobs.py` ; la façade `load()` délègue au même module ;
      l'association image lit aussi `Page/@imageFilename` ; messages d'erreur
      corrigés ; tests upload PAGE + mixte (échouaient avant le fix).
- [x] **Provenance — victoires rapides** :
  - [x] `configuration_fingerprint` sur `LLMEditProducer` (hash prompt
        système + schéma de sortie), propagé par `for_provider` ;
  - [x] persistance de `Usage` dans le `CorrectionReport` (additif,
        `None` quand rien n'a été rapporté) ;
  - [x] identifiant de réponse fournisseur (`Usage.response_ids`,
        capté par `extract_usage` : `id` OpenAI/Mistral/Anthropic,
        `responseId` Gemini).
- [x] **Validation XSD** : `corrigenda.formats.validation`, schémas
      officiels embarqués (ALTO v2/v3/v4, PAGE 2013/2019/2024), résolution
      xlink hors-ligne ; diagnostic en entrée (dialecte Transkribus
      documenté), gate en sortie (« aucune violation nouvelle ») ; matrice
      publiée dans `packages/corrigenda/docs/format-support.md`.
- [x] **Tests e2e upload→download** pour ALTO **et** PAGE dans la démo
      (uvicorn réel + fournisseur mocké, sortie PcGts vérifiée).

**Phase 0 : terminée** (2026-07-22). Le câblage applicatif du diagnostic
XSD à l'upload (surfacer les violations dans l'UI) est noté pour la
Phase 5 — la bibliothèque expose déjà tout le nécessaire.

**Critère de sortie** : un fichier PAGE traverse la démo de bout en bout ; les
specs ne contredisent plus l'API ; chaque run est reproductible en principe
(hashes complets dans le rapport).

## Phase 1 — Aligneur et fondations de confiance

Le composant partagé d'abord.

- [x] **Aligneur token-à-token** (`core/alignment.py`) : Levenshtein
      caractère → DP monotone sur les mots ; une correspondance exige de
      l'évidence caractère (jamais de match à similarité nulle) ;
      réordonnancement suspecté **signalé** (`move_suspected`), jamais
      appliqué. Pur, déterministe, zéro dépendance.
- [x] **Chemin lent ALTO aligné** :
  - [x] recyclage de `ID`/`STYLEREFS`/`STYLE` par alignement (plus jamais
        positionnel) ; source stylée non appariée = perte comptée
        (`style_dropped`) ; réordonnancement suspecté surfacé
        (`word_order_suspected` dans le rapport de pertes) ; IDs générés
        dédupliqués contre les IDs recyclés ;
  - [x] politique `token_realign` (`LossPolicy.min_alignment_score`,
        défaut `None` = off) : projection refusée si alignement faible sur
        changement de nombre de mots, ou drapeau de réordonnancement (y
        compris à nombre de mots égal) ; unité de césure atomique ; la
        correction refusée est **préservée** dans le sidecar
        (`CorrectionReport.sidecar` + `sidecar.json` via `write()`).
        Cible ce que les gardes ne voient pas (expansion plausible qui
        passe la similarité source). Fingerprint composite §11 : 
        `55dc80679dd71f94` → `15dc07cba9122106` (champ ajouté, défauts
        inchangés).
- [x] **Canal d'incertitude LLM** (`uncertainty_channel=True`, opt-in) :
      `status: certain|uncertain` obligatoire par ligne + codes de raison
      par token modifié (`confusion_connue`, `mot_du_lexique`,
      `infere_du_contexte`, `conjecture`), **vérifiés côté app**
      (`score_producer_claims` : table de confusions, lexique, existence
      des tokens revendiqués) — une justification fabriquée (0.2) vaut
      MOINS qu'une conjecture avouée (0.3). Le score vérifié voyage sur
      `ReplaceLine.producer_confidence` et alimente la composante
      `producer` du bloc `LineConfidence`. Contrat de base inchangé à
      l'octet quand le canal est off (épinglé).

**Phase 1 : terminée** (2026-07-22).
- [x] **`ConfidencePolicy(drop | report_only | write_wc)`** — défaut
      `drop` (= comportement historique, règle transversale n°4 ; le
      `report_only` de l'idée initiale devient le réglage recommandé des
      hôtes, pas le défaut bibliothèque) ; `write_wc` déclaré mais
      **verrouillé** (lève à la construction) jusqu'à la calibration ;
      hors du fingerprint composite §8.2 tant que `write_wc` n'affecte
      pas les sorties (épinglé par test).
- [x] **Bloc `confidence` multi-composantes sur `LineOutcome`** : `ocr`
      (WC ALTO / conf PAGE désormais préservés par les parseurs dans
      `LineManifest.ocr_confidence`), `producer` (réservé au canal
      d'incertitude LLM), `alignment`, `scorers` nommés, décision agrégée
      avec formule identifiée (`min` des composantes présentes). Jamais de
      score magique unique.
- [x] **Protocole `ConfidenceScorer` + `HeuristicScorer`** dans le cœur
      (`core/confidence.py` : évidence caractère, table de confusions
      classiques, lexique optionnel — zéro dépendance).

**Critère de sortie** : chaque run produit une file de lignes triée par risque
dans le rapport, sans un token de plus dans le cas nominal ; le chemin lent
ALTO n'associe plus jamais un texte à la mauvaise identité de mot.

## Phase 2 — Preuve de qualité

Le chemin critique de la crédibilité. **À faire avant la Phase 4.**

- [x] **Corpus réel gelé** (amorce) : deux paires OCR17+ réelles (Descartes
      1637, La Fayette 1678 — CC-BY) enregistrées dans `tests/corpus_gt/`
      (manifest 0.2.0), OCR Transkribus brut vs vérité terrain humaine ;
      `derive_ocr17.py` réexpose le vrai OCR au niveau ligne. *Reste* : pages
      Gallica stratifiées supplémentaires (presse multi-colonnes), images.
- [x] **Benchmark étendu** : le premier run oracle sur ces pages a débusqué
      un vrai bug (P5 décision≠artefact, corrigé) ; **seuils bloquants en CI**
      (`false_positives == 0`, `lines_degraded == 0` sur corpus réel inclus).
      *Reste* : coût/page LLM réel, latence p95 (dépend d'un cassette LLM).
- [x] **Harnais de calibration ECE/Brier** dans le benchmark, par cas +
      micro-agrégat. Verdict mesuré : confiances mal calibrées sur vrai OCR
      (Brier rules 0.82 / oracle 0.57) — le signal manquant « cette ligne
      est-elle déjà correcte ? » justifie le QE scorer Phase 3. Conditionne
      l'ouverture de `write_wc`.
- [x] **Générateur de données QE** (`scripts/qe_data.py`) : deux sources
      étiquetées token par token — **réelle** (alignement raw↔ref, labels or)
      et **synthétique** (dégradations scriptées `clean → OCR`,
      déterministes par hash, sans RNG). ~730 tokens, ~30 % d'erreurs sur le
      corpus actuel ; actif d'entraînement de la Phase 3.

**Critère de sortie** : on sait dire, chiffres à l'appui, si corrigenda
améliore de vrais OCR — et un plafond de fausses corrections gate chaque
release. *(Atteint sur l'amorce ; à étendre avec plus de corpus réel.)*

## Phase 3 — QE local et routage hybride

La phase qui rend l'ensemble économiquement positif.

- [x] **Seam QE + baseline** (`core/quality.py`, pur, zéro dépendance) :
      protocole `QEScorer` (score pré-LLM du besoin de correction),
      `HeuristicQEScorer` (signaux **orthographe-neutres** uniquement —
      la découverte mesurée : les glyphes archaïques NE sont PAS un signal
      de « à corriger » puisque la vérité terrain les préserve ; distinguer
      un vrai non-mot OCR d'une forme historique valide exige un lexique
      historique ou un modèle → justifie D'AlemBERT).
- [x] **`RoutingPolicy` + `route_line`** (brain pur) : décision par ligne
      `skip` (pas d'appel LLM) / `llm` / `escalate` ; défaut = tout au LLM
      (opt-in).
- [x] **Câblage pipeline** : `CorrectionPipeline(qe_scorer=…,
      routing_policy=…)` — une ligne SKIP est retirée des cibles du chunk
      (reste en contexte, sortie ignorée) ; un chunk entièrement skippé est
      supprimé (aucun appel producteur) ; unité de césure jamais skippée ;
      `CorrectionResult.lines_skipped` mesure l'économie. Défaut off →
      run byte-identique.
- [x] **Comptabilité de coût** : `CorrectionResult.producer_calls`
      (invocations `produce`, retries inclus — le vrai coût par appel) ;
      routing-on vs routing-off sur un même document prouve que l'hybride
      est moins cher, sans contrefactuel fabriqué. *Reste* : tier `rules`,
      exposition dans le benchmark avec comparaison on/off.
- [x] **Extra `corrigenda[qe]`** : `MaskedLMQEScorer` ZÉRO-SHOT derrière le
      protocole `QEScorer`, sur la **pseudo-perplexité masquée** (Salazar
      2020) de D'AlemBERT (`pjox/dalembert`, Apache-2.0) — pas
      d'entraînement, un token que le modèle de langue d'époque juge
      improbable est une erreur OCR probable. Runtime **onnxruntime +
      tokenizers, PAS torch** (conversion torch→ONNX en dev-time,
      `scripts/export_masked_lm_onnx.py`) ; imports lourds paresseux, cœur
      jamais contaminé (test de contrat). Découverte ÉTAPE 0 : D'AlemBERT
      connaît la graphie d'époque (`eſt`/`auoir` tokens natifs) mais porte
      une pénalité TYPOGRAPHIQUE long-s → on note une **copie dé-glyphée**
      (`ſ→s`, ligatures), texte du document jamais modifié (règle 3).
      Mesuré (`scripts/qe_benchmark.py`) il bat `HeuristicQEScorer` sur
      tout (réel/synth) : AUC token 0.66/0.66 vs 0.50, ECE token 0.04/0.03
      vs 0.32, AUC ligne 0.77/0.88 vs 0.50. *Reste (ÉTAPE 4, optionnel)* :
      fine-tuning d'une tête QE type ELECTRA sur les données Phase 2 si le
      zéro-shot devient insuffisant.
- [x] **QE agnostique au modèle + adaptatif au registre** : le même
      `MaskedLMQEScorer` pilote n'importe quel masked LM ; chaque bundle
      ONNX est AUTO-DESCRIPTIF (manifeste = constantes Platt + `word_reducer`
      + `line_reducer`), donc `model_dir` sur le bon bundle charge la bonne
      calibration. Mapping mots par offsets (robuste WordPiece/SentencePiece,
      graphie d'origine préservée). Pour la **presse fin 19e** : CamemBERT
      (`camembert-base`, MIT) + `line_reducer="mean"` — AUC token 0.98,
      score ligne propre/OCR 0.14/0.51 (le `mean` bat le `max` : les noms
      propres de presse font pointer quelques mots propres qu'un modèle
      contemporain a peu vus). Outils : `scripts/export_masked_lm_onnx.py`,
      `scripts/fit_qe_calibration.py` ; doc `docs/qe-scorer.md`. Anticipe le
      registre `ModelCapabilities` de la Phase 4 (router le bon modèle vers
      la bonne matière). *Reste* : calibration 19e provisoire (à refitter
      sur corpus Gallica presse réel, Phase 2).
- [ ] **Comptabilité de coût** dans le rapport : tokens économisés par le
      gate vs dépensés par l'escalade — l'hybride doit *prouver* qu'il est
      moins cher.
- [ ] **Ouverture de `write_wc`** (opt-in) : seulement si la calibration
      passe le seuil ; écriture avec `postProcessingStep` déclaré côté ALTO,
      `TextEquiv` multiple côté PAGE (lecture OCR conservée avec sa
      confiance). **Jamais de CC fabriqués.**

**Critère de sortie** : sur le corpus gelé, l'hybride fait au moins aussi bien
que le tout-LLM pour un coût par page inférieur, mesuré et publié.

## Phase 4 — La chaîne vision

Le grand chantier de la revue, enfin outillé. Dépend de la Phase 2.

- [x] **`ImageAsset` structuré** (page_id, uri, sha256, MIME réel, dimensions
      pixels, index de frame, orientation EXIF, transformation XML→pixels via
      `ImageTransform`). `ImageRef = str` reste accepté ; `ImageAsset` devient
      le contrat recommandé — `run(page_images=…)` accepte l'union
      `PageImage = ImageRef | ImageAsset`, l'asset voyage sur l'enveloppe §4.1
      **verbatim** (le cœur n'ouvre aucun pixel, I4), et `require_page_images`
      refuse un `ImageAsset` déposé sous une clé ≠ son `page_id`. Additif,
      opt-in, byte-identique sans vision. *Reste* : le builder qui **peuple**
      un `ImageAsset` depuis un fichier = extra `[vision]` (item suivant).
- [ ] **Extra `corrigenda[vision]`** : décodage/validation d'images (Pillow),
      TIFF multipage, crops ligne/bloc/page avec marge configurable, polygones
      PAGE et rotation, association page→image robuste avec préflight
      (« 100 % des pages ont une image ou une erreur claire »).
- [ ] **`VisionEditProducer` officiel** : encode le crop, appelle le
      fournisseur multimodal, trace hash de l'image et du crop. Le cœur reste
      pixel-blind — l'enveloppe §4.1 existante est la couture.
- [ ] **Gardes vision** (profil `GuardConfig.vision()` réservé dans le code) :
      anti-hallucination visuelle, repli vers OCR ou revue si image
      absente/ambiguë.
- [ ] **Registre `ModelCapabilities`** (text/vision/structured_output/
      max_images/context) alimentant le router — le VLM n'est qu'un producteur
      de plus, routé vers les seules lignes où il vaut son coût.
- [ ] **Benchmark texte vs vision vs hybride** : le VLM doit battre le texte
      seul sur le corpus gelé pour mériter sa place par défaut.
- [ ] `CandidateSet` émergera ici, du besoin concret de candidats concurrents
      (règles/texte/vision) — pas avant.

## Phase 5 — Revue humaine et intégrations

La bibliothèque fournit les états, pas l'UI.

- [ ] **États de revue de première classe** : `review_required` alimenté par
      les gardes, le router, `token_realign` et la `LossPolicy` ; format
      sidecar versionné ; modèle de données brouillon→approbation (décision,
      auteur, horodatage, justification) dans le rapport.
- [ ] **Extras fournisseurs** : remonter les adaptateurs de la démo en
      `corrigenda[openai|anthropic|mistral|google]` avec déclarations de
      capacités.
- [ ] **Écosystème patrimonial** : METS/ALTO, manifestes IIIF, CLI simple
      (`corrigenda correct *.xml --producer=hybrid`).
- [ ] **Politique de versionnement / compatibilité d'API** publiée ; guide de
      déploiement honnête pour la démo.

## Écarté explicitement

- Les exigences SaaS de la revue v1 (base durable, queues, OIDC, quotas,
  RGPD, SLO) : légitimes pour un service, hors périmètre bibliothèque.
- L'accusation de provenance sur la politique de pairing : déjà corrigée
  (`jobs.py` → `runner.py` → `for_provider(pairing_policy=…)`, testé par
  `backend/tests/test_pairing_fingerprint.py`).
- La refonte spéculative `DocumentIR`/`CandidateSet`/`ProjectionPlan` en
  amont du besoin (voir Phase 4).
- Les logprobs comme socle de confiance (hétérogénéité fournisseurs) — au
  mieux un enrichisseur optionnel par provider.
- L'auto-consistance ×N par défaut (stratégie d'escalade, pas mécanisme de
  base).
- Toute fabrication de CC.

## Critères d'acceptation finaux

1. Aucun fallback silencieux ALTO/PAGE nulle part ; toute page a une image
   explicite ou une erreur claire (Phase 4).
2. Identifiants, ordre et géométrie des lignes invariants ; en mode strict,
   ceux des mots aussi ; toute perte comptée par ligne et par page.
3. Tout changement non projetable part en sidecar ou en revue — jamais
   reconstruit en silence.
4. Toutes les sorties passent la validation XSD.
5. Chaque décision est reproductible : hashes (source, image, crop, prompt,
   schéma), modèle, politiques fingerprintées, usage, coût.
6. Un plafond de fausses corrections est bloquant en CI sur corpus réel
   gelé ; la qualité n'est jamais gagnée au prix de la segmentation.
7. Les confiances écrites dans l'XML sont calibrées, multi-composantes dans
   l'audit, et leur provenance est déclarée dans le document.
8. Le VLM et l'hybride battent le texte seul sur le corpus gelé avant d'être
   des défauts.
