# Perfect JSON API — «Идеальный JSON из любого хаоса»

Production-ready B2B SaaS: API-first сервис, который принимает сырой текст, файлы и извлечённый контент и возвращает **строго валидный JSON по JSON Schema Draft 2020-12** — с нормализацией, валидацией и автопочинкой через повторные запросы к LLM.

Версия **3.1.0 «release stabilization»**: трансформации в боте по умолчанию **отключены** (публичный бот не тратит внутренний баланс), **ротация ключа сохраняет баланс** и блокируется активными холдами, оплата Telegram Stars зачисляется по **снапшоту инвойса** (смена цены/деактивация пакета не ломает оплату; несоответствия → `problem` без зачисления), stale-платежи алертятся, неоплаченные инвойсы чистятся CLI, production-гварды конфигурации (подтверждение цен Stars, запрет опасного демо), coverage + интеграционные тесты Postgres/Redis в CI, релизные чеклисты и support-runbook.

- **Стек:** Python 3.12+, FastAPI, Pydantic v2, SQLAlchemy 2 (async), SQLite/PostgreSQL(+asyncpg), Redis (опц.), aiogram 3, любой OpenAI-совместимый LLM, Alembic.
- **Монетизация:** usage-based кредиты с холдами; админ-API/CLI для управления; **платежи только Telegram Stars** (единственный метод; Stripe и Fake-checkout отключены и возвращают 410).
- **Telegram-бот:** self-serve с личными ключами клиентов и оплатой через Telegram Stars; трансформации в боте по умолчанию отключены (`TELEGRAM_DEMO_TRANSFORM_ENABLED=false`) — публичный бот не тратит внутренний баланс.

Проверено (Python 3.14.7, macOS): **271 офлайн-тест — все зелёные** (`pytest -q` → `271 passed`, без Telegram/Stripe/LLM/Redis/Postgres; интеграционные deselect по маркеру), `ruff`/`pyflakes`/`compileall` — чисто, `make coverage` собирает отчёт, миграции применяются к чистой базе и к базам 2.0/3.0 с данными (тесты), eval fake — 60 кейсов, schema_pass 1.0. Живые проверки (Stars-оплата, `make verify-docker`, Postgres/Redis-интеграция, live LLM) не выполнялись автоматически — есть `make verify-docker`, `INTEGRATION_POSTGRES/REDIS=1` и чеклисты ([RELEASE_CHECKLIST](docs/RELEASE_CHECKLIST.md), [Stars](docs/TELEGRAM_STARS_MANUAL_CHECKLIST.md)).

**Статус: код готов к продакшену, живой инстанс не развёрнут.** Сервер под сервис я не арендую — это постоянные расходы, а бюджета на них сейчас нет. Всё для подъёма в репозитории (`docker compose`, миграции, бэкап-скрипт, [docs/OPERATIONS.md](docs/OPERATIONS.md)): на чистом VPS сервис поднимается за ~15–30 минут — как только появится для этого причина (клиент, заказ).

---

## 1. Что это и ключевая ценность

Разработчики AI-агентов, интеграторы n8n/Make и автоматизаторы постоянно превращают хаос (переписку, скан счёта, выгрузку CRM) в строгий JSON. Perfect JSON API делает это инфраструктурной функцией:

```
хаос (текст / txt / md / json / csv / pdf / docx / картинка / аудио)
   → извлечение → нормализация (off/basic/strict) → LLM (ретрай + fallback)
   → извлечение JSON (markdown/BOM/скобки) → валидация Draft 2020-12
   → АВТОПОЧИНКА с перечнем ошибок → валидный JSON + мета
```

Гарантии: ответ либо проходит схему, либо клиент получает понятную ошибку; «почти JSON» не бывает. Сырые данные не сохраняются. Стоимость известна до вызова (холд), списания всегда отражены в транзакциях.

## 2. Что изменилось в 3.1 (кратко)

