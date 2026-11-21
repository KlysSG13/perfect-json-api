"""Пользовательские вебхуки (WEBHOOKS_ENABLED=false по умолчанию)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from app.api.deps import SessionDep, SettingsDep, require_scope
from app.exceptions import InvalidRequestError, NotFoundError
from app.models import ApiKey
from app.services.webhooks import SUPPORTED_EVENTS, WebhookService

router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])


def _service(request: Request, session) -> WebhookService:
    return WebhookService(session, request.app.state.settings, request.app.state.webhook_transport)


@router.post("", status_code=201)
async def create_webhook(
    payload: dict[str, Any],
    request: Request,
    api_key: Annotated[ApiKey, Depends(require_scope("keys.write"))],
    session: SessionDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    if not settings.webhooks_enabled:
        raise NotFoundError("Webhooks are disabled (WEBHOOKS_ENABLED=false)")
    url = str(payload.get("url", "")).strip()
    events = payload.get("events") or list(SUPPORTED_EVENTS)
    if not url.startswith(("http://", "https://")):
        raise InvalidRequestError("Field 'url' must be an http(s) URL")
    endpoint, secret = await _service(request, session).create_endpoint(api_key.id, url, list(events))
    return {
        "id": endpoint.id,
        "url": endpoint.url,
        "events": endpoint.events,
        "secret": secret,
        "note": "Store the secret now: it is shown only once and is used to verify signatures.",
    }


@router.get("")
async def list_webhooks(
    request: Request,
    api_key: Annotated[ApiKey, Depends(require_scope("keys.read"))],
    session: SessionDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    if not settings.webhooks_enabled:
        raise NotFoundError("Webhooks are disabled (WEBHOOKS_ENABLED=false)")
    endpoints = await _service(request, session).list_endpoints(api_key.id)
    return {
        "items": [
            {"id": item.id, "url": item.url, "events": item.events, "active": item.active}
            for item in endpoints
        ]
    }


@router.delete("/{endpoint_id}")
async def delete_webhook(
    endpoint_id: int,
    request: Request,
    api_key: Annotated[ApiKey, Depends(require_scope("keys.write"))],
    session: SessionDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    if not settings.webhooks_enabled:
        raise NotFoundError("Webhooks are disabled (WEBHOOKS_ENABLED=false)")
    deleted = await _service(request, session).delete_endpoint(api_key.id, endpoint_id)
    if not deleted:
        raise NotFoundError(f"Webhook endpoint {endpoint_id} not found")
    return {"deleted": True}
