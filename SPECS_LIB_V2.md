# SPEC — bibliothèque d'édition sûre de transcriptions patrimoniales

> **Statut** : proposition v2, issue de la revue de code complète d'`alto-core`
> (commit courant de `alto-llm-corrector`) + pesée du protocole d'édition par
> spans (« C »). Ce document est la **cible** ; le plan de livraison (§13)
> découpe le chemin en tranches dont chacune laisse la lib publiable.

---

## 1. Identité & principes

**Ce que la lib est** : une bibliothèque Python pour **éditer le texte de
transcriptions structurées patrimoniales (ALTO, PAGE XML) via des modèles ou
des règles, sans jamais corrompre la structure**. Parser → représentation
pivot → plan de chunks → production d'éditions ancrées → validation
multi-étages → recomposition minimale du XML d'origine.

**Ce que la lib n'est pas** : un OCR, un segmenteur, un évaluateur, un
visualiseur, un convertisseur universel (§12).

Trois invariants fondateurs, hérités de l'existant et non négociables :

- **I1 — Le texte ne voyage jamais sans son ancre.** Toute donnée envoyée à
  un producteur d'éditions (LLM, règles, modèle spécialisé) porte l'identité
  de sa ligne ; toute édition revient adressée à cette identité. La
  recomposition est une écriture indexée, jamais une recherche d'alignement.
- **I2 — L'application décide, le modèle informe.** Aucune sortie de modèle
  n'atteint le XML sans passer les gardes ; au moindre doute, repli sur le
  texte source. Aucune édition structurelle (fusion/scission/déplacement de
  lignes) n'est représentable dans le protocole — pas seulement interdite :
  **inexprimable**.
- **I3 — La structure d'origine est intouchable.** IDs, géométrie ligne,
  ordre XML, attributs non textuels : préservés à l'octet quand rien ne
  change (stratégie 4 chemins), modifiés au minimum quand le texte change.
- **I4 — La lib est aveugle aux pixels.** Le cœur ne charge, ne découpe et
  n'encode jamais d'image. La correction *guidée par l'image* (VLM) est un
  **producteur** qui reçoit une **référence** d'image opaque ; charger et
  cropper les pixels est la responsabilité de l'implémentation du producteur,
  hors du cœur (§5.2 bis). C'est le corollaire vision de « zéro I/O dans
  `core` ».

Corollaire transverse : **le cœur est agnostique de la modalité**. Validation
1:1, gardes, réconciliation et rewriter ne voient jamais que du texte-in /
texte-out clé par `line_id` — qu'il vienne d'un LLM texte, d'un VLM qui a lu
l'image, ou d'un moteur de règles ne change rien pour eux. C'est ce qui fait
que la correction par VLM « traverse » la lib sans en toucher le cœur.

---

## 2. Décision : intégrer le protocole d'édition par spans (« C »)

### Pesée

**Pour** :
1. Le pipeline actuel EST déjà un cas particulier du protocole : `enrich_chunk_lines`
   compile les manifests en représentation simplifiée, le modèle édite, le
   rewriter recompose par `line_id`. C nomme et généralise l'existant —
   coût de conception marginal, pas d'étage étranger.
2. Une édition ancrée est **plus vérifiable** qu'une réécriture complète :
   existence de l'ancre, borne de taille, non-chevauchement, dérive mesurée
   *par édition*. Les gardes deviennent plus fortes, pas plus faibles.
3. Sorties plus courtes → moins cher, plus rapide, et c'est le format de
   sortie naturel d'un futur modèle spécialisé de post-correction.
4. Le **moteur de règles déterministe** (§5.3) émet nativement des spans :
   le protocole a un premier producteur réel, testable et gratuit dès sa
   livraison — l'objection « pas de consommateur » tombe.
5. Un consommateur qui benche des pipelines de correction consomme ce
   protocole au lieu de construire le sien : un seul lieu de conception.

**Contre** (et mitigations) :
1. *Spéculation* : concevoir le protocole avant le modèle spécialisé qui le
   nourrira. → Discipline des deux axes : l'**enveloppe** (types, ancres,
   recomposeur) est conçue maintenant ; la **surface** arrive
   incrémentalement, le moteur de règles servant de producteur de référence.
2. *Les offsets de caractères sont un piège pour les LLM* (ils comptent
   mal). → Deux modes d'adressage (§4.3) : `match` (sous-chaîne + occurrence,
   robuste pour LLM) et `range` (offsets, pour producteurs déterministes),
   normalisés en `range` par le recomposeur qui rejette l'ambigu.
3. *Sur-abstraction* : si tout devient « document générique + spans », la
   valeur spécifique (césure) se dissout. → La césure reste une logique de
   **paires de lignes** dans le cœur, hors du protocole d'édition ; le
   protocole n'exprime que de l'intra-ligne.
4. *Grossir la v1 retarde la sortie.* → Staging strict (§13) : v1.0 publie
   l'existant corrigé, le protocole vient en v2.0 en **ré-exprimant**
   l'existant (la réécriture de ligne devient l'op `replace_line`).

### Verdict

