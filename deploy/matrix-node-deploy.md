# Deploying a private Matrix node "this way"

Authoritative guide for standing up a **Matrix homeserver as a full AttestMesh node** on the
self-hosted on-chain dstack box — **private (tailnet-only), confidential, fast, and host-isolated**.
This is the method as of 2026-06-24, after the bridge-networking + login-`well_known` work. For the
blow-by-blow history and every bug behind these decisions, see
[`matrix-node-access-journal.md`](./matrix-node-access-journal.md); the original build journey is in
[`matrix-node-steps-log.md`](./matrix-node-steps-log.md).

Orchestrate it with the Smithers workflow [`workflows/matrix-node.tsx`](./workflows/matrix-node.tsx),
which drives the re-entrant bash driver [`matrix-node.sh`](./matrix-node.sh) (+ the box-side helper
[`matrix-node-box.py`](./matrix-node-box.py)) over the compose
[`compose/matrix-node.yaml`](./compose/matrix-node.yaml).

---

## What you get

One sealed dstack CVM running, in a single docker-compose: the AttestMesh **sidecar**
(`cluster-mesh-agent`, self-registers the node on-chain), **Postgres**, **Synapse**, **nginx**
(reverse proxy + well-known), **Tailscale** (`tailscale serve` terminates HTTPS *inside* the CVM),
and the **matrix-admin-agent** (Synapse admin control plane + LLM bot) behind a **deny-all egress
firewall**. The homeserver is reachable **only** over the tailnet, at
`https://<magicdns-name>.<tailnet>.ts.net`.

```
   Element ──TLS──>  tailscale serve (:443, in the CVM)  ──>  nginx:80  ──>  Synapse:8008
   (tailnet)         terminates HTTPS, cert auto-provisioned        │
                                                                     └─> matrix-admin-agent (egress-locked)
   THE HOST (hypervisor) sees: nothing. No port-map, no plaintext socket to any CVM service.
```

## Design decisions / invariants (the "this way" part)

Each of these is load-bearing — they're why the node is private, fast, and safe. Don't regress them.

1. **Bridge networking, not user/SLIRP.** The box runs CVMs in `[cvm.networking] mode="bridge"` on
   `dstack-br0`, so the CVM is routable and its **own** Tailscale gets a **DIRECT** path (~9 ms) instead
   of a slow DERP relay. `matrix-node.sh` sets `BOX_NET_MODE=bridge` by default. KMS is reached via the
   host DNAT `10.0.2.2:9101` (the RA-TLS cert SANs `10.0.2.2`, **not** `10.0.100.1`). See
   [[dstack-box-bridge-networking]] in memory.
