# PG-HA blockchain command protocol

PG-HA administrative requests use AttestMesh `MessageFacet`; the sidecar encrypts every
payload to the recipient's registered X25519 key. Plaintext is never placed on chain.

The decrypted payload is canonical JSON (UTF-8, no duplicate keys):

```json
{
  "protocol": "attestmesh.pgha.command.v1",
  "command_id": "018f5f2e-7b89-7abc-8def-0123456789ab",
  "cluster": "andrew-xyn-pg",
  "target": "pg2",
  "issued_at": "2026-07-16T12:00:00Z",
  "expires_at": "2026-07-16T12:05:00Z",
  "mode": "explain",
  "command": "patroni.status",
  "arguments": {},
  "reason": "Investigate replica lag"
}
```

Required invariants:

- `command_id` is a UUIDv7 and is the replay/idempotency key.
- `cluster` and `target` must match the receiving node's sealed configuration.
- the sender must equal `keccak256("attestmesh.cluster-owner.v1" || clusterOwner)`; owner
  messages are submitted by the Safe through `sendOwnerCommand` and do not create a hidden member.
- `expires_at - issued_at` is at most five minutes and receiver clock must be in that window.
- `mode=explain` is read-only. `mode=execute` is accepted only for an allowlisted command;
  arbitrary shell, SQL, URLs, and LLM-supplied tool calls are never executed.
- v1 commits terminal status to the node-local replay/audit ledger. It does not pretend the
  Safe pseudonym is a member or send a result through `Agent.SendMessage`; a future return
  channel requires an explicit owner reply encryption key and a separate result event.

Initial command allowlist:

| Command | Modes | Effect |
|---|---|---|
| `patroni.status` | explain, execute | Read Patroni `/cluster` and local PostgreSQL state |
| `patroni.explain` | explain | Ask redpill to interpret supplied status/error text using the fixed PG-HA system prompt |
| `backup.status` | explain, execute | Read last WAL/dump activity and retention state |
| `patroni.switchover.plan` | explain | Produce a plan only; never changes leadership |

Redpill receives bounded diagnostic data and the fixed Patroni reasoning prompt. Its output is
advisory text, never a command line. The command agent has egress only to the local Patroni,
Postgres, sidecar UDS, and the pinned redpill endpoint.

The deployment contains no Matrix, Tailscale, SSH daemon/member, web administration listener,
or emergency network control path. The only administrative ingress is the encrypted owner-command
event; CVM lifecycle operations remain at the dstack runtime layer.

## Safe submission

The sender MCP resolves the target's registered X25519 public key, seals the canonical JSON,
and generates `sendOwnerCommand(recipientMemberId,envelopeId,ciphertext)` calldata. The 1-of-1
Safe signs and executes that call against its cluster. The MCP must verify the Safe address,
chain ID, cluster address, recipient membership, and decoded calldata before submission. It
does not expose arbitrary Safe transactions.

The event sender ID is domain-separated as
`keccak256(abi.encodePacked("attestmesh.cluster-owner.v1", safeAddress))`. The receiving agent
derives the same value from its sealed cluster/Safe configuration; it does not accept a freely
configured sender list in production.
