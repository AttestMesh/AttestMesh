# AttestMesh indexer (attestmesh-indexer) OCI image.
# Build context is the REPO ROOT (the indexer compiles the sidecar-owned canonical
# proto via ../sidecar/proto/indexer.proto — spec §2.2):
#   docker build -t ghcr.io/attestmesh/attestmesh-indexer:latest -f indexer/Dockerfile .
#
# Runs in an attested dstack CVM; mounts /var/run/dstack.sock for key derivation +
# the binding quote, and a persistent volume for the sled cursor store.

FROM rust:1-bookworm AS builder
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends \
        pkg-config libssl-dev \
    && rm -rf /var/lib/apt/lists/*
COPY sidecar/proto sidecar/proto
COPY indexer indexer
WORKDIR /build/indexer
RUN cargo build --release --bin attestmesh-indexer && strip target/release/attestmesh-indexer

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates libssl3 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /build/indexer/target/release/attestmesh-indexer /usr/local/bin/attestmesh-indexer
ENTRYPOINT ["/usr/local/bin/attestmesh-indexer"]
