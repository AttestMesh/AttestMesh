# AttestMesh sidecar (cluster-mesh-agent) OCI image.
# Build context is this crate dir (standalone; protoc is vendored via protoc_bin_vendored).
#   docker build -t ghcr.io/attestmesh/cluster-mesh-agent:latest ./sidecar
#
# Runs inside every dstack CVM; mounts /var/run/dstack.sock for the guest agent.

FROM rust:1.92.0-bookworm@sha256:e90e846de4124376164ddfbaab4b0774c7bdeef5e738866295e5a90a34a307a2 AS builder
WORKDIR /build
# reqwest pulls in openssl (native-tls); protoc is vendored, so no system protobuf needed.
RUN apt-get update && apt-get install -y --no-install-recommends \
        pkg-config libssl-dev \
    && rm -rf /var/lib/apt/lists/*
COPY . .
RUN cargo build --locked --release --bin cluster-mesh-agent --bin attestmesh-registration-calldata \
    && strip target/release/cluster-mesh-agent target/release/attestmesh-registration-calldata

# Minimal runtime. wireguard-tools + iproute2 for mesh bring-up; libssl3 for TLS reads.
FROM debian:bookworm-slim@sha256:7b140f374b289a7c2befc338f42ebe6441b7ea838a042bbd5acbfca6ec875818
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates libssl3 wireguard-tools iproute2 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /build/target/release/cluster-mesh-agent /usr/local/bin/cluster-mesh-agent
COPY --from=builder /build/target/release/attestmesh-registration-calldata /usr/local/bin/attestmesh-registration-calldata
ENTRYPOINT ["/usr/local/bin/cluster-mesh-agent"]
