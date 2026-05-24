"""Shared Unicode normalization helpers for ALTO text handling.

NFC normalization is essential when comparing strings sourced from
heterogeneous ALTO producers and from LLM outputs: a French word
like 'café' may arrive in precomposed (NFC) or decomposed (NFD) form,
and a naive ``==`` or ``.lower()`` comparison silently fails to
match the two.
"""

from __future__ import annotations

import unicodedata


def nfc(s: str) -> str:
    """Return ``s`` normalized to Unicode NFC form (case preserved)."""
    return unicodedata.normalize("NFC", s)


def ncfold(s: str) -> str:
    """Return ``s`` in NFC and casefolded — for case-insensitive equality.

    Prefer this over ``.lower()`` whenever two strings of unknown origin
    need to compare equal regardless of case and normalization form.
    """
    return unicodedata.normalize("NFC", s).casefold()


def clean_content(s: str) -> str:
    """Strip soft-hyphen (U+00AD) from token text before writing to ALTO CONTENT.

    Some OCR engines emit U+00AD (SOFT HYPHEN) as a hyphen variant.  The
    ALTO CONTENT attribute should never carry invisible control characters,
    so we normalize it to the empty string here rather than in each call site.
    """
    return s.replace("­", "")


# --- __all__ (Stage 3 audit remediation) ---
__all__ = [
    "nfc",
    "ncfold",
    "clean_content",
]
