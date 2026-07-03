# redis-ha

Redis 7.4 + Sentinel + HAProxy image for the AttestMesh Redis HA cluster (3 CVMs on C3,
members `r1 r2 r3`). One image, three container roles selected by entrypoint override in
`deploy/compose/redis-ha-node.yaml` — structural twin of `deploy/pg-ha/`.

## What / why

- **redis** (`redisha-redis-entrypoint.sh`): redis-server on `:6380` in the sidecar netns,
  AOF on, data on the persistent `/data` volume. First peer bootstraps as master when
  `REDISHA_BOOTSTRAP=new` and the disk is empty; others `REPLICAOF <first-peer>:6380`.
  Existing data ⇒ plain restart; sentinels reconcile roles.
- **sentinel** (`redisha-sentinel-entrypoint.sh`): `:26379`, monitors master name
  `redisha` with quorum 2, owns failover. Its self-rewritten conf persists on
  `/sentinel-data`; a fresh conf probes peers for the CURRENT master before rendering.
- **haproxy** (`redisha-haproxy-entrypoint.sh`): stable endpoints on every node's mesh IP —
  `:6379` → current master, `:6381` → replicas (roundrobin) — via a tcp-check dialogue
  (AUTH → `INFO replication` → expect `role:master`/`role:slave`) against each `<peer>:6380`.

## Ports (mesh IP unless noted)

| Port | Owner | Purpose |
|---|---|---|
| 6379 | haproxy | current master (writes) |
| 6380 | redis | actual redis-server (backends, replication) |
| 6381 | haproxy | replicas (reads, roundrobin) |
| 26379 | sentinel | sentinel gossip + failover |
| 127.0.0.1:7000 | haproxy | stats |

## Secret derivation

No secrets in the image or env. Everything is HKDF-derived from the cluster shared key via
the sidecar gRPC (`redisha-common.sh:csk_derive`, identical recipe to pg-ha):

| Label | Use |
|---|---|
| `attestmesh.redisha.auth.v1` | `requirepass` + `masterauth` + sentinel `auth-pass` + HAProxy check AUTH — and the client password any C3 member derives |

One sealed (non-CSK) credential: `REDISHA_VERIFY_PASSWORD` — the redis entrypoint creates a
`meshverify` ACL user with it (`~verify:* +ping +info +get +set +del`) and sentinel uses it
as its own `requirepass` (redis ≥6.2 also authenticates sentinel-to-sentinel with it), so
the driver's verify-ha/verify-failover run from the mesh shell without the CSK.

## Observability

The TEE blocks container logs, so all entrypoints self-report phase/status lines to
`/redisha-status/state` (served over the mesh by the compose's status-server on `:8009`).

Peer identity comes from `REDIS_PEERS="r1=IP,r2=IP,r3=IP"` sealed at CreateVm; each
entrypoint hard-fails via `assert_self_ip` if the live `attestmesh0` IP drifts from the
precomputed value.
