"""corrigenda[vision] pixel-pure cropper (ROADMAP V3 Phase 4).

Self-skips when Pillow (the ``[vision]`` extra) is absent — like the qe
suite. Every fixture is drawn by Pillow in-process: no network, no API
key, no checked-in binaries.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

pytest.importorskip("PIL")

from PIL import Image  # noqa: E402

from corrigenda.core.protocols import EditProducer, ProducerOptions  # noqa: E402
from corrigenda.core.schemas import (  # noqa: E402
    ChunkGranularity,
    Coords,
    CorrectionRequest,
    ImageAsset,
    ImageTransform,
    LineContext,
    LineGeometry,
    Usage,
)
from corrigenda.errors import ConfigurationError  # noqa: E402
from corrigenda.integrations.vision import (  # noqa: E402
    Crop,
    ImagePart,
    VisionEditProducer,
    build_image_asset,
    crop_region,
)


def _png(path: Path, size: tuple[int, int], color: tuple[int, int, int]) -> Path:
    Image.new("RGB", size, color).save(path, format="PNG")
    return path


def _multipage_tiff(path: Path) -> Path:
    frames = [
        Image.new("RGB", (100, 100), (255, 0, 0)),
        Image.new("RGB", (200, 80), (0, 255, 0)),
        Image.new("RGB", (40, 40), (0, 0, 255)),
    ]
    frames[0].save(path, format="TIFF", save_all=True, append_images=frames[1:])
    return path


# ---------------------------------------------------------------------------
# build_image_asset — decode a file into the core's carried contract
# ---------------------------------------------------------------------------


def test_build_image_asset_populates_provenance(tmp_path: Path) -> None:
    p = _png(tmp_path / "scan.png", (640, 480), (10, 20, 30))
    asset = build_image_asset("P1", p)
    assert isinstance(asset, ImageAsset)
    assert asset.page_id == "P1" and asset.uri == str(p)
    assert asset.media_type == "image/png"
    assert asset.pixel_width == 640 and asset.pixel_height == 480
    assert asset.frame_index == 0
    # sha256 is of the exact file bytes.
    import hashlib

    assert asset.sha256 == hashlib.sha256(p.read_bytes()).hexdigest()


def test_build_image_asset_reads_the_requested_tiff_frame(tmp_path: Path) -> None:
    p = _multipage_tiff(tmp_path / "multi.tif")
    a0 = build_image_asset("P1", p, frame_index=0)
    a1 = build_image_asset("P1", p, frame_index=1)
    assert (a0.pixel_width, a0.pixel_height) == (100, 100)
    assert (a1.pixel_width, a1.pixel_height) == (200, 80)
    assert a1.media_type == "image/tiff" and a1.frame_index == 1
    # Same file bytes → same sha256 regardless of frame.
    assert a0.sha256 == a1.sha256


def test_build_image_asset_rejects_out_of_range_frame(tmp_path: Path) -> None:
    p = _png(tmp_path / "one.png", (50, 50), (0, 0, 0))
    with pytest.raises(ValueError, match="frame_index"):
        build_image_asset("P1", p, frame_index=3)


# ---------------------------------------------------------------------------
# crop_region — geometry, transform, margin, determinism
# ---------------------------------------------------------------------------


def test_crop_identity_transform_box(tmp_path: Path) -> None:
    p = _png(tmp_path / "s.png", (1000, 800), (200, 100, 50))
    asset = build_image_asset("P1", p)
    coords = Coords(hpos=100, vpos=50, width=300, height=120)
    crop = crop_region(asset, coords)
    assert isinstance(crop, Crop)
    assert crop.pixel_box == (100, 50, 400, 170)
    assert crop.width == 300 and crop.height == 120
    with Image.open(io.BytesIO(crop.data)) as im:
        assert im.size == (300, 120)
    assert crop.media_type == "image/png"


def test_crop_transform_scales_coordinates(tmp_path: Path) -> None:
    """A 2× transform means XML coords map to double the pixels."""
    p = _png(tmp_path / "s.png", (1000, 800), (0, 0, 0))
    asset = build_image_asset(
        "P1", p, transform=ImageTransform(scale_x=2.0, scale_y=2.0)
    )
    coords = Coords(hpos=10, vpos=20, width=100, height=50)
    crop = crop_region(asset, coords)
    assert crop.pixel_box == (20, 40, 220, 140)


def test_crop_margin_expands_box(tmp_path: Path) -> None:
    p = _png(tmp_path / "s.png", (1000, 800), (0, 0, 0))
    asset = build_image_asset("P1", p)
    coords = Coords(hpos=200, vpos=200, width=100, height=100)
    crop = crop_region(asset, coords, margin_ratio=0.1)
    # +10% of 100 = 10px each side.
    assert crop.pixel_box == (190, 190, 310, 310)


def test_crop_clamps_to_image_bounds(tmp_path: Path) -> None:
    p = _png(tmp_path / "s.png", (100, 100), (0, 0, 0))
    asset = build_image_asset("P1", p)
    # Box that runs off the right/bottom edge.
    coords = Coords(hpos=80, vpos=80, width=200, height=200)
    crop = crop_region(asset, coords)
    assert crop.pixel_box == (80, 80, 100, 100)


def test_crop_is_deterministic(tmp_path: Path) -> None:
    p = _png(tmp_path / "s.png", (500, 500), (123, 231, 132))
    asset = build_image_asset("P1", p)
    coords = Coords(hpos=50, vpos=50, width=200, height=100)
    a = crop_region(asset, coords)
    b = crop_region(asset, coords)
    assert a.sha256 == b.sha256 and a.data == b.data


def test_crop_polygon_mask_makes_outside_transparent(tmp_path: Path) -> None:
    p = _png(tmp_path / "s.png", (400, 400), (255, 255, 255))
    asset = build_image_asset("P1", p)
    # A triangle inside the bbox; corners outside it must go transparent.
    coords = Coords(
        hpos=0, vpos=0, width=200, height=200, polygon="100,0 200,200 0,200"
    )
    crop = crop_region(asset, coords, mask_polygon=True)
    with Image.open(io.BytesIO(crop.data)) as im:
        assert im.mode == "RGBA"
        alpha = im.getchannel("A")
        assert alpha.getpixel((0, 0)) == 0  # top-left corner: outside triangle
        assert alpha.getpixel((100, 150)) == 255  # inside triangle


def test_crop_without_polygon_flag_stays_rgb(tmp_path: Path) -> None:
    p = _png(tmp_path / "s.png", (400, 400), (255, 255, 255))
    asset = build_image_asset("P1", p)
    coords = Coords(hpos=0, vpos=0, width=200, height=200, polygon="0,0 200,0 100,200")
    crop = crop_region(asset, coords, mask_polygon=False)
    with Image.open(io.BytesIO(crop.data)) as im:
        assert im.mode == "RGB"


# ---------------------------------------------------------------------------
# VisionEditProducer — crops per line, calls the VLM, parses the reply
# ---------------------------------------------------------------------------

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class _FakeVLM:
    """Records the multimodal call and returns a canned structured reply."""

    def __init__(self, reply: dict) -> None:
        self._reply = reply
        self.seen: dict = {}

    async def complete_structured_multimodal(
        self,
        *,
        api_key,
        model,
        system_prompt,
        user_payload,
        images,
        json_schema,
        temperature=0.0,
    ):
        self.seen = {
            "user_payload": user_payload,
            "images": images,
            "system_prompt": system_prompt,
            "temperature": temperature,
        }
        return self._reply, Usage(input_tokens=3, output_tokens=4)


class _EchoVLM:
    """Returns each target line unchanged — a full-coverage identity reply."""

    async def complete_structured_multimodal(
        self,
        *,
        api_key,
        model,
        system_prompt,
        user_payload,
        images,
        json_schema,
        temperature=0.0,
    ):
        lines = [
            {"line_id": ln["line_id"], "corrected_text": ln["ocr_text"]}
            for ln in user_payload["lines"]
        ]
        return {"lines": lines}, None


def _request_with_geometry(asset: ImageAsset) -> CorrectionRequest:
    return CorrectionRequest(
        granularity=ChunkGranularity.LINE,
        document_id="d",
        page_id="P1",
        lines=[
            LineContext(
                line_id="l1",
                ocr_text="Bonjovr",
                geometry=LineGeometry(
                    coords=Coords(hpos=10, vpos=10, width=200, height=40),
                    page_width=1000,
                    page_height=800,
                ),
            ),
            LineContext(
                line_id="l2",
                ocr_text="mss",
                geometry=LineGeometry(
                    coords=Coords(hpos=10, vpos=60, width=180, height=40),
                    page_width=1000,
                    page_height=800,
                ),
            ),
        ],
        image_ref=asset,
    )


def test_vision_producer_crops_each_line_and_produces_ops(tmp_path: Path) -> None:
    asset = build_image_asset(
        "P1", _png(tmp_path / "pg.png", (1000, 800), (250, 250, 250))
    )
    req = _request_with_geometry(asset)
    vlm = _FakeVLM(
        {
            "lines": [
                {"line_id": "l1", "corrected_text": "Bonjour"},
                {"line_id": "l2", "corrected_text": "mes"},
            ]
        }
    )
    prod = VisionEditProducer(vlm, "key", "vlm-1")
    assert isinstance(prod, EditProducer)  # structural
    assert prod.wants_image and prod.wants_geometry

    import asyncio

    script, usage = asyncio.run(prod.produce(req, options=ProducerOptions()))

    assert {o.line_id: o.text for o in script.ops} == {"l1": "Bonjour", "l2": "mes"}
    # One crop per line, labelled and hashed; every crop is a real PNG.
    parts = vlm.seen["images"]
    assert [p.line_id for p in parts] == ["l1", "l2"]
    assert all(isinstance(p, ImagePart) and len(p.sha256) == 64 for p in parts)
    assert all(p.data.startswith(_PNG_MAGIC) for p in parts)
    # The crop hash is exactly what crop_region computes (margin default 0.05).
    expected = crop_region(
        asset, req.lines[0].geometry.coords, margin_ratio=0.05
    ).sha256
    assert parts[0].sha256 == expected
    # The ImageAsset is sent as image parts, never inlined in the text JSON.
    assert "image_ref" not in vlm.seen["user_payload"]
    assert usage == Usage(input_tokens=3, output_tokens=4)


def test_vision_producer_rejects_bare_image_ref() -> None:
    req = CorrectionRequest(
        granularity=ChunkGranularity.LINE,
        document_id="d",
        page_id="P1",
        lines=[],
        image_ref="opaque://p1",  # a bare ImageRef cannot be cropped
    )
    prod = VisionEditProducer(_FakeVLM({"lines": []}), "k", "m")

    import asyncio

    with pytest.raises(ConfigurationError, match="ImageAsset"):
        asyncio.run(prod.produce(req, options=ProducerOptions()))


def test_vision_producer_drives_the_full_pipeline(tmp_path: Path) -> None:
    """End to end: the pipeline copies the ImageAsset + geometry into the
    §4.1 envelope, the producer crops real ALTO geometry and the run
    completes. An echo VLM keeps it deterministic (identity corrections)."""
    from corrigenda import CorrectionPipeline
    from corrigenda.formats.alto.parser import build_document_manifest

    sample = Path(__file__).parent.parent.parent.parent / "examples" / "sample.xml"
    doc = build_document_manifest([(sample, sample.name)])
    assets = {}
    for page in doc.pages:
        img = _png(
            tmp_path / f"{page.page_id}.png",
            (page.page_width, page.page_height),
            (255, 255, 255),
        )
        assets[page.page_id] = build_image_asset(page.page_id, img)

    class _Null:
        def on_event(self, *a, **k):
            pass

    prod = VisionEditProducer(_EchoVLM(), "key", "vlm-1")
    pipeline = CorrectionPipeline(producer=prod, observer=_Null())

    import asyncio

    result = asyncio.run(
        pipeline.run(
            document_manifest=doc,
            source_files={sample.name: sample},
            page_images=assets,
        )
    )
    # Identity reply → no line degraded, run succeeds through the vision seam.
    assert result.fallback_chunks == 0
    assert result.producer_calls >= 1
