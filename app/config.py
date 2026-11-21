"""Настройки приложения на pydantic-settings.

Все секреты читаются только из переменных окружения (или .env файла).
Значения по умолчанию безопасны для локальной разработки;
в production обязательные секреты проверяются при старте (validate_runtime).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

NormalizationMode = Literal["off", "basic", "strict"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Окружение ---
    env: Literal["local", "staging", "production"] = "local"
    log_level: str = "INFO"
    log_format: Literal["text", "json"] = "text"

    # --- Секреты ---
    secret_key: str = ""
    admin_api_key: str = ""

    # --- База данных ---
    database_url: str = "sqlite+aiosqlite:///./data/perfect_json.db"

    # --- Redis (опционально: распределённые лимиты) ---
    redis_url: str = ""

    # --- LLM ---
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = 60.0
    llm_max_output_tokens: int = 4096
    llm_temperature: float = 0.1
    # Одна автоматическая повторная попытка при таймауте/5xx/429 (задержка до повтора)
    llm_retry_enabled: bool = True
    llm_retry_delay_seconds: float = 0.5

    # --- Fallback-модель (опционально) ---
    llm_fallback_base_url: str = ""
    llm_fallback_api_key: str = ""
    llm_fallback_model: str = ""

    # --- Лимиты и автопочинка ---
    max_input_chars: int = Field(default=50_000, ge=100)
    max_file_mb: int = Field(default=20, ge=1)
    default_max_repairs: int = Field(default=2, ge=0, le=5)

    # --- Тарификация ---
    credit_base_cost: int = Field(default=1, ge=0)
    credit_cost_per_1k_input_chars: float = Field(default=0.5, ge=0.0)
    credit_cost_per_1k_output_tokens: float = Field(default=2.0, ge=0.0)

    # --- Биллинговый контур (P0) ---
    holds_enabled: bool = True
    max_cost_default_credits: int = Field(default=120, ge=1)
    idempotency_enabled: bool = True
    admin_require_idempotency: bool = False
    balance_low_threshold: int = Field(default=10, ge=0)

    # --- Холды 3.0: TTL, heartbeat, reaper ---
    holds_ttl_seconds: int = Field(default=900, ge=30)
    hold_heartbeat_interval_seconds: int = Field(default=120, ge=5)
    run_hold_reaper: bool = True
    hold_reaper_interval_seconds: int = Field(default=60, ge=5)

    # --- Production-строгость лимитов (3.0) ---
    web_concurrency: int = Field(default=1, ge=1)
    allow_in_memory_rate_limit_in_prod: bool = False

    # --- Лимиты по умолчанию (0 = без лимита) ---
    rate_limit_default_rpm: int = Field(default=60, ge=0)
    rate_limit_default_rpd: int = Field(default=5_000, ge=0)
    default_concurrency_limit: int = Field(default=5, ge=0)
    default_daily_spend_limit: int = Field(default=0, ge=0)
    default_monthly_spend_limit: int = Field(default=0, ge=0)
    default_daily_input_chars_limit: int = Field(default=0, ge=0)

    # --- Наблюдаемость ---
    enable_metrics: bool = False
    metrics_key: str = ""
    request_id_header: str = "X-Request-ID"

    # --- OCR / STT ---
    ocr_enabled: bool = False
    ocr_lang: str = "eng+rus"
    stt_enabled: bool = False
    stt_provider: str = "openai"
    stt_api_key: str = ""
    stt_model: str = "whisper-1"

    # --- Качество: нормализация, промпты, eval ---
    normalization_default: NormalizationMode = "off"
    prompt_version: str = ""
    eval_datasets_dir: str = "app/eval/datasets"
    eval_reports_dir: str = "eval/reports"

    # --- Безопасность файлов (P0) ---
    max_pdf_pages: int = Field(default=200, ge=1)
    max_docx_paragraphs: int = Field(default=5_000, ge=1)
    max_csv_rows: int = Field(default=10_000, ge=1)
    max_json_depth: int = Field(default=32, ge=1)
    max_json_keys: int = Field(default=50_000, ge=1)
    max_extracted_chars: int = Field(default=200_000, ge=1_000)
    pdf_ocr_enabled: bool = False
    malware_scan_enabled: bool = False
    clamav_url: str = ""

    # --- Telegram ---
    telegram_bot_token: str = ""
    telegram_admin_ids: str = ""
    telegram_daily_limit: int = Field(default=10, ge=0)
    telegram_bot_start_credits: int = Field(default=1000, ge=0)
    telegram_public_mode: bool = False
    telegram_allowed_chat_ids: str = ""
    telegram_rate_limit_per_minute: int = Field(default=20, ge=1)
    telegram_rate_limit_per_day: int = Field(default=0, ge=0)
    # 3.1: трансформации в боте выключены по умолчанию (деньги только API-ключом)
    telegram_demo_transform_enabled: bool = False
    enable_dangerous_public_demo_transform: bool = False
    allow_internal_demo_credits: bool = False

    # --- CORS ---
    cors_origins: str = ""

    # --- P1: события и уведомления ---
    alert_balance_threshold: int = Field(default=10, ge=0)
    alert_admin_telegram_ids: str = ""
    smtp_enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    alert_email_from: str = ""

    # --- P1: асинхронные задачи и вебхуки ---
    jobs_enabled: bool = False
    job_input_retention_hours: int = Field(default=24, ge=0)
    batch_inline_limit: int = Field(default=5, ge=1)
    batch_max_items: int = Field(default=100, ge=1)
    webhooks_enabled: bool = False
    webhook_max_retries: int = Field(default=5, ge=0)
    webhook_timeout_seconds: float = 10.0

    # --- P1: self-serve и платежи ---
    self_serve_enabled: bool = False
    payments_enabled: bool = False
    telegram_stars_enabled: bool = False
    telegram_stars_max_purchases_per_day: int = Field(default=10, ge=1)
    telegram_stars_max_invoices_per_hour: int = Field(default=5, ge=1)
    # 3.1: подтверждение, что цены пакетов не плейсхолдеры (production-гард)
    telegram_stars_prices_confirmed: bool = False
    # 3.1: пороги зависших платежей и TTL неоплаченных инвойсов
    payment_processing_alert_minutes: int = Field(default=15, ge=1)
    payment_received_alert_minutes: int = Field(default=30, ge=1)
    payment_created_ttl_hours: int = Field(default=24, ge=1)
    telegram_stars_usd_per_star: float = Field(default=0.0, ge=0.0)
    telegram_stars_packages: str = "starter:500:100,pro:2000:350,growth:6000:900"
    credit_price_usd: float = Field(default=0.01, ge=0.0)

    # --- P1: экономическая аналитика ---
    provider_cost_per_1k_input_tokens: float = Field(default=0.0, ge=0.0)
    provider_cost_per_1k_output_tokens: float = Field(default=0.0, ge=0.0)
    chars_per_token_estimate: int = Field(default=4, ge=1)

    # --- Миграции и бэкапы ---
    alembic_ini_path: str = "alembic.ini"
    backup_dir: str = "backups"

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = value.upper()
        if upper not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}")
        return upper

    # ------------------------------------------------------------------ derived

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def telegram_admin_id_list(self) -> list[int]:
        return _parse_id_list(self.telegram_admin_ids)

    @property
    def telegram_allowed_chat_id_list(self) -> list[int]:
        return _parse_id_list(self.telegram_allowed_chat_ids)

    @property
    def alert_admin_telegram_id_list(self) -> list[int]:
        return _parse_id_list(self.alert_admin_telegram_ids)

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_api_key.strip())

    @property
    def fallback_configured(self) -> bool:
        return bool(self.llm_fallback_api_key.strip() and self.llm_fallback_model.strip())

    @property
    def admin_configured(self) -> bool:
        return bool(self.admin_api_key.strip())

    # ------------------------------------------------------------- validation

    def validate_runtime(self) -> list[str]:
        """Проверки при старте. production + невыполнимые условия → SystemExit."""
        problems: list[str] = []
        if self.env == "production":
            if not self.secret_key.strip():
                problems.append("SECRET_KEY must be set in production")
            if not self.admin_api_key.strip():
                problems.append("ADMIN_API_KEY must be set in production")
            if self.database_url.startswith("sqlite"):
                problems.append(
                    "SQLite is not allowed in production: set DATABASE_URL to PostgreSQL"
                )
            if (
                self.web_concurrency > 1
                and not self.redis_url.strip()
                and not self.allow_in_memory_rate_limit_in_prod
            ):
                problems.append(
                    "Refusing to start: WEB_CONCURRENCY>1 without REDIS_URL would make "
                    "rate limits per-process; set REDIS_URL or "
                    "ALLOW_IN_MEMORY_RATE_LIMIT_IN_PROD=true to accept the risk"
                )
            if (
                self.telegram_public_mode
                and self.telegram_demo_transform_enabled
                and not self.enable_dangerous_public_demo_transform
            ):
                problems.append(
                    "Refusing to start: TELEGRAM_PUBLIC_MODE=true with "
                    "TELEGRAM_DEMO_TRANSFORM_ENABLED=true would spend money on public "
                    "users; set ENABLE_DANGEROUS_PUBLIC_DEMO_TRANSFORM=true to accept"
                )
            if (
                self.telegram_public_mode
                and self.telegram_bot_start_credits > 0
                and not self.allow_internal_demo_credits
            ):
                problems.append(
                    "Refusing to start: TELEGRAM_PUBLIC_MODE=true with "
                    "TELEGRAM_BOT_START_CREDITS>0 gives public users free credits; "
                    "set TELEGRAM_BOT_START_CREDITS=0 or ALLOW_INTERNAL_DEMO_CREDITS=true"
                )
            if (
                self.payments_enabled
                and self.telegram_stars_enabled
                and not self.telegram_stars_prices_confirmed
            ):
                problems.append(
                    "Refusing to start: Подтвердите, что цены пакетов не "
                    "плейсхолдерные (TELEGRAM_STARS_PRICES_CONFIRMED=true)"
                )
            if self.jobs_enabled:
                problems.append(
                    "WARNING: JOBS_ENABLED=true: задачи временно хранят входные данные "
                    "(JOB_INPUT_RETENTION_HOURS) — проверьте политику хранения"
                )
            if self.webhooks_enabled:
                problems.append(
                    "WARNING: WEBHOOKS_ENABLED=true: проверьте исходящие вебхуки "
                    "(SSRF: только https, приватные диапазоны запрещены на стороне приёмника)"
                )
            if self.ocr_enabled or self.stt_enabled:
                problems.append(
                    "WARNING: OCR/STT включены — оцените стоимость и нагрузку"
                )
            if not self.redis_url.strip():
                problems.append(
                    "WARNING: REDIS_URL is not set; rate limits will be per-process in-memory"
                )
            if self.telegram_public_mode:
                problems.append(
                    "WARNING: TELEGRAM_PUBLIC_MODE=true in production: public demo bot is exposed"
                )
        return problems


def _parse_id_list(raw: str) -> list[int]:
    result: list[int] = []
    for part in raw.replace(" ", "").split(","):
        if not part:
            continue
        try:
            result.append(int(part))
        except ValueError:
            continue
    return result


@lru_cache
def get_settings() -> Settings:
    """Синглтон настроек (кэшируется; в тестах подменяется напрямую)."""
    return Settings()
