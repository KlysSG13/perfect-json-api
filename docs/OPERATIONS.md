# Эксплуатация (Operations)

## Деплой

### Локально
```bash
make install-dev
cp .env.example .env            # заполнить секреты
make migrate                    # alembic upgrade head
make run-api                    # uvicorn --reload
make run-bot                    # отдельный терминал
```

### Docker (production-профиль)
```bash
cp .env.example .env
# ENV=production, DATABASE_URL=postgresql+asyncpg://..., SECRET_KEY, ADMIN_API_KEY, LLM_API_KEY
make docker-up                  # postgres + api + bot
docker compose exec api alembic upgrade head
```

### Docker (dev-профиль: + Redis, метрики, задачи)
```bash
make docker-dev-up
curl -s http://127.0.0.1:8000/metrics -H "X-Metrics-Key: dev-metrics-key" | head
```

Обратный прокси/TLS/ограничение /docs и /metrics — docs/nginx-example.conf.

### Проверки при старте (автоматически)
- `ENV=production` + нет `SECRET_KEY`/`ADMIN_API_KEY` → отказ запуска.
- `ENV=production` + SQLite → отказ запуска.
- `ENV=production` + нет `REDIS_URL` → WARNING (лимиты per-process).
- `TELEGRAM_PUBLIC_MODE=true` в production → WARNING о публичном демо.

## Мониторинг
- `GET /health` — статус + компоненты (database/redis/llm/jobs/webhooks/payments).
- `GET /metrics` (Prometheus, `ENABLE_METRICS=true`, защита `METRICS_KEY`):
  запросы, ошибки по кодам/категориям, вызовы LLM, автопочинки, таймауты,
  провалы валидации, кредиты, активные холды, отказы по лимитам/кредитам,
  ошибки извлечения файлов, длительности, входные объёмы, здоровье
  БД/Redis/LLM.
- Логи `LOG_FORMAT=json`: `ts`, `level`, `logger`, `message`, `request_id`,
  `api_key_prefix`. Корреляция: `X-Request-ID` принимается на вход и
  возвращается в ответе.

## Алерты (настроить в Prometheus/Alertmanager)
| Сигнал | Порог | Действие |
|---|---|---|
| `perfect_json_up` | != 1 | страница дежурному |
| `rate(perfect_json_errors_total{category="provider"}[5m])` | > 0.5/s | проверить LLM-провайдера, fallback |
| `perfect_json_rate_limited_total` рост | > 10× нормы | проверить клиента/атаку |
| `perfect_json_insufficient_credits_total` рост | — | напоминание о пополнении (события balance.low) |
| `perfect_json_db_healthy` | 0 | база недоступна |
| `perfect_json_holds_active` | растёт и не падает | «зависшие» холды — см. инциденты |

События (см. docs/JOBS.md, docs/WEBHOOKS.md): `balance.low`, `balance.zero`,
`usage_over_hold`, `payment.*`, `job.*` — идут в лог, админ-Telegram
(`ALERT_ADMIN_TELEGRAM_IDS`), SMTP (`SMTP_ENABLED`), вебхуки клиентов.

## Бэкапы
- Ежедневно cron: `BACKUP_DIR=backups ./scripts/backup_postgres.sh`
  (ротация `BACKUP_KEEP_COUNT=14`).
- Восстановление: docs/restore_instructions.md (scripts/) — шаги, проверка
  инварианта «баланс = сумма транзакций».
- Рекомендация: RPO ≤ 24 ч (при росте платежей — ежечасно).

## Инциденты

### Утечка API-ключа
1. Отозвать: `POST /admin/api-keys/{id}/credits` не трогать; деактивировать
   ключ (`is_active=false` через БД/CLI) — 403 для всех запросов.
2. Проверить журнал: `GET /admin/usage?key_id=...` — объёмы за период.
3. Выпустить новый ключ клиенту, начислить компенсацию `credits_delta` с
   `reason="security incident"` (аудит запишется).

### Отказ LLM-провайдера
- Автоматически: 1 ретрай + fallback-модель (`LLM_FALLBACK_*`), если
  настроена; иначе 502/504 `llm_unavailable`, кредиты не списываются,
  холды освобождаются.
- Ручной контур: переключить `LLM_BASE_URL/LLM_MODEL` на резервного
  провайдера и перезапустить api+bot.

### Превышение лимитов клиентом
- 429 с кодами `rate_limit_exceeded`/`daily_spend_limit_exceeded` —
  ожидаемое поведение; при злоупотреблении снизить пер-ключевые
  `rate_limit_rpm`/`daily_spend_limit_credits` или деактивировать ключ.

### Подозрительная активность
- `GET /admin/usage?success=false` по ключу, `admin_audit_logs`, логи с
  `api_key_prefix`. Массовые `unsupported_file`/`invalid_file_content` —
  сканирование; rate limits уже режут объём. Эскалация — по
  docs/ACCEPTABLE_USE.md.

### «Зависшие» холды (авто-экспирация 3.0)
- Каждый холд создаётся с TTL (`HOLDS_TTL_SECONDS=900`) и `last_heartbeat_at`;
  живой запрос продлевает его heartbeat-ом
  (`HOLD_HEARTBEAT_INTERVAL_SECONDS=120`), пока реально выполняется.
- Фоновый **reaper** (`RUN_HOLD_REAPER=true`, интервал
  `HOLD_REAPER_INTERVAL_SECONDS=60`) идемпотентным атомарным UPDATE
  закрывает истёкшие холды (`status=expired`), пишет release-транзакцию,
  метрику `perfect_json_holds_expired_total` и событие `hold_expired`.
  Безопасен при нескольких воркерах (UPDATE-идемпотентность; при
  `REDIS_URL` дополнительно берёт лок на цикл).
- Если холд истёк, а запрос ещё жив и не шлёт heartbeat — он получает
  409 `hold_expired`, кредиты НЕ списываются (запись usage с
  `error_code=hold_expired, cost=0`), при частичных вызовах LLM — алерт
  `hold_expired_after_partial_usage`.
- Диагностика: `SELECT * FROM credit_holds WHERE status IN ('active') AND
  expires_at < now();` — должен быть пуст (reaper уже прошёл);
  рост `perfect_json_holds_active` без нагрузки — включите/участите reaper.
- Ручной прогон при необходимости: `app.services.billing.BillingService.expire_stale_holds()`
  (используется тестами; тот же код, что и в фоне).

### Зависшие платежи (Telegram Stars)
- Платёж в `received/processing` с `problem=true` — событие
  `payment.alert` (reason=payment.credit_failed / payment.package_problem).
- Сверка и доначисление: `python -m app.cli payments-list --status received`
  → `reconcile-payment --charge-id ...` (идемпотентно); возврат —
  `refund-payment --charge-id ...` (см. docs/PAYMENTS.md).
- Метрики: `perfect_json_payments_stuck`,
  `perfect_json_telegram_stars_payments_failed_total`.
