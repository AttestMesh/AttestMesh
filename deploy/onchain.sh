#!/usr/bin/env bash
# Standardized on-chain deploy routine (Track A) — ordered, idempotent, logged.
# Subcommands run the specific-ordering sequence; smithers (deploy/workflows/deploy.tsx)
# sequences these as durable steps, or run directly:
#   source deploy/env.sh && deploy/onchain.sh all
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
source "$HERE/lib.sh"
: "${RPC_URL:?source deploy/env.sh first}"; require RPC_URL CHAIN_ID KMS_ROOT

RECEIPT="$ROOT/contracts/script/deployments/${CHAIN_ID}.json"

# Reviewed Base Stage-A signer-cluster trust anchors. These are intentionally
# not operator-overridable: changing any one is a new security review.
readonly INDEXER_STAGE_A_CHAIN_ID=8453
readonly INDEXER_STAGE_A_SAFE=0xD97b5e3Fc685e29825d76b4F90B7B3ACAE7D66f0
readonly INDEXER_STAGE_A_SAFE_PROXY_CODEHASH=0xd7d408ebcd99b2b70be43e20253d6d92a8ea8fab29bd3be7f55b10032331fb4c
readonly INDEXER_STAGE_A_SAFE_SINGLETON=0x41675C099F32341bf84BFc5382aF534df5C7461a
readonly INDEXER_STAGE_A_SAFE_SINGLETON_CODEHASH=0x1fe2df852ba3299d6534ef416eefa406e56ced995bca886ab7a553e6d0c5e1c4
readonly INDEXER_STAGE_A_SAFE_OWNER_0=0x37f5761218D30E90CeeF54CcA1b71208115Acc4F
readonly INDEXER_STAGE_A_SAFE_OWNER_1=0x890e54A378b07483a6e04AD3D2264609bD3fd07E
readonly INDEXER_STAGE_A_SAFE_GUARD_SLOT=0x4a204f620c8c5ccdca3fd54d003badd85ba500436a431f0cbda4f558c93c34c8
readonly INDEXER_STAGE_A_SAFE_FALLBACK_SLOT=0x6c9a6c4a39284e37ed1cf53d337577d14212a4870fb976a4366c693b939918d5
readonly INDEXER_STAGE_A_SAFE_FALLBACK_HANDLER=0xfd0732Dc9E303f09fCEf3a7388Ad10A83459Ec99
readonly INDEXER_STAGE_A_SAFE_FALLBACK_HANDLER_CODEHASH=0x7c6007a5d711cea8dfd5d91f5940ec29c7f200fe511eb1fc1397b367af3c42f9
readonly INDEXER_STAGE_A_CLUSTER_FACTORY=0xf6E85fD138E3208d3AAE63ce4E2A33f20e82b9fb
readonly INDEXER_STAGE_A_CLUSTER_FACTORY_CODEHASH=0x9cd0a5b30b384cc625105713ef346fc086d723c6801e3a0cb07e47fc91e4e71b
readonly INDEXER_STAGE_A_MEMBER_FACTORY=0xFf9f438EFdAa197f4ae59D1d711C4f09640b8417
readonly INDEXER_STAGE_A_MEMBER_FACTORY_CODEHASH=0x46b41e453a4437bba466f4491c38c33f99d92c0b129756d45d269d35241e4718
readonly INDEXER_STAGE_A_FACTORY_OWNER=0x60b174704AdAf2b0BF87B426B364D6EbD81818E1
readonly INDEXER_STAGE_A_DIAMOND_INIT=0xe3C9CE59b6c164c7b4c81f686C876EB85198cFC3
readonly INDEXER_STAGE_A_ATTEST_FACET=0x10532164ca3BdaCf1dAd13Fb534262DEc5aAA9AA
readonly INDEXER_STAGE_A_MESSAGE_FACET=0x0F67cd8c1D8A2F71d2bb8091B2eb166E6b6bB564
# The immutable factory predates the Ed25519 heartbeat-key NetworkFacet cut.
readonly INDEXER_STAGE_A_FACTORY_NETWORK_FACET=0x6AB2b7D506c85C7A9eA22f2ECcE0cc9191006159
readonly INDEXER_STAGE_A_FACTORY_DSTACK_FACET=0xe9d463974c6E833DC38794d7f2DC5AB692352968
readonly INDEXER_STAGE_A_FACTORY_MEMBER_IMPL=0xd05223da04B4E73AC02ECA9638D490f45f765843
readonly INDEXER_STAGE_A_DIAMOND_INIT_CODEHASH=0x0f11d9b0fa554b9b78051e49d7f92c440808e0eea23a4d30c7f925c4f44f842c
readonly INDEXER_STAGE_A_ATTEST_FACET_CODEHASH=0x94ec9771e40ecb2b817cd33b56ea1a442037911a4a04ed6a92cbef7b5abd6223
readonly INDEXER_STAGE_A_MESSAGE_FACET_CODEHASH=0x4328ba0202b88caceca4ed02eff3052c775f89f3620bba3fc90654f3c51256c2
readonly INDEXER_STAGE_A_FACTORY_NETWORK_FACET_CODEHASH=0x0e63b71315c355eb05547aafc898ca82618abf1fb5614da782b7db16690cfc55
readonly INDEXER_STAGE_A_FACTORY_DSTACK_FACET_CODEHASH=0xa8d3883794943b307f491bc6b8ff2cd7f3817f17b76457b6dc48eceba36d973e
readonly INDEXER_STAGE_A_FACTORY_MEMBER_IMPL_CODEHASH=0xf7228096d52f508780b4a03e0632340a1ba139b104d392ddf47bb264c3be81f5
readonly INDEXER_STAGE_A_CREATE2_DEPLOYER=0x4e59b44847b379578588920cA78FbF26c0B4956C
readonly INDEXER_STAGE_A_CREATE2_DEPLOYER_CODEHASH=0x2fa86add0aed31f33a762c9d88e807c475bd51d0f52bd0955754b2608f7e4989
readonly INDEXER_STAGE_A_DSTACK_SALT=0xcef5a875b56ff1fd96fd03ab048c551f1e40e478d13fc040fcd374a671de86f3
readonly INDEXER_STAGE_A_MEMBER_SALT=0xac3d0f1737eb55ea6a051b5a5d36d8a37dd42e0408cd9c8d415905623c7f9074
readonly INDEXER_STAGE_A_DSTACK_INIT_CODEHASH=0x4b022b0b0542765fd75e895713a3aa621e5730c4ddd0172598177235adf65662
readonly INDEXER_STAGE_A_MEMBER_INIT_CODEHASH=0x667dae361d485f9cb65c74af1548c9e5054c89eb2669156dbbd3da7f24fd0ef1
readonly INDEXER_STAGE_A_DSTACK_RUNTIME_CODEHASH=0xf19b470a052d0b3ab62e80ce94dd828aae33d554e507c01dab6ef7e779f77c79
# Address-bound because ClusterMember inherits UUPSUpgradeable's immutable __self.
readonly INDEXER_STAGE_A_MEMBER_RUNTIME_CODEHASH=0xf5820491e5bb675a2e410b583652f4e651b98a070c5900ed1d93ac3a87d263ff
readonly INDEXER_STAGE_A_DSTACK_FACET=0xE0c2140dD2a163198b3DDdB78D959b115Cd43ff1
readonly INDEXER_STAGE_A_MEMBER_IMPL=0x3354510A01fAb92359dBD4204CcEc91dA9BC1E06
readonly -a INDEXER_STAGE_A_DSTACK_SELECTORS=(
  0xdfc77223 0x67b3f22c 0x2a819728 0x1d266200 0x7c4beeb8
  0x6e4c7422 0x2f6622e5 0xbf8b211b 0x3440a16a 0x0aa58c83
  0x54fd4d50 0x2514ce2d 0x7e02756a 0x12c604da 0x537d491c
  0x1e079198 0x64e985f8 0x4bc7cbb7 0x875f31fb
)
readonly -a INDEXER_STAGE_A_SELF_SELECTORS=(
  0x2c408059 0x91423765 0x1f931c1c 0x7a0ed627 0xadfca15e 0x52ef6b2c
  0xcdffacc6 0x01ffc9a7 0x8da5cb5b 0x8ab5150a 0xf2fde38b 0x79ba5097
)
readonly -a INDEXER_STAGE_A_ATTEST_SELECTORS=(
  0x7f989b8d 0x63a30b72 0x3b4c9891 0x87dc7ae5 0x7918228d
  0xb6afd2ca 0x11aee380 0x08c75c4f 0x0441484e 0x9068639e
  0xbb671732 0x3eeb8ee8 0x38c640e9 0xd604bed7 0x35901459
  0x8af487aa 0x319b215c 0xd9c8dbfe 0x29ca97eb
)
readonly -a INDEXER_STAGE_A_MESSAGE_SELECTORS=(0x84076765)
readonly -a INDEXER_STAGE_A_FACTORY_NETWORK_SELECTORS=(
  0x4979ff72 0x06815bc9 0x4fda654e
)

