"""Providers API router."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.providers import get_provider
from app.schemas import ListModelsRequest, ListModelsResponse

router = APIRouter()


@router.post("/models", response_model=ListModelsResponse)
async def list_models(body: ListModelsRequest) -> ListModelsResponse:
    """List available models for a given provider and API key."""
    try:
        provider = get_provider(body.provider)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {body.provider}") from exc

    try:
        models = await provider.list_models(body.api_key)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Provider error ({body.provider}): {exc}",
        ) from exc

    return ListModelsResponse(provider=body.provider, models=models)
