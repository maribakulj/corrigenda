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

from corrigenda.core.schemas import Coords, ImageAsset, ImageTransform  # noqa: E402
from corrigenda.integrations.vision import (  # noqa: E402
    Crop,
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
