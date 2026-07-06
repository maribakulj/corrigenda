# PLAN — corrigenda 1.0 (release unique)

Décision mainteneur (juil. 2026) : rien n'étant publié, **tout le périmètre
du plan (ex-« V2 complète ») sort comme LA v1.0 de `corrigenda`** — pas de
staging publié v1.0→v1.1→v2.0. Le contrat reste `SPECS_LIB_V2.md` ; les
numéros de version internes de la spec (§13) deviennent des PHASES d'une
même release. Version de travail : `0.1.0a1` jusqu'au tag final `1.0.0`.

## Phases (chacune committée verte ; état à jour ici)

- [x] **P0 — Décisions & corpus** : nom **corrigenda** (validé, renommage
  exécuté) ; corpus PAGE réels acquis (`examples/page/` : OCR17plus
  triplets + NewsEye-FR presse en colonnes ; lacunes listées dans
  PROVENANCE.md). Restes mainteneur : renommer le dépôt GitHub, réserver
  le nom sur PyPI, ratifier les 3 décisions de PROGRESS_V1.md.
- [x] **P1 — Réorganisation §3** : arbre `core/` (schemas, guards,
  hyphenation, planner, validator, pipeline, protocols) / `formats/alto/`
  (parser, rewriter, _ns, _text — la *détection* de césure y reste) /
  `producers/` (llm.py : SYSTEM_PROMPT + OUTPUT_JSON_SCHEMA). `_norm` et
  `errors` purs côté core/racine. **Test-contrat d'imports** : `core`
  n'importe ni lxml ni formats/producers (import de `corrigenda.core.*`
  ne charge pas lxml — vérifié par sous-processus) ; DEUX exceptions
  pinnées, function-local dans core/pipeline : défaut ALTO lazy et défaut
  prompt/schéma lazy (frontière de composition ; prompt/schéma désormais
  INJECTABLES sur le pipeline). Init racine lazy (PEP 562) pour les
  symboles formats/producers.
  Pas d'alias (rien de publié) : lib tests + backend migrent aux chemins
  définitifs. DoD : goldens byte-parity INTACTS (pur déplacement), suites
  vertes, mypy strict, contrat d'imports vert.
- [x] **P2 — Couture de format** (fusionnée avec P1) : port `FormatAdapter`
  (rewrite_file/extract_texts) dans core/protocols ; `AltoFormatAdapter`
  dans formats/alto ; le pipeline n'importe plus le rewriter ALTO.
- [x] **P3 — PAGE XML** (spec §6.2 P1–P7 + parité §6.3) : `formats/page/`
  (`_ns`, `_text`, `_custom`, `parser`, `rewriter`, `adapter`).
  Polygones conservés verbatim sur `Coords.polygon`, bbox englobante
  dérivée pour le planner (P1) ; texte canonique P2/P3 (TextEquiv `@index`
  minimal, repli concat des Word) ; césure heuristique `- ¬ ⸗ U+00AD`
  (`core.pairing.trailing_hyphen_char`, détection BOTH chaînée),
  `hyphen_source_explicit=False` systématique (P5) ; réécriture 3 chemins
  UNTOUCHED/fast/slow — jamais de réécriture géométrique (P1) ; P3
  (canonical TextEquiv, drop `@conf`, drop alternatives, MàJ PlainText) ;
  P4 (Word fast/slow, granularité perdue comptée) ; P5 caractère de césure
  préservé (E5 étendu, un swap seul → UNTOUCHED) ; P6 `custom` groupes sans
  offsets préservés verbatim / à offsets retirés+comptés ; P7
  make_safe_parser (contrat grep couvre déjà `formats/**`), provenance
  `MetadataItem` (2019+) sinon `Metadata/Comments`, sans horodatage →
  sortie déterministe. Compteurs de pertes exposés via
  `PageRewriterMetrics.as_losses()` et `CorrectionReport.format_losses`
  (champ **additif** ⇒ `report_version` inchangé "1.0" : le contrat du
  champ est de bumper sur rupture, pas sur ajout). Parité §6.3 prouvée :
  la page LaFayette PAGE parse 13 lignes byte-identiques à son export ALTO4
  (14ᵉ ligne ALTO = réclame, divergence ±1 documentée) ; rôles de césure
  identiques entre variantes raw/corrected ; round-trip identité stable
  (NewsEye 820 lignes, LaFayette). Fixtures synthétiques : `@conf`,
  alternatives, PlainText, `custom` à offsets, ns 2019, ⸗ (corpus réels
  lacunaires). **Note archi** : `make_safe_parser` reste canonique dans
  `formats/alto/_ns` et est réexporté par `formats/page/_ns` (réutilisation
  d'une primitive de sécurité ; nettoyage possible = module `formats/_xml`
  partagé, non bloquant).
