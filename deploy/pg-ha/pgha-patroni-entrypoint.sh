#!/usr/bin/env bash
# Patroni + Postgres entrypoint. Runs in the SIDECAR netns; Postgres listens on 5434 (HAProxy owns
# the mesh IP's 5432/5433 in the same netns). All cluster-wide credentials are HKDF-derived from
# the CSK — every pg node computes identical values with no secret exchange (docs/specs/pg-ha.md §4).
# WAL-G archiving/base-backups are on when BACKUP_ENABLED=true; only the primary ships anything.
set -u
PGHA_LOG_TAG=patroni
source /usr/local/bin/pgha-common.sh

NODE="${PGHA_NODE_NAME:?PGHA_NODE_NAME not set}"
PGDATA_DIR="${PGDATA:-/var/lib/postgresql/data}"
MESH_CIDR="${PGHA_MESH_CIDR:-10.18.0.0/16}"
RUN_DIR=/run/pgha
SECRETS_DIR="${PGHA_SECRETS_DIR:-/pgha-secrets}"
mkdir -p "$RUN_DIR" "$SECRETS_DIR" /var/run/postgresql "$PGDATA_DIR"
chown postgres:postgres "$RUN_DIR" /var/run/postgresql "$PGDATA_DIR"
# The pgdata volume mount point arrives 0755 — initdb fixes that on the leader, but
# pg_basebackup does NOT on replicas, and postgres then refuses to start ("data directory
# has invalid permissions"). Enforce 0700 up front; idempotent across rolls.
chmod 700 "$PGDATA_DIR"
# Patroni execs its bootstrap/post_init commands AS postgres, but the shared status file may
# have been created root-owned by an earlier root-phase entrypoint (etcd/this script). Make
# the status area world-appendable so the walg_restore bootstrap + post_init can self-report
# (otherwise their `>> $STAT` redirect fails and, e.g., DR restore silently never runs).
mkdir -p "$(dirname "$STAT")"
chmod 1777 "$(dirname "$STAT")" 2>/dev/null || true
touch "$STAT" 2>/dev/null && chmod 666 "$STAT" 2>/dev/null || true
mkdir -p "$(dirname "$STAT")/pglog"
chown postgres:postgres "$(dirname "$STAT")/pglog"

_st "boot: waiting for attestmesh0"
MY_IP="$(wait_for_mesh_ip)"
assert_self_ip "$MY_IP"

_st "deriving CSK-bound credentials"
SUPW="$(csk_derive attestmesh.pgha.superuser.v1)" || exit 1
REPPW="$(csk_derive attestmesh.pgha.replication.v1)" || exit 1
RWPW="$(csk_derive attestmesh.pgha.rewind.v1)" || exit 1
APIPW="$(csk_derive attestmesh.pgha.patroni-api.v1)" || exit 1
ETCDPW="$(csk_derive attestmesh.pgha.etcd.v1)" || exit 1

# Hand the admin agent (own netns, same CVM) what it needs: DB superuser + Patroni REST cred.
umask 027
printf '%s' "$SUPW" > "$SECRETS_DIR/pg-superuser"
printf '%s' "$APIPW" > "$SECRETS_DIR/patroni-api"
chmod 644 "$SECRETS_DIR/pg-superuser" "$SECRETS_DIR/patroni-api"

# Explicit former-primary recovery mode. Patroni normally performs this same
# single-user crash replay, but a stuck inherited stdin can leave it waiting
# forever. A recovery boot feeds EOF deliberately, disables archiving, records
# control-state evidence, and never starts the HA daemon. The operator must then
# roll the node normally; no WAL reset or data rewrite is performed here.
if [ "${PGHA_CRASH_RECOVERY_ONLY:-false}" = true ]; then
  RLOG="$(dirname "$STAT")/crash-recovery-$NODE.log"
  _st "explicit crash recovery: starting single-user replay with archive_command=false"
  set +e
  timeout 900 gosu postgres /usr/lib/postgresql/16/bin/postgres \
    --single -D "$PGDATA_DIR" -c archive_mode=on -c archive_command=false template1 \
    </dev/null >>"$RLOG" 2>&1
  rc=$?
  set -e
  /usr/lib/postgresql/16/bin/pg_controldata "$PGDATA_DIR" >>"$RLOG" 2>&1 || true
  _st "explicit crash recovery: finished rc=$rc; see $RLOG"
  [ "$rc" -eq 0 ] || exit "$rc"
  exec sleep infinity
fi

