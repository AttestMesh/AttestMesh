#!/usr/bin/env python3
"""Pure tests for the Python controller embedded in indexer-lb-node.yaml."""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "deploy" / "compose" / "indexer-lb-node.yaml"
ADMIN_KEY = "test-admin-key"
LB_CLUSTER = "0x" + ("a" * 40)


def controller_source() -> str:
    text = COMPOSE.read_text(encoding="utf-8")
    marker = "        cat >/tmp/indexer_lb_controller.py <<'PY'\n"
    start = text.index(marker) + len(marker)
    end = text.index("\n        PY\n", start)
    return "\n".join(
        line[8:] if line.startswith("        ") else line
        for line in text[start:end].splitlines()
    )


def load_controller(*, include_handler: bool = False) -> dict:
    source = controller_source()
    boundary = "for _ in range(120):" if include_handler else "class Handler"
    namespace: dict = {}
    with mock.patch.dict(
        os.environ,
        {
            "INDEXER_LB_ADMIN_KEY": ADMIN_KEY,
            "INDEXER_LB_CLUSTER": LB_CLUSTER,
        },
    ):
        exec(source[: source.index(boundary)], namespace)
    return namespace


class ControllerPoolTests(unittest.TestCase):
    def test_embedded_controller_compiles(self) -> None:
        compile(controller_source(), "indexer_lb_controller.py", "exec")

    def test_shared_pool_requires_consensus_and_dedicated_cluster(self) -> None:
        controller = load_controller()
        pubkey = "0x" + ("1" * 64)
        code_id = "0x" + ("2" * 64)
        cluster = "0x" + ("b" * 40)
        members = {
            "10.0.0.2": "0x" + ("3" * 64),
            "10.0.0.3": "0x" + ("4" * 64),
        }

        def probe(ip: str, expected_pubkey: str, **kwargs) -> dict:
            self.assertEqual(expected_pubkey, pubkey)
            self.assertTrue(kwargs["shared_ha"])
            self.assertEqual(kwargs["expected_code_id"], code_id)
            self.assertEqual(kwargs["expected_cluster"], cluster)
            self.assertFalse(kwargs["require_serving"])
            return {
                "member_id": members[ip],
                "pubkey": pubkey,
                "code_id": code_id,
                "cluster": cluster,
            }

        controller["probe_backend"] = probe
        prepared, probes = controller["prepare_from_body"](
            {
                "backends": list(members),
                "expected_pubkey": pubkey,
                "expected_code_id": code_id,
                "expected_cluster": cluster,
            }
        )
        self.assertTrue(prepared["shared_ha"])
        self.assertEqual(prepared["members"], list(members.values()))
        self.assertEqual(len(probes), 2)

        with self.assertRaisesRegex(ValueError, "dedicated cluster"):
            controller["prepare_from_body"](
                {
                    "backends": list(members),
                    "expected_pubkey": pubkey,
                    "expected_code_id": code_id,
                    "expected_cluster": LB_CLUSTER,
                }
            )

        members["10.0.0.3"] = members["10.0.0.2"]
        with self.assertRaisesRegex(RuntimeError, "distinct servingMemberIds"):
            controller["prepare_from_body"](
                {
                    "backends": list(members),
                    "expected_pubkey": pubkey,
                    "expected_code_id": code_id,
                    "expected_cluster": cluster,
                }
            )

    def test_partial_commit_restores_pool_and_evicts_candidate_sessions(self) -> None:
        controller = load_controller(include_handler=True)
        with tempfile.TemporaryDirectory() as tmp:
            for key, filename in {
                "ACTIVE_BACKEND_PATH": "active_backend",
                "ACTIVE_PUBKEY_PATH": "active_pubkey",
                "ACTIVE_CODE_ID_PATH": "active_code_id",
                "ACTIVE_CLUSTER_PATH": "active_cluster",
                "ACTIVE_MEMBERS_PATH": "active_members",
                "PREPARED_PATH": "prepared.json",
            }.items():
                controller[key] = str(Path(tmp) / filename)

            old = {
                "backends": ["10.0.0.1"],
                "pubkey": "0x" + ("1" * 64),
                "code_id": "0x" + ("2" * 64),
                "cluster": "",
                "members": [],
            }
            controller["persist_active"](
                old["backends"], old["pubkey"], code_id=old["code_id"]
            )
            controller["write_prepared"](
                {
                    "backends": ["10.0.0.2", "10.0.0.3"],
                    "pubkey": "0x" + ("3" * 64),
                    "code_id": "0x" + ("4" * 64),
                    "cluster": "0x" + ("5" * 40),
                    "members": ["0x" + ("6" * 64), "0x" + ("7" * 64)],
                    "shared_ha": True,
                    "previous": old,
                    "commit_started": False,
                }
            )
            controller["wait_until_serving"] = lambda _prepared: []

            commands: list[str] = []
            failed = False

            def haproxy(command: str) -> str:
                nonlocal failed
                commands.append(command)
                if (
                    command.startswith(
                        "set server indexer_grpc_active/worker2 addr 10.0.0.3"
                    )
                    and not failed
                ):
                    failed = True
                    raise RuntimeError("injected partial slot failure")
                return ""

            controller["haproxy_cmd"] = haproxy
            server = controller["ThreadingHTTPServer"](
                ("127.0.0.1", 0), controller["Handler"]
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/commit",
                data=b"{}",
                headers={
                    "Authorization": f"Bearer {ADMIN_KEY}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(request)
                with raised.exception as error:
                    self.assertEqual(error.code, 400)
                    self.assertIn(
                        "injected partial slot failure",
                        json.load(error)["error"],
                    )
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

            self.assertEqual(controller["active_state"](), old)
            self.assertTrue(controller["read_prepared"]()["commit_started"])
            failure_index = commands.index(
                "set server indexer_grpc_active/worker2 addr 10.0.0.3 port 50052"
            )
            restored = commands.index(
                "set server indexer_grpc_active/worker1 addr 10.0.0.1 port 50052",
                failure_index + 1,
            )
            candidate_closed = commands.index(
                "shutdown sessions server indexer_grpc_active/worker2",
                failure_index + 1,
            )
            reopened = commands.index("enable frontend indexer_grpc", failure_index + 1)
            self.assertLess(restored, candidate_closed)
            self.assertLess(candidate_closed, reopened)


if __name__ == "__main__":
    unittest.main()
