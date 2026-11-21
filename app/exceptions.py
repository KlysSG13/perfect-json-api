"""Единая иерархия ошибок приложения.

Каждая ошибка имеет машиночитаемый код, HTTP-статус, категорию
(client / provider / internal / content / limits / security / billing),
опциональные details и опциональные HTTP-заголовки (например, Retry-After).
Обработчик в app.api.main приводит их к единому формату:

    {"error": {"code": "...", "message": "...", "details": {...}}}
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Базовая ошибка приложения."""

    code: str = "internal_error"
    status: int = 500
    message: str = "Internal server error"
    category: str = "internal"

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        if message is not None:
            self.message = message
        self.details = details or {}
        self.headers = headers or {}
        super().__init__(self.message)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


# ------------------------------------------------------------------- клиентские


class UnauthorizedError(AppError):
    code = "unauthorized"
    status = 401
    message = "Missing or invalid API key"
    category = "client"


class ForbiddenError(AppError):
    code = "forbidden"
    status = 403
    message = "Access denied"
    category = "security"


class ForbiddenScopeError(AppError):
    code = "forbidden_scope"
    status = 403
    message = "API key scope does not allow this action"
    category = "security"


class InvalidSchemaError(AppError):
    code = "invalid_schema"
    status = 422
    message = "Provided JSON Schema is invalid"
    category = "content"


class UnknownPresetError(AppError):
    code = "unknown_preset"
    status = 422
    message = "Unknown schema preset"
    category = "content"


class EmptyInputError(AppError):
    code = "empty_input"
    status = 400
    message = "Input text is empty"
    category = "client"


class InputTooLargeError(AppError):
    code = "input_too_large"
    status = 413
    message = "Input text exceeds the size limit"
    category = "client"


class InvalidRequestError(AppError):
    """Некорректное тело/параметры запроса (расширение формата, см. README)."""

    code = "invalid_request"
    status = 422
    message = "Request is malformed"
    category = "client"


class NotFoundError(AppError):
    code = "not_found"
    status = 404
    message = "Resource not found"
    category = "client"


# ------------------------------------------------------------------- биллинг


class InsufficientCreditsError(AppError):
    code = "insufficient_credits"
    status = 402
    message = "Insufficient credits for this request"
    category = "billing"


class CostLimitExceededError(AppError):
    code = "cost_limit_exceeded"
    status = 402
    message = "Request cost exceeds the allowed cost limit"
    category = "billing"


class IdempotencyConflictError(AppError):
    code = "idempotency_conflict"
    status = 409
    message = "Idempotency-Key was already used with a different request"
    category = "client"


class HoldExpiredError(AppError):
    """Холд истёк (reaper освободил баланс) до завершения запроса."""

    code = "hold_expired"
    status = 409
    message = "Credit hold expired before the request finished; retry the request"
    category = "billing"


class PaymentFailedError(AppError):
    code = "payment_failed"
    status = 402
    message = "Payment failed"
    category = "billing"


class PaymentProviderDisabledError(AppError):
    """Метод оплаты отключён в 3.0 (единственный метод — Telegram Stars)."""

    code = "payment_provider_disabled"
    status = 410
    message = "This payment provider is disabled; the only available method is Telegram Stars"
    category = "security"


class PackageInactiveError(AppError):
    code = "package_inactive"
    status = 409
    message = "Credit package is not active"
    category = "billing"


class PackageNotFoundError(AppError):
    code = "package_not_found"
    status = 404
    message = "Credit package not found"
    category = "client"


class PurchaseLimitExceededError(AppError):
    code = "purchase_limit_exceeded"
    status = 429
    message = "Purchase limit exceeded"
    category = "limits"


class TelegramPaymentDuplicateError(AppError):
    code = "telegram_payment_duplicate"
    status = 409
    message = "Telegram payment was already processed"
    category = "billing"


class RefundShortfallError(AppError):
    code = "refund_shortfall"
    status = 409
    message = "Refund reduced the balance to zero; shortfall recorded"
    category = "billing"


class CustomerNotFoundError(AppError):
    code = "customer_not_found"
    status = 404
    message = "Customer not found"
    category = "client"


class RotationBlockedByActiveHoldsError(AppError):
    """3.1: ротация ключа заблокирована активными холдами."""

    code = "rotation_blocked_by_active_holds"
    status = 409
    message = "Key rotation is blocked by active credit holds; try again later"
    category = "billing"


