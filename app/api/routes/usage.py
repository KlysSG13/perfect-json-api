"""История использования текущего API-ключа: GET /v1/usage."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.deps import SessionDep, require_api_key
from app.models import ApiKey
from app.schemas import UsageItem, UsageListResponse
from app.services.usage import UsageService

router = APIRouter(prefix="/v1", tags=["usage"])


@router.get("/usage", response_model=UsageListResponse)
async def usage(
    session: SessionDep,
    api_key: ApiKey = Depends(require_api_key),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> UsageListResponse:
    service = UsageService(session)
    items, total = await service.list_for_key(api_key.id, limit=limit, offset=offset)
    return UsageListResponse(
        items=[UsageItem.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )
