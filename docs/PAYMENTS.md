# Платежи 3.0: только Telegram Stars

## Принцип
Единственный пользовательский способ оплаты на запуске — **Telegram Stars**.
Stripe/Fake-checkout/вебхуки удалены из пользовательского контура: маршруты
`POST /v1/payments/checkout`, `POST /v1/payments/fake/confirm`,
`POST /webhooks/stripe` отвечают **410 `payment_provider_disabled`** всегда
(роуты зарегистрированы, чтобы ответ был предсказуемым).
`FakePaymentProvider` существует только для DI в тестах.

## Включение
```env
PAYMENTS_ENABLED=true
TELEGRAM_STARS_ENABLED=true
TELEGRAM_BOT_TOKEN=...          # бот проводит оплату
TELEGRAM_STARS_PACKAGES=starter:500:100,pro:2000:350,growth:6000:900
python -m app.cli seed-packages # применить пакеты в БД
```
Пакеты: `code:credits:stars_amount` — целые числа; цены в чеке —
плейсхолдеры, отредактируйте под свой прайс (Бот не валидирует «рынок»).

## Пакеты (по умолчанию)
| Код | Кредиты | Stars (плейсхолдер) |
|---|---|---|
| starter | 500 | 100 |
| pro | 2000 | 350 |
| growth | 6000 | 900 |

Управление: `python -m app.cli seed-packages | list-packages |
activate-package --code X | deactivate-package --code X`. Неактивные пакеты
не показываются в `/buy` и отклоняются на pre-checkout.

## Поток оплаты (бот)
1. `/buy` (только личные сообщения; антифлуд 3/мин) → кнопки пакетов.
2. Кнопка → `pre_checkout_check` (пакет активен, лимиты) →
   `send_invoice(currency="XTR", provider_token не задаётся)` с payload
   `pkg:<code>:uid:<user>:pid:<payment_id>` — без секретов; инвойс
   логируется и создаёт запись `payments (status=created)`.
3. `pre_checkout_query`: проверка пакета/клиента/дневного лимита → `ok`
   или отказ с причиной.
4. `successful_payment`: `telegram_payment_charge_id` — уникальный
   `provider_payment_id`:
   - идемпотентное создание/поиск платежа (`status=received`);
   - атомарный захват `received → processing` (двойная доставка не начислит дважды);
   - зачисление на личный ключ клиента (`credit_transactions`, type=payment,
     idempotency_key=charge_id), `status=succeeded`, счёт, метрики,
     событие `payment.succeeded`, сообщение пользователю.
5. Повторная доставка → `already processed`, баланс не меняется.
6. Пакет неизвестен/неактивен → платёж `failed`+`problem`, кредиты не
   начисляются, админ-алерт.
7. Клиент и ключ создаются автоматически, если их ещё нет.

## Лимиты и антифрод
- `TELEGRAM_STARS_MAX_PURCHASES_PER_DAY=10` (успешные+received за UTC-день);
- `TELEGRAM_STARS_MAX_INVOICES_PER_HOUR=5`;
- антифлуд `/buy`; покупки только в личных чатах;
- алерт при всплеске платежей от одного пользователя (события payment.*).

## Возвраты
Ручной контур (Telegram не доставляет refund-события в базовом Bot API):
```bash
python -m app.cli refund-payment --charge-id <telegram_payment_charge_id>
```
- ровно один возврат (повтор → `payment_duplicate`);
- кредиты снимаются транзакцией `refund`; баланс не уходит в минус;
- при нехватке кредитов — списание до нуля, ошибка `refund_shortfall`,
  запись в журнал и админ-алерт.

## Сверка и зависшие платежи
```bash
python -m app.cli payments-list --provider telegram_stars
python -m app.cli payments-list --status received
python -m app.cli reconcile-payment --charge-id ...   # повторить зачисление
GET /admin/payments?provider=telegram_stars&status=... # HTTP-просмотр
```
Платёж «получен, но не зачислен» остаётся в `received/processing` с
`problem=true` (алерт `payment.credit_failed`): сверьте с Telegram,
зачислите `reconcile-payment` или верните Stars через Telegram-поддержку.

## Экономика
`GET /admin/economics` содержит блок `telegram_stars`: число платежей,
сумма Stars, выданные кредиты, возвраты, оценка выручки в USD через
`TELEGRAM_STARS_USD_PER_STAR` (не задан — `revenue_est_usd: null`,
`estimated: true`).

## Ручная проверка
Чеклист живой оплаты: [TELEGRAM_STARS_MANUAL_CHECKLIST.md](TELEGRAM_STARS_MANUAL_CHECKLIST.md).
Автотесты (`tests/test_payments.py`, `test_bot_self_serve.py`) покрывают
логику на фейках без сети; живой прогон оплаты требует реального бота.

## 3.1: снапшот цены и несоответствия
- Инвойс сохраняет `package_code`, `credits`, `stars_amount` на момент создания.
  Успешная оплата зачисляет **снапшот**: изменение цены пакета после инвойса
  не влияет на уже выставленный счёт.
- Деактивация пакета после инвойса: `pre_checkout_query` отклоняется; если
  оплата уже прошла — зачисление по снапшоту + алерт-заметка оператору
  (`payment.package_deactivated_after_purchase`).
- Несоответствия доставки (сумма / валюта ≠ XTR / другой пользователь /
  другой пакет / неизвестный платёж) → статус `failed` + `problem=true`,
  кредиты не начисляются, алерт `payment.mismatch` (код `payment_mismatch`).
- Payload инвойса ≤128 байт, без секретов (`pkg:<code>:uid:<id>:pid:<id>`);
  превышение → `payload_too_long`, инвойс не создаётся.

## 3.1: зависшие платежи и очистка
- `processing` старше `PAYMENT_PROCESSING_ALERT_MINUTES` (15) и `received`
  старше `PAYMENT_RECEIVED_ALERT_MINUTES` (30) НЕ удаляются: фоновый
  watch-loop алертит `payment.stale` + метрика `payments_stale_total`.
- Неоплаченные `created` старше `PAYMENT_CREATED_TTL_HOURS` (24) чистятся
  идемпотентно: `python -m app.cli cleanup-stale-payments --dry-run|--apply`;
  на лимиты покупок они не влияют (лимит считает оплаченные).

## 3.1: цены — защита от плейсхолдеров
Стартовые цены (100/350/900 Stars) — редактируемые заглушки. В production при
`PAYMENTS_ENABLED=true` + `TELEGRAM_STARS_ENABLED=true` сервис требует
`TELEGRAM_STARS_PRICES_CONFIRMED=true`, иначе отказывается стартовать:
«Подтвердите, что цены пакетов не плейсхолдерные».

## 3.1: изоляция, ротация и поддержка
- `/v1/payments` изолирован по клиенту: ключ видит платежи всех своих ключей
  (после `/rotate` история сохраняется); чужой ключ чужого не видит.
- Ручные сценарии поддержки: `freeze-key`, `list-customers`, `show-customer`,
  `rotate-customer-key` (перенос баланса/лимитов; блокируется активными
  холдами), `transfer-balance` (идемпотентен по `--idempotency-key`).
  Полный runbook: [SUPPORT_RUNBOOK.md](SUPPORT_RUNBOOK.md).

