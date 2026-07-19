"""Shared real-pipeline test harness (audit Phase 1 — securing by tests).

The corpus reconciliation tests used to drive a *hand-rolled copy* of the
pipeline's hyphen-reconciliation loop (``_reconcile_all_pairs`` in
``test_x0000002.py``). That copy could pass while the real
:class:`CorrectionPipeline` diverged — a false-confidence gate over the
single most load-bearing invariant of the system (a hyphen pair must
never be split, and a mixed pair — one line at OCR, one corrected — must
never survive).

This module drives the **real** pipeline end to end so that any later
refactor of hyphen partner-resolution (audit Problem 1) or guard
placement (audit Problem 2) is actually protected: inject a fault into
``pipeline._reconcile_chunk_hyphens`` and the tests built on this harness
go red, which the phantom driver never did.

A ``DictProvider`` returns a caller-controlled correction per line
(defaulting to identity = the enriched OCR text), so a test expresses
"the LLM answered X for line L" without any network.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from corrigenda.core.pipeline import CorrectionPipeline, CorrectionResult
from corrigenda.core.schemas import DocumentManifest, LineManifest
from corrigenda.formats.alto.parser import build_document_manifest

EXAMPLES = Path(__file__).resolve().parent.parent.parent.parent / "examples"


class DictProvider:
    """A ``BaseProvider`` that echoes a fixed ``{line_id: corrected_text}``.

    Any line not in ``corrections`` is returned unchanged (identity =
    the enriched ``ocr_text`` the pipeline sent), so an empty mapping is a
    pure identity pass.
    """

    def __init__(self, corrections: dict[str, str] | None = None) -> None:
        self.corrections = corrections or {}

    async def list_models(self, api_key: str) -> list:  # pragma: no cover
        return []

    async def complete_structured(
        self,
        *,
        api_key: str,
        model: str,
        system_prompt: str,
        user_payload: dict[str, Any],
        json_schema: dict[str, Any],
        temperature: float = 0.0,
    ) -> tuple[dict[str, Any], None]:
        lines = user_payload.get("lines", [])
        out = [
            {
                "line_id": ln["line_id"],
                "corrected_text": self.corrections.get(
                    ln["line_id"], ln.get("ocr_text", "")
                ),
            }
            for ln in lines
        ]
        return {"lines": out}, None


class RecordingObserver:
    """Collects every pipeline event as ``(event_value, payload)`` tuples."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def on_event(self, event_type: Any, payload: dict[str, Any]) -> None:
        value = event_type.value if hasattr(event_type, "value") else str(event_type)
        self.events.append((value, payload))

    def count(self, event_value: str) -> int:
        return sum(1 for v, _ in self.events if v == event_value)


@dataclass
class PipelineRun:
    """The observable surface of one real pipeline run over a corpus file."""

    result: CorrectionResult
    document_manifest: DocumentManifest
    observer: RecordingObserver

    @property
    def lines(self) -> dict[str, LineManifest]:
        return {
            lm.line_id: lm for page in self.document_manifest.pages for lm in page.lines
        }


def run_pipeline(
    xml_name: str,
    corrections: dict[str, str] | None = None,
) -> PipelineRun:
    """Run the real ``CorrectionPipeline`` over ``examples/<xml_name>``.

    The in-memory rewrite runs so reconciliation and acceptance execute
    exactly as in production; nothing is persisted (the engine has no
    writer — ADR-011). The document manifest is mutated in place; read
    ``run.lines`` for per-line corrected_text / status, and
    ``run.result.reconcile_metrics`` for the real pipeline's
    reconciliation counts (NOT a test-side re-implementation).
    """
    path = EXAMPLES / xml_name
    doc = build_document_manifest([(path, xml_name)])
    observer = RecordingObserver()
    pipeline = CorrectionPipeline.for_provider(
        DictProvider(corrections),
        api_key="k",
        model="m",
        observer=observer,
    )
    result = pipeline.run_sync(
        document_manifest=doc,
        source_files={xml_name: path},
    )
    return PipelineRun(result=result, document_manifest=doc, observer=observer)