_same_address() { [ "${1,,}" = "${2,,}" ]; }

_canonicalize_indexer_device_ids() {
  local canonical
  canonical=$(printf '%s\n' "$INDEXER_DEVICE_IDS_JSON" | jq -ce '
    . as $ids
    | if type == "array"
        and length > 0
        and all(.[];
          type == "string"
          and test("^0x[0-9a-fA-F]{64}$")
          and ascii_downcase != ("0x" + ("0" * 64)))
        and (($ids | map(ascii_downcase) | unique | length) == ($ids | length))
      then map(ascii_downcase) | sort
      else error("invalid Indexer device-id set")
      end
  ') || die "INDEXER_DEVICE_IDS_JSON must be a non-empty unique nonzero bytes32 JSON array"
  INDEXER_DEVICE_IDS_JSON="$canonical"
  export INDEXER_DEVICE_IDS_JSON
}

_require_indexer_stage_a_chain() {
  local actual_chain
  [ "$CHAIN_ID" = "$INDEXER_STAGE_A_CHAIN_ID" ] \
    || die "Stage-A signer clusters are reviewed only for Base chain $INDEXER_STAGE_A_CHAIN_ID"
  actual_chain=$(cast chain-id --rpc-url "$RPC_URL") \
    || die "could not read RPC chain id"
  [ "$actual_chain" = "$INDEXER_STAGE_A_CHAIN_ID" ] \
    || die "RPC chain mismatch ($actual_chain != reviewed $INDEXER_STAGE_A_CHAIN_ID)"
}

_expect_codehash() {
  local label="$1" account="$2" expected="$3" actual
  actual=$(cast codehash "$account" --rpc-url "$RPC_URL") \
    || die "could not read $label runtime code hash: $account"
  [ "${actual,,}" = "${expected,,}" ] \
    || die "$label runtime code hash mismatch ($actual != $expected)"
}

_expect_address_call() {
  local label="$1" target="$2" signature="$3" expected="$4" actual
  actual=$(cast call "$target" "$signature" --rpc-url "$RPC_URL") \
    || die "could not read $label"
  _same_address "$actual" "$expected" || die "$label mismatch ($actual != $expected)"
}

_validate_indexer_stage_a_safe() {
  local safe="$1" master version threshold owners modules guard handler handler_word
  _same_address "$safe" "$INDEXER_STAGE_A_SAFE" \
    || die "INDEXER_CLUSTER_OWNER must be the reviewed Safe $INDEXER_STAGE_A_SAFE"
  _expect_codehash "approved Safe proxy" "$safe" "$INDEXER_STAGE_A_SAFE_PROXY_CODEHASH"
  _expect_codehash "approved Safe singleton" "$INDEXER_STAGE_A_SAFE_SINGLETON" \
    "$INDEXER_STAGE_A_SAFE_SINGLETON_CODEHASH"
  master=$(cast call "$safe" 'masterCopy()(address)' --rpc-url "$RPC_URL") \
    || die "could not read approved Safe masterCopy"
  _same_address "$master" "$INDEXER_STAGE_A_SAFE_SINGLETON" \
    || die "approved Safe masterCopy mismatch ($master != $INDEXER_STAGE_A_SAFE_SINGLETON)"
  version=$(cast call "$safe" 'VERSION()(string)' --rpc-url "$RPC_URL" --json \
    | jq -er 'if type == "array" and length == 1 then .[0] else empty end') \
    || die "could not validate approved Safe version"
  [ "$version" = 1.4.1 ] || die "approved Safe version mismatch ($version != 1.4.1)"
  threshold=$(cast call "$safe" 'getThreshold()(uint256)' --rpc-url "$RPC_URL") \
    || die "could not read approved Safe threshold"
  [ "$threshold" = 1 ] || die "approved Safe threshold changed ($threshold != 1)"
  owners=$(cast call "$safe" 'getOwners()(address[])' --rpc-url "$RPC_URL" --json) \
    || die "could not read approved Safe owners"
  printf '%s\n' "$owners" | jq -e \
    --arg owner0 "${INDEXER_STAGE_A_SAFE_OWNER_0,,}" \
    --arg owner1 "${INDEXER_STAGE_A_SAFE_OWNER_1,,}" '
      type == "array" and length == 1
      and (.[0] | type == "array" and length == 2)
      and ((.[0] | map(ascii_downcase) | sort) == ([$owner0, $owner1] | sort))
    ' >/dev/null || die "approved Safe owner set changed; review before continuing"
  modules=$(cast call "$safe" \
    'getModulesPaginated(address,uint256)(address[],address)' \
    0x0000000000000000000000000000000000000001 100 --rpc-url "$RPC_URL" --json) \
    || die "could not read approved Safe modules"
  printf '%s\n' "$modules" | jq -e '
    type == "array" and length == 2
    and (.[0] | type == "array" and length == 0)
    and ((.[1] | ascii_downcase) == "0x0000000000000000000000000000000000000001")
  ' >/dev/null || die "approved Safe module execution surface changed"
  guard=$(cast call "$safe" 'getStorageAt(uint256,uint256)(bytes)' \
    "$INDEXER_STAGE_A_SAFE_GUARD_SLOT" 1 --rpc-url "$RPC_URL") \
    || die "could not read approved Safe guard storage"
  [ "${guard,,}" = "0x$(printf '0%.0s' {1..64})" ] \
    || die "approved Safe guard changed ($guard != zero)"
  handler=$(cast call "$safe" 'getStorageAt(uint256,uint256)(bytes)' \
    "$INDEXER_STAGE_A_SAFE_FALLBACK_SLOT" 1 --rpc-url "$RPC_URL") \
    || die "could not read approved Safe fallback-handler storage"
  handler_word="0x$(printf '0%.0s' {1..24})${INDEXER_STAGE_A_SAFE_FALLBACK_HANDLER:2}"
  [ "${handler,,}" = "${handler_word,,}" ] \
    || die "approved Safe fallback handler changed"
  _expect_codehash "approved Safe fallback handler" \
    "$INDEXER_STAGE_A_SAFE_FALLBACK_HANDLER" \
    "$INDEXER_STAGE_A_SAFE_FALLBACK_HANDLER_CODEHASH"
}

_validate_indexer_stage_a_factories() {
  [ -f "$RECEIPT" ] || die "infrastructure receipt not found: $RECEIPT"
  local receipt_cluster receipt_member
  receipt_cluster=$(jq -er .clusterDiamondFactory "$RECEIPT") \
    || die "receipt has no clusterDiamondFactory"
  receipt_member=$(jq -er .clusterMemberFactory "$RECEIPT") \
    || die "receipt has no clusterMemberFactory"
  _same_address "$receipt_cluster" "$INDEXER_STAGE_A_CLUSTER_FACTORY" \
    || die "receipt cluster factory is not the reviewed Base deployment"
  _same_address "$receipt_member" "$INDEXER_STAGE_A_MEMBER_FACTORY" \
    || die "receipt member factory is not the reviewed Base deployment"

  _expect_codehash "canonical ClusterDiamondFactory" "$INDEXER_STAGE_A_CLUSTER_FACTORY" \
    "$INDEXER_STAGE_A_CLUSTER_FACTORY_CODEHASH"
  _expect_codehash "canonical ClusterMemberFactory" "$INDEXER_STAGE_A_MEMBER_FACTORY" \
    "$INDEXER_STAGE_A_MEMBER_FACTORY_CODEHASH"
  _expect_codehash "canonical DiamondInit" "$INDEXER_STAGE_A_DIAMOND_INIT" \
    "$INDEXER_STAGE_A_DIAMOND_INIT_CODEHASH"
  _expect_codehash "canonical AttestFacet" "$INDEXER_STAGE_A_ATTEST_FACET" \
    "$INDEXER_STAGE_A_ATTEST_FACET_CODEHASH"
  _expect_codehash "canonical MessageFacet" "$INDEXER_STAGE_A_MESSAGE_FACET" \
    "$INDEXER_STAGE_A_MESSAGE_FACET_CODEHASH"
  _expect_codehash "canonical factory NetworkFacet" "$INDEXER_STAGE_A_FACTORY_NETWORK_FACET" \
    "$INDEXER_STAGE_A_FACTORY_NETWORK_FACET_CODEHASH"
  _expect_codehash "canonical factory DstackFacet" "$INDEXER_STAGE_A_FACTORY_DSTACK_FACET" \
    "$INDEXER_STAGE_A_FACTORY_DSTACK_FACET_CODEHASH"
  _expect_codehash "canonical factory ClusterMember implementation" \
    "$INDEXER_STAGE_A_FACTORY_MEMBER_IMPL" \
    "$INDEXER_STAGE_A_FACTORY_MEMBER_IMPL_CODEHASH"
  _expect_address_call "ClusterDiamondFactory.factoryOwner" \
    "$INDEXER_STAGE_A_CLUSTER_FACTORY" 'factoryOwner()(address)' "$INDEXER_STAGE_A_FACTORY_OWNER"
  _expect_address_call "ClusterDiamondFactory.diamondInitImpl" \
    "$INDEXER_STAGE_A_CLUSTER_FACTORY" 'diamondInitImpl()(address)' "$INDEXER_STAGE_A_DIAMOND_INIT"
  _expect_address_call "ClusterDiamondFactory.attestFacet" \
    "$INDEXER_STAGE_A_CLUSTER_FACTORY" 'attestFacet()(address)' "$INDEXER_STAGE_A_ATTEST_FACET"
  _expect_address_call "ClusterDiamondFactory.messageFacet" \
    "$INDEXER_STAGE_A_CLUSTER_FACTORY" 'messageFacet()(address)' "$INDEXER_STAGE_A_MESSAGE_FACET"
  _expect_address_call "ClusterDiamondFactory.networkFacet" \
    "$INDEXER_STAGE_A_CLUSTER_FACTORY" 'networkFacet()(address)' \
    "$INDEXER_STAGE_A_FACTORY_NETWORK_FACET"
  _expect_address_call "ClusterDiamondFactory.dstackFacet" \
    "$INDEXER_STAGE_A_CLUSTER_FACTORY" 'dstackFacet()(address)' \
    "$INDEXER_STAGE_A_FACTORY_DSTACK_FACET"
  _expect_address_call "ClusterMemberFactory.factoryOwner" \
    "$INDEXER_STAGE_A_MEMBER_FACTORY" 'factoryOwner()(address)' "$INDEXER_STAGE_A_FACTORY_OWNER"
  _expect_address_call "ClusterMemberFactory.implementation" \
    "$INDEXER_STAGE_A_MEMBER_FACTORY" 'implementation()(address)' \
    "$INDEXER_STAGE_A_FACTORY_MEMBER_IMPL"
}

_verify_canonical_cluster() {
  local cluster="$1" deployed
  deployed=$(cast call "$INDEXER_STAGE_A_CLUSTER_FACTORY" \
    'deployedClusters(address)(bool)' "$cluster" --rpc-url "$RPC_URL") \
    || die "could not verify canonical cluster provenance"
  [ "$deployed" = true ] \
    || die "cluster was not deployed by the reviewed ClusterDiamondFactory: $cluster"
}

_verify_dstack_topology() {
  local cluster="$1" expected="$2" selector actual
  for selector in "${INDEXER_STAGE_A_DSTACK_SELECTORS[@]}"; do
    actual=$(cast call "$cluster" 'facetAddress(bytes4)(address)' "$selector" \
      --rpc-url "$RPC_URL") || die "could not resolve dstack selector $selector"
    _same_address "$actual" "$expected" \
      || die "dstack selector $selector resolves to $actual, expected $expected"
  done
}

_selector_array_json() {
  printf '%s\n' "$@" | jq -Rsc 'split("\n")[:-1] | map(ascii_downcase) | sort'
}

# Require the complete five-facet, 54-selector set emitted by the immutable
# Stage-A factory. Target and selector order are deliberately ignored.
_verify_exact_cluster_topology() {
  local cluster="$1" expected_dstack="$2" actual self attest message network dstack
  actual=$(cast call "$cluster" 'facets()((address,bytes4[])[])' \
    --rpc-url "$RPC_URL" --json) || die "could not read complete cluster facet topology"
  self=$(_selector_array_json "${INDEXER_STAGE_A_SELF_SELECTORS[@]}") || die "could not encode self selectors"
  attest=$(_selector_array_json "${INDEXER_STAGE_A_ATTEST_SELECTORS[@]}") || die "could not encode attest selectors"
  message=$(_selector_array_json "${INDEXER_STAGE_A_MESSAGE_SELECTORS[@]}") || die "could not encode message selectors"
  network=$(_selector_array_json "${INDEXER_STAGE_A_FACTORY_NETWORK_SELECTORS[@]}") || die "could not encode network selectors"
  dstack=$(_selector_array_json "${INDEXER_STAGE_A_DSTACK_SELECTORS[@]}") || die "could not encode dstack selectors"
  printf '%s\n' "$actual" | jq -e \
    --arg selfTarget "${cluster,,}" \
    --arg attestTarget "${INDEXER_STAGE_A_ATTEST_FACET,,}" \
    --arg messageTarget "${INDEXER_STAGE_A_MESSAGE_FACET,,}" \
    --arg networkTarget "${INDEXER_STAGE_A_FACTORY_NETWORK_FACET,,}" \
    --arg dstackTarget "${expected_dstack,,}" \
    --argjson self "$self" --argjson attest "$attest" \
    --argjson message "$message" --argjson network "$network" --argjson dstack "$dstack" '
      def normalized:
        {target: (.[0] | ascii_downcase), selectors: (.[1] | map(ascii_downcase) | sort)};
      type == "array" and length == 1
      and (.[0] | type == "array" and length == 5)
      and ((.[0] | map(normalized) | sort_by(.target)) ==
        ([
          {target:$selfTarget, selectors:$self},
          {target:$attestTarget, selectors:$attest},
          {target:$messageTarget, selectors:$message},
          {target:$networkTarget, selectors:$network},
          {target:$dstackTarget, selectors:$dstack}
        ] | sort_by(.target)))
    ' >/dev/null || die "cluster facet topology is not the exact reviewed five-facet/54-selector set"
}

_verify_fresh_generation() {
  local cluster="$1" members commitment
  members=$(cast call "$cluster" 'memberCount()(uint256)' --rpc-url "$RPC_URL") \
    || die "could not read Indexer cluster member count"
  commitment=$(cast call "$cluster" 'cskCommitment()(bytes32)' --rpc-url "$RPC_URL") \
    || die "could not read Indexer cluster CSK commitment"
  [ "$members" = 0 ] || die "Indexer cluster is not a fresh generation (memberCount=$members)"
  [ "${commitment,,}" = "0x$(printf '0%.0s' {1..64})" ] \
    || die "Indexer cluster already has a CSK commitment"
}

_reviewed_patha_calldata() {
  local facet="$1" selectors
  local IFS=,
  selectors="${INDEXER_STAGE_A_DSTACK_SELECTORS[*]}"
  cast calldata 'diamondCut((address,uint8,bytes4[])[],address,bytes)' \
    "[($facet,1,[$selectors])]" 0x0000000000000000000000000000000000000000 0x
}

_predict_indexer_cluster() {
  local salt="$1" devices tuple
  devices=$(printf '%s\n' "$INDEXER_DEVICE_IDS_JSON" | jq -r 'join(",")') \
    || die "could not encode INDEXER_DEVICE_IDS_JSON for prediction"
  tuple="($INDEXER_STAGE_A_SAFE,$KMS_ROOT,[$INDEXER_COMPOSE_HASH],[$devices],false,true,169279488,16,$INDEXER_STAGE_A_MEMBER_FACTORY)"
  cast call "$INDEXER_STAGE_A_CLUSTER_FACTORY" \
    'predictClusterAddress((address,address,bytes32[],bytes32[],bool,bool,uint32,uint8,address),bytes32)(address)' \
    "$tuple" "$salt" --rpc-url "$RPC_URL"
}

_verify_indexer_cluster_identity() {
  local cluster="$1" name="$2" salt predicted
  require INDEXER_COMPOSE_HASH INDEXER_DEVICE_IDS_JSON INDEXER_CLUSTER_OWNER
  echo "$name" | grep -Eq '^[a-zA-Z0-9._-]+$' \
    || die "indexer cluster name may contain only letters, digits, dot, underscore, and dash"
  _canonicalize_indexer_device_ids
  salt=$(cast keccak "attestmesh-indexer-cluster-${name}") \
    || die "could not derive Indexer cluster identity salt"
  predicted=$(_predict_indexer_cluster "$salt") \
    || die "could not recompute dedicated Indexer cluster identity"
  _same_address "$predicted" "$cluster" \
    || die "cluster does not match the reviewed factory prediction for name/config ($predicted != $cluster)"
}

_verify_indexer_boot_policy() {
  local cluster="$1" device
  [ "$(cast call "$cluster" 'allowAnyDevice()(bool)' --rpc-url "$RPC_URL")" = false ] \
    || die "Indexer cluster unexpectedly allows any device"
  [ "$(cast call "$cluster" 'requireTcbUpToDate()(bool)' --rpc-url "$RPC_URL")" = true ] \
    || die "Indexer cluster does not require an up-to-date TCB"
  [ "$(cast call "$cluster" 'allowedComposeHashes(bytes32)(bool)' \
      "$INDEXER_COMPOSE_HASH" --rpc-url "$RPC_URL")" = true ] \
    || die "Indexer cluster is missing the reviewed compose hash"
  [ "$(cast call "$cluster" 'allowedKmsRoots(address)(bool)' \
      "$KMS_ROOT" --rpc-url "$RPC_URL")" = true ] \
    || die "Indexer cluster is missing the reviewed KMS root"
  while IFS= read -r device; do
    [ "$(cast call "$cluster" 'allowedDeviceIds(bytes32)(bool)' \
        "$device" --rpc-url "$RPC_URL")" = true ] \
      || die "Indexer cluster is missing reviewed device $device"
  done < <(printf '%s\n' "$INDEXER_DEVICE_IDS_JSON" | jq -r '.[]')
}

_verify_indexer_cluster_policy() {
  local cluster="$1" code policy_owner solidstate_owner nominee
  code=$(cast code "$cluster" --rpc-url "$RPC_URL") \
    || die "could not read predicted Indexer cluster code"
  [ "$code" != 0x ] || die "predicted Indexer cluster has no code: $cluster"
  _verify_canonical_cluster "$cluster"
  policy_owner=$(cast call "$cluster" 'clusterOwner()(address)' --rpc-url "$RPC_URL") \
    || die "could not read Indexer cluster policy owner"
  _same_address "$policy_owner" "$INDEXER_STAGE_A_SAFE" \
    || die "Indexer cluster policy owner is not the reviewed Safe"
  solidstate_owner=$(cast call "$cluster" 'owner()(address)' --rpc-url "$RPC_URL") \
    || die "could not read Indexer cluster solidstate owner"
  if _same_address "$solidstate_owner" "$INDEXER_STAGE_A_CLUSTER_FACTORY"; then
    nominee=$(cast call "$cluster" 'nomineeOwner()(address)' --rpc-url "$RPC_URL") \
      || die "could not read Indexer cluster ownership nominee"
    _same_address "$nominee" "$INDEXER_STAGE_A_SAFE" \
      || die "reviewed Safe is not the pending solidstate owner"
  elif ! _same_address "$solidstate_owner" "$INDEXER_STAGE_A_SAFE"; then
    die "Indexer cluster solidstate owner is neither factory nor reviewed Safe"
  fi
  _verify_indexer_boot_policy "$cluster"
  _verify_exact_cluster_topology "$cluster" "$INDEXER_STAGE_A_FACTORY_DSTACK_FACET"
  _verify_fresh_generation "$cluster"
}

# Returns 0 for an exact, reconciled deployment and 1 when the predicted address
# is still cleanly absent. Any split mapping/code state fails closed.
_reconcile_indexer_cluster() {
  local cluster="$1" deployed code
  deployed=$(cast call "$INDEXER_STAGE_A_CLUSTER_FACTORY" \
    'deployedClusters(address)(bool)' "$cluster" --rpc-url "$RPC_URL") \
    || die "could not reconcile predicted Indexer cluster mapping"
  code=$(cast code "$cluster" --rpc-url "$RPC_URL") \
    || die "could not reconcile predicted Indexer cluster code"
  if [ "$deployed" = false ] && [ "$code" = 0x ]; then
    return 1
  fi
  [ "$deployed" = true ] && [ "$code" != 0x ] \
    || die "predicted Indexer cluster has inconsistent factory/code state"
  _verify_indexer_cluster_policy "$cluster"
}

preflight() {
  require PRIVATE_KEY DEPLOYER_ADDR
  local d; d=$(cast wallet address --private-key "$PRIVATE_KEY" 2>/dev/null)
  [ "${d,,}" = "${DEPLOYER_ADDR,,}" ] || die "deployer key mismatch ($d != $DEPLOYER_ADDR)"
  log "preflight ok: deployer=$d chain=$(cast chain-id --rpc-url "$RPC_URL") balance=$(cast from-wei "$(cast balance "$d" --rpc-url "$RPC_URL")")ETH"
}

# infra: idempotent — skip if the receipt's factory already has code on-chain.
infra() {
  if [ -f "$RECEIPT" ]; then
    local f; f=$(jq -r .clusterDiamondFactory "$RECEIPT" 2>/dev/null)
    if [ -n "$f" ] && [ "$(cast code "$f" --rpc-url "$RPC_URL" 2>/dev/null)" != "0x" ]; then
      log "infra already deployed (factory $f has code) — skipping. Set FORCE=1 to redeploy."
      [ "${FORCE:-0}" = "1" ] || return 0
    fi
  fi
  run_step deploy-infra bash -c "cd '$ROOT/contracts' && forge script script/DeployInfra.s.sol:DeployInfra --rpc-url '$RPC_URL' --broadcast"
  log "infra receipt: $RECEIPT"; jq . "$RECEIPT" >&2
}

# cluster: deploy a ClusterDiamond from a generated config (KMS root + compose hash seeded).
cluster() {
  local name="${1:-attestmesh-1}"
  require COMPOSE_HASH PRIVATE_KEY DEPLOYER_ADDR
  export CLUSTER_FACTORY MEMBER_FACTORY CLUSTER_CONFIG
  CLUSTER_FACTORY=$(jq -r .clusterDiamondFactory "$RECEIPT"); MEMBER_FACTORY=$(jq -r .clusterMemberFactory "$RECEIPT")
  CLUSTER_CONFIG="script/clusters/${name}.json"
  local salt; salt=$(cast keccak "attestmesh-cluster-${name}")
  cat > "$ROOT/contracts/$CLUSTER_CONFIG" <<JSON
{ "clusterOwner": "$DEPLOYER_ADDR", "kmsRootSigner": "$KMS_ROOT",
  "initialComposeHashes": ["$COMPOSE_HASH"], "initialDeviceIds": [],
  "allowAnyDevice": true, "requireTcbUpToDate": false,
  "meshCidrIp": 168624128, "meshCidrPrefix": 16, "salt": "$salt" }
JSON
  run_step "deploy-cluster-${name}" bash -c "cd '$ROOT/contracts' && forge script script/DeployCluster.s.sol:DeployCluster --rpc-url '$RPC_URL' --broadcast"
}

# indexer-cluster: create the dedicated signer cluster used by the active-active
# Indexer pool. This intentionally has a stricter boot policy than the general
# development cluster helper above: the CSK becomes the fleet-wide envelope
# signing key, so admitting a device is equivalent to admitting a signer.
#
# Required env:
#   INDEXER_COMPOSE_HASH      exact rendered dstack compose hash shared by replicas
#   INDEXER_DEVICE_IDS_JSON   JSON array of explicitly approved bytes32 device ids
#   INDEXER_CLUSTER_OWNER     org Safe that owns both cluster policy surfaces
indexer_cluster() {
  local name="${1:-attestmesh-indexer-ha}"
  local salt predicted rc
  require INDEXER_COMPOSE_HASH INDEXER_DEVICE_IDS_JSON INDEXER_CLUSTER_OWNER
  echo "$name" | grep -Eq '^[a-zA-Z0-9._-]+$' \
    || die "indexer cluster name may contain only letters, digits, dot, underscore, and dash"
  echo "$INDEXER_COMPOSE_HASH" | grep -Eq '^0x[0-9a-fA-F]{64}$' \
    || die "INDEXER_COMPOSE_HASH must be a bytes32 hex value"
  [ "$INDEXER_COMPOSE_HASH" != "0x$(printf '0%.0s' {1..64})" ] \
    || die "INDEXER_COMPOSE_HASH must be nonzero"
  _canonicalize_indexer_device_ids
  echo "$INDEXER_CLUSTER_OWNER" | grep -Eq '^0x[0-9a-fA-F]{40}$' \
    || die "INDEXER_CLUSTER_OWNER must be an address"
  [ "${INDEXER_CLUSTER_OWNER,,}" != "0x$(printf '0%.0s' {1..40})" ] \
    || die "INDEXER_CLUSTER_OWNER must be nonzero"
  echo "$KMS_ROOT" | grep -Eq '^0x[0-9a-fA-F]{40}$' \
    || die "KMS_ROOT must be an address"
  [ "${KMS_ROOT,,}" != "0x$(printf '0%.0s' {1..40})" ] \
    || die "KMS_ROOT must be nonzero"

  _require_indexer_stage_a_chain
  _validate_indexer_stage_a_safe "$INDEXER_CLUSTER_OWNER"
  _validate_indexer_stage_a_factories

  export CLUSTER_FACTORY MEMBER_FACTORY CLUSTER_CONFIG
  CLUSTER_FACTORY="$INDEXER_STAGE_A_CLUSTER_FACTORY"
  MEMBER_FACTORY="$INDEXER_STAGE_A_MEMBER_FACTORY"

  CLUSTER_CONFIG="script/clusters/${name}.json"
  salt=$(cast keccak "attestmesh-indexer-cluster-${name}") \
    || die "could not derive Indexer cluster salt"
  jq -n \
    --arg owner "$INDEXER_CLUSTER_OWNER" \
    --arg kms "$KMS_ROOT" \
    --arg compose "$INDEXER_COMPOSE_HASH" \
    --argjson devices "$INDEXER_DEVICE_IDS_JSON" \
    --arg salt "$salt" \
    '{clusterOwner:$owner, kmsRootSigner:$kms,
      initialComposeHashes:[$compose], initialDeviceIds:$devices,
      allowAnyDevice:false, requireTcbUpToDate:true,
      meshCidrIp:169279488, meshCidrPrefix:16, salt:$salt}' \
    > "$ROOT/contracts/$CLUSTER_CONFIG"
  predicted=$(_predict_indexer_cluster "$salt") \
    || die "could not predict dedicated Indexer cluster address"
  echo "$predicted" | grep -Eq '^0x[0-9a-fA-F]{40}$' \
    || die "factory returned a malformed Indexer cluster prediction"

  if _reconcile_indexer_cluster "$predicted"; then
    log "dedicated Indexer cluster already deployed and exactly reconciled: $predicted"
    printf 'Cluster deployed: %s\n' "$predicted"
    return 0
  fi

  if run_step "deploy-indexer-cluster-${name}" bash -c \
    "cd '$ROOT/contracts' && forge script script/DeployCluster.s.sol:DeployCluster --rpc-url '$RPC_URL' --broadcast --slow"; then
    :
  else
    rc=$?
    if _reconcile_indexer_cluster "$predicted"; then
      log "forge returned rc=$rc after deployment, but the predicted cluster is exactly reconciled"
    else
      die "dedicated Indexer cluster deployment failed before the predicted cluster was committed"
    fi
  fi
  _reconcile_indexer_cluster "$predicted" \
    || die "forge returned success but the predicted Indexer cluster is absent"
  log "dedicated Indexer cluster deployed with closed device policy; config=$CLUSTER_CONFIG"
  log "fresh factory cluster retains NetworkFacet $INDEXER_STAGE_A_FACTORY_NETWORK_FACET; the Path-A cut does not add heartbeat-key selectors"
  printf 'Cluster deployed: %s\n' "$predicted"
}

