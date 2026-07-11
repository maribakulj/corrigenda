"""corrigenda error hierarchy (SPECS_LIB_V2 §8.4).

A single root, ``CorrectionError``, sits above every error the library
raises so consumers can ``except CorrectionError`` once::

    CorrectionError
    ├── ParseError          (also ValueError)
    │   └── DuplicateIdError       — ambiguous identities in source/manifest
    ├── ValidationError     (also ValueError) — producer response invalid
    │   └── HyphenIntegrityError   (defined in pipeline.validator)
    └── CorrectionAborted

The value-shaped errors additionally inherit ``ValueError`` so the bare
``ValueError`` raises that predate this hierarchy keep working under
``except ValueError`` (§8.4) — and the pipeline's retry classifier, which
routes ``(ValueError, json.JSONDecodeError)`` to the malformed-output
branch, still catches them.
"""

from __future__ import annotations


class CorrectionError(Exception):
    """Base class for every error raised by corrigenda (§8.4)."""


class ParseError(CorrectionError, ValueError):
    """A source document could not be parsed into a manifest.

    Inherits ``ValueError`` for backwards compatibility with call sites
    that caught the bare ``ValueError`` the parser used to raise.
    """


class DuplicateIdError(ParseError):
    """A source document or manifest set carries duplicate identities (P0-5).

    Every internal association between a correction and its physical line
    (rewriter lookup, trace projection, hyphen partner resolution) is keyed
    by ``line_id`` within one source file. A document where two ``TextLine``
    elements share an ID — or a hand-built manifest repeating a ``line_id``
    within one file — is *ambiguous*: silently continuing would risk applying
    a correction to the wrong physical line. The library refuses such input
    explicitly instead of guessing.

    Inherits :class:`ParseError` (hence ``CorrectionError`` and
    ``ValueError``) so existing ``except ParseError`` call sites keep
    working.
    """


class ValidationError(CorrectionError, ValueError):
    """A producer (LLM / rules / VLM) response failed validation.

    Raised by :func:`corrigenda.core.validator.validate_llm_response`
    for structural problems (missing/duplicate/unknown line ids, wrong
    count, empty or multi-line ``corrected_text``, …). Inherits
    ``ValueError`` so the retry classifier keeps routing it to the
    malformed-output branch.
    """


class CorrectionAborted(CorrectionError):
    """Raised when ``should_abort()`` requested cancellation (F10).

    The pipeline probes the caller-supplied ``should_abort`` callback
    between chunks and between pages. When it returns ``True`` the run
    stops and this exception propagates out of ``run()`` **before any
    output is written** — the corrected XML and trace are not persisted.
    Provider calls already in flight are not interrupted (the cancellation
    is cooperative, observed only at chunk/page boundaries).
    """


__all__ = [
    "CorrectionError",
    "ParseError",
    "DuplicateIdError",
    "ValidationError",
    "CorrectionAborted",
]
