#!/usr/bin/env bash
# Patroni post_init: runs once on the bootstrap leader right after initdb, with the local
# superuser connection URL as $1. Creates the low-privilege verification user the deploy
# driver uses for end-to-end SQL checks from the mesh shell (docs/specs/pg-ha.md §6).
set -u
STAT="${PGHA_STATUS_FILE:-/pgha-status/state}"
_st() { echo "$(date -u +%FT%TZ) post-init: $*" >> "$STAT" 2>/dev/null || true; }

if [ -z "${PGHA_VERIFY_PASSWORD:-}" ]; then
  _st "PGHA_VERIFY_PASSWORD not set — skipping meshverify user"
  exit 0
fi

# Idempotent: Patroni runs post_init after EVERY bootstrap method, including walg_restore,
# where the restored base backup ALREADY contains this role + schema. A bare CREATE ROLE would
# error and make Patroni abort + wipe the data dir, looping disaster recovery forever.
psql "$1" -v ON_ERROR_STOP=1 \
  -v pw="$PGHA_VERIFY_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE meshverify LOGIN PASSWORD %L', :'pw')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'meshverify')
\gexec
CREATE SCHEMA IF NOT EXISTS verify AUTHORIZATION meshverify;
SQL
rc=$?
[ $rc -eq 0 ] && _st "meshverify user + verify schema ensured" || _st "FAILED rc=$rc"
exit $rc
