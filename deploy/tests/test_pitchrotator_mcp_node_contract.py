from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DRIVER = ROOT / "deploy/pitchrotator-mcp-node.sh"
BOX = ROOT / "deploy/pitchrotator-mcp-node-box.py"
COMPOSE = ROOT / "deploy/compose/pitchrotator-mcp-node.yaml"
WORKFLOW = ROOT / "deploy/workflows/pitchrotator-mcp-node.tsx"
UPSTREAM_COMMIT = "66b5495b0ea0695ef6d2a35969d444da4f680a52"
UPSTREAM_TREE = "30ef21a38034bf1d1f7001445a6feea89a424cb3"
SOURCE_ARCHIVE_SHA256 = (
    "57aa6a29108cdaa5a46cd6d12b962c7c01c8ca824882b77f16767ea395843e1d"
)
LOCKFILE_SHA256 = "3a3e75e10c0ebb9ed132cf93fd4641cc3c8d043c55e4a443ac42f32c25d73342"


class PitchRotatorMcpNodeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.driver = DRIVER.read_text(encoding="utf-8")
        cls.box = BOX.read_text(encoding="utf-8")
        cls.compose = COMPOSE.read_text(encoding="utf-8")
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_upstream_source_and_every_base_image_are_immutable(self) -> None:
        deployment = self.driver + self.box + self.compose
        self.assertIn(UPSTREAM_COMMIT, deployment)
        self.assertIn(UPSTREAM_TREE, deployment)
        self.assertIn(SOURCE_ARCHIVE_SHA256, deployment)
        self.assertIn(LOCKFILE_SHA256, deployment)
        self.assertIn(f"PITCHROTATOR_SOURCE_COMMIT={UPSTREAM_COMMIT}", self.compose)
        self.assertNotIn("${PITCHROTATOR_SOURCE_COMMIT}", self.compose)
        self.assertNotIn("${PUBLIC_URL}", self.compose)

        images = re.findall(r"(?m)^\s*image:\s*([^\s#]+)", self.compose)
        self.assertTrue(images, "compose must declare its deterministic workload image")
        for image in images:
            if image == "${PITCHROTATOR_IMAGE}":
                self.assertRegex(
                    self.driver,
                    r"PITCHROTATOR_IMAGE[^\n]*@sha256:[^\n]*[0-9a-f]",
                    "the driver must reject a workload image without a full digest",
                )
                self.assertRegex(
                    self.driver,
                    r"(?i)(render|envsubst|substitut)[^\n]*compose",
                    "the image must be bound into a rendered compose before hashing",
                )
                self.assertIn("RENDERED_COMPOSE=$(mktemp", self.driver)
                box_run = self.driver.split("_box_run()", 1)[1].split(
                    "preflight()", 1
                )[0]
                self.assertLess(box_run.index("_render_compose"), box_run.index("scp"))
                self.assertRegex(box_run, r'scp[^\n]*"\$RENDERED_COMPOSE"')
                self.assertIn("hash=$(_box_run hash", self.driver)
                self.assertNotRegex(
                    re.search(r"ENV_KEYS\s*=\s*\[(.*?)\]", self.box, re.DOTALL).group(1),
                    r"PITCHROTATOR_IMAGE",
                    "the admitted image cannot vary through sealed runtime env",
                )
            else:
                self.assertRegex(
                    image,
                    r"@sha256:[0-9a-f]{64}$",
                    f"mutable or unpinned image reference: {image}",
                )
        self.assertNotRegex(deployment, r"(?i):latest(?:\s|$)")

    def test_production_compose_cannot_enable_simulated_attestation(self) -> None:
        self.assertNotRegex(
            self.compose,
            r"(?m)^\s*-\s*(?:ALLOW_INSECURE_NO_TEE|DSTACK_SIMULATOR_ENDPOINT)\s*=",
        )
        self.assertNotRegex(
            self.driver + self.box,
            r"(?m)^(?!\s*#).*\b(?:ALLOW_INSECURE_NO_TEE|DSTACK_SIMULATOR_ENDPOINT)\b",
        )

    def test_service_is_mesh_only_and_has_no_public_gateway_or_host_ports(self) -> None:
        deployment = self.driver + self.box
        self.assertRegex(self.box, r'BOX_GATEWAY_ENABLED",\s*"false"')
        self.assertRegex(self.box, r'BOX_PORTS",\s*"\[\]"')
        workload = self.compose.split("  pitchrotator-mcp:", 1)[1].split(
            "  app-egress-fw:", 1
        )[0]
        self.assertNotRegex(workload, r"(?m)^\s*ports\s*:")
        self.assertIn('bind="$${ip}"', self.compose)

    def test_model_secret_is_sealed_and_not_exposed_as_a_cli_option(self) -> None:
        deployment = self.driver + self.box + self.compose
        self.assertIn("OPENROUTER_API_KEY", self.compose)
        self.assertRegex(
            self.driver,
            r"printf\s+['\"]E_OPENROUTER_API_KEY=%q",
            "the model credential must enter the CVM through the sealed environment",
        )
        self.assertIn('"OPENROUTER_API_KEY"', self.box)
        self.assertNotRegex(deployment, r"(?m)^\s*set\s+-[^\n]*x")
        self.assertNotRegex(self.box, r"add_argument\([^\n]*OPENROUTER_API_KEY")
        self.assertNotRegex(self.driver, r"--[a-z0-9-]*(?:key|token)[= ]\"?\$OPENROUTER")

    def test_driver_exposes_repeatable_deploy_and_verification_verbs(self) -> None:
        for verb in (
            "deploy",
            "cluster",
            "patha",
            "prime",
            "bind",
            "start",
            "verify",
            "verify-mcp",
            "update",
            "all",
        ):
            self.assertRegex(self.driver, rf"(?m)^\s*{re.escape(verb)}\)")

        self.assertIn("/health", self.driver)
        self.assertIn("/attestation", self.driver)
        self.assertRegex(self.driver, r"(?i)trusted[^\n]*(true|jq)")
        self.assertRegex(self.driver, r"(?i)(mode[^\n]*tdx|tdx[^\n]*mode)")

    def test_driver_creates_a_dedicated_cluster_and_uses_path_a(self) -> None:
        self.assertNotRegex(
            self.driver + self.box,
            r"(?i)(matrix-node|pg-ha)[^\n]*(state|cluster)",
            "PitchRotator must not inherit membership from an existing fleet",
        )
        self.assertRegex(self.driver, r"(?m)^\s*cluster\)")
        self.assertRegex(self.driver, r"(?m)^\s*patha\)")

    def test_smithers_orders_mcp_smoke_after_membership_verification(self) -> None:
        self.assertIn('Input.parse(ctx.input || {})', self.workflow)
        self.assertIn("execFileSync", self.workflow)
        self.assertNotIn("OPENROUTER_API_KEY", self.workflow)
        task_ids = re.findall(r'<Task id="([^"]+)"', self.workflow)
        self.assertEqual(
            task_ids,
            [
                "deploy",
                "cluster",
                "patha",
                "prime",
                "bind",
                "start",
                "verify",
                "mcp",
            ],
        )
        self.assertIn('run("mcp", node, "verify-mcp")', self.workflow)


if __name__ == "__main__":
    unittest.main()
