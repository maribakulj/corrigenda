"""P3.6 — typed EngineEvents are THE definition of every payload.

Two pins: (1) the type↔class bijection — every PipelineEventType has
exactly one EngineEvent dataclass; (2) a real run's emitted payloads
match their class's fields exactly (the emit sites construct the
dataclasses, so an ad-hoc dict can no longer drift from the contract).
"""

from __future__ import annotations

import dataclasses

import pytest

from corrigenda.core import events as ev
from corrigenda.core.schemas import PipelineEventType

from tests._pipeline_harness import run_pipeline


def test_every_event_type_has_exactly_one_payload_class() -> None:
    by_type: dict[PipelineEventType, list[type]] = {}
    for cls in ev.EVENT_CLASSES:
        by_type.setdefault(cls.type, []).append(cls)
    duplicated = {t: c for t, c in by_type.items() if len(c) > 1}
    assert not duplicated, f"types with several payload classes: {duplicated}"
    missing = set(PipelineEventType) - set(by_type)
    assert not missing, f"engine event types without a payload class: {missing}"


def test_payload_is_exactly_the_dataclass_fields() -> None:
    e = ev.ChunkStarted(chunk_id="c1", granularity="page", line_count=3)
    assert e.payload() == {"chunk_id": "c1", "granularity": "page", "line_count": 3}
    assert e.type is PipelineEventType.CHUNK_STARTED
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.chunk_id = "other"  # type: ignore[misc]


def test_real_run_payloads_match_their_class_fields() -> None:
    """Every event a real run emits carries exactly its class's fields."""
    fields_by_value = {
        cls.type.value: {f.name for f in dataclasses.fields(cls)}
        for cls in ev.EVENT_CLASSES
    }
    run = run_pipeline("sample.xml")
    assert run.observer.events, "the run must have emitted events"
    seen: set[str] = set()
    for value, payload in run.observer.events:
        assert value in fields_by_value, f"unknown engine event {value!r}"
        assert set(payload) == fields_by_value[value], (
            f"{value}: payload keys {sorted(payload)} != declared fields "
            f"{sorted(fields_by_value[value])}"
        )
        seen.add(value)
    # The happy path exercises the core lifecycle events.
    for expected in (
        "document_parsed",
        "page_started",
        "chunk_planned",
        "chunk_started",
        "chunk_completed",
        "page_completed",
        "rewriter_stats",
    ):
        assert expected in seen, f"happy path did not emit {expected}"
