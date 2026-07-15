# AttestMesh R2-Host Node — Component Spec

**Status**: SPECIFIED — implementation in `deploy/` (trio + image), pending live verification
**Parent spec**: [`attestmesh-coordination-layer.md`](./attestmesh-coordination-layer.md) (membership §5–§8, CSK §8)
**Related**: [`sidecar.md`](./sidecar.md) (agent gRPC §12.1, CSK §6), [`matrix-admin-agent.md`](./matrix-admin-agent.md) (wal-g CSK-derived R2 backup precedent)
**Component**: `deploy/r2-host/` (image `ghcr.io/attestmesh/r2host-s3gw`) + `deploy/compose/r2-host-node.yaml` + `deploy/r2-host-node.{sh,-box.py}` + `deploy/workflows/r2-host-node.tsx`
**Author**: AttestMesh (owned in `teemesh`)
**Created**: 2026-07-02
**Last Updated**: 2026-07-02

---

## 1. Purpose

An **encrypting S3 gateway node** on the AttestMesh wireguard mesh. Mesh clients speak
plain S3 (with static credentials) to the node's mesh IP; the node **encrypts on write
and decrypts on read** and stores **only ciphertext** in a dedicated **Cloudflare R2
bucket**. R2 is the authoritative store; the CVM disk holds only a write-back cache.

The at-rest key is **derived from the cluster shared key (CSK)**, so the node is
effectively stateless: a CVM lost entirely and re-provisioned under the same `app_id`
re-derives the same key (originator re-derivation / mesh re-pull, sidecar spec §6) and
can read every object — recovery with zero backup machinery.

Trust model: **plaintext exists only inside the CVM**. Cloudflare sees ciphertext with
encrypted filenames; the box operator sees nothing (mesh-only listener); mesh clients
are inside the cluster trust domain and authenticate with S3 credentials.

### 1.1 Non-goals / scope boundary

- **NOT a general S3 replacement.** rclone `serve s3` implements a useful subset
  (put/get/list/delete/copy/multipart); no versioning, no ACLs, no bucket policies,
  no presigned-URL delegation to third parties (§6.4).
- **NOT publicly reachable.** No dstack gateway ingress, no tailnet, no published app
  port. The S3 listener binds the wireguard mesh IP only.
- **NOT multi-tenant (v1).** One credential pair, full access to the whole encrypted
  namespace. Per-app credentials/prefix scoping is a future extension (§9).
- **NOT attestation-method-aware.** Consumes only the sidecar's method-agnostic agent
  gRPC; no dstack specifics outside the deploy tooling.

---

## 2. Toolchain

- **rclone** (pinned release, digest-pinned image) — S3 server (`rclone serve s3`),
  encryption layer (`crypt` remote: NaCl secretbox, 64 KiB chunks, encrypted file
  names), R2 client (`s3`/Cloudflare provider remote).
- **grpcurl** (pinned release) — fetches the CSK over the sidecar agent UDS.
- **python3** (alpine) — HKDF-SHA256 derivation, as in `deploy/postgres-walg/`.
- Image: `deploy/r2-host/Dockerfile` → `ghcr.io/attestmesh/r2host-s3gw`, built by
  `.github/workflows/build-r2host-s3gw.yml` (or on the box).

---

## 3. Configuration (env)

All values arrive sealed (dstack `encrypted_env`); key NAMES are measured into the
compose hash via `allowed_envs`.

| Env var | Required | Default | Meaning |
|---|---|---|---|
| `BACKEND_PROVIDER` | no | `Cloudflare` | rclone S3 provider (`Cloudflare`, `AWS`, or another supported S3 implementation). |
| `BACKEND_ENDPOINT` | yes | — | R2 or S3 endpoint. Also the egress-fw allow target. |
| `BACKEND_BUCKET` | yes | — | Ciphertext bucket (optionally with a prefix). |
| `BACKEND_REGION` | no | `auto` | Backend region (`auto` for R2; an AWS region for S3). |
| `BACKEND_ACCESS_KEY_ID` / `BACKEND_SECRET_ACCESS_KEY` | yes | — | Bucket-scoped backend credentials. |
| `BACKEND_FORCE_PATH_STYLE` | no | `true` | Use path-style requests; set `false` if the S3 provider requires virtual-host style. |
| `R2_*` | compatibility | — | Existing R2 names remain accepted as fallbacks for deployed environments. |
| `R2_BUCKET` | yes | — | Dedicated ciphertext bucket (e.g. `attestmesh-r2-host`). |
| `R2_REGION` | no | `auto` | R2 region. |
| `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` | yes | — | R2 token scoped to that bucket. |
| `S3GW_ACCESS_KEY_ID` / `S3GW_SECRET_ACCESS_KEY` | yes | — | Mesh-client S3 credentials (rclone `--auth-key`). Generated once into `~/.attestmesh/r2-host.env`. |
| `S3GW_LISTEN_PORT` | no | `19000` | S3 listener port (inside CVM; surfaced on the mesh IP by the socat proxy). |
| `S3GW_VFS_CACHE_MODE` | no | `writes` | rclone VFS cache mode. |
| `S3GW_VFS_CACHE_MAX_SIZE` | no | `40G` | VFS cache cap; keep ≈40–60% of CVM disk (§6.5). |
| `AGENT_GRPC_SOCKET` | no | `/var/run/attestmesh/agent.sock` | Sidecar agent UDS (shared `agent-sock` volume). |
| sidecar set: `CHAIN_ID RPC_URL BUNDLER_URL GAS_POLICY_ID INDEXER_REGISTRY_ADDR GATEWAY_DOMAIN` | yes | — | Path-A registration infra (identical to every node type). |
| `DSTACK_DOCKER_USERNAME/PASSWORD/REGISTRY` | yes | — | ghcr pull creds (private images), consumed by `pre_launch_script`. |

