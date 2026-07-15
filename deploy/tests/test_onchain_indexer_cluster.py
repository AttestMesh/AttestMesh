#!/usr/bin/env python3
"""Host-only fail-closed tests for the dedicated Indexer cluster preflight."""

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
SAFE = "0x" + ("3" * 40)
CLUSTER_FACTORY = "0x" + ("4" * 40)
MEMBER_FACTORY = "0x" + ("5" * 40)
COMPOSE_HASH = "0x" + ("6" * 64)
DEVICE_ID = "0x" + ("7" * 64)


def write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def cast_stub() -> str:
    return textwrap.dedent(
        f"""\
        #!/usr/bin/env bash
        set -u
        case "$1" in
          wallet)
            [ "$2" = address ] || exit 90
            echo {DEPLOYER}
            ;;
          chain-id) echo "${{STUB_CHAIN_ID:-{CHAIN_ID}}}" ;;
          balance) echo 1000000000000000000 ;;
          from-wei) echo 1 ;;
          keccak) echo 0x$(printf '8%.0s' {{1..64}}) ;;
          code)
            if [ "${{2,,}}" = "${{STUB_NO_CODE_ADDRESS:-0x0}}" ]; then
              echo 0x
            else
              echo 0x60006000
            fi
            ;;
          call)
            [ "${{STUB_SAFE_CALL_FAIL:-0}}" != 1 ] || exit 93
            case "${{3:-}}" in
              'getThreshold()(uint256)') echo "${{STUB_SAFE_THRESHOLD:-2}}" ;;
              'getOwners()(address[])')
                [ "${{STUB_SAFE_OWNERS_FAIL:-0}}" != 1 ] || exit 94
                if [ -n "${{STUB_SAFE_OWNERS_JSON:-}}" ]; then
                  printf '%s\\n' "$STUB_SAFE_OWNERS_JSON"
                else
                  printf '%s\\n' '[["{DEPLOYER}","{KMS_ROOT}"]]'
                fi
                ;;
              *) exit 91 ;;
            esac
            ;;
          to-dec) echo "$2" ;;
          *) echo "unexpected cast command: $*" >&2; exit 92 ;;
        esac
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
        (self.repo / "contracts" / "script" / "deployments" / f"{CHAIN_ID}.json").write_text(
            json.dumps(
                {
                    "clusterDiamondFactory": CLUSTER_FACTORY,
                    "clusterMemberFactory": MEMBER_FACTORY,
                }
            ),
            encoding="utf-8",
        )
        write_executable(self.tmp / "bin" / "cast", cast_stub())
        write_executable(
            self.tmp / "bin" / "forge",
            "#!/bin/sh\nprintf '%s\\n' \"$*\" > \"$FORGE_MARKER\"\n",
        )

        self.marker = self.tmp / "forge-called"
        self.env = {
            "PATH": f"{self.tmp / 'bin'}:/usr/bin:/bin",
            "HOME": str(self.tmp),
            "LOGDIR": str(self.tmp / "logs"),
            "FORGE_MARKER": str(self.marker),
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

    def assert_rejected_before_broadcast(
        self, result: subprocess.CompletedProcess[str], message: str
    ) -> None:
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(message, result.stderr)
        self.assertFalse(self.marker.exists(), "forge broadcast must not be attempted")
        self.assertFalse(
            (self.repo / "contracts" / "script" / "clusters" / "unit-test.json").exists(),
            "cluster config must not be written before preflight passes",
        )

    def test_rejects_rpc_chain_mismatch_before_broadcast(self) -> None:
        result = self.run_cluster(STUB_CHAIN_ID="1")
        self.assert_rejected_before_broadcast(result, "RPC chain mismatch")

    def test_rejects_cluster_factory_without_code_before_broadcast(self) -> None:
        result = self.run_cluster(STUB_NO_CODE_ADDRESS=CLUSTER_FACTORY.lower())
        self.assert_rejected_before_broadcast(result, "ClusterDiamondFactory has no code")

    def test_rejects_member_factory_without_code_before_broadcast(self) -> None:
        result = self.run_cluster(STUB_NO_CODE_ADDRESS=MEMBER_FACTORY.lower())
        self.assert_rejected_before_broadcast(result, "ClusterMemberFactory has no code")

    def test_rejects_eoa_cluster_owner_before_broadcast(self) -> None:
        result = self.run_cluster(STUB_NO_CODE_ADDRESS=SAFE.lower())
        self.assert_rejected_before_broadcast(result, "must be a deployed Safe contract")

    def test_rejects_zero_safe_threshold_before_broadcast(self) -> None:
        result = self.run_cluster(STUB_SAFE_THRESHOLD="0")
        self.assert_rejected_before_broadcast(result, "Safe threshold must be positive")

    def test_rejects_contract_without_safe_threshold_surface_before_broadcast(self) -> None:
        result = self.run_cluster(STUB_SAFE_CALL_FAIL="1")
        self.assert_rejected_before_broadcast(result, "does not expose getThreshold()")

    def test_rejects_contract_without_safe_owners_surface_before_broadcast(self) -> None:
        result = self.run_cluster(STUB_SAFE_OWNERS_FAIL="1")
        self.assert_rejected_before_broadcast(result, "does not expose getOwners()")

    def test_rejects_empty_safe_owner_set_before_broadcast(self) -> None:
        result = self.run_cluster(STUB_SAFE_OWNERS_JSON="[[]]")
        self.assert_rejected_before_broadcast(result, "owners must be non-empty, unique, nonzero")

    def test_rejects_zero_safe_owner_before_broadcast(self) -> None:
        zero = "0x" + ("0" * 40)
        result = self.run_cluster(STUB_SAFE_OWNERS_JSON=json.dumps([[zero]]))
        self.assert_rejected_before_broadcast(result, "owners must be non-empty, unique, nonzero")

    def test_rejects_duplicate_safe_owners_before_broadcast(self) -> None:
        result = self.run_cluster(
            STUB_SAFE_OWNERS_JSON=json.dumps([[DEPLOYER, DEPLOYER.upper().replace("0X", "0x")]])
        )
        self.assert_rejected_before_broadcast(result, "owners must be non-empty, unique, nonzero")

    def test_rejects_safe_threshold_above_owner_count_before_broadcast(self) -> None:
        result = self.run_cluster(STUB_SAFE_THRESHOLD="3")
        self.assert_rejected_before_broadcast(result, "Safe threshold exceeds owner count")

    def test_valid_preflight_writes_closed_policy_and_runs_forge(self) -> None:
        result = self.run_cluster()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.marker.exists())
        self.assertIn("DeployCluster.s.sol:DeployCluster", self.marker.read_text(encoding="utf-8"))

        config = json.loads(
            (self.repo / "contracts" / "script" / "clusters" / "unit-test.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(config["clusterOwner"], SAFE)
        self.assertEqual(config["initialComposeHashes"], [COMPOSE_HASH])
        self.assertEqual(config["initialDeviceIds"], [DEVICE_ID])
        self.assertFalse(config["allowAnyDevice"])
        self.assertTrue(config["requireTcbUpToDate"])


if __name__ == "__main__":
    unittest.main()
