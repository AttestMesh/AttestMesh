# AttestMesh sidecar (cluster-mesh-agent) OCI image.
# Build context is this crate dir (standalone; protoc is vendored via protoc_bin_vendored).
#   docker build -t ghcr.io/attestmesh/cluster-mesh-agent:latest ./sidecar
#
# Runs inside every dstack CVM; mounts /var/run/dstack.sock for the guest agent.

FROM rust:1-bookworm AS builder
WORKDIR /build
# reqwest pulls in openssl (native-tls); protoc is vendored, so no system protobuf needed.
RUN apt-get update && apt-get install -y --no-install-recommends \
        pkg-config libssl-dev \
    && rm -rf /var/lib/apt/lists/*
COPY . .
RUN cargo build --release --bin cluster-mesh-agent && strip target/release/cluster-mesh-agent

# Minimal runtime. wireguard-tools + iproute2 for mesh bring-up; libssl3 for TLS reads.
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates libssl3 wireguard-tools iproute2 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /build/target/release/cluster-mesh-agent /usr/local/bin/cluster-mesh-agent
ENTRYPOINT ["/usr/local/bin/cluster-mesh-agent"]
