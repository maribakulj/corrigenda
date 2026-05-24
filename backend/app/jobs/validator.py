"""Backward-compat shim. Implementation lives in :mod:`alto_core.pipeline.validator`.

New code should import from `alto_core.pipeline.validator` directly. This module exists
so that the existing `from app.jobs.validator import X` imports keep
working during the Phase 2 / Phase 3 extraction. Once consumers
migrate, this shim can be deleted.
"""

from alto_core.pipeline.validator import (  # noqa: F401  re-export
    LLMLineOutput,
    LLMResponse,
    _check_pair_drift,
    _validate_hyphen_integrity,
    ncfold,
    validate_llm_response,
)
