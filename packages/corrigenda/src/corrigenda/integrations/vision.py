"""Pixel-pure vision toolkit — the deterministic half of ``corrigenda[vision]``.

ROADMAP V3 Phase 4 splits "the vision producer" into two seams because
they have opposite natures:

* **this module** resolves, decodes and *crops* page pixels — pure,
  deterministic, hashable, and testable with a Pillow-drawn fixture and
  **no network, no API key**;
* the forthcoming ``VisionEditProducer`` is the thin, non-deterministic
  half: it hands a crop to a multimodal provider and parses the reply
  into an :class:`~corrigenda.core.editing.EditScript`.

Keeping the cropper standalone means the crop hash (audit criterion 5)
and every geometry decision (XML→pixel transform, EXIF orientation,
margin, PAGE polygon mask) are verified without a VLM in the loop, and a
second producer (another VLM, a rules-on-crop pass) reuses the same
pixels.

Pillow is the ONLY image dependency and it is imported **lazily inside
each function** — importing this module (introspection, the VLM producer
picking it up) never pays the image runtime, and the pixel-blind core
never pulls it (invariant I4, enforced by the static scan in
``tests/test_edit_producer.py`` and the runtime import contract in
``tests/test_import_contract.py``). The core only ever *carries* an
:class:`~corrigenda.core.schemas.ImageAsset`; this module is what decodes
a file to populate one and what turns its geometry into a crop.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from pathlib import Path

from corrigenda.core.schemas import Coords, ImageAsset, ImageTransform

__all__ = ["Crop", "build_image_asset", "crop_region"]

#: EXIF Orientation tag id (0x0112).
_EXIF_ORIENTATION_TAG = 274


@dataclass(frozen=True)
class Crop:
    """One encoded page-region crop plus the provenance a run stamps.

    ``data`` is the encoded image bytes; ``sha256`` is their digest — the
    crop-hash the audit trail records next to the source and image hashes
    (acceptance criterion 5). ``pixel_box`` is the ``(left, top, right,
    bottom)`` actually cropped, in the EXIF-normalized ("visual") pixel
    space the transform maps into — so a caller can reproduce or overlay
    it. The crop is a pure function of (image bytes, frame, transform,
    coords, margin, mask flag): same inputs → identical ``sha256``.
    """

    data: bytes
    media_type: str
    sha256: str
    pixel_box: tuple[int, int, int, int]

    @property
    def width(self) -> int:
        return self.pixel_box[2] - self.pixel_box[0]

    @property
    def height(self) -> int:
        return self.pixel_box[3] - self.pixel_box[1]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _exif_orientation(img: object) -> int | None:
    """The stored EXIF Orientation (1–8), or ``None`` when absent."""
    getexif = getattr(img, "getexif", None)
    if getexif is None:
        return None
    try:
        exif = getexif()
        raw = exif.get(_EXIF_ORIENTATION_TAG)
    except Exception:  # pragma: no cover - malformed EXIF is "no orientation"
        return None
    if raw is None:
        return None
    value = int(raw)
    return value if 1 <= value <= 8 else None


def build_image_asset(
    page_id: str,
    path: str | Path,
    *,
    transform: ImageTransform | None = None,
    frame_index: int = 0,
) -> ImageAsset:
    """Decode ``path`` and return the populated :class:`ImageAsset` the core
    only ever carries — the "builder" promised by the Phase-4 contract.

    Reads the exact file bytes (their SHA-256 is the provenance anchor),
    opens the requested ``frame_index`` (multipage TIFF), and records the
    real decoded MIME type, the EXIF orientation, and the **visual** pixel
    dimensions (after EXIF transpose — the space :attr:`ImageAsset.transform`
    maps XML coordinates into, and the space :func:`crop_region` works in).
    ``transform`` is carried verbatim; pass it when the OCR coordinate space
    is not the image's native resolution.
    """
    from PIL import Image, ImageOps  # lazy — I4

    p = Path(path)
    raw = p.read_bytes()
    with Image.open(io.BytesIO(raw)) as img:
        fmt = img.format
        mime = Image.MIME.get(fmt) if fmt else None
        media_type = str(mime) if mime else None
        n_frames = int(getattr(img, "n_frames", 1))
        if not 0 <= frame_index < n_frames:
            raise ValueError(
                f"frame_index {frame_index} out of range for {p} ({n_frames} frame(s))"
            )
        img.seek(frame_index)
        orientation = _exif_orientation(img)
        visual = ImageOps.exif_transpose(img)
        width, height = visual.size

    return ImageAsset(
        page_id=page_id,
        uri=str(p),
        sha256=_sha256(raw),
        media_type=media_type,
        pixel_width=int(width),
        pixel_height=int(height),
        frame_index=frame_index,
        exif_orientation=orientation,
        transform=transform,
    )


def _xml_bbox_to_pixels(
    coords: Coords, transform: ImageTransform | None
) -> tuple[float, float, float, float]:
    """Map an XML axis-aligned bbox to visual pixels: ``px = scale*xml +
    offset`` per axis (identity when no transform)."""
    t = transform or ImageTransform()
    left = t.scale_x * coords.hpos + t.offset_x
    top = t.scale_y * coords.vpos + t.offset_y
    right = t.scale_x * (coords.hpos + coords.width) + t.offset_x
    bottom = t.scale_y * (coords.vpos + coords.height) + t.offset_y
    return left, top, right, bottom


def _apply_margin(
    box: tuple[float, float, float, float], ratio: float
) -> tuple[float, float, float, float]:
    left, top, right, bottom = box
    mx = (right - left) * ratio
    my = (bottom - top) * ratio
    return left - mx, top - my, right + mx, bottom + my


def _clamp_box(
    box: tuple[float, float, float, float], width: int, height: int
) -> tuple[int, int, int, int]:
    """Round to int and clamp to the image, keeping at least a 1×1 box."""
    left = max(0, min(int(round(box[0])), width - 1))
    top = max(0, min(int(round(box[1])), height - 1))
    right = max(left + 1, min(int(round(box[2])), width))
    bottom = max(top + 1, min(int(round(box[3])), height))
    return left, top, right, bottom


def _polygon_pixels(
    polygon: str, transform: ImageTransform | None, offset: tuple[int, int]
) -> list[tuple[float, float]]:
    """PAGE ``Coords@points`` ("x,y x,y …") mapped to crop-local pixels."""
    t = transform or ImageTransform()
    ox, oy = offset
    points: list[tuple[float, float]] = []
    for token in polygon.split():
        xs, _, ys = token.partition(",")
        px = t.scale_x * float(xs) + t.offset_x - ox
        py = t.scale_y * float(ys) + t.offset_y - oy
        points.append((px, py))
    return points


def crop_region(
    asset: ImageAsset,
    coords: Coords,
    *,
    margin_ratio: float = 0.0,
    mask_polygon: bool = False,
    encode_format: str = "PNG",
) -> Crop:
    """Crop ``coords`` from ``asset``'s image and return an encoded :class:`Crop`.

    Opens ``asset.uri`` at ``asset.frame_index``, normalizes EXIF
    orientation (so pixels match the OCR's visual coordinate space), maps
    the XML bbox to pixels via ``asset.transform``, optionally grows it by
    ``margin_ratio`` on each side (0.1 = +10 %), clamps to the image, and
    re-encodes as ``encode_format`` (PNG = lossless, deterministic bytes).

    ``mask_polygon`` (PAGE only): when the line carries a
    ``coords.polygon``, pixels outside it are made transparent (RGBA), so
    a slanted or multi-column line does not leak its neighbours into the
    crop. A no-op when there is no polygon.

    Pure and deterministic: identical inputs yield an identical
    ``sha256`` — the crop hash the run records for provenance.
    """
    from PIL import Image, ImageDraw, ImageOps  # lazy — I4

    with Image.open(asset.uri) as raw:
        raw.seek(asset.frame_index)
        transposed = ImageOps.exif_transpose(raw)
        use_polygon = mask_polygon and bool(coords.polygon)
        image = transposed.convert("RGBA" if use_polygon else "RGB")
        img_w, img_h = image.size

        box = _apply_margin(_xml_bbox_to_pixels(coords, asset.transform), margin_ratio)
        left, top, right, bottom = _clamp_box(box, int(img_w), int(img_h))
        crop = image.crop((left, top, right, bottom))

        if use_polygon and coords.polygon is not None:
            points = _polygon_pixels(coords.polygon, asset.transform, (left, top))
            mask = Image.new("L", crop.size, 0)
            ImageDraw.Draw(mask).polygon(points, fill=255)
            crop.putalpha(mask)

        buffer = io.BytesIO()
        crop.save(buffer, format=encode_format)
        data = buffer.getvalue()

    return Crop(
        data=data,
        media_type=f"image/{encode_format.lower()}",
        sha256=_sha256(data),
        pixel_box=(left, top, right, bottom),
    )
