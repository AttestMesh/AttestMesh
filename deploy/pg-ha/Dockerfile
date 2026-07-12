# pg-ha — Patroni + etcd + HAProxy on top of postgres-walg (Postgres 16 + wal-g + grpcurl).
# One image, three container roles selected by entrypoint override in deploy/compose/pg-ha-node.yaml:
#   pgha-etcd-entrypoint.sh | pgha-patroni-entrypoint.sh | pgha-haproxy-entrypoint.sh
# All shared secrets are HKDF-derived from the cluster shared key at boot (see docs/specs/pg-ha.md §4);
# the image contains none. Built/pushed by .github/workflows/build-pg-ha.yml.
FROM ghcr.io/attestmesh/postgres-walg@sha256:0edf068ddf5f89f73b41a00ffaf94679495c1d67a559f47f2e713036c6a681ad

ARG ETCD_VERSION=v3.5.17
ARG PATRONI_SPEC="patroni[etcd3]~=4.0.4"

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        python3-venv haproxy jq iproute2 curl ca-certificates postgresql-16-pgvector; \
    python3 -m venv /opt/patroni; \
    /opt/patroni/bin/pip install --no-cache-dir "${PATRONI_SPEC}" "psycopg[binary]"; \
    ln -s /opt/patroni/bin/patroni /usr/local/bin/patroni; \
    ln -s /opt/patroni/bin/patronictl /usr/local/bin/patronictl; \
    curl -fsSL -o /tmp/etcd.tar.gz \
        "https://github.com/etcd-io/etcd/releases/download/${ETCD_VERSION}/etcd-${ETCD_VERSION}-linux-amd64.tar.gz"; \
    tar -xzf /tmp/etcd.tar.gz -C /tmp; \
    install -m 0755 "/tmp/etcd-${ETCD_VERSION}-linux-amd64/etcd" /usr/local/bin/etcd; \
    install -m 0755 "/tmp/etcd-${ETCD_VERSION}-linux-amd64/etcdctl" /usr/local/bin/etcdctl; \
    rm -rf /tmp/etcd* /var/lib/apt/lists/*; \
    etcd --version; patroni --version; haproxy -v

COPY pgha-common.sh pgha-etcd-entrypoint.sh pgha-patroni-entrypoint.sh pgha-haproxy-entrypoint.sh \
     pgha-backup-loop.sh pgha-post-init.sh pgha-walg-bootstrap.sh walg-archive /usr/local/bin/
RUN chmod +x /usr/local/bin/pgha-*.sh /usr/local/bin/walg-archive
