from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from pathlib import Path

from lxml import etree

from alto_core.alto._norm import clean_content, nfc
from alto_core.alto._ns import _detect_namespace, _tag
from alto_core.schemas import HyphenRole, LineManifest, PageManifest

# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


@dataclass
class RewriterMetrics:
    """Counts of lines per rewriter path."""

    untouched: int = 0
    subs_only: int = 0
    fast_path: int = 0
    slow_path: int = 0

    @property
    def total_processed(self) -> int:
        return self.subs_only + self.fast_path + self.slow_path

    @property
    def total_lines(self) -> int:
        return self.untouched + self.subs_only + self.fast_path + self.slow_path


# ---------------------------------------------------------------------------
# Tokenisation
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> list[str]:
    """Split text into alternating word/space tokens, dropping empty strings."""
    return [t for t in re.split(r"(\s+)", text) if t]


# ---------------------------------------------------------------------------
# Geometry (slow-path only)
# ---------------------------------------------------------------------------


def _compute_geometry(
    hpos: int,
    width: int,
    tokens: list[str],
) -> list[tuple[str, int, int]]:
    """
    Return list of (token, token_hpos, token_width) for every token.

    Space tokens get proportional width; word tokens get proportional width.
    The last token is adjusted so sum(widths) == width exactly.
    """
    if not tokens:
        return []

    total_chars = sum(len(t) for t in tokens)
    if total_chars == 0:
        per = width // len(tokens) if tokens else 0
        return [(t, hpos + i * per, per) for i, t in enumerate(tokens)]

    unit = width / total_chars
    raw_widths: list[int] = []
    for t in tokens:
        if t.strip() == "":
            w = max(1, round(len(t) * 0.6 * unit))
        else:
            w = max(1, round(len(t) * unit))
        raw_widths.append(w)

    correction = width - sum(raw_widths)
    raw_widths[-1] = max(1, raw_widths[-1] + correction)

    result: list[tuple[str, int, int]] = []
    cursor = hpos
    for t, w in zip(tokens, raw_widths):
        result.append((t, cursor, w))
        cursor += w
    return result


# ---------------------------------------------------------------------------
# Element accessors (non-destructive)
# ---------------------------------------------------------------------------


def _get_string_children(el: etree._Element, ns: str) -> list[etree._Element]:
    tag = _tag("String", ns)
    return [c for c in el if c.tag == tag]


def _get_sp_children(el: etree._Element, ns: str) -> list[etree._Element]:
    tag = _tag("SP", ns)
    return [c for c in el if c.tag == tag]


def _get_hyp_children(el: etree._Element, ns: str) -> list[etree._Element]:
    tag = _tag("HYP", ns)
    return [c for c in el if c.tag == tag]


# ---------------------------------------------------------------------------
# Text comparison
# ---------------------------------------------------------------------------


def _extract_text_from_line(el: etree._Element, ns: str) -> str:
    """Reconstruct text from a TextLine's children (String + SP + HYP).

    Applies the same normalization as the parser: soft-hyphen → "-",
    and HYP is skipped if preceding CONTENT already ends with "-".
    """
    string_tag = _tag("String", ns)
    sp_tag = _tag("SP", ns)
    hyp_tag = _tag("HYP", ns)
    parts: list[str] = []
    for child in el:
        if child.tag == string_tag:
            parts.append(child.get("CONTENT", ""))
        elif child.tag == sp_tag:
            parts.append(" ")
        elif child.tag == hyp_tag:
            hyp_char = child.get("CONTENT", "-")
            if hyp_char == "\u00ad":
                hyp_char = "-"
            current = "".join(parts)
            if current.endswith("-"):
                continue
            if hyp_char:
                parts.append(hyp_char)
    # NFC-normalize so equality vs. parser-produced ocr_text is reliable
    # on corpora that mix NFC/NFD (the parser normalizes; this used not to).
    return nfc("".join(parts))


def _line_text_unchanged(el: etree._Element, corrected: str, ns: str) -> bool:
    return _extract_text_from_line(el, ns) == nfc(corrected)


# ---------------------------------------------------------------------------
# SUBS attribute logic (centralized — the ONLY place SUBS is written)
# ---------------------------------------------------------------------------


