"""Backward-compat shim. Implementation lives in :mod:`alto_core.alto.rewriter`.

New code should import from `alto_core.alto.rewriter` directly. The shim now
re-exports only what alto-core declares in `__all__` — private
helpers (`_*`) and stdlib leaks (Path, asyncio, logging, ...)
are NOT visible here. Pull those directly from alto-core if a
test legitimately needs them.
"""

from alto_core.alto.rewriter import (
    RewriterMetrics,
    extract_output_texts,
    rewrite_alto_file,
)

__all__ = ["RewriterMetrics", "extract_output_texts", "rewrite_alto_file"]
