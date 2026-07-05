from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from pathlib import Path

from lxml import etree

from alto_core.alto._norm import clean_content, nfc
from alto_core.alto._ns import _detect_namespace, _int_attr, _tag, make_safe_parser
from alto_core.alto._text import reconstruct_textline
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

    Widths are proportional to a per-token *weight*: a word weighs its
    character count, a run of spaces weighs 0.6x its character count
    (spaces render narrower than glyphs).

    Spec F6 — the 0.6 space weight must enter the total weight used to
    compute the per-unit width. Pre-fix, ``unit`` was computed against the
    full character count (spaces at 1.0) while each space was then drawn
    at 0.6; the accumulated shortfall of every space was dumped onto the
    LAST token via a single ``correction`` term, inflating it. Now the
    weight is consistent on both sides and the rounding error is spread
    across all tokens by cumulative rounding — the final token only ever
    absorbs the residual rounding, never the sum of every space's deficit.
    """
    if not tokens:
        return []

    def _weight(t: str) -> float:
        return len(t) * 0.6 if t.strip() == "" else float(len(t))

    weights = [_weight(t) for t in tokens]
    total_weight = sum(weights)
    if total_weight == 0:
        per = width // len(tokens)
        return [(t, hpos + i * per, per) for i, t in enumerate(tokens)]

    unit = width / total_weight

    # Cumulative rounding: round the running total at each token boundary
    # and take successive differences. Every token lands on the floor or
    # ceil of its ideal share and the widths sum EXACTLY to ``width``.
    widths: list[int] = []
    cumulative = 0.0
    prev_rounded = 0
    for w in weights:
        cumulative += w * unit
        rounded = round(cumulative)
        widths.append(rounded - prev_rounded)
        prev_rounded = rounded

    # Defensive min-1 floor for degenerate lines. Raise every sub-1 width
    # to 1, then repay the deficit from the widest donors (each clamped to
    # 1) until the exact-sum invariant is restored. When ``width`` is
    # smaller than the token count the invariant is mathematically
    # unsatisfiable with all-≥1 widths; the min-1 floor wins and the sum
    # settles at ``len(tokens)`` — the only honest outcome, and pinned by
    # test_compute_geometry. Real ALTO never reaches this branch.
    if min(widths) < 1:
        deficit = 0
        for i, w in enumerate(widths):
            if w < 1:
                deficit += 1 - w
                widths[i] = 1
        while deficit > 0:
            donor = max(range(len(widths)), key=lambda i: widths[i])
            if widths[donor] <= 1:
                break  # all at the floor — sum > width, unavoidable
            take = min(deficit, widths[donor] - 1)
            widths[donor] -= take
            deficit -= take

    result: list[tuple[str, int, int]] = []
    cursor = hpos
    for t, w in zip(tokens, widths):
        result.append((t, cursor, w))
        cursor += w
    return result


# ---------------------------------------------------------------------------
# Element accessors (non-destructive)
# ---------------------------------------------------------------------------


def _attrib_dict(el: etree._Element) -> dict[str, str]:
    """Snapshot an element's attributes as a plain ``dict[str, str]``.

    lxml types ``_Attrib`` keys/values as ``str | bytes``; ALTO attributes
    are always text, so we coerce to ``str`` — this also satisfies
    ``mypy --strict`` where ``dict(el.attrib)`` does not.
    """
    return {str(k): str(v) for k, v in el.attrib.items()}


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


def _line_text_unchanged(el: etree._Element, corrected: str, ns: str) -> bool:
    # Spec F4 — compare STRIPPED forms on both sides. The parser derives
    # ``ocr_text`` as ``reconstruct_textline(...).replace("\r", "").strip()``
    # (parser._build_ocr_text) while this comparison used the raw, un-stripped
    # reconstruction. A line whose XML reconstructs with a trailing space
    # (e.g. a trailing ``<SP/>``) but whose corrected text equals the stripped
    # ``ocr_text`` therefore never matched — it was needlessly rewritten and
    # the UNTOUCHED metric under-counted. Stripping both sides restores the
    # UNTOUCHED path for such lines.
    source = reconstruct_textline(el, ns).replace("\r", "").strip()
    return source == nfc(corrected).replace("\r", "").strip()


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
        new_content = clean_content(word)
        changed = string_el.get("CONTENT") != new_content
        string_el.set("CONTENT", new_content)
        # Spec F2 — a changed CONTENT invalidates the OCR confidences: WC
        # (word confidence) and CC (per-character confidences) describe the
        # OLD glyph string and CC's length no longer matches the new CONTENT.
        # Drop them on any String whose CONTENT actually changes; a String
        # left byte-identical keeps its confidences untouched.
        if changed:
            for attr in ("WC", "CC"):
                if attr in string_el.attrib:
                    del string_el.attrib[attr]
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


def _emit_sp(
    el: etree._Element,
    ns: str,
    orig_sp_attribs: list[dict[str, str]],
    sp_n: int,
    tok_hpos: int,
    tok_width: int,
    vpos: int,
) -> None:
    """Append a fresh SP child with RECOMPUTED geometry.

    Post-audit fix (doctrine §6.1) — the slow path used to recycle the nth
    original SP's attributes verbatim, keeping OLD HPOS/WIDTH from the
    pre-correction layout while the surrounding Strings got recomputed
    positions: the interleaved SP/String geometry contradicted itself
    (worse after F6 rebalanced the token widths). SPs are pure spacing —
    they carry no confidences and no identity worth recycling — so their
    geometry is now always derived from the same ``_compute_geometry``
    pass as the Strings around them. ``orig_sp_attribs`` is still received
    so any non-geometric attribute an exotic producer set (none in the
    ALTO corpus at hand) survives; the geometric trio is overwritten.
    """
    sp = etree.SubElement(el, _tag("SP", ns))
    if sp_n < len(orig_sp_attribs):
        for k, v in orig_sp_attribs[sp_n].items():
            if k not in ("HPOS", "VPOS", "WIDTH", "HEIGHT"):
                sp.set(k, v)
    sp.set("WIDTH", str(tok_width))
    sp.set("HPOS", str(tok_hpos))
    sp.set("VPOS", str(vpos))


def _emit_string(
    el: etree._Element,
    ns: str,
    orig_string_attribs: list[dict[str, str]],
    str_n: int,
    line_id: str,
    token: str,
    tok_hpos: int,
    tok_width: int,
    vpos: int,
    height: int,
) -> None:
    """Append a fresh String child for the slow-path rebuild.

    Spec F2 / §6.1 — the slow path recycles ONLY ``ID`` and ``STYLEREFS``
    from the original String (positionally). ``HPOS``/``WIDTH`` are
    recomputed, ``VPOS``/``HEIGHT`` are inherited from the line, and
    ``WC``/``CC``/``SUBS_*`` are **never** recycled: the confidences
    describe the old glyph string (and ``CC``'s length would no longer
    match the new ``CONTENT``), and SUBS attributes are written separately
    by ``_apply_subs``. Pre-F2 the reuse branch copied every original
    attribute except SUBS, carrying stale ``WC``/``CC`` onto the rebuilt
    String.
    """
    s = etree.SubElement(el, _tag("String", ns))
    if str_n < len(orig_string_attribs):
        orig = orig_string_attribs[str_n]
        for k in ("ID", "STYLEREFS"):
            if k in orig:
                s.set(k, orig[k])
    else:
        s.set("ID", f"{line_id}_STR_{str_n:04d}")
    s.set("CONTENT", clean_content(token))
    s.set("HPOS", str(tok_hpos))
    s.set("VPOS", str(vpos))
    s.set("WIDTH", str(tok_width))
    s.set("HEIGHT", str(height))


def _append_trailing_hyp(
    el: etree._Element,
    ns: str,
    orig_hyp_attribs: dict[str, str],
    default_hpos: int,
    default_vpos: int,
    default_width: int,
    default_height: int,
) -> None:
    """Append a HYP child to a PART1-like TextLine.

    Preserves all original HYP attributes when one was present before
    the rebuild; otherwise synthesises one with the supplied geometry
    and a default ``-`` content.
    """
    hyp = etree.SubElement(el, _tag("HYP", ns))
    if orig_hyp_attribs:
        for k, v in orig_hyp_attribs.items():
            hyp.set(k, v)
    else:
        hyp.set("CONTENT", "-")
        hyp.set("HPOS", str(default_hpos))
        hyp.set("VPOS", str(default_vpos))
        hyp.set("WIDTH", str(default_width))
        hyp.set("HEIGHT", str(default_height))


def _rebuild_line(
    el: etree._Element,
    corrected: str,
    manifest: LineManifest,
    ns: str,
) -> None:
    """Slow-path rebuild for any TextLine (normal, PART1, BOTH, PART2).

    Behaviour by ``manifest.hyphen_role``:
      - PART1 / BOTH: reserve 4% of total width for a trailing HYP
        element, rebuilt with the original HYP attributes when present
        or synthesised at end-of-text otherwise.
      - PART2: full text width; never carries a trailing HYP.
      - NONE: full text width; any stray HYPs on the source element are
        deep-copied and restored verbatim after the rebuild (defensive —
        production ALTO rarely has HYPs on non-hyphenated lines).
    """
    is_part1_like = manifest.hyphen_role in (HyphenRole.PART1, HyphenRole.BOTH)
    is_normal = manifest.hyphen_role == HyphenRole.NONE

    orig_string_attribs = [_attrib_dict(s) for s in _get_string_children(el, ns)]
    orig_sp_attribs = [_attrib_dict(s) for s in _get_sp_children(el, ns)]

    if is_part1_like:
        orig_hyps = _get_hyp_children(el, ns)
        orig_hyp_attribs: dict[str, str] = (
            _attrib_dict(orig_hyps[0]) if orig_hyps else {}
        )
        saved_hyp: list[etree._Element] = []
    elif is_normal:
        orig_hyp_attribs = {}
        saved_hyp = [copy.deepcopy(c) for c in el if c.tag == _tag("HYP", ns)]
    else:  # PART2
        orig_hyp_attribs = {}
        saved_hyp = []

    _clear_line(el, ns)

    hpos = _int_attr(el, "HPOS")
    vpos = _int_attr(el, "VPOS")
    width = _int_attr(el, "WIDTH")
    height = _int_attr(el, "HEIGHT")

    if is_part1_like:
        hyp_width = max(1, round(width * 0.04))
        text_width = max(1, width - hyp_width)
    else:
        hyp_width = 0
        text_width = width

    tokens = _tokenize(corrected)
    if not tokens:
        if is_part1_like:
            _append_trailing_hyp(
                el,
                ns,
                orig_hyp_attribs,
                default_hpos=hpos + text_width,
                default_vpos=vpos,
                default_width=hyp_width,
                default_height=height,
            )
        else:
            for h in saved_hyp:
                el.append(h)
        return

    geo = _compute_geometry(hpos, text_width, tokens)
    str_n = sp_n = 0
    last_word_hpos = hpos
    last_word_width = hyp_width

    for token, tok_hpos, tok_width in geo:
        if token.strip() == "":
            _emit_sp(el, ns, orig_sp_attribs, sp_n, tok_hpos, tok_width, vpos)
            sp_n += 1
        else:
            _emit_string(
                el,
                ns,
                orig_string_attribs,
                str_n,
                manifest.line_id,
                token,
                tok_hpos,
                tok_width,
                vpos,
                height,
            )
            last_word_hpos = tok_hpos
            last_word_width = tok_width
            str_n += 1

    if is_part1_like:
        _append_trailing_hyp(
            el,
            ns,
            orig_hyp_attribs,
            default_hpos=last_word_hpos + last_word_width,
            default_vpos=vpos,
            default_width=hyp_width,
            default_height=height,
        )
    else:
        for h in saved_hyp:
            el.append(h)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def rewrite_alto_file(
    xml_path: Path,
    page_manifests: list[PageManifest],
    provider: str,
    model: str,
    *,
    lib_version: str | None = None,
    config_fingerprint: str | None = None,
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
    # Hardened parser — see alto_core.alto._ns.make_safe_parser docstring
    # for the rationale. Using lxml's default here would expose every
    # rewrite to entity-amplification DoS via crafted ALTO uploads.
    tree = etree.parse(str(xml_path), make_safe_parser())
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
        _rebuild_line(tl_el, corrected, lm, ns)
        _apply_subs(tl_el, lm, ns)
        metrics.slow_path += 1
        line_paths[line_id] = "slow_path"

    _add_processing_entry(root, ns, provider, model, lib_version, config_fingerprint)
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

    Uses the shared ``reconstruct_textline`` helper so the output text
    seen here matches both the parser's ocr_text and the rewriter's
    UNTOUCHED-detection comparison.
    """
    # Hardened parser — see alto_core.alto._ns.make_safe_parser. The
    # bytes here are typically the OUTPUT of rewrite_alto_file but the
    # function is documented as accepting arbitrary ALTO bytes, so we
    # treat them as untrusted.
    root = etree.fromstring(xml_bytes, make_safe_parser())
    ns = _detect_namespace(root)
    textline_tag = _tag("TextLine", ns)
    result: dict[str, str] = {}
    for tl_el in root.iter(textline_tag):
        line_id = tl_el.get("ID")
        if line_id in line_ids:
            result[line_id] = reconstruct_textline(tl_el, ns)
    return result


def _add_processing_entry(
    root: etree._Element,
    ns: str,
    provider: str,
    model: str,
    lib_version: str | None = None,
    config_fingerprint: str | None = None,
) -> None:
    """Record a ``processingStep`` documenting the correction pass (§11).

    Beyond the provider/model already written, the step now carries the
    **library version** and a **configuration fingerprint** (§8.2) so a
    corrected XML says by what and under which policy it was produced. Both
    are optional for backward compatibility; when omitted the historical
    description is emitted verbatim.
    """
    desc = root.find(_tag("Description", ns))
    if desc is None:
        return
    processing = desc.find(_tag("Processing", ns))
    if processing is None:
        return
    step = etree.SubElement(processing, _tag("processingStep", ns))
    step.set("type", "contentModification")

    provenance = "alto-llm-corrector"
    if lib_version:
        provenance += f" {lib_version}"
    if config_fingerprint:
        provenance += f"; config {config_fingerprint}"
    step.set(
        "description",
        f"Post-OCR correction via {provider}/{model} ({provenance})",
    )


# --- __all__ (Stage 3 audit remediation) ---
__all__ = [
    "RewriterMetrics",
    "rewrite_alto_file",
    "extract_output_texts",
]
