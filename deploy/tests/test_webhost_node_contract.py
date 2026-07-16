#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
CANDIDATE = ROOT / "deploy/compose/webhost-node.yaml"
ROLLBACK = ROOT / "deploy/compose/webhost-node-v1.1.3-rollback.yaml"
CONTROL_IMAGE = (
    "ghcr.io/dmvt/webhost-control-control-plane@"
    "sha256:97655dfcd6d888d6f59f61abb95e5e8251dedc8c2ac587e1616deb4db1c2d2d0"
)
STORAGE_IMAGE = (
    "ghcr.io/dmvt/webhost-control-storage-helper@"
    "sha256:099310c8869c181720f10d5a35e87533b7f0f6490ed2643c8525ce35b72aa10b"
)
TLS_IMAGE = (
    "ghcr.io/dmvt/webhost-control-tlsproxy@"
    "sha256:fa2f2dfaeb9d5bf9c61540eba27a2fad789a950e4217aa0339b38b54fbc9ea81"
)
VOLUME_NAMES = {
    "daemon_data": "dstack_daemon_data",
    "briefs": "dstack_briefs",
    "digests": "dstack_digests",
    "caddy_data_v2": "dstack_caddy_data_v2",
    "caddy_config_v2": "dstack_caddy_config_v2",
    "sidecar-state": "dstack_sidecar-state",
}


def compose_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "CHAIN_ID": "8453",
            "RPC_URL": "https://rpc.example.invalid",
            "BUNDLER_URL": "https://bundler.example.invalid",
            "GAS_POLICY_ID": "test-policy",
            "INDEXER_REGISTRY_ADDR": "0x0000000000000000000000000000000000000001",
            "GATEWAY_DOMAIN": "gateway.example.invalid",
            "TEE_DAEMON_TOKEN": "a" * 32,
            "WEBHOST_MCP_TOKEN": "b" * 32,
            "GITHUB_ID": "test-client",
            "GITHUB_SECRET": "test-secret",
            "NEXTAUTH_SECRET": "c" * 32,
            "NEXTAUTH_URL": "https://apps.synclave.net",
            "APP_DOMAIN": "app.synclave.net",
            "DIRECTORY_HOST": "apps.synclave.net",
            "CONSOLE_HOST": "apps.synclave.net",
            "WEBHOST_ADMIN_HOST": "daemon.synclave.net",
            "ACME_EMAIL": "admin@synclave.net",
            "REDPILL_API_KEY": "test",
            "REDPILL_BASE_URL": "https://api.example.invalid",
            "REDPILL_MODEL": "test",
            "VENICE_API_KEY": "test-venice",
            "VENICE_BASE_URL": "https://api.example.invalid",
            "VENICE_MODEL": "test",
            "RUNYARD_HUB_URL": "https://runyard.example.invalid",
            "RUNYARD_HUB_TOKEN": "d" * 32,
            "RUNYARD_CALLBACK_SECRET": "e" * 32,
            "RUNYARD_CALLBACK_URL": "https://synclave.net/api/runyard/privacy-audit-callback",
            "CLOUDFLARE_API_TOKEN": "test-cloudflare",
            "CLOUDFLARE_SYNCLAVE_API_TOKEN": "test-cloudflare-synclave",
        }
    )
    return env


