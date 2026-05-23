"""Main correction orchestrator."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

from app.alto.hyphenation import enrich_chunk_lines, reconcile_hyphen_pair
from app.alto.rewriter import extract_output_texts, rewrite_alto_file
from app.jobs.chunk_planner import plan_page
from app.jobs.line_acceptance import AcceptanceResult, check_adjacent_duplicates, check_line
from app.jobs.store import job_store
from app.jobs.validator import validate_llm_response
from app.providers.base import OUTPUT_JSON_SCHEMA, SYSTEM_PROMPT, BaseProvider
from app.schemas import (
    ChunkPlannerConfig,
    ChunkRequest,
    DocumentManifest,
    HyphenRole,
    JobStatus,
    JobTrace,
    LineManifest,
    LineStatus,
    LineTrace,
    LLMUserPayload,
    PageManifest,
)

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG = ChunkPlannerConfig()

# Global timeout for the entire job pipeline (seconds). 0 = no limit.
try:
    _JOB_TIMEOUT_SECONDS: int = int(os.environ.get("JOB_TIMEOUT_SECONDS", "1800"))
except ValueError:
    import warnings as _warnings
    _warnings.warn(
        "JOB_TIMEOUT_SECONDS env var is not a valid integer; using default 1800s",
        stacklevel=1,
    )
    _JOB_TIMEOUT_SECONDS = 1800

# Pattern to redact Bearer tokens, API keys, and common key formats
_SECRET_RE = re.compile(
    r"(Bearer\s+)\S+|"            # Authorization: Bearer <key>
    r"(sk-[A-Za-z0-9]{4})\S+|"   # OpenAI-style sk-...
    r"(key-[A-Za-z0-9]{4})\S+",  # Mistral-style key-...
    re.IGNORECASE,
)


def _sanitize_error(msg: str, api_key: str | None = None) -> str:
    """Strip API keys and secrets from error messages."""
    if api_key and len(api_key) > 8 and api_key in msg:
        msg = msg.replace(api_key, api_key[:4] + "****")
    return _SECRET_RE.sub(lambda m: (m.group(1) or m.group(2) or m.group(3) or "") + "****", msg)


def _trace_key(lm: LineManifest) -> str:
    """Composite key for line traces, avoiding collisions across pages."""
    return f"{lm.page_id}:{lm.line_order_global}:{lm.line_id}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_hyphen_pairs(
    lines: list[LineManifest],
) -> dict[str, str]:
    """Return PART1→PART2 and PART2→PART1 mapping for lines in the chunk."""
    pairs: dict[str, str] = {}
    for lm in lines:
        if lm.hyphen_role == HyphenRole.PART1 and lm.hyphen_pair_line_id:
            pairs[lm.line_id] = lm.hyphen_pair_line_id
            pairs[lm.hyphen_pair_line_id] = lm.line_id
        elif lm.hyphen_role == HyphenRole.BOTH and lm.hyphen_forward_pair_id:
            # Forward (PART1) side of a BOTH line
            pairs[lm.line_id] = lm.hyphen_forward_pair_id
            pairs[lm.hyphen_forward_pair_id] = lm.line_id
    return pairs


def _count_hyphen_pairs_in_chunk(lines: list[LineManifest]) -> int:
    return sum(
        1 for lm in lines
        if (lm.hyphen_role == HyphenRole.PART1 and lm.hyphen_pair_line_id)
        or (lm.hyphen_role == HyphenRole.BOTH and lm.hyphen_forward_pair_id)
    )


def _resolve_partner(
    lm: LineManifest,
    *,
    is_forward: bool,
    line_by_id: dict[str, LineManifest],
    cross_page_partners: Optional[dict[tuple[str, str], LineManifest]],
) -> Optional[LineManifest]:
    """Resolve a hyphen partner using a page-qualified lookup.

    When two ALTO files declare the same TextLine ID (e.g. both call
    their first line "TL1"), a bare-id lookup against the page-local
    ``line_by_id`` returns the wrong manifest for cross-page pairs.
    Prefer the qualified ``(page_id, line_id)`` lookup whenever the
    parser populated ``hyphen_pair_page_id`` / ``hyphen_forward_pair_page_id``.
    """
    if is_forward:
        partner_id = lm.hyphen_forward_pair_id
        partner_page = lm.hyphen_forward_pair_page_id
    else:
        partner_id = lm.hyphen_pair_line_id
        partner_page = lm.hyphen_pair_page_id

    if not partner_id:
        return None

    # Intra-page (or unknown page) → local lookup
    if partner_page is None or partner_page == lm.page_id:
        return line_by_id.get(partner_id)

    # Cross-page → qualified lookup
    if cross_page_partners is None:
        return None
    return cross_page_partners.get((partner_page, partner_id))


def _reconcile_one_pair(
    lm: LineManifest,
    part2: LineManifest,
    text_by_id: dict[str, str],
    *,
    is_forward: bool,
) -> None:
    """Apply reconcile_hyphen_pair and write results back onto the manifests.

    ``is_forward=True`` is for the BOTH→PART2 pass: uses the BOTH line's
    forward subs/explicit fields and reads lm.corrected_text (already set by
    pass 1) rather than text_by_id.
    """
    if is_forward:
        corrected_p1 = lm.corrected_text or text_by_id.get(lm.line_id, lm.ocr_text)
        extra: dict[str, Any] = {}
        if lm.hyphen_forward_subs_content is not None:
            extra["subs_content"] = lm.hyphen_forward_subs_content
        if lm.hyphen_forward_explicit is not None:
            extra["source_explicit"] = lm.hyphen_forward_explicit
    else:
        corrected_p1 = text_by_id.get(lm.line_id, lm.ocr_text)
        extra = {}

    corrected_p2 = text_by_id.get(part2.line_id, part2.ocr_text)

    final_p1, final_p2, subs = reconcile_hyphen_pair(
        lm, part2, corrected_p1, corrected_p2, **extra
    )

    lm.corrected_text = final_p1
    lm.status = LineStatus.CORRECTED
    part2.corrected_text = final_p2
    part2.status = LineStatus.CORRECTED
    part2.hyphen_subs_content = subs

    if is_forward:
        lm.hyphen_forward_subs_content = subs
    else:
        lm.hyphen_subs_content = subs


# ---------------------------------------------------------------------------
# Chunk execution
# ---------------------------------------------------------------------------

async def _run_chunk(
    job_id: str,
    chunk: ChunkRequest,
    line_by_id: dict[str, LineManifest],
    provider: BaseProvider,
    api_key: str,
    model: str,
    provider_name: str,
    traces: Optional[dict[str, LineTrace]] = None,
    cross_page_partners: Optional[dict[tuple[str, str], LineManifest]] = None,
) -> int:
    """
    Process one chunk through the LLM pipeline.

    Returns the number of hyphen pairs reconciled in this chunk.
    """
    chunk_lines = [line_by_id[lid] for lid in chunk.line_ids if lid in line_by_id]
    if not chunk_lines:
        return 0

    hyphen_pairs = _build_hyphen_pairs(chunk_lines)
    all_lines_by_id = line_by_id

    job_store.emit(job_id, "chunk_started", {
        "chunk_id": chunk.chunk_id,
        "granularity": chunk.granularity.value,
        "line_count": len(chunk_lines),
    })

    # --- Retry loop ---
    max_attempts = 3
    hyphen_violation = False

    for attempt in range(1, max_attempts + 1):
        # Retry temperature strategy: start deterministic (0.0), then
        # increase diversity on subsequent attempts to escape bad patterns.
        # Hyphen violations always use 0.0 for maximum precision.
        if hyphen_violation:
            temperature = 0.0
        elif attempt == 1:
            temperature = 0.0
        elif attempt == 2:
            temperature = 0.3
        else:
            temperature = 0.5

        enriched = enrich_chunk_lines(chunk_lines, all_lines_by_id)

        # --- Trace: model_input_text ---
        if traces is not None:
            enriched_by_id = {e.line_id: e for e in enriched}
            for lm in chunk_lines:
                t = traces.get(_trace_key(lm))
                if t is not None:
                    ei = enriched_by_id.get(lm.line_id)
                    if ei is not None:
                        t.model_input_text = ei.ocr_text

        payload = LLMUserPayload(
            granularity=chunk.granularity,
            document_id=chunk.document_id,
            page_id=chunk.page_id,
            block_id=chunk.block_id,
            lines=enriched,
        )
        user_dict = payload.model_dump(exclude_none=True)

        try:
            raw = await provider.complete_structured(
                api_key=api_key,
                model=model,
                system_prompt=SYSTEM_PROMPT,
                user_payload=user_dict,
                json_schema=OUTPUT_JSON_SCHEMA,
                temperature=temperature,
            )

            # --- Trace: capture raw LLM output before validation ---
            if traces is not None:
                lid_to_tkey = {lm.line_id: _trace_key(lm) for lm in chunk_lines}
                raw_lines = raw.get("lines", []) if isinstance(raw, dict) else []
                for rl in raw_lines:
                    lid = rl.get("line_id", "") if isinstance(rl, dict) else ""
                    rt = rl.get("corrected_text", "") if isinstance(rl, dict) else ""
                    tkey = lid_to_tkey.get(lid)
                    if tkey:
                        t = traces.get(tkey)
                        if t is not None:
                            t.model_corrected_text = rt

            # Build subs mapping for fusion detection in the validator
            hyphen_subs: dict[str, str] = {}
            for lm in chunk_lines:
                if lm.hyphen_role == HyphenRole.PART1 and lm.hyphen_subs_content:
                    hyphen_subs[lm.line_id] = lm.hyphen_subs_content
                elif lm.hyphen_role == HyphenRole.BOTH and lm.hyphen_forward_subs_content:
                    hyphen_subs[lm.line_id] = lm.hyphen_forward_subs_content

            response = validate_llm_response(
                raw,
                [lm.line_id for lm in chunk_lines],
                hyphen_pairs if hyphen_pairs else None,
                {lm.line_id: lm.ocr_text for lm in chunk_lines},
                hyphen_subs if hyphen_subs else None,
            )
            hyphen_violation = False

        except Exception as exc:
            msg = _sanitize_error(str(exc), api_key)
            is_http_error = not isinstance(exc, ValueError)
            is_hyphen_violation = (
                isinstance(exc, ValueError)
                and "hyphen_integrity_violation" in str(exc)
            )

            if attempt < max_attempts:
                # First hyphen violation → retry immediately at temperature=0.
                # Subsequent violations or any other failure → exponential backoff.
                if is_hyphen_violation and not hyphen_violation:
                    hyphen_violation = True
                    backoff = 0
                    error_tag: str = "hyphen_integrity_violation"
                elif is_http_error:
                    backoff = attempt * 2
                    error_tag = msg[:120]
                else:
                    backoff = attempt
                    error_tag = msg[:120]

                if backoff > 0:
                    await asyncio.sleep(backoff)
                job_store.emit(job_id, "retry", {
                    "chunk_id": chunk.chunk_id,
                    "attempt": attempt,
                    "error": error_tag,
                })
                job_store.increment_counter(job_id, "retries")
                continue

            # All attempts exhausted → fallback
            logger.warning("Chunk %s: all attempts failed, falling back to OCR source", chunk.chunk_id)
            job_store.emit(job_id, "warning", {
                "chunk_id": chunk.chunk_id,
                "message": f"Fallback to OCR source: {msg[:120]}",
            })
            for lm in chunk_lines:
                lm.corrected_text = lm.ocr_text
                lm.status = LineStatus.FALLBACK
                if traces is not None:
                    t = traces.get(_trace_key(lm))
                    if t is not None:
                        t.projected_text = lm.ocr_text
                        t.validation_status = "fallback"
                        t.fallback_reason = f"all_attempts_exhausted: {msg[:120]}"
            job_store.increment_counter(job_id, "fallbacks")
            return 0

        # --- Success: apply corrections ---
        text_by_id: dict[str, str] = {o.line_id: o.corrected_text for o in response.lines}

        # Reconcile hyphen pairs in two deterministic passes:
        # Pass 1: backward pairs (PART1 → PART2/BOTH)
        # Pass 2: forward pairs (BOTH → PART2)
        # This order guarantees BOTH's backward side is resolved before
        # its forward side, so the forward reconciliation uses the
        # already-validated text.
        reconciled_count = 0
        # Page-qualified key so two pages with the same partner line_id
        # don't appear to "share" a processed partner.
        processed_part2: set[tuple[str, str]] = set()

        # --- Pass 1: PART1 → partner (partner may be PART2 or BOTH) ---
        for lm in chunk_lines:
            if lm.hyphen_role != HyphenRole.PART1 or not lm.hyphen_pair_line_id:
                continue
            part2 = _resolve_partner(
                lm, is_forward=False,
                line_by_id=line_by_id, cross_page_partners=cross_page_partners,
            )
            if part2 is None:
                logger.warning(
                    "Hyphen pair partner %s not found for PART1 %s "
                    "(likely cross-page pair — skipping reconciliation)",
                    lm.hyphen_pair_line_id, lm.line_id,
                )
                continue
            part2_key = (part2.page_id, part2.line_id)
            if part2_key in processed_part2:
                continue
            _reconcile_one_pair(lm, part2, text_by_id, is_forward=False)
            processed_part2.add(part2_key)
            reconciled_count += 1

        # --- Pass 2: BOTH → forward partner ---
        for lm in chunk_lines:
            if lm.hyphen_role != HyphenRole.BOTH or not lm.hyphen_forward_pair_id:
                continue
            part2 = _resolve_partner(
                lm, is_forward=True,
                line_by_id=line_by_id, cross_page_partners=cross_page_partners,
            )
            if part2 is None:
                logger.warning(
                    "Hyphen forward partner %s not found for BOTH %s "
                    "(likely cross-page pair — skipping reconciliation)",
                    lm.hyphen_forward_pair_id, lm.line_id,
                )
                continue
            part2_key = (part2.page_id, part2.line_id)
            if part2_key in processed_part2:
                continue
            _reconcile_one_pair(lm, part2, text_by_id, is_forward=True)
            processed_part2.add(part2_key)
            reconciled_count += 1

        # Apply remaining lines via line_acceptance policy
        for lm in chunk_lines:
            if lm.corrected_text is None:
                corrected = text_by_id.get(lm.line_id)
                if corrected is not None:
                    # Guard: orphan PART1 (no partner in chunk) must keep
                    # its trailing hyphen.  If the LLM removed it, it
                    # likely tried to complete the word — reject.
                    if (
                        lm.hyphen_role in (HyphenRole.PART1, HyphenRole.BOTH)
                        and lm.ocr_text.rstrip().endswith("-")
                        and not corrected.rstrip().endswith("-")
                    ):
                        lm.corrected_text = lm.ocr_text
                        lm.status = LineStatus.FALLBACK
                        if traces is not None:
                            t = traces.get(_trace_key(lm))
                            if t is not None:
                                t.fallback_reason = "orphan_hyphen_completed"
                        continue

                    prev_ocr = all_lines_by_id[lm.prev_line_id].ocr_text if lm.prev_line_id and lm.prev_line_id in all_lines_by_id else None
                    next_ocr = all_lines_by_id[lm.next_line_id].ocr_text if lm.next_line_id and lm.next_line_id in all_lines_by_id else None
                    result = check_line(lm.ocr_text, corrected, prev_ocr, next_ocr)
                    lm.corrected_text = result.text
                    if result.accepted:
                        lm.status = LineStatus.CORRECTED
                    else:
                        lm.status = LineStatus.FALLBACK
                        if traces is not None:
                            t = traces.get(_trace_key(lm))
                            if t is not None:
                                t.fallback_reason = result.reason

        # Adjacent duplicate detection (post-acceptance pass)
        accepted_lines = [
            (lm.line_id, lm.ocr_text, lm.corrected_text or lm.ocr_text)
            for lm in chunk_lines
        ]
        dup_reverts = check_adjacent_duplicates(accepted_lines)
        for lm in chunk_lines:
            if lm.line_id in dup_reverts:
                lm.corrected_text = lm.ocr_text
                lm.status = LineStatus.FALLBACK

        # --- Trace: projected_text + validation_status ---
        if traces is not None:
            for lm in chunk_lines:
                t = traces.get(_trace_key(lm))
                if t is None:
                    continue
                t.projected_text = lm.corrected_text if lm.corrected_text is not None else lm.ocr_text
                t.validation_status = lm.status.value
                if lm.line_id in dup_reverts and not t.fallback_reason:
                    t.fallback_reason = dup_reverts[lm.line_id]

        job_store.emit(job_id, "chunk_completed", {
            "chunk_id": chunk.chunk_id,
            "line_count": len(chunk_lines),
            "hyphen_pairs_reconciled": reconciled_count,
        })
        return reconciled_count

    # Should not reach here
    return 0


# ---------------------------------------------------------------------------
# Sub-phases extracted from run_job
# ---------------------------------------------------------------------------

async def _process_page(
    job_id: str,
    page: PageManifest,
    document_id: str,
    provider: BaseProvider,
    api_key: str,
    model: str,
    provider_name: str,
    config: ChunkPlannerConfig,
    traces: dict[str, LineTrace],
    cross_page_partners: dict[tuple[str, str], LineManifest] | None = None,
) -> tuple[int, int]:
    """Process a single page: plan chunks, run LLM, reconcile.

    Returns (chunks_processed, hyphen_pairs_reconciled).

    ``cross_page_partners`` is keyed by ``(partner_page_id, partner_line_id)``
    so two pages with colliding TextLine IDs (e.g. both "TL1") never
    shadow each other. Cross-page partners are NOT injected into the
    page-local ``line_by_id``; reconciliation resolves them via
    ``_resolve_partner`` which prefers the qualified lookup.
    """
    line_by_id: dict[str, LineManifest] = {lm.line_id: lm for lm in page.lines}

    page_hyphen_pairs = sum(
        1 for lm in page.lines
        if lm.hyphen_role in (HyphenRole.PART1, HyphenRole.BOTH)
    )
    job_store.emit(job_id, "page_started", {
        "page_id": page.page_id,
        "page_index": page.page_index,
        "line_count": len(page.lines),
        "hyphen_pair_count": page_hyphen_pairs,
    })

    plan = plan_page(page, document_id, config)

    job_store.emit(job_id, "chunk_planned", {
        "page_id": page.page_id,
        "chunk_count": len(plan.chunks),
        "granularity": plan.granularity.value,
    })

    page_reconciled = 0
    page_chunks = 0

    for chunk in plan.chunks:
        page_chunks += 1
        try:
            n = await _run_chunk(
                job_id, chunk, line_by_id,
                provider, api_key, model, provider_name,
                traces=traces,
                cross_page_partners=cross_page_partners,
            )
            page_reconciled += n
        except Exception as exc:
            logger.exception("Chunk %s raised unexpectedly", chunk.chunk_id)
            job_store.emit(job_id, "warning", {
                "chunk_id": chunk.chunk_id,
                "message": str(exc)[:200],
            })

    page.status = JobStatus.COMPLETED

    page_corrections = sum(
        1 for lm in page.lines
        if lm.corrected_text is not None and lm.corrected_text != lm.ocr_text
    )
    job_store.emit(job_id, "page_completed", {
        "page_id": page.page_id,
        "page_index": page.page_index,
        "corrections": page_corrections,
        "hyphen_pairs_reconciled": page_reconciled,
    })

    return page_chunks, page_reconciled


def _write_outputs(
    document_manifest: DocumentManifest,
    source_files: dict[str, Path],
    out_dir: Path,
    provider_name: str,
    model: str,
    traces: dict[str, LineTrace],
    job_id: str,
) -> None:
    """Rewrite corrected ALTO files and build trace data."""
    for source_name, xml_path in source_files.items():
        pages_for_file = [
            p for p in document_manifest.pages
            if p.source_file == source_name
        ]
        if not pages_for_file:
            continue

        xml_bytes, _metrics, rewriter_paths = rewrite_alto_file(
            xml_path, pages_for_file, provider_name, model,
        )
        out_path = out_dir / f"{xml_path.stem}_corrected.xml"
        out_path.write_bytes(xml_bytes)

        # Build line_id → trace_key mapping for this file's pages
        lid_to_tkey: dict[str, str] = {}
        for p in pages_for_file:
            for lm in p.lines:
                lid_to_tkey[lm.line_id] = _trace_key(lm)

        for lid, rpath in rewriter_paths.items():
            tkey = lid_to_tkey.get(lid)
            if tkey:
                t = traces.get(tkey)
                if t is not None:
                    t.rewriter_path = rpath

        file_line_ids = {lm.line_id for p in pages_for_file for lm in p.lines}
        output_texts = extract_output_texts(xml_bytes, file_line_ids)
        for lid, otxt in output_texts.items():
            tkey = lid_to_tkey.get(lid)
            if tkey:
                t = traces.get(tkey)
                if t is not None:
                    t.output_alto_text = otxt

    # Write trace.json
    job_trace = JobTrace(
        job_id=job_id,
        total_lines=len(traces),
        lines=list(traces.values()),
    )
    trace_path = out_dir / "trace.json"
    trace_path.write_text(
        job_trace.model_dump_json(indent=2),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Pipeline core (wrapped in timeout by run_job)
# ---------------------------------------------------------------------------

async def _run_pipeline(
    job_id: str,
    document_manifest: DocumentManifest,
    provider: BaseProvider,
    api_key: str,
    model: str,
    provider_name: str,
    output_dir: Path,
    source_files: dict[str, Path],
) -> tuple[int, int]:
    """Run the correction pipeline. Returns (total_chunks, total_reconciled)."""
    job_store.update_job(job_id, status=JobStatus.STARTED)
    job_store.emit(job_id, "started", {"job_id": job_id})

    total_hyphen_pairs = sum(
        sum(
            1 for lm in page.lines
            if lm.hyphen_role in (HyphenRole.PART1, HyphenRole.BOTH)
        )
        for page in document_manifest.pages
    )

    job_store.emit(job_id, "document_parsed", {
        "total_pages": document_manifest.total_pages,
        "total_lines": document_manifest.total_lines,
        "hyphen_pairs": total_hyphen_pairs,
    })

    job_store.update_job(
        job_id,
        status=JobStatus.RUNNING,
        document_manifest=document_manifest,
        total_lines=document_manifest.total_lines,
    )

    total_chunks = 0
    total_reconciled = 0
    config = _DEFAULT_CONFIG

    # Initialize line traces
    traces: dict[str, LineTrace] = {}
    for page in document_manifest.pages:
        for lm in page.lines:
            traces[_trace_key(lm)] = LineTrace(
                line_id=lm.line_id,
                page_id=lm.page_id,
                source_ocr_text=lm.ocr_text,
                hyphen_role=lm.hyphen_role.value,
            )

    # Global lookup keyed by (page_id, line_id) so two ALTO files that
    # declare the same TextLine ID don't shadow each other (a bare-id
    # dict picks the last writer and silently mis-resolves cross-page
    # partners). The parser populates hyphen_*_pair_page_id at link time.
    all_lines_global: dict[tuple[str, str], LineManifest] = {}
    for page in document_manifest.pages:
        for lm in page.lines:
            all_lines_global[(lm.page_id, lm.line_id)] = lm

    for page in document_manifest.pages:
        # Find cross-page partners needed by this page's lines. Skip
        # intra-page links (they're already in the page-local line_by_id).
        cross_page: dict[tuple[str, str], LineManifest] = {}
        for lm in page.lines:
            for partner_id, partner_page in (
                (lm.hyphen_pair_line_id, lm.hyphen_pair_page_id),
                (lm.hyphen_forward_pair_id, lm.hyphen_forward_pair_page_id),
            ):
                if not partner_id or not partner_page:
                    continue
                if partner_page == page.page_id:
                    continue
                partner = all_lines_global.get((partner_page, partner_id))
                if partner is not None:
                    cross_page[(partner_page, partner_id)] = partner

        page_chunks, page_reconciled = await _process_page(
            job_id, page, document_manifest.document_id,
            provider, api_key, model, provider_name,
            config, traces,
            cross_page_partners=cross_page if cross_page else None,
        )
        total_chunks += page_chunks
        total_reconciled += page_reconciled

    _write_outputs(
        document_manifest, source_files, output_dir,
        provider_name, model, traces, job_id,
    )
    job_store.update_job(job_id, line_traces=traces)

    return total_chunks, total_reconciled


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def run_job(
    job_id: str,
    document_manifest: DocumentManifest,
    provider_name: str,
    api_key: str,
    model: str,
    output_dir: Path,
    source_files: dict[str, Path],
    provider: Optional[BaseProvider] = None,
) -> None:
    """
    Run the full correction pipeline for a job.

    source_files: mapping of source_name → xml_path on disk.
    provider: injected provider (for testing); if None, resolved from registry.
    """
    if provider is None:
        from app.providers import get_provider
        from app.schemas import Provider
        provider = get_provider(Provider(provider_name))

    start_time = time.monotonic()

    try:
        timeout = _JOB_TIMEOUT_SECONDS if _JOB_TIMEOUT_SECONDS > 0 else None
        total_chunks, total_reconciled = await asyncio.wait_for(
            _run_pipeline(
                job_id, document_manifest, provider, api_key, model,
                provider_name, output_dir, source_files,
            ),
            timeout=timeout,
        )

        lines_modified = sum(
            1 for page in document_manifest.pages
            for lm in page.lines
            if lm.corrected_text is not None and lm.corrected_text != lm.ocr_text
        )
        elapsed = round(time.monotonic() - start_time, 2)

        job_store.update_job(
            job_id,
            status=JobStatus.COMPLETED,
            chunks_total=total_chunks,
            lines_modified=lines_modified,
            duration_seconds=elapsed,
        )

        job_store.emit(job_id, "completed", {
            "job_id": job_id,
            "total_lines": document_manifest.total_lines,
            "lines_modified": lines_modified,
            "hyphen_pairs_total": total_reconciled,
            "chunks_total": total_chunks,
            "duration_seconds": elapsed,
        })

    except asyncio.TimeoutError:
        logger.error("Job %s timed out after %ss", job_id, _JOB_TIMEOUT_SECONDS)
        elapsed = round(time.monotonic() - start_time, 2)
        safe_error = f"Job timed out after {_JOB_TIMEOUT_SECONDS}s"
        job_store.update_job(
            job_id, status=JobStatus.FAILED, error=safe_error,
            duration_seconds=elapsed,
        )
        job_store.emit(job_id, "failed", {"job_id": job_id, "error": safe_error})

    except Exception as exc:
        logger.exception("Job %s failed", job_id)
        # Sanitize BEFORE truncating: if the api_key straddles the 500-char
        # boundary, slicing first would leave half the key visible and the
        # regex would fail to mask it.
        safe_error = _sanitize_error(str(exc), api_key)[:500]
        job_store.update_job(
            job_id,
            status=JobStatus.FAILED,
            error=safe_error,
            duration_seconds=time.monotonic() - start_time,
        )
        job_store.emit(job_id, "failed", {
            "job_id": job_id,
            "error": safe_error,
        })