def _desired_subs(
    manifest: LineManifest,
) -> tuple[str | None, str | None]:
    """Return (wanted_subs_type, wanted_subs_content) for the primary role.

    For PART1: backward subs on last String.
    For PART2: backward subs on first String.
    For BOTH: backward subs on first String (forward handled separately).
    """
    if manifest.hyphen_role == HyphenRole.PART1:
        if manifest.hyphen_source_explicit and manifest.hyphen_subs_content:
            return "HypPart1", manifest.hyphen_subs_content
    elif manifest.hyphen_role == HyphenRole.PART2:
        if manifest.hyphen_source_explicit and manifest.hyphen_subs_content:
            return "HypPart2", manifest.hyphen_subs_content
    elif manifest.hyphen_role == HyphenRole.BOTH:
        # Backward (PART2 side) on first String
        if manifest.hyphen_source_explicit and manifest.hyphen_subs_content:
            return "HypPart2", manifest.hyphen_subs_content
    return None, None


def _desired_forward_subs(
    manifest: LineManifest,
) -> tuple[str | None, str | None]:
    """Return (wanted_subs_type, wanted_subs_content) for the forward/PART1 role.

    Only applies to BOTH lines.
    """
    if manifest.hyphen_role != HyphenRole.BOTH:
        return None, None
    if manifest.hyphen_forward_explicit and manifest.hyphen_forward_subs_content:
        return "HypPart1", manifest.hyphen_forward_subs_content
    return None, None


def _subs_target(
    el: etree._Element,
    manifest: LineManifest,
    ns: str,
) -> etree._Element | None:
    """Return the String element that should carry backward SUBS attributes."""
    strings = _get_string_children(el, ns)
    if not strings:
        return None
    if manifest.hyphen_role == HyphenRole.PART1:
        return strings[-1]
    if manifest.hyphen_role == HyphenRole.PART2:
        return strings[0]
    if manifest.hyphen_role == HyphenRole.BOTH:
        return strings[0]  # backward (PART2) subs on first String
    return None


def _subs_need_update(
    el: etree._Element,
    manifest: LineManifest,
    ns: str,
) -> bool:
    """Return True if the XML SUBS state differs from the desired state."""
    if manifest.hyphen_role == HyphenRole.NONE:
        return False

    # Check backward subs
    want_type, want_content = _desired_subs(manifest)
    target = _subs_target(el, manifest, ns)
    if target is None:
        if want_type is not None:
            return True
    elif (
        target.get("SUBS_TYPE") != want_type
        or target.get("SUBS_CONTENT") != want_content
    ):
        return True

    # Check forward subs for BOTH lines
    if manifest.hyphen_role == HyphenRole.BOTH:
        fw_type, fw_content = _desired_forward_subs(manifest)
        strings = _get_string_children(el, ns)
        if strings:
            last = strings[-1]
            if (
                last.get("SUBS_TYPE") != fw_type
                or last.get("SUBS_CONTENT") != fw_content
            ):
                return True

    return False


def _set_subs_on_element(
    target: etree._Element,
    want_type: str | None,
    want_content: str | None,
) -> None:
    """Set or remove SUBS_TYPE/SUBS_CONTENT on a single element."""
    if want_type and want_content:
        target.set("SUBS_TYPE", want_type)
        target.set("SUBS_CONTENT", want_content)
    else:
        for attr in ("SUBS_TYPE", "SUBS_CONTENT"):
            if attr in target.attrib:
                del target.attrib[attr]


def _apply_subs(
    el: etree._Element,
    manifest: LineManifest,
    ns: str,
) -> None:
    """Set or remove SUBS_TYPE/SUBS_CONTENT on the correct String element(s)."""
    # Backward subs
    target = _subs_target(el, manifest, ns)
    if target is not None:
        want_type, want_content = _desired_subs(manifest)
        _set_subs_on_element(target, want_type, want_content)

    # Forward subs for BOTH lines (on last String)
    if manifest.hyphen_role == HyphenRole.BOTH:
        strings = _get_string_children(el, ns)
        if strings:
            last = strings[-1]
            fw_type, fw_content = _desired_forward_subs(manifest)
            _set_subs_on_element(last, fw_type, fw_content)


# ---------------------------------------------------------------------------
# Fast path: in-place CONTENT update (word count unchanged)
# ---------------------------------------------------------------------------


