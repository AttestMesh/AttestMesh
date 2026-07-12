# pg-ha — Patroni + etcd + HAProxy on top of Postgres 16 + wal-g + grpcurl.
# One image, three container roles selected by entrypoint override in deploy/compose/pg-ha-node.yaml:
#   pgha-etcd-entrypoint.sh | pgha-patroni-entrypoint.sh | pgha-haproxy-entrypoint.sh
# All shared secrets are HKDF-derived from the cluster shared key at boot (see docs/specs/pg-ha.md §4);
# the image contains none. Built/pushed by .github/workflows/build-pg-ha.yml.
FROM postgres:16-bookworm@sha256:da788743d2060767375896de4d646f7576f5911461444b372616f19ea61db2ec

# Build the postgres-walg layer in this provenance run instead of depending on
# GHCR package-to-package permissions. The source remains shared with the
# standalone Matrix image under deploy/postgres-walg/.
ARG WALG_VERSION=v3.0.8
ARG GRPCURL_VERSION=v1.9.3
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl python3 xz-utils; \
    curl -fsSL -o /tmp/walg.tar.gz "https://github.com/wal-g/wal-g/releases/download/${WALG_VERSION}/wal-g-pg-20.04-amd64.tar.gz"; \
    tar -xzf /tmp/walg.tar.gz -C /usr/local/bin; \
    mv /usr/local/bin/wal-g-pg-20.04-amd64 /usr/local/bin/wal-g; \
    chmod +x /usr/local/bin/wal-g; \
    curl -fsSL -o /tmp/grpcurl.tar.gz "https://github.com/fullstorydev/grpcurl/releases/download/${GRPCURL_VERSION}/grpcurl_${GRPCURL_VERSION#v}_linux_x86_64.tar.gz"; \
    tar -xzf /tmp/grpcurl.tar.gz -C /usr/local/bin grpcurl; \
    chmod +x /usr/local/bin/grpcurl; \
    apt-get purge -y curl xz-utils; apt-get autoremove -y; \
    rm -rf /tmp/*.tar.gz /var/lib/apt/lists/*; \
    /usr/local/bin/wal-g --version

COPY postgres-walg/agent.proto /etc/walg/agent.proto
COPY postgres-walg/walg-env.sh postgres-walg/walg-csk-key.sh \
     postgres-walg/walg-wal-fetch postgres-walg/walg-backup-loop.sh \
     postgres-walg/walg-restore.sh postgres-walg/docker-entrypoint-walg.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/walg-csk-key.sh /usr/local/bin/walg-wal-fetch \
    /usr/local/bin/walg-backup-loop.sh /usr/local/bin/walg-restore.sh \
    /usr/local/bin/docker-entrypoint-walg.sh

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

COPY pg-ha/pgha-common.sh pg-ha/pgha-etcd-entrypoint.sh pg-ha/pgha-patroni-entrypoint.sh \
     pg-ha/pgha-haproxy-entrypoint.sh pg-ha/pgha-backup-loop.sh pg-ha/pgha-post-init.sh \
     pg-ha/pgha-walg-bootstrap.sh pg-ha/walg-archive /usr/local/bin/
RUN chmod +x /usr/local/bin/pgha-*.sh /usr/local/bin/walg-archive

ENTRYPOINT ["docker-entrypoint-walg.sh"]
CMD ["postgres"]