# seed-appid <cluster> <memberAddr>: owner pre-approves an app_id so the KMS gate admits
# the CVM at first boot (the cold-start fix). Owner-gas (not sponsored).
seed_appid() {
  local cluster="$1" member="$2"
  require PRIVATE_KEY DEPLOYER_ADDR
  send_with_nonce_retry "seed-appid-${member}" "$cluster" "addAllowedAppId(address)" "$member"
  log "allowedAppIds[$member] = $(cast call "$cluster" 'allowedAppIds(address)(bool)' "$member" --rpc-url "$RPC_URL")"
}

# patha-upgrade <cluster>: legacy EOA-owner flow. Diamond-cut the cluster's DstackFacet to the
# Path A build (dstack_register accepts owner-allowlisted app_ids) + deploy the Path A ClusterMember
# impl. Safe-owned clusters must use patha-safe-prepare below; this command cannot act as a Safe.
patha_upgrade() {
  local cluster="${1:?usage: onchain.sh patha-upgrade <cluster>}"
  require PRIVATE_KEY DEPLOYER_ADDR
  export CLUSTER="$cluster"
  run_step "patha-upgrade-${cluster}" bash -c "cd '$ROOT/contracts' && forge script script/UpgradeDstackFacetPathA.s.sol:UpgradeDstackFacetPathA --rpc-url '$RPC_URL' --broadcast"
}

