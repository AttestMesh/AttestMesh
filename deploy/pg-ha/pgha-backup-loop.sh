#!/usr/bin/env bash
# Primary-only base-backup loop. Same cadence/retention as walg-backup-loop.sh, but every pg-ha
# node runs it and only the CURRENT PRIMARY pushes (skips while pg_is_in_recovery() is true), so
# exactly one node writes to the shared BACKUP_PREFIX regardless of failovers. WAL between bases
# ships via archive_command, which Postgres only runs outside recovery.
set -u
INTERVAL="${BACKUP_BASE_INTERVAL_SECONDS:-21600}"     # 6h
RETAIN="${BACKUP_RETAIN_FULL:-5}"
KEY_FILE="${WALG_KEY_FILE:-/run/pgha/walg.key}"
PGDATA_DIR="${PGDATA:-/var/lib/postgresql/data}"
STAT="${WALG_STATUS_FILE:-/pgha-status/walg}"; mkdir -p "$(dirname "$STAT")" 2>/dev/null || true
_st() { echo "$(date -u +%FT%TZ) base: $*" >> "$STAT" 2>/dev/null || true; }

export PGHOST=/var/run/postgresql PGPORT=5434 PGUSER=postgres PGDATABASE=postgres

_st "loop start; waiting for CSK key + live postgres"
while [ ! -s "$KEY_FILE" ]; do sleep 5; done
until pg_isready -q 2>/dev/null; do sleep 10; done
_st "ready (base every ${INTERVAL}s, retain ${RETAIN}, primary-only)"
while :; do
  . /usr/local/bin/walg-env.sh
  in_recovery="$(psql -tAc 'select pg_is_in_recovery()' 2>/dev/null | tr -d '[:space:]')"
  if [ "$in_recovery" != "f" ]; then
    _st "replica (in_recovery=${in_recovery:-?}) — skipping backup-push"
  else
    _st "primary — backup-push starting (timeout 600s)"
    out="$(timeout 600 wal-g backup-push "$PGDATA_DIR" 2>&1)"; rc=$?
    if [ "$rc" -eq 0 ]; then
      _st "backup-push OK"
      wal-g delete retain FIND_FULL "$RETAIN" --confirm >/dev/null 2>&1 || true
    else
      _st "backup-push FAILED rc=$rc :: $(printf '%s' "$out" | tr '\n' ' ' | tail -c 420)"
    fi
  fi
  sleep "$INTERVAL"
done
