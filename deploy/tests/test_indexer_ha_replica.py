#!/usr/bin/env python3
"""Focused, host-only checks for the dedicated Indexer replica deployment shape."""

import hashlib
import json
import os
import runpy
import shutil
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
GENERIC = ROOT / "deploy" / "generic-node.sh"
CHAIN_ID = "8453"
CLUSTER = "0x" + ("a" * 40)
SAFE = "0x" + ("b" * 40)
MEMBER_IMPL = "0x" + ("c" * 40)
DSTACK_FACET = "0x" + ("f" * 40)
APP_ID = "0x" + ("d" * 40)
KMS_ROOT = "0x" + ("e" * 40)
COMPOSE_HASH = "0x" + ("1" * 64)
MEMBER_ID = "0x" + ("2" * 64)
PUBKEY = "0x" + ("3" * 64)
MEASURED_NAME = "attestmesh-indexer-ha-replica"
ENTRY_POINT_V07 = "0x0000000071727De22E5E9d8BAf0edAc6f37da032"
OLD_INDEXER_DIGEST = "14e9e3f6869ada585642c14e4e2e43bee197978ffc68d146dd6d3cddbc49d2bb"
STAGE_A_INDEXER_DIGEST = "0d7cbbdb049e1c7606d169ea69f890cf05d3384bc1777c5dac3e1237ebaaa68c"
DSTACK_FACET_CODEHASH = "0x91c3c31fabe7d7c55924bd46873bcb46960c1e5db5fb1e322fc8fb2f1ad76563"
MEMBER_IMPL_CODEHASH = "0xadc979a69cd23526776858fefe6ed0c9e143e7ffbe2f81d715b2ea84468f0f22"
INDEXER_REGISTRY = "0xbC003686943fB957100E517D3CEf66c52B5CDdBf"
GHCR_USER = "test-user"
GHCR_TOKEN = "test-token"
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
            DSTACK_FACET={DSTACK_FACET}
            MEMBER_IMPL={MEMBER_IMPL}
            INDEXER_COMPOSE_HASH={COMPOSE_HASH}
            INDEXER_CLUSTER_OWNER={SAFE}
            KMS_ROOT={KMS_ROOT}
            MEASURED_COMPOSE_NAME={MEASURED_NAME}
            """
        ),
        encoding="utf-8",
    )


def guest_config_fingerprint(env: dict[str, str]) -> str:
    guest_rpc = env.get("CVM_RPC_URL") or env["RPC_URL"]
    guest_bundler = (
        env.get("CVM_BUNDLER_URL")
        or env.get("BUNDLER_URL")
        or env["RPC_URL"]
    )
    fields = [
        "schema=attestmesh.generic-guest.v1",
        f"CHAIN_ID={env.get('CHAIN_ID', CHAIN_ID)}",
        f"RPC_URL={guest_rpc}",
        f"BUNDLER_URL={guest_bundler}",
        f"GAS_POLICY_ID={env.get('GAS_POLICY_ID', '')}",
        f"INDEXER_REGISTRY_ADDR={env.get('INDEXER_REGISTRY_ADDR', INDEXER_REGISTRY)}",
        f"GATEWAY_DOMAIN={env.get('GATEWAY_DOMAIN', '')}",
        f"CLUSTER={env.get('CLUSTER', CLUSTER)}",
        f"MEMBER_IMPL={env.get('MEMBER_IMPL', MEMBER_IMPL)}",
        f"APP_ENV_B64={env.get('APP_ENV_B64', '')}",
        f"DSTACK_DOCKER_USERNAME={GHCR_USER}",
        f"DSTACK_DOCKER_PASSWORD={GHCR_TOKEN}",
        "DSTACK_DOCKER_REGISTRY=ghcr.io",
    ]
    canonical = b"".join(field.encode("utf-8") + b"\0" for field in fields)
    return hashlib.sha256(canonical).hexdigest()


def write_replica_state(
    logdir: Path, node: str, fingerprint: str, *, phase: str = "deployed-stopped"
) -> None:
    (logdir / f"generic-node-{node}.state").write_text(
        textwrap.dedent(
            f"""\
            STATE_SCHEMA=2
            UPDATED_AT=20260714T000000Z
            STATE_PHASE={phase}
            X={APP_ID}
            H={COMPOSE_HASH[2:]}
            VM_ID=test-vm-id
            CLUSTER={CLUSTER}
            MEMBER_IMPL={MEMBER_IMPL}
            KMS_ROOT={KMS_ROOT}
            GATEWAY_DOMAIN=example.invalid
            GUEST_CONFIG_SHA256={fingerprint}
            """
        ),
        encoding="utf-8",
    )


def cast_stub(
    *,
    solidstate_owner: str = SAFE,
    guest_chain_id: str = CHAIN_ID,
    dstack_facet: str = DSTACK_FACET,
    dstack_codehash: str = DSTACK_FACET_CODEHASH,
    member_codehash: str = MEMBER_IMPL_CODEHASH,
    member_id: str = MEMBER_ID,
) -> str:
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
          codehash)
            case "$2" in
              {DSTACK_FACET}) echo {dstack_codehash} ;;
              {MEMBER_IMPL}) echo {member_codehash} ;;
              *) echo "unexpected codehash target: $2" >&2; exit 90 ;;
            esac
            ;;
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
              'facetAddress(bytes4)(address)') echo {dstack_facet} ;;
              'getThreshold()(uint256)') echo 2 ;;
              'allowAnyDevice()(bool)') echo false ;;
              'requireTcbUpToDate()(bool)') echo true ;;
              'allowedComposeHashes(bytes32)(bool)') echo true ;;
              'allowedKmsRoots(address)(bool)') echo true ;;
              'allowedAppIds(address)(bool)') echo true ;;
              'memberIdOf(address)(bytes32)') echo {member_id} ;;
              'memberCount()(uint256)') echo 1 ;;
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
    credentials = tmp / ".teesql"
    credentials.mkdir()
    (credentials / "ghcr-pull.toml").write_text(
        f'username = "{GHCR_USER}"\ntoken = "{GHCR_TOKEN}"\n',
        encoding="utf-8",
    )
    cluster_state = tmp / "indexer-ha-cluster.state"
    compose = tmp / "indexer-ha-replica-node.yaml"
    compose.write_text(COMPOSE.read_text(encoding="utf-8"), encoding="utf-8")
    write_executable(bindir / "cast", cast_stub())
    write_executable(bindir / "curl", "#!/bin/sh\nexit 95\n")
    env = {
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
        "INDEXER_REGISTRY_ADDR": INDEXER_REGISTRY,
        "CHAIN_ID": CHAIN_ID,
        "CLUSTER": CLUSTER,
        "MEMBER_IMPL": MEMBER_IMPL,
        "KMS_ROOT": KMS_ROOT,
    }
    if create_state:
        write_cluster_state(cluster_state)
        write_replica_state(logdir, node, guest_config_fingerprint(env))
    return env


def run_wrapper(
    node: str,
    action: str,
    env: dict[str, str],
    *,
    script: Path = WRAPPER,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(script), node, action],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def run_generic(node: str, action: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(GENERIC), node, action],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def read_state(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines())


def isolated_wrapper_with_fake_drivers(tmp: Path) -> Path:
    deploy_dir = tmp / "isolated-deploy"
    deploy_dir.mkdir()
    wrapper = deploy_dir / WRAPPER.name
    shutil.copy2(WRAPPER, wrapper)
    shutil.copy2(ROOT / "deploy" / "lib.sh", deploy_dir / "lib.sh")
    write_executable(
        deploy_dir / "indexer-lb-node.sh",
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -eu
            printf 'lb|%s|%s|%s|%s\n' "$1" "$2" "$3" \
              "$INDEXER_LB_ACTIVE_OPERATION_ID" >> "$EVENT_LOG"
            case "${LB_PROOF_MODE:-success}" in
              fail) exit 42 ;;
              false)
                printf '{"drained":false,"operation_id":"%s","active_backends":[]}\n' \
                  "$INDEXER_LB_ACTIVE_OPERATION_ID"
                ;;
              mismatch)
                printf '{"drained":true,"operation_id":"%064d","active_backends":[]}\n' 9
                ;;
              success)
                printf '{"drained":true,"operation_id":"%s","active_backends":["10.0.0.2"]}\n' \
                  "$INDEXER_LB_ACTIVE_OPERATION_ID"
                ;;
              *) exit 43 ;;
            esac
            """
        ),
    )
    write_executable(
        deploy_dir / "generic-node.sh",
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -eu
            printf 'generic|%s|%s\n' "$1" "$2" >> "$EVENT_LOG"
            """
        ),
    )
    return wrapper


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

    def test_stop_vm_polls_until_a_literal_terminal_state(self) -> None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as compose:
            compose.write("services:\n  workload:\n    image: example.invalid/test\n")
            compose.flush()
            helper = load_box_helper("worker", "worker", compose.name)

        helper["m"].vmm = mock.Mock(
            side_effect=[
                {"found": True, "info": {"status": "running"}},
                {},
                {"found": True, "info": {"status": "stopping"}},
                {"found": True, "info": {"status": "stopped"}},
            ]
        )
        with mock.patch.dict(
            os.environ,
            {"BOX_STOP_ATTEMPTS": "2", "BOX_STOP_POLL_SECONDS": "0"},
            clear=False,
        ):
            result = helper["stop_vm"]("vm-1")

        self.assertTrue(result["stopped"])
        self.assertEqual(result["status"], "stopped")

    def test_stop_vm_treats_null_found_as_ambiguous(self) -> None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as compose:
            compose.write("services:\n  workload:\n    image: example.invalid/test\n")
            compose.flush()
            helper = load_box_helper("worker", "worker", compose.name)

        helper["m"].vmm = mock.Mock(return_value={"found": None})
        with self.assertRaisesRegex(RuntimeError, "ambiguous found value"):
            helper["stop_vm"]("vm-1")

    def test_stop_vm_does_not_treat_stopping_as_stopped(self) -> None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as compose:
            compose.write("services:\n  workload:\n    image: example.invalid/test\n")
            compose.flush()
            helper = load_box_helper("worker", "worker", compose.name)

        helper["m"].vmm = mock.Mock(
            side_effect=[
                {"found": True, "info": {"status": "running"}},
                {},
                {"found": True, "info": {"status": "stopping"}},
            ]
        )
        with mock.patch.dict(
            os.environ,
            {"BOX_STOP_ATTEMPTS": "1", "BOX_STOP_POLL_SECONDS": "0"},
            clear=False,
        ), self.assertRaisesRegex(RuntimeError, "could not prove"):
            helper["stop_vm"]("vm-1")

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
                    "DSTACK_FACET": DSTACK_FACET,
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

    def test_cluster_must_have_the_persisted_path_a_facet_installed(self) -> None:
        node = "indexer-ha-r1"
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            env = wrapper_env(tmp, node)
            old_facet = "0x" + ("6" * 40)
            write_executable(
                tmp / "bin" / "cast", cast_stub(dstack_facet=old_facet)
            )
            result = run_wrapper(node, "verify-cluster", env)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match prepared Path-A DSTACK_FACET", result.stderr)

    def test_stage_a_implementations_must_have_reviewed_runtime_codehashes(self) -> None:
        cases = [
            (
                {"dstack_codehash": "0x" + ("6" * 64)},
                "DSTACK_FACET runtime code hash",
            ),
            (
                {"member_codehash": "0x" + ("7" * 64)},
                "MEMBER_IMPL runtime code hash",
            ),
        ]
        for overrides, expected_error in cases:
            with self.subTest(expected_error=expected_error), tempfile.TemporaryDirectory() as raw_tmp:
                tmp = Path(raw_tmp)
                env = wrapper_env(tmp, "indexer-ha-r1")
                write_executable(tmp / "bin" / "cast", cast_stub(**overrides))
                result = run_wrapper("indexer-ha-r1", "verify-cluster", env)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn(expected_error, result.stderr)
            self.assertIn("not the reviewed Stage-A build", result.stderr)

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

    def test_cvm_bundler_override_is_preflighted_and_sealed_exactly(self) -> None:
        node = "indexer-ha-r1"
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            env = wrapper_env(tmp, node)
            exact_bundler = "https://cvm-bundler.example.invalid/custom/"
            env["CVM_BUNDLER_URL"] = exact_bundler
            env["SEAL_CAPTURE"] = str(tmp / "sealed-env.txt")
            env["CURL_LOG"] = str(tmp / "curl-urls.txt")
            write_replica_state(
                Path(env["LOGDIR"]), node, guest_config_fingerprint(env)
            )
            write_executable(tmp / "bin" / "scp", "#!/bin/sh\nexit 0\n")
            write_executable(
                tmp / "bin" / "ssh",
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    cat > "$SEAL_CAPTURE"
                    echo {COMPOSE_HASH[2:]}
                    """
                ),
            )
            write_executable(
                tmp / "bin" / "curl",
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    last=""
                    for arg in "$@"; do last="$arg"; done
                    printf '%s\n' "$last" >> "$CURL_LOG"
                    case "$*" in
                      *eth_supportedEntryPoints*)
                        echo '{{"jsonrpc":"2.0","id":1,"result":["{ENTRY_POINT_V07}"]}}'
                        ;;
                      *eth_chainId*) echo '{{"jsonrpc":"2.0","id":1,"result":"0x2105"}}' ;;
                      *) exit 96 ;;
                    esac
                    """
                ),
            )
            result = run_wrapper(node, "preflight", env)

            sealed = (tmp / "sealed-env.txt").read_text(encoding="utf-8")
            urls = (tmp / "curl-urls.txt").read_text(encoding="utf-8").splitlines()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(urls, [exact_bundler, exact_bundler])
        self.assertIn(f"E_BUNDLER_URL={exact_bundler}", sealed)

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

    def test_legacy_non_shared_indexer_image_is_a_hard_deployment_failure(self) -> None:
        node = "indexer-ha-r1"
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            env = wrapper_env(tmp, node)
            legacy_compose = tmp / "legacy-indexer.yaml"
            legacy_compose.write_text(
                COMPOSE.read_text(encoding="utf-8").replace(
                    STAGE_A_INDEXER_DIGEST, OLD_INDEXER_DIGEST
                ),
                encoding="utf-8",
            )
            env["COMPOSE"] = str(legacy_compose)
            result = run_wrapper(node, "preflight", env)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("reviewed Stage-A sidecar and Indexer OCI digests", result.stderr)

    def test_save_cluster_state_rejects_a_supplied_unrendered_hash(self) -> None:
        node = "indexer-ha-r1"
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            env = wrapper_env(tmp, node, create_state=False)
            env.update(
                {
                    "DSTACK_FACET": DSTACK_FACET,
                    "INDEXER_COMPOSE_HASH": COMPOSE_HASH,
                    "INDEXER_CLUSTER_OWNER": SAFE,
                }
            )
            write_executable(tmp / "bin" / "scp", "#!/bin/sh\nexit 0\n")
            write_executable(
                tmp / "bin" / "ssh",
                "#!/bin/sh\ncat >/dev/null\necho " + ("9" * 64) + "\n",
            )
            result = run_wrapper(node, "save-cluster-state", env)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rendered compose hash", result.stderr)
        self.assertIn("does not match signer-cluster hash", result.stderr)
        self.assertFalse(Path(env["INDEXER_HA_CLUSTER_STATE"]).exists())

    def test_registered_stop_requires_explicit_valid_lb_proof_inputs(self) -> None:
        node = "indexer-ha-r1"
        operation = "a" * 64
        cases = [
            ({}, "explicit INDEXER_LB_NODE"),
            (
                {"INDEXER_LB_NODE": "attestmesh-indexer-lb"},
                "exactly 64 lowercase hex characters",
            ),
            (
                {
                    "INDEXER_LB_NODE": "../indexer-lb",
                    "INDEXER_LB_ACTIVE_OPERATION_ID": operation,
                },
                "safe node name",
            ),
            (
                {
                    "INDEXER_LB_NODE": "attestmesh-indexer-lb",
                    "INDEXER_LB_ACTIVE_OPERATION_ID": "A" * 64,
                },
                "exactly 64 lowercase hex characters",
            ),
            (
                {
                    "INDEXER_LB_NODE": "attestmesh-indexer-lb",
                    "INDEXER_LB_ACTIVE_OPERATION_ID": "0x" + operation,
                },
                "exactly 64 lowercase hex characters",
            ),
        ]
        for updates, expected_error in cases:
            with self.subTest(expected_error=expected_error), tempfile.TemporaryDirectory() as raw_tmp:
                tmp = Path(raw_tmp)
                env = wrapper_env(tmp, node)
                env.update(updates)
                result = run_wrapper(node, "stop", env)
                state = read_state(
                    Path(env["LOGDIR"]) / f"generic-node-{node}.state"
                )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn(expected_error, result.stderr)
            self.assertEqual(state["STATE_PHASE"], "deployed-stopped")

    def test_registered_stop_passes_pinned_generation_before_stop(self) -> None:
        node = "indexer-ha-r1"
        operation = "a" * 64
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            env = wrapper_env(tmp, node)
            wrapper = isolated_wrapper_with_fake_drivers(tmp)
            events = tmp / "events.log"
            env.update(
                {
                    "EVENT_LOG": str(events),
                    "INDEXER_LB_NODE": "attestmesh-indexer-lb",
                    "INDEXER_LB_ACTIVE_OPERATION_ID": operation,
                }
            )
            result = run_wrapper(node, "stop", env, script=wrapper)
            recorded = events.read_text(encoding="utf-8").splitlines()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            recorded,
            [
                f"lb|attestmesh-indexer-lb|assert-drained|{node}|{operation}",
                f"generic|{node}|stop",
            ],
        )
        self.assertIn("authenticated LB drain proof accepted", result.stderr)

    def test_registered_stop_fails_closed_when_lb_proof_fails(self) -> None:
        node = "indexer-ha-r1"
        operation = "a" * 64
        cases = [
            ("fail", "authenticated Indexer LB drain proof failed"),
            ("false", "invalid or mismatched drain proof"),
            ("mismatch", "invalid or mismatched drain proof"),
        ]
        for mode, expected_error in cases:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as raw_tmp:
                tmp = Path(raw_tmp)
                env = wrapper_env(tmp, node)
                wrapper = isolated_wrapper_with_fake_drivers(tmp)
                events = tmp / "events.log"
                env.update(
                    {
                        "EVENT_LOG": str(events),
                        "LB_PROOF_MODE": mode,
                        "INDEXER_LB_NODE": "attestmesh-indexer-lb",
                        "INDEXER_LB_ACTIVE_OPERATION_ID": operation,
                    }
                )
                result = run_wrapper(node, "stop", env, script=wrapper)
                recorded = events.read_text(encoding="utf-8").splitlines()

            self.assertNotEqual(result.returncode, 0)
            self.assertIn(expected_error, result.stderr)
            self.assertEqual(
                recorded,
                [f"lb|attestmesh-indexer-lb|assert-drained|{node}|{operation}"],
            )

    def test_never_registered_candidate_can_be_cleaned_without_lb_drain(self) -> None:
        node = "indexer-ha-r1"
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            env = wrapper_env(tmp, node)
            env["GATEWAY_DOMAIN"] = "changed-after-failed-deploy.example.invalid"
            write_executable(
                tmp / "bin" / "cast", cast_stub(member_id="0x" + ("0" * 64))
            )
            write_executable(tmp / "bin" / "scp", "#!/bin/sh\nexit 0\n")
            write_executable(
                tmp / "bin" / "ssh",
                '#!/bin/sh\necho \'{"vm_id":"test-vm-id","found":true,"stopped":true,"status":"stopped"}\'\n',
            )
            result = run_wrapper(node, "stop", env)
            state_path = Path(env["LOGDIR"]) / f"generic-node-{node}.state"
            state = read_state(state_path)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "LB drain confirmation is not required", result.stdout + result.stderr
        )
        self.assertEqual(state["STATE_PHASE"], "cleaned")
        self.assertEqual(state["STATE_SCHEMA"], "2")

    def test_cleanup_does_not_mark_state_cleaned_on_ambiguous_stop(self) -> None:
        node = "indexer-ha-r1"
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            env = wrapper_env(tmp, node)
            write_executable(
                tmp / "bin" / "cast", cast_stub(member_id="0x" + ("0" * 64))
            )
            write_executable(tmp / "bin" / "scp", "#!/bin/sh\nexit 0\n")
            write_executable(
                tmp / "bin" / "ssh",
                '#!/bin/sh\necho \'{"vm_id":"test-vm-id","found":true,"stopped":false,"status":"stopping"}\'\n',
            )
            result = run_wrapper(node, "stop", env)
            state = read_state(
                Path(env["LOGDIR"]) / f"generic-node-{node}.state"
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "keeping state for manual follow-up", result.stdout + result.stderr
        )
        self.assertEqual(state["STATE_PHASE"], "deployed-stopped")

    def test_replica_state_is_never_evaluated_as_shell(self) -> None:
        node = "indexer-ha-r1"
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            env = wrapper_env(tmp, node)
            state_path = Path(env["LOGDIR"]) / f"generic-node-{node}.state"
            marker = tmp / "state-was-evaluated"
            state_path.write_text(
                state_path.read_text(encoding="utf-8").replace(
                    "VM_ID=test-vm-id", f"VM_ID=$(touch {marker})"
                ),
                encoding="utf-8",
            )
            result = run_wrapper(node, "safe-admission", env)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("VM_ID contains unsupported characters", result.stderr)
        self.assertFalse(marker.exists())

    def test_replica_state_rejects_unknown_and_duplicate_fields(self) -> None:
        node = "indexer-ha-r1"
        cases = [
            ("UNREVIEWED=value\n", "unknown replica state field"),
            ("VM_ID=second-vm\n", "duplicate replica state field"),
        ]
        for suffix, expected_error in cases:
            with self.subTest(expected_error=expected_error), tempfile.TemporaryDirectory() as raw_tmp:
                tmp = Path(raw_tmp)
                env = wrapper_env(tmp, node)
                state_path = Path(env["LOGDIR"]) / f"generic-node-{node}.state"
                with state_path.open("a", encoding="utf-8") as state:
                    state.write(suffix)
                result = run_wrapper(node, "safe-admission", env)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn(expected_error, result.stderr)

    def test_later_worker_actions_fail_on_sealed_configuration_drift(self) -> None:
        node = "indexer-ha-r1"
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            env = wrapper_env(tmp, node)
            env["GAS_POLICY_ID"] = "changed-after-deploy"
            result = run_wrapper(node, "verify-candidate", env)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("sealed guest configuration drifted", result.stderr)

    def test_remote_deploy_identifiers_are_validated_before_persistence(self) -> None:
        node = "indexer-ha-r1"
        cases = [
            (
                {"app_id": "0x123", "compose_hash": COMPOSE_HASH[2:], "vm_id": "vm-1"},
                "not an address",
            ),
            (
                {"app_id": APP_ID, "compose_hash": "not-a-hash", "vm_id": "vm-1"},
                "invalid compose_hash",
            ),
            (
                {"app_id": APP_ID, "compose_hash": COMPOSE_HASH[2:], "vm_id": "$(touch marker)"},
                "invalid VM_ID",
            ),
        ]
        for response, expected_error in cases:
            with self.subTest(expected_error=expected_error), tempfile.TemporaryDirectory() as raw_tmp:
                tmp = Path(raw_tmp)
                env = wrapper_env(tmp, node, create_state=False)
                payload = json.dumps(response)
                write_executable(tmp / "bin" / "scp", "#!/bin/sh\nexit 0\n")
                write_executable(
                    tmp / "bin" / "ssh",
                    f"#!/bin/sh\ncat >/dev/null\nprintf '%s\\n' '{payload}'\n",
                )
                result = run_generic(node, "deploy", env)
                state = read_state(
                    Path(env["LOGDIR"]) / f"generic-node-{node}.state"
                )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn(expected_error, result.stderr)
            self.assertEqual(state["X"], "")
            self.assertEqual(state["H"], "")
            self.assertEqual(state["VM_ID"], "")

    def test_bootstrap_help_documents_exact_cluster_state_handoff(self) -> None:
        node = "indexer-ha-r1"
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            env = wrapper_env(tmp, node, create_state=False)
            result = run_wrapper(node, "bootstrap-help", env)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('capture its "Cluster deployed:" address', result.stdout)
        self.assertIn(
            'deploy/onchain.sh patha-safe-prepare "$CLUSTER" "$INDEXER_CLUSTER_OWNER"',
            result.stdout,
        )
        self.assertIn(".dstackFacet", result.stdout)
        self.assertIn(".clusterMemberImplementation", result.stdout)
        self.assertIn("save-cluster-state", result.stdout)
        self.assertIn(
            "CHAIN_ID, CLUSTER, DSTACK_FACET, MEMBER_IMPL", result.stdout
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
        self.assertNotIn("INDEXER_LB_DRAIN_CONFIRMED", wrapper)
        self.assertIn("addAllowedAppId(address)", wrapper)
        self.assertIn(
            '"$HERE/indexer-lb-node.sh" "$INDEXER_LB_NODE" assert-drained "$NODE"',
            wrapper,
        )
        self.assertIn("eth_supportedEntryPoints", wrapper)


if __name__ == "__main__":
    unittest.main()
