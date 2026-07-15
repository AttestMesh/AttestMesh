#!/usr/bin/env python3
"""Pure tests for the Python controller embedded in indexer-lb-node.yaml."""

import json
import http.client
import os
import csv
import io
import shutil
import socket
import subprocess
import tempfile
import textwrap
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "deploy" / "compose" / "indexer-lb-node.yaml"
DRIVER = ROOT / "deploy" / "indexer-lb-node.sh"
ADMIN_KEY = "test-admin-key"
LB_CLUSTER = "0x" + ("a" * 40)
OPERATION_1 = "1" * 64
OPERATION_2 = "2" * 64
HAPROXY_IMAGE = (
    "haproxy@sha256:dee54db8b27cd6c21519ea4f0ba0604f3742e8e7369bc16fb5b69133dec3f47f"
)


def controller_source() -> str:
    text = COMPOSE.read_text(encoding="utf-8")
    marker = "        cat >/tmp/indexer_lb_controller.py <<'PY'\n"
    start = text.index(marker) + len(marker)
    end = text.index("\n        PY\n", start)
    return "\n".join(
        line[8:] if line.startswith("        ") else line
        for line in text[start:end].splitlines()
    )


def load_controller(
    *, include_handler: bool = False, fleet_confirmed: bool = True
) -> dict:
    source = controller_source()
    boundary = "for _ in range(120):" if include_handler else "class Handler"
    namespace: dict = {}
    with mock.patch.dict(
        os.environ,
        {
            "INDEXER_LB_ADMIN_KEY": ADMIN_KEY,
            "INDEXER_LB_CLUSTER": LB_CLUSTER,
            "INDEXER_PROTOCOL_V3_FLEET_CONFIRMED": "1" if fleet_confirmed else "0",
        },
    ):
        exec(source[: source.index(boundary)], namespace)
    return namespace


def point_state_at(controller: dict, directory: str) -> None:
    for key, filename in {
        "ACTIVE_BACKEND_PATH": "active_backend",
        "ACTIVE_PUBKEY_PATH": "active_pubkey",
        "ACTIVE_CODE_ID_PATH": "active_code_id",
        "ACTIVE_CLUSTER_PATH": "active_cluster",
        "ACTIVE_MEMBERS_PATH": "active_members",
        "ACTIVE_OPERATION_PATH": "active_operation",
        "ACTIVE_STATE_PATH": "active.json",
        "PREPARED_PATH": "prepared.json",
        "DRAIN_RESERVATIONS_PATH": "drain-reservations.json",
    }.items():
        controller[key] = str(Path(directory) / filename)


