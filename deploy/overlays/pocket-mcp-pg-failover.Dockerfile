# Narrow runtime overlay: preserve the exact deployed image and replace only
# the poller loop that reconnects and retries an idempotent cycle after failover.
FROM ghcr.io/attestmesh/pocket-mcp@sha256:e9285bc6c2c0b180a3211d41572683bba649d6c6bced4e6ae0645212033bf24e

COPY poller/loop.py /app/poller/loop.py
