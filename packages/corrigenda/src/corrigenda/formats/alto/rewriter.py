from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from pathlib import Path

from lxml import etree

from corrigenda.core._norm import clean_content, nfc
from corrigenda.core._parse import parse_int_tolerant
from corrigenda.core.identity import ensure_unique_identities
from corrigenda.core.pairing import HYPHEN_CHARS
from corrigenda.errors import DuplicateIdError
from corrigenda.formats.alto._ns import (
    _detect_namespace,
    _int_attr,
    _tag,
    make_safe_parser,
)
from corrigenda.formats.alto._text import reconstruct_textline
from corrigenda.core.protocols import RewriteResult
from corrigenda.core.schemas import HyphenRole, LineManifest, PageManifest

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


def _forward_subs_target(
    el: etree._Element,
    manifest: LineManifest,
    ns: str,
) -> etree._Element | None:
    """Return the String that carries a BOTH line's forward (HypPart1)
    subs, or None when no distinct element exists for them.

    Shared by ``_apply_subs`` AND ``_subs_need_update`` so the
    writer and the change-detection predicate agree. When the line has a
    single String, ``strings[-1]`` IS the element carrying the BACKWARD
    (HypPart2) subs. Writing the forward HypPart1 onto it would clobber
    the continuation marker, flipping HypPart2→HypPart1 and destroying
    the "continues from the previous line" signal (and breaking
    byte-parity on an identity correction). The trailing HYP element
    already marks the forward hyphen, so forward SUBS only live on a
    DISTINCT last String — and the predicate must not demand them
    elsewhere (pre-fix it did, so a byte-correct single-String BOTH line
    was misrouted to SUBS-ONLY on every run, never UNTOUCHED).
    """
    if manifest.hyphen_role != HyphenRole.BOTH:
        return None
    strings = _get_string_children(el, ns)
    if not strings:
        return None
    last = strings[-1]
    if last is _subs_target(el, manifest, ns):
        return None
    return last


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

    # Check forward subs for BOTH lines — only on the distinct last
    # String _apply_subs would actually write (see
    # _forward_subs_target).
    last = _forward_subs_target(el, manifest, ns)
    if last is not None:
        fw_type, fw_content = _desired_forward_subs(manifest)
        if last.get("SUBS_TYPE") != fw_type or last.get("SUBS_CONTENT") != fw_content:
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

    # Forward subs for BOTH lines — only on a DISTINCT last String
    # (guard shared with _subs_need_update, see
    # _forward_subs_target for the single-String rationale).
    last = _forward_subs_target(el, manifest, ns)
    if last is not None:
        fw_type, fw_content = _desired_forward_subs(manifest)
        _set_subs_on_element(last, fw_type, fw_content)


# ---------------------------------------------------------------------------
# Fast path: in-place CONTENT update (word count unchanged)
# ---------------------------------------------------------------------------


