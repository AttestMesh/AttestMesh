# Direct UDP punch canary

This runbook closes the live-validation portion of issue #36 without weakening
the gateway WireGuard-over-TCP bootstrap/fallback or exposing host-local services.
It is written for two persistent prod5 members whose topology is expected to
support two-sided punching.

## Safety and completion boundary

- Never publish sidecar port `9090`; collect health and metrics from the CVM or
  an approved host-local/mesh-local shell.
- Never use `wg show ... dump`: its interface row contains the WireGuard private
  key. The helper uses only `endpoints`, `latest-handshakes`, and `transfer`.
- Never record sealed env, private keys, CSKs, API tokens, RPC credentials, or
  credential-bearing URLs.
- Do not roll a node marked frozen until the issue owner explicitly resolves the
  freeze. As of 2026-07-19, repository operations docs mark `ssh-node` frozen.
- Gateway TCP `:51900` must remain reachable throughout the canary.
- A restricted-NAT pair is expected TCP fallback, not proof that the rollout is
  broken. Rotate to another approved persistent pair for the positive canary.

The code/config/docs portion can merge before the live run. The issue cannot
close until the full 24-hour soak and forced-failure recovery are recorded.

If the workbench freeze remains in force, use the prepared persistent
**Hindsight ↔ Fugu-router** pair. Hindsight's existing mesh-only provider calls
to the router are the sustained workload. Do not route the canary through the
workbench merely for evidence collection.

## 1. Establish image provenance

Inventory live members from their on-chain compose hash, reconcile that against
the deployed compose and `docker inspect` inside the CVM, then record every live
sidecar digest in `docs/deployment.md`. Generated files under
`deploy/orchestrator/compose/` are not fleet inventory.

For a candidate image, verify the OCI source revision before rollout:

```bash
IMAGE=ghcr.io/attestmesh/cluster-mesh-agent@sha256:<digest>
docker buildx imagetools inspect "$IMAGE" --format '{{json .Image.config.Labels}}' \
  | jq '{source: .["org.opencontainers.image.source"], revision: .["org.opencontainers.image.revision"]}'
git merge-base --is-ancestor 12a8289 <revision>
```

Exit status zero from `merge-base` is required. This repository's sidecar build
workflow now stamps `org.opencontainers.image.revision` on all new images.

Known pre-canary state:

| Digest | Source/build evidence | Punch code? | Decision |
|---|---|---|---|
| `b9e0ae107d9d...` | Built 2026-07-11; no OCI source labels; deployment record calls it an isolated CSK-only build | unproven | rebuild candidate from labeled source; do not canary |
| `e3d53442aa6e...` | GHCR tag maps to `449da276136d...`; that tree has no `sidecar/src/transport/punch.rs` | no | rebuild and roll before canary |
| `b4f662820088...` | OCI revision `3bd212617971...`; built 2026-07-19; revision descends from `12a8289` | yes | issue #36 canary candidate; pending approved roll |

## 2. Pin the canary image and explicit configuration

Build the candidate from the reviewed issue branch, record its immutable digest
and OCI revision, then replace the canary pair's old sidecar digest. Both canary
composes must explicitly contain:

```yaml
- WG_UDP_PUNCH=true
- PUNCH_TIMEOUT_SECS=10
- PUNCH_RETRY_BACKOFF_SECS=30
- WG_TCP_PORT=51900
- WG_LISTEN_PORT=51821
```

Allowlist each new compose hash before an in-place roll. Preserve the member
app ID and durable disk. After each roll, reconcile the running image digest with
the compose and verify normal health before touching the second member.

## 3. Capture TCP bootstrap and UDP latch

Copy `deploy/udp-punch-canary.sh` into an approved shell sharing the sidecar
network namespace. Identify the peer's hex member ID and WireGuard public key;
both are public identifiers.

Immediately after normal mesh bring-up, capture the TCP/bootstrap state:

```bash
./udp-punch-canary.sh snapshot >before.json
```

Then wait through the initial attempt plus retries and capture the UDP state:

```bash
./udp-punch-canary.sh wait-udp <hex-member-id> 180 >latched.json
```

The evidence must show all of the following on both nodes:

- `transports[member_id] == "udp"`;
- `punch_peer_status[member_id] == "udp"`;
- `attestmesh_punch_success_total >= 1`;
- `attestmesh_peer_transport{...,transport="udp"} 1`;
- a non-loopback `wg` peer endpoint different from the TCP bridge. Record the
  observed external port; it need not equal `51821` after NAT remapping.

## 4. Run the continuous 24-hour soak

Run one workload request at least every 30 seconds for 24 hours. Put any required
credential in a short wrapper that reads it from the operator environment; do not
place credentials in arguments, logs, or this repository. The wrapper must emit
no response body. For example:

```bash
./udp-punch-canary.sh soak <hex-member-id> 86400 30 -- ./canary-workload
```

The helper emits JSONL samples and fails immediately if transport/status leaves
UDP, punch success is zero, the workload fails, or `udp_reverts_total` changes.
Redirect stdout to a redacted evidence file. Continuous heartbeats remain the
second workload and the 24-hour window spans many WireGuard rekeys.

## 5. Force direct UDP failure without breaking TCP fallback

After the clean soak, on one canary only, install a rule scoped to the observed
remote endpoint and local WireGuard source port. The workbench compose includes
`iptables` for this purpose.

```bash
./udp-punch-canary.sh fault-add <peer-wireguard-public-key>
```

Do not block `:51900`, kill either container, or drop all traffic. Within roughly
three to four minutes, capture evidence that:

- transport becomes `tcp` and per-peer punch status becomes `reverted`;
- `attestmesh_udp_reverts_total` increments once;
- health, heartbeats, and the mesh workload remain live over gateway TCP.

Always remove the rule, including after a failed verification:

```bash
./udp-punch-canary.sh fault-remove
./udp-punch-canary.sh wait-udp <hex-member-id> 180 >relatched.json
```

Confirm the link re-upgrades and the workload still succeeds.

## 6. Record the result

Add a dated row to `docs/deployment.md` with cluster, redacted member identifiers,
digest, source revision, observed endpoint port, 24-hour soak interval, workload
cadence, punch count, revert count, and successful TCP fallback/re-latch. Include
only the redacted fields emitted by the helper. Mark maturity `CANARY-PROVEN` only
after both members meet the evidence requirements; reserve `FLEET-ADOPTED` for a
separate fleet-wide inventory showing qualifying links prefer UDP.
