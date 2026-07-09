"""PAGE XML parser — builds the same ``DocumentManifest`` as ALTO (6.3).

Walks ``PcGts → Page → TextRegion → TextLine → Word`` and produces the
format-neutral manifests the pure core consumes. Geometry is read from
``Coords@points`` polygons: the polygon is kept verbatim on
``Coords.polygon`` and the enclosing bbox drives the planner (P1). Line
text follows P2/P3 (see ``_text``). Hyphenation is heuristic-only (P5):
``hyphen_source_explicit`` is always ``False`` and no ``SUBS_CONTENT`` is
invented; the shared ``core.pairing`` linker does the second pass exactly
as it does for ALTO.
"""

from __future__ import annotations

from pathlib import Path

from lxml import etree

from corrigenda.core._parse import parse_int_tolerant
from corrigenda.core.pairing import (
    HYPHEN_CHARS,
    disambiguate_page_ids,
    link_cross_page_hyphens,
    link_hyphen_pairs,
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
from corrigenda.formats.page._ns import (
    _detect_namespace,
    _tag,
    make_safe_parser,
    polygon_to_bbox,
)
from corrigenda.formats.page._text import canonical_line_text


def _coords_of(el: etree._Element, ns: str) -> Coords:
    """Build a Coords (bbox + verbatim polygon) from an element's Coords child."""
    coords_el = el.find(_tag("Coords", ns))
    points = coords_el.get("points", "") if coords_el is not None else ""
    hpos, vpos, width, height = polygon_to_bbox(points)
    return Coords(
        hpos=hpos,
        vpos=vpos,
        width=width,
        height=height,
        polygon=points or None,
    )


def _assign_hyphen_roles(lines: list[LineManifest]) -> None:
    """First-pass heuristic hyphenation for PAGE (P5).

    A line ending in a word-break hyphen is PART1; if the line *before* it
    also ends in one, it is the middle of a chain → BOTH. Everything is
    heuristic: ``hyphen_source_explicit`` / ``hyphen_forward_explicit`` stay
    ``False`` and no SUBS content is invented. The PART2 side is assigned
    later by the shared linker when it consumes a PART1/BOTH line.
    """
    trailing = [trailing_hyphen_char(lm.ocr_text, HYPHEN_CHARS) for lm in lines]
    for i, lm in enumerate(lines):
        if trailing[i] is None:
            continue
        prev_hyphenated = i > 0 and trailing[i - 1] is not None
        if prev_hyphenated:
            lm.hyphen_role = HyphenRole.BOTH
        else:
            lm.hyphen_role = HyphenRole.PART1
        # Heuristic mode throughout — no explicit flags, no subs content.


def parse_page_file(
    xml_path: Path,
    source_name: str,
    page_index_offset: int = 0,
    global_line_offset: int = 0,
    pairing_policy: PairingPolicy = DEFAULT_PAIRING_POLICY,
) -> tuple[list[PageManifest], etree._Element]:
    """Parse one PAGE XML file → (list_of_PageManifest, root_element)."""
    tree = etree.parse(str(xml_path), make_safe_parser())
    root = tree.getroot()
    ns = _detect_namespace(root)

    pages: list[PageManifest] = []
    global_line_idx = global_line_offset

    for page_idx, page_el in enumerate(root.findall(_tag("Page", ns))):
        # PAGE has no Page@ID; the image filename is the stable identity.
        raw_name = page_el.get("imageFilename")
        page_id = raw_name or f"PAGE_{page_index_offset + page_idx}"
        page_width = _int_or_zero(page_el.get("imageWidth"))
        page_height = _int_or_zero(page_el.get("imageHeight"))

        blocks: list[BlockManifest] = []
        lines: list[LineManifest] = []

        block_order = 0
        for region in page_el.findall(_tag("TextRegion", ns)):
            block_id = region.get("id", f"TR_{page_id}_{block_order}")
            block_coords = _coords_of(region, ns)
            line_ids: list[str] = []
            line_order_in_block = 0

            for tl in region.findall(_tag("TextLine", ns)):
                line_id = tl.get("id", f"TL_{block_id}_{line_order_in_block}")
                coords = _coords_of(tl, ns)
                ocr_text = canonical_line_text(tl, ns)

                lm = LineManifest(
                    line_id=line_id,
                    page_id=page_id,
                    block_id=block_id,
                    line_order_global=global_line_idx,
                    line_order_in_block=line_order_in_block,
                    coords=coords,
                    ocr_text=ocr_text,
                )
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

        # Link prev/next in reading order.
        for i, lm in enumerate(lines):
            if i > 0:
                lm.prev_line_id = lines[i - 1].line_id
            if i < len(lines) - 1:
                lm.next_line_id = lines[i + 1].line_id

        # First-pass heuristic roles, then the shared second-pass linker.
        _assign_hyphen_roles(lines)
        link_hyphen_pairs(lines, pairing_policy)

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


def _int_or_zero(raw: str | None) -> int:
    """PAGE dimensions: blank/float/garbage → 0 (shared tolerant policy)."""
    return parse_int_tolerant(raw, 0)


def build_document_manifest(
    files: list[tuple[Path, str]],
    pairing_policy: PairingPolicy = DEFAULT_PAIRING_POLICY,
) -> DocumentManifest:
    """Build a DocumentManifest from PAGE files (mirrors the ALTO builder)."""
    source_files: list[str] = []
    page_offset = 0
    line_offset = 0
    parsed: list[tuple[str, list[PageManifest]]] = []

    for xml_path, source_name in files:
        source_files.append(source_name)
        pages, _ = parse_page_file(
            xml_path, source_name, page_offset, line_offset, pairing_policy
        )
        parsed.append((source_name, pages))
        page_offset += len(pages)
        for p in pages:
            line_offset += len(p.lines)

    disambiguate_page_ids(parsed)

    all_pages: list[PageManifest] = [p for _, pages in parsed for p in pages]
    link_cross_page_hyphens(all_pages, pairing_policy)

    total_blocks = sum(len(p.blocks) for p in all_pages)
    total_lines = sum(len(p.lines) for p in all_pages)

    return DocumentManifest(
        source_files=source_files,
        pages=all_pages,
        total_pages=len(all_pages),
        total_blocks=total_blocks,
        total_lines=total_lines,
    )


__all__ = [
    "parse_page_file",
    "build_document_manifest",
]
