#!/usr/bin/env python3
"""Versioned correction benchmark over the ground-truth corpus (P4.2).

Measures a producer against ``tests/corpus_gt/`` (source vs
human/synthetic reference, same line IDs) and emits a JSON report a
release can cite: library version, corpus version, §11 policy
fingerprint, producer identity, per-case and aggregate metrics.

    python scripts/benchmark.py --producer rules --out report.json
    python scripts/benchmark.py --producer oracle
    python scripts/benchmark.py --producer cassette:recorded.json

Producers:

- ``rules``   — ``default_french_ocr_rules()`` (deterministic, offline);
- ``oracle``  — a cassette derived from the REFERENCE (upper bound: what
  a perfect producer would propose; the engine's guards still arbitrate);
- ``cassette:<path>`` — replay a recorded ``{line_id: corrected_text}``
  JSON (e.g. captured LLM responses), deterministic and CI-runnable.

Metrics per case: micro-averaged CER/WER before/after (Levenshtein over
characters/tokens vs the reference), improved/degraded/unchanged line
counts, false positives (line already correct, output changed it),
fallback lines, reconcile outcomes, structural losses, latency and peak
memory. The report carries no wall-clock timestamp — two runs on the
same inputs produce comparable documents (latency/memory fields are
informational and naturally vary).

House rule (P4.2): no guard threshold or temperature-ramp default
changes without a measured improvement HERE.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import tracemalloc
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "packages" / "corrigenda" / "src"))

import corrigenda  # noqa: E402
from corrigenda import (  # noqa: E402
    CorrectionResult,
    EditScript,
    ProducerMetadata,
    ProducerOptions,
    ReplaceLine,
    RulesProducer,
    Usage,
    default_french_ocr_rules,
)
from corrigenda.core.pipeline import CorrectionPipeline  # noqa: E402
from corrigenda.core.schemas import ConfidencePolicy, CorrectionRequest  # noqa: E402

BENCHMARK_VERSION = "1"
DEFAULT_CORPUS = (
    REPO_ROOT / "packages" / "corrigenda" / "tests" / "corpus_gt" / "manifest.json"
)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _levenshtein(a: str | list[str], b: str | list[str]) -> int:
    """Plain two-row DP edit distance (chars for CER, tokens for WER)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            cur.append(
                min(
                    prev[j] + 1,  # deletion
                    cur[j - 1] + 1,  # insertion
                    prev[j - 1] + (ca != cb),  # substitution
                )
            )
        prev = cur
    return prev[-1]


def _rates(pairs: list[tuple[str, str]]) -> tuple[float, float]:
    """Micro-averaged (CER, WER) of (reference, hypothesis) pairs."""
    char_dist = sum(_levenshtein(ref, hyp) for ref, hyp in pairs)
    char_len = sum(len(ref) for ref, _ in pairs)
    word_dist = sum(_levenshtein(ref.split(), hyp.split()) for ref, hyp in pairs)
    word_len = sum(len(ref.split()) for ref, _ in pairs)
    return (
        char_dist / char_len if char_len else 0.0,
        word_dist / word_len if word_len else 0.0,
    )


# ---------------------------------------------------------------------------
# Producers
# ---------------------------------------------------------------------------


