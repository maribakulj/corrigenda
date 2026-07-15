"""Plan V4.1 — the pipeline's concurrency contract is enforced.

Per-run state (counters, producer ops, accepted snapshots, finalisation
owners) lives on the instance and resets at the start of each run(): a
second concurrent run() on the same instance would wipe the first's
state mid-flight. The guard turns that silent corruption into an
immediate RuntimeError; sequential re-use stays supported.
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
        raise AssertionError("released without cancellation — not expected in this test")


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
