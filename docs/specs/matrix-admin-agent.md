# AttestMesh Matrix-Admin-Agent — Component Spec

**Status**: Specified v1 — **not yet implemented**. Depends on the sidecar inbound-delivery change (§6.1).
**Parent spec**: [`attestmesh-coordination-layer.md`](./attestmesh-coordination-layer.md) (especially §3 encryption, §5 messaging, §7 sidecar)
**Related**: [`sidecar.md`](./sidecar.md) (§12 app-facing gRPC), [`contracts.md`](./contracts.md) (§5 MessageFacet, §5.2 reserved envelopes)
**Component**: own repo `AttestMesh/matrix-admin-agent` (image `ghcr.io/attestmesh/matrix-admin-agent`); deployed via `deploy/compose/matrix-node.yaml`
**Last updated**: 2026-06-19

---

## 1. Purpose

The matrix-admin-agent is a per-node process that runs **inside a Matrix-node CVM**, alongside the Synapse homeserver and the AttestMesh sidecar, and turns "a Synapse server in an attested mesh node" into "a Synapse server that authorized mesh members and operators can administer **without ever exec-ing into the sealed TEE**."

It provides **two command channels** that converge on one executor:

1. **On-chain control plane (trustless, cross-node, deterministic — no LLM).** Authorized cluster members send encrypted admin commands over the existing `MessageFacet` messaging layer. The node's sidecar decrypts them and hands them to the agent over the app-facing gRPC (`SubscribeMessages`); the agent validates and executes them against Synapse, and may send a sealed reply/ack back over the same channel.
2. **In-Matrix operator bot (natural language, LLM).** The agent joins the homeserver as a bot user and answers admin requests from **allowlisted human operators** in Matrix rooms, using an LLM (redpill.ai) to translate natural language into the same admin actions.

