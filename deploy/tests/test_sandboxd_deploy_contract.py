from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
COMPOSE = (ROOT / "deploy" / "compose" / "sandboxd-node.yaml").read_text(
    encoding="utf-8"
)
PRELAUNCH = (ROOT / "deploy" / "sandboxd-node-box.py").read_text(encoding="utf-8")


class SandboxdDeployContract(unittest.TestCase):
    def test_release_pins_one_daemon_image_and_enables_bounded_exec(self) -> None:
        daemon_digest = (
            "ghcr.io/dmvt/confidential-sandboxes@sha256:"
            "786720fcac62597c536ba30337954b472fb40c5844882c01089a61a232accce4"
        )
        self.assertEqual(COMPOSE.count(daemon_digest), 3)
        self.assertIn(f'QUOTA_TOOLS_IMAGE="{daemon_digest}"', PRELAUNCH)
        self.assertIn('SANDBOXD_ENABLE_EXEC: "1"', COMPOSE)
        self.assertNotIn('SANDBOXD_ENABLE_EXEC: "0"', COMPOSE)

    def test_unique_host_tls_exposes_canonical_and_literal_mcp_ports(self) -> None:
        self.assertIn('SANDBOXD_PUBLIC_HTTPS_PORTS: "443,8787"', COMPOSE)
        self.assertIn('*.{$$SANDBOX_APPS_DOMAIN}:8787', COMPOSE)
        self.assertIn('[.]{$$SANDBOX_APPS_DOMAIN}(?::8787)?$', COMPOSE)
        self.assertIn('(?::8787)?$', COMPOSE)
        self.assertNotIn(r'\.sandbox\.synclave\.net', COMPOSE)
        self.assertIn('- "443:443"', COMPOSE)
        self.assertIn('- "8787:8787"', COMPOSE)
        self.assertIn("reverse_proxy frontproxy:8088", COMPOSE)

    def test_secret_runtime_is_shared_tmpfs_and_executable(self) -> None:
        self.assertIn("SANDBOXD_SECRET_RUNTIME_ROOT: /run/sandboxd-secrets", COMPOSE)
        self.assertIn("/run/sandboxd-secrets:/run/sandboxd-secrets", COMPOSE)
        self.assertIn('[ "$(stat -f -c %T /run)" = "tmpfs" ]', PRELAUNCH)
        self.assertIn("host /run is noexec", PRELAUNCH)
        self.assertIn("core:\n        soft: 0\n        hard: 0", COMPOSE)

    def test_source_builder_is_pinned_runsc_bounded_and_socket_only(self) -> None:
        tools = COMPOSE.split("  sandbox-builder-tools:", 1)[1].split(
            "\n  sandbox-builder:", 1
        )[0]
        builder = COMPOSE.split("  sandbox-builder:", 1)[1].split(
            "\n  sandboxd:", 1
        )[0]
        daemon = COMPOSE.split("\n  sandboxd:", 1)[1].split(
            "\n  tlsproxy:", 1
        )[0]

        self.assertIn("network_mode: none", tools)
        self.assertIn("/usr/local/libexec/runsc-buildkit-shim", tools)
        self.assertIn(
            "image: moby/buildkit@sha256:6b59b7df63a8cb9902736f9ddf7fcff8261613d3e7449b8ea8b7537fc399c03a",
            builder,
        )
        self.assertIn(
            "--oci-worker-binary=/opt/synclave/runsc-buildkit-shim", builder
        )
        self.assertIn("--oci-worker-snapshotter=native", builder)
        self.assertIn("/var/lib/cni:rw,nosuid,nodev,noexec,size=16m", builder)
        self.assertIn("/dstack/persistent/bin/runsc:/usr/local/bin/runsc:ro", builder)
        self.assertIn("sandboxd-builder-tools:/opt/synclave:ro", builder)
        self.assertIn("sandboxd-buildkit-socket:/run/buildkit", builder)
        self.assertIn('cpus: "0.50"', builder)
        self.assertNotIn("dstack.sock", builder)
        self.assertNotIn("SANDBOX_DAEMON_TOKEN", builder)
        self.assertIn('SANDBOXD_REQUIRE_BUILDER: "1"', daemon)
        self.assertIn('cpus: "1.0"', daemon)
        self.assertIn(
            "SANDBOXD_BUILD_REPOSITORY: ghcr.io/attestmesh/synclave-workloads",
            daemon,
        )
        self.assertNotIn("ghcr.io/dmvt/cs-sandbox-base", COMPOSE)
        self.assertIn('DOCKER_DATA_LIMIT="22G"', PRELAUNCH)
        self.assertIn('COMBINED_CACHE_LIMIT="28G"', PRELAUNCH)
        self.assertIn('BUILDKIT_LIMIT="6G"', PRELAUNCH)
        self.assertIn('HOST_VCPU_MILLIS: "5000"', daemon)
        self.assertIn("csbuild0 -j SANDBOXD-TENANT", PRELAUNCH)
        self.assertIn("csbuild0 -j REJECT", PRELAUNCH)


if __name__ == "__main__":
    unittest.main()