- **Публичный бот без бесплатных трансформаций**: `TELEGRAM_DEMO_TRANSFORM_ENABLED=false`
  по умолчанию — бот подсказывает REST API, файлы не обрабатывает, внутренний
  ключ не расходуется; опасные комбинации (public+demo, public+стартовые
  кредиты) не стартуют в production без явных аварийных флагов; группы
  получают отказ на трансформации/покупки.
- **Ротация без потери денег**: `/rotate` и `rotate-customer-key` переносят
  баланс, лимиты и привязку клиента; активные холды блокируют ротацию
  (`rotation_blocked_by_active_holds`); админ-перенос `transfer-balance`
  идемпотентен; всё пишется в аудит и кредитный журнал.
- **Stars по снапшоту**: инвойс фиксирует credits/stars; успешная оплата
  сверяется с инвойсом (сумма/валюта/пользователь/пакет/pid) — при
  несоответствии `problem` без зачисления; смена цены/деактивация пакета
  после инвойса не ломает оплату; payload ≤128 байт без секретов.
- **Гигиена платежей**: stale-детект `processing/received` (алерт
  `payment.stale` + метрика), идемпотентная очистка неоплаченных `created`
  (`cleanup-stale-payments`), они же не расходуют дневной лимит покупок.
- **Изоляция**: `/v1/payments` показывает платежи всех ключей клиента —
  после ротации история не теряется; чужой ключ чужого не видит.
- **Эксплуатация**: coverage (`make coverage`, CI-артефакт), интеграционные
  тесты с Postgres/Redis (`INTEGRATION_POSTGRES=1`/`INTEGRATION_REDIS=1`,
  отдельная CI-джоба с сервисами), `verify_docker.sh` сеет пакеты и
  проверяет 410, restore-инварианты (баланс/холды/дубли платежей),
  `TELEGRAM_STARS_PRICES_CONFIRMED` против плейсхолдерных цен.
- Документы: [RELEASE_CHECKLIST.md](docs/RELEASE_CHECKLIST.md) (go/no-go),
  [SUPPORT_RUNBOOK.md](docs/SUPPORT_RUNBOOK.md) (11 сценариев дежурного).

## 2c. История: 3.0

- **Холды не зависают**: TTL + heartbeat живого запроса + фоновый reaper
  (идемпотентный UPDATE, безопасен при многих воркерах); settle поверх
  истёкшего холда невозможен — 409 `hold_expired` без списаний.
- **Платежи = Telegram Stars** (единственный метод): пакеты в Stars,
  инвойс XTR из бота, идемпотентный `successful_payment` (уникальный
  `telegram_payment_charge_id`), возвраты CLI `refund-payment` (один раз,
  `refund_shortfall` при нехватке), сверка `reconcile-payment`,
  `GET /admin/payments`, лимиты покупок и антифрод.
- **Self-serve в боте**: клиент+ключ на `/start`, баланс с учётом холдов,
  префикс на `/key`, ротация на `/rotate` (старый отзывается, аудит),
  покупка на `/buy` (только личные чаты).
- **Инварианты усилены**: CHECK `credits_balance >= 0` в БД, индекс
  истекающих холдов, платежи с `credited_at/refunded_at/problem`;
  тесты: 20 параллельных, параллельные холды/списания, двойной settle,
  сбой между холдом и ответом.
- **Production-строгость**: `WEB_CONCURRENCY>1` без `REDIS_URL` → отказ
  запуска (если не принят риск флагом); метрики лимитов по кодам.
- **Инфра**: CI (линтеры, тесты+cov, миграции up/down, docker build,
  мягкий pip-audit), `scripts/verify_docker.sh`, `.env.example` без Stripe.
- **Eval**: 60 кейсов (нормализация дат/денег/телефонов, инъекции,
  противоречия, пропуски), метрика `numeric_accuracy_rate`.
- Полный список: [docs/CHANGELOG.md](docs/CHANGELOG.md) (3.0.0).

## 2d. История: 2.0

