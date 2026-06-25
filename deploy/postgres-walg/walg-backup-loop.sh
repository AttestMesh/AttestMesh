#!/bin/sh
# Periodic base backups (wal-g backup-push) + retention. Base every BACKUP_BASE_INTERVAL_SECONDS (6h),
# keeping BACKUP_RETAIN_FULL full backups (+ the WAL after the oldest) — sized to cover the WAL window so
# PITR can land anywhere in the last ~24h. WAL between bases is shipped continuously by the archive_command.
# Status/errors go to the status file (nginx /_backup/status); the TEE blocks container logs.
set -u
INTERVAL="${BACKUP_BASE_INTERVAL_SECONDS:-21600}"     # 6h
RETAIN="${BACKUP_RETAIN_FULL:-5}"
KEY_FILE="${WALG_KEY_FILE:-/run/walg/key}"
PGDATA_DIR="${PGDATA:-/var/lib/postgresql/data}"
STAT="${WALG_STATUS_FILE:-/walg-status/state}"; mkdir -p "$(dirname "$STAT")" 2>/dev/null || true
_st() { echo "$(date -u +%FT%TZ) base: $*" >> "$STAT" 2>/dev/null || true; }

_st "loop start; waiting for CSK key + live postgres"
while [ ! -s "$KEY_FILE" ]; do sleep 5; done
until pg_isready -q 2>/dev/null; do sleep 5; done
_st "ready (base every ${INTERVAL}s, retain ${RETAIN})"
while :; do
  . /usr/local/bin/walg-env.sh
  bl="$(timeout 45 wal-g backup-list 2>&1)"; _st "R2 check (backup-list): $(printf '%s' "$bl" | tr '\n' ' ' | tail -c 220)"
  _st "backup-push starting (timeout 600s)"
  out="$(timeout 600 wal-g backup-push "$PGDATA_DIR" 2>&1)"; rc=$?
  if [ "$rc" -eq 0 ]; then
    _st "backup-push OK"
    wal-g delete retain FIND_FULL "$RETAIN" --confirm >/dev/null 2>&1 || true
  elif [ "$rc" -eq 124 ]; then
    _st "backup-push TIMED OUT (600s) :: $(printf '%s' "$out" | tr '\n' ' ' | tail -c 420)"
  else
    _st "backup-push FAILED rc=$rc :: $(printf '%s' "$out" | tr '\n' ' ' | tail -c 460)"
  fi
  sleep "$INTERVAL"
done
