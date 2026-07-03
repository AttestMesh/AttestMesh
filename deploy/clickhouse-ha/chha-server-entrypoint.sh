#!/usr/bin/env bash
# clickhouse-server entrypoint. Runs in the SIDECAR netns; HTTP on 18123 and native on 19001
# (HAProxy owns the mesh IP's 8123/9000 in the same netns). Renders config.d/users.d overrides
# on top of the stock config: keeper ensemble + remote_servers cluster "default"
# (1 shard × N replicas) from the precomputed CH_PEERS, interserver replication over mesh IPs,
# and CSK-derived credentials — every node computes identical values with no secret exchange
# (pg-ha pattern). The default profile does NOT force replicated table engines: Langfuse's
# CLICKHOUSE_CLUSTER_ENABLED drives ON CLUSTER / ReplicatedMergeTree itself. All telemetry
# and crash reporting is disabled. Data persists on /var/lib/clickhouse (chdata volume).
set -u
CHHA_LOG_TAG=server
source /usr/local/bin/chha-common.sh

NODE="${CHHA_NODE_NAME:?CHHA_NODE_NAME not set}"
DATA="${CH_DATA_DIR:-/var/lib/clickhouse}"
mkdir -p "$DATA" /run/chha
chown -R clickhouse:clickhouse "$DATA" 2>/dev/null || true

_st "boot: waiting for attestmesh0"
MY_IP="$(wait_for_mesh_ip)"
assert_self_ip "$MY_IP"

_st "deriving CSK-bound credentials"
CLIENT_PW="$(csk_derive attestmesh.chha.client.v1)" || exit 1
INTER_PW="$(csk_derive attestmesh.chha.interserver.v1)" || exit 1
CLIENT_PW_SHA="$(printf '%s' "$CLIENT_PW" | sha256sum | cut -d' ' -f1)"
# Driver-side verification credential (sealed env, NOT the CSK): the mesh shell runs
# verify-ha/verify-failover as the `meshverify` user, restricted to the verify database
# (+ CLUSTER for ON CLUSTER DDL, SELECT on system.clusters/system.replicas) —
# deploy/clickhouse-ha-node.sh.
VERIFY_PW="${CHHA_VERIFY_PASSWORD:?CHHA_VERIFY_PASSWORD not set}"
VERIFY_PW_SHA="$(printf '%s' "$VERIFY_PW" | sha256sum | cut -d' ' -f1)"

KEEPER_NODES=""
REPLICAS=""
for n in $(peer_names); do
  ip="$(peer_ip "$n")"
  KEEPER_NODES="$KEEPER_NODES
    <node>
      <host>$ip</host>
      <port>9181</port>
    </node>"
  REPLICAS="$REPLICAS
        <replica>
          <host>$ip</host>
          <port>19001</port>
          <user>default</user>
          <password>$CLIENT_PW</password>
        </replica>"
done

umask 077
mkdir -p /etc/clickhouse-server/config.d /etc/clickhouse-server/users.d
CFG=/etc/clickhouse-server/config.d/attestmesh.xml
cat > "$CFG" <<XML
<clickhouse>
  <logger>
    <level>information</level>
    <log>$(dirname "$STAT")/clickhouse-$NODE.log</log>
    <errorlog>$(dirname "$STAT")/clickhouse-$NODE.err.log</errorlog>
    <size>100M</size>
    <count>2</count>
    <console>0</console>
  </logger>
  <listen_host>0.0.0.0</listen_host>
  <http_port>18123</http_port>
  <tcp_port>19001</tcp_port>
  <mysql_port remove="remove"/>
  <postgresql_port remove="remove"/>
  <interserver_http_port>9009</interserver_http_port>
  <interserver_http_host>$MY_IP</interserver_http_host>
  <interserver_http_credentials>
    <user>interserver</user>
    <password>$INTER_PW</password>
  </interserver_http_credentials>
  <path>$DATA/</path>
  <tmp_path>$DATA/tmp/</tmp_path>
  <user_files_path>$DATA/user_files/</user_files_path>
  <format_schema_path>$DATA/format_schemas/</format_schema_path>
  <zookeeper>$KEEPER_NODES
  </zookeeper>
  <macros>
    <shard>01</shard>
    <replica>$NODE</replica>
  </macros>
  <remote_servers replace="replace">
    <default>
      <shard>
        <internal_replication>true</internal_replication>$REPLICAS
      </shard>
    </default>
  </remote_servers>
  <distributed_ddl>
    <path>/clickhouse/task_queue/ddl</path>
  </distributed_ddl>
  <!-- No phoning home from inside the CVM. -->
  <send_crash_reports>
    <enabled>false</enabled>
  </send_crash_reports>
</clickhouse>
XML
USERS=/etc/clickhouse-server/users.d/attestmesh.xml
cat > "$USERS" <<XML
<clickhouse>
  <users>
    <default>
      <password remove="remove"/>
      <password_sha256_hex>$CLIENT_PW_SHA</password_sha256_hex>
      <networks>
        <ip>::/0</ip>
      </networks>
      <profile>default</profile>
      <access_management>1</access_management>
    </default>
    <meshverify>
      <password_sha256_hex>$VERIFY_PW_SHA</password_sha256_hex>
      <networks>
        <ip>::/0</ip>
      </networks>
      <profile>default</profile>
      <grants>
        <query>GRANT CLUSTER ON *.*</query>
        <query>GRANT CREATE DATABASE, CREATE TABLE, DROP DATABASE, DROP TABLE, INSERT, SELECT ON verify.*</query>
        <query>GRANT SELECT ON system.clusters</query>
        <query>GRANT SELECT ON system.replicas</query>
      </grants>
    </meshverify>
  </users>
</clickhouse>
XML
chown clickhouse:clickhouse "$CFG" "$USERS" 2>/dev/null || true
chmod 600 "$CFG" "$USERS"
# Server logs live on the shared status volume (TEE blocks container logs).
chmod 1777 "$(dirname "$STAT")" 2>/dev/null || true

_st "starting clickhouse-server (http :18123, native :19001, interserver $MY_IP:9009, replica=$NODE)"
run_as_clickhouse clickhouse-server --config-file=/etc/clickhouse-server/config.xml
