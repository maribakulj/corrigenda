"""P3.7 — produce() receives a per-call envelope, not the RetryPolicy.

The engine owns retry/downgrade; a producer only learns about THIS
call: the attempt number, the resolved temperature (ramp + hyphen pin
decided engine-side), and the run's cancellation probe — so long I/O
can be abandoned mid-flight instead of only between chunks.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from corrigenda import CorrectionPipeline, EditProducer, ProducerOptions
from corrigenda.core.editing import EditScript, ReplaceLine
from corrigenda.formats.alto.parser import build_document_manifest

_SAMPLE = Path(__file__).parent.parent.parent.parent / "examples" / "sample.xml"


class _Null:
    def on_event(self, *a, **k):
        pass


class _Recording:
    wants_geometry = False
    wants_image = False
    requires_full_coverage = False

    def __init__(self) -> None:
        self.options: list[ProducerOptions] = []

    async def produce(self, payload, *, options):
        self.options.append(options)
        ops = [
            ReplaceLine(line_id=line.line_id, text=line.ocr_text)
            for line in payload.lines
        ]
        return EditScript(ops=ops), None


def test_seam_signature_is_options_not_policy() -> None:
    params = inspect.signature(EditProducer.produce).parameters
    assert "options" in params and "policy" not in params
    assert not ProducerOptions(should_abort=None).cancelled()
    assert ProducerOptions(should_abort=lambda: True).cancelled()


@pytest.mark.asyncio
async def test_producer_receives_the_runs_probe_and_attempt_state() -> None:
    doc = build_document_manifest([(_SAMPLE, _SAMPLE.name)])
    producer = _Recording()
    probe_calls = {"n": 0}

    def probe() -> bool:
        probe_calls["n"] += 1
        return False

    pipeline = CorrectionPipeline(
        producer=producer, observer=_Null(), provider_name="x", model="m"
    )
    await pipeline.run(
        document_manifest=doc,
        source_files={_SAMPLE.name: _SAMPLE},
        should_abort=probe,
    )
    assert producer.options, "the producer must have been called"
    for opt in producer.options:
        assert opt.attempt == 1  # no retries in a clean identity run
        assert opt.temperature == pipeline.retry_policy.temperature_for(1)
        assert opt.should_abort is probe  # the RUN\'S probe, verbatim
    # The producer can poll the probe itself.
    assert producer.options[0].cancelled() is False
    assert probe_calls["n"] > 0
