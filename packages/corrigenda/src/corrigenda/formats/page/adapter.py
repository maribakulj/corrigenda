"""PAGE implementation of the core ``FormatAdapter`` port (section 3 seam)."""

from __future__ import annotations

from pathlib import Path

from corrigenda.core.protocols import RewriteResult
from corrigenda.core.schemas import PageManifest
from corrigenda.formats.page.rewriter import rewrite_page_file


class PageFormatAdapter:
    """Thin adapter: the pipeline's format seam, bound to PAGE XML."""

    #: Matches ``DocumentManifest.source_format`` — the engine refuses a
    #: run whose manifest declares a different format.
    format_name = "page"

    def rewrite_file(
        self,
        xml_path: Path,
        pages: list[PageManifest],
        provider: str,
        model: str,
        *,
        lib_version: str | None = None,
        config_fingerprint: str | None = None,
    ) -> RewriteResult:
        return rewrite_page_file(
            xml_path,
            pages,
            provider,
            model,
            lib_version=lib_version,
            config_fingerprint=config_fingerprint,
        )


__all__ = ["PageFormatAdapter"]
