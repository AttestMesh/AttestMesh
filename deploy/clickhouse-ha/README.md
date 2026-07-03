# clickhouse-ha

ClickHouse 24.8 server + bundled Keeper + HAProxy image for the AttestMesh ClickHouse HA
cluster (3 CVMs on C3, members `ch1 ch2 ch3`; main tenant: Langfuse analytics). One image,
three container roles selected by entrypoint override in
`deploy/compose/clickhouse-ha-node.yaml` — structural twin of `deploy/pg-ha/`.

## What / why

- **keeper** (`chha-keeper-entrypoint.sh`): clickhouse-keeper standalone, static 3-node raft
  ensemble from `CH_PEERS` (raft `:9234`, client `:9181`), `server_id` = the node's 1-based
  position in `CH_PEERS`. Data on `/var/lib/clickhouse-keeper` (keeper-data volume).
- **server** (`chha-server-entrypoint.sh`): clickhouse-server with config.d/users.d overrides —
  cluster `default` = 1 shard × N replicas over the peers' native ports, keeper = all peers'
  `:9181`, macros `shard=01 replica=<node>`, interserver replication over mesh IPs. The
  default profile does NOT force replicated engines (Langfuse's `CLICKHOUSE_CLUSTER_ENABLED`
  handles ON CLUSTER / ReplicatedMergeTree). Crash reporting/telemetry disabled. Data on
  `/var/lib/clickhouse` (chdata volume).
- **haproxy** (`chha-haproxy-entrypoint.sh`): stable endpoints on every node's mesh IP —
  `:8123` (HTTP) and `:9000` (native) roundrobin over healthy replicas, health =
  `GET /ping` on `:18123` (used as check port for both listeners).

## Ports (mesh IP unless noted)

| Port | Owner | Purpose |
|---|---|---|
| 8123 | haproxy | HTTP (roundrobin over replicas) |
| 9000 | haproxy | native protocol (roundrobin) |
| 18123 | server | actual HTTP port (backends, /ping) |
| 19001 | server | actual native port (backends, remote_servers) |
| 9009 | server | interserver replication (credential-gated) |
| 9181 | keeper | keeper client |
| 9234 | keeper | keeper raft |
| 127.0.0.1:7000 | haproxy | stats |

## Secret derivation

No secrets in the image or env. HKDF-derived from the cluster shared key via the sidecar
gRPC (`chha-common.sh:csk_derive`, identical recipe to pg-ha):

| Label | Use |
|---|---|
| `attestmesh.chha.client.v1` | `default` user password (sha256 in users.d; plaintext only in remote_servers) — any C3 member (e.g. Langfuse) derives it |
| `attestmesh.chha.interserver.v1` | interserver_http_credentials for replication fetches |

One sealed (non-CSK) credential: `CHHA_VERIFY_PASSWORD` — the server entrypoint creates a
`meshverify` user with it, granted `CLUSTER ON *.*`, CREATE/DROP/INSERT/SELECT on
`verify.*`, and SELECT on `system.clusters`/`system.replicas`, so the driver's
verify-ha/verify-failover run from the mesh shell without the CSK.

Keeper raft/client traffic is unauthenticated on the attested wireguard mesh (same accepted
v1 deviation as pg-ha's etcd peer traffic).

## Observability

The TEE blocks container logs, so entrypoints self-report to `/chha-status/state` and the
server/keeper log files are pointed at the same status volume (served over the mesh by the
compose's status-server on `:8009`).

Peer identity comes from `CH_PEERS="ch1=IP,ch2=IP,ch3=IP"` sealed at CreateVm; each
entrypoint hard-fails via `assert_self_ip` if the live `attestmesh0` IP drifts from the
precomputed value.
