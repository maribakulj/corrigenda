"""corrigenda error hierarchy (SPECS_LIB_V2 §8.4).

A single root, ``CorrectionError``, sits above every error the library
raises so consumers can ``except CorrectionError`` once::

    CorrectionError
    ├── ParseError          (also ValueError)
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


class ValidationError(CorrectionError, ValueError):
    """A producer (LLM / rules / VLM) response failed validation.

    Raised by :func:`corrigenda.pipeline.validator.validate_llm_response`
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
    "ValidationError",
    "CorrectionAborted",
]
