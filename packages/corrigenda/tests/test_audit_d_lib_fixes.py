"""Audit-D remediation (2026-07-12) — library correctness cluster.

Each test pins one confirmed audit finding in the pure-core library
(hyphenation, editing, validator, schemas, guards, rules, _ns, parsers).
Every test is written to FAIL on the pre-fix code and pass after.
"""

from __future__ import annotations

import pytest

from corrigenda.core.editing import (
    EditScript,
    RangeAnchor,
    ReplaceSpan,
    apply_edit_script,
)
from corrigenda.core.guards import check_adjacent_duplicates
from corrigenda.core.hyphenation import reconcile_hyphen_pair
from corrigenda.core.schemas import Coords, HyphenRole, LineManifest, PairingPolicy
from corrigenda.core.validator import (
    HyphenIntegrityError,
    _validate_hyphen_integrity,
)
from corrigenda.errors import ParseError
from corrigenda.formats.alto.parser import parse_alto_file
from corrigenda.formats.page.parser import parse_page_file
from corrigenda.formats.page._ns import polygon_to_bbox
from corrigenda.producers.rules import RulesProducer, SubstitutionRule


def _line(
    line_id: str,
    ocr: str,
    *,
    page_id: str = "p1",
    block_id: str = "b1",
    role: HyphenRole = HyphenRole.NONE,
    subs: str | None = None,
    explicit: bool = False,
) -> LineManifest:
    return LineManifest(
        line_id=line_id,
        page_id=page_id,
        block_id=block_id,
        line_order_global=0,
        line_order_in_block=0,
        coords=Coords(hpos=0, vpos=0, width=100, height=20),
        ocr_text=ocr,
        hyphen_role=role,
        hyphen_subs_content=subs,
        hyphen_source_explicit=explicit,
    )


# ---------------------------------------------------------------------------
# #14 — polygon_to_bbox: a half-malformed x,y pair must be skipped atomically
# ---------------------------------------------------------------------------


def test_polygon_bbox_skips_half_malformed_pair_atomically():
    # Last pair has a good x (500) but a bad y (abc). The old code appended
    # x before y raised, inflating width to 490 from a coordinate the
    # docstring promises to skip.
    hpos, vpos, w, h = polygon_to_bbox("10,10 20,20 500,abc")
    assert (hpos, vpos, w, h) == (10, 10, 10, 10)


def test_polygon_bbox_wellformed_unchanged():
    assert polygon_to_bbox("617,1046 3450,1046 3450,5797 617,5797") == (
        617,
        1046,
        2833,
        4751,
    )


# ---------------------------------------------------------------------------
# #29 — check_adjacent_duplicates catches the third line of a run
# ---------------------------------------------------------------------------


def test_duplicate_run_of_three_all_reverted():
    # Distinct sources, identical corrections. The old loop flagged only the
    # first pair, leaving line 2 unreverted.
    reverts = check_adjacent_duplicates(
        [
            ("id0", "source alpha", "HALLUCINATED IDENTICAL LINE"),
            ("id1", "source beta", "HALLUCINATED IDENTICAL LINE"),
            ("id2", "source gamma", "HALLUCINATED IDENTICAL LINE"),
        ]
    )
    assert set(reverts) == {"id0", "id1", "id2"}


# ---------------------------------------------------------------------------
# #28 — RulesProducer lexicon guard normalises through NFC (ncfold)
# ---------------------------------------------------------------------------


def test_lexicon_guard_matches_nfd_entry():
    import unicodedata

    # Lexicon supplied in DECOMPOSED (NFD) form; the token the rule produces
    # is composed (NFC, as the parser emits). The guard must still fire.
    nfd_word = unicodedata.normalize("NFD", "modérné")
    assert nfd_word != "modérné"  # sanity: genuinely decomposed
    prod = RulesProducer(
        [SubstitutionRule("rn", "rné", lexicon_guarded=True)],
        lexicon={nfd_word},
    )
    # OCR "modérn" → the guarded rule turns "rn" into "rné" giving the known
    # word "modérné"; the guard (NFC-folded) accepts it.
    script = prod.build_edit_script({"l1": "modérn"})
    assert script.ops, "guarded substitution should fire against an NFD lexicon"


# ---------------------------------------------------------------------------
# #17 — PairingPolicy.same_block_only is page-qualified
# ---------------------------------------------------------------------------


