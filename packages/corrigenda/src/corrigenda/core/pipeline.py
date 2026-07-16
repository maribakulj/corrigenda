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
    LineRef,
    ensure_unique_identities,
    ensure_unique_page_ids_across_files,
    line_ref,
)
from corrigenda.errors import (
    ConfigurationError,
    CorrectionAborted,
    CorrectionError,
    ProjectionError,
)
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

# ADR-008 (revised) — recoverability is an ALLOWLIST. Exactly the two
# families the retry classifier can route are recoverable on the
# producer-attempt path:
#   - ProviderTransientError — transport flakiness a conforming provider
#     wrapped (wrapping is the provider CONTRACT, not a courtesy: the
#     provider-agnostic pipeline cannot name raw httpx/SDK exceptions,
#     so an unwrapped one is indistinguishable from a bug and fails the
#     run rather than degrading to a fake success);
#   - ValueError — the documented malformed-producer-output family
#     (ValidationError, HyphenIntegrityError, json.JSONDecodeError all
#     inherit it; §8.4 keeps them value-shaped for exactly this route).
# Everything else — RuntimeError, KeyError, a pydantic bug, an SDK
# exception nobody classified — fails the run: an unknown exception
# must never become a silently-uncorrected "success".
_RECOVERABLE_ERROR_TYPES: tuple[type[BaseException], ...] = (
    ProviderTransientError,
    ValueError,
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


def _adapter_for_format(source_format: str | None) -> FormatAdapter:
    """Resolve the adapter the MANIFEST declares — no implicit default (§3).

    The format travels with the document: the parsers stamp
    ``DocumentManifest.source_format`` and the engine derives the
    matching adapter here. A manifest without a stamped format
    (hand-built) has no derivable adapter, and silently assuming one —
    the historical ALTO default — is exactly how a PAGE document ended
    up rewritten by the ALTO rewriter.

    This function is the ONLY place ``core`` touches a concrete format,
    and the imports are function-local so importing any
    ``corrigenda.core`` module never loads lxml. The import-contract
    test pins both facts: core modules carry no static formats/lxml
    import, and this exact function is the single allowed lazy site.
    """
    if source_format == "alto":
        from corrigenda.formats.alto.adapter import AltoFormatAdapter

        return AltoFormatAdapter()
    if source_format == "page":
        from corrigenda.formats.page.adapter import PageFormatAdapter

        return PageFormatAdapter()
    raise ConfigurationError(
        f"the manifest declares no derivable format "
        f"(source_format={source_format!r}); load the document through a "
        "corrigenda format parser, or inject format_adapter explicitly "
        "on the pipeline"
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


def _verify_all_lines_terminal(document_manifest: DocumentManifest) -> None:
    """Refuse to finish a run while any line lacks a terminal decision.

    Every decision path (acceptance, reconciliation, fallback, absorbed
    chunk error) guarantees per-line terminality; this is the run-level
    backstop before outputs are written. A ``PENDING`` line here is an
    engine bug — a decision path that forgot its lines — never an input
    problem, so it fails the run instead of degrading.
    """
    undecided = [
        (page.page_id, lm.line_id)
        for page in document_manifest.pages
        for lm in page.lines
        if lm.status is LineStatus.PENDING
    ]
    if undecided:
        shown = ", ".join(f"({p!r}, {li!r})" for p, li in undecided[:5])
        suffix = " …" if len(undecided) > 5 else ""
        raise RuntimeError(
            f"{len(undecided)} line(s) reached the end of the run with no "
            f"terminal decision (PENDING): {shown}{suffix}"
        )


def _projection_normal_form(text: str) -> str:
    """Whitespace-run normal form for the projection invariant.

    ALTO/PAGE tokenize a line into word elements, so runs of consecutive
    whitespace cannot survive the write→extract round-trip. Word-level
    equality is the enforceable contract; exact spacing is a property of
    the formats, not a correctness signal.
    """
    return " ".join(text.split())


def _verify_projection(
    source_name: str,
    pages: list[PageManifest],
    output_texts: dict[str, str],
) -> None:
    """The rewritten file must SAY what the run decided, line for line.

    Compares the text re-extracted from the rewritten XML against each
    line's final decision (corrected text, or source text for fallback /
    untouched lines) in whitespace normal form. A missing line or a
    word-level divergence is corruption of the deliverable — the run
    fails here, before the writer can persist the artefact.
    """
    for page in pages:
        for lm in page.lines:
            decided = (
                lm.corrected_text if lm.corrected_text is not None else lm.ocr_text
            )
            extracted = output_texts.get(lm.line_id)
            if extracted is None:
                raise ProjectionError(
                    f"line {lm.line_id!r} (page {lm.page_id!r}) of "
                    f"{source_name!r} is missing from the rewritten XML"
                )
            if _projection_normal_form(extracted) != _projection_normal_form(decided):
                raise ProjectionError(
                    f"rewritten XML for {source_name!r} diverges from the "
                    f"run's decision on line {lm.line_id!r} (page "
                    f"{lm.page_id!r}): decided {decided!r} but the artefact "
                    f"contains {extracted!r}"
                )


def _set_trace(
    traces: dict[LineRef, LineTrace] | None,
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
    trace = traces.get(line_ref(lm))
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
    cross_page_partners: dict[LineRef, LineManifest] | None,
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
    return cross_page_partners.get(LineRef(page_id=partner_page, line_id=partner_id))


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
    #: Number of chunks whose producer attempts were exhausted (an
    #: orchestration counter — one rejected 20-line chunk counts once).
    fallback_chunks: int
    #: Number of LINES whose terminal status is ``FALLBACK`` — they kept
    #: their OCR source text, whether a whole chunk fell back, a guard
    #: rejected the correction, or a duplicate revert undid it. Manifest
    #: statuses are the authority; "completed with fallbacks" means
    #: exactly ``fallback_lines > 0``.
    fallback_lines: int
    #: Aggregated ``fallback_reason`` prefixes → line counts for the
    #: fallen lines (e.g. ``{"all_attempts_exhausted": 20}``), so a
    #: consumer can say WHY without parsing messages.
    fallback_reasons: dict[str, int]
    traces: dict[LineRef, LineTrace]
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
    """All mutable state of ONE pipeline execution (ADR-005).

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
    fallback_chunks: int = 0
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
    producer_ops: dict[LineRef, tuple[list[EditOp], str]] = field(default_factory=dict)
    #: Per-line PRE-REVERT accepted correction, keyed by _trace_key. The
    #: cross-chunk boundary pass and the page-seam pass compare against
    #: THIS snapshot (like the intra-chunk pass does via its local
    #: `accepted_lines`): reading the live corrected_text after an
    #: earlier revert would mask the third line of an identical-
    #: correction run straddling a chunk/page seam.
    accepted_snapshot: dict[LineRef, str] = field(default_factory=dict)
    #: Which finalization pass ACTUALLY owned each line (keyed by
    #: _trace_key). Granularity descent spawns sub-chunks the plan never
    #: listed, so the boundary pass must derive its seams from these
    #: owners, not from plan.chunks.
    finalized_owner: dict[LineRef, int] = field(default_factory=dict)
    finalize_seq: int = 0
    #: §4.1 vision envelope — resolved once per run from run(source_images=…).
    image_ref_by_page_id: dict[str, ImageRef] = field(default_factory=dict)
    page_dims: dict[str, tuple[int, int]] = field(default_factory=dict)


class CorrectionPipeline:
    """Pure orchestration of the correction pipeline over an EditProducer.

    Dependencies are injected via the constructor; the pipeline never
    reaches for global state. The instance holds only immutable
    configuration (ADR-005): every run creates a fresh :class:`RunContext`
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
        # §3 format seam — None derives the adapter from the MANIFEST's
        # stamped source_format at write time (_adapter_for_format); an
        # injected adapter that contradicts that format is refused at
        # run start. There is no implicit default format.
        self.format_adapter = format_adapter
        # §11 — provenance labels stamped into the corrected XML's
        # processingStep. Pure strings: the pipeline never dials a vendor.
        self.provider_name = provider_name
        self.model = model
        # Reentrancy guard (ADR-005). Per-run state lives in RunContext,
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

        **Concurrency contract (ADR-005)** — one instance, one run at
        a time. All per-run state lives in a fresh :class:`RunContext`
        created here (the pipeline instance itself carries only
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
        # ADR-005 — one fresh context per execution; no per-run state
        # remains on the instance.
        ctx = RunContext()

        # §5.1 — a vision producer without its images is a start-up error,
        # never a silent image-less call.
        require_source_images(self.producer, list(source_files.keys()), source_images)

        # §3 — the format travels with the document. An injected adapter
        # that contradicts the format the manifest was parsed as would
        # only surface at write time (as a confusing projection failure);
        # refuse it before any correction work is spent. Adapters without
        # a ``format_name`` (custom implementations) are trusted as-is.
        declared = document_manifest.source_format
        adapter_format = getattr(self.format_adapter, "format_name", None)
        if declared and adapter_format and declared != adapter_format:
            raise ConfigurationError(
                f"the injected format_adapter writes {adapter_format!r} but "
                f"the manifest was parsed as {declared!r} — parse with the "
                "matching corrigenda parser or inject the matching adapter"
            )

        # ADR-007 — identity-uniqueness invariant, enforced at the pipeline
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
        traces: dict[LineRef, LineTrace] = {}
        for page in document_manifest.pages:
            for lm in page.lines:
                traces[line_ref(lm)] = LineTrace(
                    line_id=lm.line_id,
                    page_id=lm.page_id,
                    source_ocr_text=lm.ocr_text,
                    hyphen_role=lm.hyphen_role.value,
                )

        # Global page-qualified registry for cross-page partner lookups
        all_lines_global: dict[LineRef, LineManifest] = {}
        for page in document_manifest.pages:
            for lm in page.lines:
                all_lines_global[line_ref(lm)] = lm

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
            cross_page: dict[LineRef, LineManifest] = {}
            for lm in page.lines:
                for partner_id, partner_page in (
                    (lm.hyphen_pair_line_id, lm.hyphen_pair_page_id),
                    (lm.hyphen_forward_pair_id, lm.hyphen_forward_pair_page_id),
                ):
                    if not partner_id or not partner_page:
                        continue
                    if partner_page == page.page_id:
                        continue
                    partner = all_lines_global.get(
                        LineRef(page_id=partner_page, line_id=partner_id)
                    )
                    if partner is not None:
                        cross_page[
                            LineRef(page_id=partner_page, line_id=partner_id)
                        ] = partner

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

        # Page-seam duplicate pass: chunk plans and the page-level pass
        # are both page-scoped, yet page-boundary lines DO see each other
        # through cross-page hyphen context — so each page seam is checked
        # explicitly (O(#pages), one pair per seam). The lookup is built
        # per seam, never document-wide: bare line_ids may legitimately
        # repeat across FILES, and a global bare-id dict is the exact
        # ambiguity ADR-007 bans — an ambiguous seam is skipped instead.
        for prev_page, next_page in zip(
            document_manifest.pages, document_manifest.pages[1:]
        ):
            if not prev_page.lines or not next_page.lines:
                continue
            # Only compare a seam WITHIN one source file: pages of
            # different files are concatenated in document_manifest, and
            # the last physical line of file A is NOT adjacent to the
            # first line of file B — comparing them could spuriously
            # revert either as a "duplicate".
            if prev_page.source_file != next_page.source_file:
                continue
            seam_map = {lm.line_id: lm for lm in (*prev_page.lines, *next_page.lines)}
            if len(seam_map) != len(prev_page.lines) + len(next_page.lines):
                continue  # cross-file line_id reuse → ambiguous, skip
            a, b = prev_page.lines[-1], next_page.lines[0]
            # Same pre-revert snapshot basis as the cross-chunk boundary
            # pass: an intra-page revert of the seam line would otherwise
            # mask the third member of a run straddling the page seam.
            seam_reverts = check_adjacent_duplicates(
                [
                    (
                        lm.line_id,
                        lm.ocr_text,
                        ctx.accepted_snapshot.get(
                            line_ref(lm),
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

        # Run-level terminality backstop: outputs exist only for a
        # document where every line carries a terminal decision.
        _verify_all_lines_terminal(document_manifest)

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

        # Line-level fallback accounting. Manifest statuses are the
        # authority: they cover every path that leaves a line at its OCR
        # text (chunk fallback, guard rejection, duplicate revert), not
        # just chunks whose attempts were exhausted.
        fallback_lms = [
            lm
            for page in document_manifest.pages
            for lm in page.lines
            if lm.status is LineStatus.FALLBACK
        ]
        fallback_reasons: dict[str, int] = {}
        for lm in fallback_lms:
            trace = traces.get(line_ref(lm))
            raw = (trace.fallback_reason if trace else None) or "unspecified"
            prefix = raw.split(":", 1)[0].strip()
            fallback_reasons[prefix] = fallback_reasons.get(prefix, 0) + 1

        return CorrectionResult(
            total_chunks=total_chunks,
            total_reconciled=total_reconciled,
            retry_count=ctx.retry_count,
            fallback_chunks=ctx.fallback_chunks,
            fallback_lines=len(fallback_lms),
            fallback_reasons=fallback_reasons,
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
        (a dry-run consumer replaying it would otherwise diverge
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
                captured = ctx.producer_ops.get(line_ref(lm))
                if captured is None:
                    # An accepted line the producer left untouched (no op) —
                    # e.g. a rules producer's uncovered line. Nothing applied.
                    continue
                line_ops, produced_text = captured
                if produced_text == lm.corrected_text:
                    # The producer's output survived every guard unchanged —
                    # keep its original ops (and their TYPE, e.g. span),
                    # stamped with the page_id so a consumer can attribute
                    # them per file (bare line_ids repeat across
                    # files — ADR-001).
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
        traces: dict[LineRef, LineTrace],
        cross_page_partners: dict[LineRef, LineManifest] | None,
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
                # to a chunk_error event. ADR-008 — a permanent provider
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
                # ADR-008 — only RECOVERABLE domain errors may be absorbed as
                # a chunk_error + continue. Anything else (KeyError,
                # AttributeError, a pydantic bug, a broken invariant) is a
                # programming error: continuing would let the run complete
                # "successfully" with lines in an unknown state.
                if not isinstance(exc, CorrectionError):
                    raise
                # The absorbed error may have interrupted the chunk between
                # its producer attempt and its finalization: any target
                # line still awaiting a decision falls back to its source
                # text NOW. The run may degrade; it may never continue
                # with undecided lines (lines the chunk — or a descent
                # sub-chunk — already finalized keep their decision).
                undecided = [
                    line_by_id[lid]
                    for lid in chunk.targets()
                    if lid in line_by_id
                    and line_by_id[lid].status is LineStatus.PENDING
                ]
                if undecided:
                    reason = sanitize_error(str(exc))[:120]
                    for lm in undecided:
                        lm.corrected_text = lm.ocr_text
                        lm.status = LineStatus.FALLBACK
                        _set_trace(
                            traces,
                            lm,
                            projected_text=lm.ocr_text,
                            validation_status="fallback",
                            fallback_reason=f"chunk_error_absorbed: {reason}",
                        )
                    ctx.fallback_chunks += 1

        # Cross-chunk adjacency pass. Per-chunk finalization only sees
        # that chunk's TARGET lines, so two document-adjacent lines owned
        # by different chunks are never compared there — a duplication
        # straddling a chunk boundary needs this pass. Only the boundary
        # pairs are new — intra-chunk pairs were already checked with the
        # same function and config — so the pass is restricted to
        # adjacent pairs whose owners differ (re-checking whole pages
        # would re-run SequenceMatcher over every already-checked pair
        # for nothing).
        #
        # The owners are the finalization passes that
        # ACTUALLY ran (ctx.finalized_owner), not the planned chunks:
        # granularity descent finalizes a planned chunk as many
        # sub-chunks, whose seams the plan-derived map could not see —
        # and a single-chunk plan (`len(plan.chunks) > 1` gate) skipped
        # the pass outright. Lines that never finalized (full-chunk OCR
        # fallback) share owner None; such pairs sit at source text on
        # both sides, where a revert would be a no-op anyway.
        boundary_reverts: dict[str, str] = {}
        for a, b in zip(page.lines, page.lines[1:]):
            if ctx.finalized_owner.get(line_ref(a)) == ctx.finalized_owner.get(
                line_ref(b)
            ):
                continue
            # Compare the PRE-REVERT accepted corrections (snapshotted
            # in _finalize_chunk_traces), not the live corrected_text: an
            # intra-chunk revert of the boundary line would otherwise mask
            # the third member of an identical-correction run straddling
            # the boundary.
            pair = [
                (
                    lm.line_id,
                    lm.ocr_text,
                    ctx.accepted_snapshot.get(
                        line_ref(lm),
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
        traces: dict[LineRef, LineTrace] | None = None,
        cross_page_partners: dict[LineRef, LineManifest] | None = None,
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
                    ctx.fallback_chunks += 1
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
        ctx.fallback_chunks += 1
        return 0

    def _finish_successful_chunk(
        self,
        *,
        ctx: RunContext,
        chunk: ChunkRequest,
        chunk_lines: list[LineManifest],
        response: LLMResponse,
        line_by_id: dict[str, LineManifest],
        cross_page_partners: dict[LineRef, LineManifest] | None,
        traces: dict[LineRef, LineTrace] | None,
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
        traces: dict[LineRef, LineTrace] | None,
        sanitised_msg: str,
    ) -> None:
        """Revert the chunk's TARGET lines to their OCR text and emit a
        ``warning`` event. Mutates ``corrected_text`` / ``status`` /
        line traces. Called once the retry loop exhausts its budget or
        hits a non-retryable error.

        F8 — only target lines are reverted; context lines are owned by an
        adjacent chunk and must not be forced to OCR here.

        The pipeline-level ``_fallback_chunks`` is bumped by the caller,
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
        traces: dict[LineRef, LineTrace] | None,
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
                # different text must not leave a stale op behind (a
                # dry-run consumer replaying it would diverge from the
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
                    ctx.producer_ops[
                        LineRef(page_id=chunk.page_id, line_id=line_id)
                    ] = (
                        line_ops,
                        produced_by_line[line_id],
                    )
                return response, attempts_used, False, "", chunk_usage

            except ProviderPermanentError:
                # ADR-008 — credentials/model rejected: retrying is pointless
                # and falling back would fake success. Fatal for the run.
                raise
            except Exception as exc:
                # ADR-008 (attempt-path branch, revised): only the
                # allowlisted recoverable families degrade to
                # retry-then-OCR-fallback. Anything else — a programming
                # error, an unwrapped SDK transport exception, a broken
                # invariant — FAILS the run: masking it as uncorrected OCR
                # text would degrade EVERY chunk while still reporting
                # success. Providers signal transport flakiness by
                # wrapping it as ProviderTransientError (their contract).
                if not isinstance(exc, _RECOVERABLE_ERROR_TYPES):
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
    # Chunk helpers extracted from _run_chunk
    # ------------------------------------------------------------------

    def _reconcile_chunk_hyphens(
        self,
        *,
        ctx: RunContext,
        chunk_id: str,
        chunk_lines: list[LineManifest],
        text_by_id: dict[str, str],
        line_by_id: dict[str, LineManifest],
        cross_page_partners: dict[LineRef, LineManifest] | None,
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
        traces: dict[LineRef, LineTrace] | None,
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
        traces: dict[LineRef, LineTrace] | None,
        line_by_id: dict[str, LineManifest],
        cross_page_partners: dict[LineRef, LineManifest] | None = None,
    ) -> None:
        """Revert duplicate-flagged lines to OCR — atomically with their
        hyphen partner.

        ONE shared implementation for the chunk-level sweep, the
        page-level cross-chunk pass and the page-boundary pass: a mixed
        OCR+corrected pair is the exact state ``reconcile_hyphen_pair``
        guarantees can never survive, so every copy of the revert logic
        must preserve pair atomicity — a flagged line's partner is
        reverted too, with its own trace reason.

        Partner extension resolves through ``_resolve_partner`` so a
        *cross-page* partner (living on another page, absent from the
        page-local ``line_by_id``) is reverted too — a page-local guard
        would silently skip it and leave the reconciled cross-page pair
        half OCR / half corrected.
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
        # Walk the partner extension to a FIXED POINT: enrolled partners
        # are themselves iterated so whole 3+-line hyphen chains
        # (PART1→BOTH→…→PART2) revert atomically. A one-hop pass would
        # leave a chain neighbour two hops from any flagged line with its
        # corrected text — the mixed OCR+corrected pair state that
        # reconcile_hyphen_pair's contract and this docstring forbid.
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
                trace = traces.get(line_ref(lm))
                if trace is not None and not trace.fallback_reason:
                    trace.fallback_reason = reason

    def _finalize_chunk_traces(
        self,
        *,
        ctx: RunContext,
        chunk_lines: list[LineManifest],
        traces: dict[LineRef, LineTrace] | None,
        line_by_id: dict[str, LineManifest],
        cross_page_partners: dict[LineRef, LineManifest] | None = None,
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
            ctx.accepted_snapshot[line_ref(lm)] = (
                lm.corrected_text if lm.corrected_text is not None else lm.ocr_text
            )
            ctx.finalized_owner[line_ref(lm)] = ctx.finalize_seq
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
        traces: dict[LineRef, LineTrace],
        apply: bool = True,
    ) -> None:
        """Rewrite corrected ALTO files, update traces, and (when ``apply``)
        persist via the writer.

        §9 dry-run — the rewrite always runs in memory so the report's
        ``rewriter_path`` / ``output_alto_text`` are populated, but when
        ``apply`` is ``False`` the injected ``OutputWriter`` is never
        called: nothing is persisted.

        The heavy calls (``rewrite_file``: a full lxml
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
        # Adapter resolution is lazy (first file to write): a run with no
        # output files — every hand-built-manifest dry-run in the test
        # suite passes source_files={} — needs no format at all.
        adapter: FormatAdapter | None = self.format_adapter

        for source_name, xml_path in source_files.items():
            pages_for_file = [
                p for p in document_manifest.pages if p.source_file == source_name
            ]
            if not pages_for_file:
                continue
            if adapter is None:
                adapter = _adapter_for_format(document_manifest.source_format)

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

            file_line_ids = {lm.line_id for p in pages_for_file for lm in p.lines}
            output_texts = await asyncio.to_thread(
                adapter.extract_texts, xml_bytes, file_line_ids
            )
            # Projection invariant: the artefact must SAY what the run
            # decided. Verified BEFORE the writer sees the bytes — a
            # divergent artefact is corruption, never a valid output.
            _verify_projection(source_name, pages_for_file, output_texts)

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

            lid_to_ref: dict[str, LineRef] = {}
            for p in pages_for_file:
                for lm in p.lines:
                    lid_to_ref[lm.line_id] = line_ref(lm)

            for lid, rpath in rewriter_paths.items():
                tkey = lid_to_ref.get(lid)
                if tkey:
                    t = traces.get(tkey)
                    if t is not None:
                        t.rewriter_path = rpath

            for lid, otxt in output_texts.items():
                tkey = lid_to_ref.get(lid)
                if tkey:
                    t = traces.get(tkey)
                    if t is not None:
                        t.output_alto_text = otxt

        # Trace persistence moved to run(): trace.json IS the
        # CorrectionReport — one §9 artefact, not a parallel JobTrace shape.


# --- public surface ---
__all__ = [
    "sanitize_error",
    "CorrectionResult",
    "CorrectionPipeline",
]
