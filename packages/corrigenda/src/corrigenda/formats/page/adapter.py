"""PAGE implementation of the core ``FormatAdapter`` port (section 3 seam)."""

from __future__ import annotations

from pathlib import Path

from corrigenda.core.schemas import PageManifest
from corrigenda.formats.page.rewriter import (
    PageRewriterMetrics,
    extract_output_texts,
    rewrite_page_file,
)


class PageFormatAdapter:
    """Thin adapter: the pipeline's format seam, bound to PAGE XML."""

    def rewrite_file(
        self,
        xml_path: Path,
        pages: list[PageManifest],
        provider: str,
        model: str,
        *,
        lib_version: str | None = None,
        config_fingerprint: str | None = None,
    ) -> tuple[bytes, PageRewriterMetrics, dict[str, str]]:
        return rewrite_page_file(
            xml_path,
            pages,
            provider,
            model,
            lib_version=lib_version,
            config_fingerprint=config_fingerprint,
        )

    def extract_texts(self, xml_bytes: bytes, line_ids: set[str]) -> dict[str, str]:
        return extract_output_texts(xml_bytes, line_ids)


__all__ = ["PageFormatAdapter"]
