"""P3.11 (first slice) — the error root gets its final name.

``CorrigendaError`` (named for the LIBRARY) replaces ``CorrectionError``
as the root; ``ProposalValidationError`` replaces the pydantic-colliding
``ValidationError``. The old names remain 0.9.x deprecation ALIASES of
the very same classes: ``except``, ``isinstance`` and subclassing behave
identically through either name. Removed at the top-level reduction.
"""

from __future__ import annotations

import corrigenda
from corrigenda.errors import (
    CorrectionAborted,
    CorrectionError,
    CorrigendaError,
    ParseError,
    ProposalValidationError,
    ProviderError,
    ValidationError,
)


def test_old_names_are_the_same_classes():
    assert CorrectionError is CorrigendaError
    assert ValidationError is ProposalValidationError


def test_catch_compatibility_both_directions():
    try:
        raise CorrigendaError("boom")
    except CorrectionError as exc:  # old name catches new raise
        assert isinstance(exc, CorrigendaError)

    try:
        raise ValidationError("bad proposal")  # old name still raisable
    except ProposalValidationError as exc:  # new name catches it
        assert exc.code == "invalid_producer_output"


def test_hierarchy_is_unchanged():
    # Every §8.4 family still sits under the single root.
    for cls in (ParseError, ProposalValidationError, ProviderError, CorrectionAborted):
        assert issubclass(cls, CorrigendaError)
    # The machine codes did not move with the rename.
    assert CorrigendaError.code == "correction_error"
    assert ProposalValidationError.code == "invalid_producer_output"
    # HyphenIntegrityError keeps its place under the renamed parent.
    from corrigenda.core.validator import HyphenIntegrityError

    assert issubclass(HyphenIntegrityError, ProposalValidationError)


def test_both_names_are_top_level_exports():
    for name in (
        "CorrigendaError",
        "CorrectionError",
        "ProposalValidationError",
        "ValidationError",
    ):
        assert getattr(corrigenda, name) is not None
        assert name in corrigenda.__all__
