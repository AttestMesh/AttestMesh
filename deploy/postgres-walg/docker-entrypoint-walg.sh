#!/usr/bin/env bash
# postgres-walg entrypoint. With BACKUP_ENABLED=true it: (optionally) restores from R2 into an empty data
# dir, starts the CSK-key writer + the base-backup loop, and runs Postgres with WAL archiving on. With
# BACKUP_ENABLED unset/false it behaves EXACTLY like the stock postgres image (no wal-g, no archiving).
set -e
ARGS=("$@")
PGDATA_DIR="${PGDATA:-/var/lib/postgresql/data}"

# Ownership migration: wal-g needs glibc so this image is debian (postgres uid 999), but the data dir may
# have been created by the old postgres:16-ALPINE (uid 70) and is preserved across our in-place rolls.
# Chown ONCE if the owner differs so Postgres can read its files; a no-op on every subsequent start.
if [ "${1:-}" = "postgres" ] && [ -d "$PGDATA_DIR" ] && [ "$(id -u)" = "0" ]; then
  owner="$(stat -c %u "$PGDATA_DIR" 2>/dev/null || echo "")"
  if [ -n "$owner" ] && [ "$owner" != "$(id -u postgres)" ]; then
    echo "postgres-walg: migrating $PGDATA_DIR ownership ($owner -> postgres $(id -u postgres))"
    chown -R postgres:postgres "$PGDATA_DIR" || true
  fi
fi

if [ "${BACKUP_ENABLED:-false}" = "true" ] && [ "${1:-}" = "postgres" ]; then
  : "${WALG_KEY_FILE:=/run/walg/key}"; export WALG_KEY_FILE
  mkdir -p "$(dirname "$WALG_KEY_FILE")"
  PGDATA_DIR="${PGDATA:-/var/lib/postgresql/data}"

  if [ -n "${BACKUP_RESTORE:-}" ] && [ ! -s "$PGDATA_DIR/PG_VERSION" ]; then
    # Deliberate disaster-recovery restore into an empty data dir (matrix-node.sh restore).
    echo "postgres-walg: BACKUP_RESTORE=$BACKUP_RESTORE into empty data dir -> restoring from R2"
    /usr/local/bin/walg-csk-key.sh                  # foreground: block until the CSK key is written
    /usr/local/bin/walg-restore.sh "$BACKUP_RESTORE" "$PGDATA_DIR"
  else
    /usr/local/bin/walg-csk-key.sh &                # background: write the key once the sidecar is ready
  fi
  /usr/local/bin/walg-backup-loop.sh &              # background: 6h base backups + retention

  # WAL archiving. archive_command DEFERS (exit 1) until the CSK key exists, so Postgres keeps WAL until
  # it can encrypt+ship — no loss during the brief sidecar-bringup window. (restore_command is written
  # into postgresql.auto.conf only on a restore, by walg-restore.sh.)
  ARGS+=(-c wal_level=replica -c archive_mode=on -c "archive_command=/usr/local/bin/walg-archive %p"
         -c "archive_timeout=${BACKUP_ARCHIVE_TIMEOUT:-300}" -c max_wal_senders=3
         -c "wal_keep_size=${BACKUP_WAL_KEEP_SIZE:-512MB}")
fi

exec docker-entrypoint.sh "${ARGS[@]}"
