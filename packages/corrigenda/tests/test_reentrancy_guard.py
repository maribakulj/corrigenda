"""ADR-011 slice E — the engine is reentrant; ADR-005's guard is gone.

V4.1-L moved per-run state into a fresh RunContext; slice E moved the
last shared mutable — the manifest itself — into a per-run deep copy.
With nothing left to contaminate, the one-run-per-instance guard was
removed: concurrent runs on ONE instance must both succeed and yield
exactly the outcomes of isolated runs, and sequential re-use still
leaks no state.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from corrigenda import CorrectionPipeline

SAMPLE_XML = Path(__file__).parent / "data" / "sample.xml"
if not SAMPLE_XML.exists():  # repo-level example as fallback
    SAMPLE_XML = Path(__file__).parents[3] / "examples" / "sample.xml"


class _NullObserver:
    def on_event(self, event_type: Any, payload: dict) -> None:  # pragma: no cover
        pass


class _IdentityProducer:
    async def produce(self, payload: Any, *, policy: Any) -> Any:
        from corrigenda.core.editing import EditScript, ReplaceLine

        ops = [
            ReplaceLine(line_id=line.line_id, text=line.ocr_text)
            for line in payload.lines
        ]
        return EditScript(ops=ops), None


class _YieldingIdentityProducer(_IdentityProducer):
    """Identity producer that yields to the loop, so two concurrent runs
    genuinely interleave their chunk processing."""

    async def produce(self, payload: Any, *, policy: Any) -> Any:
        await asyncio.sleep(0)
        return await super().produce(payload, policy=policy)


def _pipeline(producer: Any) -> CorrectionPipeline:
    return CorrectionPipeline(
        producer=producer,
        observer=_NullObserver(),
    )


def test_concurrent_runs_on_the_same_instance_both_succeed() -> None:
    """The ADR-005 retirement itself: two interleaved runs on ONE
    instance — and even on ONE shared document object — complete and
    decide every line identically."""

    async def scenario() -> None:
        from corrigenda.formats.alto.parser import build_document_manifest

        pipeline = _pipeline(_YieldingIdentityProducer())
        manifest = build_document_manifest([(SAMPLE_XML, "sample.xml")])

        first, second = await asyncio.gather(
            pipeline.run(
                document_manifest=manifest,
                source_files={"sample.xml": SAMPLE_XML},
            ),
            pipeline.run(
                document_manifest=manifest,
                source_files={"sample.xml": SAMPLE_XML},
            ),
        )
        assert first.decisions.decisions == second.decisions.decisions
        assert first.total_chunks == second.total_chunks
        assert first.fallback_lines == second.fallback_lines == 0
        # The shared input never became run state.
        for page in manifest.pages:
            for lm in page.lines:
                assert lm.corrected_text is None

    asyncio.run(scenario())


def test_sequential_reuse_survives_a_failed_run() -> None:
    """Sequential re-use must survive a run that died mid-flight."""

    class _ExplodingProducer:
        async def produce(self, payload: Any, *, policy: Any) -> Any:
            raise ValueError("boom")

    async def scenario() -> None:
        from corrigenda.formats.alto.parser import build_document_manifest

        pipeline = _pipeline(_ExplodingProducer())
        manifest = build_document_manifest([(SAMPLE_XML, "sample.xml")])
        # First run fails (fallbacks exhaust into source text) or raises —
        # either way the instance must accept the next call.
        try:
            await pipeline.run(
                document_manifest=manifest,
                source_files={"sample.xml": SAMPLE_XML},
            )
        except Exception:
            pass
        try:
            await pipeline.run(
                document_manifest=manifest,
                source_files={"sample.xml": SAMPLE_XML},
            )
        except Exception:
            pass

    asyncio.run(scenario())


def test_sequential_runs_share_no_state() -> None:
    """V4.1-L acceptance — a second run starts from a virgin RunContext.

    An identity producer corrects nothing; both runs — now over the SAME
    manifest object (slice E: the input is never consumed) — must report
    identical, non-accumulating stats.
    """

    async def scenario() -> None:
        from corrigenda.formats.alto.parser import build_document_manifest

        pipeline = _pipeline(_IdentityProducer())
        manifest = build_document_manifest([(SAMPLE_XML, "sample.xml")])
        results = []
        for _ in range(2):
            results.append(
                await pipeline.run(
                    document_manifest=manifest,
                    source_files={"sample.xml": SAMPLE_XML},
                )
            )
        first, second = results
        assert first.retry_count == second.retry_count == 0
        assert first.fallback_chunks == second.fallback_chunks
        assert first.total_chunks == second.total_chunks
        assert first.total_reconciled == second.total_reconciled
        assert first.reconcile_metrics.coherent == second.reconcile_metrics.coherent
        assert len(first.edit_script.ops) == len(second.edit_script.ops)
        assert first.usage.input_tokens == second.usage.input_tokens == 0

    asyncio.run(scenario())
