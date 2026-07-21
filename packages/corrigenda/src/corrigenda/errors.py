"""corrigenda error hierarchy (SPECS_LIB_V2 §8.4).

A single root, ``CorrigendaError``, sits above every error the library
raises so consumers can ``except CorrigendaError`` once::

    CorrigendaError
    ├── ParseError          (also ValueError)
    │   └── DuplicateIdError       — ambiguous identities in source/manifest
    ├── ProposalValidationError  (also ValueError) — producer response invalid
    │   └── HyphenIntegrityError   (defined in pipeline.validator)
    ├── ProviderError              — provider errors (core.protocols)
    │   ├── ProviderTransientError — recoverable transport failure
    │   └── ProviderPermanentError — fatal rejection (credentials, model)
    ├── ConfigurationError         — contradictory/incomplete composition
    ├── ProjectionError            — output artefact ≠ decisions
    └── CorrectionAborted

Naming (P3.11): the root is ``CorrigendaError`` — named for the LIBRARY,
like ``requests.RequestException`` — and the producer-response error is
``ProposalValidationError`` (it validates producer *proposals*, P3.7
vocabulary; the bare ``ValidationError`` collided with pydantic's in
every consumer's imports). ``CorrectionError`` and ``ValidationError``
remain as deprecation ALIASES of the same classes for the 0.9.x series —
``except`` clauses, ``isinstance`` and subclasses behave identically
through either name — and disappear at the P3.11 top-level reduction.

The value-shaped errors additionally inherit ``ValueError`` so the bare
``ValueError`` raises that predate this hierarchy keep working under
``except ValueError`` (§8.4) — and the pipeline's retry classifier, which
routes ``(ValueError, json.JSONDecodeError)`` to the malformed-output
branch, still catches them. Provider errors deliberately do NOT inherit
``ValueError``: a transport failure routed to the malformed-output branch
would be mis-filed.

Every class carries two machine-readable class attributes so hosts route
on structure, never on message text:

- ``code`` — a stable snake_case identifier, unique per class;
- ``retryable`` — whether a fresh attempt of the SAME operation can heal
  the failure. This describes the error, not the pipeline's policy: the
  attempt budget and backoff stay in ``RetryPolicy``.
"""

from __future__ import annotations

from typing import ClassVar


class CorrigendaError(Exception):
    """Base class for every error raised by corrigenda (§8.4)."""

    code: ClassVar[str] = "correction_error"
    retryable: ClassVar[bool] = False


class ParseError(CorrigendaError, ValueError):
    """A source document could not be parsed into a manifest.

    Inherits ``ValueError`` for backwards compatibility with call sites
    that caught the bare ``ValueError`` the parser used to raise.
    """

    code: ClassVar[str] = "parse_error"


class DuplicateIdError(ParseError):
    """A source document or manifest set carries duplicate identities (ADR-007).

    Every internal association between a correction and its physical line
    (rewriter lookup, trace projection, hyphen partner resolution) is keyed
    by ``line_id`` within one source file. A document where two ``TextLine``
    elements share an ID — or a hand-built manifest repeating a ``line_id``
    within one file — is *ambiguous*: silently continuing would risk applying
    a correction to the wrong physical line. The library refuses such input
    explicitly instead of guessing.

    Inherits :class:`ParseError` (hence ``CorrigendaError`` and
    ``ValueError``) so existing ``except ParseError`` call sites keep
    working.
    """

    code: ClassVar[str] = "duplicate_identity"


class ProposalValidationError(CorrigendaError, ValueError):
    """A producer (LLM / rules / VLM) response failed validation.

    Raised by :func:`corrigenda.core.validator.validate_llm_response`
    for structural problems (missing/duplicate/unknown line ids, wrong
    count, empty or multi-line ``corrected_text``, …). Inherits
    ``ValueError`` so the retry classifier keeps routing it to the
    malformed-output branch. Retryable: a fresh attempt may produce a
    response that does validate.
    """

    code: ClassVar[str] = "invalid_producer_output"
    retryable: ClassVar[bool] = True


class ProviderError(CorrigendaError):
    """Base for failures reported by a producer's underlying provider.

    The concrete classes live in :mod:`corrigenda.core.protocols` next to
    the ``BaseProvider`` contract that raises them; this base anchors them
    under the single ``CorrigendaError`` root. Carries the originating
    HTTP status when there was one.
    """

    code: ClassVar[str] = "provider_error"

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ConfigurationError(CorrigendaError):
    """The run was assembled from contradictory or incomplete pieces.

    Raised before any correction work begins (or before any output is
    written): e.g. an injected format adapter that contradicts the
    format the manifest was parsed as, or a hand-built manifest with no
    stamped format reaching the write phase without an explicit adapter.
    Never retryable — the caller must fix the composition.
    """

    code: ClassVar[str] = "configuration_error"


class ProjectionError(CorrigendaError):
    """The rewritten XML does not say what the run decided.

    Raised when the text re-extracted from the output artefact diverges
    (beyond whitespace-run normalization — word tokenization cannot
    represent consecutive spaces) from the final per-line decision, or
    when a decided line is missing from the artefact altogether. This is
    corruption of the deliverable, never a degradation: the run fails
    before the writer persists anything.
    """

    code: ClassVar[str] = "projection_mismatch"


class CorrectionAborted(CorrigendaError):
    """Raised when ``should_abort()`` requested cancellation (F10).

    The pipeline probes the caller-supplied ``should_abort`` callback
    between chunks and between pages. When it returns ``True`` the run
    stops and this exception propagates out of ``run()`` **before any
    output is written** — the corrected XML and trace are not persisted.
    Provider calls already in flight are not interrupted (the cancellation
    is cooperative, observed only at chunk/page boundaries).
    """

    code: ClassVar[str] = "cancelled"


# --- 0.9.x deprecation aliases (P3.11) --------------------------------------
# Plain assignments: same class objects under both names, so `except`,
# `isinstance` and subclassing behave identically whichever name a
# consumer uses. Removed at the P3.11 top-level reduction (1.0).
CorrectionError = CorrigendaError
ValidationError = ProposalValidationError


__all__ = [
    "CorrigendaError",
    "ParseError",
    "DuplicateIdError",
    "ProposalValidationError",
    "ProviderError",
    "ConfigurationError",
    "ProjectionError",
    "CorrectionAborted",
    # 0.9.x deprecation aliases (P3.11) — removed at the top-level reduction.
    "CorrectionError",
    "ValidationError",
]
