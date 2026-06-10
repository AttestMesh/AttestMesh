//! gRPC server module (spec §7.4, §8). The tonic server is bootstrapped from `main`
//! (which owns the graceful-shutdown signal); this module provides the service impl
//! (`service`), the envelope sign/verify convention (`envelope`), and per-session
//! plumbing (`subscribe`).

pub mod envelope;
pub mod service;
pub mod subscribe;
