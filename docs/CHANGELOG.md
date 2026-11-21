# Изменения

## 3.4.0 (2026-11-21) — финальный аудит перед первыми клиентами

### Fixed (найдено аудитом)
- **КРИТИЧНО: кнопки меню съедали дневной лимит** — «💰 Баланс» и др. не
  начинаются с "/" и считались трансформациями: 10 нажатий «Баланс»
  блокировали чат до полуночи UTC. Дневной лимит перенесён из мидлвари в
  хендлеры трансформаций (считаются только реальные запросы); мидлварь —
  только антифлуд/мин, кнопки меню exempt. Регрессионный тест добавлен.
- **Документы >17k символов получали 402 cost_limit_exceeded до LLM**:
  консервативный холд (4096 токенов × 3 попытки + вход) упирался в кап
  MAX_COST_DEFAULT_CREDITS=50 уже при ~17k симв. Кап поднят до 120
  (код + .env.example + прод): 50k симв → max_cost 103 < 120 — проходит.
- **Висящее состояние /schema**: кнопки меню/команды balance/key/buy/help
  теперь сбрасывают FSM-состояние (раньше после «💰 Баланс» следующий
  текст мог уйти в разбор схемы).
- **Спам в группах**: fallback-хендлер молчит в группах (раньше отвечал
  «Не понял команду» на стикеры/фото админа).

### Ops (прод)
- **Бэкапы включены**: scripts/backup_prod.sh (pg_dump в контейнере →
  gzip → /opt/perfect-json/backups, ротация 14 дней), cron 03:15 UTC,
  первый бэкап создан и проверен.

### Проверено
- 285 тестов зелёных, ruff/pyflakes чисто; на проде: нажатия «Баланс» не
  увеличивают daily_used, трансформация увеличивает, пакеты/цены целы.

## 3.3.1 (2026-11-21) — цены пакетов Stars

### Pricing
- Утверждены цены: starter 500 кр = ⭐99, pro 2000 кр = ⭐199,
  growth 6000 кр = ⭐899 (раньше плейсхолдеры ⭐100/350/900).
- Прод: `TELEGRAM_STARS_PACKAGES` в `.env`, `seed-packages`, пересоздание
  контейнеров (`up -d`, НЕ `restart` — env запекается при создании, а бот
  пересеивает пакеты из своего окружения при старте).

## 3.3.0 (2026-11-21) — навигация и понятная платежка

### UX
- **Главное меню** (reply-клавиатура под полем ввода): 💰 Баланс / ⭐ Пополнить /
  🔑 Мой ключ / ❓ Помощь — кнопки работают как алиасы команд (в личке).
- **`/balance` переписан**: личный баланс, «в обработке → доступно» при
  активных холдах, запросы сегодня X/Y, префикс ключа; для админа —
  компактная нота про внутренний ключ (без CLI-инструкций в чате).
- **Нехватка кредитов** теперь с inline-кнопкой «⭐ Пополнить» → сразу
  пакеты (callback `menu:buy`).
- **Файл-результат**: «📋 Результат большой (N симв.) — прикрепляю файлом
  result.json ⬇️ следующим сообщением» (раньше формулировка «отправляю
  файлом» вызывала вопросы, куда пришёл файл).
- Переписаны `/start` (3 шага + меню) и `/help` (кнопки, команды, пример).

### Тесты
- Меню/тексты/алиасы: 2 новых (всего 283 зелёных).

## 3.2.2 (2026-11-21) — UX: подтверждение приёма файла

### Added
- **Бот мгновенно отвечает на документ** («📥 Принял файл… это займёт до
  минуты, повторно отправлять не нужно») до скачивания — раньше до
  30–60 с тишины (скачивание + извлечение + LLM), и пользователи
  дублировали файлы. Добавлен текст `FILE_ACCEPTED` (+ `_human_size`).
- **Сбой скачивания файла больше не молчит**: download/extract обёрнут в
  try/except → понятное `FILE_DOWNLOAD_FAILED` вместо тишины после ack.
- Тест: форматирование размера и текста подтверждения. Всего 281 зелёный.

## 3.2.1 (2026-11-21) — hotfix: CLI add-redits без записи в журнал

