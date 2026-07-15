"""Storage helpers: job directories and file I/O."""

from __future__ import annotations

import logging
import os
import shutil
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

_BASE_DIR = Path(os.environ.get("JOB_STORAGE_DIR", "/tmp/app-jobs"))

_ALLOWED_EXTENSIONS = {".xml", ".alto"}
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}
_MAX_ZIP_EXTRACTED_BYTES = 500 * 1024 * 1024  # 500 MB safety limit
_MAX_ZIP_MEMBERS = 1000  # inode-exhaustion limit
_ZIP_READ_CHUNK = 64 * 1024


def _is_extractable(member_path: Path) -> bool:
    """True if a ZIP member will actually be extracted (allowed ALTO/XML
    or image extension, not macOS metadata). Used for both the
    declared-size precheck and the extraction loop so they agree."""
    if member_path.name.startswith("._"):  # AppleDouble
        return False
    if "__MACOSX" in member_path.parts:
        return False
    suffix = member_path.suffix.lower()
    return suffix in _ALLOWED_EXTENSIONS or suffix in _IMAGE_EXTENSIONS


def _safe_zip_read(
    zf: zipfile.ZipFile,
    member: zipfile.ZipInfo,
    remaining_bytes: int,
) -> bytes:
    """Read a ZIP member, aborting if extraction exceeds ``remaining_bytes``.

    Guards against ZIPs that declare a small ``file_size`` in the central
    directory header but contain much larger data in the actual stream
    (a common bomb pattern). Reads in chunks and checks the running total
    against the caller-supplied budget.
    """
    # Audit P3 — accumulate into a single growable buffer instead of a
    # list-of-chunks + b"".join(): the join transiently doubled peak
    # memory (~2x the member size) before the list was freed.
    buf = bytearray()
    with zf.open(member) as src:
        while True:
            chunk = src.read(_ZIP_READ_CHUNK)
            if not chunk:
                break
            if len(buf) + len(chunk) > remaining_bytes:
                raise ValueError(
                    f"ZIP member {member.filename!r} would exceed extraction "
                    f"safety limit ({_MAX_ZIP_EXTRACTED_BYTES} bytes total)"
                )
            buf.extend(chunk)
    return bytes(buf)


# ---------------------------------------------------------------------------
# Directory helpers
# ---------------------------------------------------------------------------


def job_dir(job_id: str) -> Path:
    return _BASE_DIR / job_id


def input_dir(job_id: str) -> Path:
    return job_dir(job_id) / "input"


def output_dir(job_id: str) -> Path:
    return job_dir(job_id) / "output"


def images_dir(job_id: str) -> Path:
    return input_dir(job_id) / "images"


def init_job_dirs(job_id: str) -> None:
    input_dir(job_id).mkdir(parents=True, exist_ok=True)
    output_dir(job_id).mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------


def _register_flat_name(saved: dict[str, Path], seen_stems: dict[str, str], flat_name: str) -> None:
    """P1-9 — refuse silent overwrites: flattening ZIP members (and direct
    uploads) to their basename means ``volume-1/page.xml`` and
    ``volume-2/page.xml`` both become ``page.xml``; the historical
    last-write-wins silently DROPPED the earlier document. Stems must be
    unique too — the output writer names every corrected file
    ``{stem}_corrected.xml``, so ``page.xml`` + ``page.alto`` would
    collide at output time after passing here."""
    if flat_name in saved:
        raise ValueError(
            f"duplicate file name after flattening: {flat_name!r} appears "
            "more than once across the upload (ZIP subdirectories are "
            "flattened). Rename the files so every basename is unique."
        )
    stem = Path(flat_name).stem.lower()
    other = seen_stems.get(stem)
    if other is not None:
        raise ValueError(
            f"conflicting file names: {flat_name!r} and {other!r} share the "
            f"stem {stem!r} — corrected outputs are named "
            "'{stem}_corrected.xml' and would overwrite each other. "
            "Rename one of the files."
        )
    seen_stems[stem] = flat_name


