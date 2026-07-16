"""EditProducer contract, vision envelope, LLM adapter, and I4 (§5.1, §4.1)."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from corrigenda.core.editing import EditScript, ReplaceLine
from corrigenda.core.hyphenation import enrich_chunk_lines
from corrigenda.core.protocols import EditProducer, require_page_images
from corrigenda.core.schemas import (
    PageManifest,
    ChunkGranularity,
    Coords,
    LineManifest,
    LLMUserPayload,
    RetryPolicy,
    Usage,
)
from corrigenda.errors import ConfigurationError
from corrigenda.producers.llm_edit import LLMEditProducer
from corrigenda.producers.rules import RulesProducer, default_french_ocr_rules

_SRC = Path(__file__).parent.parent / "src" / "corrigenda"


def _line(line_id: str, page_id: str = "pg") -> LineManifest:
    return LineManifest(
        line_id=line_id,
        page_id=page_id,
        block_id="b",
        line_order_global=0,
        line_order_in_block=0,
        coords=Coords(hpos=1, vpos=2, width=3, height=4),
        ocr_text="text",
    )


# ---------------------------------------------------------------------------
# Contract shape
# ---------------------------------------------------------------------------


def test_rules_and_llm_producers_have_wants_flags():
    r = RulesProducer(default_french_ocr_rules())
    assert r.wants_geometry is False and r.wants_image is False


# ---------------------------------------------------------------------------
# Vision envelope copy (§4.1) — compiler copies geometry, never opens a pixel
# ---------------------------------------------------------------------------


def test_enrich_omits_geometry_by_default():
    lm = _line("l1")
    inputs = enrich_chunk_lines([lm], {"l1": lm})
    assert inputs[0].geometry is None


def test_enrich_copies_geometry_when_requested():
    lm = _line("l1", page_id="pageA")
    inputs = enrich_chunk_lines(
        [lm],
        {"l1": lm},
        include_geometry=True,
        page_dims={"pageA": (1000, 2000)},
    )
    geo = inputs[0].geometry
    assert geo is not None
    assert geo.coords == lm.coords
    assert geo.page_width == 1000 and geo.page_height == 2000


def test_payload_carries_opaque_image_ref():
    lm = _line("l1")
    payload = LLMUserPayload(
        granularity=ChunkGranularity.LINE,
        document_id="d",
        page_id="pg",
        lines=enrich_chunk_lines([lm], {"l1": lm}),
        image_ref="s3://bucket/page1.tif",  # opaque, never opened
    )
    assert payload.image_ref == "s3://bucket/page1.tif"


# ---------------------------------------------------------------------------
# require_page_images (§5.1) — one image per PAGE, never per file
# ---------------------------------------------------------------------------


class _VisionProducer:
    wants_geometry = True
    wants_image = True

    async def produce(self, payload: LLMUserPayload, *, policy: RetryPolicy):
        return EditScript(ops=[]), None


def _page(page_id: str, source: str) -> PageManifest:
    return PageManifest(
        page_id=page_id,
        source_file=source,
        page_index=0,
        page_width=1000,
        page_height=1000,
        blocks=[],
        lines=[],
    )


def test_text_producer_never_requires_images():
    require_page_images(RulesProducer([]), [_page("P1", "a.xml")], None)  # no raise


def test_vision_producer_without_images_raises():
    with pytest.raises(ConfigurationError):
        require_page_images(_VisionProducer(), [_page("P1", "a.xml")], None)


def test_vision_producer_missing_page_raises():
    """Coverage is PER PAGE: a multipage file with one ref is incomplete —
    the historical per-file mapping silently sent page 1's scan for every
    page of the file."""
    pages = [_page("P1", "a.xml"), _page("P2", "a.xml")]
    with pytest.raises(ConfigurationError, match="P2"):
        require_page_images(_VisionProducer(), pages, {"P1": "img-p1"})


def test_vision_producer_with_all_pages_ok():
    pages = [_page("P1", "a.xml"), _page("P2", "a.xml")]
    require_page_images(
        _VisionProducer(), pages, {"P1": "img-p1", "P2": "img-p2"}
    )  # no raise


# ---------------------------------------------------------------------------
# LLM adapter: BaseProvider -> EditProducer (replace_line re-expression)
# ---------------------------------------------------------------------------


class _FakeProvider:
    async def list_models(self, api_key: str):  # pragma: no cover - unused
        return []

    async def complete_structured(
        self,
        api_key: str,
        model: str,
        system_prompt: str,
        user_payload: dict[str, Any],
        json_schema: dict[str, Any],
        temperature: float = 0.0,
    ) -> tuple[dict[str, Any], Usage | None]:
        self.last_temperature = temperature
        return (
            {
                "lines": [
                    {"line_id": "l1", "corrected_text": "Hello"},
                    {"line_id": "l2", "corrected_text": "World"},
                    {"bad": "entry"},  # skipped
                ]
            },
            Usage(input_tokens=10, output_tokens=5),
        )


def test_llm_adapter_produces_replace_line_script_and_usage():
    import asyncio

    provider = _FakeProvider()
    prod = LLMEditProducer(
        provider, "key", "model", system_prompt="sys", output_schema={}
    )
    assert isinstance(prod, EditProducer)  # structural
    payload = LLMUserPayload(
        granularity=ChunkGranularity.LINE, document_id="d", page_id="pg", lines=[]
    )
    script, usage = asyncio.run(prod.produce(payload, policy=RetryPolicy.default()))
    ops = script.ops
    assert all(isinstance(o, ReplaceLine) for o in ops)
    assert {o.line_id: o.text for o in ops} == {"l1": "Hello", "l2": "World"}
    assert usage == Usage(input_tokens=10, output_tokens=5)
    assert provider.last_temperature == 0.0  # attempt-1 temperature


# ---------------------------------------------------------------------------
# I4 — the library touches no pixel (no image libs anywhere in corrigenda)
# ---------------------------------------------------------------------------


_IMAGE_MODULES = ("PIL", "cv2", "imageio", "skimage", "wand", "pillow", "torchvision")


def test_i4_no_image_libraries_in_corrigenda():
    """Invariant I4 — core AND formats AND bundled producers must never
    import an image-processing library: the lib forwards an opaque image
    ref and leaves every pixel to the (out-of-lib) vision producer."""
    offenders: list[str] = []
    for py in _SRC.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                top = name.split(".")[0]
                if top in _IMAGE_MODULES:
                    offenders.append(f"{py.name}:{node.lineno} imports {name}")
    assert not offenders, f"I4 violation — image lib in corrigenda: {offenders}"
