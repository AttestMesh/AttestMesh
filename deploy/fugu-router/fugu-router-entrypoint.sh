#!/bin/sh
# fugu-router entrypoint: derive the redis-ha client password from the CLUSTER SHARED KEY
# (sidecar agent UDS, HKDF label attestmesh.redisha.auth.v1 — same recipe as
# deploy/pg-ha/pgha-common.sh csk_derive), prune model_list deployments whose api_key env
# var is absent (SAKANA_SUB_3_KEY is optional), then exec litellm.
# Progress/errors self-report to the status volume — the TEE blocks container logs.
set -u

SOCK="${AGENT_GRPC_SOCKET:-/var/run/attestmesh/agent.sock}"
SVC="attestmesh.agent.v1.Agent"
# unix:// SCHEME + import-path (grpcurl 1.9.x lessons, see deploy/r2-host/s3gw-entrypoint.sh).
GRPC="grpcurl -plaintext -import-path /etc/fugu-router -proto agent.proto"
ADDR="unix://$SOCK"
STAT="${FUGU_STATUS_FILE:-/fugu-status/state}"; mkdir -p "$(dirname "$STAT")" 2>/dev/null || true
_st() { echo "$(date -u +%FT%TZ) fugu-router: $*" >> "$STAT" 2>/dev/null || true; }
# Observable failure, no crash-loop storm: report, linger, let restart policy retry.
_die() { _st "ERROR: $*"; sleep 30; exit 1; }

for v in SAKANA_API_BASE SAKANA_SUB_1_KEY SAKANA_SUB_2_KEY SAKANA_PAYG_KEY \
         LITELLM_MASTER_KEY LITELLM_SALT_KEY DATABASE_URL; do
  eval "val=\${$v:-}"
  [ -n "$val" ] || _die "missing required env $v"
done

_st "boot: waiting for sidecar CSK (sock=$SOCK)"
i=0
while :; do
  resp="$($GRPC -d '{}' "$ADDR" "$SVC/GetMeshStatus" 2>&1)"
  acq="$(printf '%s' "$resp" | jq -r 'if .cskAcquired or .csk_acquired then "yes" else "no" end' 2>/dev/null || echo no)"
  [ "$acq" = "yes" ] && break
  i=$((i + 1))
  [ $((i % 6)) -eq 0 ] && _st "waiting for CSK ($((i * 5))s); GetMeshStatus -> $(printf '%s' "$resp" | tr '\n' ' ' | head -c 200)"
  sleep 5
done

_st "CSK acquired; deriving redis-ha password"
csk="$($GRPC -d '{}' "$ADDR" "$SVC/GetClusterSharedKey" 2>/dev/null | jq -r '.key // empty' 2>/dev/null)"
[ -n "$csk" ] || _die "GetClusterSharedKey returned no key"

# CSK via argv (heredoc owns stdin). HKDF-Extract zero salt + HKDF-Expand versioned label,
# identical to pgha-common.sh csk_derive — redis-ha derives the same password server-side.
REDIS_PASSWORD="$(python3 - "$csk" "attestmesh.redisha.auth.v1" <<'PY'
import sys, base64, hashlib, hmac
csk = base64.b64decode(sys.argv[1])
assert len(csk) == 32, f"CSK length {len(csk)} != 32"
prk = hmac.new(b"\x00" * 32, csk, hashlib.sha256).digest()
okm = hmac.new(prk, sys.argv[2].encode() + b"\x01", hashlib.sha256).digest()
print(okm.hex())
PY
)" || _die "redis password derivation failed"
[ -n "$REDIS_PASSWORD" ] || _die "derived empty redis password"
export REDIS_PASSWORD
unset csk

# Prune model_list entries whose `api_key: os.environ/<VAR>` var is unset/empty
# (the third subscription key may not exist yet); litellm hard-fails on missing env refs.
CONF=/app/config.runtime.yaml
python3 - /app/config.yaml "$CONF" <<'PY' || exit 1
import os, sys, yaml
src, dst = sys.argv[1], sys.argv[2]
with open(src) as f:
    cfg = yaml.safe_load(f)
kept, pruned = [], []
for d in cfg.get("model_list", []):
    key = (d.get("litellm_params") or {}).get("api_key", "")
    if isinstance(key, str) and key.startswith("os.environ/") and not os.environ.get(key.split("/", 1)[1]):
        pruned.append(f'{d.get("model_name")}<-{key}')
    else:
        kept.append(d)
cfg["model_list"] = kept
with open(dst, "w") as f:
    yaml.safe_dump(cfg, f, sort_keys=False)
print(f"fugu-router: config pruned={pruned or 'none'} kept={len(kept)} deployments", flush=True)
PY
[ -s "$CONF" ] || _die "runtime config generation failed"

export PYTHONPATH="/app${PYTHONPATH:+:$PYTHONPATH}"  # fugu_telemetry importable by litellm
_st "starting litellm proxy on :4000"
cd /app || _die "cd /app failed"
exec litellm --config "$CONF" --port 4000
