#!/usr/bin/env python3
"""Host-only tests for operator-submitted Indexer member registration calldata."""

import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "indexer-member-node.sh"
CHAIN_ID = "8453"
NODE = "indexer-ha-r1"
CLUSTER = "0x" + ("a" * 40)
MEMBER = "0x" + ("b" * 40)
MEMBER_IMPL = "0x" + ("c" * 40)
KMS_ROOT = "0x" + ("d" * 40)
CODE_ID = MEMBER.lower() + ("0" * 24)
X_PUB = "0x" + ("1" * 64)
WG_PUB = "0x" + ("2" * 64)
MESSAGE_HASH = "0x" + ("3" * 64)
SIGNATURE = (
    "dstack_register((bytes32,bytes32,bytes,bytes,bytes,bytes,bytes,string),"
    "address,bytes32,bytes32)"
)


def word(value: int) -> str:
    return f"{value:064x}"


def dynamic_bytes(value: bytes) -> str:
    padding = (-len(value)) % 32
    return word(len(value)) + value.hex() + ("00" * padding)


def registration_calldata() -> str:
    # Four argument words, followed by the eight-word DstackProof tuple and its
    # six dynamic tails. Empty signatures are sufficient for ABI decoding; the
    # contract remains responsible for cryptographic proof verification.
    argument_head = (
        word(0x80)
        + ("0" * 24)
        + MEMBER[2:]
        + X_PUB[2:]
        + WG_PUB[2:]
    )
    proof_head = CODE_ID[2:] + MESSAGE_HASH[2:]
    proof_head += "".join(word(offset) for offset in (0x100, 0x120, 0x140, 0x160, 0x180, 0x1A0))
    proof_tail = (dynamic_bytes(b"") * 5) + dynamic_bytes(b"ethereum")
    return "0x537d491c" + argument_head + proof_head + proof_tail


def decoded_payload(member: str = MEMBER) -> list:
    return [
        [CODE_ID, MESSAGE_HASH, "0x", "0x", "0x", "0x", "0x", "ethereum"],
        member,
        X_PUB,
        WG_PUB,
    ]


def payload(**updates: str) -> dict[str, str]:
    value = {
        "cluster": CLUSTER,
        "member": MEMBER,
        "owner": "0x" + ("e" * 40),
        "codeId": CODE_ID,
        "xPubKey": X_PUB,
        "wgPubKey": WG_PUB,
        "calldata": registration_calldata(),
    }
    value.update(updates)
    return value


def write_state(logdir: Path, *, x_value: str = MEMBER, duplicate_x: bool = False) -> Path:
    state = logdir / f"generic-node-{NODE}.state"
    duplicate = f"X={MEMBER}\n" if duplicate_x else ""
    state.write_text(
        textwrap.dedent(
            f"""\
            UPDATED_AT=20260714T000000Z
            STATE_PHASE=deployed-stopped
            X={x_value}
            {duplicate}H={"4" * 64}
            VM_ID=test-vm-id
            CLUSTER={CLUSTER}
            MEMBER_IMPL={MEMBER_IMPL}
            KMS_ROOT={KMS_ROOT}
            GATEWAY_DOMAIN=example.invalid
            GUEST_CONFIG_SHA256={"5" * 64}
            """
        ),
        encoding="utf-8",
    )
    state.chmod(0o600)
    return state


def write_cast_stub(path: Path, expected_calldata: str, decoded: list) -> None:
    response = path.parent / "decoded.json"
    response.write_text(json.dumps(decoded), encoding="utf-8")
    path.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            [ "$1" = decode-calldata ] || exit 91
            [ "$2" = --json ] || exit 92
            [ "$3" = '{SIGNATURE}' ] || exit 93
            [ "$4" = '{expected_calldata}' ] || exit 94
            cat '{response}'
            """
        ),
        encoding="utf-8",
    )
    path.chmod(0o755)


def run_validation(tmp: Path, value: dict[str, str], decoded: list) -> subprocess.CompletedProcess:
    logdir = tmp / "logs"
    bindir = tmp / "bin"
    logdir.mkdir(exist_ok=True)
    bindir.mkdir(exist_ok=True)
    if not (logdir / f"generic-node-{NODE}.state").exists():
        write_state(logdir)
    payload_file = tmp / "payload.json"
    payload_file.write_text(json.dumps(value), encoding="utf-8")
    write_cast_stub(bindir / "cast", value["calldata"], decoded)
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "HOME": str(tmp),
        "LOGDIR": str(logdir),
        "RPC_URL": "https://rpc.invalid",
        "CHAIN_ID": CHAIN_ID,
        "PRIVATE_KEY": "0x" + ("6" * 64),
        "DEPLOYER_ADDR": "0x" + ("7" * 40),
        "KMS_ROOT": KMS_ROOT,
    }
    return subprocess.run(
        [str(SCRIPT), NODE, "validate-registration-payload", str(payload_file)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


class IndexerMemberRegistrationTests(unittest.TestCase):
    def test_exact_payload_and_decoded_calldata_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            result = run_validation(Path(raw_tmp), payload(), decoded_payload())

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("canonical dstack_register calldata", result.stderr)

    def test_calldata_member_must_match_verified_payload(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            wrong_member = "0x" + ("8" * 40)
            result = run_validation(
                Path(raw_tmp), payload(), decoded_payload(member=wrong_member)
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("calldata member does not match", result.stderr)

    def test_payload_cluster_must_match_operator_cluster(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            result = run_validation(
                Path(raw_tmp),
                payload(cluster="0x" + ("9" * 40)),
                decoded_payload(),
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("expected CLUSTER", result.stderr)

    def test_state_data_is_never_executed(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            logdir = tmp / "logs"
            logdir.mkdir()
            marker = tmp / "executed"
            write_state(logdir, x_value=f"$(touch {marker})")
            result = run_validation(tmp, payload(), decoded_payload())

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(marker.exists())
        self.assertIn("state X is not an address", result.stderr)

    def test_duplicate_state_fields_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            logdir = tmp / "logs"
            logdir.mkdir()
            write_state(logdir, duplicate_x=True)
            result = run_validation(tmp, payload(), decoded_payload())

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exactly one X field", result.stderr)


if __name__ == "__main__":
    unittest.main()
