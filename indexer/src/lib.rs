//! AttestMesh Indexer — attested off-chain event distribution (spec
//! `docs/specs/indexer.md`).
//!
//! The library crate exposes every module to the unit/integration tests; the
//! `attestmesh-indexer` binary is a thin wrapper that wires them into the runtime.

/// Generated protobuf types (canonical `proto/indexer.proto`, owned by the sidecar
/// component and compiled in-place by `build.rs`).
pub mod pb {
    tonic::include_proto!("attestmesh.indexer.v1");
}

pub mod chain;
pub mod config;
pub mod dstack;
pub mod grpc;
pub mod health;
pub mod identity;
pub mod metrics;
pub mod query;
pub mod registry;
pub mod runtime;
pub mod state;
