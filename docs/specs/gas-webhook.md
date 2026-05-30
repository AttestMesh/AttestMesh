# TeeMesh Gas Sponsorship Webhook — Component Spec

**Status**: Draft v0.1
**Parent spec**: [`teemesh-coordination-layer.md`](./teemesh-coordination-layer.md) (especially §13 item 18)
**Component**: `services/gas-sponsorship-webhook/`
**Last updated**: 2026-05-30

---

## 1. Purpose

The webhook is the policy gate between Alchemy's EIP-4337 paymaster and the rest of the world. It exists so that the TeeMesh org's Alchemy paymaster balance can only be drained by legitimate TeeMesh UserOperations — never by anyone who happens to know the paymaster's public surface.

Flow:

1. Sidecar signs an EIP-4337 UserOp with its dstack-derived binding key, submits to Alchemy's bundler RPC.
2. Bundler asks the Alchemy paymaster service whether to sponsor.
3. Alchemy paymaster POSTs the UserOp to *this webhook's URL with a shared-secret token*.
4. Webhook runs cheap static checks, then a single cached `eth_call` to `ClusterDiamondFactory.isDeployedCluster(target)`.
5. Webhook returns `{approved: true|false}`.
6. On `true`, Alchemy's paymaster signs the sponsorship fields, the bundler bundles, gas is paid by Alchemy and billed to the TeeMesh org account.

This pattern is ported directly from dstackgres's `services/gas-sponsorship-webhook/`. The differences in TeeMesh are narrower scope (one selector allowlist instead of dstackgres's broader set), different canonical factory address, and the worker lives in a new Cloudflare Worker deployment rather than dstackgres's existing one.

---

## 2. Toolchain

- **TypeScript 5.x** (strict mode).
- **Cloudflare Workers** runtime; deployed via `wrangler`.
- **viem** (lightweight EVM client) for the single `eth_call` and ABI encoding.
- **Workers KV** for the `isDeployedCluster` cache.
- **vitest** for unit tests; `wrangler dev` for local integration.

---

## 3. File layout

```
services/gas-sponsorship-webhook/
├── package.json
├── tsconfig.json
├── wrangler.toml                    # env vars + KV namespace binding
├── src/
│   ├── index.ts                     # router; entry export
│   ├── policy.ts                    # static checks (chain, selector, value)
│   ├── provenance.ts                # eth_call → isDeployedCluster + KV cache
│   ├── selectors.ts                 # the allowlist constants
│   ├── decode.ts                    # PackedUserOperation + execute calldata parsing
│   └── env.ts                       # env-var schema + typing
└── test/
    ├── policy.spec.ts
    ├── provenance.spec.ts
    └── e2e.spec.ts                  # wrangler-miniflare style integration
```

---

## 4. Configuration (Worker env)

| Key | Required | Source | Meaning |
|---|---|---|---|
| `EXPECTED_CHAIN_ID` | yes | env (plaintext) | `84532` for v1 Sepolia; `8453` for milestone B mainnet |
| `CANONICAL_CLUSTER_FACTORY` | yes | env (plaintext) | hex address of the `ClusterDiamondFactory` deployed by `DeployInfra.s.sol` |
| `CANONICAL_MEMBER_FACTORY` | yes | env (plaintext) | hex address of the `ClusterMemberFactory` deployed by `DeployInfra.s.sol` |
| `RPC_URL` | yes | env (secret) | Alchemy or other EVM RPC for the `eth_call` lookups |
| `ALCHEMY_WEBHOOK_TOKEN` | yes | env (secret) | shared-secret used in the webhook URL by Alchemy |
| `CACHE_TTL_SECONDS` | no | env (plaintext, default `86400`) | KV cache TTL for `isDeployedCluster` answers |
| `LOG_LEVEL` | no | env (plaintext, default `info`) | `info` \| `warn` \| `error` |

KV namespaces (declared in `wrangler.toml`):

- `FACTORY_PROVENANCE_CACHE` — cached answers from `ClusterDiamondFactory.isDeployedCluster`. Keyed `${chainId}:${target}`.
- `MEMBER_PROVENANCE_CACHE` — cached answers from `ClusterMemberFactory.isOurMember`. Keyed `${chainId}:${sender}`.

---

## 5. Endpoints

### 5.1 `POST /` — Alchemy custom-rules webhook

The primary endpoint. Authenticated by `?token=<ALCHEMY_WEBHOOK_TOKEN>` in the URL (constant-time string compare). TLS is required (Cloudflare gives this).

