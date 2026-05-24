"""Backward-compat shim. Implementation lives in :mod:`alto_core.alto.parser`.

New code should import from `alto_core.alto.parser` directly. This module exists
so that the existing `from app.alto.parser import X` imports keep
working during the Phase 2 / Phase 3 extraction. Once consumers
migrate, this shim can be deleted.
"""

from alto_core.alto.parser import (  # noqa: F401  re-export
    BlockManifest,
    Coords,
    DocumentManifest,
    HyphenRole,
    LineManifest,
    PageManifest,
    Path,
    _build_ocr_text,
    _detect_hyphenation,
    _detect_namespace,
    _disambiguate_page_ids,
    _link_hyphen_pairs,
    _parse_textline_hyphen_info,
    _tag,
    build_document_manifest,
    etree,
    parse_alto_file,
    unicodedata,
)
