#!/usr/bin/env python3
"""Static production contracts for the measured sandboxd node pre-launch."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "deploy" / "sandboxd-node-box.py").read_text()


class SandboxdNodeContractTests(unittest.TestCase):
    def test_cpu_profile_is_exact_and_does_not_auto_expand(self) -> None:
        self.assertIn("EXPECTED_HOST_VCPUS=8", SOURCE)
        self.assertIn(
            '[ "$actual_vcpus" -eq "$EXPECTED_HOST_VCPUS" ]',
            SOURCE,
        )
        self.assertNotIn("MIN_HOST_VCPUS", SOURCE)
        self.assertIn("if VCPU != 8:", SOURCE)
        self.assertIn("if MEM != 16384:", SOURCE)
        self.assertIn("if DISK != 300:", SOURCE)

        deploy_script = (ROOT / "deploy" / "sandboxd-node.sh").read_text()
        self.assertIn("(.vcpu | tonumber) == ($vcpu | tonumber)", deploy_script)
        self.assertIn("(.memory | tonumber) == ($memory | tonumber)", deploy_script)
        self.assertIn("(.disk_size | tonumber) == ($disk | tonumber)", deploy_script)

    def test_measured_prelaunch_shadows_vendor_docker_affinity(self) -> None:
        self.assertIn(
            'DOCKER_AFFINITY_DIR="/etc/systemd/system/docker.service.d"',
            SOURCE,
        )
        self.assertRegex(SOURCE, r"(?m)^CPUAffinity=0 1 2 3 4 5 6 7$")
        self.assertIn(
            'mv -f "$DOCKER_AFFINITY_TMP" "$DOCKER_AFFINITY_DIR/override.conf"',
            SOURCE,
        )
        self.assertLess(
            SOURCE.index('chmod 0644 "$DOCKER_AFFINITY_TMP"'),
            SOURCE.index('mv -f "$DOCKER_AFFINITY_TMP"'),
        )
        self.assertLess(
            SOURCE.index("systemctl daemon-reload"),
            SOURCE.index("systemctl restart docker"),
        )

    def test_prelaunch_fails_closed_on_docker_cpu_mismatch(self) -> None:
        self.assertIn("systemctl show docker.service --property MainPID --value", SOURCE)
        self.assertIn('docker_cpu_affinity" = "0-7"', SOURCE)
        self.assertIn("docker info --format '{{.NCPU}}'", SOURCE)
        self.assertIn(
            '[ "$docker_ncpu" -eq "$EXPECTED_HOST_VCPUS" ]',
            SOURCE,
        )
        # The tenant sellable ceiling stays explicit and below the machine size.
        compose = (ROOT / "deploy" / "compose" / "sandboxd-node.yaml").read_text()
        self.assertRegex(compose, r'HOST_VCPU_MILLIS:\s*"5000"')
        self.assertRegex(compose, r'HOST_CPU_OVERCOMMIT:\s*"1[.]0"')
        self.assertIn("test \"$$(docker info --format '{{.NCPU}}')\" = 8", compose)

    def test_update_commits_hash_only_after_target_health(self) -> None:
        deploy_script = (ROOT / "deploy" / "sandboxd-node.sh").read_text()
        readback_gate = deploy_script.index("VMM did not read back target compose")
        health_gate = deploy_script.index('_wait_health 45 10 "$target_h"')
        inventory_gate = deploy_script.index('_inventory_matches "$VM_ID"', health_gate)
        commit_hash = deploy_script.index('H="$target_h"', inventory_gate)
        save_state = deploy_script.index("  _save", commit_hash)
        self.assertLess(readback_gate, health_gate)
        self.assertLess(health_gate, inventory_gate)
        self.assertLess(inventory_gate, commit_hash)
        self.assertLess(commit_hash, save_state)


if __name__ == "__main__":
    unittest.main()
