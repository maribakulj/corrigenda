"""ALTO implementation of the core ``FormatAdapter`` port (§3 seam)."""

from __future__ import annotations

from pathlib import Path

from corrigenda.core.schemas import PageManifest
from corrigenda.formats.alto.rewriter import (
    RewriterMetrics,
    extract_output_texts,
    rewrite_alto_file,
)


class AltoFormatAdapter:
    """Thin adapter: the pipeline's format seam, bound to ALTO."""

    def rewrite_file(
        self,
        xml_path: Path,
        pages: list[PageManifest],
        provider: str,
        model: str,
        *,
        lib_version: str | None = None,
        config_fingerprint: str | None = None,
    ) -> tuple[bytes, RewriterMetrics, dict[str, str]]:
        return rewrite_alto_file(
            xml_path,
            pages,
            provider,
            model,
            lib_version=lib_version,
            config_fingerprint=config_fingerprint,
        )

    def extract_texts(self, xml_bytes: bytes, line_ids: set[str]) -> dict[str, str]:
        return extract_output_texts(xml_bytes, line_ids)


__all__ = ["AltoFormatAdapter"]
