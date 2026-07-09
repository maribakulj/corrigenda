"""Pure read-model projections for a completed job.

Extracted from the jobs router (audit Problem 4): the /diff and /layout
endpoints held ~50 lines each of manifest→JSON presentation logic mixed in
with the router's ingest / SSE / download concerns. These functions are
pure (a ``DocumentManifest`` in, a JSON-able ``dict`` out) with no FastAPI
or HTTP dependency, so they are unit-testable without spinning up the app.
The endpoints in ``app.api.jobs`` are now thin adapters that resolve the
job, guard the HTTP preconditions, and delegate here.
"""

from __future__ import annotations

from app.schemas import DocumentManifest, HyphenRole


def build_diff(job_id: str, document_manifest: DocumentManifest) -> dict:
    """Per-line OCR-vs-corrected diff, page by page, with roll-up stats."""
    pages_out = []
    total_lines = 0
    modified_lines = 0
    hyphen_pairs = 0

    for page in document_manifest.pages:
        lines_out = []
        for lm in page.lines:
            corrected = lm.corrected_text if lm.corrected_text is not None else lm.ocr_text
            modified = corrected != lm.ocr_text
            lines_out.append(
                {
                    "line_id": lm.line_id,
                    "ocr_text": lm.ocr_text,
                    "corrected_text": corrected,
                    "modified": modified,
                    "hyphen_role": lm.hyphen_role.value,
                    "hyphen_subs_content": lm.hyphen_subs_content,
                }
            )
            total_lines += 1
            if modified:
                modified_lines += 1
            if lm.hyphen_role == HyphenRole.PART1:
                hyphen_pairs += 1

        pages_out.append(
            {
                "page_id": page.page_id,
                "page_index": page.page_index,
                "lines": lines_out,
            }
        )

    return {
        "job_id": job_id,
        "pages": pages_out,
        "stats": {
            "total_lines": total_lines,
            "modified_lines": modified_lines,
            "hyphen_pairs": hyphen_pairs,
        },
    }


def build_layout(
    job_id: str,
    document_manifest: DocumentManifest,
    images: dict[str, str],
) -> dict:
    """Structural layout: blocks + lines with ALTO coordinates, per page.

    Page dimensions are derived from line coordinates when the source Page
    element omits WIDTH/HEIGHT. ``images`` maps source_file → image filename;
    a matching entry becomes the page's ``image_url``.
    """
    pages_out = []
    for page in document_manifest.pages:
        line_by_id = {lm.line_id: lm for lm in page.lines}

        blocks_out = []
        for block in page.blocks:
            lines_out = []
            for line_id in block.line_ids:
                lm = line_by_id.get(line_id)
                if lm is None:
                    continue
                corrected = lm.corrected_text if lm.corrected_text is not None else lm.ocr_text
                lines_out.append(
                    {
                        "line_id": lm.line_id,
                        "hpos": lm.coords.hpos,
                        "vpos": lm.coords.vpos,
                        "width": lm.coords.width,
                        "height": lm.coords.height,
                        "ocr_text": lm.ocr_text,
                        "corrected_text": corrected,
                        "modified": corrected != lm.ocr_text,
                        "hyphen_role": lm.hyphen_role.value,
                    }
                )
            blocks_out.append(
                {
                    "block_id": block.block_id,
                    "hpos": block.coords.hpos,
                    "vpos": block.coords.vpos,
                    "width": block.coords.width,
                    "height": block.coords.height,
                    "lines": lines_out,
                }
            )

        # Derive page dimensions from line coordinates if the source Page
        # element doesn't carry WIDTH/HEIGHT (some producers omit them).
        pw = page.page_width
        ph = page.page_height
        if pw == 0 or ph == 0:
            xs = [lm.coords.hpos + lm.coords.width for lm in page.lines]
            ys = [lm.coords.vpos + lm.coords.height for lm in page.lines]
            if pw == 0 and xs:
                pw = max(xs)
            if ph == 0 and ys:
                ph = max(ys)

        # images is keyed by source_file (not page_id) to avoid collisions
        # when multiple ALTO files share the same Page/@ID value.
        image_filename = images.get(page.source_file)
        image_url = f"/api/jobs/{job_id}/images/{image_filename}" if image_filename else None
        pages_out.append(
            {
                "page_id": page.page_id,
                "page_index": page.page_index,
                "page_width": pw,
                "page_height": ph,
                "image_url": image_url,
                "blocks": blocks_out,
            }
        )

    return {"job_id": job_id, "pages": pages_out}


__all__ = ["build_diff", "build_layout"]