**Oui — comme enveloppe de la v2, pas comme chantier de la v1.** La v2.0
ré-exprime le pipeline actuel dans le protocole (aucun changement de
comportement) et livre le moteur de règles comme premier producteur de spans
réels. Le producteur LLM d'éditions fines et le modèle spécialisé viennent
ensuite, chacun avec sa preuve.

---

## 3. Architecture cible

```
lib/
├── core/                    # pur : zéro I/O, zéro réseau, zéro lxml
│   ├── schemas.py           # manifests, chunks, protocole d'édition, traces
│   ├── guards.py            # matrice anti-migration (3 étages) + GuardConfig
│   ├── hyphenation.py       # réconciliation de paires (logique TEXTE, format-agnostique)
│   ├── planner.py           # chunk planner (PAGE→BLOCK→WINDOW→LINE, césure-conscient)
│   ├── editing.py           # EditScript : validation, normalisation match→range, application
│   ├── pipeline.py          # orchestration (retry, descente de granularité, traces)
│   └── protocols.py         # EditProducer, PipelineObserver, FormatAdapter
├── formats/
│   ├── alto/                # parser + rewriter ALTO (lxml, durci)
│   └── page/                # parser + rewriter PAGE XML (lxml, durci)
└── producers/
    ├── llm.py               # contrat provider LLM (payload, schémas de sortie, prompts)
    └── rules.py             # moteur de règles déterministe (v2.0)
```

Règle d'import : `core` n'importe rien de `formats` ni `producers` ;
`formats` n'importe que `core` ; `producers` n'importe que `core`. La
détection de césure (qui lit `<HYP>`/`SUBS_*` ou `¬`) vit dans `formats/*` ;
la **réconciliation** (qui ne voit que du texte) vit dans `core` — c'est le
déplacement du `alto/hyphenation.py` actuel, mal rangé sous `alto/` alors
qu'il n'importe pas lxml.

Le découpage actuel (`alto_core.alto`, `alto_core.pipeline`,
`alto_core.protocols`, `alto_core.schemas`) migre vers cet arbre en v2.0 ;
les renommages sont des ruptures assumées de pré-2.0 (§8.5).

---

## 4. Le protocole d'édition par spans

### 4.1 Vue d'ensemble

```
structure (ALTO/PAGE)
   │  parse (formats/*)
   ▼
DocumentManifest ──compile──▶ ModelPayload ──producteur──▶ EditScript
   ▲                              (simplifié,                  │
   │                               ancré)                      │ validate + normalize
   └──────────── recompose (formats/*) ◀── manifests édités ◀──┘
```

Le **compilateur** existe déjà (`enrich_chunk_lines` + payload) ; le
**recomposeur** existe déjà (rewriter 4 chemins) ; la v2.0 insère l'étage
`EditScript` entre les deux et ré-exprime la réponse « ligne entière »
comme une op parmi d'autres.

Le `ModelPayload` porte **optionnellement**, par ligne, deux champs
d'ancrage physique — présents uniquement quand un producteur les demande
(producteur vision, §5.2 bis), ignorés par les producteurs texte :
`geometry` (les `coords` du manifest, déjà disponibles — bbox/polygone +
dimensions de page, de quoi calculer une bbox relative sans connaître
l'unité) et `image_ref` de page (chaîne opaque, jamais ouverte par la lib,
issue du mapping `source_images` passé à `run()`). Le compilateur ne fait
que **recopier** ces champs depuis le manifest et le mapping ; il ne touche
aucun pixel.

### 4.2 Opérations

```python
class ReplaceLine(BaseModel):      # v2.0 — ré-expression de l'existant
    op: Literal["replace_line"]
    line_id: str
    text: str                       # ligne complète corrigée

class ReplaceSpan(BaseModel):      # v2.0 (producteur règles) / v2.1 (producteur LLM)
    op: Literal["replace_span"]
    line_id: str
    anchor: MatchAnchor | RangeAnchor
    text: str                       # remplacement du span uniquement

EditOp = ReplaceLine | ReplaceSpan
class EditScript(BaseModel):
    ops: list[EditOp]
```

**Aucune op structurelle.** Pas de `merge_lines`, pas de `split_line`, pas
de `move_text` : l'invariant I2 est garanti par le type, pas par une
validation.

### 4.3 Ancrage — deux modes, un seul après normalisation

```python
class RangeAnchor(BaseModel):       # producteurs déterministes
    start: int                      # offsets DANS LE TEXTE CANONIQUE de la ligne
    end: int                        # (celui de reconstruct_textline, strippé)

class MatchAnchor(BaseModel):       # producteurs LLM
    match: str                      # sous-chaîne exacte du texte canonique
    occurrence: int | None = None   # None = unicité requise ;
                                    # n explicite (0 = première) = n-ième occurrence
```

