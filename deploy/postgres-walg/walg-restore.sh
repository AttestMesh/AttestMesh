#!/bin/sh
# Disaster recovery: fetch + decrypt the latest (or a named) base backup into an EMPTY data dir, then arm
# WAL replay so Postgres recovers to a point-in-time. Invoked by the entrypoint when BACKUP_RESTORE is set
# (a deliberate, fresh-disk restore deploy — see deploy/matrix-node.sh restore). The CSK key must already
# be written. $1 = LATEST | <backup-name>, $2 = data dir.
set -eu
TARGET="${1:-LATEST}"
PGDATA_DIR="${2:-${PGDATA:-/var/lib/postgresql/data}}"
. /usr/local/bin/walg-env.sh
[ -n "${WALG_LIBSODIUM_KEY:-}" ] || { echo "walg-restore: no CSK key — cannot decrypt" >&2; exit 1; }

echo "walg-restore: backup-fetch $TARGET -> $PGDATA_DIR"
wal-g backup-fetch "$PGDATA_DIR" "$TARGET"

# Arm archive recovery: replay WAL from R2 via the restore_command, up to the requested time (or all WAL).
{
  echo "restore_command = '/usr/local/bin/walg-wal-fetch %f %p'"
  if [ -n "${BACKUP_RESTORE_TARGET_TIME:-}" ]; then
    echo "recovery_target_time = '${BACKUP_RESTORE_TARGET_TIME}'"
    echo "recovery_target_action = 'promote'"
  fi
} >> "$PGDATA_DIR/postgresql.auto.conf"
touch "$PGDATA_DIR/recovery.signal"
chown -R postgres:postgres "$PGDATA_DIR" 2>/dev/null || true
echo "walg-restore: base restored + WAL replay armed (target=${BACKUP_RESTORE_TARGET_TIME:-latest WAL})"