- [x] **P4 — Protocole d'édition** (spec §4/§5) : **complet** — substance
  additive + les deux ruptures (option A validée par le mainteneur).
  - [x] `core/editing.py` : ReplaceLine/ReplaceSpan, MatchAnchor→RangeAnchor,
    E1–E6 (E4/E5 sur replace_span uniquement ⇒ E4 neutre sur replace_line) ;
    ré-expression replace_line **PROUVÉE par les goldens** (sample/X0000002).
  - [x] contrat `EditProducer` (wants_geometry/wants_image,
    produce→(EditScript, Usage|None)) ; adaptateur `LLMEditProducer`
    (BaseProvider→replace_line) ; enveloppe vision (`LineGeometry` + `ImageRef`
    opaque, recopiés par le compilateur à la demande) ; `require_source_images`
    (ValidationError si wants_image sans image) ; **test-contrat I4** (zéro
    lib image sous corrigenda, AST).
  - [x] producteur règles `RulesProducer` (§5.3) : ReplaceSpan+RangeAnchor,
    garde lexique optionnelle, zéro dépendance, **byte-reproductible** ;
    `default_french_ocr_rules()` (ſ→s, ligatures).
  - [x] pipeline : réponse LLM ré-exprimée en EditScript replace_line +
    `apply_edit_script` (byte-parité via golden gate) ; **dry-run renvoie
    l'EditScript normalisé** (`CorrectionResult.edit_script`) ; doc protocole
    (`docs/edit-protocol.md`) + surface publique (`corrigenda.__init__`).
  - [x] **Rupture 1 — unification `JobTrace→CorrectionReport`** (§9) :
    `JobTrace` supprimé ; `trace.json` ET l'endpoint `/trace` portent le
    `CorrectionReport` versionné verbatim (`run_id` == job_id) ;
    `JobManifest.report` côté backend ; type `TraceData` frontend aligné
    (tsc propre).
  - [x] **Rupture 2 — résorption §5.1** : `CorrectionPipeline(producer=
    EditProducer)` ; `run()`/`run_sync()` sans api_key/model/provider_name
    (credentials dans le producteur, labels de provenance au constructeur) ;
    `run(source_images=…)` opaque vérifié au démarrage ;
    `CorrectionPipeline.for_provider(...)` = migration en un appel pour le
    cas LLM (2ᵉ exception lazy pinnée du contrat d'imports, remplace
    `_default_llm_contract`) ; la rampe de température reste pilotée par le
    pipeline (policy mono-température par tentative) ⇒ classification retry,
    températures et octets inchangés (goldens verts) ;
    `requires_full_coverage=False` pour les producteurs déterministes
    (pas d'op == pas d'édition) ; test seam : RulesProducer pilote le
    pipeline entier sans credentials, enveloppe vision jusqu'au producteur.
- [x] **P5 — Hygiène & release 1.0.0** : docs (`docs/quickstart.md`,
  `docs/formats.md` — ALTO+PAGE+provenance, `docs/versioning.md` —
  SemVer strict + dépréciation + politique `report_version`,
  `docs/edit-protocol.md` déjà en P4 ; `mkdocs.yml` minimal) ; exemple
  exécutable `examples/quickstart.py` (2 producteurs, offline, gardé par
  un test subprocess) ; **test-snapshot de l'API publique** (54 symboles
  gelés + signatures run/run_sync/for_provider + clés JSON du
  CorrectionReport) ; version **1.0.0** (classifier Production/Stable,
  description ALTO+PAGE), CHANGELOG daté 2026-07-06 (0.1.0a1 marquée
  « internal, never published ») ; build + smoke-install wheel vérifiés.
  **La publication elle-même** (tag `corrigenda-v1.0.0` + workflow
  TestPyPI→PyPI en Trusted Publishing) est une action mainteneur — check-
  list dans PROGRESS_V1.md.

## Hors 1.0 (gated par la spec, ne pas commencer)
Mode span LLM opt-in (banc CER/coût requis), GuardConfig.vision() calibré,
sérialisation seq2seq, remap offsets custom via EditScript — « pas de
consommateur = pas de code » (§12).

## Risques actifs
E4 à défauts neutres (seul point pouvant casser la parité v1) ;
byte-stabilité PAGE (2 namespaces, ordre d'attributs) ; conventions P5/P7
à confirmer sur exports eScriptorium récents (corpus alto4 dispo, export
PAGE eScriptorium manquant).
