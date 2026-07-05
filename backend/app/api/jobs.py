"""Jobs API router."""

from __future__ import annotations

import json
import os
import tempfile
import zipfile
from collections.abc import AsyncGenerator
from pathlib import Path

from corrigenda.alto.parser import build_document_manifest
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from sse_starlette.sse import EventSourceResponse
from starlette.background import BackgroundTask

from app.api.deps import get_job_store
from app.api.rate_limit import limiter
from app.jobs import runner as _runner_module
from app.jobs.runner import JobRunner
from app.protocols import JobStore
from app.schemas import (
    CreateJobResponse,
    HyphenRole,
    JobManifest,
    JobStatus,
    JobStatusResponse,
    Provider,
)
from app.storage import (
    get_output_files,
    images_dir,
    init_job_dirs,
    link_alto_to_images,
    output_dir,
    save_uploaded_files,
)
from app.storage.output_writer import FilesystemOutputWriter

router = APIRouter()

_ALLOWED_UPLOAD_EXTENSIONS = {".xml", ".alto", ".zip"}

# L10/B5 — per-file upload cap (inclusive). `await UploadFile.read()`
# loads the full body into memory; without a cap a 100 GB upload would
# OOM the single-worker process before any decoding. 100 MB is generous
# for ALTO files (single-page is typically <1 MB; ZIPs with embedded
# page scans can reach tens of MB) while bounding worst-case allocation.
# Looked up dynamically inside `create_job` so tests can monkey-patch
# the constant without re-importing the module.
_MAX_UPLOAD_FILE_BYTES = 100 * 1024 * 1024  # 100 MiB


# ---------------------------------------------------------------------------
# Shared dependency for endpoints that require a completed job with a manifest
# ---------------------------------------------------------------------------


def get_completed_job(
    job_id: str,
    store: JobStore = Depends(get_job_store),
) -> JobManifest:
    """FastAPI dependency: resolve job_id → JobManifest or raise 4xx."""
    job = store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id!r}")
    if job.status != JobStatus.COMPLETED:
        raise HTTPException(
            status_code=400,
            detail=f"Job is not completed yet (status: {job.status.value})",
        )
    if job.document_manifest is None:
        raise HTTPException(status_code=404, detail="No document manifest available.")
    return job


# ---------------------------------------------------------------------------
# POST /api/jobs
# ---------------------------------------------------------------------------


