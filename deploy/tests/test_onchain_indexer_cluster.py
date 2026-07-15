#!/usr/bin/env python3
"""Host-only fail-closed tests for the reviewed Base Indexer cluster flow."""

import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ONCHAIN = ROOT / "deploy" / "onchain.sh"
LIB = ROOT / "deploy" / "lib.sh"

CHAIN_ID = "8453"
DEPLOYER = "0x" + ("1" * 40)
KMS_ROOT = "0x" + ("2" * 40)
COMPOSE_HASH = "0x" + ("6" * 64)
DEVICE_ID = "0x" + ("7" * 64)
SALT = "0x" + ("8" * 64)
PREDICTED = "0x" + ("9" * 40)

SAFE = "0xD97b5e3Fc685e29825d76b4F90B7B3ACAE7D66f0"
SAFE_PROXY_HASH = "0xd7d408ebcd99b2b70be43e20253d6d92a8ea8fab29bd3be7f55b10032331fb4c"
SAFE_SINGLETON = "0x41675C099F32341bf84BFc5382aF534df5C7461a"
SAFE_SINGLETON_HASH = "0x1fe2df852ba3299d6534ef416eefa406e56ced995bca886ab7a553e6d0c5e1c4"
SAFE_OWNER_0 = "0x37f5761218D30E90CeeF54CcA1b71208115Acc4F"
SAFE_OWNER_1 = "0x890e54A378b07483a6e04AD3D2264609bD3fd07E"
SAFE_FALLBACK = "0xfd0732Dc9E303f09fCEf3a7388Ad10A83459Ec99"
SAFE_FALLBACK_HASH = "0x7c6007a5d711cea8dfd5d91f5940ec29c7f200fe511eb1fc1397b367af3c42f9"
CLUSTER_FACTORY = "0xf6E85fD138E3208d3AAE63ce4E2A33f20e82b9fb"
CLUSTER_FACTORY_HASH = "0x9cd0a5b30b384cc625105713ef346fc086d723c6801e3a0cb07e47fc91e4e71b"
MEMBER_FACTORY = "0xFf9f438EFdAa197f4ae59D1d711C4f09640b8417"
MEMBER_FACTORY_HASH = "0x46b41e453a4437bba466f4491c38c33f99d92c0b129756d45d269d35241e4718"
FACTORY_OWNER = "0x60b174704AdAf2b0BF87B426B364D6EbD81818E1"
DIAMOND_INIT = "0xe3C9CE59b6c164c7b4c81f686C876EB85198cFC3"
ATTEST_FACET = "0x10532164ca3BdaCf1dAd13Fb534262DEc5aAA9AA"
MESSAGE_FACET = "0x0F67cd8c1D8A2F71d2bb8091B2eb166E6b6bB564"
NETWORK_FACET = "0x6AB2b7D506c85C7A9eA22f2ECcE0cc9191006159"
DSTACK_FACET = "0xe9d463974c6E833DC38794d7f2DC5AB692352968"
MEMBER_IMPL = "0xd05223da04B4E73AC02ECA9638D490f45f765843"
DIAMOND_INIT_HASH = "0x0f11d9b0fa554b9b78051e49d7f92c440808e0eea23a4d30c7f925c4f44f842c"
ATTEST_HASH = "0x94ec9771e40ecb2b817cd33b56ea1a442037911a4a04ed6a92cbef7b5abd6223"
MESSAGE_HASH = "0x4328ba0202b88caceca4ed02eff3052c775f89f3620bba3fc90654f3c51256c2"
NETWORK_HASH = "0x0e63b71315c355eb05547aafc898ca82618abf1fb5614da782b7db16690cfc55"
DSTACK_HASH = "0xa8d3883794943b307f491bc6b8ff2cd7f3817f17b76457b6dc48eceba36d973e"
MEMBER_IMPL_HASH = "0xf7228096d52f508780b4a03e0632340a1ba139b104d392ddf47bb264c3be81f5"
CREATE2_DEPLOYER = "0x4e59b44847b379578588920cA78FbF26c0B4956C"
CREATE2_DEPLOYER_HASH = "0x2fa86add0aed31f33a762c9d88e807c475bd51d0f52bd0955754b2608f7e4989"
NEW_DSTACK = "0xE0c2140dD2a163198b3DDdB78D959b115Cd43ff1"
NEW_DSTACK_SALT = "0xcef5a875b56ff1fd96fd03ab048c551f1e40e478d13fc040fcd374a671de86f3"
NEW_DSTACK_INIT_HASH = "0x4b022b0b0542765fd75e895713a3aa621e5730c4ddd0172598177235adf65662"
NEW_DSTACK_RUNTIME_HASH = "0xf19b470a052d0b3ab62e80ce94dd828aae33d554e507c01dab6ef7e779f77c79"
NEW_MEMBER = "0x3354510A01fAb92359dBD4204CcEc91dA9BC1E06"
NEW_MEMBER_SALT = "0xac3d0f1737eb55ea6a051b5a5d36d8a37dd42e0408cd9c8d415905623c7f9074"
NEW_MEMBER_INIT_HASH = "0x667dae361d485f9cb65c74af1548c9e5054c89eb2669156dbbd3da7f24fd0ef1"
NEW_MEMBER_RUNTIME_HASH = "0xf5820491e5bb675a2e410b583652f4e651b98a070c5900ed1d93ac3a87d263ff"