**Capability is the full Synapse administration surface** — the core primitive is a generic authenticated Synapse request (admin API + the bot's client API), with named convenience verbs layered on top for ergonomics and replay-safety. "As basic as possible" governs the agent's *code* (one generic tool + a thin executor), **not** its capability ceiling; the LLM's system prompt carries the knowledge of how to administer Matrix.

This subsumes the original concrete task — "create the Matrix admin user" — which becomes either a declarative sealed-env entry applied at boot (§13), a single on-chain `ensure_user` command, or a natural-language request to the bot.

### 1.1 Implementation status

Specified, not built. v1 build order: (1) the sidecar change §6.1; (2) this agent in its own repo; (3) the compose/deploy wiring §14. No contract changes are required for v1 (§7 rides `MessageFacet` as-is).

---

## 2. Toolchain

- **Python 3.12** (the agent is an LLM tool-loop + HTTP admin client + a thin gRPC-over-UDS consumer; a small, auditable interpreted codebase fits "as basic as possible" and LLM ergonomics).
- **Build**: multi-stage `Dockerfile` (`python:3.12-slim` builder → slim non-root runtime); `uv` for locked, reproducible deps. CI runs `ruff`, `mypy`, `pytest`, then publishes the image.

### 2.1 Key dependencies

| Package | Purpose |
|---|---|
| `matrix-nio` | Matrix client (bot login + sync + room reply) for the human channel |
| `httpx` | Synapse admin/client HTTP calls |
| `grpcio` + `grpcio-tools` | app-facing gRPC client over the sidecar UDS (`SubscribeMessages`, `SendMessage`, `GetSelf`) |
| `openai` | OpenAI-compatible client pointed at redpill.ai (`base_url` + `api_key` + `model`) |
| `pydantic` v2 | config schema + the typed command/verb models (also the source of the LLM tool JSON-schemas) |
| `psycopg` (3) | Postgres dedup ledger + bootstrap-credential durability |
| `pyyaml` | read `registration_shared_secret` from the homeserver config at bootstrap |
| `pytest` + `respx` + `pytest-asyncio` | unit + mocked-HTTP + mocked-LLM tests |

### 2.2 Build-time codegen

- `proto/agent.proto` is a **vendored copy** of `sidecar/proto/agent.proto` (package `attestmesh.agent.v1`), with a header noting the canonical source and "keep in sync". Stubs are generated at build (`grpc_tools.protoc`); CI fails if regeneration is dirty. The agent uses only `SubscribeMessages`, `SendMessage`, and `GetSelf`.

---

## 3. Process model

One process per Matrix node, a docker-compose service named `matrix-admin-agent`. Single `asyncio` event loop with three layers:

```
 SidecarAdapter (on-chain, deterministic) ─┐
                                           ├─▶ CommandExecutor ─▶ SynapseAdmin ─▶ http://synapse:8008
 MatrixAdapter (human, LLM) ───────────────┘   (validate, scope,    (admin token / bot client token)
        │                                        audit, dedup)
        └─▶ LlmAgent ─▶ redpill.ai (the ONE permitted egress)
```

- `depends_on: { synapse: started, sidecar: started }` (**not** `service_healthy` — a single-node mesh never reaches `healthy`; Matrix administration must work regardless of mesh state).
- Restart policy `unless-stopped`. The bootstrap (§13) is idempotent, so a restart (or a fresh-disk CVM roll) re-converges cleanly.
- **Egress-firewalled** (§12): a companion `agent-egress-fw` service locks the agent's network namespace to deny-all-outbound except Synapse, Postgres, DNS, and the single pinned LLM endpoint.
- **Least-privilege mounts**: the agent mounts only the sidecar UDS dir, a read-only scoped secret file (§13), and reaches Synapse/Postgres over the compose bridge. It does **not** mount `/var/run/dstack.sock` (only the sidecar/synapse-init need the guest agent).

---

## 4. Repository layout

```
matrix-admin-agent/                       # AttestMesh/matrix-admin-agent
├── README.md  spec.md  SYSTEM_PROMPT.md   # spec.md mirrors this doc; SYSTEM_PROMPT teaches the full Synapse admin API
├── pyproject.toml  uv.lock  Dockerfile  Makefile
├── proto/agent.proto                      # vendored from sidecar (canonical-source header)
├── matrix_admin_agent/
│   ├── __main__.py                        # entry: load config → bootstrap → start adapters → run forever
│   ├── config.py  log.py  health.py       # pydantic Settings (fail-fast); JSON logs; /healthz (admin-readiness)
│   ├── bootstrap.py                       # §13 boot sequence
│   ├── synapse_admin.py                   # §10 generic request + nonce+HMAC register + convenience wrappers
│   ├── commands.py                        # §7.4 typed verb models (generic synapse_request + named verbs)
│   ├── executor.py                        # the ONLY effecting path: validate → scope-check → audit → run → dedup
│   ├── adapters/sidecar.py                # §6 gRPC-UDS consumer (SubscribeMessages → Command → executor; SendMessage acks)
│   ├── adapters/matrix.py                 # §11 nio bot listener (authz → LlmAgent → reply)
│   ├── llm/agent.py  llm/tools.py  llm/prompt.py   # §11 bounded tool loop; tool schemas from commands.py; renders SYSTEM_PROMPT
│   ├── ledger.py                          # §9 Postgres dedup ledger + bootstrap-credential store
│   └── sidecar_grpc/                      # generated from proto
└── tests/  docker-compose.test.yaml  test-harness/   # §16: respx + mock-LLM + stub-sidecar (UDS) + local synapse/postgres
```

---

## 5. Configuration

All via environment variables; fail-fast on missing required. **S = sealed secret** (dstack `encrypted_env`, never in repo/compose), **P = plain** (measured into `compose_hash`, fine to be visible).

| Env var | Req | S/P | Default | Meaning |
|---|---|---|---|---|
| `SYNAPSE_URL` | no | P | `http://synapse:8008` | Synapse admin + client API base (internal bridge). |
| `MATRIX_HOMESERVER_URL` | no | P | `http://nginx:80` | Where the nio bot logs in / syncs. |
| `SERVER_NAME` | no | P | read from homeserver.yaml | Matrix `server_name` (for MXIDs + the system prompt). |
| `AGENT_GRPC_SOCKET` | no | P | `/var/run/attestmesh/agent.sock` | Sidecar app-facing UDS (client connects here). |
| `SIDECAR_ENABLED` | no | P | `auto` | `auto` connect-if-present / `on` require / `off`. |
| `REG_SECRET_PATH` | no | P | `/agent-secrets/registration_shared_secret` | Scoped file holding the registration shared secret (§13). |
| `BOT_USERNAME` | no | P | `matrix-admin-agent` | Bot localpart → `@matrix-admin-agent:<server_name>`. |
| `BOT_PASSWORD` | yes | **S** | — | Bot account password (register/login). |
| `MATRIX_REQUIRE_MENTION` | no | P | `true` | Matrix room messages from allowlisted operators are ignored unless they tag the bot. |
| `BOOTSTRAP_DEACTIVATE_USERS` | no | P | — | Comma-list of stale local user localparts/MXIDs to deactivate during bootstrap; intended for one-way cleanup after bot renames. The current bot is protected. |
| `MATRIX_ADMIN_MXIDS` | yes | P | — | Comma-list of human MXIDs allowed to command the bot in Matrix. |
| `MATRIX_ADMIN_SENDERS` | no | P | — | Comma-list of 32-byte hex memberIds allowed on the on-chain channel. Empty → on-chain channel disabled. |
| `INITIAL_ADMIN` | no | P/S | — | Declarative human admin(s) to ensure at boot (`@alice:server`; password part, if any, is S). |
| `LLM_BASE_URL` | yes | P | — | redpill.ai OpenAI-compatible base URL (also the egress-allowed host). |
| `LLM_API_KEY` | yes | **S** | — | redpill.ai API key. |
| `LLM_MODEL` | yes | P | — | Model name (must support tool/function calling). |
| `LLM_TIMEOUT_S` | no | P | `60` | Per-call timeout. |
| `MAX_TOOL_STEPS` | no | P | `5` | Tool-loop hard cap. |
| `CONFIRM_DESTRUCTIVE` | no | P | `1` | Require a confirm turn for destructive ops in the Matrix channel. |
| `PG_DSN` | yes | **S** | — | Postgres DSN for the dedup ledger (reuses the node's Postgres; password sealed). |
| `COMMAND_MAX_AGE_S` | no | P | `86400` | Reject on-chain commands older than this (replay-staleness bound). |
| `HEALTH_HTTP_ADDR` | no | P | `0.0.0.0:9100` | Agent's own `/healthz`. |
| `LOG_LEVEL` | no | P | `info` | Standard level. |

---

## 6. Sidecar interface

The agent is a gRPC **client** of the sidecar's app-facing service over the UDS at `AGENT_GRPC_SOCKET` (no transport auth — the trust domain is the node, per [`sidecar.md`](./sidecar.md) §12.2). It uses:

- **`SubscribeMessages(Empty) → stream IncomingMessage{sender_member_id, payload, block_number}`** — the inbound command channel. The sidecar has already decrypted the sealed-box; `payload` is plaintext bytes. `sender_member_id` is chain-authenticated (the facet emits the on-chain sender, not a caller-supplied value).
- **`SendMessage(SendRequest{recipient_member_id, payload, envelope_id}) → SendResponse{envelope_id, tx_hash}`** — used to send sealed replies/acks back to a command's sender. The sidecar seals to the recipient's x25519 key and submits the sponsored UserOp.
- **`GetSelf() → SelfInfo{member_id, …}`** — the node's own memberId (for logging + reply correlation).

### 6.1 Sidecar prerequisite (implemented)

The signed Indexer event dispatcher decrypts addressed `MessageSent` events and demuxes on the inner kind. A `PeerEndpoint` with the reserved kind is consumed internally; every other plaintext is forwarded verbatim to `incoming_tx` as `AppIncoming{sender_member_id, payload, block_number}`. The sidecar remains app-protocol-agnostic. Delivery is at-least-once and the in-memory broadcast channel is bounded, so the agent **must** dedup by request id (see §9).

---

## 7. On-chain command protocol

### 7.1 Transport

A command is one `MessageFacet.send(recipientMemberId = the Matrix node, envelopeId, ciphertext)`, where `ciphertext` is the sender's sidecar sealing (NaCl sealed-box to the node's x25519 key) of the JSON envelope below. The chain sees only opaque ciphertext; the sidecar forwards opaque plaintext; only the agent parses it. No on-chain change.

### 7.2 Command envelope (JSON)

```json
{ "v": 1,
  "kind": "attestmesh.matrix-admin.command.v1",
  "request_id": "<128-bit hex, sender-chosen, stable across retries>",
  "issued_at_ms": 1718800000000,
  "verb": "ensure_user",
  "args": { "username": "alice", "admin": true } }
```

### 7.3 Reply envelope (JSON, sealed back to the sender)

```json
{ "v": 1,
  "kind": "attestmesh.matrix-admin.reply.v1",
  "request_id": "<echoes the command>",
  "status": "ok | error | unauthorized | malformed | duplicate",
  "code": "ok | user_exists | synapse_error | …",
  "detail": "<human-readable; secret-scrubbed>",
  "output": { "user_id": "@alice:server" } }
```

`detail`/`output` are scrubbed of secrets (never tokens, passwords, or the registration secret; sole exception: `create_login_token` returns the token it just minted — that is its purpose). Replies are best-effort and single-shot (they cost sponsored gas); the sender's source of truth is re-issuing the idempotent command or reading Synapse directly.

### 7.4 Command vocabulary

The core primitive gives **full coverage**; named verbs are convenience/idempotency wrappers over it.

- **`synapse_request`** — the generic primitive. Args: `target` (`admin` | `client`), `method` (GET/POST/PUT/DELETE), `path` (must begin `/_synapse/admin/` for `admin`, `/_matrix/client/` for `client`), `body` (optional JSON). Executed verbatim with the appropriate token. This reaches **all** admin actions (users, rooms, media, devices, federation, registration tokens, server notices, event reports, …) and auto-covers future Synapse endpoints. **Carve-out:** `POST` to any `/login` (token-minting) endpoint is rejected here — minting must go through `create_login_token`, whose token-bearing result is delivered runtime-direct and kept out of model context (a `synapse_request` result is fed back to the LLM).
- **Named convenience verbs** (absolute-state / idempotent; nicer LLM calls + replay-safety): `ensure_user{username,password?,admin?,displayname?}`, `set_password{username,password?,logout_devices?}`, `create_login_token{username,valid_hours?}` (mints a **new** access token via admin login-as-user; destructive/confirm-first; the fresh token is delivered to the operator runtime-direct and **never enters model context** — the sole exception to the secret-free-output rule), `deactivate_user{username,erase?}`, `set_admin{username,admin}`, `list_users{from?,limit?,name?}`, `get_server_info`, `create_room{name?,alias?,topic?,invite?,preset?}`, `send_notice{username|room_id, body}`. Each maps to a specific admin/client endpoint (§10).

Every verb — generic or named — passes through the executor's validation, scope-check (path prefix), and audit log before any HTTP call (§10, §15).

### 7.5 envelopeId derivation (replay-safe; set by the sender)

```
envelopeId = keccak256( utf8(kind) ‖ senderMemberId ‖ request_id ‖ attempt_le_u32 )
```

- Distinct `request_id` → distinct id (no cross-command collision).
- A byte-identical re-send (same attempt) → same id → the contract's `(recipient, envelopeId)` dedup reverts `DuplicateEnvelope` (cheap idempotency; the sidecar surfaces `FailedPrecondition`).
- A forced retry bumps `attempt`, lands a fresh `MessageSent`; the agent then dedups on `request_id` (§9). Senders MUST salt this (an unsalted/`keccak256(payload)` default risks silent `DuplicateEnvelope` on a re-send — the same lesson as `PeerEndpoint` resends in `contracts.md` §5.2). The reply uses `keccak256(reply_kind ‖ agentMemberId ‖ request_id ‖ status)`.

### 7.6 Reserved kinds

Register in [`contracts.md`](./contracts.md) §5.2 (bookkeeping only — these are **app-layer**; neither the contract nor the sidecar special-cases them): `attestmesh.matrix-admin.command.v1`, `attestmesh.matrix-admin.reply.v1`.

---

## 8. Authorization

There are **no per-member roles on chain** (only a `clusterOwner` Safe, which cannot call `MessageFacet.send` — it is `onlyClusterMember`). So authorization is an explicit allowlist, layered on the chain's own guarantees.

### 8.1 On-chain channel

An inbound command is acted on **iff all** hold:

1. **Delivered by the sidecar** ⇒ the sender is a registered member (`onlyClusterMember`), the `senderMemberId` is chain-authenticated, and the message decrypted to *this node's* x25519 key.
2. **`senderMemberId ∈ MATRIX_ADMIN_SENDERS`** (sealed-env allowlist) — the real authorization gate. "Any registered member" is explicitly **not** sufficient (membership is a code-integrity gate, not an admin-authority grant).
3. **Not stale** — `now - issued_at_ms < COMMAND_MAX_AGE_S` (bounds replay of a captured `MessageSent`).
4. **`request_id` unseen** (§9).
5. **Verb + args validate** and the path is in scope (§10).

An authorized sender gets the **full** admin surface — that is the design; the allowlist is the trust boundary. **memberId footgun** for populating the allowlist: `memberId = keccak256(abi.encode(cluster, memberContract, keccak256("attestmesh.attestor.dstack")))` (the attestorId is *already hashed* in the encoding — `sidecar/src/state/mod.rs` `compute_member_id`). Easiest source: read the authorized sender node's sidecar `GetSelf.member_id`.

### 8.2 In-Matrix human channel

A separate allowlist of **Matrix MXIDs** (`MATRIX_ADMIN_MXIDS`). The bot acts on a room message **only if** the Synapse-attested `event.sender ∈ MATRIX_ADMIN_MXIDS`. MXID authenticity is provided by Synapse/federation, never by message text. Authorization is on `event.sender`, evaluated before the LLM is even consulted for command purposes.

### 8.3 Rotation

Both allowlists are sealed env, so their **keys** are measured into the compose hash (and thus the on-chain allowlisted `composeHash`) — the authorization set is itself attested. Changing it is a compose-hash change + in-place roll (§14). Live, Safe-controlled rotation without redeploy is the future on-chain `MatrixAdminFacet` option (§17) — deferred.

---

## 9. Replay / idempotency / ordering

Delivery is **at-least-once and may replay after an unflushed Ack or reconnect**; the sidecar's application queue itself is not durable. The generic `synapse_request` is **not** inherently idempotent, so a dedup ledger is a **v1 requirement**, not just defense-in-depth.

- **Ledger** in the node's Postgres (the only store that survives in-place rolls — pgdata is a named volume):
  ```sql
  CREATE TABLE processed_commands (
    request_id   bytea PRIMARY KEY,
    sender       bytea NOT NULL,
    verb         text  NOT NULL,
    status       text  NOT NULL,
    reply_digest bytea,
    first_seen_ms bigint NOT NULL,
    applied_ms    bigint
  );
  CREATE TABLE agent_secrets ( k text PRIMARY KEY, v bytea NOT NULL );  -- bootstrap-credential durability (§13)
  ```
- **Protocol:** `INSERT … ON CONFLICT (request_id) DO NOTHING`. On conflict → load the prior terminal status, resend the same reply (idempotent by `request_id`), **never re-execute**. On fresh insert → execute, then set `status`/`applied_ms`, then reply. A row with `applied_ms IS NULL` re-seen → retry the (convergent) call. The PK row lock serializes racing deliveries.
- **Ordering:** never assumed. v1 verbs are absolute-state, so the last-processed command defines the converged state; the operator avoids issuing conflicting commands concurrently. GC rows older than `COMMAND_MAX_AGE_S` + margin.

---

## 10. Synapse administration

`SynapseAdmin` is an `httpx` client over `SYNAPSE_URL`. The Synapse admin API is reachable **only** at `http://synapse:8008` on the internal bridge (nginx does not proxy `/_synapse/admin`).

- **Generic** `request(target, method, path, body)`:
  - `target=admin` → `Authorization: Bearer <admin_token>`, path under `/_synapse/admin/`.
  - `target=client` → the bot's client token, path under `/_matrix/client/` (room creation, messaging, invites as the bot).
  - Scope-checked (path prefix), audited (actor, method, path — never body secrets), and the response is returned structurally (secret-scrubbed) to the caller/ledger.
- **Bootstrap registration** (used exactly once, §13): `register_via_shared_secret(username, password, admin)` = `GET /_synapse/admin/v1/register` for a nonce, then `mac = HMAC_SHA1(shared_secret, nonce ‖ 0x00 ‖ user ‖ 0x00 ‖ password ‖ 0x00 ‖ ("admin"|"notadmin"))`, then `POST /_synapse/admin/v1/register`. (This is exactly what `register_new_matrix_user` does under the hood.)
- **Convenience wrappers** map named verbs to endpoints (`PUT /_synapse/admin/v2/users/{id}` for `ensure_user`; `POST …/reset_password/{id}`; `POST …/deactivate/{id}`; `PUT …/users/{id}/admin`; `GET /_synapse/admin/v2/users`; `POST …/send_server_notice`; `POST /_matrix/client/v3/createRoom`).
- **Token handling:** the shared secret is used only to mint the bot's own admin account, then dropped from memory; steady-state uses the in-memory admin bearer token (one transparent re-login on 401). The bot password lives in sealed `BOT_PASSWORD` (re-login is deterministic across restarts).

---

## 11. LLM agent (Matrix channel only)

A hand-rolled, bounded tool-calling loop (~150–200 lines; **no agent framework**). One Matrix message → at most `MAX_TOOL_STEPS` model turns → a final reply.

- **Model call:** OpenAI-compatible (`openai` client) at `LLM_BASE_URL`/`LLM_MODEL`, `temperature=0`, tool schemas generated from the pydantic verb models (so the prompt and the executor's validation cannot drift). `tool_choice=auto`.
- **Tools = the named convenience verbs + the generic `synapse_request`** — so the bot can take **any** admin action. There is **no** shell/file/host/network/browse/DNS tool. The model's only effect on the world is to emit a typed verb call, which the executor then validates and runs.
- **Untrusted model output:** every `arguments` payload is re-validated in code (pydantic) and scope-checked (path prefix) before execution, identical to the on-chain path; the model never constructs a URL or HTTP body that bypasses validation.
- **Destructive-op confirmation** (`CONFIRM_DESTRUCTIVE=1`, Matrix channel only): `deactivate_user`, `set_password`, `set_admin(true)`, and any `synapse_request` with `DELETE` / `deactivate` / `purge` / room-delete require an explicit human confirm turn. This prevents accidents; it does **not** cap capability.
- **Audit:** every executed call logs actor MXID + verb + (method, path) at info, before execution.

### 11.1 System prompt (`SYSTEM_PROMPT.md`)

Shipped as a file (auditable, editable without code change), rendered with runtime facts (`server_name`, bot MXID, the verb list generated from `commands.py`). It teaches: the agent's role (administer this one Synapse homeserver for an AttestMesh node); the **full Synapse admin API** and how to express actions as `synapse_request` or a convenience verb; hard safety rules (act only for the authorized operator; never reveal secrets — the registration secret, tokens, passwords, DB creds, config; refuse out-of-scope requests — host/shell/federation-config/other-servers; ask before destructive ops); terse operator-facing replies.

### 11.2 Prompt-injection defense (structural, not prompt-only)

- **Authority is on `event.sender`,** evaluated against the sealed allowlist before the model runs — instructions embedded in room content from non-allowlisted users can never trigger a tool call.
- **The model cannot exceed its caller's authority:** the worst case from a jailbreak by an allowlisted admin is the admin doing something they were already authorized to do; a smuggled forbidden capability simply does not exist as a tool.
- **Secrets are never in the model's context** (not in the system prompt, not in tool outputs) and never echoed to a room.
- **Egress isolation (§12)** means even a fully jailbroken loop cannot open a socket to anywhere but Synapse/Postgres/the pinned LLM host.

---

## 12. Network egress isolation (NON-NEGOTIABLE)

The agent is high-privilege (full admin API; reads the registration secret; can read all user data) and it talks to an external LLM, so **exfiltration is the headline risk**. Defense is **network-enforced, not code-trusted**.

- **`agent-egress-fw`** (new compose service, mirrors the existing `ts-firewall`): an alpine container with `cap_add: NET_ADMIN` and `network_mode: service:matrix-admin-agent` (shares the agent's netns) sets the agent's `OUTPUT` policy to **default-DROP** and ACCEPTs only: `lo`, `ESTABLISHED,RELATED`, the **resolved IPs of the internal services it needs** (Synapse, Postgres, nginx — by resolved IP, *not* the whole bridge subnet, so the host gateway and other peers stay unreachable), **outbound `:53` to any resolver** (DNS — required; see the caveat below), and the **pinned redpill host on `:443`** (re-resolved IP(s) plus a static CIDR pin, so it stays reachable regardless of resolution timing). Every other outbound packet is dropped.
- **The agent has no self-egress capability:** no shell, no arbitrary-HTTP/browse/DNS tool is exposed to the LLM; the process's only external socket is the redpill client to the pinned host, and **no secrets are ever placed in LLM prompts/outputs**, so the one sanctioned egress leaks nothing.
- **DNS caveat (load-bearing — this broke the live LLM bot):** `:53` must be allowed to **any** resolver, not just Docker's embedded DNS at `127.0.0.11`. Docker's embedded resolver answers *internal* service names (Synapse/Postgres) locally, but forwards *external* queries (the LLM host) out of the agent's **own locked netns** to the upstream — so pinning only `127.0.0.11:53` lets internal names resolve while `api.redpill.ai` fails with `EAI_AGAIN`, surfacing as `APIConnectionError` on every LLM turn. The firewall additionally resolves `LLM_BASE_URL`'s host at config time and re-resolves periodically to keep the `:443` allow current. **Residual:** `:53`-to-any is a low-bandwidth DNS-tunnel surface (all *data* ports stay deny-all; the canary `:443` is still blocked, so `egress_locked` holds); the tighter follow-up is DNS-locked resolution (e.g. dnsmasq restricting *which* domains resolve).
- **In-TEE verifiability — the agent self-checks (fail closed, never crash-loops):** because the CVM is a sealed TEE (no exec; the box cannot fetch per-container logs), the agent itself TCP-probes a non-allowlisted canary (`EGRESS_CANARY`, default `1.1.1.1:443`) at startup and **will not serve the admin channels until it is unreachable**; while egress is unlocked it logs `CRITICAL` and **holds** (loops, reporting `phase`/`last_error` on `/healthz`) rather than exiting — an invisible crash-loop in a TEE is undebuggable. It also probes the LLM (resolve `LLM_BASE_URL`'s host + `GET /models`). Both results fold into `/healthz` — `egress_locked` **and** `llm_ok` (with the resolved IP) — exposed on the box loopback (like the sidecar), so `deploy/matrix-node.sh <node> verify-agent` confirms bootstrap, egress lock, **and** LLM reachability in one curl. `EGRESS_SELFCHECK=off` skips the canary (local harness only — no firewall there).
- **Verification is mandatory (§16.4):** a negative test proves the agent **cannot** reach an arbitrary host while Synapse/Postgres/the pinned LLM succeed.
- Literal zero-internet would require an in-CVM local model instead of redpill — out of scope for v1 (operator chose the single-pinned-egress model).

---

## 13. Bootstrap sequence (idempotent; re-runs on every fresh-disk roll)

1. Load + validate config; fail-fast on missing required.
2. Wait for Synapse `GET /health` `200` (bounded backoff; keep retrying — Synapse is a hard dependency that will arrive).
3. Read `registration_shared_secret` from `REG_SECRET_PATH` (a scoped file, §14 — not the whole `synapse-data` volume).
4. **Ensure the bot's own admin account:** try login as `BOT_USERNAME`/`BOT_PASSWORD`; on no-such-user, `register_via_shared_secret(admin=true)`; obtain an admin token; **drop the secret from memory**. (Bot credential/token cached in the `agent_secrets` table for restart without re-register.)
5. Start the Matrix bot (`nio` login + initial sync with `since="now"` so it ignores backlog and never replays old room messages as commands).
6. **Declaratively ensure** `INITIAL_ADMIN` and `MATRIX_ADMIN_MXIDS` as admin users (idempotent `ensure_user`/`set_admin`). *This is the original "create the admin user" task, now declarative.* Initial human passwords are not invented/logged — create declaratively, then the operator issues `set_password` over a channel (recommended), or supply a sealed one-time password in `INITIAL_ADMIN`.
7. Start both adapters concurrently under a supervisor (restart a crashed adapter with backoff). The sidecar adapter is lazy/best-effort — it never blocks the Matrix admin path.
8. `/healthz` goes `200` once `admin_token_acquired && matrix_synced` (deliberately decoupled from mesh health).

---

## 14. Deployment & compose integration

Edits to `deploy/compose/matrix-node.yaml`:

- **Shared UDS volume `agent-sock`** mounted at `/var/run/attestmesh` in **both** `sidecar` and `matrix-admin-agent`; set `AGENT_GRPC_SOCKET=/var/run/attestmesh/agent.sock` explicitly on the sidecar (today the socket is not shared into any container).
- **`matrix-admin-agent` service** (`image: ghcr.io/attestmesh/matrix-admin-agent:latest`, `depends_on` synapse+sidecar *started*, sealed env, no host port — outbound only, sidesteps the VMM <20000 host-port limit).
- **`agent-egress-fw` service** (§12).
- **Scoped secret:** `synapse-init` additionally writes just `registration_shared_secret` to a small `agent-secrets` volume, mounted `:ro` into the agent (avoids exposing the Synapse signing key that a full `synapse-data:ro` would). Keep firewall/secret logic in images, not inline compose shell (`$$`-escaping pitfalls).
- **Sealed env keys** → add to `ENV_KEYS` in `deploy/matrix-node-box.py` and pass as in-memory `E_*` in `deploy/matrix-node.sh` `_box_run`: `BOT_USERNAME, BOT_PASSWORD, MATRIX_ADMIN_MXIDS, MATRIX_ADMIN_SENDERS, INITIAL_ADMIN, LLM_BASE_URL, LLM_MODEL, LLM_API_KEY, PG_DSN`. Key **names** are measured into `compose_hash`; values are sealed. Existing `DSTACK_DOCKER_*` already cover the private GHCR pull (the pre-launch `docker login` is registry-scoped to `ghcr.io`, covering `attestmesh/*`).
- **Roll onto live C3/W3 via app_id reuse** (preserves membership + CSK-originator — no new cluster): `deploy/matrix-node.sh <node> update` → compute `H'` → `addComposeHash(C3, H')` (**must** precede `CreateVm` or the KMS boot-gate rejects) → `StopVm` old (keep for rollback) → `CreateVm(app_id=X)` → wait for Synapse.
- **Data-wipe caveat:** `update` boots a fresh disk — Synapse DB + signing key reset. Acceptable now (the live node has no users/rooms) precisely because bootstrap is declarative; flag loudly before any future roll on a node people use. Real Matrix-data persistence across rolls is out of scope for v1.

---

## 15. Security model (summary)

- **Trust boundary in:** the sidecar (chain-authenticated `senderMemberId`, decryption to our key) + the sealed sender allowlist (on-chain channel); Synapse-attested `event.sender` + the sealed MXID allowlist (human channel).
- **Capability:** full Synapse admin/client API — fenced by **scope, not a verb shortlist**: only Synapse's HTTP surface (never shell/host/other services), every call shape-checked + path-scoped + audited, destructive ops confirmed in chat.
- **Egress:** network-enforced deny-all except Synapse/Postgres/pinned-LLM (§12) — the load-bearing exfiltration control.
- **Secrets:** registration secret used once then dropped; bot/admin tokens in memory (+ sealed bot password, Postgres-backed token cache); nothing sensitive ever enters LLM prompts, logs, or the repo. `create_login_token` is the one sanctioned secret-bearing *reply*: a freshly minted token travels runtime-direct to the operator's room (or the encrypted on-chain reply) — the LLM never sees it.
- **Determinism:** the on-chain channel never invokes the LLM. The LLM is a natural-language front-end for the human channel only, with no privileged path.

---

## 16. Tests (v1)

1. **SynapseAdmin (respx-mocked HTTP):** the nonce+HMAC computation is pinned by a golden test (the `\x00`-separated SHA1 input is easy to get wrong); generic `request` sets the right token/path; the admin token never appears in any log line.
2. **Executor + validation:** every verb's pydantic model (valid pass, malformed/oversized/out-of-range reject); scope-check rejects out-of-prefix paths; destructive verbs log before executing; dedup ledger conflict path resends-not-re-executes.
3. **LLM routing (mock LLM):** scripted `tool_calls` → the loop validates `arguments` (malformed → tool-error, **not** execution), enforces `MAX_TOOL_STEPS`, rejects a non-existent tool name; proves the model cannot escape the verb set or bypass validation.
4. **Egress lockdown (mandatory before live):** from inside the agent container, an arbitrary outbound (`curl https://example.com`, raw TCP dial) is **DROPPED**; Synapse/Postgres/the pinned LLM host succeed.
5. **Adapters + bootstrap:** sidecar-adapter kind-filter + sender-allowlist + dispatch (stub gRPC stream); matrix-adapter MXID gate + reply (stub nio); bootstrap ordering/idempotency + mesh-down tolerance (sidecar socket absent → bootstrap still completes, `/healthz` green).
6. **Local integration (`docker-compose.test.yaml`, no CVM/chain):** postgres + synapse + a known-secret minimal `synapse-init` + a `stub_sidecar` (UDS gRPC emitting one `ensure_user` command + recording acks) + the agent. Asserts: healthcheck green; bot account is admin; the stub command created the target user; the ack was recorded. A second test drives the Matrix path against a fake OpenAI-compatible LLM.
7. **Sidecar (in `sidecar/`):** `app_payload_is_not_a_peer_endpoint`; `app_message_reaches_subscriber`; extend the `#[ignore]`d integration A→B-delivered / not-to-C to a non-PeerEndpoint message.

---

## 17. Open questions / deferred

- **On-chain `MatrixAdminFacet`** for live Safe-controlled authz rotation (removes the §8.3 "rotation = redeploy" cost). Needs a new facet + storage namespace + diamond cut + migration spec (per CLAUDE.md). Deferred; sealed-env allowlist ships v1.
- **Proto subscribe-time `kind` filter** (`SubscribeMessages(SubscribeFilter)`) — unneeded for a single co-located app; the agent self-filters. Deferred.
- **dstack-derived bot password** (zero secrets in env) — adds a dstack-client dependency + attestation-method coupling; deferred in favor of sealed `BOT_PASSWORD`.
- **Matrix-data persistence across rolls** — needs volume-preserving deploy support that does not exist today.
- **redpill model selection** — must support tool/function calling; pinned via `LLM_MODEL`.

---

## References

- Master: [`attestmesh-coordination-layer.md`](./attestmesh-coordination-layer.md) — encryption (§3), messaging (§5/§7), sidecar (§7).
- [`sidecar.md`](./sidecar.md) — app-facing gRPC (§12), the inbound-delivery semantics §6.1 realizes (§12.3), CSK/peer model.
- [`contracts.md`](./contracts.md) — `MessageFacet` (§5), reserved envelope/kind registry (§5.2).
- Deploy: `deploy/compose/matrix-node.yaml`, `deploy/matrix-node.sh`, `deploy/matrix-node-box.py`, `deploy/matrix-node-steps-log.md`.
