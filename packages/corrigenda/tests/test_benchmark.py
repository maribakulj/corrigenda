"""P4.2 — the versioned benchmark runs offline in CI over corpus_gt.

Executes ``scripts/benchmark.py`` as a subprocess (same pattern as the
quickstart example test) with the two deterministic producers and pins
the report contract: provenance header, metric keys, and the seed
corpus's designed behaviour — rules improve CER but keep the documented
``rn`` residual; the oracle erases it entirely; nobody degrades a line
or touches an already-correct one.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
_BENCHMARK = _REPO / "scripts" / "benchmark.py"


def _run(tmp_path: Path, producer: str) -> dict:
    out = tmp_path / f"{producer.replace(':', '_')}.json"
    proc = subprocess.run(
        [sys.executable, str(_BENCHMARK), "--producer", producer, "--out", str(out)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, f"benchmark failed:\n{proc.stderr}"
    return json.loads(out.read_text(encoding="utf-8"))


def test_rules_report_contract_and_improvement(tmp_path: Path):
    report = _run(tmp_path, "rules")

    # Provenance header a release can cite.
    assert report["benchmark_version"] == "1"
    assert report["lib_version"]
    assert report["corpus_version"]
    assert len(report["config_fingerprint"]) == 16
    assert report["producer"]["name"] == "rules"
    assert report["producer"]["configuration_fingerprint"]

    case = report["cases"][0]
    assert case["name"] == "synthetic-fr-early-print"
    # The long-s/ligature degradations are fixed, the documented `rn`
    # confusion is not (no lexicon) — improvement with a residual.
    assert case["cer_after"] < case["cer_before"]
    assert 0 < case["cer_after"] < 0.05
    assert case["lines_degraded"] == 0
    assert case["false_positives"] == 0
    assert case["latency_s_per_page"] > 0
    assert case["peak_memory_mb"] > 0

    assert case["lines"] == 6
    # Corpus 0.2.0 — the aggregate spans EVERY case (synthetic + the two
    # real OCR17+ pages), not just the first one.
    agg = report["aggregate"]
    assert agg["lines"] == sum(c["lines"] for c in report["cases"])
    assert len(report["cases"]) == 3
    assert agg["cer_after"] < agg["cer_before"]


def test_oracle_erases_the_error(tmp_path: Path):
    report = _run(tmp_path, "oracle")
    case = report["cases"][0]
    assert case["cer_before"] > 0
    assert case["cer_after"] == 0.0
    assert case["wer_after"] == 0.0
    assert case["lines_improved"] == case["lines"] == 6
    assert case["lines_degraded"] == 0
    assert case["fallback_lines"] == 0
    assert report["producer"]["name"] == "oracle"


def test_cassette_replay_matches_oracle(tmp_path: Path):
    """A cassette file replays deterministically — recording the oracle's
    mapping and replaying it must reproduce the oracle's metrics."""
    import corrigenda

    ref = Path(__file__).parent / "corpus_gt" / "synthetic-fr-early-print.ref.alto.xml"
    document = corrigenda.load(ref)
    cassette = {
        lm.line_id: lm.ocr_text for page in document.manifest.pages for lm in page.lines
    }
    cassette_path = tmp_path / "cassette.json"
    cassette_path.write_text(json.dumps(cassette, ensure_ascii=False), encoding="utf-8")

    replayed = _run(tmp_path, f"cassette:{cassette_path}")
    oracle = _run(tmp_path, "oracle")
    for key in ("cer_after", "wer_after", "lines_improved", "false_positives"):
        assert replayed["cases"][0][key] == oracle["cases"][0][key]
    assert replayed["producer"]["name"] == "cassette"