SELF_SELECTORS = "2c408059 91423765 1f931c1c 7a0ed627 adfca15e 52ef6b2c cdffacc6 01ffc9a7 8da5cb5b 8ab5150a f2fde38b 79ba5097".split()
ATTEST_SELECTORS = "7f989b8d 63a30b72 3b4c9891 87dc7ae5 7918228d b6afd2ca 11aee380 08c75c4f 0441484e 9068639e bb671732 3eeb8ee8 38c640e9 d604bed7 35901459 8af487aa 319b215c d9c8dbfe 29ca97eb".split()
MESSAGE_SELECTORS = ["84076765"]
NETWORK_SELECTORS = "4979ff72 06815bc9 4fda654e".split()
DSTACK_SELECTORS = "dfc77223 67b3f22c 2a819728 1d266200 7c4beeb8 6e4c7422 2f6622e5 bf8b211b 3440a16a 0aa58c83 54fd4d50 2514ce2d 7e02756a 12c604da 537d491c 1e079198 64e985f8 4bc7cbb7 875f31fb".split()


def topology_json(dstack: str = DSTACK_FACET) -> str:
    prefixed = lambda values: ["0x" + value for value in values]
    return json.dumps(
        [[
            [PREDICTED, prefixed(SELF_SELECTORS)],
            [ATTEST_FACET, prefixed(ATTEST_SELECTORS)],
            [MESSAGE_FACET, prefixed(MESSAGE_SELECTORS)],
            [NETWORK_FACET, prefixed(NETWORK_SELECTORS)],
            [dstack, prefixed(DSTACK_SELECTORS)],
        ]],
        separators=(",", ":"),
    )