class PaymentMismatchError(AppError):
    """3.1: successful_payment не совпал с сохранённым платежом."""

    code = "payment_mismatch"
    status = 409
    message = "Payment payload does not match the stored invoice"
    category = "billing"


class PayloadTooLongError(AppError):
    """3.1: payload инвойса превышает лимит Telegram (128 байт)."""

    code = "payload_too_long"
    status = 422
    message = "Invoice payload exceeds the Telegram limit"
    category = "client"


class PaymentDuplicateError(AppError):
    code = "payment_duplicate"
    status = 409
    message = "Payment already processed"
    category = "billing"


# --------------------------------------------------------------------- лимиты


class RateLimitExceededError(AppError):
    code = "rate_limit_exceeded"
    status = 429
    message = "Rate limit exceeded"
    category = "limits"


class DailySpendLimitExceededError(AppError):
    code = "daily_spend_limit_exceeded"
    status = 429
    message = "Daily spend limit exceeded"
    category = "limits"


class MonthlySpendLimitExceededError(AppError):
    code = "monthly_spend_limit_exceeded"
    status = 429
    message = "Monthly spend limit exceeded"
    category = "limits"


class ConcurrencyLimitExceededError(AppError):
    code = "concurrency_limit_exceeded"
    status = 429
    message = "Too many concurrent transformations for this API key"
    category = "limits"


# --------------------------------------------------------------------- контент


class UnsupportedFileError(AppError):
    code = "unsupported_file"
    status = 415
    message = "Unsupported file type"
    category = "content"


class InvalidFileContentError(AppError):
    code = "invalid_file_content"
    status = 415
    message = "File content does not match its type or is corrupted"
    category = "content"


class FileTooLargeError(AppError):
    code = "file_too_large"
    status = 413
    message = "File exceeds the size limit"
    category = "client"


class FileLimitsExceededError(AppError):
    code = "file_limits_exceeded"
    status = 413
    message = "File exceeds structural limits (pages, rows, depth)"
    category = "content"


class ValidationFailedError(AppError):
    code = "validation_failed"
    status = 422
    message = "Could not produce JSON matching the schema"
    category = "content"


class OCRUnavailableError(AppError):
    code = "ocr_unavailable"
    status = 503
    message = "OCR is not available"
    category = "provider"


class PDFOCRUnavailableError(AppError):
    code = "pdf_ocr_unavailable"
    status = 503
    message = "PDF scan OCR is not available in this deployment"
    category = "provider"


class STTUnavailableError(AppError):
    code = "stt_unavailable"
    status = 503
    message = "Speech-to-text is not available"
    category = "provider"


class MalwareScannerUnavailableError(AppError):
    code = "malware_scanner_unavailable"
    status = 503
    message = "Malware scanner is enabled but unreachable"
    category = "provider"


# -------------------------------------------------------------------- провайдер


class LLMNotConfiguredError(AppError):
    code = "llm_unavailable"
    status = 503
    message = "LLM provider is not configured"
    category = "provider"


class LLMProviderError(AppError):
    code = "llm_unavailable"
    status = 502
    message = "LLM provider request failed"
    category = "provider"

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message, details=details, headers=headers)
        self.provider_status_code = status_code


class LLMTimeoutError(AppError):
    code = "llm_timeout"
    status = 504
    message = "LLM provider timed out"
    category = "provider"


class WebhookDeliveryFailedError(AppError):
    code = "webhook_delivery_failed"
    status = 502
    message = "Webhook delivery failed"
    category = "provider"


# ------------------------------------------------------------------ внутренние


class InternalError(AppError):
    code = "internal_error"
    status = 500
    message = "Internal server error"
    category = "internal"


class JobNotFoundError(AppError):
    code = "job_not_found"
    status = 404
    message = "Job not found"
    category = "client"


class JobCanceledError(AppError):
    code = "job_canceled"
    status = 409
    message = "Job was canceled"
    category = "client"


class SessionExpiredError(AppError):
    code = "session_expired"
    status = 401
    message = "Session expired or invalid"
    category = "security"


class CsrfFailedError(AppError):
    code = "csrf_failed"
    status = 403
    message = "CSRF token is missing or invalid"
    category = "security"


ERROR_CODE_BY_CATEGORY = "client/provider/internal/content/limits/security/billing"
