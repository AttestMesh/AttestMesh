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
