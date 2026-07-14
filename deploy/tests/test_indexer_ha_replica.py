#!/usr/bin/env python3
"""Focused, host-only checks for the dedicated Indexer replica deployment shape."""

import json
import os
import runpy
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
BOX_HELPER = ROOT / "deploy" / "generic-node-box.py"
COMPOSE = ROOT / "deploy" / "compose" / "indexer-ha-replica-node.yaml"
WRAPPER = ROOT / "deploy" / "indexer-ha-replica-node.sh"
CHAIN_ID = "8453"
CLUSTER = "0x" + ("a" * 40)
SAFE = "0x" + ("b" * 40)
MEMBER_IMPL = "0x" + ("c" * 40)
APP_ID = "0x" + ("d" * 40)
KMS_ROOT = "0x" + ("e" * 40)
COMPOSE_HASH = "0x" + ("1" * 64)
MEMBER_ID = "0x" + ("2" * 64)
PUBKEY = "0x" + ("3" * 64)
MEASURED_NAME = "attestmesh-indexer-ha-replica"
ENTRY_POINT_V07 = "0x0000000071727De22E5E9d8BAf0edAc6f37da032"
OLD_INDEXER_DIGEST = "14e9e3f6869ada585642c14e4e2e43bee197978ffc68d146dd6d3cddbc49d2bb"
ADD_APP_CALLDATA = "0x64e985f8" + ("0" * 24) + APP_ID[2:]
ACCEPT_OWNERSHIP_CALLDATA = "0x79ba5097"


def load_box_helper(box_name: str, compose_name: str, compose_path: str) -> dict:
    fake_mcp = types.SimpleNamespace(KMS_URLS=[], GATEWAY_RPC="https://gateway.invalid")
    env = {
        "BOX_NAME": box_name,
        "BOX_COMPOSE_NAME": compose_name,
        "BOX_COMPOSE": compose_path,
    }
    with mock.patch.dict(os.environ, env, clear=False), mock.patch.dict(
        sys.modules, {"mcp_dstack": fake_mcp}
    ):
        return runpy.run_path(str(BOX_HELPER), run_name="generic_node_box_test")


def write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def write_cluster_state(path: Path) -> None:
    path.write_text(
        textwrap.dedent(
            f"""\
            UPDATED_AT=20260714T000000Z
            CHAIN_ID={CHAIN_ID}
            CLUSTER={CLUSTER}
            MEMBER_IMPL={MEMBER_IMPL}
            INDEXER_COMPOSE_HASH={COMPOSE_HASH}
            INDEXER_CLUSTER_OWNER={SAFE}
            KMS_ROOT={KMS_ROOT}
            MEASURED_COMPOSE_NAME={MEASURED_NAME}
            """
        ),
        encoding="utf-8",
    )


def write_replica_state(logdir: Path, node: str) -> None:
    (logdir / f"generic-node-{node}.state").write_text(
        textwrap.dedent(
            f"""\
            UPDATED_AT=20260714T000000Z
            STATE_PHASE=deployed-stopped
            X={APP_ID}
            H={COMPOSE_HASH[2:]}
            VM_ID=test-vm-id
            CLUSTER={CLUSTER}
            MEMBER_IMPL={MEMBER_IMPL}
            KMS_ROOT={KMS_ROOT}
            GATEWAY_DOMAIN=example.invalid
            """
        ),
        encoding="utf-8",
    )