@router.post("", response_model=CreateJobResponse)
# Rate limited to throttle file uploads + spawned background tasks
# against a single-worker server with bounded disk/CPU budget.
@limiter.limit("20/minute")
async def create_job(
    request: Request,
    files: list[UploadFile] = File(...),
    provider: str = Form(...),
    api_key: str = Form(...),
    model: str = Form(...),
    store: JobStore = Depends(get_job_store),
) -> CreateJobResponse:
    """Upload ALTO files and start a correction job."""
    # Validate upload extensions
    for f in files:
        suffix = Path(f.filename or "").suffix.lower()
        if suffix not in _ALLOWED_UPLOAD_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported file type: {f.filename!r}. "
                f"Allowed: {sorted(_ALLOWED_UPLOAD_EXTENSIONS)}",
            )

    # Validate provider
    try:
        provider_enum = Provider(provider)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {provider!r}") from exc

    # Read all file bytes. Bounded by `_MAX_UPLOAD_FILE_BYTES` per file
    # so a 100 GB upload yields a fast 413 instead of OOMing the
    # process. We read `cap + 1` bytes and reject if the result is
    # longer than `cap` (i.e. there was at least one more byte to read).
    cap = _MAX_UPLOAD_FILE_BYTES
    file_tuples: list[tuple[str, bytes]] = []
    for f in files:
        content = await f.read(cap + 1)
        if len(content) > cap:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"Uploaded file {f.filename!r} exceeds the per-file "
                    f"limit ({cap} bytes). Split the upload or reduce its size."
                ),
            )
        file_tuples.append((f.filename or "upload.xml", content))

    # Create job and dirs
    job_id = store.create_job(provider_enum, model)
    init_job_dirs(job_id)

    # Save and extract files (also extracts images from ZIPs)
    saved, image_files = save_uploaded_files(job_id, file_tuples)

    if not saved:
        raise HTTPException(
            status_code=400,
            detail="No ALTO/XML files found after extraction.",
        )

    # Build document manifest
    file_pairs = [(path, name) for name, path in saved.items()]
    try:
        doc_manifest = build_document_manifest(file_pairs)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to parse files: {exc}") from exc

    if doc_manifest.total_lines == 0:
        raise HTTPException(
            status_code=400,
            detail="No text lines found in the uploaded ALTO files.",
        )

    pages_info = [(p.page_id, p.source_file) for p in doc_manifest.pages]
    images_map = link_alto_to_images(pages_info, saved, image_files)
    store.update_job(job_id, document_manifest=doc_manifest, images=images_map)

    # Resolve provider instance
    from app.providers import get_provider as _get_provider

    provider_instance = _get_provider(provider_enum)

    out_dir = output_dir(job_id)

    runner = JobRunner(job_store=store)
    output_writer_instance = FilesystemOutputWriter(out_dir)

    # Spawn correction through the per-app registry so the task is
    # strongly referenced (prevents GC mid-run) AND so the lifespan
    # handler can drain it on SIGTERM. Crash logging is centralised
    # in BackgroundTaskRegistry._on_done.
    request.app.state.tasks.spawn(
        runner.run(
            job_id=job_id,
            document_manifest=doc_manifest,
            provider_name=provider,
            api_key=api_key,
            model=model,
            output_writer=output_writer_instance,
            source_files={name: path for name, path in saved.items()},
            provider=provider_instance,
            # Lookup is dynamic (not a snapshot) so tests that
            # `monkeypatch.setattr("app.jobs.runner.DEFAULT_JOB_TIMEOUT_SECONDS", N)`
            # actually see the override at spawn time.
            timeout_seconds=_runner_module.DEFAULT_JOB_TIMEOUT_SECONDS,
        ),
        name=f"run_job:{job_id}",
    )

    return CreateJobResponse(job_id=job_id)


# ---------------------------------------------------------------------------
# GET /api/jobs/{job_id}
# ---------------------------------------------------------------------------


@router.get("/{job_id}", response_model=JobStatusResponse)
async def get_job(
    job_id: str,
    store: JobStore = Depends(get_job_store),
) -> JobStatusResponse:
    """Poll the status of a correction job."""
    job = store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id!r}")

    return JobStatusResponse(
        job_id=job.job_id,
        status=job.status,
        total_lines=job.total_lines,
        lines_modified=job.lines_modified,
        chunks_total=job.chunks_total,
        retries=job.retries,
        fallbacks=job.fallbacks,
        duration_seconds=job.duration_seconds,
        error=job.error,
    )


# ---------------------------------------------------------------------------
# GET /api/jobs/{job_id}/events
# ---------------------------------------------------------------------------


@router.get("/{job_id}/events")
async def job_events(
    job_id: str,
    store: JobStore = Depends(get_job_store),
) -> EventSourceResponse:
    """SSE stream of correction job events."""
    if store.get_job(job_id) is None:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id!r}")

    async def generator() -> AsyncGenerator[dict, None]:
        async for sse_event in store.stream_events(job_id):
            yield {
                "event": sse_event.event,
                "data": json.dumps(sse_event.data),
            }

    return EventSourceResponse(generator())


# ---------------------------------------------------------------------------
# GET /api/jobs/{job_id}/download
# ---------------------------------------------------------------------------


