# Preserve the measured Pocket application and change only the read-only MCP
# connection wrapper. A Patroni switchover can leave HAProxy without a writable
# backend for several seconds; retrying these idempotent reads keeps one request
# inside the approved ten-second recovery window.
FROM ghcr.io/attestmesh/pocket-mcp@sha256:471a99c04123ae6461d5f177fc570d5457bf4411309c4bab3ad0b5e90f75d1e4

ARG VCS_REF
LABEL org.opencontainers.image.source="https://github.com/AttestMesh/AttestMesh" \
      org.opencontainers.image.revision="${VCS_REF}"

COPY --chown=pocket:pocket pocket-mcp-read-retry.py /tmp/pocket-mcp-read-retry.py
RUN python /tmp/pocket-mcp-read-retry.py && rm /tmp/pocket-mcp-read-retry.py

RUN python -m py_compile /app/server/mcp_server.py
