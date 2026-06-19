# Matrix node on the self-hosted on-chain dstack box — deploy runbook

> **SUPERSEDED (2026-06-19).** The authoritative, corrected procedure is the repeatable Smithers
> flow `deploy/workflows/matrix-node.tsx` + `deploy/matrix-node.sh` (+ `deploy/matrix-node-box.py`),
> with the full journey and all 5 bug fixes in **`deploy/matrix-node-steps-log.md`**. This file is
> the EARLY manual draft — some details (e.g. the `0xd05223` ClusterMember-impl fallback in §2b, and
> the single-pass ordering) are WRONG / corrected in the steps log. Kept for historical context.

Goal: deploy a Matrix homeserver as a **full AttestMesh node on a brand-new ClusterDiamond**, on
the self-hosted on-chain dstack box (attestmesh.xyz), via the patched `mcp__dstack__deploy_app`.

Status when this was written (2026-06-19): MCP `deploy_app` patched to accept `env` (sealed
encrypted_env) — VERIFIED (seal decrypts in the TEE; clean boot through `unseal_env_vars()`).
**A Claude session restart is required** before the new `env` param is callable from the tool
schema. After restart, execute the steps below.

Compose: `deploy/compose/matrix-node.yaml` (sidecar + postgres + synapse-init + synapse + nginx +
tailscale; server_name resolved at boot to `<app_id>.gateway.attestmesh.xyz`; Matrix NOT gated on
sidecar health since a single-node mesh never converges).

## Key facts (Base mainnet, chain 8453)
- New cluster `kmsRootSigner` = **0x7fa63d99495be2129cf28eee54e2ef2724e3aa2e** (THIS box's KMS root; NOT Phala's 0x52d3cf51…).
- Box deployer / stock-DstackApp owner = **0x91736295e68Df649A7daF2B8B1DF6140C15858BD** (key root-only on the box: `/root/.attestmesh/base-deployer.json`, a JSON array `[{"private_key":...}]`; foundry at `/root/.foundry/bin`).
- Factory `0xf6E85fD138E3208d3AAE63ce4E2A33f20e82b9fb`, memberFactory `0xFf9f438EFdAa197f4ae59D1d711C4f09640b8417`, clusterMemberImpl `0xd05223da04B4E73AC02ECA9638D490f45f765843`, indexerRegistry `0xbC003686943fB957100E517D3CEf66c52B5CDdBf`, dstackFacet `0xe9d463974c6E833DC38794d7f2DC5AB692352968`.

## Secrets to assemble at deploy time (passed via `env=` to deploy_app; NEVER in the compose/repo)
- `RPC_URL`, `BUNDLER_URL` = `https://base-mainnet.g.alchemy.com/v2/<ALCHEMY_API_KEY>` (from `~/.teesql/alchemy-api.key`).
- `GAS_POLICY_ID` (from `~/.teesql/alchemy-policy.id`).
- `POSTGRES_PASSWORD` = freshly generated (`openssl rand -hex 24`).
- `TS_AUTHKEY` = operator-provided Tailscale auth key (in-session; do not persist).
- `DSTACK_DOCKER_REGISTRY=ghcr.io`, `DSTACK_DOCKER_USERNAME`, `DSTACK_DOCKER_PASSWORD` = GHCR pull creds (from `~/.teesql/ghcr-pull.toml`). **REQUIRED**: the sidecar image `ghcr.io/attestmesh/cluster-mesh-agent` is a **private** GHCR package, so an anonymous pull fails with `unauthorized` and NO containers start. The dstack guest pre-launch script reads these three from the decrypted env and runs `docker login` before `docker compose up` (same mechanism Phala used). They are consumed by the guest, not the containers — so they only go in `env=`, never in the compose YAML.

## Steps

### 1. Deploy the CVM (box, via MCP)  — registers stock DstackApp X + boots the compose
```
mcp__dstack__deploy_app(
  name="matrix-node",
  docker_compose=<contents of deploy/compose/matrix-node.yaml>,
  env={RPC_URL, BUNDLER_URL, GAS_POLICY_ID, POSTGRES_PASSWORD, TS_AUTHKEY,
       DSTACK_DOCKER_REGISTRY, DSTACK_DOCKER_USERNAME, DSTACK_DOCKER_PASSWORD},
  gateway_enabled=true,
  ports=["tcp:127.0.0.1:8080:80","tcp:127.0.0.1:9091:9090"],   # VMM [cvm.port_mapping] allows host ports 1–20000 ONLY; wg 51900 needs NO host map (peers reach it via <app_id>-51900s.gateway…, not box loopback). The CVM still exposes 51900 internally via the compose.
  vcpu=4, memory_mb=8192, disk_gb=60,
)
```
Capture **X = app_id** and **COMPOSE_HASH** from the result. (gateway URL = `https://<X>.gateway.attestmesh.xyz`.)
NOTE: app_id is unknown before this call, so server_name self-resolves at boot. If the patched tool
auto-injects `APP_ID`, synapse-init uses it; else it probes the dstack guest socket.

