# Matrix node on the self-hosted on-chain dstack box — STEPS LOG

Running log of every step taken to deploy a **Matrix homeserver as a full AttestMesh node on a
brand-new ClusterDiamond** on the self-hosted on-chain dstack box (`attestmesh.xyz`), plus the
in-place hardening (Tailscale + iptables). Raw material for distilling a **repeatable flow**
(see the last section). Chronological + honest about dead-ends, because the dead-ends are what the
repeatable flow must avoid.

Date: 2026-06-19. Chain: Base mainnet (8453).

---

## 0. Environment & key facts

- **Box**: `ubuntu@173.231.234.133` (tailnet `myserver01.tail39cb2e.ts.net`). Passwordless `sudo`.
  Runs dstack-vmm + on-chain KMS (DstackKms on Base `0xceb327511ea20da53d809c87d7b316435ae1bcf1`,
  k256 root `022f3f82…`) + dstack-gateway + the admin MCP server.
- **Deploy via** `mcp__dstack__deploy_app` (MCP server `/opt/dstack-mcp/mcp_dstack.py`, systemd
  `dstack-mcp.service`). It builds an app-compose, **registers a stock DstackApp on Base**
  (`script/Manage.s.sol:DeployApp`, box deployer key), and CreateVm. ~$0.001 gas/call.
- **Box deployer / stock-app owner** = `0x91736295e68Df649A7daF2B8B1DF6140C15858BD`
  (key root-only `/root/.attestmesh/base-deployer.json`, `[{"private_key":…}]`; foundry at
  `/root/.foundry/bin`). Used for the on-box `upgradeToAndCall` step.
- **AttestMesh deployer (this machine)** = global-deployer `0x60b174704AdAf2b0BF87B426B364D6EbD81818E1`
  (`deploy/env.sh` → `$PRIVATE_KEY`, `$RPC_URL`=Alchemy). clusterOwner + cluster-gate txs.
- **New cluster kmsRootSigner** = `0x7fa63d99495be2129cf28eee54e2ef2724e3aa2e` (derived from THIS
  box's KMS root — NOT Phala's `0x52d3cf51…` that `deploy/env.sh` hardcodes). The cluster JSON
  must set this explicitly.
- **Base infra (`contracts/script/deployments/8453.json`)**: clusterDiamondFactory
  `0xf6E85fD138E3208d3AAE63ce4E2A33f20e82b9fb`, clusterMemberFactory
  `0xFf9f438EFdAa197f4ae59D1d711C4f09640b8417`, indexerRegistry
  `0xbC003686943fB957100E517D3CEf66c52B5CDdBf`. (clusterMemberImpl `0xd05223…` is the FACTORY-path
  impl — do NOT use for Path-A, see Bug 3.)
- **Secrets** (sealed into the CVM via `deploy_app(env=…)`, never in the compose/repo):
  `RPC_URL`,`BUNDLER_URL`=`https://base-mainnet.g.alchemy.com/v2/<key>` (`~/.teesql/alchemy-api.key`),
  `GAS_POLICY_ID` (`~/.teesql/alchemy-policy.id`), `POSTGRES_PASSWORD` (generated),
  `TS_AUTHKEY` (operator-provided), `DSTACK_DOCKER_{REGISTRY,USERNAME,PASSWORD}`
  (`~/.teesql/ghcr-pull.toml`; dmvt token, can pull `attestmesh/*`). Tool auto-injects `APP_ID`.
- Artifacts: compose `deploy/compose/matrix-node.yaml`; cluster configs
  `contracts/script/clusters/matrix-node{,-2,-3}.json`.

---

## 1. Chronological steps + outcomes

1. **Pre-flight (read-only)**: confirmed patched `deploy_app` `env` param live in schema; box healthy
   (`host_status`/`kms_info`, KMS Base 8453, k256 `022f3f82…`); secret files present; SSH+sudo to box;
   box deployer key present. ✓
2. **deploy_app #1** (`ports` incl. wg `tcp:…:51901:51900`) → **400 "Port mapping is not allowed for
   tcp:51901"** (Bug 1). On-chain DstackApp got registered first (dangling).
3. Diagnosed: reproduced CreateVm out-of-band against the dangling app_id to read the swallowed body;
   found VMM `/var/lib/dstack/vmm.toml` `[cvm.port_mapping] range = 1–20000`. Dropped the wg host-map.