def save_uploaded_files(
    job_id: str,
    files: list[tuple[str, bytes | Path]],
) -> tuple[dict[str, Path], dict[str, Path]]:
    """
    Persist uploaded files to input_dir(job_id).

    Each entry is ``(original_filename, payload)`` where the payload is
    either raw ``bytes`` (legacy/tests) or a ``Path`` to a staged file on
    disk (Plan V2.1 — the API streams uploads to disk in 1 MiB chunks
    instead of buffering up to 200 MiB per request in memory). Path
    payloads are MOVED into place (same filesystem) and ZIPs are opened
    directly from disk.

    Handles ZIP archives: members whose extension is in _ALLOWED_EXTENSIONS
    are extracted with only their basename (no subdirectory structure).
    Image members (JPEG, PNG, TIFF) are saved to images_dir(job_id).

    P0-3 — the decompressed-bytes budget is shared across EVERY archive of
    the job (historically each ZIP got its own 500 MB budget, so a
    30-ZIP request could stage 15 GB). P1-9 — name collisions (flattened
    basenames, output stems, image stems) raise instead of silently
    overwriting an earlier document.

    Returns a tuple of:
    - alto_files: {filename → Path} for every ALTO/XML file saved
    - image_files: {lowercase_stem → Path} for every image file saved
    """
    dest = input_dir(job_id)
    dest.mkdir(parents=True, exist_ok=True)
    saved: dict[str, Path] = {}
    images: dict[str, Path] = {}
    seen_stems: dict[str, str] = {}

    # P0-3 — ONE decompression budget for the whole job, not per archive.
    extracted_total = 0

    for filename, content in files:
        suffix = Path(filename).suffix.lower()

        if suffix == ".zip":
            import io

            zip_source = content if isinstance(content, Path) else io.BytesIO(content)
            with zipfile.ZipFile(zip_source) as zf:
                members = zf.infolist()

                # Member-count guard: prevents inode exhaustion and pathological
                # archives with millions of tiny files.
                if len(members) > _MAX_ZIP_MEMBERS:
                    raise ValueError(
                        f"ZIP archive contains too many members "
                        f"({len(members)}, max {_MAX_ZIP_MEMBERS})"
                    )

                # Declared-size precheck rejects "honest" bombs early
                # without opening any member stream. Audit P2 — count ONLY
                # members that will actually be EXTRACTED: a legitimate
                # upload with a large unrelated member (a 600 MB dataset.csv
                # next to the ALTO files) is skipped at extraction, so
                # counting it here false-rejected the whole archive.
                total_declared = sum(
                    m.file_size for m in members if _is_extractable(Path(m.filename))
                )
                if extracted_total + total_declared > _MAX_ZIP_EXTRACTED_BYTES:
                    raise ValueError(
                        f"ZIP archive declared uncompressed size "
                        f"({total_declared} bytes) exceeds the job's remaining "
                        f"extraction budget "
                        f"({_MAX_ZIP_EXTRACTED_BYTES - extracted_total} bytes)"
                    )

                # Track actual extracted bytes during streaming reads so that
                # lying central-directory entries can't slip a larger payload past.
                for member in members:
                    member_path = Path(member.filename)
                    if not _is_extractable(member_path):
                        continue
                    msuffix = member_path.suffix.lower()

                    data = _safe_zip_read(
                        zf,
                        member,
                        _MAX_ZIP_EXTRACTED_BYTES - extracted_total,
                    )
                    extracted_total += len(data)

                    flat_name = member_path.name
                    if msuffix in _ALLOWED_EXTENSIONS:
                        _register_flat_name(saved, seen_stems, flat_name)
                        out_path = dest / flat_name
                        out_path.write_bytes(data)
                        saved[flat_name] = out_path
                    else:  # image
                        img_key = member_path.stem.lower()
                        if img_key in images:
                            raise ValueError(
                                f"duplicate image name after flattening: two "
                                f"images share the stem {img_key!r}. Rename "
                                "one of them."
                            )
                        imgs = images_dir(job_id)
                        imgs.mkdir(parents=True, exist_ok=True)
                        out_path = imgs / flat_name
                        out_path.write_bytes(data)
                        images[img_key] = out_path
        elif suffix in _ALLOWED_EXTENSIONS:
            flat_name = Path(filename).name
            _register_flat_name(saved, seen_stems, flat_name)
            out_path = dest / flat_name
            if isinstance(content, Path):
                # Staged on disk by the API — same volume, so this is a
                # rename, never a copy of the payload through memory.
                shutil.move(str(content), out_path)
            else:
                out_path.write_bytes(content)
            saved[flat_name] = out_path
        # Silently ignore files with other extensions

    return saved, images


