# Webhost v1.1.24 node migration

This runbook promotes the existing `open-webhost` CVM to Webhost `v1.1.24`
without creating a new VM or disk. The measured Compose project remains
`dstack`, all six durable Docker volume names remain unchanged, and the public
origins remain:

- `https://daemon.synclave.net` — authenticated control API, admin UI, health,
  readiness, and MCP;
- `https://apps.synclave.net` — public application directory and substrate
  metadata;
- `https://*-app.synclave.net` — Fleet-brokered tenant ingress.

## Release authority

The reviewed node manifest binds immutable, locally built and inspected image
indexes for `dmvt/webhost-control` version `v1.1.24`, source commit
`afdff6377796dd889d9ae42979b25c42b1270a44`:

- control plane: `sha256:2c3fa7072686a116e2ec091581def40090cef3e5875d74aee2fe3d02cc0225ab`;
- storage helper: `sha256:388ac40ef296525264f894fc16790a2960f8289eb68801413ef651a7cd4f6203`;
- TLS proxy: `sha256:863a20e3f2fc84cbdbda141eeff5146384f6fbd801ceaf8b7fac386dc3060239`.

`preflight` pulls each exact digest and verifies its OCI source, revision, and
version labels against the release constants. GitHub Actions and mutable tags
are not deployment authority.

## Sealed inputs

Keep `/home/ubuntu/.attestmesh/webhost.env`, the two Cloudflare token files,
and the GHCR pull credential file private and non-world-readable. In addition
to the existing control, MCP, GitHub, Runyard, and backup values,
`webhost.env` must contain a real `ACME_EMAIL`. The control, MCP, Runyard hub,
and callback secrets must be pairwise distinct. Secret values are sent only in
memory to the box helper and sealed to the existing app ID.

The legacy Runyard values remain in the sealed allowlist for the reviewed
rollback topology, but the v1.1.24 control plane does not consume them. The
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
locally inspected OCI source/version/commit bindings, rejects fresh-disk mode,
checks the production target and state, and computes both measured hashes on
the box. It does not stop or update the VM.

## State and rollback boundary

The update uses dstack `UpgradeApp` on the same VM ID. The original v1.1.3
migration attempt created the immutable local snapshot before any unified
Webhost writer started. Before the v1.1.24 writer starts, the
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
Cloudflare Tunnel routing and edge TLS, unauthenticated `401`, wrong-token
`403`, and authenticated `200` through the public hostnames. Failure
automatically applies the reviewed rollback Compose on the same VM.
`migration-restore` validates the checksum and restores the snapshot before any
legacy writer starts; that rollback also carries the outbound-only tunnel and
does not republish ports on the box or CVM.

Before the v1.1.24 writer starts, the measured `telemetry-repair` one-shot
atomically renames legacy telemetry logs other than the sole active project,
`beamr-economy`, to non-active quarantine names. It then atomically writes a
durable completion marker, so subsequent restarts and future projects never
repeat this migration. This preserves the legacy bytes without following the
telemetry pathnames while allowing the fail-closed retention scanner to
validate every active log.

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
