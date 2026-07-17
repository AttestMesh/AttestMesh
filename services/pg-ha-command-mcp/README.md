# PG-HA command MCP

This package contains the command envelope validator, sender MCP, and node consumer defined in
`docs/specs/pg-ha-chain-commands.md`.

## Implementation status

The validator and node consumer exist. The current `send_command` implementation still uses the
sidecar member-send API and is development-only. Production rollout is blocked until it is replaced
with the Safe-specific submission path: resolve the registered X25519 key, seal the command, build
`sendOwnerCommand` calldata, validate it, then execute it through the configured 1-of-1 Safe. It
must not be deployed in member-send mode for `andrew-xyn-pg`.

The production MCP will be configured with fixed chain, Safe, cluster, and target-member mappings.
It will not expose arbitrary destinations, shell commands, SQL, or free-form Safe transactions.
The node consumer mounts the sidecar UDS, but the sender does not require a node shell or any
Matrix, Tailscale, or SSH ingress.
