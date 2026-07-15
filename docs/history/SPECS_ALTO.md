# SPECS_ALTO — Parser, Hyphenation Reconciler, Rewriter

---

## Parser ALTO (`alto/parser.py`)

**Responsabilité :** lire un fichier ALTO XML, extraire pages/blocs/lignes, retourner des `PageManifest`. Détecter et annoter les césures interlignes.

### Règles générales

- Détecter automatiquement le namespace depuis le tag racine
- Supporter ALTO v2, v3, v4, et sans namespace
- Pour chaque `TextLine`, extraire : ID, HPOS, VPOS, WIDTH, HEIGHT
- Reconstruire `ocr_text` :
  - `String` → append `CONTENT`
  - `SP` → append `" "`
  - `HYP` → append `CONTENT` si présent, sinon `"-"`
- Normaliser en Unicode NFC, supprimer `\r`, strip bords
- Lier `prev_line_id` / `next_line_id` entre lignes consécutives

### Détection des césures — règles de priorité

**Cas 1 — Césure explicite (`source_explicit = True`) :**

Lors du parcours des enfants d'une TextLine, détecter :
- Un élément `HYP` présent en dernière position (= PART1)
- Un attribut `SUBS_TYPE="HypPart1"` sur le dernier `String` (= PART1)
- Un attribut `SUBS_TYPE="HypPart2"` sur le premier `String` (= PART2)
- Extraire `SUBS_CONTENT` s'il est présent sur l'un ou l'autre

**Cas 2 — Césure heuristique (`source_explicit = False`) :**

Si aucun marquage SUBS_TYPE/HYP n'est présent mais que le dernier token non-espace de la ligne se termine par `-` : marquer comme candidate heuristique. Mode conservateur : ne pas inventer de `SUBS_CONTENT`.

**Liaison des paires :**

Après avoir parcouru toutes les lignes de la page, faire un second pass :
- Pour chaque ligne marquée PART1, la ligne suivante dans l'ordre global est candidate PART2
- Si la ligne suivante porte déjà PART2 ou est une candidate heuristique cohérente → créer le lien bidirectionnel via `hyphen_pair_line_id`
- Si `SUBS_CONTENT` est présent sur PART1 et absent sur PART2 (ou vice-versa), propager la valeur sur les deux

### Signatures principales

```python
def build_document_manifest(files: list[tuple[Path, str]]) -> DocumentManifest
def parse_alto_file(xml_path, source_name, page_index_offset, global_line_offset)
    -> tuple[list[PageManifest], etree._Element]
def _detect_hyphenation(lines: list[LineManifest]) -> None
    # Mutates lines in-place : remplit hyphen_role, hyphen_pair_line_id, hyphen_subs_content
```

---

## Hyphenation Reconciler (`alto/hyphenation.py`)

C'est le module central. Son rôle est d'orchestrer la gestion des mots cassés entre deux lignes : **l'application décide, le LLM informe**.

**Principe fondamental :**

> Les césures interlignes ne doivent pas être laissées à la seule initiative du LLM. L'application détecte les paires de lignes liées par césure, transmet cette information au modèle, puis réinscrit la sortie sur les deux lignes physiques. En cas d'ambiguïté, la forme source est préservée.

### Responsabilités du module

1. **`enrich_chunk_lines()`** — préparer les `LLMLineInput` enrichis avec métadonnées de césure
2. **`reconcile_hyphen_pair()`** — après réponse LLM, réinscrire la correction sur la paire physique
3. **`should_stay_in_same_chunk()`** — prédicat pour le chunk planner

### Fonction `enrich_chunk_lines()`

```python
def enrich_chunk_lines(
    line_manifests: list[LineManifest],
    all_lines_by_id: dict[str, LineManifest],
) -> list[LLMLineInput]:
```

Pour chaque ligne, construire le `LLMLineInput` avec :
- `prev_text` / `next_text` comme d'habitude
- Si `hyphen_role != NONE` :
  - Renseigner `hyphenation_role`, `hyphen_candidate = True`
  - Sur PART1 : `hyphen_join_with_next = True`
  - Sur PART2 : `hyphen_join_with_prev = True`
  - Si `hyphen_subs_content` connu : `logical_join_candidate = hyphen_subs_content`

### Fonction `reconcile_hyphen_pair()`

```python
def reconcile_hyphen_pair(
    part1: LineManifest,
    part2: LineManifest,
    corrected_part1: str,
    corrected_part2: str,
) -> tuple[str, str, Optional[str]]:
    """
    Retourne (final_text_part1, final_text_part2, resolved_subs_content).

    Garantit :
    - Les deux lignes physiques restent distinctes
    - Aucun texte ne migre d'une ligne à l'autre
    - Si la correction est ambiguë, retourner les textes source
    """
```

**Algorithme :**

