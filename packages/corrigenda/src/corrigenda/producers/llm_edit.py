"""Adapt a ``StructuredCompletionClient`` (LLM) to the ``EditProducer`` contract.

Since the §5.1 resorption the LLM is *an implementation* of the edit
protocol, not the protocol itself: the pipeline only ever talks to an
``EditProducer``, and this adapter is what turns a provider + credentials
+ prompt/schema into one. It converts the historical
``{lines:[{line_id, corrected_text}]}`` structured response into a
``replace_line`` :class:`EditScript` — byte-equivalent to the direct path
(proved in ``test_editing``) — plus the token ``Usage`` (F14).

Structural validation and the guard matrix (E6) stay downstream in the
pipeline; this adapter only shapes the provider call into the protocol.
Malformed response entries (non-dict, missing ``line_id``, non-string
text) yield no op — the pipeline's validator then reports the line as
missing and the retry machinery takes over, exactly as it did on the raw
dict. It is a **text** producer: ``wants_geometry`` / ``wants_image`` are
``False``; ``requires_full_coverage`` is ``True`` because an LLM asked to
correct N target lines must return all N — a dropped line is a degraded
response, not a "no edit".
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from corrigenda.core.confidence import DEFAULT_CONFUSIONS, score_producer_claims
from corrigenda.core.editing import EditScript, ReplaceLine
from corrigenda.core.protocols import (
    ProducerMetadata,
    ProducerOptions,
    StructuredCompletionClient,
)
from corrigenda.core.schemas import CorrectionRequest, Usage
from corrigenda.integrations.llm import (
    OUTPUT_JSON_SCHEMA,
    SYSTEM_PROMPT,
    uncertainty_output_schema,
    uncertainty_system_prompt,
)


class LLMEditProducer:
    """Wrap a :class:`StructuredCompletionClient` as an :class:`EditProducer`.

    ``system_prompt`` / ``output_schema`` default to the canonical LLM
    contract (:mod:`corrigenda.integrations.llm`); inject to experiment.
    """

    wants_geometry: bool = False
    wants_image: bool = False
    #: An LLM must cover every line it was asked to correct (a missing
    #: target line means a degraded response → validator error → retry).
    #: Deterministic producers set this False: no op simply means no edit.
    requires_full_coverage: bool = True

    def __init__(
        self,
        provider: StructuredCompletionClient,
        api_key: str,
        model: str,
        *,
        system_prompt: str | None = None,
        output_schema: dict[str, Any] | None = None,
        uncertainty_channel: bool = False,
        lexicon: set[str] | None = None,
        confusions: tuple[tuple[str, str], ...] = DEFAULT_CONFUSIONS,
    ) -> None:
        self._provider = provider
        self._api_key = api_key
        self._model = model
        # Phase 1 uncertainty channel (opt-in): the contract variant asks
        # the model for a per-line status and per-token reason-coded
        # edits; produce() VERIFIES those claims (confusion table /
        # lexicon) and stamps the resulting score on each ReplaceLine.
        # An explicit prompt/schema injection always wins over the
        # channel's defaults.
        self._uncertainty_channel = uncertainty_channel
        self._lexicon = lexicon
        self._confusions = confusions
        default_prompt = (
            uncertainty_system_prompt() if uncertainty_channel else SYSTEM_PROMPT
        )
        default_schema = (
            uncertainty_output_schema() if uncertainty_channel else OUTPUT_JSON_SCHEMA
        )
        self._system_prompt = default_prompt if system_prompt is None else system_prompt
        self._output_schema = default_schema if output_schema is None else output_schema
        #: Declared provenance (P3.7-4) — the adapter cannot know the
        #: vendor's marketing name, so ``name`` stays the generic "llm";
        #: ``for_provider(provider_name=…)`` overrides it with the
        #: caller's label via explicit constructor metadata (carrying
        #: this fingerprint along). The fingerprint covers the producer
        #: CONFIGURATION — system prompt + output schema, the two knobs
        #: that change what the model is asked; the model itself is the
        #: ``implementation`` field, same split as RulesProducer.
        self.metadata = ProducerMetadata(
            name="llm",
            implementation=model,
            configuration_fingerprint=self._config_fingerprint(),
        )

    def _config_fingerprint(self) -> str:
        """Stable 16-hex sha256 over prompt + schema (same shape as the
        rules producer's and the pipeline's §11 policy fingerprint)."""
        payload = json.dumps(
            {
                "system_prompt": self._system_prompt,
                "output_schema": self._output_schema,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    async def produce(
        self, payload: CorrectionRequest, *, options: ProducerOptions
    ) -> tuple[EditScript, Usage | None]:
        raw, usage = await self._provider.complete_structured(
            api_key=self._api_key,
            model=self._model,
            system_prompt=self._system_prompt,
            # exclude_none matches the historical direct-call payload byte
            # for byte (None hyphen/vision fields never reached providers).
            user_payload=payload.model_dump(exclude_none=True),
            json_schema=self._output_schema,
            # The pipeline drives the retry ramp: the envelope carries
            # this attempt's resolved temperature (P3.7).
            temperature=options.temperature,
        )
        source_by_id = {
            ln.get("line_id"): ln.get("ocr_text", "")
            for ln in payload.model_dump().get("lines", [])
        }
        ops: list[ReplaceLine] = []
        lines = raw.get("lines", []) if isinstance(raw, dict) else []
        if isinstance(lines, list):
            for entry in lines:
                if not isinstance(entry, dict):
                    continue
                line_id = entry.get("line_id")
                text = entry.get("corrected_text")
                if not line_id or not isinstance(text, str):
                    continue
                confidence: float | None = None
                if self._uncertainty_channel:
                    status = entry.get("status")
                    claims = entry.get("edits")
                    confidence = score_producer_claims(
                        source_text=source_by_id.get(line_id, ""),
                        corrected_text=text,
                        status=status if isinstance(status, str) else None,
                        claims=claims if isinstance(claims, list) else [],
                        confusions=self._confusions,
                        lexicon=self._lexicon,
                    )
                ops.append(
                    ReplaceLine(
                        line_id=line_id, text=text, producer_confidence=confidence
                    )
                )
        return EditScript(ops=ops), usage  # type: ignore[arg-type]


__all__ = ["LLMEditProducer"]
