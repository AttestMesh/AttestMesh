#!/usr/bin/env bash
# ClickHouse Keeper entrypoint (coordination for replicated tables — bundled binary, no
# zookeeper image). Runs in the SIDECAR netns: client :9181, raft :9234, static 3-node raft
# ensemble rendered from the precomputed CH_PEERS. server_id = my 1-based position in
# CH_PEERS, so every node computes a stable, collision-free id with no coordination.
# Raft/keeper traffic rides the attested wireguard mesh unauthenticated (same accepted v1
# deviation as pg-ha's etcd peer traffic). Data persists on /var/lib/clickhouse-keeper.
set -u
CHHA_LOG_TAG=keeper
source /usr/local/bin/chha-common.sh

NODE="${CHHA_NODE_NAME:?CHHA_NODE_NAME not set}"
DATA="${KEEPER_DATA_DIR:-/var/lib/clickhouse-keeper}"
mkdir -p "$DATA/log" "$DATA/snapshots" /run/chha
chown -R clickhouse:clickhouse "$DATA" 2>/dev/null || true

_st "boot: waiting for attestmesh0"
MY_IP="$(wait_for_mesh_ip)"
assert_self_ip "$MY_IP"

MY_ID="$(peer_index "$NODE")"
[ -n "$MY_ID" ] || _die "could not compute server_id for $NODE from CH_PEERS=$CH_PEERS"

RAFT=""
for n in $(peer_names); do
  RAFT="$RAFT
      <server>
        <id>$(peer_index "$n")</id>
        <hostname>$(peer_ip "$n")</hostname>
        <port>9234</port>
      </server>"
done

CFG=/run/chha/keeper.xml
umask 077
cat > "$CFG" <<XML
<clickhouse>
  <logger>
    <level>information</level>
    <log>$(dirname "$STAT")/keeper-$NODE.log</log>
    <errorlog>$(dirname "$STAT")/keeper-$NODE.err.log</errorlog>
    <size>50M</size>
    <count>2</count>
    <console>0</console>
  </logger>
  <listen_host>0.0.0.0</listen_host>
  <keeper_server>
    <tcp_port>9181</tcp_port>
    <server_id>$MY_ID</server_id>
    <log_storage_path>$DATA/log</log_storage_path>
    <snapshot_storage_path>$DATA/snapshots</snapshot_storage_path>
    <coordination_settings>
      <operation_timeout_ms>10000</operation_timeout_ms>
      <session_timeout_ms>30000</session_timeout_ms>
      <raft_logs_level>information</raft_logs_level>
    </coordination_settings>
    <raft_configuration>$RAFT
    </raft_configuration>
  </keeper_server>
</clickhouse>
XML
chown clickhouse:clickhouse "$CFG" 2>/dev/null || true
# The keeper log files live on the shared status volume (TEE blocks container logs) —
# make sure the clickhouse user can write there.
chmod 1777 "$(dirname "$STAT")" 2>/dev/null || true

_st "starting clickhouse-keeper server_id=$MY_ID (client $MY_IP:9181, raft :9234)"
run_as_clickhouse clickhouse-keeper --config-file="$CFG"