```
1. Isoler le dernier token non-espace de corrected_part1 (candidat fragment gauche)
2. Isoler le premier token non-espace de corrected_part2 (candidat fragment droit)
3. Si source_explicit == True (césure encodée dans l'ALTO source) :
   a. Si hyphen_subs_content connu → utiliser comme référence pour valider
   b. Vérifier que la concaténation (fragment_gauche + fragment_droit) est cohérente
      avec le mot logique attendu (si connu)
   c. Conserver les frontières physiques : part1 garde son texte, part2 garde le sien
   d. resolved_subs_content = mot logique déterminé avec confiance
4. Si source_explicit == False (heuristique) :
   a. Mode conservateur : ne rien reconstruire agressivement
   b. Retourner corrected_part1, corrected_part2 tels quels
   c. resolved_subs_content = None
5. En cas de doute à n'importe quelle étape : retourner les textes OCR source
```

**Ce que cette fonction ne fait JAMAIS :**
- Fusionner les deux lignes en une
- Déplacer "porte" sur la ligne 1 et vider la ligne 2
- Inventer un SUBS_CONTENT sans base dans la source

### Fonction `should_stay_in_same_chunk()`

```python
def should_stay_in_same_chunk(
    line_a: LineManifest,
    line_b: LineManifest,
) -> bool:
    """
    Retourne True si line_a et line_b doivent impérativement être
    dans le même chunk LLM (paire liée par césure).
    """
    return (
        line_a.hyphen_role == HyphenRole.PART1
        and line_a.hyphen_pair_line_id == line_b.line_id
    ) or (
        line_b.hyphen_role == HyphenRole.PART1
        and line_b.hyphen_pair_line_id == line_a.line_id
    )
```

---

## Rewriter ALTO (`alto/rewriter.py`)

**Responsabilité :** réécrire un fichier ALTO en remplaçant les enfants textuels des TextLine, en reconstituant HYP et SUBS_* pour les paires de césure.

### Invariants absolus à respecter

- Ne jamais modifier `TextLine/@ID`, `/@HPOS`, `/@VPOS`, `/@WIDTH`, `/@HEIGHT`
- Ne jamais changer l'ordre XML des `TextLine`
- Ne jamais fusionner deux TextLine

### Algorithme par TextLine — cas sans césure

1. Supprimer tous les enfants `String`, `SP`, `HYP` existants
2. Supprimer attributs `WC`, `CC` de la TextLine
3. Tokeniser `corrected_text` avec `re.split(r'(\s+)', text)`
4. Segments espace → élément `SP`
5. Segments non-espace → élément `String` avec ID `{line_id}_STR_{n:04d}`
6. Géométrie heuristique : redistribuer `TextLine.WIDTH` proportionnellement à `len(token)`
7. Tous les nouveaux `String` héritent de `VPOS` et `HEIGHT` de la TextLine

**Géométrie proportionnelle :**
- Poids mot = `len(mot)`
- Poids espace = `max(1, round(len(espace) * 0.6 * unit))`
- `unit = TextLine.WIDTH / total_poids`
- Corriger l'arrondi sur le dernier token pour que la somme = `TextLine.WIDTH` exact

### Algorithme par TextLine — cas PART1 (ligne terminée par césure)

Condition : `line_manifest.hyphen_role == HyphenRole.PART1`

1. Supprimer les enfants existants
2. Construire les `String` pour tous les tokens jusqu'à l'avant-dernier mot inclus
3. Pour le dernier mot (fragment gauche) :
   - Créer un `String` avec son `CONTENT` (ex: `"por"`)
   - Si `hyphen_subs_content` est connu : ajouter `SUBS_TYPE="HypPart1"` et `SUBS_CONTENT=hyphen_subs_content`
4. Créer un élément `HYP` après ce dernier `String` :
   - `CONTENT="-"`, `HPOS/VPOS/WIDTH/HEIGHT` heuristiques en fin de ligne

### Algorithme par TextLine — cas PART2 (ligne commençant par suite de césure)

Condition : `line_manifest.hyphen_role == HyphenRole.PART2`

1. Supprimer les enfants existants
2. Pour le premier mot (fragment droit) :
   - Créer un `String` avec son `CONTENT` (ex: `"te"`)
   - Si `hyphen_subs_content` est connu : ajouter `SUBS_TYPE="HypPart2"` et `SUBS_CONTENT=hyphen_subs_content`
3. Construire les `String` + `SP` pour les tokens suivants normalement

### Politique de confiance pour SUBS_CONTENT

| Condition | Action |
|-----------|--------|
| `source_explicit=True` et `hyphen_subs_content` fourni par source | Écrire SUBS_CONTENT tel quel |
| `source_explicit=True` et SUBS_CONTENT résolu par reconciler avec confiance | Écrire SUBS_CONTENT résolu |
| `source_explicit=False` (heuristique) | Ne pas écrire SUBS_CONTENT |
| Ambiguïté ou incertitude | Ne pas écrire SUBS_CONTENT |

Ajouter une entrée de processing dans `Description/Processing` si la section existe.
