# PitchRotator MCP node runbook

This runbook operates the dedicated PitchRotator AttestMesh network defined by
`docs/specs/pitchrotator-mcp-network.md`. The checked-in deployment is a
repeatable pre-production target for synthetic data. It is not authorization to
send founder material: the upstream receipt, attestation, egress, and session
hardening blockers in issue 35 must be closed and independently reviewed first.

## Prerequisites and publication gate

- A dstack/TDX box supported by the existing box deployment tooling, operator
  SSH access to it, and a separate mesh-member SSH host for private probes.
- The Base deployment credentials consumed by `deploy/env.sh`, Foundry, Python
  3, `jq`, `curl`, Bun, and the pinned Smithers dependencies under `deploy/`.
- `SECRETS_FILE` (default
  `~/.attestmesh/pitchrotator-mcp.env`) with mode `0600` and an
  `OPENROUTER_API_KEY` entry. `PITCH_MODEL` is optional sealed configuration.
  Never add either value to Smithers JSON, argv, source, or logs.
- The PitchRotator source commit and source-archive SHA-256 in
  `deploy/pitchrotator-mcp-node.sh` must match the reviewed revision. Every
  deployed workload image must use an `@sha256:` reference. Supply the reviewed
  `PITCHROTATOR_IMAGE` digest when no immutable default has been published. Stop
  if either invariant fails; tag-only images and unverified source archives are
  not deployable.
- Verify the checked commit tree, deterministic local `git archive`, and npm
  lockfile digests recorded by the driver. A GitHub API-generated tarball is not
  a durable release artifact and must not be substituted as the trust anchor.
- Review and allowlist the rendered compose hash through the standard on-chain
  admission path before Path A registration. A changed source digest, image
  digest, compose file, or sealed configuration requires a new measurement and
  a new review; do not reuse prior evidence.

Run the offline contract gate before touching a box:

```bash
python3 -m unittest deploy/tests/test_pitchrotator_mcp_node_contract.py
```

## Bring up the dedicated network

Load operator configuration without printing it, then start the durable
workflow. The input contains only the non-secret node label.

```bash
source deploy/env.sh
export PITCHROTATOR_IMAGE='ghcr.io/approved/pitchrotator-mcp@sha256:REPLACE_WITH_REVIEWED_64_HEX_DIGEST'
bunx smithers-orchestrator up deploy/workflows/pitchrotator-mcp-node.tsx \
  --input '{"node":"pitchrotator-mcp-node"}'
```

Replace the image example with the reviewed digest; the driver rejects the
placeholder, tags, and malformed digests. Create the secrets file separately
with mode `0600` and edit it interactively so its value never enters shell
history.

The workflow runs, in order: CVM deployment, creation of a new cluster, Path A
membership, key priming, member binding, CVM start, chain/mesh verification, and an MCP
smoke test. It must not inherit a Matrix, PG-HA, or other fleet's cluster state.
On failure, correct the cause and use the run ID printed by Smithers:

```bash
bunx smithers-orchestrator up deploy/workflows/pitchrotator-mcp-node.tsx \
  --run-id <run-id> --resume true
```

For diagnosis or a narrowly scoped rerun, invoke the same idempotent primitives
directly:

```bash
deploy/pitchrotator-mcp-node.sh pitchrotator-mcp-node verify
deploy/pitchrotator-mcp-node.sh pitchrotator-mcp-node verify-mcp
```

Use `update` for a reviewed day-2 roll. Preserve the existing app identity and
membership; a replacement measurement must already be allowlisted.

## Required evidence

Retain redacted command output and public identifiers sufficient to prove:

1. The new cluster and member exist on-chain and the admitted app/compose
   identity matches the reviewed measurement.
2. The sidecar joined WireGuard and the service is reachable from the designated
   mesh probe host, while the box publishes no host port and no public gateway.
3. `verify-mcp` obtained a healthy response and an attestation response with
   production TDX mode and `trusted: true`. This smoke check does not replace
   client-side cryptographic quote and measurement verification.
4. The compose used the reviewed upstream commit/archive digest and digest-pinned
   images, with simulator and insecure-no-TEE flags absent.
5. Logs and process arguments contain no founder text, model payloads, API keys,
   session keys/IDs, sealed environment, or raw quote challenges.

After verification, use only synthetic excerpts for smoke testing. Do not claim
production confidentiality or receipt integrity until the upstream blockers are
implemented, tested, and the resulting immutable artifact is measured again.
