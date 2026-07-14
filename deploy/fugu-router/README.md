# fugu-router

LiteLLM proxy image implementing `docs/research/fugu-subscription-pooling.md`: it serves
account-specific upstream model names (`fugu-ultra-sub-N`, `fugu-sub-N`) while the
front-door `fugu_session_router.py` exposes the stable public names (`fugu-ultra`,
`fugu`). It also exposes direct provider models (`glm-5.2`,
`qwen/qwen3-embedding-8b`, `openai/gpt-oss-120b`, `grok-4.5`, `grok-4.3`,
`grok-imagine-image-quality`, `grok-imagine-image`) without Fugu subscription
placement. New Fugu sessions are assigned to the lowest-utilized subscription by
5-hour, then weekly, then monthly usage; quota/rate-limit errors retry another
subscription. PAYG is intentionally absent from the serving path.

When a Sakana Fugu upstream returns HTTP 451/content-policy for a chat request,
the front door retries once through the RedPill `glm-5.2` model. Successful
fallback responses carry `x-fugu-fallback: content-policy` headers, response
metadata describing the original Fugu block, and a visible notice prepended to
normal chat text responses.

Direct model prices are configured in LiteLLM and mirrored in the ledger estimate
path: `glm-5.2` is $1.40/M input, $4.40/M output, $0.70/M cache read;
`qwen/qwen3-embedding-8b` is $0.01/M input and $0.00/M output;
`openai/gpt-oss-120b` is $0.15/M input and $0.60/M output/reasoning; `grok-4.5` is
$2.00/M input, $6.00/M output, $0.50/M cache read; `grok-4.3` is $1.25/M input,
$2.50/M output, $0.20/M cache read. xAI image generations report exact spend
as `usage.cost_in_usd_ticks`; the router also pins 1K output estimates for
`grok-imagine-image-quality` ($0.05/image) and `grok-imagine-image`
($0.02/image).

`fugu_credit.py` initializes the Postgres ledger tables and is registered as the
LiteLLM callback. It captures the full raw usage/token metadata, derived cost units,
session/account routing metadata, and sanitized LiteLLM standard logging payloads.
`/fugu/dashboard` on the mesh LiteLLM endpoint serves the read-only token dashboard.

The entrypoint derives `REDIS_PASSWORD` from the Cluster Shared Key (HKDF label
`attestmesh.redisha.auth.v1`, sidecar UDS) — no redis secret ever leaves the CVM.
Built by `.github/workflows/build-fugu-router.yml` → `ghcr.io/attestmesh/fugu-router`.
