"""P3.10 — EditScript preconditions: a script only applies to the
document it was computed against.

The script records its protocol version, the run's source-file digests,
and one LinePrecondition (source-text digest) per op-carrying line.
``apply_edit_script`` raises on an unknown protocol version and rejects
ops whose target line carries the same id over DIFFERENT content — a
lookalike is never a target.
"""

from __future__ import annotations

import hashlib

import pytest

from corrigenda.core.editing import (
    EDIT_PROTOCOL_VERSION,
    EditScript,
    LinePrecondition,
    R_PRECONDITION,
    ReplaceLine,
    apply_edit_script,
    line_digest,
)
from corrigenda.errors import ValidationError
from corrigenda.formats.alto.parser import build_document_manifest

from tests._pipeline_harness import EXAMPLES, DictProvider, RecordingObserver

_SAMPLE = EXAMPLES / "sample.xml"


def _script(text: str = "corrigé", digest: str | None = None) -> EditScript:
    return EditScript(
        ops=[ReplaceLine(line_id="l1", text=text)],
        preconditions=(
            [LinePrecondition(line_id="l1", digest=digest)] if digest else []
        ),
    )


# ---------------------------------------------------------------------------
# Per-line digests
# ---------------------------------------------------------------------------


def test_matching_digest_applies():
    src = "texte source"
    result = apply_edit_script(_script(digest=line_digest(src)), {"l1": src})
    assert result.text_by_id == {"l1": "corrigé"}
    assert result.rejected == []


def test_diverging_content_is_rejected_never_edited():
    """Same line_id, different content: the op must not land."""
    result = apply_edit_script(
        _script(digest=line_digest("texte source")),
        {"l1": "un tout autre texte"},
    )
    assert result.text_by_id == {}  # the line keeps its prior text
    assert len(result.rejected) == 1
    rej = result.rejected[0]
    assert rej.reason == R_PRECONDITION
    assert rej.line_id == "l1"


def test_script_without_preconditions_keeps_historical_behaviour():
    result = apply_edit_script(_script(), {"l1": "peu importe"})
    assert result.text_by_id == {"l1": "corrigé"}


def test_precondition_page_scope_follows_the_ops():
    """A precondition stamped for ANOTHER page is out of scope, exactly
    like an op stamped for another page (line_ids repeat across files)."""
    script = EditScript(
        ops=[ReplaceLine(line_id="l1", text="corrigé", page_id="pgA")],
        preconditions=[
            # pgB's l1 had different text — irrelevant when replaying pgA.
            LinePrecondition(line_id="l1", page_id="pgB", digest=line_digest("autre")),
            LinePrecondition(
                line_id="l1", page_id="pgA", digest=line_digest("bon texte")
            ),
        ],
    )
    result = apply_edit_script(script, {"l1": "bon texte"}, page_id="pgA")
    assert result.text_by_id == {"l1": "corrigé"}
    assert result.rejected == []


# ---------------------------------------------------------------------------
# Protocol version
# ---------------------------------------------------------------------------


def test_unknown_protocol_version_fails_loudly():
    script = EditScript(
        ops=[ReplaceLine(line_id="l1", text="x")], protocol_version="99"
    )
    with pytest.raises(ValidationError, match="protocol version"):
        apply_edit_script(script, {"l1": "y"})


def test_current_and_absent_protocol_versions_apply():
    for version in (EDIT_PROTOCOL_VERSION, None):
        script = EditScript(
            ops=[ReplaceLine(line_id="l1", text="x")], protocol_version=version
        )
        assert apply_edit_script(script, {"l1": "y"}).text_by_id == {"l1": "x"}


# ---------------------------------------------------------------------------
# The run's final edit script is stamped
# ---------------------------------------------------------------------------


def _run_result():
    from corrigenda.core.pipeline import CorrectionPipeline

    doc = build_document_manifest([(_SAMPLE, _SAMPLE.name)])
    target = doc.pages[0].lines[0]
    pipeline = CorrectionPipeline.for_provider(
        DictProvider({target.line_id: target.ocr_text + " corrigé"}),
        api_key="k",
        model="m",
        observer=RecordingObserver(),
    )
    result = pipeline.run_sync(
        document_manifest=doc, source_files={_SAMPLE.name: _SAMPLE}
    )
    return doc, target, result


def test_final_edit_script_carries_preconditions():
    doc, target, result = _run_result()
    script = result.edit_script

    assert script.protocol_version == EDIT_PROTOCOL_VERSION
    expected_digest = "sha256:" + hashlib.sha256(_SAMPLE.read_bytes()).hexdigest()
    assert script.source_digests == {_SAMPLE.name: expected_digest}
    # Same digests the provenance record carries — shared computation.
    assert result.report.provenance is not None
    assert script.source_digests == result.report.provenance.source_digests

    by_line = {pc.line_id: pc for pc in script.preconditions}
    pc = by_line[target.line_id]
    assert pc.page_id == target.page_id
    assert pc.digest == line_digest(target.ocr_text)
    # One precondition per op-carrying line.
    assert set(by_line) == {op.line_id for op in script.ops}


def test_final_edit_script_replays_on_the_original_and_refuses_a_fake():
    doc, target, result = _run_result()
    script = result.edit_script
    canonical = {lm.line_id: lm.ocr_text for p in doc.pages for lm in p.lines}

    # Replay on the original document: every op lands.
    replay = apply_edit_script(script, canonical, page_id=target.page_id)
    assert replay.text_by_id[target.line_id] == target.ocr_text + " corrigé"
    assert replay.rejected == []

    # Replay on a lookalike (same ids, different text): explicit rejection.
    fake = {lid: text + " altéré" for lid, text in canonical.items()}
    replay_fake = apply_edit_script(script, fake, page_id=target.page_id)
    assert target.line_id not in replay_fake.text_by_id
    assert any(r.reason == R_PRECONDITION for r in replay_fake.rejected)
