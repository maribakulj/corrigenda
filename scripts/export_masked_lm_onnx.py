#!/usr/bin/env python3
"""Dev-time export of a masked-LM to an ONNX QE bundle (ROADMAP V3 Phase 3).

Tooling, NOT part of the ``corrigenda[qe]`` extra: it needs the heavy
conversion stack (``optimum``, ``transformers``, ``torch``) once, offline,
to produce a self-contained runtime bundle any ``MaskedLMQEScorer`` can
load with ``onnxruntime`` + ``tokenizers`` alone:

    <out>/model.onnx        the masked-LM graph (dynamic axes)
    <out>/tokenizer.json    the fast tokenizer
    <out>/qe_model.json     manifest: mask id, provenance, and — for a
                            non-default model — its own ``calibration``
                            block (Platt midpoint/scale + word reducer),
                            so the bundle is SELF-DESCRIBING and the scorer
                            picks the right constants automatically.

The scorer is model-agnostic, so this is how a DIFFERENT period gets its
own model behind the same seam:

    # 16-18th c. — D'AlemBERT (the module defaults already match it):
    python scripts/export_masked_lm_onnx.py \
        --model-id pjox/dalembert --license Apache-2.0 \
        --out ~/.cache/corrigenda/dalembert-onnx --validate

    # late-19th c. press — CamemBERT, with its own fitted calibration:
    python scripts/export_masked_lm_onnx.py \
        --model-id camembert-base --license MIT \
        --midpoint 8.4 --scale 3.1 --reducer max \
        --out ~/.cache/corrigenda/camembert-onnx --validate

``--validate`` proves the export is faithful (ONNX logits ≈ torch, model-
agnostic). Fit ``--midpoint/--scale`` offline with
``scripts/qe_benchmark.py --fit`` on a period-appropriate corpus.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def export(
    model_id: str,
    out: Path,
    *,
    license_id: str,
    calibration: dict[str, float | str] | None,
) -> None:
    from optimum.onnxruntime import ORTModelForMaskedLM
    from transformers import AutoTokenizer

    out.mkdir(parents=True, exist_ok=True)
    print(f"exporting {model_id} -> {out} (converts the torch graph once)")
    model = ORTModelForMaskedLM.from_pretrained(model_id, export=True)
    model.save_pretrained(out)
    tok = AutoTokenizer.from_pretrained(model_id)
    tok.save_pretrained(out)

    onnx_files = sorted(out.glob("*.onnx"))
    if not onnx_files:
        raise SystemExit("export produced no .onnx file")
    if onnx_files[0].name != "model.onnx":
        onnx_files[0].rename(out / "model.onnx")

    manifest: dict[str, object] = {
        "model_id": model_id,
        "license": license_id,
        "kind": "masked-lm-pll-qe",
        "mask_token_id": int(tok.mask_token_id),
        "mask_token": tok.mask_token,
        "special_token_ids": sorted(int(i) for i in tok.all_special_ids),
        "onnx_sha256": hashlib.sha256((out / "model.onnx").read_bytes()).hexdigest(),
        "note": "Built by scripts/export_masked_lm_onnx.py; runtime uses "
        "onnxruntime + tokenizers only.",
    }
    if calibration:
        # Omitted for the default (D'AlemBERT) model — the module defaults
        # already match it; a non-default bundle carries its own constants.
        manifest["calibration"] = calibration
    (out / "qe_model.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"wrote model.onnx, tokenizer.json, qe_model.json (mask id {tok.mask_token_id})")
    if calibration:
        print(f"  calibration: {calibration}")


def validate(out: Path, model_id: str) -> int:
    """Model-agnostic faithfulness check: ONNX logits ≈ torch logits."""
    import numpy as np
    import onnxruntime as ort
    import torch
    from transformers import AutoModelForMaskedLM, AutoTokenizer

    session = ort.InferenceSession(str(out / "model.onnx"))
    hf_tok = AutoTokenizer.from_pretrained(str(out))
    torch_model = AutoModelForMaskedLM.from_pretrained(model_id).eval()
    names = {i.name for i in session.get_inputs()}

    samples = [
        "Le télégraphe annonce que Monsieur prononcera un discours",
        "qu'il est bon de les avoir",
        "La séance de la Chambre a été levée à sept heures",
    ]
    max_abs = 0.0
    argmax_ok = True
    for s in samples:
        enc = hf_tok(s, return_tensors="pt")
        with torch.no_grad():
            tl = torch_model(**enc).logits.numpy()
        feeds = {k: v.numpy() for k, v in enc.items() if k in names}
        ol = session.run(None, feeds)[0]
        max_abs = max(max_abs, float(np.abs(tl - ol).max()))
        argmax_ok = argmax_ok and bool((tl.argmax(-1) == ol.argmax(-1)).all())
    print("=" * 60)
    print(f"PARITY  max |Δlogit| = {max_abs:.2e}   argmax identical: {argmax_ok}")
    ok = max_abs < 1e-2 and argmax_ok
    print(f"VALIDATION {'PASSED' if ok else 'FAILED'}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-id", default="pjox/dalembert")
    parser.add_argument("--license", dest="license_id", default="Apache-2.0")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path.home() / ".cache" / "corrigenda" / "dalembert-onnx",
    )
    parser.add_argument("--midpoint", type=float, default=None)
    parser.add_argument("--scale", type=float, default=None)
    parser.add_argument("--reducer", choices=["mean", "max"], default=None)
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--skip-export", action="store_true")
    args = parser.parse_args(argv)

    calibration: dict[str, float | str] = {}
    if args.midpoint is not None:
        calibration["surprisal_midpoint"] = args.midpoint
    if args.scale is not None:
        calibration["surprisal_scale"] = args.scale
    if args.reducer is not None:
        calibration["word_reducer"] = args.reducer

    if not args.skip_export:
        export(
            args.model_id,
            args.out,
            license_id=args.license_id,
            calibration=calibration or None,
        )
    if args.validate:
        return validate(args.out, args.model_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
