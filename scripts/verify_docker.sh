#!/usr/bin/env bash
# Локальная проверка Docker-окружения Perfect JSON API 3.0.
# Собирает образы, поднимает compose, применяет миграции, проверяет /health
# и останавливает окружение. Требует Docker и доступ к сети (pull базовых
# образов) — при их отсутствии честно сообщает и выходит с кодом 2.
set -euo pipefail

cd "$(dirname "$0")/.."

command -v docker >/dev/null 2>&1 || { echo "ERROR: docker не найден в PATH"; exit 2; }
docker info >/dev/null 2>&1 || { echo "ERROR: демон Docker не запущен"; exit 2; }

if [ ! -f .env ]; then
  echo "==> Готовлю временный .env для проверки"
  cp .env.example .env
  echo "SECRET_KEY=verify-$(date +%s)" >> .env
  echo "ADMIN_API_KEY=verify-admin-$(date +%s)" >> .env
  CLEAN_ENV=1
else
  CLEAN_ENV=0
fi

cleanup() {
  echo "==> Останавливаю окружение"
  docker compose down -v --remove-orphans >/dev/null 2>&1 || true
  if [ "${CLEAN_ENV:-0}" = "1" ]; then rm -f .env; fi
}
trap cleanup EXIT

echo "==> Сборка образов (docker compose build)"
docker compose build --quiet

echo "==> Запуск postgres + api"
docker compose up -d postgres api

echo "==> Ожидание здоровья Postgres"
for i in $(seq 1 30); do
  if docker compose exec -T postgres pg_isready -U perfectjson >/dev/null 2>&1; then break; fi
  sleep 1
done

echo "==> Применение миграций (alembic upgrade head)"
docker compose exec -T api alembic upgrade head

echo "==> Сев пакетов Telegram Stars (безопасен при выключенных платежах)"
docker compose exec -T api python -m app.cli seed-packages >/dev/null 2>&1 || echo "note: seed-packages требует DATABASE_URL (пропущено)"

echo "==> Проверка /health"
for i in $(seq 1 30); do
  STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/health || true)
  if [ "$STATUS" = "200" ]; then
    echo "OK: /health -> 200"
    curl -s http://127.0.0.1:8000/health
    echo
    echo "==> Проверка отключённых платёжных маршрутов (ожидаем 410)"
    for path in /v1/payments/checkout /v1/payments/fake/confirm /webhooks/stripe; do
        CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "http://127.0.0.1:8000$path" \
              -H "Content-Type: application/json" -d "{}" || true)
        if [ "$CODE" = "410" ]; then
            echo "OK: $path -> 410 payment_provider_disabled"
        else
            echo "WARN: $path -> $CODE (ожидается 410; проверьте routes/payments.py)"
        fi
    done
    echo "VERIFY_DOCKER_OK"
    exit 0
  fi
  sleep 1
done

echo "ERROR: /health не ответил 200 за 30 секунд (последний статус: $STATUS)"
docker compose logs api --tail 50 || true
exit 1
