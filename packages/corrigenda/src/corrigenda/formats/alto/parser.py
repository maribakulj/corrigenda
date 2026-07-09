from __future__ import annotations

from pathlib import Path

from lxml import etree

from corrigenda.formats.alto._ns import (
    _detect_namespace,
    _int_attr,
    _tag,
    make_safe_parser,
)
from corrigenda.formats.alto._text import reconstruct_textline
from corrigenda.core.pairing import (
    disambiguate_page_ids as _disambiguate_page_ids,
    link_cross_page_hyphens as _link_cross_page_hyphens,
    link_hyphen_pairs as _link_hyphen_pairs,
    trailing_hyphen_char,
)
from corrigenda.core.schemas import (
    DEFAULT_PAIRING_POLICY,
    BlockManifest,
    Coords,
    DocumentManifest,
    HyphenRole,
    LineManifest,
    PageManifest,
    PairingPolicy,
)

# ---------------------------------------------------------------------------
# ocr_text reconstruction
# ---------------------------------------------------------------------------


def _build_ocr_text(textline: etree._Element, ns: str) -> str:
    """Build the parser's logical line text: NFC + carriage-return strip + strip."""
    return reconstruct_textline(textline, ns).replace("\r", "").strip()


# ---------------------------------------------------------------------------
# Hyphenation detection (mutates lines in-place)
# ---------------------------------------------------------------------------


def _parse_textline_hyphen_info(
    textline: etree._Element,
    ns: str,
    line: LineManifest,
) -> None:
    """
    First-pass hyphenation scan for a single TextLine.
    Fills hyphen_role / hyphen_source_explicit / hyphen_subs_content
    directly on the LineManifest.

    A line can be both PART2 (first String has SUBS_TYPE="HypPart2") AND
    PART1 (trailing HYP element or last String has SUBS_TYPE="HypPart1").
    In that case, role is set to BOTH with forward fields for the PART1 side.
    """
    # Spec F5/F3 — comments and processing instructions carry a callable
    # ``tag`` (``etree.Comment`` / ``etree.PI``), not a ``str``. A trailing
    # comment inside a TextLine made ``etree.QName(last_child.tag)`` below
    # raise and abort the whole file. Filter them out up front so every
    # downstream child access sees only real elements.
    children = [c for c in textline if isinstance(c.tag, str)]
    if not children:
        return

    string_tag = _tag("String", ns)

    # --- Detect PART2: first String has SUBS_TYPE="HypPart2" ---
    is_part2 = False
    backward_subs: str | None = None

    first_string = next((c for c in children if c.tag == string_tag), None)
    if first_string is not None:
        subs_type = first_string.get("SUBS_TYPE", "")
        if subs_type == "HypPart2":
            is_part2 = True
            backward_subs = first_string.get("SUBS_CONTENT")

    # --- Detect PART1: trailing HYP, or last String SUBS_TYPE="HypPart1",
    #     or heuristic trailing dash ---
    is_part1 = False
    forward_subs: str | None = None
    forward_explicit = False

    last_child = children[-1]
    if etree.QName(last_child.tag).localname == "HYP":
        is_part1 = True
        forward_explicit = True
        prev_strings = [c for c in children if c.tag == string_tag]
        if prev_strings:
            sc = prev_strings[-1].get("SUBS_CONTENT")
            if sc:
                forward_subs = sc
    else:
        last_string = next((c for c in reversed(children) if c.tag == string_tag), None)
        if last_string is not None:
            if last_string.get("SUBS_TYPE", "") == "HypPart1":
                is_part1 = True
                forward_explicit = True
                sc = last_string.get("SUBS_CONTENT")
                if sc:
                    forward_subs = sc

    # Heuristic: a genuine word-break hyphen is the last non-space token
    # ending in "-" with an ALPHABETIC character immediately before it.
    # L10/B6 — this narrowing rejects pure-numeric forms ("1789-", "n°5-")
    # and dialog em-dashes that would otherwise mark a phantom PART1 and
    # make the rewriter emit a spurious HYP on output. The rule now lives
    # in ``core.pairing.trailing_hyphen_char`` (shared with the PAGE parser,
    # which passes the wider HYPHEN_CHARS repertoire); ALTO restricts it to
    # the plain hyphen-minus. The explicit SUBS_TYPE="HypPart1" path above
    # still catches every hyphen pair the OCR engine itself flagged.
    if not is_part1:
        if trailing_hyphen_char(line.ocr_text, ("-",)) is not None:
            is_part1 = True
            forward_explicit = False

    # --- Set role based on detection ---
    if is_part2 and is_part1:
        line.hyphen_role = HyphenRole.BOTH
        # Backward (PART2 side) in existing fields
        line.hyphen_source_explicit = True  # PART2 from SUBS_TYPE is always explicit
        if backward_subs:
            line.hyphen_subs_content = backward_subs
        # Forward (PART1 side) in new fields
        line.hyphen_forward_explicit = forward_explicit
        if forward_subs:
            line.hyphen_forward_subs_content = forward_subs
    elif is_part2:
        line.hyphen_role = HyphenRole.PART2
        line.hyphen_source_explicit = True
        if backward_subs:
            line.hyphen_subs_content = backward_subs
    elif is_part1:
        line.hyphen_role = HyphenRole.PART1
        line.hyphen_source_explicit = forward_explicit
        if forward_subs:
            line.hyphen_subs_content = forward_subs


# ---------------------------------------------------------------------------
# Core parsing
# ---------------------------------------------------------------------------