def cast_stub(*, solidstate_owner: str = SAFE, guest_chain_id: str = CHAIN_ID) -> str:
    return textwrap.dedent(
        f"""\
        #!/bin/sh
        case "$1" in
          chain-id)
            case "$*" in
              *https://cvm-rpc.example.invalid*) echo {guest_chain_id} ;;
              *) echo {CHAIN_ID} ;;
            esac
            ;;
          code) echo 0x60006000 ;;
          calldata)
            case "$2" in
              'addAllowedAppId(address)')
                [ "$3" = {APP_ID} ] || exit 91
                echo {ADD_APP_CALLDATA}
                ;;
              'acceptOwnership()') echo {ACCEPT_OWNERSHIP_CALLDATA} ;;
              *) exit 92 ;;
            esac
            ;;
          call)
            case "$3" in
              'clusterOwner()(address)') echo {SAFE} ;;
              'owner()(address)') echo {solidstate_owner} ;;
              'getThreshold()(uint256)') echo 2 ;;
              'allowAnyDevice()(bool)') echo false ;;
              'requireTcbUpToDate()(bool)') echo true ;;
              'allowedComposeHashes(bytes32)(bool)') echo true ;;
              'allowedKmsRoots(address)(bool)') echo true ;;
              'allowedAppIds(address)(bool)') echo true ;;
              'memberIdOf(address)(bytes32)') echo {MEMBER_ID} ;;
              'cluster()(address)') echo {CLUSTER} ;;
              *) echo "unexpected cast call: $*" >&2; exit 93 ;;
            esac
            ;;
          *) echo "unexpected cast command: $*" >&2; exit 94 ;;
        esac
        """
    )


def wrapper_env(tmp: Path, node: str, *, create_state: bool = True) -> dict[str, str]:
    logdir = tmp / "logs"
    bindir = tmp / "bin"
    logdir.mkdir()
    bindir.mkdir()
    cluster_state = tmp / "indexer-ha-cluster.state"
    if create_state:
        write_cluster_state(cluster_state)
        write_replica_state(logdir, node)
    compose = tmp / "indexer-ha-replica-node.yaml"
    compose.write_text(
        COMPOSE.read_text(encoding="utf-8").replace(
            OLD_INDEXER_DIGEST, "7" * 64
        ),
        encoding="utf-8",
    )
    write_executable(bindir / "cast", cast_stub())
    write_executable(bindir / "curl", "#!/bin/sh\nexit 95\n")
    return {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "HOME": str(tmp),
        "LOGDIR": str(logdir),
        "INDEXER_HA_CLUSTER_STATE": str(cluster_state),
        "COMPOSE": str(compose),
        "MATRIX_STATE": str(tmp / "matrix.state"),
        "RPC_URL": "https://rpc.example.invalid",
        "CVM_RPC_URL": "https://cvm-rpc.example.invalid",
        "BUNDLER_URL": "https://bundler.example.invalid",
        "GAS_POLICY_ID": "test-policy",
        "PRIVATE_KEY": "0x" + ("4" * 64),
        "DEPLOYER_ADDR": "0x" + ("5" * 40),
        "GATEWAY_DOMAIN": "example.invalid",
    }


def run_wrapper(node: str, action: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(WRAPPER), node, action],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


