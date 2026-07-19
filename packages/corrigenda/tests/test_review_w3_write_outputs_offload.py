"""Wave-3 adversarial-review follow-up — the output rewrite ran inline
on the event loop.

``_render_outputs`` calls ``adapter.rewrite_file`` (a full lxml
parse/rewrite/serialize of the source file — ~100 MiB corpora) and
``adapter.extract_texts`` synchronously from the async ``run()``. In the
backend, that blocks SSE keepalives and /health for the whole rewrite,
exactly the class Audit-F19/F21 fixed in the request handlers.

The test pins LOOP RESPONSIVENESS directly: a ticker coroutine must keep
ticking while a (deliberately slow) rewrite runs. Pre-fix the 0.8 s
sleep inside rewrite_file froze the loop — the max tick gap was ≥ 0.8 s.
"""

from __future__ import annotations

import asyncio
import time

from corrigenda.core.pipeline import CorrectionPipeline
from corrigenda.formats.alto.parser import build_document_manifest
from tests._pipeline_harness import DictProvider, RecordingObserver
from tests.test_planner_budget_and_cross_chunk_guard import _write_doc

_REWRITE_DELAY = 0.8
_MAX_ACCEPTABLE_GAP = 0.5


class _SlowRewriteAdapter:
    """Delegates to the real ALTO adapter, but makes rewrite_file slow —
    a stand-in for a large file's parse/rewrite/serialize cost."""

    def __init__(self, inner: object) -> None:
        self._inner = inner

    def rewrite_file(self, *args: object, **kwargs: object) -> object:
        time.sleep(_REWRITE_DELAY)
        return self._inner.rewrite_file(*args, **kwargs)  # type: ignore[attr-defined]

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


def test_review_w3_rewrite_does_not_block_the_event_loop(tmp_path):
    from corrigenda.core.pipeline import _adapter_for_format

    path = _write_doc(tmp_path)
    doc = build_document_manifest([(path, "doc.xml")])

    pipeline = CorrectionPipeline.for_provider(
        DictProvider({}),  # identity corrections — rewrite still runs
        api_key="k",
        model="m",
        observer=RecordingObserver(),
        format_adapter=_SlowRewriteAdapter(_adapter_for_format("alto")),
    )

    gaps: list[float] = []

    async def _main() -> None:
        stop = asyncio.Event()

        async def _ticker() -> None:
            last = time.monotonic()
            while not stop.is_set():
                await asyncio.sleep(0.01)
                now = time.monotonic()
                gaps.append(now - last)
                last = now

        ticker = asyncio.create_task(_ticker())
        # Give the ticker a head start: a DictProvider run can otherwise
        # complete without ever yielding to the loop, and the ticker
        # would first run only after stop is already set (zero gaps).
        await asyncio.sleep(0.03)
        try:
            await pipeline.run(
                document_manifest=doc,
                source_files={"doc.xml": path},
            )
        finally:
            stop.set()
            await ticker

    asyncio.run(_main())

    assert gaps, "ticker never ticked"
    worst = max(gaps)
    assert worst < _MAX_ACCEPTABLE_GAP, (
        f"event loop stalled {worst:.2f}s during the rewrite "
        f"(rewrite runs inline instead of in a worker thread)"
    )
