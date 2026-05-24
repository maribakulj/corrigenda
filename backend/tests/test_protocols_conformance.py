"""Verify existing classes structurally satisfy the Protocols in `app.protocols`.

These tests pin the contract: any future refactor that breaks a Protocol
shape (e.g. renames a method on `JobStore`, drops a kwarg from a provider)
will trip a fast, clear failure here instead of a confusing TypeError
deep inside the pipeline.
"""
from __future__ import annotations

from app.jobs.store import JobStore as JobStoreImpl
from app.protocols import BaseProvider, JobStore, PipelineObserver
from app.providers.anthropic_provider import AnthropicProvider
from app.providers.google_provider import GoogleProvider
from app.providers.mistral_provider import MistralProvider
from app.providers.openai_provider import OpenAIProvider


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
