"""ADR-008 — the error hierarchy is ONE tree under ``CorrectionError``.

``errors.py`` promises "a single root … above every error the library
raises so consumers can ``except CorrectionError`` once". These tests
make that promise structural instead of documentary:

- every public error class is a subclass of the root — including the
  provider errors, which historically inherited bare ``Exception`` and
  silently escaped an ``except CorrectionError`` catch-all;
- each error carries a stable machine ``code`` and a ``retryable`` flag
  so hosts can route on class attributes instead of string-matching
  messages;
- reparenting must NOT soften fatality: a ``ProviderPermanentError``
  still propagates out of ``run()`` (never absorbed as a chunk error,
  never degraded to OCR fallback).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from corrigenda import CorrectionPipeline
from corrigenda.core.protocols import (
    ProviderPermanentError,
    ProviderTransientError,
)
from corrigenda.core.validator import HyphenIntegrityError
from corrigenda.errors import (
    CorrectionAborted,
    CorrectionError,
    DuplicateIdError,
    ParseError,
    ProviderError,
    ValidationError,
)
from corrigenda.formats.alto.parser import build_document_manifest

_SAMPLE = Path(__file__).parent.parent.parent.parent / "examples" / "sample.xml"

_PUBLIC_ERRORS = [
    CorrectionError,
    ParseError,
    DuplicateIdError,
    ValidationError,
    HyphenIntegrityError,
    CorrectionAborted,
    ProviderError,
    ProviderTransientError,
    ProviderPermanentError,
]


def test_every_public_error_is_under_the_single_root() -> None:
    for exc_type in _PUBLIC_ERRORS:
        assert issubclass(exc_type, CorrectionError), (
            f"{exc_type.__name__} escapes `except CorrectionError` — the "
            "documented single-root contract is broken"
        )


def test_provider_errors_form_their_own_branch() -> None:
    assert issubclass(ProviderTransientError, ProviderError)
    assert issubclass(ProviderPermanentError, ProviderError)
    # NOT ValueError: the retry classifier routes ValueError to the
    # malformed-output branch, which would mis-file transport failures.
    assert not issubclass(ProviderTransientError, ValueError)
    assert not issubclass(ProviderPermanentError, ValueError)


def test_error_codes_are_stable_and_unique() -> None:
    codes = {exc_type: exc_type.code for exc_type in _PUBLIC_ERRORS}
    assert all(isinstance(c, str) and c for c in codes.values())
    # Every concrete class names itself — no subclass silently reuses its
    # parent's code, which would make codes useless for routing.
    assert len(set(codes.values())) == len(codes), (
        f"duplicate machine codes: {sorted(codes.values())}"
    )


def test_retryable_flags_match_adr_008_semantics() -> None:
    assert ProviderTransientError.retryable is True
    assert ProviderPermanentError.retryable is False
    # Malformed producer output is retried (fresh attempt may parse).
    assert ValidationError.retryable is True
    assert HyphenIntegrityError.retryable is True
    # Source problems and cancellations never heal by retrying.
    assert ParseError.retryable is False
    assert DuplicateIdError.retryable is False
    assert CorrectionAborted.retryable is False


class _Null:
    def on_event(self, *a, **k):
        pass

    def write_corrected(self, *, source_stem, xml_bytes):
        pass

    def write_trace(self, *, traces_payload):
        pass


class _PermanentlyRejectedProducer:
    wants_geometry = False
    wants_image = False
    requires_full_coverage = False

    async def produce(self, payload, *, options):
        raise ProviderPermanentError("invalid credentials", status_code=401)


@pytest.mark.asyncio
async def test_permanent_provider_error_still_fails_the_whole_run() -> None:
    """Reparenting under CorrectionError must not soften ADR-008 fatality:
    the chunk loop absorbs recoverable CorrectionErrors, but a permanent
    provider rejection propagates out of run() before any output."""
    doc = build_document_manifest([(_SAMPLE, _SAMPLE.name)])
    pipeline = CorrectionPipeline(
        producer=_PermanentlyRejectedProducer(),
        observer=_Null(),
        provider_name="rejected",
        model="m",
    )
    with pytest.raises(ProviderPermanentError):
        await pipeline.run(
            document_manifest=doc,
            source_files={_SAMPLE.name: _SAMPLE},
        )
    # And the single-root contract now actually covers it:
    assert issubclass(ProviderPermanentError, CorrectionError)
