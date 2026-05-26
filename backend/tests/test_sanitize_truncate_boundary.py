"""Pin the sanitize-then-truncate ordering inside JobRunner (audit §12.2).

`runner.py:131-134` documents the contract:

    # Sanitise BEFORE truncating: if the api_key straddles the 500-char
    # boundary, slicing first would leave half the key visible and the
    # regex would fail to mask it.

`test_sanitize_error.py::test_truncate_then_sanitize_loses_partial_key`
already pins this property at the helper level. This file pins it at
the runner's *caller* level: a real exception escaping the pipeline
with an api_key sitting around the 500-char boundary must produce a
sanitised, truncated `job.error` without a partial leak.

If a future refactor inverts the ordering (truncate then sanitise),
this test catches it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.alto.parser import build_document_manifest
from app.jobs.correction_pipeline import CorrectionPipeline
from app.jobs.runner import JobRunner
from app.jobs.store import JobStore
from app.schemas import ModelInfo, Provider

SAMPLE_XML = Path(__file__).parent.parent.parent / "examples" / "sample.xml"


class _NullProvider:
    """No-op provider — never invoked because we monkeypatch the pipeline."""

    async def list_models(self, api_key: str) -> list[ModelInfo]:
        return [ModelInfo(id="mock", label="mock")]

    async def complete_structured(self, **kwargs: Any) -> dict[str, Any]:
        return {"lines": []}


def _make_store_and_job() -> tuple[JobStore, str]:
    store = JobStore()
    job_id = store.create_job(Provider.OPENAI, "mock")
    return store, job_id


@pytest.mark.asyncio
async def test_runner_does_not_leak_partial_api_key_across_500_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Place the api_key so its first half falls before char 500 and the
    second half after. Truncate-first would leave the first half visible
    (no whole-substring match). Sanitize-first replaces the full key
    before truncation."""
    api_key = "sk-secret-token-12345678"
    # Anchor the key at index ~490 of the message — straddling the 500 cut.
    prefix = "X" * 490
    suffix = "Y" * 200
    long_msg = f"{prefix}{api_key}{suffix}"
    assert len(long_msg) > 500
    # Sanity: the boundary truly splits the key.
    assert long_msg[:500] != prefix + api_key  # truncated form omits part of key
    assert api_key[:6] in long_msg[:500]  # first half present in truncated msg

    async def _boom(self, **kwargs):
        raise RuntimeError(long_msg)

    monkeypatch.setattr(CorrectionPipeline, "run", _boom)

    store, job_id = _make_store_and_job()
    doc = build_document_manifest([(SAMPLE_XML, SAMPLE_XML.name)])
    await JobRunner(job_store=store).run(
        job_id=job_id,
        document_manifest=doc,
        provider_name="openai",
        api_key=api_key,
        model="mock",
        output_dir=tmp_path,
        source_files={SAMPLE_XML.name: SAMPLE_XML},
        provider=_NullProvider(),
    )

    job = store.get_job(job_id)
    assert job is not None
    assert job.status.value == "failed"
    assert job.error is not None

    # The whole api_key must be absent.
    assert api_key not in job.error
    # The boundary-leaked partial must also be absent.
    assert "sk-secret-token-1234" not in job.error
    assert "sk-secret-token" not in job.error
    # And the error must still respect the 500-char truncation.
    assert len(job.error) <= 500
    # The redaction marker should be present (prove sanitize ran, not just truncate).
    assert "sk-s****" in job.error or "sk-se****" in job.error


@pytest.mark.asyncio
async def test_runner_sanitizes_short_message_with_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Baseline: a short message already covered by T-014. Re-asserting it
    here keeps the boundary test honest — if both ordering pass, the
    sanitization is genuinely happening at the caller."""
    api_key = "sk-secret-token-12345678"
    msg = f"failure carrying {api_key} mid-string"

    async def _boom(self, **kwargs):
        raise RuntimeError(msg)

    monkeypatch.setattr(CorrectionPipeline, "run", _boom)

    store, job_id = _make_store_and_job()
    doc = build_document_manifest([(SAMPLE_XML, SAMPLE_XML.name)])
    await JobRunner(job_store=store).run(
        job_id=job_id,
        document_manifest=doc,
        provider_name="openai",
        api_key=api_key,
        model="mock",
        output_dir=tmp_path,
        source_files={SAMPLE_XML.name: SAMPLE_XML},
        provider=_NullProvider(),
    )

    job = store.get_job(job_id)
    assert job is not None
    assert job.status.value == "failed"
    assert job.error is not None
    assert api_key not in job.error
    assert "sk-s****" in job.error