class IndexerHaReplicaTests(unittest.TestCase):
    def test_unique_vm_names_share_one_measured_compose_hash(self) -> None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as compose:
            compose.write("services:\n  workload:\n    image: example.invalid/test@sha256:abc\n")
            compose.flush()
            replica_a = load_box_helper("indexer-ha-r1", "indexer-ha-replica", compose.name)
            replica_b = load_box_helper("indexer-ha-r2", "indexer-ha-replica", compose.name)

            rendered_a, hash_a = replica_a["app_compose_and_hash"](["RPC_URL"])
            rendered_b, hash_b = replica_b["app_compose_and_hash"](["RPC_URL"])

        self.assertNotEqual(replica_a["NAME"], replica_b["NAME"])
        self.assertEqual(hash_a, hash_b)
        self.assertEqual(json.loads(rendered_a)["name"], "indexer-ha-replica")
        self.assertEqual(json.loads(rendered_b)["name"], "indexer-ha-replica")

    def test_generic_helper_defaults_measured_name_to_legacy_vm_name(self) -> None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as compose:
            compose.write("services:\n  workload:\n    image: example.invalid/test\n")
            compose.flush()
            legacy = load_box_helper("legacy-node", "", compose.name)
            rendered, _digest = legacy["app_compose_and_hash"](["RPC_URL"])

        self.assertEqual(json.loads(rendered)["name"], "legacy-node")

    def test_agent_socket_is_shared_only_with_the_indexer(self) -> None:
        compose = COMPOSE.read_text(encoding="utf-8")
        self.assertIn("INDEXER_IDENTITY=cluster-shared", compose)
        self.assertIn("INDEXER_CLUSTER_ADDR=${CLUSTER}", compose)
        self.assertIn("AGENT_GRPC_ADDR=unix:/var/run/attestmesh/agent.sock", compose)
        self.assertEqual(compose.count("agent-sock:/var/run/attestmesh"), 2)
        self.assertIn('chmod 0600 /var/run/attestmesh/agent.sock', compose)
        self.assertIn('- "9092:9092"', compose)
        self.assertIn("REGISTRATION_HELPER_ADDR=0.0.0.0:9092", compose)

    def test_safe_payload_has_exact_target_and_calldata(self) -> None:
        node = "indexer-ha-r1"
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            env = wrapper_env(tmp, node)
            result = run_wrapper(node, "safe-admission", env)
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(
                (tmp / "logs" / f"indexer-ha-safe-admission-{node}.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertIn(f"SAFE_ADDRESS={SAFE}", result.stdout)
        self.assertIn(f"SAFE_TARGET={CLUSTER}", result.stdout)
        self.assertIn(f"SAFE_CALLDATA={ADD_APP_CALLDATA}", result.stdout)
        self.assertEqual(payload["meta"]["createdFromSafeAddress"], SAFE)
        self.assertEqual(
            payload["transactions"],
            [
                {
                    "to": CLUSTER,
                    "value": "0",
                    "data": ADD_APP_CALLDATA,
                    "contractMethod": None,
                    "contractInputsValues": None,
                }
            ],
        )

    def test_solidstate_ownership_must_be_accepted_by_the_safe(self) -> None:
        node = "indexer-ha-r1"
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            env = wrapper_env(tmp, node, create_state=False)
            env.update(
                {
                    "CHAIN_ID": CHAIN_ID,
                    "CLUSTER": CLUSTER,
                    "MEMBER_IMPL": MEMBER_IMPL,
                    "INDEXER_COMPOSE_HASH": COMPOSE_HASH,
                    "INDEXER_CLUSTER_OWNER": SAFE,
                    "KMS_ROOT": KMS_ROOT,
                }
            )
            write_executable(
                tmp / "bin" / "cast",
                cast_stub(solidstate_owner="0x" + ("6" * 40)),
            )
            result = run_wrapper(node, "save-cluster-state", env)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Safe must execute acceptOwnership()", result.stderr)
        self.assertIn(f"SAFE_TARGET={CLUSTER}", result.stderr)
        self.assertIn(f"SAFE_CALLDATA={ACCEPT_OWNERSHIP_CALLDATA}", result.stderr)

    def test_dedicated_state_never_falls_back_to_matrix_state(self) -> None:
        node = "indexer-ha-r1"
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            env = wrapper_env(tmp, node, create_state=False)
            Path(env["MATRIX_STATE"]).write_text(
                f"CLUSTER={CLUSTER}\nMEMBER_IMPL={MEMBER_IMPL}\n", encoding="utf-8"
            )
            result = run_wrapper(node, "safe-admission", env)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing dedicated state", result.stderr)

    def test_bundler_must_not_equal_node_rpc(self) -> None:
        node = "indexer-ha-r1"
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            env = wrapper_env(tmp, node)
            env["BUNDLER_URL"] = env["RPC_URL"]
            result = run_wrapper(node, "preflight", env)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("BUNDLER_URL must not equal RPC_URL", result.stderr)

    def test_bundler_must_support_the_sidecar_v07_entry_point(self) -> None:
        node = "indexer-ha-r1"
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            env = wrapper_env(tmp, node)
            write_executable(
                tmp / "bin" / "curl",
                textwrap.dedent(
                    """\
                    #!/bin/sh
                    case "$*" in
                      *eth_supportedEntryPoints*)
                        echo '{"jsonrpc":"2.0","id":1,"result":["0x1111111111111111111111111111111111111111"]}'
                        ;;
                      *eth_chainId*) echo '{"jsonrpc":"2.0","id":1,"result":"0x2105"}' ;;
                      *) exit 96 ;;
                    esac
                    """
                ),
            )
            result = run_wrapper(node, "preflight", env)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("canonical v0.7 EntryPoint", result.stderr)
        self.assertIn(ENTRY_POINT_V07, result.stderr)

    def test_separately_sealed_guest_rpc_must_match_chain(self) -> None:
        node = "indexer-ha-r1"
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            env = wrapper_env(tmp, node)
            write_executable(
                tmp / "bin" / "cast", cast_stub(guest_chain_id="1")
            )
            result = run_wrapper(node, "preflight", env)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("sealed CVM_RPC_URL chain 1", result.stderr)

    def test_temporary_indexer_image_is_a_hard_deployment_failure(self) -> None:
        node = "indexer-ha-r1"
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            env = wrapper_env(tmp, node)
            env["COMPOSE"] = str(COMPOSE)
            result = run_wrapper(node, "preflight", env)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("temporary production Indexer digest", result.stderr)

    def test_stop_requires_explicit_lb_drain_confirmation(self) -> None:
        node = "indexer-ha-r1"
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            env = wrapper_env(tmp, node)
            result = run_wrapper(node, "stop", env)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("INDEXER_LB_DRAIN_CONFIRMED=1", result.stderr)

    def test_bootstrap_help_documents_exact_cluster_state_handoff(self) -> None:
        node = "indexer-ha-r1"
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            env = wrapper_env(tmp, node, create_state=False)
            result = run_wrapper(node, "bootstrap-help", env)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('capture its "Cluster deployed:" address', result.stdout)
        self.assertIn('capture "new ClusterMember impl:"', result.stdout)
        self.assertIn("save-cluster-state", result.stdout)
        self.assertIn(
            "CHAIN_ID, CLUSTER, MEMBER_IMPL, INDEXER_COMPOSE_HASH", result.stdout
        )

    def test_warmed_candidate_may_keep_grpc_closed_before_registry_rotation(self) -> None:
        node = "indexer-ha-r1"
        status = {
            "chainId": int(CHAIN_ID),
            "pubKey": PUBKEY,
            "codeId": COMPOSE_HASH,
            "identityMode": "cluster-shared",
            "indexerCluster": CLUSTER,
            "servingMemberId": MEMBER_ID,
            "health": {
                "ok": False,
                "reason": "registry identity not active",
                "rpcReachable": True,
                "chainHeadLagBlocks": 2,
                "grpcAccepting": False,
            },
            "readModel": {"clusterCount": 1, "memberCount": 1, "atBlock": 123},
        }
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            env = wrapper_env(tmp, node)
            status_path = tmp / "status.json"
            status_path.write_text(json.dumps(status), encoding="utf-8")
            write_executable(
                tmp / "bin" / "curl", f"#!/bin/sh\ncat {status_path}\n"
            )
            env["INDEXER_VERIFY_ATTEMPTS"] = "1"
            result = run_wrapper(node, "verify-candidate", env)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)["health"]["grpcAccepting"])

    def test_wrapper_has_no_registry_write_and_preflights_bundler(self) -> None:
        wrapper = WRAPPER.read_text(encoding="utf-8")
        self.assertNotIn("setIndexer", wrapper)
        self.assertIn("addAllowedAppId(address)", wrapper)
        self.assertIn("eth_supportedEntryPoints", wrapper)


if __name__ == "__main__":
    unittest.main()
