from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
import tempfile
import types
import unittest
import uuid
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
HELPER_PATH = ROOT / "deploy" / "sandboxd-node-box.py"


class FakeDstack(types.ModuleType):
    def __init__(self) -> None:
        super().__init__("mcp_dstack")
        self.app_id = "0x" + "44" * 20
        self.vm_id = "vm-test"
        self.found = True
        self.status = "running"
        self.vcpu = 8
        self.memory = 16384
        self.disk = 300
        self.compose_file = "old-compose"
        self.previous_vm_id = "vm-previous"
        self.previous_found = False
        self.previous_status = "stopped"
        self.previous_vcpu = 4
        self.previous_memory = 8192
        self.previous_disk = 80
        self.previous_compose_file = "previous-compose"
        self.extra_vms: list[dict[str, object]] = []
        self.calls: list[tuple[str, dict[str, object] | None]] = []
        self.on_get_info = None
        self.on_stop = None
        self.on_upgrade = None
        self.on_remove = None
        self.GATEWAY_RPC = "https://gateway.invalid"
        self.KMS_URLS = ["https://kms.invalid"]
        self._parse_port = lambda value: value
        self._app_env_encrypt_pubkey = lambda app_id: b"public-key"
        self._seal_env = lambda env, public_key: {"sealed": True}

    def _info(self, vm_id: str) -> dict[str, object]:
        if vm_id == self.previous_vm_id:
            return {
                "found": self.previous_found,
                "info": {
                    "status": self.previous_status,
                    "configuration": {
                        "vcpu": self.previous_vcpu,
                        "memory": self.previous_memory,
                        "disk_size": self.previous_disk,
                        "app_id": self.app_id,
                        "compose_file": self.previous_compose_file,
                    },
                },
            }
        return {
            "found": self.found,
            "info": {
                "status": self.status,
                "configuration": {
                    "vcpu": self.vcpu,
                    "memory": self.memory,
                    "disk_size": self.disk,
                    "app_id": self.app_id,
                    "compose_file": self.compose_file,
                },
            },
        }

    def _inventory(self) -> dict[str, object]:
        vms: list[dict[str, object]] = []
        if self.found:
            vms.append(
                {
                    "id": self.vm_id,
                    "name": "sandboxd",
                    "status": self.status,
                    "app_id": self.app_id,
                }
            )
        if self.previous_found:
            vms.append(
                {
                    "id": self.previous_vm_id,
                    "name": "sandboxd-previous",
                    "status": self.previous_status,
                    "app_id": self.app_id,
                }
            )
        return {"vms": [*vms, *self.extra_vms]}

    def vmm(self, method: str, payload: dict[str, object] | None = None) -> dict:
        self.calls.append((method, payload))
        if method == "GetInfo":
            if self.on_get_info is not None:
                self.on_get_info()
            return self._info(str((payload or {}).get("id") or self.vm_id))
        if method == "Status":
            return self._inventory()
        if method == "StopVm":
            if self.on_stop is not None:
                self.on_stop()
            else:
                self.status = "stopped"
            return {"stopped": True}
        if method == "UpgradeApp":
            if self.on_upgrade is not None:
                self.on_upgrade(payload or {})
            else:
                self.compose_file = str((payload or {})["compose_file"])
            return {"upgraded": True}
        if method == "StartVm":
            self.status = "starting"
            return {"started": True}
        if method == "RemoveVm":
            if self.on_remove is not None:
                self.on_remove()
            else:
                self.previous_found = False
            return {"removed": True}
        raise AssertionError(f"unexpected VMM method: {method}")

    @property
    def mutation_methods(self) -> list[str]:
        return [
            method
            for method, _ in self.calls
            if method in {"StopVm", "UpgradeApp", "StartVm", "RemoveVm"}
        ]


