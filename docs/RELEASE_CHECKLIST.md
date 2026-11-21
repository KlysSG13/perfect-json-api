# Release Checklist 3.1.0 — go/no-go

Статусы: ☐ не проверено / ✅ пройдено / ⛔ блокер.

## 1. Offline checks (обязательны, без сети)
- [ ] `make install-dev` без ошибок
- [ ] `make lint` (ruff + pyflakes + compileall) — чисто
- [ ] `pytest -q` — **271 passed** (offline; 4 integration deselected)
- [ ] `make coverage` — отчёт создаётся; зафиксировать текущий процент в заметках релиза
- [ ] Миграции на чистой базе: `alembic upgrade head` + `alembic downgrade -1` + upgrade
- [ ] Миграция с копии 3.0: восстановить бэкап 3.0 в тестовую БД → `alembic upgrade head`
- [ ] `pytest tests/test_migrations.py` — зелёный (включая 3.0→head с данными)
- [ ] `python -m app.eval.run --provider fake` — 60 кейсов, schema_pass_rate = 1.0
- [ ] В `.env.example` нет `STRIPE_*`; grep -r "STRIPE_SECRET" по репозиторию пуст

## 2. Docker checks
- [ ] `make verify-docker` полностью проходит (build → up → миграции → seed → /health → 410-роуты → down)
- [ ] Образ не от root; healthcheck внутри образа зелёный
- [ ] `docker compose config` без предупреждений о неизвестных переменных

## 3. Postgres checks (staging-копия)
- [ ] `INTEGRATION_POSTGRES=1 pytest tests/integration -q -m integration -o addopts=""` — зелёный
- [ ] Конкурентные списания на PG: баланс сходится с журналом
- [ ] Инварианты после нагрузочного прогона (scripts/restore_instructions.md, раздел 5): пусто

## 4. Redis checks
- [ ] `INTEGRATION_REDIS=1 pytest tests/integration/test_redis.py ...` — зелёный
- [ ] При `REDIS_URL` приложение стартует с RedisRateLimiter (лог старта)

## 5. Telegram Stars checks ([TELEGRAM_STARS_MANUAL_CHECKLIST.md](TELEGRAM_STARS_MANUAL_CHECKLIST.md))
- [ ] Все пункты чеклиста прожаты вручную на тестовом боте
- [ ] Оплата идемпотентна (перезапуск обработчика не дублирует баланс)
- [ ] Возврат: ровно один; shortfall фиксируется
- [ ] Зависшие processing/received видны в `payments-list` и алертят
- [ ] `cleanup-stale-payments --dry-run` показывает только созданные>TTL

## 6. Live LLM checks
- [ ] `POST /v1/transform` с реальным ключом: 200, meta.attempts/cost_credits адекватны
- [ ] Списание видно в `/v1/usage` и `credit_transactions`
- [ ] `python -m app.eval.run --provider live` — отчёт в eval/reports, метрики зафиксированы

## 7. Backup/restore checks
- [ ] `BACKUP_DIR=... ./scripts/backup_postgres.sh` — дамп создан
- [ ] Восстановление в тестовую БД по scripts/restore_instructions.md — шаги 1–6 прошли
- [ ] Инвариантные SQL-проверки после восстановления — пусто

## 8. Pricing checks
- [ ] Цены в `TELEGRAM_STARS_PACKAGES` не плейсхолдеры (проверены вручную)
- [ ] `TELEGRAM_STARS_PRICES_CONFIRMED=true` в production .env (иначе старт упадёт)
- [ ] Экономика: `GET /admin/economics` → telegram_stars.revenue_est_usd осмыслен
  (TELEGRAM_STARS_USD_PER_STAR задан) или честно `estimated: true`

## 9. Refund policy checks
- [ ] Политика возвратов описана (docs/PRICING.md) и известна поддержке
- [ ] `refund-payment` отрепетирован на staging: успех, дубль, shortfall

## 10. Support checks
- [ ] docs/SUPPORT_RUNBOOK.md доступен дежурному (без доступа к репе — распечатан/в вики)
- [ ] Канал алертов живой (лог → админ-TG/SMTP при настройке)
- [ ] Контакты поддержки указаны в боте (`/help`)

## Go/No-Go
**GO**, если: разделы 1–4 полностью зелёные; из 5–9 не выполнены только пункты,
не влияющие на деньги (например, live-eval при плановом старте без него).
**NO-GO**, если любое из: тесты красные; миграции неapply на копии 3.0;
инварианты баланса нарушены; Stars-чеклист не прожит; цены не подтверждены.
