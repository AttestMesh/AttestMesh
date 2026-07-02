#!/usr/bin/env bash
# Patroni custom bootstrap method for full-cluster disaster recovery (BACKUP_RESTORE set):
# instead of initdb, the bootstrap leader fetches the latest (or named) encrypted base backup
# from R2 and arms WAL replay. Replicas then basebackup from the restored leader as usual.
# Patroni invokes this as: pgha-walg-bootstrap.sh --scope=<scope> --datadir=<dir>
set -u
STAT="${PGHA_STATUS_FILE:-/pgha-status/state}"
_st() { echo "$(date -u +%FT%TZ) walg-bootstrap: $*" >> "$STAT" 2>/dev/null || true; }

DATADIR=""
for arg in "$@"; do
  case "$arg" in
    --datadir=*) DATADIR="${arg#--datadir=}" ;;
  esac
done
[ -n "$DATADIR" ] || { _st "FATAL: no --datadir argument"; exit 1; }

TARGET="${BACKUP_RESTORE:-LATEST}"
[ "$TARGET" = "1" ] || [ "$TARGET" = "true" ] && TARGET=LATEST
_st "restoring $TARGET into $DATADIR"
if /usr/local/bin/walg-restore.sh "$TARGET" "$DATADIR" >> "$STAT" 2>&1; then
  _st "restore complete; WAL replay armed"
  exit 0
fi
_st "FATAL: walg-restore failed"
exit 1
