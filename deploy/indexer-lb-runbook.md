# Indexer Load Balancer / Blue-Green Runbook

## Shape

`attestmesh-indexer-lb` is a small C3 member that owns the stable public Indexer endpoints:

```text
sidecars ── gRPC/TLS ──> <lb-app>-50052.gateway.attestmesh.xyz
                              │
                              └─ HAProxy TCP ──> active Indexer :50052

operators ── C3 mesh ──> <lb-mesh-ip>:50053 prepare / commit / abort
public diagnostics ─────> <lb-app>-9090.gateway.attestmesh.xyz
                              │
                              └─ HAProxy HTTP ─> active Indexer :9090
```

The LB has no Indexer signing key and never terminates the gRPC protocol. `IndexerRegistry.current()` keeps the stable LB endpoint but always carries the active backend's compose/code ID and Ed25519 signing pubkey.

## Current Base deployment

As of 2026-07-11, the production blue/green pair is:

| Role | App ID | Detail |
| --- | --- | --- |
| Stable LB | `0x42B122e37c9805E7C5A7c77aFF0f6C3c80603602` | mesh control `10.18.1.13:50053` |
| Active blue | `0x0ab50663D241C2A34a57Bb05c50018E680a92a21` | compose/code ID `0xf6e46e33dce5ecb83e5877e7ff519f8e48974af160646c840575f60298edb9df` |
| Standby green | `0x74C0370C675af3a5a2c89FE8D90dBC433fBDB7d1` | retained as the immediate rollback backend |

The stable gRPC endpoint is `https://42b122e37c9805e7c5a7c77aff0f6c3c80603602-50052.gateway.attestmesh.xyz`; stable HTTP diagnostics use the same host with port `9090`. Both colors run Indexer image `ghcr.io/attestmesh/attestmesh-indexer@sha256:14e9e3f6869ada585642c14e4e2e43bee197978ffc68d146dd6d3cddbc49d2bb`.

Treat the on-chain registry and `active` command as authoritative if this record is stale. Keep green running until the protocol-v2 Sidecar rollout is complete and subscriber/checkpoint diagnostics remain caught up.

## Why the switch is two-phase

An Indexer backend has an attestation-derived signing key and its own cursor database. A plain HAProxy address change would send reconnecting sidecars to a key that does not match `IndexerRegistry`, and a new backend could otherwise treat them as new subscribers.

The implemented cutover is:

1. `prepare`: probe candidate `:50052`, require healthy `:9090`, and verify `/status.pubKey`; pause new LB frontend accepts while existing streams continue.
2. Update `IndexerRegistry` to the stable LB endpoint plus candidate code ID/pubkey.
3. `commit`: re-probe, switch both HAProxy backends, close old LB streams, and reopen the frontends.
4. Sidecars reconnect, re-read the registry, and send their last handled block. The candidate replays that block, so same-block duplicates are possible but gaps are not.

The sidecar persists the last signed checkpoint in `SIDECAR_STATE_DIR/indexer-cursor.v1`. Roll the sidecar image containing this behavior before relying on blue/green Indexer switching.

If the registry transaction fails, the driver aborts the prepare and keeps the old backend. If commit fails after the transaction, it restores the previous registry record before reopening the LB.

## One-time migration from a direct endpoint

Deploy and verify a separate green candidate first; do not use the currently registered direct backend as the LB's first candidate.

```bash
source deploy/env.sh

# Candidate is fully indexed and healthy but does not touch IndexerRegistry.
deploy/indexer-member-node.sh attestmesh-indexer-c3-green candidate

# Deploy the stable LB and atomically select green.
deploy/indexer-lb-node.sh attestmesh-indexer-lb all attestmesh-indexer-c3-green

# Inspect the stable endpoint, registry key, and active backend.
deploy/indexer-lb-node.sh attestmesh-indexer-lb verify-lb
deploy/indexer-lb-node.sh attestmesh-indexer-lb active
```

Sidecars already connected directly to the old Indexer will remain on that established connection until it closes. After confirming the registry points to the LB and the green backend is active, stop the old direct backend to force those remaining streams through the LB:

```bash
FORCE_CLEANUP=1 deploy/indexer-member-node.sh attestmesh-indexer-c3 stop
```

Confirm sidecar diagnostics show `indexer_connected=true`, `indexer_caught_up=true`, and cursor movement before removing any old deployment resources.

## Subsequent hot switch

```bash
source deploy/env.sh

deploy/indexer-member-node.sh attestmesh-indexer-c3-next candidate
deploy/indexer-lb-node.sh attestmesh-indexer-lb switch attestmesh-indexer-c3-next
deploy/indexer-lb-node.sh attestmesh-indexer-lb verify-lb

# Keep the old candidate available until the fleet has reconnected and checkpointed.
FORCE_CLEANUP=1 deploy/indexer-member-node.sh attestmesh-indexer-c3-green stop
```

Named candidates default to their same-host bridge IP because it avoids routing the Indexer's public serving path back through the mesh gateway transport. Set `INDEXER_LB_BACKEND_MODE=mesh` to use the candidate's C3 mesh IP, or pass a literal private IP together with `INDEXER_BACKEND_CODE_ID`.

## LB operations

```bash
# Generate the 0600 mesh-control secret.
deploy/indexer-lb-node.sh attestmesh-indexer-lb setup

# Show active/prepared state and HAProxy runtime state.
deploy/indexer-lb-node.sh attestmesh-indexer-lb active

# Recover an operator-abandoned prepare (reopens the old frontends).
deploy/indexer-lb-node.sh attestmesh-indexer-lb abort

# Roll the LB compose itself; its active-backend volume survives the update.
deploy/indexer-lb-node.sh attestmesh-indexer-lb update
```

The control key lives in `~/.attestmesh/indexer-lb.env`. The control listener binds only to `attestmesh0:50053`; it is not a public gateway API.

## Expected delivery semantics

- Backend changes intentionally close gRPC streams; sidecars reconnect with backoff.
- The last checkpoint block is replayed on a backend with no server cursor. Duplicate events from that block are expected and preserve at-least-once delivery.
- Candidate and old Indexers can index concurrently; they are read-only apart from their independent cursor volumes.
- Do not stop the old backend until the stable public `/status` pubkey matches `IndexerRegistry.current()` and fleet diagnostics have caught up.
