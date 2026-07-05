"""Pure correction pipeline.

The pipeline takes a parsed :class:`DocumentManifest`, drives the chunk
planner, calls the LLM provider, validates responses, reconciles hyphen
pairs, and writes outputs via the injected :class:`OutputWriter`. It
depends only on the three Protocols in :mod:`corrigenda.protocols` — no
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
import hashlib
import json
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from corrigenda.alto.hyphenation import (
    ReconcileMetrics,
    classify_reconcile_outcome,
    enrich_chunk_lines,
    reconcile_hyphen_pair,
)
from corrigenda.alto.rewriter import extract_output_texts, rewrite_alto_file
from corrigenda.errors import CorrectionAborted
from corrigenda.pipeline.chunk_planner import downgrade_granularity, plan_page
from corrigenda.pipeline.line_acceptance import check_adjacent_duplicates, check_line
from corrigenda.pipeline.validator import HyphenIntegrityError, validate_llm_response
from corrigenda.protocols import (
    BaseProvider,
    OutputWriter,
    PipelineObserver,
    ProviderTransientError,
)
from corrigenda.protocols.provider import OUTPUT_JSON_SCHEMA, SYSTEM_PROMPT
from corrigenda.schemas import (
    DEFAULT_GUARD_CONFIG,
    DEFAULT_PAIRING_POLICY,
    DEFAULT_RETRY_POLICY,
    BlockManifest,
    ChunkPlannerConfig,
    ChunkRequest,
    CorrectionReport,
    DocumentManifest,
    GuardConfig,
    HyphenRole,
    JobTrace,
    LineManifest,
    LineStatus,
    LineTrace,
    LLMResponse,
    LLMUserPayload,
    PageManifest,
    PairingPolicy,
    PipelineEventType,
    RetryPolicy,
    Usage,
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


@dataclass(frozen=True)
class _RetryDecision:
    """Pure result of classifying a retry-loop exception.

    Decoupled from the retry loop so the classifier can be tested in
    isolation (no chunk, no observer, no traces — just the exception
    and the per-chunk hyphen latch).
    """

    is_retryable: bool
    backoff: float
    error_tag: str
    is_hyphen_violation: bool


def _classify_retry(
    *,
    exc: BaseException,
    sanitised_msg: str,
    attempt: int,
    hyphen_already_seen: bool,
    policy: RetryPolicy = DEFAULT_RETRY_POLICY,
) -> _RetryDecision:
    """Decide what to do with an exception during the LLM retry loop.

    Three retryable branches:
      - ``HyphenIntegrityError`` (first occurrence per chunk):
        backoff 0, fixed tag ``"hyphen_integrity_violation"``.
      - ``ProviderTransientError`` (transport): backoff = attempt * 2.
      - other ``ValueError`` / ``JSONDecodeError`` (malformed LLM
        output): backoff = attempt.

    Anything else (or a second hyphen-integrity violation in the same
    chunk) is non-retryable from THIS decision's standpoint — the
    caller short-circuits to the OCR fallback.

    Caller passes ``sanitised_msg`` (already run through
    ``sanitize_error``) so we don't re-sanitise here.
    """
    is_hyphen_violation = isinstance(exc, HyphenIntegrityError)
    is_transient_http = isinstance(exc, ProviderTransientError)
    # A repeated HyphenIntegrityError on the same chunk falls into the
    # LLM-output-error path (linear backoff): the per-chunk latch only
    # exempts the FIRST occurrence; subsequent ones are treated like
    # any other malformed LLM output.
    is_llm_output_error = isinstance(exc, (ValueError, json.JSONDecodeError))

    if is_hyphen_violation and not hyphen_already_seen:
        return _RetryDecision(
            is_retryable=True,
            backoff=0,
            error_tag="hyphen_integrity_violation",
            is_hyphen_violation=True,
        )
    if is_transient_http:
        return _RetryDecision(
            is_retryable=True,
            backoff=attempt * policy.transient_backoff_base,
            error_tag=sanitised_msg[:120],
            is_hyphen_violation=False,
        )
    if is_llm_output_error:
        return _RetryDecision(
            is_retryable=True,
            backoff=attempt * policy.output_backoff_base,
            error_tag=sanitised_msg[:120],
            is_hyphen_violation=False,
        )
    return _RetryDecision(
        is_retryable=False,
        backoff=0,
        error_tag=sanitised_msg[:120],
        is_hyphen_violation=False,
    )


def _subpage_for_lines(page: PageManifest, lines: list[LineManifest]) -> PageManifest:
    """Build a synthetic single-page manifest holding just ``lines`` (F1).

    Used to re-plan a failed chunk's lines at a finer granularity via the
    normal chunk planner: the planner needs a ``PageManifest`` with the
    blocks that own these lines. Blocks are copied with their ``line_ids``
    filtered to the subset, preserving block order and geometry so BLOCK /
    WINDOW planning behave exactly as on the real page.
    """
    kept_ids = {lm.line_id for lm in lines}
    sub_blocks = [
        BlockManifest(
            block_id=b.block_id,
            page_id=b.page_id,
            block_order=b.block_order,
            coords=b.coords,
            line_ids=[lid for lid in b.line_ids if lid in kept_ids],
        )
        for b in page.blocks
        if any(lid in kept_ids for lid in b.line_ids)
    ]
    return PageManifest(
        page_id=page.page_id,
        source_file=page.source_file,
        page_index=page.page_index,
        page_width=page.page_width,
        page_height=page.page_height,
        blocks=sub_blocks,
        lines=lines,
    )


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
    config: GuardConfig = DEFAULT_GUARD_CONFIG,
) -> str:
    """Apply reconcile_hyphen_pair and write results back onto the manifests.

    Returns the outcome classification produced by
    ``classify_reconcile_outcome``: ``"coherent"`` / ``"fallback"`` /
    ``"neutralised"``. The pipeline aggregates these into the per-job
    ReconcileMetrics surfaced on the reconcile_stats observability event.
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
            config=config,
        )
    else:
        corrected_p1 = text_by_id.get(lm.line_id, lm.ocr_text)
        final_p1, final_p2, subs = reconcile_hyphen_pair(
            lm,
            part2,
            corrected_p1,
            corrected_p2,
            config=config,
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
    #: F14 — aggregate token consumption across every producer call in the
    #: run (zero when no provider reported usage).
    usage: Usage
    #: §9 — public, versioned correction report (same line traces, promoted
    #: to a documented artefact). Present on every run, including dry runs.
    report: CorrectionReport


class CorrectionPipeline:
    """Pure orchestration of the LLM-based ALTO correction pipeline.

    Dependencies are injected via the constructor; the pipeline never
    reaches for global state. Counters track stats locally and are
    exposed in the final `CorrectionResult` for the caller to persist.
    """

    def __init__(
        self,
        provider: BaseProvider,
        observer: PipelineObserver,
        output_writer: OutputWriter,
        config: ChunkPlannerConfig | None = None,
        retry_policy: RetryPolicy | None = None,
        guard_config: GuardConfig | None = None,
        pairing_policy: PairingPolicy | None = None,
    ) -> None:
        self.provider = provider
        self.observer = observer
        self.output_writer = output_writer
        self.config = config or ChunkPlannerConfig()
        # F9 — retry ramp / attempt cap / per-chunk budget. Default reproduces
        # the historical temperature ramp (0.0/0.3/0.5) and 3-attempt cap.
        self.retry_policy = retry_policy or DEFAULT_RETRY_POLICY
        # F13 — all anti-migration / acceptance thresholds. Default reproduces
        # the historical constants byte-for-byte.
        self.guard_config = guard_config or DEFAULT_GUARD_CONFIG
        # §11 — provenance only. Hyphen pairing happens at PARSE time, before
        # the pipeline exists; pass the same PairingPolicy you parsed with so
        # the configuration fingerprint stamped into the corrected XML covers
        # every §8.2 policy. The pipeline itself never re-pairs lines.
        self.pairing_policy = pairing_policy or DEFAULT_PAIRING_POLICY
        # Counters reset on every call to run()
        self._retry_count = 0
        self._fallback_count = 0
        self._reconcile_metrics = ReconcileMetrics()
        self._usage = Usage()

    def config_fingerprint(self) -> str:
        """Stable 16-hex hash over the pipeline's §8.2 policies (§11).

        Public and reproducible from the public API alone: it is the sha256
        (truncated to 16 hex chars) of the sorted JSON object mapping each
        policy name to its ``policy_fingerprint()``::

            {"chunk_planner": …, "guard": …, "pairing": …, "retry": …}

        Covers all four §8.2 policies — RetryPolicy, GuardConfig,
        ChunkPlannerConfig and PairingPolicy — so the ``processingStep``
        stamped into a corrected XML records the exact configuration it was
        produced under, and a consumer holding the same policy objects can
        recompute and verify it.
        """
        payload = json.dumps(
            {
                "chunk_planner": self.config.policy_fingerprint(),
                "guard": self.guard_config.policy_fingerprint(),
                "pairing": self.pairing_policy.policy_fingerprint(),
                "retry": self.retry_policy.policy_fingerprint(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def _record_reconcile_outcome(self, outcome: str) -> None:
        """Bump the per-job ReconcileMetrics counter for a single pair."""
        if outcome == "coherent":
            self._reconcile_metrics.coherent += 1
        elif outcome == "fallback":
            self._reconcile_metrics.fallback += 1
        elif outcome == "neutralised":
            self._reconcile_metrics.neutralised += 1

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
        should_abort: Callable[[], bool] | None = None,
        apply: bool = True,
    ) -> CorrectionResult:
        """Run the full pipeline. Mutates `document_manifest.pages` in place.

        ``run_id`` is an optional identifier embedded in the emitted
        JobTrace so consumers can correlate the trace.json with their
        own job/request id. Generated as a uuid4 when omitted; it never
        leaks back into the public events.

        ``should_abort`` (F10) is an optional cancellation probe. It is
        polled between pages and between chunks; when it returns ``True``
        the run raises :class:`CorrectionAborted` and **no output is
        written** (neither corrected XML nor trace). A provider call
        already in flight is not interrupted — cancellation is cooperative
        and observed only at chunk/page boundaries.

        ``apply`` (§9 dry-run) — when ``False``, the full pipeline runs
        (production, guards, reconciliation, and an in-memory rewrite so the
        report's ``rewriter_path`` / ``output_alto_text`` are populated) but
        the injected ``OutputWriter`` is **never called**: no corrected XML
        and no trace are persisted. The returned :class:`CorrectionResult`
        (and its :class:`CorrectionReport`) is the whole deliverable —
        useful for preview or for a consumer benchmarking without writing.
        """
        run_id = run_id or str(uuid.uuid4())
        self._retry_count = 0
        self._fallback_count = 0
        self._reconcile_metrics = ReconcileMetrics()
        self._usage = Usage()

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
            # F10 — cooperative cancellation between pages, before any work
            # on this page and before any output is written.
            if should_abort is not None and should_abort():
                raise CorrectionAborted(
                    f"run aborted before page {page.page_id!r} (page {page.page_index})"
                )

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
                should_abort=should_abort,
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
            apply=apply,
        )

        report = CorrectionReport(
            run_id=run_id,
            total_lines=len(traces),
            lines=list(traces.values()),
        )

        return CorrectionResult(
            total_chunks=total_chunks,
            total_reconciled=total_reconciled,
            retry_count=self._retry_count,
            fallback_count=self._fallback_count,
            traces=traces,
            reconcile_metrics=self._reconcile_metrics,
            usage=self._usage,
            report=report,
        )

    def run_sync(
        self,
        *,
        document_manifest: DocumentManifest,
        api_key: str,
        model: str,
        provider_name: str,
        source_files: dict[str, Path],
        run_id: str | None = None,
        should_abort: Callable[[], bool] | None = None,
        apply: bool = True,
    ) -> CorrectionResult:
        """Synchronous façade over :meth:`run` (§8.1).

        Wraps the coroutine in :func:`asyncio.run` for consumers without an
        event loop (scripts, notebooks, CLIs). Same parameters and return
        value as :meth:`run`. Must NOT be called from within a running
        event loop — ``asyncio.run`` raises ``RuntimeError`` there; use
        ``await pipeline.run(...)`` instead.
        """
        return asyncio.run(
            self.run(
                document_manifest=document_manifest,
                api_key=api_key,
                model=model,
                provider_name=provider_name,
                source_files=source_files,
                run_id=run_id,
                should_abort=should_abort,
                apply=apply,
            )
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
        should_abort: Callable[[], bool] | None = None,
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
            # F10 — cooperative cancellation between chunks. Checked before
            # the per-chunk try/except so CorrectionAborted propagates out
            # instead of being swallowed as a chunk error.
            if should_abort is not None and should_abort():
                raise CorrectionAborted(
                    f"run aborted before chunk {chunk.chunk_id!r} on page "
                    f"{page.page_id!r}"
                )

            page_chunks += 1
            try:
                n = await self._run_chunk(
                    chunk=chunk,
                    page=page,
                    line_by_id=line_by_id,
                    api_key=api_key,
                    model=model,
                    provider_name=provider_name,
                    traces=traces,
                    cross_page_partners=cross_page_partners,
                    should_abort=should_abort,
                )
                page_reconciled += n
            except CorrectionAborted:
                # F10 — the descent-level probe raises inside _run_chunk;
                # cancellation must propagate, never be downgraded to a
                # chunk_error event.
                raise
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
        page: PageManifest,
        line_by_id: dict[str, LineManifest],
        api_key: str,
        model: str,
        provider_name: str,
        traces: dict[str, LineTrace] | None = None,
        cross_page_partners: dict[tuple[str, str], LineManifest] | None = None,
        budget: list[int] | None = None,
        should_abort: Callable[[], bool] | None = None,
    ) -> int:
        """Process one chunk through the LLM, with F1 granularity descent.

        On success: reconcile + accept + finalize, return reconciled pairs.

        On retry-budget exhaustion at a granularity coarser than LINE:
        emit ``chunk_downgraded`` and re-plan **this chunk's TARGET lines**
        one granularity finer (PAGE→BLOCK→WINDOW→LINE), retrying each
        sub-chunk. Context lines (F8) are NOT re-planned — they belong to
        an adjacent chunk, and correcting them here at a finer grain would
        steal ownership from the window where their context is maximal.
        Only lines whose LINE-level chunk still fails — or that run out of
        the shared ``RetryPolicy.per_chunk_budget`` (default 6 cumulative
        attempts) — fall back to OCR source. A non-retryable error (e.g.
        HTTP 4xx) skips the descent and falls back immediately: smaller
        chunks would hit the same wall.

        ``budget`` is a 1-element list holding the remaining cumulative
        attempts for this original chunk's whole descent; ``None`` at the
        top level starts a fresh budget. ``should_abort`` (F10) is probed
        before each sub-chunk of the descent — a long PAGE→…→LINE cascade
        stays cancellable.
        """
        chunk_lines = [line_by_id[lid] for lid in chunk.line_ids if lid in line_by_id]
        if not chunk_lines:
            return 0

        if budget is None:
            budget = [self.retry_policy.per_chunk_budget]

        hyphen_pairs = _build_hyphen_pairs(chunk_lines)

        self.observer.on_event(
            PipelineEventType.CHUNK_STARTED,
            {
                "chunk_id": chunk.chunk_id,
                "granularity": chunk.granularity.value,
                "line_count": len(chunk_lines),
            },
        )

        attempts_cap = min(self.retry_policy.max_attempts, max(budget[0], 0))
        (
            response,
            attempts_used,
            can_downgrade,
            last_msg,
            usage,
        ) = await self._attempt_chunk(
            chunk=chunk,
            chunk_lines=chunk_lines,
            hyphen_pairs=hyphen_pairs,
            all_lines_by_id=line_by_id,
            api_key=api_key,
            model=model,
            traces=traces,
            max_attempts=attempts_cap,
        )
        budget[0] -= attempts_used

        if response is not None:
            return self._finish_successful_chunk(
                chunk=chunk,
                chunk_lines=chunk_lines,
                response=response,
                line_by_id=line_by_id,
                cross_page_partners=cross_page_partners,
                traces=traces,
                usage=usage,
            )

        # --- Failure: try a granularity descent (F1). ---
        next_g = downgrade_granularity(chunk.granularity)
        if can_downgrade and next_g is not None and budget[0] > 0:
            # F1×F8 — only the chunk's TARGET lines descend. Context lines
            # are owned by an adjacent chunk; re-planning them here would
            # correct them at a finer grain and make their rightful window
            # skip them (acceptance ignores already-corrected lines).
            target_ids = set(chunk.targets())
            descent_lines = [lm for lm in chunk_lines if lm.line_id in target_ids]
            self.observer.on_event(
                PipelineEventType.CHUNK_DOWNGRADED,
                {
                    "chunk_id": chunk.chunk_id,
                    "from_granularity": chunk.granularity.value,
                    "to_granularity": next_g.value,
                    "line_count": len(chunk_lines),
                    "target_count": len(descent_lines),
                    "budget_remaining": budget[0],
                },
            )
            sub_plan = plan_page(
                _subpage_for_lines(page, descent_lines),
                chunk.document_id,
                self.config,
                force_granularity=next_g,
            )
            total = 0
            for sub in sub_plan.chunks:
                # F10 — the descent can spawn many finest-grain chunks;
                # keep the run cancellable inside it, not only between
                # top-level chunks.
                if should_abort is not None and should_abort():
                    raise CorrectionAborted(
                        f"run aborted during granularity descent of chunk "
                        f"{chunk.chunk_id!r} on page {page.page_id!r}"
                    )
                if budget[0] <= 0:
                    # Budget spent mid-descent: OCR-fallback the rest.
                    sub_lines = [
                        line_by_id[lid] for lid in sub.line_ids if lid in line_by_id
                    ]
                    self._apply_chunk_fallback(
                        chunk=sub,
                        chunk_lines=sub_lines,
                        traces=traces,
                        sanitised_msg=last_msg or "per_chunk_budget exhausted",
                    )
                    self._fallback_count += 1
                    continue
                total += await self._run_chunk(
                    chunk=sub,
                    page=page,
                    line_by_id=line_by_id,
                    api_key=api_key,
                    model=model,
                    provider_name=provider_name,
                    traces=traces,
                    cross_page_partners=cross_page_partners,
                    budget=budget,
                    should_abort=should_abort,
                )
            return total

        # --- Terminal fallback (LINE grain, budget gone, or hard error). ---
        self._apply_chunk_fallback(
            chunk=chunk,
            chunk_lines=chunk_lines,
            traces=traces,
            sanitised_msg=last_msg or "all_attempts_exhausted",
        )
        self._fallback_count += 1
        return 0

    def _finish_successful_chunk(
        self,
        *,
        chunk: ChunkRequest,
        chunk_lines: list[LineManifest],
        response: LLMResponse,
        line_by_id: dict[str, LineManifest],
        cross_page_partners: dict[tuple[str, str], LineManifest] | None,
        traces: dict[str, LineTrace] | None,
        usage: Usage | None = None,
    ) -> int:
        """Reconcile / accept / finalize a chunk whose LLM call succeeded.

        F8 — only the chunk's *target* lines are corrected here. Context
        lines (in ``line_ids`` but not ``target_line_ids``) were sent to the
        producer for context but are owned by an adjacent chunk, so their
        output is discarded on this pass.
        """
        text_by_id: dict[str, str] = {
            o.line_id: o.corrected_text for o in response.lines
        }

        target_ids = set(chunk.targets())
        target_lines = [lm for lm in chunk_lines if lm.line_id in target_ids]

        reconciled_count = self._reconcile_chunk_hyphens(
            chunk_id=chunk.chunk_id,
            chunk_lines=target_lines,
            text_by_id=text_by_id,
            line_by_id=line_by_id,
            cross_page_partners=cross_page_partners,
        )
        self._apply_line_acceptance(
            chunk_lines=target_lines,
            text_by_id=text_by_id,
            all_lines_by_id=line_by_id,
            traces=traces,
        )
        self._finalize_chunk_traces(
            chunk_lines=target_lines,
            traces=traces,
        )

        self.observer.on_event(
            PipelineEventType.CHUNK_COMPLETED,
            {
                "chunk_id": chunk.chunk_id,
                "line_count": len(chunk_lines),
                "target_count": len(target_lines),
                "hyphen_pairs_reconciled": reconciled_count,
                # F14 — token usage for this chunk's producer call (0 when
                # the provider did not report it).
                "input_tokens": usage.input_tokens if usage else 0,
                "output_tokens": usage.output_tokens if usage else 0,
            },
        )
        return reconciled_count

    def _apply_chunk_fallback(
        self,
        *,
        chunk: ChunkRequest,
        chunk_lines: list[LineManifest],
        traces: dict[str, LineTrace] | None,
        sanitised_msg: str,
    ) -> None:
        """Revert the chunk's TARGET lines to their OCR text and emit a
        ``warning`` event. Mutates ``corrected_text`` / ``status`` /
        line traces. Called once the retry loop exhausts its budget or
        hits a non-retryable error.

        F8 — only target lines are reverted; context lines are owned by an
        adjacent chunk and must not be forced to OCR here.

        The pipeline-level ``_fallback_count`` is bumped by the caller,
        mirroring how ``_retry_count`` is incremented at the retry
        call site — both counters are pipeline-orchestration state, not
        chunk-level side effects.
        """
        self.observer.on_event(
            PipelineEventType.WARNING,
            {
                "chunk_id": chunk.chunk_id,
                "message": f"Fallback to OCR source: {sanitised_msg[:120]}",
            },
        )
        target_ids = set(chunk.targets())
        for lm in chunk_lines:
            if lm.line_id not in target_ids:
                continue
            lm.corrected_text = lm.ocr_text
            lm.status = LineStatus.FALLBACK
            _set_trace(
                traces,
                lm,
                projected_text=lm.ocr_text,
                validation_status="fallback",
                fallback_reason=f"all_attempts_exhausted: {sanitised_msg[:120]}",
            )

    async def _attempt_chunk(
        self,
        *,
        chunk: ChunkRequest,
        chunk_lines: list[LineManifest],
        hyphen_pairs: dict[str, str],
        all_lines_by_id: dict[str, LineManifest],
        api_key: str,
        model: str,
        traces: dict[str, LineTrace] | None,
        max_attempts: int,
    ) -> tuple[LLMResponse | None, int, bool, str, Usage | None]:
        """Call the LLM provider with retries; return the outcome.

        Returns ``(response, attempts_used, can_downgrade, last_msg, usage)``:
          - ``response`` — the validated :class:`LLMResponse`, or ``None``
            on failure;
          - ``attempts_used`` — how many attempts this call consumed
            (charged against the per-chunk budget by the caller);
          - ``can_downgrade`` — on failure, ``True`` when the terminal
            error was retryable (malformed output / transient) and hence
            worth retrying at a finer granularity (F1); ``False`` for a
            non-retryable hard error (e.g. 4xx), which won't heal on
            smaller chunks;
          - ``last_msg`` — the sanitised terminal error message.

        This method NEVER applies the OCR fallback — that decision (and
        the ``warning`` event) belongs to the caller (:meth:`_run_chunk`),
        which may instead downgrade the granularity.

        Retry strategy (F9): up to ``max_attempts`` attempts (bounded by
        the caller to the remaining budget); temperature from
        ``retry_policy.temperatures`` (default 0.0 → 0.3 → 0.5), pinned at
        0.0 after a ``HyphenIntegrityError``; backoff 0 s for the first
        hyphen violation, ``attempt * transient_backoff_base`` for
        transient HTTP, ``attempt * output_backoff_base`` for other
        malformed output. Each retry emits a ``retry`` event.
        """
        hyphen_violation = False
        attempts_used = 0
        last_msg = ""
        # F14 — token usage accumulated across EVERY call of this chunk's
        # attempt loop, including calls whose response later failed
        # validation (tokens were spent regardless). Returned on success so
        # the chunk_completed event reports the chunk's true total, not
        # just the final successful call.
        chunk_usage = Usage()

        for attempt in range(1, max_attempts + 1):
            attempts_used = attempt
            # F9 — temperature comes from the injected RetryPolicy. A hyphen
            # violation still pins the next attempt to 0.0 (the LLM mishandled
            # the pair; a colder attempt sticks closer to source).
            if hyphen_violation:
                temperature = 0.0
            else:
                temperature = self.retry_policy.temperature_for(attempt)

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
                raw, usage = await self.provider.complete_structured(
                    api_key=api_key,
                    model=model,
                    system_prompt=SYSTEM_PROMPT,
                    user_payload=user_dict,
                    json_schema=OUTPUT_JSON_SCHEMA,
                    temperature=temperature,
                )
                if usage is not None:
                    self._usage = self._usage + usage
                    chunk_usage = chunk_usage + usage

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
                    guard_config=self.guard_config,
                    # F8 — the 1:1 count is enforced on targets; a missing
                    # context line's output is not an error (it belongs to
                    # an adjacent chunk).
                    target_line_ids=chunk.target_line_ids,
                )
                return response, attempts_used, False, "", chunk_usage

            except Exception as exc:
                msg = sanitize_error(str(exc), api_key)
                last_msg = msg
                decision = _classify_retry(
                    exc=exc,
                    sanitised_msg=msg,
                    attempt=attempt,
                    hyphen_already_seen=hyphen_violation,
                    policy=self.retry_policy,
                )

                if attempt < max_attempts and decision.is_retryable:
                    if decision.is_hyphen_violation:
                        hyphen_violation = True
                    if decision.backoff > 0:
                        await asyncio.sleep(decision.backoff)
                    self.observer.on_event(
                        PipelineEventType.RETRY,
                        {
                            "chunk_id": chunk.chunk_id,
                            "attempt": attempt,
                            "error": decision.error_tag,
                        },
                    )
                    self._retry_count += 1
                    continue

                # Attempts exhausted (or non-retryable error class). Do NOT
                # fall back here — the caller decides between a granularity
                # downgrade (F1) and the OCR fallback. ``can_downgrade`` is
                # True only when the terminal error was retryable.
                return None, attempts_used, decision.is_retryable, msg, None

        # max_attempts <= 0 (no budget left): nothing attempted.
        return None, attempts_used, False, last_msg, None

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
            outcome = _reconcile_one_pair(
                lm, part2, text_by_id, is_forward=False, config=self.guard_config
            )
            self._record_reconcile_outcome(outcome)
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
            outcome = _reconcile_one_pair(
                lm, part2, text_by_id, is_forward=True, config=self.guard_config
            )
            self._record_reconcile_outcome(outcome)
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
            result = check_line(
                lm.ocr_text, corrected, prev_ocr, next_ocr, config=self.guard_config
            )
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
        dup_reverts = check_adjacent_duplicates(
            accepted_lines, config=self.guard_config
        )
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
        apply: bool = True,
    ) -> None:
        """Rewrite corrected ALTO files, update traces, and (when ``apply``)
        persist via the writer.

        §9 dry-run — the rewrite always runs in memory so the report's
        ``rewriter_path`` / ``output_alto_text`` are populated, but when
        ``apply`` is ``False`` the injected ``OutputWriter`` is never
        called: nothing is persisted.
        """
        # §11 — provenance stamped into every corrected file's processingStep.
        from corrigenda import __version__ as _lib_version

        config_fingerprint = self.config_fingerprint()

        for source_name, xml_path in source_files.items():
            pages_for_file = [
                p for p in document_manifest.pages if p.source_file == source_name
            ]
            if not pages_for_file:
                continue

            xml_bytes, metrics, rewriter_paths = rewrite_alto_file(
                xml_path,
                pages_for_file,
                provider_name,
                model,
                lib_version=_lib_version,
                config_fingerprint=config_fingerprint,
            )
            if apply:
                self.output_writer.write_corrected(
                    source_stem=xml_path.stem,
                    xml_bytes=xml_bytes,
                )
            # rewriter_stats observability event — pure read-only diagnostic
            # surfacing how each line classified (UNTOUCHED / SUBS_ONLY /
            # FAST_PATH / SLOW_PATH). Zero impact on the corrected XML.
            self.observer.on_event(
                PipelineEventType.REWRITER_STATS,
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

        if apply:
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
