#!/usr/bin/env python3
"""Dev-time export of D'AlemBERT (masked LM) to ONNX for the QE scorer.

This is TOOLING, not part of the ``corrigenda[qe]`` extra: it needs the
heavy conversion stack (``optimum``, ``transformers``, ``torch``) exactly
ONCE, offline, to produce a self-contained runtime bundle:

    <out>/model.onnx        the RoBERTa masked-LM graph (dynamic axes)
    <out>/tokenizer.json    the fast byte-level BPE tokenizer
    <out>/qe_model.json     the little manifest the scorer reads
                            (mask id, special ids, model id, sha, license)

At RUNTIME the scorer loads only that bundle with ``onnxruntime`` +
``tokenizers`` — no torch, no transformers (§ ROADMAP Phase 3: "onnxruntime,
pas torch"). D'AlemBERT is Apache-2.0 (``pjox/dalembert``), so the bundle
may be redistributed; a maintainer can publish it to a HF repo the scorer
downloads, which is why the manifest records provenance.

Usage:
    python scripts/export_dalembert_onnx.py --out ~/.cache/corrigenda/dalembert-onnx
    python scripts/export_dalembert_onnx.py --out <dir> --validate   # + parity checks

``--validate`` proves the export is faithful (ONNX logits ≈ torch logits)
and re-confirms the ÉTAPE 0 verdict THROUGH the runtime path (tokenizers +
onnxruntime + numpy), so the signal that justified the scorer still holds.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

MODEL_ID = "pjox/dalembert"

# GLYPH-only normalization (typography, NOT language) — kept in lockstep
# with corrigenda.integrations.qe._DEGLYPH. Scoring-only; documents are
# never rewritten (ROADMAP rule 3: historical orthography is preserved).
_DEGLYPH = {"ſ": "s", "ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi",
            "ﬄ": "ffl", "æ": "ae", "œ": "oe", "Æ": "Ae", "Œ": "Oe"}


def deglyph(text: str) -> str:
    for a, b in _DEGLYPH.items():
        text = text.replace(a, b)
    return text


def export(out: Path) -> None:
    from optimum.onnxruntime import ORTModelForMaskedLM
    from transformers import AutoTokenizer

    out.mkdir(parents=True, exist_ok=True)
    print(f"exporting {MODEL_ID} -> {out} (this converts the torch graph once)")
    model = ORTModelForMaskedLM.from_pretrained(MODEL_ID, export=True)
    model.save_pretrained(out)
    tok = AutoTokenizer.from_pretrained(MODEL_ID)
    tok.save_pretrained(out)

    # Normalize the graph file name to a stable model.onnx.
    onnx_files = sorted(out.glob("*.onnx"))
    if not onnx_files:
        raise SystemExit("export produced no .onnx file")
    if onnx_files[0].name != "model.onnx":
        onnx_files[0].rename(out / "model.onnx")

    mask_id = tok.mask_token_id
    manifest = {
        "model_id": MODEL_ID,
        "license": "Apache-2.0",
        "kind": "masked-lm-pll-qe",
        "mask_token_id": int(mask_id),
        "mask_token": tok.mask_token,
        "special_token_ids": sorted(int(i) for i in tok.all_special_ids),
        "onnx_sha256": hashlib.sha256(
            (out / "model.onnx").read_bytes()
        ).hexdigest(),
        "note": "Built by scripts/export_dalembert_onnx.py; runtime uses "
        "onnxruntime + tokenizers only.",
    }
    (out / "qe_model.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"wrote model.onnx, tokenizer.json, qe_model.json  (mask id {mask_id})")


# ---------------------------------------------------------------------------
# Validation — parity + the ÉTAPE 0 discrimination re-check through the
# runtime path (tokenizers + onnxruntime + numpy pseudo-PPL).
# ---------------------------------------------------------------------------

REF_LINES = [  # clean human reference, period orthography — must score LOW
    "qu'il eſt bon de les auoir",
    "ſciences apportent des honneurs & des richeſſes a ceux",
    "Il eſt bon de ſçauoir quelque choſe des meurs",
    "fauſſes, affin de connoiſtre leur iuſte valeur, & ſe garder",
]
RAW_LINES = [  # real raw OCR with genuine errors — must score HIGH
    "qu'il eû bon de les auoir",
    "ſcicnces apporrent des honneurs",
    "Il efi bon de fçauoir quelque chofe",
    "connoître leur iufte valcur",
]


def _pll_ppl_onnx(session, tk, text: str, mask_id: int) -> float:
    """Masked pseudo-perplexity via the RUNTIME stack (numpy + ORT)."""
    import numpy as np

    enc = tk.encode(deglyph(text))
    ids = np.asarray(enc.ids, dtype=np.int64)
    word_ids = enc.word_ids
    positions = [i for i, w in enumerate(word_ids) if w is not None]
    if not positions:
        return float("nan")
    # Batch: one row per masked position (all rows share seq length).
    batch = np.tile(ids, (len(positions), 1))
    for row, pos in enumerate(positions):
        batch[row, pos] = mask_id
    attn = np.ones_like(batch)
    feeds = {"input_ids": batch, "attention_mask": attn}
    input_names = {i.name for i in session.get_inputs()}
    if "token_type_ids" in input_names:
        feeds["token_type_ids"] = np.zeros_like(batch)
    logits = session.run(None, feeds)[0]  # [rows, seq, vocab]
    total_ll = 0.0
    for row, pos in enumerate(positions):
        vec = logits[row, pos]
        vec = vec - vec.max()
        logp = vec - math.log(float(np.exp(vec).sum()))
        total_ll += float(logp[int(ids[pos])])
    return math.exp(-total_ll / len(positions))


def validate(out: Path) -> int:
    import numpy as np
    import onnxruntime as ort
    import torch
    from tokenizers import Tokenizer
    from transformers import AutoModelForMaskedLM, AutoTokenizer

    manifest = json.loads((out / "qe_model.json").read_text(encoding="utf-8"))
    mask_id = manifest["mask_token_id"]
    session = ort.InferenceSession(str(out / "model.onnx"))
    tk = Tokenizer.from_file(str(out / "tokenizer.json"))

    print("=" * 68)
    print("PARITY: ONNX logits vs torch logits on a sample line")
    print("=" * 68)
    hf_tok = AutoTokenizer.from_pretrained(str(out))
    torch_model = AutoModelForMaskedLM.from_pretrained(MODEL_ID).eval()
    sample = "qu'il est bon de les auoir"
    enc = hf_tok(sample, return_tensors="pt")
    with torch.no_grad():
        torch_logits = torch_model(**enc).logits.numpy()
    feeds = {k: v.numpy() for k, v in enc.items()
             if k in {i.name for i in session.get_inputs()}}
    onnx_logits = session.run(None, feeds)[0]
    max_abs = float(np.abs(torch_logits - onnx_logits).max())
    argmax_match = bool(
        (torch_logits.argmax(-1) == onnx_logits.argmax(-1)).all()
    )
    print(f"  max |Δlogit| = {max_abs:.5f}   argmax identical: {argmax_match}")
    parity_ok = max_abs < 1e-2 and argmax_match
    print(f"  parity {'OK' if parity_ok else 'FAILED'}")
    print()

    print("=" * 68)
    print("ÉTAPE 0 re-check THROUGH the runtime path (tokenizers+ORT+numpy)")
    print("=" * 68)
    ref = [_pll_ppl_onnx(session, tk, x, mask_id) for x in REF_LINES]
    raw = [_pll_ppl_onnx(session, tk, x, mask_id) for x in RAW_LINES]
    print(f"  clean refs : {[round(p, 1) for p in ref]}  max {max(ref):.1f}")
    print(f"  raw OCR    : {[round(p, 1) for p in raw]}  min {min(raw):.1f}")
    separated = min(raw) > max(ref)
    print(f"  separation ref<raw: {separated}  "
          f"[max ref {max(ref):.1f} < min raw {min(raw):.1f}]")
    print()
    ok = parity_ok and separated
    print(f"VALIDATION {'PASSED' if ok else 'FAILED'}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path.home() / ".cache" / "corrigenda" / "dalembert-onnx",
    )
    parser.add_argument("--validate", action="store_true")
    parser.add_argument(
        "--skip-export", action="store_true", help="validate an existing bundle"
    )
    args = parser.parse_args(argv)
    if not args.skip_export:
        export(args.out)
    if args.validate:
        return validate(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
