"""Derive the OCR17+ real-corpus cases from ``examples/page/`` (P4.1).

Upstream artifact this script exists for: in the OCR17+ Transkribus
exports, the RAW file's line-level ``TextEquiv`` already carries the
corrected reading — the genuine OCR output (``cukiuent``, ``eft``…)
survives only on the ``Word`` elements. corrigenda reads lines, so a
naive raw-vs-corrected pair is identical at line level and measures
nothing.

Derivation (deterministic, no content is invented):

- ``*.src.page.xml`` — the raw file with each line's ``TextEquiv``
  REPLACED by the join of its own ``Word`` texts (the real OCR,
  mechanically re-exposed at line level). Lines without Words keep
  their original text.
- ``*.ref.page.xml`` — the upstream human-corrected file, verbatim.

Provenance: OCR17+ (Simon Gabay et al., e-ditiones), CC-BY — see
``examples/page/PROVENANCE.md`` and this corpus' README. Re-run after
an upstream re-pin; the outputs are committed so the default suite
stays offline.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from lxml import etree

from corrigenda.formats._xml import make_safe_parser

_REPO_ROOT = Path(__file__).resolve().parents[4]
_EXAMPLES = _REPO_ROOT / "examples" / "page"
_CORPUS = Path(__file__).resolve().parent

_CASES = {
    "ocr17-descartes-discours-p14": (
        "Descartes1637_Discours_btv1b86069594_corrected_0014"
    ),
    "ocr17-lafayette-cleves-p11": ("LaFayette1678_Cleves_btv1b8610820b_corrected_0011"),
}


def _expose_word_ocr(raw_path: Path, out_path: Path) -> int:
    """Write ``raw_path`` with line TextEquiv = join of Word texts.

    Returns the number of lines whose text actually changed.
    """
    tree = etree.parse(str(raw_path), make_safe_parser())
    root = tree.getroot()
    ns = root.tag[1 : root.tag.index("}")]

    def tag(local: str) -> str:
        return f"{{{ns}}}{local}"

    changed = 0
    for line in root.iter(tag("TextLine")):
        words = [
            text
            for w in line.findall(tag("Word"))
            if (text := w.findtext(f"{tag('TextEquiv')}/{tag('Unicode')}"))
        ]
        if not words:
            continue  # no Word markup — keep the line's original text
        unicode_el = line.find(f"{tag('TextEquiv')}/{tag('Unicode')}")
        if unicode_el is None:
            continue
        ocr_text = " ".join(words)
        if unicode_el.text != ocr_text:
            unicode_el.text = ocr_text
            changed += 1
    tree.write(str(out_path), encoding="utf-8", xml_declaration=True)
    return changed


def main() -> None:
    for case, stem in _CASES.items():
        raw = _EXAMPLES / f"{stem}_page_raw.xml"
        corrected = _EXAMPLES / f"{stem}_page_corrected.xml"
        changed = _expose_word_ocr(raw, _CORPUS / f"{case}.src.page.xml")
        shutil.copyfile(corrected, _CORPUS / f"{case}.ref.page.xml")
        print(f"{case}: {changed} lines expose raw Word OCR")


if __name__ == "__main__":
    main()
