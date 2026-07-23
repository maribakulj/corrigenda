# QE scorer — zero-shot masked-LM quality estimation (`corrigenda[qe]`)

The quality-estimation scorer answers the pre-LLM question the Phase-2
calibration proved was missing: **does this source line still carry an OCR
error, or is it already clean?** A high score routes a line to the LLM (or
an escalation); a low score lets the Router SKIP it with no model call —
the hybrid-selective economics.

`MaskedLMQEScorer` (in `corrigenda.integrations.qe`, behind the pure-core
`QEScorer` protocol) reads the **masked pseudo-perplexity** of a
pre-trained masked language model (Salazar et al. 2020): a token the
language model finds improbable is a likely OCR break. **Zero-shot** — no
QE training; the model informs, the app decides.

- Runtime deps: `onnxruntime` + `tokenizers` only (**no torch, no
  transformers**). Install with `pip install 'corrigenda[qe]'`.
- The pixel-light core never imports it (import-contract test); heavy
  imports are lazy.
- Historical orthography is **never** an error signal (README rule 3): the
  scorer reads a glyph-neutralized copy (`ſ→s`, ligatures → ASCII) so
  perplexity measures language, not typography — the document is untouched.

## The scorer is model- and register-adaptive

One model does not fit every period. The scorer is agnostic: it loads an
ONNX **bundle** (`model.onnx` + `tokenizer.json` + `qe_model.json`) built
offline by `scripts/export_masked_lm_onnx.py`, and every bundle is
**self-describing** — its `qe_model.json` carries the calibration the
scorer needs, so pointing `model_dir` at the right bundle is all it takes:

```python
from corrigenda.integrations.qe import MaskedLMQEScorer

qe = MaskedLMQEScorer(model_dir="~/.cache/corrigenda/camembert-onnx")
score = qe.needs_correction("Le télégrapbe annonee que Mousieur…")  # 0..1
```

The manifest's `calibration` block holds three knobs, each fit per bundle:

| knob | meaning |
| --- | --- |
| `surprisal_midpoint`, `surprisal_scale` | Platt scaling of word surprisal → P(error). Only rescales the zero-shot signal onto `[0, 1]`; never trains the model. |
| `word_reducer` (`max`/`mean`) | subword → word surprisal. |
| `line_reducer` (`max`/`mean`) | word probabilities → line score. **Register-dependent** (see below). |

## Recommended models per period

Measured on labelled OCR data (`scripts/qe_benchmark.py`,
`scripts/fit_qe_calibration.py`):

| period / material | model (`--model-id`) | licence | token AUC | line score clean → OCR | `line_reducer` |
| --- | --- | --- | --- | --- | --- |
| 16–18th c. print | `pjox/dalembert` (D'AlemBERT) | Apache-2.0 | 0.66 | 0.77 line AUC | `max` |
| **late-19th c. press** | **`camembert-base`** (CamemBERT) | MIT | **0.98** | **0.14 → 0.51** | **`mean`** |
| historic FR newspapers (alt.) | `dbmdz/bert-base-french-europeana-cased` | MIT | 0.90 | 0.63 → 0.96 | `max` |

Why `line_reducer` flips with the register:

- **16–18th c. (D'AlemBERT, `max`).** Clean lines are word-sparse and
  errors dense; `max` ("does ANY word look wrong?") catches the single
  error a `mean` would dilute (line AUC 0.77 vs 0.47).
- **19th-c. press (CamemBERT, `mean`).** Press is dense with proper nouns
  (Gambetta, Haussmann, Marseille) a contemporary model rarely saw, so a
  few clean words spike. `max` false-positives on them (clean line score
  0.90!); `mean` ("what fraction look wrong?") averages the tail out and
  the strong word-level signal (AUC 0.98) separates clean (0.14) from OCR
  (0.51). `fit_qe_calibration.py` picks the reducer by line-level AUC.

CamemBERT is the recommendation for 19th-c. press: best discrimination and
lowest clean-line false-positives once aggregated by the mean.
Europeana-BERT is in-domain (trained on OCR'd historic newspapers) and
calm on proper nouns, but its OCR-tolerance weakens the error signal.

## Building and calibrating a bundle

```bash
# 1. Export (needs the dev-time optimum/torch stack, ONCE, offline):
python scripts/export_masked_lm_onnx.py \
    --model-id camembert-base --license MIT \
    --out ~/.cache/corrigenda/camembert-onnx --validate

# 2. Fit the calibration on a target-register corpus (one clean line per
#    file line) and patch the bundle manifest:
python scripts/fit_qe_calibration.py \
    --model-dir ~/.cache/corrigenda/camembert-onnx \
    --sentences scripts/data/press19_clean.txt --write
```

`--validate` proves the ONNX export is faithful to torch (logits within
fp tolerance, identical argmax).

### Adding a new period

1. Pick a masked LM whose training register matches the material.
2. Export it to a bundle (`export_masked_lm_onnx.py --model-id …`).
3. Fit its calibration on a period-appropriate clean corpus
   (`fit_qe_calibration.py --write`); the reducer is chosen for you.
4. Point `MaskedLMQEScorer(model_dir=…)` at it.

The D'AlemBERT (16–18th c.) constants are the module defaults, so its
bundle needs no `calibration` block.

## Honest limitations

- Calibration constants shipped for 19th-c. press are **provisional**: fit
  on scripted OCR degradations of hand-written press pastiche, not a real
  labelled corpus. Token AUC (ranking) is meaningful; the absolute
  thresholds should be refit on real Gallica/BnF press (ROADMAP Phase 2)
  before trusting SKIP/ESCALATE bounds in production.
- Pseudo-perplexity costs one masked forward pass per subword (batched per
  line). It is a QE gate that *saves* expensive LLM calls, but it is not
  free; batch across lines for throughput.
- The model bundle (~0.4–0.5 GB) is built locally and not shipped in the
  wheel. D'AlemBERT (Apache-2.0) and CamemBERT/Europeana (MIT) permit
  redistribution with attribution, so a maintainer may host pre-exported
  ONNX bundles later.
