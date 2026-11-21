# Вебхуки пользователей (WEBHOOKS_ENABLED=false по умолчанию)

## Включение
```env
WEBHOOKS_ENABLED=true
WEBHOOK_MAX_RETRIES=5
WEBHOOK_TIMEOUT_SECONDS=10
```

## Регистрация
```bash
curl -X POST http://127.0.0.1:8000/v1/webhooks \
  -H "X-API-Key: pj_..." -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/hook", "events": ["job.completed", "balance.low"]}'
```
Ответ содержит `secret` (`whsec_...`) — **показывается один раз**.
События: `job.completed`, `job.failed`, `balance.low`, `payment.succeeded`,
`payment.failed`. Управление: `GET /v1/webhooks`, `DELETE /v1/webhooks/{id}`.

## Доставка
```json
{
  "delivery_id": "уникальный идентификатор доставки",
  "type": "job.completed",
  "created_at": "2026-11-21T12:00:00+00:00",
  "data": { ... }
}
```
- Подпись: `X-PerfectJSON-Signature: t=<unix>,v1=<hex>`; HMAC-SHA256 от
  `"{t}.{body}"` с секретом эндпоинта. Проверка на приёме:
  ```python
  expected = hmac.new(secret, f"{t}.{body}".encode(), hashlib.sha256).hexdigest()
  hmac.compare_digest(expected, provided)
  ```
- Успех = HTTP 2xx. Иначе — ретраи с экспоненциальной задержкой
  (30с × 2^n), максимум `WEBHOOK_MAX_RETRIES`, затем `failed`.
  Проход ретраев выполняет фоновый цикл (раз в 30 с) и метод
  `process_pending_once()`.
- Идемпотентность на приёме: ключ `delivery_id` (повторную доставку
  можно безопасно игнорировать).

## Хранение
`webhook_endpoints` (URL, секрет, события, active) и `webhook_deliveries`
(delivery_id, payload, статус, попытки, next_attempt_at, код ответа).
Полезная нагрузка содержит только идентификаторы/суммы — без сырых данных.

## Тесты без сети
`tests/test_webhooks.py` использует `FakeWebhookTransport`: подпись,
идемпотентность, ретраи (управляемое время через `process_pending_once(now=)`),
отказ после max_retries, выключенный флаг → 404.
