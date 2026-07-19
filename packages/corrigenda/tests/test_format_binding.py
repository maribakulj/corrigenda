"""The format travels WITH the document — no implicit ALTO default.

The pipeline used to fall back to the ALTO adapter whenever none was
injected, whatever the manifest actually contained: following the
quickstart's "swap the parser import for PAGE" hint produced a run that
rewrote a PAGE file with the ALTO rewriter (silently broken output
before the projection invariant; a confusing late ProjectionError
after). The parsers now stamp ``DocumentManifest.source_format`` and
the engine derives the right adapter from it:

- a PAGE document corrects end-to-end with NO adapter injected;
- an explicitly injected adapter that contradicts the manifest's format
  fails at run start, not at write time;
- a hand-built manifest (no stamped format) that reaches the write
  phase without an explicit adapter fails with an actionable message.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from lxml import etree

from corrigenda import CorrectionPipeline
from corrigenda.errors import ConfigurationError
from corrigenda.formats.alto.adapter import AltoFormatAdapter
from corrigenda.formats.alto.parser import build_document_manifest as build_alto
from corrigenda.formats.page.parser import build_document_manifest as build_page
from corrigenda.producers.rules import RulesProducer, SubstitutionRule

_SAMPLE_ALTO = Path(__file__).parent.parent.parent.parent / "examples" / "sample.xml"

_PAGE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<PcGts xmlns="http://schema.primaresearch.org/PAGE/gts/pagecontent/2019-07-15">
  <Metadata><Creator>x</Creator><Created>2020-01-01T00:00:00</Created>
    <LastChange>2020-01-01T00:00:00</LastChange></Metadata>
  <Page imageFilename="p.png" imageWidth="1000" imageHeight="2000">
    <TextRegion id="r1">
      <Coords points="0,0 100,0 100,50 0,50"/>
      <TextLine id="ln1">
        <Coords points="0,0 100,0 100,20 0,20"/>
        <TextEquiv index="0"><Unicode>hello world</Unicode></TextEquiv>
      </TextLine>
    </TextRegion>
  </Page>
</PcGts>
"""


class _Null:
    def on_event(self, *a, **k):
        pass


def _pipeline(**kwargs) -> CorrectionPipeline:
    return CorrectionPipeline(
        producer=RulesProducer([SubstitutionRule("o", "0")]),
        observer=_Null(),
        provider_name="rules",
        model="v1",
        **kwargs,
    )


@pytest.mark.asyncio
async def test_page_document_corrects_without_an_injected_adapter(tmp_path) -> None:
    src = tmp_path / "doc.xml"
    src.write_text(_PAGE_XML, encoding="utf-8")
    doc = build_page([(src, src.name)])
    assert doc.source_format == "page"

    result = await _pipeline().run(document_manifest=doc, source_files={src.name: src})

    out = result.corrected_files[src.name]
    root = etree.fromstring(out)
    assert root.tag.endswith("PcGts"), "the output must still be PAGE XML"
    ns = root.tag[1 : root.tag.index("}")]
    unicodes = [u.text for u in root.iter(f"{{{ns}}}Unicode")]
    assert "hell0 w0rld" in unicodes, unicodes


@pytest.mark.asyncio
async def test_alto_document_still_corrects_without_an_injected_adapter() -> None:
    doc = build_alto([(_SAMPLE_ALTO, _SAMPLE_ALTO.name)])
    assert doc.source_format == "alto"
    result = await _pipeline().run(
        document_manifest=doc, source_files={_SAMPLE_ALTO.name: _SAMPLE_ALTO}
    )
    assert result.corrected_files


@pytest.mark.asyncio
async def test_contradictory_adapter_fails_at_run_start(tmp_path) -> None:
    src = tmp_path / "doc.xml"
    src.write_text(_PAGE_XML, encoding="utf-8")
    doc = build_page([(src, src.name)])

    pipeline = _pipeline(format_adapter=AltoFormatAdapter())
    with pytest.raises(ConfigurationError, match="alto"):
        await pipeline.run(document_manifest=doc, source_files={src.name: src})


@pytest.mark.asyncio
async def test_unstamped_manifest_needs_an_explicit_adapter() -> None:
    """A hand-built manifest carries no format; deriving ALTO silently
    (the historical default) is exactly the trap being removed."""
    doc = build_alto([(_SAMPLE_ALTO, _SAMPLE_ALTO.name)])
    doc.source_format = None  # simulate a hand-built manifest

    with pytest.raises(ConfigurationError, match="format_adapter"):
        await _pipeline().run(
            document_manifest=doc, source_files={_SAMPLE_ALTO.name: _SAMPLE_ALTO}
        )