_validate_patha_cluster_boundary() {
  local cluster="$1" safe="$2" code accepted_owner policy_owner
  echo "$cluster" | grep -Eq '^0x[0-9a-fA-F]{40}$' \
    || die "Path-A cluster must be an address"
  echo "$safe" | grep -Eq '^0x[0-9a-fA-F]{40}$' \
    || die "Path-A Safe must be an address"
  _require_indexer_stage_a_chain
  _validate_indexer_stage_a_safe "$safe"
  _validate_indexer_stage_a_factories
  code=$(cast code "$cluster" --rpc-url "$RPC_URL") \
    || die "could not read Path-A cluster code: $cluster"
  [ "$code" != 0x ] || die "Path-A cluster has no code: $cluster"
  _verify_canonical_cluster "$cluster"
  accepted_owner=$(cast call "$cluster" 'owner()(address)' --rpc-url "$RPC_URL") \
    || die "could not read Path-A cluster solidstate owner"
  _same_address "$accepted_owner" "$safe" \
    || die "reviewed Safe has not accepted solidstate ownership ($accepted_owner != $safe)"
  policy_owner=$(cast call "$cluster" 'clusterOwner()(address)' --rpc-url "$RPC_URL") \
    || die "could not read Path-A cluster policy owner"
  _same_address "$policy_owner" "$safe" \
    || die "reviewed Safe does not own cluster policy ($policy_owner != $safe)"
}