def _update_content_in_place(
    el: etree._Element,
    corrected: str,
    ns: str,
) -> bool:
    """
    When word count matches, update only CONTENT on existing String elements.

    Returns True on success. ALL other attributes (ID, HPOS, VPOS, WIDTH,
    HEIGHT, WC, CC, STYLEREFS, etc.) and SP/HYP elements stay untouched.
    """
    orig_strings = _get_string_children(el, ns)
    words = [t for t in _tokenize(corrected) if t.strip()]
    if len(words) != len(orig_strings):
        return False
    for string_el, word in zip(orig_strings, words):
        string_el.set("CONTENT", clean_content(word))
    return True


# ---------------------------------------------------------------------------
# Internal: clear existing String/SP/HYP children
# ---------------------------------------------------------------------------


def _clear_line(el: etree._Element, ns: str) -> None:
    """Remove String/SP/HYP children.  TextLine attributes are untouched."""
    tags = {_tag("String", ns), _tag("SP", ns), _tag("HYP", ns)}
    for c in [c for c in el if c.tag in tags]:
        el.remove(c)


# ---------------------------------------------------------------------------
# Slow path: rebuild when word count changed
# ---------------------------------------------------------------------------


def _rebuild_normal_line(
    el: etree._Element,
    corrected: str,
    manifest: LineManifest,
    ns: str,
) -> None:
    """Slow-path rebuild for a normal (non-hyphenated) TextLine."""
    orig_string_attribs = [dict(s.attrib) for s in _get_string_children(el, ns)]
    orig_sp_attribs = [dict(s.attrib) for s in _get_sp_children(el, ns)]
    saved_hyp = [copy.deepcopy(c) for c in el if c.tag == _tag("HYP", ns)]

    _clear_line(el, ns)

    hpos = int(el.get("HPOS", 0))
    vpos = int(el.get("VPOS", 0))
    width = int(el.get("WIDTH", 0))
    height = int(el.get("HEIGHT", 0))

    tokens = _tokenize(corrected)
    if not tokens:
        for h in saved_hyp:
            el.append(h)
        return

    geo = _compute_geometry(hpos, width, tokens)
    str_n = sp_n = 0

    for token, tok_hpos, tok_width in geo:
        if token.strip() == "":
            sp = etree.SubElement(el, _tag("SP", ns))
            if sp_n < len(orig_sp_attribs):
                for k, v in orig_sp_attribs[sp_n].items():
                    sp.set(k, v)
            else:
                sp.set("WIDTH", str(tok_width))
                sp.set("HPOS", str(tok_hpos))
                sp.set("VPOS", str(vpos))
            sp_n += 1
        else:
            s = etree.SubElement(el, _tag("String", ns))
            if str_n < len(orig_string_attribs):
                for k, v in orig_string_attribs[str_n].items():
                    if k not in ("SUBS_TYPE", "SUBS_CONTENT"):
                        s.set(k, v)
                s.set("CONTENT", clean_content(token))
                s.set("HPOS", str(tok_hpos))
                s.set("WIDTH", str(tok_width))
            else:
                s.set("ID", f"{manifest.line_id}_STR_{str_n:04d}")
                s.set("CONTENT", clean_content(token))
                s.set("HPOS", str(tok_hpos))
                s.set("VPOS", str(vpos))
                s.set("WIDTH", str(tok_width))
                s.set("HEIGHT", str(height))
            str_n += 1

    for h in saved_hyp:
        el.append(h)


