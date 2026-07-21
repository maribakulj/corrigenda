"""P3.7 — the core needs completions only; the catalog is app-side.

``StructuredCompletionClient`` (complete_structured) is the ONLY LLM
capability the engine consumes; ``ModelCatalog`` (list_models) is
application vocabulary. The pin: a client with NO ``list_models`` at
all drives a full run through ``for_provider``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from corrigenda import (
    BaseProvider,
    CorrectionPipeline,
    ModelCatalog,
    StructuredCompletionClient,
)
from corrigenda.formats.alto.parser import build_document_manifest

_SAMPLE = Path(__file__).parent.parent.parent.parent / "examples" / "sample.xml"


class _CompletionsOnly:
    """A client that can ONLY complete — no list_models anywhere."""

    async def complete_structured(self, **kw: Any) -> tuple[dict[str, Any], None]:
        payload = kw["user_payload"]
        return {
            "lines": [
                {"line_id": ln["line_id"], "corrected_text": ln["ocr_text"]}
                for ln in payload.get("lines", [])
            ]
        }, None


class _Null:
    def on_event(self, *a: Any, **k: Any) -> None:
        pass


def test_protocol_split_is_structural() -> None:
    client = _CompletionsOnly()
    assert isinstance(client, StructuredCompletionClient)
    assert not isinstance(client, ModelCatalog)
    assert not isinstance(client, BaseProvider)


@pytest.mark.asyncio
async def test_completions_only_client_drives_a_full_run() -> None:
    doc = build_document_manifest([(_SAMPLE, _SAMPLE.name)])
    pipeline = CorrectionPipeline.for_provider(
        _CompletionsOnly(),
        api_key="k",
        model="m",
        observer=_Null(),
    )
    result = await pipeline.run(
        document_manifest=doc, source_files={_SAMPLE.name: _SAMPLE}
    )
    assert result.corrected_files
    assert result.fallback_lines == 0