---

## 4. Architecture

```
mesh client (any C3 member / ssh-node SOCKS)
    │  S3 over HTTP, creds S3GW_*  (plaintext bodies — mesh is the trust boundary)
    ▼
attestmesh0 <mesh-ip>:19000      s3-mesh-proxy: socat in the SIDECAR netns,
    │                            bind=<mesh-ip> ONLY (postgres-node precedent)
    ▼
s3gw (rclone serve s3, compose-internal :19000, never published)
    │  crypt: remote — NaCl secretbox content + std filename encryption
    │  password  = HKDF(CSK, "attestmesh.r2host.crypt.v1")
    │  password2 = HKDF(CSK, "attestmesh.r2host.crypt.salt.v1")
    │  CSK ← agent UDS GetClusterSharedKey (after GetMeshStatus.csk_acquired)
    │  VFS write-back cache on /vfs-cache (disposable named volume)
    ▼
r2: remote ──── egress-fw: OUTPUT deny-all except R2_ENDPOINT + Docker DNS
    ▼
Cloudflare R2 bucket  (ciphertext objects, opaque names)
```

Node lifecycle is the canonical Path-A join-cluster flow (deploy → prime → bind →
verify), joining the existing C3 cluster; see `deploy/r2-host-node.sh`.

---

## 5. Interfaces (frozen contract)

1. **S3 endpoint** — `http://<mesh-ip>:19000`, path-style, sigv4 with `S3GW_*`
   credentials. Buckets map to top-level directories under the encrypted namespace.
   Unauthenticated requests are rejected (403).
2. **Consumed: sidecar agent gRPC** (`attestmesh.agent.v1.Agent` over UDS) —
   `GetMeshStatus` (gate on `csk_acquired`), `GetClusterSharedKey`. The proto is
   vendored at `deploy/r2-host/agent.proto`.
3. **R2 side** — objects under `R2_BUCKET` are rclone-crypt format (self-describing
   given the two derived passwords); any rclone with the same derived config can read
   them directly (documented recovery path, §6.3).
4. **Boot status file** — `/s3gw-status/state` (append-only phases; the TEE blocks
   container logs). Never contains key material.

---

## 6. Design

### 6.1 Key derivation (walg-compatible recipe)

```
csk  = GetClusterSharedKey()                      # 32 bytes, base64 over grpcurl
prk  = HMAC-SHA256(key=0x00*32, msg=csk)
pw   = HMAC-SHA256(prk, "attestmesh.r2host.crypt.v1"      || 0x01).hex()
salt = HMAC-SHA256(prk, "attestmesh.r2host.crypt.salt.v1" || 0x01).hex()
```

`pw`/`salt` become the crypt remote's `password`/`password2` (via `rclone obscure`).
Derivation happens **in-process** in the container entrypoint; the plaintext key
material exists only in that process env (only obscured forms reach rclone config
env), never on disk, never in the status file.

### 6.2 Serving

`rclone serve s3 crypt: --auth-key $S3GW_ACCESS_KEY_ID,$S3GW_SECRET_ACCESS_KEY
--addr 0.0.0.0:19000 --vfs-cache-mode writes --vfs-cache-max-size ... --cache-dir /vfs-cache`.
`0.0.0.0` inside the s3gw netns is safe: the port is not compose-published; the only
route in is the mesh-IP-bound socat in the sidecar netns.

### 6.3 Recovery

Same `app_id` ⇒ same CSK (originator re-derivation or mesh re-pull) ⇒ same HKDF
outputs ⇒ same crypt config ⇒ all R2 objects readable. `BOX_FRESH_DISK=1` update is
therefore always safe and doubles as the recovery drill. Manual escape hatch: any
machine holding the CSK can reconstruct the rclone config and read the bucket with
its own R2 credentials.