### Fixed
- **CLI `add-credits` менял баланс без записи в `credit_transactions`**
  (нарушение инварианта «баланс == сумма транзакций»; вскрыто проверкой
  инварианта на проде после первой трансформации в боте). CLI переведён на
  `BillingService.adjust_balance` (type=admin_adjustment, actor=cli:admin);
  добавлен регрессионный тест `test_cli_add_credits_writes_ledger`.
- На прод-базе выполнен бэкфилл недостающей транзакции (+1000, key 4).

## 3.2.0 (2026-11-21) — бот обрабатывает текст/файлы за кредиты личного ключа

### Behavior changes
- **Личные чаты открыты всем в приватном режиме**: AccessControlMiddleware
  пропускает любые сообщения в ЛС (текст, файлы, команды) — деньги защищает
  биллинг, а не замок. Группы по-прежнему только админы/allow-list.
- **Внутренний ключ бота — фолбэк только для админов**
  (`_resolve_billing_key`): посторонний без аккаунта получает
  «сначала /start», клиент — списание строго с ЛИЧНОГО ключа; чужой
  запрос физически не может потратить внутренний баланс.
- Прод-инстанс: `TELEGRAM_DEMO_TRANSFORM_ENABLED=true` (приватный режим —
  гвард «публичный+демо» не срабатывает, т.к. PUBLIC_MODE=false).

### Added
- Тесты: ЛС постороннего проходит мидлварь; группа — отказ; биллинг-ключ:
  личный приоритет / админ-фолбэк / посторонний → None (3 новых + 1 изменён).

## 3.1.3 (2026-11-21) — hotfix: админ получал «🔒 доступ только для администраторов»

### Fixed
- **`data["event_from_user"]` — это ОБЪЕКТ `User`, а не id** (aiogram):
  сравнение `User in {1085557902}` всегда False → AccessControlMiddleware
  не узнавал админа, и любые некомандные сообщения (файл, текст) в личке
  получали ACCESS_DENIED. Фикс: `getattr(user_obj, "id", None)`.
  Юнит-тесты маскировали баг (передавали int) — обновлены на User-объект
  + две регрессии (файл админа разрешён, посторонний отклонён).

### Проверено на production
- Живой feed_update документа от админа через реальный dispatcher:
  корректный ответ про REST API (демо-трансформации отключены by design),
  ошибок в логах нет. 275 тестов зелёные.

## 3.1.2 (2026-11-21) — hotfix: бот не отвечал (aiogram DI)

### Fixed
- **Хендлеры бота падали `TypeError: missing argument 'data'`**: aiogram
  внедряет зависимости ПО ИМЕНИ параметра из workflow data, а значения
  `data` там нет. Все хендлеры с аргументом `data` (включая `/start`)
  молча падали — SQLite-тесты этого не ловили, т.к. не гоняли хендлеры
  через dispatcher. Хендлеры переведены на явные имена
  (session/settings/llm/bot/event_bus/bot_metrics/buy_limiter/
  telegram_api_key_id); добавлен регрессионный тест
  `test_router_handlers_have_no_data_parameter`.

### Проверено на production (живой прогон через dispatcher.feed_update)
- `/start` → клиент создан, личный ключ выдан, приветствие доставлено;
- `/balance`, `/buy` → ответы отправлены; ошибок в логах 0.

## 3.1.1 (2026-11-21) — production hotfix (первый деплой)

Найдено при живом деплое на PostgreSQL (SQLite-тесты не ловили):

### Fixed
- **`max(0, x)` не существует в PostgreSQL** (в SQLite — скалярный max):
  списание/начисление баланса (`_charge_balance`, `_charge_delta`) падали
  `UndefinedFunctionError` → 500 на каждом transform. Заменено на
  диалект-независимый `CASE WHEN ... < 0 THEN 0 ELSE ... END`.
- **Дрейф схемы «модели vs миграции»**: миграции не создавали
  `usage_logs.meta` (мета каждого списания) и `credit_packages.created_at` —
  на чистой PG-базе INSERT usage_logs падал. Добавлена миграция
  `0004_drift_fix` и регрессионный тест
  `test_alembic_schema_matches_models` (сравнивает колонки всех таблиц).

