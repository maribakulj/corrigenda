"""Wave-0 E2E scenarios (Audit plan `docs/audit/PLAN-CORRECTIONS.md`).

Black-box tests against the REAL backend served by uvicorn, with the
Mistral provider pointed at a local mock vendor (honest or saboteur).
They pin the behaviour proven live on 2026-07-13:

- honest job: upload → SSE → download, corrections applied, geometry
  and hyphenation invariants intact;
- sabotaged job: hyphen fusion, next-line absorption and emptied lines
  are ALL intercepted by the guards — the lines fall back to OCR and
  the job ends ``completed_with_fallbacks``;
- capability token: every job endpoint is a 404 without the token.
"""

from __future__ import annotations

import httpx
import pytest
from lxml import etree

from tests.e2e._harness import (
    PAGE_SAMPLE_XML,
    SAMPLE_XML,
    collect_sse_until_terminal,
    download_xml,
    submit_job,
)

pytestmark = pytest.mark.e2e

_GEOMETRY_ATTRS = ("HPOS", "VPOS", "WIDTH", "HEIGHT")


def _lines_by_id(xml_bytes: bytes) -> dict[str, etree._Element]:
    root = etree.fromstring(xml_bytes)
    ns = {"a": root.nsmap[None]}
    return {el.get("ID"): el for el in root.findall(".//a:TextLine", ns)}


def _line_text(line_el: etree._Element) -> str:
    strings = [el for el in line_el if etree.QName(el).localname == "String"]
    return " ".join(el.get("CONTENT", "") for el in strings)


def _assert_geometry_unchanged(source: bytes, output: bytes) -> None:
    """Core invariant: TextLine identity and geometry are never touched."""
    src_lines = _lines_by_id(source)
    out_lines = _lines_by_id(output)
    assert set(out_lines) == set(src_lines)
    for line_id, src_el in src_lines.items():
        out_el = out_lines[line_id]
        for attr in _GEOMETRY_ATTRS:
            assert out_el.get(attr) == src_el.get(attr), (
                f"{line_id}@{attr}: {src_el.get(attr)!r} -> {out_el.get(attr)!r}"
            )


# ---------------------------------------------------------------------------
# Scenario 1 — honest vendor: full happy path
# ---------------------------------------------------------------------------


def test_honest_job_end_to_end(backend_server, use_honest_vendor):
    base_url = backend_server.base_url
    created = submit_job(base_url, model=use_honest_vendor)
    job_id, token = created["job_id"], created["job_token"]

    events = collect_sse_until_terminal(base_url, job_id, token)
    names = [name for name, _ in events]
    terminal_name, terminal_data = events[-1]
    assert terminal_name == "completed", events
    assert terminal_data["status"] == "completed"
    # Live progress reached the subscriber (not just the synthetic
    # terminal): the honest mock's completion delay guarantees at least
    # the end-of-chunk/page/stats events are emitted after we connect.
    progress = [n for n in names[:-1] if n not in ("keepalive",)]
    assert progress, f"no live progress events before terminal: {names}"

    status = httpx.get(f"{base_url}/api/jobs/{job_id}", headers={"X-Job-Token": token}, timeout=30)
    assert status.status_code == 200
    assert status.json()["status"] == "completed"

    output = download_xml(base_url, job_id, token)
    source = SAMPLE_XML.read_bytes()
    _assert_geometry_unchanged(source, output)

    text = output.decode("utf-8")
    # Honest corrections applied…
    for corrected in ("France", "citoyens", "troublée", "nationale", "principes"):
        assert corrected in text
    # …and the OCR confusions gone from CONTENT.
    for ocr_error in ("Frauce", "citoyeus", "tronblée", "uationale", "priucipes"):
        assert f'CONTENT="{ocr_error}' not in text

    # Hyphenation invariant: the explicit TL4/TL5 pair is intact — the
    # PART1 fragment still ends with '-', PART2 still starts the line,
    # and the SUBS_*/HYP markers survived the rewrite ON THIS PAIR
    # (attribute-level check: a whole-document substring would be
    # satisfied by the other explicit pair TL8/TL9).
    out_lines = _lines_by_id(output)
    tl4_text = _line_text(out_lines["TL4"])
    tl5_text = _line_text(out_lines["TL5"])
    assert tl4_text.endswith("dénon-"), tl4_text
    assert tl5_text.startswith("çait"), tl5_text
    tl4_last_string = [el for el in out_lines["TL4"] if el.tag.endswith("}String")][-1]
    assert tl4_last_string.get("SUBS_TYPE") == "HypPart1"
    assert tl4_last_string.get("SUBS_CONTENT") == "dénonçait"
    assert any(el.tag.endswith("}HYP") for el in out_lines["TL4"])
    tl5_first_string = [el for el in out_lines["TL5"] if el.tag.endswith("}String")][0]
    assert tl5_first_string.get("SUBS_TYPE") == "HypPart2"
    assert tl5_first_string.get("SUBS_CONTENT") == "dénonçait"


