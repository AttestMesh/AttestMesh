#!/bin/sh
# Shared wal-g environment — sourced by the archive_command, the base-backup loop, and restore.
# Storage = Cloudflare R2 (S3 API); secrets (R2_*) arrive as sealed CVM env. The encryption key is the
# CSK-derived WALG_LIBSODIUM_KEY written by walg-csk-key.sh, so backups are bound to the CLUSTER identity
# (any re-provisioned node of the same app_id can re-derive it and decrypt — recovery from total CVM loss).
export AWS_ACCESS_KEY_ID="${R2_ACCESS_KEY_ID:-}"
export AWS_SECRET_ACCESS_KEY="${R2_SECRET_ACCESS_KEY:-}"
export AWS_ENDPOINT="${R2_ENDPOINT:-}"
export AWS_REGION="${R2_REGION:-auto}"
export AWS_S3_FORCE_PATH_STYLE="true"            # R2 needs path-style addressing
export WALG_S3_PREFIX="s3://${R2_BUCKET:-matrix-node-backups}/${BACKUP_PREFIX:-matrix-node}"
export WALG_COMPRESSION_METHOD="${WALG_COMPRESSION_METHOD:-zstd}"
export WALG_LIBSODIUM_KEY_TRANSFORM="hex"
export WALG_LOG_LEVEL="${WALG_LOG_LEVEL:-DEVEL}"   # verbose — captured to the status file for diagnosis
# Local Postgres connection for backup-push bracketing (pg_backup_start/stop). Local socket = trust.
export PGHOST="${PGHOST:-/var/run/postgresql}"
export PGUSER="${PGUSER:-${POSTGRES_USER:-synapse}}"
export PGDATABASE="${PGDATABASE:-${POSTGRES_DB:-synapse}}"
_kf="${WALG_KEY_FILE:-/run/walg/key}"
[ -s "$_kf" ] && export WALG_LIBSODIUM_KEY="$(cat "$_kf")"
