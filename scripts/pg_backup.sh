#!/usr/bin/env bash
set -euo pipefail

# Native pg_dump with daily/weekly/monthly rotation (7/4/6 kept), replacing the
# prodrigestivill/postgres-backup-local Docker container now that Postgres runs natively.
# Run daily via deploy/systemd/invoice-pg-backup.timer, or manually to test a restore:
#   gunzip -c backups/postgres/daily/invoices-<date>.sql.gz | psql -U invoice -d invoices

cd "$(dirname "$0")/.."
set -a
source .env
set +a

BACKUP_ROOT="${PG_BACKUP_DIR:-$(pwd)/backups/postgres}"
DATE=$(date +%Y%m%d)
DAY_OF_WEEK=$(date +%u)   # 1 = Monday
DAY_OF_MONTH=$(date +%d)

mkdir -p "$BACKUP_ROOT/daily" "$BACKUP_ROOT/weekly" "$BACKUP_ROOT/monthly"

DUMP_FILE="$BACKUP_ROOT/daily/${POSTGRES_DB}-${DATE}.sql.gz"
PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -h localhost -U "$POSTGRES_USER" -d "$POSTGRES_DB" | gzip > "$DUMP_FILE"
echo "Backed up to $DUMP_FILE"

if [ "$DAY_OF_WEEK" = "1" ]; then
  cp "$DUMP_FILE" "$BACKUP_ROOT/weekly/${POSTGRES_DB}-$(date +%Y-W%V).sql.gz"
fi

if [ "$DAY_OF_MONTH" = "01" ]; then
  cp "$DUMP_FILE" "$BACKUP_ROOT/monthly/${POSTGRES_DB}-$(date +%Y%m).sql.gz"
fi

find "$BACKUP_ROOT/daily" -name '*.sql.gz' -mtime +7 -delete
find "$BACKUP_ROOT/weekly" -name '*.sql.gz' -mtime +28 -delete
find "$BACKUP_ROOT/monthly" -name '*.sql.gz' -mtime +180 -delete
