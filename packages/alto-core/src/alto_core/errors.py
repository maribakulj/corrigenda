"""alto-core error hierarchy (SPECS_LIB_V2 §8.4).

A single root, ``CorrectionError``, sits above every error the library
raises so consumers can ``except CorrectionError`` once. The value-shaped
errors additionally inherit ``ValueError`` so existing ``except
ValueError`` call sites keep working (§8.4).

This module is introduced with F10 (``CorrectionAborted``); the remaining
members (``ParseError``, ``ValidationError``, and the re-parenting of
``HyphenIntegrityError``) land in the API-surface slice.
"""

from __future__ import annotations


class CorrectionError(Exception):
    """Base class for every error raised by alto-core (§8.4)."""


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
    "CorrectionAborted",
]
