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
4. **Model endpoint**: default is `fugu-router` with `fugu-ultra`; the deploy
   helper resolves the router URL from `deploy/logs/fugu-router-node-fugu-router.state`
   and the LiteLLM key from `~/.attestmesh/fugu-router.env`. For direct Sakana,
   set `MODEL_PROVIDER_NAME=custom`, `MODEL_BASE_URL=https://api.sakana.ai/v1`,
   and `MODEL_API_KEY=<Sakana key>`.
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
| register-direct | no live 4337 bundler (Alchemy dead): fetches the sidecar helper's calldata (HTTP :9092 via gateway, serial-log fallback) and the deployer sends it, paying gas | `✔` + tx status 1 |
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

Maiden-deploy (tessera, 2026-07-09) lessons baked into the trio — for awareness:

- **Model keys**: the fugu-router LB (`10.18.133.81:18410`) only accepts LiteLLM
  `sk-…` virtual keys; the `LITELLM_MASTER_KEY` in `~/.attestmesh/fugu-router.env`
  can go stale across fugu blue/green rolls. If the agent gets 401s, mint a fresh
  per-agent virtual key (LiteLLM `/key/generate` with the CURRENT master key) and
  update both the agent env file and `/root/.hermes/{.env,config.yaml}` on the node.
- **Never run `hermes gateway`/`hermes gateway restart` from the ssh shells** —
  it starts a rogue gateway in the WRONG netns (bridge = no mesh routes) that
  steals the lock from the container gateway; matrix then times out forever
  while email (public egress) still works. Recovery: kill the rogue in the
  :1022 shell, `rm /root/.hermes/gateway.lock gateway.pid`, and if the container
  gateway died, an in-place `update` roll restarts everything cleanly.

- **gateway idles with "no MATRIX_ACCESS_TOKEN yet"** — provisioning was
  skipped/failed; run `provision-matrix`, then either `update` (re-seal) or ssh
  in and set it in `/root/.hermes/.env` directly (gateway picks it up on its
  next loop).
- **provision-matrix times out** — it drives the admin bot's LLM; check the DM
  room from Element (the transcript shows where it stalled), then re-run (it's
  idempotent; `ensure_user` is absolute-state, a fresh token is minted; blank
  `MATRIX_ACCESS_TOKEN=` in the env file first or it skips).
- **⚠️ Matrix tokens expire after 1 year** — the admin agent's
  `create_login_token` caps `valid_hours` at 8760 and coerces omitted values to
  24h (executor bug vs its docstring: None should mean no expiry — fix at the
  next matrix-admin-agent roll). Rotate before expiry: blank the token in the
  env file → `provision-matrix` → `update` (or paste into
  `/root/.hermes/.env` over ssh and restart the gateway container-free via a
  roll). Minted 2026-07-03 for tessera → rotate by 2027-07.
- **verify-hermes 30× "not connected"** — ssh in (`-1022`), read
  `/root/.hermes/logs/gateway.log`; the usual suspects are a bad token or the
  matrix mesh IP (must be the matrix node's `matrix-mesh-proxy` at :18080).
- **TEE blocks docker logs** — same as every node: diagnose via the shells and
  the self-reported files (`gateway_state.json`, `logs/*.log`), not `vm_logs`.
- **Host key changed after fresh-disk roll**:
  `ssh-keygen -R '[<host>]:443'` (in-place rolls keep host keys — they live on
  the volume).
