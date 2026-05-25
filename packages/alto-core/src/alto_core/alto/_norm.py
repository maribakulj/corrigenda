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


_INVISIBLE_CHARS = (
    # Soft hyphen — some OCR engines emit it as a hyphen variant; ALTO
    # CONTENT must not carry it (the hyphenation layer reconstructs it
    # from manifest state).
    "­",
    # Zero-width characters that survive most "whitespace" cleaning but
    # corrupt downstream consumers that index by character. Most common
    # leak path: copy-paste of OCR'd PDF text through layout engines.
    "​",  # ZERO WIDTH SPACE
    "‌",  # ZERO WIDTH NON-JOINER
    "‍",  # ZERO WIDTH JOINER
    "﻿",  # ZERO WIDTH NO-BREAK SPACE (BOM)
    # Newlines / carriage returns / tabs — explicitly invalid in a
    # single-line ALTO CONTENT attribute (the validator already rejects
    # `\n`/`\r` in corrected_text, but a CONTENT attribute carrying one
    # would silently corrupt downstream re-parsing).
    "\n",
    "\r",
    "\t",
)

_INVISIBLE_TRANSLATION = str.maketrans({c: "" for c in _INVISIBLE_CHARS})


def clean_content(s: str) -> str:
    """Strip invisible / control characters before writing to ALTO CONTENT.

    Removes:
      - U+00AD (SOFT HYPHEN) — some OCR engines emit it as a hyphen
        variant; ALTO CONTENT should not carry it.
      - Zero-width characters (U+200B, U+200C, U+200D, U+FEFF) — leak
        in via copy-paste of OCR'd PDF text and corrupt character-
        indexed downstream consumers.
      - Newlines / carriage returns / tabs — invalid in a single-line
        ALTO CONTENT attribute. The validator already rejects ``\\n`` /
        ``\\r`` in ``corrected_text``, but a CONTENT attribute with
        one would silently corrupt re-parsing.

    Also strips C0 / C1 control characters (U+0000..U+001F /
    U+007F..U+009F) via a generator filter — these never have legitimate
    semantics in an OCR text node.

    Pre-L10 only U+00AD was stripped; the other invisibles slipped
    through and ended up in CONTENT attributes (L10/R2).
    """
    s = s.translate(_INVISIBLE_TRANSLATION)
    # C0/C1 control chars — never legitimate in OCR text. Filter via
    # generator (cheaper than another translate for an open-ended set).
    return "".join(
        c
        for c in s
        if not (
            ord(c) < 0x20  # C0 (already covers \n \r \t above as defensive)
            or 0x7F <= ord(c) <= 0x9F  # DEL + C1
        )
    )


# --- __all__ (Stage 3 audit remediation) ---
__all__ = [
    "nfc",
    "ncfold",
    "clean_content",
]
