"""Providers API router."""

from __future__ import annotations

from corrigenda import sanitize_error
from corrigenda.core.protocols import (
    ProviderPermanentError,
    ProviderTransientError,
)
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
        # P2-11 — map the provider taxonomy onto meaningful statuses
        # instead of flattening everything to 400: the client can now
        # distinguish "your key is wrong" (401) from "you're rate
        # limited" (429), "the vendor is down" (502) and "the vendor
        # timed out" (504).
        status = 400
        if isinstance(exc, ProviderPermanentError):
            status = 401 if exc.status_code in (401, 403) else 400
        elif isinstance(exc, ProviderTransientError):
            if exc.status_code == 429:
                status = 429
            elif exc.status_code is None:
                status = 504  # transport timeout / network failure
            else:
                status = 502  # upstream 5xx
        raise HTTPException(
            status_code=status,
            detail=f"Provider error ({body.provider}): {safe}",
        ) from exc

    return ListModelsResponse(provider=body.provider, models=models)
