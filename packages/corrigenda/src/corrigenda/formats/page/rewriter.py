"""PAGE XML rewriter — writes corrected text back without touching geometry.

Implements spec 6.2 P1–P5/P7. Unlike ALTO there is no geometric slow path:
polygons are never rewritten (P1). A modified line is handled as:

  - **P3** — update the canonical (minimal ``@index``) line ``TextEquiv``:
    set its ``Unicode`` (and ``PlainText`` if present), drop its stale
    ``@conf``, and delete the alternative line-level ``TextEquiv`` (they
    described the old reading). Create the canonical one if the line only
    carried word-level text.
  - **P4** — word elements. When the corrected word count matches the
    number of ``Word`` children, update each ``Word``'s canonical
    ``TextEquiv`` in place, keep its ``Coords``, drop its ``@conf`` (fast
    path). When the count changed, the ``Word`` children are removed and
    the text lives at line level — fabricating word polygons on a skewed
    line would be more dishonest than ALTO's bbox approximation; the loss
    of word granularity is counted (slow path).
  - **P5** — the original hyphen character is preserved verbatim: a
    producer may not normalise ``¬`` → ``-`` (E5 extended).
  - **P7** — ``make_safe_parser`` throughout; provenance recorded as a
    ``MetadataItem`` on 2019+ schemas, else appended to ``Metadata/Comments``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from lxml import etree

from corrigenda.core._norm import nfc
from corrigenda.core.pairing import HYPHEN_CHARS, trailing_hyphen_char
from corrigenda.errors import DuplicateIdError
from corrigenda.core.schemas import LineManifest, PageManifest
from corrigenda.formats.page._custom import strip_offset_groups
from corrigenda.formats.page._ns import (
    _detect_namespace,
    _tag,
    make_safe_parser,
    supports_metadata_item,
)
from corrigenda.formats.page._text import (
    canonical_line_text,
    canonical_textequiv,
    word_text,
)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


@dataclass
class PageRewriterMetrics:
    """Per-path line counts for the PAGE rewriter.

    ``untouched``/``subs_only``/``fast_path``/``slow_path`` satisfy the core
    ``RewriteMetrics`` port so the pipeline treats PAGE like ALTO;
    ``subs_only`` is always 0 (PAGE has no SUBS markup). The remaining
    counters feed the CorrectionReport's PAGE-specific losses (6.2).
    """

    untouched: int = 0
    subs_only: int = 0  # never used by PAGE; present for RewriteMetrics parity
    fast_path: int = 0
    slow_path: int = 0

    # PAGE-specific provenance of what was dropped / detected.
    words_dropped: int = 0
    conf_dropped: int = 0
    alt_textequiv_dropped: int = 0
    custom_offset_stripped: int = 0
    hyphen_preserved: int = 0
    line_word_disagreement: int = 0

    @property
    def total_processed(self) -> int:
        return self.subs_only + self.fast_path + self.slow_path

    @property
    def total_lines(self) -> int:
        return self.untouched + self.total_processed

    def as_losses(self) -> dict[str, int]:
        """Non-zero PAGE-specific counters, for ``CorrectionReport.format_losses``."""
        raw = {
            "words_dropped": self.words_dropped,
            "conf_dropped": self.conf_dropped,
            "alt_textequiv_dropped": self.alt_textequiv_dropped,
            "custom_offset_stripped": self.custom_offset_stripped,
            "hyphen_preserved": self.hyphen_preserved,
            "line_word_disagreement": self.line_word_disagreement,
        }
        return {k: v for k, v in raw.items() if v}


# ---------------------------------------------------------------------------
# Low-level element helpers
# ---------------------------------------------------------------------------


def _direct(el: etree._Element, local: str, ns: str) -> list[etree._Element]:
    want = _tag(local, ns)
    return [c for c in el if c.tag == want]


def _drop_conf(te: etree._Element) -> bool:
    """Remove a stale ``@conf`` from a TextEquiv. Returns True if one went."""
    if "conf" in te.attrib:
        del te.attrib["conf"]
        return True
    return False


def _set_textequiv_text(te: etree._Element, text: str, ns: str) -> None:
    """Set a TextEquiv's ``Unicode`` (create if missing) and its
    ``PlainText`` when one is present. Geometry/order untouched."""
    uni = te.find(_tag("Unicode", ns))
    if uni is None:
        uni = etree.SubElement(te, _tag("Unicode", ns))
    uni.text = text
    plain = te.find(_tag("PlainText", ns))
    if plain is not None:
        plain.text = text


def _insertion_index(tl: etree._Element, ns: str) -> int:
    """Where to insert a new line-level TextEquiv: before a line ``TextStyle``
    if one exists, else at the end. Keeps the PAGE child sequence valid."""
    style_tag = _tag("TextStyle", ns)
    for i, child in enumerate(tl):
        if child.tag == style_tag:
            return i
    return len(tl)


def _update_line_textequiv(
    tl: etree._Element, text: str, ns: str, metrics: PageRewriterMetrics
) -> None:
    """P3: update the canonical line TextEquiv, drop @conf and alternatives.

    Creates the canonical TextEquiv when the line had none (word-only line).
    """
    equivs = _direct(tl, "TextEquiv", ns)
    if not equivs:
        te = etree.Element(_tag("TextEquiv", ns))
        _set_textequiv_text(te, text, ns)
        tl.insert(_insertion_index(tl, ns), te)
        return

    canonical = canonical_textequiv(tl, ns)
    assert canonical is not None  # equivs non-empty
    _set_textequiv_text(canonical, text, ns)
    if _drop_conf(canonical):
        metrics.conf_dropped += 1
    # Remove the alternatives — they described the old reading (P3).
    for te in equivs:
        if te is not canonical:
            tl.remove(te)
            metrics.alt_textequiv_dropped += 1


def _update_words_fast(
    tl: etree._Element,
    word_els: list[etree._Element],
    words: list[str],
    ns: str,
    metrics: PageRewriterMetrics,
) -> None:
    """P4 fast path: word count unchanged — update each Word's canonical
    TextEquiv in place, keep Coords, drop @conf, remove word alternatives."""
    for w_el, token in zip(word_els, words):
        equivs = _direct(w_el, "TextEquiv", ns)
        canonical = canonical_textequiv(w_el, ns)
        if canonical is None:
            canonical = etree.SubElement(w_el, _tag("TextEquiv", ns))
        _set_textequiv_text(canonical, token, ns)
        if _drop_conf(canonical):
            metrics.conf_dropped += 1
        for te in equivs:
            if te is not canonical:
                w_el.remove(te)
                metrics.alt_textequiv_dropped += 1


def _remove_words(
    tl: etree._Element,
    word_els: list[etree._Element],
    metrics: PageRewriterMetrics,
) -> None:
    """P4 slow path: word count changed — drop Word children (text lives at
    line level). Word polygons on a skewed line would be dishonest."""
    for w_el in word_els:
        tl.remove(w_el)
        metrics.words_dropped += 1


def _strip_custom_offsets(el: etree._Element, metrics: PageRewriterMetrics) -> None:
    """P6: drop offset-anchored ``custom`` groups whose ranges are now stale.

    Structural groups (readingOrder/structure) are preserved verbatim. When
    nothing offset-anchored remains the attribute is left as-is; when it
    empties out entirely it is removed rather than left blank.
    """
    custom = el.get("custom")
    if not custom:
        return
    new_custom, removed = strip_offset_groups(custom)
    if removed == 0:
        return
    metrics.custom_offset_stripped += removed
    if new_custom:
        el.set("custom", new_custom)
    else:
        del el.attrib["custom"]


def _preserve_hyphen(source_text: str, corrected: str) -> str:
    """P5 / E5-extended: if the source line ended in a word-break hyphen,
    force the corrected line to end in the SAME character (no ``¬`` → ``-``
    normalisation). Internal spacing is preserved."""
    src_h = trailing_hyphen_char(source_text, HYPHEN_CHARS)
    if src_h is None:
        return corrected
    stripped = corrected.rstrip()
    trailing_ws = corrected[len(stripped) :]
    for ch in HYPHEN_CHARS:
        if stripped.endswith(ch):
            if ch != src_h:
                stripped = stripped[:-1] + src_h
            break
    return stripped + trailing_ws


# ---------------------------------------------------------------------------
# Provenance (P7)
# ---------------------------------------------------------------------------


def _provenance_text(
    provider: str,
    model: str,
    lib_version: str | None,
    config_fingerprint: str | None,
) -> str:
    provenance = "corrigenda"
    if lib_version:
        provenance += f" {lib_version}"
    if config_fingerprint:
        provenance += f"; config {config_fingerprint}"
    return f"Post-OCR correction via {provider}/{model} ({provenance})"


def _add_provenance(
    root: etree._Element,
    ns: str,
    provider: str,
    model: str,
    lib_version: str | None,
    config_fingerprint: str | None,
) -> None:
    """Record the correction pass (P7): a ``MetadataItem`` on 2019+ schemas,
    else appended to ``Metadata/Comments``. No wall-clock timestamp is
    written, keeping the output deterministic (byte-stable for a given
    input) — the same choice the ALTO ``processingStep`` makes."""
    metadata = root.find(_tag("Metadata", ns))
    if metadata is None:
        return
    desc = _provenance_text(provider, model, lib_version, config_fingerprint)

    if supports_metadata_item(ns):
        item = etree.SubElement(metadata, _tag("MetadataItem", ns))
        item.set("type", "processingStep")
        item.set("name", "corrigenda")
        item.set("value", desc)
        return

    comments = metadata.find(_tag("Comments", ns))
    if comments is None:
        comments = etree.SubElement(metadata, _tag("Comments", ns))
        comments.text = desc
    else:
        existing = comments.text or ""
        comments.text = f"{existing}\n{desc}" if existing.strip() else desc


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def rewrite_page_file(
    xml_path: Path,
    page_manifests: list[PageManifest],
    provider: str,
    model: str,
    *,
    lib_version: str | None = None,
    config_fingerprint: str | None = None,
) -> tuple[bytes, PageRewriterMetrics, dict[str, str]]:
    """Rewrite a PAGE XML file with corrected text from ``page_manifests``.

    Returns ``(xml_bytes, metrics, line_id -> path)`` where path is one of
    ``untouched`` / ``fast_path`` / ``slow_path`` (never ``subs_only``).
    """
    tree = etree.parse(str(xml_path), make_safe_parser())
    root = tree.getroot()
    ns = _detect_namespace(root)
    metrics = PageRewriterMetrics()
    line_paths: dict[str, str] = {}

    # P0-5 — a bare line_id keys every correction-to-element association
    # below; duplicates (manifest or element side) fail loudly instead of
    # silently rewriting the wrong physical line. Mirrors the ALTO rewriter.
    line_by_id: dict[str, LineManifest] = {}
    for page in page_manifests:
        for lm in page.lines:
            if lm.line_id in line_by_id:
                raise DuplicateIdError(
                    f"duplicate line_id {lm.line_id!r} across page manifests "
                    f"for {xml_path.name!r} — correction-to-line association "
                    "would be ambiguous (P0-5)."
                )
            line_by_id[lm.line_id] = lm

    seen_element_ids: set[str] = set()
    textline_tag = _tag("TextLine", ns)
    for tl in root.iter(textline_tag):
        line_id = tl.get("id")
        if line_id not in line_by_id:
            continue
        if line_id in seen_element_ids:
            raise DuplicateIdError(
                f"duplicate TextLine id {line_id!r} in {xml_path.name!r} — "
                "two physical lines would receive the same correction (P0-5)."
            )
        seen_element_ids.add(line_id)
        lm = line_by_id[line_id]

        source_text = canonical_line_text(tl, ns)
        raw_corrected = (
            lm.corrected_text if lm.corrected_text is not None else lm.ocr_text
        )
        corrected = (
            _preserve_hyphen(source_text, nfc(raw_corrected)).replace("\r", "").strip()
        )

        # P2 diagnostic — line text vs word concat disagreement (line wins).
        word_els = _direct(tl, "Word", ns)
        if word_els and _direct(tl, "TextEquiv", ns):
            concat = nfc(
                " ".join(t for w in word_els if (t := word_text(w, ns)))
            ).strip()
            if concat and concat != source_text:
                metrics.line_word_disagreement += 1

        # --- Path 1: UNTOUCHED ---
        if corrected == source_text:
            metrics.untouched += 1
            line_paths[line_id] = "untouched"
            continue

        if trailing_hyphen_char(source_text, HYPHEN_CHARS) is not None:
            metrics.hyphen_preserved += 1

        words = corrected.split()

        # --- P4 word handling ---
        if word_els and len(words) != len(word_els):
            _remove_words(tl, word_els, metrics)
            path = "slow_path"
        else:
            if word_els:
                _update_words_fast(tl, word_els, words, ns, metrics)
                # P6 — surviving Words: strip their stale offset groups.
                for w_el in word_els:
                    _strip_custom_offsets(w_el, metrics)
            path = "fast_path"

        # --- P3 line-level update (both paths) ---
        _update_line_textequiv(tl, corrected, ns, metrics)
        # --- P6 line-level custom: offsets into the old text are now stale ---
        _strip_custom_offsets(tl, metrics)

        if path == "fast_path":
            metrics.fast_path += 1
        else:
            metrics.slow_path += 1
        line_paths[line_id] = path

    _add_provenance(root, ns, provider, model, lib_version, config_fingerprint)
    xml_bytes = etree.tostring(
        root, xml_declaration=True, encoding="UTF-8", pretty_print=False
    )
    return xml_bytes, metrics, line_paths


def extract_output_texts(xml_bytes: bytes, line_ids: set[str]) -> dict[str, str]:
    """Re-extract canonical line text from rewritten PAGE XML for the given
    line IDs (trace/report), matching the parser's reconstruction."""
    root = etree.fromstring(xml_bytes, make_safe_parser())
    ns = _detect_namespace(root)
    textline_tag = _tag("TextLine", ns)
    result: dict[str, str] = {}
    for tl in root.iter(textline_tag):
        line_id = tl.get("id")
        if line_id in line_ids:
            if line_id in result:
                # P0-5 — a repeated id would silently collapse two physical
                # lines into one trace entry.
                raise DuplicateIdError(
                    f"duplicate TextLine id {line_id!r} in rewritten PAGE — "
                    "output-text extraction would be ambiguous (P0-5)."
                )
            result[line_id] = canonical_line_text(tl, ns)
    return result


__all__ = [
    "PageRewriterMetrics",
    "rewrite_page_file",
    "extract_output_texts",
]