def _rebuild_hyp_part1(
    el: etree._Element,
    corrected: str,
    manifest: LineManifest,
    ns: str,
) -> None:
    """Slow-path rebuild for a PART1 (hyphen-left) TextLine."""
    orig_string_attribs = [dict(s.attrib) for s in _get_string_children(el, ns)]
    orig_sp_attribs = [dict(s.attrib) for s in _get_sp_children(el, ns)]
    orig_hyps = _get_hyp_children(el, ns)
    orig_hyp_attribs = dict(orig_hyps[0].attrib) if orig_hyps else {}

    _clear_line(el, ns)

    hpos = int(el.get("HPOS", 0))
    vpos = int(el.get("VPOS", 0))
    width = int(el.get("WIDTH", 0))
    height = int(el.get("HEIGHT", 0))

    hyp_width = max(1, round(width * 0.04))
    text_width = max(1, width - hyp_width)

    tokens = _tokenize(corrected)
    if not tokens:
        hyp = etree.SubElement(el, _tag("HYP", ns))
        if orig_hyp_attribs:
            for k, v in orig_hyp_attribs.items():
                hyp.set(k, v)
        else:
            hyp.set("CONTENT", "-")
            hyp.set("HPOS", str(hpos + text_width))
            hyp.set("VPOS", str(vpos))
            hyp.set("WIDTH", str(hyp_width))
            hyp.set("HEIGHT", str(height))
        return

    geo = _compute_geometry(hpos, text_width, tokens)
    str_n = sp_n = 0
    last_word_hpos = hpos
    last_word_width = hyp_width

    for token, tok_hpos, tok_width in geo:
        if token.strip() == "":
            sp = etree.SubElement(el, _tag("SP", ns))
            if sp_n < len(orig_sp_attribs):
                for k, v in orig_sp_attribs[sp_n].items():
                    sp.set(k, v)
            else:
                sp.set("WIDTH", str(tok_width))
                sp.set("HPOS", str(tok_hpos))
                sp.set("VPOS", str(vpos))
            sp_n += 1
        else:
            s = etree.SubElement(el, _tag("String", ns))
            if str_n < len(orig_string_attribs):
                for k, v in orig_string_attribs[str_n].items():
                    if k not in ("SUBS_TYPE", "SUBS_CONTENT"):
                        s.set(k, v)
                s.set("CONTENT", clean_content(token))
                s.set("HPOS", str(tok_hpos))
                s.set("WIDTH", str(tok_width))
            else:
                s.set("ID", f"{manifest.line_id}_STR_{str_n:04d}")
                s.set("CONTENT", clean_content(token))
                s.set("HPOS", str(tok_hpos))
                s.set("VPOS", str(vpos))
                s.set("WIDTH", str(tok_width))
                s.set("HEIGHT", str(height))

            last_word_hpos = tok_hpos
            last_word_width = tok_width
            str_n += 1

    # Append HYP element preserving all original attributes
    hyp = etree.SubElement(el, _tag("HYP", ns))
    if orig_hyp_attribs:
        for k, v in orig_hyp_attribs.items():
            hyp.set(k, v)
    else:
        hyp.set("CONTENT", "-")
        hyp.set("HPOS", str(last_word_hpos + last_word_width))
        hyp.set("VPOS", str(vpos))
        hyp.set("WIDTH", str(hyp_width))
        hyp.set("HEIGHT", str(height))


