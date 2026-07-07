# fugu-router — LiteLLM proxy pooling Sakana Fugu subscription keys
# (docs/research/fugu-subscription-pooling.md §3). Redis auth is HKDF-derived in-CVM from
# the CLUSTER SHARED KEY (sidecar agent UDS) by the entrypoint — the image holds no secrets.
# Built/pushed by .github/workflows/build-fugu-router.yml.
# The litellm base image has no package manager, so tools come from a fetch stage
# as static binaries (grpcurl release tarball, jq static build).
# pinned 2026-07-03
FROM debian:bookworm-slim@sha256:60eac759739651111db372c07be67863818726f754804b8707c90979bda511df AS tools
ARG GRPCURL_VERSION=v1.9.3
ARG JQ_VERSION=1.7.1
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl ca-certificates; \
    curl -fsSL -o /tmp/grpcurl.tar.gz "https://github.com/fullstorydev/grpcurl/releases/download/${GRPCURL_VERSION}/grpcurl_${GRPCURL_VERSION#v}_linux_x86_64.tar.gz"; \
    tar -xzf /tmp/grpcurl.tar.gz -C /usr/local/bin grpcurl; \
    curl -fsSL -o /usr/local/bin/jq "https://github.com/jqlang/jq/releases/download/jq-${JQ_VERSION}/jq-linux-amd64"; \
    chmod +x /usr/local/bin/grpcurl /usr/local/bin/jq; \
    /usr/local/bin/grpcurl --version; /usr/local/bin/jq --version

# pinned 2026-07-03
FROM ghcr.io/berriai/litellm:v1.83.14-stable@sha256:c81eb79cd4333c6cfe374c0ec929110fd23f0ee5f7fd198855a6fbddc77b83ba

COPY --from=tools /usr/local/bin/grpcurl /usr/local/bin/jq /usr/local/bin/
# langfuse tracer package for the success_callback. The base image has no `pip` on PATH,
# so go through the interpreter (ensurepip fallback); no-op if the base already bundles it.
RUN python3 -m pip install --no-cache-dir "langfuse<3" "psycopg[binary]>=3.1" "httpx>=0.27" "fastapi>=0.110" "uvicorn[standard]>=0.27" || \
    (python3 -m ensurepip --upgrade && python3 -m pip install --no-cache-dir "langfuse<3" "psycopg[binary]>=3.1" "httpx>=0.27" "fastapi>=0.110" "uvicorn[standard]>=0.27"); \
    python3 -c "import langfuse, psycopg, httpx, fastapi, uvicorn; print('fugu deps ok')"

COPY agent.proto /etc/fugu-router/agent.proto
COPY config.yaml fugu_telemetry.py fugu_credit.py fugu_session_router.py /app/
COPY fugu-router-entrypoint.sh /usr/local/bin/fugu-router-entrypoint.sh
RUN chmod 755 /usr/local/bin/fugu-router-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/fugu-router-entrypoint.sh"]
