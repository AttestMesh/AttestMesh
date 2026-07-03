# clickhouse-ha — ClickHouse server + Keeper + HAProxy for the AttestMesh ClickHouse HA
# cluster nodes. One image, three container roles selected by entrypoint override in
# deploy/compose/clickhouse-ha-node.yaml:
#   chha-keeper-entrypoint.sh | chha-server-entrypoint.sh | chha-haproxy-entrypoint.sh
# Keeper is the bundled clickhouse-keeper (no zookeeper image). All shared secrets are
# HKDF-derived from the cluster shared key at boot (same recipe as pg-ha); the image
# contains none. Built/pushed by .github/workflows/build-clickhouse-ha.yml.
FROM clickhouse/clickhouse-server:24.8@sha256:1ffa82edee000a42c09313bd9f1293d94c570aee74babc1b3ca9983a35fa597b  # pinned 2026-07-03

ARG GRPCURL_VERSION=v1.9.3

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        haproxy jq iproute2 curl ca-certificates python3; \
    curl -fsSL -o /tmp/grpcurl.tar.gz "https://github.com/fullstorydev/grpcurl/releases/download/${GRPCURL_VERSION}/grpcurl_${GRPCURL_VERSION#v}_linux_x86_64.tar.gz"; \
    tar -xzf /tmp/grpcurl.tar.gz -C /usr/local/bin grpcurl; \
    chmod +x /usr/local/bin/grpcurl; \
    rm -rf /tmp/*.tar.gz /var/lib/apt/lists/*; \
    clickhouse-server --version; clickhouse-keeper --version || true; haproxy -v; grpcurl --version

COPY agent.proto /etc/attestmesh/agent.proto
COPY chha-common.sh chha-keeper-entrypoint.sh chha-server-entrypoint.sh \
     chha-haproxy-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/chha-*.sh

# Roles pick their own entrypoint in the compose; neutralize the stock image entrypoint.
ENTRYPOINT []
CMD ["chha-server-entrypoint.sh"]