def write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def cast_stub() -> str:
    topology = topology_json()
    upgraded_topology = topology_json(NEW_DSTACK)
    return textwrap.dedent(
        f"""\
        #!/usr/bin/env bash
        set -u
        lower() {{ printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }}
        state_exists() {{ [ -f "$CLUSTER_STATE" ]; }}
        target="${{2:-}}"
        signature="${{3:-}}"
        case "$1" in
          wallet)
            [ "$2" = address ] || exit 90
            echo {DEPLOYER}
            ;;
          chain-id) echo "${{STUB_CHAIN_ID:-{CHAIN_ID}}}" ;;
          balance) echo 1000000000000000000 ;;
          from-wei) echo 1 ;;
          keccak)
            if [ "${{STUB_REAL_CRYPTO:-0}}" = 1 ]; then exec "$REAL_CAST" "$@"; fi
            echo {SALT}
            ;;
          create2|calldata) exec "$REAL_CAST" "$@" ;;
          codehash)
            if [ "$(lower "$target")" = "$(lower "${{STUB_BAD_CODEHASH_ADDRESS:-0x0}}")" ]; then
              echo 0x$(printf 'f%.0s' {{1..64}})
              exit 0
            fi
            case "$(lower "$target")" in
              "$(lower {SAFE})") echo {SAFE_PROXY_HASH} ;;
              "$(lower {SAFE_SINGLETON})") echo {SAFE_SINGLETON_HASH} ;;
              "$(lower {SAFE_FALLBACK})") echo {SAFE_FALLBACK_HASH} ;;
              "$(lower {CLUSTER_FACTORY})") echo {CLUSTER_FACTORY_HASH} ;;
              "$(lower {MEMBER_FACTORY})") echo {MEMBER_FACTORY_HASH} ;;
              "$(lower {DIAMOND_INIT})") echo {DIAMOND_INIT_HASH} ;;
              "$(lower {ATTEST_FACET})") echo {ATTEST_HASH} ;;
              "$(lower {MESSAGE_FACET})") echo {MESSAGE_HASH} ;;
              "$(lower {NETWORK_FACET})") echo {NETWORK_HASH} ;;
              "$(lower {DSTACK_FACET})") echo {DSTACK_HASH} ;;
              "$(lower {MEMBER_IMPL})") echo {MEMBER_IMPL_HASH} ;;
              "$(lower {CREATE2_DEPLOYER})") echo {CREATE2_DEPLOYER_HASH} ;;
              "$(lower {NEW_DSTACK})") echo {NEW_DSTACK_RUNTIME_HASH} ;;
              "$(lower {NEW_MEMBER})") echo {NEW_MEMBER_RUNTIME_HASH} ;;
              *) echo "unexpected codehash target: $target" >&2; exit 91 ;;
            esac
            ;;
          code)
            if [ "$(lower "$target")" = "$(lower {PREDICTED})" ] && state_exists; then
              echo 0x60006000
            elif [ "$(lower "$target")" = "$(lower {NEW_DSTACK})" ] || [ "$(lower "$target")" = "$(lower {NEW_MEMBER})" ]; then
              echo 0x60006000
            else
              echo 0x
            fi
            ;;
          call)
            case "$(lower "$target"):$signature" in
              "$(lower {SAFE}):masterCopy()(address)") echo {SAFE_SINGLETON} ;;
              "$(lower {SAFE}):VERSION()(string)") echo '["1.4.1"]' ;;
              "$(lower {SAFE}):getThreshold()(uint256)") echo "${{STUB_SAFE_THRESHOLD:-1}}" ;;
              "$(lower {SAFE}):getOwners()(address[])")
                if [ -n "${{STUB_SAFE_OWNERS_JSON:-}}" ]; then
                  echo "$STUB_SAFE_OWNERS_JSON"
                else
                  echo '[["{SAFE_OWNER_0}","{SAFE_OWNER_1}"]]'
                fi
                ;;
              "$(lower {SAFE}):getModulesPaginated(address,uint256)(address[],address)")
                if [ "${{STUB_SAFE_MODULE:-0}}" = 0 ]; then
                  echo '[[ ],"0x0000000000000000000000000000000000000001"]'
                else
                  echo '[["0x000000000000000000000000000000000000beef"],"0x0000000000000000000000000000000000000001"]'
                fi
                ;;
              "$(lower {SAFE}):getStorageAt(uint256,uint256)(bytes)")
                if [ "${{4,,}}" = "0x4a204f620c8c5ccdca3fd54d003badd85ba500436a431f0cbda4f558c93c34c8" ]; then
                  echo "${{STUB_SAFE_GUARD_WORD:-0x$(printf '0%.0s' {{1..64}})}}"
                else
                  echo 0x000000000000000000000000{SAFE_FALLBACK[2:].lower()}
                fi
                ;;
              "$(lower {CLUSTER_FACTORY}):factoryOwner()(address)")
                echo "${{STUB_FACTORY_OWNER:-{FACTORY_OWNER}}}"
                ;;
              "$(lower {CLUSTER_FACTORY}):diamondInitImpl()(address)") echo {DIAMOND_INIT} ;;
              "$(lower {CLUSTER_FACTORY}):attestFacet()(address)") echo {ATTEST_FACET} ;;
              "$(lower {CLUSTER_FACTORY}):messageFacet()(address)") echo {MESSAGE_FACET} ;;
              "$(lower {CLUSTER_FACTORY}):networkFacet()(address)") echo {NETWORK_FACET} ;;
              "$(lower {CLUSTER_FACTORY}):dstackFacet()(address)") echo {DSTACK_FACET} ;;
              "$(lower {CLUSTER_FACTORY}):predictClusterAddress"*) echo {PREDICTED} ;;
              "$(lower {CLUSTER_FACTORY}):deployedClusters(address)(bool)")
                state_exists && echo true || echo false
                ;;
              "$(lower {MEMBER_FACTORY}):factoryOwner()(address)") echo {FACTORY_OWNER} ;;
              "$(lower {MEMBER_FACTORY}):implementation()(address)") echo {MEMBER_IMPL} ;;
              "$(lower {PREDICTED}):clusterOwner()(address)") echo {SAFE} ;;
              "$(lower {PREDICTED}):owner()(address)")
                [ "${{STUB_ACCEPTED_OWNER:-0}}" = 1 ] && echo {SAFE} || echo {CLUSTER_FACTORY}
                ;;
              "$(lower {PREDICTED}):nomineeOwner()(address)") echo {SAFE} ;;
              "$(lower {PREDICTED}):allowAnyDevice()(bool)") echo false ;;
              "$(lower {PREDICTED}):requireTcbUpToDate()(bool)") echo true ;;
              "$(lower {PREDICTED}):allowedComposeHashes(bytes32)(bool)") echo true ;;
              "$(lower {PREDICTED}):allowedKmsRoots(address)(bool)") echo true ;;
              "$(lower {PREDICTED}):allowedDeviceIds(bytes32)(bool)") echo true ;;
              "$(lower {PREDICTED}):memberCount()(uint256)") echo "${{STUB_MEMBER_COUNT:-0}}" ;;
              "$(lower {PREDICTED}):cskCommitment()(bytes32)") echo 0x$(printf '0%.0s' {{1..64}}) ;;
              "$(lower {PREDICTED}):facets()((address,bytes4[])[])")
                if [ "${{STUB_BAD_TOPOLOGY:-0}}" = 1 ]; then
                  echo '[[["{PREDICTED}",["0x2c408059"]]]]'
                elif [ "${{STUB_PATHA_CUT:-0}}" = 1 ]; then
                  echo '{upgraded_topology}'
                else
                  echo '{topology}'
                fi
                ;;
              "$(lower {PREDICTED}):facetAddress(bytes4)(address)")
                [ "${{STUB_PATHA_CUT:-0}}" = 1 ] && echo {NEW_DSTACK} || echo {DSTACK_FACET}
                ;;
              *) echo "unexpected cast call: $target $signature" >&2; exit 92 ;;
            esac
            ;;
          *) echo "unexpected cast command: $*" >&2; exit 93 ;;
        esac
        """
    )


