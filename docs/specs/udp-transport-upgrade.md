# Pure-UDP Hole-Punched Mesh Transport

**Status:** IMPLEMENTED
**Author:** LSDan
**Created:** 2026-06-10
**Last Updated:** 2026-06-10
**Parent spec:** [`attestmesh-coordination-layer.md`](./attestmesh-coordination-layer.md) §7 / [`sidecar.md`](./sidecar.md) §10
**Component:** `sidecar/`

## Overview

v1 carries every wireguard link over length-prefixed UDP-over-TCP through the dstack
gateway's TLS-passthrough route (`<member>-51900s.<gateway-domain>`), because dstack
CVMs accept no inbound UDP. This works and is live, but every datagram pays the
gateway round-trip plus TCP head-of-line blocking, and the gateway is a shared
dependency on the data path of every peer link.

The 2026-06-10 probe run on the prod5 fleet (`docs/deployment.md` §status-log) proved
the upgrade path: outbound UDP works, NAT mapping is endpoint-independent, **two-sided
simultaneous UDP hole-punching succeeds (including hairpin)**, and a node can discover
its own egress IP from gateway-domain DNS — no STUN, no rendezvous server. One-sided
punching does **not** work (filtering is address-restricted), so both peers must fire
simultaneously.

This spec wires that result in: each established wireguard link is upgraded in place
from the gateway-TCP leg to a direct punched UDP path, with **gateway TCP remaining the
permanent, first-class fallback**. Mesh bring-up, health gating, and on-chain surface
are unchanged; punch coordination rides the already-authenticated mesh itself.

## Requirements

### Must Have

- [x] Every peer link bootstraps over gateway TCP exactly as today; the punch upgrade
      runs only after the link is established (wg handshake completed over TCP).
- [x] Punch coordination is peer-to-peer over the existing in-mesh peer gRPC channel
      (`PeerControl` on `mesh_ip:50051`, the same channel as the CSK pull) — no new
      on-chain messages, no third-party coordinator.
- [x] Both sides attempt the punch simultaneously from the kernel wireguard socket
      itself (by retargeting the wg peer endpoint), so the NAT mapping that opens is
      the one wireguard will keep using.
- [x] Success is judged by observing a fresh wg handshake on the candidate path within
      a bounded window; on failure or on later death of the UDP path, the link reverts
      to the gateway-TCP loopback bridge automatically. Mesh health must never depend
      on punch success.
- [x] A peer running an older sidecar (no punch support) degrades cleanly: the gRPC
      call returns `UNIMPLEMENTED` and the link stays on TCP.
- [x] Per-peer transport state (`tcp` / `punching` / `udp`) is exposed on the health
      endpoint and in metrics.
- [x] Punch retry is bounded with exponential backoff; a link that repeatedly fails to
      punch settles on TCP without log spam or churn.

### Should Have

- [x] Peer-reflexive candidate refinement: when one direction of a punch lands,
      the receiving side reads the observed source `(ip, port)` from the wg device
      (wireguard roams the endpoint on the first authenticated packet) and reports it
      back over `PeerControl`, so the next attempt uses the precise external mapping
      instead of the port-preservation guess.
- [x] Optional advertised UDP candidate in the `PeerEndpoint` envelope (new optional
      CBOR fields — backward compatible) to cut one negotiation round trip.

### Must NOT Have

- No STUN/TURN servers, no rendezvous service, no new external dependency.
- No removal of the TCP transport path — on gateway fleets (the default and only
  deployed profile) it is the permanent fallback and the bootstrap path. (A
  `WG_TCP_PORT=0` config door exists for hypothetical native-inbound-UDP fleets; see
  Open Questions.)
- No change to heartbeats, convergence gating, CSK flow, or any on-chain interface.

## Non-Requirements

- Relay selection / multi-hop routing when both TCP and UDP fail (out of scope; if the
  gateway path is down the node is unreachable today too).
- QUIC or any transport other than wireguard-native UDP.
- IPv6 candidates (prod5 fleet is v4; revisit when a target fleet offers v6).

## Design

### Architecture

