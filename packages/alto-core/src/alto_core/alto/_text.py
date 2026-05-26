"""Shared text reconstruction for ALTO TextLine elements.

Both the parser (building ``LineManifest.ocr_text``) and the rewriter
(comparing the source XML against corrected text on the UNTOUCHED path)
walk a TextLine's String/SP/HYP children and produce the same logical
string. Centralising the logic prevents the two from drifting apart —
the very bug the rewriter docstring used to document.

The function returned here is the raw, NFC-normalised reconstruction.
Callers that want the "logical" form (parser-side ocr_text) wrap with
``.replace("\\r", "").strip()`` on top.
"""

from __future__ import annotations

from lxml import etree

from alto_core.alto._norm import nfc
from alto_core.alto._ns import _tag


def reconstruct_textline(textline: etree._Element, ns: str) -> str:
    """Walk a TextLine's String/SP/HYP children and rebuild its text.

    Rules:
      - ``String`` contributes its CONTENT attribute verbatim.
      - ``SP`` contributes a single space.
      - ``HYP`` contributes its CONTENT (defaulting to ``"-"``), with
        U+00AD (soft hyphen) collapsed to ``"-"``. If the accumulated
        text already ends with ``"-"``, the HYP is skipped so a String
        ending in ``"-"`` followed by a HYP doesn't produce ``"--"``.

    Returns NFC-normalised text. Does NOT strip or remove carriage
    returns — those are parser-specific concerns.
    """
    string_tag = _tag("String", ns)
    sp_tag = _tag("SP", ns)
    hyp_tag = _tag("HYP", ns)
    parts: list[str] = []
    for child in textline:
        if child.tag == string_tag:
            parts.append(child.get("CONTENT", ""))
        elif child.tag == sp_tag:
            parts.append(" ")
        elif child.tag == hyp_tag:
            hyp_char = child.get("CONTENT", "-")
            if hyp_char == "­":
                hyp_char = "-"
            if hyp_char and not "".join(parts).endswith("-"):
                parts.append(hyp_char)
    return nfc("".join(parts))
