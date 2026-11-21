"""Информация о текущем API-ключе: GET /v1/me."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import require_api_key
from app.models import ApiKey
from app.schemas import ApiKeyInfoResponse

router = APIRouter(prefix="/v1", tags=["keys"])


@router.get("/me", response_model=ApiKeyInfoResponse)
async def me(api_key: ApiKey = Depends(require_api_key)) -> ApiKeyInfoResponse:
    return ApiKeyInfoResponse.model_validate(api_key)