2. **Host-isolation invariant** (the platform's reason for existing): *a private CVM service must never
   be reachable from the host.* `forward_service_enabled=false` (global) + **no `ports:` in the compose**
   + bridge mode ⇒ the host opens nothing toward the CVM. Verified by `matrix-node.sh … verify-isolation`.
3. **Gateway OFF.** Never publish the homeserver to the public dstack gateway. `BOX_GATEWAY_ENABLED=false`
   ⇒ no `*.gateway.attestmesh.xyz` route, no public login endpoint. (Re-enabling it is what originally
   exposed the server to the internet.)
4. **HTTPS terminated in the CVM** via `tailscale serve` (TS_SERVE_CONFIG → `:443` → `127.0.0.1:80`).
   The cert is auto-provisioned for the node's MagicDNS name (`${TS_CERT_DOMAIN}`), so it tracks the
   hostname even though that changes on every fresh-disk roll.
5. **CORS-enabled, self-referential client well-known.** nginx serves `/.well-known/matrix/client` with
   `Access-Control-Allow-Origin: *` **always** and `base_url: https://$host` — so browser discovery
   works and the URL tracks the host the client used. A bare 404 here is CORS-blocked and Element calls
   the server "not valid."
6. **Login `well_known` rewrite — the subtle one.** Synapse echoes `public_baseurl` in the **login
   response's** `m.homeserver` well_known, and a Matrix client *switches its base_url to that*. So
   `public_baseurl` is the sentinel `https://homeserver.invalid/`, and nginx `sub_filter`s
   `homeserver.invalid` → `$host` on `/_matrix` responses (with `Accept-Encoding ""`). The login
   well_known then always points back at the live tailnet URL the client used — never the dead gateway,
   and robust to the per-roll name change. (`server_name` stays `<app_id>.gateway.attestmesh.xyz` so
   MXIDs / on-chain identity are unchanged.)

## Prerequisites

- The self-hosted on-chain box: SSH + sudo (`ubuntu@173.231.234.133`), bridge networking already enabled
  in `vmm.toml` (one-time; see the journal's W1–W4), box deployer key at
  `/root/.attestmesh/base-deployer.json`, the patched dstack MCP at `/opt/dstack-mcp`.
- `contracts/lib/` populated (`forge` available locally); `deploy/env.sh` sourced.
- Secrets, never committed: `RPC_URL`/`BUNDLER_URL` (Alchemy), `GAS_POLICY_ID`, a **reusable** Tailscale
  auth key, GHCR pull creds (`~/.teesql/ghcr-pull.toml`), and the matrix-admin-agent env
  (`BOT_PASSWORD`, `MATRIX_ADMIN_MXIDS`, `LLM_BASE_URL`, `LLM_MODEL=z-ai/glm-5.2`, `LLM_API_KEY`,
  optional `INITIAL_ADMIN`/`MATRIX_ADMIN_SENDERS`).

## Deploy (fresh node + new cluster)

```bash
source deploy/env.sh
TS_AUTHKEY=tskey-…  BOT_PASSWORD=…  LLM_API_KEY=…  LLM_BASE_URL=https://api.redpill.ai/v1 \
  LLM_MODEL=z-ai/glm-5.2  MATRIX_ADMIN_MXIDS=@you:<server>  INITIAL_ADMIN=@you:<server> \
  bunx smithers-orchestrator up deploy/workflows/matrix-node.tsx \
    --input '{"node":"matrix-node","meshCidrIp":"168951808"}'
# resume after fixing a failed step:
#   bunx smithers-orchestrator up deploy/workflows/matrix-node.tsx --run-id <id> --resume true
```

The pipeline (each step shells out to `matrix-node.sh`, logs to `deploy/logs/`, persists state, resumes):

| Step | Does |
|------|------|
| `deploy` | register stock DstackApp + seal env + **bridge** CreateVm (gateway-off), then **wait for Synapse over the tailnet** |
| `cluster` | `DeployCluster` diamond (kmsRootSigner = this box's; seeds compose hash) |
| `patha` | Path-A DstackFacet + a fresh `ClusterMember` impl (the 8453 factory impl lacks `reinitializeFromDstackApp`) |
| `prime` | `addAllowedAppId(cluster, X)` |
| `bind` | `upgradeToAndCall(X, impl, reinitializeFromDstackApp(cluster))` (box deployer key) |
| `verify` | poll `memberIdOf(X)` + Synapse `/versions` over the tailnet |
| `agent` | matrix-admin-agent `/healthz` over the tailnet — confirms admin token + matrix sync + **egress LOCKED** |
| `client` | **login → follow login `well_known` → initial sync = 200** (the exact Element path; catches a bad `public_baseurl`) |
| `isolation` | from the **host**, the CVM's bridge IP must **refuse** `:80/:443/:9100/:9090/:51900` (the host-isolation invariant) |

> The first member of a fresh cluster is the **immutable CSK originator** and the deployed contracts have
> **no `removeMember`**, so the node must be fully working **before** it registers — hence `deploy` waits
> for Synapse before `cluster`/`bind`.

## Day-2: roll a new compose/env (REUSES the app_id → keeps membership + CSK originator)

```bash
source deploy/env.sh
TS_AUTHKEY=…  BOT_PASSWORD=…  LLM_API_KEY=…  LLM_BASE_URL=…  LLM_MODEL=z-ai/glm-5.2 \
  MATRIX_ADMIN_MXIDS=@you:<server>  INITIAL_ADMIN=@you:<server> \
  deploy/matrix-node.sh matrix-node update     # addComposeHash(H') → StopVm → CreateVm(app_id=X)
deploy/matrix-node.sh matrix-node verify-client      # prove the client path still works
deploy/matrix-node.sh matrix-node verify-isolation   # prove the host still can't reach it
```

A roll does a **fresh disk** (intentional wipe), so the node **re-joins the tailnet under a new name**
(`matrix-attestmesh-3` → `-4` → …). The login-well_known rewrite makes any name work; just connect to the
current one (`tailscale status`).

## Connect

Element → homeserver **`https://<current matrix-attestmesh-N>.<tailnet>.ts.net`** (read the current name
from `tailscale status`) → **username `lsdan`** (the localpart, *not* the full `@lsdan:…` MXID, which would
make Element probe the dead gateway domain) → the initial-admin password (`IAPW=` in
`deploy/logs/matrix-node-matrix-node.state`). Your device must be on the tailnet.

## Troubleshooting

- **Element hangs on "Syncing…"** → the login `well_known` is pointing somewhere dead. Run
  `verify-client`; it prints the `well_known.base_url` Element switches to. It must be the live tailnet
  URL. (Root cause history: invariant #6 above.)
- **"not a valid Matrix homeserver"** in a browser → the `/.well-known/matrix/client` CORS header is
  missing (invariant #5). Verify the way the client does — with a browser `Origin`, not bare `curl`.
- **Slow UI** → the CVM is relaying via DERP, i.e. it didn't get a bridge/direct path
  (`tailscale ping <node>` shows `via DERP`). Check bridge networking (invariant #1).
- **The tailnet name keeps changing each roll** → stale offline `matrix-attestmesh-*` nodes hold the
  lower names. A truly stable URL needs predecessor pruning via a Tailscale **API** key (an auth key
  can't delete nodes); the `verify-client` path works regardless of the name.