Motivation : l'expérience de terrain des formats d'édition LLM montre que
les modèles échouent sur les offsets numériques et les numéros de ligne,
mais sont fiables sur « remplace *cette sous-chaîne* ». C'est la pratique
convergente des outils éprouvés : blocs recherche/remplacement d'aider
(mesurés supérieurs aux diffs à numéros de ligne dans leurs benchmarks
d'edit-formats), commande `str_replace` de l'outil `text_editor`
d'Anthropic (correspondance exacte **et unique** exigée, sinon erreur).
À l'inverse, les systèmes déterministes (moteurs de règles, détecteurs
d'erreurs type ICDAR post-OCR, qui émettent des corrections
positionnelles) calculent des offsets exacts sans effort. Et les modèles
seq2seq fine-tunés ré-émettent naturellement des lignes entières. D'où les
trois formes : `replace_line` (LLM par défaut + seq2seq), `MatchAnchor`
(LLM avancé), `RangeAnchor` (déterministe).

Le recomposeur **normalise** tout `MatchAnchor` en `RangeAnchor` contre le
texte canonique ; un match introuvable ou dont l'occurrence n'existe pas →
op rejetée (repli I2), un match ambigu (plusieurs occurrences sans
`occurrence` explicite) → op rejetée. Le texte canonique de référence est
celui que le parser expose (`ocr_text`), ce qui rend l'adressage
indépendant du format.

### 4.4 Invariants d'un EditScript (validation `core/editing.py`)

- E1 : chaque `line_id` existe dans le chunk visé (jamais hors chunk).
- E2 : spans normalisés d'une même ligne **sans chevauchement**, appliqués
  de droite à gauche (offsets stables).
- E3 : `text` sans `\n`/`\r`, non vide après strip pour `replace_line`
  (un `replace_span` peut être une suppression : `text=""` autorisé si le
  résultat de la ligne reste non vide).
- E4 : bornes de dérive **par op** (`GuardConfig`) : ratio de longueur max
  du remplacement vs span remplacé, budget de caractères réellement
  modifiés par ligne (fenêtre différente après élagage du préfixe/suffixe
  communs — une réécriture à longueur constante coûte sa taille réelle).
- E5 : une ligne de rôle césure (PART1/PART2/BOTH) éditée par span ne peut
  pas voir son mot-frontière supprimé ni son tiret final retiré — mêmes
  gardes qu'aujourd'hui, appliquées au **résultat** de la ligne.
- E6 : après application, le pipeline de gardes existant (matrice 3 étages,
  §7 F-héritées) s'applique au texte de ligne résultant, à l'identique du
  chemin `replace_line`. Le protocole ajoute des gardes, il n'en retire
  aucune.

### 4.5 Ce que le protocole ne transporte pas

