# Matrix Node — Access, Security & Performance Journal

**Living doc — read this first to get up to speed, then append dated entries to the
[Ongoing Log](#ongoing-log) as work continues.** Secret *values* are never stored here, only
their locations. Related: [`matrix-node-steps-log.md`](./matrix-node-steps-log.md) (original
build journey), [`matrix-node-runbook.md`](./matrix-node-runbook.md),
[`../docs/specs/matrix-admin-agent.md`](../docs/specs/matrix-admin-agent.md), and the auto-memory
note `matrix-node-on-dstack-box.md`.

---

## TL;DR — current status (2026-06-24)

- ✅ **DONE — all three goals met.** The Matrix homeserver is **LIVE, private (tailnet-only),
  confidential on its user path** (HTTPS terminated *inside* the CVM via `tailscale serve`), **fast**
  (DIRECT 9ms Tailscale path), and **host-isolated** (no plaintext path from the host). The original
  **public-internet exposure was removed** (the security fix that started this). `lsdan` admin login works.
- ✅ **Slowness SOLVED via bridge-mode networking** (was the 🔴 open decision). Root cause was the
  userspace-VMM DERP relay; fixed by switching the box + the Matrix CVM to **`bridge` networking on
  `dstack-br0`** → the CVM's own Tailscale gets a **DIRECT** path (9ms, was a jittery 34→335ms relay).
  The Phala-move and public-gateway options were both DROPPED ("we are a host — make it work"). See the
  W4–W6 log entries.
- ✅ **Host plaintext view CLOSED** (was the ⚠️ secondary). Bridge mode + `forward_service_enabled=false`
  + empty per-VM `ports:` → the host opens NOTHING toward the Matrix CVM; verify moved onto the tailnet
  (`/_agent/healthz`). Proven in W6: every private port (80/443/9100/9090/51900) refused from the host.
- **Remaining (housekeeping only):** commit the 3 deploy files; push branch; rotate GHCR token; delete
  stale tailnet nodes in the TS admin console (see the last log entry).

---

## System map (3 machines — do not conflate them)

| Machine | What it is | Address |
|---|---|---|
| **dev box** | Where the repo + deploy scripts + Claude run. On the tailnet with a **direct** path to the CVM. | `ubuntu@dev`, tailnet `100.71.84.124` |
| **dstack HOST box** | Physical host running the dstack VMM; *hosts* the CVM. The dev box has key-based SSH to it (`matrix-node.sh`'s `ssh_box`). Public IP, datacenter net (bonded NICs + VLANs, `10.204.8.95/19`). | `ubuntu@173.231.234.133` = `myserver01.tail39cb2e.ts.net` |
| **Matrix CVM** | The sealed TDX confidential VM. **No shell access — that's the seal** (why container logs are invisible; the agent self-reports health). | app_id `0xF847Dd18…`; tailnet name changes each roll |

> "SSH to the box" in this journal = the **HOST**, never the CVM.

### On-chain (Base mainnet, chainId 8453)
- **Cluster C3** = `0x5ab4706fCa998A0792E5c06432b13c73c54E4557` (clusterOwner = global deployer
  `0x60b174704AdAf2b0BF87B426B364D6EbD81818E1`; kmsRootSigner `0x7fa63d99…`; mesh `10.18.0.0/16`).
- **Node W3 / app_id X** = `0xF847Dd18F55aBB9C5d62BD60A3c2bD4a7Ee6555A`; ClusterMember impl
  `0xf3f594bd…`; **member #0 / CSK originator**.
- ⚠️ Deployed contracts have **no `removeMember`** → a tainted/abandoned member #0 can't be cleaned;
  moving hosting means a **new member/cluster** (on-chain churn — the "orphan originator" problem).

### Matrix CVM stack (`deploy/compose/matrix-node.yaml`)
`sidecar` (cluster-mesh-agent) · `postgres` · `synapse-init` (one-shot: resolves server_name,
writes config/keys) · `synapse` · `matrix-admin-agent` (+ `agent-egress-fw` = deny-all egress except
redpill LLM) · `nginx` (reverse proxy) · `tailscale` (`serve` terminates HTTPS → nginx) · `ts-firewall`
(tailnet iface, allows :80/:443 only).
- `server_name` = `f847dd18f55abb9c5d62bd60a3c2bd4a7ee6555a.gateway.attestmesh.xyz` (MXIDs use this:
  `@lsdan:f847dd18…`). The *homeserver URL* is the tailnet name (separate from server_name).
- LLM: redpill, model **`z-ai/glm-5.2`** (NOT `phala/glm-5.2` — that prefix doesn't exist),
  base `https://api.redpill.ai/v1` (IP `66.220.6.104`, pinned in egress-fw CIDR `66.220.6.0/24`).

### Deploy tooling & state
- `deploy/matrix-node.sh` — `BOX_HOST`, `ssh_box`, the `update` (in-place roll) flow. `BOX_GATEWAY_ENABLED`
  defaults **false** (private). `BOX_PORTS` = host loopback port-maps (the plaintext leak — see below).
- `deploy/matrix-node-box.py` — box-side helper; `GATEWAY_ENABLED` env toggle.
- State: `deploy/logs/matrix-node-matrix-node.state` (**gitignored**): `X, H, VM_ID, CLUSTER,
  MEMBER_IMPL, PGPW, IAPW`.
- **Roll = `deploy/matrix-node.sh matrix-node update`**: recompute compose_hash → `addComposeHash(C3)` →
  `StopVm` old → `CreateVm(app_id=X)` **fresh disk (= data wipe + tailscale re-auth)** → wait synapse →
  verify agent. Membership/CSK preserved (app_id reused).

### Secret locations (VALUES NOT stored here)
- `lsdan` password → `IAPW=` in the state file (gitignored). Bot password → `~/.attestmesh/bot-password`.
- TS auth key (reusable) → `~/.attestmesh/tailscale-key`. redpill key → `~/.attestmesh/redpill-key`.
- Deployer key / RPC / bundler → `deploy/env.sh`. GHCR pull token → `~/.teesql/ghcr-pull.toml`.

---

## How to connect (current)
1. Be on the **tailnet** (`tail39cb2e`).
2. Homeserver = the CVM's **current** MagicDNS name — **it changes every roll** (now
   `https://matrix-attestmesh-2.tail39cb2e.ts.net`). Check with `tailscale status | grep matrix`
   and use the **online** one.
3. Username `lsdan` (localpart only — **not** the full MXID, which triggers discovery on the dead
   public domain). Password from the state file.
- Works in Element web **and** desktop (valid Let's-Encrypt cert via tailscale serve; the well-known
  is served with CORS + `base_url: https://$host` so discovery succeeds).
- ⚠️ **Currently slow** over the tailnet (DERP relay) — see the open decision.

---

## What happened this session (chronological)
1. **Found public exposure.** Trying to connect revealed Matrix was reachable on the **public
   internet** via the dstack gateway: `gateway_enabled=true` published `<app_id>.gateway.attestmesh.xyz`
   → nginx:80 → Synapse (public, federating). `ts-firewall` only guarded `tailscale0`, never the
   gateway path. Blast radius bounded: `enable_registration:false`, admin API not proxied.
2. **Contained:** `stop_vm` on the exposed VM `513fdd6b…` (public URL went dead). Decided: data wipe +
   rotate the surfaced `lsdan` password.
3. **Roll 1 — gateway off** (VM `0dc3d62e…`, hash `0x8fec3dbe…`): added `BOX_GATEWAY_ENABLED=false`
   (drives `gateway_enabled` + `gateway_urls`). Public URL confirmed dead *with the VM running*;
   tailnet path survives. Rotated `IAPW` + `PGPW`. **Committed `e27e984`.**
4. **Roll 2 — drop client well-known** (VM `b3b429f2…`, hash `0x53606048…`): with the gateway off the
   well-known still advertised the dead public URL → Element discovery broke.
5. **Roll 3 — HTTPS via `tailscale serve`** (VM `00eac317…`, hash `0x20c83a91…`): Element *web*
   refuses plain-`http` homeservers (browser mixed-content). Added `tailscale serve` (TS_SERVE_CONFIG,
   `${TS_CERT_DOMAIN}`) so the CVM serves HTTPS on the tailnet with a valid cert. Also fixed the LLM
   model `phala/glm-5.2` → **`z-ai/glm-5.2`** (the model the user gave didn't exist). **Committed `13e720f`.**
6. **Roll 4 — well-known CORS** (VM `122efd64…`, hash `0x7083055e…`): Element still rejected the URL.
   Root cause: the dropped well-known returned a **404 with no `Access-Control-Allow-Origin`**, so the
   browser CORS-blocked Element's discovery fetch. Fix: nginx serves
   `location = /.well-known/matrix/client` → `200`, `add_header … always` (CORS on every response),
   `base_url: https://$host` (tracks the MagicDNS name across rolls). **UNCOMMITTED** (was holding for
   confirmation).
7. **It was the Mac, not the server.** Element still failed — turned out to be the **Mac's local
   Tailscale/MagicDNS** config, not our node (`curl: could not resolve host`). User fixed it, logged in.
8. **Slow UI.** Measured the server: fast (~70ms endpoints, 0.21s empty sync, long-poll OK, direct
   9ms from `dev`). So not the server, not machine size (researched — 4 vCPU/8 GB is over-provisioned
   for an empty node).
9. **Localized to the path.** User's *other* CVM works fine from the same Mac → problem is specific to
   our node. `tailscale ping` from the Mac: **`via DERP(iad)`, "direct connection not established"**,
   jitter 34→335ms + timeouts. Raw ICMP to the host ~47ms steady.
10. **Recon proved why** (this session's key finding): no host `iptables` DNAT for the CVM, no CVM
    tap/bridge to target, no UPnP/PMP, port-maps are TCP/loopback/≤20000 → **no inbound path to the
    CVM's Tailscale UDP** → DERP-only. The box *itself* is direct-capable, which is why host-fronting
    would be fast — but that puts the host in the plaintext path.

---

## Key findings & lessons (durable)
- **TEE protects memory/compute/keys, NOT network I/O.** The host is the hypervisor and sees the
  CVM's packets — **ciphertext** for encrypted protocols, **plaintext** for plaintext ones. For
  confidentiality *from the host*, terminate TLS *inside* the CVM (our `tailscale serve` path does;
  the loopback port-maps do not).
- **The loopback port-maps (`127.0.0.1:8080→80`, `9091→9090`, `9102→9100`) are a host-readable
  plaintext view** of Matrix/sidecar/agent (used by the deploy's verify). On a don't-trust-the-host
  posture that's a leak; closing it means moving verify onto the encrypted tailnet path.
- **Verify the way the *client* does it, not with `curl`.** `curl` ignores CORS and gave repeated
  false "it works" while Element kept failing. The CORS-less 404 well-known was invisible to curl.
  The only true proof of "Element connects" is **Element itself**.
- **Self-hosted dstack box uses userspace VMM networking** → a CVM's Tailscale can't get a stable
  **direct** inbound path here → stuck on the **DERP relay** → slow. **Phala CVMs get direct paths**
  (that's why the user's other CVM is fast). This is architectural, not tunable on this box.
- `gateway_enabled=true` = **public** exposure; `tailscale serve` = **tailnet-only** (private);
  `tailscale funnel` = public. Use `serve`, never `funnel`, for private.
- **Every fresh-disk roll wipes data + tailscale state** → new tailnet name (`-1`, `-2`, … as old
  nodes linger offline) + re-auth. The cert/well-known auto-track via `${TS_CERT_DOMAIN}` / `$host`,
  so connectivity follows the current name — but **connect via the current name** and clean up stale
  TS nodes to reclaim the un-suffixed one.

---

## Open issues & the pending decision
### 🔴 Speed → reframed as a PLATFORM problem (WE ARE THE HOST)
"Move to Phala" is **OUT** — confidential-compute hosting IS the business; the platform must work.
**Root cause (confirmed from `/var/lib/dstack/vmm.toml`):** CVMs run on **userspace networking**
(`[cvm.networking] net = "10.0.2.0/24"`, SLIRP-style NAT in the VMM) → CVMs have **no routable/inbound
presence** → Tailscale can't get a direct path → DERP relay → slow. Config of this box, not a dstack law.

**The gateway is the designed tool:** dstack-gateway + **Zero-Trust TLS passthrough** (`…-<port>s` /
dstack-ingress) → the **CVM terminates TLS** (KMS-derived cert, never leaves the TEE); gateway routes
by SNI and **never sees plaintext**. NAT-friendly because the CVM dials **out** (vsock). So **fast +
confidential is solved by the gateway in passthrough mode** (we'd been using terminate mode, then killed it).
Real gap = **access control on a confidential gateway route** (gateway is public by default).

| Requirement | Mechanism |
|---|---|
| Fast (behind userspace NAT) | dstack-gateway, CVM dials out ✓ |
| Confidential (host can't read) | TLS **passthrough** — CVM terminates TLS (ZT-TLS) ✓ |
| Private (not internet-open) | **the gap** — gate the route |

**Two paths:**
- **(A) Gateway + passthrough + access control [recommended, dstack-native].** Serve via `…-<port>s`
  passthrough; gate private routes via tailnet/VPN-only gateway endpoint, mTLS (ZT-TLS client verify),
  or per-app auth. Fits HTTPS apps (Matrix) perfectly.
- **(B) Bridged CVM networking [platform upgrade].** Move CVMs off userspace `10.0.2.0/24` to
  routable/bridged nets so overlays get direct paths. More general (arbitrary inbound), bigger change.
> Decision needed: pursue **(A)** or **(B)**. Then pin the exact access-control mechanism / vmm config.
> Refs: Phala ZT-TLS custom domains; dstack-gateway docs.

### ⚠️ Confidentiality — host plaintext port-maps
Closeable via `BOX_PORTS=[]` + re-plumbing `_wait_synapse`/`verify_agent` onto the tailnet (dev box
has a direct path) + an nginx health path for the agent. **Moot if moving to Phala** → deferred
behind the speed decision.

---

## Commit / branch state (branch `matrix-admin-agent`, NOT pushed)
- `e27e984` — gateway-off by default + drop client well-known.
- `13e720f` — HTTPS over the tailnet via `tailscale serve`.
- **Uncommitted:** the roll-4 **well-known CORS fix** in `compose/matrix-node.yaml` (validated — Element
  connected after it). Also two **unrelated** pre-existing files: `deploy/node-pathA.sh`,
  `contracts/script/clusters/matrix-agent-test.json`.

## Housekeeping TODO
- [ ] Commit the well-known CORS fix (if staying on this box).
- [ ] Push branch `matrix-admin-agent` (2 commits) when ready.
- [ ] Rotate the transcript-exposed GHCR pull token (`~/.teesql/ghcr-pull.toml`).
- [ ] Delete stale offline TS nodes (`matrix-attestmesh`, `matrix-attestmesh-1`, `matrix-attestmesh-11`)
      so a roll reclaims the clean name.

---

## Ongoing log
*Append dated entries below as work continues. Newest at the bottom.*

- **2026-06-24** — Security incident found + fixed (public→private), 4 rolls (gateway-off → drop
  well-known → tailscale-serve HTTPS + LLM model fix → well-known CORS). Element connects over the
  tailnet. Diagnosed slow UI to the **DERP relay / no-direct-path** constraint of the self-hosted box.
  Created this journal. **Paused for the user's hosting decision** (accept relay vs move to Phala).
- **2026-06-24 (cont.)** — User reframed: **we ARE the confidential-compute host; make the platform
  work, don't offload to Phala.** Recon confirmed CVMs use **userspace networking** (`net=10.0.2.0/24`)
  = the root cause of no-direct-path/relay. Researched dstack: gateway + **ZT-TLS passthrough**
  (`s` suffix / dstack-ingress) is the native confidential ingress (CVM terminates TLS, gateway routes
  by SNI, NAT-friendly via outbound dial). Reframed the real problem as **confidential +
  access-controlled gateway ingress** (not Tailscale-vs-relay). Two paths: (A) gateway passthrough +
  access control [recommended], (B) bridged CVM networking. Awaiting A-vs-B direction.
- **2026-06-24 (A chosen + invariant set).** User picked **(A)** and set a **platform invariant:** a CVM
  service bound to the private/tailscale side **must not be reachable from the host** (testable: from the
  host you can't open a socket to it). Today's `127.0.0.1:8080/9091/9102` port-maps **violate** it → must go.
  **Gateway topology recon (authoritative — the web search hit the unrelated `dstack.ai`, ignored):** the
  ingress is **HAProxy** on the box (`/etc/haproxy/haproxy.cfg`, hand-managed — has a `.bak`), dual-bound:
  - `public_https_tcp` `173.231.234.133:443` **mode tcp** → SNI `*.gateway.attestmesh.xyz` → `gw_passthrough`
    (→ dstack-gateway CVM → app CVM). **This was the public Matrix path.** Already passthrough (host doesn't
    terminate).
  - `vmm_tailscale` `100.76.147.72:443` (box TAILNET ip) **mode http**, TLS-terminated → box admin/VMM only.
  - gateway.toml `[core.proxy]` listen 8443, external 443; `[core.wg]` wg0:51820; gateway runs as a CVM
    (qemu `:9202`) fronted by HAProxy.
  **Concrete design for A (private + fast + confidential):** (1) serve the app's SNI as **mode tcp
  passthrough on the TAILNET frontend** (`100.76.147.72`) → app CVM terminates TLS (`…-<port>s` / ZT-TLS) →
  fast (box tailnet ip is *directly* reachable) + confidential (host sees only ciphertext); (2) **exclude
  that SNI from `public_https_tcp`** = the access control (public-vs-private = which frontend carries the
  SNI; generalizes to a per-app switch — a real host feature); (3) **no host port-maps** → invariant holds.
  **Open specifics before touching prod:** (a) exact HAProxy edit + confirm whether the gateway regenerates
  `haproxy.cfg`; (b) app-CVM cert for `s`-passthrough (KMS/ZT-TLS); (c) client DNS → box tailnet ip.
  ⚠️ haproxy.cfg = ingress for ALL hosted CVMs → propose exact diff + confirm + backup before any change.
- **2026-06-24 (wildcard REJECTED).** Inventory check (`list_vms`): only public prod app is `nginx-gw`
  (`8d0c965c…`) via a **custom domain**; KMS/gateway = infra; `test1` = test; `matrix` = ours. I proposed
  making the whole `*.gateway.attestmesh.xyz` namespace private — **user rejected: "I still need gateway
  stuff to be public."** So the gateway namespace stays PUBLIC by default; **private must be the EXCEPTION,
  not the default.** Two candidate models (both keep gateway public): **(i) per-app carve-out** = a managed
  list of private SNIs pulled off the public listener (app keeps its gateway name); **(ii) dedicated private
  zone** = e.g. `*.priv.attestmesh.xyz` served tailnet-only, gateway zone untouched (app opts in by hostname;
  Matrix server_name change = wipe). **Awaiting user's preferred model / clarification of the concern.** Do
  NOT touch haproxy.cfg's public gateway routing.
- **2026-06-24 (DIRECTION SET: generic VMM bridge networking).** User: **no per-CVM host config** — only
  generic host config OR fully CVM-driven. That kills the HAProxy carve-out AND the wildcard-gating. So:
  privacy stays CVM-driven (Tailscale on the CVM, no public exposure); the ONLY blocker is speed, which
  traces to a single GENERIC host setting — `[cvm.networking] mode = "user"` (QEMU SLIRP `net=10.0.2.0/24`)
  gives CVMs no routable presence → Tailscale relays via DERP → slow. **dstack supports `mode = "bridge"`**
  (`NetworkingMode::Bridge` in `vmm/src/app/qemu.rs`; config `bridge = "<iface>"`; per-VM override via
  `--net bridge` / `networking.mode`; doc `/root/dstack/docs/bridge-networking.md`). Plan: switch CVM
  networking to bridge (generic, one-time) so every CVM's Tailscale gets a DIRECT path; privacy stays
  CVM-side. **Consulting the dstack expert (paseo agent `2afd9c62-d947-440e-b531-8c76598c2e72`, who built
  this box's MCP/networking)** on 5 box-specific Qs before touching live: (1) KMS reachability in bridge
  mode (CVMs hit KMS via SLIRP `10.0.2.2:9101` today; KMS runs in its own CVM at host `127.0.0.1:9101`);
  (2) coexistence — run JUST matrix in bridge while infra CVMs stay user-mode, or must the global default
  flip?; (3) bridge + nftables setup on THIS datacenter box (libvirt virbr0 vs manual dstack-br0); (4) the
  doc's DHCP-lease→`ReportDhcpLease`→auto host port-forward vs our no-host-exposure invariant
  (`forward_service_enabled=false` already set — enough?); (5) will a bridged CVM get a DIRECT Tailscale
  path. **Plan: validate on a THROWAWAY CVM first; no global-default change / no live-CVM disruption until proven.**
- **2026-06-24 (EXPERT ANSWER, verified vs live box + VMM source; pre-flight GREENLIT).** Bridge mode IS
  right; **per-VM `mode=bridge` (Matrix only, infra stays user) is supported** — keep global `mode="user"`,
  just ADD `bridge="dstack-br0"` (user-mode VMs ignore it). **Gotcha 1:** bridge NAME is global-only (read at
  VMM startup) + `dstack-vmm.service` `KillMode=control-group` (all 5 CVMs in its cgroup) → the enabling
  restart **SIGTERMs every CVM**; `[cvm.auto_restart]=true` recovers (~1-2 min). Required even to test bridge
  on a dstack throwaway. **Gotcha 2:** KMS reachability breaks (ONLY KMS — host-api is vsock `vsock://2`,
  mode-independent). KMS loopback-only (`127.0.0.1:9101`, `route_localnet=0`). FIX (generic, 1 rule):
  `sysctl …dstack-br0.route_localnet=1` + `nft … PREROUTING iif dstack-br0 daddr 10.0.100.1:9101 dnat to
  127.0.0.1:9101` + per-VM `kms_urls=["https://10.0.100.1:9101"]` (manifest field; RA-TLS validates the TDX
  quote, not the IP). **Q4 = clean win:** `app.rs:466` host→CVM forward only on `bridge &&
  forward_service_enabled`; ours=`false` → host opens NOTHING toward CVM (better than user-mode hostfwd).
  Close the L3-router residual CVM-side: **bind Synapse to `tailscale0` only**. **Q3:** no bridge/libvirt →
  Option B (`dstack-br0`, systemd-networkd, isolated NAT, NOT on bond/VLANs); `FORWARD`=drop (Docker) →
  forward-accepts in `DOCKER-USER`; qemu-bridge-helper not setuid but VMM=root OK (+ `/etc/qemu/bridge.conf
  allow dstack-br0`); dnsmasq, drop dhcp-script. **Q5 = direct LIKELY but NOT guaranteed** (host is direct:
  `MappingVariesByDestIP:false`, own public IP on a real /28; bridge adds 1 masquerade layer → MEASURE).
  Fallback if it relays: public IP from the /28 (routed to `bond0.582`), tailscale-firewalled.
  **De-risked sequence:** (1) prove direct on a BARE netns on `dstack-br0` — **NO VMM restart, zero infra
  impact**; (2) host pieces; (3) add `bridge=` to vmm.toml; (4) **planned maintenance-window** VMM restart
  (all CVMs bounce+recover); (5) throwaway dstack CVM in bridge → boots+direct; (6) migrate Matrix (bind
  `tailscale0`). **Operator GREENLIT step 1** — expert `2afd9c62` running the bare-netns pre-flight now
  (reversible, no restart). Steps 4+ require explicit operator OK (bounces the on-chain KMS + gateway).
- **2026-06-24 (PRE-FLIGHT = GO; PREP authorized).** Expert ran the bare-netns pre-flight on `dstack-br0`:
  **GO** — `netcheck` `MappingVariesByDestIP:false` (cone NAT, mapped to box public IP), `tailscale ping`
  **direct `via …:41641 in 8ms`** (not DERP) on the first ping → a bridge-mode CVM gets a DIRECT ~8ms path;
  `/28` fallback NOT needed. Caveat: netns+veth is a faithful NAT-behavior proxy; real end-to-end CVM
  confirmation is rollout step 5. Box left **pristine** (artifacts removed; 5/5 CVMs untouched; `vmm.toml`
  unchanged; nothing restarted). Housekeeping: offline tailnet node `bridge-preflight` (100.114.151.104) safe
  to delete in TS admin. **Operator authorized PREP** (restart still gated): expert is (background) preparing
  the maintenance-window **runbook** + **staging all NON-disruptive host prep** (persistent `dstack-br0` via
  networkd + dnsmasq, KMS DNAT + `route_localnet=1`, `DOCKER-USER` accepts, `/etc/qemu/bridge.conf`),
  re-confirming direct on the persistent bridge, then **STOPPING before** the `vmm.toml` `bridge=` edit /
  `dstack-vmm` restart / any running-CVM touch. Invariants preserved (`forward_service_enabled=false`, no host
  port-maps, Synapse→`tailscale0`, per-VM `kms_urls=["https://10.0.100.1:9101"]`). **ONLY remaining downtime =
  the VMM restart** (all 5 CVMs bounce ~1–2 min, `auto_restart` recovers). Awaiting expert READY → operator
  schedules the window. Nothing restarts the VMM without explicit operator go.
- **2026-06-24 (MAINTENANCE WINDOW EXECUTED — "do it now"; bridge rollout DONE).** Operator gave go. Expert
  ran the staged window (W1–W4): **W1** `vmm.toml` → global `[cvm.networking] mode="bridge"`,
  `bridge="dstack-br0"`, `forward_service_enabled=false` (box is dedicated → went fully bridge, not the
  per-VM variant). **W2–W3** host prep made persistent: `dstack-br0` via systemd-networkd, dnsmasq DHCP on
  10.0.100.0/24, `DOCKER-USER` forward-accepts, `/etc/qemu/bridge.conf allow dstack-br0`. **KMS DNAT
  correction:** the KMS RA-TLS cert SANs **10.0.2.2** (the SLIRP gw alias), NOT 10.0.100.1 — so the live rule
  is `route_localnet=1` on dstack-br0 + PREROUTING dnat `…:9101 → 127.0.0.1:9101`, reached via per-VM
  **`kms_urls=["https://10.0.2.2:9101"]`** (earlier 10.0.100.1 guidance was WRONG; expert fixed it live).
  **W4** the gated `dstack-vmm` restart: all 5 CVMs SIGTERM'd + `auto_restart` recovered (~2 min), health gate
  PASS; a throwaway bridge CVM (`w4-bridge-cvm`) then booted clean through the KMS DNAT + DHCP and got a
  **DIRECT 13ms** Tailscale path → end-to-end GO. Box-side runbook/rollback staged in `/root/dstack-bridge-rollout/`.
- **2026-06-24 (W5 MATRIX MIGRATION + W6 VERIFY — COMPLETE; original security + speed goals MET).** Migrated
  the live Matrix node to bridge via our OWN tooling. **Edits:** `matrix-node-box.py` — `NET_MODE` env +
  `networking:{mode}` + `kms_urls=10.0.2.2` (bridge) in the update-mode CreateVm; `matrix-node.sh` —
  `BOX_NET_MODE=bridge`/`BOX_PORTS=[]` defaults, `_cvm_fqdn` tailnet helper, `_wait_synapse`+`verify_agent`
  re-plumbed to the tailnet (no host ports to probe); `compose/matrix-node.yaml` — removed ALL nginx/sidecar/
  agent host `ports:`, added nginx `/_agent/healthz` proxy for tailnet verify. **Roll:** new
  `compose_hash 0x061e99…` → `addComposeHash` (nonce 926) → StopVm `122efd64` (user) → **bridge CreateVm
  `aec27437` — CreateVm ACCEPTED `networking:{mode:bridge}` (HTTP 200)** → tailnet verify GREEN (synapse @
  `matrix-attestmesh-3`, agent ready + egress LOCKED). Membership/CSK preserved (`X=0xF847Dd18…`).
  **W6 (all PASS):** (1) **direct 9ms** (`173.231.234.133:21878`, no DERP) — slowness fixed; (2) tailnet
  client access — synapse versions OK, CORS well-known `ACAO:*`, agent `/healthz`
  `{egress_locked:true, llm_ok:true (z-ai/glm-5.2, redpill /models→200), admin_token+matrix_synced:true}`;
  (3) **HOST-ISOLATION INVARIANT PROVEN** — Matrix CVM = bridge IP **10.0.100.11** (MAC 4a:6a:68:e4:41:14,
  qemu pid 2035498); from the host **:80/:443/:9100/:9090/:51900 ALL refused**, and the Matrix qemu has **no
  host port-forward** (absent from `ss` qemu list; only host forwards = KMS loopback :9101, gateway public
  :9202/:9204, 2 other CVMs' loopback). **NET RESULT:** Synapse reachable ONLY over the CVM's own Tailscale
  (encrypted, terminated in-CVM), **fast + zero plaintext path from the host** — the exact invariant
  *"anything bound to the tailscale IP within the CVM is not also exposing itself to the host."*
  **Outstanding:** commit the 3 deploy files (gated on operator ask); push branch; rotate GHCR token; delete
  stale tailnet nodes `bridge-preflight` / `bridge-preflight2` / `matrix-attestmesh-1` / `-2` / `w4-bridge-cvm`
  in the TS admin console.
