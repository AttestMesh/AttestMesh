# fugu-router

LiteLLM proxy image implementing `docs/research/fugu-subscription-pooling.md`: it pools
2–3 Sakana Fugu subscription keys as same-named deployments (`fugu-ultra`, `fugu`) with
PAYG deployments reachable only via fallbacks — drain-then-spill (§3.2), never hard failure.
`config.yaml` mirrors the §3.4 reference config: `usage-based-routing-v2` over redis-ha,
`cooldown_time: 60`, dated model pin `fugu-ultra-20260615`, and custom Fugu pricing in
`model_info` (LiteLLM's price table has no Sakana entries; standard-tier passthrough costs
are deliberately unset/untracked). `fugu_telemetry.py` is the §4.2 orchestration-token
callback: it captures unrecognized usage extras + `{account, agent_id, agent_role,
session_id}` into Langfuse trace metadata. The entrypoint derives `REDIS_PASSWORD` from the
Cluster Shared Key (HKDF label `attestmesh.redisha.auth.v1`, sidecar UDS) — no redis secret
ever leaves the CVM. Built by `.github/workflows/build-fugu-router.yml` → `ghcr.io/attestmesh/fugu-router`.
