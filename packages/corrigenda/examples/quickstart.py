#!/usr/bin/env python3
"""corrigenda quickstart — correct an ALTO file end-to-end, offline.

Runs the full pipeline on the repo's ``examples/sample.xml`` with two
producers, no network and no API key:

  1. a deterministic ``RulesProducer`` (ſ→s + a demo substitution) — the
     §5.3 span path;
  2. an identity LLM provider via ``CorrectionPipeline.for_provider`` —
     the §5.2 whole-line path, shaped exactly like a real vendor client.

Outputs land in ``./quickstart-out/`` via ``result.write(dir)``
(ADR-011 — the engine never persists; the result carries the corrected
ALTO and the versioned CorrectionReport, §9, written as
``report.json``).

Usage:  python examples/quickstart.py [output_dir]
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from corrigenda import (
    CorrectionPipeline,
    RulesProducer,
    SubstitutionRule,
    build_document_manifest,
    default_french_ocr_rules,
)

# Repo-relative sample (this script lives in packages/corrigenda/examples/).
SAMPLE = Path(__file__).resolve().parents[3] / "examples" / "sample.xml"


class PrintObserver:
    """Minimal PipelineObserver: log lifecycle events to stdout."""

    def on_event(self, event_type, payload):
        print(f"  [{event_type}] {payload}")


class IdentityProvider:
    """LLM-shaped provider that returns each line unchanged (offline demo).

    Swap in a real vendor client implementing the same two methods to go
    live — nothing else in this script changes.
    """

    async def list_models(self, api_key):
        return []

    async def complete_structured(
        self, api_key, model, system_prompt, user_payload, json_schema, temperature=0.0
    ):
        return {
            "lines": [
                {"line_id": ln["line_id"], "corrected_text": ln["ocr_text"]}
                for ln in user_payload["lines"]
            ]
        }, None  # (parsed_json, Usage|None) — F14


async def main(out_dir: Path) -> None:
    doc = build_document_manifest([(SAMPLE, SAMPLE.name)])
    print(
        f"Parsed {doc.total_lines} lines / {doc.total_pages} page(s) from {SAMPLE.name}"
    )

    # ------------------------------------------------------------------
    # 1. Deterministic rules pass (§5.3) — replace_span ops, exact offsets
    # ------------------------------------------------------------------
    rules = RulesProducer(
        default_french_ocr_rules() + [SubstitutionRule("e", "3", name="demo_e3")]
    )
    # No provenance labels needed: RulesProducer DECLARES its identity
    # (ProducerMetadata name="rules" + a fingerprint of its rules table),
    # and the §11 stamp picks it up. Pass producer_metadata=... to override.
    pipeline = CorrectionPipeline(
        producer=rules,
        observer=PrintObserver(),
    )
    result = await pipeline.run(
        document_manifest=doc,
        source_files={SAMPLE.name: SAMPLE},
        run_id="quickstart-rules",
    )
    # ADR-011 — persistence is the caller's: one call writes the
    # corrected XML (under its source name) and report.json.
    result.write(out_dir / "rules")
    changed = sum(
        1 for t in result.report.lines if t.decision.final_text != t.source_text
    )
    print(
        f"[rules] {changed}/{result.report.total_lines} lines edited via "
        f"{len(result.edit_script.ops)} span op(s); outputs in {out_dir / 'rules'}"
    )

    # ------------------------------------------------------------------
    # 2. LLM-shaped pass (§5.2) — for_provider wraps provider + key
    # ------------------------------------------------------------------
    doc2 = build_document_manifest([(SAMPLE, SAMPLE.name)])
    pipeline2 = CorrectionPipeline.for_provider(
        IdentityProvider(),
        api_key="",  # a real key goes HERE, never on run()
        model="identity-demo",
        provider_name="local",
        observer=PrintObserver(),
    )
    result2 = await pipeline2.run(
        document_manifest=doc2,
        source_files={SAMPLE.name: SAMPLE},
        run_id="quickstart-llm",
    )
    result2.write(out_dir / "llm")
    print(
        f"[llm] report v{result2.report.report_version}: "
        f"{result2.report.total_lines} lines, "
        f"{result2.total_reconciled} hyphen pair(s) reconciled, "
        f"{result2.usage.total_tokens} token(s); outputs in {out_dir / 'llm'}"
    )


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("./quickstart-out")
    asyncio.run(main(target))
    print("Done.")