- **Биллинг**: `credit_transactions`/`credit_holds`/`admin_audit_logs`; изменение баланса — только атомарным UPDATE + транзакция; холды на консервативный `max_cost`; `options.max_cost_credits`; дневные/месячные лимиты трат; идемпотентность `Idempotency-Key` (transform — опционально, админ-кредиты — по флагу `ADMIN_REQUIRE_IDEMPOTENCY`).
- **Лимиты**: rpm/rpd на ключ (пер-ключевые переопределения), лимит конкурентности, Telegram public mode/allow-list/антфлуд.
- **Наблюдаемость**: `/metrics` (Prometheus, вкл. флагом, защита ключом), `LOG_FORMAT=json`, `X-Request-ID`, компоненты в `/health`.
- **Качество**: версии промптов (`meta.prompt_version`, анти-инъекция v2), нормализация дат/валют/сумм/телефонов с `meta.normalization`, provenance, eval с 25 кейсами и метриками.
- **Файлы**: magic bytes, структурные лимиты (страницы/строки/глубина), опц. OCR PDF-сканов и антивирус (ClamAV REST).
- **LLM**: 1 ретрай на таймаут/429/5xx + fallback-модель (`meta.fallback_used`); сбои провайдера — без списаний.
- **P1 под флагами**: `JOBS_ENABLED`, `WEBHOOKS_ENABLED`, `PAYMENTS_ENABLED`; события и алерты; `GET /admin/economics`.
- Behavior changes зафиксированы в [docs/CHANGELOG.md](docs/CHANGELOG.md).

## 3. Архитектура

```
perfect-json/
├── app/
│   ├── config.py            # pydantic-settings: ~90 переменных окружения
│   ├── db.py                # async-движок (SQLite WAL/StaticPool, PostgreSQL)
│   ├── models.py            # 18 таблиц: ядро + биллинг + аудит + P1
│   ├── schemas.py           # Pydantic-модели API
│   ├── security.py          # генерация ключей, SHA-256, compare_digest
│   ├── exceptions.py        # коды/категории/HTTP-статусы ошибок
│   ├── llm.py               # OpenAI-совместимый клиент + ретрай + fallback
│   ├── extractors.py        # экстракторы: сигнатуры, лимиты, OCR/STT/антивирус
│   ├── json_utils.py        # extract_json, безопасность схем
│   ├── normalization.py     # off/basic/strict с мета-заметками
│   ├── provenance.py        # ссылки на источник (эвристика)
│   ├── prompts.py           # версии промптов (v2 анти-инъекция)
│   ├── presets.py           # 5 встроенных JSON Schema
│   ├── pricing.py           # estimate/max_cost/фактическая стоимость
│   ├── metrics.py           # мини-реестр Prometheus (без зависимостей)
│   ├── rate_limit.py        # in-memory/Redis лимиты + ConcurrencyGuard
│   ├── logging_utils.py     # JSON-логи c request_id
│   ├── cli.py               # 20 команд (ключи, пакеты, платежи, поддержка)
│   ├── services/            # billing, transform, idempotency, jobs,
│   │                        # webhooks, payments, events, economics, ...
│   ├── api/                 # main (фабрика+ lifespan) + routes/*
│   ├── bot/                 # aiogram 3: доступ, антфлуд, дневной лимит
│   └── eval/                # датасеты + раннер качества
├── migrations/              # Alembic: 0001 baseline + 0002 commercial
├── tests/                   # 271 офлайн-тест (включая конкурентность биллинга)
├── scripts/                 # backup_postgres.sh, healthcheck.py, load_test.py
├── docs/                    # 13 документов + nginx-example.conf
├── Dockerfile, docker-compose.yml, docker-compose.dev.yml, Makefile
└── .env.example
```

## 4. Быстрый старт локально

```bash
cd perfect-json
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

cp .env.example .env
make migrate          # alembic upgrade head (SQLite-файл по умолчанию)
pytest -q             # 271 passed — без сети и внешних ключей

make run-api          # uvicorn app.api.main:app --reload → :8000/docs
python -m app.cli create-api-key --name demo --credits 100
```

## 5. Быстрый старт через Docker

