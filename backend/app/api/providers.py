"""Providers API router."""

from __future__ import annotations

from alto_core import sanitize_error
from fastapi import APIRouter, HTTPException, Request

from app.api.rate_limit import limiter
from app.providers import get_provider
from app.schemas import ListModelsRequest, ListModelsResponse

router = APIRouter()


@router.post("/models", response_model=ListModelsResponse)
# Rate limited because the route validates user-supplied api_keys
# against upstream providers — without throttling it becomes a free
# credential-spray oracle.
@limiter.limit("10/minute")
async def list_models(request: Request, body: ListModelsRequest) -> ListModelsResponse:
    """List available models for a given provider and API key."""
    try:
        provider = get_provider(body.provider)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {body.provider}") from exc

    try:
        models = await provider.list_models(body.api_key)
    except Exception as exc:
        # L10/F1 — every provider can leak the api_key into its exception
        # message (httpx.HTTPStatusError repr embeds the request URL,
        # vendor SDKs sometimes echo the Authorization header, etc.).
        # `sanitize_error` redacts the literal key passed as a hint AND
        # known secret-shaped substrings (Bearer …, sk-…, api_key=…,
        # x-api-key: …) so even a future provider that leaks via an
        # unanticipated path cannot reach the HTTP response in clear.
        safe = sanitize_error(str(exc), api_key=body.api_key)
        raise HTTPException(
            status_code=400,
            detail=f"Provider error ({body.provider}): {safe}",
        ) from exc

    return ListModelsResponse(provider=body.provider, models=models)