La **césure** : les rôles, les paires, la réconciliation restent une affaire
de manifests de lignes (couche cœur), invisibles dans l'EditScript. Un
producteur voit les indices de césure dans le payload (comme aujourd'hui) et
édite chaque ligne séparément ; la réconciliation juge le résultat.

---

## 5. Producteurs d'éditions

### 5.1 Contrat (v2.0)

```python
class EditProducer(Protocol):
    #: Le compilateur inclut geometry + image_ref dans le payload uniquement
    #: si le producteur les réclame — évite d'alourdir un payload texte.
    wants_geometry: bool = False
    wants_image: bool = False

    async def produce(
        self, payload: ModelPayload, *, policy: RetryPolicy
    ) -> tuple[EditScript, Usage | None]: ...
```

À partir de v2.0, `BaseProvider` (LLM) devient **une implémentation** de ce
contrat, pas le contrat lui-même. `Usage` (tokens in/out) remonte au rapport
et au consommateur (qui le mappe sur sa propre comptabilité de ressources) ;
en v1.0 il est déjà remonté par `complete_structured` (F14).

`run()` accepte un mapping optionnel `source_images: dict[str, ImageRef]`
(clé = même identité de source que `source_files`), que la lib **forwarde**
comme référence opaque et **n'ouvre jamais**. Un producteur à
`wants_image=True` sans `source_images` correspondant → `ValidationError`
au démarrage (jamais un appel vision muet sans image).

### 5.2 Producteur LLM (existant, généralisé)

- v2.0 : sortie `replace_line` uniquement — comportement actuel byte-stable,
  schéma JSON strict, validation 1:1 inchangée.
- v2.1 : schéma de sortie alternatif `replace_span` + `MatchAnchor`
  (opt-in par configuration). Prompt système dédié. À ne livrer qu'avec
  un banc de mesure comparatif (`replace_span` vs `replace_line`).
- Le payload distingue **lignes cibles** et **lignes de contexte** (fix F8,
  §7) : le producteur ne doit émettre d'ops que pour les cibles.

### 5.2 bis — Producteur vision / VLM (enveloppe v2.0, surface v2.x)

Correction **guidée par l'image** : un VLM reçoit, par ligne, l'`ocr_text`
**et** l'image de la ligne, et propose la correction. Cas d'usage : un
consommateur qui met en concurrence « correction texte-seul » et « correction
image + structure » (typiquement un banc), ou qui veut simplement récupérer un
OCR très fautif que seul le pixel permet de relire. C'est la correction
d'ALTO/PAGE **ligne par ligne** guidée par l'image (write-back par `line_id`).

Ce qui appartient à la **lib** (enveloppe, minimal) :
- Le contrat `EditProducer` vision-aware (§5.1 : `wants_geometry`/`wants_image`
  + `source_images`).
- Le compilateur qui **recopie** `geometry` (déjà dans le manifest) et
  `image_ref` de page (opaque) dans le `ModelPayload` (§4.1).
- **Rien d'autre.** La lib ne charge pas l'image, ne crope pas, n'encode pas.

Ce qui appartient au **consommateur** (le producteur concret, hors lib) :
- Charger l'image de page depuis `image_ref`, **cropper** chaque ligne via sa
  `geometry` (bbox relative = `coord/dimension_page`, sans souci d'unité),
  encoder, construire le message multimodal, appeler le VLM, renvoyer le même
  JSON `{lines:[{line_id, corrected_text}]}`.
- C'est une **implémentation de producteur** — par définition hors du cœur,
  comme tout provider concret (I4 : la lib ne touche aucun pixel).

**Prototypage sans changement de lib** : un producteur *stateful par document*
qui se construit avec le `DocumentManifest` + l'image, résout `coords[line_id]`
et crope, fonctionne avec la lib **inchangée** (il n'a besoin que des `line_id`
que le payload transporte déjà). L'enveloppe `source_images`/`wants_*` n'est
requise que pour un producteur **stateless et générique** (réutilisable par
n'importe quel consommateur, non reconstruit par document) — c'est la cible
v2.0 ; le producteur stateful est le raccourci de prototypage.

**Interaction avec les gardes (essentiel) :** les seuils d'acceptation
actuels sont calibrés pour du texte-seul, où « une correction très éloignée de
l'OCR est suspecte » (`MIN_SOURCE_SIMILARITY`). Un VLM a une **preuve
indépendante** — l'image — et peut légitimement diverger fortement d'un OCR
très fautif. Il faut donc un **profil `GuardConfig.vision()`** (§7 F13) :
similarité-source **détendue**, mais **garde-migration inter-lignes
maintenue** (c'est elle qui protège l'ancre, indépendante de la modalité). Les
seuils de ce profil se **calibrent par la mesure**, pas au doigt mouillé.
Réglage **hors lib** (au consommateur) : granularité des crops (par ligne /
par bloc / page entière) — arbitrage coût-tokens vs qualité de grounding ; le
crop par ligne est le plus fidèle à I1 mais le plus cher.

### 5.3 Producteur règles (nouveau, v2.0 — le premier émetteur de spans réels)

Moteur déterministe : table de substitutions (regex ou littérales) avec
garde optionnelle par dictionnaire/lexique. Exemples cibles : `ſ→s`,
confusions `rn→m` sous condition lexicale, ponctuation OCR. Émet des
`ReplaceSpan` à `RangeAnchor` (il calcule les offsets exactement).
Zéro dépendance, zéro réseau, reproductible à l'octet — c'est aussi le
producteur de référence des tests du protocole, et une passe de
pré-correction gratuite avant LLM.

### 5.4 Modèle spécialisé (futur, v2.x)

Enveloppe prévue, surface différée : sérialisation **texte** du
`ModelPayload` (lignes numérotées) + parseur de la réponse pour seq2seq
fine-tuné qui ne fait pas de JSON. Ne se construit que lorsqu'un tel modèle
existe et se benche (réflexe : pas de consommateur = pas de code).

---

## 6. Formats

### 6.1 ALTO (acquis + corrections normatives)

L'existant est conservé (parser, rewriter 4 chemins, provenance
`processingStep`) avec les corrections normatives du §7. Points fixés :

- **Réutilisation d'attributs en slow path — liste blanche explicite** :
  `ID`, `STYLEREFS` et `STYLE` sont réutilisés positionnellement ;
  `HPOS`/`WIDTH` recalculés ; `VPOS`/`HEIGHT` hérités de la ligne ;
  `WC`/`CC`/`SUBS_*` **jamais recyclés** (F2). `STYLE` (stylage inline
  bold/italics, jumeau par-valeur de `STYLEREFS`) est dans la liste par
  la doctrine F2 elle-même : elle ne proscrit que les données *invalidées*
  par le changement de texte, et le stylage ne l'est pas — le supprimer
  détruisait 45/47 `String` stylés du corpus X0000002 (mesuré, manchettes
  de presse en tête). *Ratifié le 2026-07-07.*
- La géométrie mot post-correction est une **approximation documentée**
  (attribut d'en-tête ou commentaire XML optionnel signalant la passe de
  correction ; le `processingStep` porte déjà la provenance).

### 6.2 PAGE XML (nouveau, v1.1)

PAGE (PRImA) est le format natif de Transkribus et d'eScriptorium ; le
supporter fait passer la lib de « correcteur ALTO » à « correcteur d'XML de
transcription patrimoniale ». Le cœur (manifests, planner, gardes,
réconciliation, protocole) est réutilisé tel quel ; seul `formats/page/`
est nouveau. Règles normatives :

- **P1 — Géométrie = polygones.** PAGE encode `Coords@points` (polygones),
  pas des bbox. Le manifest conserve le polygone source verbatim et expose
  la bbox englobante calculée (besoin du planner). **Aucune géométrie n'est
  jamais réécrite** — pas d'équivalent du slow path géométrique d'ALTO.
- **P2 — Texte canonique d'une ligne** = `Unicode` du TextEquiv canonique
  (P3) de la `TextLine`, NFC + strip. S'il est absent, concaténation des
  `Word/TextEquiv` séparés par des espaces. En cas de désaccord entre le
  texte ligne et la concaténation des mots, **le texte ligne fait foi**
  (signalé dans le rapport).
- **P3 — TextEquiv canonique** = celui d'`@index` minimal (absence d'index
  ≡ 0). À la réécriture d'une ligne modifiée : mise à jour de son
  `Unicode` (et `PlainText` s'il existe), **suppression de son `@conf`**
  (confiance périmée — même doctrine que F2) et **suppression des TextEquiv
  alternatifs** de l'élément (ils décrivaient l'ancien texte) ; le tout
  compté dans le `CorrectionReport`.
- **P4 — Éléments `Word`.** Fast path (compte de mots inchangé) : mise à
  jour des `TextEquiv` de chaque `Word` en place, `Coords` conservées,
  `@conf` supprimé. Slow path (compte changé) : les `Word` de la ligne sont
  **supprimés**, le texte vit au niveau ligne — fabriquer des polygones de
  mots dans une ligne inclinée serait plus mensonger que l'approximation
  bbox d'ALTO ; perte de granularité **documentée et comptée**.
- **P5 — Césure : heuristique, toujours.** PAGE n'a ni `<HYP>` ni
  `SUBS_TYPE`/`SUBS_CONTENT`. Détection de rôle sur caractères terminaux
  configurables : `-`, `¬` (U+00AC, convention Transkribus), `⸗` (U+2E17,
  Fraktur), `­` (U+00AD). `hyphen_source_explicit = False` systématiquement
  → la réconciliation tourne en mode conservateur, sans reconstruction de
  mot logique. Le **caractère de césure d'origine est préservé** à la
  réécriture (garde E5 étendue : un producteur ne peut pas normaliser
  `¬` → `-`). *Conventions à confirmer sur exports réels Transkribus et
  eScriptorium — c'est une exigence de la DoD v1.1.*
- **P6 — Microformat `custom`.** Transkribus stocke dans
  `custom="readingOrder {index:0;} textStyle {offset:…; length:…;} …"` des
  annotations dont certaines sont **ancrées par offsets de caractères**
  (textStyle, tags sémantiques). Doctrine « jamais de donnée périmée »
  (cf. F2) : les groupes **sans** offsets (`readingOrder`, `structure`)
  sont préservés verbatim ; les groupes **à** offsets sont retirés dès que
  le texte de la ligne change, et comptés dans le rapport. v2.x (chemin
  span uniquement) : **remappage des offsets** à travers l'EditScript — les
  `RangeAnchor` normalisés donnent les deltas exacts ; c'est une synergie
  directe du protocole §4, impossible avec des réécritures de lignes
  entières.
- **P7 — Sécurité & provenance.** `make_safe_parser()` obligatoire (le
  test-contrat grep s'étend à `formats/`). Provenance : mise à jour de
  `Metadata/LastChange` + écriture d'un `MetadataItem type="processingStep"`
  quand le schéma cible (2019+) le permet, repli sur `Metadata/Comments`
  sinon — à valider contre le XSD effectivement visé.

### 6.3 Parité inter-formats

Un même `DocumentManifest` en sortie de parse, quel que soit le format ;
les tests de parité imposent : texte canonique identique pour un même
contenu logique, rôles de césure détectés équivalents quand l'information
existe, round-trip byte-stable sur documents non modifiés dans les deux
formats.

---

## 7. Corrections normatives sur l'existant (issues de la revue)

Chaque entrée : constat → règle normative. Toutes sont **v1.0** sauf mention.

| # | Constat (fichier:ligne au commit revu) | Règle normative |
|---|---|---|
| **F1** | `downgrade_granularity` (`chunk_planner.py:30`) jamais appelé ; à l'épuisement des retries, `_apply_chunk_fallback` (`correction_pipeline.py:651`) reverte **tout le chunk** à l'OCR — au grain PAGE, une ligne malformée coûte la page entière | À l'épuisement du budget d'un chunk de grain G, **re-planifier les lignes du chunk au grain inférieur** (PAGE→BLOCK→WINDOW→LINE) et retenter ; seules les lignes dont le chunk LINE échoue passent en repli OCR. Budget total borné par `RetryPolicy.per_chunk_budget` (défaut : 6 tentatives cumulées). Événement `chunk_downgraded` émis à chaque descente |
| **F2** | Fast path (`rewriter.py:272`) et slow path (`rewriter.py:314`) conservent/recyclent `WC`/`CC` : confidences périmées, `CC` de longueur incohérente avec le nouveau `CONTENT` | Tout changement de `CONTENT` **supprime `WC` et `CC`** sur le `String` concerné. Le slow path ne recycle que `ID`, `STYLEREFS` et `STYLE` (liste blanche §6.1, ratifiée 2026-07-07) |
| **F3** | `etree.QName(last_child.tag)` (`parser.py:143`) lève sur commentaire/PI en fin de `TextLine` → échec du fichier entier | Toute itération d'enfants ignore les nœuds dont `tag` n'est pas `str` (commentaires, PI). Test avec fixture contenant commentaires |
| **F4** | Détection UNTOUCHED : `reconstruct_textline(el) == nfc(corrected)` (`rewriter.py:117`) non strippé vs `ocr_text` strippé (`parser.py:25`) → lignes jamais UNTOUCHED, réécritures et métriques faussées | Comparaison sur formes **strippées des deux côtés**. Test : ligne avec SP de queue non corrigée → chemin UNTOUCHED |
| **F5** | `_int_attr` (`_ns.py:46`) lève sur coordonnées flottantes (`"123.0"`) | `int(float(raw))`, arrondi trunc, avec test. Une valeur non numérique lève toujours |
| **F6** | `_compute_geometry` (`rewriter.py:67-82`) : `unit` calculé sur le compte plein mais espaces pondérés 0,6 → le dernier token absorbe tout le déficit | Le poids 0,6 des espaces entre dans `total_weight` ; la correction d'arrondi se répartit ; le dernier token n'absorbe que l'arrondi résiduel |
| **F7** | Appariement de césure purement séquentiel (`parser.py:33`), aucun contrôle géométrique inter-blocs | Documenté comme hypothèse + **politique d'appariement** injectable (`PairingPolicy`, défaut = comportement actuel). Pas de géométrie par défaut : les gardes aval couvrent ; le seam permet de durcir sans fork |
| **F8** | Chevauchement de fenêtres : ligne corrigée au chunk N (bord, contexte tronqué) **sautée** au chunk N+1 (contexte plein) — la moins bonne correction gagne ; même mécanique quand la réconciliation écrit un PART2 hors chunk | Les chunks distinguent **lignes cibles** et **lignes de contexte** : une ligne n'est cible que dans le chunk où son contexte est maximal ; les recouvrements deviennent contexte pur. Le validateur n'attend de sortie que pour les cibles (le comptage 1:1 porte sur les cibles) ; la sortie d'une ligne de contexte est **optionnelle mais strictement vérifiée quand présente**, puis écartée (la ligne est cible d'un chunk adjacent — invariant : chaque ligne est cible dans exactement un chunk). Ratifié 2026-07-07 |
| **F9** | Rampe de température 0.0→0.3→0.5 codée en dur (`correction_pipeline.py:725`) → non-déterminisme dès le premier retry | `RetryPolicy(max_attempts, temperatures, backoffs, per_chunk_budget)` injectable. `RetryPolicy.default()` = comportement actuel ; `RetryPolicy.deterministic()` = températures toutes à 0 (pour un usage reproductible) |
| **F10** | Aucun point d'annulation : un run ne peut pas être interrompu proprement | `should_abort: Callable[[], bool]` optionnel sur `run()`, sondé entre chunks et entre pages → `CorrectionAborted` levée, sorties non écrites. Les appels provider en vol ne sont pas interrompus (coopératif, documenté) |
| **F11** | Les tests de l'algorithme (`hyphenation`, `chunk_planner`, `validator`, `line_acceptance`, `rewriter`, `parser`) vivent dans `backend/tests/` — la lib ne porte pas sa propre preuve | Rapatriement dans `packages/<lib>/tests/` ; le backend ne garde que ses tests d'intégration/transport. CI de la lib indépendante (matrix 3.11–3.13) |
| **F12** | Packaging : pas de `py.typed`, enums applicatives dans le cœur (`Provider`, `JobStatus`/`JobManifest` documenté « server-side » dans `schemas`) | Marqueur `py.typed` + `mypy --strict` en CI. `Provider`, `JobManifest`, `JobStatus` (et `images: dict`) **sortent du cœur** vers le backend — le cœur n'énumère pas des vendeurs. `LineStatus`, `PipelineEventType` restent |
| **F13** | Seuils des gardes en constantes dispersées (`line_acceptance.py:37-51`, `migration_guards.py`) | `GuardConfig` (frozen) regroupant tous les seuils, défauts = valeurs actuelles (byte-compatible). Docstring : les trois étages se règlent ensemble. **v2.x** : profil `GuardConfig.vision()` (similarité-source détendue, garde-migration inter-lignes maintenue) pour la correction VLM (§5.2 bis) — seuils calibrés au banc, non livrés tant qu'un producteur vision ne les benche pas |
| **F14** | `complete_structured` ne remonte pas la consommation de tokens | v1.0 : `complete_structured` renvoie `(dict, Usage \| None)` (rupture pré-publication) ; v2.0 : porté par le contrat `EditProducer` (§5.1). Le rapport et les événements l'exposent |

---

## 8. API publique v1.0

### 8.1 Surface

```python
# parse
build_document_manifest(files) -> DocumentManifest          # existant
parse_alto_file(path, ...) -> tuple[list[PageManifest], _Element]

# pipeline
CorrectionPipeline(
    provider: BaseProvider,
    observer: PipelineObserver,
    output_writer: OutputWriter,
    config: ChunkPlannerConfig | None = None,
    retry_policy: RetryPolicy | None = None,      # F9
    guard_config: GuardConfig | None = None,      # F13
)
await pipeline.run(
    document_manifest=..., api_key=..., model=..., provider_name=...,
    source_files=..., run_id=None,
    should_abort=None,                            # F10
    apply=True,                                   # §9 dry-run
) -> CorrectionResult

pipeline.run_sync(...)                            # façade asyncio.run, documentée

# Note (ADR-011, 2026-07) : la persistance a quitté la surface moteur —
# plus de `output_writer` au constructeur ni de `apply=` sur run() ; le
# résultat porte les artefacts (`result.corrected_files`, `result.report`)
# et `result.write(dir)` est l'aide côté appelant. (La résorption §5.1 a
# déjà retiré api_key/model/provider_name de run() — voir ADR ; le bloc
# ci-dessus reste la photographie v2.0 d'origine.)

# bas niveau (déjà publics, maintenus)
rewrite_alto_file(...), extract_output_texts(...),
reconcile_hyphen_pair(...), check_line(...), plan_page(...)
```

### 8.2 Politiques

`RetryPolicy`, `GuardConfig`, `ChunkPlannerConfig`, `PairingPolicy` :
objets frozen Pydantic, tous avec un défaut reproduisant le comportement
actuel. **Empreinte de configuration** (`policy_fingerprint()` : hash stable
du dump JSON trié) exposée pour la provenance (§11).

Note (ratifiée 2026-07-07) : `CorrectionPipeline(pairing_policy=…)` est un
paramètre de **provenance uniquement** — l'appariement des paires de
coupure se fait au parse, avant le pipeline, et le pipeline ne ré-apparie
jamais. Il existe pour que `config_fingerprint()` couvre les quatre
politiques ci-dessus. Contrat appelant : passer la **même** `PairingPolicy`
qu'au parse ; le pipeline ne peut pas le vérifier, et une politique
différente rendrait l'empreinte estampillée mensongère.

### 8.3 Typage & qualité

`py.typed`, `mypy --strict` en CI, `ruff`, couverture cible 85 % sur le
paquet lib. `__all__` exhaustifs (déjà en place).

### 8.4 Contrat d'erreurs

Hiérarchie unique : `CorrectionError` (base) ← `ParseError`,
`ValidationError` (réponse producteur), `HyphenIntegrityError`,
`CorrectionAborted`. Les `ValueError` nues actuelles migrent sous cette
racine en conservant l'héritage `ValueError` (compatibilité `except`).

### 8.5 Versionnage

SemVer strict après première publication. Les ruptures listées ici (F12,
§5.1, §8.4) se font **avant** le premier tag publié — d'où l'intérêt de les
grouper en v1.0. La ré-organisation de modules (§3) attend la v2.0, avec
alias d'import dépréciés pendant une version mineure.

---

## 9. Observabilité, rapport, dry-run

- **`CorrectionReport` public** : le `LineTrace` actuel (source → entrée
  modèle → sortie modèle → projeté → texte ré-extrait, chemin rewriter,
  raison de repli) devient un artefact de sortie **documenté, schéma JSON
  stable versionné** — plus seulement un fichier interne du backend. C'est
  la matière d'un diff/aperçu côté consommateur.
- **Dry-run** : depuis ADR-011 (2026-07), TOUT run est un dry-run côté
  moteur — il n'écrit jamais rien ; il renvoie rapport + EditScript
  normalisé + XML corrigé (`result.corrected_files`). La « vraie »
  écriture est le choix de l'appelant (`result.write(dir)`, ou la
  transaction du backend). Usage : prévisualisation, ou mesure sans
  écriture par un consommateur qui benche.
