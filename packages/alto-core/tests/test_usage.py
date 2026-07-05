"""Usage token accounting surfaced by the pipeline (spec F14, §5.1)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from alto_core import CorrectionPipeline, Usage
from alto_core.alto.parser import build_document_manifest

_SAMPLE = Path(__file__).parent.parent.parent.parent / "examples" / "sample.xml"


class _UsageProvider:
    """Reports a fixed usage per call."""

    def __init__(self) -> None:
        self.calls = 0

    async def list_models(self, api_key: str) -> list[Any]:
        return []

    async def complete_structured(self, **kw) -> tuple[dict[str, Any], Usage | None]:
        self.calls += 1
        payload = kw["user_payload"]
        out = {
            "lines": [
                {"line_id": ln["line_id"], "corrected_text": ln["ocr_text"]}
                for ln in payload.get("lines", [])
            ]
        }
        return out, Usage(input_tokens=10, output_tokens=3)


class _Null:
    def on_event(self, *a, **k):
        pass

    def write_corrected(self, **k):
        pass

    def write_trace(self, **k):
        pass


def test_usage_add_and_total():
    a = Usage(input_tokens=5, output_tokens=2)
    b = Usage(input_tokens=1, output_tokens=4)
    c = a + b
    assert (c.input_tokens, c.output_tokens) == (6, 6)
    assert c.total_tokens == 12


@pytest.mark.asyncio
async def test_pipeline_aggregates_usage_into_result():
    provider = _UsageProvider()
    doc = build_document_manifest([(_SAMPLE, _SAMPLE.name)])
    pipeline = CorrectionPipeline(
        provider=provider, observer=_Null(), output_writer=_Null()
    )
    result = await pipeline.run(
        document_manifest=doc,
        api_key="k",
        model="m",
        provider_name="mock",
        source_files={},
    )
    assert provider.calls >= 1
    assert result.usage.input_tokens == 10 * provider.calls
    assert result.usage.output_tokens == 3 * provider.calls


@pytest.mark.asyncio
async def test_usage_is_zero_when_provider_reports_none():
    class _NoUsage(_UsageProvider):
        async def complete_structured(self, **kw):
            out, _ = await super().complete_structured(**kw)
            return out, None

    doc = build_document_manifest([(_SAMPLE, _SAMPLE.name)])
    pipeline = CorrectionPipeline(
        provider=_NoUsage(), observer=_Null(), output_writer=_Null()
    )
    result = await pipeline.run(
        document_manifest=doc,
        api_key="k",
        model="m",
        provider_name="mock",
        source_files={},
    )
    assert result.usage.total_tokens == 0
