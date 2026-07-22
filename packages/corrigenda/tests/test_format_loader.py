"""Phase 0 (ROADMAP V3) — the generic, namespace-sniffing manifest builder.

Hard-wiring a format-specific parser onto user files silently mis-reads
the other format: the ALTO parser applied to a valid PAGE file finds no
ALTO pages and returns an EMPTY manifest (0 pages, 0 lines) instead of
an error. ``formats.loader.build_document_manifest`` is the drop-in
replacement: same ``(path, source_name)`` + ``pairing_policy``
signature, format sniffed per file from the root namespace, mixed
batches refused.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from corrigenda.errors import ParseError
from corrigenda.formats.alto.parser import (
    build_document_manifest as build_alto_manifest,
)
from corrigenda.formats.loader import build_document_manifest, sniff_format

from tests._pipeline_harness import EXAMPLES

_ALTO_SAMPLE = EXAMPLES / "sample.xml"
_PAGE_SAMPLE = (
    EXAMPLES
    / "page"
    / "Descartes1637_Discours_btv1b86069594_corrected_0014_page_raw.xml"
)


# ---------------------------------------------------------------------------
# sniff_format
# ---------------------------------------------------------------------------


def test_sniffs_alto_and_page_by_root_namespace():
    assert sniff_format(_ALTO_SAMPLE) == "alto"
    assert sniff_format(_PAGE_SAMPLE) == "page"


def test_sniff_refuses_unknown_namespace(tmp_path: Path):
    p = tmp_path / "other.xml"
    p.write_text('<root xmlns="urn:not-a-transcription"/>', encoding="utf-8")
    with pytest.raises(ParseError, match="neither ALTO nor PAGE"):
        sniff_format(p)


# ---------------------------------------------------------------------------
# build_document_manifest — dispatch
# ---------------------------------------------------------------------------


def test_alto_batch_matches_the_direct_alto_builder():
    pairs = [(_ALTO_SAMPLE, _ALTO_SAMPLE.name)]
    generic = build_document_manifest(pairs)
    direct = build_alto_manifest(pairs)
    assert generic.source_format == "alto"
    # document_id is generated per build; everything parsed must match.
    assert generic.model_dump(exclude={"document_id"}) == direct.model_dump(
        exclude={"document_id"}
    )


def test_page_file_builds_a_page_manifest_not_an_empty_alto_one():
    """The regression this module exists for: the ALTO parser applied to
    this exact file yields 0 pages / 0 lines without raising."""
    empty = build_alto_manifest([(_PAGE_SAMPLE, _PAGE_SAMPLE.name)])
    assert empty.total_lines == 0  # the silent mis-read, still true

    manifest = build_document_manifest([(_PAGE_SAMPLE, _PAGE_SAMPLE.name)])
    assert manifest.source_format == "page"
    assert len(manifest.pages) == 1
    assert manifest.total_lines > 0


def test_mixed_formats_are_refused():
    with pytest.raises(ParseError, match="one document, one format"):
        build_document_manifest(
            [
                (_ALTO_SAMPLE, _ALTO_SAMPLE.name),
                (_PAGE_SAMPLE, _PAGE_SAMPLE.name),
            ]
        )


def test_empty_batch_is_refused():
    with pytest.raises(ParseError, match="at least one"):
        build_document_manifest([])