### Проверено на production (живой прогон)
- transform invoice_v1 + normalization: дата `15.12.2026 → 2026-12-15`,
  3 кредита, токены/модель/prompt_version в мета; usage_logs.meta пишется.
- Идемпотентность: replay не меняет баланс, дублей usage нет.
- Инвариант «баланс == сумма транзакций» сходится.
- `/metrics` без ключа → 401; платёжные роуты → 410.

## 3.1.0 (2026-11-21) — release stabilization

### Breaking / Behavior changes
- **Трансформации в боте отключены по умолчанию**
  (`TELEGRAM_DEMO_TRANSFORM_ENABLED=false`): бот отвечает подсказкой про
  REST API; файлы не скачиваются; внутренний ключ не тратится. Включение
  возможно, но в production с публичным режимом старт упадёт без явного
  `ENABLE_DANGEROUS_PUBLIC_DEMO_TRANSFORM=true`.
- **successful_payment сверяется с сохранённым инвойсом**: зачисляется
  снапшот (credits/stars на момент инвойса); несоответствия суммы/валюты/
  пользователя/пакета/платежа → `failed+problem`, алерт, кредиты не
  начисляются (новый код `payment_mismatch`).
- **Ротация переносит баланс** (раньше новый ключ начинался с нуля);
  активные холды блокируют ротацию (`rotation_blocked_by_active_holds`).
- Публичный режим + стартовые кредиты внутреннего ключа в production —
  отказ запуска без `ALLOW_INTERNAL_DEMO_CREDITS=true`.
- Неоплаченные `created`-инвойсы не учитываются в дневном лимите покупок.

### Добавлено
- CLI поддержки: `freeze-key`, `list-customers`, `show-customer`,
  `rotate-customer-key`, `transfer-balance` (идемпотентен),
  `cleanup-stale-payments --dry-run/--apply`.
- Платежи: payload-лимит 128 байт (`payload_too_long`), stale-детект
  (`PAYMENT_PROCESSING_ALERT_MINUTES`, `PAYMENT_RECEIVED_ALERT_MINUTES`,
  метрика `payments_stale_total`, событие `payment.stale`), TTL неоплаченных
  (`PAYMENT_CREATED_TTL_HOURS`), изоляция платежей по клиенту
  (`/v1/payments` показывает все ключи клиента).
- Конфиг-гварды: `TELEGRAM_STARS_PRICES_CONFIRMED` (обязателен в production
  со Stars), предупреждения для JOBS/WEBHOOKS/OCR/STT.
- Инфра: `pytest-cov` + `make coverage` (+`COVERAGE_FAIL_BELOW`),
  интеграционные тесты (`INTEGRATION_POSTGRES/REDIS=1`), CI-джоба с
  сервисами Postgres+Redis, `verify_docker.sh` сеет пакеты и проверяет 410,
  restore-инструкции дополнены инвариантными SQL-проверками.
- Метрики бота: `telegram_transform_disabled_total`,
  `telegram_group_action_denied_total`, `telegram_public_guard_errors_total`.
- Документы: RELEASE_CHECKLIST.md, SUPPORT_RUNBOOK.md.

### Новые переменные
`TELEGRAM_DEMO_TRANSFORM_ENABLED`, `ENABLE_DANGEROUS_PUBLIC_DEMO_TRANSFORM`,
`ALLOW_INTERNAL_DEMO_CREDITS`, `TELEGRAM_STARS_PRICES_CONFIRMED`,
`PAYMENT_PROCESSING_ALERT_MINUTES`, `PAYMENT_RECEIVED_ALERT_MINUTES`,
`PAYMENT_CREATED_TTL_HOURS`, `COVERAGE_FAIL_BELOW` (Makefile).

### Новые коды ошибок
`rotation_blocked_by_active_holds`, `payment_mismatch`, `payload_too_long`.

### Известные ограничения
- Живые проверки Stars/Docker/PG/Redis/LLM не выполнялись автоматически —
  чеклисты: RELEASE_CHECKLIST.md, TELEGRAM_STARS_MANUAL_CHECKLIST.md.
