# fugu-router — LiteLLM proxy pooling Sakana Fugu subscription keys with PAYG fallback
# (docs/research/fugu-subscription-pooling.md §3). Redis auth is HKDF-derived in-CVM from
# the CLUSTER SHARED KEY (sidecar agent UDS) by the entrypoint — the image holds no secrets.
# Built/pushed by .github/workflows/build-fugu-router.yml.
FROM ghcr.io/berriai/litellm:v1.83.14-stable@sha256:c81eb79cd4333c6cfe374c0ec929110fd23f0ee5f7fd198855a6fbddc77b83ba  # pinned 2026-07-03

ARG GRPCURL_VERSION=v1.9.3
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl jq ca-certificates; \
    curl -fsSL -o /tmp/grpcurl.tar.gz "https://github.com/fullstorydev/grpcurl/releases/download/${GRPCURL_VERSION}/grpcurl_${GRPCURL_VERSION#v}_linux_x86_64.tar.gz"; \
    tar -xzf /tmp/grpcurl.tar.gz -C /usr/local/bin grpcurl; \
    chmod +x /usr/local/bin/grpcurl; \
    rm -f /tmp/grpcurl.tar.gz /var/lib/apt/lists/* || true; \
    grpcurl --version; jq --version

COPY agent.proto /etc/fugu-router/agent.proto
COPY config.yaml fugu_telemetry.py /app/
COPY fugu-router-entrypoint.sh /usr/local/bin/fugu-router-entrypoint.sh
RUN chmod +x /usr/local/bin/fugu-router-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/fugu-router-entrypoint.sh"]
