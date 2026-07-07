# fugu-router

LiteLLM proxy image implementing `docs/research/fugu-subscription-pooling.md`: it serves
account-specific upstream model names (`fugu-ultra-sub-N`, `fugu-sub-N`) while the
front-door `fugu_session_router.py` exposes the stable public names (`fugu-ultra`,
`fugu`). New sessions are assigned to the lowest-utilized subscription by 5-hour,
then weekly, then monthly usage; quota/rate-limit errors retry another subscription.
PAYG is intentionally absent from the serving path.

`fugu_credit.py` initializes the Postgres ledger tables and is registered as the
LiteLLM callback. It captures the full raw usage/token metadata, derived cost units,
session/account routing metadata, and sanitized LiteLLM standard logging payloads.
`/fugu/dashboard` on the mesh LiteLLM endpoint serves the read-only token dashboard.

The entrypoint derives `REDIS_PASSWORD` from the Cluster Shared Key (HKDF label
`attestmesh.redisha.auth.v1`, sidecar UDS) — no redis secret ever leaves the CVM.
Built by `.github/workflows/build-fugu-router.yml` → `ghcr.io/attestmesh/fugu-router`.