def get_image_files(job_id: str) -> dict[str, Path]:
    """Return {lowercase_stem: Path} for all images in images_dir(job_id)."""
    d = images_dir(job_id)
    if not d.exists():
        return {}
    return {p.stem.lower(): p for p in d.iterdir() if p.suffix.lower() in _IMAGE_EXTENSIONS}


def link_alto_to_images(
    pages: list[tuple[str, str]],
    saved_alto: dict[str, Path],
    saved_images: dict[str, Path],
) -> dict[str, str]:
    """
    Match ALTO source files to images.

    pages: list of (page_id, source_file) pairs from the document manifest.
    saved_alto: {filename → Path} mapping from save_uploaded_files.
    saved_images: {lowercase_stem → Path} mapping from save_uploaded_files.

    Strategy per source file:
    1. Parse sourceImageInformation/fileName from the ALTO XML.
    2. Fall back to matching by lowercase stem of the ALTO source filename.

    Returns {source_file: image_filename}.

    Keying by source_file (not page_id) avoids collisions when multiple ALTO
    files all declare the same Page ID (e.g. ID="Page1"), which is very common
    in per-page scan workflows. The layout endpoint looks up by source_file.
    """
    from lxml import etree

    result: dict[str, str] = {}

    # Deduplicate: each source_file appears once (even if it contains many pages)
    seen_sources: set[str] = set()
    for _page_id, source_file in pages:
        if source_file in seen_sources:
            continue
        seen_sources.add(source_file)

        alto_path = saved_alto.get(source_file)
        if alto_path is None:
            continue

        # Strategy 1: read sourceImageInformation/fileName from ALTO XML
        image_key: str | None = None
        try:
            # Single source of truth for the hardened parser: shared
            # with corrigenda.formats.alto.parser / rewriter — see
            # corrigenda.formats.alto._ns.make_safe_parser docstring.
            from corrigenda.formats.alto._ns import make_safe_parser

            tree = etree.parse(str(alto_path), make_safe_parser())
            for el in tree.findall(".//{*}fileName"):
                fname = (el.text or "").strip()
                if fname:
                    image_key = Path(fname).stem.lower()
                    break
        except Exception:
            # Falls back to the ALTO stem below. Logging the parse
            # failure helps diagnose why an image link is missing for
            # a given source file.
            logger.debug(
                "ALTO image-link parse failed for %s; falling back to filename stem",
                alto_path,
                exc_info=True,
            )

        # Strategy 2: fallback to ALTO filename stem
        if not image_key or image_key not in saved_images:
            image_key = Path(source_file).stem.lower()

        if image_key in saved_images:
            result[source_file] = saved_images[image_key].name

    return result


def get_output_files(job_id: str) -> list[Path]:
    """Return corrected XML files in output_dir(job_id), sorted by name."""
    d = output_dir(job_id)
    if not d.exists():
        return []
    return sorted(p for p in d.iterdir() if p.suffix.lower() in (".xml", ".alto"))


def cleanup_job(job_id: str) -> None:
    """Remove the job directory tree."""
    d = job_dir(job_id)
    if d.exists():
        shutil.rmtree(d)


# Public surface declared explicitly so `from app.storage import *` and
# static analysers don't expose the helper imports (Path, etree, zipfile,
# logging, ...) sitting at module top-level.
__all__ = [
    "cleanup_job",
    "get_image_files",
    "get_output_files",
    "images_dir",
    "init_job_dirs",
    "input_dir",
    "job_dir",
    "link_alto_to_images",
    "output_dir",
    "save_uploaded_files",
]