def forge_stub() -> str:
    return textwrap.dedent(
        """\
        #!/usr/bin/env bash
        echo "$*" > "$FORGE_MARKER"
        [ "${STUB_FORGE_FAIL_BEFORE_DEPLOY:-0}" != 1 ] || exit 1
        : > "$CLUSTER_STATE"
        [ "${STUB_FORGE_FAIL_AFTER_DEPLOY:-0}" != 1 ] || exit 1
        """
    )


def reviewed_bundle(data: str | None = None) -> dict[str, object]:
    real_cast = shutil.which("cast")
    if not real_cast:
        raise RuntimeError("cast is required for the onchain host tests")
    selectors = ",".join("0x" + selector for selector in DSTACK_SELECTORS)
    if data is None:
        data = subprocess.check_output(
            [
                real_cast,
                "calldata",
                "diamondCut((address,uint8,bytes4[])[],address,bytes)",
                f"[({NEW_DSTACK},1,[{selectors}])]",
                "0x0000000000000000000000000000000000000000",
                "0x",
            ],
            text=True,
        ).strip()
    calldata_hash = subprocess.check_output(
        [real_cast, "keccak", data], text=True
    ).strip()
    return {
        "schemaVersion": 1,
        "chainId": int(CHAIN_ID),
        "broadcaster": DEPLOYER,
        "safeOwner": SAFE,
        "safeSingleton": SAFE_SINGLETON,
        "safeProxyCodeHash": SAFE_PROXY_HASH,
        "safeSingletonCodeHash": SAFE_SINGLETON_HASH,
        "safeModuleCount": 0,
        "safeModulesNext": "0x0000000000000000000000000000000000000001",
        "safeGuard": "0x0000000000000000000000000000000000000000",
        "safeFallbackHandler": SAFE_FALLBACK,
        "safeFallbackHandlerCodeHash": SAFE_FALLBACK_HASH,
        "clusterFactory": CLUSTER_FACTORY,
        "memberFactory": MEMBER_FACTORY,
        "diamondInitCodeHash": DIAMOND_INIT_HASH,
        "attestFacetCodeHash": ATTEST_HASH,
        "messageFacetCodeHash": MESSAGE_HASH,
        "factoryNetworkFacetCodeHash": NETWORK_HASH,
        "factoryDstackFacetCodeHash": DSTACK_HASH,
        "factoryMemberImplementationCodeHash": MEMBER_IMPL_HASH,
        "create2Deployer": CREATE2_DEPLOYER,
        "cluster": PREDICTED,
        "clusterFacetCount": 5,
        "clusterSelectorCount": 54,
        "memberCount": 0,
        "cskCommitment": "0x" + ("0" * 64),
        "currentDstackFacet": DSTACK_FACET,
        "dstackFacet": NEW_DSTACK,
        "dstackFacetSalt": NEW_DSTACK_SALT,
        "dstackFacetInitCodeHash": NEW_DSTACK_INIT_HASH,
        "dstackFacetRuntimeCodeHash": NEW_DSTACK_RUNTIME_HASH,
        "clusterMemberImplementation": NEW_MEMBER,
        "clusterMemberImplementationSalt": NEW_MEMBER_SALT,
        "clusterMemberImplementationInitCodeHash": NEW_MEMBER_INIT_HASH,
        "clusterMemberImplementationRuntimeCodeHash": NEW_MEMBER_RUNTIME_HASH,
        "target": PREDICTED,
        "value": 0,
        "calldataHash": calldata_hash,
        "data": data,
    }


