from __future__ import annotations

from pathlib import Path

from lxml import etree

from alto_core.alto._ns import _detect_namespace, _int_attr, _tag, make_safe_parser
from alto_core.alto._text import reconstruct_textline
from alto_core.schemas import (
    BlockManifest,
    Coords,
    DocumentManifest,
    HyphenRole,
    LineManifest,
    PageManifest,
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


def _link_hyphen_pairs(lines: list[LineManifest]) -> None:
    """
    Second pass: link PART1/BOTH lines to their forward partners.

    A line with role PART1 or BOTH has a forward PART1 relationship.
    The next line is linked as PART2/BOTH (backward side).

    For PART1:  pair_line_id = forward partner, subs_content = pair subs
    For BOTH:   forward_pair_id = forward partner, forward_subs_content = pair subs
                (backward fields were already set by a previous iteration)
    """
    for i, line in enumerate(lines):
        # Skip lines that don't have a forward (PART1) role
        if line.hyphen_role not in (HyphenRole.PART1, HyphenRole.BOTH):
            continue
        if i + 1 >= len(lines):
            continue

        candidate = lines[i + 1]

        # Accept PART2, BOTH, or NONE as forward partner
        if candidate.hyphen_role not in (
            HyphenRole.PART2,
            HyphenRole.BOTH,
            HyphenRole.NONE,
        ):
            continue

        # Mark NONE candidate as PART2
        if candidate.hyphen_role == HyphenRole.NONE:
            if line.hyphen_role == HyphenRole.BOTH:
                candidate.hyphen_role = HyphenRole.PART2
                candidate.hyphen_source_explicit = line.hyphen_forward_explicit
            else:
                candidate.hyphen_role = HyphenRole.PART2
                candidate.hyphen_source_explicit = line.hyphen_source_explicit

        # Determine subs_content and set links for this pair
        if line.hyphen_role == HyphenRole.BOTH:
            # Forward side of a BOTH line
            subs = (
                line.hyphen_forward_subs_content
                or candidate.hyphen_subs_content
                or None
            )

            # Set forward link on the BOTH line
            line.hyphen_forward_pair_id = candidate.line_id
            line.hyphen_forward_pair_page_id = candidate.page_id
            if subs:
                line.hyphen_forward_subs_content = subs

            # Set backward link on the candidate
            candidate.hyphen_pair_line_id = line.line_id
            candidate.hyphen_pair_page_id = line.page_id
            if subs:
                candidate.hyphen_subs_content = subs
        else:
            # Regular PART1 line
            subs = line.hyphen_subs_content or candidate.hyphen_subs_content

            # Bidirectional link (page_id qualifies for cross-page disambiguation)
            line.hyphen_pair_line_id = candidate.line_id
            line.hyphen_pair_page_id = candidate.page_id
            candidate.hyphen_pair_line_id = line.line_id
            candidate.hyphen_pair_page_id = line.page_id

            if subs:
                line.hyphen_subs_content = subs
                candidate.hyphen_subs_content = subs


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
    children = list(textline)
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

    # Heuristic: last non-space token ends with "-" AND contains at
    # least one alphabetic character before the trailing dash.
    # L10/B6 — the pre-fix check accepted any token ending in "-",
    # including pure-numeric forms ("1789-", "n°5-") and dialog
    # em-dashes ("—"). A page like "Régnait de 1789-\n1799" would
    # mark "1789-" as PART1 and "1799" as PART2; the rewriter
    # would then emit a phantom HYP element on output. Requiring
    # an alpha-before-dash narrows the heuristic to genuine
    # word-break hyphens (the explicit SUBS_TYPE="HypPart1" path
    # above still catches every legitimate hyphen pair flagged
    # by the OCR engine itself).
    if not is_part1:
        tokens = line.ocr_text.split()
        if tokens:
            last = tokens[-1]
            if last.endswith("-"):
                bare = last.rstrip("-")
                # Require the character IMMEDIATELY before the dash to
                # be alphabetic — that's the signature of a genuine
                # word-break hyphen ("écri-", "infor-"). Pre-fix "any
                # alpha anywhere" let `n°5-` slip through because "n"
                # is alpha; a year range like `1789-` was rejected
                # correctly, but mixed alpha-numeric like list numbers
                # still produced false positives. Adjacent-alpha is
                # the strict version of the original heuristic.
                if bare and bare[-1].isalpha():
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
) -> tuple[list[PageManifest], etree._Element]:
    """
    Parse one ALTO XML file and return (list_of_PageManifest, root_element).
    """
    # Hardened parser shared with rewriter.py + extract_output_texts.
    # See alto_core.alto._ns.make_safe_parser docstring.
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
        _link_hyphen_pairs(lines)

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