class ControllerPoolTests(unittest.TestCase):
    def test_embedded_controller_compiles(self) -> None:
        compile(controller_source(), "indexer_lb_controller.py", "exec")
        source = controller_source()
        self.assertIn("option httpchk GET /healthz", COMPOSE.read_text())
        self.assertIn("check port 9090 disabled", COMPOSE.read_text())
        self.assertIn("on-marked-down shutdown-sessions", COMPOSE.read_text())

    def test_lb_compose_renders_with_sealed_environment(self) -> None:
        if not shutil.which("docker"):
            self.skipTest("docker is unavailable")
        version = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        if version.returncode != 0:
            self.skipTest("docker compose is unavailable")
        environment = {
            **os.environ,
            "APP_ENV_B64": "SU5ERVhFUl9MQl9BRE1JTl9LRVk9dGVzdAo=",
            "BUNDLER_URL": "https://bundler.invalid",
            "CHAIN_ID": "8453",
            "CLUSTER": "0x" + ("1" * 40),
            "GAS_POLICY_ID": "test-policy",
            "GATEWAY_DOMAIN": "gateway.invalid",
            "INDEXER_REGISTRY_ADDR": "0x" + ("2" * 40),
            "RPC_URL": "https://rpc.invalid",
        }
        rendered = subprocess.run(
            ["docker", "compose", "-f", str(COMPOSE), "config", "--quiet"],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(rendered.returncode, 0, rendered.stderr)

    def test_haproxy_cli_errors_are_not_treated_as_success(self) -> None:
        controller = load_controller()

        class FakeSocket:
            replies = [b"No such server.\n", b""]

            def settimeout(self, _timeout):
                pass

            def connect(self, _path):
                pass

            def sendall(self, _data):
                pass

            def shutdown(self, _how):
                pass

            def recv(self, _size):
                return self.replies.pop(0)

            def close(self):
                pass

        with mock.patch.object(controller["socket"], "socket", return_value=FakeSocket()):
            with self.assertRaisesRegex(RuntimeError, "No such server"):
                controller["haproxy_cmd"]("enable server missing/worker1")

    def test_runtime_pool_verification_rejects_slot_drift(self) -> None:
        controller = load_controller()

        def stats(*, drift: bool = False) -> str:
            rows = ["# pxname,svname,status,addr"]
            for backend, port in (
                ("indexer_grpc_active", 50052),
                ("indexer_http_active", 9090),
            ):
                for index in range(1, 9):
                    active = index <= 2
                    address = (
                        f"10.0.0.{index + 1}:{port}"
                        if active
                        else "127.0.0.1:1"
                    )
                    if drift and backend == "indexer_grpc_active" and index == 2:
                        address = f"10.0.0.99:{port}"
                    rows.append(
                        f"{backend},worker{index},{'UP' if active else 'MAINT'},"
                        f"{address}"
                    )
            return "\n".join(rows) + "\n"

        controller["haproxy_cmd"] = lambda _command: stats()
        controller["verify_runtime_pool"](["10.0.0.2", "10.0.0.3"])
        controller["haproxy_cmd"] = lambda _command: stats(drift=True)
        with self.assertRaisesRegex(RuntimeError, "runtime address mismatch"):
            controller["verify_runtime_pool"](["10.0.0.2", "10.0.0.3"])

    def test_backend_addresses_reject_self_loops_and_non_rfc1918(self) -> None:
        controller = load_controller()
        self.assertEqual(controller["validate_backend"]("10.0.0.2"), "10.0.0.2")
        for invalid in ("127.0.0.1", "0.0.0.0", "169.254.1.1", "224.0.0.1"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                controller["validate_backend"](invalid)
        controller["local_ipv4_addresses"] = lambda: {"10.0.0.9"}
        with self.assertRaisesRegex(ValueError, "LB itself"):
            controller["validate_backend"]("10.0.0.9")

    def test_bounded_backend_http_rejects_oversize_and_redirect(self) -> None:
        controller = load_controller()

        class Headers:
            def __init__(self, values):
                self.values = values

            def get_all(self, _name, _default=None):
                return self.values

        class Response:
            def __init__(self, values, body=b"{}"):
                self.headers = Headers(values)
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, limit):
                return self.body[:limit]

        controller["BACKEND_HTTP"] = mock.Mock()
        controller["BACKEND_HTTP"].open.return_value = Response(["65537"])
        with self.assertRaisesRegex(RuntimeError, "too large"):
            controller["bounded_backend_get"]("10.0.0.2", "/status")
        controller["BACKEND_HTTP"].open.return_value = Response(
            [], b"x" * 65537
        )
        with self.assertRaisesRegex(RuntimeError, "exceeded 64KiB"):
            controller["bounded_backend_get"]("10.0.0.2", "/status")
        controller["BACKEND_HTTP"].open.side_effect = urllib.error.HTTPError(
            "http://10.0.0.2:9090/status", 302, "redirect", {}, None
        )
        with self.assertRaises(urllib.error.HTTPError):
            controller["bounded_backend_get"]("10.0.0.2", "/status")

    def test_backend_identity_opener_ignores_ambient_proxies(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "HTTP_PROXY": "http://127.0.0.1:1",
                "HTTPS_PROXY": "http://127.0.0.1:2",
                "http_proxy": "http://127.0.0.1:3",
                "https_proxy": "http://127.0.0.1:4",
                "NO_PROXY": "",
                "no_proxy": "",
            },
        ):
            controller = load_controller()
        proxy_configs = [
            handler.proxies
            for handler in controller["BACKEND_HTTP"].handlers
            if hasattr(handler, "proxies")
        ]
        self.assertTrue(all(config == {} for config in proxy_configs))
        self.assertIn(
            "urllib.request.ProxyHandler({}), RejectRedirects()",
            controller_source(),
        )

    def test_shared_restart_tolerates_only_identity_bound_degradation(self) -> None:
        controller = load_controller()
        state = {
            "backends": ["10.0.0.2", "10.0.0.3"],
            "pubkey": "0x" + ("1" * 64),
            "code_id": "0x" + ("2" * 64),
            "cluster": "0x" + ("b" * 40),
            "members": ["0x" + ("3" * 64), "0x" + ("4" * 64)],
            "operation_id": OPERATION_1,
        }

        def identity(ip, *_args, **_kwargs):
            if ip == "10.0.0.2":
                raise OSError("down")
            return {"status": {"health": {"ok": True, "grpcAccepting": True}}}

        controller["probe_backend"] = identity
        controller["bounded_backend_get"] = lambda *_args: b'{"status":"ok"}'
        controller["socket"].create_connection = mock.Mock(
            return_value=mock.MagicMock()
        )
        self.assertEqual(
            controller["validate_active_before_reopen"](
                state, tolerate_unavailable=True
            ),
            ["10.0.0.3"],
        )

        def mismatch(ip, *_args, **_kwargs):
            if ip == "10.0.0.2":
                raise controller["BackendIdentityError"]("reused address")
            return {"status": {"health": {"ok": True, "grpcAccepting": True}}}

        controller["probe_backend"] = mismatch
        with self.assertRaisesRegex(controller["BackendIdentityError"], "reused"):
            controller["validate_active_before_reopen"](
                state, tolerate_unavailable=True
            )

        controller["probe_backend"] = lambda *_args, **_kwargs: {
            "status": {"health": {"ok": True, "grpcAccepting": True}}
        }

        def health(ip, _path):
            if ip == "10.0.0.2":
                raise urllib.error.HTTPError(
                    "http://10.0.0.2:9090/healthz", 503, "down", {}, None
                )
            return b'{"status":"ok"}'

        controller["bounded_backend_get"] = health
        self.assertEqual(
            controller["validate_active_before_reopen"](
                state, tolerate_unavailable=True
            ),
            ["10.0.0.3"],
        )

    def test_periodic_identity_refresh_reenables_recovered_slots(self) -> None:
        controller = load_controller()
        state = {
            "backends": ["10.0.0.2", "10.0.0.3"],
            "pubkey": "0x" + ("1" * 64),
            "code_id": "0x" + ("2" * 64),
            "cluster": "0x" + ("b" * 40),
            "members": ["0x" + ("3" * 64), "0x" + ("4" * 64)],
            "operation_id": OPERATION_1,
        }
        calls = []
        controller["socket_generation"] = lambda: (1, 2, 3)
        controller["read_prepared"] = lambda: {}
        controller["active_state"] = lambda: state
        controller["validate_active_before_reopen"] = (
            lambda _state, **_kwargs: ["10.0.0.2", "10.0.0.3"]
        )
        controller["set_pool"] = lambda backends, **kwargs: calls.append(
            (list(backends), kwargs["enabled_backends"])
        )
        controller["frontends"] = lambda enabled: calls.append(("frontends", enabled))
        with mock.patch.object(
            controller["time"], "monotonic", side_effect=[0, 16, 16]
        ), mock.patch.object(
            controller["time"], "sleep", side_effect=SystemExit
        ):
            with self.assertRaises(SystemExit):
                controller["watch_haproxy"]((1, 2, 3))
        self.assertIn(
            (["10.0.0.2", "10.0.0.3"], ["10.0.0.2", "10.0.0.3"]),
            calls,
        )

    def test_stale_refresh_failure_cannot_quarantine_new_generation(self) -> None:
        controller = load_controller()
        old = {
            "backends": ["10.0.0.2", "10.0.0.3"],
            "pubkey": "0x" + ("1" * 64),
            "code_id": "0x" + ("2" * 64),
            "cluster": "0x" + ("b" * 40),
            "members": ["0x" + ("3" * 64), "0x" + ("4" * 64)],
            "operation_id": OPERATION_1,
        }
        new = {
            **old,
            "backends": ["10.0.0.8"],
            "members": [],
            "cluster": "",
            "operation_id": OPERATION_2,
        }
        current = {"state": old}
        quarantined = []
        controller["socket_generation"] = lambda: (1, 2, 3)
        controller["read_prepared"] = lambda: {}
        controller["active_state"] = lambda: current["state"]

        def stale_probe(_state, **_kwargs):
            current["state"] = new
            raise controller["BackendIdentityError"]("old generation failed")

        controller["validate_active_before_reopen"] = stale_probe
        controller["quarantine_runtime_pool"] = lambda: quarantined.append(True)
        with mock.patch.object(
            controller["time"], "monotonic", side_effect=[0, 16, 16]
        ), mock.patch.object(
            controller["time"], "sleep", side_effect=SystemExit
        ):
            with self.assertRaises(SystemExit):
                controller["watch_haproxy"]((1, 2, 3))
        self.assertEqual(quarantined, [])

    def test_active_state_is_atomic_json_authority(self) -> None:
        controller = load_controller()
        with tempfile.TemporaryDirectory() as tmp:
            point_state_at(controller, tmp)
            expected = {
                "backends": ["10.0.0.1"],
                "pubkey": "0x" + ("1" * 64),
                "code_id": "0x" + ("2" * 64),
                "cluster": "",
                "members": [],
                "operation_id": OPERATION_1,
            }
            controller["persist_active"](
                expected["backends"],
                expected["pubkey"],
                code_id=expected["code_id"],
                operation_id=OPERATION_1,
            )
            Path(controller["ACTIVE_BACKEND_PATH"]).write_text("10.0.0.99\n")
            self.assertEqual(controller["active_state"](), expected)
            Path(controller["ACTIVE_STATE_PATH"]).write_text("{not-json\n")
            with self.assertRaisesRegex(RuntimeError, "active.json is corrupt"):
                controller["active_state"]()

    def test_socket_generation_reconciles_persisted_pool_and_pauses_journal(self) -> None:
        controller = load_controller()
        with tempfile.TemporaryDirectory() as tmp:
            point_state_at(controller, tmp)
            old = {
                "backends": ["10.0.0.1"],
                "pubkey": "0x" + ("1" * 64),
                "code_id": "0x" + ("2" * 64),
                "cluster": "",
                "members": [],
                "operation_id": "",
            }
            controller["persist_active"](
                old["backends"], old["pubkey"], code_id=old["code_id"]
            )
            calls = []
            controller["frontends"] = lambda enabled: calls.append(("frontends", enabled))
            controller["validate_active_before_reopen"] = lambda state, **_kwargs: calls.append(
                ("validate", state["backends"])
            )
            controller["set_pool"] = lambda backends, **_kwargs: calls.append(
                ("set_pool", list(backends))
            ) or {}
            real_reconcile = controller["reconcile_haproxy"]

            def reconcile_once(reason):
                real_reconcile(reason)
                raise SystemExit

            controller["reconcile_haproxy"] = reconcile_once
            controller["socket_generation"] = lambda: (1, 2, 3)
            with self.assertRaises(SystemExit):
                controller["watch_haproxy"]((1, 2, 2))
            self.assertEqual(
                calls,
                [
                    ("frontends", False),
                    ("validate", ["10.0.0.1"]),
                    ("set_pool", ["10.0.0.1"]),
                    ("frontends", True),
                ],
            )

            controller["write_prepared"](
                {
                    "operation_id": OPERATION_1,
                    "previous": old,
                    "registry_intent": True,
                    "commit_started": False,
                }
            )
            calls.clear()
            controller["restore_state"] = lambda state, close_existing, **_kwargs: calls.append(
                ("restore", state["backends"], close_existing)
            ) or {}
            result = real_reconcile("prepared-restart")
            self.assertTrue(result["paused"])
            self.assertEqual(
                calls,
                [("frontends", False), ("restore", ["10.0.0.1"], True)],
            )

    def test_completed_commit_journal_finishes_new_generation_on_restart(self) -> None:
        controller = load_controller()
        with tempfile.TemporaryDirectory() as tmp:
            point_state_at(controller, tmp)
            new = {
                "backends": ["10.0.0.2"],
                "pubkey": "0x" + ("1" * 64),
                "code_id": "0x" + ("2" * 64),
                "cluster": "",
                "members": [],
                "operation_id": OPERATION_1,
            }
            controller["persist_active"](
                new["backends"],
                new["pubkey"],
                code_id=new["code_id"],
                operation_id=OPERATION_1,
            )
            controller["write_prepared"](
                {
                    "operation_id": OPERATION_1,
                    "commit_started": True,
                    "commit_complete": True,
                    "registry_intent": True,
                    "previous": {"backends": ["10.0.0.1"]},
                }
            )
            calls = []
            controller["frontends"] = lambda enabled: calls.append(
                ("frontends", enabled)
            )
            controller["validate_active_before_reopen"] = lambda state, **_kwargs: calls.append(
                ("validate", state["operation_id"])
            )
            controller["set_pool"] = lambda backends, **_kwargs: calls.append(
                ("set_pool", list(backends))
            ) or {}
            result = controller["reconcile_haproxy"]("commit-complete-restart")
            self.assertFalse(result["paused"])
            self.assertTrue(result["completed_commit"])
            self.assertEqual(
                calls,
                [
                    ("frontends", False),
                    ("validate", OPERATION_1),
                    ("set_pool", ["10.0.0.2"]),
                    ("frontends", True),
                ],
            )
            self.assertEqual(controller["read_prepared"](), {})

            controller["persist_active"](
                ["10.0.0.1"],
                "0x" + ("3" * 64),
                code_id="0x" + ("4" * 64),
                operation_id=OPERATION_2,
            )
            controller["write_prepared"](
                {
                    "operation_id": OPERATION_1,
                    "commit_started": True,
                    "commit_complete": True,
                    "registry_intent": True,
                    "previous": {"backends": ["10.0.0.1"]},
                }
            )
            calls.clear()
            controller["restore_state"] = lambda state, close_existing, **_kwargs: calls.append(
                ("restore", state["backends"], close_existing)
            ) or {}
            result = controller["reconcile_haproxy"]("rolled-back-marker-restart")
            self.assertTrue(result["paused"])
            self.assertEqual(
                calls,
                [
                    ("frontends", False),
                    ("restore", ["10.0.0.1"], True),
                ],
            )
            self.assertFalse(controller["read_prepared"]()["commit_complete"])

    def test_initial_shared_pool_requires_fleet_confirmation(self) -> None:
        controller = load_controller(fleet_confirmed=False)
        with self.assertRaisesRegex(RuntimeError, "protocol-v3 fleet confirmation"):
            controller["probe_initial_shared_pool"](["10.0.0.2", "10.0.0.3"])

        with self.assertRaisesRegex(ValueError, "protocol-v3 fleet confirmation"):
            controller["prepare_from_body"](
                {
                    "backends": ["10.0.0.2", "10.0.0.3"],
                    "expected_pubkey": "0x" + ("1" * 64),
                    "expected_code_id": "0x" + ("2" * 64),
                    "expected_cluster": "0x" + ("b" * 40),
                    "protocol_v3_fleet_confirmed": True,
                    "operation_id": OPERATION_1,
                }
            )

    def test_legacy_single_does_not_require_fleet_confirmation(self) -> None:
        controller = load_controller(fleet_confirmed=False)
        controller["probe_pool"] = lambda *_args, **_kwargs: [{}]
        prepared, _probes = controller["prepare_from_body"](
            {
                "backend": "10.0.0.2",
                "expected_pubkey": "0x" + ("1" * 64),
                "expected_code_id": "0x" + ("2" * 64),
                "operation_id": OPERATION_1,
            }
        )
        self.assertFalse(prepared["shared_ha"])

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
                "protocol_v3_fleet_confirmed": True,
                "operation_id": OPERATION_1,
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
                    "protocol_v3_fleet_confirmed": True,
                    "operation_id": OPERATION_1,
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
                    "protocol_v3_fleet_confirmed": True,
                    "operation_id": OPERATION_1,
                }
            )

        with self.assertRaisesRegex(ValueError, "protocol-v3 fleet confirmation"):
            controller["prepare_from_body"](
                {
                    "backends": list(members),
                    "expected_pubkey": pubkey,
                    "expected_code_id": code_id,
                    "expected_cluster": cluster,
                    "operation_id": OPERATION_1,
                }
            )

    def test_prepare_is_serialized_and_tokens_bind_abort(self) -> None:
        controller = load_controller(include_handler=True)
        with tempfile.TemporaryDirectory() as tmp:
            point_state_at(controller, tmp)
            entered = threading.Event()
            release = threading.Event()
            controller["active_state"] = lambda: {
                "backends": ["10.0.0.1"],
                "pubkey": "0x" + ("a" * 64),
                "code_id": "0x" + ("b" * 64),
                "cluster": "",
                "members": [],
                "operation_id": "",
            }

            def prepare(body):
                entered.set()
                release.wait(2)
                return (
                    {
                        "backend": body["backend"],
                        "backends": [body["backend"]],
                        "pubkey": body["expected_pubkey"],
                        "code_id": body["expected_code_id"],
                        "operation_id": body["operation_id"],
                        "shared_ha": False,
                    },
                    [],
                )

            controller["prepare_from_body"] = prepare
            controller["frontends"] = lambda _enabled: None
            controller["validate_active_before_reopen"] = (
                lambda _state, **_kwargs: None
            )
            controller["restore_state"] = lambda _state, close_existing, **_kwargs: {}
            server = controller["BoundedThreadingHTTPServer"](
                ("127.0.0.1", 0), controller["Handler"]
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            def post(path: str, body: dict) -> tuple[int, dict]:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}{path}",
                    data=json.dumps(body).encode(),
                    headers={
                        "Authorization": f"Bearer {ADMIN_KEY}",
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(request) as response:
                        return response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    with error:
                        return error.code, json.load(error)

            base = {
                "backend": "10.0.0.2",
                "expected_pubkey": "0x" + ("1" * 64),
                "expected_code_id": "0x" + ("2" * 64),
                "expected_previous": controller["active_state"](),
            }
            results = {}
            first = threading.Thread(
                target=lambda: results.update(
                    first=post("/prepare", {**base, "operation_id": OPERATION_1})
                )
            )
            second = threading.Thread(
                target=lambda: results.update(
                    second=post("/prepare", {**base, "operation_id": OPERATION_2})
                )
            )
            try:
                first.start()
                self.assertTrue(entered.wait(1))
                second.start()
                release.set()
                first.join(2)
                second.join(2)
                self.assertEqual(results["first"][0], 200)
                self.assertEqual(results["second"][0], 409)
                self.assertEqual(
                    post("/prepare", {**base, "operation_id": OPERATION_1})[0],
                    200,
                )
                self.assertEqual(
                    post(
                        "/prepare",
                        {
                            **base,
                            "backend": "10.0.0.9",
                            "operation_id": OPERATION_1,
                        },
                    )[0],
                    409,
                )
                self.assertEqual(post("/abort", {"operation_id": OPERATION_2})[0], 400)
                self.assertEqual(post("/abort", {"operation_id": OPERATION_1})[0], 200)
            finally:
                release.set()
                server.shutdown()
                server.server_close()
                thread.join()

    def test_active_requires_auth_and_body_length_is_bounded(self) -> None:
        controller = load_controller(include_handler=True)
        with tempfile.TemporaryDirectory() as tmp:
            point_state_at(controller, tmp)
            controller["SOCKET_PATH"] = str(Path(tmp) / "missing.sock")
            server = controller["BoundedThreadingHTTPServer"](
                ("127.0.0.1", 0), controller["Handler"]
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(
                        f"http://127.0.0.1:{server.server_port}/active"
                    )
                with raised.exception as error:
                    self.assertEqual(error.code, 401)

                connection = http.client.HTTPConnection(
                    "127.0.0.1", server.server_port, timeout=2
                )
                connection.putrequest("POST", "/prepare")
                connection.putheader("Authorization", f"Bearer {ADMIN_KEY}")
                connection.putheader("Content-Type", "application/json")
                connection.putheader("Content-Length", "16385")
                connection.endheaders(b"{}")
                response = connection.getresponse()
                self.assertEqual(response.status, 400)
                self.assertIn("Content-Length", json.load(response)["error"])
                connection.close()

                for invalid_length in ("-1", "not-an-integer"):
                    connection = http.client.HTTPConnection(
                        "127.0.0.1", server.server_port, timeout=2
                    )
                    connection.putrequest("POST", "/prepare")
                    connection.putheader("Authorization", f"Bearer {ADMIN_KEY}")
                    connection.putheader("Content-Type", "application/json")
                    connection.putheader("Content-Length", invalid_length)
                    connection.endheaders(b"{}")
                    response = connection.getresponse()
                    self.assertEqual(response.status, 400)
                    self.assertIn("Content-Length", json.load(response)["error"])
                    connection.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_assert_drained_is_bound_to_active_generation_and_live_slots(self) -> None:
        controller = load_controller(include_handler=True)
        with tempfile.TemporaryDirectory() as tmp:
            point_state_at(controller, tmp)
            controller["persist_active"](
                ["10.0.0.2"],
                "0x" + ("1" * 64),
                code_id="0x" + ("2" * 64),
                operation_id=OPERATION_1,
            )
            verified = []
            controller["verify_runtime_pool"] = lambda backends, **_kwargs: verified.append(
                list(backends)
            )
            server = controller["BoundedThreadingHTTPServer"](
                ("127.0.0.1", 0), controller["Handler"]
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            def post(body: dict, path: str = "/assert-drained") -> tuple[int, dict]:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}{path}",
                    data=json.dumps(body).encode(),
                    headers={
                        "Authorization": f"Bearer {ADMIN_KEY}",
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(request) as response:
                        return response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    with error:
                        return error.code, json.load(error)

            try:
                self.assertEqual(
                    post({"operation_id": OPERATION_2, "backend": "10.0.0.3"})[0],
                    409,
                )
                self.assertEqual(
                    post({"operation_id": OPERATION_1, "backend": "10.0.0.2"})[0],
                    409,
                )
                status, body = post(
                    {"operation_id": OPERATION_1, "backend": "10.0.0.3"}
                )
                self.assertEqual(status, 200)
                self.assertTrue(body["drained"])
                self.assertRegex(body["reservation_id"], r"^[0-9a-f]{64}$")
                self.assertRegex(body["release_token"], r"^[0-9a-f]{64}$")
                self.assertEqual(body["reserved_at_operation_id"], OPERATION_1)
                self.assertEqual(verified, [["10.0.0.2"]])

                status, retry = post(
                    {"operation_id": OPERATION_1, "backend": "10.0.0.3"}
                )
                self.assertEqual(status, 200)
                self.assertTrue(retry["idempotent"])
                self.assertEqual(retry["reservation_id"], body["reservation_id"])
                self.assertEqual(retry["release_token"], body["release_token"])

                # A newer active generation does not overwrite the original
                # reservation/token, but retries are bound to the current one.
                controller["persist_active"](
                    ["10.0.0.2"],
                    "0x" + ("1" * 64),
                    code_id="0x" + ("2" * 64),
                    operation_id=OPERATION_2,
                )
                status, advanced = post(
                    {"operation_id": OPERATION_2, "backend": "10.0.0.3"}
                )
                self.assertEqual(status, 200)
                self.assertEqual(advanced["operation_id"], OPERATION_2)
                self.assertEqual(advanced["reserved_at_operation_id"], OPERATION_1)
                self.assertEqual(advanced["release_token"], body["release_token"])

                release = {
                    "operation_id": OPERATION_2,
                    "backend": "10.0.0.3",
                    "reservation_id": body["reservation_id"],
                    "release_token": body["release_token"],
                }
                self.assertEqual(
                    post({**release, "release_token": OPERATION_1}, "/release-drain")[0],
                    409,
                )
                status, released = post(release, "/release-drain")
                self.assertEqual(status, 200)
                self.assertFalse(released["idempotent"])
                # Response loss after durable deletion is safely retryable from
                # the bounded release tombstone.
                status, released_retry = post(release, "/release-drain")
                self.assertEqual(status, 200)
                self.assertTrue(released_retry["idempotent"])
                self.assertEqual(controller["read_drain_reservations"](), {})

                controller["write_prepared"](
                    {"operation_id": OPERATION_2, "previous": {}}
                )
                self.assertEqual(
                    post({"operation_id": OPERATION_2, "backend": "10.0.0.3"})[0],
                    409,
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_driver_never_sources_generic_state_and_validates_fixed_fields(self) -> None:
        source = DRIVER.read_text(encoding="utf-8")
        self.assertNotIn('source "$GENERIC_STATE"', source)
        start = source.index("_backend_state() {")
        end = source.index("\n_trim() {", start)
        validators = source[start:end]
        harness = """
die() { printf '%s\\n' "$*" >&2; exit 1; }
LOGDIR=/tmp
ZERO_ADDRESS=0x0000000000000000000000000000000000000000
ZERO32=0x0000000000000000000000000000000000000000000000000000000000000000
""" + validators

        def run(expression: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["bash", "-c", harness + "\n" + expression],
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(run("_validate_vm_id worker-123").returncode, 0)
        self.assertNotEqual(run("_validate_vm_id 'worker;id'").returncode, 0)
        self.assertNotEqual(run("_backend_state '../worker'").returncode, 0)
        self.assertEqual(
            run("_validate_address 0x" + ("a" * 40) + " X").returncode, 0
        )
        self.assertNotEqual(
            run("_validate_address 0x" + ("a" * 39) + " X").returncode, 0
        )
        self.assertNotEqual(
            run("_validate_address 0x" + ("0" * 40) + " X").returncode, 0
        )
        self.assertEqual(
            run("_validate_bytes32 0x" + ("b" * 64) + " H").returncode, 0
        )
        self.assertNotEqual(
            run("_validate_bytes32 0x" + ("b" * 63) + " H").returncode, 0
        )
        self.assertNotEqual(
            run("_validate_bytes32 0x" + ("0" * 64) + " H").returncode, 0
        )

    def test_partial_commit_restores_pool_and_evicts_candidate_sessions(self) -> None:
        controller = load_controller(include_handler=True)
        with tempfile.TemporaryDirectory() as tmp:
            point_state_at(controller, tmp)

            old = {
                "backends": ["10.0.0.1"],
                "pubkey": "0x" + ("1" * 64),
                "code_id": "0x" + ("2" * 64),
                "cluster": "",
                "members": [],
                "operation_id": "",
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
                    "registry_intent": True,
                    "operation_id": OPERATION_1,
                }
            )
            controller["wait_until_serving"] = lambda _prepared: []
            controller["verify_runtime_pool"] = lambda _backends, **_kwargs: None
            controller["frontends"] = lambda _enabled: None

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
            server = controller["BoundedThreadingHTTPServer"](
                ("127.0.0.1", 0), controller["Handler"]
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/commit",
                data=json.dumps({"operation_id": OPERATION_1}).encode(),
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
            self.assertLess(restored, candidate_closed)
            self.assertNotIn("enable frontend indexer_grpc", commands[failure_index + 1 :])


class PinnedHaproxyRuntimeTests(unittest.TestCase):
    def test_pinned_runtime_frontend_slot_and_check_port_semantics(self) -> None:
        if not shutil.which("docker"):
            self.skipTest("docker is unavailable")
        info = subprocess.run(
            ["docker", "info"], capture_output=True, text=True, check=False, timeout=20
        )
        if info.returncode != 0:
            self.skipTest("docker daemon is unavailable")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run"
            run_dir.mkdir(mode=0o777)
            os.chmod(run_dir, 0o777)
            config = root / "haproxy.cfg"
            config.write_text(
                textwrap.dedent(
                    """
                    global
                      stats socket /runtime/admin.sock mode 666 level admin
                    defaults
                      mode tcp
                      timeout connect 1s
                      timeout client 10s
                      timeout server 10s
                      timeout check 1s
                    frontend indexer_grpc
                      bind :50052
                      default_backend indexer_grpc_active
                    backend indexer_grpc_active
                      option httpchk GET /healthz
                      http-check expect status 200
                      server worker1 127.0.0.1:1 check port 9090 disabled
                    frontend indexer_http
                      mode http
                      bind :9090
                      default_backend indexer_http_active
                    backend indexer_http_active
                      mode http
                      option httpchk GET /healthz
                      http-check expect status 200
                      server worker1 127.0.0.1:1 check disabled
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            name = f"indexer-lb-haproxy-test-{uuid.uuid4().hex[:12]}"
            started = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "-d",
                    "--name",
                    name,
                    "--user",
                    "0:0",
                    "-v",
                    f"{config}:/cfg/haproxy.cfg:ro",
                    "-v",
                    f"{run_dir}:/runtime",
                    "--entrypoint",
                    "haproxy",
                    HAPROXY_IMAGE,
                    "-f",
                    "/cfg/haproxy.cfg",
                    "-db",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
            self.assertEqual(started.returncode, 0, started.stderr)
            socket_path = run_dir / "admin.sock"

            def admin(command: str) -> str:
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    client.settimeout(5)
                    client.connect(str(socket_path))
                    client.sendall((command + "\n").encode())
                    client.shutdown(socket.SHUT_WR)
                    chunks = []
                    while True:
                        chunk = client.recv(65536)
                        if not chunk:
                            break
                        chunks.append(chunk)
                    return b"".join(chunks).decode()
                finally:
                    client.close()

            def frontend_statuses() -> dict[str, str]:
                raw = admin("show stat")
                rows = csv.DictReader(io.StringIO(raw[2:]))
                return {
                    row["pxname"]: row["status"]
                    for row in rows
                    if row["svname"] == "FRONTEND"
                }

            try:
                for _ in range(50):
                    if socket_path.exists():
                        break
                    time.sleep(0.1)
                self.assertTrue(socket_path.exists(), started.stdout)
                self.assertEqual(frontend_statuses()["indexer_grpc"], "OPEN")
                self.assertEqual(frontend_statuses()["indexer_http"], "OPEN")
                admin("disable frontend indexer_grpc")
                admin("disable frontend indexer_http")
                self.assertEqual(frontend_statuses()["indexer_grpc"], "PAUSED")
                self.assertEqual(frontend_statuses()["indexer_http"], "PAUSED")
                admin("enable frontend indexer_grpc")
                admin("enable frontend indexer_http")
                self.assertEqual(frontend_statuses()["indexer_grpc"], "OPEN")
                self.assertEqual(frontend_statuses()["indexer_http"], "OPEN")

                admin(
                    "set server indexer_grpc_active/worker1 addr 10.0.0.2 port 50052"
                )
                admin("enable server indexer_grpc_active/worker1")
                stat_rows = list(csv.DictReader(io.StringIO(admin("show stat")[2:])))
                worker = next(
                    row
                    for row in stat_rows
                    if row["pxname"] == "indexer_grpc_active"
                    and row["svname"] == "worker1"
                )
                self.assertEqual(worker["addr"], "10.0.0.2:50052")
                self.assertFalse(worker["status"].startswith("MAINT"))

                state_lines = [
                    line for line in admin("show servers state").splitlines() if line
                ]
                header_index = next(
                    index for index, line in enumerate(state_lines) if line.startswith("# ")
                )
                header = state_lines[header_index].removeprefix("# ").split()
                states = [
                    dict(zip(header, line.split()))
                    for line in state_lines[header_index + 1 :]
                    if not line.startswith("#")
                ]
                grpc = next(
                    row
                    for row in states
                    if row["be_name"] == "indexer_grpc_active"
                    and row["srv_name"] == "worker1"
                )
                self.assertEqual(grpc["srv_port"], "50052")
                self.assertEqual(grpc["srv_check_port"], "9090")
                admin("shutdown sessions server indexer_grpc_active/worker1")
            finally:
                subprocess.run(
                    ["docker", "rm", "-f", name],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=20,
                )


if __name__ == "__main__":
    unittest.main()