### 2. Deploy a NEW ClusterDiamond C (this machine — repo + forge + AttestMesh deployer)
```
source deploy/env.sh           # exports PRIVATE_KEY (AttestMesh deployer), RPC_URL(Alchemy), CHAIN_ID, factory addrs via 8453.json
cd contracts
cat > script/clusters/matrix-node.json <<JSON
{ "clusterOwner": "<AttestMesh deployer = $DEPLOYER_ADDR>",
  "kmsRootSigner": "0x7fa63d99495be2129cf28eee54e2ef2724e3aa2e",
  "initialComposeHashes": ["0x<COMPOSE_HASH>"],
  "initialDeviceIds": [], "allowAnyDevice": true, "requireTcbUpToDate": false,
  "meshCidrIp": 168821248, "meshCidrPrefix": 16,
  "salt": "$(cast keccak attestmesh-cluster-matrix-node)" }
JSON
CLUSTER_FACTORY=$(jq -r .clusterDiamondFactory script/deployments/8453.json) \
MEMBER_FACTORY=$(jq -r .clusterMemberFactory script/deployments/8453.json) \
CLUSTER_CONFIG=script/clusters/matrix-node.json \
forge script script/DeployCluster.s.sol:DeployCluster --rpc-url "$RPC_URL" --broadcast --private-key "$PRIVATE_KEY"
```
Capture **C = cluster address**. (Use a UNIQUE meshCidrIp/16 that doesn't collide with existing clusters; 168821248 = 10.16.0.0.)

### 2b. (CONDITIONAL) Path-A facet upgrade — only if a fresh cluster's DstackFacet isn't Path-A-capable
Test first: `cast call C "allowedAppIds(address)(bool)" 0x0000000000000000000000000000000000000000 --rpc-url $RPC_URL`
— if it reverts (function missing), run the Path-A upgrade (needs SolidState acceptOwnership):
```
source deploy/env.sh && deploy/onchain.sh patha-upgrade C
```
Capture the printed Path-A **MEMBER_IMPL** (else use 0xd05223…). If `allowedAppIds` works, skip 2b and use MEMBER_IMPL=0xd05223….

### 3. Prime the cluster boot gate (this machine, clusterOwner = AttestMesh deployer)
```
cast send C "addComposeHash(bytes32)" 0x<COMPOSE_HASH> --rpc-url $RPC_URL --private-key $PRIVATE_KEY   # if not seeded at init (it is, via initialComposeHashes — verify with allowedComposeHashes)
cast send C "addAllowedAppId(address)" X --rpc-url $RPC_URL --private-key $PRIVATE_KEY
cast call C "allowedAppIds(address)(bool)" X --rpc-url $RPC_URL   # expect true
```

### 4. Upgrade the stock DstackApp X → ClusterMember + bind to C (BOX, box deployer owns X)
```
# on the box, as root:
PATH=$PATH:/root/.foundry/bin
KEY=$(jq -r '.[0].private_key' /root/.attestmesh/base-deployer.json)
REINIT=$(cast calldata "reinitializeFromDstackApp(address)" C)
cast send X "upgradeToAndCall(address,bytes)" <MEMBER_IMPL> "$REINIT" --rpc-url https://base-rpc.publicnode.com --private-key "$KEY"
cast call X "cluster()(address)" --rpc-url https://base-rpc.publicnode.com   # expect C
```

### 5. Verify registration (the in-CVM sidecar self-registers via sponsored UserOp)
```
# poll for up to ~15 min:
cast call C "memberIdOf(address)(bytes32)" X --rpc-url $RPC_URL     # expect non-zero
cast call C "memberCount()(uint256)" --rpc-url $RPC_URL             # expect 1
# sidecar health (registration-only; will NOT be phase=healthy for a single node):
curl -s https://<X>-9090.gateway.attestmesh.xyz/healthz
```

### 6. Verify Matrix
```
curl -s https://<X>.gateway.attestmesh.xyz/.well-known/matrix/server     # {"m.server":"<X>.gateway.attestmesh.xyz:443"}
curl -s https://<X>.gateway.attestmesh.xyz/.well-known/matrix/client
curl -s https://<X>.gateway.attestmesh.xyz/_matrix/client/versions       # Synapse responds
# create a user (needs registration_shared_secret from inside the CVM, or enable_registration):
#   register_new_matrix_user -c /data/homeserver.yaml -u admin -a   (run inside the synapse container)
```
Also reachable over Tailscale at `http://matrix-attestmesh` (shares nginx netns).

## Risks / open items (verify during execution)
- **kmsRootSigner**: must be 0x7fa6… (done in step 2). If registration fails with sig-chain error, re-check this addr (recompute from box KMS k256 022f3f82…).
- **patha-upgrade**: resolve 2b before step 4 (determines MEMBER_IMPL).
- **wg-over-TCP gateway routing** (`<X>-51900s.gateway.attestmesh.xyz`) is unverified on this box — irrelevant for a single node (no peers), but needed before adding a 2nd node.
- **server_name self-resolution**: confirm synapse-init logs show the resolved `<X>.gateway.attestmesh.xyz`; if app_id probe fails, set APP_ID via env (re-deploy) or fix the probe path.
- Single-node mesh never reaches phase=healthy — expected; Matrix runs regardless.