# ---------------------------------------------------------------------------
# Scenario 1 bis — honest vendor, PAGE XML (ROADMAP V3 Phase 0)
# ---------------------------------------------------------------------------


def test_honest_page_job_end_to_end(backend_server, use_honest_vendor):
    """PAGE crosses the full HTTP boundary: upload → SSE → download.

    Failed before the generic-loader switch: create_job hard-wired the
    ALTO parser, which reads this valid PAGE file as 0 pages / 0 lines,
    so the upload died on a 400 before any job existed.
    """
    base_url = backend_server.base_url
    created = submit_job(base_url, model=use_honest_vendor, xml_path=PAGE_SAMPLE_XML)
    job_id, token = created["job_id"], created["job_token"]

    events = collect_sse_until_terminal(base_url, job_id, token)
    terminal_name, terminal_data = events[-1]
    assert terminal_name == "completed", events
    assert terminal_data["status"] == "completed"

    output = download_xml(base_url, job_id, token)

    # The output is PAGE again (PcGts root, PAGE namespace) — never an
    # ALTO re-serialization.
    out_root = etree.fromstring(output)
    assert "primaresearch.org/PAGE" in out_root.nsmap[None]
    assert etree.QName(out_root).localname == "PcGts"

    # Line identity and order are invariant across the rewrite.
    src_root = etree.fromstring(PAGE_SAMPLE_XML.read_bytes())

    def _text_line_ids(root: etree._Element) -> list[str]:
        return [el.get("id") for el in root.iter() if etree.QName(el).localname == "TextLine"]

    assert _text_line_ids(out_root) == _text_line_ids(src_root)
    assert len(_text_line_ids(out_root)) > 0


# ---------------------------------------------------------------------------
# Scenario 2 — saboteur vendor: guards intercept every corruption
# ---------------------------------------------------------------------------


def test_sabotaged_job_falls_back_and_reports_it(backend_server, use_sabotage_vendor):
    base_url = backend_server.base_url
    created = submit_job(base_url, model=use_sabotage_vendor)
    job_id, token = created["job_id"], created["job_token"]

    events = collect_sse_until_terminal(base_url, job_id, token, timeout=300.0)
    terminal_name, terminal_data = events[-1]
    assert terminal_name == "completed", events
    # Degraded-success visibility: sabotaged lines were replaced by OCR
    # source, so the terminal status must be completed_with_fallbacks.
    assert terminal_data["status"] == "completed_with_fallbacks"
    assert terminal_data.get("fallbacks", 0) > 0

    output = download_xml(base_url, job_id, token)
    source = SAMPLE_XML.read_bytes()
    _assert_geometry_unchanged(source, output)

    out_lines = _lines_by_id(output)
    # TL4 — hyphen fusion refused: PART1 still ends with the fragment,
    # PART2 still holds its own fragment (nothing merged/moved).
    assert _line_text(out_lines["TL4"]).endswith("dénon-")
    assert _line_text(out_lines["TL5"]).startswith("çait")
    # TL7 — absorption refused: the line reverted to its OCR text and
    # did NOT swallow the next line's words.
    assert _line_text(out_lines["TL7"]) == "ments."
    # TL8 — the absorbed-from line still owns its words.
    assert "proclama" in _line_text(out_lines["TL8"])
    # TL10 — emptied line refused: OCR text preserved verbatim.
    assert _line_text(out_lines["TL10"]) == ("Ces priucipes allaient trausformer le moude eutier.")


