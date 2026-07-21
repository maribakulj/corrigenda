"""Every run is side-effect-free and fully reported (spec §9, ADR-011).

Historically ``run(apply=False)`` was the dry-run mode: the full
pipeline ran but the injected ``OutputWriter`` stayed untouched. Slice
D-fin removed the writer and the flag from the engine surface — now
EVERY run behaves that way: the engine touches no filesystem, and the
returned :class:`CorrectionResult` (report, EditScript, corrected
bytes) is the whole deliverable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from corrigenda import CorrectionPipeline, CorrectionReport
from corrigenda.formats.alto.parser import build_document_manifest

_SAMPLE = Path(__file__).parent.parent.parent.parent / "examples" / "sample.xml"


class _IdentityProvider:
    async def list_models(self, api_key: str) -> list[Any]:
        return []

    async def complete_structured(self, **kw) -> tuple[dict[str, Any], Any]:
        payload = kw["user_payload"]
        return {
            "lines": [
                {"line_id": ln["line_id"], "corrected_text": ln["ocr_text"]}
                for ln in payload.get("lines", [])
            ]
        }, None


class _Null:
    def on_event(self, *a, **k):
        pass


async def _run():
    doc = build_document_manifest([(_SAMPLE, _SAMPLE.name)])
    pipeline = CorrectionPipeline.for_provider(
        _IdentityProvider(),
        api_key="k",
        model="m",
        observer=_Null(),
    )
    return await pipeline.run(
        document_manifest=doc,
        source_files={_SAMPLE.name: _SAMPLE},
    )


@pytest.mark.asyncio
async def test_run_persists_nothing_but_reports(tmp_path, monkeypatch):
    # Run from an empty cwd: if the engine wrote anything anywhere
    # relative, it would land here.
    monkeypatch.chdir(tmp_path)
    result = await _run()
    assert list(tmp_path.iterdir()) == [], "the engine must not touch the fs"
    # The report is fully populated, including the in-memory rewrite.
    assert isinstance(result.report, CorrectionReport)
    assert result.report.total_lines > 0
    assert result.report.lines
    assert all(
        ln.projection is not None and ln.projection.rewriter_path is not None
        for ln in result.report.lines
    )
    assert all(ln.projection.extracted_text is not None for ln in result.report.lines)
    # And the artefact travels on the result instead.
    assert result.corrected_files[_SAMPLE.name].startswith(b"<?xml")


def test_engine_surface_has_no_writer_and_no_apply():
    """The retirement itself, pinned: neither the constructor nor run()
    accepts the pre-ADR-011 persistence surface."""
    import inspect

    ctor = inspect.signature(CorrectionPipeline.__init__).parameters
    assert "output_writer" not in ctor
    for method in (CorrectionPipeline.run, CorrectionPipeline.run_sync):
        assert "apply" not in inspect.signature(method).parameters
    assert (
        "output_writer"
        not in inspect.signature(CorrectionPipeline.for_provider).parameters
    )


@pytest.mark.asyncio
async def test_report_version_is_stable():
    result = await _run()
    assert result.report.report_version == "2.0"
    # round-trips through JSON with a stable schema
    dumped = result.report.model_dump_json()
    assert '"report_version":"2.0"' in dumped.replace(" ", "")


@pytest.mark.asyncio
async def test_run_returns_normalized_edit_script():
    """§4 — the run returns the normalized EditScript it applied. With
    the identity provider every op is a replace_line whose text equals
    the line's OCR text, one op per corrected line."""
    from corrigenda.core.editing import EditScript, ReplaceLine

    result = await _run()
    script = result.edit_script
    assert isinstance(script, EditScript)
    assert script.ops, "the run must surface the EditScript"
    assert all(isinstance(op, ReplaceLine) for op in script.ops)
    # One op per line trace; the op text matches the projected line text.
    by_line = {op.line_id: op.text for op in script.ops}
    for tr in result.report.lines:
        assert by_line.get(tr.line_id) == tr.source_text
