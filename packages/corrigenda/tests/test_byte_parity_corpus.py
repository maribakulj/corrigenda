"""Byte-parity gate on the non-regression corpus (spec §13 DoD, v1.0).

Same inputs → same output bytes. Two deterministic scenarios over the two
corpus files, pinned by sha256:

  - **identity** — every line corrected with its own OCR text. Verified
    BYTE-IDENTICAL to the pre-v1.0 baseline (commit 8c4789c) during the
    post-audit parity run: F4 only widens the UNTOUCHED path and F2/F6
    never fire without a content change.
  - **scripted** — deterministic corrections exercising the fast path
    (every 3rd line: first 'e' → '3', same word count) and the slow path
    (every 7th line: ' zz' appended). Versus the same baseline, the only
    differing TextLines were classified as the DOCUMENTED v1.0 changes:
    F2 (WC/CC dropped on changed Strings) and F6/§6.1 (rebalanced
    slow-path String+SP geometry). No text drift, no structure drift.

If a hash moves, do NOT regenerate blindly: re-run the classifier
(scratch parity_classify.py pattern — parse both outputs, diff per
TextLine, bucket into text/structure/confidence/geometry) and update the
hash ONLY for a deliberate, documented byte change, naming it in the
commit message. The rewrite is invoked WITHOUT provenance arguments so
these hashes are independent of the library version string.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from corrigenda.alto.parser import build_document_manifest
from corrigenda.alto.rewriter import rewrite_alto_file

_EXAMPLES = Path(__file__).parent.parent.parent.parent / "examples"

_GOLDEN = {
    ("sample.xml", "identity"): (
        "10eda74a8afbc2eb3a1c3cf5dd488091f05388e887d17a4f86343e5a54855ec7"
    ),
    ("sample.xml", "scripted"): (
        "41f7eaae9c1ae257ae6c95cb1730be5f05ed9c0f4be833356ecca257681c7859"
    ),
    ("X0000002.xml", "identity"): (
        "18387a3d4dfdd2a117a0bf4593d9533da3f5aeef35edd6c8a5b5e3d875c759b6"
    ),
    ("X0000002.xml", "scripted"): (
        "10bfa338c8b4a29c426dda6270d45dec40a1a3fcf9dd6e5bcb99c5e9e2487c41"
    ),
}


def _scripted_correction(i: int, text: str) -> str:
    words = text.split()
    if not words:
        return text
    if i % 7 == 0:
        return text + " zz"  # word count +1 → slow path
    if i % 3 == 0 and "e" in words[0]:
        words = [words[0].replace("e", "3", 1)] + words[1:]
        return " ".join(words)  # same word count → fast path
    return text


@pytest.mark.parametrize(
    ("filename", "scenario"),
    sorted(_GOLDEN),
    ids=lambda v: v if isinstance(v, str) else str(v),
)
def test_corpus_output_bytes_are_pinned(filename: str, scenario: str) -> None:
    xml_path = _EXAMPLES / filename
    doc = build_document_manifest([(xml_path, xml_path.name)])
    i = 0
    for page in doc.pages:
        for lm in page.lines:
            if scenario == "identity":
                lm.corrected_text = lm.ocr_text
            else:
                lm.corrected_text = _scripted_correction(i, lm.ocr_text)
            i += 1
    xml_bytes, _metrics, _paths = rewrite_alto_file(xml_path, doc.pages, "test", "mock")
    digest = hashlib.sha256(xml_bytes).hexdigest()
    assert digest == _GOLDEN[(filename, scenario)], (
        f"{filename}/{scenario}: output bytes moved. If deliberate, classify "
        f"the diff per TextLine and update the golden hash with a documented "
        f"commit message; see this file's docstring."
    )


def test_identity_output_equals_source_reserialisation() -> None:
    """Identity corrections must not perturb a single TextLine: the output
    re-parses to the same per-line text as the source for every line."""
    from corrigenda.alto.rewriter import extract_output_texts

    xml_path = _EXAMPLES / "X0000002.xml"
    doc = build_document_manifest([(xml_path, xml_path.name)])
    all_ids = set()
    for page in doc.pages:
        for lm in page.lines:
            lm.corrected_text = lm.ocr_text
            all_ids.add(lm.line_id)
    xml_bytes, metrics, _ = rewrite_alto_file(xml_path, doc.pages, "test", "mock")
    assert metrics.fast_path == 0 and metrics.slow_path == 0
    out_texts = extract_output_texts(xml_bytes, all_ids)
    by_id = {lm.line_id: lm for p in doc.pages for lm in p.lines}
    for lid, txt in out_texts.items():
        assert txt.strip() == by_id[lid].ocr_text
