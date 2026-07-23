"""ROADMAP V3 Phase 2 — the QE training-data generator (scripts/qe_data.py).

Verifies the two label sources (real alignment, synthetic degradation),
determinism (same seed → same data, offline), and the invariant that a
token's label is exactly ``token != clean``. This is the data the
Phase 3 QE scorer trains on, so the labels must be trustworthy.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO_ROOT / "scripts" / "qe_data.py"

_spec = importlib.util.spec_from_file_location("qe_data", _SCRIPT)
assert _spec and _spec.loader
qe_data = importlib.util.module_from_spec(_spec)
# Register before exec: the module's @dataclass resolves its own module
# via sys.modules, which importlib does not populate for us.
sys.modules["qe_data"] = qe_data
_spec.loader.exec_module(qe_data)


# ---------------------------------------------------------------------------
# Synthetic degradation
# ---------------------------------------------------------------------------


def test_degradation_labels_are_exactly_the_changed_tokens():
    tokens, labels = qe_data.synthetic_labels(
        "la maison des fideles", seed=1, index=0, rate_percent=100
    )
    for tok, lab, clean in zip(tokens, labels, "la maison des fideles".split()):
        assert lab == (1 if tok != clean else 0)


def test_synthetic_is_deterministic_offline():
    a = qe_data.synthetic_labels(
        "soleil des fideles", seed=42, index=3, rate_percent=50
    )
    b = qe_data.synthetic_labels(
        "soleil des fideles", seed=42, index=3, rate_percent=50
    )
    assert a == b


def test_seed_changes_the_degradation():
    ref = "la maison sur la montagne ensoleillee"
    a = qe_data.synthetic_labels(ref, seed=1, index=0, rate_percent=50)
    b = qe_data.synthetic_labels(ref, seed=2, index=0, rate_percent=50)
    assert a != b


def test_rate_zero_degrades_nothing_rate_hundred_degrades_all_matches():
    ref = "maison fideles soleil"
    _, none = qe_data.synthetic_labels(ref, seed=1, index=0, rate_percent=0)
    assert sum(none) == 0
    # Every token here contains a degradable substring (m, fi, s).
    _, alld = qe_data.synthetic_labels(ref, seed=1, index=0, rate_percent=100)
    assert sum(alld) == 3


def test_degraded_token_is_a_known_confusion_of_its_clean_form():
    """The synthetic error must be recoverable by the confusion table —
    it is a REALISTIC OCR error, not noise."""
    from corrigenda.core.confidence import is_known_confusion

    clean = "maison"
    degraded = qe_data.degrade_token(clean, seed=1, index=0, rate_percent=100)
    assert degraded != clean
    # The confusion table maps the OCR form back to the correct one.
    assert is_known_confusion(degraded, clean)


# ---------------------------------------------------------------------------
# Real alignment labels
# ---------------------------------------------------------------------------


def test_real_labels_mark_only_the_differing_tokens():
    tokens, labels = qe_data.real_labels(
        "la rnaison eft blanche", "la maison est blanche"
    )
    assert tokens == ["la", "rnaison", "eft", "blanche"]
    assert labels == [0, 1, 1, 0]


def test_real_labels_identical_line_is_all_clean():
    tokens, labels = qe_data.real_labels("tout est bien", "tout est bien")
    assert labels == [0, 0, 0]


# ---------------------------------------------------------------------------
# End to end over the corpus
# ---------------------------------------------------------------------------


def test_generate_over_the_real_corpus_and_stats():
    records = qe_data.generate(
        qe_data.DEFAULT_CORPUS, mode="both", seed=1637, rate_percent=35
    )
    assert records
    # Every record's labels are 0/1 and match its token count.
    for r in records:
        assert set(r["labels"]) <= {0, 1}
        assert len(r["labels"]) == len(r["tokens"])
        assert r["source"] in ("real", "synthetic")

    st = qe_data.stats(records)
    assert st["tokens"] == sum(len(r["tokens"]) for r in records)
    assert st["errors"] == sum(sum(r["labels"]) for r in records)
    assert 0.0 < st["error_rate"] < 1.0
    assert "real" in st["by_source"] and "synthetic" in st["by_source"]


def test_cli_stats_is_valid_json(tmp_path: Path):
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), "--mode", "synthetic", "--stats"],
        capture_output=True,
        text=True,
        check=True,
    )
    report = json.loads(proc.stdout)
    # --mode synthetic → only the synthetic source is present.
    assert list(report["by_source"]) == ["synthetic"]
    assert report["tokens"] > 0