_validate_patha_create2_boundary() {
  local predicted
  _expect_codehash "canonical CREATE2 deployer" "$INDEXER_STAGE_A_CREATE2_DEPLOYER" \
    "$INDEXER_STAGE_A_CREATE2_DEPLOYER_CODEHASH"
  predicted=$(cast create2 --deployer "$INDEXER_STAGE_A_CREATE2_DEPLOYER" \
    --salt "$INDEXER_STAGE_A_DSTACK_SALT" \
    --init-code-hash "$INDEXER_STAGE_A_DSTACK_INIT_CODEHASH") \
    || die "could not recompute deterministic DstackFacet address"
  _same_address "$predicted" "$INDEXER_STAGE_A_DSTACK_FACET" \
    || die "deterministic DstackFacet prediction drifted"
  predicted=$(cast create2 --deployer "$INDEXER_STAGE_A_CREATE2_DEPLOYER" \
    --salt "$INDEXER_STAGE_A_MEMBER_SALT" \
    --init-code-hash "$INDEXER_STAGE_A_MEMBER_INIT_CODEHASH") \
    || die "could not recompute deterministic ClusterMember address"
  _same_address "$predicted" "$INDEXER_STAGE_A_MEMBER_IMPL" \
    || die "deterministic ClusterMember prediction drifted"
}

