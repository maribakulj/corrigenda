"""ROADMAP V3 Phase 0 — offline XSD validation, diagnostic in, GATE out.

Input validation is a diagnostic: real-world exports carry dialect
extensions (Transkribus writes a ``TranskribusMetadata`` element the
official 2013-07-15 PAGE schema does not know), so a host surfaces
violations without refusing the document. Output validation is the
gate this file pins: a rewrite must never INTRODUCE a violation —
zero violations when the source was clean, no new ones when the
source carried a dialect.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from corrigenda import CorrectionPipeline
from corrigenda.errors import ParseError
from corrigenda.formats.loader import build_document_manifest
from corrigenda.formats.validation import (
    SCHEMA_BY_NAMESPACE,
    _schema_for,
    validate_bytes,
    validate_file,
)

from tests._pipeline_harness import EXAMPLES, DictProvider

_ALTO_V3 = EXAMPLES / "sample.xml"
_ALTO_V4 = (
    EXAMPLES / "page" / "Descartes1637_Discours_btv1b86069594_corrected_0014_alto4.xml"
)
_PAGE_2013 = (
    EXAMPLES
    / "page"
    / "Descartes1637_Discours_btv1b86069594_corrected_0014_page_raw.xml"
)


class _Null:
    def on_event(self, event_type: Any, payload: Any) -> None:
        pass


def _messages(violations: list[str]) -> set[str]:
    """Strip the ``source:line:`` prefix — line numbers shift across a
    rewrite; the MESSAGE is what must not newly appear."""
    return {v.split(": ", 1)[1] for v in violations}


async def _corrected_bytes(
    xml_path: Path, corrections: dict[str, str] | None = None
) -> dict[str, bytes]:
    doc = build_document_manifest([(xml_path, xml_path.name)])
    pipeline = CorrectionPipeline.for_provider(
        DictProvider(corrections or {}), api_key="k", model="m", observer=_Null()
    )
    result = await pipeline.run(
        document_manifest=doc, source_files={xml_path.name: xml_path}
    )
    assert result.corrected_files
    return result.corrected_files


# ---------------------------------------------------------------------------
# Schemas and diagnostics
# ---------------------------------------------------------------------------


def test_every_bundled_schema_compiles_offline():
    """Compilation resolves the ALTO schemas' absolute xlink import URL
    to the bundled copy — with a no_network parser, so a regression here
    fails loudly instead of fetching."""
    for namespace in SCHEMA_BY_NAMESPACE:
        _schema_for(namespace)


def test_clean_fixtures_validate():
    assert validate_file(_ALTO_V3) == []
    assert validate_file(_ALTO_V4) == []


def test_transkribus_dialect_is_reported_not_hidden():
    """Real Transkribus exports extend PAGE 2013-07-15 with a
    TranskribusMetadata element the official schema does not know.
    The diagnostic names it — and nothing else — on this fixture."""
    violations = validate_file(_PAGE_2013)
    assert len(violations) == 1
    assert "TranskribusMetadata" in violations[0]


def test_unknown_namespace_is_a_classified_error():
    with pytest.raises(ParseError, match="no bundled XSD"):
        validate_bytes(b'<root xmlns="urn:not-a-transcription"/>')


def test_malformed_xml_is_a_classified_error():
    with pytest.raises(ParseError, match="cannot parse"):
        validate_bytes(b"<alto", source_name="broken.xml")


def test_violations_are_reported_with_source_and_line():
    source = _ALTO_V3.read_text(encoding="utf-8")
    broken = source.replace("<TextLine", "<Bogus/><TextLine", 1)
    violations = validate_bytes(broken.encode("utf-8"), source_name="mutated.xml")
    assert violations
    assert violations[0].startswith("mutated.xml:")
    assert "Bogus" in violations[0]


# ---------------------------------------------------------------------------
# The OUTPUT gate — rewrites never introduce violations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_alto_identity_rewrite_output_validates():
    for name, xml_bytes in (await _corrected_bytes(_ALTO_V3)).items():
        assert validate_bytes(xml_bytes, source_name=name) == []


@pytest.mark.asyncio
async def test_alto_slow_path_rewrite_output_validates():
    """A word-count-changing correction takes the rebuild path (String/SP
    re-emitted, geometry recomputed) — the rebuilt markup must still be
    schema-valid."""
    doc = build_document_manifest([(_ALTO_V3, _ALTO_V3.name)])
    first = doc.pages[0].lines[0]
    corrections = {first.line_id: first.ocr_text + " sic"}
    files = await _corrected_bytes(_ALTO_V3, corrections)
    out = files[_ALTO_V3.name]
    # The edit must have survived the guards (the extra word lands in
    # its own rebuilt String), or this test exercises nothing: the fast
    # path would revalidate trivially.
    assert b'CONTENT="sic"' in out
    assert validate_bytes(out, source_name=_ALTO_V3.name) == []


@pytest.mark.asyncio
async def test_page_rewrite_introduces_no_new_violations():
    """The 2013 fixture carries the Transkribus dialect (see above), so
    'zero violations' is unreachable — the gate is: nothing NEW."""
    source_messages = _messages(validate_file(_PAGE_2013))
    for name, xml_bytes in (await _corrected_bytes(_PAGE_2013)).items():
        out_messages = _messages(validate_bytes(xml_bytes, source_name=name))
        assert out_messages <= source_messages, (
            f"rewrite introduced new schema violations: "
            f"{out_messages - source_messages}"
        )