**Request body** (Alchemy's shape):

```json
{
  "userOperation": {
    "sender": "0x...",
    "nonce": "0x...",
    "factory": null,
    "factoryData": null,
    "callData": "0x...",
    "callGasLimit": "0x...",
    "verificationGasLimit": "0x...",
    "preVerificationGas": "0x...",
    "maxFeePerGas": "0x...",
    "maxPriorityFeePerGas": "0x...",
    "paymaster": null,
    "paymasterVerificationGasLimit": null,
    "paymasterPostOpGasLimit": null,
    "paymasterData": null,
    "signature": "0x..."
  },
  "entryPoint": "0x0000000071727De22E5E9d8BAf0edAc6f37da032",
  "chainId": "0x14a34"
}
```

**Response**:

```json
{ "approved": true }
```

or

```json
{ "approved": false, "reason": "selector-not-allowed" }
```

`reason` is one of: `bad-token`, `chain-mismatch`, `not-cluster-member`, `outer-selector-not-execute`, `value-nonzero`, `inner-selector-not-allowed`, `target-not-our-cluster`, `rpc-failure`. Used for ops dashboards; never blocks-or-allows differently based on reason.

### 5.2 `GET /check?cluster=<addr>` — Sidecar startup probe

Helper for the sidecar to verify, before booting in earnest, that its target cluster is one the webhook will sponsor. Public read (no token); returns:

```json
{ "deployed": true, "sponsored": true }
```

`deployed` is the cached `isDeployedCluster` answer. `sponsored` is the same boolean today; the field exists for forward-compat with v1.5+ where a cluster could be deployed-but-quota-exhausted.

### 5.3 `GET /healthz` — Cloudflare uptime probe

Returns `200 OK` body `{ ok: true }` if the worker can reach `RPC_URL` (lightweight `eth_chainId` probe; cached 60s).

---

## 6. Policy

Run in this order; first failure short-circuits. Costs in order are: zero, zero, zero, zero, zero, zero, one cached eth_call (rare miss → one RPC roundtrip).

1. **Token**: `URL.searchParams.get("token") === ALCHEMY_WEBHOOK_TOKEN` (constant-time). Else `bad-token`.
2. **Chain id**: `chainId === EXPECTED_CHAIN_ID`. Else `chain-mismatch`.
3. **Sender provenance**: `userOperation.sender` is a known ClusterMember. Implementation: `eth_call ClusterMemberFactory.isOurMember(sender)`. Cached separately under `MEMBER_PROVENANCE_CACHE`. Else `not-cluster-member`.
4. **Outer selector**: First 4 bytes of `userOperation.callData` match `ClusterMember.execute(address,uint256,bytes)` selector (`0xb61d27f6`). Else `outer-selector-not-execute`.
5. **Value zero**: The second arg of the decoded `execute(...)` call is `0`. Else `value-nonzero`.
6. **Inner selector**: The first 4 bytes of the third arg (`data`) match one of the entries in `selectors.ts` (see §7). Else `inner-selector-not-allowed`.
7. **Target provenance**: The first arg of `execute(...)` (the target address) returns `true` from `ClusterDiamondFactory.isDeployedCluster`. Cached. Else `target-not-our-cluster`.

If all pass: `{ approved: true }`.

---

## 7. Allowlisted inner selectors

The inner-selector allowlist is the *exact set* of cluster operations the operator is willing to sponsor. Anything outside is rejected. v1:

```typescript
export const ALLOWED_SELECTORS = [
  // Member-driven operations:
  selectorOf("dstack_register(DstackProof,address,bytes32,bytes32)"),
  selectorOf("publishWgKey(bytes32)"),
  selectorOf("send(bytes32,bytes32,bytes)"),

  // Cluster ownership transitions (used during deploy + Safe rotation):
  selectorOf("transferClusterOwnership(address)"),
  selectorOf("acceptClusterOwnership()"),
  selectorOf("transferBothOwners(address)"),
  selectorOf("acceptBothOwners()"),
  selectorOf("acceptOwnership()"),                  // solidstate SafeOwnable accept side; cluster Safe calls this after deploy

  // dstack allowlist mutations (cluster owner does these from the Safe, not from a CVM,
  // but include them so an ops Safe operated via 4337 can manage allowlists too):
  selectorOf("addComposeHash(bytes32)"),
  selectorOf("removeComposeHash(bytes32)"),
  selectorOf("addDevice(bytes32)"),
  selectorOf("removeDevice(bytes32)"),
  selectorOf("setAllowAnyDevice(bool)"),
  selectorOf("setRequireTcbUpToDate(bool)"),
  selectorOf("addAllowedKmsRoot(address)"),
  selectorOf("removeAllowedKmsRoot(address)"),
] as const;
```

Adding a new selector is a single-line PR; rolls out with the next worker deploy.

---

## 8. Caching

Two KV caches, both keyed by `${chainId}:${address}`:

- `FACTORY_PROVENANCE_CACHE` — answers from `isDeployedCluster(target)`. TTL `CACHE_TTL_SECONDS` (default 24h).
- `MEMBER_PROVENANCE_CACHE` — answers from `isOurMember(sender)`. Same TTL.

Negative answers (`false`) are cached with a shorter TTL (10 min) so a new cluster/member shows up reasonably quickly. Positive answers (`true`) get the full TTL because cluster and member contracts cannot be un-deployed.

---

## 9. Failure modes

- **RPC unreachable** → return `{ approved: false, reason: "rpc-failure" }`. The UserOp does not get sponsored; the sidecar retries the bundler submission later. Logged at error level.
- **KV unavailable** → fall back to direct RPC, log at warn.
- **Malformed UserOp body** → return 400; never crash the worker. Logged at warn.
- **Webhook unreachable from Alchemy** → Alchemy's paymaster's own behavior: it falls back to "do not sponsor" for that UserOp. Bundler returns "paymaster declined" to the sidecar; sidecar retries.

---

## 10. Tests (v1 scope)

- Unit: every policy rule with fixture UserOps that trigger each failure path.
- Unit: `decode.ts` round-trip against ABI fixtures.
- Integration: a local `wrangler dev` server fed real UserOps captured from a Sepolia run; assert approve/deny.
- No on-chain tests in this component; the on-chain side is exercised by `contracts/test/integration`.

---

## 11. Open questions

1. **Rate limiting.** The Alchemy dashboard's per-sender cap is the first defense. Should the webhook add its own (e.g. "no more than N approvals per sender per minute")? v1: no; revisit if we see abuse.
2. **Multi-cluster sponsorship policy.** v1 sponsors every UserOp targeting any TeeMesh cluster on this chain. Milestone B might want per-cluster opt-in (some clusters self-fund; some are sponsored). Not in v1.
3. **Token rotation.** `ALCHEMY_WEBHOOK_TOKEN` is the only secret. Rotating means coordinated update of Alchemy's dashboard + the worker env. v1 documents the runbook; milestone B may add a HMAC-style signed request to Alchemy in place of the shared secret if/when Alchemy ships it.
