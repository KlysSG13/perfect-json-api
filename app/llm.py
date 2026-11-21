"""LLM-клиент: любой OpenAI-совместимый провайдер через переменные окружения.

Возможности:
- основной провайдер: LLM_BASE_URL + LLM_API_KEY + LLM_MODEL;
- одна автоматическая повторная попытка при таймауте / 429 / 5xx
  (LLM_RETRY_ENABLED, задержка LLM_RETRY_DELAY_SECONDS);
- опциональная fallback-модель: LLM_FALLBACK_* — один вызов через
  резервный провайдер, если основной недоступен после повтора;
- если LLM_API_KEY не задан, используется NotConfiguredLLMClient:
  трансформации возвращают llm_unavailable (503), сервис работает.

Повторные попытки не знают о биллинге и выполняются до расчёта
фактической стоимости: тарифицируется реально выполненная работа.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.config import Settings
from app.exceptions import LLMNotConfiguredError, LLMProviderError, LLMTimeoutError

logger = logging.getLogger(__name__)

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


@dataclass
class LLMResult:
    """Результат одного вызова LLM."""

    content: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    provider: str = "primary"
    model: str | None = None
    fallback_used: bool = False


class LLMClient(Protocol):
    async def complete(
        self,
        *,
        system: str,
        user: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        """Один вызов чат-комплишена. Raise LLM*Error при проблемах провайдера."""
        ...


class NotConfiguredLLMClient:
    """Заглушка, когда LLM_API_KEY не задан."""

    async def complete(
        self,
        *,
        system: str,
        user: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        raise LLMNotConfiguredError(
            "LLM is not configured: set LLM_API_KEY (and optionally LLM_BASE_URL, LLM_MODEL)"
        )


class OpenAICompatibleLLMClient:
    """HTTP-клиент к /chat/completions OpenAI-совместимого API."""

    def __init__(
        self,
        settings: Settings,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        provider_name: str = "primary",
    ) -> None:
        self._settings = settings
        self._base_url = (base_url or settings.llm_base_url).rstrip("/")
        self._api_key = api_key or settings.llm_api_key
        self._model = model or settings.llm_model
        self.provider_name = provider_name
        self._client = httpx.AsyncClient(timeout=settings.llm_timeout_seconds)

    async def complete(
        self,
        *,
        system: str,
        user: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self._settings.llm_temperature if temperature is None else temperature,
            "max_tokens": self._settings.llm_max_output_tokens if max_tokens is None else max_tokens,
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}
        url = self._base_url + "/chat/completions"

        try:
            response = await self._client.post(url, json=payload, headers=headers)
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(
                f"LLM request timed out after {self._settings.llm_timeout_seconds}s"
            ) from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            logger.warning(
                "LLM provider %s returned HTTP %s for model %s",
                self.provider_name,
                status,
                self._model,
            )
            raise LLMProviderError(
                f"LLM provider returned HTTP {status}", status_code=status
            ) from exc
        except httpx.HTTPError as exc:
            logger.warning("LLM provider %s connection failed: %s", self.provider_name, type(exc).__name__)
            raise LLMProviderError("LLM provider connection failed") from exc

        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise ValueError("content is not a string")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMProviderError("LLM provider returned malformed response") from exc

        usage = body.get("usage") or {}
        input_tokens = usage.get("prompt_tokens")
        output_tokens = usage.get("completion_tokens")
        return LLMResult(
            content=content,
            input_tokens=int(input_tokens) if isinstance(input_tokens, int) else None,
            output_tokens=int(output_tokens) if isinstance(output_tokens, int) else None,
            provider=self.provider_name,
            model=self._model,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


class ResilientLLMClient:
    """Ретрай (1×) на таймаут/429/5xx + один вызов через fallback."""

    def __init__(self, primary: LLMClient, fallback: LLMClient | None = None, settings: Settings | None = None) -> None:
        self._primary = primary
        self._fallback = fallback
        self._settings = settings

    async def complete(
        self,
        *,
        system: str,
        user: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        retry_enabled = self._settings.llm_retry_enabled if self._settings else True
        delay = self._settings.llm_retry_delay_seconds if self._settings else 0.5

        first_error: Exception | None = None
        for attempt in (1, 2) if retry_enabled else (1,):
            try:
                return await self._primary.complete(
                    system=system, user=user, temperature=temperature, max_tokens=max_tokens
                )
            except (LLMTimeoutError, LLMProviderError) as exc:
                first_error = exc
                retryable = _is_retryable(exc)
                if attempt == 1 and retryable and delay > 0:
                    await asyncio.sleep(delay)
                    continue
                break

        if self._fallback is not None and _is_retryable_or_unavailable(first_error):
            logger.warning("Primary LLM failed (%s); trying fallback provider", type(first_error).__name__)
            result = await self._fallback.complete(
                system=system, user=user, temperature=temperature, max_tokens=max_tokens
            )
            result.fallback_used = True
            result.provider = "fallback"
            return result

        assert first_error is not None
        raise first_error

    async def aclose(self) -> None:
        for client in (self._primary, self._fallback):
            close = getattr(client, "aclose", None)
            if close is not None:
                await close()


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, LLMTimeoutError):
        return True
    if isinstance(exc, LLMProviderError):
        return exc.provider_status_code in RETRYABLE_STATUS_CODES or exc.provider_status_code is None
    return False


def _is_retryable_or_unavailable(exc: Exception | None) -> bool:
    if exc is None:
        return False
    return isinstance(exc, (LLMTimeoutError, LLMProviderError))


def build_llm_client(settings: Settings) -> LLMClient:
    if not settings.llm_configured:
        return NotConfiguredLLMClient()
    primary: LLMClient = OpenAICompatibleLLMClient(settings)
    fallback: LLMClient | None = None
    if settings.fallback_configured:
        fallback = OpenAICompatibleLLMClient(
            settings,
            base_url=settings.llm_fallback_base_url,
            api_key=settings.llm_fallback_api_key,
            model=settings.llm_fallback_model,
            provider_name="fallback",
        )
    return ResilientLLMClient(primary, fallback, settings)


def safe_json_dumps(value: Any) -> str:
    """Сериализация без утечки: используется для промптов и логов меты."""
    return json.dumps(value, ensure_ascii=False, indent=2)