### 6.4 Known limitations (rclone `serve s3` is upstream-flagged Experimental)

- **Multipart uploads buffer parts in RAM** (upstream issue #7453). Clients should
  raise their multipart threshold or disable multipart; CVM RAM is sized with
  headroom (8 GiB).
- **ETag is not the plaintext MD5** over a crypt remote — etag-delta sync
  (`aws s3 sync`) and Content-MD5-verifying clients misbehave; use size/mtime modes.
- **Read-after-write is NOT immediate** (live-verified 2026-07-02): a GET of a
  freshly PUT object 404s until the VFS write-back (~5s) + upload to R2 completes.
  Clients must retry reads of just-written keys.
- No versioning / ACL / policy / website endpoints.

### 6.5 Cache sizing

`--vfs-cache-max-size` must stay well under the CVM disk (default 40G of 100G; the
dstack image + docker layers need the rest). A full cache blocks writes until
write-back drains — symptom: hung PUTs.

---

## 7. Constants

| Constant | Value |
|---|---|
| HKDF extract key | `0x00 * 32` |
| HKDF info (password) | `attestmesh.r2host.crypt.v1` + `0x01` |
| HKDF info (password2) | `attestmesh.r2host.crypt.salt.v1` + `0x01` |
| S3 listener port | `19000` (mesh IP only) |
| Published CVM ports | `9090` (sidecar health), `51900` (wg transport) — nothing else |
| Filename encryption | `standard` |

---

## 8. Requirements

**Must**
- [ ] Store only ciphertext in R2 (content + filenames encrypted).
- [ ] Serve S3 only on the wireguard mesh IP; bridge/host/public unreachable.
- [ ] Reject unauthenticated S3 requests.
- [ ] Derive the crypt key from the CSK with the §6.1 recipe; survive total CVM loss.
- [ ] Egress deny-all except `R2_ENDPOINT` (+ Docker DNS).
- [ ] Self-report boot phase to the status volume (TEE blocks logs); never log keys.

**Must NOT**
- [ ] Persist plaintext objects or key material to disk.
- [ ] Publish the S3 port via compose `ports:`, the dstack gateway, or a tailnet.

---

## 9. Open Questions

- **CSK rotation**: a cluster-level CSK rotation would re-key the crypt remote and
  orphan existing ciphertext. Needs a re-encrypt migration story before any rotation.
- **Per-app credentials / prefix scoping**: v1 has one credential pair; rclone
  `--auth-key` is repeatable, so per-app pairs are cheap, but prefix isolation needs
  a fronting proxy or per-app buckets.
- **Large-object multipart**: if mesh apps need multi-GB uploads, revisit the
  multipart-in-RAM constraint (fork flag upstream, or front with a spooling proxy).

---

## 10. Alternatives Considered

- **MinIO gateway** — removed upstream; dead end.
- **Custom S3-subset proxy (SSE, streaming AES-GCM)** — full control over auth and
  metadata, but meaningfully more code to build/audit; rejected for v1.
- **Garage/SeaweedFS locally + encrypted replication to R2** — full S3 fidelity but
  two moving parts and a stateful node; conflicts with the stateless/recovery goal.
- **Local-primary with async mirror** — faster hot path, but needs disk sizing,
  sync monitoring, conflict handling; rejected in favor of write-through + cache.

---

## 11. Traceability

| Requirement | Implementation | Verification |
|---|---|---|
| Ciphertext-only in R2 | crypt remote in `s3gw-entrypoint.sh` | `verify-r2` (opaque names, no plaintext) |
| Mesh-only listener | `s3-mesh-proxy` bind=mesh-IP; no `ports:` for 19000 | `verify-isolation` (bridge IP refuses) |
| Auth gate | `--auth-key` | `verify-s3` (unauth → 403) |
| CSK-derived key / recovery | §6.1 in entrypoint | `verify-s3` after `BOX_FRESH_DISK=1` update |
| Egress lockdown | `s3gw-egress-fw` service | compose review + hung-PUT canary |
| Boot observability | `/s3gw-status/state` | manual (status volume) |

---

## 12. Changelog

| Date | Author | Changes |
|---|---|---|
| 2026-07-02 | AttestMesh | Initial spec (rclone serve s3 + crypt over R2, CSK-derived key, mesh-only, join C3). |
| 2026-07-15 | AttestMesh | Added provider-neutral R2/S3 backend settings and a resumable Smithers KMS-root recovery workflow (`deploy/workflows/r2-host-recovery.tsx`). The workflow captures the in-CVM proof, independently recovers and pins the expected KMS signer, allowlists it, simulates registration, submits idempotently, clean-rolls away the helper, and verifies the node. |
