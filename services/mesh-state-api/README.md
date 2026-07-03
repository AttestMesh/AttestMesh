# services/mesh-state-api/

Indexed **on-chain mesh-state JSON API** for AttestMesh clusters.

**Spec**: [`docs/specs/mesh-state-api.md`](../../docs/specs/mesh-state-api.md)

It discovers cluster diamonds from `ClusterDiamondFactory.ClusterDeployed` logs,
projects cluster membership/key events into a local JSON index, and serves clean
JSON from that indexed state. It serves the **public / on-chain tier only** — `vm` and `health`
are always present as keys and always `null`. Operator-tier augmentation (VM status,
tailnet liveness) is merged by the consumer that has host/tailnet access (the Box Admin
sidecar), never here. No DB server, no key material, no host/tailnet access.

The default mode is indexed:

- first boot performs the historical scan once;
- the index is stored at `~/.cache/mesh-state-api/index.json`;
- later refreshes scan only blocks after the last indexed block;
- a newly discovered cluster costs one `meshCidr()` read because v1 contracts do
  not emit CIDR in `ClusterDeployed`;
- dashboard requests read the local projection instead of issuing per-cluster
  `listMembers` / `memberById` calls.

## Run

```bash
npm install
RPC_URL=https://base-mainnet.g.alchemy.com/v2/<key> \
CLUSTER_FACTORY_ADDR=0xf6E85fD138E3208d3AAE63ce4E2A33f20e82b9fb \
CLUSTER_FACTORY_START_BLOCK=46868742 \
  LISTEN_ADDR=127.0.0.1:8787 \
  npm start
```

Runs on Node ≥ 20 with native TypeScript execution (no build step). `RPC_URL` is the only
secret env. The factory address and start block are required runtime config; cluster
addresses are never configured directly.

- A reliable RPC endpoint is still needed for historical `getLogs`, but indexed
  mode makes it low-volume after the first scan. `INDEXED_READS=0` restores the
  old live read-through behavior for debugging only.
- `INDEX_REFRESH_MS=300000` controls the background index refresh interval.
- `TIMELINE_ENABLED=0` disables expensive live timeline scans. In indexed mode,
  `/mesh/timeline` is served from the local projection.

## Endpoints (frozen v1.0 — see spec §5)

| Route | Returns |
|---|---|
| `GET /mesh/clusters` | discovered clusters + snapshot summaries |
| `GET /mesh/members` | aggregate cluster meta + full member lists (memberId, keys, mesh IP, appId, `vm`/`health` null) |
| `GET /mesh/members?cluster=0x...` | one cluster's member list |
| `GET /mesh/topology` | aggregate per-cluster nodes + all-pairs edges (full mesh; edge `state: "unknown"` on this tier) |
| `GET /mesh/health` | aggregate on-chain summaries (member count, CSK committed, originator) |
| `GET /mesh/timeline` | aggregate indexed membership-event histories from each discovered cluster deploy block |
| `GET /healthz` | liveness probe (`ok`, `rpcReachable`, `clusterCount`, `cacheAgeMs`) |

Hex casing: `bytes32` lowercase; `address` EIP-55 checksummed; `appId` = `memberContract`
lowercased, no `0x` (the operator join key, since **app_id == ClusterMember address**).

**Node classification is tier-scoped** — on the public tier (`vm`/`health` null for *every*
member) do **not** infer orphan/down/live from `null`. Only a consumer that populates `vm`
applies the orphan (`vm==null`) / down (`vm.status!=running`) / live / "live, liveness
unknown" (`vm.status==running && health==null`) states. See spec §5.1.

## Test / typecheck

```bash
npm test        # node --test — pure decode/format + response-shaping units
npm run typecheck
```

## Deploy

- **Box Admin (operator):** `127.0.0.1:8787` on `myserver01`; the Box Admin sidecar fetches
  it server-side and merges `vm`+`health`.
- **Public console:** the same image behind `nginx-gw` (`LISTEN_ADDR=0.0.0.0:8787`), identical
  JSON, `vm`/`health` still null. See `Dockerfile`.