class CassetteProducer:
    """Replay a recorded ``{line_id: corrected_text}`` mapping (P4.2).

    Deterministic and offline: lines absent from the cassette (or equal
    to their OCR text) get no op — no edit, never an error.
    """

    wants_geometry = False
    wants_image = False
    requires_full_coverage = False

    def __init__(self, cassette: dict[str, str], *, label: str) -> None:
        self._cassette = cassette
        digest = hashlib.sha256(
            json.dumps(cassette, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:16]
        self.metadata = ProducerMetadata(name=label, configuration_fingerprint=digest)

    async def produce(
        self, payload: CorrectionRequest, *, options: ProducerOptions
    ) -> tuple[EditScript, Usage | None]:
        ops = [
            ReplaceLine(line_id=ln.line_id, text=self._cassette[ln.line_id])
            for ln in payload.lines
            if self._cassette.get(ln.line_id, ln.ocr_text) != ln.ocr_text
        ]
        return EditScript(ops=ops), None


def _make_producer(spec: str, reference_texts: dict[str, str]):
    if spec == "rules":
        return RulesProducer(default_french_ocr_rules())
    if spec == "oracle":
        return CassetteProducer(reference_texts, label="oracle")
    if spec.startswith("cassette:"):
        path = Path(spec.split(":", 1)[1])
        cassette = json.loads(path.read_text(encoding="utf-8"))
        return CassetteProducer(cassette, label="cassette")
    raise SystemExit(f"unknown producer {spec!r} (rules | oracle | cassette:<path>)")


# ---------------------------------------------------------------------------
# One case
# ---------------------------------------------------------------------------


class _NullObserver:
    def on_event(self, event_type, payload) -> None:
        pass


def _line_texts(path: Path) -> dict[str, str]:
    document = corrigenda.load(path)
    return {
        lm.line_id: lm.ocr_text for page in document.manifest.pages for lm in page.lines
    }


def run_case(case_dir: Path, case: dict, producer_spec: str) -> dict:
    source = case_dir / case["source"]
    reference = case_dir / case["reference"]
    ref_texts = _line_texts(reference)

    document = corrigenda.load(source)
    producer = _make_producer(producer_spec, ref_texts)
    pipeline = CorrectionPipeline(
        producer=producer,
        observer=_NullObserver(),
        # Phase 2 — the calibration harness scores every line's decision
        # confidence against ground truth (ECE/Brier below).
        confidence_policy=ConfidencePolicy(mode="report_only"),
    )

    tracemalloc.start()
    started = time.perf_counter()
    result: CorrectionResult = pipeline.run_sync(
        document_manifest=document.manifest,
        source_files=document.source_paths,
        run_id=f"benchmark-{case['name']}",
    )
    latency_s = time.perf_counter() - started
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    improved = degraded = unchanged = false_positives = 0
    before_pairs: list[tuple[str, str]] = []
    after_pairs: list[tuple[str, str]] = []
    for decision in result.decisions.decisions:
        ref = ref_texts.get(decision.ref.line_id)
        if ref is None:
            continue  # reference misses the line — misaligned corpus entry
        src, out = decision.source_text, decision.final_text
        before_pairs.append((ref, src))
        after_pairs.append((ref, out))
        d_before = _levenshtein(ref, src)
        d_after = _levenshtein(ref, out)
        if d_after < d_before:
            improved += 1
        elif d_after > d_before:
            degraded += 1
        else:
            unchanged += 1
        if d_before == 0 and out != ref:
            false_positives += 1

    cer_before, wer_before = _rates(before_pairs)
    cer_after, wer_after = _rates(after_pairs)
    pages = max(1, len(document.manifest.pages))
    losses = result.report.format_losses

    # Phase 2 calibration — (predicted decision confidence, was the
    # decided text exactly right) per line, pooled by main() into the
    # aggregate ECE/Brier. write_wc stays locked until these numbers,
    # measured on a real corpus, say the confidences can be trusted.
    calibration_pairs: list[tuple[float, float]] = []
    for outcome in result.report.lines:
        ref = ref_texts.get(outcome.line_id)
        if ref is None or outcome.confidence is None:
            continue
        calibration_pairs.append(
            (
                outcome.confidence.decision,
                1.0 if outcome.decision.final_text == ref else 0.0,
            )
        )

    return {
        "name": case["name"],
        "format": case["format"],
        "lines": len(after_pairs),
        "cer_before": round(cer_before, 6),
        "cer_after": round(cer_after, 6),
        "wer_before": round(wer_before, 6),
        "wer_after": round(wer_after, 6),
        "lines_improved": improved,
        "lines_degraded": degraded,
        "lines_unchanged": unchanged,
        "false_positives": false_positives,
        "fallback_lines": result.fallback_lines,
        "fallback_reasons": result.fallback_reasons,
        "reconcile": {
            "coherent": result.reconcile_metrics.coherent,
            "fallback": result.reconcile_metrics.fallback,
            "neutralised": result.reconcile_metrics.neutralised,
        },
        "format_losses": losses,
        "latency_s_per_page": round(latency_s / pages, 4),
        "peak_memory_mb": round(peak_bytes / (1024 * 1024), 2),
        "calibration": _calibration_metrics(calibration_pairs),
        # Popped by main() into the pooled aggregate, never serialized.
        "_calibration_pairs": calibration_pairs,
    }


def _calibration_metrics(
    pairs: list[tuple[float, float]], bins: int = 10
) -> dict[str, float | int]:
    """Brier score + expected calibration error over equal-width bins.

    ``pairs`` = (predicted confidence, 1.0 if the decided text matched
    ground truth else 0.0). Both metrics in [0, 1], lower is better.
    """
    if not pairs:
        return {"lines": 0, "brier": 0.0, "ece": 0.0, "bins": bins}
    brier = sum((p - c) ** 2 for p, c in pairs) / len(pairs)
    ece = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        in_bin = [
            (p, c) for p, c in pairs if (lo <= p < hi) or (b == bins - 1 and p == hi)
        ]
        if not in_bin:
            continue
        avg_conf = sum(p for p, _ in in_bin) / len(in_bin)
        accuracy = sum(c for _, c in in_bin) / len(in_bin)
        ece += abs(avg_conf - accuracy) * len(in_bin) / len(pairs)
    return {
        "lines": len(pairs),
        "brier": round(brier, 6),
        "ece": round(ece, 6),
        "bins": bins,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--producer", default="rules")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    manifest = json.loads(args.corpus.read_text(encoding="utf-8"))
    case_dir = args.corpus.parent

    cases = [run_case(case_dir, case, args.producer) for case in manifest["cases"]]

    # Aggregate: micro over all cases (weighted by reference length via
    # the summed distances the per-case rates already encode — recompute
    # from line counts would be macro; keep it simple and honest: macro
    # across cases, each case already micro across its lines).
    def _avg(key: str) -> float:
        return round(sum(c[key] for c in cases) / len(cases), 6) if cases else 0.0

    # Producer identity + policy fingerprint come from a default pipeline
    # around the same producer spec (first case's reference for oracle).
    first_ref = (
        _line_texts(case_dir / manifest["cases"][0]["reference"])
        if manifest["cases"]
        else {}
    )
    producer = _make_producer(args.producer, first_ref)
    pipeline = CorrectionPipeline(producer=producer, observer=_NullObserver())
    md = pipeline.producer_metadata

    report = {
        "benchmark_version": BENCHMARK_VERSION,
        "lib_version": corrigenda.__version__,
        "corpus_version": manifest["corpus_version"],
        "config_fingerprint": pipeline.config_fingerprint(),
        "producer": {
            "spec": args.producer,
            "name": md.name,
            "version": md.version,
            "implementation": md.implementation,
            "configuration_fingerprint": md.configuration_fingerprint,
        },
        "cases": cases,
        "aggregate": {
            "cases": len(cases),
            "lines": sum(c["lines"] for c in cases),
            "cer_before": _avg("cer_before"),
            "cer_after": _avg("cer_after"),
            "wer_before": _avg("wer_before"),
            "wer_after": _avg("wer_after"),
            "lines_improved": sum(c["lines_improved"] for c in cases),
            "lines_degraded": sum(c["lines_degraded"] for c in cases),
            "false_positives": sum(c["false_positives"] for c in cases),
            "fallback_lines": sum(c["fallback_lines"] for c in cases),
            # Phase 2 — micro-pooled over EVERY line of every case (a
            # macro average across cases would let a tiny case swamp the
            # aggregate calibration).
            "calibration": _calibration_metrics(
                [pair for c in cases for pair in c["_calibration_pairs"]]
            ),
        },
    }
    for c in cases:
        del c["_calibration_pairs"]  # pooled above, never serialized

    payload = json.dumps(report, indent=2, ensure_ascii=False)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
