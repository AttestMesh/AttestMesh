# Hermes Agent Node — Operator Runbook

How to create, verify, operate, and roll a new Hermes agent node. Design and
trust notes live in [`docs/specs/hermes-node.md`](../docs/specs/hermes-node.md);
this file is the do-this-then-that.

The reference agent (Verity on the ssh node) is hand-built and **not** managed
by this trio — do not run these commands against `ssh-node`.

---

## 0. Standing prerequisites (once, already in place)

- dstack box + on-chain KMS/gateway; cluster C3 live with the **matrix**,
  **hindsight**, and **ssh** nodes (their state files under `deploy/logs/` are
  read for defaults: cluster/impl + IAPW, Hindsight TAK, mesh IPs).
- `source deploy/env.sh` (deployer key, RPC).
- `~/.teesql/ghcr-pull.toml` (image pulls), `~/.attestmesh/ssh-node-authorized-keys`
  (union ssh keys sealed into every node), `attestmesh-mesh-node` ssh alias
  (provisioning tunnel onto the mesh).
- Image: `ghcr.io/attestmesh/hermes-workbench` pinned by digest in
  `deploy/compose/hermes-node.yaml`; rebuilt by CI when
  `deploy/hermes-workbench/**` changes (re-pin the digest after any rebuild).

## 1. Per-agent prerequisites (manual, ~10 min)

Pick an agent name (lowercase, no spaces — it becomes the Matrix localpart,
Hindsight bank id, and default git user.name).

1. `bash deploy/hermes-node.sh <agent> init`
   → writes `~/.attestmesh/agents/<agent>.env` (0600 template).
2. **Fastmail** (family account): create the agent's mailbox + an app password
   → fill `EMAIL_ADDRESS`, `EMAIL_PASSWORD`.
3. **GitHub**: create the machine account (human signup; use the agent's new
   email), mint a PAT → fill `GITHUB_TOKEN` (+ `GIT_USER_NAME` if the GitHub
   handle differs from the agent name).
4. **Model key**: fill `MODEL_API_KEY` (Sakana; `fugu-ultra` is the default).
5. Optional: persona at `~/.attestmesh/agents/<agent>.soul.md`; workspace repo
   via `WORKSPACE_GIT_URL` (private repos work — the PAT is seeded first).

Leave `MATRIX_USER_ID` / `MATRIX_ACCESS_TOKEN` empty — the next step fills them.

## 2. Deploy

```
source deploy/env.sh
bash deploy/hermes-node.sh <agent> all
```

`all` = `provision-matrix → deploy → prime → bind → verify → verify-ssh → verify-hermes`:

| step | what happens | what "good" looks like |
|---|---|---|
| provision-matrix | ssh -L tunnel via the mesh shell → logs in as lsdan → DMs `@matrix-admin-agent` → `ensure_user` + `create_login_token` | `✔ provisioned @<agent>:… (token stored …)` |
| deploy | box CreateVm, secrets sealed via 0600 tmpfs env file | `✔ deployed … app_id=0x…` |
| prime | `addComposeHash` + `addAllowedAppId` on C3 | tx status 1 (skips if already allowed) |
| bind | box deployer upgrades the DstackApp proxy → ClusterMember | `✔ bound … X -> <cluster>` |
| verify | waits for on-chain registration | `✔ … memberId=0x… memberCount=N` |
| verify-ssh | SSH banner through `<app_id>-1022.<gw>:443` | `✔ ssh gateway is reachable` |
| verify-hermes | reads `/root/.hermes/gateway_state.json` over ssh | `✔ hermes gateway up, matrix connected` |

⚠️ **Membership is permanent** (no `removeMember` on C3 yet — see PR #3 /
`docs/specs/csk-rotation.md` for the eviction/rotation story). Deploy deliberately.

## 3. First-contact checklist (after `all` is green)

- **Say hello**: from Element (lsdan), DM `@<agent>:<server>` — the gateway
  auto-handles invites; allowed users default to `@lsdan` + `@fran`.
- **Paseo pairing** (to drive it from app.paseo.sh):
  `ssh` to the node (below) → `paseo daemon pair` → scan/open the link.
- **Shell access**: add to `~/.ssh/config`, mirroring the ssh-node entries:
  `<app_id>-1022.gateway.attestmesh.xyz` (bridge) and `-1023` (mesh netns),
  `User root`, `Port 443`,
  `ProxyCommand openssl s_client -quiet -connect %h:%p -servername %h`.
- **Tune anything** over ssh: `/root/.hermes/config.yaml`, `.env`, `SOUL.md`,
  `/root/.paseo/config.json`. Files are seeded only-if-missing — your edits are
  the source of truth from now on and survive in-place rolls.

## 4. Day-2

- **Roll (config/image change)**: `bash deploy/hermes-node.sh <agent> update`
  — in-place UpgradeApp, disk (and thus `/root`) preserved; re-allowlists the
  new compose hash automatically. Re-run `verify-hermes` after.
- **Fresh disk** (`BOX_FRESH_DISK=1 … update`): wipes `/root` — the agent loses
  local state and re-seeds from sealed env (Matrix crypto store resets; expect
  re-verification in encrypted rooms). Hindsight memory survives (it lives on
  the Hindsight node).
- **Rotate a credential**: edit the env file, then `update` (re-seals), then
  update the live file over ssh too (seeding won't overwrite existing files) —
  or just edit over ssh and treat the env file as the fresh-disk fallback.
- **Sizing**: `BOX_VCPU/BOX_MEM/BOX_DISK` env overrides (defaults 4/16384/60).

## 5. Troubleshooting

- **gateway idles with "no MATRIX_ACCESS_TOKEN yet"** — provisioning was
  skipped/failed; run `provision-matrix`, then either `update` (re-seal) or ssh
  in and set it in `/root/.hermes/.env` directly (gateway picks it up on its
  next loop).
- **provision-matrix times out** — it drives the admin bot's LLM; check the DM
  room from Element (the transcript shows where it stalled), then re-run (it's
  idempotent; `ensure_user` is absolute-state, a fresh token is minted).
- **verify-hermes 30× "not connected"** — ssh in (`-1022`), read
  `/root/.hermes/logs/gateway.log`; the usual suspects are a bad token or the
  matrix mesh IP (must be the matrix node's `matrix-mesh-proxy` at :18080).
- **TEE blocks docker logs** — same as every node: diagnose via the shells and
  the self-reported files (`gateway_state.json`, `logs/*.log`), not `vm_logs`.
- **Host key changed after fresh-disk roll**:
  `ssh-keygen -R '[<host>]:443'` (in-place rolls keep host keys — they live on
  the volume).
