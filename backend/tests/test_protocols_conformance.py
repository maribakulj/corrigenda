"""Verify existing classes structurally satisfy the Protocols in `app.protocols`.

These tests pin the contract: any future refactor that breaks a Protocol
shape (e.g. renames a method on `JobStore`, drops a kwarg from a provider)
will trip a fast, clear failure here instead of a confusing TypeError
deep inside the pipeline.
"""

from __future__ import annotations

from pathlib import Path

from app.jobs.store import JobStore as JobStoreImpl
from app.protocols import BaseProvider, JobStore, OutputWriter, PipelineObserver
from app.providers.anthropic_provider import AnthropicProvider
from app.providers.google_provider import GoogleProvider
from app.providers.mistral_provider import MistralProvider
from app.providers.openai_provider import OpenAIProvider
from app.storage.output_writer import FilesystemOutputWriter


def test_concrete_providers_implement_base_provider():
    """The four shipped providers must satisfy BaseProvider via duck typing."""
    assert isinstance(OpenAIProvider(), BaseProvider)
    assert isinstance(AnthropicProvider(), BaseProvider)
    assert isinstance(MistralProvider(), BaseProvider)
    assert isinstance(GoogleProvider(), BaseProvider)


def test_in_memory_job_store_implements_jobstore_protocol():
    """JobStoreImpl must satisfy the JobStore Protocol."""
    assert isinstance(JobStoreImpl(), JobStore)


def test_pipeline_observer_is_runtime_checkable():
    """A minimal class with `on_event` should pass the structural check."""

    class _NoopObserver:
        def on_event(self, event_type, payload):
            pass

    assert isinstance(_NoopObserver(), PipelineObserver)


def test_pipeline_observer_rejects_missing_method():
    """A class WITHOUT `on_event` must NOT satisfy PipelineObserver."""

    class _Empty:
        pass

    assert not isinstance(_Empty(), PipelineObserver)


def test_filesystem_output_writer_implements_output_writer_protocol(tmp_path: Path):
    """FilesystemOutputWriter must satisfy the OutputWriter Protocol."""
    assert isinstance(FilesystemOutputWriter(tmp_path), OutputWriter)


def test_filesystem_output_writer_persists_corrected_and_trace(tmp_path: Path):
    """Writer round-trip: bytes/strings handed in are read back identically."""
    writer = FilesystemOutputWriter(tmp_path)

    writer.write_corrected(source_stem="doc1", xml_bytes=b"<xml>corrected</xml>")
    writer.write_trace(traces_payload='{"job_id":"j1","lines":[]}')

    assert (tmp_path / "doc1_corrected.xml").read_bytes() == b"<xml>corrected</xml>"
    assert (tmp_path / "trace.json").read_text(encoding="utf-8") == '{"job_id":"j1","lines":[]}'


def test_jobstore_observer_adapter_implements_pipeline_observer():
    """The runner's internal JobStore→Observer adapter must satisfy the Protocol."""
    from app.jobs.observers import JobStoreObserver

    observer = JobStoreObserver(JobStoreImpl(), job_id="j1")
    assert isinstance(observer, PipelineObserver)


def test_jobstore_observer_forwards_events_to_store():
    """Events on the observer must surface on the wrapped store."""
    from app.jobs.observers import JobStoreObserver
    from app.schemas import Provider as ProviderEnum

    store = JobStoreImpl()
    job_id = store.create_job(ProviderEnum.OPENAI, "mock")
    queue = store.subscribe(job_id)

    observer = JobStoreObserver(store, job_id)
    observer.on_event("custom_event", {"k": "v"})

    event = queue.get_nowait()
    assert event.event == "custom_event"
    assert event.data == {"k": "v"}
