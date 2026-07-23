#!/usr/bin/env python3
"""Fit Platt calibration for a QE bundle on a target-register corpus.

``MaskedLMQEScorer`` is model-agnostic; each period wants its OWN model +
constants (D'AlemBERT for 16-18th c., a contemporary model for 19th-c.
press). This fits the 2-parameter surprisal→P(error) scaling for ANY
bundle on ANY clean-text corpus (one sentence per line) by scripted OCR
degradation, and can PATCH the bundle manifest so it becomes
self-describing (the scorer then reads the right constants automatically).

    python scripts/fit_qe_calibration.py \
        --model-dir ~/.cache/corrigenda/camembert-onnx \
        --sentences scripts/data/press19_clean.txt --reducer max --write

Provisional by nature: the degradations are scripted (not real OCR) and
the sample sentences are 19th-c. press pastiche. Refit on a real labeled
corpus (Gallica press — ROADMAP Phase 2) before trusting the absolute
thresholds; the token AUC (ranking) is already meaningful.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "packages" / "corrigenda" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from corrigenda.integrations.qe import MaskedLMQEScorer, _sigmoid  # noqa: E402
from qe_benchmark import auc, fit_platt  # noqa: E402

# Classic OCR confusions on MODERN print (late-19th c.), clean→OCR. Unlike
# the early-modern set in qe_data.py (long-s, ligatures), these are the
# letter confusions that survive standardized orthography.
DEGRADATIONS = [
    ("m", "rn"),
    ("l", "1"),
    ("o", "0"),
    ("u", "n"),
    ("e", "c"),
    ("n", "u"),
]


def _gate(key: str, rate: int) -> bool:
    return int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) % 100 < rate


def degrade(text: str, rate: int) -> tuple[str, list[int]]:
    out, labels = [], []
    for i, tok in enumerate(text.split()):
        new = tok
        for clean, ocr in DEGRADATIONS:
            if clean in tok and _gate(f"{i}:{clean}:{tok}", rate):
                new = tok.replace(clean, ocr, 1)
                break
        out.append(new)
        labels.append(1 if new != tok else 0)
    return " ".join(out), labels


def _alnum(t: str) -> bool:
    return any(c.isalnum() for c in t)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--sentences", type=Path, required=True)
    parser.add_argument("--rate", type=int, default=30)
    parser.add_argument("--reducer", choices=["mean", "max"], default="max")
    parser.add_argument(
        "--write", action="store_true", help="patch the bundle manifest's calibration"
    )
    args = parser.parse_args(argv)

    scorer = MaskedLMQEScorer(model_dir=args.model_dir, word_reducer=args.reducer)
    # Per line, keep the CLEAN word surprisals (a negative, label-0 line
    # for routing) and the DEGRADED ones with per-word labels (token fit +
    # the positive, label-1 line).
    dirty: list[tuple[list[float], list[int]]] = []
    clean_lines: list[list[float]] = []
    skipped = 0
    for line in args.sentences.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        ocr, labs = degrade(line, args.rate)
        alnum_labs = [l for t, l in zip(ocr.split(), labs) if _alnum(t)]
        words = scorer._word_surprisals(ocr)  # raw surprisal, pre-Platt
        if len(words) != len(alnum_labs):
            skipped += 1
            continue
        dirty.append(([s for _, s in words], alnum_labs))
        clean_lines.append([s for _, s in scorer._word_surprisals(line)])

    surpr = [s for sl, _ in dirty for s in sl]
    labels = [l for _, ll in dirty for l in ll]
    clean = [s for s, l in zip(surpr, labels) if not l]
    err = [s for s, l in zip(surpr, labels) if l]
    midpoint, scale = fit_platt(surpr, labels)

    # Choose the line reducer by LINE-level AUC on clean (label 0) vs
    # degraded (label 1) lines — the actual routing question. max = "any
    # word suspect"; mean = "fraction suspect". A contemporary model on
    # proper-noun-heavy press wins with mean (proper nouns spike a few
    # words; max false-positives, the mean averages them out); a period
    # model on dense-error text wins with max. Let the data decide.
    def prob(s: float) -> float:
        return _sigmoid((s - midpoint) / scale)

    line_words = [sl for sl in clean_lines] + [sl for sl, _ in dirty]
    line_labels = [0] * len(clean_lines) + [
        1 if any(ll) else 0 for _, ll in dirty
    ]
    line_max = [max(prob(s) for s in sl) for sl in line_words]
    line_mean = [sum(prob(s) for s in sl) / len(sl) for sl in line_words]
    auc_max, auc_mean = auc(line_max, line_labels), auc(line_mean, line_labels)
    line_reducer = "mean" if auc_mean > auc_max else "max"

    report = {
        "model_dir": str(args.model_dir),
        "tokens": len(labels),
        "errors": sum(labels),
        "skipped_lines": skipped,
        "token_auc": round(auc(surpr, labels), 4),
        "line_auc_max": round(auc_max, 4),
        "line_auc_mean": round(auc_mean, 4),
        "clean_surprisal_mean": round(sum(clean) / len(clean), 3) if clean else None,
        "error_surprisal_mean": round(sum(err) / len(err), 3) if err else None,
        "calibration": {
            "surprisal_midpoint": round(midpoint, 4),
            "surprisal_scale": round(scale, 4),
            "word_reducer": args.reducer,
            "line_reducer": line_reducer,
        },
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))

    if args.write:
        mpath = args.model_dir / "qe_model.json"
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
        manifest["calibration"] = {
            **report["calibration"],
            "fit": "provisional (scripted OCR degradation on 19c press pastiche)",
        }
        mpath.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"patched {mpath}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
