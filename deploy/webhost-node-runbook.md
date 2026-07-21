# Webhost v1.1.21 node migration

This runbook promotes the existing `open-webhost` CVM to Webhost `v1.1.21`
without creating a new VM or disk. The measured Compose project remains
`dstack`, all six durable Docker volume names remain unchanged, and the public
origins remain:

- `https://daemon.synclave.net` — authenticated control API, admin UI, health,
  readiness, and MCP;
- `https://apps.synclave.net` — public application directory and substrate
  metadata;
- `https://*.app.synclave.net` — tenant ingress.

## Release authority

The reviewed node manifest binds these immutable, keyless-Cosign-verified
multi-platform indexes from the successful private-repository release workflow
for `dmvt/webhost-control` tag `v1.1.21`, source commit
`9def92a5c0a0c0d40cbf0eca95d6fdb0d8d3dac4`:

- control plane: `sha256:724450c1d2ea886f23385e5c595fd68e84afc9aa305f9dfa99c765cfd853516a`;
- storage helper: `sha256:f2fa0c1eec2518f5d9e7b125ac10e17810427e13a2f8d28caece9dc8cf889be8`;
- TLS proxy: `sha256:b84a6e320a8055946a2d1b05019d75dbb6bdadb82cf66d18f8f7fa230b291e41`.

`preflight` re-verifies all three signatures against the exact tagged
`.github/workflows/release.yml` identity and GitHub OIDC issuer. Mutable tags
are not deployment authority.

## Sealed inputs

Keep `/home/ubuntu/.attestmesh/webhost.env`, the two Cloudflare token files,
and the GHCR pull credential file private and non-world-readable. In addition
to the existing control, MCP, GitHub, Runyard, and backup values,
`webhost.env` must contain a real `ACME_EMAIL`. The control, MCP, Runyard hub,
and callback secrets must be pairwise distinct. Secret values are sent only in
memory to the box helper and sealed to the existing app ID.

The legacy Runyard values remain in the sealed allowlist for the reviewed
rollback topology, but the v1.1.21 control plane does not consume them. The
current Runyard member exposes only an internal HTTP hub while the production
Webhost contract requires HTTPS, and the compatibility analyzer must not use
Runyard. Enabling that optional integration later requires a separate reviewed
HTTPS endpoint change; this migration does not redesign it.

Run the non-mutating gate from a clean checkout of the reviewed AttestMesh
revision:

```bash
source deploy/env.sh >/dev/null
LOGDIR=/home/ubuntu/attestmesh/deploy/logs \
  deploy/webhost-node.sh open-webhost preflight
```

The gate renders both candidate and rollback Compose models, verifies the
release signatures and exact version/commit bindings, rejects fresh-disk mode,
checks the production target and state, and computes both measured hashes on
the box. It does not stop or update the VM.

## State and rollback boundary

The update uses dstack `UpgradeApp` on the same VM ID. The original v1.1.3
migration attempt created the immutable local snapshot before any unified
Webhost writer started. Before the v1.1.21 writer starts, the
`migration-backup` one-shot service revalidates that snapshot and checksum in
the persistent `dstack_webhost_v1_1_3_migration_backup` volume:

- `dstack_daemon_data`;
- `dstack_briefs`;
- `dstack_digests`;
- `dstack_caddy_data_v2`;
- `dstack_caddy_config_v2`;
- `dstack_sidecar-state`.

The existing app-key-sealed scheduled backup service and its external backup
configuration remain present. The local migration snapshot is a deployment
rollback boundary, not a replacement for off-VM disaster recovery.

Every new writer depends on successful snapshot completion. Candidate
readiness then proves the exact version/build commit, all dependency checks,
valid TLS, unauthenticated `401`, wrong-token `403`, and authenticated `200`
against the CVM bridge address. Failure automatically applies the reviewed
rollback Compose on the same VM. `migration-restore` validates the checksum and
restores the snapshot before any legacy writer starts.

Manual rollback is intentionally destructive to post-migration writes and must
therefore be used only for this release window:

```bash
source deploy/env.sh >/dev/null
LOGDIR=/home/ubuntu/attestmesh/deploy/logs \
  deploy/webhost-node.sh open-webhost rollback
```

## Promotion and evidence

Promote and verify with:

```bash
source deploy/env.sh >/dev/null
LOGDIR=/home/ubuntu/attestmesh/deploy/logs \
  deploy/webhost-node.sh open-webhost update
LOGDIR=/home/ubuntu/attestmesh/deploy/logs \
  deploy/webhost-node.sh open-webhost verify-release
LOGDIR=/home/ubuntu/attestmesh/deploy/logs \
  deploy/webhost-node.sh open-webhost verify
```

Retain the reviewed AttestMesh revision, candidate and rollback compose hashes,
stable app/VM ID, released image digests, on-chain allowlist transaction,
readiness/substrate JSON, auth-boundary status codes, public UI/API/CLI smoke
results, and post-update sidecar registration evidence. Do not enable Synclave
compatibility enforcement until those live checks pass.