def render(path: Path) -> dict:
    result = subprocess.run(
        ["docker", "compose", "-f", str(path), "config", "--format", "json"],
        cwd=ROOT,
        env=compose_env(),
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


class WebhostNodeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.candidate = render(CANDIDATE)
        cls.rollback = render(ROLLBACK)

    def test_candidate_uses_exact_released_unified_topology(self) -> None:
        services = self.candidate["services"]
        self.assertEqual(self.candidate["name"], "dstack")
        self.assertEqual(services["frontproxy"]["image"], CONTROL_IMAGE)
        self.assertEqual(services["storage-helper"]["image"], STORAGE_IMAGE)
        self.assertEqual(services["tlsproxy"]["image"], TLS_IMAGE)
        self.assertNotIn("tee-daemon", services)
        self.assertNotIn("concierge", services)
        env = services["frontproxy"]["environment"]
        self.assertEqual(env["WEBHOST_ENV"], "production")
        self.assertEqual(env["WEBHOST_VERSION"], "v1.1.3")
        self.assertEqual(
            env["WEBHOST_BUILD_COMMIT"],
            "fa0023cb8fd43ad7102b7e1a3b7c30ae5b30361a",
        )
        self.assertEqual(env["DAEMON_CONTAINER_RUNTIME"], "runsc")
        self.assertEqual(env["DAEMON_ENFORCE_EGRESS"], "1")
        self.assertEqual(env["INGRESS_PORT"], "8088")
        self.assertEqual(services["frontproxy"]["cpus"], 1.0)
        self.assertFalse(
            any(key.startswith("RUNYARD_") for key in env),
            "the unified Webhost must not consume the legacy HTTP Runyard handoff",
        )
        self.assertEqual(
            services["frontproxy"]["depends_on"]["migration-backup"]["condition"],
            "service_completed_successfully",
        )
        self.assertEqual(
            services["frontproxy"]["depends_on"]["storage-helper"]["condition"],
            "service_healthy",
        )
        self.assertEqual(
            services["tlsproxy"]["depends_on"]["frontproxy"]["condition"],
            "service_healthy",
        )
        self.assertEqual(
            {port["published"] for port in services["tlsproxy"]["ports"]},
            {"80", "443"},
        )

    def test_durable_volume_names_and_backup_inventory_are_preserved(self) -> None:
        for logical, physical in VOLUME_NAMES.items():
            self.assertEqual(self.candidate["volumes"][logical]["name"], physical)
            self.assertEqual(self.rollback["volumes"][logical]["name"], physical)
        backup_targets = {
            volume["target"] for volume in self.candidate["services"]["backup"]["volumes"]
        }
        for name in VOLUME_NAMES:
            self.assertIn(f"/src/{name}", backup_targets)
        self.assertEqual(
            self.candidate["volumes"]["migration_backup"]["name"],
            "dstack_webhost_v1_1_3_migration_backup",
        )
        self.assertEqual(
            self.rollback["volumes"]["migration_backup"]["name"],
            "dstack_webhost_v1_1_3_migration_backup",
        )

    def test_rollback_restores_before_legacy_writers(self) -> None:
        services = self.rollback["services"]
        self.assertIn("migration-restore", services)
        self.assertIn("tee-daemon", services)
        self.assertIn("concierge", services)
        self.assertEqual(
            services["sidecar"]["depends_on"]["migration-restore"]["condition"],
            "service_completed_successfully",
        )
        self.assertEqual(
            services["restore"]["depends_on"]["migration-restore"]["condition"],
            "service_completed_successfully",
        )

    def test_driver_fail_closes_and_automatically_rolls_back(self) -> None:
        driver = (ROOT / "deploy/webhost-node.sh").read_text(encoding="utf-8")
        box = (ROOT / "deploy/webhost-node-box.py").read_text(encoding="utf-8")
        for required in (
            "preflight()",
            "verify_release()",
            "rollback_member()",
            'rollback_member\n  die "Webhost ${WEBHOST_RELEASE_VERSION} failed smoke',
            "BOX_FRESH_DISK is forbidden",
            "cosign verify",
            "WEBHOST_RELEASE_COMMIT",
        ):
            self.assertIn(required, driver)
        self.assertGreaterEqual(
            driver.count("BOX_FRESH_DISK is forbidden"),
            2,
            "both preflight and the update primitive must reject a disk wipe",
        )
        self.assertIn('"WEBHOST_ADMIN_HOST"', box)
        self.assertIn('"ACME_EMAIL"', box)
        self.assertIn("????????????_dstack-sidecar-1", box)
        self.assertIn('tombstone_running" != false', box)
        self.assertIn('tombstone_project" != dstack', box)
        self.assertIn('tombstone_service" != sidecar', box)
        self.assertIn('docker rm "$tombstone_id"', box)
        self.assertNotIn('docker rm -f "$tombstone_id"', box)
        self.assertIn("Webhost CVM bridge lease not ready", driver)

    def test_snapshot_and_restore_commands_round_trip(self) -> None:
        backup_service = self.candidate["services"]["migration-backup"]
        restore_service = self.rollback["services"]["migration-restore"]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            target = root / "target"
            snapshot = root / "snapshot"
            source.mkdir()
            target.mkdir()
            snapshot.mkdir()
            # A named Docker volume is root-owned in production. This bind-mount
            # harness is owned by the invoking developer, so grant the container
            # equivalent write access while retaining the exact capability set.
            snapshot.chmod(0o777)
            for name in VOLUME_NAMES:
                (source / name).mkdir()
                (target / name).mkdir()
                (source / name / "sentinel").write_text(
                    f"pre-migration:{name}\n", encoding="utf-8"
                )
                (target / name / "changed").write_text("candidate-write\n", encoding="utf-8")

            subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--read-only",
                    "--security-opt",
                    "no-new-privileges:true",
                    "--cap-drop",
                    "ALL",
                    "--cap-add",
                    "DAC_READ_SEARCH",
                    "-v",
                    f"{source}:/source:ro",
                    "-v",
                    f"{snapshot}:/snapshot",
                    "--entrypoint",
                    "/bin/sh",
                    backup_service["image"],
                    "-ec",
                    backup_service["command"][0],
                ],
                check=True,
            )
            subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "-v",
                    f"{target}:/target",
                    "-v",
                    f"{snapshot}:/snapshot",
                    "--entrypoint",
                    "/bin/sh",
                    restore_service["image"],
                    "-ec",
                    restore_service["command"][0],
                ],
                check=True,
            )
            for name in VOLUME_NAMES:
                self.assertEqual(
                    (target / name / "sentinel").read_text(encoding="utf-8"),
                    f"pre-migration:{name}\n",
                )
                self.assertFalse((target / name / "changed").exists())


if __name__ == "__main__":
    unittest.main()
