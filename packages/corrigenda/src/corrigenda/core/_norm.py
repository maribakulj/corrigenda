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
    """Normalise and strip a string before writing to ALTO CONTENT.

    Three responsibilities:

    1. **NFC normalisation** (L10/R1). The parser NFC-normalises every
       CONTENT it READS; the rewriter must do the same on WRITE so the
       on-disk bytes are symmetric across the round-trip. Without this,
       an LLM returning `café` in NFD form (`cafe\\u0301`) would land
       4 NFD bytes on disk; downstream byte-indexed consumers (search
       index, byte-snapshot tests) would diverge from anything that
       re-parses through `parser.py`.

    2. **Invisible-character stripping** (L10/R2):
         - U+00AD (SOFT HYPHEN) — emitted by some OCR engines as a
           hyphen variant; the hyphenation reconciler reconstructs it
           from manifest state, so the raw CONTENT must not carry it.
         - Zero-width characters (U+200B, U+200C, U+200D, U+FEFF) —
           leak in via copy-paste of OCR'd PDF text and corrupt
           character-indexed downstream consumers.
         - Newlines / carriage returns / tabs — invalid in a single-
           line ALTO CONTENT attribute. The validator already rejects
           ``\\n`` / ``\\r`` in ``corrected_text``, but a CONTENT
           attribute with one would silently corrupt re-parsing.

    3. **C0 / C1 control-char stripping** (L10/R2). U+0000..U+001F and
       U+007F..U+009F have no legitimate semantics in an OCR text node.

    Order matters: NFC first so the translate table sees fully
    composed characters (a NFD `é` is two codepoints `e` + combining
    acute, neither of which is in the invisible set; NFC merges them
    into the single codepoint U+00E9 before invisible/control
    stripping runs).
    """
    s = nfc(s)
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
