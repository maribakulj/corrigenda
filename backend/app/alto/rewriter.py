"""Backward-compat shim. Implementation lives in :mod:`alto_core.alto.rewriter`.

New code should import from `alto_core.alto.rewriter` directly. This module exists
so that the existing `from app.alto.rewriter import X` imports keep
working during the Phase 2 / Phase 3 extraction. Once consumers
migrate, this shim can be deleted.
"""

from alto_core.alto.rewriter import (  # noqa: F401  re-export
    HyphenRole,
    LineManifest,
    PageManifest,
    Path,
    RewriterMetrics,
    _add_processing_entry,
    _apply_subs,
    _clear_line,
    _compute_geometry,
    _desired_forward_subs,
    _desired_subs,
    _detect_namespace,
    _extract_text_from_line,
    _get_hyp_children,
    _get_sp_children,
    _get_string_children,
    _line_text_unchanged,
    _rebuild_hyp_part1,
    _rebuild_hyp_part2,
    _rebuild_normal_line,
    _set_subs_on_element,
    _subs_need_update,
    _subs_target,
    _tag,
    _tokenize,
    _update_content_in_place,
    clean_content,
    copy,
    dataclass,
    etree,
    extract_output_texts,
    nfc,
    re,
    rewrite_alto_file,
)
