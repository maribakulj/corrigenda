"""Plan V4.2 — property-based tests (Hypothesis) over generated ALTO.

The example-based suite pins known bugs; these properties assert the
library's core INVARIANTS over arbitrary generated documents, which is
where hypotheses shared between code and tests go to die:

1. parse → rewrite never touches TextLine geometry or identity,
   whatever the corrections;
2. hyphen pairs are atomic in every chunk plan, at every granularity;
3. rewriting with no corrections is byte-identical on text content.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st
from lxml import etree

from corrigenda.core.planner import plan_page
from corrigenda.core.schemas import ChunkGranularity, ChunkPlannerConfig, HyphenRole
from corrigenda.formats.alto.parser import build_document_manifest
from corrigenda.formats.alto.rewriter import rewrite_alto_file

# ---------------------------------------------------------------------------
# ALTO document strategy
# ---------------------------------------------------------------------------

# Letters only (incl. Latin-1/Latin-A accents): no '-' so the trailing-dash
# heuristic never fires by accident — explicit SUBS_* marks the pairs.
_WORD = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Lu"), max_codepoint=0x024F),
    min_size=1,
    max_size=8,
)


@st.composite
def alto_documents(draw: st.DrawFn) -> str:
    """A syntactically valid single-page ALTO v3 document with random
    blocks, lines, words and non-overlapping explicit hyphen pairs."""
    n_blocks = draw(st.integers(1, 3))
    line_no = 0
    blocks_xml: list[str] = []
    for b in range(n_blocks):
        n_lines = draw(st.integers(1, 6))
        words_per_line: list[list[str]] = [
            draw(st.lists(_WORD, min_size=1, max_size=4)) for _ in range(n_lines)
        ]
        # Non-overlapping consecutive hyphen pairs: line i is PART1 and
        # line i+1 is PART2 of the same word.
        pair_start: dict[int, bool] = {}
        i = 0
        while i < n_lines - 1:
            if draw(st.booleans()):
                pair_start[i] = True
                i += 2
            else:
                i += 1

        lines_xml: list[str] = []
        for li, words in enumerate(words_per_line):
            vpos = 10 + 30 * line_no
            strings: list[str] = []
            hyp = ""
            for wi, w in enumerate(words):
                attrs = (
                    f'ID="S{line_no}_{wi}" CONTENT="{w}" '
                    f'HPOS="{10 + 90 * wi}" VPOS="{vpos}" WIDTH="80" HEIGHT="20"'
                )
                if wi == len(words) - 1 and pair_start.get(li):
                    part2_first = words_per_line[li + 1][0]
                    attrs += f' SUBS_TYPE="HypPart1" SUBS_CONTENT="{w}{part2_first}"'
                    hyp = '<HYP CONTENT="-"/>'
                if wi == 0 and pair_start.get(li - 1):
                    part1_last = words_per_line[li - 1][-1]
                    attrs += f' SUBS_TYPE="HypPart2" SUBS_CONTENT="{part1_last}{w}"'
                strings.append(f"<String {attrs}/>")
            lines_xml.append(
                f'<TextLine ID="L{line_no}" HPOS="10" VPOS="{vpos}" '
                f'WIDTH="900" HEIGHT="20">{"".join(strings)}{hyp}</TextLine>'
            )
            line_no += 1
        blocks_xml.append(
            f'<TextBlock ID="B{b}" HPOS="0" VPOS="0" WIDTH="1000" HEIGHT="900">'
            f'{"".join(lines_xml)}</TextBlock>'
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<alto xmlns="http://www.loc.gov/standards/alto/ns-v3#"><Layout>'
        '<Page ID="P1" WIDTH="1000" HEIGHT="1000">'
        '<PrintSpace HPOS="0" VPOS="0" WIDTH="1000" HEIGHT="900">'
        f'{"".join(blocks_xml)}'
        "</PrintSpace></Page></Layout></alto>"
    )


_NS = "{http://www.loc.gov/standards/alto/ns-v3#}"
_GEOM_ATTRS = ("ID", "HPOS", "VPOS", "WIDTH", "HEIGHT")


def _textline_geometry(xml: bytes) -> list[tuple[str | None, ...]]:
    root = etree.fromstring(xml)
    return [
        tuple(el.get(a) for a in _GEOM_ATTRS) for el in root.iter(f"{_NS}TextLine")
    ]


def _string_contents(xml: bytes) -> list[str | None]:
    root = etree.fromstring(xml)
    return [el.get("CONTENT") for el in root.iter(f"{_NS}String")]


def _write_tmp(xml: str) -> Path:
    tmp = tempfile.NamedTemporaryFile(suffix=".xml", delete=False)
    tmp.write(xml.encode("utf-8"))
    tmp.close()
    return Path(tmp.name)


# ---------------------------------------------------------------------------
# Property 1 — geometry & identity survive parse → correct → rewrite
# ---------------------------------------------------------------------------


@settings(max_examples=40, deadline=None)
@given(doc=alto_documents(), data=st.data())
def test_rewrite_never_touches_textline_geometry(doc: str, data: st.DataObject) -> None:
    path = _write_tmp(doc)
    try:
        manifest = build_document_manifest([(path, path.name)])
        # Arbitrary corrections on non-hyphen lines (hyphen lines keep
        # their source text — their invariants are covered separately).
        for page in manifest.pages:
            for lm in page.lines:
                if lm.hyphen_role == HyphenRole.NONE and data.draw(st.booleans()):
                    lm.corrected_text = " ".join(
                        data.draw(st.lists(_WORD, min_size=1, max_size=5))
                    )

        out, _metrics, _ = rewrite_alto_file(path, manifest.pages, "test", "test")

        assert _textline_geometry(out) == _textline_geometry(doc.encode("utf-8")), (
            "TextLine identity/geometry must be byte-stable through the rewriter"
        )
    finally:
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Property 2 — hyphen pairs are atomic at every granularity
# ---------------------------------------------------------------------------


@settings(max_examples=40, deadline=None)
@given(doc=alto_documents(), tiny=st.booleans())
def test_hyphen_pairs_never_split_across_chunks(doc: str, tiny: bool) -> None:
    path = _write_tmp(doc)
    try:
        manifest = build_document_manifest([(path, path.name)])
        # A tiny budget forces WINDOW/LINE plans — the granularities where
        # splitting a pair is actually tempting.
        config = (
            ChunkPlannerConfig(
                max_input_chars_per_request=30,
                max_lines_per_request=2,
                line_window_size=2,
                line_window_overlap=1,
            )
            if tiny
            else ChunkPlannerConfig()
        )
        for page in manifest.pages:
            partner = {
                lm.line_id: lm.hyphen_pair_line_id
                for lm in page.lines
                if lm.hyphen_role in (HyphenRole.PART1, HyphenRole.BOTH)
                and lm.hyphen_pair_line_id
            }
            for force in (None, ChunkGranularity.WINDOW, ChunkGranularity.LINE):
                plan = plan_page(page, "doc", config, force_granularity=force)
                for chunk in plan.chunks:
                    ids = set(chunk.line_ids)
                    for line_id, partner_id in partner.items():
                        if line_id in ids:
                            assert partner_id in ids, (
                                f"hyphen pair {line_id}/{partner_id} split at "
                                f"granularity {plan.granularity} (chunk {chunk.chunk_id})"
                            )
    finally:
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Property 3 — no corrections → text content is untouched
# ---------------------------------------------------------------------------


@settings(max_examples=40, deadline=None)
@given(doc=alto_documents())
def test_rewrite_without_corrections_preserves_all_string_content(doc: str) -> None:
    path = _write_tmp(doc)
    try:
        manifest = build_document_manifest([(path, path.name)])
        out, _metrics, _ = rewrite_alto_file(path, manifest.pages, "test", "test")
        assert _string_contents(out) == _string_contents(doc.encode("utf-8"))
    finally:
        path.unlink(missing_ok=True)
