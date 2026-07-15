"""Plan V4.1 — the pipeline's concurrency contract is enforced.

V4.1-L: per-run state lives in a fresh RunContext per execution — the
instance carries only immutable configuration. The reentrancy guard
stays because the injected observer/output_writer are shared: two
concurrent runs would interleave events and overwrite outputs. The
guard turns that into an immediate RuntimeError; sequential re-use
stays supported and leaks no state across runs.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from corrigenda import CorrectionPipeline
from corrigenda.formats.alto.parser import build_document_manifest

SAMPLE_XML = Path(__file__).parent / "data" / "sample.xml"
if not SAMPLE_XML.exists():  # repo-level example as fallback
    SAMPLE_XML = Path(__file__).parents[3] / "examples" / "sample.xml"


class _BlockingProducer:
    """Producer that parks on an event so the run stays in flight."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def produce(self, payload: Any, *, policy: Any) -> Any:
        self.entered.set()
        await self.release.wait()
        raise AssertionError(
            "released without cancellation — not expected in this test"
        )


class _NullObserver:
    def on_event(self, event_type: Any, payload: dict) -> None:  # pragma: no cover
        pass


class _NullWriter:
    def write_corrected_xml(self, *a: Any, **k: Any) -> None:  # pragma: no cover
        pass

    def write_trace(self, *a: Any, **k: Any) -> None:  # pragma: no cover
        pass


def _pipeline(producer: Any) -> CorrectionPipeline:
    return CorrectionPipeline(
        producer=producer,
        observer=_NullObserver(),
        output_writer=_NullWriter(),
    )


def test_concurrent_run_on_the_same_instance_raises() -> None:
    async def scenario() -> None:
        producer = _BlockingProducer()
        pipeline = _pipeline(producer)
        manifest = build_document_manifest([(SAMPLE_XML, "sample.xml")])

        first = asyncio.create_task(
            pipeline.run(
                document_manifest=manifest,
                source_files={"sample.xml": SAMPLE_XML},
            )
        )
        await asyncio.wait_for(producer.entered.wait(), timeout=10)

        second_manifest = build_document_manifest([(SAMPLE_XML, "sample.xml")])
        with pytest.raises(RuntimeError, match="one run at a time"):
            await pipeline.run(
                document_manifest=second_manifest,
                source_files={"sample.xml": SAMPLE_XML},
            )

        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

    asyncio.run(scenario())


def test_guard_releases_after_a_failed_run() -> None:
    """Sequential re-use must survive a run that died mid-flight."""

    class _ExplodingProducer:
        async def produce(self, payload: Any, *, policy: Any) -> Any:
            raise ValueError("boom")

    async def scenario() -> None:
        pipeline = _pipeline(_ExplodingProducer())
        manifest = build_document_manifest([(SAMPLE_XML, "sample.xml")])
        # First run fails (fallbacks exhaust into source text) or raises —
        # either way the guard must be released for the next call.
        try:
            await pipeline.run(
                document_manifest=manifest,
                source_files={"sample.xml": SAMPLE_XML},
            )
        except Exception:
            pass
        fresh = build_document_manifest([(SAMPLE_XML, "sample.xml")])
        try:
            await pipeline.run(
                document_manifest=fresh,
                source_files={"sample.xml": SAMPLE_XML},
            )
        except RuntimeError as exc:  # pragma: no cover
            if "one run at a time" in str(exc):
                pytest.fail("guard leaked: sequential re-use blocked after a failure")
            raise
        except Exception:
            pass

    asyncio.run(scenario())


def test_sequential_runs_share_no_state() -> None:
    """V4.1-L acceptance — a second run starts from a virgin RunContext.

    An identity producer corrects nothing; both runs over fresh manifests
    of the same file must report identical, non-accumulating stats. Under
    the pre-refactor instance-state model this held only thanks to manual
    resets at the top of run(); RunContext makes it structural.
    """

    class _IdentityProducer:
        async def produce(self, payload: Any, *, policy: Any) -> Any:
            from corrigenda.core.editing import EditScript, ReplaceLine

            ops = [
                ReplaceLine(line_id=line.line_id, text=line.ocr_text)
                for line in payload.lines
            ]
            return EditScript(ops=ops), None

    async def scenario() -> None:
        pipeline = _pipeline(_IdentityProducer())
        results = []
        for _ in range(2):
            manifest = build_document_manifest([(SAMPLE_XML, "sample.xml")])
            results.append(
                await pipeline.run(
                    document_manifest=manifest,
                    source_files={"sample.xml": SAMPLE_XML},
                    apply=False,
                )
            )
        first, second = results
        assert first.retry_count == second.retry_count == 0
        assert first.fallback_count == second.fallback_count
        assert first.total_chunks == second.total_chunks
        assert first.total_reconciled == second.total_reconciled
        assert first.reconcile_metrics.coherent == second.reconcile_metrics.coherent
        assert len(first.edit_script.ops) == len(second.edit_script.ops)
        assert first.usage.input_tokens == second.usage.input_tokens == 0

    asyncio.run(scenario())