_validate_patha_bundle() {
  local bundle="$1" cluster="$2" safe="$3" expected_current="$4"
  local expected_data actual_data actual_hash
  [ -f "$bundle" ] || die "Path-A Safe bundle is missing: $bundle"
  jq -e \
    --arg chain "$INDEXER_STAGE_A_CHAIN_ID" \
    --arg cluster "${cluster,,}" \
    --arg safe "${safe,,}" \
    --arg safeSingleton "${INDEXER_STAGE_A_SAFE_SINGLETON,,}" \
    --arg safeProxyHash "${INDEXER_STAGE_A_SAFE_PROXY_CODEHASH,,}" \
    --arg safeSingletonHash "${INDEXER_STAGE_A_SAFE_SINGLETON_CODEHASH,,}" \
    --arg safeFallback "${INDEXER_STAGE_A_SAFE_FALLBACK_HANDLER,,}" \
    --arg safeFallbackHash "${INDEXER_STAGE_A_SAFE_FALLBACK_HANDLER_CODEHASH,,}" \
    --arg clusterFactory "${INDEXER_STAGE_A_CLUSTER_FACTORY,,}" \
    --arg memberFactory "${INDEXER_STAGE_A_MEMBER_FACTORY,,}" \
    --arg diamondInitHash "${INDEXER_STAGE_A_DIAMOND_INIT_CODEHASH,,}" \
    --arg attestHash "${INDEXER_STAGE_A_ATTEST_FACET_CODEHASH,,}" \
    --arg messageHash "${INDEXER_STAGE_A_MESSAGE_FACET_CODEHASH,,}" \
    --arg networkHash "${INDEXER_STAGE_A_FACTORY_NETWORK_FACET_CODEHASH,,}" \
    --arg oldDstackHash "${INDEXER_STAGE_A_FACTORY_DSTACK_FACET_CODEHASH,,}" \
    --arg oldMemberHash "${INDEXER_STAGE_A_FACTORY_MEMBER_IMPL_CODEHASH,,}" \
    --arg create2 "${INDEXER_STAGE_A_CREATE2_DEPLOYER,,}" \
    --arg broadcaster "${DEPLOYER_ADDR,,}" \
    --arg current "${expected_current,,}" \
    --arg facet "${INDEXER_STAGE_A_DSTACK_FACET,,}" \
    --arg facetSalt "${INDEXER_STAGE_A_DSTACK_SALT,,}" \
    --arg facetInit "${INDEXER_STAGE_A_DSTACK_INIT_CODEHASH,,}" \
    --arg facetRuntime "${INDEXER_STAGE_A_DSTACK_RUNTIME_CODEHASH,,}" \
    --arg member "${INDEXER_STAGE_A_MEMBER_IMPL,,}" \
    --arg memberSalt "${INDEXER_STAGE_A_MEMBER_SALT,,}" \
    --arg memberInit "${INDEXER_STAGE_A_MEMBER_INIT_CODEHASH,,}" \
    --arg memberRuntime "${INDEXER_STAGE_A_MEMBER_RUNTIME_CODEHASH,,}" '
      .schemaVersion == 1
      and (.chainId | tostring) == $chain
      and (.cluster | ascii_downcase) == $cluster
      and (.target | ascii_downcase) == $cluster
      and (.safeOwner | ascii_downcase) == $safe
      and (.safeSingleton | ascii_downcase) == $safeSingleton
      and (.safeProxyCodeHash | ascii_downcase) == $safeProxyHash
      and (.safeSingletonCodeHash | ascii_downcase) == $safeSingletonHash
      and .safeModuleCount == 0
      and (.safeModulesNext | ascii_downcase) == "0x0000000000000000000000000000000000000001"
      and (.safeGuard | ascii_downcase) == "0x0000000000000000000000000000000000000000"
      and (.safeFallbackHandler | ascii_downcase) == $safeFallback
      and (.safeFallbackHandlerCodeHash | ascii_downcase) == $safeFallbackHash
      and (.clusterFactory | ascii_downcase) == $clusterFactory
      and (.memberFactory | ascii_downcase) == $memberFactory
      and (.diamondInitCodeHash | ascii_downcase) == $diamondInitHash
      and (.attestFacetCodeHash | ascii_downcase) == $attestHash
      and (.messageFacetCodeHash | ascii_downcase) == $messageHash
      and (.factoryNetworkFacetCodeHash | ascii_downcase) == $networkHash
      and (.factoryDstackFacetCodeHash | ascii_downcase) == $oldDstackHash
      and (.factoryMemberImplementationCodeHash | ascii_downcase) == $oldMemberHash
      and (.create2Deployer | ascii_downcase) == $create2
      and (.broadcaster | ascii_downcase) == $broadcaster
      and (.currentDstackFacet | ascii_downcase) == $current
      and (.dstackFacet | ascii_downcase) == $facet
      and (.dstackFacetSalt | ascii_downcase) == $facetSalt
      and (.dstackFacetInitCodeHash | ascii_downcase) == $facetInit
      and (.dstackFacetRuntimeCodeHash | ascii_downcase) == $facetRuntime
      and (.clusterMemberImplementation | ascii_downcase) == $member
      and (.clusterMemberImplementationSalt | ascii_downcase) == $memberSalt
      and (.clusterMemberImplementationInitCodeHash | ascii_downcase) == $memberInit
      and (.clusterMemberImplementationRuntimeCodeHash | ascii_downcase) == $memberRuntime
      and .clusterFacetCount == 5
      and .clusterSelectorCount == 54
      and .memberCount == 0
      and (.cskCommitment | ascii_downcase) ==
        "0x0000000000000000000000000000000000000000000000000000000000000000"
      and .value == 0
      and (.data | type) == "string"
      and (.calldataHash | test("^0x[0-9a-fA-F]{64}$"))
    ' "$bundle" >/dev/null || die "Path-A Safe bundle failed exact trust-anchor validation"

  _validate_patha_create2_boundary
  _expect_codehash "deterministic Path-A DstackFacet" "$INDEXER_STAGE_A_DSTACK_FACET" \
    "$INDEXER_STAGE_A_DSTACK_RUNTIME_CODEHASH"
  _expect_codehash "deterministic Path-A ClusterMember" "$INDEXER_STAGE_A_MEMBER_IMPL" \
    "$INDEXER_STAGE_A_MEMBER_RUNTIME_CODEHASH"
  expected_data=$(_reviewed_patha_calldata "$INDEXER_STAGE_A_DSTACK_FACET") \
    || die "could not independently encode reviewed Path-A diamondCut"
  actual_data=$(jq -er .data "$bundle") || die "Path-A Safe bundle has no calldata"
  [ "${actual_data,,}" = "${expected_data,,}" ] \
    || die "Path-A Safe calldata is not the exact reviewed one-entry 19-selector REPLACE cut"
  actual_hash=$(cast keccak "$actual_data") || die "could not hash Path-A Safe calldata"
  [ "${actual_hash,,}" = "$(jq -r '.calldataHash | ascii_downcase' "$bundle")" ] \
    || die "Path-A Safe bundle calldata hash mismatch"
}