- Метрики бота живут в процессе бота (в /metrics API не попадают до
  реализации push-gateway — см. ROADMAP).
- Возвраты — ручной CLI-контур (Bot API не доставляет refund-события).

## 3.0.0 (2026-11-21) — launch hardening

### Breaking changes
- **Платежи: только Telegram Stars.** HTTP-checkout, фейковое подтверждение
  и Stripe-вебхук отключены навсегда: 410 `payment_provider_disabled`.
  Переменные `STRIPE_*` удалены из `.env.example`; StripeProvider и
  verify_stripe_signature удалены из кода. `FakePaymentProvider` — только DI.
- **Self-serve = Telegram-бот.** Покупка кредитов только через бота
  (`/buy`, инвойс XTR); группы покупать не могут.

### Behavior changes
- Холды имеют TTL (`HOLDS_TTL_SECONDS=900`), heartbeat продлевает живой
  запрос; истёкший холд → reaper закрывает (`status=expired`) и освобождает
  доступный баланс; settle поверх закрытого холда невозможен (409
  `hold_expired`, без списаний; при частичных вызовах — алерт
  `hold_expired_after_partial_usage`).
- Production + `WEB_CONCURRENCY>1` без `REDIS_URL` → отказ запуска, если не
  задан `ALLOW_IN_MEMORY_RATE_LIMIT_IN_PROD=true`.
- Пакеты кредитов переходят на Stars: `stars_amount`, валютa XTR, дефолты
  starter/pro/growth (цены — редактируемые плейсхолдеры).
- Eval: 60 кейсов (+ датасеты нормализации дат/денег/телефонов, инъекции,
  противоречия, пропуски); метрика переименована в `numeric_accuracy_rate`.

### Добавлено
- `customers` + `api_keys.customer_id/is_primary`; бот-команды `/balance`
  (доступный баланс с холдами), `/key` (префикс), `/rotate` (аудит),
  `/buy` (инвойсы XTR, pre-checkout, successful_payment — идемпотентно),
  антифлуд и дневной/часовой лимиты покупок.
- Возвраты: CLI `refund-payment` (ровно один, `refund_shortfall` при
  нехватке), `reconcile-payment`, `payments-list`, `GET /admin/payments`.
- Биллинг: CHECK `credits_balance >= 0`, индексы холдов по истечению,
  `payments.credited_at/refunded_at/problem`.
- Метрики: `holds_expired_total`, `rate_limit_hits_total`,
  `concurrency_limit_hits_total`, `daily/monthly_spend_limit_hits_total`,
  `telegram_stars_*` (инвойсы/успехи/провалы/возвраты/кредиты),
  `payments_stuck`.
- CI (`.github/workflows/ci.yml`): линтеры, тесты+cov, миграции на чистой
  базе (up/down), сборка Docker-образа, мягкий pip-audit.
- `scripts/verify_docker.sh`; Makefile `verify-docker`; `.env.example` 3.0.

### Известные ограничения
- Live-проверки Telegram Stars/Docker/Postgres/Redis/LLM не выполнялись
  автоматически — ручные чеклисты: TELEGRAM_STARS_MANUAL_CHECKLIST.md,
  OPERATIONS.md, EVAL.md.
- Возвраты — ручной CLI-контур (Bot API не доставляет refund-события).
- Веб-кабинет self-serve отложен (docs/SELF_SERVE.md).
- Reaper-лок при нескольких воркерах без Redis — только UPDATE-идемпотентность.

## 2.0.0 (2026-11-21) — commercial hardening


### Breaking / Behavior changes
- **Холды включены по умолчанию** (`HOLDS_ENABLED=true`): доступный баланс =
  `credits_balance − активные холды`; недостаточность проверяется по
  консервативному `max_cost` (repairs+1 вызовов), а не по оценке одного
  вызова. `error.details` ошибок 402 теперь содержит `max_cost` и
  `estimated_cost`.
- **Пер-запросный кап стоимости**: `MAX_COST_DEFAULT_CREDITS=50` (и
  пер-ключевой `per_request_max_cost_credits`): запросы, чей максимум
  дороже капа, отклоняются `cost_limit_exceeded` (402).
