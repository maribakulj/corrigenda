# SPECS_JOBS — Chunk Planner, Validateur, Orchestrateur

Fichiers cibles : `backend/app/jobs/`

---

## Chunk Planner (`chunk_planner.py`)

**Règle centrale : les paires de césure ne peuvent jamais être séparées.**

Le planner intègre la contrainte `should_stay_in_same_chunk()` du Hyphenation Reconciler à chaque niveau de découpage.

### Hiérarchie de décision

```
1. PAGE ENTIÈRE
   Condition : total chars ≤ 12000 ET total lignes ≤ 80
   → 1 seul chunk contenant toutes les lignes de la page

2. BLOC PAR BLOC
   Condition : chaque bloc tient dans les budgets
   MAIS : si une paire de césure est à cheval sur deux blocs,
          les deux blocs concernés doivent être regroupés dans un seul chunk.
   → Si un regroupement dépasse le budget → invalide, passer à WINDOW

3. FENÊTRES DE LIGNES
   window_size=12, overlap=1, step=11
   MAIS : aucune fenêtre ne peut couper une paire de césure en deux.
   Règle : si la ligne N est PART1 et que la ligne N+1 est sa PART2,
           et que N est le dernier index d'une fenêtre,
           étendre la fenêtre d'une ligne pour inclure N+1.
   → Chevauchement possible : ajuster le step pour ne pas laisser de paire orpheline.

4. LIGNE PAR LIGNE (dernier recours)
   Si une ligne fait partie d'une paire de césure,
   traiter la paire comme un bloc atomique inséparable :
   → le "chunk ligne" contient en réalité 2 lignes liées.
```

**Fonction `downgrade_granularity(current)` :** retourne le niveau suivant dans la hiérarchie (PAGE → BLOCK → WINDOW → LINE) ou None si déjà au minimum.

---

## Validateur (`validator.py`)

Après chaque réponse LLM, valider :
1. Présence de la clé `"lines"`
2. Nombre d'entrées = nombre attendu
3. Tous les `line_id` attendus présents
4. Aucun `line_id` doublon ou inconnu
5. Chaque `corrected_text` : string non vide, sans `\n` ni `\r`

### Validation additionnelle pour les paires de césure

Si le chunk contient une paire PART1/PART2, vérifier que :
- `corrected_text` de PART1 ne contient pas le texte logique entier du mot coupé (ce serait une fusion interdite)
- `corrected_text` de PART2 n'est pas vide (la suite de la césure ne doit pas avoir disparu)

En cas de violation sur une paire de césure : lever `ValueError` avec motif `"hyphen_integrity_violation"`.

---

## Orchestrateur (`orchestrator.py`)

L'orchestrateur intègre le Hyphenation Reconciler **avant** et **après** chaque appel LLM.

### Pipeline par chunk

```
AVANT l'appel LLM :
  1. Récupérer les LineManifest du chunk
  2. Appeler enrich_chunk_lines() → LLMLineInput enrichis avec métadonnées césure
  3. Construire le payload user

APPEL LLM

APRÈS l'appel LLM :
  4. Valider la réponse (validator.py)
  5. Pour chaque paire PART1/PART2 présente dans le chunk :
     a. Extraire corrected_part1 et corrected_part2 depuis la réponse
     b. Appeler reconcile_hyphen_pair(part1, part2, corrected_part1, corrected_part2)
     c. Remplacer les corrected_text dans le résultat par les textes réconciliés
     d. Stocker resolved_subs_content sur les deux LineManifest
  6. Appliquer les corrections finales aux LineManifest
```

### Politique de retry — cas spécifique aux paires de césure

Si la validation échoue avec `"hyphen_integrity_violation"` :
- Ne pas downgrader la granularité
- Retry immédiat avec temperature=0 et prompt plus explicite sur la règle 13
- Si second échec : conserver les textes OCR source pour les deux lignes de la paire

### Politique générale de retry

| Tentative | Action |
|-----------|--------|
| 1 | Appel normal |
| 2 | Retry même chunk, temperature=0 |
| 3 | Retry encore |
| Après 3 échecs | Downgrade granularité |
| Plus de granularité | Conserver texte OCR source, logger warning |

---

## Tests obligatoires

### `test_chunk_planner.py`
- Cas page, bloc, fenêtre, ligne
- Une paire PART1/PART2 n'est jamais séparée par une frontière de fenêtre
- Downgrade granularité

### `test_validator.py`
- Réponse valide
- Missing/doublon/inconnu line_id
- Newline dans text
- `hyphen_integrity_violation` : PART2 vide ou PART1 contient tout le mot