@router.get("/{job_id}/download")
async def download_job(
    job_id: str,
    store: JobStore = Depends(get_job_store),
) -> Response:
    """Download corrected XML file(s)."""
    if store.get_job(job_id) is None:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id!r}")

    out_files = get_output_files(job_id)
    if not out_files:
        raise HTTPException(
            status_code=404,
            detail="Output not ready yet. Wait for job to complete.",
        )

    if len(out_files) == 1:
        # FileResponse streams the file in 64 KB chunks instead of
        # holding the full body in memory (the old `xml_path.read_bytes()`
        # buffered up to 500 MB per request × N concurrent downloads).
        xml_path = out_files[0]
        return FileResponse(
            xml_path,
            media_type="application/xml",
            filename=xml_path.name,
        )

    # L10/F8 — multi-file: build the ZIP on disk in a NamedTemporaryFile,
    # then FileResponse streams it back. Previously we materialised the
    # whole archive in `io.BytesIO()` and shipped `.getvalue()` as a
    # bytes blob; on a 500 MB job × a handful of concurrent downloads
    # that's multi-GB resident memory. The tempfile is cleaned up via
    # a BackgroundTask that fires AFTER the response is fully sent.
    zip_name = f"job_{job_id}_corrected.zip"
    with tempfile.NamedTemporaryFile(suffix=".zip", prefix="alto_dl_", delete=False) as tmp:
        tmp_path = tmp.name
    # The `with` block closed the file handle but `delete=False` means
    # the file persists on disk for `FileResponse` to read.
    try:
        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for p in out_files:
                zf.write(p, arcname=p.name)
    except Exception:
        # If we crash building the ZIP, the BackgroundTask hasn't been
        # attached yet — clean up by hand so we don't leak the tempfile.
        os.unlink(tmp_path)
        raise

    return FileResponse(
        tmp_path,
        media_type="application/zip",
        filename=zip_name,
        background=BackgroundTask(os.unlink, tmp_path),
    )


# ---------------------------------------------------------------------------
# GET /api/jobs/{job_id}/trace
# ---------------------------------------------------------------------------


@router.get("/{job_id}/trace")
async def get_job_trace(job: JobManifest = Depends(get_completed_job)) -> dict:
    """Return per-line text traces for a completed job."""
    if not job.line_traces:
        raise HTTPException(status_code=404, detail="No traces available for this job.")

    return {
        "job_id": job.job_id,
        "total_lines": len(job.line_traces),
        "lines": [t.model_dump(exclude_none=True) for t in job.line_traces.values()],
    }


# ---------------------------------------------------------------------------
# GET /api/jobs/{job_id}/diff
# ---------------------------------------------------------------------------


@router.get("/{job_id}/diff")
async def get_job_diff(job: JobManifest = Depends(get_completed_job)) -> dict:
    """Return per-line OCR vs corrected diff data for a completed job."""
    pages_out = []
    total_lines = 0
    modified_lines = 0
    hyphen_pairs = 0

    # get_completed_job already 404s if document_manifest is None, but
    # an `assert` here would disappear under `python -O` (bandit B101).
    # Keep a real runtime guard instead so the contract holds in any
    # interpreter mode.
    if job.document_manifest is None:
        raise HTTPException(status_code=500, detail="Job has no document_manifest.")
    for page in job.document_manifest.pages:
        lines_out = []
        for lm in page.lines:
            corrected = lm.corrected_text if lm.corrected_text is not None else lm.ocr_text
            modified = corrected != lm.ocr_text
            lines_out.append(
                {
                    "line_id": lm.line_id,
                    "ocr_text": lm.ocr_text,
                    "corrected_text": corrected,
                    "modified": modified,
                    "hyphen_role": lm.hyphen_role.value,
                    "hyphen_subs_content": lm.hyphen_subs_content,
                }
            )
            total_lines += 1
            if modified:
                modified_lines += 1
            if lm.hyphen_role == HyphenRole.PART1:
                hyphen_pairs += 1

        pages_out.append(
            {
                "page_id": page.page_id,
                "page_index": page.page_index,
                "lines": lines_out,
            }
        )

    return {
        "job_id": job.job_id,
        "pages": pages_out,
        "stats": {
            "total_lines": total_lines,
            "modified_lines": modified_lines,
            "hyphen_pairs": hyphen_pairs,
        },
    }


