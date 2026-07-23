#!/usr/bin/env python3
"""QE scorer bake-off (ROADMAP V3 Phase 3): D'AlemBERT vs the heuristic.

Answers the Phase-3 exit question with numbers a PR can cite: does the
zero-shot D'AlemBERT masked-LM scorer (``corrigenda[qe]``) beat the
zero-dependency :class:`HeuristicQEScorer` at telling a line/token that
still needs correction from one that is already clean?

It scores the token-labeled data from ``scripts/qe_data.py`` (the same
real raw↔ref alignment and synthetic degradations) and reports, per
source and pooled:

* **token AUC** — ranking power, invariant to any monotonic calibration
  (this is the headline "does the signal separate errors from clean");
* **line AUC** — same, at line granularity (any-error label);
* **ECE / Brier** — calibration of the probability, using the SAME
  ``_calibration_metrics`` bins as ``scripts/benchmark.py``.

``--fit`` fits the 2-parameter Platt scaling (surprisal midpoint/scale)
on the SYNTHETIC split by 1-D logistic regression and prints the
constants to bake into ``integrations/qe.py`` — the LM stays zero-shot;
only its surprisal is rescaled onto [0, 1]. Real gold is never fit on,
so it stays an honest test set.

    python scripts/qe_benchmark.py --fit          # print Platt constants
    python scripts/qe_benchmark.py                # full comparison table

Dev tooling: needs the ``corrigenda[qe]`` deps and the exported ONNX
bundle (``scripts/export_dalembert_onnx.py``). Never imported by the core.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "packages" / "corrigenda" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import qe_data  # noqa: E402  (sibling script: record generation)
from corrigenda.core.quality import HeuristicQEScorer  # noqa: E402
from corrigenda.integrations.qe import (  # noqa: E402
    DEFAULT_MODEL_DIR,
    MaskedLMQEScorer,
    _sigmoid,
)

DEFAULT_CORPUS = (
    _REPO_ROOT / "packages" / "corrigenda" / "tests" / "corpus_gt" / "manifest.json"
)


# ---------------------------------------------------------------------------
# Metrics (dependency-light; numpy only for the Platt fit)
# ---------------------------------------------------------------------------


def auc(scores: list[float], labels: list[int]) -> float:
    """Tie-aware ROC AUC via average ranks (Mann–Whitney U)."""
    data = sorted(zip(scores, labels, strict=True), key=lambda p: p[0])
    ranks = [0.0] * len(data)
    i = 0
    while i < len(data):
        j = i
        while j < len(data) and data[j][0] == data[i][0]:
            j += 1
        avg = (i + j - 1) / 2 + 1  # 1-based average rank over the tie block
        for k in range(i, j):
            ranks[k] = avg
        i = j
    n_pos = sum(1 for _, lbl in zip(scores, labels, strict=True) if lbl == 1)
    n_neg = len(labels) - n_pos
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    sum_pos = sum(r for r, (_, lbl) in zip(ranks, data, strict=True) if lbl == 1)
    return (sum_pos - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)


def calibration(pairs: list[tuple[float, float]], bins: int = 10) -> dict[str, float]:
    """Brier + expected calibration error — same recipe as benchmark.py."""
    if not pairs:
        return {"brier": float("nan"), "ece": float("nan")}
    brier = sum((p - c) ** 2 for p, c in pairs) / len(pairs)
    ece = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        in_bin = [
            (p, c) for p, c in pairs if (lo <= p < hi) or (b == bins - 1 and p == hi)
        ]
        if not in_bin:
            continue
        conf = sum(p for p, _ in in_bin) / len(in_bin)
        acc = sum(c for _, c in in_bin) / len(in_bin)
        ece += abs(conf - acc) * len(in_bin) / len(pairs)
    return {"brier": round(brier, 4), "ece": round(ece, 4)}


def fit_platt(surprisals: list[float], labels: list[int]) -> tuple[float, float]:
    """1-D logistic regression of label ~ surprisal via Newton/IRLS.

    Returns ``(midpoint, scale)`` such that
    ``P = sigmoid((surprisal - midpoint) / scale)``."""
    import numpy as np

    x = np.asarray(surprisals, dtype=np.float64)
    y = np.asarray(labels, dtype=np.float64)
    X = np.column_stack([x, np.ones_like(x)])  # [surprisal, 1]
    beta = np.zeros(2)
    for _ in range(100):
        p = 1.0 / (1.0 + np.exp(-(X @ beta)))
        w = np.clip(p * (1.0 - p), 1e-6, None)
        grad = X.T @ (p - y)
        hess = (X * w[:, None]).T @ X + 1e-6 * np.eye(2)
        step = np.linalg.solve(hess, grad)
        beta -= step
        if float(np.abs(step).max()) < 1e-9:
            break
    slope, intercept = float(beta[0]), float(beta[1])
    if slope <= 0:  # degenerate; fall back to a gentle default
        return 3.0, 1.0
    return -intercept / slope, 1.0 / slope


# ---------------------------------------------------------------------------
# Data assembly — token- and line-level (score, label) for both scorers
# ---------------------------------------------------------------------------


def _alnum(token: str) -> bool:
    return any(c.isalnum() for c in token)


def collect(records: list[dict], scorer: MaskedLMQEScorer, heuristic: HeuristicQEScorer):
    """Per token: (dalembert surprisal, heuristic flag, label). Per line:
    (dalembert line score, heuristic line score, any-error label)."""
    tok_surprisal: list[float] = []
    tok_heur: list[float] = []
    tok_label: list[int] = []
    line_dalembert: list[float] = []
    line_heur: list[float] = []
    line_label: list[int] = []
    misaligned = 0
    for rec in records:
        pairs = [
            (t, lbl)
            for t, lbl in zip(rec["tokens"], rec["labels"], strict=True)
            if _alnum(t)
        ]
        if not pairs:
            continue
        line = " ".join(rec["tokens"])
        surprisals = scorer._word_surprisals(line)
        if len(surprisals) != len(pairs):
            misaligned += 1
            continue
        for (tok, lbl), (_, sur) in zip(pairs, surprisals, strict=True):
            tok_surprisal.append(sur)
            tok_heur.append(1.0 if heuristic._is_suspicious(tok) else 0.0)
            tok_label.append(int(lbl))
        line_dalembert.append(scorer.needs_correction(line))
        line_heur.append(heuristic.needs_correction(line))
        line_label.append(1 if any(lbl for _, lbl in pairs) else 0)
    return {
        "tok_surprisal": tok_surprisal,
        "tok_heur": tok_heur,
        "tok_label": tok_label,
        "line_dalembert": line_dalembert,
        "line_heur": line_heur,
        "line_label": line_label,
        "misaligned": misaligned,
    }


def evaluate(data: dict, midpoint: float, scale: float) -> dict:
    tok_p = [_sigmoid((s - midpoint) / scale) for s in data["tok_surprisal"]]
    return {
        "tokens": len(data["tok_label"]),
        "token_errors": sum(data["tok_label"]),
        "dalembert": {
            "token_auc": round(auc(data["tok_surprisal"], data["tok_label"]), 4),
            "line_auc": round(auc(data["line_dalembert"], data["line_label"]), 4),
            **calibration(list(zip(tok_p, map(float, data["tok_label"]), strict=True))),
            **{
                "line_"
                + k: v
                for k, v in calibration(
                    list(
                        zip(
                            data["line_dalembert"],
                            map(float, data["line_label"]),
                            strict=True,
                        )
                    )
                ).items()
            },
        },
        "heuristic": {
            "token_auc": round(auc(data["tok_heur"], data["tok_label"]), 4),
            "line_auc": round(auc(data["line_heur"], data["line_label"]), 4),
            **calibration(
                list(zip(data["tok_heur"], map(float, data["tok_label"]), strict=True))
            ),
            **{
                "line_"
                + k: v
                for k, v in calibration(
                    list(
                        zip(
                            data["line_heur"],
                            map(float, data["line_label"]),
                            strict=True,
                        )
                    )
                ).items()
            },
        },
        "misaligned_lines": data["misaligned"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    parser.add_argument("--seed", type=int, default=1637)
    parser.add_argument("--rate", type=int, default=35)
    parser.add_argument("--reducer", choices=["mean", "max"], default="mean")
    parser.add_argument(
        "--fit", action="store_true", help="fit + print Platt constants, then exit"
    )
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    scorer = MaskedLMQEScorer(model_dir=args.model_dir, word_reducer=args.reducer)
    heuristic = HeuristicQEScorer()

    real = qe_data.generate(args.corpus, mode="real", seed=args.seed, rate_percent=args.rate)
    synth = qe_data.generate(
        args.corpus, mode="synthetic", seed=args.seed, rate_percent=args.rate
    )

    d_real = collect(real, scorer, heuristic)
    d_synth = collect(synth, scorer, heuristic)

    # Fit Platt on SYNTHETIC only (abundant, deterministic) — real gold
    # stays a test set.
    midpoint, scale = fit_platt(d_synth["tok_surprisal"], d_synth["tok_label"])

    if args.fit:
        print(json.dumps({"midpoint": round(midpoint, 4), "scale": round(scale, 4)}))
        return 0

    report = {
        "reducer": args.reducer,
        "platt": {"midpoint": round(midpoint, 4), "scale": round(scale, 4)},
        "real": evaluate(d_real, midpoint, scale),
        "synthetic": evaluate(d_synth, midpoint, scale),
    }
    payload = json.dumps(report, indent=2, ensure_ascii=False)
    if args.out:
        args.out.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
