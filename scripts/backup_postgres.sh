#!/usr/bin/env bash
# Бэкап PostgreSQL для Perfect JSON API.
# Использование: ./scripts/backup_postgres.sh [имя_файла]
# Секреты берутся из окружения (PGHOST/PGUSER/PGDATABASE/PGPASSWORD или DATABASE_URL).
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-backups}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="${1:-$BACKUP_DIR/perfectjson_$STAMP.sql.gz}"

if [ -z "${PGDATABASE:-}" ] && [ -z "${DATABASE_URL:-}" ]; then
  echo "ERROR: задайте PGDATABASE (и PGHOST/PGUSER/PGPASSWORD) или DATABASE_URL" >&2
  exit 2
fi

mkdir -p "$(dirname "$OUT")"

if [ -n "${PGDATABASE:-}" ]; then
  pg_dump --no-owner --no-privileges | gzip -9 > "$OUT"
else
  # Выделение параметров из postgresql://... (без вывода пароля в лог)
  URL="${DATABASE_URL%%\?*}"
  PGHOST="$(echo "$URL" | sed -E 's#.*@([^:/]+).*#\1#')"
  PGPORT="$(echo "$URL" | sed -nE 's#.*:([0-9]+)/.*#\1#p')"
  PGDATABASE="$(echo "$URL" | sed -E 's#.*/([^/?]+)$#\1#')"
  export PGHOST PGPORT="${PGPORT:-5432}" PGDATABASE
  pg_dump --no-owner --no-privileges | gzip -9 > "$OUT"
fi

echo "OK: $OUT ($(du -h "$OUT" | cut -f1))"

# Ротация: хранить последние N копий
KEEP="${BACKUP_KEEP_COUNT:-14}"
ls -1t "$BACKUP_DIR"/perfectjson_*.sql.gz 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f
echo "Ротация: оставлено не более $KEEP копий в $BACKUP_DIR"
