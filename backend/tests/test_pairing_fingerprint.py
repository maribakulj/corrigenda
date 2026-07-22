"""The config fingerprint stamped into the output must reflect the job's
actual pairing policy (geometric_pairing opt-out).

`POST /api/jobs` builds `PairingPolicy(geometric_checks=geometric_pairing)`
and parses with it, but the runner used to construct the pipeline WITHOUT
that policy — so `config_fingerprint()` (which hashes the pairing policy)
always reflected the DEFAULT policy. A document parsed with
`geometric_pairing=false` was then stamped with a provenance fingerprint
claiming the default was used: the fingerprint lied.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from corrigenda.core.schemas import PairingPolicy
from corrigenda.formats.alto.parser import build_document_manifest
from lxml import etree

from app.jobs.runner import JobRunner
from app.jobs.store import JobStore
from app.schemas import Provider
from app.storage.output_writer import FilesystemOutputWriter
from tests.test_orchestrator import MockProvider

SAMPLE_XML = Path(__file__).parent.parent.parent / "examples" / "sample.xml"

_CONFIG_RE = re.compile(r"config ([0-9a-f]{8,})")


def _stamped_fingerprint(out_dir: Path) -> str:
    out_xml = next(out_dir.glob("*_corrected.xml"))
    text = etree.tostring(etree.parse(str(out_xml)).getroot(), encoding="unicode")
    m = _CONFIG_RE.search(text)
    assert m, f"no config fingerprint stamped in {out_xml.name}"
    return m.group(1)


async def _run_with_policy(out_dir: Path, policy: PairingPolicy | None) -> str:
    store = JobStore()
    job_id = store.create_job(Provider("openai"), "mock")
    doc = build_document_manifest(
        [(SAMPLE_XML, SAMPLE_XML.name)],
        pairing_policy=policy or PairingPolicy(),
    )
    await JobRunner(job_store=store).run(
        job_id=job_id,
        document_manifest=doc,
        provider_name="openai",
        api_key="fake-key",
        model="mock",
        output_writer=FilesystemOutputWriter(out_dir),
        source_files={SAMPLE_XML.name: SAMPLE_XML},
        provider=MockProvider(),
        pairing_policy=policy,
    )
    return _stamped_fingerprint(out_dir)


@pytest.mark.asyncio
async def test_geometric_pairing_optout_changes_stamped_fingerprint(tmp_path: Path):
    default_dir = tmp_path / "default"
    optout_dir = tmp_path / "optout"
    default_dir.mkdir()
    optout_dir.mkdir()

    fp_default = await _run_with_policy(default_dir, PairingPolicy())
    fp_optout = await _run_with_policy(optout_dir, PairingPolicy(geometric_checks=False))

    # The opt-out must produce a DIFFERENT fingerprint — the stamp reflects
    # the policy the document was actually processed under.
    assert fp_default != fp_optout

    # And the opt-out stamp must equal the library's fingerprint for that
    # exact policy (not merely "some other value").
    from corrigenda import CorrectionPipeline

    class _NoopObserver:
        def on_event(self, event_type: str, payload: dict) -> None:
            pass

    expected = CorrectionPipeline.for_provider(
        MockProvider(),
        api_key="k",
        model="mock",
        provider_name="openai",
        observer=_NoopObserver(),
        pairing_policy=PairingPolicy(geometric_checks=False),
    ).config_fingerprint()
    assert fp_optout == expected
