# AttestMesh Deploy Orchestration — Component Spec

**Status**: Draft v0.1
**Parent spec**: [`attestmesh-coordination-layer.md`](./attestmesh-coordination-layer.md)
**Component**: `deploy/` — Smithers workflows wrapping the Foundry scripts, the indexer/sidecar image builds, and the gas-webhook deploy
**Last updated**: 2026-06-02

---

## 1. Purpose

AttestMesh bring-up is a long, ordered, transaction-heavy choreography: deploy per-chain infrastructure, deploy + atomically bootstrap a `ClusterDiamond`, then bring up N attested nodes — with every node-originated call sponsored through the EIP-4337 paymaster and several hard bootstrap cycles (Indexer→registry; predict-member→compose-hash→cluster→member). This spec defines how that choreography is **agentically orchestrated** — durably, with human-approval gates, and using attested model inference — per the project directive that every step be agent-operable.

The deploy is driven by **Smithers** (a durable agent-workflow runtime) over the existing deterministic tooling (Foundry scripts, `wrangler`, `docker`), with **redpill.ai** as the model provider for the judgment/diagnosis tasks. The chain stays the only required coordination surface — there is no off-chain coordinator. This orchestration's job is to *produce on-chain state and boot nodes*, not to broker between them.

This spec defines *what the orchestration does and how it is structured*; the concrete deploy steps it drives are derived from the component specs (contracts §9.2/§10/§11/§12, indexer, sidecar, gas-webhook) and enumerated in §5.

> **Status note.** This is a design spec for a v1-and-beyond capability. It records the agreed toolchain (Smithers + redpill, per the project directive) and the orchestration shape; it is not a commitment to build the orchestrator before the contracts/sidecar/indexer exist. Generation order is the operator's call (§9).

---

## 2. Toolchain