def patha_forge_stub() -> str:
    return textwrap.dedent(
        """\
        #!/usr/bin/env bash
        attempts=0
        [ ! -f "$FORGE_ATTEMPTS" ] || attempts=$(cat "$FORGE_ATTEMPTS")
        attempts=$((attempts + 1))
        echo "$attempts" > "$FORGE_ATTEMPTS"
        echo "$*" > "$FORGE_MARKER"
        if [ "${STUB_PARTIAL_FIRST:-0}" = 1 ] && [ "$attempts" -eq 1 ]; then
          exit 1
        fi
        mkdir -p "$(dirname "$PATHA_BUNDLE_FILE")"
        cp "$BUNDLE_TEMPLATE" "$PATHA_BUNDLE_FILE"
        """
    )


class OnchainIndexerClusterPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = self.tmp / "repo"
        (self.repo / "deploy").mkdir(parents=True)
        (self.repo / "contracts" / "script" / "deployments").mkdir(parents=True)
        (self.repo / "contracts" / "script" / "clusters").mkdir(parents=True)
        (self.tmp / "bin").mkdir()
        (self.tmp / "logs").mkdir()

        shutil.copy2(ONCHAIN, self.repo / "deploy" / "onchain.sh")
        shutil.copy2(LIB, self.repo / "deploy" / "lib.sh")
        self.receipt = (
            self.repo / "contracts" / "script" / "deployments" / f"{CHAIN_ID}.json"
        )
        self.receipt.write_text(
            json.dumps(
                {
                    "clusterDiamondFactory": CLUSTER_FACTORY,
                    "clusterMemberFactory": MEMBER_FACTORY,
                }
            ),
            encoding="utf-8",
        )
        write_executable(self.tmp / "bin" / "cast", cast_stub())
        write_executable(self.tmp / "bin" / "forge", forge_stub())

        self.marker = self.tmp / "forge-called"
        self.state = self.tmp / "cluster-deployed"
        self.env = {
            "PATH": f"{self.tmp / 'bin'}:/usr/bin:/bin",
            "HOME": str(self.tmp),
            "LOGDIR": str(self.tmp / "logs"),
            "FORGE_MARKER": str(self.marker),
            "CLUSTER_STATE": str(self.state),
            "RPC_URL": "https://rpc.example.invalid",
            "CHAIN_ID": CHAIN_ID,
            "PRIVATE_KEY": "0x" + ("9" * 64),
            "DEPLOYER_ADDR": DEPLOYER,
            "KMS_ROOT": KMS_ROOT,
            "INDEXER_COMPOSE_HASH": COMPOSE_HASH,
            "INDEXER_DEVICE_IDS_JSON": json.dumps([DEVICE_ID]),
            "INDEXER_CLUSTER_OWNER": SAFE,
        }

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_cluster(self, **env_updates: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(self.env)
        env.update(env_updates)
        return subprocess.run(
            [str(self.repo / "deploy" / "onchain.sh"), "indexer-cluster", "unit-test"],
            cwd=self.repo,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    @property
    def config(self) -> Path:
        return self.repo / "contracts" / "script" / "clusters" / "unit-test.json"

    def assert_rejected_before_broadcast(
        self, result: subprocess.CompletedProcess[str], message: str
    ) -> None:
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(message, result.stderr)
        self.assertFalse(self.marker.exists(), "forge broadcast must not be attempted")
        self.assertFalse(self.config.exists(), "config must not precede trust validation")

    def test_rejects_rpc_chain_mismatch_before_broadcast(self) -> None:
        self.assert_rejected_before_broadcast(
            self.run_cluster(STUB_CHAIN_ID="1"), "RPC chain mismatch"
        )

    def test_rejects_unapproved_safe_address_before_broadcast(self) -> None:
        self.assert_rejected_before_broadcast(
            self.run_cluster(INDEXER_CLUSTER_OWNER="0x" + ("a" * 40)),
            "must be the reviewed Safe",
        )

    def test_rejects_safe_proxy_codehash_change_before_broadcast(self) -> None:
        self.assert_rejected_before_broadcast(
            self.run_cluster(STUB_BAD_CODEHASH_ADDRESS=SAFE),
            "approved Safe proxy runtime code hash mismatch",
        )

    def test_rejects_safe_owner_set_change_before_broadcast(self) -> None:
        owners = json.dumps([[SAFE_OWNER_0, DEPLOYER]])
        self.assert_rejected_before_broadcast(
            self.run_cluster(STUB_SAFE_OWNERS_JSON=owners), "Safe owner set changed"
        )

    def test_rejects_safe_module_before_broadcast(self) -> None:
        self.assert_rejected_before_broadcast(
            self.run_cluster(STUB_SAFE_MODULE="1"), "module execution surface changed"
        )

    def test_rejects_safe_guard_before_broadcast(self) -> None:
        guard = "0x" + ("0" * 24) + ("a" * 40)
        self.assert_rejected_before_broadcast(
            self.run_cluster(STUB_SAFE_GUARD_WORD=guard), "approved Safe guard changed"
        )

    def test_rejects_unreviewed_receipt_factory_before_broadcast(self) -> None:
        self.receipt.write_text(
            json.dumps(
                {
                    "clusterDiamondFactory": "0x" + ("a" * 40),
                    "clusterMemberFactory": MEMBER_FACTORY,
                }
            ),
            encoding="utf-8",
        )
        self.assert_rejected_before_broadcast(
            self.run_cluster(), "receipt cluster factory is not the reviewed Base deployment"
        )

    def test_rejects_factory_codehash_change_before_broadcast(self) -> None:
        self.assert_rejected_before_broadcast(
            self.run_cluster(STUB_BAD_CODEHASH_ADDRESS=CLUSTER_FACTORY),
            "ClusterDiamondFactory runtime code hash mismatch",
        )

    def test_rejects_factory_immutable_change_before_broadcast(self) -> None:
        self.assert_rejected_before_broadcast(
            self.run_cluster(STUB_FACTORY_OWNER=DEPLOYER),
            "ClusterDiamondFactory.factoryOwner mismatch",
        )

    def test_valid_preflight_writes_closed_policy_and_reconciles_deployment(self) -> None:
        result = self.run_cluster()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.marker.exists())
        self.assertIn("DeployCluster.s.sol:DeployCluster", self.marker.read_text(encoding="utf-8"))
        self.assertIn(f"Cluster deployed: {PREDICTED}", result.stdout)

        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(config["clusterOwner"], SAFE)
        self.assertEqual(config["initialComposeHashes"], [COMPOSE_HASH])
        self.assertEqual(config["initialDeviceIds"], [DEVICE_ID])
        self.assertFalse(config["allowAnyDevice"])
        self.assertTrue(config["requireTcbUpToDate"])

    def test_device_ids_are_canonicalized_before_config_and_prediction(self) -> None:
        second = "0x" + ("A" * 64)
        result = self.run_cluster(
            INDEXER_DEVICE_IDS_JSON=json.dumps([second, DEVICE_ID.upper().replace("0X", "0x")])
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(config["initialDeviceIds"], [DEVICE_ID, second.lower()])

    def test_exact_preexisting_cluster_skips_forge(self) -> None:
        self.state.touch()
        result = self.run_cluster()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.marker.exists())
        self.assertIn("already deployed and exactly reconciled", result.stderr)

    def test_forge_error_after_deployment_is_reconciled_without_resubmission(self) -> None:
        result = self.run_cluster(STUB_FORGE_FAIL_AFTER_DEPLOY="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.marker.exists())
        self.assertIn("forge returned rc=1 after deployment", result.stderr)

    def test_forge_success_without_predicted_cluster_fails_closed(self) -> None:
        result = self.run_cluster(STUB_FORGE_FAIL_BEFORE_DEPLOY="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("failed before the predicted cluster was committed", result.stderr)

    def test_reconciliation_rejects_wrong_dstack_topology(self) -> None:
        self.state.touch()
        result = self.run_cluster(STUB_BAD_TOPOLOGY="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exact reviewed five-facet/54-selector set", result.stderr)
        self.assertFalse(self.marker.exists())

    def test_reconciliation_rejects_nonfresh_cluster(self) -> None:
        self.state.touch()
        result = self.run_cluster(STUB_MEMBER_COUNT="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a fresh generation", result.stderr)
        self.assertFalse(self.marker.exists())


class OnchainSafePathATests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = self.tmp / "repo"
        (self.repo / "deploy").mkdir(parents=True)
        (self.repo / "contracts" / "script" / "deployments").mkdir(parents=True)
        (self.repo / "contracts" / "script" / "clusters").mkdir(parents=True)
        (self.tmp / "bin").mkdir()
        (self.tmp / "logs").mkdir()
        shutil.copy2(ONCHAIN, self.repo / "deploy" / "onchain.sh")
        shutil.copy2(LIB, self.repo / "deploy" / "lib.sh")
        (
            self.repo / "contracts" / "script" / "deployments" / f"{CHAIN_ID}.json"
        ).write_text(
            json.dumps(
                {
                    "clusterDiamondFactory": CLUSTER_FACTORY,
                    "clusterMemberFactory": MEMBER_FACTORY,
                }
            ),
            encoding="utf-8",
        )
        write_executable(self.tmp / "bin" / "cast", cast_stub())
        write_executable(self.tmp / "bin" / "forge", patha_forge_stub())

        self.marker = self.tmp / "forge-called"
        self.attempts = self.tmp / "forge-attempts"
        self.state = self.tmp / "cluster-deployed"
        self.state.touch()
        self.template = self.tmp / "bundle-template.json"
        self.template.write_text(json.dumps(reviewed_bundle()), encoding="utf-8")
        self.bundle = (
            self.repo
            / "contracts"
            / "script"
            / "deployments"
            / f"{CHAIN_ID}-patha-safe-{PREDICTED.lower()}.json"
        )
        self.env = {
            "PATH": f"{self.tmp / 'bin'}:/usr/bin:/bin",
            "HOME": str(self.tmp),
            "LOGDIR": str(self.tmp / "logs"),
            "REAL_CAST": shutil.which("cast") or "",
            "STUB_REAL_CRYPTO": "1",
            "STUB_ACCEPTED_OWNER": "1",
            "FORGE_MARKER": str(self.marker),
            "FORGE_ATTEMPTS": str(self.attempts),
            "BUNDLE_TEMPLATE": str(self.template),
            "CLUSTER_STATE": str(self.state),
            "RPC_URL": "https://rpc.example.invalid",
            "CHAIN_ID": CHAIN_ID,
            "PRIVATE_KEY": "0x" + ("9" * 64),
            "DEPLOYER_ADDR": DEPLOYER,
            "KMS_ROOT": KMS_ROOT,
            "INDEXER_COMPOSE_HASH": COMPOSE_HASH,
            "INDEXER_DEVICE_IDS_JSON": json.dumps([DEVICE_ID]),
            "INDEXER_CLUSTER_OWNER": SAFE,
        }

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_onchain(
        self, action: str, *args: str, without_key: bool = False, **updates: str
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(self.env)
        env.update(updates)
        if without_key:
            env.pop("PRIVATE_KEY", None)
            env.pop("DEPLOYER_ADDR", None)
        return subprocess.run(
            [str(self.repo / "deploy" / "onchain.sh"), action, *args],
            cwd=self.repo,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_prepare_retries_and_reconciles_partial_deterministic_deployment(self) -> None:
        result = self.run_onchain(
            "patha-safe-prepare", PREDICTED, SAFE, STUB_PARTIAL_FIRST="1"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.attempts.read_text(encoding="utf-8").strip(), "2")
        self.assertTrue(self.bundle.exists())
        self.assertIn("diamondCut has NOT been submitted", result.stderr)

    def test_prepare_rejects_tampered_calldata_even_with_matching_hash(self) -> None:
        self.template.write_text(
            json.dumps(reviewed_bundle("0xdeadbeef")), encoding="utf-8"
        )
        result = self.run_onchain("patha-safe-prepare", PREDICTED, SAFE)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not the exact reviewed one-entry 19-selector", result.stderr)
        self.assertFalse(self.bundle.exists(), "invalid Safe bundle must be discarded")

    def test_post_safe_verify_requires_exact_upgraded_topology(self) -> None:
        self.bundle.write_text(json.dumps(reviewed_bundle()), encoding="utf-8")
        result = self.run_onchain(
            "patha-safe-verify", PREDICTED, SAFE, STUB_PATHA_CUT="1"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        rejected = self.run_onchain(
            "patha-safe-verify",
            PREDICTED,
            SAFE,
            STUB_PATHA_CUT="1",
            STUB_BAD_TOPOLOGY="1",
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("exact reviewed five-facet/54-selector set", rejected.stderr)

    def test_read_only_worker_verifier_needs_no_private_key_or_bundle(self) -> None:
        result = self.run_onchain(
            "indexer-stage-a-verify",
            PREDICTED,
            without_key=True,
            STUB_PATHA_CUT="1",
            DSTACK_FACET=NEW_DSTACK,
            MEMBER_IMPL=NEW_MEMBER,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.marker.exists())
        self.assertIn("verified complete Stage-A runtime boundary", result.stderr)


if __name__ == "__main__":
    unittest.main()