ARCHIVE_CMD=/bin/true
if [ "${BACKUP_ENABLED:-false}" = "true" ]; then
  export WALG_KEY_FILE="${WALG_KEY_FILE:-$RUN_DIR/walg.key}"
  export WALG_STATUS_FILE="${WALG_STATUS_FILE:-/pgha-status/walg}"
  csk_derive attestmesh.pgha.walg.v1 > "$WALG_KEY_FILE" || _die "WAL-G key derivation failed"
  [ -s "$WALG_KEY_FILE" ] || _die "WAL-G key file empty after derivation"
  chmod 600 "$WALG_KEY_FILE"; chown postgres:postgres "$WALG_KEY_FILE"
  ARCHIVE_CMD="/usr/local/bin/pgha-walg-archive %p"
  _st "backups enabled: prefix=${BACKUP_PREFIX:-pg-ha} bucket=${R2_BUCKET:-?}"
  /usr/local/bin/pgha-backup-loop.sh &
  /opt/patroni/bin/python /usr/local/bin/pgha-logical-backups.py &
fi

ETCD_HOSTS=""
for n in $(peer_names); do
  ETCD_HOSTS="${ETCD_HOSTS:+$ETCD_HOSTS,}$(peer_ip "$n"):2379"
done

BOOTSTRAP_METHOD=initdb
if [ -n "${BACKUP_RESTORE:-}" ] && [ ! -s "$PGDATA_DIR/PG_VERSION" ]; then
  # Full-cluster disaster recovery: the bootstrap leader restores from R2 instead of initdb.
  BOOTSTRAP_METHOD=walg_restore
  _st "BACKUP_RESTORE=$BACKUP_RESTORE -> bootstrap method walg_restore"
fi

CONF="$RUN_DIR/patroni.yml"
cat > "$CONF" <<YAML
scope: pg-ha
name: $NODE

restapi:
  listen: 0.0.0.0:8008
  connect_address: $MY_IP:8008
  authentication:
    username: patroni
    password: $APIPW

etcd3:
  hosts: $ETCD_HOSTS
  username: root
  password: $ETCDPW

bootstrap:
  method: $BOOTSTRAP_METHOD
  walg_restore:
    command: /usr/local/bin/pgha-walg-bootstrap.sh
    keep_existing_recovery_conf: true
  dcs:
    ttl: 30
    loop_wait: 10
    retry_timeout: 10
    maximum_lag_on_failover: 1048576
    synchronous_mode: false
    postgresql:
      use_pg_rewind: true
      parameters:
        wal_level: replica
        hot_standby: "on"
        max_wal_senders: 10
        max_replication_slots: 10
        wal_keep_size: ${BACKUP_WAL_KEEP_SIZE:-512MB}
        archive_mode: "on"
        archive_command: $ARCHIVE_CMD
        archive_timeout: ${BACKUP_ARCHIVE_TIMEOUT:-300}
  initdb:
    - encoding: UTF8
    - locale: C
    - data-checksums
  post_init: /usr/local/bin/pgha-post-init.sh

postgresql:
  listen: 0.0.0.0:5434
  connect_address: $MY_IP:5434
  data_dir: $PGDATA_DIR
  bin_dir: /usr/lib/postgresql/16/bin
  unix_socket_directories: /var/run/postgresql
  pgpass: $RUN_DIR/pgpass
  parameters:
    # The TEE blocks container stdout, so postgres logs to the shared status volume —
    # readable over the mesh via the :8009 status server (spec §8 observability rule).
    logging_collector: "on"
    log_directory: /pgha-status/pglog
    log_filename: postgresql-%a.log
    log_truncate_on_rotation: "on"
    log_rotation_age: 1d
  authentication:
    superuser:
      username: postgres
      password: $SUPW
    replication:
      username: replicator
      password: $REPPW
    rewind:
      username: rewind_user
      password: $RWPW
  pg_hba:
    - local all all trust
    - host all all 127.0.0.1/32 trust
    # Replication connections match ONLY lines whose db field is replication; the
    # localhost entries above do NOT cover them, and Patroni checks the replication
    # credential against the local postgres (and pg_rewind needs it after failovers).
    - local replication all trust
    - host replication replicator 127.0.0.1/32 scram-sha-256
    - host replication replicator $MESH_CIDR scram-sha-256
    - host replication rewind_user $MESH_CIDR scram-sha-256
    - host all all $MESH_CIDR scram-sha-256
    - host all all 172.16.0.0/12 scram-sha-256

tags: {}
YAML
chmod 600 "$CONF"
chown postgres:postgres "$CONF"

_st "starting patroni (etcd3=$ETCD_HOSTS, connect=$MY_IP:5434, bootstrap=$BOOTSTRAP_METHOD)"
# Patroni's stdout/stderr carry its own errors AND postmaster stderr from before the
# logging collector engages — the TEE blocks container stdout, so keep them on the
# mesh-readable status volume instead.
PLOG="$(dirname "$STAT")/patroni-$NODE.log"
touch "$PLOG"; chown postgres:postgres "$PLOG"
exec gosu postgres bash -c "exec patroni '$CONF' >> '$PLOG' 2>&1"
