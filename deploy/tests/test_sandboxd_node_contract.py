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
        self.assertIn(
            "systemctl show docker.service --property MainPID --value", SOURCE
        )
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

    def test_lifetime_ledgers_have_measured_hard_caps(self) -> None:
        compose = (ROOT / "deploy" / "compose" / "sandboxd-node.yaml").read_text()
        expected = {
            "SANDBOXD_MAX_LIFETIME_ALLOCATIONS": "512",
            "SANDBOXD_MAX_ATTESTATION_EVENTS_PER_SANDBOX": "17",
            "SANDBOXD_MAX_LIFETIME_ATTESTATION_EVENTS": "512",
            "SANDBOXD_MAX_LIFETIME_ATTESTATION_EVENT_BYTES": "2097152",
        }
        for name, value in expected.items():
            self.assertRegex(compose, rf'{name}:\s*"{value}"')

    def test_synclave_receives_the_measured_sandbox_app_domain(self) -> None:
        deploy_script = (ROOT / "deploy" / "synclave-node.sh").read_text()
        box_helper = (ROOT / "deploy" / "synclave-node-box.py").read_text()
        compose = (ROOT / "deploy" / "compose" / "synclave-node.yaml").read_text()

        self.assertIn(
            'SANDBOX_APPS_DOMAIN="${SANDBOX_APPS_DOMAIN:-sandbox.synclave.net}"',
            deploy_script,
        )
        self.assertIn("E_SANDBOX_APPS_DOMAIN=%q", deploy_script)
        self.assertIn(
            "SANDBOX_APPS_DOMAIN must be the dedicated sandbox.synclave.net zone",
            deploy_script,
        )
        self.assertIn('"SANDBOX_APPS_DOMAIN"', box_helper)
        self.assertIn("SANDBOX_APPS_DOMAIN: ${SANDBOX_APPS_DOMAIN}", compose)
        # Adding the sandbox origin must not displace the existing application session secret.
        self.assertIn("SESSION_SECRET: ${SESSION_SECRET}", compose)

    def test_update_commits_hash_only_after_target_health(self) -> None:
        deploy_script = (ROOT / "deploy" / "sandboxd-node.sh").read_text()
        readback_gate = deploy_script.index("VMM did not read back target compose")
        health_gate = deploy_script.index('_wait_health 45 10 "$UPDATE_H"')
        inventory_gate = deploy_script.index('_inventory_matches "$VM_ID"', health_gate)
        commit_hash = deploy_script.index('H="$UPDATE_H"', inventory_gate)
        save_state = deploy_script.index("  _save", commit_hash)
        self.assertLess(readback_gate, health_gate)
        self.assertLess(health_gate, inventory_gate)
        self.assertLess(inventory_gate, commit_hash)
        self.assertLess(commit_hash, save_state)

    def test_update_is_durably_reconcilable_and_rejects_a_third_hash(self) -> None:
        deploy_script = (ROOT / "deploy" / "sandboxd-node.sh").read_text()
        for field in (
            "UPDATE_PHASE",
            "UPDATE_VM_ID",
            "UPDATE_H",
            "UPDATE_PREVIOUS_H",
        ):
            self.assertIn(f"{field}=${{{field}:-}}", deploy_script)
        prepared = deploy_script.index("UPDATE_PHASE=prepared")
        prepared_save = deploy_script.index("    _save", prepared)
        mutating = deploy_script.index("UPDATE_PHASE=mutating", prepared_save)
        mutating_save = deploy_script.index("    _save", mutating)
        mutation = deploy_script.index('_box_run update "$X" "$VM_ID"', mutating_save)
        self.assertLess(prepared, prepared_save)
        self.assertLess(prepared_save, mutating)
        self.assertLess(mutating, mutating_save)
        self.assertLess(mutating_save, mutation)
        self.assertIn("SANDBOXD_UPDATE_RECOVERY_FROM_HASH", deploy_script)
        self.assertIn(
            "SANDBOXD_UPDATE_FAILED_TARGET_REBASE_FROM_HASH",
            deploy_script,
        )
        self.assertIn(
            "is neither previous 0x$UPDATE_PREVIOUS_H nor target 0x$UPDATE_H",
            deploy_script,
        )

    def test_failed_target_rebase_preserves_last_good_hash_and_journals_first(
        self,
    ) -> None:
        deploy_script = (ROOT / "deploy" / "sandboxd-node.sh").read_text()
        opt_in = deploy_script.index(
            'failed_target_rebase_h="${SANDBOXD_UPDATE_FAILED_TARGET_REBASE_FROM_HASH:-}"'
        )
        unhealthy = deploy_script.index('_wait_health 3 2 "$UPDATE_H"', opt_in)
        reread = deploy_script.index(
            'current=$(_box_run describe "" "$VM_ID")', unhealthy
        )
        inventory = deploy_script.index('_inventory_matches "$VM_ID"', reread)
        previous = deploy_script.index('UPDATE_PREVIOUS_H="$UPDATE_H"', inventory)
        target = deploy_script.index('UPDATE_H="$nh"', previous)
        prepared = deploy_script.index("UPDATE_PHASE=prepared", target)
        rebase_save = deploy_script.index("      _save", prepared)
        allowlist = deploy_script.index(
            '_allowlist_compose_hash "$UPDATE_H"', rebase_save
        )
        mutation = deploy_script.index('_box_run update "$X" "$VM_ID"', allowlist)
        final_health = deploy_script.index('_wait_health 45 10 "$UPDATE_H"', mutation)
        commit = deploy_script.index('H="$UPDATE_H"', final_health)
        self.assertLess(opt_in, unhealthy)
        self.assertLess(unhealthy, reread)
        self.assertLess(reread, inventory)
        self.assertLess(inventory, previous)
        self.assertLess(previous, target)
        self.assertLess(target, prepared)
        self.assertLess(prepared, rebase_save)
        self.assertLess(rebase_save, allowlist)
        self.assertLess(allowlist, mutation)
        self.assertLess(mutation, final_health)
        self.assertLess(final_health, commit)


if __name__ == "__main__":
    unittest.main()
