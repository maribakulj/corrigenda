"""ADR-008 revised — recoverability is an ALLOWLIST, not a denylist.

The attempt path historically re-raised eight known programming-error
types and degraded EVERYTHING else to retry-then-OCR-fallback: an
unknown exception — a ``RuntimeError`` from a producer bug, a raw SDK
transport error nobody wrapped — ended as a "successful" run with
silently uncorrected text. An unknown exception must never become a
degraded success.

Recoverable is now exactly what the retry classifier can route:

- ``ProviderTransientError`` — transport flakiness a conforming
  provider wrapped (wrapping is the provider contract, not a courtesy);
- ``ValueError`` (which covers ``ValidationError``,
  ``HyphenIntegrityError`` and ``json.JSONDecodeError``) — the
  documented malformed-producer-output family.

Everything else fails the run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from corrigenda import CorrectionPipeline
from corrigenda.core.protocols import ProviderTransientError
from corrigenda.core.schemas import LineStatus, RetryPolicy
from corrigenda.formats.alto.parser import build_document_manifest

_SAMPLE = Path(__file__).parent.parent.parent.parent / "examples" / "sample.xml"


class _Null:
    def on_event(self, *a, **k):
        pass

    def write_corrected(self, *, source_stem, xml_bytes):
        pass

    def write_trace(self, *, traces_payload):
        pass


class _Raising:
    wants_geometry = False
    wants_image = False
    requires_full_coverage = False

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def produce(self, payload, *, policy):
        raise self._exc


class _UnwrappedTransportError(Exception):
    """Stand-in for a raw SDK/httpx exception a provider failed to wrap."""


def _pipeline(producer) -> CorrectionPipeline:
    return CorrectionPipeline(
        producer=producer,
        observer=_Null(),
        output_writer=_Null(),
        # Zero backoff: these tests exercise CLASSIFICATION, not pacing —
        # real backoffs would make the degradation paths sleep for tens
        # of seconds through retries and granularity descent.
        retry_policy=RetryPolicy(transient_backoff_base=0.0, output_backoff_base=0.0),
        provider_name="x",
        model="m",
    )


async def _run(producer):
    doc = build_document_manifest([(_SAMPLE, _SAMPLE.name)])
    result = await _pipeline(producer).run(
        document_manifest=doc, source_files={_SAMPLE.name: _SAMPLE}, apply=False
    )
    return doc, result


@pytest.mark.asyncio
async def test_runtime_error_fails_the_run() -> None:
    """The acceptance criterion of the allowlist: a producer raising
    RuntimeError must FAIL the run — under the old denylist it degraded
    every chunk to OCR fallback and reported success."""
    with pytest.raises(RuntimeError, match="unexpected producer bug"):
        await _run(_Raising(RuntimeError("unexpected producer bug")))


@pytest.mark.asyncio
async def test_unwrapped_transport_error_fails_the_run() -> None:
    """A raw transport exception nobody wrapped is indistinguishable
    from a bug — the provider contract (wrap as ProviderTransientError)
    is enforced by failing, not by guessing."""
    with pytest.raises(_UnwrappedTransportError):
        await _run(_Raising(_UnwrappedTransportError("connection reset")))


@pytest.mark.asyncio
async def test_wrapped_transient_error_still_degrades_to_fallback() -> None:
    doc, result = await _run(
        _Raising(ProviderTransientError("upstream 503", status_code=503))
    )
    statuses = {lm.status for page in doc.pages for lm in page.lines}
    assert statuses == {LineStatus.FALLBACK}
    assert result.fallback_lines == sum(len(p.lines) for p in doc.pages)
    assert result.retry_count > 0, "transients are retried before falling back"


@pytest.mark.asyncio
async def test_bare_value_error_stays_recoverable() -> None:
    """The documented malformed-output family (ValueError and subclasses)
    keeps degrading: value-shaped errors are producer-output errors by
    contract (§8.4), not bugs."""
    doc, result = await _run(_Raising(ValueError("malformed output")))
    statuses = {lm.status for page in doc.pages for lm in page.lines}
    assert statuses == {LineStatus.FALLBACK}
    assert result.fallback_lines == sum(len(p.lines) for p in doc.pages)