def test_same_block_only_forbids_cross_page_even_with_reused_block_id():
    policy = PairingPolicy(same_block_only=True, geometric_checks=False)
    part1 = _line(
        "TL9", "mot-", page_id="pA", block_id="TextBlock1", role=HyphenRole.PART1
    )
    # Cross-page candidate reusing the SAME block id (both pages export
    # "TextBlock1"). The documented guarantee: cross-page pairing forbidden.
    candidate = _line(
        "TL1", "suite", page_id="pB", block_id="TextBlock1", role=HyphenRole.PART2
    )
    assert policy.can_pair(part1, candidate) is False


def test_same_block_only_still_allows_intra_block_same_page():
    policy = PairingPolicy(same_block_only=True, geometric_checks=False)
    part1 = _line("TL1", "mot-", page_id="pA", block_id="TB1", role=HyphenRole.PART1)
    candidate = _line(
        "TL2", "suite", page_id="pA", block_id="TB1", role=HyphenRole.PART2
    )
    assert policy.can_pair(part1, candidate) is True


# ---------------------------------------------------------------------------
# #18 — fusion check ignores context-only pairs (F8 window mode)
# ---------------------------------------------------------------------------


def test_fusion_check_skips_context_only_pair():
    # A full hyphen pair sits entirely in the chunk's CONTEXT region (neither
    # member is a target). Even if the LLM fuses PART1 (its last word ==
    # subs_content), the target chunk must NOT be failed.
    hyphen_pairs = {"ctxP1": "ctxP2", "ctxP2": "ctxP1"}
    text_by_id = {
        "ctxP1": "necessaires",  # fused: contains the full logical word
        "ctxP2": "du roi",
        "tgt": "corrected target",
    }
    ocr_texts = {"ctxP1": "neces-", "ctxP2": "saires", "tgt": "target"}
    hyphen_subs = {"ctxP1": "necessaires"}
    chunk_ids = {"tgt"}  # only the target line is in scope

    # Must NOT raise — the context-only fusion is not this chunk's concern.
    _validate_hyphen_integrity(
        text_by_id,
        hyphen_pairs,
        chunk_ids,
        ocr_texts,
        hyphen_subs,
    )


def test_fusion_check_still_fires_for_a_target_pair():
    hyphen_pairs = {"P1": "P2", "P2": "P1"}
    text_by_id = {"P1": "necessaires", "P2": "du roi"}
    ocr_texts = {"P1": "neces-", "P2": "saires"}
    hyphen_subs = {"P1": "necessaires"}
    chunk_ids = {"P1", "P2"}
    with pytest.raises(HyphenIntegrityError):
        _validate_hyphen_integrity(
            text_by_id, hyphen_pairs, chunk_ids, ocr_texts, hyphen_subs
        )


# ---------------------------------------------------------------------------
# #13 — explicit-mode subs join strips the FULL hyphen repertoire
# ---------------------------------------------------------------------------


def test_explicit_subs_join_accepts_non_ascii_break_char():
    # Fraktur double-oblique hyphen U+2E17 ("⸗"). The corrected pair keeps it
    # and the subs join must still match "Aufmerksamkeit".
    part1 = _line(
        "p1", "Aufmerksam⸗", role=HyphenRole.PART1, subs="Aufmerksamkeit", explicit=True
    )
    part2 = _line("p2", "keit", role=HyphenRole.PART2)
    f1, f2, subs = reconcile_hyphen_pair(part1, part2, "Aufmerksam⸗", "keit")
    # Not a fallback: the corrected texts and subs survive.
    assert f1 == "Aufmerksam⸗"
    assert f2 == "keit"
    assert subs == "Aufmerksamkeit"


# ---------------------------------------------------------------------------
# #6 — explicit-mode PART2 that absorbed trailing words falls back
# ---------------------------------------------------------------------------


def test_explicit_part2_absorption_falls_back():
    part1 = _line(
        "p1", "neces-", role=HyphenRole.PART1, subs="necessaires", explicit=True
    )
    part2 = _line("p2", "saires", role=HyphenRole.PART2)
    # PART2 absorbed "du roi" from the next line — boundary join still
    # matches subs, but the physical line grew.
    f1, f2, subs = reconcile_hyphen_pair(part1, part2, "neces-", "saires du roi")
    assert (f1, f2, subs) == (part1.ocr_text, part2.ocr_text, None)


def test_explicit_part2_no_absorption_accepted():
    part1 = _line(
        "p1", "neces-", role=HyphenRole.PART1, subs="necessaires", explicit=True
    )
    part2 = _line("p2", "saires", role=HyphenRole.PART2)
    f1, f2, subs = reconcile_hyphen_pair(part1, part2, "neces-", "saires")
    assert (f1, f2, subs) == ("neces-", "saires", "necessaires")


# ---------------------------------------------------------------------------
# #5 — E2 rejects a zero-length insertion co-located with a replacement
# ---------------------------------------------------------------------------


