# Corpus PAGE XML — provenance et usage

Deux sous-corpus complémentaires : **OCR17plus** (livres, triplets
PAGE/ALTO de la même page, vérité corrigée, `¬`) et **NewsEye-FR**
(`newseye-fr/`, presse en colonnes — la mise en page la plus complexe :
ReadingOrder multi-colonnes, densité de césures, OCR brut réel).

Corpus de non-régression pour le support **PAGE XML** (SPECS_LIB_V2 §6.2,
v1.1) et les **tests de parité inter-formats** (§6.3). Équivalent PAGE du
corpus ALTO BnF (`examples/X0000002.xml`) : imprimés français, même
provenance Gallica/BnF.

## Source

**OCR17+** — Simon Gabay et al., *OCR17: Ground Truth and Models for
17th c. French Prints* (projet e-ditiones).

- Dépôt : <https://github.com/e-ditiones/OCR17plus>
- Article : <https://hal.science/hal-02577236> (JDMDH #11401)
- **Licence des données XML : CC-BY** (les images, non incluses ici,
  relèvent des conditions Gallica). Attribution requise — conservée par ce
  fichier.

Chemins d'origine (branche `main`, juillet 2026) :
`Data/<Œuvre>/pageXmlTranskribus/…` (brut), `…/pageXmlTranskribusCorrected/…`
(corrigé), `…/alto4eScriptorium/…` (ALTO 4).

## Contenu

Deux pages de **prose** (la prose maximise les césures, contrairement au
théâtre en vers) ; pour chacune, un **triplet** de la même page :

| Fichier | Rôle |
|---|---|
| `*_page_raw.xml` | Sortie OCR Transkribus **brute** (fautes réelles : `cukiuent`, `eft`…) |
| `*_page_corrected.xml` | Vérité terrain corrigée |
| `*_alto4.xml` | La même page en **ALTO 4** (export eScriptorium) |

- `Descartes1637_Discours_btv1b86069594_…_0014` — *Discours de la méthode*
  (ark : btv1b86069594), 32 lignes.
- `LaFayette1678_Cleves_btv1b8610820b_…_0011` — *La Princesse de Clèves*
  (ark : btv1b8610820b), 13 lignes.

## Ce que chaque fichier exerce (mapping P1–P7)

- **P1 (polygones)** : `Coords@points` partout ; bbox à calculer.
- **P2 (texte canonique)** : les **raw** ont TextEquiv aux DEUX niveaux
  (mot + ligne) → chemin nominal ; les **corrected** n'ont PAS de
  TextEquiv ligne (un unique `Word` porte la ligne entière) → exerce
  exactement le repli « concaténation des Word/TextEquiv ».
- **P4 (Words)** : raw Descartes = 275 Words (fast/slow path mot) ;
  corrected = 1 Word/ligne (dégénéré).
- **P5 (césure heuristique)** : raw = tiret simple `-` (`tou-`) ;
  corrected = **`¬` U+00AC** (convention Transkribus), 6 occurrences
  (Descartes) / 2 (La Fayette). PAS de `⸗` ici (voir Lacunes).
- **P6 (`custom`)** : `readingOrder {index:…}` présent (groupe SANS
  offsets → à préserver verbatim). L'élément standard `TextStyle` est
  présent dans les raw.
- **P7 (sécurité/provenance)** : namespace `pagecontent/2013-07-15` —
  le repli provenance `Metadata/Comments` s'applique (pas de
  MetadataItem 2019).
- **§6.3 (parité)** : chaque triplet donne la même page en PAGE et en
  ALTO 4. ⚠️ Les segmentations divergent légèrement (33 vs 32 lignes
  Descartes ; 14 vs 13 La Fayette) : la parité doit apparier les lignes
  par contenu/géométrie, pas par index naïf.
- **Bonus** : le couple raw/corrected est un banc de post-correction réel
  (entrée OCR fautive → sortie attendue).

## Sous-corpus `newseye-fr/` — presse française en colonnes

**Source** : fichiers fournis par le mainteneur (juillet 2026) ; métadonnées
internes `TranskribusMetadata docId="545185"` (pageNr 95/96, status="GT",
créés 2018-12-18) — presse française ~1900 produite par Transkribus dans le
cadre NewsEye/READ, cohérent avec le jeu BnF
[Zenodo 4293602](https://zenodo.org/records/4293602) /
[5654841](https://zenodo.org/records/5654841) (**licence CC BY 4.0 présumée
— à confirmer contre le record Zenodo avant toute redistribution hors de ce
dépôt**).

| Fichier | Régions | Lignes | Words | Césures `-` |
|---|---|---|---|---|
| `0250199004.xml` | 310 | 820 | 5 491 | 95 |
| `0253902003.xml` | 235 | 914 | 5 647 | ~100 |

Ce que ce sous-corpus apporte de plus qu'OCR17plus :

- **La complexité maximale de mise en page** : presse multi-colonnes,
  310/235 TextRegions par page, **élément `<ReadingOrder>` explicite**
  (OrderedGroup indexé) — le vrai test d'I3 (l'ordre du XML n'est pas
  l'ordre de lecture) et du chunk planner à l'échelle (820–914 lignes,
  ~15–25× OCR17plus).
- **Le jumeau presse exact de `examples/X0000002.xml`** (même monde BnF,
  même densité, même genre éditorial) — c'est l'équivalent PAGE demandé.
- **OCR brut réel non corrigé** (« organisée nar », « c°nff'el n™nicipal ») :
  l'*entrée* type de la bibliothèque de post-correction, à l'échelle d'une
  vraie page de journal.
- **TextEquiv aux trois niveaux** (région/ligne/mot, ~6 600 par page) avec
  **2 désaccords mot↔ligne réels** dans `0250199004.xml` → cas naturels du
  chemin P2 « en cas de désaccord, le texte ligne fait foi (signalé) ».
- `custom readingOrder` sur ~6 600 éléments (groupes sans offsets, P6).

Toujours absents ici aussi : `@conf`, `custom textStyle{offset:…}`,
`¬`/`⸗` (césures en `-` simple), namespace 2019. Régions sans `@type`.

Note taille : ~2,4 Mo/page (polygones au mot) — exclusion dédiée dans
`.pre-commit-config.yaml` (hook check-added-large-files).

## Lacunes connues (à compléter)

1. **`TextEquiv@conf`** absent (GT académique) → P3 (suppression de la
   confiance périmée) devra être testé sur fixture synthétique ou sur un
   export Transkribus de production.
2. **`custom` à offsets** (`textStyle {offset:…;length:…;}`) absent → P6
   (retrait des groupes à offsets) : fixture synthétique nécessaire.
3. **`⸗` (U+2E17/Fraktur)** : disponible dans
   [UB-Mannheim/AustrianNewspapers](https://github.com/UB-Mannheim/AustrianNewspapers)
   (NewsEye ONB, CC BY 4.0, PAGE Transkribus, allemand) — pas de listing
   d'arbre accessible depuis cet environnement (API GitHub scopée) ; à
   récupérer manuellement si P5-Fraktur doit être couvert par corpus réel.
4. **Namespace 2019** : ces fichiers sont en 2013-07-15 ; prévoir au moins
   une fixture 2019+ pour le chemin MetadataItem (P7).
5. Le jeu **NewsEye français** (journaux BnF, Zenodo
   [4293602](https://zenodo.org/records/4293602)) serait le complément
   idéal en presse (proche de X0000002) — Zenodo est bloqué par la
   politique réseau de cet environnement ; téléchargement manuel requis.
