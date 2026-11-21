# Восстановление Perfect JSON API из бэкапа

## Когда применять
- потеря/повреждение тома PostgreSQL;
- перенос окружения (staging → production);
- расследование инцидента на копии данных.

## Шаги

1. **Остановите API и бота** (чтобы не писать в восстанавливаемую базу):

   ```bash
   docker compose stop api bot
   ```

2. **Восстановите дамп в чистую базу** (⚠️ деструктивно — база пересоздаётся):

   ```bash
   # из переменных окружения
   gunzip -c backups/perfectjson_20261121T120000Z.sql.gz | \
     psql "$DATABASE_URL"

   # или через docker
   gunzip -c backups/perfectjson_<stamp>.sql.gz | \
     docker compose exec -T postgres psql -U perfectjson -d perfectjson
   ```

3. **Сверьте схему**: после восстановления примените миграции, если бэкап
   старше кода:

   ```bash
   docker compose run --rm api alembic upgrade head
   ```

4. **Проверьте целостность**:

   ```bash
   psql "$DATABASE_URL" -c "SELECT count(*) FROM api_keys;"
   psql "$DATABASE_URL" -c "SELECT count(*) FROM usage_logs;"
   psql "$DATABASE_URL" -c "SELECT coalesce(sum(amount_delta),0) FROM credit_transactions;"
   # Инвариант: credits_balance каждого ключа == сумма его транзакций
   psql "$DATABASE_URL" -c "
     SELECT k.id, k.credits_balance,
            coalesce((SELECT sum(amount_delta) FROM credit_transactions t
                      WHERE t.api_key_id = k.id AND t.type IN
                      ('usage','admin_adjustment','payment','refund','promo','correction')),0) AS derived
     FROM api_keys k;"
   ```

5. **Проверьте инварианты 3.1** (после миграций, до пуска трафика):

   ```bash
   # нет отрицательных балансов
   psql "$DATABASE_URL" -c "SELECT id FROM api_keys WHERE credits_balance < 0;"
   # баланс каждого ключа == сумма его транзакций изменения баланса
   psql "$DATABASE_URL" -c "
     SELECT k.id FROM api_keys k WHERE k.credits_balance <> (
       SELECT coalesce(sum(t.amount_delta),0) FROM credit_transactions t
       WHERE t.api_key_id = k.id AND t.type IN
       ('usage','admin_adjustment','payment','refund','promo','correction'));"
   # нет активных холдов без ключа и нет «вечно активных» (старше 2×TTL)
   psql "$DATABASE_URL" -c "
     SELECT h.id FROM credit_holds h
     LEFT JOIN api_keys k ON k.id = h.api_key_id
     WHERE h.status = 'active' AND (k.id IS NULL OR h.expires_at < now() - interval '2 hours');"
   # нет дублирующихся платежей провайдера
   psql "$DATABASE_URL" -c "
     SELECT provider_payment_id, count(*) FROM payments
     GROUP BY provider_payment_id HAVING count(*) > 1;"
   ```

6. **Запустите сервисы и проверьте /health**:

   ```bash
   docker compose up -d
   curl -s http://127.0.0.1:8000/health | python3 -m json.tool
   ```

## Рекомендации по частоте

| Данные | RPO (максимум потерь) | Способ |
|---|---|---|
| PostgreSQL (ключи, кредиты, журнал) | ≤ 24 ч | `scripts/backup_postgres.sh` ежедневно по cron; для роста — каждый час |
| Секреты (.env) | — | хранить в менеджере секретов; бэкап .env вне репозитория |
| Сырые данные запросов | не восстанавливаются | не сохраняются сервисом (кроме временных входов задач — см. docs/JOBS.md) |

## Учёт при восстановлении

- Не восстанавливайте базу «назад во времени» при живых плательщиках без
  сверки транзакций: после восстановления сверьте балансы ключей с
  последними событиями `payment.*` (см. docs/OPERATIONS.md, «Инциденты»).
- Файлы бэкапов содержат данные клиентов — шифруйте хранилище и ограничьте
  доступ (chmod 600, отдельный бакет/том).
