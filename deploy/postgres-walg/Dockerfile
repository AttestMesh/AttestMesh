# postgres-walg — Postgres 16 + wal-g (encrypted base+WAL PITR backups → Cloudflare R2) + grpcurl (pull
# the cluster shared key from the sidecar over the agent UDS to derive the encryption key). A drop-in for
# the Matrix node's `postgres` service; backups are off unless BACKUP_ENABLED=true. Built/pushed by
# .github/workflows/build-postgres-walg.yml (and buildable on the box: docker build -t … && push).
FROM postgres:16-bookworm

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

COPY agent.proto /etc/walg/agent.proto
COPY walg-env.sh walg-csk-key.sh walg-archive walg-wal-fetch walg-backup-loop.sh walg-restore.sh docker-entrypoint-walg.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/walg-csk-key.sh /usr/local/bin/walg-archive /usr/local/bin/walg-wal-fetch \
    /usr/local/bin/walg-backup-loop.sh /usr/local/bin/walg-restore.sh /usr/local/bin/docker-entrypoint-walg.sh

ENTRYPOINT ["docker-entrypoint-walg.sh"]
CMD ["postgres"]