def test_e2_rejects_colocated_insertion_and_replacement():
    # insert@[2,2]='X' listed BEFORE replace@[2,7]='Y' on '0123456789'.
    # Old code accepted both and produced '01Y6789' (char 6 survived).
    script = EditScript(
        ops=[
            ReplaceSpan(line_id="l1", anchor=RangeAnchor(start=2, end=2), text="X"),
            ReplaceSpan(line_id="l1", anchor=RangeAnchor(start=2, end=7), text="Y"),
        ]
    )
    res = apply_edit_script(script, {"l1": "0123456789"})
    # The co-located pair must not corrupt the line: the replacement is
    # rejected as an overlap, so '6' can never survive a supposed [2,7) wipe.
    assert "6" not in res.text_by_id.get("l1", "0123456789")[:6] or res.rejected
    assert any(r.reason == "e2_overlap" for r in res.rejected)


# ---------------------------------------------------------------------------
# #30 — an id-less TextLine cannot round-trip; both parsers must refuse it
# ---------------------------------------------------------------------------

_ALTO_IDLESS = """\
<?xml version="1.0" encoding="UTF-8"?>
<alto xmlns="http://www.loc.gov/standards/alto/ns-v3#">
  <Layout>
    <Page ID="P1" WIDTH="1000" HEIGHT="1000">
      <PrintSpace HPOS="0" VPOS="0" WIDTH="1000" HEIGHT="1000">
        <TextBlock ID="B1" HPOS="0" VPOS="0" WIDTH="1000" HEIGHT="900">
          <TextLine HPOS="10" VPOS="10" WIDTH="900" HEIGHT="20">
            <String CONTENT="orphan" HPOS="10" VPOS="10" WIDTH="900" HEIGHT="20"/>
          </TextLine>
        </TextBlock>
      </PrintSpace>
    </Page>
  </Layout>
</alto>"""

_PAGE_IDLESS = """\
<?xml version="1.0" encoding="UTF-8"?>
<PcGts xmlns="http://schema.primaresearch.org/PAGE/gts/pagecontent/2019-07-15">
  <Page imageFilename="p.png" imageWidth="1000" imageHeight="1000">
    <TextRegion id="r1">
      <Coords points="0,0 1000,0 1000,900 0,900"/>
      <TextLine>
        <Coords points="10,10 900,10 900,30 10,30"/>
        <TextEquiv><Unicode>orphan</Unicode></TextEquiv>
      </TextLine>
    </TextRegion>
  </Page>
</PcGts>"""


def test_alto_idless_textline_refused(tmp_path):
    p = tmp_path / "a.xml"
    p.write_text(_ALTO_IDLESS, encoding="utf-8")
    with pytest.raises(ParseError, match="without an id"):
        parse_alto_file(p, "a.xml")


def test_page_idless_textline_refused(tmp_path):
    p = tmp_path / "p.xml"
    p.write_text(_PAGE_IDLESS, encoding="utf-8")
    with pytest.raises(ParseError, match="without an id"):
        parse_page_file(p, "p.xml")


# ---------------------------------------------------------------------------
# #32 — an id-less region under a ReadingOrder keeps document order
# ---------------------------------------------------------------------------


def test_idless_region_under_reading_order_keeps_document_order():
    from lxml import etree

    from corrigenda.formats.page._ns import _detect_namespace
    from corrigenda.formats.page.parser import _regions_in_reading_order

    # Regions [A(id), B(NO id), C(id)] with ReadingOrder [C, A]. The
    # declaration says nothing about B; sorting would yank B to the end.
    # Conservative fix: bail to document order [A, B, C].
    xml = """\
<PcGts xmlns="http://schema.primaresearch.org/PAGE/gts/pagecontent/2019-07-15">
  <Page imageFilename="p.png" imageWidth="1000" imageHeight="1000">
    <ReadingOrder>
      <OrderedGroup id="g">
        <RegionRefIndexed index="0" regionRef="C"/>
        <RegionRefIndexed index="1" regionRef="A"/>
      </OrderedGroup>
    </ReadingOrder>
    <TextRegion id="A"><Coords points="0,0 10,0 10,10 0,10"/></TextRegion>
    <TextRegion><Coords points="0,20 10,20 10,30 0,30"/></TextRegion>
    <TextRegion id="C"><Coords points="0,40 10,40 10,50 0,50"/></TextRegion>
  </Page>
</PcGts>"""
    root = etree.fromstring(xml.encode())
    ns = _detect_namespace(root)
    page_el = root.find(f"{{{ns}}}Page")
    ordered = _regions_in_reading_order(page_el, ns)
    assert [r.get("id") for r in ordered] == ["A", None, "C"]
