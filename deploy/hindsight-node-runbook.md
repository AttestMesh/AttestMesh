# Hindsight Memory Node — Operator Runbook

How to access, operate, roll, and re-create the Hindsight agent-memory node
([vectorize-io/hindsight](https://github.com/vectorize-io/hindsight): retain /
recall / reflect over an LLM). This is the do-this-then-that; the measured
runtime payload with inline design comments is
[`deploy/compose/hindsight-node.yaml`](compose/hindsight-node.yaml), the driver
is [`deploy/hindsight-node.sh`](hindsight-node.sh).

**Design stance (operator directive, 2026-07-01): MESH-ONLY. No Tailscale, no
public HTTP.** The API and UI are reachable exclusively over the cluster's
WireGuard mesh; humans get in through the ssh-node's mesh shell. Consumers are
other AttestMesh nodes (e.g. Hermes agents using per-agent memory banks).

---

## 1. Live deployment record (deployed 2026-07-01)

| Item | Value |
|---|---|
| Cluster | C3 `0x5ab4706fCa998A0792E5c06432b13c73c54E4557` (Base mainnet, self-hosted dstack box) |
| app_id (X) | `0xa151d945e3bdB7d68f8dFdc3602c98f80D2F46bc` |
| memberId | `0xd1ab76cfb56d811cf971adaf2cb6d43d4d740ece737b883f7d740cafb2bdff6c` (member #5) |
| VM | `9190f502-7f67-4224-ba54-d29446df608e` (`hindsight-node`, 4 vcpu / 6144 MB / 40 GB, bridge mode) |
| Mesh IP | **`10.18.78.76`** (C3 mesh = 10.18.0.0/16) — API `:18888`, UI `:18999` |
| Image | `ghcr.io/vectorize-io/hindsight:0.8.4` (full image: local embeddings + reranker + tiktoken baked in; embedded pg0 Postgres) |
| LLM | `openai/gpt-oss-120b` via `https://api.redpill.ai/v1` (OpenAI-compatible), egress-enforced |
| State file | `deploy/logs/hindsight-node-hindsight-node.state` (0600 — holds the **TAK/CPK secrets**, do not commit) |
| Compose hash (bound) | `0x92f374f2ce50939eb88b614807da265ccd2926cddc8b7867575bd15478448655` |

## 2. Topology and security model

One CVM, five containers:

| Container | Netns | Role |
|---|---|---|
| `sidecar` | own (owns ports) | cluster-mesh-agent: Path-A self-registration, wg mesh, CSK. Publishes `:9090` (health) + `:51900` (wg-over-gateway-TCP ingress) — the ONLY published ports. |
| `mesh-proxy-api` | `service:sidecar` | socat `:18888` → `hindsight:8888` — binds where the wg iface lives, so only mesh peers reach it. |
| `mesh-proxy-ui` | `service:sidecar` | socat `:18999` → `hindsight:9999` (control-plane UI). |
| `hindsight` | own | API `:8888` + Next.js UI `:9999` + embedded pg0. `expose` only — never published. |
| `hindsight-egress-fw` | `service:hindsight` | default-DROP OUTPUT; allows loopback, established, Docker DNS, and **only** the redpill host `:443` (`LLM_ALLOW_CIDRS=66.220.6.0/24`). |

Auth layers on top of mesh isolation:

- **API (`:18888`, and the built-in MCP server at `/mcp/{bank_id}/`)**: Hindsight
  ships with NO auth; we enable the built-in `ApiKeyTenantExtension`. Every call
  needs `Authorization: Bearer <TAK>` (raw key also accepted). No key → 401.
- **UI (`:18999`)**: gated by `HINDSIGHT_CP_ACCESS_KEY` (CPK). The UI talks to
  the API in-container using the TAK (`HINDSIGHT_CP_DATAPLANE_API_KEY`).
- TAK/CPK are generated once by the driver, sealed into the CVM, and persisted
  in the state file so they survive rolls. Read them:
  `grep -E '^(TAK|CPK)=' deploy/logs/hindsight-node-hindsight-node.state`

Egress-lock and offline-model settings that matter (all in the compose):
`HF_HUB_OFFLINE=1` + `TRANSFORMERS_OFFLINE=1` (without these, model load makes
HuggingFace etag HEAD requests that hang under deny-all), do **not** mount a
volume over `/home/hindsight/.cache` (it would hide the baked-in models), and
`stop_grace_period: 30s` (pg0 needs a clean shutdown — upstream data-loss
issue #675 with Docker's 10 s default).

## 3. Access

From the ssh-node **mesh shell** (`ssh attestmesh-mesh-node` — sshd `:1023`
inside the ssh-node sidecar's netns; see `deploy/compose/ssh-node.yaml`):

```bash
TAK=<from the state file>
# health (no auth needed)
curl http://10.18.78.76:18888/health          # {"status":"healthy","database":"connected"}
# retain (bank auto-created; synchronous with async:false — runs LLM extraction)
curl -H "Authorization: Bearer $TAK" -H 'Content-Type: application/json' \
  -X POST http://10.18.78.76:18888/v1/default/banks/<bank>/memories \
  -d '{"items":[{"content":"..."}],"async":false}'
# recall
curl -H "Authorization: Bearer $TAK" -H 'Content-Type: application/json' \
  -X POST http://10.18.78.76:18888/v1/default/banks/<bank>/memories/recall \
  -d '{"query":"...","max_tokens":4096}'
```

From your own machine, tunnel through the mesh shell:
`ssh -D 1080 attestmesh-mesh-node` then
`curl --socks5-hostname localhost:1080 http://10.18.78.76:18888/health`, or a
plain port-forward for the UI: `ssh -L 9999:10.18.78.76:18999 attestmesh-mesh-node`
→ browse `http://localhost:9999` (login = CPK).

Other API surface: Swagger at `/docs`, Prometheus at `/metrics`, `/version`;
reflect via `POST /v1/default/banks/{bank}/memories/reflect`-family routes (see
Swagger). Recommended per-consumer convention: one bank per agent.

## 4. Fresh deploy / re-create

```bash
source deploy/env.sh
deploy/hindsight-node.sh <node-name> all
```

Prereqs: `~/.attestmesh/redpill-key` (LLM key), `~/.teesql/ghcr-pull.toml`
(private sidecar/egress-fw images; hindsight itself is public),
`deploy/logs/matrix-node-matrix-node.state` (CLUSTER + MEMBER_IMPL defaults),
and a working `attestmesh-mesh-node` ssh alias for the verify steps.

`all` = `deploy → prime → bind → verify → verify-sidecar → verify-app → verify-e2e → verify-isolation`:

| step | what happens | what "good" looks like |
|---|---|---|
| deploy | box registers a stock DstackApp + seals env + bridge CreateVm (gateway ON only for wg `:51900`) | `✔ deployed … app_id=0x…` |
| prime | `addComposeHash` + `addAllowedAppId` on C3 (deployer key) | tx status 1 (skips if already allowed) |
| bind | box deployer: `upgradeToAndCall(X, ClusterMember impl, reinitializeFromDstackApp(C3))` | `X.cluster()=0x5ab4…` |
| verify | poll `memberIdOf(X)` | `✔ … memberId=0x… memberCount=N` |
| verify-sidecar | box-side `curl <bridge-ip>:9090/healthz` | `phase` JSON (see §6 — port binds POST-bind only) |
| verify-app | mesh shell discovers the mesh IP (wg allowed-ips × `:18888/health` probe) | `✔ hindsight healthy over the mesh at <ip>:18888` |
| verify-e2e | retain → LLM extraction → recall + a no-auth probe | `✔ E2E OK … + auth gate (401 without key)` |
| verify-isolation | from the box: 8888/9999/18888/18999 must refuse at the bridge IP; 9090 must answer | `ISOLATION: PASS` |

**Ordering caveat**: C3 has **no removeMember** — membership is permanent. For
this node that risk was retired by validating the app pre-bind (local docker
smoke test with the exact sealed env + the §6 debug-shell pattern in-CVM);
the node-level stack (sidecar) is the proven common component.

## 5. Day-2 operations

- **Roll a compose/env change** (disk-preserving; pg0 memory data survives):
  `deploy/hindsight-node.sh hindsight-node update` — computes the new hash,
  allowlists it on C3 first, then in-place `StopVm → UpgradeApp → StartVm`.
  Follow with `verify-app` / `verify-e2e`.
- **Swap the LLM model / rotate TAK / CPK / LLM key**: values are sealed, NOT
  measured — same compose hash, so plain `update` (no new allowlist needed).
  Model: `HINDSIGHT_LLM_MODEL=<redpill model id> … update`. Keys: edit/delete
  the `TAK=`/`CPK=` lines in the state file (empty → regenerated) or export
  `HINDSIGHT_TENANT_API_KEY`/`HINDSIGHT_CP_ACCESS_KEY`, then `update`; hand the
  new TAK to consumers.
- **Resize**: `BOX_VCPU=… BOX_MEM=… BOX_FRESH_DISK=1 … update` (UpgradeApp
  cannot resize; fresh-disk CreateVm reuses the app_id → membership kept,
  **memory store wiped** — deliberate only).
- **Wipe the memory store**: `BOX_FRESH_DISK=1 … update`.
- If the VM is ever lost/removed from the VMM (it happened to the ssh-node):
  `BOX_FRESH_DISK=1 … update` recreates it under the same app_id — the box
  helpers tolerate a missing vm_id.

## 6. Hard-won lessons / troubleshooting

- **The sidecar binds `:9090` (and `:51900`) only AFTER bind.** Pre-bind it
  loops `cluster not resolvable yet (awaiting ClusterMember upgrade?)` every
  10 s with all ports closed — a refused `:9090` before bind is NORMAL, not a
  crash. Do not debug it; bind. (Cost 40 minutes of ghost-hunting on 2026-07-01.)
- **Pre-bind in-CVM debugging recipe** (the TEE blocks `docker logs` from the
  box; serial shows compose bring-up only): roll a temporary `debug-shell`
  sshd service with `/var/run/docker.sock` mounted and a published port, then
  read logs/exec via `curl --unix-socket /var/run/docker.sock
  http://localhost/v1.43/containers/<name>/logs?...`. Pre-bind the hash gate is
  the **stock DstackApp contract** (not the cluster): allowlist the debug hash
  with the BOX deployer key — `cast send <X> 'addComposeHash(bytes32)' 0x<hash>`
  — then UpgradeApp. **Roll back to the clean compose BEFORE bind** so the
  cluster only ever allowlists the clean hash. (The debug hash left in the old
  DstackApp storage is inert — the implementation is ClusterMember afterwards.)
- **`ip neigh show dev dstack-br0` is stale/empty for fresh CVMs** — ping-sweep
  `10.0.100.0/24` first, then match the qemu TAP MAC (the drivers' box-side
  snippets do the MAC→IP mapping; the sweep is the missing first step when a
  probe mysteriously refuses).
- **Boot-time LLM verification**: hindsight calls the LLM provider at startup
  (skippable via `HINDSIGHT_API_SKIP_LLM_VERIFICATION` — we deliberately leave
  it ON as an llm_ok probe). `/health` stays 503 until engine init completes
  (model load + pg0 + migrations; `HINDSIGHT_API_MODEL_INIT_TIMEOUT` default
  300 s), then 200 `{"status":"healthy","database":"connected"}`.
- **Pre-seal validation trick**: `docker run` the exact image+env on the dev
  box first — proved redpill + `gpt-oss-120b` structured-output extraction
  before anything was sealed into the TEE.
- `HINDSIGHT_API_RETAIN_MAX_COMPLETION_TOKENS=32000` (default assumes ~64k
  output tokens; must stay > `RETAIN_CHUNK_SIZE`=3000).

## 7. Known caveats (accepted, documented)

- **`<app_id>-9090.gateway.attestmesh.xyz` serves the sidecar healthz
  publicly** (gateway routes any published guest port). It leaks only
  `{csk_acquired, first_converged, live_peers, phase}` — no secrets — and is
  the box-visible observability the verify steps rely on; same posture as the
  synclave/ssh nodes. Dropping the `9090` publish (one `update` roll) closes it
  at the cost of box-side health checks. The app/mesh ports are NOT reachable
  this way (verified: `-8888`/`-18888` routes dead, host-isolation PASS).
- **Sealed env values ride `sudo … E_*` argv over SSH to the box** (visible to
  root-owned procfs on the box for the call's duration) — shared property of
  all the box drivers, flagged in the 2026-07 deploy-scripts review; fix
  planned repo-wide (env-file over stdin), not per-node.
- The TAK was shared in an operator session transcript on 2026-07-02; rotation
  is a cheap values-only roll (§5) whenever desired.
