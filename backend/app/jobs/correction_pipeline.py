"""Pure correction pipeline.

The pipeline takes a parsed `DocumentManifest`, drives the chunk planner,
calls the LLM provider, validates responses, reconciles hyphen pairs,
and writes outputs via the injected `OutputWriter`. It depends only on
the three Protocols in `app.protocols` — no `job_store`, no FastAPI,
no filesystem path manipulation beyond reading source files.

Side effects:
  - LLM HTTP calls via `BaseProvider`
  - Event notifications via `PipelineObserver`
  - Persistence via `OutputWriter`

Statistics (retry count, fallback count, total chunks, hyphen pairs
reconciled) are returned in `CorrectionResult` so the caller can update
its job state.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from app.alto.hyphenation import (
    ReconcileMetrics,
    classify_reconcile_outcome,
    enrich_chunk_lines,
    reconcile_hyphen_pair,
)
from app.alto.rewriter import extract_output_texts, rewrite_alto_file
from app.jobs.chunk_planner import plan_page
from app.jobs.line_acceptance import check_adjacent_duplicates, check_line
from app.jobs.validator import HyphenIntegrityError, validate_llm_response
from app.protocols import BaseProvider, OutputWriter, PipelineObserver
from app.providers.base import OUTPUT_JSON_SCHEMA, SYSTEM_PROMPT
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
    LLMResponse,
    LLMUserPayload,
    PageManifest,
    SSEEventType,
)

logger = logging.getLogger(__name__)

# Pattern to redact Bearer tokens, API keys, and common key formats
_SECRET_RE = re.compile(
    r"(Bearer\s+)\S+|"  # Authorization: Bearer <key>
    r"(sk-[A-Za-z0-9]{4})\S+|"  # OpenAI-style sk-...
    r"(key-[A-Za-z0-9]{4})\S+",  # Mistral-style key-...
    re.IGNORECASE,
)


def sanitize_error(msg: str, api_key: str | None = None) -> str:
    """Strip API keys and secrets from error messages."""
    if api_key and len(api_key) > 8 and api_key in msg:
        msg = msg.replace(api_key, api_key[:4] + "****")
    return _SECRET_RE.sub(lambda m: (m.group(1) or m.group(2) or m.group(3) or "") + "****", msg)


def _trace_key(lm: LineManifest) -> str:
    """Composite key for line traces, avoiding collisions across pages."""
    return f"{lm.page_id}:{lm.line_order_global}:{lm.line_id}"


def _set_trace(
    traces: dict[str, LineTrace] | None,
    lm: LineManifest,
    **fields: object,
) -> None:
    """Assign trace fields on the LineTrace keyed by `lm`, if traces are tracked.

    Centralises the `if traces is not None: t = traces.get(...); if t is not None: ...`
    pattern that was repeated six times throughout `_run_chunk`.
    """
    if traces is None:
        return
    trace = traces.get(_trace_key(lm))
    if trace is None:
        return
    for name, value in fields.items():
        setattr(trace, name, value)


def _build_hyphen_pairs(lines: list[LineManifest]) -> dict[str, str]:
    """Return PART1↔PART2 mapping (bidirectional) for lines in the chunk."""
    pairs: dict[str, str] = {}
    for lm in lines:
        if lm.hyphen_role == HyphenRole.PART1 and lm.hyphen_pair_line_id:
            pairs[lm.line_id] = lm.hyphen_pair_line_id
            pairs[lm.hyphen_pair_line_id] = lm.line_id
        elif lm.hyphen_role == HyphenRole.BOTH and lm.hyphen_forward_pair_id:
            pairs[lm.line_id] = lm.hyphen_forward_pair_id
            pairs[lm.hyphen_forward_pair_id] = lm.line_id
    return pairs


def _resolve_partner(
    lm: LineManifest,
    *,
    is_forward: bool,
    line_by_id: dict[str, LineManifest],
    cross_page_partners: dict[tuple[str, str], LineManifest] | None,
) -> LineManifest | None:
    """Resolve a hyphen partner using a page-qualified lookup.

    When two ALTO files declare the same TextLine ID, a bare-id lookup
    against the page-local `line_by_id` returns the wrong manifest for
    cross-page pairs. Prefer the qualified `(page_id, line_id)` lookup
    whenever the parser populated `hyphen_pair_page_id`/
    `hyphen_forward_pair_page_id`.
    """
    if is_forward:
        partner_id = lm.hyphen_forward_pair_id
        partner_page = lm.hyphen_forward_pair_page_id
    else:
        partner_id = lm.hyphen_pair_line_id
        partner_page = lm.hyphen_pair_page_id

    if not partner_id:
        return None

    if partner_page is None or partner_page == lm.page_id:
        return line_by_id.get(partner_id)

    if cross_page_partners is None:
        return None
    return cross_page_partners.get((partner_page, partner_id))


def _reconcile_one_pair(
    lm: LineManifest,
    part2: LineManifest,
    text_by_id: dict[str, str],
    *,
    is_forward: bool,
) -> str:
    """Apply reconcile_hyphen_pair and write results back onto the manifests.

    Returns the outcome classification produced by
    ``classify_reconcile_outcome``: ``"coherent"`` / ``"fallback"`` /
    ``"neutralised"``. Counters in ``_reconcile_pairs`` aggregate this
    into the per-job ReconcileMetrics observability event.
    """
    corrected_p2 = text_by_id.get(part2.line_id, part2.ocr_text)

    if is_forward:
        corrected_p1 = lm.corrected_text or text_by_id.get(lm.line_id, lm.ocr_text)
        final_p1, final_p2, subs = reconcile_hyphen_pair(
            lm,
            part2,
            corrected_p1,
            corrected_p2,
            subs_content=lm.hyphen_forward_subs_content,
            source_explicit=lm.hyphen_forward_explicit,
        )
    else:
        corrected_p1 = text_by_id.get(lm.line_id, lm.ocr_text)
        final_p1, final_p2, subs = reconcile_hyphen_pair(
            lm,
            part2,
            corrected_p1,
            corrected_p2,
        )

    outcome = classify_reconcile_outcome(
        lm.ocr_text,
        part2.ocr_text,
        corrected_p1,
        corrected_p2,
        final_p1,
        final_p2,
        subs,
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

    return outcome


def _reconcile_pairs(
    chunk_lines: list[LineManifest],
    line_by_id: dict[str, LineManifest],
    text_by_id: dict[str, str],
    cross_page_partners: dict[tuple[str, str], LineManifest] | None,
) -> tuple[int, ReconcileMetrics]:
    """Reconcile all hyphen pairs whose PART1 (or BOTH forward) side sits
    in this chunk. Mutates the manifests in place via _reconcile_one_pair.

    Two passes — one per source role — share the same body:
      PART1 reconciles with its (backward) partner;
      BOTH reconciles with its forward partner.
    Order matters: a BOTH line can be PART2 of a preceding PART1 (handled
    in pass 1 from the PART1 side) and PART1 of the next pair (handled in
    pass 2 from the BOTH side). A shared `processed_part2` set prevents
    double-reconciliation when the two passes land on the same partner.

    Returns (pairs_reconciled, ReconcileMetrics). The metrics break the
    count down by outcome (coherent / fallback / neutralised) for the
    job-end reconcile_stats observability event.
    """
    reconciled = 0
    metrics = ReconcileMetrics()
    processed_part2: set[tuple[str, str]] = set()
    passes = (
        (HyphenRole.PART1, False, "hyphen_pair_line_id"),
        (HyphenRole.BOTH, True, "hyphen_forward_pair_id"),
    )
    for role, is_forward, partner_attr in passes:
        for lm in chunk_lines:
            if lm.hyphen_role != role or not getattr(lm, partner_attr):
                continue
            part2 = _resolve_partner(
                lm,
                is_forward=is_forward,
                line_by_id=line_by_id,
                cross_page_partners=cross_page_partners,
            )
            if part2 is None:
                logger.warning(
                    "Hyphen partner %s not found for %s line %s "
                    "(likely cross-page pair — skipping reconciliation)",
                    getattr(lm, partner_attr),
                    role.value,
                    lm.line_id,
                )
                continue
            part2_key = (part2.page_id, part2.line_id)
            if part2_key in processed_part2:
                continue
            outcome = _reconcile_one_pair(lm, part2, text_by_id, is_forward=is_forward)
            if outcome == "coherent":
                metrics.coherent += 1
            elif outcome == "fallback":
                metrics.fallback += 1
            elif outcome == "neutralised":
                metrics.neutralised += 1
            processed_part2.add(part2_key)
            reconciled += 1
    return reconciled, metrics


def _apply_line_acceptance(
    chunk_lines: list[LineManifest],
    text_by_id: dict[str, str],
    all_lines_by_id: dict[str, LineManifest],
    traces: dict[str, LineTrace] | None,
) -> None:
    """Apply per-line acceptance policy to chunk lines not yet decided.

    Lines whose ``corrected_text`` was already set by hyphen reconciliation
    are skipped. For the others:
      - Orphan-hyphen guard: a PART1/BOTH whose OCR ends with ``-`` but
        whose corrected text doesn't still end with ``-`` is reverted to
        OCR with ``fallback_reason="orphan_hyphen_completed"``.
      - Otherwise ``check_line`` is consulted with neighbour OCR context;
        rejected corrections are reverted to OCR with the rejection
        reason captured in the trace.
    """
    for lm in chunk_lines:
        if lm.corrected_text is not None:
            continue
        corrected = text_by_id.get(lm.line_id)
        if corrected is None:
            continue

        if (
            lm.hyphen_role in (HyphenRole.PART1, HyphenRole.BOTH)
            and lm.ocr_text.rstrip().endswith("-")
            and not corrected.rstrip().endswith("-")
        ):
            lm.corrected_text = lm.ocr_text
            lm.status = LineStatus.FALLBACK
            _set_trace(traces, lm, fallback_reason="orphan_hyphen_completed")
            continue

        prev_ocr = (
            all_lines_by_id[lm.prev_line_id].ocr_text
            if lm.prev_line_id and lm.prev_line_id in all_lines_by_id
            else None
        )
        next_ocr = (
            all_lines_by_id[lm.next_line_id].ocr_text
            if lm.next_line_id and lm.next_line_id in all_lines_by_id
            else None
        )
        result = check_line(lm.ocr_text, corrected, prev_ocr, next_ocr)
        lm.corrected_text = result.text
        if result.accepted:
            lm.status = LineStatus.CORRECTED
        else:
            lm.status = LineStatus.FALLBACK
            _set_trace(traces, lm, fallback_reason=result.reason)


@dataclass
class CorrectionResult:
    """Outcome of a full pipeline run.

    The `document_manifest` is mutated in place during the run; callers
    can read corrected_text/status on each line. `traces` is the
    line-by-line text trace through every stage.
    """

    total_chunks: int
    total_reconciled: int
    retry_count: int
    fallback_count: int
    traces: dict[str, LineTrace]
    reconcile_metrics: ReconcileMetrics


class CorrectionPipeline:
    """Pure orchestration of the LLM-based ALTO correction pipeline.

    Dependencies are injected via the constructor; the pipeline never
    reaches for global state. Counters track stats locally and are
    exposed in the final `CorrectionResult` for the caller to persist.
    """

    DEFAULT_MAX_ATTEMPTS = 3

    def __init__(
        self,
        provider: BaseProvider,
        observer: PipelineObserver,
        output_writer: OutputWriter,
        config: ChunkPlannerConfig | None = None,
    ) -> None:
        self.provider = provider
        self.observer = observer
        self.output_writer = output_writer
        self.config = config or ChunkPlannerConfig()
        # Counters reset on every call to run()
        self._retry_count = 0
        self._fallback_count = 0
        self._reconcile_metrics = ReconcileMetrics()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(
        self,
        *,
        job_id: str,
        document_manifest: DocumentManifest,
        api_key: str,
        model: str,
        provider_name: str,
        source_files: dict[str, Path],
    ) -> CorrectionResult:
        """Run the full pipeline. Mutates `document_manifest.pages` in place."""
        self._retry_count = 0
        self._fallback_count = 0
        self._reconcile_metrics = ReconcileMetrics()

        total_hyphen_pairs = sum(
            sum(1 for lm in page.lines if lm.hyphen_role in (HyphenRole.PART1, HyphenRole.BOTH))
            for page in document_manifest.pages
        )

        self.observer.on_event(
            SSEEventType.DOCUMENT_PARSED,
            {
                "total_pages": document_manifest.total_pages,
                "total_lines": document_manifest.total_lines,
                "hyphen_pairs": total_hyphen_pairs,
            },
        )

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

        # Global page-qualified registry for cross-page partner lookups
        all_lines_global: dict[tuple[str, str], LineManifest] = {}
        for page in document_manifest.pages:
            for lm in page.lines:
                all_lines_global[(lm.page_id, lm.line_id)] = lm

        total_chunks = 0
        total_reconciled = 0

        for page in document_manifest.pages:
            # Cross-page partners needed by this page's lines
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

            page_chunks, page_reconciled = await self._process_page(
                job_id=job_id,
                page=page,
                document_id=document_manifest.document_id,
                api_key=api_key,
                model=model,
                provider_name=provider_name,
                traces=traces,
                cross_page_partners=cross_page if cross_page else None,
            )
            total_chunks += page_chunks
            total_reconciled += page_reconciled

        self._write_outputs(
            document_manifest=document_manifest,
            source_files=source_files,
            provider_name=provider_name,
            model=model,
            traces=traces,
            job_id=job_id,
        )

        return CorrectionResult(
            total_chunks=total_chunks,
            total_reconciled=total_reconciled,
            retry_count=self._retry_count,
            fallback_count=self._fallback_count,
            traces=traces,
            reconcile_metrics=self._reconcile_metrics,
        )

    # ------------------------------------------------------------------
    # Per-page orchestration
    # ------------------------------------------------------------------

    async def _process_page(
        self,
        *,
        job_id: str,
        page: PageManifest,
        document_id: str,
        api_key: str,
        model: str,
        provider_name: str,
        traces: dict[str, LineTrace],
        cross_page_partners: dict[tuple[str, str], LineManifest] | None,
    ) -> tuple[int, int]:
        line_by_id: dict[str, LineManifest] = {lm.line_id: lm for lm in page.lines}

        page_hyphen_pairs = sum(
            1 for lm in page.lines if lm.hyphen_role in (HyphenRole.PART1, HyphenRole.BOTH)
        )
        self.observer.on_event(
            SSEEventType.PAGE_STARTED,
            {
                "page_id": page.page_id,
                "page_index": page.page_index,
                "line_count": len(page.lines),
                "hyphen_pair_count": page_hyphen_pairs,
            },
        )

        plan = plan_page(page, document_id, self.config)

        self.observer.on_event(
            SSEEventType.CHUNK_PLANNED,
            {
                "page_id": page.page_id,
                "chunk_count": len(plan.chunks),
                "granularity": plan.granularity.value,
            },
        )

        page_reconciled = 0
        page_chunks = 0

        for chunk in plan.chunks:
            page_chunks += 1
            try:
                n = await self._run_chunk(
                    job_id=job_id,
                    chunk=chunk,
                    line_by_id=line_by_id,
                    api_key=api_key,
                    model=model,
                    provider_name=provider_name,
                    traces=traces,
                    cross_page_partners=cross_page_partners,
                )
                page_reconciled += n
            except Exception as exc:
                logger.exception("Chunk %s raised unexpectedly", chunk.chunk_id)
                self.observer.on_event(
                    SSEEventType.WARNING,
                    {
                        "chunk_id": chunk.chunk_id,
                        "message": str(exc)[:200],
                    },
                )

        page.status = JobStatus.COMPLETED

        page_corrections = sum(
            1
            for lm in page.lines
            if lm.corrected_text is not None and lm.corrected_text != lm.ocr_text
        )
        self.observer.on_event(
            SSEEventType.PAGE_COMPLETED,
            {
                "page_id": page.page_id,
                "page_index": page.page_index,
                "corrections": page_corrections,
                "hyphen_pairs_reconciled": page_reconciled,
            },
        )

        return page_chunks, page_reconciled

    # ------------------------------------------------------------------
    # Per-chunk LLM call + reconciliation
    # ------------------------------------------------------------------

    async def _call_with_retry(
        self,
        *,
        chunk: ChunkRequest,
        chunk_lines: list[LineManifest],
        hyphen_pairs: dict[str, str],
        all_lines_by_id: dict[str, LineManifest],
        api_key: str,
        model: str,
        traces: dict[str, LineTrace] | None,
    ) -> LLMResponse | None:
        """Call the LLM provider with retries; return a validated response.

        On success: returns the validated LLMResponse.
        On exhaustion: applies the OCR fallback to every line in
        ``chunk_lines`` (corrected_text / status / trace), emits a
        ``warning`` SSE event, increments ``self._fallback_count`` and
        returns ``None``. The caller must short-circuit on ``None``.

        Retry strategy (see test_retry_classification for the pinned
        contract):
          - Up to ``DEFAULT_MAX_ATTEMPTS`` attempts.
          - Temperature ramp 0.0 → 0.3 → 0.5, pinned at 0.0 after a
            HyphenIntegrityError (which suggests the LLM mis-handled the
            hyphen pair — a lower temperature is more likely to stick to
            the source).
          - Backoff: 0s for hyphen violations (first occurrence),
            ``attempt * 2`` seconds for non-ValueError (HTTP, runtime),
            ``attempt`` seconds for any other ValueError. Each retry
            emits a ``retry`` SSE event with the classification tag.
        """
        max_attempts = self.DEFAULT_MAX_ATTEMPTS
        hyphen_violation = False

        for attempt in range(1, max_attempts + 1):
            if hyphen_violation or attempt == 1:
                temperature = 0.0
            elif attempt == 2:
                temperature = 0.3
            else:
                temperature = 0.5

            enriched = enrich_chunk_lines(chunk_lines, all_lines_by_id)

            enriched_by_id = {e.line_id: e for e in enriched}
            for lm in chunk_lines:
                ei = enriched_by_id.get(lm.line_id)
                if ei is not None:
                    _set_trace(traces, lm, model_input_text=ei.ocr_text)

            payload = LLMUserPayload(
                granularity=chunk.granularity,
                document_id=chunk.document_id,
                page_id=chunk.page_id,
                block_id=chunk.block_id,
                lines=enriched,
            )
            user_dict = payload.model_dump(exclude_none=True)

            try:
                raw = await self.provider.complete_structured(
                    api_key=api_key,
                    model=model,
                    system_prompt=SYSTEM_PROMPT,
                    user_payload=user_dict,
                    json_schema=OUTPUT_JSON_SCHEMA,
                    temperature=temperature,
                )

                lm_by_id = {lm.line_id: lm for lm in chunk_lines}
                raw_lines = raw.get("lines", []) if isinstance(raw, dict) else []
                for rl in raw_lines:
                    if not isinstance(rl, dict):
                        continue
                    lm = lm_by_id.get(rl.get("line_id", ""))
                    if lm is not None:
                        _set_trace(traces, lm, model_corrected_text=rl.get("corrected_text", ""))

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
                return response

            except Exception as exc:
                msg = sanitize_error(str(exc), api_key)
                is_hyphen_violation = isinstance(exc, HyphenIntegrityError)
                is_http_error = not isinstance(exc, ValueError)

                if attempt < max_attempts:
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
                    self.observer.on_event(
                        "retry",
                        {
                            "chunk_id": chunk.chunk_id,
                            "attempt": attempt,
                            "error": error_tag,
                        },
                    )
                    self._retry_count += 1
                    continue

                # All attempts exhausted → fallback
                logger.warning(
                    "Chunk %s: all attempts failed, falling back to OCR source",
                    chunk.chunk_id,
                )
                self.observer.on_event(
                    SSEEventType.WARNING,
                    {
                        "chunk_id": chunk.chunk_id,
                        "message": f"Fallback to OCR source: {msg[:120]}",
                    },
                )
                for lm in chunk_lines:
                    lm.corrected_text = lm.ocr_text
                    lm.status = LineStatus.FALLBACK
                    _set_trace(
                        traces,
                        lm,
                        projected_text=lm.ocr_text,
                        validation_status="fallback",
                        fallback_reason=f"all_attempts_exhausted: {msg[:120]}",
                    )
                self._fallback_count += 1
                return None

        # Unreachable: every iteration either returns or continues.
        return None

    async def _run_chunk(
        self,
        *,
        job_id: str,
        chunk: ChunkRequest,
        line_by_id: dict[str, LineManifest],
        api_key: str,
        model: str,
        provider_name: str,
        traces: dict[str, LineTrace] | None = None,
        cross_page_partners: dict[tuple[str, str], LineManifest] | None = None,
    ) -> int:
        """Process one chunk through the LLM. Returns hyphen pairs reconciled."""
        chunk_lines = [line_by_id[lid] for lid in chunk.line_ids if lid in line_by_id]
        if not chunk_lines:
            return 0

        hyphen_pairs = _build_hyphen_pairs(chunk_lines)
        all_lines_by_id = line_by_id

        self.observer.on_event(
            SSEEventType.CHUNK_STARTED,
            {
                "chunk_id": chunk.chunk_id,
                "granularity": chunk.granularity.value,
                "line_count": len(chunk_lines),
            },
        )

        response = await self._call_with_retry(
            chunk=chunk,
            chunk_lines=chunk_lines,
            hyphen_pairs=hyphen_pairs,
            all_lines_by_id=all_lines_by_id,
            api_key=api_key,
            model=model,
            traces=traces,
        )
        if response is None:
            # All attempts exhausted; _call_with_retry already applied the
            # OCR fallback and the warning event. Nothing left to do here.
            return 0

        text_by_id: dict[str, str] = {o.line_id: o.corrected_text for o in response.lines}

        reconciled_count, chunk_reconcile_metrics = _reconcile_pairs(
            chunk_lines, line_by_id, text_by_id, cross_page_partners
        )
        self._reconcile_metrics.coherent += chunk_reconcile_metrics.coherent
        self._reconcile_metrics.fallback += chunk_reconcile_metrics.fallback
        self._reconcile_metrics.neutralised += chunk_reconcile_metrics.neutralised

        # Apply remaining lines via line_acceptance policy
        _apply_line_acceptance(chunk_lines, text_by_id, all_lines_by_id, traces)

        # Adjacent duplicate detection (post-acceptance pass)
        accepted_lines = [
            (lm.line_id, lm.ocr_text, lm.corrected_text or lm.ocr_text) for lm in chunk_lines
        ]
        dup_reverts = check_adjacent_duplicates(accepted_lines)
        for lm in chunk_lines:
            if lm.line_id in dup_reverts:
                lm.corrected_text = lm.ocr_text
                lm.status = LineStatus.FALLBACK

        # Trace: projected_text + validation_status
        for lm in chunk_lines:
            _set_trace(
                traces,
                lm,
                projected_text=(
                    lm.corrected_text if lm.corrected_text is not None else lm.ocr_text
                ),
                validation_status=lm.status.value,
            )
            # Only set fallback_reason for adjacent-duplicate reverts when
            # an earlier reason hasn't already pinned the cause.
            if traces is not None and lm.line_id in dup_reverts:
                trace = traces.get(_trace_key(lm))
                if trace is not None and not trace.fallback_reason:
                    trace.fallback_reason = dup_reverts[lm.line_id]

        self.observer.on_event(
            SSEEventType.CHUNK_COMPLETED,
            {
                "chunk_id": chunk.chunk_id,
                "line_count": len(chunk_lines),
                "hyphen_pairs_reconciled": reconciled_count,
            },
        )
        return reconciled_count

    # ------------------------------------------------------------------
    # Output writing (rewriter + trace assembly)
    # ------------------------------------------------------------------

    def _write_outputs(
        self,
        *,
        document_manifest: DocumentManifest,
        source_files: dict[str, Path],
        provider_name: str,
        model: str,
        traces: dict[str, LineTrace],
        job_id: str,
    ) -> None:
        """Rewrite corrected ALTO files, update traces, persist via writer."""
        for source_name, xml_path in source_files.items():
            pages_for_file = [p for p in document_manifest.pages if p.source_file == source_name]
            if not pages_for_file:
                continue

            xml_bytes, metrics, rewriter_paths = rewrite_alto_file(
                xml_path,
                pages_for_file,
                provider_name,
                model,
            )
            self.output_writer.write_corrected(
                source_stem=xml_path.stem,
                xml_bytes=xml_bytes,
            )
            self.observer.on_event(
                SSEEventType.REWRITER_STATS,
                {
                    "source_stem": xml_path.stem,
                    "untouched": metrics.untouched,
                    "subs_only": metrics.subs_only,
                    "fast_path": metrics.fast_path,
                    "slow_path": metrics.slow_path,
                },
            )

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

        job_trace = JobTrace(
            job_id=job_id,
            total_lines=len(traces),
            lines=list(traces.values()),
        )
        self.output_writer.write_trace(
            traces_payload=job_trace.model_dump_json(indent=2),
        )