- **Оценка стоимости выше**: включает запас выходных токенов
  (`ESTIMATED_OUTPUT_TOKENS=1000`) — минимальная оценка запроса с
  дефолтными ценами = 4 кредита (было ~3).
- **Rate limits по умолчанию**: 60 RPM / 5000 RPD на ключ; пер-ключевые
  переопределения; 429 с заголовками.
- **Системный промпт v2** (анти-инъекция): `meta.prompt_version="v2"`.
- **Списание и usage_log атомарны** и всегда сопровождаются записью в
  `credit_transactions` (инвариант «без транзакции баланс не меняется»).

### Добавлено (P0)
- Биллинговый контур: `credit_transactions`, `credit_holds`,
  `admin_audit_logs`; идемпотентность (`Idempotency-Key` для
  `/v1/transform`, опционально обязательный для админ-кредитов);
  дневные/месячные лимиты трат; `options.max_cost_credits`; контроль
  стоимости автопоисок (остановка `cost_limit_exceeded`).
- Лимиты/защита: rate limiting (in-memory/Redis/Fake), конкурентность на
  ключ, Telegram public mode + allow-list + антфлуд.
- Наблюдаемость: `/metrics` Prometheus (вкл. `ENABLE_METRICS`, защита
  `METRICS_KEY`), `LOG_FORMAT=json`, `X-Request-ID`, компоненты `/health`.
- Качество: `app/prompts.py` (версии), `app/normalization.py`
  (off/basic/strict + `meta.normalization`), provenance
  (`options.include_provenance`), eval-фреймворк `app/eval/` с 25
  синтетическими кейсами и метриками.
- Безопасность файлов: magic bytes, `MAX_PDF_PAGES`/`MAX_CSV_ROWS`/
  `MAX_JSON_DEPTH`/..., `PDF_OCR_ENABLED` (опц. pypdfium2+tesseract),
  `MALWARE_SCAN_ENABLED`+`CLAMAV_URL`.
- Устойчивость LLM: 1 ретрай (таймаут/429/5xx), fallback-модель
  (`LLM_FALLBACK_*`, `meta.fallback_used`), категоризация ошибок.
- Миграции Alembic (baseline 0001 + 0002 commercial), Makefile
  `migrate/migrate-check/revision`.
- Скрипты: `backup_postgres.sh`, `restore_instructions.md`,
  `healthcheck.py`, `load_test.py`; docker-compose.dev.yml (Redis-профиль);
  пример nginx.

### Добавлено (P1, по умолчанию выключено)
- Вебхуки (`WEBHOOKS_ENABLED`): подпись HMAC-SHA256 `X-PerfectJSON-Signature`,
  ретраи с экспоненциальной задержкой, `delivery_id`.
- Асинхронные задачи (`JOBS_ENABLED`): `/v1/jobs`, `/v1/transform/batch`,
  in-process/Redis очередь, retention входов.
- Платежи (`PAYMENTS_ENABLED`; историческое для 2.0 — удалено в 3.0 в
  пользу Telegram Stars): пакеты, Fake + Stripe (PaymentIntent +
  проверка подписи вебхука), идемпотентность по `provider_payment_id` и
  `Idempotency-Key`.
- События: `balance.low/zero`, `usage_over_hold`, `payment.*`, `job.*`;
  каналы: лог, админ-Telegram, SMTP, вебхуки.
- Экономика: `GET /admin/economics` (себестоимость/маржа/разбивки).
- Аудит: `GET /admin/audit-logs`.

### Известные ограничения (на момент 2.0; актуальность — см. 3.0/3.1)
- Self-serve: веб-кабинет отложен и в 3.1 (минимальный self-serve через
  Telegram-бот реализован в 3.0 — docs/SELF_SERVE.md).
- ~~Telegram Stars: интеграция — 2.1~~ → реализовано в 3.0 (единственный метод).
- ~~Холды не имеют авто-экспирации~~ → исправлено в 3.0 (TTL + reaper).
- Воркер задач — один consumer на процесс (актуально и в 3.1; ROADMAP).

## 1.0.0 (2026-11-20)
- MVP: API трансформации с автопочинкой, кредиты, пресеты, экстракторы,
  Telegram-бот, CLI, Docker, 90 тестов.