def _drop_structural_break_hyphen(text: str) -> str:
    """Remove ONE trailing break hyphen from an explicit PART1 line's text.

    The hyphen is represented structurally by the line's ``<HYP>`` element,
    so it must not also live in the last ``String``'s CONTENT. Accepts the
    full ALTO/PAGE hyphen repertoire (``-`` ``¬`` ``⸗`` soft-hyphen), mirroring
    ``reconcile_hyphen_pair``'s trailing-hyphen gate. A line with no trailing
    hyphen is returned unchanged (defensive — the reconciler guarantees an
    explicit PART1 correction ends in one, but the rewriter never assumes it).
    """
    stripped = text.rstrip()
    if stripped.endswith(HYPHEN_CHARS):
        return stripped[:-1]
    return text


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

    §6.1 — SP geometry must agree with the recomputed String geometry
    around it: recycling an original SP's HPOS/WIDTH verbatim would make
    the interleaved SP/String layout contradict itself. SPs are pure
    spacing — they carry no confidences and no identity worth recycling —
    so their geometry is always derived from the same
    ``_compute_geometry`` pass as the Strings around them. ``orig_sp_attribs`` is still received
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

    Spec F2 / §6.1 — the slow path recycles ONLY identity and styling from
    the original String (positionally): ``ID``, ``STYLEREFS``, and
    ``STYLE``. ``HPOS``/``WIDTH`` are recomputed, ``VPOS``/``HEIGHT`` are
    inherited from the line, and ``WC``/``CC``/``SUBS_*`` are **never**
    recycled: the confidences describe the old glyph string (and ``CC``'s
    length would no longer match the new ``CONTENT``), and SUBS attributes
    are written separately by ``_apply_subs``. Pre-F2 the reuse branch
    copied every original attribute except SUBS, carrying stale
    ``WC``/``CC`` onto the rebuilt String.

    ``STYLE`` (inline bold/italics/…) is part of the §6.1 whitelist
    (ratified 2026-07-07): the §6.1 doctrine
    targets data INVALIDATED by the text change, and styling —
    like ``STYLEREFS``, its reference-based twin — is not. Dropping it
    destroyed real formatting on the non-regression corpus (45 of the 47
    styled Strings in X0000002, mostly press headlines whose garbled OCR
    makes a slow-path word-count change the LIKELY correction, not an
    edge case).
    """
    s = etree.SubElement(el, _tag("String", ns))
    if str_n < len(orig_string_attribs):
        orig = orig_string_attribs[str_n]
        for k in ("ID", "STYLEREFS", "STYLE"):
            if k in orig:
                s.set(k, orig[k])
    else:
        s.set("ID", f"{line_id}_STR_{str_n:04d}")
    s.set("CONTENT", clean_content(token))
    s.set("HPOS", str(tok_hpos))
    s.set("VPOS", str(vpos))
    s.set("WIDTH", str(tok_width))
    s.set("HEIGHT", str(height))


_HYP_GEOM_ATTRS = frozenset({"HPOS", "VPOS", "WIDTH", "HEIGHT"})


def _append_trailing_hyp(
    el: etree._Element,
    ns: str,
    orig_hyp_attribs: dict[str, str],
    hpos: int,
    vpos: int,
    width: int,
    height: int,
) -> None:
    """Append a HYP child to an explicit PART1-like TextLine.

    Non-geometry attributes (CONTENT, ID, STYLEREFS, …) carry over from an
    original HYP when present; a synthesised HYP defaults to ``-`` content.
    Geometry is ALWAYS the reserved end-of-line slot supplied by the
    caller — the original HYP's stale HPOS/WIDTH must NOT be copied
    verbatim, or the rebuilt children would sum past the line WIDTH and the
    HYP would overlap the last String.
    """
    hyp = etree.SubElement(el, _tag("HYP", ns))
    content_set = False
    for k, v in orig_hyp_attribs.items():
        if k in _HYP_GEOM_ATTRS:
            continue
        hyp.set(k, v)
        if k == "CONTENT":
            content_set = True
    if not content_set:
        hyp.set("CONTENT", "-")
    hyp.set("HPOS", str(hpos))
    hyp.set("VPOS", str(vpos))
    hyp.set("WIDTH", str(width))
    hyp.set("HEIGHT", str(height))


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
    # Trim leading/trailing whitespace before tokenizing: the validator
    # accepts corrected text with edge whitespace, and an edge SP token
    # would land the trailing HYP ON TOP of the SP's HPOS range (overlap,
    # children no longer tiling the line) since last_word_hpos/width
    # track only String tokens.
    # Trimming matches the fast path, where split() drops edge
    # whitespace implicitly.
    corrected = corrected.strip()

    # Only an EXPLICITLY hyphenated PART1/BOTH line carries a HYP element.
    # A heuristically-detected PART1 (trailing dash in CONTENT, no HYP /
    # SUBS_TYPE markup) must NOT get a synthesised <HYP CONTENT="-">: that
    # would invent explicit markup the source never had (conservative-
    # heuristic violation) and append a phantom trailing hyphen to the
    # output text. Such a line is rebuilt like a plain line — its trailing
    # dash stays inside the String CONTENT.
    is_part1_like = (
        manifest.hyphen_role in (HyphenRole.PART1, HyphenRole.BOTH)
        and manifest.hyphen_source_explicit
    )
    is_normal = not is_part1_like and manifest.hyphen_role in (
        HyphenRole.NONE,
        HyphenRole.PART1,
        HyphenRole.BOTH,
    )

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
        # Reserve the ORIGINAL HYP's real width when one was present, so the
        # rebuilt String/SP widths plus the HYP width sum EXACTLY to the line
        # WIDTH. The old 4% estimate combined with copying the original HYP's
        # verbatim WIDTH made the children sum to width + original_hyp_width
        # and overlapped the last String. Fall back to the 4% estimate only
        # when synthesising a HYP (explicit line with no HYP element).
        # Parse via the shared tolerant policy: HYP attributes are never
        # pre-parsed by ``_int_attr`` upstream, and a bare
        # ``int(float(...))`` would let ``WIDTH="1e999"`` escape as an
        # uncaught OverflowError. Unusable → 0 → 4% estimate.
        orig_hyp_w = parse_int_tolerant(orig_hyp_attribs.get("WIDTH"), 0)
        hyp_width = orig_hyp_w if orig_hyp_w > 0 else max(1, round(width * 0.04))
        hyp_width = min(hyp_width, max(1, width - 1))  # keep room for text
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
                hpos=hpos + text_width,
                vpos=vpos,
                width=hyp_width,
                height=height,
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
            hpos=last_word_hpos + last_word_width,
            vpos=vpos,
            width=hyp_width,
            height=height,
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
) -> RewriteResult:
    """
    Rewrite an ALTO XML file with corrected text from page_manifests.

    Follows a 4-path strategy:
      Path 1 — UNTOUCHED:  text same + SUBS same → skip entirely
      Path 2 — SUBS-ONLY:  text same + SUBS changed → in-place SUBS update
      Path 3 — FAST PATH:  text changed + word count same → in-place CONTENT + SUBS
      Path 4 — SLOW PATH:  word count changed → rebuild line + SUBS

    Returns a :class:`RewriteResult` (bytes, metrics, per-line rewriter
    paths, final texts, losses).
    """
    # Hardened parser — see corrigenda.formats.alto._ns.make_safe_parser docstring
    # for the rationale. Using lxml's default here would expose every
    # rewrite to entity-amplification DoS via crafted ALTO uploads.
    tree = etree.parse(str(xml_path), make_safe_parser())
    root = tree.getroot()
    ns = _detect_namespace(root)
    metrics = RewriterMetrics()
    line_paths: dict[str, str] = {}

    # ADR-007 — a bare line_id keys every correction-to-element
    # association below. A duplicate (in the manifests OR on the XML
    # elements) would silently apply one line's correction to another
    # physical line, so both sides fail loudly instead. Parsers enforce
    # the same invariant up front; this guards direct calls with
    # hand-built manifests via the canonical shared check.
    ensure_unique_identities(page_manifests, xml_path.name)
    line_by_id: dict[str, LineManifest] = {
        lm.line_id: lm for page in page_manifests for lm in page.lines
    }

    seen_element_ids: set[str] = set()
    textline_tag = _tag("TextLine", ns)
    for tl_el in root.iter(textline_tag):
        line_id = tl_el.get("ID")
        if line_id not in line_by_id:
            continue
        if line_id in seen_element_ids:
            raise DuplicateIdError(
                f"duplicate TextLine ID {line_id!r} in {xml_path.name!r} — "
                "two physical lines would receive the same correction (ADR-007)."
            )
        seen_element_ids.add(line_id)
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

        # An EXPLICIT PART1 line carries its end-of-line hyphen structurally,
        # in the <HYP> element (the rewrite paths re-emit it). If the LLM
        # returned the fragment WITH a trailing hyphen ("préve-", natural at a
        # word break), storing it in the String CONTENT too would double the
        # hyphen. Drop it here for the write text only — AFTER the change
        # detection above, which must compare the full reconstructed line
        # (String + HYP) so an identity correction still classifies UNTOUCHED.
        # A HEURISTIC PART1 (no HYP/SUBS markup) has no structural hyphen and
        # keeps its trailing dash in CONTENT — untouched by this branch.
        write_text = corrected
        if (
            lm.hyphen_role in (HyphenRole.PART1, HyphenRole.BOTH)
            and lm.hyphen_source_explicit
        ):
            write_text = _drop_structural_break_hyphen(corrected)

        # --- Path 3: FAST PATH (word count same) ---
        if _update_content_in_place(tl_el, write_text, ns):
            _apply_subs(tl_el, lm, ns)
            metrics.fast_path += 1
            line_paths[line_id] = "fast_path"
            continue

        # --- Path 4: SLOW PATH (word count changed) ---
        _rebuild_line(tl_el, write_text, lm, ns)
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
    # ADR-011 — the output texts are read off the very tree the bytes
    # were just serialized from: the projection invariant verifies them
    # without a second full parse of the output. ALTO rewrites are
    # lossless (no granularity counters to report).
    return RewriteResult(
        xml_bytes=xml_bytes,
        metrics=metrics,
        rewriter_paths=line_paths,
        texts=_extract_texts_from_root(root, ns, set(line_by_id)),
    )


def _extract_texts_from_root(
    root: etree._Element, ns: str, line_ids: set[str]
) -> dict[str, str]:
    """Per-line text of an ALTO tree, via the shared
    ``reconstruct_textline`` helper — so the text seen here matches both
    the parser's ocr_text and the rewriter's UNTOUCHED-detection
    comparison."""
    textline_tag = _tag("TextLine", ns)
    result: dict[str, str] = {}
    for tl_el in root.iter(textline_tag):
        line_id = tl_el.get("ID")
        if line_id in line_ids:
            if line_id in result:
                # ADR-007 — a repeated ID would silently collapse two
                # physical lines into one trace entry.
                raise DuplicateIdError(
                    f"duplicate TextLine ID {line_id!r} in rewritten ALTO — "
                    "output-text extraction would be ambiguous (ADR-007)."
                )
            result[line_id] = reconstruct_textline(tl_el, ns)
    return result


def extract_output_texts(xml_bytes: bytes, line_ids: set[str]) -> dict[str, str]:
    """Re-extract text from rewritten ALTO XML for the given line IDs.

    The pipeline no longer calls this (the rewrite returns its texts on
    the :class:`RewriteResult`); it remains for round-trip checks over
    arbitrary ALTO bytes.
    """
    # Hardened parser — see corrigenda.formats.alto._ns.make_safe_parser. The
    # bytes here are typically the OUTPUT of rewrite_alto_file but the
    # function is documented as accepting arbitrary ALTO bytes, so we
    # treat them as untrusted.
    root = etree.fromstring(xml_bytes, make_safe_parser())
    return _extract_texts_from_root(root, _detect_namespace(root), line_ids)


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

    Placement follows the ALTO container actually present:

    - ``<Processing>`` (the ALTO 4.0 generic slot) → append a
      ``<processingStep>`` (historical corrigenda behaviour, unchanged).
    - ``<OCRProcessing>`` (what real ABBYY / Tesseract / Gallica exports
      use) → append a ``<postProcessingStep>`` there. Without this branch
      §11's "every corrected file records the pass" silently failed for
      exactly the files real users bring — none of them carry ``<Processing>``.
    - neither, but a ``<Description>`` exists → create a ``<Processing>`` so
      the pass is still recorded rather than dropped.
    """
    desc = root.find(_tag("Description", ns))
    if desc is None:
        return
    description = _provenance_description(
        provider, model, lib_version, config_fingerprint
    )

    processing = desc.find(_tag("Processing", ns))
    if processing is not None:
        _append_processing_step(processing, ns, description)
        return

    ocr_processings = desc.findall(_tag("OCRProcessing", ns))
    if ocr_processings:
        _append_post_processing_step(ocr_processings[-1], ns, description, lib_version)
        return

    _append_processing_step(
        etree.SubElement(desc, _tag("Processing", ns)), ns, description
    )