- Les événements (`PipelineEventType`) restent la seule interface de
  progression ; `chunk_downgraded` (F1) s'ajoute au contrat SSE.

---

## 10. Sécurité

- `make_safe_parser()` obligatoire pour **tout** parse lxml, y compris le
  futur parser PAGE ; le test-contrat grep (`test_xml_security.py:182`)
  s'étend au nouveau dossier `formats/`.
- Zéro réseau et zéro filesystem dans `core/` (déjà vrai, maintenu par
  l'arbre d'imports §3).
- `sanitize_error` conservé tel quel (patterns de secrets) et appliqué à
  tout message d'événement sortant.

---

## 11. Reproductibilité & provenance

- `RetryPolicy.deterministic()` + producteur règles : chaîne entièrement
  reproductible à réponse LLM égale ; documenter que la reproductibilité
  totale exige un cache de réponses **côté consommateur** (hors lib — un LLM
  reste non déterministe même à température 0).
- Le `processingStep` écrit dans l'ALTO/PAGE corrigé porte :
  `provider/model` (existant) + **version de la lib** + **empreinte de
  configuration** (§8.2). Un XML corrigé dit par quoi et sous quelle
  politique il a été corrigé.

---

## 12. Hors-périmètre (explicite et définitif)

OCR ; segmentation/analyse de mise en page ; métriques d'évaluation
(CER/WER…) ; rendu HTML/visualisation ; IIIF ; conversion générique
ALTO↔PAGE↔TEI (on **corrige dans** un format, on ne convertit pas entre
formats) ; gestion de jobs/persistance/SSE (backend) ; providers HTTP
concrets (backend ou paquet séparé) ; NER/enrichissement sémantique.