def _rebuild_hyp_part2(
    el: etree._Element,
    corrected: str,
    manifest: LineManifest,
    ns: str,
) -> None:
    """Slow-path rebuild for a PART2 (hyphen-right) TextLine."""
    orig_string_attribs = [dict(s.attrib) for s in _get_string_children(el, ns)]
    orig_sp_attribs = [dict(s.attrib) for s in _get_sp_children(el, ns)]

    _clear_line(el, ns)

    hpos = int(el.get("HPOS", 0))
    vpos = int(el.get("VPOS", 0))
    width = int(el.get("WIDTH", 0))
    height = int(el.get("HEIGHT", 0))

    tokens = _tokenize(corrected)
    if not tokens:
        return

    geo = _compute_geometry(hpos, width, tokens)
    str_n = sp_n = 0

    for token, tok_hpos, tok_width in geo:
        if token.strip() == "":
            sp = etree.SubElement(el, _tag("SP", ns))
            if sp_n < len(orig_sp_attribs):
                for k, v in orig_sp_attribs[sp_n].items():
                    sp.set(k, v)
            else:
                sp.set("WIDTH", str(tok_width))
                sp.set("HPOS", str(tok_hpos))
                sp.set("VPOS", str(vpos))
            sp_n += 1
        else:
            s = etree.SubElement(el, _tag("String", ns))
            if str_n < len(orig_string_attribs):
                for k, v in orig_string_attribs[str_n].items():
                    if k not in ("SUBS_TYPE", "SUBS_CONTENT"):
                        s.set(k, v)
                s.set("CONTENT", clean_content(token))
                s.set("HPOS", str(tok_hpos))
                s.set("WIDTH", str(tok_width))
            else:
                s.set("ID", f"{manifest.line_id}_STR_{str_n:04d}")
                s.set("CONTENT", clean_content(token))
                s.set("HPOS", str(tok_hpos))
                s.set("VPOS", str(vpos))
                s.set("WIDTH", str(tok_width))
                s.set("HEIGHT", str(height))
            str_n += 1


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def rewrite_alto_file(
    xml_path: Path,
    page_manifests: list[PageManifest],
    provider: str,
    model: str,
) -> tuple[bytes, RewriterMetrics, dict[str, str]]:
    """
    Rewrite an ALTO XML file with corrected text from page_manifests.

    Follows a 4-path strategy:
      Path 1 — UNTOUCHED:  text same + SUBS same → skip entirely
      Path 2 — SUBS-ONLY:  text same + SUBS changed → in-place SUBS update
      Path 3 — FAST PATH:  text changed + word count same → in-place CONTENT + SUBS
      Path 4 — SLOW PATH:  word count changed → rebuild line + SUBS

    Returns (rewritten_xml_bytes, metrics, line_rewriter_paths).
    line_rewriter_paths maps line_id → "untouched"/"subs_only"/"fast_path"/"slow_path".
    """
    tree = etree.parse(str(xml_path))
    root = tree.getroot()
    ns = _detect_namespace(root)
    metrics = RewriterMetrics()
    line_paths: dict[str, str] = {}

    line_by_id: dict[str, LineManifest] = {}
    for page in page_manifests:
        for lm in page.lines:
            line_by_id[lm.line_id] = lm

    textline_tag = _tag("TextLine", ns)
    for tl_el in root.iter(textline_tag):
        line_id = tl_el.get("ID")
        if line_id not in line_by_id:
            continue
        lm = line_by_id[line_id]

        corrected = lm.corrected_text if lm.corrected_text is not None else lm.ocr_text
        text_changed = not _line_text_unchanged(tl_el, corrected, ns)
        subs_changed = _subs_need_update(tl_el, lm, ns)

        # --- Path 1: UNTOUCHED ---
        if not text_changed and not subs_changed:
            metrics.untouched += 1
            line_paths[line_id] = "untouched"
            continue

        # --- Path 2: SUBS-ONLY ---
        if not text_changed:
            _apply_subs(tl_el, lm, ns)
            metrics.subs_only += 1
            line_paths[line_id] = "subs_only"
            continue

        # --- Path 3: FAST PATH (word count same) ---
        if _update_content_in_place(tl_el, corrected, ns):
            _apply_subs(tl_el, lm, ns)
            metrics.fast_path += 1
            line_paths[line_id] = "fast_path"
            continue

        # --- Path 4: SLOW PATH (word count changed) ---
        if lm.hyphen_role in (HyphenRole.PART1, HyphenRole.BOTH):
            _rebuild_hyp_part1(tl_el, corrected, lm, ns)
        elif lm.hyphen_role == HyphenRole.PART2:
            _rebuild_hyp_part2(tl_el, corrected, lm, ns)
        else:
            _rebuild_normal_line(tl_el, corrected, lm, ns)
        _apply_subs(tl_el, lm, ns)
        metrics.slow_path += 1
        line_paths[line_id] = "slow_path"

    _add_processing_entry(root, ns, provider, model)
    # pretty_print=False: avoid gratuitously reformatting the entire XML
    # (whitespace between elements) when the user only changed CONTENT on a
    # handful of lines. Users comparing source vs. output should see only
    # real diffs.
    xml_bytes = etree.tostring(
        root, xml_declaration=True, encoding="UTF-8", pretty_print=False
    )
    return xml_bytes, metrics, line_paths


def extract_output_texts(xml_bytes: bytes, line_ids: set[str]) -> dict[str, str]:
    """Re-extract text from rewritten ALTO XML for the given line IDs.

    Uses the same _extract_text_from_line logic as the rewriter's
    _line_text_unchanged check, ensuring consistency with parser normalization.
    """
    root = etree.fromstring(xml_bytes)
    ns = _detect_namespace(root)
    textline_tag = _tag("TextLine", ns)
    result: dict[str, str] = {}
    for tl_el in root.iter(textline_tag):
        line_id = tl_el.get("ID")
        if line_id in line_ids:
            result[line_id] = _extract_text_from_line(tl_el, ns)
    return result


def _add_processing_entry(
    root: etree._Element,
    ns: str,
    provider: str,
    model: str,
) -> None:
    desc = root.find(_tag("Description", ns))
    if desc is None:
        return
    processing = desc.find(_tag("Processing", ns))
    if processing is None:
        return
    step = etree.SubElement(processing, _tag("processingStep", ns))
    step.set("type", "contentModification")
    step.set(
        "description",
        f"Post-OCR correction via {provider}/{model} (alto-llm-corrector)",
    )


# --- __all__ (Stage 3 audit remediation) ---
__all__ = [
    "RewriterMetrics",
    "rewrite_alto_file",
    "extract_output_texts",
]