# patha-safe-prepare <cluster> <safe>: deploy the Path A facet/member implementation from the
# configured broadcaster, then emit an exact target/value/calldata bundle for separate Safe
# review and execution. This command NEVER submits the diamondCut and fails unless the Safe has
# already accepted the cluster's solidstate ownership.
patha_safe_prepare() {
  local cluster="${1:?usage: onchain.sh patha-safe-prepare <cluster> <safe>}"
  local safe="${2:?usage: onchain.sh patha-safe-prepare <cluster> <safe>}"
  local name="${3:-attestmesh-indexer-ha}"
  local bundle_rel bundle_abs current_facet attempt prepared=0

  _validate_patha_cluster_boundary "$cluster" "$safe"
  require INDEXER_CLUSTER_OWNER INDEXER_COMPOSE_HASH INDEXER_DEVICE_IDS_JSON
  _same_address "$INDEXER_CLUSTER_OWNER" "$safe" \
    || die "INDEXER_CLUSTER_OWNER does not match the reviewed Path-A Safe"
  _verify_indexer_cluster_identity "$cluster" "$name"
  _verify_indexer_boot_policy "$cluster"
  _verify_fresh_generation "$cluster"
  _verify_exact_cluster_topology "$cluster" "$INDEXER_STAGE_A_FACTORY_DSTACK_FACET"
  current_facet=$(cast call "$cluster" 'facetAddress(bytes4)(address)' \
    "${INDEXER_STAGE_A_DSTACK_SELECTORS[0]}" --rpc-url "$RPC_URL") \
    || die "could not resolve the current dstack facet"
  if _same_address "$current_facet" "$INDEXER_STAGE_A_DSTACK_FACET"; then
    die "reviewed Path-A facet is already installed; run patha-safe-verify, never replay the cut"
  fi
  _same_address "$current_facet" "$INDEXER_STAGE_A_FACTORY_DSTACK_FACET" \
    || die "fresh cluster does not use the reviewed factory DstackFacet: $current_facet"
  _validate_patha_create2_boundary

  bundle_rel="script/deployments/${CHAIN_ID}-patha-safe-${cluster,,}.json"
  bundle_abs="$ROOT/contracts/$bundle_rel"
  export CLUSTER="$cluster" EXPECTED_SAFE_OWNER="$safe" PATHA_BUNDLE_FILE="$bundle_rel"

  for attempt in 1 2; do
    rm -f "$bundle_abs"
    if run_step "patha-safe-prepare-${cluster}-attempt-${attempt}" bash -c \
      "cd '$ROOT/contracts' && forge script script/PrepareDstackFacetPathASafe.s.sol:PrepareDstackFacetPathASafe --rpc-url '$RPC_URL' --broadcast --slow"; then
      prepared=1
      break
    fi
    rm -f "$bundle_abs"
    [ "$attempt" -eq 1 ] \
      && log "Path-A preparation did not complete; retrying once to reconcile any deterministic partial deployment"
  done
  [ "$prepared" -eq 1 ] \
    || die "Path-A Safe preparation failed after deterministic recovery; no bundle was retained"

  if ! (
    _validate_patha_bundle "$bundle_abs" "$cluster" "$safe" "$current_facet"
    _verify_exact_cluster_topology "$cluster" "$current_facet"
    _verify_fresh_generation "$cluster"
  ); then
    rm -f "$bundle_abs"
    die "discarded invalid Path-A Safe bundle after post-broadcast reconciliation failed"
  fi

  log "Path-A implementations deployed; the cluster diamondCut has NOT been submitted"
  log "Path-A intentionally leaves NetworkFacet $INDEXER_STAGE_A_FACTORY_NETWORK_FACET unchanged; workers require PEER_ENVELOPE_FALLBACK=true until a separate reviewed NetworkFacet cut"
  log "review the exact Safe transaction bundle: $bundle_abs"
  jq '{target, value, data, dstackFacet, clusterMemberImplementation, currentDstackFacet, calldataHash}' \
    "$bundle_abs" >&2
}

