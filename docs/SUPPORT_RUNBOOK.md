# Support Runbook 3.1 — сценарии дежурного

Все команды выполняются из корня репозитория с настроенным `.env`
(или с переменными окружения проекта). Ничего не требует доступа
к памяти авторов — только к репозиторию и БД.

## 0. Быстрая диагностика
```bash
curl -s http://127.0.0.1:8000/health | python3 -m json.tool   # компоненты
python -m app.cli payments-list --provider telegram_stars
python -m app.cli list-customers
python -m app.cli show-customer --telegram-user-id <id>
```

## 1. Платёж прошёл, кредиты не пришли
1. Найдите платёж: `python -m app.cli payments-list --status received`
   (или `--status processing`); возьмите `provider_payment_id` (charge id).
2. Причина в логах: `payment.credit_failed` / `payment.mismatch` / `payment.package_problem`.
3. Лечение: устранить причину (пакет? ключ?) и
   `python -m app.cli reconcile-payment --charge-id <id>` — идемпотентно.
4. Если зачислить нельзя (ошибка mismatch подтверждена) — вернуть Stars
   пользователю через Telegram-поддержку и зафиксировать инцидент.
5. Если платёж вообще не найден — сверить у пользователя charge id из
   чека Telegram; при отсутствии записи — проблема на стороне доставки,
   создать тикет (логи `successful_payment`).

## 2. Платёж задублирован (двойное начисление?)
1. `python -m app.cli payments-list --provider telegram_stars` — статус
   `succeeded` уникален по charge id; дублей быть не может (CHECK-логика).
2. Если баланс всё же кажется раздутым — проверить журнал:
   `SELECT * FROM credit_transactions WHERE api_key_id=<id> ORDER BY id;`
   каждое начисление `payment` обязано иметь уникальный `idempotency_key`.
3. При подтверждённом дубле (внешняя правка БД) — компенсировать через
   `python -m app.cli add-credits --key-id <id> --credits -N` (аудит) и
   починить источник дубля.

## 3. Пользователь потерял ключ
- Сырой ключ не восстанавливается (только хэш). Ротация:
  `python -m app.cli rotate-customer-key --customer-id <id> --reason "lost key"` —
  баланс переносится, старый ключ отзывается, сырой ключ печатается один раз.
  Передайте его пользователю защищённым каналом.
- При активных запросах ротация блокируется — повторить через пару минут.

## 4. Ключ скомпрометирован
1. Немедленно: `python -m app.cli freeze-key --key-id <id> --reason "compromised"`.
2. Проверить использование: `python -m app.cli show-usage --key-id <id> --limit 50`.
3. Связаться с клиентом → `rotate-customer-key` (новый ключ, баланс сохранён).
4. Инцидент записать в `admin_audit_logs` (freeze/rotate уже пишутся).

## 5. Пользователь просит возврат
- Полный: `python -m app.cli refund-payment --charge-id <id>` (возврат Stars
  делается через Telegram-поддержку отдельно; команда списывает кредиты).
- Дубль возврата невозможен (статус refunded → `payment_duplicate`).

## 6. Возврат после потраченных кредитов
- Если на балансе меньше, чем нужно вернуть: команда спишет до нуля и
  упадёт с `refund_shortfall` (запись + алерт). Решение: ручной возврат
  остатка Stars через Telegram с пометкой в инциденте; кредиты не уходят в минус.

## 7. Зависший холд
- Reaper закрывает истёкшие сам (интервал 60 c): статус станет `expired`,
  доступный баланс вернётся. Проверить:
  `SELECT id, status, expires_at FROM credit_holds WHERE status='active';`
- Если активных старше 2×TTL нет — всё в порядке. Если есть — проверить,
  что процесс API жив и `RUN_HOLD_REAPER=true`.

## 8. Зависший платёж
- `payments-list --status processing` / `--status received`: старые алертят
  (`payment.stale`). Действие — как в сценарии 1 (reconcile или возврат).
- Неоплаченные инвойсы чистятся:
  `python -m app.cli cleanup-stale-payments --dry-run` → `--apply`.

## 9. Проблема с пакетом (цена/активность)
- Изменить цены: отредактировать `TELEGRAM_STARS_PACKAGES` → `seed-packages`
  (обновляет существующие). Уже созданные инвойсы не меняются (снапшот).
- Деактивация: `deactivate-package --code X`. Оплаченные до деактивации
  зачисляются по снапшоту с алертом оператору.

## 10. Бот не отвечает
1. `docker compose logs bot --tail 50` / журнал процесса.
2. Частые причины: токен невалиден; Telegram недоступен (flood wait в логах —
   просто подождать); упал на конфиг-гварде (читать «Refusing to start...»).
3. Перезапуск: `docker compose restart bot`.

## 11. LLM недоступен
- Сервис отвечает 502/504 `llm_unavailable`/`llm_timeout`, кредиты не списываются,
  холды освобождаются. Проверить провайдера; при настройке fallback — убедиться,
  что `LLM_FALLBACK_*` валидны. Временно переключить `LLM_BASE_URL/LLM_MODEL`.
