"""Pure correction pipeline.

The pipeline takes a parsed :class:`DocumentManifest`, drives the chunk
planner, calls the LLM provider, validates responses, reconciles hyphen
pairs, and writes outputs via the injected :class:`OutputWriter`. It
depends only on the three Protocols in :mod:`alto_core.protocols` — no
job store, no FastAPI, no filesystem path manipulation beyond reading
source files.

Side effects:
  - LLM HTTP calls via :class:`BaseProvider`
  - Event notifications via :class:`PipelineObserver`
  - Persistence via :class:`OutputWriter`

Statistics (retry count, fallback count, total chunks, hyphen pairs
reconciled) are returned in :class:`CorrectionResult` so the caller can
update its job state.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from alto_core.alto.hyphenation import enrich_chunk_lines, reconcile_hyphen_pair
from alto_core.alto.rewriter import extract_output_texts, rewrite_alto_file
from alto_core.pipeline.chunk_planner import plan_page
from alto_core.pipeline.line_acceptance import check_adjacent_duplicates, check_line
from alto_core.pipeline.validator import HyphenIntegrityError, validate_llm_response
from alto_core.protocols import BaseProvider, OutputWriter, PipelineObserver
from alto_core.protocols.provider import OUTPUT_JSON_SCHEMA, SYSTEM_PROMPT
from alto_core.schemas import (
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
    PipelineEventType,
)

# Patterns to redact common secret formats in error messages.
# Each pattern captures a prefix in the first group so the redacted
# output keeps human-readable context (e.g. "Bearer ****" instead of
# just "****"). Patterns are applied in order; first match wins.
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # HTTP Authorization headers — both schemes
    (re.compile(r"(Bearer\s+)\S+", re.IGNORECASE), r"\1****"),
    (re.compile(r"(Basic\s+)[A-Za-z0-9+/=]+", re.IGNORECASE), r"\1****"),
    # Vendor-prefixed keys (OpenAI sk-, Mistral key-, Anthropic sk-ant-, ...).
    # Hint = 4 chars after the prefix (sk-AAAA****) — stable test contract.
    (re.compile(r"(sk-[A-Za-z0-9_-]{4})\S+"), r"\1****"),
    (re.compile(r"(key-[A-Za-z0-9_-]{4})\S+"), r"\1****"),
    # Generic key=value patterns in query strings / form bodies / JSON.
    # Matches `api_key`, `api-key`, `apikey`, `password`, `secret`, `token`
    # then an optional closing quote (JSON-style "token":), then the
    # separator, then the value. Stops at the next quote/space/delimiter.
    (
        re.compile(
            r"((?:api[_-]?key|password|passwd|secret|token)"
            r"[\"']?\s*[=:]\s*[\"']?)[^\s\"'&,}\]]+",
            re.IGNORECASE,
        ),
        r"\1****",
    ),
    # Custom HTTP headers: x-api-key, x-auth-token, ...
    (
        re.compile(
            r"(x-(?:api-key|auth-token|access-token)\s*[:=]\s*)\S+",
            re.IGNORECASE,
        ),
        r"\1****",
    ),
)


def sanitize_error(msg: str, api_key: str | None = None) -> str:
    """Strip API keys and common secret patterns from an error message.

    The caller can supply the exact ``api_key`` for first-pass redaction;
    any remaining secret-shaped substrings are then masked by the
    pattern set above. Patterns cover:
      - HTTP ``Authorization: Bearer …`` and ``Basic …`` headers
      - Vendor-prefixed keys (``sk-…``, ``key-…``)
      - Generic ``api_key=…``, ``password=…``, ``token=…`` pairs
      - Custom headers (``X-Api-Key:``, ``X-Auth-Token:``, …)
    """
    if api_key and len(api_key) > 8 and api_key in msg:
        msg = msg.replace(api_key, api_key[:4] + "****")
    for pattern, replacement in _SECRET_PATTERNS:
        msg = pattern.sub(replacement, msg)
    return msg


def _trace_key(lm: LineManifest) -> str:
    """Composite key for line traces, avoiding collisions across pages."""
    return f"{lm.page_id}:{lm.line_order_global}:{lm.line_id}"


def _set_trace(
    traces: dict[str, LineTrace] | None,
    lm: LineManifest,
    **fields: object,
) -> None:
    """Assign trace fields on the LineTrace keyed by ``lm``, if tracked.

    Centralises the ``if traces is not None: t = traces.get(...);
    if t is not None: ...`` pattern that was repeated five times in
    ``_run_chunk`` and its helpers. A trace dict that isn't tracking
    a given line silently no-ops.
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
) -> None:
    """Apply reconcile_hyphen_pair and write results back onto the manifests."""
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

    lm.corrected_text = final_p1
    lm.status = LineStatus.CORRECTED
    part2.corrected_text = final_p2
    part2.status = LineStatus.CORRECTED
    part2.hyphen_subs_content = subs

    if is_forward:
        lm.hyphen_forward_subs_content = subs
    else:
        lm.hyphen_subs_content = subs


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

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(
        self,
        *,
        document_manifest: DocumentManifest,
        api_key: str,
        model: str,
        provider_name: str,
        source_files: dict[str, Path],
        run_id: str | None = None,
    ) -> CorrectionResult:
        """Run the full pipeline. Mutates `document_manifest.pages` in place.

        ``run_id`` is an optional identifier embedded in the emitted
        JobTrace so consumers can correlate the trace.json with their
        own job/request id. Generated as a uuid4 when omitted; it never
        leaks back into the public events.
        """
        run_id = run_id or str(uuid.uuid4())
        self._retry_count = 0
        self._fallback_count = 0

        total_hyphen_pairs = sum(
            sum(
                1
                for lm in page.lines
                if lm.hyphen_role in (HyphenRole.PART1, HyphenRole.BOTH)
            )
            for page in document_manifest.pages
        )

        self.observer.on_event(
            PipelineEventType.DOCUMENT_PARSED,
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
            run_id=run_id,
        )

        return CorrectionResult(
            total_chunks=total_chunks,
            total_reconciled=total_reconciled,
            retry_count=self._retry_count,
            fallback_count=self._fallback_count,
            traces=traces,
        )

    # ------------------------------------------------------------------
    # Per-page orchestration
    # ------------------------------------------------------------------

    async def _process_page(
        self,
        *,
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
            1
            for lm in page.lines
            if lm.hyphen_role in (HyphenRole.PART1, HyphenRole.BOTH)
        )
        self.observer.on_event(
            PipelineEventType.PAGE_STARTED,
            {
                "page_id": page.page_id,
                "page_index": page.page_index,
                "line_count": len(page.lines),
                "hyphen_pair_count": page_hyphen_pairs,
            },
        )

        plan = plan_page(page, document_id, self.config)

        self.observer.on_event(
            PipelineEventType.CHUNK_PLANNED,
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
                # ADR-006: pipeline does not log directly; emit an
                # event the host application can log/trace.
                self.observer.on_event(
                    PipelineEventType.CHUNK_ERROR,
                    {
                        "chunk_id": chunk.chunk_id,
                        "message": str(exc)[:200],
                        "exception_type": type(exc).__name__,
                    },
                )

        page.status = JobStatus.COMPLETED

        page_corrections = sum(
            1
            for lm in page.lines
            if lm.corrected_text is not None and lm.corrected_text != lm.ocr_text
        )
        self.observer.on_event(
            PipelineEventType.PAGE_COMPLETED,
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

    async def _run_chunk(
        self,
        *,
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
            PipelineEventType.CHUNK_STARTED,
            {
                "chunk_id": chunk.chunk_id,
                "granularity": chunk.granularity.value,
                "line_count": len(chunk_lines),
            },
        )

        max_attempts = self.DEFAULT_MAX_ATTEMPTS
        hyphen_violation = False

        for attempt in range(1, max_attempts + 1):
            # Retry temperature strategy: deterministic first, then more
            # diverse to escape bad patterns. Hyphen violations always
            # at 0.0 for maximum precision.
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
                    target = lm_by_id.get(rl.get("line_id", ""))
                    if target is not None:
                        _set_trace(
                            traces,
                            target,
                            model_corrected_text=rl.get("corrected_text", ""),
                        )

                hyphen_subs: dict[str, str] = {}
                for lm in chunk_lines:
                    if lm.hyphen_role == HyphenRole.PART1 and lm.hyphen_subs_content:
                        hyphen_subs[lm.line_id] = lm.hyphen_subs_content
                    elif (
                        lm.hyphen_role == HyphenRole.BOTH
                        and lm.hyphen_forward_subs_content
                    ):
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
                msg = sanitize_error(str(exc), api_key)
                # Classify the exception to pick the right policy.
                # alto-core stays http-library-agnostic — we duck-type
                # on transient HTTP class names rather than importing
                # httpx. A future cleanup would define a
                # ``ProviderTransientError`` in alto_core.protocols
                # for providers to raise.
                exc_class = type(exc).__name__
                is_hyphen_violation = isinstance(exc, HyphenIntegrityError)
                is_transient_http = exc_class in {
                    "HTTPStatusError",
                    "TimeoutException",
                    "NetworkError",
                    "ConnectError",
                    "RemoteProtocolError",
                    "ReadTimeout",
                }
                # LLM returned malformed JSON or failed schema validation —
                # likely transient (next attempt may produce clean output).
                is_llm_output_error = (
                    isinstance(exc, (ValueError, json.JSONDecodeError))
                    and not is_hyphen_violation
                )
                is_retryable = (
                    is_hyphen_violation or is_transient_http or is_llm_output_error
                )

                if attempt < max_attempts and is_retryable:
                    if is_hyphen_violation and not hyphen_violation:
                        hyphen_violation = True
                        backoff = 0
                        error_tag: str = "hyphen_integrity_violation"
                    elif is_transient_http:
                        backoff = attempt * 2
                        error_tag = msg[:120]
                    else:  # is_llm_output_error
                        backoff = attempt
                        error_tag = msg[:120]

                    if backoff > 0:
                        await asyncio.sleep(backoff)
                    self.observer.on_event(
                        PipelineEventType.RETRY,
                        {
                            "chunk_id": chunk.chunk_id,
                            "attempt": attempt,
                            "error": error_tag,
                        },
                    )
                    self._retry_count += 1
                    continue

                # All attempts exhausted → fallback (ADR-006: no log here,
                # the warning event carries the same information for hosts).
                self.observer.on_event(
                    PipelineEventType.WARNING,
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
                return 0

            # --- Success: apply corrections ---
            text_by_id: dict[str, str] = {
                o.line_id: o.corrected_text for o in response.lines
            }

            reconciled_count = self._reconcile_chunk_hyphens(
                chunk_id=chunk.chunk_id,
                chunk_lines=chunk_lines,
                text_by_id=text_by_id,
                line_by_id=line_by_id,
                cross_page_partners=cross_page_partners,
            )
            self._apply_line_acceptance(
                chunk_lines=chunk_lines,
                text_by_id=text_by_id,
                all_lines_by_id=all_lines_by_id,
                traces=traces,
            )
            self._finalize_chunk_traces(
                chunk_lines=chunk_lines,
                traces=traces,
            )

            self.observer.on_event(
                PipelineEventType.CHUNK_COMPLETED,
                {
                    "chunk_id": chunk.chunk_id,
                    "line_count": len(chunk_lines),
                    "hyphen_pairs_reconciled": reconciled_count,
                },
            )
            return reconciled_count

        return 0

    # ------------------------------------------------------------------
    # Chunk helpers extracted from _run_chunk (audit A3)
    # ------------------------------------------------------------------

    def _reconcile_chunk_hyphens(
        self,
        *,
        chunk_id: str,
        chunk_lines: list[LineManifest],
        text_by_id: dict[str, str],
        line_by_id: dict[str, LineManifest],
        cross_page_partners: dict[tuple[str, str], LineManifest] | None,
    ) -> int:
        """Two-pass hyphen reconciliation: PART1→partner, then BOTH→forward.

        Returns the number of pairs successfully reconciled. Emits a
        ``hyphen_partner_missing`` event for each unresolvable partner
        (likely cross-page) so observers can surface the diagnostic.
        """
        reconciled_count = 0
        processed_part2: set[tuple[str, str]] = set()

        # Pass 1: PART1 → partner (partner may be PART2 or BOTH)
        for lm in chunk_lines:
            if lm.hyphen_role != HyphenRole.PART1 or not lm.hyphen_pair_line_id:
                continue
            part2 = _resolve_partner(
                lm,
                is_forward=False,
                line_by_id=line_by_id,
                cross_page_partners=cross_page_partners,
            )
            if part2 is None:
                self.observer.on_event(
                    PipelineEventType.HYPHEN_PARTNER_MISSING,
                    {
                        "chunk_id": chunk_id,
                        "line_id": lm.line_id,
                        "missing_partner_id": lm.hyphen_pair_line_id,
                        "direction": "backward",
                    },
                )
                continue
            part2_key = (part2.page_id, part2.line_id)
            if part2_key in processed_part2:
                continue
            _reconcile_one_pair(lm, part2, text_by_id, is_forward=False)
            processed_part2.add(part2_key)
            reconciled_count += 1

        # Pass 2: BOTH → forward partner
        for lm in chunk_lines:
            if lm.hyphen_role != HyphenRole.BOTH or not lm.hyphen_forward_pair_id:
                continue
            part2 = _resolve_partner(
                lm,
                is_forward=True,
                line_by_id=line_by_id,
                cross_page_partners=cross_page_partners,
            )
            if part2 is None:
                self.observer.on_event(
                    PipelineEventType.HYPHEN_PARTNER_MISSING,
                    {
                        "chunk_id": chunk_id,
                        "line_id": lm.line_id,
                        "missing_partner_id": lm.hyphen_forward_pair_id,
                        "direction": "forward",
                    },
                )
                continue
            part2_key = (part2.page_id, part2.line_id)
            if part2_key in processed_part2:
                continue
            _reconcile_one_pair(lm, part2, text_by_id, is_forward=True)
            processed_part2.add(part2_key)
            reconciled_count += 1

        return reconciled_count

    def _apply_line_acceptance(
        self,
        *,
        chunk_lines: list[LineManifest],
        text_by_id: dict[str, str],
        all_lines_by_id: dict[str, LineManifest],
        traces: dict[str, LineTrace] | None,
    ) -> None:
        """Apply the per-line acceptance policy on lines not already
        reconciled as hyphen pairs.

        Two guards in order:
          1. Orphan PART1/BOTH whose OCR ends in '-' but corrected does
             not → the LLM completed a hyphen we couldn't reconcile;
             fall back to OCR to keep the marker.
          2. Centralised :func:`check_line` with prev/next context — the
             single source of truth for "is this correction acceptable?".
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

    def _finalize_chunk_traces(
        self,
        *,
        chunk_lines: list[LineManifest],
        traces: dict[str, LineTrace] | None,
    ) -> None:
        """Adjacent-duplicate revert + projected_text/validation_status
        for every line trace.

        Combines the two final sweeps that used to sit inline in
        ``_run_chunk``: duplicate detection mutates ``corrected_text``,
        then we project the post-mutation state onto the traces (when
        the host opted into them by passing a non-None ``traces`` dict).
        """
        accepted_lines = [
            (lm.line_id, lm.ocr_text, lm.corrected_text or lm.ocr_text)
            for lm in chunk_lines
        ]
        dup_reverts = check_adjacent_duplicates(accepted_lines)
        for lm in chunk_lines:
            if lm.line_id in dup_reverts:
                lm.corrected_text = lm.ocr_text
                lm.status = LineStatus.FALLBACK

        for lm in chunk_lines:
            _set_trace(
                traces,
                lm,
                projected_text=(
                    lm.corrected_text if lm.corrected_text is not None else lm.ocr_text
                ),
                validation_status=lm.status.value,
            )
            # Adjacent-duplicate revert: only stamp the reason if no earlier
            # fallback path (e.g. orphan_hyphen_completed) already pinned one.
            if traces is not None and lm.line_id in dup_reverts:
                trace = traces.get(_trace_key(lm))
                if trace is not None and not trace.fallback_reason:
                    trace.fallback_reason = dup_reverts[lm.line_id]

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
        run_id: str,
    ) -> None:
        """Rewrite corrected ALTO files, update traces, persist via writer."""
        for source_name, xml_path in source_files.items():
            pages_for_file = [
                p for p in document_manifest.pages if p.source_file == source_name
            ]
            if not pages_for_file:
                continue

            xml_bytes, _metrics, rewriter_paths = rewrite_alto_file(
                xml_path,
                pages_for_file,
                provider_name,
                model,
            )
            self.output_writer.write_corrected(
                source_stem=xml_path.stem,
                xml_bytes=xml_bytes,
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
            job_id=run_id,
            total_lines=len(traces),
            lines=list(traces.values()),
        )
        self.output_writer.write_trace(
            traces_payload=job_trace.model_dump_json(indent=2),
        )


# --- __all__ (Stage 3 audit remediation) ---
__all__ = [
    "sanitize_error",
    "CorrectionResult",
    "CorrectionPipeline",
]
