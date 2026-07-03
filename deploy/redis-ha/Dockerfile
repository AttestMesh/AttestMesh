# redis-ha — Redis + Sentinel + HAProxy for the AttestMesh Redis HA cluster nodes.
# One image, three container roles selected by entrypoint override in deploy/compose/redis-ha-node.yaml:
#   redisha-redis-entrypoint.sh | redisha-sentinel-entrypoint.sh | redisha-haproxy-entrypoint.sh
# All shared secrets are HKDF-derived from the cluster shared key at boot (same recipe as pg-ha);
# the image contains none. Built/pushed by .github/workflows/build-redis-ha.yml.
FROM redis:7.4-bookworm@sha256:b2b95679e3b46fb51864949ed25ea976fc3a6bcc00a40a1bc00d568cb2822e50  # pinned 2026-07-03

ARG GRPCURL_VERSION=v1.9.3

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        haproxy jq iproute2 curl ca-certificates python3; \
    curl -fsSL -o /tmp/grpcurl.tar.gz "https://github.com/fullstorydev/grpcurl/releases/download/${GRPCURL_VERSION}/grpcurl_${GRPCURL_VERSION#v}_linux_x86_64.tar.gz"; \
    tar -xzf /tmp/grpcurl.tar.gz -C /usr/local/bin grpcurl; \
    chmod +x /usr/local/bin/grpcurl; \
    rm -rf /tmp/*.tar.gz /var/lib/apt/lists/*; \
    redis-server --version; haproxy -v; grpcurl --version

COPY agent.proto /etc/attestmesh/agent.proto
COPY redisha-common.sh redisha-redis-entrypoint.sh redisha-sentinel-entrypoint.sh \
     redisha-haproxy-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/redisha-*.sh
