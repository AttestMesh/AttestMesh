#!/usr/bin/env bash
# Standardized gas-sponsorship webhook routine (Track B) — deploy the Cloudflare Worker and
# ensure the custom-domain route points at it. The Alchemy Gas Manager policy calls the custom
# domain (gas-webhook.teesql.com); a route pointing at a different/older worker surfaces as
# `Unexpected webhook response code: 401` at the bundler (this is what blocked live registration).
# Idempotent. Worker secrets (RPC_URL, ALCHEMY_WEBHOOK_TOKEN) are set once via `wrangler secret
# put` and persist across deploys — not managed here.
#
#   deploy/webhook.sh deploy     # wrangler deploy + ensure route
#   deploy/webhook.sh route      # ensure route only
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
source "$HERE/lib.sh"

WORKER="attestmesh-gas-sponsorship-webhook"
CF_ZONE="a8381b9801a8cb87c9e505cd7362d5b4"   # teesql.com
ROUTE_HOST="gas-webhook.teesql.com"          # the custom domain the Alchemy policy calls
CF_CREDS="$HOME/.teesql/cloudflare-wrangler.toml"
[ -f "$CF_CREDS" ] || die "missing Cloudflare creds: $CF_CREDS"

_cf() { grep -E "^\s*$1\s*=" "$CF_CREDS" | head -1 | sed -E 's/.*=\s*//' | tr -d "\"' "; }
CF_TOKEN="$(_cf token)"; CF_ACCT="$(_cf account_id)"
[ -n "$CF_TOKEN" ] && [ -n "$CF_ACCT" ] || die "could not read token/account_id from $CF_CREDS"

deploy_worker() {
  run_step "webhook-deploy" bash -c "cd '$ROOT/services/gas-sponsorship-webhook' && \
    CLOUDFLARE_API_TOKEN='$CF_TOKEN' CLOUDFLARE_ACCOUNT_ID='$CF_ACCT' npx --yes wrangler deploy"
}

# Ensure gas-webhook.teesql.com/* routes to our worker (create or repoint as needed).
ensure_route() {
  local routes rid cur api="https://api.cloudflare.com/client/v4/zones/$CF_ZONE/workers/routes"
  routes=$(curl -s "$api" -H "Authorization: Bearer $CF_TOKEN")
  rid=$(echo "$routes" | python3 -c "import sys,json;[print(r['id']) for r in json.load(sys.stdin).get('result',[]) if r['pattern']=='$ROUTE_HOST/*']" 2>/dev/null)
  cur=$(echo "$routes" | python3 -c "import sys,json;[print(r['script']) for r in json.load(sys.stdin).get('result',[]) if r['pattern']=='$ROUTE_HOST/*']" 2>/dev/null)
  if [ -z "$rid" ]; then
    run_step "webhook-route-create" bash -c "curl -fsS -X POST '$api' -H 'Authorization: Bearer $CF_TOKEN' \
      -H 'content-type: application/json' -d '{\"pattern\":\"$ROUTE_HOST/*\",\"script\":\"$WORKER\"}'"
  elif [ "$cur" != "$WORKER" ]; then
    log "route $ROUTE_HOST/* currently → ${cur:-none}; repointing to $WORKER"
    run_step "webhook-route-repoint" bash -c "curl -fsS -X PUT '$api/$rid' -H 'Authorization: Bearer $CF_TOKEN' \
      -H 'content-type: application/json' -d '{\"pattern\":\"$ROUTE_HOST/*\",\"script\":\"$WORKER\"}'"
  else
    log "route $ROUTE_HOST/* already → $WORKER ✔"
  fi
  log "healthz via custom domain (expect 200): $(curl -s -o /dev/null -w '%{http_code}' "https://$ROUTE_HOST/healthz")"
}

case "${1:-deploy}" in
  deploy) deploy_worker; ensure_route ;;
  route)  ensure_route ;;
  *) die "usage: webhook.sh {deploy|route}" ;;
esac