def test_absorption_only_is_refused_by_the_line_guard(backend_server, use_absorption_vendor):
    """Isolated absorption on a NON-hyphenated line (TL2 absorbs TL3):
    nothing else can fail the chunk first, so the revert must come from
    the Stage-C absorption guard itself — a PER-LINE fallback (traced
    ``absorbs_next_line``) while every other line's honest correction
    survives. A guard-reverted line kept its OCR text, so the job-level
    fallback count (a LINE count) is 1 and the terminal status is the
    degraded success — never a clean ``completed`` that hides the
    uncorrected line."""
    base_url = backend_server.base_url
    created = submit_job(base_url, model=use_absorption_vendor)
    job_id, token = created["job_id"], created["job_token"]

    events = collect_sse_until_terminal(base_url, job_id, token, timeout=300.0)
    terminal_name, terminal_data = events[-1]
    assert terminal_name == "completed", events
    assert terminal_data["status"] == "completed_with_fallbacks"
    assert terminal_data["fallbacks"] == 1

    output = download_xml(base_url, job_id, token)
    _assert_geometry_unchanged(SAMPLE_XML.read_bytes(), output)
    out_lines = _lines_by_id(output)
    # The absorbed payload was refused; TL2 reverted verbatim to OCR…
    assert _line_text(out_lines["TL2"]) == "La Frauce traversa uue période tronblée."
    # …TL3 still owns its words…
    assert "citoyens" in _line_text(out_lines["TL3"])
    # …and honest corrections elsewhere were APPLIED (per-line revert,
    # not a whole-chunk collapse).
    assert "principes" in _line_text(out_lines["TL10"])
    assert "nationale" in _line_text(out_lines["TL8"])

    # The trace pins WHICH guard fired.
    trace = httpx.get(
        f"{base_url}/api/jobs/{job_id}/trace", headers={"X-Job-Token": token}, timeout=30
    )
    assert trace.status_code == 200
    tl2 = next(ln for ln in trace.json()["lines"] if ln["line_id"] == "TL2")
    assert tl2["decision"]["reason"]["code"] == "absorbs_next_line", tl2


# ---------------------------------------------------------------------------
# Scenario 3 — capability token gates the job endpoints (status, SSE,
# download — the remaining routes share the same require_job_access
# dependency in app/api/jobs.py)
# ---------------------------------------------------------------------------


def test_job_endpoints_are_404_without_token(backend_server, use_honest_vendor):
    base_url = backend_server.base_url
    created = submit_job(base_url, model=use_honest_vendor)
    job_id, token = created["job_id"], created["job_token"]

    # Wait for completion (via SSE) so download would otherwise succeed.
    collect_sse_until_terminal(base_url, job_id, token)

    for path in (f"/api/jobs/{job_id}", f"/api/jobs/{job_id}/download"):
        no_token = httpx.get(f"{base_url}{path}", timeout=30)
        assert no_token.status_code == 404, path
        bad_token = httpx.get(
            f"{base_url}{path}", headers={"X-Job-Token": "wrong-token"}, timeout=30
        )
        assert bad_token.status_code == 404, path
        # Plan V2.4 — the query-param transport is dead: a valid token in
        # the URL must be refused too (it would leak into proxy logs).
        query_token = httpx.get(f"{base_url}{path}", params={"token": token}, timeout=30)
        assert query_token.status_code == 404, path

    # SSE endpoint too (EventSource surface — uses ?sig=, see below).
    with httpx.stream("GET", f"{base_url}/api/jobs/{job_id}/events", timeout=30) as resp:
        assert resp.status_code == 404
    # The signed events_url from creation must open the stream.
    events_url = created["events_url"]
    with httpx.stream("GET", f"{base_url}{events_url}", timeout=30) as resp:
        assert resp.status_code == 200

    # With the token, the same endpoints answer.
    ok = httpx.get(f"{base_url}/api/jobs/{job_id}", headers={"X-Job-Token": token}, timeout=30)
    assert ok.status_code == 200