def _provenance_description(
    provider: str,
    model: str,
    lib_version: str | None,
    config_fingerprint: str | None,
) -> str:
    """The human-readable provenance line shared by every ALTO container."""
    provenance = "corrigenda"
    if lib_version:
        provenance += f" {lib_version}"
    if config_fingerprint:
        provenance += f"; config {config_fingerprint}"
    return f"Post-OCR correction via {provider}/{model} ({provenance})"


def _append_processing_step(
    processing: etree._Element, ns: str, description: str
) -> None:
    """Record the pass as a ``<processingStep>`` (ALTO 4.0 ``<Processing>``)."""
    step = etree.SubElement(processing, _tag("processingStep", ns))
    step.set("type", "contentModification")
    step.set("description", description)


def _append_post_processing_step(
    ocr_processing: etree._Element,
    ns: str,
    description: str,
    lib_version: str | None,
) -> None:
    """Record the pass as a ``<postProcessingStep>`` inside ``<OCRProcessing>``.

    ``postProcessingStep`` is the ALTO-standard slot for work done after OCR
    (LoC ``OCRProcessingType``); it is appended after any existing
    pre/ocr/post steps, keeping the source OCR record intact. Child order
    follows ``ProcessingStepType``: description before software.
    """
    step = etree.SubElement(ocr_processing, _tag("postProcessingStep", ns))
    desc_el = etree.SubElement(step, _tag("processingStepDescription", ns))
    desc_el.text = description
    software = etree.SubElement(step, _tag("processingSoftware", ns))
    name_el = etree.SubElement(software, _tag("softwareName", ns))
    name_el.text = "corrigenda"
    if lib_version:
        version_el = etree.SubElement(software, _tag("softwareVersion", ns))
        version_el.text = lib_version


# --- public surface ---
__all__ = [
    "RewriterMetrics",
    "rewrite_alto_file",
    "extract_output_texts",
]