4. **deploy_app #2** (ports 8080,9091 only) → success, app `0xdd69…`, but CVM **`failed to start
   containers`**: `ghcr.io/attestmesh/cluster-mesh-agent:latest … unauthorized` (Bug 2). Private image.
5. Found `~/.teesql/ghcr-pull.toml` (dmvt token CAN pull `attestmesh/*`). dstack guest
   `basefiles/app-compose.sh` **sources `.pre_launch_script`** with the decrypted env loaded
   (`EnvironmentFile=/dstack/.host-shared/.decrypted-env`). → **MCP PATCH #1** (docker-login
   pre_launch_script). Restarted `dstack-mcp.service`.
6. **deploy_app #3** (8 env keys incl. DSTACK_DOCKER_*) → app `0x53e2…`; docker login + pull
   succeeded, but **synapse-init exited 1** after ~300s (app_id-resolve loop exhausted; Bug 4).
7. Root cause: compose's `synapse-init` checks `$APP_ID` first but nothing set it; its socket
   fallback uses GET (dstack prpc is POST). → **MCP PATCH #2** (auto-inject `APP_ID`, key measured /
   value sealed) + compose `synapse-init environment: - APP_ID=${APP_ID}`. Restarted MCP.
8. **deploy_app #4** → app `0x6363…`; synapse-init passed app_id but exited fast — was actually a
   LATER bug masked, see #11. (At this point also deployed cluster + gate + tried the bind.)
9. **DeployCluster C** `0x342b676…` (`forge script DeployCluster --broadcast`; needed
   `forge`+libs: `lib/` was empty → `git clone` OZ 5.1 / OZ-upgradeable 5.1 / solidstate v0.0.61 /
   account-abstraction v0.7 / forge-std into `contracts/lib/`; per `contracts/README.md`).
10. **Bind step (upgradeToAndCall) reverted `FailedCall`** — clusterMemberImpl `0xd05223…` LACKS
    `reinitializeFromDstackApp` (selector 319e8561) (Bug 3). Ran `onchain.sh patha-upgrade <cluster>`
    which deploys a **fresh ClusterMember impl + Path-A DstackFacet** and cuts it in. Used THAT impl.
11. After fixing the impl + re-priming, the live sidecar **registered** but synapse stayed **502**
    (`/_matrix`): reproduced synapse on the box (throwaway postgres) → `generate_signing_key.py: not
    found` (it's `python -m synapse._scripts.generate_signing_key`), then after that
    `PermissionError: '/media_store'` → **add `media_store_path: /data/media_store`** (Bug 5).
12. **Orphan trap**: a broken-Matrix CVM had already **registered as member#0 = CSK originator**
    (`bringup.rs csk_once`: originator = `memberIds[0]`). Deployed contracts have **NO `removeMember`**
    (unmerged PR #3) → the orphan can't be removed. Operator: **"just start a new cluster."**
13. **Cluster C2** `0xdf5eb5a…` (sole member, clean) — but its node W `0xD6aa247…` hit the
    media_store bug (synapse crash-loop). Compose-fix → new compose_hash → tainted member#0 again.
14. **Cluster C3 `0x5ab4706fCa998A0792E5c06432b13c73c54E4557`** — the FINAL clean one. Flow:
    deploy node CVM (fully-fixed compose) → DeployCluster C3 (mesh 10.18.0.0/16, initialComposeHashes
    seeded) → `patha-upgrade C3` (facet `0xb3aa26…` + impl `0xf3f594bdb5cc2d4a56e79fb6d4c616d65b0fbb7d`)
    → `addAllowedAppId(W3)` → **verify node synapse live FIRST** → bind on box (`upgradeToAndCall(impl,
    reinitializeFromDstackApp(C3))`) → sidecar registers as member#0 → **CSK originator**
    (`csk_acquired:true, phase:waiting-peers`). Node **W3 `0xF847Dd18F55aBB9C5d62BD60A3c2bD4a7Ee6555A`**,
    compose_hash `140edc79…`. Matrix live publicly at
    `https://f847dd18f55abb9c5d62bd60a3c2bd4a7ee6555a.gateway.attestmesh.xyz`. ✓ VERIFIED.
15. **Tailscale was single-use-key-exhausted** (consumed by an abandoned CVM) → didn't join. Operator
    gave a **reusable** key + asked for **iptables to allow only Matrix over the tailnet**.
16. **In-place hardening (KEEP C3, reuse W3's app_id)** — to avoid yet another cluster:
    - compose: tailscale `TS_EXTRA_ARGS=--netfilter-mode=off` + new `ts-firewall` container (alpine
      + iptables, shares nginx netns, NET_ADMIN) allowing only `tailscale0 → tcp/80` (+est/icmp),
      DROP rest + DROP forward.
    - computed new compose_hash `cbf1732f…` (out-of-band, dummy secrets) → `addComposeHash(C3, …)`.
    - stopped old W3 CVM (kept for rollback), ran `/tmp/reuse_deploy.py` on box: builds the SAME
      app-compose, seals env (real secrets via in-memory `sudo E_*=…`, the new TS key), **CreateVm
      with `app_id = W3`** (skips on-chain registration). → new vm `42d49603…`, CreateVm 200.
    - **W3 membership unchanged** (`memberCount=1`, same memberId) ✓; synapse came back ✓; sidecar
      `csk_acquired:true` (originator re-derived) ✓.
    - **OPEN**: the new CVM's tailscale still shows `matrix-attestmesh` OFFLINE — diagnosing now.

---

## 2. Bugs found & fixes (the repeatable flow must encode these)

| # | Symptom | Root cause | Fix |
|---|---------|-----------|-----|
| 1 | CreateVm 400 "Port mapping not allowed for tcp:51901" | VMM `[cvm.port_mapping] range 1–20000` | Don't host-map ports >20000; wg reaches peers via gateway subdomain, not a host map |
| 2 | `docker compose up` → `ghcr.io/attestmesh/… unauthorized` | sidecar image is PRIVATE | MCP patch: `pre_launch_script` does `docker login` from sealed `DSTACK_DOCKER_*` |
| 3 | `upgradeToAndCall` reverts `FailedCall` (empty) | `0xd05223…` lacks `reinitializeFromDstackApp` | run `onchain.sh patha-upgrade <cluster>`; use the impl it prints |
| 4 | synapse-init exits 1 after ~300s | app_id not in container env; socket probe uses GET | MCP patch: auto-inject `APP_ID`; compose passes `- APP_ID=${APP_ID}` |
| 5 | `/_matrix` → 502 (synapse crash-loop) | missing `media_store_path` → `/media_store` perm-denied as uid 991 | add `media_store_path: /data/media_store` to homeserver.yaml |
| — | tainted cluster (can't fix member#0) | first registrant = immutable CSK originator; no `removeMember` | deploy the FULLY-VERIFIED node and let it register FIRST; else fresh cluster |

## 3. MCP tool patches applied to `/opt/dstack-mcp/mcp_dstack.py`

Builder did the original `env`-sealing patch. I added (backups `.bak.prelaunch.*`, `.bak.appid.*`):
1. **docker-login pre_launch_script** when `DSTACK_DOCKER_PASSWORD` in env.
2. **APP_ID auto-inject** (`allowed_envs += APP_ID`; seal `APP_ID = <registered app_id>`).
- Schema unchanged by both → only a `systemctl restart dstack-mcp.service` needed (no Claude restart).
- A 3rd "reuse app_id" capability was done OUT-OF-BAND (`/tmp/reuse_deploy.py`) to avoid a schema
  change (which WOULD need a Claude session restart).

## 4. Abandoned-but-harmless on-chain (no `removeMember` to clean)

Clusters **C `0x342b676…`** (orphan dead originator `0xbEBBB…` + member Z `0x15Ea…`),
**C2 `0xdf5eb5a…`** (member W `0xD6aa247…`, media_store crash). Intermediate registered apps
`0x783f…,0xdd69…,0x53e2…,0x6363…`.

## 5. The repeatable flow (BUILT — Smithers, graph-validated)

Distilled into a durable, resumable Smithers workflow (mirrors `deploy/workflows/deploy.tsx`):

- **`deploy/workflows/matrix-node.tsx`** — workflow `attestmesh-matrix-node`, sequence
  `deploy → cluster → patha → prime → bind → verify` (each step resumable; `graph` validates the DAG).
- **`deploy/matrix-node.sh <node> [deploy|cluster|patha|prime|bind|verify|update|all|setup]`** — the
  durable routine (mirrors `node-pathA.sh`): logs to `deploy/logs/`, persists `X/H/VM_ID/CLUSTER/
  MEMBER_IMPL/PGPW` in a state file for re-entrancy. `deploy` waits for synapse to be live BEFORE the
  cluster/bind steps register the node (the member#0 = immutable CSK originator rule). `update` is the
  in-place app_id-reuse roll (new key/iptables/etc. with NO new cluster).
- **`deploy/matrix-node-box.py`** — box-side helper: `deploy` calls the same `mcp_dstack.deploy_app`;
  `hash` prints the measured compose_hash; `update <app_id>` does the reuse-CreateVm. Secrets via E_*
  env (in-memory). Its app-compose MUST mirror the tool's (pre_launch + APP_ID) — keep in sync.

Run (from repo root):
```
source deploy/env.sh
TS_AUTHKEY=tskey-… bunx smithers-orchestrator up deploy/workflows/matrix-node.tsx \
  --input '{"node":"matrix-node","meshCidrIp":"168951808"}'
# resume from a failed step:  … up … --run-id <id> --resume true
# day-2 in-place roll:        TS_AUTHKEY=… deploy/matrix-node.sh <node> update
```
Pre-reqs the workflow assumes: MCP patches 1+2 live on the box; `contracts/lib/` populated (OZ 5.1 /
OZ-upgradeable 5.1 / solidstate v0.0.61 / account-abstraction v0.7 / forge-std); `deploy/compose/
matrix-node.yaml` carries all 5 fixes; SSH+sudo to the box; box deployer key present. Pick a UNIQUE
mesh `/16` per cluster (`MESH_CIDR_IP`).

## 5b. Tailscale-not-joining diagnosis (in-place W3 CVM `42d49603`, new reusable key)
- App-compose **finished** (`Reached target Multi-User System`); `dstack-tailscale-1` + `dstack-ts-firewall-1`
  both **Started**; kernel loaded `tun: Universal TUN/TAP device driver` → tailscaled is up + opened tun.
- BUT no `matrix-attestmesh` (or any new node) appears ONLINE in the box's tailnet (`tail39cb2e`, which
  also has `lsdans-macbook-air`) — only the stale offline `matrix-attestmesh 100.89.153.38` (~12h old).
- **Container logs unreachable from the box**: gateway does NOT route the guest-agent `<instance>-8090`
  subdomain for app CVMs (404 on `/` and `/logs/<ctr>`); VMM `/logs` only has `ch=serial` (no per-container
  channel); box host has no route to the CVM's dstack-wg `10.8.0.4`. So tailscaled's own logs are blind.
- **RESOLVED**: it DID join — as **`matrix-attestmesh-1` (100.112.249.113)**. The `-1` suffix because
  the stale offline `matrix-attestmesh` hostname was still held by a prior node; the BOX's tailnet view was
  just stale (this host, once added to the tailnet, saw it `active`). `--netfilter-mode=off` was NOT the
  problem (joining is control-plane). Needed device approval — operator approved + added this host.
- **Firewall VERIFIED** from a tailnet host: `http://100.112.249.113/{,_matrix/client/versions,.well-known}`
  → WORK; `:9090` (sidecar), `:8090` (**guest-agent — same netns, would be reachable w/o the firewall**),
  `:8008` (synapse) → all BLOCKED (no response); ICMP → OK. The `:8090` block proves the iptables rules are
  active (not just netns isolation). Goal met: only Matrix (tcp/80) is reachable over the tailnet.

## 6. Open items
- Tailscale key is **reusable but expires in 7 days** — after that, a CVM reboot won't re-join until a fresh
  key is sealed (in-place re-CreateVm, same compose_hash).
- Stale offline `matrix-attestmesh` (100.89.153.38) node can be deleted in the TS admin so future deploys
  reclaim the clean hostname.
- Revoke/rotate transcript-exposed secrets (old single-use TS key; GHCR pull token).
- No Matrix users yet (`enable_registration:false`).
- Reconcile `deploy/matrix-node-runbook.md` with this; commit working-tree changes if desired.
