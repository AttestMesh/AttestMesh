from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class SynclaveNodeContractTests(unittest.TestCase):
    def test_synclave_uses_the_authoritative_sandbox_workload_image(self) -> None:
        expected = (
            "ghcr.io/attestmesh/synclave-workloads@sha256:"
            "eeeab97469edf54f2d5b9582a0a1c6b49866af931573324919a3dcc6b23a0b4e"
        )
        synclave = (ROOT / "deploy/synclave-node.sh").read_text(encoding="utf-8")
        sandboxd = (ROOT / "deploy/sandboxd-node.sh").read_text(encoding="utf-8")

        self.assertIn(f"SANDBOX_DEFAULT_IMAGE:-{expected}", synclave)
        self.assertIn(f'image = "{expected}"', sandboxd)
        self.assertNotIn("ghcr.io/dmvt/cs-sandbox-base", synclave)

    def test_compose_preserves_the_existing_dstack_project_identity(self) -> None:
        compose = (ROOT / "deploy/compose/synclave-node.yaml").read_text(
            encoding="utf-8"
        )

        self.assertIn("\nname: dstack\n", compose)
        self.assertNotIn("\nname: attestmesh-synclave\n", compose)

    def test_sandbox_public_suffix_is_sealed_into_the_synclave_cvm(self) -> None:
        compose = (ROOT / "deploy/compose/synclave-node.yaml").read_text(
            encoding="utf-8"
        )
        driver = (ROOT / "deploy/synclave-node.sh").read_text(encoding="utf-8")
        box = (ROOT / "deploy/synclave-node-box.py").read_text(encoding="utf-8")

        self.assertIn("SANDBOX_APPS_DOMAIN: ${SANDBOX_APPS_DOMAIN}", compose)
        self.assertIn(
            'SANDBOX_APPS_DOMAIN="${SANDBOX_APPS_DOMAIN:-synclave.net}"',
            driver,
        )
        self.assertIn("printf 'E_SANDBOX_APPS_DOMAIN=%q", driver)
        self.assertIn('"SANDBOX_APPS_DOMAIN"', box)

    def test_fleet_brokers_exact_app_and_sandbox_dns_records(self) -> None:
        compose = (ROOT / "deploy/compose/synclave-node.yaml").read_text(encoding="utf-8")
        driver = (ROOT / "deploy/synclave-node.sh").read_text(encoding="utf-8")
        box = (ROOT / "deploy/synclave-node-box.py").read_text(encoding="utf-8")

        self.assertIn("APP_DOMAIN: ${APP_DOMAIN}", compose)
        self.assertIn("CLOUDFLARE_DNS_API_TOKEN: ${SYNCLAVE_CLOUDFLARE_API_TOKEN}", compose)
        self.assertIn("CLOUDFLARE_APP_CNAME_TARGET: ${CLOUDFLARE_APP_CNAME_TARGET:-}", compose)
        self.assertIn("CLOUDFLARE_SANDBOX_CNAME_TARGET: ${CLOUDFLARE_SANDBOX_CNAME_TARGET:-}", compose)
        self.assertIn('APP_DOMAIN="${APP_DOMAIN:-synclave.net}"', driver)
        self.assertIn('"CLOUDFLARE_APP_CNAME_TARGET"', box)
        self.assertIn('"CLOUDFLARE_SANDBOX_CNAME_TARGET"', box)

    def test_production_roll_uses_locally_verified_release_identity(self) -> None:
        compose = (ROOT / "deploy/compose/synclave-node.yaml").read_text(encoding="utf-8")
        driver = (ROOT / "deploy/synclave-node.sh").read_text(encoding="utf-8")

        image = (
            "ghcr.io/attestmesh/synclave-app@sha256:"
            "31e1340d627396091a3ad19e55c6e4dd6b4967de8592f240748c6c7b2bdbc02b"
        )
        self.assertIn(f"image: {image}", compose)
        self.assertIn(f'SYNCLAVE_RELEASE_IMAGE="{image}"', driver)
        self.assertIn("_verify_local_release_image", driver)
        self.assertIn('org.opencontainers.image.revision', driver)
        self.assertIn('org.opencontainers.image.version', driver)
        self.assertNotIn("cosign verify", driver)

    def test_prelaunch_removes_only_the_stopped_compose_sidecar_tombstone(self) -> None:
        box = (ROOT / "deploy/synclave-node-box.py").read_text(encoding="utf-8")

        self.assertIn("????????????_dstack-sidecar-1", box)
        self.assertIn('tombstone_running" != false', box)
        self.assertIn('tombstone_project" != dstack', box)
        self.assertIn('tombstone_service" != sidecar', box)
        self.assertIn('docker rm "$tombstone_id"', box)
        self.assertNotIn('docker rm -f "$tombstone_id"', box)


if __name__ == "__main__":
    unittest.main()