**Et — explicitement — toute manipulation de pixels (I4)** : chargement
d'image, découpe/crop, mise à l'échelle, encodage base64, construction d'un
message multimodal, appel VLM. La lib **expose l'ancrage** (géométrie déjà
dans le manifest, référence d'image opaque) pour qu'un producteur vision
résolve les pixels *lui-même* (§5.2 bis) ; elle ne les résout jamais. Faire
entrer PIL/Pillow ou de l'I/O image dans `core` briserait « zéro I/O dans le
cœur » — c'est la ligne rouge la plus stricte.

Chacun de ces points a un propriétaire naturel : l'application appelante ou
un outil dédié — **jamais la lib**. Les specs qui proposeraient de les ajouter
ici doivent citer ce paragraphe et argumenter contre.

---

## 13. Plan de livraison

Chaque tranche laisse la lib **publiable et verte** ; pas de branche longue.

| Version | Contenu | DoD |
|---|---|---|
| **v1.0** | Corrections F1–F14 ; rapatriement des tests (F11) ; API §8 (politiques, erreurs, usage, dry-run sans EditScript) ; `CorrectionReport` public ; publication PyPI sous le nom retenu (§14) | Suite verte dans le paquet ; `mypy --strict` ; byte-parity sur corpus de non-régression (mêmes entrées + `RetryPolicy.default()` → mêmes sorties qu'avant, hors fixes F2/F4/F6 documentés) ; CHANGELOG |
| **v1.1** | Backend **PAGE XML** (§6.2) : parser, rewriter 4 chemins, césure heuristique, tests de parité §6.3 | Round-trip byte-stable PAGE ; conventions **P5** (caractères de césure) et **P7** (provenance/XSD) confirmées sur exports réels Transkribus **et** eScriptorium ; mêmes gardes vertes sur ces corpus |
| **v2.0** | Protocole d'édition (§4) : `EditScript`, normalisation `match→range`, ré-expression `replace_line` (zéro changement de comportement, prouvé par les snapshots v1) ; **producteur règles** (§5.3) ; **enveloppe vision** (§5.2 bis : `EditProducer.wants_*`, `source_images`, géométrie + `image_ref` dans le payload — la lib forwarde, ne crope pas) ; ré-organisation §3 avec alias dépréciés | Snapshots v1 inchangés via le chemin protocole ; moteur de règles testé à l'octet ; producteur vision *mocké* prouvant que géométrie + `image_ref` transitent sans que la lib ouvre l'image ; doc du protocole |
| **v2.1** | Producteur LLM en mode `replace_span`/`MatchAnchor` (opt-in), benché contre `replace_line` avant d'être recommandé | Comparatif CER/coût publié ; le mode span n'est défaut nulle part sans preuve |
| **v2.x** | Profil `GuardConfig.vision()` (§7 F13) calibré au banc ; sérialisation texte pour modèle spécialisé (§5.4) — chacun uniquement quand son consommateur existe et se benche | — |

---

## 14. Nom & packaging

`alto-core`, `corrigenda`, `anastylose` sont libres sur PyPI (vérifié).
Recommandation : **`corrigenda`** — le terme d'imprimerie désignant la liste
des corrections d'un texte imprimé : c'est littéralement ce qu'est un
`EditScript`, ça porte le domaine patrimonial, et ça survit à l'extension
PAGE XML (contrairement à `alto-core`, qui devient faux en v1.1).
`anastylose` (remontage d'un monument à partir de ses pièces d'origine) est
la belle alternative métaphorique. Décision avant le premier tag — on ne
renomme pas un paquet publié.

Le paquet reste dans le monorepo `alto-llm-corrector`
(`packages/<nom>/`) ; le backend le consomme par dépendance de chemin comme
aujourd'hui, les externes par PyPI.

---

## 15. Consommateurs & couplage (contrainte SUR la lib)

La lib est **agnostique de ses consommateurs** : elle ne nomme, n'importe et
ne cible aucune application en particulier. Ce qui la concerne, et qui est
une contrainte de conception, se réduit à ces règles :

- **Couplage à sens unique.** Les consommateurs dépendent de la lib ; la lib
  ne dépend d'aucun consommateur, ni par import, ni par entry-point qui
  inverserait le sens. Se distribue proprement (PyPI) et s'installe comme
  dépendance standard, éventuellement derrière un extra optionnel côté
  consommateur.
- **Toute variabilité passe par l'injection**, pas par un cas particulier
  câblé : le producteur (`EditProducer`/`BaseProvider`), l'observateur, le
  writer, et les politiques (`RetryPolicy`, `GuardConfig`, `ChunkPlannerConfig`,
  `PairingPolicy`, `source_images`) sont les *seuls* points par lesquels un
  consommateur adapte la lib. Un besoin qui n'y rentrerait pas est soit un
  manque d'enveloppe à corriger dans la lib, soit un concern hors-périmètre
  (§12) — jamais un `if consommateur == …`.
- **La lib ne suppose aucun environnement d'exécution** : ni job store, ni
  transport (SSE), ni gestion de clés, ni système de fichiers imposé — tout
  cela appartient à l'appelant (§12).

La façon dont un consommateur donné câble ces points d'injection (adapters,
ponts provider, réutilisation de sa propre machinerie image/VLM) est **sa**
spec, pas celle de la lib, et vit chez lui.
