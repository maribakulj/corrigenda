"""Error hierarchy contract (spec §8.4)."""

from __future__ import annotations

from alto_core import (
    CorrectionAborted,
    CorrectionError,
    ParseError,
    ValidationError,
)
from alto_core.pipeline.validator import HyphenIntegrityError, validate_llm_response


def test_all_errors_are_correction_errors():
    for exc_type in (
        ParseError,
        ValidationError,
        CorrectionAborted,
        HyphenIntegrityError,
    ):
        assert issubclass(exc_type, CorrectionError)


def test_value_shaped_errors_still_subclass_valueerror():
    # §8.4 — bare ValueError call sites keep working.
    for exc_type in (ParseError, ValidationError, HyphenIntegrityError):
        assert issubclass(exc_type, ValueError)


def test_hyphen_integrity_is_a_validation_error():
    assert issubclass(HyphenIntegrityError, ValidationError)


def test_abort_is_not_a_valueerror():
    # Cancellation is a control-flow signal, not a value error.
    assert not issubclass(CorrectionAborted, ValueError)


def test_validator_raises_validation_error_caught_as_valueerror():
    # A structural failure raises ValidationError, catchable both ways.
    for catch in (ValidationError, ValueError, CorrectionError):
        try:
            validate_llm_response(raw={"no_lines": []}, expected_line_ids=["L1"])
        except catch:
            pass
        else:  # pragma: no cover
            raise AssertionError(f"not caught as {catch!r}")
