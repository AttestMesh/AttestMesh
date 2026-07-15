# AttestMesh indexer (attestmesh-indexer) OCI image.
# Build context is the REPO ROOT (the indexer compiles the sidecar-owned canonical
# proto via ../sidecar/proto/indexer.proto — spec §2.2):
#   docker build -t ghcr.io/attestmesh/attestmesh-indexer:latest -f indexer/Dockerfile .
#
# Runs in an attested dstack CVM; mounts /var/run/dstack.sock for key derivation +
# the binding quote, and a persistent volume for the sled cursor store.

FROM rust:1.92.0-bookworm@sha256:e90e846de4124376164ddfbaab4b0774c7bdeef5e738866295e5a90a34a307a2 AS builder
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends \
        pkg-config libssl-dev \
    && rm -rf /var/lib/apt/lists/*
COPY sidecar/proto sidecar/proto
COPY indexer indexer
WORKDIR /build/indexer
RUN cargo build --locked --release --bin attestmesh-indexer && strip target/release/attestmesh-indexer

FROM rust:1.92.0-bookworm@sha256:e90e846de4124376164ddfbaab4b0774c7bdeef5e738866295e5a90a34a307a2 AS registration-helper
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends \
        pkg-config libssl-dev protobuf-compiler \
    && rm -rf /var/lib/apt/lists/*
COPY sidecar sidecar
WORKDIR /build/sidecar
RUN cargo build --locked --release --bin attestmesh-registration-calldata \
    && strip target/release/attestmesh-registration-calldata

FROM debian:bookworm-slim@sha256:7b140f374b289a7c2befc338f42ebe6441b7ea838a042bbd5acbfca6ec875818
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates libssl3 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /build/indexer/target/release/attestmesh-indexer /usr/local/bin/attestmesh-indexer
COPY --from=registration-helper /build/sidecar/target/release/attestmesh-registration-calldata /usr/local/bin/attestmesh-registration-calldata
ENTRYPOINT ["/usr/local/bin/attestmesh-indexer"]
