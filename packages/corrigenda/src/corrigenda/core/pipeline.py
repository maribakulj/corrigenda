"""Pure correction pipeline.

The pipeline takes a parsed :class:`DocumentManifest`, drives the chunk
planner, asks the injected :class:`EditProducer` for an
:class:`EditScript` per chunk, validates the result, reconciles hyphen
pairs, and writes outputs via the injected :class:`OutputWriter`. It
depends only on the Protocols in :mod:`corrigenda.core.protocols` — no
job store, no FastAPI, no filesystem path manipulation beyond reading
source files. Credentials never reach the pipeline: an LLM's API key
lives inside its producer (see :class:`LLMEditProducer` and the
:meth:`CorrectionPipeline.for_provider` convenience).

Side effects:
  - producer calls via :class:`EditProducer` (LLM HTTP, rules engine, …)
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
from dataclasses import dataclass, field
from typing import Any
from pathlib import Path

from corrigenda.core.editing import (
    EditOp,
    EditScript,
    ReplaceLine,
    ReplaceSpan,
    apply_edit_script,
)
from corrigenda.core.hyphenation import (
    ReconcileMetrics,
    classify_reconcile_outcome,
    enrich_chunk_lines,
    reconcile_hyphen_pair,
)
from corrigenda.core.identity import (
    ensure_unique_identities,
    ensure_unique_page_ids_across_files,
)
from corrigenda.errors import CorrectionAborted, CorrectionError
from corrigenda.core.planner import downgrade_granularity, plan_page
from corrigenda.core.guards import check_adjacent_duplicates, check_line
from corrigenda.core.validator import HyphenIntegrityError, validate_llm_response
from corrigenda.core.protocols import (
    BaseProvider,
    EditProducer,
    FormatAdapter,
    OutputWriter,
    PipelineObserver,
    ProviderPermanentError,
    ProviderTransientError,
    require_source_images,
)
from corrigenda.core.schemas import (
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
    ImageRef,
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

# Audit P3 — genuine programming-error types that must FAIL the run rather
# than degrade silently to OCR fallback on the producer-attempt path.
# Deliberately EXCLUDES ValueError (JSON/validation/parse errors are
# expected producer-output failures) and any provider transport error
# (httpx / SDK exceptions the provider-agnostic pipeline cannot name),
# which stay recoverable.
_PROGRAMMING_ERROR_TYPES: tuple[type[BaseException], ...] = (
    TypeError,
    AttributeError,
    KeyError,
    IndexError,
    NameError,
    UnboundLocalError,
    AssertionError,
    NotImplementedError,
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


def _default_format_adapter() -> FormatAdapter:
    """Composition-boundary default: ALTO, resolved lazily (§3).

    This function is the ONLY place ``core`` touches a concrete format,
    and the import is function-local so importing any ``corrigenda.core``
    module never loads lxml. The import-contract test pins both facts:
    core modules carry no static formats/lxml import, and this exact
    function is the single allowed lazy site. Inject ``format_adapter``
    on the pipeline to use another format (e.g. PAGE XML).
    """
    from corrigenda.formats.alto.adapter import AltoFormatAdapter

    return AltoFormatAdapter()


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
    #: §4 — the normalized EditScript the run applied, accumulated across
    #: chunks. In v1 the LLM path emits ``replace_line`` ops (byte-identical
    #: to the direct correction); a dry run (``apply=False``) returns it as
    #: the whole deliverable, and a rules/​span producer would surface its
    #: ``replace_span`` ops here too.
    edit_script: EditScript


@dataclass
class RunContext:
    """All mutable state of ONE pipeline execution (Plan V4.1-L).

    Created fresh at the top of every :meth:`CorrectionPipeline.run` and
    threaded through the internal methods, so ``CorrectionPipeline``
    itself carries only immutable configuration and injected
    dependencies. Nothing here survives the run: the public outcome is
    copied into :class:`CorrectionResult` before returning.

    Not exported: this is internal orchestration state, not API surface.
    """

    #: Retries consumed across every chunk's attempt loop.
    retry_count: int = 0
    #: Chunks (or descent sub-chunks) that fell back to OCR source text.
    fallback_count: int = 0
    #: Per-pair reconciliation outcomes (coherent / fallback / neutralised).
    reconcile_metrics: ReconcileMetrics = field(default_factory=ReconcileMetrics)
    #: Aggregate token consumption across every producer call of the run.
    usage: Usage = field(default_factory=Usage)
    #: §4 — per target line, the producer's ops (a line may carry several,
    #: e.g. one replace_span per occurrence) and the text those ops
    #: produced (pre-guard, pre-reconcile). Consumed by
    #: _build_final_edit_script to emit the ops the run ACTUALLY applied.
    #: Keyed by (page_id, line_id): bare line_ids may legitimately repeat
    #: across FILES (only page_ids are unique document-wide), and a
    #: bare-id key would let the last file's ops overwrite an earlier
    #: file's, corrupting the dry-run edit_script.
    producer_ops: dict[tuple[str, str], tuple[list[EditOp], str]] = field(
        default_factory=dict
    )
    #: Per-line PRE-REVERT accepted correction, keyed by _trace_key. The
    #: cross-chunk boundary pass and the page-seam pass compare against
    #: THIS snapshot (like the intra-chunk pass does via its local
    #: `accepted_lines`): reading the live corrected_text after an
    #: earlier revert would mask the third line of an identical-
    #: correction run straddling a chunk/page seam.
    accepted_snapshot: dict[str, str] = field(default_factory=dict)
    #: Which finalization pass ACTUALLY owned each line (keyed by
    #: _trace_key). Granularity descent spawns sub-chunks the plan never
    #: listed, so the boundary pass must derive its seams from these
    #: owners, not from plan.chunks.
    finalized_owner: dict[str, int] = field(default_factory=dict)
    finalize_seq: int = 0
    #: §4.1 vision envelope — resolved once per run from run(source_images=…).
    image_ref_by_page_id: dict[str, ImageRef] = field(default_factory=dict)
    page_dims: dict[str, tuple[int, int]] = field(default_factory=dict)


class CorrectionPipeline:
    """Pure orchestration of the correction pipeline over an EditProducer.

    Dependencies are injected via the constructor; the pipeline never
    reaches for global state. The instance holds only immutable
    configuration (V4.1-L): every run creates a fresh :class:`RunContext`
    for its mutable state, and the stats it accumulates are exposed in
    the final `CorrectionResult` for the caller to persist.

    §5.1 resorption — the pipeline is constructed around an
    :class:`EditProducer`; there is no ``api_key``/``model`` anywhere on
    the pipeline surface. For the common LLM case, use
    :meth:`for_provider`, which wraps a :class:`BaseProvider` +
    credentials into an ``LLMEditProducer`` and sets the provenance
    labels in one call.
    """

    def __init__(
        self,
        producer: EditProducer,
        observer: PipelineObserver,
        output_writer: OutputWriter,
        config: ChunkPlannerConfig | None = None,
        retry_policy: RetryPolicy | None = None,
        guard_config: GuardConfig | None = None,
        pairing_policy: PairingPolicy | None = None,
        format_adapter: FormatAdapter | None = None,
        *,
        provider_name: str = "unknown",
        model: str = "unknown",
    ) -> None:
        self.producer = producer
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
        # §3 format seam — None resolves to the lazy ALTO default at
        # write time (_default_format_adapter); inject for other formats.
        self.format_adapter = format_adapter
        # §11 — provenance labels stamped into the corrected XML's
        # processingStep. Pure strings: the pipeline never dials a vendor.
        self.provider_name = provider_name
        self.model = model
        # Reentrancy guard. Per-run state lives in RunContext (V4.1-L),
        # but the injected observer and output_writer are shared instance
        # dependencies: two concurrent runs would interleave their events
        # and overwrite each other's outputs (write_trace has no run
        # discriminator). One instance therefore still means one run at
        # a time; concurrent callers build one pipeline per run.
        self._running = False

    @classmethod
    def for_provider(
        cls,
        provider: BaseProvider,
        *,
        api_key: str,
        model: str,
        provider_name: str = "unknown",
        observer: PipelineObserver,
        output_writer: OutputWriter,
        config: ChunkPlannerConfig | None = None,
        retry_policy: RetryPolicy | None = None,
        guard_config: GuardConfig | None = None,
        pairing_policy: PairingPolicy | None = None,
        format_adapter: FormatAdapter | None = None,
        system_prompt: str | None = None,
        output_schema: dict[str, Any] | None = None,
    ) -> CorrectionPipeline:
        """Build a pipeline around a raw LLM ``BaseProvider`` (§5.1).

        Composition-boundary convenience: wraps the provider + credentials
        + prompt contract into an ``LLMEditProducer`` so callers migrating
        from the legacy ``run(api_key=…, model=…, provider_name=…)`` keep a
        one-call setup. The import is function-local — this is one of the
        two pinned lazy composition defaults the import-contract test
        allows in core (the other is the ALTO format adapter).
        """
        from corrigenda.producers.llm_edit import LLMEditProducer

        producer = LLMEditProducer(
            provider,
            api_key,
            model,
            system_prompt=system_prompt,
            output_schema=output_schema,
        )
        return cls(
            producer=producer,
            observer=observer,
            output_writer=output_writer,
            config=config,
            retry_policy=retry_policy,
            guard_config=guard_config,
            pairing_policy=pairing_policy,
            format_adapter=format_adapter,
            provider_name=provider_name,
            model=model,
        )

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

    @staticmethod
    def _record_reconcile_outcome(ctx: RunContext, outcome: str) -> None:
        """Bump the run's ReconcileMetrics counter for a single pair."""
        if outcome == "coherent":
            ctx.reconcile_metrics.coherent += 1
        elif outcome == "fallback":
            ctx.reconcile_metrics.fallback += 1
        elif outcome == "neutralised":
            ctx.reconcile_metrics.neutralised += 1

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(
        self,
        *,
        document_manifest: DocumentManifest,
        source_files: dict[str, Path],
        run_id: str | None = None,
        should_abort: Callable[[], bool] | None = None,
        apply: bool = True,
        source_images: dict[str, ImageRef] | None = None,
    ) -> CorrectionResult:
        """Run the full pipeline. Mutates `document_manifest.pages` in place.

        **Concurrency contract (Plan V4.1)** — one instance, one run at
        a time. All per-run state lives in a fresh :class:`RunContext`
        created here (V4.1-L: the pipeline instance itself carries only
        immutable configuration), but the injected ``observer`` and
        ``output_writer`` are shared instance dependencies: two
        concurrent runs would interleave their events and overwrite each
        other's outputs. Guarded: a concurrent call raises
        :class:`RuntimeError` immediately. Instances are not
        thread-safe; concurrent callers build one pipeline per run.
        The input manifest is CONSUMED (mutated in place); re-running on
        the same manifest starts from the previous run's corrected
        state, not from the original OCR text. Sequential re-use of one
        instance on fresh manifests is supported.

        §5.1 resorption — there is no ``api_key``/``model``/``provider_name``
        here anymore: credentials and the vendor call live inside the
        injected :class:`EditProducer` (see :meth:`for_provider`), and the
        provenance labels are constructor state.

        ``source_images`` (§5.1) — optional mapping of *source name* (the
        same keys as ``source_files``) to an opaque :data:`ImageRef`. The
        library forwards each page's ref verbatim into the producer payload
        when the producer asks (``wants_image``) and NEVER opens it (I4).
        A ``wants_image`` producer run without a complete mapping raises
        :class:`ValidationError` before any work starts.

        ``run_id`` is an optional identifier embedded in the emitted
        :class:`CorrectionReport` (which is also what ``trace.json``
        contains) so consumers can correlate the persisted report with
        their own job/request id. Generated as a uuid4 when omitted; it
        never leaks back into the public events.

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
        if self._running:
            raise RuntimeError(
                "CorrectionPipeline.run() is already executing on this instance — "
                "one instance supports one run at a time (per-run state lives on "
                "the instance). Create a separate pipeline per concurrent run."
            )
        self._running = True
        try:
            return await self._run_exclusive(
                document_manifest=document_manifest,
                source_files=source_files,
                run_id=run_id,
                should_abort=should_abort,
                apply=apply,
                source_images=source_images,
            )
        finally:
            self._running = False

    async def _run_exclusive(
        self,
        *,
        document_manifest: DocumentManifest,
        source_files: dict[str, Path],
        run_id: str | None,
        should_abort: Callable[[], bool] | None,
        apply: bool,
        source_images: dict[str, ImageRef] | None,
    ) -> CorrectionResult:
        """Body of :meth:`run`, executing under the reentrancy guard."""
        run_id = run_id or str(uuid.uuid4())
        # V4.1-L — one fresh context per execution; no per-run state
        # remains on the instance.
        ctx = RunContext()

        # §5.1 — a vision producer without its images is a start-up error,
        # never a silent image-less call.
        require_source_images(self.producer, list(source_files.keys()), source_images)

        # P0-5 — identity-uniqueness invariant, enforced at the pipeline
        # door so hand-built manifests get the same guarantee as
        # parser-built ones: within one source file every page/block/line
        # ID must be unique (correction-to-line association is keyed by
        # bare line_id per file), and page_ids must be unique across the
        # whole document (trace keys, per-page image/dimension lookups).
        pages_by_file: dict[str, list[PageManifest]] = {}
        for page in document_manifest.pages:
            pages_by_file.setdefault(page.source_file, []).append(page)
        for src_name, src_pages in pages_by_file.items():
            ensure_unique_identities(src_pages, src_name)
        ensure_unique_page_ids_across_files(document_manifest.pages)
        # §4.1 — per-page vision envelope lookups, resolved once. Pure
        # copying: the ImageRef stays an opaque string end to end.
        images = source_images or {}
        ctx.image_ref_by_page_id = {
            page.page_id: images[page.source_file]
            for page in document_manifest.pages
            if page.source_file in images
        }
        ctx.page_dims = {
            page.page_id: (page.page_width, page.page_height)
            for page in document_manifest.pages
        }

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
                ctx=ctx,
                page=page,
                document_id=document_manifest.document_id,
                traces=traces,
                cross_page_partners=cross_page if cross_page else None,
                should_abort=should_abort,
            )
            total_chunks += page_chunks
            total_reconciled += page_reconciled

        # Review fix (P2-6, one level up): a duplication straddling a PAGE
        # boundary was still invisible — chunk plans and the page-level
        # pass are both page-scoped, while page-boundary lines DO see each
        # other through cross-page hyphen context. Check each page seam
        # explicitly (O(#pages), one pair per seam). The lookup is built
        # per seam, never document-wide: bare line_ids may legitimately
        # repeat across FILES, and a global bare-id dict is the exact
        # ambiguity P0-5 bans — an ambiguous seam is skipped instead.
        for prev_page, next_page in zip(
            document_manifest.pages, document_manifest.pages[1:]
        ):
            if not prev_page.lines or not next_page.lines:
                continue
            # Audit P2 — only compare a seam WITHIN one source file. Pages
            # of different files are concatenated in document_manifest,
            # so without this guard the last physical line of file A was
            # compared against the first line of file B as if adjacent,
            # and could be spuriously reverted as a "duplicate".
            if prev_page.source_file != next_page.source_file:
                continue
            seam_map = {lm.line_id: lm for lm in (*prev_page.lines, *next_page.lines)}
            if len(seam_map) != len(prev_page.lines) + len(next_page.lines):
                continue  # cross-file line_id reuse → ambiguous, skip
            a, b = prev_page.lines[-1], next_page.lines[0]
            # Audit-F3 (twin branch) — same pre-revert snapshot basis as
            # the cross-chunk boundary pass: an intra-page revert of the
            # seam line otherwise masked the third member of a run
            # straddling the page seam.
            seam_reverts = check_adjacent_duplicates(
                [
                    (
                        lm.line_id,
                        lm.ocr_text,
                        ctx.accepted_snapshot.get(
                            _trace_key(lm),
                            lm.corrected_text
                            if lm.corrected_text is not None
                            else lm.ocr_text,
                        ),
                    )
                    for lm in (a, b)
                ],
                config=self.guard_config,
            )
            self._apply_duplicate_reverts(
                reverts=seam_reverts,
                traces=traces,
                line_by_id=seam_map,
                # A seam line's hyphen partner may live on a THIRD page
                # (outside the two-page seam_map); the document-wide,
                # page-qualified index reaches it so the pair reverts
                # atomically.
                cross_page_partners=all_lines_global,
            )

        await self._write_outputs(
            document_manifest=document_manifest,
            source_files=source_files,
            traces=traces,
            apply=apply,
        )

        report = CorrectionReport(
            run_id=run_id,
            total_lines=len(traces),
            lines=list(traces.values()),
        )

        # §9 unification — trace.json IS the CorrectionReport. One artefact,
        # one versioned schema; the parallel JobTrace shape is gone.
        if apply:
            self.output_writer.write_trace(
                traces_payload=report.model_dump_json(indent=2),
            )

        return CorrectionResult(
            total_chunks=total_chunks,
            total_reconciled=total_reconciled,
            retry_count=ctx.retry_count,
            fallback_count=ctx.fallback_count,
            traces=traces,
            reconcile_metrics=ctx.reconcile_metrics,
            usage=ctx.usage,
            report=report,
            edit_script=self._build_final_edit_script(document_manifest, ctx),
        )

    def _build_final_edit_script(
        self, document_manifest: DocumentManifest, ctx: RunContext
    ) -> EditScript:
        """§4 — the EditScript the run *actually applied*, in document order.

        Reconciles the captured producer ops against the FINAL per-line
        state, after reconciliation, the acceptance guard, and every
        duplicate/seam revert have run. It therefore never carries an op
        for a line that was reverted to OCR or reconciled to different text
        (Audit P2 — a dry-run consumer replaying it would otherwise diverge
        from the pipeline's own corrected XML):

        - line not ``CORRECTED`` (fallback / failed / pending) → no op;
        - ``CORRECTED`` and the producer's op output survived unchanged →
          the producer's original op, preserving its TYPE (e.g. a rules
          producer's ``replace_span``);
        - ``CORRECTED`` but the final text differs from the op output
          (a reconciled hyphen member) → a ``replace_line`` carrying the
          final ``corrected_text``, since the original span no longer
          describes it.
        """
        ops: list[EditOp] = []
        for page in document_manifest.pages:
            for lm in page.lines:
                if lm.status is not LineStatus.CORRECTED or lm.corrected_text is None:
                    continue
                captured = ctx.producer_ops.get((lm.page_id, lm.line_id))
                if captured is None:
                    # An accepted line the producer left untouched (no op) —
                    # e.g. a rules producer's uncovered line. Nothing applied.
                    continue
                line_ops, produced_text = captured
                if produced_text == lm.corrected_text:
                    # The producer's output survived every guard unchanged —
                    # keep its original ops (and their TYPE, e.g. span),
                    # stamped with the page_id so a consumer can attribute
                    # them per file (wave-1 review, F4 residual: bare
                    # line_ids repeat across files).
                    ops.extend(
                        op.model_copy(update={"page_id": lm.page_id}) for op in line_ops
                    )
                else:
                    # A guard / the reconciler rewrote the final text; the
                    # original ops no longer describe it.
                    ops.append(
                        ReplaceLine(
                            line_id=lm.line_id,
                            text=lm.corrected_text,
                            page_id=lm.page_id,
                        )
                    )
        return EditScript(ops=ops)

    def run_sync(
        self,
        *,
        document_manifest: DocumentManifest,
        source_files: dict[str, Path],
        run_id: str | None = None,
        should_abort: Callable[[], bool] | None = None,
        apply: bool = True,
        source_images: dict[str, ImageRef] | None = None,
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
                source_files=source_files,
                run_id=run_id,
                should_abort=should_abort,
                apply=apply,
                source_images=source_images,
            )
        )

    # ------------------------------------------------------------------
    # Per-page orchestration
    # ------------------------------------------------------------------

    async def _process_page(
        self,
        *,
        ctx: RunContext,
        page: PageManifest,
        document_id: str,
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
                    ctx=ctx,
                    chunk=chunk,
                    page=page,
                    line_by_id=line_by_id,
                    traces=traces,
                    cross_page_partners=cross_page_partners,
                    should_abort=should_abort,
                )
                page_reconciled += n
            except (CorrectionAborted, ProviderPermanentError):
                # F10 — cancellation must propagate, never be downgraded
                # to a chunk_error event. P0-1 — a permanent provider
                # rejection (401/403/404) is fatal for the whole run: it
                # would hit every remaining chunk identically, and
                # converting it into per-chunk OCR fallbacks would let the
                # run END AS A SUCCESS with silently uncorrected text.
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
                # P0-2 — only RECOVERABLE domain errors may be absorbed as
                # a chunk_error + continue. Anything else (KeyError,
                # AttributeError, a pydantic bug, a broken invariant) is a
                # programming error: continuing would let the run complete
                # "successfully" with lines in an unknown state.
                if not isinstance(exc, CorrectionError):
                    raise

        # P2-6 — cross-chunk adjacency pass. Per-chunk finalization only
        # sees that chunk's TARGET lines, so two document-adjacent lines
        # owned by different chunks were never compared: a duplication
        # straddling a chunk boundary escaped the guard entirely. Only
        # the boundary pairs are new — intra-chunk pairs were already
        # checked with the same function and config — so the pass is
        # restricted to adjacent pairs whose owners differ (review fix:
        # re-checking whole pages re-ran SequenceMatcher over every
        # already-checked pair for nothing).
        #
        # Wave-1 review — the owners are the finalization passes that
        # ACTUALLY ran (ctx.finalized_owner), not the planned chunks:
        # granularity descent finalizes a planned chunk as many
        # sub-chunks, whose seams the plan-derived map could not see —
        # and a single-chunk plan (`len(plan.chunks) > 1` gate) skipped
        # the pass outright. Lines that never finalized (full-chunk OCR
        # fallback) share owner None; such pairs sit at source text on
        # both sides, where a revert would be a no-op anyway.
        boundary_reverts: dict[str, str] = {}
        for a, b in zip(page.lines, page.lines[1:]):
            if ctx.finalized_owner.get(_trace_key(a)) == ctx.finalized_owner.get(
                _trace_key(b)
            ):
                continue
            # Audit-F3 — compare the PRE-REVERT accepted corrections
            # (snapshotted in _finalize_chunk_traces), not the live
            # corrected_text: an intra-chunk revert of the boundary
            # line otherwise masked the third member of an
            # identical-correction run straddling the boundary.
            pair = [
                (
                    lm.line_id,
                    lm.ocr_text,
                    ctx.accepted_snapshot.get(
                        _trace_key(lm),
                        lm.corrected_text
                        if lm.corrected_text is not None
                        else lm.ocr_text,
                    ),
                )
                for lm in (a, b)
            ]
            boundary_reverts.update(
                check_adjacent_duplicates(pair, config=self.guard_config)
            )
        if boundary_reverts:
            self._apply_duplicate_reverts(
                reverts=boundary_reverts,
                traces=traces,
                line_by_id=line_by_id,
                cross_page_partners=cross_page_partners,
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
        ctx: RunContext,
        chunk: ChunkRequest,
        page: PageManifest,
        line_by_id: dict[str, LineManifest],
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
            ctx=ctx,
            chunk=chunk,
            chunk_lines=chunk_lines,
            hyphen_pairs=hyphen_pairs,
            all_lines_by_id=line_by_id,
            traces=traces,
            max_attempts=attempts_cap,
        )
        budget[0] -= attempts_used

        if response is not None:
            return self._finish_successful_chunk(
                ctx=ctx,
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
                    ctx.fallback_count += 1
                    continue
                total += await self._run_chunk(
                    ctx=ctx,
                    chunk=sub,
                    page=page,
                    line_by_id=line_by_id,
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
        ctx.fallback_count += 1
        return 0

    def _finish_successful_chunk(
        self,
        *,
        ctx: RunContext,
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
        # The validated response is already the applied EditScript's output
        # (the producer's ops were normalised and applied in _attempt_chunk,
        # which also accumulated them for CorrectionResult.edit_script).
        text_by_id: dict[str, str] = {
            o.line_id: o.corrected_text for o in response.lines
        }

        target_ids = set(chunk.targets())
        target_lines = [lm for lm in chunk_lines if lm.line_id in target_ids]

        reconciled_count = self._reconcile_chunk_hyphens(
            ctx=ctx,
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
            ctx=ctx,
            chunk_lines=target_lines,
            traces=traces,
            line_by_id=line_by_id,
            cross_page_partners=cross_page_partners,
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
        ctx: RunContext,
        chunk: ChunkRequest,
        chunk_lines: list[LineManifest],
        hyphen_pairs: dict[str, str],
        all_lines_by_id: dict[str, LineManifest],
        traces: dict[str, LineTrace] | None,
        max_attempts: int,
    ) -> tuple[LLMResponse | None, int, bool, str, Usage | None]:
        """Call the edit producer with retries; return the outcome.

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

            # §4.1 — vision envelope, copied only when the producer asks.
            enriched = enrich_chunk_lines(
                chunk_lines,
                all_lines_by_id,
                include_geometry=getattr(self.producer, "wants_geometry", False),
                page_dims=ctx.page_dims,
            )

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
                image_ref=(
                    ctx.image_ref_by_page_id.get(chunk.page_id)
                    if getattr(self.producer, "wants_image", False)
                    else None
                ),
            )

            try:
                # §5.1 — the pipeline drives the temperature ramp: it hands
                # the producer a policy whose FIRST temperature is this
                # attempt's, so the ramp (and the hyphen 0.0 pin) is decided
                # here regardless of the producer implementation.
                per_attempt_policy = self.retry_policy.model_copy(
                    update={"temperatures": (temperature,)}
                )
                script, usage = await self.producer.produce(
                    payload, policy=per_attempt_policy
                )
                raw = self._script_to_raw(script, chunk_lines)
                if usage is not None:
                    ctx.usage = ctx.usage + usage
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
                # §4 — capture each TARGET line's producer op alongside the
                # text that op produced (pre-guard, pre-reconcile). The final
                # EditScript is NOT emitted from here: a line later reverted
                # (duplicate / rejected by check_line) or reconciled to
                # different text must not leave a stale op behind (Audit P2 —
                # a dry-run consumer replaying it would diverge from the
                # pipeline's own corrected XML). _build_final_edit_script
                # reconciles these captured ops against the FINAL per-line
                # state, preserving the producer's op TYPE (e.g. a rules
                # producer's replace_span) when its output survived unchanged.
                target_ids = set(chunk.targets())
                produced_by_line = {o.line_id: o.corrected_text for o in response.lines}
                ops_by_line: dict[str, list[EditOp]] = {}
                for op in script.ops:
                    if op.line_id in target_ids and op.line_id in produced_by_line:
                        ops_by_line.setdefault(op.line_id, []).append(op)
                for line_id, line_ops in ops_by_line.items():
                    # Chunks are page-scoped, so chunk.page_id qualifies
                    # every target line unambiguously.
                    ctx.producer_ops[(chunk.page_id, line_id)] = (
                        line_ops,
                        produced_by_line[line_id],
                    )
                return response, attempts_used, False, "", chunk_usage

            except ProviderPermanentError:
                # P0-1 — credentials/model rejected: retrying is pointless
                # and falling back would fake success. Fatal for the run.
                raise
            except Exception as exc:
                # Audit P3 (same class as P0-2, on the attempt path): a
                # genuine PROGRAMMING error — a bug in _script_to_raw /
                # validation, or a broken invariant — must FAIL the run, not
                # be silently masked as uncorrected OCR text (which would
                # degrade EVERY chunk to OCR while still reporting success).
                # A denylist (not an allowlist) is used deliberately: the
                # pipeline is provider-agnostic and cannot name every
                # provider transport type (httpx errors, SDK exceptions),
                # which are EXPECTED and must remain recoverable. Only the
                # classic programmer-bug types propagate; ValueError family
                # (JSON/validation/parse/HyphenIntegrityError),
                # ProviderTransientError, CorrectionError, and any provider
                # transport error all degrade to retry-then-OCR-fallback.
                if isinstance(exc, _PROGRAMMING_ERROR_TYPES):
                    raise
                # §5.1 — the pipeline no longer holds credentials; the
                # pattern-based redaction still masks secret-shaped
                # substrings a producer may leak into the message, and the
                # consumer layer (which DOES hold the key) sanitises again
                # on its own error paths.
                msg = sanitize_error(str(exc))
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
                    ctx.retry_count += 1
                    continue

                # Attempts exhausted (or non-retryable error class). Do NOT
                # fall back here — the caller decides between a granularity
                # downgrade (F1) and the OCR fallback. ``can_downgrade`` is
                # True only when the terminal error was retryable.
                return None, attempts_used, decision.is_retryable, msg, None

        # max_attempts <= 0 (no budget left): nothing attempted.
        return None, attempts_used, False, last_msg, None

    def _script_to_raw(
        self, script: EditScript, chunk_lines: list[LineManifest]
    ) -> dict[str, Any]:
        """Normalise a producer's EditScript into the validator's raw shape.

        - ``replace_line`` ops pass through as-is (duplicates and empty
          texts included — the validator's structural checks must see them
          exactly as the historical raw response did).
        - ``replace_span`` ops are normalised and applied against the
          chunk's canonical text via :func:`apply_edit_script` (E1–E5); a
          rejected op leaves its line uncovered.
        - When the producer declares ``requires_full_coverage = False``
          (deterministic producers: no op == no edit), uncovered lines are
          filled with their canonical text so the validator's 1:1 check
          passes. An LLM producer keeps full-coverage semantics: a dropped
          target line stays missing → ValidationError → retry.
        """
        canonical = {lm.line_id: lm.ocr_text for lm in chunk_lines}
        entries: list[dict[str, str]] = []

        span_ops = [op for op in script.ops if isinstance(op, ReplaceSpan)]
        for op in script.ops:
            if isinstance(op, ReplaceLine):
                entries.append({"line_id": op.line_id, "corrected_text": op.text})
        if span_ops:
            span_result = apply_edit_script(
                EditScript(ops=list(span_ops)),
                canonical,
                chunk_line_ids=set(canonical),
                guard_config=self.guard_config,
                line_by_id={lm.line_id: lm for lm in chunk_lines},
            )
            for lid, txt in span_result.text_by_id.items():
                entries.append({"line_id": lid, "corrected_text": txt})

        if not getattr(self.producer, "requires_full_coverage", True):
            covered = {e["line_id"] for e in entries}
            for lid, txt in canonical.items():
                if lid not in covered:
                    entries.append({"line_id": lid, "corrected_text": txt})

        return {"lines": entries}

    # ------------------------------------------------------------------
    # Chunk helpers extracted from _run_chunk (audit A3)
    # ------------------------------------------------------------------

    def _reconcile_chunk_hyphens(
        self,
        *,
        ctx: RunContext,
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
            self._record_reconcile_outcome(ctx, outcome)
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
            self._record_reconcile_outcome(ctx, outcome)
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

    def _apply_duplicate_reverts(
        self,
        *,
        reverts: dict[str, str],
        traces: dict[str, LineTrace] | None,
        line_by_id: dict[str, LineManifest],
        cross_page_partners: dict[tuple[str, str], LineManifest] | None = None,
    ) -> None:
        """Revert duplicate-flagged lines to OCR — atomically with their
        hyphen partner.

        Shared by the chunk-level sweep, the page-level cross-chunk pass
        and the page-boundary pass (review fix: the revert logic used to
        be duplicated and none of the copies preserved pair atomicity —
        reverting one member of a reconciled pair left a mixed
        OCR+corrected pair, the exact state ``reconcile_hyphen_pair``
        guarantees can never survive). A flagged line's partner is
        reverted too, with its own trace reason.

        Audit P1 — partner extension resolves through ``_resolve_partner``
        so a *cross-page* partner (living on another page, absent from the
        page-local ``line_by_id``) is reverted too. The old page-local
        ``pid in line_by_id`` guard silently skipped it, leaving the
        reconciled cross-page pair half OCR / half corrected.
        """
        if not reverts:
            return
        # Collect the manifests to revert by object identity, keeping the
        # first reason assigned to each. Originals are enrolled before the
        # atomicity extension so an original revert reason always wins.
        to_revert: dict[int, tuple[LineManifest, str]] = {}

        def _enroll(lm: LineManifest, reason: str) -> None:
            to_revert.setdefault(id(lm), (lm, reason))

        for lid, reason in reverts.items():
            lm = line_by_id.get(lid)
            if lm is not None:
                _enroll(lm, reason)
        # Audit-F2 — walk the partner extension to a FIXED POINT: enrolled
        # partners are themselves iterated so whole 3+-line hyphen chains
        # (PART1→BOTH→…→PART2) revert atomically. The previous single pass
        # over the original flags was one-hop, so a chain neighbour two
        # hops from any flagged line kept its corrected text — the mixed
        # OCR+corrected pair state that reconcile_hyphen_pair's contract
        # and this function's own docstring forbid.
        worklist: list[LineManifest] = [
            lm for lm in (line_by_id.get(lid) for lid in reverts) if lm is not None
        ]
        visited: set[int] = {id(lm) for lm in worklist}
        while worklist:
            lm = worklist.pop()
            for is_forward in (False, True):
                partner = _resolve_partner(
                    lm,
                    is_forward=is_forward,
                    line_by_id=line_by_id,
                    cross_page_partners=cross_page_partners,
                )
                if partner is not None:
                    _enroll(partner, "adjacent_duplicate_pair_atomicity")
                    if id(partner) not in visited:
                        visited.add(id(partner))
                        worklist.append(partner)

        for lm, reason in to_revert.values():
            lm.corrected_text = lm.ocr_text
            lm.status = LineStatus.FALLBACK
            _set_trace(
                traces,
                lm,
                projected_text=lm.ocr_text,
                validation_status=lm.status.value,
            )
            # Only stamp the reason if no earlier fallback path (e.g.
            # orphan_hyphen_completed) already pinned one.
            if traces is not None:
                trace = traces.get(_trace_key(lm))
                if trace is not None and not trace.fallback_reason:
                    trace.fallback_reason = reason

    def _finalize_chunk_traces(
        self,
        *,
        ctx: RunContext,
        chunk_lines: list[LineManifest],
        traces: dict[str, LineTrace] | None,
        line_by_id: dict[str, LineManifest],
        cross_page_partners: dict[tuple[str, str], LineManifest] | None = None,
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
        # Persist the pre-revert snapshot for the boundary and page-seam
        # passes (same comparison basis as this pass), and record the
        # ACTUAL finalization owner: downgrade sub-chunks create seams
        # the planned chunk list never had.
        ctx.finalize_seq += 1
        for lm in chunk_lines:
            ctx.accepted_snapshot[_trace_key(lm)] = (
                lm.corrected_text if lm.corrected_text is not None else lm.ocr_text
            )
            ctx.finalized_owner[_trace_key(lm)] = ctx.finalize_seq
        dup_reverts = check_adjacent_duplicates(
            accepted_lines, config=self.guard_config
        )
        self._apply_duplicate_reverts(
            reverts=dup_reverts,
            traces=traces,
            line_by_id=line_by_id,
            cross_page_partners=cross_page_partners,
        )

        for lm in chunk_lines:
            if lm.line_id in dup_reverts:
                continue  # already projected by the revert helper
            _set_trace(
                traces,
                lm,
                projected_text=(
                    lm.corrected_text if lm.corrected_text is not None else lm.ocr_text
                ),
                validation_status=lm.status.value,
            )

    # ------------------------------------------------------------------
    # Output writing (rewriter + trace assembly)
    # ------------------------------------------------------------------

    async def _write_outputs(
        self,
        *,
        document_manifest: DocumentManifest,
        source_files: dict[str, Path],
        traces: dict[str, LineTrace],
        apply: bool = True,
    ) -> None:
        """Rewrite corrected ALTO files, update traces, and (when ``apply``)
        persist via the writer.

        §9 dry-run — the rewrite always runs in memory so the report's
        ``rewriter_path`` / ``output_alto_text`` are populated, but when
        ``apply`` is ``False`` the injected ``OutputWriter`` is never
        called: nothing is persisted.

        Wave-3 review — the heavy calls (``rewrite_file``: a full lxml
        parse/rewrite/serialize of the source file; ``write_corrected``:
        disk IO; ``extract_texts``: another parse) run in worker threads
        so a ~100 MiB rewrite no longer freezes the host's event loop
        (SSE keepalives, /health). Observer events stay ON the loop —
        emit sites must never run from a thread (the store's queues are
        not thread-safe).
        """
        # §11 — provenance stamped into every corrected file's processingStep.
        from corrigenda import __version__ as _lib_version

        config_fingerprint = self.config_fingerprint()
        adapter = self.format_adapter or _default_format_adapter()

        for source_name, xml_path in source_files.items():
            pages_for_file = [
                p for p in document_manifest.pages if p.source_file == source_name
            ]
            if not pages_for_file:
                continue

            xml_bytes, metrics, rewriter_paths = await asyncio.to_thread(
                adapter.rewrite_file,
                xml_path,
                pages_for_file,
                # §11 provenance labels — constructor state since the §5.1
                # resorption (run() no longer carries provider/model).
                self.provider_name,
                self.model,
                lib_version=_lib_version,
                config_fingerprint=config_fingerprint,
            )
            if apply:
                await asyncio.to_thread(
                    self.output_writer.write_corrected,
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
            output_texts = await asyncio.to_thread(
                adapter.extract_texts, xml_bytes, file_line_ids
            )
            for lid, otxt in output_texts.items():
                tkey = lid_to_tkey.get(lid)
                if tkey:
                    t = traces.get(tkey)
                    if t is not None:
                        t.output_alto_text = otxt

        # Trace persistence moved to run(): trace.json IS the
        # CorrectionReport — one §9 artefact, not a parallel JobTrace shape.


# --- __all__ (Stage 3 audit remediation) ---
__all__ = [
    "sanitize_error",
    "CorrectionResult",
    "CorrectionPipeline",
]
