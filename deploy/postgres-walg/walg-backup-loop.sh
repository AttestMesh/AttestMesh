#!/bin/sh
# Periodic base backups (wal-g backup-push) + retention. Base every BACKUP_BASE_INTERVAL_SECONDS (6h),
# keeping BACKUP_RETAIN_FULL full backups (and the WAL after the oldest) — sized to cover the WAL window
# so PITR can land anywhere in the last ~24h. WAL between bases is shipped continuously by the
# archive_command (walg-archive). Runs as a background process started by the entrypoint.
set -u
INTERVAL="${BACKUP_BASE_INTERVAL_SECONDS:-21600}"     # 6h
RETAIN="${BACKUP_RETAIN_FULL:-5}"                      # ~5 x 6h = 30h of restorable bases
KEY_FILE="${WALG_KEY_FILE:-/run/walg/key}"
PGDATA_DIR="${PGDATA:-/var/lib/postgresql/data}"

while [ ! -s "$KEY_FILE" ]; do sleep 5; done          # need the CSK key to encrypt
until pg_isready -q 2>/dev/null; do sleep 5; done      # need a live Postgres to bracket the backup
echo "walg-backup-loop: started (base every ${INTERVAL}s, retain ${RETAIN} full)"
while :; do
  . /usr/local/bin/walg-env.sh
  echo "walg-backup-loop: backup-push $(date -u +%FT%TZ)"
  if wal-g backup-push "$PGDATA_DIR"; then
    wal-g delete retain FIND_FULL "$RETAIN" --confirm 2>&1 | tail -2 || true
  else
    echo "walg-backup-loop: backup-push FAILED (retrying next cycle)" >&2
  fi
  sleep "$INTERVAL"
done