```bash
cp .env.example .env
# обязательно: SECRET_KEY, ADMIN_API_KEY, LLM_API_KEY (+ токен бота при необходимости)
docker compose up --build          # postgres → api :8000 → bot
docker compose exec api alembic upgrade head

# dev-профиль (+ Redis, метрики, задачи):
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build -d
curl -s :8000/metrics -H "X-Metrics-Key: dev-metrics-key" | head
```

Обратный прокси/TLS/ограничение `/docs` и `/metrics` — [docs/nginx-example.conf](docs/nginx-example.conf).

## 6. Эндпоинты

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/health` | статус + компоненты (БД/Redis/LLM/флаги) |
| GET | `/metrics` | Prometheus (ENABLE_METRICS, X-Metrics-Key) |
| GET | `/v1/presets` | 5 встроенных схем |
| POST | `/v1/transform` | текст → JSON (Idempotency-Key поддерживается) |
| POST | `/v1/transform/file` | файл → JSON (multipart; schema/options как строки) |
| POST | `/v1/transform/batch` | батч: ≤5 inline (частичные результаты), больше → задача |
| POST | `/v1/jobs`, GET `/v1/jobs/{id}`, POST `/v1/jobs/{id}/cancel` | асинхронные задачи (JOBS_ENABLED) |
| GET/POST/DELETE | `/v1/webhooks[...]` | вебхуки клиента (появляются в OpenAPI только при WEBHOOKS_ENABLED) |
| GET | `/v1/payments/packages`, GET `/v1/payments` | пакеты Stars и платежи клиента (PAYMENTS_ENABLED + TELEGRAM_STARS_ENABLED; покупка — только в боте `/buy`) |
| POST | `/v1/payments/checkout`, POST `/v1/payments/fake/confirm`, POST `/webhooks/stripe` | **отключены, возвращают 410 `payment_provider_disabled`** (проверяется verify_docker и тестами) |
| GET | `/v1/me`, `/v1/usage` | баланс и журнал ключа |
| POST/GET | `/admin/api-keys[...]`, POST `/admin/api-keys/{id}/credits` | управление ключами (аудит, идемпотентность) |
| GET | `/admin/usage`, `/admin/audit-logs`, `/admin/economics`, `/admin/payments` | журнал, аудит, экономика, платежи Stars |

Формат ошибок единый: `{"error": {"code", "message", "details?}}`; коды — в [docs/CHANGELOG.md](docs/CHANGELOG.md) и OpenAPI (`/docs`).

**CLI (20 команд):** ключи и баланс — `create-api-key`, `list-api-keys`,
`add-credits`, `show-usage`; пакеты — `seed-packages`, `list-packages`,
`activate-package`, `deactivate-package`; платежи — `payments-list`,
`reconcile-payment`, `refund-payment`, `cleanup-stale-payments`; поддержка —
`freeze-key`, `list-customers`, `show-customer`, `rotate-customer-key`,
`transfer-balance`; сервис — `ensure-telegram-key`, `run-api`, `run-bot`.

## 7–16. Как это работает (подробно в docs/)

- **Холды и биллинг без гонок** — [docs/PRICING.md](docs/PRICING.md): pre-flight `max_cost`, атомарные UPDATE+транзакции, `usage_over_hold` с капом, идемпотентность.
- **Лимиты** — [docs/OPERATIONS.md](docs/OPERATIONS.md): rpm/rpd/concurrency/день-месяц, Telegram public mode; 429 с `Retry-After`/`X-RateLimit-*`.
- **Нормализация и provenance** — `options.normalization` (`off|basic|strict`), `meta.normalization` (raw/normalized/confidence/warning), `options.include_provenance`; низкая уверенность → исходное значение + предупреждение; нормализация откатывается, если ломает схему.
- **Автопочинка** — до `max_repairs` повторов со списком ошибок; остановка по лимиту стоимости (`cost_limit_exceeded`), уже выполненные вызовы тарифицируются.
- **Миграции** — `make migrate`, `make migrate-check`, `make revision message="..."`; baseline 0001 + 0002; в Docker `docker compose exec api alembic upgrade head`.
- **Метрики** — [docs/OPERATIONS.md](docs/OPERATIONS.md): список метрик и алертов.
- **Eval** — [docs/EVAL.md](docs/EVAL.md): `python -m app.eval.run --provider fake|live`.
- **Self-serve/платежи/вебхуки/задачи** — [docs/SELF_SERVE.md](docs/SELF_SERVE.md), [docs/PAYMENTS.md](docs/PAYMENTS.md), [docs/WEBHOOKS.md](docs/WEBHOOKS.md), [docs/JOBS.md](docs/JOBS.md).
- **Бэкапы/восстановление** — `scripts/backup_postgres.sh`, [scripts/restore_instructions.md](scripts/restore_instructions.md).
- **Экономика запросов** — `GET /admin/economics` + `PROVIDER_COST_PER_1K_*`, `CREDIT_PRICE_USD` (флаг `estimated`).

## Примеры (curl)

**Покупка кредитов (Telegram Stars, единственный метод):** в боте `/buy` →
инвойс XTR → `successful_payment`. REST-роуты покупки отключены (410
`payment_provider_disabled`); пакеты: `python -m app.cli seed-packages`.

**Создание ключа (админ):**
```bash
curl -X POST http://127.0.0.1:8000/admin/api-keys \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"name":"integration","credits":500,"rate_limit_rpm":120,"daily_spend_limit_credits":1000}'
```

**Текст по пресету с нормализацией и provenance:**
```bash
curl -s http://127.0.0.1:8000/v1/transform \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"raw_text":"Счёт от Романа на 1 500,50 руб, оплата до 15.05.2026",
       "schema_preset":"invoice_v1",
       "options":{"normalization":"strict","include_provenance":true,"max_cost_credits":10}}'
```
Мета ответа: `prompt_version`, `attempts`, `cost_credits`, `balance_after`,
`normalization.fields` (`$.total_amount`: "1 500,50"→1500.5, high;
`$.due_date`: "15.05.2026"→"2026-05-15", high), `provenance.fields`.

**Файл:**
```bash
curl -s http://127.0.0.1:8000/v1/transform/file -H "X-API-Key: $KEY" \
  -F "file=@invoice.pdf" -F "schema_preset=invoice_v1" -F 'options={"max_repairs":3}'
```

**Пользовательская схема** (`schema` имеет приоритет над пресетом):
```bash
curl -s http://127.0.0.1:8000/v1/transform -H "X-API-Key: $KEY" \
  -H "Content-Type: application/json" -d '{
    "raw_text":"Лена из Рома, нужен бот, бюджет 100к, до марта",
    "schema":{"type":"object","properties":{"name":{"type":"string"},"budget":{"type":["number","string","null"]}},"required":["name"]}}'
```

**Идемпотентный вызов:** добавьте заголовок `Idempotency-Key: order-42` —
повтор с тем же телом вернёт сохранённый ответ (`Idempotency-Replayed: true`)
без списаний; с другим телом — 409 `idempotency_conflict`.

## 17. Тестирование

Полностью офлайн: in-memory/file SQLite, `FakeLLMClient`, `FakeRedisClient`,
`FakePaymentProvider`, `FakeWebhookTransport`, `FakeMalwareScanner`,
`FakeSink`. Ни сети, ни LLM/Telegram/Stripe/Redis/SMTP/Postgres/ClamAV.

```bash
pytest -q          # 271 passed (offline; интеграционные сняты маркером)
make coverage      # отчёт покрытия (COVERAGE_FAIL_BELOW=N — опциональный порог)
make lint          # ruff + pyflakes + compileall
make eval-fake     # eval-отчёт по 60 кейсам (офлайн)
make verify-docker # сборка+подъём+миграции+пакеты+/health+410 (нужен Docker)
INTEGRATION_POSTGRES=1 INTEGRATION_REDIS=1 pytest tests/integration -q -m integration -o addopts=""
```

Покрытие кластеров: биллинг и холды (включая **10 параллельных запросов**
на файловой БД — без двойных трат и минуса), идемпотентность, лимиты и 429
с заголовками, метрики, нормализация, безопасность файлов, анти-инъекции,
устойчивость LLM (ретрай/fallback/без списаний), вебхуки, задачи и батчи,
платежи Telegram Stars (снапшот, несоответствия, идемпотентность, возвраты,
зависшие, изоляция по клиенту), экономика, события, миграции Alembic.
Интеграционные (Postgres/Redis) — отдельным маркером `integration`
(upgrade/downgrade на чистой базе).

Нагрузочный скрипт (вне pytest): `scripts/load_test.py`.

## 18. Типовые ошибки и устранение

| Симптом | Причина | Решение |
|---|---|---|
| 402 `insufficient_credits` с `max_cost` в details | холд больше доступного баланса (консервативный максимум) | пополнить или снизить `options.max_cost_credits`/кап |
| 402 `cost_limit_exceeded` | максимум запроса дороже капа (`MAX_COST_DEFAULT_CREDITS`, пер-ключевой) или починки упёрлись в лимит | поднять кап/лимит или упростить вход |
| 429 `rate_limit_exceeded`/`daily_spend_limit_exceeded` | лимиты ключа | заголовки `Retry-After`; пер-ключевые переопределения |
| 409 `idempotency_conflict` | тот же Idempotency-Key с другим телом / запрос ещё выполняется | использовать уникальные ключи на операцию |
| 415 `invalid_file_content` | содержимое не соответствует расширению/бинарник/битый PDF | проверить реальный тип файла |
| 413 `file_limits_exceeded` | страниц/строк/глубина больше лимита | поднять `MAX_*` или уменьшить файл |
| 503 `pdf_ocr_unavailable` | `PDF_OCR_ENABLED=true` без pypdfium2/tesseract | установить зависимости или выключить флаг |
| 503 `malware_scanner_unavailable` | сканер включён, но недоступен | поднять ClamAV-REST или выключить |
| 502/504 `llm_unavailable`/`llm_timeout` | провайдер недоступен после ретрая и fallback | проверить провайдера; настроить `LLM_FALLBACK_*` |
| Метки кредитов «не сходятся» | — | проверка инварианта в [scripts/restore_instructions.md](scripts/restore_instructions.md) |

## Принятые допущения (актуально для 3.1.0)

- Холды имеют авто-экспирацию: TTL + heartbeat живого запроса + фоновый
  reaper (docs/OPERATIONS.md).
- Self-serve и Telegram Stars **реализованы** (3.0/3.1): клиенты, личные
  ключи, покупка/возвраты/сверка (docs/SELF_SERVE.md, docs/PAYMENTS.md).
- Оплата — только Telegram Stars; прочие методы отключены (410).
- Трансформации в боте отключены по умолчанию (`TELEGRAM_DEMO_TRANSFORM_ENABLED`);
  включение в приватном режиме списывает кредиты строго с ЛИЧНОГО ключа
  клиента (внутренний ключ — только админ-фолбэк); в публичном режиме
  production включение не стартует без аварийного флага.
- STT использует `LLM_BASE_URL`; секрет вебхука хранится для подписи;
  один consumer задач; метрики бота живут в процессе бота.
- Цены пакетов Stars — редактируемые плейсхолдеры; production требует
  `TELEGRAM_STARS_PRICES_CONFIRMED=true`.
- Исторические допущения ранних версий — в [docs/CHANGELOG.md](docs/CHANGELOG.md).

## Безопасность и приватность

[docs/SECURITY.md](docs/SECURITY.md) • [docs/PRIVACY.md](docs/PRIVACY.md) •
[docs/TERMS.md](docs/TERMS.md) • [docs/ACCEPTABLE_USE.md](docs/ACCEPTABLE_USE.md) •
[docs/OPERATIONS.md](docs/OPERATIONS.md)

## Лицензия

MIT.
