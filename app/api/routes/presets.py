"""Пресеты схем: список доступных встроенных пресетов."""

from __future__ import annotations

from fastapi import APIRouter

from app.presets import PRESETS
from app.schemas import PresetInfo, PresetListResponse

router = APIRouter(tags=["presets"])


@router.get("/v1/presets", response_model=PresetListResponse)
async def list_presets() -> PresetListResponse:
    return PresetListResponse(
        presets=[
            PresetInfo(name=name, title=schema.get("title", name), description=schema.get("description"))
            for name, schema in PRESETS.items()
        ]
    )
