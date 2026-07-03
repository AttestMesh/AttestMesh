# Hermes Agent Node

**Status**: IMPLEMENTED (deploy trio, 2026-07-02; not yet exercised end-to-end)
**Related**: [`matrix-admin-agent.md`](./matrix-admin-agent.md) (account provisioning), [`csk-rotation.md`](./csk-rotation.md) (eviction/rotation posture), [`../../deploy/hermes-node-runbook.md`](../../deploy/hermes-node-runbook.md) (operator runbook), `deploy/compose/hermes-node.yaml`, `deploy/hermes-node.sh`, `deploy/hermes-node-box.py`, `deploy/hermes-matrix-provision.py`, `deploy/hermes-workbench/Dockerfile`

## 1. Purpose

A repeatable AttestMesh node type that runs one [Nous Research Hermes Agent](https://github.com/NousResearch/hermes-agent) per CVM, so the operator can deploy many agents. Each agent gets **its own identity everywhere**:

| Identity | Source | Provisioning |
|---|---|---|
| Matrix account `@<agent>:<server>` | cluster Matrix node (mesh-only Synapse) | **automated** — `provision-matrix` drives the matrix-admin-agent LLM bot (`ensure_user` + `create_login_token`) as the admin operator over a mesh tunnel |
| Hindsight memory bank `bank_id=<agent>` | Hindsight node (mesh-only API) | automatic — bank ids are namespaced per agent under the shared tenant key |
| Email mailbox | Fastmail (family account) | **manual** — operator creates the mailbox + app password |
| GitHub identity | machine account | **manual** — operator creates the account + PAT (GitHub ToS requires human signup) |
| Model key | Sakana `fugu-ultra` (default) | operator supplies the key |

The reference for the node shape is the hand-built agent on the ssh node ("Verity"); that node is **not managed** by this trio and stays untouched.

## 2. Architecture

`deploy/compose/hermes-node.yaml` — five services, four sharing the `ghcr.io/attestmesh/hermes-workbench` image (ubuntu 26.04 workbench + sshd + Hermes editable install at a pinned upstream commit + uv CPython 3.11 + Node 22 + Paseo CLI; built by `.github/workflows/build-hermes-workbench.yml`):

- `sidecar` — cluster-mesh-agent (Path A member; wg mesh; :9090 health, :51900 wg)
- `sshd` :1022 — bridge-netns workbench shell (gateway TLS-passthrough)
- `sshd-mesh` :1023 — sidecar-netns shell ON the mesh (`ssh -D` = SOCKS onto the mesh)
- `hermes-gateway` — supervised `hermes gateway` in the sidecar netns (reaches the Matrix node's mesh listener + Hindsight over wg)
- `paseo` — Paseo daemon :6767 (sidecar netns ⇒ mesh-reachable only, not gateway-published); sole provider = `hermes acp`

**Config-over-ssh invariant**: the sealed env is used ONLY for first-boot seeding of `/root/.hermes/{.env,config.yaml,SOUL.md,hindsight/config.json}`, `/root/.paseo/config.json`, and git/gh credentials — each rendered *only if missing*. After that, the `/root` workspace volume is the source of truth and the operator tunes the agent over ssh; in-place rolls never clobber edits. sshd host keys are generated onto the volume (stable across rolls, never baked in the image).

## 3. Operator flow

```
bash deploy/hermes-node.sh <agent> init          # writes ~/.attestmesh/agents/<agent>.env template
# create Fastmail mailbox + GitHub machine account; fill env file
# optional persona: ~/.attestmesh/agents/<agent>.soul.md
bash deploy/hermes-node.sh <agent> all           # provision-matrix → deploy → prime → bind → verify → verify-ssh → verify-hermes
```

Day-2: `update` (in-place roll, disk preserved), `verify-hermes` (reads the gateway's self-reported `gateway_state.json` over ssh; expects `matrix: connected`).

## 4. Trust & caveats

- **Permanent membership**: the cluster has no `removeMember` — every agent node is a permanent member. Deploy deliberately.
- Secrets ride to the box helper via a 0600 tmpfs env file, not argv.
- `provision-matrix` drives an LLM channel (best-effort, bounded confirms). The deterministic upgrade is the **on-chain command channel**: seal `MATRIX_ADMIN_SENDERS` (an authorized member id) on the matrix node and speak `attestmesh.matrix-admin.command.v1` — see matrix-admin-agent spec §7/§8.1. Adopt at the next planned matrix-node roll.
- Agent egress is currently **unrestricted** (workbench semantics). If an agent should be locked down, add the `agent-egress-fw` pattern from the matrix node as a follow-up.