# patha-safe-verify <cluster> <safe>: after the Safe executes the emitted transaction,
# revalidate every pinned boundary and require every reviewed dstack selector to resolve
# to the deterministic Path-A facet. This command never submits a transaction.
patha_safe_verify() {
  local cluster="${1:?usage: onchain.sh patha-safe-verify <cluster> <safe>}"
  local safe="${2:?usage: onchain.sh patha-safe-verify <cluster> <safe>}"
  local name="${3:-attestmesh-indexer-ha}"
  local bundle_abs

  _validate_patha_cluster_boundary "$cluster" "$safe"
  require INDEXER_CLUSTER_OWNER INDEXER_COMPOSE_HASH INDEXER_DEVICE_IDS_JSON
  _same_address "$INDEXER_CLUSTER_OWNER" "$safe" \
    || die "INDEXER_CLUSTER_OWNER does not match the reviewed Path-A Safe"
  _verify_indexer_cluster_identity "$cluster" "$name"
  _verify_indexer_boot_policy "$cluster"
  _verify_fresh_generation "$cluster"
  bundle_abs="$ROOT/contracts/script/deployments/${CHAIN_ID}-patha-safe-${cluster,,}.json"
  _validate_patha_bundle \
    "$bundle_abs" "$cluster" "$safe" "$INDEXER_STAGE_A_FACTORY_DSTACK_FACET"
  _verify_exact_cluster_topology "$cluster" "$INDEXER_STAGE_A_DSTACK_FACET"
  log "verified Safe Path-A cut: all 19 reviewed dstack selectors resolve to $INDEXER_STAGE_A_DSTACK_FACET"
  log "NetworkFacet remains $INDEXER_STAGE_A_FACTORY_NETWORK_FACET; keep PEER_ENVELOPE_FALLBACK=true until its separate reviewed cut"
  jq '{target, dstackFacet, clusterMemberImplementation, calldataHash}' "$bundle_abs"
}

# indexer-stage-a-verify <cluster> [name]: read-only worker/runtime boundary.
# It deliberately does not require the preparation bundle or a private key.
indexer_stage_a_verify() {
  local cluster="${1:?usage: onchain.sh indexer-stage-a-verify <cluster> [name]}"
  local name="${2:-attestmesh-indexer-ha}"
  require INDEXER_CLUSTER_OWNER INDEXER_COMPOSE_HASH INDEXER_DEVICE_IDS_JSON \
    DSTACK_FACET MEMBER_IMPL
  _same_address "$DSTACK_FACET" "$INDEXER_STAGE_A_DSTACK_FACET" \
    || die "DSTACK_FACET is not the reviewed Stage-A Path-A implementation"
  _same_address "$MEMBER_IMPL" "$INDEXER_STAGE_A_MEMBER_IMPL" \
    || die "MEMBER_IMPL is not the reviewed Stage-A ClusterMember implementation"
  _validate_patha_cluster_boundary "$cluster" "$INDEXER_CLUSTER_OWNER"
  _verify_indexer_cluster_identity "$cluster" "$name"
  _verify_indexer_boot_policy "$cluster"
  _validate_patha_create2_boundary
  _expect_codehash "deterministic Path-A DstackFacet" "$DSTACK_FACET" \
    "$INDEXER_STAGE_A_DSTACK_RUNTIME_CODEHASH"
  _expect_codehash "deterministic Path-A ClusterMember" "$MEMBER_IMPL" \
    "$INDEXER_STAGE_A_MEMBER_RUNTIME_CODEHASH"
  _verify_exact_cluster_topology "$cluster" "$DSTACK_FACET"
  log "verified complete Stage-A runtime boundary for cluster $cluster"
}

case "${1:-all}" in
  preflight) preflight ;;
  infra)     preflight; infra ;;
  cluster)   cluster "${2:-attestmesh-1}" ;;
  indexer-cluster) preflight; indexer_cluster "${2:-attestmesh-indexer-ha}" ;;
  patha-upgrade) patha_upgrade "$2" ;;
  patha-safe-prepare) preflight; patha_safe_prepare "${2:-}" "${3:-}" "${4:-attestmesh-indexer-ha}" ;;
  patha-safe-verify) preflight; patha_safe_verify "${2:-}" "${3:-}" "${4:-attestmesh-indexer-ha}" ;;
  indexer-stage-a-verify) indexer_stage_a_verify "${2:-}" "${3:-attestmesh-indexer-ha}" ;;
  seed-appid) seed_appid "$2" "$3" ;;
  all)       preflight; infra; cluster "${2:-attestmesh-1}" ;;
  *) die "usage: onchain.sh {preflight|infra|cluster [name]|indexer-cluster [name]|patha-upgrade <cluster>|patha-safe-prepare <cluster> <safe> [name]|patha-safe-verify <cluster> <safe> [name]|indexer-stage-a-verify <cluster> [name]|seed-appid <cluster> <member>|all}" ;;
esac
