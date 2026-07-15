"""Jobs API router."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import shutil
import tempfile
import zipfile
from collections.abc import AsyncGenerator
from pathlib import Path

from corrigenda.core.schemas import PairingPolicy
from corrigenda.formats.alto.parser import build_document_manifest
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from sse_starlette.sse import EventSourceResponse
from starlette.background import BackgroundTask

from app.api.deps import get_job_store
from app.api.rate_limit import limiter
from app.api.read_models import build_diff, build_layout
from app.jobs import runner as _runner_module
from app.jobs.runner import JobRunner
from app.protocols import JobStore
from app.schemas import (
    TERMINAL_SUCCESS_STATES,
    CreateJobResponse,
    JobManifest,
    JobStatus,
    JobStatusResponse,
    Provider,
)
from app.storage import (
    get_output_files,
    images_dir,
    init_job_dirs,
    job_dir,
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

# P0-3 — the per-file cap alone was bypassable by cardinality: 30 files
# of 100 MiB each stayed under it while pinning ~3 GiB in the process.
# Both the file count and the WHOLE request's cumulative bytes are now
# bounded (checked incrementally, so an oversized request 413s as soon
# as the running total crosses the cap — not after buffering it all).
_MAX_UPLOAD_FILES = 100
_MAX_TOTAL_UPLOAD_BYTES = 200 * 1024 * 1024  # 200 MiB per request

# P1-5 — admission control. The 20/min rate limit throttles REQUESTS,
# not concurrency: with jobs allowed to run up to 1800 s, unbounded
# admissions stack dozens of concurrent pipelines (and their provider
# spend). active_count existed but gated nothing. Refusals are explicit
# 503s with Retry-After — an overload policy, not a silent queue.
_MAX_ACTIVE_JOBS = int(os.environ.get("MAX_ACTIVE_JOBS", "4"))

# Plan V2.1 — upload-phase reservation, SEPARATE from the running-jobs
# cap: the job cap only bounds spawned pipelines, so N concurrent
# requests could all buffer their payload before the authoritative
# check let 4 spawn. This counter is reserved at handler entry (before
# any body read) and released when the handler exits. Repeat the limit
# at the reverse proxy in institutional deployments — this guard is the
# app's own last line, not the only one.
_MAX_CONCURRENT_UPLOADS = int(
    os.environ.get("MAX_CONCURRENT_UPLOADS", str(_MAX_ACTIVE_JOBS))
)

# Plan V2.1 — uploads stream to disk in chunks this size; peak RAM per
# request drops from the 200 MiB request cap to one chunk.
_UPLOAD_CHUNK_BYTES = 1024 * 1024


# ---------------------------------------------------------------------------
# Shared dependencies: capability-token access + completed-job resolution
# ---------------------------------------------------------------------------


def _token_matches(job: JobManifest, presented: str | None) -> bool:
    if not presented:
        return False
    digest = hashlib.sha256(presented.encode("utf-8")).hexdigest()
    return secrets.compare_digest(digest, job.token_hash or "")


def require_job_access(
    job_id: str,
    request: Request,
    store: JobStore = Depends(get_job_store),
) -> JobManifest:
    """P1-7 — capability-token gate on every job endpoint.

    The job_id used to be the ONLY secret — a UUID that leaks into
    operator logs, browser history and referrers gave full read access
    to a stranger's OCR text, corrections and images. Every job created
    through the public API now carries a token (only its SHA-256 hash is
    stored); callers present it via the ``X-Job-Token`` header, or via
    ``?token=`` for the surfaces that cannot set headers (EventSource,
    <img>, download links). Jobs created OUTSIDE the HTTP layer (direct
    store access — tests, embedding consumers) have no hash and are not
    gated. Missing/wrong token → 404, not 403: an unauthenticated caller
    must not be able to distinguish "job exists" from "job doesn't".

    Deployment model (decided): the app runs behind the institution's
    SSO/reverse-proxy for authentication; this token provides per-job
    isolation BETWEEN authenticated users, not user authentication.
    """
    job = store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id!r}")
    if job.token_hash is not None:
        presented = request.headers.get("x-job-token") or request.query_params.get("token")
        if not _token_matches(job, presented):
            raise HTTPException(status_code=404, detail=f"Job not found: {job_id!r}")
    return job


def get_completed_job(
    job: JobManifest = Depends(require_job_access),
) -> JobManifest:
    """FastAPI dependency: access-checked job in a terminal-success state."""
    # P0-1 — both terminal success states expose their (valid) outputs;
    # COMPLETED_WITH_FALLBACKS is degraded but downloadable by design.
    if job.status not in TERMINAL_SUCCESS_STATES:
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


async def _stream_uploads_to_staging(
    files: list[UploadFile], staging: Path
) -> list[tuple[str, Path]]:
    """Plan V2.1 — copy each upload to disk in bounded chunks.

    Enforces the per-file and cumulative caps INCREMENTALLY (the request
    413s as soon as the running total crosses a cap, not after buffering
    everything). Staged names are index-prefixed so duplicate upload
    names can't collide before ``save_uploaded_files`` applies its own
    collision policy. Returns ``(original_filename, staged_path)`` pairs.
    """
    cap = _MAX_UPLOAD_FILE_BYTES
    total_cap = _MAX_TOTAL_UPLOAD_BYTES
    total_bytes = 0
    staged: list[tuple[str, Path]] = []
    for idx, f in enumerate(files):
        original = f.filename or "upload.xml"
        target = staging / f"{idx:04d}_{Path(original).name}"
        file_bytes = 0
        with target.open("wb") as out:
            while True:
                chunk = await f.read(_UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                file_bytes += len(chunk)
                if file_bytes > cap:
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"Uploaded file {original!r} exceeds the per-file "
                            f"limit ({cap} bytes). Split the upload or reduce its size."
                        ),
                    )
                # P0-3 — cumulative bound: reject as soon as the running
                # total crosses the request cap.
                total_bytes += len(chunk)
                if total_bytes > total_cap:
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"Upload exceeds the total request limit "
                            f"({total_cap} bytes). Split the job into smaller batches."
                        ),
                    )
                out.write(chunk)
        staged.append((original, target))
    return staged


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
    # P1-2 opt-out — the default PairingPolicy vets heuristic hyphen
    # pairs geometrically; exotic layouts can restore the historical
    # purely-sequential pairing without forking the deployment.
    geometric_pairing: bool = Form(True),
    store: JobStore = Depends(get_job_store),
) -> CreateJobResponse:
    """Upload ALTO files and start a correction job."""
    # Plan V2.1 — reserve an upload slot BEFORE touching any body byte.
    # The job cap below only bounds spawned pipelines; without this
    # reservation N concurrent requests could all stage their payload
    # while active_count was still low. Check-and-increment is atomic on
    # the single-threaded event loop (no await between them).
    app_state = request.app.state
    if app_state.uploads_in_progress >= _MAX_CONCURRENT_UPLOADS:
        raise HTTPException(
            status_code=503,
            detail=(
                f"Server is at upload capacity ({_MAX_CONCURRENT_UPLOADS} "
                "concurrent uploads). Retry shortly."
            ),
            headers={"Retry-After": "10"},
        )
    app_state.uploads_in_progress += 1
    try:
        return await _create_job_reserved(
            request=request,
            files=files,
            provider=provider,
            api_key=api_key,
            model=model,
            geometric_pairing=geometric_pairing,
            store=store,
        )
    finally:
        app_state.uploads_in_progress -= 1


async def _create_job_reserved(
    *,
    request: Request,
    files: list[UploadFile],
    provider: str,
    api_key: str,
    model: str,
    geometric_pairing: bool,
    store: JobStore,
) -> CreateJobResponse:
    """The body of ``create_job``, running inside an upload reservation."""
    # P1-5 — admission control before reading a single byte: the task
    # registry's live count is the source of truth for running pipelines.
    if request.app.state.tasks.active_count >= _MAX_ACTIVE_JOBS:
        raise HTTPException(
            status_code=503,
            detail=(f"Server is at capacity ({_MAX_ACTIVE_JOBS} concurrent jobs). Retry shortly."),
            headers={"Retry-After": "30"},
        )
    # P0-3 — cardinality bound before reading a single byte.
    if len(files) > _MAX_UPLOAD_FILES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"Too many files ({len(files)}, max {_MAX_UPLOAD_FILES}). "
                "Group them into a ZIP archive or split the job."
            ),
        )
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

    # Create job and dirs. P1-10 — everything from here to the spawn is
    # TRANSACTIONAL: the job record and its directories exist before
    # extraction/parsing/validation, so any failure in that window must
    # roll both back. Historically a parse failure left a QUEUED job with
    # files on disk forever (never terminal -> never TTL-evicted).
    # Audit-F21 — store.create_job runs an opportunistic eviction whose
    # shutil.rmtree would otherwise block the event loop; offload it (the
    # store is thread-safe via its RLock).
    job_id = await asyncio.to_thread(store.create_job, provider_enum, model)
    # P1-7 — capability token: shown once in the response; only its hash
    # is stored. Every job endpoint requires it from now on.
    job_token = secrets.token_urlsafe(32)
    store.update_job(job_id, token_hash=hashlib.sha256(job_token.encode("utf-8")).hexdigest())
    try:
        init_job_dirs(job_id)

        # Plan V2.1 — stream the uploads to a staging dir under the job
        # in 1 MiB chunks (per-file and cumulative caps enforced
        # incrementally): peak RAM per request is one chunk, not the
        # 200 MiB request cap. The staging dir lives inside the job dir
        # so the transactional rollback below reclaims it too.
        staging = job_dir(job_id) / "upload-staging"
        staging.mkdir(parents=True, exist_ok=True)
        staged_files = await _stream_uploads_to_staging(files, staging)

        # Save and extract files (also extracts images from ZIPs).
        # ValueError = bounded/refused input (zip bomb, name collision,
        # too many members) — a client error, not a server fault (P1-9).
        # Audit-F19 — ZIP decompression + per-member disk writes are
        # synchronous and CPU/IO-bound; offload so a large upload can't
        # freeze the single-worker event loop (SSE keepalives, health
        # probes, in-flight downloads).
        try:
            saved, image_files = await asyncio.to_thread(save_uploaded_files, job_id, staged_files)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            # XML files were MOVED out of staging; only ZIP payloads (and
            # rejected leftovers) remain — reclaim them now, not at TTL.
            await asyncio.shield(
                asyncio.to_thread(shutil.rmtree, staging, ignore_errors=True)
            )

        if not saved:
            raise HTTPException(
                status_code=400,
                detail="No ALTO/XML files found after extraction.",
            )

        # Build document manifest
        pairing_policy = PairingPolicy(geometric_checks=geometric_pairing)
        file_pairs = [(path, name) for name, path in saved.items()]
        try:
            # Audit-F19 — lxml parse + manifest build over up to 200 MiB of
            # XML is synchronous and CPU-bound; offload off the event loop.
            doc_manifest = await asyncio.to_thread(
                build_document_manifest, file_pairs, pairing_policy=pairing_policy
            )
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

        # Audit P2 — the early admission check (before reading uploads) is
        # only fail-fast: the handler awaits file reads afterwards, so N
        # concurrent uploads could all pass it while active_count was
        # still low, then all spawn. This AUTHORITATIVE re-check sits
        # immediately before the synchronous spawn() with NO await
        # between them, so in single-worker asyncio the check-and-spawn is
        # atomic (no yield point) and the cap is never exceeded.
        if request.app.state.tasks.active_count >= _MAX_ACTIVE_JOBS:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"Server is at capacity ({_MAX_ACTIVE_JOBS} concurrent jobs). Retry shortly."
                ),
                headers={"Retry-After": "30"},
            )

        # Plan V2.2 — register the cancellation event BEFORE the task
        # exists so POST /cancel can never race an unregistered run; the
        # probe (event.is_set) is polled by the pipeline between pages
        # and chunks.
        app_state = request.app.state
        cancel_event = app_state.cancellations.register(job_id)

        async def _run_job() -> None:
            try:
                await runner.run(
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
                    should_abort=cancel_event.is_set,
                )
            finally:
                # The run settled (any terminal state): the event has no
                # further reader — drop it so the registry never leaks.
                app_state.cancellations.discard(job_id)

        # Spawn correction through the per-app registry so the task is
        # strongly referenced (prevents GC mid-run) AND so the lifespan
        # handler can drain it on SIGTERM. Crash logging is centralised
        # in BackgroundTaskRegistry._on_done.
        request.app.state.tasks.spawn(_run_job(), name=f"run_job:{job_id}")
    except BaseException:
        # P1-10 — rollback: no half-created QUEUED job may survive a
        # failed creation (they are never terminal, hence never evicted).
        # Wave-3 review — the rmtree inside delete_job can span a
        # multi-hundred-MB extraction: offloaded like every other heavy
        # call in this handler, and SHIELDED so a client abort
        # (CancelledError can land right here) can't skip the cleanup —
        # the thread runs to completion even if the await is cancelled.
        request.app.state.cancellations.discard(job_id)
        await asyncio.shield(asyncio.to_thread(store.delete_job, job_id))
        raise

    return CreateJobResponse(job_id=job_id, job_token=job_token)


# ---------------------------------------------------------------------------
# GET /api/jobs/{job_id}
# ---------------------------------------------------------------------------


@router.get("/{job_id}", response_model=JobStatusResponse)
async def get_job(
    job: JobManifest = Depends(require_job_access),
) -> JobStatusResponse:
    """Poll the status of a correction job."""
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
# POST /api/jobs/{job_id}/cancel
# ---------------------------------------------------------------------------


#: States after which a cancel request is a no-op (job already settled).
_SETTLED_STATES = frozenset(
    {
        JobStatus.COMPLETED,
        JobStatus.COMPLETED_WITH_FALLBACKS,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
    }
)


@router.post("/{job_id}/cancel", response_model=JobStatusResponse, status_code=202)
async def cancel_job(
    request: Request,
    job: JobManifest = Depends(require_job_access),
    store: JobStore = Depends(get_job_store),
) -> JobStatusResponse:
    """Plan V2.2 — request cooperative cancellation. Idempotent.

    Sets the job's cancellation event; the pipeline's ``should_abort``
    probe (polled between pages and chunks) trips on the next check and
    the runner lands the job in CANCELLED with no output promoted. A
    request on an already-settled job acknowledges without effect —
    the response body always carries the CURRENT status.
    """
    if job.status not in _SETTLED_STATES:
        requested = request.app.state.cancellations.request(job.job_id)
        if requested and job.status != JobStatus.CANCEL_REQUESTED:
            store.update_job(job.job_id, status=JobStatus.CANCEL_REQUESTED)

    fresh = store.get_job(job.job_id) or job
    return JobStatusResponse(
        job_id=fresh.job_id,
        status=fresh.status,
        total_lines=fresh.total_lines,
        lines_modified=fresh.lines_modified,
        chunks_total=fresh.chunks_total,
        retries=fresh.retries,
        fallbacks=fresh.fallbacks,
        duration_seconds=fresh.duration_seconds,
        error=fresh.error,
    )


# ---------------------------------------------------------------------------
# GET /api/jobs/{job_id}/events
# ---------------------------------------------------------------------------


@router.get("/{job_id}/events")
async def job_events(
    job_id: str,
    store: JobStore = Depends(get_job_store),
    _job: JobManifest = Depends(require_job_access),
) -> EventSourceResponse:
    """SSE stream of correction job events. P1-7 — EventSource cannot set
    headers, so the capability token arrives as ``?token=``."""

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


def _build_zip_archive(tmp_path: str, out_files: list[Path]) -> None:
    """Write a DEFLATE ZIP of ``out_files`` to ``tmp_path`` (Audit-F19).

    Extracted so the CPU-bound compression can run under
    ``asyncio.to_thread`` off the event loop.
    """
    with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in out_files:
            zf.write(p, arcname=p.name)


@router.get("/{job_id}/download")
async def download_job(
    job_id: str,
    job: JobManifest = Depends(require_job_access),
) -> Response:
    """Download corrected XML file(s)."""
    # P0-4 — outputs are only served for a terminal-success job. The
    # writer stages files and only commits on success, but this guard is
    # the contract: a FAILED/RUNNING job's /download can never return a
    # partial or stale set, whatever is on disk.
    if job.status not in TERMINAL_SUCCESS_STATES:
        raise HTTPException(
            status_code=409,
            detail=(f"Job is not in a downloadable state (status: {job.status.value})."),
        )

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
        # Audit-F19 — DEFLATE compression of a multi-file job (up to the
        # 500 MB extraction budget) is CPU-bound; running it inline on the
        # async handler froze every other coroutine on the single-worker
        # loop for the whole build. Offload it.
        await asyncio.to_thread(_build_zip_archive, tmp_path, out_files)
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
    """Return the job's CorrectionReport (§9) — per-line text traces.

    The response IS the versioned ``CorrectionReport`` JSON (the same
    document persisted as ``trace.json``): ``report_version`` / ``run_id``
    (== ``job_id``) / ``total_lines`` / ``lines``. The pre-unification
    ``{job_id, total_lines, lines}`` JobTrace shape is gone.
    """
    if job.report is None or not job.report.lines:
        raise HTTPException(status_code=404, detail="No traces available for this job.")

    # Wave-3 review — serialising a full report (one entry per corpus
    # line) is CPU-bound; keep it off the event loop like every other
    # heavy call in this router.
    return await asyncio.to_thread(job.report.model_dump, exclude_none=True)


# ---------------------------------------------------------------------------
# GET /api/jobs/{job_id}/diff
# ---------------------------------------------------------------------------


@router.get("/{job_id}/diff")
async def get_job_diff(job: JobManifest = Depends(get_completed_job)) -> dict:
    """Return per-line OCR vs corrected diff data for a completed job.

    Thin adapter: the projection lives in ``app.api.read_models.build_diff``
    (pure, unit-tested). ``get_completed_job`` already 404s on a missing
    manifest, but an ``assert`` would disappear under ``python -O`` (bandit
    B101), so keep a real runtime guard.
    """
    if job.document_manifest is None:
        raise HTTPException(status_code=500, detail="Job has no document_manifest.")
    # Wave-3 review — a full-manifest projection is CPU-bound: offload.
    return await asyncio.to_thread(build_diff, job.job_id, job.document_manifest)


# ---------------------------------------------------------------------------
# GET /api/jobs/{job_id}/layout
# ---------------------------------------------------------------------------


@router.get("/{job_id}/layout")
async def get_job_layout(job: JobManifest = Depends(get_completed_job)) -> dict:
    """Return structural layout data (blocks + lines with ALTO coordinates).

    Thin adapter over ``app.api.read_models.build_layout`` (pure, unit-tested).
    """
    if job.document_manifest is None:
        raise HTTPException(status_code=500, detail="Job has no document_manifest.")
    # Wave-3 review — same offload rationale as /diff.
    return await asyncio.to_thread(build_layout, job.job_id, job.document_manifest, job.images)


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
    _job: JobManifest = Depends(require_job_access),
) -> Response:
    """Serve a source scan image for a job. P1-7 — <img> tags cannot set
    headers, so the capability token arrives as ``?token=``."""

    # Sanitise: only allow plain filenames (no path traversal)
    if "/" in image_name or "\\" in image_name or image_name.startswith("."):
        raise HTTPException(status_code=400, detail="Invalid image name.")

    # Audit P3 — check is_symlink on the UNRESOLVED path: resolve()
    # follows symlinks, so the resolved path is never itself a symlink,
    # making the old post-resolve is_symlink() check dead code. Reject a
    # symlinked member up front (defence in depth) before resolving.
    raw_path = images_dir(job_id) / image_name
    if raw_path.is_symlink():
        raise HTTPException(status_code=404, detail=f"Image not found: {image_name!r}")
    img_path = raw_path.resolve()
    allowed_dir = images_dir(job_id).resolve()
    if not img_path.is_relative_to(allowed_dir):
        raise HTTPException(status_code=400, detail="Invalid image name.")
    if not img_path.is_file():
        raise HTTPException(status_code=404, detail=f"Image not found: {image_name!r}")

    mime = _IMAGE_MIME.get(img_path.suffix.lower(), "application/octet-stream")
    # P2-12 — stream in chunks like the XML download does; read_bytes()
    # buffered whole scans (tens of MB) per request in memory.
    return FileResponse(img_path, media_type=mime)