- **Smithers** (`smithers-orchestrator`, [smithers.sh](https://smithers.sh)) — a durable execution runtime for long-running agent workflows. Bun ≥ 1.3 + local SQLite; workflows authored in TypeScript/JSX (`Task` / `Sequence` / `Parallel` / `Branch` / `Loop` / `Approval`), task outputs Zod-validated. Properties this spec relies on: **a completed task is never re-executed**, crash-resume (`--resume`), per-task **idempotency keys** for side-effecting work, and **first-class human-approval gates**. Self-hosted (no managed cloud); built by the `evmts` Ethereum-tooling org. Pre-1.0 (≈ v0.22) — **pin a version**; the DSL/CLI may churn.
- **redpill.ai** — an OpenAI-compatible inference gateway (`https://api.redpill.ai/v1`, `Authorization: Bearer`), TEE-protected, wired via the Vercel AI SDK `@ai-sdk/openai-compatible` provider. Confidential `phala/…` models run fully inside GPU enclaves and carry per-response TEE attestation (Intel TDX quote + NVIDIA GPU proof); they are built on **Phala/dstack — the same attestation stack AttestMesh anchors on**. Generic `openai/…`-style models get only gateway protection (no inference attestation).
- **Deterministic tooling** the orchestration shells out to (Smithers `compute` tasks): **Foundry** (`forge build`, the `script/Deploy*.s.sol` scripts, `cast` for verification reads), **`wrangler`** (gas-webhook Worker deploy), **`docker`** (indexer + sidecar OCI image build/push).
- **Signing is not done by Smithers** (§4): the org/cluster **Safe** signs infrastructure and per-cluster transactions; each node signs its own UserOps from TEE-derived keys and holds zero ETH.

---

## 3. Where it lives

```
deploy/
├── workflows/
│   ├── infra.tsx          # PHASE 1 — per-chain infrastructure
│   ├── cluster.tsx        # PHASE 2 — one ClusterDiamond
│   ├── node.tsx           # PHASE 3 — one attested node (mapped ×N)
│   └── deploy-all.tsx     # top level: infra → cluster → node×N
├── tasks/                 # compute-task wrappers (forge / cast / wrangler / docker shells)
├── agents/                # redpill-backed agent defs (config-gen, verify, diagnose)
└── smithers.config.ts     # model providers (redpill), sandbox, approval policy
```

The Foundry scripts themselves (`DeployInfra.s.sol`, `DeployCluster.s.sol`, `DeployMember.s.sol`) live in `contracts/script/` (contracts spec §12); the Smithers workflow invokes them and consumes their `script/deployments/<chainId>.json` output as typed task outputs.

---

## 4. Trust model & secrets

Smithers is **untrusted glue running around trusted, attested components**. Deploy secrets never enter Smithers' state:

| Secret / key | Holder | Smithers' relationship |
|---|---|---|
| Org Safe / Cluster Safe signers | Humans / hardware | Smithers *proposes* the tx; a **human approves and the Safe signs**. Never in Smithers. |
| Deployer EOA (Foundry-script gas) | Operator keystore / env | Supplied to the `forge`/`cast` `compute` task's environment at run time; never committed. |
| `RPC_URL`, `BUNDLER_URL`, `ALCHEMY_WEBHOOK_TOKEN` | Worker / CVM env, `wrangler secret` | Passed via env to the relevant `compute` task; never in source or Smithers' SQLite. |
| Node TEE keys (x25519 / ed25519 / wg / k256 / CSK) | each CVM's TEE | Never leave the TEE; the deploy never handles them. CVMs hold **zero ETH** (paymaster). |
| dstack KMS root | Phala managed KMS | External; only its address is allowlisted, never held by us. |

Smithers has **no secrets vault** — state is a local SQLite file and credentials are env-vars — so: (a) never hand it a signing key, (b) treat its host as sensitive, (c) sign via the Safe/keystore *external* to the workflow. This keeps the deploy within the CLAUDE.md directives (no secrets in source control; CVMs hold zero ETH).

**Orchestrator trust domain (open — §9).** For v1 the Smithers runtime runs on an operator box. The attestation-native option — run Smithers **inside a dstack CVM** and use redpill `phala/…` models — places the entire deploy pipeline in the same attestation trust domain AttestMesh itself anchors on (the orchestrator is attested; its AI judgments carry TEE proofs). Smithers can run this way (it is a plain process) but provides none of it; the dstack/attestation layer is entirely ours.

---

## 5. The deploy DAG

Three phases, globally ordered; derived from the component specs. Each step notes its driver and how success is verified on-chain. Full per-step detail (inputs, idempotency, verify criteria) tracks the cited component specs.

> The chain is the only required coordination surface (master §13 item 5 / the off-chain-coordination directive). The orchestration produces on-chain state and boots CVMs; it does not broker between nodes.

### Phase 1 — INFRA (one-time per chain; org Safe)
Driver: `forge script DeployInfra.s.sol` for the on-chain items, then image builds + Worker deploy.
1. `forge build` (Solc 0.8.24, EVM `cancun`) — prerequisite artifact; ABIs feed indexer/sidecar codegen and the webhook decoder.
2. Deploy the four facet impls (Attest / Message / Network / Dstack).
3. Deploy `DiamondInit` and the `ClusterMember` impl. (EntryPoint v0.7 is canonical and pre-existing — never deployed.)
4. Deploy `ClusterMemberFactory` (member impl + org-Safe owner) and `ClusterDiamondFactory` (facets + DiamondInit + org-Safe owner) — contracts §9.2 / §10.
5. Deploy `IndexerRegistry` (org-Safe owner) — contracts §11.
6. Build + publish the **indexer** OCI image; boot the single Indexer CVM (derives its ed25519 signing key from the TEE each boot).
7. **Set the IndexerRegistry record** `{ endpoint, codeId, pubKey }` — *after* the Indexer is live (this breaks the bootstrap cycle; the Indexer tolerates the pre-set window). **[approval gate]**
8. Deploy the **gas-webhook** Worker (`wrangler`), hard-configured with the real factory addresses; wire the Alchemy paymaster → webhook and fund the org Alchemy account. **[approval gate — funds]**
9. Build + publish the **sidecar** OCI image.

*Critical edge:* the webhook (8) must be live with the real factory addresses **before any node can register** — every node-originated call is a sponsored UserOp.

### Phase 2 — PER-CLUSTER (one ClusterDiamond; cluster Safe)
Driver: `forge script DeployCluster.s.sol` + a JSON config.
1. **Author the cluster config** — `clusterOwner` Safe, KMS root, initial compose hashes, device ids, `meshCidrIp/Prefix` (default `10.13.0.0/16` = `0x0a0d0000` = `168624128`), salt (contracts §12.2). **[agent task: generate + validate from operator intent]**
2. `ClusterDiamondFactory.deployCluster(initArgs, salt)` — CREATE2 + atomic `DiamondInit` bootstrap (seeds allowlists, cluster owner, and CIDR in one tx; the diamond is never reachable unconfigured). **[approval gate, hard on mainnet]**
3. Cluster Safe `acceptOwnership()` (solidstate owner). Verify `isDeployedCluster(cluster)` → true — the webhook will now sponsor calls to it.

### Phase 3 — PER-NODE (×N; the v1 demo has 3)
Driver: `forge script DeployMember.s.sol` for the address; the register/publish steps run *inside the CVM's sidecar*.
1. **Predict + deploy** the `ClusterMember` address (CREATE2, `owner = 0`). The address is predictable before boot, which lets the operator bake it into the dstack compose config as `app_id`.
2. Finalize the CVM compose config (`app_id` = predicted member; pinned sidecar image) → compute the **compose hash** that must be in the cluster's allowlist. *(This is why member prediction + compose-hashing precede Phase 2's config — the chicken-and-egg.)*
3. Boot the CVM; dstack KMS boot-gate (`isAppAllowed`).
4. Sidecar derives keys, builds the registration proof, and **registers** via a sponsored 4337 UserOp (`execute → dstack_register`), which atomically installs the binding key as the member's wallet owner.
5. *Node-runtime bring-up (the sidecar's autonomous job over the chain + mesh — not deploy steps):* determine CSK role, publish the wg key, subscribe to the Indexer, exchange `PeerEndpoint`s, **pull the CSK peer-to-peer** (the originator having published its `keccak256(CSK)` commitment — master §8), heartbeat, and converge. Demo acceptance: 3 nodes healthy, mesh converged, a sealed-box test message round-trips, and `GetClusterSharedKey` returns identical bytes on all three.

The orchestration's per-node responsibility ends at step 4 (produce the on-chain member + boot the CVM); step 5 is the sidecar's own job.

---

## 6. Task taxonomy: deterministic vs agent vs approval

- **`compute` (deterministic — the majority):** `forge build`, the three `Deploy*.s.sol` runs, `cast` verification reads, `wrangler deploy`, `docker build/push`. Each side-effecting task carries an **idempotency key** so a retry never re-broadcasts a landed transaction; Smithers' "completed task never re-runs" handles crash-resume of the rest.
- **`agent` (redpill-backed — narrow judgment, never holds keys or broadcasts):**
  - generate + validate the Phase-2 cluster JSON config from operator intent;
  - evaluate each step's on-chain "Verify" criteria (parse events/reads → pass/fail);
  - diagnose failures and propose fixes (e.g. a `dstack_register` revert → an allowlist / compose-hash mismatch);
  - produce the go/no-go summary that feeds each approval gate.
- **`<Approval>` (human-in-the-loop):** funding the Alchemy account, setting the IndexerRegistry record, `deployCluster` (hard on mainnet), and any mainnet state change. On v1/Sepolia the low-risk gates may auto-approve; milestone-B/mainnet gates require a human.

---

## 7. Model layer (redpill)

```ts
// deploy/smithers.config.ts
import { createOpenAICompatible } from "@ai-sdk/openai-compatible";

const redpill = createOpenAICompatible({
  name: "redpill",
  baseURL: "https://api.redpill.ai/v1",
  apiKey: process.env.REDPILL_API_KEY,
});
// agent task model:  redpill("phala/<model>")   // confidential — TEE-attested judgment
//               or:  redpill("openai/<model>")  // gateway-protected (no inference proof)
```

Choose a confidential `phala/…` model where the agent's judgment should itself be TEE-attested (so the deploy can record *which attested model produced which decision*); use gateway models otherwise. Verifying a confidential response is a deliberate multi-step bind (chat → `GET /v1/signature/{id}` → `GET /v1/attestation/report` → confirm the same `signing_address` appears in both and in the report data). The `intel_quote` / `tcb_info` it returns are the same TDX/dstack primitives the `DstackFacet` already reasons about — re-verify the exact endpoint shapes against current redpill docs before building a verifier (their deep docs moved during research).

---

## 8. v1 scope vs milestone B

**v1 (Base Sepolia, 3-node demo):** Smithers on an operator box; full-stack deploy (infra + Indexer CVM + sidecar image + gas-webhook + Alchemy wiring + one cluster + three nodes); approval gates present but the low-risk Sepolia ones may auto-approve; redpill as the model provider (model tier TBD — §9).

**Deferred to milestone B / later:**
- Smithers-in-CVM (attestation-native orchestrator).
- Signed / reproducible OCI images (master §11); v1 builds unsigned images.
- HA Indexer (multiple replicas behind a load balancer).
- Mainnet approval-gate hardening.
- A redpill confidential-response verifier in the verification tasks.
- Reorg / finality-depth handling.

---

## 9. Open questions

1. **Orchestrator trust domain** — operator box (v1) vs dstack CVM (attestation-native). When do we move?
2. **redpill model tier** — confidential `phala/…` (attested judgments) vs gateway models for the agent tasks (proof vs cost/latency)?
3. **Smithers version pin** — pre-1.0; pick a version and track churn.
4. **Sandbox backend** for any code-execution tasks (gVisor / Daytona / Cloudflare / K8s) — only needed if agent tasks must *run* code; the deploy mostly shells out locally.
5. **Generation order** — when is the orchestrator built relative to contracts/sidecar/indexer? (It can only be exercised once those exist.)

---

## 10. References

- [`attestmesh-coordination-layer.md`](./attestmesh-coordination-layer.md) §11 — v1 scope (what the deploy must stand up).
- [`contracts.md`](./contracts.md) §9.2 (factories), §10 (`ClusterDiamondFactory`), §11 (`IndexerRegistry`), §12 (Deploy scripts) — the deterministic deploy surface.
- [`indexer.md`](./indexer.md), [`sidecar.md`](./sidecar.md), [`gas-webhook.md`](./gas-webhook.md) — image builds, node bring-up, sponsorship.
- Smithers — https://smithers.sh — durable agent-workflow runtime (`smithers-orchestrator`).
- redpill.ai — https://api.redpill.ai/v1 — OpenAI-compatible TEE inference; `phala/…` confidential models.
