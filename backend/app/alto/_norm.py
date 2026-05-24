"""Backward-compat shim. Implementation lives in :mod:`alto_core.alto._norm`.

New code should import from `alto_core.alto._norm` directly. This module exists
so that the existing `from app.alto._norm import X` imports keep
working during the Phase 2 / Phase 3 extraction. Once consumers
migrate, this shim can be deleted.
"""

from alto_core.alto._norm import (  # noqa: F401  re-export
    clean_content,
    ncfold,
    nfc,
    unicodedata,
)