class SandboxdBoxUpdateTests(unittest.TestCase):
    profile = (8, 16384, 300)

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.compose_path = Path(self.tempdir.name) / "compose.yaml"
        self.compose_path.write_text("services:\n  sandboxd:\n    image: target-a\n")
        self.fake = FakeDstack()
        module_name = f"sandboxd_node_box_test_{uuid.uuid4().hex}"
        spec = importlib.util.spec_from_file_location(module_name, HELPER_PATH)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        with (
            mock.patch.dict(
                os.environ,
                {
                    "BOX_COMPOSE": str(self.compose_path),
                    "BOX_VCPU": "8",
                    "BOX_MEM": "16384",
                    "BOX_DISK": "300",
                    "BOX_PORTS": "[]",
                    "BOX_GATEWAY_ENABLED": "true",
                    "BOX_NET_MODE": "bridge",
                },
            ),
            mock.patch.dict(sys.modules, {"mcp_dstack": self.fake}),
        ):
            spec.loader.exec_module(module)
        self.helper = module
        self.target_compose, self.target_hash = self.helper.app_compose_and_hash(
            list(self.helper.build_env(self.fake.app_id).keys())
        )
        self.previous_hash = hashlib.sha256(self.fake.compose_file.encode()).hexdigest()

    def update(self) -> dict[str, object]:
        return self.helper.checked_update_vm(
            self.fake.app_id,
            self.fake.vm_id,
            self.previous_hash,
            self.target_hash,
            self.profile,
        )

    def test_uses_one_compose_snapshot_even_if_path_changes_after_read(self) -> None:
        swapped = False

        def swap_after_snapshot() -> None:
            nonlocal swapped
            if not swapped:
                self.compose_path.write_text(
                    "services:\n  sandboxd:\n    image: attacker-swap\n"
                )
                swapped = True

        self.fake.on_get_info = swap_after_snapshot
        result = self.update()
        self.assertEqual(result["compose_hash"], self.target_hash)
        self.assertEqual(
            self.fake.mutation_methods,
            ["StopVm", "UpgradeApp", "StartVm"],
        )
        upgrade_payload = next(
            payload for method, payload in self.fake.calls if method == "UpgradeApp"
        )
        assert upgrade_payload is not None
        self.assertEqual(upgrade_payload["compose_file"], self.target_compose)
        self.assertEqual(
            hashlib.sha256(str(upgrade_payload["compose_file"]).encode()).hexdigest(),
            self.target_hash,
        )

    def test_compose_swap_before_snapshot_fails_before_vmm_mutation(self) -> None:
        self.compose_path.write_text(
            "services:\n  sandboxd:\n    image: swapped-before\n"
        )
        with self.assertRaisesRegex(SystemExit, "does not match journaled target"):
            self.update()
        self.assertEqual(self.fake.calls, [])

    def test_third_current_hash_fails_before_mutation(self) -> None:
        self.fake.compose_file = "third-unexpected-compose"
        with self.assertRaisesRegex(SystemExit, "pre-update VM identity/hash"):
            self.update()
        self.assertEqual(self.fake.mutation_methods, [])

    def test_wrong_current_resources_fail_before_mutation(self) -> None:
        self.fake.memory = 8192
        with self.assertRaisesRegex(SystemExit, "pre-update VM identity/hash/profile"):
            self.update()
        self.assertEqual(self.fake.mutation_methods, [])

    def test_boolean_resource_readback_fails_before_mutation(self) -> None:
        self.fake.vcpu = True
        with self.assertRaisesRegex(SystemExit, "pre-update VM identity/hash/profile"):
            self.update()
        self.assertEqual(self.fake.mutation_methods, [])

    def test_transitional_or_unknown_status_fails_before_stop(self) -> None:
        for status in ("starting", "stopping", "running-unverified", "unknown", ""):
            with self.subTest(status=status):
                self.setUp()
                self.fake.status = status
                with self.assertRaisesRegex(SystemExit, "neither exactly stopped"):
                    self.update()
                self.assertEqual(self.fake.mutation_methods, [])

    def test_duplicate_active_inventory_fails_before_mutation(self) -> None:
        self.fake.extra_vms = [
            {
                "id": "vm-duplicate",
                "name": "duplicate",
                "status": "running",
                "app_id": self.fake.app_id,
            }
        ]
        with self.assertRaisesRegex(SystemExit, "pre-update same-app inventory"):
            self.update()
        self.assertEqual(self.fake.mutation_methods, [])

    def test_dormant_duplicate_inventory_fails_before_mutation(self) -> None:
        self.fake.extra_vms = [
            {
                "id": "vm-dormant-duplicate",
                "name": "dormant-duplicate",
                "status": "stopped",
                "app_id": self.fake.app_id,
            }
        ]
        with self.assertRaisesRegex(SystemExit, "pre-update same-app inventory"):
            self.update()
        self.assertEqual(self.fake.mutation_methods, [])

    def test_hash_change_during_stop_blocks_upgrade_and_start(self) -> None:
        def corrupt_during_stop() -> None:
            self.fake.status = "stopped"
            self.fake.compose_file = "third-after-stop"

        self.fake.on_stop = corrupt_during_stop
        with self.assertRaisesRegex(SystemExit, "post-stop VM identity/hash"):
            self.update()
        self.assertEqual(self.fake.mutation_methods, ["StopVm"])

    def test_bad_post_upgrade_state_never_starts(self) -> None:
        corruptions = {
            "third hash": lambda payload: setattr(
                self.fake, "compose_file", "third-after-upgrade"
            ),
            "wrong resources": lambda payload: (
                setattr(self.fake, "compose_file", str(payload["compose_file"])),
                setattr(self.fake, "vcpu", 7),
            ),
            "active duplicate": lambda payload: (
                setattr(self.fake, "compose_file", str(payload["compose_file"])),
                self.fake.extra_vms.append(
                    {
                        "id": "vm-duplicate",
                        "status": "running",
                        "app_id": self.fake.app_id,
                    }
                ),
            ),
            "dormant duplicate": lambda payload: (
                setattr(self.fake, "compose_file", str(payload["compose_file"])),
                self.fake.extra_vms.append(
                    {
                        "id": "vm-dormant-duplicate",
                        "status": "stopped",
                        "app_id": self.fake.app_id,
                    }
                ),
            ),
        }
        for label, corruption in corruptions.items():
            with self.subTest(label=label):
                self.setUp()
                self.fake.on_upgrade = corruption
                with self.assertRaises(SystemExit):
                    self.update()
                self.assertEqual(
                    self.fake.mutation_methods,
                    ["StopVm", "UpgradeApp"],
                )

    def test_checked_start_rejects_drift_or_active_inventory_before_start(self) -> None:
        scenarios = {
            "third hash": lambda: setattr(
                self.fake, "compose_file", "wrong-stopped-target"
            ),
            "wrong resources": lambda: setattr(self.fake, "disk", 299),
            "active duplicate": lambda: self.fake.extra_vms.append(
                {
                    "id": "vm-duplicate",
                    "status": "running",
                    "app_id": self.fake.app_id,
                }
            ),
            "dormant duplicate": lambda: self.fake.extra_vms.append(
                {
                    "id": "vm-dormant-duplicate",
                    "status": "stopped",
                    "app_id": self.fake.app_id,
                }
            ),
        }
        for label, corruption in scenarios.items():
            with self.subTest(label=label):
                self.setUp()
                self.fake.status = "stopped"
                self.fake.compose_file = self.target_compose
                corruption()
                with self.assertRaises(SystemExit):
                    self.helper.checked_start_vm(
                        self.fake.vm_id,
                        self.fake.app_id,
                        self.target_hash,
                        self.profile,
                    )
                self.assertNotIn("StartVm", self.fake.mutation_methods)

    def retire_previous(
        self,
        *,
        previous_hash: str | None = None,
        previous_profile: tuple[int, int, int] = (4, 8192, 80),
    ) -> dict[str, object]:
        current_hash = hashlib.sha256(self.fake.compose_file.encode()).hexdigest()
        if previous_hash is None:
            previous_hash = hashlib.sha256(
                self.fake.previous_compose_file.encode()
            ).hexdigest()
        return self.helper.checked_retire_previous_vm(
            self.fake.app_id,
            self.fake.vm_id,
            current_hash,
            self.fake.previous_vm_id,
            previous_hash,
            self.profile,
            previous_profile,
        )

    def test_checked_retirement_removes_only_exact_stopped_predecessor(self) -> None:
        self.fake.previous_found = True
        result = self.retire_previous()
        self.assertFalse(result["already_removed"])
        self.assertFalse(self.fake.previous_found)
        self.assertEqual(self.fake.mutation_methods, ["RemoveVm"])

    def test_checked_retirement_rejects_third_same_app_entry(self) -> None:
        self.fake.previous_found = True
        self.fake.extra_vms = [
            {
                "id": "vm-third",
                "name": "third",
                "status": "stopped",
                "app_id": self.fake.app_id,
            }
        ]
        with self.assertRaisesRegex(SystemExit, "retirement inventory mismatch"):
            self.retire_previous()
        self.assertEqual(self.fake.mutation_methods, [])

    def test_checked_retirement_rejects_previous_hash_or_profile_drift(self) -> None:
        for label, corruption, kwargs in (
            (
                "hash",
                lambda: setattr(
                    self.fake, "previous_compose_file", "unexpected-previous"
                ),
                {"previous_hash": hashlib.sha256(b"previous-compose").hexdigest()},
            ),
            (
                "profile",
                lambda: setattr(self.fake, "previous_disk", 81),
                {},
            ),
            (
                "status",
                lambda: setattr(self.fake, "previous_status", "starting"),
                {},
            ),
        ):
            with self.subTest(label=label):
                self.setUp()
                self.fake.previous_found = True
                corruption()
                with self.assertRaises(SystemExit):
                    self.retire_previous(**kwargs)
                self.assertEqual(self.fake.mutation_methods, [])

    def test_checked_retirement_recovers_applied_remove_transport_error(self) -> None:
        self.fake.previous_found = True

        def remove_then_error() -> None:
            self.fake.previous_found = False
            raise RuntimeError("lost response")

        self.fake.on_remove = remove_then_error
        result = self.retire_previous()
        self.assertFalse(result["already_removed"])
        self.assertIn("remove_error", result["remove"])
        self.assertEqual(self.fake.mutation_methods, ["RemoveVm"])

    def test_checked_retirement_retry_proves_already_removed_poststate(self) -> None:
        result = self.retire_previous()
        self.assertTrue(result["already_removed"])
        self.assertEqual(self.fake.mutation_methods, [])


if __name__ == "__main__":
    unittest.main()
