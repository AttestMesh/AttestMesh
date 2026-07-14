# Narrow runtime overlay: preserve the exact deployed image and replace only
# the pool factory that validates idle connections before checkout.
FROM ghcr.io/attestmesh/agent-session-mcp@sha256:5be40180a4761f78e8bc9f44fef38569714cd910117d853fea22db7a93c5fd0c

COPY server/db.py /app/server/db.py
