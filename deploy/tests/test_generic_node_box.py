from __future__ import annotations

import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path


class FakeDstack(types.ModuleType):
    GATEWAY_RPC = "https://gateway.invalid"
    KMS_URLS = ["https://kms.invalid"]

    def __init__(self) -> None:
        super().__init__("mcp_dstack")
        self.vms = {"old-vm"}
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.fail_create_once = False

    def vmm(self, method: str, args: dict[str, object]):
        self.calls.append((method, args))
        vm_id = str(args.get("id") or "")
        if method == "GetInfo":
            return {"found": vm_id in self.vms, "info": {"status": "exited"}}
        if method == "StopVm":
            return {}
        if method == "RemoveVm":
            if vm_id not in self.vms:
                raise RuntimeError("VM not found")
            self.vms.remove(vm_id)
            return {}
        if method == "CreateVm":
            if self.fail_create_once:
                self.fail_create_once = False
                raise RuntimeError("injected CreateVm failure")
            self.vms.add("replacement-vm")
            return {"result": {"vm_id": "replacement-vm"}}
        raise AssertionError(method)

    @staticmethod
    def _parse_port(value):
        return value

    @staticmethod
    def _seal_env(env, key):
        return env

    @staticmethod
    def _app_env_encrypt_pubkey(app_id):
        return app_id


def load_module(fake: FakeDstack):
    sys.modules["mcp_dstack"] = fake
    path = Path(__file__).parents[1] / "generic-node-box.py"
    spec = importlib.util.spec_from_file_location("generic_node_box_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RecreateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeDstack()
        self.module = load_module(self.fake)
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.module.RECREATE_JOURNAL_DIR = Path(self.tempdir.name)

    def test_retry_after_create_failure_does_not_remove_old_vm_twice(self) -> None:
        self.fake.fail_create_once = True
        with self.assertRaisesRegex(RuntimeError, "injected CreateVm failure"):
            self.module.recreate_vm("0xapp", "old-vm", "compose", {"APP_ID": "0xapp"})

        journal = self.module._recreate_journal("0xapp", "old-vm")
        self.assertIn('"old_vm_removed": true', journal.read_text())
        self.assertEqual([c[0] for c in self.fake.calls].count("RemoveVm"), 1)

        replacement = self.module.recreate_vm(
            "0xapp", "old-vm", "compose", {"APP_ID": "0xapp"}
        )
        self.assertEqual(replacement, "replacement-vm")
        self.assertEqual([c[0] for c in self.fake.calls].count("RemoveVm"), 1)

        creates = [c[0] for c in self.fake.calls].count("CreateVm")
        self.assertEqual(
            self.module.recreate_vm("0xapp", "old-vm", "compose", {}),
            "replacement-vm",
        )
        self.assertEqual([c[0] for c in self.fake.calls].count("CreateVm"), creates)

    def test_missing_old_vm_is_an_idempotent_destructive_step(self) -> None:
        self.fake.vms.clear()
        replacement = self.module.recreate_vm("0xapp", "gone-vm", "compose", {})
        self.assertEqual(replacement, "replacement-vm")
        self.assertNotIn("RemoveVm", [call[0] for call in self.fake.calls])

    def test_create_result_requires_a_replacement_id(self) -> None:
        self.assertEqual(self.module._replacement_id({"vm": {"id": "vm-2"}}), "vm-2")
        self.assertEqual(self.module._replacement_id({"result": {}}), "")


if __name__ == "__main__":
    unittest.main()
