"""Adapt a text ``BaseProvider`` (LLM) to the ``EditProducer`` contract.

From v2.0 the LLM is *an implementation* of the edit protocol (§5.1/§5.2),
not the protocol itself. This adapter binds a provider with its credentials
and system prompt / output schema, and turns the historical
``{lines:[{line_id, corrected_text}]}`` structured response into a
``replace_line`` :class:`EditScript` — byte-equivalent to the direct path
(proved in ``test_editing``), plus the token ``Usage`` (F14).

Structural validation and the guard matrix (E6) stay downstream in the
pipeline; this adapter only shapes the provider call into the protocol.
It is a **text** producer: ``wants_geometry`` / ``wants_image`` are False.
"""

from __future__ import annotations

from typing import Any

from corrigenda.core.editing import EditScript, ReplaceLine
from corrigenda.core.protocols import BaseProvider
from corrigenda.core.schemas import LLMUserPayload, RetryPolicy, Usage


class LLMEditProducer:
    """Wrap a :class:`BaseProvider` as an :class:`EditProducer`."""

    wants_geometry: bool = False
    wants_image: bool = False

    def __init__(
        self,
        provider: BaseProvider,
        api_key: str,
        model: str,
        *,
        system_prompt: str,
        output_schema: dict[str, Any],
    ) -> None:
        self._provider = provider
        self._api_key = api_key
        self._model = model
        self._system_prompt = system_prompt
        self._output_schema = output_schema

    async def produce(
        self, payload: LLMUserPayload, *, policy: RetryPolicy
    ) -> tuple[EditScript, Usage | None]:
        raw, usage = await self._provider.complete_structured(
            api_key=self._api_key,
            model=self._model,
            system_prompt=self._system_prompt,
            user_payload=payload.model_dump(),
            json_schema=self._output_schema,
            temperature=policy.temperature_for(1),
        )
        ops: list[ReplaceLine] = []
        lines = raw.get("lines", []) if isinstance(raw, dict) else []
        if isinstance(lines, list):
            for entry in lines:
                if not isinstance(entry, dict):
                    continue
                line_id = entry.get("line_id")
                text = entry.get("corrected_text")
                if line_id and isinstance(text, str):
                    ops.append(ReplaceLine(line_id=line_id, text=text))
        return EditScript(ops=ops), usage  # type: ignore[arg-type]


__all__ = ["LLMEditProducer"]