Today, kernel wg's endpoint for each peer is a **loopback UDP socket** owned by the
per-peer bridge task (`sidecar/src/transport/mod.rs:203`), which frames datagrams over
TLS to the peer's gateway ingress. The upgrade does not touch that machinery; it adds a
**link upgrader** that, per peer, can swap the wg endpoint between two values:

- `Endpoint::Tcp` — the loopback bridge address (today's behavior, and the fallback);
- `Endpoint::Udp(ip:port)` — the peer's punched external address.

Because wireguard authenticates every packet against the on-chain-pinned peer key,
retargeting the endpoint is safe at any moment: a candidate that doesn't work simply
produces no handshake, and reverting restores the TCP path. The wg session itself
survives the endpoint swap (same keys, same session); at worst it re-handshakes.

#### Link state machine (per peer)

```
            punch negotiated (both sides)        fresh handshake on UDP path
   ┌─────┐ ───────────────────────────────► ┌──────────┐ ─────────────────► ┌─────┐
   │ TCP │                                  │ PUNCHING │                    │ UDP │
   └─────┘ ◄─────────────────────────────── └──────────┘                    └─────┘
      ▲        timeout (PUNCH_TIMEOUT_SECS,                                    │
      │        endpoint reverted, backoff++)                                   │
      └────────────────────────────────────────────────────────────────────────┘
                 UDP path dead: handshake age > WG_REKEY_TIMEOUT(180s)
                 or keepalive silence; revert + schedule re-punch
```

`first_converged` and heartbeat liveness are computed exactly as today — they ride
inside wireguard and are transport-oblivious.

### Components

1. **`transport::punch` (new module)** — candidate discovery, the punch scheduler, and
   the endpoint swap/revert logic. Owns per-peer `LinkTransport` state inside the
   existing peer table (`sidecar/src/wg/peer.rs` `PeerInfo` gains a `transport` field).
2. **`PeerControl` gRPC extension** (`sidecar/proto/peer.proto`) — two new RPCs:

   ```protobuf
   message PunchOffer {
     bytes  requester_member_id = 1;   // 32 bytes
     repeated Candidate candidates = 2;
     uint64 start_at_ms = 3;           // proposed T0 (unix ms)
     bytes  nonce = 4;                 // 16 bytes, correlates offer/result
   }
   message Candidate { string ip = 1; uint32 port = 2; CandidateKind kind = 3; }
   enum CandidateKind { GUESS = 0; PEER_REFLEXIVE = 1; ADVERTISED = 2; }
   message PunchAccept { repeated Candidate candidates = 1; uint64 start_at_ms = 2; }
   message PunchReport {
     bytes nonce = 1;
     bool  success = 2;
     Candidate observed_source = 3;    // peer-reflexive: what wg actually saw
   }

   rpc NegotiatePunch(PunchOffer) returns (PunchAccept);
   rpc ReportPunch(PunchReport) returns (Empty);
   ```

   The channel is the existing peer gRPC over the converged mesh, so both parties are
   already mutually authenticated members; no extra signing is needed.
3. **Candidate discovery** — egress IP from a DNS lookup of `GATEWAY_DOMAIN` (verified
   live: gateway-domain DNS == egress IP); first-attempt external port assumes port
   preservation of `WG_LISTEN_PORT` (51821). Subsequent attempts use peer-reflexive
   candidates from `PunchReport.observed_source`.
4. **Punch executor** — at the agreed `start_at_ms`, set the wg peer endpoint to the
   best candidate; wireguard's own handshake initiations + `persistent_keepalive 25`
   are the punch probes (they originate from the real wg socket, opening the correct
   NAT mapping). Poll the wg device for `last_handshake` newer than T0 and an endpoint
   matching a candidate. On success: latch `UDP`, send `PunchReport{success}` carrying
   the observed source. On timeout: restore the loopback-bridge endpoint, send
   `PunchReport{!success}` with whatever was observed, schedule retry.
5. **UDP-path watchdog** — while in `UDP`, if handshake age exceeds the wireguard
   rekey-attempt window (180 s) or keepalives go silent, revert to TCP immediately and
   schedule a re-punch with backoff. Reverting is one endpoint swap; the TLS bridge
   task for that peer is kept alive (idle) the whole time precisely so fallback is
   instant.

### Interfaces

- **Config** (all optional, sane defaults): `WG_UDP_PUNCH=true|false` (default true),
  `PUNCH_TIMEOUT_SECS` (default 10), `PUNCH_RETRY_BACKOFF_SECS` (default 30, doubling,
  cap 3600), `PUNCH_INITIATOR_RULE` — the peer with the lexically lower memberId sends
  `PunchOffer` (deterministic, avoids offer glare).
- **Health endpoint** — per-peer `transport: "tcp"|"punching"|"udp"`, plus counters
  `punch_attempts_total`, `punch_success_total`, `udp_reverts_total`.
- **`PeerEndpoint` envelope** — unchanged Must-Have; Should-Have adds optional
  `udp_ip`/`udp_port` fields (CBOR maps tolerate unknown fields both directions —
  verified property of the existing `ciborium` decode).

### Data Model

`PeerInfo` gains:

```rust
pub enum LinkTransport { Tcp, Punching { since_ms: u64 }, Udp { endpoint: SocketAddr } }
pub struct PunchState { attempts: u32, next_retry_ms: u64, reflexive: Option<SocketAddr> }
```

No persistent state: punched endpoints are rediscovered after restart (NAT mappings
don't survive reboots anyway; the TCP bootstrap makes rediscovery cheap).

## Open Questions

- [ ] Clock skew on `start_at_ms`: CVMs run NTP-synced clocks and the punch window is
      seconds wide, so unix-ms T0 should suffice — confirm on the fleet, otherwise
      switch to a "both sides start on RPC completion" convention.
- [x] ~~Should `WG_TCP_PORT` ingress remain mandatory, or may a UDP-only operator
      profile exist later?~~ **Resolved (2026-06-10): leave the door open.**
      `WG_TCP_PORT=0` disables the TCP ingress for fleets with genuine inbound UDP
      (no gateway in the path); such nodes connect directly using the
      `PeerEndpoint` envelope's existing `host`/`port` fields and never punch — and
      accept that they have no fallback path. Default remains the gateway-TCP
      ingress, and on gateway fleets TCP stays the permanent fallback; detailed
      UDP-only bring-up is out of scope until such a fleet exists (no test-matrix
      row yet, just the config door and a validation error if `WG_TCP_PORT=0` is
      combined with a `GATEWAY_DOMAIN`-derived endpoint scheme).
- [ ] Does retargeting the endpoint mid-session ever wedge `defguard_wireguard_rs`
      peer config on the kernel impl? Needs a soak test in CI's netns harness.

## Alternatives Considered

### Coordinate the punch via MessageFacet (on-chain envelopes)
Rejected: punch negotiation is latency-sensitive and chatty (retries, reflexive
refinement); on-chain messages cost sponsored UserOps and seconds of latency. The mesh
itself is already an authenticated channel between exactly the right two parties.

### Dedicated UDP probe sockets + socket handoff
Probing from a separate socket discovers a mapping for *that* socket, not wireguard's;
handing the mapping over requires SO_REUSEPORT tricks that fight the kernel wg
implementation. Retargeting the wg endpoint and letting wg's own packets punch is
simpler and exercises the exact 5-tuple that will carry traffic.

### UDP-primary with TCP for bootstrap only
Rejected per design decision (2026-06-10): a fleet/NAT policy change could then break
a previously healthy mesh. TCP stays a permanent fallback; punch failure is a
performance note, never a health failure.

## Traceability

All implementation and tests live in `sidecar/`; test names are in
`cargo test`'s suite (module path shown).

| Requirement | Implementation | Tests |
|-------------|----------------|-------|
| TCP bootstrap unchanged; punch only after the link is established | `src/transport/punch.rs` (`due_for_punch` gates on configured + heartbeat-live + bridge recorded); bridge bootstrap untouched in `src/transport/mod.rs` / `src/bringup.rs` | `transport::punch::due_for_punch_applies_every_gate` |
| P2P negotiation over `PeerControl`, no new on-chain surface | `proto/peer.proto` (`NegotiatePunch`/`ReportPunch`), `src/peer_grpc.rs`, `Puncher::{initiate,handle_offer}` in `src/transport/punch.rs` | `transport::punch::handle_offer_validates_inputs`, `transport::punch::punch_proto_round_trips` |
| Simultaneous punch from the kernel wg socket (endpoint retarget) | `MeshControl::set_peer_endpoint` + `peer_status` (`src/wg/mod.rs`, `wg show dump` parser), `execute_punch`; deterministic T0 agreement in `handle_offer`/`initiate` | `wg::parse_wg_dump_finds_peer_endpoint_and_handshake`, `transport::punch::execute_punch_latches_fresh_handshake_on_candidate_path` |
| Fresh-handshake success window; auto-revert on failure/UDP death; health independent of punch | `execute_punch` (loopback-handshake rejection, timeout revert), `Puncher::watchdog_pass`; health gates untouched (`src/health.rs`) | `transport::punch::execute_punch_{rejects_loopback_handshake,times_out_on_stale_handshake}_and_reverts`, `transport::punch::responder_punch_latches_udp_then_watchdog_reverts`, `health::healthz_and_metrics_expose_transport_state` |
| Old peers degrade cleanly (`UNIMPLEMENTED` → stay on TCP) | `Puncher::on_unimplemented` (re-probe only at the backoff cap); disabled nodes present the same surface (`src/peer_grpc.rs`) | `peer_grpc::punch_rpcs_unimplemented_when_disabled`, `transport::punch::unimplemented_marks_peer_unsupported_at_cap_cadence` |
| Per-peer transport on health endpoint + metrics | `LinkTransport` in `src/wg/peer.rs`; `/healthz` `transports` + `punch` counters and `/metrics` Prometheus text in `src/health.rs`; counters in `state::PunchMetrics` | `health::healthz_and_metrics_expose_transport_state`, `wg::peer::transport_swaps_and_nonce_lookup` |
| Bounded exponential backoff, no churn | `backoff_ms` (doubling, cap 3600 s), `fail_backoff`, `watchdog_pass` re-punch scheduling | `transport::punch::backoff_doubles_and_caps`, `transport::punch::responder_punch_failure_reverts_and_backs_off` |
| Peer-reflexive refinement (Should-Have) | `PunchReport.observed_source` capture in `run_punch`; `handle_report` stores `self_reflexive`; `rank_targets` prefers reflexive candidates | `transport::punch::handle_report_stores_self_reflexive_by_nonce`, `transport::punch::rank_targets_orders_dedups_and_sanitizes` |
| Advertised UDP candidate in `PeerEndpoint` (Should-Have, CBOR-compatible) | optional `udp_ip`/`udp_port` in `src/envelopes.rs`; sent in `bringup::send_peer_endpoint`, absorbed in `bringup::poll_envelopes` | `envelopes::peer_endpoint_udp_fields_are_backward_and_forward_compatible`, `envelopes::peer_endpoint_udp_addr_rejects_garbage` |
| Config knobs (`WG_UDP_PUNCH`, `PUNCH_TIMEOUT_SECS`, `PUNCH_RETRY_BACKOFF_SECS`) | `src/config.rs`; wired in `bringup::launch` | `config::punch_knobs_parse_and_disable` |
| `WG_TCP_PORT=0` door rejected with `GATEWAY_DOMAIN` | validation in `Config::from_env` | `config::tcp_port_zero_rejected_with_gateway_domain` |

## Changelog

| Date | Author | Changes |
|------|--------|---------|
| 2026-06-10 | LSDan | Initial draft |
| 2026-06-10 | LSDan | Status → IMPLEMENTING; implementation started on `milestone-b-udp-transport-upgrade` |
| 2026-06-10 | LSDan | Status → IMPLEMENTED: `transport::punch` module, `NegotiatePunch`/`ReportPunch` RPCs, per-peer link state machine with revert, UDP-path watchdog, config knobs, health/metrics exposure, traceability filled. Fleet-validation-only items (clock-skew tolerance on `start_at_ms`, defguard endpoint-retarget soak) remain open checkboxes above. |
