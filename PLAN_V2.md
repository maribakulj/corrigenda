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
- [ ] **P1 — Réorganisation §3** : arbre `core/` (schemas, guards,
  hyphenation, planner, validator, pipeline, protocols) / `formats/alto/`
  (parser, rewriter, _ns, _text — la *détection* de césure y reste) /
  `producers/` (llm.py : SYSTEM_PROMPT + OUTPUT_JSON_SCHEMA). `_norm` et
  `errors` purs côté core/racine. **Test-contrat d'imports** : `core`
  n'importe ni lxml ni formats/producers (import de `corrigenda.core.*`
  ne charge pas lxml — vérifié par sous-processus) ; une seule exception
  pinnée : le défaut ALTO lazy du pipeline (frontière de composition).
  Pas d'alias (rien de publié) : lib tests + backend migrent aux chemins
  définitifs. DoD : goldens byte-parity INTACTS (pur déplacement), suites
  vertes, mypy strict, contrat d'imports vert.
- [ ] **P2 — Couture de format** (fusionnée avec P1) : port `FormatAdapter`
  (rewrite_file/extract_texts) dans core/protocols ; `AltoFormatAdapter`
  dans formats/alto ; le pipeline n'importe plus le rewriter ALTO.
- [ ] **P3 — PAGE XML** (spec §6.2 P1–P7 + parité §6.3) : parser/rewriter
  4 chemins formats/page ; polygones conservés (champ polygone sur
  Coords) ; césure heuristique `- ¬ ⸗ U+00AD`, caractère préservé (E5
  étendu) ; custom sans offsets préservé / à offsets retiré+compté ;
  make_safe_parser + extension du test-contrat grep ; provenance
  MetadataItem(2019+)/Comments ; compteurs CorrectionReport (bump schéma) ;
  parité par APPARIEMENT contenu/géométrie (les segmentations des corpus
  divergent de ±1 ligne) ; goldens PAGE. Fixtures synthétiques pour
  @conf, custom à offsets, ns 2019, ⸗ (corpus réels lacunaires).
- [ ] **P4 — Protocole d'édition** (spec §4/§5) : core/editing.py
  (ReplaceLine/ReplaceSpan, MatchAnchor→RangeAnchor, E1–E6 ; E4 neutre sur
  replace_line) ; ré-expression replace_line PROUVÉE par les goldens ;
  contrat `EditProducer` (wants_*, produce→(EditScript, Usage|None)) ;
  adaptateur BaseProvider→EditProducer et résorption du legacy
  run(api_key/model/provider_name) ; `run(source_images=…)` opaque +
  ValidationError si wants_image sans image ; test-contrat I4 (zéro
  PIL/encodage image dans core+formats) ; producteur règles
  (ReplaceSpan+RangeAnchor, zéro dépendance, testé à l'octet) ;
  unification JobTrace→CorrectionReport (rupture trace.json, backend
  ajusté) ; dry-run renvoie l'EditScript normalisé ; doc protocole.
- [ ] **P5 — Hygiène & publication 1.0.0** : docs (mkdocs, quickstart,
  protocole, formats, provenance, politique versionnage/dépréciation),
  exemples exécutables, test-snapshot de l'API publique, CHANGELOG daté
  1.0.0, publication TestPyPI→PyPI `corrigenda`, SemVer strict ensuite.

## Hors 1.0 (gated par la spec, ne pas commencer)
Mode span LLM opt-in (banc CER/coût requis), GuardConfig.vision() calibré,
sérialisation seq2seq, remap offsets custom via EditScript — « pas de
consommateur = pas de code » (§12).

## Risques actifs
E4 à défauts neutres (seul point pouvant casser la parité v1) ;
byte-stabilité PAGE (2 namespaces, ordre d'attributs) ; conventions P5/P7
à confirmer sur exports eScriptorium récents (corpus alto4 dispo, export
PAGE eScriptorium manquant).
