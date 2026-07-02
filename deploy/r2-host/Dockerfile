# r2host-s3gw — rclone `serve s3` over a `crypt` remote over Cloudflare R2, keyed by the CLUSTER
# SHARED KEY (fetched in-CVM from the sidecar over the agent UDS, HKDF'd in-process). Mesh clients
# PUT/GET plaintext; only ciphertext (content + filenames) ever reaches R2. See
# docs/specs/r2-host-node.md. Built/pushed by .github/workflows/build-r2host-s3gw.yml (and buildable
# on the box: docker build -t ghcr.io/attestmesh/r2host-s3gw:latest . && docker push …).
FROM rclone/rclone:1.74.2

ARG GRPCURL_VERSION=v1.9.3
RUN set -eux; \
    apk add --no-cache python3 ca-certificates curl; \
    curl -fsSL -o /tmp/grpcurl.tar.gz "https://github.com/fullstorydev/grpcurl/releases/download/${GRPCURL_VERSION}/grpcurl_${GRPCURL_VERSION#v}_linux_x86_64.tar.gz"; \
    tar -xzf /tmp/grpcurl.tar.gz -C /usr/local/bin grpcurl; \
    chmod +x /usr/local/bin/grpcurl; \
    apk del curl; \
    rm -f /tmp/grpcurl.tar.gz; \
    rclone version; grpcurl --version

COPY agent.proto /etc/r2host/agent.proto
COPY s3gw-entrypoint.sh /usr/local/bin/s3gw-entrypoint.sh
RUN chmod +x /usr/local/bin/s3gw-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/s3gw-entrypoint.sh"]