def parse_alto_file(
    xml_path: Path,
    source_name: str,
    page_index_offset: int = 0,
    global_line_offset: int = 0,
    pairing_policy: PairingPolicy = DEFAULT_PAIRING_POLICY,
) -> tuple[list[PageManifest], etree._Element]:
    """
    Parse one ALTO XML file and return (list_of_PageManifest, root_element).

    ``pairing_policy`` (F7) is forwarded to the hyphen-pair linker; the
    default reproduces the historical purely-sequential pairing.
    """
    # Hardened parser shared with rewriter.py + extract_output_texts.
    # See corrigenda.formats.alto._ns.make_safe_parser docstring.
    tree = etree.parse(str(xml_path), make_safe_parser())
    root = tree.getroot()
    ns = _detect_namespace(root)

    pages: list[PageManifest] = []
    global_line_idx = global_line_offset

    layout = root.find(_tag("Layout", ns))
    if layout is None:
        return pages, root

    for page_idx, page_el in enumerate(layout.findall(_tag("Page", ns))):
        page_id = page_el.get("ID", f"PAGE_{page_index_offset + page_idx}")
        page_width = _int_attr(page_el, "WIDTH")
        page_height = _int_attr(page_el, "HEIGHT")

        blocks: list[BlockManifest] = []
        lines: list[LineManifest] = []

        printspace = page_el.find(_tag("PrintSpace", ns))
        container = printspace if printspace is not None else page_el

        block_order = 0
        for tb in container.findall(_tag("TextBlock", ns)):
            block_id = tb.get("ID", f"TB_{page_id}_{block_order}")
            block_coords = Coords(
                hpos=_int_attr(tb, "HPOS"),
                vpos=_int_attr(tb, "VPOS"),
                width=_int_attr(tb, "WIDTH"),
                height=_int_attr(tb, "HEIGHT"),
            )
            line_ids: list[str] = []
            line_order_in_block = 0

            for tl in tb.findall(_tag("TextLine", ns)):
                line_id = tl.get("ID", f"TL_{block_id}_{line_order_in_block}")
                coords = Coords(
                    hpos=_int_attr(tl, "HPOS"),
                    vpos=_int_attr(tl, "VPOS"),
                    width=_int_attr(tl, "WIDTH"),
                    height=_int_attr(tl, "HEIGHT"),
                )
                ocr_text = _build_ocr_text(tl, ns)

                lm = LineManifest(
                    line_id=line_id,
                    page_id=page_id,
                    block_id=block_id,
                    line_order_global=global_line_idx,
                    line_order_in_block=line_order_in_block,
                    coords=coords,
                    ocr_text=ocr_text,
                )

                # First-pass hyphenation scan
                _parse_textline_hyphen_info(tl, ns, lm)

                lines.append(lm)
                line_ids.append(line_id)
                line_order_in_block += 1
                global_line_idx += 1

            blocks.append(
                BlockManifest(
                    block_id=block_id,
                    page_id=page_id,
                    block_order=block_order,
                    coords=block_coords,
                    line_ids=line_ids,
                )
            )
            block_order += 1

        # Link prev/next
        for i, lm in enumerate(lines):
            if i > 0:
                lm.prev_line_id = lines[i - 1].line_id
            if i < len(lines) - 1:
                lm.next_line_id = lines[i + 1].line_id

        # Second-pass: link hyphen pairs
        _link_hyphen_pairs(lines, pairing_policy)

        pages.append(
            PageManifest(
                page_id=page_id,
                source_file=source_name,
                page_index=page_index_offset + page_idx,
                page_width=page_width,
                page_height=page_height,
                blocks=blocks,
                lines=lines,
            )
        )

    return pages, root


def build_document_manifest(
    files: list[tuple[Path, str]],
    pairing_policy: PairingPolicy = DEFAULT_PAIRING_POLICY,
) -> DocumentManifest:
    """
    Build a DocumentManifest from a list of (xml_path, source_name) tuples.
    Files are processed in order; page/line indices are continuous.

    ``pairing_policy`` (F7) is applied to both intra-page and cross-page
    hyphen linking; the default reproduces the historical behaviour.
    """
    source_files: list[str] = []
    page_offset = 0
    line_offset = 0
    parsed: list[tuple[str, list[PageManifest]]] = []

    for xml_path, source_name in files:
        source_files.append(source_name)
        pages, _ = parse_alto_file(
            xml_path, source_name, page_offset, line_offset, pairing_policy
        )
        parsed.append((source_name, pages))
        page_offset += len(pages)
        for p in pages:
            line_offset += len(p.lines)

    # Resolve cross-file Page ID collisions BEFORE cross-page hyphen linking
    # so the resulting hyphen_pair_page_id refs already use qualified ids.
    _disambiguate_page_ids(parsed)

    all_pages: list[PageManifest] = [p for _, pages in parsed for p in pages]

    # Cross-page hyphen linking (shared core.pairing helper): a PART1/BOTH
    # line at the bottom of page N that was NOT linked intra-page is paired
    # with the first line of page N+1.
    _link_cross_page_hyphens(all_pages, pairing_policy)

    total_blocks = sum(len(p.blocks) for p in all_pages)
    total_lines = sum(len(p.lines) for p in all_pages)

    return DocumentManifest(
        source_files=source_files,
        pages=all_pages,
        total_pages=len(all_pages),
        total_blocks=total_blocks,
        total_lines=total_lines,
    )


# --- __all__ (Stage 3 audit remediation) ---
__all__ = [
    "parse_alto_file",
    "build_document_manifest",
]
