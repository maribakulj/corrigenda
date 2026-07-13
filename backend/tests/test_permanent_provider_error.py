"""P0-1 — a permanent provider error can NEVER produce a successful job.

The audit's central false-success finding: an invalid API key (401), a
forbidden account (403) or an unknown model (404) used to make every
chunk silently fall back to its OCR source text, and the job ended
COMPLETED — the UI announced success on a run where the provider never
worked once.

The contract now: our providers wrap 4xx-non-429 as
``ProviderPermanentError`` (pinned in ``test_providers.py``); the
pipeline propagates it out of ``run()`` without retry, downgrade or
fallback; the ``JobRunner`` lands the job in FAILED with an actionable,
credential-free error. COMPLETED strictly means "zero fallbacks";
degraded runs end in COMPLETED_WITH_FALLBACKS.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from corrigenda.core.protocols import ProviderPermanentError
from corrigenda.formats.alto.parser import build_document_manifest

from app.jobs.runner import JobRunner
from app.jobs.store import JobStore
from app.schemas import JobStatus, ModelInfo, Provider
from app.storage.output_writer import FilesystemOutputWriter

SAMPLE_XML = Path(__file__).resolve().parent.parent.parent / "examples" / "sample.xml"


class _PermanentlyRejectedProvider:
    """Simulates what our providers raise on 401/403/404 (via
    ``_wrap_if_transient``): the request is definitively rejected."""

    def __init__(self, status: int = 401) -> None:
        self.status = status
        self.calls = 0

    async def list_models(self, api_key: str) -> list[ModelInfo]:  # pragma: no cover
        return []

    async def complete_structured(self, **_kwargs: Any) -> Any:
        self.calls += 1
        raise ProviderPermanentError(
            f"provider rejected the request (HTTP {self.status}) — check the "
            "API key, model name and request format",
            status_code=self.status,
        )


async def _run_job(provider: _PermanentlyRejectedProvider, tmp_path: Path):
    store = JobStore()
    job_id = store.create_job(Provider("openai"), "mock")
    doc = build_document_manifest([(SAMPLE_XML, SAMPLE_XML.name)])
    await JobRunner(job_store=store).run(
        job_id=job_id,
        document_manifest=doc,
        provider_name="openai",
        api_key="sk-invalid-key-123456",
        model="mock",
        output_writer=FilesystemOutputWriter(tmp_path),
        source_files={SAMPLE_XML.name: SAMPLE_XML},
        provider=provider,
    )
    return store, job_id


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 404])
async def test_permanent_provider_error_fails_the_job(tmp_path: Path, status: int):
    """THE audit invariant: 401/403/404 → FAILED, never any success state."""
    provider = _PermanentlyRejectedProvider(status)
    store, job_id = await _run_job(provider, tmp_path)

    job = store.get_job(job_id)
    assert job is not None
    assert job.status == JobStatus.FAILED
    assert job.status not in (JobStatus.COMPLETED, JobStatus.COMPLETED_WITH_FALLBACKS)
    assert job.error is not None
    assert str(status) in job.error
    # The API key must never leak into the stored error.
    assert "sk-invalid-key-123456" not in job.error


@pytest.mark.asyncio
async def test_permanent_error_is_not_retried_per_chunk(tmp_path: Path):
    """Fail-fast: one provider call total — no 3-attempt retry burn, no
    granularity-downgrade descent re-spending API calls per chunk."""
    provider = _PermanentlyRejectedProvider(401)
    store, job_id = await _run_job(provider, tmp_path)

    assert provider.calls == 1
    job = store.get_job(job_id)
    assert job is not None and job.status == JobStatus.FAILED


@pytest.mark.asyncio
async def test_permanent_error_writes_no_output(tmp_path: Path):
    """The run aborts before any corrected XML or trace is persisted —
    /download has nothing to serve for a failed-auth job."""
    provider = _PermanentlyRejectedProvider(403)
    await _run_job(provider, tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_failed_event_emitted_with_sanitized_error(tmp_path: Path):
    store = JobStore()
    job_id = store.create_job(Provider("openai"), "mock")
    queue: asyncio.Queue = store.subscribe(job_id)
    doc = build_document_manifest([(SAMPLE_XML, SAMPLE_XML.name)])
    await JobRunner(job_store=store).run(
        job_id=job_id,
        document_manifest=doc,
        provider_name="openai",
        api_key="sk-invalid-key-123456",
        model="mock",
        output_writer=FilesystemOutputWriter(tmp_path),
        source_files={SAMPLE_XML.name: SAMPLE_XML},
        provider=_PermanentlyRejectedProvider(401),
    )
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    failed = [e for e in events if e.event == "failed"]
    completed = [e for e in events if e.event == "completed"]
    assert failed, "a permanent provider error must emit a terminal failed event"
    assert not completed, "no completed event may follow a permanent provider error"
    assert "sk-invalid-key-123456" not in str(failed[0].data)
