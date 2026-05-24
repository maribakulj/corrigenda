"""Backward-compat shim. Implementation lives in :mod:`alto_core.pipeline.validator`.

New code should import from `alto_core.pipeline.validator` directly. The shim now
re-exports only what alto-core declares in `__all__` — private
helpers (`_*`) and stdlib leaks (Path, asyncio, logging, ...)
are NOT visible here. Pull those directly from alto-core if a
test legitimately needs them.
"""

from alto_core.pipeline.validator import (  # noqa: F401  re-export
    validate_llm_response,
)
