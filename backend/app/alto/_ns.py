"""Backward-compat shim. Implementation lives in :mod:`alto_core.alto._ns`.

New code should import from `alto_core.alto._ns` directly. This module exists
so that the existing `from app.alto._ns import X` imports keep
working during the Phase 2 / Phase 3 extraction. Once consumers
migrate, this shim can be deleted.
"""

from alto_core.alto._ns import (  # noqa: F401  re-export
    _detect_namespace,
    _tag,
    etree,
)
