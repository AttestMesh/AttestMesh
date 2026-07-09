#!/usr/bin/env bash
# AttestMesh deployment environment — Base mainnet (chain 8453), interoperating with
# the Phala dstack-base-prod5 KMS that the real CVM fleet boots against.
#
# SECURITY: this file contains NO secret values. Keys/tokens are read from ~/.teesql
# at source time (CLAUDE.md: no secrets/.env in source control). Addresses, the KMS
# root, and the compose hash below are public on-chain data.
#
# Usage:  source deploy/env.sh   (then run deploy routines)
set -euo pipefail
TEESQL="${TEESQL:-$HOME/.teesql}"
[ -d "$TEESQL" ] || { echo "ERROR: $TEESQL not found (need the teesql credentials)"; return 1 2>/dev/null || exit 1; }

_read() { tr -d '[:space:]' < "$1"; }

export CHAIN_ID=8453
export ALCHEMY_API_KEY="$(_read "$TEESQL/alchemy-api.key")"
export ALCHEMY_RPC_URL="https://base-mainnet.g.alchemy.com/v2/${ALCHEMY_API_KEY}"
# The Alchemy app has BASE_MAINNET disabled (403 on EVERY method), which breaks the DEPLOYER's own
# on-chain cast sends/reads — it bricked a live sandboxd roll (addComposeHash 403 while the old
# script sailed on to UpgradeApp onto an unallowlisted hash). Route the deployer's on-chain ops
# through a public node so rolls land.
#
# UPDATE 2026-07-08: Alchemy is fully dead now, and the CVM-sealed Alchemy RPC took console DOWN —
# on a full stop/start the sidecar couldn't READ its peers on-chain to re-join the mesh, so
# pg-provision hung and the app never started (~1.5h outage). So the CVM-sealed RPC is publicnode
# now too (interim). CAVEAT: publicnode is a plain RPC, NOT an AA bundler. Fine for an EXISTING node
# whose ed25519 key is already published (one sponsored op per node LIFETIME → boot is reads-only);
# a NEW node / fresh registration still needs a real bundler (Pimlico, or our own gateway-exposed
# Base node). Durable plan: our own Base node's RPC via the C3 gateway (box-admin/base-node).
export RPC_URL="${RPC_URL:-https://base-rpc.publicnode.com}"       # deployer host-side cast: nonce/send/call
export CVM_RPC_URL="${CVM_RPC_URL:-https://base-rpc.publicnode.com}"       # sealed into CVM sidecars (was Alchemy → dead → outage)
export BUNDLER_URL="${BUNDLER_URL:-$ALCHEMY_RPC_URL}"              # host-side AA bundler (unused by `update`; Alchemy dead)
export CVM_BUNDLER_URL="${CVM_BUNDLER_URL:-https://base-rpc.publicnode.com}"  # sealed; interim — NEW-node registration needs a real bundler
export GAS_POLICY_ID="$(_read "$TEESQL/alchemy-policy.id")"

export PRIVATE_KEY="$(_read "$TEESQL/global-deployer.key")"
export DEPLOYER_ADDR="$(_read "$TEESQL/global-deployer.address")"
export ORG_SAFE="${ORG_SAFE:-$DEPLOYER_ADDR}"       # deployer-owned during bring-up; transfer to the hub Safe later

# dstack KMS (Phala dstack-base-prod5): the real, validated root signer
# (= the corrected 0x52d3CF51… from dstackgres's monitor-kms-fix-root-signer Safe bundle)
# plus a real compose hash captured from a live CVM.
export KMS_ROOT="0x52d3cf51c8a37a2ccfc79bbb98c7810d7dd4ce51"
export COMPOSE_HASH="0x206a322e7ad0cfec0a2080822a4dfef10c4240a33e1dc553dd9e76b606b8ed0f"
export KMS_URL="https://kms.dstack-base-prod5.phala.network"
export KMS_CONTRACT="0x2f83172A49584C017F2B256F0FB2Dca14126Ba9C"

# Basescan key for `forge verify` (read from hub.env if present)
export BASESCAN_API_KEY="$(grep -oE '^BASESCAN_API_KEY=.*' "$TEESQL/hub.env" 2>/dev/null | cut -d= -f2- | tr -d '[:space:]' || true)"

# Phala Cloud API key for `phala deploy` (Track D5 / node bring-up). The non-interactive
# shell can't see an interactive `phala login`, so the deploy routines read it from a
# file. To unblock node deployment: write the key to $TEESQL/phala-cloud-api.key
# (or export PHALA_CLOUD_API_KEY before sourcing). Optional — only D5 needs it.
if [ -z "${PHALA_CLOUD_API_KEY:-}" ] && [ -f "$TEESQL/phala-cloud-api.key" ]; then
    export PHALA_CLOUD_API_KEY="$(_read "$TEESQL/phala-cloud-api.key")"
fi

export PATH="$HOME/.foundry/bin:$PATH"
set +euo pipefail 2>/dev/null || true
echo "env loaded: chain=$CHAIN_ID deployer=$DEPLOYER_ADDR org_safe=$ORG_SAFE kms_root=$KMS_ROOT"