def _disambiguate_page_ids(
    parsed: list[tuple[str, list[PageManifest]]],
) -> None:
    """Prefix colliding Page IDs with their source filename.

    Multiple ALTO files commonly declare the same Page ID (``"Page1"``,
    ``"P1"``…) — a per-scan workflow practically guarantees this.
    Without disambiguation, the orchestrator's cross-page hyphen partner
    lookup picks the wrong page, intra-page hyphen pair_page_id refs
    become ambiguous, and the trace/diff/layout endpoints emit duplicate
    page_id values to the frontend.

    This is called BEFORE cross-page hyphen linking so that the
    qualified IDs flow into ``hyphen_pair_page_id`` naturally.
    """
    counts: dict[str, int] = {}
    for _, pages in parsed:
        for p in pages:
            counts[p.page_id] = counts.get(p.page_id, 0) + 1

    colliding = {pid for pid, n in counts.items() if n > 1}
    if not colliding:
        return

    for source_name, pages in parsed:
        for p in pages:
            old_pid = p.page_id
            if old_pid not in colliding:
                continue
            new_pid = f"{source_name}::{old_pid}"
            p.page_id = new_pid
            for b in p.blocks:
                b.page_id = new_pid
            for lm in p.lines:
                lm.page_id = new_pid
                # Intra-page hyphen partner refs were set to the old page_id
                # by _link_hyphen_pairs during parse_alto_file. Rewrite them
                # to the qualified id so downstream lookups stay consistent.
                if lm.hyphen_pair_page_id == old_pid:
                    lm.hyphen_pair_page_id = new_pid
                if lm.hyphen_forward_pair_page_id == old_pid:
                    lm.hyphen_forward_pair_page_id = new_pid


def build_document_manifest(
    files: list[tuple[Path, str]],
) -> DocumentManifest:
    """
    Build a DocumentManifest from a list of (xml_path, source_name) tuples.
    Files are processed in order; page/line indices are continuous.
    """
    source_files: list[str] = []
    page_offset = 0
    line_offset = 0
    parsed: list[tuple[str, list[PageManifest]]] = []

    for xml_path, source_name in files:
        source_files.append(source_name)
        pages, _ = parse_alto_file(xml_path, source_name, page_offset, line_offset)
        parsed.append((source_name, pages))
        page_offset += len(pages)
        for p in pages:
            line_offset += len(p.lines)

    # Resolve cross-file Page ID collisions BEFORE cross-page hyphen linking
    # so the resulting hyphen_pair_page_id refs already use qualified ids.
    _disambiguate_page_ids(parsed)

    all_pages: list[PageManifest] = [p for _, pages in parsed for p in pages]

    # Cross-page hyphen linking: if the last line of page N is PART1
    # (or BOTH) and was not already linked, try to pair it with the
    # first line of page N+1.  _link_hyphen_pairs works on any list
    # of consecutive lines, so we pass it a 2-element list.
    for i in range(len(all_pages) - 1):
        if not all_pages[i].lines or not all_pages[i + 1].lines:
            continue
        last_line = all_pages[i].lines[-1]
        first_line = all_pages[i + 1].lines[0]
        # Only attempt if the last line looks like a PART1/BOTH that
        # was NOT linked during intra-page pass (pair_line_id is None).
        needs_forward_link = (
            last_line.hyphen_role == HyphenRole.PART1
            and not last_line.hyphen_pair_line_id
        ) or (
            last_line.hyphen_role == HyphenRole.BOTH
            and not last_line.hyphen_forward_pair_id
        )
        if needs_forward_link:
            _link_hyphen_pairs([last_line, first_line])

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
