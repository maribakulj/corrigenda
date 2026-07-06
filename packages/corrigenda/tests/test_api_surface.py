"""§8 API-surface contracts: run_sync façade, frozen policies, public
provenance fingerprint (post-audit corrective round B)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError as PydanticValidationError

from corrigenda import CorrectionPipeline
from corrigenda.formats.alto.parser import build_document_manifest
from corrigenda.core.schemas import (
    ChunkPlannerConfig,
    GuardConfig,
    PairingPolicy,
    RetryPolicy,
)

_SAMPLE = Path(__file__).parent.parent.parent.parent / "examples" / "sample.xml"


class _IdentityProvider:
    async def list_models(self, api_key: str) -> list[Any]:
        return []

    async def complete_structured(self, **kw: Any) -> tuple[dict[str, Any], Any]:
        payload = kw["user_payload"]
        return {
            "lines": [
                {"line_id": ln["line_id"], "corrected_text": ln["ocr_text"]}
                for ln in payload.get("lines", [])
            ]
        }, None


class _Null:
    def on_event(self, *a: Any, **k: Any) -> None:
        pass

    def write_corrected(self, **k: Any) -> None:
        pass

    def write_trace(self, **k: Any) -> None:
        pass


def _pipeline() -> CorrectionPipeline:
    return CorrectionPipeline(
        provider=_IdentityProvider(), observer=_Null(), output_writer=_Null()
    )


# ---------------------------------------------------------------------------
# run_sync (§8.1)
# ---------------------------------------------------------------------------


def test_run_sync_runs_from_sync_context():
    doc = build_document_manifest([(_SAMPLE, _SAMPLE.name)])
    result = _pipeline().run_sync(
        document_manifest=doc,
        api_key="k",
        model="m",
        provider_name="mock",
        source_files={},
    )
    assert result.total_chunks >= 1
    assert result.report.total_lines > 0


@pytest.mark.asyncio
async def test_run_sync_refuses_running_loop():
    doc = build_document_manifest([(_SAMPLE, _SAMPLE.name)])
    with pytest.raises(RuntimeError):
        _pipeline().run_sync(
            document_manifest=doc,
            api_key="k",
            model="m",
            provider_name="mock",
            source_files={},
        )


# ---------------------------------------------------------------------------
# Frozen policies (§8.2) — all four
# ---------------------------------------------------------------------------


def test_all_four_policies_are_frozen_and_fingerprintable():
    for policy in (ChunkPlannerConfig(), GuardConfig(), PairingPolicy(), RetryPolicy()):
        fp = policy.policy_fingerprint()
        assert len(fp) == 16
        first_field = next(iter(type(policy).model_fields))
        with pytest.raises(PydanticValidationError):
            # every policy rejects mutation
            setattr(policy, first_field, object())


# ---------------------------------------------------------------------------
# Public provenance fingerprint (§11) — reproducible from the public API
# ---------------------------------------------------------------------------


def test_config_fingerprint_reproducible_from_public_api():
    pipeline = _pipeline()
    expected_payload = json.dumps(
        {
            "chunk_planner": pipeline.config.policy_fingerprint(),
            "guard": pipeline.guard_config.policy_fingerprint(),
            "pairing": pipeline.pairing_policy.policy_fingerprint(),
            "retry": pipeline.retry_policy.policy_fingerprint(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    expected = hashlib.sha256(expected_payload.encode("utf-8")).hexdigest()[:16]
    assert pipeline.config_fingerprint() == expected


def test_config_fingerprint_covers_pairing_policy():
    """Two pipelines differing ONLY by PairingPolicy must fingerprint
    differently — §8.2 lists all four policies in the configuration."""
    a = CorrectionPipeline(
        provider=_IdentityProvider(), observer=_Null(), output_writer=_Null()
    )
    b = CorrectionPipeline(
        provider=_IdentityProvider(),
        observer=_Null(),
        output_writer=_Null(),
        pairing_policy=PairingPolicy(same_block_only=True),
    )
    assert a.config_fingerprint() != b.config_fingerprint()