# ---------------------------------------------------------------------------
# GET /api/jobs/{job_id}/layout
# ---------------------------------------------------------------------------


@router.get("/{job_id}/layout")
async def get_job_layout(job: JobManifest = Depends(get_completed_job)) -> dict:
    """Return structural layout data (blocks + lines with ALTO coordinates)."""
    pages_out = []
    if job.document_manifest is None:
        raise HTTPException(status_code=500, detail="Job has no document_manifest.")
    for page in job.document_manifest.pages:
        line_by_id = {lm.line_id: lm for lm in page.lines}

        blocks_out = []
        for block in page.blocks:
            lines_out = []
            for line_id in block.line_ids:
                lm = line_by_id.get(line_id)
                if lm is None:
                    continue
                corrected = lm.corrected_text if lm.corrected_text is not None else lm.ocr_text
                lines_out.append(
                    {
                        "line_id": lm.line_id,
                        "hpos": lm.coords.hpos,
                        "vpos": lm.coords.vpos,
                        "width": lm.coords.width,
                        "height": lm.coords.height,
                        "ocr_text": lm.ocr_text,
                        "corrected_text": corrected,
                        "modified": corrected != lm.ocr_text,
                        "hyphen_role": lm.hyphen_role.value,
                    }
                )
            blocks_out.append(
                {
                    "block_id": block.block_id,
                    "hpos": block.coords.hpos,
                    "vpos": block.coords.vpos,
                    "width": block.coords.width,
                    "height": block.coords.height,
                    "lines": lines_out,
                }
            )

        # Derive page dimensions from line coordinates if the ALTO Page element
        # doesn't carry WIDTH/HEIGHT (some producers omit these attributes).
        pw = page.page_width
        ph = page.page_height
        if pw == 0 or ph == 0:
            xs = [lm.coords.hpos + lm.coords.width for lm in page.lines]
            ys = [lm.coords.vpos + lm.coords.height for lm in page.lines]
            if pw == 0 and xs:
                pw = max(xs)
            if ph == 0 and ys:
                ph = max(ys)

        # images map is keyed by source_file (not page_id) to avoid collisions
        # when multiple ALTO files share the same Page/@ID value.
        image_filename = job.images.get(page.source_file)
        image_url = f"/api/jobs/{job.job_id}/images/{image_filename}" if image_filename else None
        pages_out.append(
            {
                "page_id": page.page_id,
                "page_index": page.page_index,
                "page_width": pw,
                "page_height": ph,
                "image_url": image_url,
                "blocks": blocks_out,
            }
        )

    return {"job_id": job.job_id, "pages": pages_out}


# ---------------------------------------------------------------------------
# GET /api/jobs/{job_id}/images/{image_name}
# ---------------------------------------------------------------------------

_IMAGE_MIME: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}


@router.get("/{job_id}/images/{image_name}")
async def get_job_image(
    job_id: str,
    image_name: str,
    store: JobStore = Depends(get_job_store),
) -> Response:
    """Serve a source scan image for a job."""
    if store.get_job(job_id) is None:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id!r}")

    # Sanitise: only allow plain filenames (no path traversal)
    if "/" in image_name or "\\" in image_name or image_name.startswith("."):
        raise HTTPException(status_code=400, detail="Invalid image name.")

    img_path = (images_dir(job_id) / image_name).resolve()
    allowed_dir = images_dir(job_id).resolve()
    if not img_path.is_relative_to(allowed_dir):
        raise HTTPException(status_code=400, detail="Invalid image name.")
    if not img_path.is_file() or img_path.is_symlink():
        raise HTTPException(status_code=404, detail=f"Image not found: {image_name!r}")

    mime = _IMAGE_MIME.get(img_path.suffix.lower(), "application/octet-stream")
    return Response(content=img_path.read_bytes(), media_type=mime)
