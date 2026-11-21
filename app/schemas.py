"""Pydantic v2 модели запросов и ответов API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


# ---------------------------------------------------------------- transform


class TransformOptions(BaseModel):
    max_repairs: int | None = Field(default=None, ge=0, le=5)
    include_debug: bool = False
    max_cost_credits: int | None = Field(default=None, gt=0)
    normalization: str | None = Field(default=None, pattern="^(off|basic|strict)$")
    include_provenance: bool = False


class TransformRequest(BaseModel):
    raw_text: str
    schema_: dict[str, Any] | None = Field(default=None, alias="schema")
    schema_preset: str | None = None
    options: TransformOptions | None = None

    model_config = ConfigDict(populate_by_name=True)


class TokenUsageInfo(BaseModel):
    input_tokens: int | None = None
    output_tokens: int | None = None


class TransformDebugInfo(BaseModel):
    attempts_detail: list[dict[str, Any]] = Field(default_factory=list)


class TransformMeta(BaseModel):
    schema_name: str
    schema_used: dict[str, Any] = Field(serialization_alias="schema")
    attempts: int
    llm_calls: int
    cost_credits: int
    warnings: list[str] = Field(default_factory=list)
    tokens: TokenUsageInfo | None = None
    debug: TransformDebugInfo | None = None
    prompt_version: str | None = None
    normalization: dict[str, Any] | None = None
    provenance: dict[str, Any] | None = None
    llm_model: str | None = None
    fallback_used: bool = False
    balance_after: int | None = None


class TransformResponse(BaseModel):
    data: dict[str, Any] | list[Any] | None = None
    meta: TransformMeta


# ---------------------------------------------------------------- api keys


class ApiKeyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    credits: int = Field(default=0, ge=0)
    is_active: bool = True
    per_request_max_cost_credits: int | None = Field(default=None, gt=0)
    daily_spend_limit_credits: int | None = Field(default=None, gt=0)
    monthly_spend_limit_credits: int | None = Field(default=None, gt=0)
    rate_limit_rpm: int | None = Field(default=None, gt=0)
    rate_limit_rpd: int | None = Field(default=None, gt=0)
    concurrency_limit: int | None = Field(default=None, gt=0)
    scopes: list[str] | None = None


class ApiKeyCreatedResponse(BaseModel):
    id: int
    name: str
    key_prefix: str
    api_key: str
    credits_balance: int
    is_active: bool
    created_at: datetime


class ApiKeyInfoResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    key_prefix: str
    is_active: bool
    credits_balance: int
    created_at: datetime
    last_used_at: datetime | None = None


class ApiKeyBalanceResponse(BaseModel):
    id: int
    name: str
    credits_balance: int


class CreditsDeltaRequest(BaseModel):
    credits_delta: int
    reason: str | None = Field(default=None, max_length=500)


# ------------------------------------------------------------------ usage


class UsageItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    endpoint: str
    input_type: str
    schema_name: str
    input_chars: int
    llm_calls: int
    attempts: int
    success: bool
    cost_credits: int
    error_code: str | None = None


class UsageListResponse(BaseModel):
    items: list[UsageItem]
    total: int
    limit: int
    offset: int


class AdminUsageItem(UsageItem):
    api_key_id: int


class AdminUsageListResponse(BaseModel):
    items: list[AdminUsageItem]
    total: int
    limit: int
    offset: int


# ------------------------------------------------------------------ health


class HealthResponse(BaseModel):
    status: str
    version: str
    llm_configured: bool
    ocr_enabled: bool
    stt_enabled: bool


# ----------------------------------------------------------------- presets


class PresetInfo(BaseModel):
    name: str
    title: str
    description: str | None = None


class PresetListResponse(BaseModel):
    presets: list[PresetInfo]
