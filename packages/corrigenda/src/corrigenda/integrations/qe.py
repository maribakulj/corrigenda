"""Zero-shot masked-LM quality estimator — the ``corrigenda[qe]`` scorer.

Behind the pure-core :class:`~corrigenda.core.quality.QEScorer` protocol,
:class:`MaskedLMQEScorer` answers the same pre-LLM question the
:class:`~corrigenda.core.quality.HeuristicQEScorer` baseline can only
guess at: *does this SOURCE line still carry an OCR error, or is it
already clean?* It reads the masked **pseudo-perplexity** (Salazar et
al. 2020, "Masked Language Model Scoring") of D'AlemBERT — a RoBERTa
masked LM pre-trained on early-modern French (``pjox/dalembert``,
Apache-2.0) — as a surprisal signal: a token the period language model
finds improbable is a likely OCR break.

Two doctrine points make the zero-shot signal safe (both measured on the
OCR17+ corpus, 2026-07-24):

* **Historical orthography is never an error signal** (ROADMAP rule 3).
  D'AlemBERT knows period spelling — ``eſt``/``auoir`` are native vocab
  tokens — but it carries a mild TYPOGRAPHIC penalty for the long-s and
  ligature GLYPHS (its training corpus mixed long-s-normalized editions).
  So the scorer reads a **glyph-neutralized copy** (``ſ→s``, ligatures →
  ASCII): the perplexity measures linguistic implausibility, not
  typography, and the document text is never touched.
* **The model informs, the app decides.** This returns a number; the
  Router (:func:`~corrigenda.core.quality.route_line`) decides.

Heavy deps are LAZY and confined to this module — the pixel-light core
never imports it (import-contract test). Runtime needs only
``onnxruntime`` + ``tokenizers`` (no torch, no transformers); the ONNX
bundle is produced offline by ``scripts/export_dalembert_onnx.py``.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Literal

#: GLYPH-only normalization: typography, NOT language. Applied to a
#: throwaway copy before scoring so the surprisal is glyph-neutral (see
#: module docstring). Kept in lockstep with the same table in
#: ``scripts/export_dalembert_onnx.py``. No substitution introduces
#: whitespace, so word count and order are preserved 1:1 with the source.
_DEGLYPH: dict[str, str] = {
    "ſ": "s",
    "ﬁ": "fi",
    "ﬂ": "fl",
    "ﬀ": "ff",
    "ﬃ": "ffi",
    "ﬄ": "ffl",
    "æ": "ae",
    "œ": "oe",
    "Æ": "Ae",
    "Œ": "Oe",
}

#: Default location of the ONNX bundle (``model.onnx`` + ``tokenizer.json``
#: + ``qe_model.json``) that ``scripts/export_dalembert_onnx.py`` writes.
DEFAULT_MODEL_DIR = Path.home() / ".cache" / "corrigenda" / "dalembert-onnx"

#: Platt scaling of a word's masked surprisal (nats) into P(needs
#: correction). The LM is zero-shot — these two constants only RESCALE
#: its surprisal onto [0, 1]; they never train the model. Fit offline on
#: the Phase-2 QE labels via ``scripts/qe_benchmark.py --fit`` (SYNTHETIC
#: split only, so the scarce real gold stays a test set; default
#: ``word_reducer="max"``).
DEFAULT_SURPRISAL_MIDPOINT = 10.94
DEFAULT_SURPRISAL_SCALE = 6.72

#: Cap on masked variants forwarded per ONNX call (bounds peak memory:
#: rows × seq × vocab floats). A long line is scored in several batches.
_BATCH_ROWS = 16


def _deglyph(text: str) -> str:
    """Return ``text`` with archaic GLYPHS mapped to their modern
    typographic equivalent — for SCORING only. The document is never
    rewritten; historical orthography is preserved (ROADMAP rule 3)."""
    for archaic, modern in _DEGLYPH.items():
        text = text.replace(archaic, modern)
    return text


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    z = math.exp(x)
    return z / (1.0 + z)


class MaskedLMQEScorer:
    """Zero-shot QE scorer over a masked LM's pseudo-perplexity.

    Implements :class:`~corrigenda.core.quality.QEScorer`:
    ``needs_correction(text)`` returns, in ``[0, 1]``, the mean
    per-word probability that a word needs correction — higher means the
    line more likely carries an OCR error. Deterministic (greedy masked
    forward passes, no sampling). Reads only the text; calls no provider
    and mutates nothing.

    The model bundle is loaded LAZILY on first score, so constructing the
    scorer is cheap and importing this module never requires the extra to
    be installed. ``model_dir`` defaults to :data:`DEFAULT_MODEL_DIR`
    (produced by ``scripts/export_dalembert_onnx.py``); a clear error
    names the missing bundle or the missing ``corrigenda[qe]`` deps.
    """

    def __init__(
        self,
        model_dir: str | Path | None = None,
        *,
        name: str = "dalembert-qe",
        surprisal_midpoint: float = DEFAULT_SURPRISAL_MIDPOINT,
        surprisal_scale: float = DEFAULT_SURPRISAL_SCALE,
        word_reducer: Literal["mean", "max"] = "max",
    ) -> None:
        if surprisal_scale <= 0.0:
            raise ValueError("surprisal_scale must be > 0")
        self.name = name
        self._model_dir = (
            Path(model_dir) if model_dir is not None else DEFAULT_MODEL_DIR
        )
        self._midpoint = surprisal_midpoint
        self._scale = surprisal_scale
        self._reducer = word_reducer
        self._session: Any = None
        self._tokenizer: Any = None
        self._mask_id: int = -1

    # -- lazy model loading -------------------------------------------------

    def _ensure_loaded(self) -> None:
        if self._session is not None:
            return
        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ModuleNotFoundError as exc:  # pragma: no cover - env-dependent
            raise ModuleNotFoundError(
                "MaskedLMQEScorer needs the 'qe' extra: pip install 'corrigenda[qe]'"
            ) from exc

        manifest_path = self._model_dir / "qe_model.json"
        onnx_path = self._model_dir / "model.onnx"
        tok_path = self._model_dir / "tokenizer.json"
        if not (manifest_path.exists() and onnx_path.exists() and tok_path.exists()):
            raise FileNotFoundError(
                f"D'AlemBERT ONNX bundle not found under {self._model_dir}. "
                "Build it with: python scripts/export_dalembert_onnx.py "
                f"--out {self._model_dir}"
            )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self._mask_id = int(manifest["mask_token_id"])
        self._tokenizer = Tokenizer.from_file(str(tok_path))
        self._session = ort.InferenceSession(
            str(onnx_path), providers=["CPUExecutionProvider"]
        )

    # -- scoring ------------------------------------------------------------

    def _subword_nll(self, ids: list[int], maskable: list[int]) -> dict[int, float]:
        """Masked negative log-likelihood of each maskable position.

        For every position we forward a copy of the sequence with just
        that position masked and read the log-prob the LM assigns to the
        true token there (pseudo-log-likelihood). Positions are batched
        (``_BATCH_ROWS`` at a time) into single ONNX calls."""
        import numpy as np

        base = np.asarray(ids, dtype=np.int64)
        input_names = {i.name for i in self._session.get_inputs()}
        nll: dict[int, float] = {}
        for start in range(0, len(maskable), _BATCH_ROWS):
            chunk = maskable[start : start + _BATCH_ROWS]
            batch = np.tile(base, (len(chunk), 1))
            for row, pos in enumerate(chunk):
                batch[row, pos] = self._mask_id
            feeds: dict[str, Any] = {
                "input_ids": batch,
                "attention_mask": np.ones_like(batch),
            }
            if "token_type_ids" in input_names:
                feeds["token_type_ids"] = np.zeros_like(batch)
            logits = self._session.run(None, feeds)[0]
            for row, pos in enumerate(chunk):
                vec = logits[row, pos].astype(np.float64)
                vec -= vec.max()  # softmax stability; cancels in the ratio
                log_z = math.log(float(np.exp(vec).sum()))
                nll[pos] = log_z - float(vec[base[pos]])
        return nll

    def _word_surprisals(self, text: str) -> list[tuple[str, float]]:
        """Per-word surprisal (nats) for every alnum-bearing source word.

        Scores a glyph-neutralized copy; word ``w`` maps back to the
        original ``text.split()[w]`` (glyph substitution preserves word
        boundaries). Punctuation-only words are dropped, exactly like the
        heuristic baseline's token filter."""
        self._ensure_loaded()
        original_words = text.split()
        encoding = self._tokenizer.encode(_deglyph(text))
        ids: list[int] = list(encoding.ids)
        word_ids: list[int | None] = list(encoding.word_ids)

        positions_by_word: dict[int, list[int]] = {}
        for pos, wid in enumerate(word_ids):
            if wid is not None:
                positions_by_word.setdefault(wid, []).append(pos)
        if not positions_by_word:
            return []

        maskable = [pos for pos, wid in enumerate(word_ids) if wid is not None]
        nll = self._subword_nll(ids, maskable)

        out: list[tuple[str, float]] = []
        for wid in sorted(positions_by_word):
            # Fall back to the tokenizer's own word if the 1:1 mapping to
            # ``text.split()`` ever breaks (defensive; deglyph keeps them
            # aligned in practice).
            word = original_words[wid] if wid < len(original_words) else ""
            if not any(c.isalnum() for c in word):
                continue
            subword_nlls = [nll[pos] for pos in positions_by_word[wid]]
            surprisal = (
                max(subword_nlls)
                if self._reducer == "max"
                else sum(subword_nlls) / len(subword_nlls)
            )
            out.append((word, surprisal))
        return out

    def score_words(self, text: str) -> list[tuple[str, float]]:
        """Public per-word view: ``(word, P(needs correction))`` for each
        alnum word, after Platt scaling. Feeds token-level measurement
        and, later, a named component of the confidence block."""
        return [
            (word, _sigmoid((surprisal - self._midpoint) / self._scale))
            for word, surprisal in self._word_surprisals(text)
        ]

    def needs_correction(self, text: str) -> float:
        """QEScorer contract: the line's need for correction in ``[0, 1]``,
        taken as the MOST-suspicious word's calibrated error probability
        (``max`` over words). Max, not mean: routing asks "does ANY word
        likely need fixing?", and a mean dilutes one real error across a
        line of clean archaic words — measured line AUC 0.77 (real) / 0.88
        (synthetic) for max vs 0.47 / 0.62 for the mean, and max carries no
        length bias so a long clean line still SKIPs. ``0.0`` for an empty
        or punctuation-only line — the conservative answer the heuristic
        baseline also gives."""
        probs = [p for _, p in self.score_words(text)]
        if not probs:
            return 0.0
        return max(probs)


__all__ = [
    "DEFAULT_MODEL_DIR",
    "DEFAULT_SURPRISAL_MIDPOINT",
    "DEFAULT_SURPRISAL_SCALE",
    "MaskedLMQEScorer",
]
