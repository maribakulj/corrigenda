"""Sentinel: ensure the corpus fixtures are present.

Roughly 15 tests across test_corpus_validation.py, test_x0000002.py,
test_double_dash.py, test_chunk_planner.py and others use
``pytest.skip`` when ``examples/sample.xml`` or ``examples/X0000002.xml``
is missing. Silent skips can hide real regressions — a CI run that loses
the fixtures (LFS misconfiguration, partial checkout, repo move) reports
"all green" even though dozens of integration tests didn't run.

This file fails loudly when either fixture is absent so the CI summary
surfaces a single, actionable failure instead of dozens of skips. If a
deployment intentionally ships without the corpus, mark these tests
`xfail`/`skip` in pytest configuration rather than removing them.
"""

from __future__ import annotations

from pathlib import Path

_EXAMPLES = Path(__file__).resolve().parent.parent.parent / "examples"


def test_sample_xml_corpus_present():
    p = _EXAMPLES / "sample.xml"
    assert p.exists(), (
        f"Missing required corpus fixture: {p}. "
        "Without it ~15 corpus-dependent tests skip silently."
    )


def test_x0000002_xml_corpus_present():
    p = _EXAMPLES / "X0000002.xml"
    assert p.exists(), (
        f"Missing required corpus fixture: {p}. "
        "Without it the BnF non-regression suite skips silently."
    )
