# PitchRotator MCP Dedicated Network

**Status:** SPECCED — not LIVE; deployment and hardware-backed verification are
UNVERIFIED
**Source pin:** `AlbionaHoti/Pitch-Rotator` commit
`66b5495b0ea0695ef6d2a35969d444da4f680a52` (private upstream repository)
**Deploy target:** upstream `mcp-server/` only
**Excluded surface:** upstream Next.js `src/lib/connect/mcp-handler.ts` and its
`/api/mcp` pairing flow
**Issue:** [#35](https://github.com/AttestMesh/AttestMesh/issues/35)
**Last updated:** 2026-07-20

This document uses the following claim labels deliberately:

- **SPECCED** — required by this design but not thereby present in code or a CVM.
- **IMPLEMENTED** — present in source at the named revision or in this repository.
- **LIVE** — exercised successfully on the intended production infrastructure with
  retained evidence.
- **DEFERRED** — intentionally outside v1; it must not be implied by v1 claims.
- **UNVERIFIED** — plausible or implemented, but not proven at the relevant trust
  boundary.

No statement in this document upgrades an IMPLEMENTED or SPECCED property to LIVE.

## 1. Decision summary

The v1 deployment is a **new, dedicated AttestMesh network**, not another service in
an existing general-purpose cluster. It contains one PitchRotator application CVM and
only the minimum admitted client/operations members needed to use and verify it.
On-chain state is the source of truth for membership and bring-up.

The MCP endpoint is **mesh-only**. A founder reaches it through an admitted,
attestation-verifying client/agent that is itself a member of this network. There is
no public MCP gateway, public dstack gateway route, browser-to-enclave adapter, or
host-published MCP port in v1. A public ingress is DEFERRED until it has its own auth,
attestation-enforcement, abuse-control, and exposure design.

The first deployment is one application replica with **ephemeral-only session state**.
Restart, update, crash, or CVM replacement invalidates every MCP session and loses its
working data. Clients must re-attest, re-initialize, and re-ingest. Sealed persistence
and multi-replica routing are DEFERRED. This choice avoids claiming durability or
inventing a shared store that would widen the confidential boundary.

Raw founder text may enter the CVM only after the client has verified a real,
measurement-pinned TDX attestation and authenticated to the service. It may exist
transiently in enclave memory, but only policy-approved/redacted founder-derived
fields may leave for a model provider. The external model provider remains a trusted
data processor for those fields; TDX does not make model egress confidential from the
provider.

## 2. Status ledger

| Claim | Status | Evidence or gate |
|---|---|---|
| Upstream source is pinned to the full commit above | SPECCED | Build must fetch that exact commit and record its tree/image digest; a branch, tag, or `latest` is insufficient. |
| Private upstream source and deployable application image are available to the operator/CVM | UNVERIFIED | The repository is private. Release must prove authorized checkout at the exact commit and an immutable, pullable image digest without exposing either credential. |
| Standalone server exposes `pitch_ingest`, `pitch_prep`, `pitch_evaluate`, `pitch_reprep`, and `pitch_receipt` over Streamable HTTP | IMPLEMENTED@`66b5495b0ea0695ef6d2a35969d444da4f680a52` | Source investigation recorded in issue #35. |
| Standalone session data and receipt ledgers are process-local and in-memory | IMPLEMENTED@`66b5495b0ea0695ef6d2a35969d444da4f680a52` | Three `sid`-keyed maps; no durable-store call path. |
| Dedicated AttestMesh network and Path A admission | SPECCED | Requires deploy trio, new cluster deployment, allowlists, registration, and chain-state verification. |
| MCP endpoint is mesh-only | SPECCED | Requires compose/host/gateway inspection plus negative reachability tests. |
| Exact compose and application image measurements are admitted | SPECCED | Build and deploy must pin digests, compute the compose hash, seed the boot gate, and read it back. |
| Real TDX quote is fully verified and measurement-pinned by the client before ingress | SPECCED | Prefix/substring checks do not satisfy this. Requires a standards-compliant verifier and a negative test matrix. |
| Simulator can never satisfy production TDX policy | SPECCED | Requires explicit runtime mode and fail-closed client/server policy. |
| Every founder-derived model field passes one egress policy chokepoint | SPECCED | Current upstream paths for `currentPitch`, `transcript`, and `rawSpan` do not satisfy this. |
| Receipt hash commits to full nested evidence and model-call details | SPECCED | Current upstream canonicalization does not satisfy this. |
| Deployment is LIVE | UNVERIFIED | No successful dedicated-network CVM deployment evidence is recorded here. |
| Public ingress | DEFERRED | Not part of v1. |
| Sealed session persistence and horizontal scaling | DEFERRED | Not part of v1. |

## 3. Scope and topology

### 3.1 Network boundary

The network gets a dedicated ClusterDiamond and membership set. It must not reuse the
membership, CSK, CIDR, or allowlists of C3 or another application network. The normal
AttestMesh Path A lifecycle applies:

```
new cluster policy on chain
  -> deterministic compose and image pin
  -> deploy CVM
  -> allowlist compose hash and app_id
  -> bind/upgrade ClusterMember
  -> sidecar registration and CSK acquisition
  -> WireGuard mesh healthy
  -> application verification
```

**SPECCED:** The deploy surface follows the issue-mandated trio:

- `deploy/pitchrotator-mcp-node.sh`
- `deploy/pitchrotator-mcp-node-box.py`
- `deploy/compose/pitchrotator-mcp-node.yaml`

The trio must be re-entrant and agent-operable from repository state plus its runbook.
It must seal secrets without putting them in source, command arguments, logs, compose
labels, or measured plain environment.

### 3.2 Runtime shape

```
admitted client/agent                     PitchRotator CVM
on the dedicated mesh                    +----------------------------+
  attestation policy + expected hash --> | dstack guest agent (TDX)   |
  verify /attestation                    | cluster-mesh sidecar       |
  MCP Streamable HTTP over WireGuard --> | mesh-only TCP proxy        |
                                          | standalone mcp-server      |
                                          |  - one-process sessions    |
                                          |  - receipt ledger          |
                                          |  - egress policy/redactor  |
                                          +-------------+--------------+
                                                        |
                                                        | policy-approved TLS
                                                        | model request only
                                                        v
                                             external model provider
```

The MCP listener is not published with compose `ports:` and is not exposed through a
public dstack gateway hostname. Following the existing mesh-service pattern, a proxy
in the sidecar network namespace binds the chosen MCP port to the node's mesh
interface. The application container remains on an internal compose network.

### 3.3 Client and ingress model

**SPECCED v1:** clients are admitted mesh members. A client must perform this sequence
for every fresh application identity or session:

1. Read the expected application identity and measurement policy from authenticated
   operator/release metadata; do not learn the expected value from the endpoint being
   verified.
2. Fetch `/attestation` over the intended mesh path and cryptographically verify the
   complete TDX quote, Intel collateral/TCB status, report-data binding, application
   identity, and exact approved compose/image measurement.
3. Reject absent, expired, malformed, unpinned, simulator, debug, or policy-mismatched
   evidence. No founder-derived bytes may have been sent yet.
4. Authenticate with a registry-backed, revocable credential over TLS. The credential
   is never in a URL/query string. The service rate-limits initialization by identity.
5. Initialize MCP and treat `Mcp-Session-Id` as a bearer secret. Send it only in the
   protocol header, never URLs, receipts, telemetry, or logs.
6. On reconnect after an attestation-policy or application-identity change, re-attest
   before resuming. After restart/404, discard the old ID and start a blank session.

TLS protects the application hop inside WireGuard and prevents another admitted mesh
process from reading bearer credentials. Mesh membership alone is network admission,
not founder authorization.

**DEFERRED:** external founders connecting through a public ingress. Such an ingress
would terminate a public transport and therefore must either (a) give the founder an
end-to-end attestation-aware channel to the CVM, or (b) become an explicitly trusted
gateway that enforces attestation and session policy. A generic reverse proxy is not
sufficient. The Next.js `/api/mcp` pairing endpoint may eventually act as a
non-confidential onboarding/control-plane adapter, but it is not within the TDX trust
boundary and must never advertise hardware confidentiality for data it handles.

## 4. Threat model

### 4.1 Protected assets

- raw and redacted founder notes, repository excerpts, locators, and transcripts;
- voice profiles, prepared pitches, recorded takes, evaluations, and claims;
- MCP bearer session IDs, founder credentials, model/API keys, sealed environment,
  CSK, signing/sealing keys, and operator credentials;
- receipt signing keys and integrity of the receipt/evidence ledger;
- application identity, measurement policy, model/prompt/policy versions, and audit
  evidence.

### 4.2 Adversaries and assumptions

| Actor or failure | Capability considered | Required defense |
|---|---|---|
| CVM host/cloud operator | Controls host OS, disks, networking, restarts, and stdout/stderr collection; can deny service or replay stale storage | TDX confidentiality/integrity, fresh measurement-pinned attestation, encrypted transport, ephemeral state, no secret logs. Availability is not guaranteed. |
| Unauthenticated Internet user | Scans, floods, or attempts direct MCP access | No public MCP exposure; host/gateway negative probes. |
| Malicious or compromised mesh member | Can reach mesh listeners and replay/steal application credentials it possesses | Application-layer auth, per-founder authorization, TLS, quotas, revocation, strict session isolation. Mesh membership is not sufficient authorization. |
| Malicious MCP client/input | Supplies oversized inputs, prompt injection, secrets in any field, forged excerpt IDs, or bearer IDs | Bounds and schemas, unified ingress/egress policy, evidence validation, rate limits, constant isolation keys. |
| Model provider or network observer | Sees allowed request fields at provider termination; may retain, alter, or fabricate output | Minimal redacted egress, pinned endpoint/model policy, TLS, response schema/evidence validation, request/response commitments. Provider confidentiality and correctness remain trust assumptions. |
| Compromised dependency/image/supply chain | Changes server behavior or exfiltrates data | Exact source commit, locked dependencies, reproducible image, digest and compose measurement pinning, SBOM/provenance where available. |
| Receipt consumer | Attempts to infer secrets or treats a receipt as proof of claims it does not bind | Secret-free receipt schema, full canonical commitments, signed identity/measurement context, documented guarantee limits. |
| Crash/update/resource exhaustion | Loses state, creates unbounded sessions, or routes a session to the wrong replica | Single replica, TTL/idle expiry, quotas, explicit deletion, restart semantics, memory/rate limits. |

The design does not protect data after an authorized client displays or stores it, does
not hide permitted model input from the model provider, does not guarantee model
truthfulness, and does not guarantee availability against the host or chain.

## 5. Data ingress, storage, and egress

### 5.1 Allowed flow

| Data | May enter CVM? | Stored in v1? | May leave for model? | May appear in logs/receipt? |
|---|---:|---:|---:|---:|
| Raw founder excerpt text | Yes, after attestation + auth | No; transient only until redacted | No | No |
| Locally redacted excerpt text | Yes | Yes, session memory only | Yes, after in-enclave policy pass | Hash/reference only in receipt; never logs |
| `rawSpan` / source locator | Yes, bounded and validated | Only sanitized form | No in v1 | Sanitized locator or hash only |
| Current pitch | Yes | Yes, session memory only | Only after the same in-enclave redaction/policy pass | Hash only |
| Recorded transcript | Yes | Yes, session memory only | Only after the same in-enclave redaction/policy pass | Hash only |
| Voice profile/evaluation/model output | Generated inside | Yes, session memory only | Only when required by the next declared operation and after policy | Hash/structured non-secret metadata only |
| Founder credential / MCP session ID | Yes, protocol use only | Volatile auth state | Never | Never |
| Model API credential / sealed env / CSK | Sealed/runtime only | Runtime secret path only | Only the API credential to its pinned provider as protocol auth | Never |

**IMPLEMENTED upstream:** `pitch_ingest` accepts `text` or `redactedText`, redacts the
selected input inside the process, and stores the redacted form. Raw text is transient.

**IMPLEMENTED upstream but unsafe:** `rawSpan` is stored without redaction and included
in model prompts; `currentPitch` and recorded `transcript` fields reach model calls
without the excerpt redactor. The current regex redactor does not cover general person,
company, or customer identities.

**SPECCED gate:** there is one auditable egress function through which every
founder-derived model-bound field passes. Direct calls to the model client outside that
module fail CI/static checks. It applies length/schema limits, secret and identity
policy, field allowlisting, prompt-version tagging, destination/model allowlisting,
and emits only secret-free counters/hashes. Tests place canary secrets and identities
in every field, including `rawSpan`, `currentPitch`, transcript, prior model output,
and nested structures, and prove none bypasses policy.

The model service hostname and model identity are measured plain configuration. The
API key is sealed into the CVM and supplied by a runtime secret path/environment only;
it is never an argv value. Egress is deny-by-default except DNS and the pinned model
endpoint. Redirects to non-allowlisted hosts are rejected.

### 5.2 Logs and observability

Application, proxy, sidecar, and deployment logs must never contain request/response
bodies, excerpts, pitches, transcripts, credentials, query strings, authorization
headers, `Mcp-Session-Id`, sealed environment, CSK, or model payloads. The upstream
server currently logs `sid`; that is a release blocker, not something solved merely by
asking the host not to export logs. Logging must be changed to a non-secret correlation
ID or one-way domain-separated commitment before founder traffic.

Allowed telemetry is bounded operational metadata: counters, durations, status codes,
memory/session counts, policy version, application version, and non-secret deployment
identity. Verification includes seeded canaries and a search across captured logs.

## 6. Sessions and availability

### 6.1 Upstream behavior

The following is IMPLEMENTED at the pinned upstream commit:

- `sessions`, `MemorySessionStore`, and receipt `ledgers` are module-level in-memory
  maps keyed by random `sid` values;
- `founderRef = sha256(key).slice(0,16)` is recorded but does not participate in
  lookups or existing-session authorization; `sessionStore.get()` has no callers;
- any non-empty `?key=` passes the creation gate, after which `sid` is the effective
  bearer capability;
- the same founder key reconnecting creates a new empty session;
- graceful session close deletes store and ledger entries; unknown IDs return 404;
- restart loses all sessions; `deriveSealingKey()` exists but has no caller;
- there is no TTL, idle expiry, session cap, memory quota, or built-in rate limit;
- a second replica cannot serve the first replica's session.

### 6.2 v1 contract

**SPECCED:** v1 runs exactly one application process/replica. A health check must not
cause a second active process to serve sessions during replacement. Rolling replacement
drains or invalidates all sessions; it does not promise seamless continuity.

Each session has:

- a registry-authenticated founder/principal binding that is rechecked on every
  request, including requests carrying an existing session ID;
- an absolute TTL, shorter idle timeout, explicit delete/close, and revocation path;
- per-session limits for excerpts, takes, bytes, model calls, and concurrent work;
- node-wide limits for active sessions, total memory, request rate, and model spend;
- secret bearer IDs stored and compared without logging or returning them in receipts;
- deterministic cleanup of the transport, session object, ledger, and pending calls;
- cross-session tests covering excerpts, voice profile, pitch, takes, evaluations,
  receipt ledger, close, expiration, and concurrent access.

The exact numeric limits are deploy configuration measured into the compose hash; the
service must fail closed if omitted. Verification must exercise every configured bound.

**SPECCED restart behavior:** session state is ephemeral by design. A restart produces
no recoverable session data, old IDs return 404/unauthorized, and clients are told to
re-attest and re-ingest. Any volume used for caches must contain no founder-derived
plaintext and is disposable.

**DEFERRED:** sealed persistence. If later required, it needs a separate spec covering
AEAD format and associated data, sealing-key purpose/version, rollback/freshness,
deletion semantics, backup, recovery, key rotation, disk quotas, and whether the host
can correlate access. Calling `deriveSealingKey()` and writing JSON to disk is not an
acceptable persistence design.

**DEFERRED:** horizontal scaling. It requires either cryptographically protected
session-affinity routing plus defined failure behavior, or a durable encrypted store
whose privacy and rollback properties are specified. It cannot silently add replicas
to the current in-memory implementation.

## 7. Receipts

A receipt proves only that the identified, attested application reports having run the
committed operations under the committed policy. It does not prove that founder input
was true, a model was honest, an external provider deleted data, or prose claims are
factually correct.

### 7.1 Required binding

**SPECCED:** a v1 receipt is a versioned canonical structure and signature that binds:

- receipt schema/version and a fresh non-secret receipt ID;
- dedicated network/cluster ID, application `app_id`, exact PitchRotator source
  commit, OCI image digest, compose hash, and attestation evidence reference/hash;
- TDX verification policy/version and production-vs-simulator mode;
- non-secret founder/session commitment, never the live MCP bearer `sid`;
- ordered operation ledger with operation type, sequence number, timestamps, and
  hashes of material inputs and outputs;
- for each model call: purpose, provider/model identity and revision when available,
  endpoint policy ID, request hash, response hash, timestamp, referenced excerpt IDs,
  and whether raw-file access was permitted (v1: false);
- generated claims and the exact valid source excerpt IDs supporting each claim;
- redactor, prompt, egress-policy, receipt-canonicalization, and application versions;
- receipt signing public key/algorithm and the signing key's attestation binding.

The receipt excludes founder text, prompts/responses, API keys, session bearer IDs,
and other secrets. Evidence can be retained privately and compared by hash; the
receipt is not a vehicle for publishing founder content.

Canonicalization must recursively encode every nested object and array with an
unambiguous, versioned algorithm before hashing. Runtime validation rejects duplicate,
unknown, or cross-session excerpt IDs. Tamper tests independently mutate every nested
field, reorder ordered ledger entries, replace an evidence reference, and change a
model/policy/version field; every mutation must invalidate the signature or hash.

### 7.2 Current gap

**IMPLEMENTED upstream but unsafe:** the current hash uses
`JSON.stringify(partial, Object.keys(partial).sort())`. Because the replacer key array
is applied recursively, nested `modelCalls` and `generatedClaims` objects serialize
without their material fields. The hash therefore does not bind the complete ledger.
The receipt also includes the live bearer `sessionId`. Both are release blockers.

Receipt generation must return a non-secret receipt ID/commitment and either close the
underlying session or leave it accessible only through independently rechecked founder
authentication. Publishing a receipt must never grant session access.

## 8. TDX, attestation, and simulator separation

### 8.1 Production policy

**SPECCED:** production mode starts only when the dstack guest-agent path supplies real
hardware evidence and the deployment identity is admitted by the dedicated network's
Path A policy. Client verification is ultimately what protects “attest before send”;
the server cannot prove that a client checked it. The supported client or a future
policy gateway must enforce the no-data-before-attestation state machine.

Production client verification must:

1. parse and cryptographically verify the quote and certificate/collateral chain;
2. enforce acceptable TCB status and freshness/expiry policy;
3. verify report-data binding to the application key/challenge and reject replay;
4. compare all required measurements, including the exact approved compose/image
   identity, against authenticated release policy;
5. bind the application TLS identity/key to the verified evidence;
6. return a distinct production verdict only after every check succeeds.

A quote-version prefix, substring search, self-reported `mode`, unpinned measurement,
or successful guest-agent `info()` call is not TDX verification.

### 8.2 Simulator and development

**IMPLEMENTED upstream but unsafe:** current mode detection treats any successful
guest-agent `info()` response, including the dstack simulator, as `tdx`; the existing
client uses structural prefix and optional substring measurement checks and can report
`tdx-verified` without a pinned measurement.

**SPECCED:** runtime modes are disjoint values such as `production-tdx` and
`insecure-simulator`. Simulator evidence has its own unmistakable type and signing
root, is never admitted to the production cluster/allowlist, never returns a production
verdict, and is rejected by production clients regardless of caller flags. Development
mode uses a separate network, credentials, release metadata, visual/CLI warning, and
test data only. There is no environment toggle that upgrades simulator evidence to
production trust.

Negative tests cover simulator responses, missing quote, malformed quote, stale
collateral, unacceptable TCB, wrong app identity, wrong compose/image measurement,
wrong report data, replayed evidence, wrong TLS key, and an unpinned verifier policy.

## 9. Secrets and supply chain

- All credentials and private keys are delivered in `encrypted_env` or an equivalent
  sealed runtime path via a mode-0600 temporary file, never argv or committed files.
- Access to the private `AlbionaHoti/Pitch-Rotator` repository is an explicit build
  gate. An authorized builder must resolve and check out the full commit pin; a failed
  private-repository lookup must abort rather than fall back to a fork, cached branch,
  or locally modified tree. Source-access credentials stay in the builder's secret
  store and are not copied into the runtime image.
- Access to a private application image is a separate deploy gate. Before CVM start,
  the operator records the expected immutable digest and proves the CVM can pull that
  same digest using sealed registry credentials. Pull credentials are not image
  labels, compose literals, build arguments, or public logs. A mutable tag, local
  cache hit, or successful registry login alone does not satisfy the gate.
- Compose holds only non-secret, measured configuration. Deployment output is scrubbed
  and must not echo the sealed env.
- Upstream source is fetched at exactly
  `66b5495b0ea0695ef6d2a35969d444da4f680a52`; submodules and lockfiles are honored.
- The application image is built reproducibly, published and referenced by immutable
  digest. The digest, compose hash, and source commit are recorded together in release
  evidence and receipt metadata.
- Mutable image tags may be informational but are never the deployment authority.
- Dependency audit/SBOM results are retained with the release. A source pin alone does
  not pin registry bases or package-manager downloads.

## 10. Verification gates

Nothing is LIVE until retained evidence demonstrates all applicable checks:

1. deterministic build from the pinned source; record source tree, image digest, and
   compose hash; prove authenticated access to the private source and pullability of
   the private image at that exact digest without credential disclosure;
2. create a new cluster and verify its address, chain ID, policy, membership, distinct
   CIDR/CSK domain, allowlisted app ID, and compose hash from chain state;
3. verify Path A registration, sidecar CSK acquisition, WireGuard peers, and healthy
   mesh state;
4. positive MCP reachability through the mesh path and negative probes from public
   Internet, box host, compose bridge, and dstack public gateway;
5. full production attestation positive test and the negative matrix in §8.2 before
   sending canary founder content;
6. auth, replay, revocation, TTL, idle expiry, explicit close, quota, rate-limit,
   restart-loss, and concurrent cross-session-isolation tests;
7. canary coverage for every ingress/egress field and deny-by-default provider egress;
8. receipt canonicalization, evidence-reference, bearer-disclosure, and nested-tamper
   adversarial tests;
9. log capture and secret scan across application, sidecar, proxy, deploy, and host
   surfaces;
10. TypeScript checks, MCP integration tests, and a complete
    `ingest -> prep -> evaluate -> reprep -> receipt` smoke test.

Each verification record must state date, operator/tool version, chain and cluster,
app ID, CVM ID, source commit, image digest, compose hash, expected measurements,
actual result, and redacted artifact locations. A green local simulator run remains
IMPLEMENTED/UNVERIFIED, never LIVE.

## 11. Release blockers and deferred work

### Release blockers for real founder data

- [ ] Deploy trio and dedicated-network creation are implemented and repeatable.
- [ ] Source/image/compose are immutably pinned and measurement allowlists verified.
- [ ] Private-source checkout and private-image pull gates pass without fallback or
      credential disclosure.
- [ ] Mesh-only exposure is proven by positive and negative probes.
- [ ] Real TDX quote verification, TLS binding, and client-side “attest before send”
      are implemented; simulator is fail-closed.
- [ ] Registry-backed auth, bearer handling, TTL, quotas, deletion, and isolation tests
      satisfy §6.
- [ ] All founder-derived model fields satisfy §5; `rawSpan`, `currentPitch`, and
      transcripts cannot bypass policy.
- [ ] Receipt canonicalization and evidence validation satisfy §7; live bearer IDs are
      absent.
- [ ] Logs and deployment surfaces pass secret/canary scans.
- [ ] Full-loop smoke and restart-loss behavior pass on a real CVM.

### Explicitly DEFERRED

- public/browser ingress and use of Next.js `/api/mcp` as an adapter;
- sealed session persistence, backup/recovery, and resume across restart;
- multiple active PitchRotator replicas and session routing;
- claims that the external model provider cannot see or retain permitted model input;
- publication of founder evidence or receipts to a public/on-chain registry;
- production use of simulator/dev mode.

## 12. Changelog

| Date | Change |
|---|---|
| 2026-07-20 | Initial SPECCED design for a dedicated, mesh-only network at exact upstream commit `66b5495b0ea0695ef6d2a35969d444da4f680a52`; selected one ephemeral application replica, defined ingress/egress and receipt contracts, and separated simulator from production TDX. No LIVE claim. |
