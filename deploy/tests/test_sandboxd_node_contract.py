#!/usr/bin/env python3
"""Static production contracts for the measured sandboxd node pre-launch."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "deploy" / "sandboxd-node-box.py").read_text()
COMPOSE_SOURCE = (ROOT / "deploy" / "compose" / "sandboxd-node.yaml").read_text()


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

    def test_host_cgroup_readback_is_measured_and_fail_closed(self) -> None:
        compose = COMPOSE_SOURCE
        self.assertRegex(compose, r"(?m)^\s+cgroup:\s+host$")
        self.assertRegex(compose, r'SANDBOXD_REQUIRE_HOST_CGROUP:\s*"1"')
        self.assertIn("SANDBOXD_HOST_CGROUP_ROOT: /host/sys/fs/cgroup", compose)
        self.assertIn("SANDBOXD_EXPECTED_CGROUP_DRIVER: systemd", compose)
        self.assertIn("source: /sys/fs/cgroup", compose)
        self.assertIn("target: /host/sys/fs/cgroup", compose)
        self.assertIn("read_only: true", compose)
        self.assertIn("create_host_path: false", compose)
        self.assertIn(
            "test \"$$(docker info --format '{{.CgroupVersion}}/{{.CgroupDriver}}')\" = 2/systemd",
            compose,
        )

        self.assertIn('"native.cgroupdriver=systemd"', SOURCE)
        self.assertIn('."cgroup-parent" = "system.slice"', SOURCE)
        self.assertIn('"cgroup-parent": "system.slice"', SOURCE)
        self.assertIn("findmnt -n -o FSTYPE /sys/fs/cgroup", SOURCE)
        self.assertIn("docker info --format '{{.CgroupVersion}}/{{.CgroupDriver}}'", SOURCE)
        self.assertEqual(SOURCE.count('\nDOCKER_CONFIG='), 1)
        self.assertIn(
            'DOCKER_DAEMON_CONFIG=/etc/docker/daemon.json', SOURCE
        )
        validate = SOURCE.index(
            'dockerd --validate --config-file "$DOCKER_DAEMON_CONFIG_NEW"'
        )
        install = SOURCE.index(
            'mv -f "$DOCKER_DAEMON_CONFIG_NEW" "$DOCKER_DAEMON_CONFIG"'
        )
        restart = SOURCE.index("systemctl restart docker")
        self.assertLess(validate, install)
        self.assertLess(install, restart)

        self.assertIn("HOST_GUEST_PIDS_BUDGET=4096", SOURCE)
        self.assertIn("HOST_MAX_SANDBOXES=32", SOURCE)
        self.assertIn("RUNSC_HOST_PIDS_PER_GUEST_TASK=18", SOURCE)
        self.assertIn("RUNSC_FIXED_HOST_PID_OVERHEAD=512", SOURCE)
        self.assertIn("HOST_TASK_RESERVE=24576", SOURCE)
        self.assertIn("sysctl -n kernel.threads-max", SOURCE)
        self.assertIn("sysctl -n kernel.pid_max", SOURCE)
        self.assertIn("/sys/fs/cgroup/system.slice/pids.max", SOURCE)
        self.assertIn(
            "systemctl show --property ControlGroup --value -- system.slice", SOURCE
        )
        self.assertIn("awk 'NR == 1 {print $4}' /proc/loadavg", SOURCE)
        self.assertNotIn("find /proc -mindepth", SOURCE)
        self.assertIn(
            '[ "$threads_max" -ge "$REQUIRED_HOST_TASK_CAPACITY" ]', SOURCE
        )
        self.assertIn(
            '[ "$global_tasks_current" -le "$HOST_TASK_RESERVE" ]', SOURCE
        )
        self.assertRegex(compose, r'HOST_PIDS:\s*"4096"')
        self.assertRegex(compose, r'HOST_MAX_SANDBOXES:\s*"32"')
        self.assertIn('threads >= 114688 and pids > 114688', compose)
        self.assertIn('/host/sys/fs/cgroup/system.slice/pids.max', compose)
        self.assertIn('response_header_timeout 70s', compose)
        self.assertNotIn('response_header_timeout 30s', compose)

        def shell_integer(name: str) -> int:
            match = re.search(rf"(?m)^{re.escape(name)}=([0-9]+)$", SOURCE)
            self.assertIsNotNone(match, name)
            return int(match.group(1))

        guest_pids = shell_integer("HOST_GUEST_PIDS_BUDGET")
        max_sandboxes = shell_integer("HOST_MAX_SANDBOXES")
        per_guest = shell_integer("RUNSC_HOST_PIDS_PER_GUEST_TASK")
        fixed = shell_integer("RUNSC_FIXED_HOST_PID_OVERHEAD")
        reserve = shell_integer("HOST_TASK_RESERVE")
        runtime_budget = per_guest * guest_pids + fixed * max_sandboxes
        self.assertEqual(runtime_budget, 90_112)
        self.assertEqual(runtime_budget + reserve, 114_688)

        service_env = yaml.safe_load(compose)["services"]["sandboxd"]["environment"]
        self.assertEqual(int(service_env["HOST_PIDS"]), guest_pids)
        self.assertEqual(int(service_env["HOST_MAX_SANDBOXES"]), max_sandboxes)

        static_gate = SOURCE.index(
            '[ "$threads_max" -ge "$REQUIRED_HOST_TASK_CAPACITY" ]'
        )
        first_install_mutation = SOURCE.index('mkdir -p "$INSTALL_DIR"')
        final_quiesce_check = SOURCE.index(
            'failed to stop container before dockerd restart'
        )
        baseline_gate = SOURCE.index(
            '[ "$global_tasks_current" -le "$HOST_TASK_RESERVE" ]'
        )
        self.assertLess(static_gate, first_install_mutation)
        self.assertLess(final_quiesce_check, baseline_gate)

        service = yaml.safe_load(compose)["services"]["sandboxd"]
        self.assertEqual(service["cgroup"], "host")
        self.assertEqual(service["pid"], "host")
        self.assertEqual(service["environment"]["SANDBOXD_REQUIRE_HOST_CGROUP"], "1")
        cgroup_mounts = [
            mount
            for mount in service["volumes"]
            if isinstance(mount, dict)
            and mount.get("target") == "/host/sys/fs/cgroup"
        ]
        self.assertEqual(
            cgroup_mounts,
            [
                {
                    "type": "bind",
                    "source": "/sys/fs/cgroup",
                    "target": "/host/sys/fs/cgroup",
                    "read_only": True,
                    "bind": {"create_host_path": False},
                }
            ],
        )

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
        inventory_gate = deploy_script.index(
            '_inventory_exact_single "$VM_ID" steady', health_gate
        )
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

    def test_update_mutation_is_bound_to_both_journal_hashes_and_profile(self) -> None:
        deploy_script = (ROOT / "deploy" / "sandboxd-node.sh").read_text()
        update_start = deploy_script.index("update_member() {")
        update_end = deploy_script.index("\n_validate_replacement_vm()", update_start)
        update = deploy_script[update_start:update_end]
        self.assertIn(
            '_box_run update "$X" "$VM_ID" "$UPDATE_PREVIOUS_H" "$UPDATE_H"',
            update,
        )
        self.assertIn('"$BOX_VCPU" "$BOX_MEM" "$BOX_DISK"', update)
        self.assertEqual(
            update.count('_box_run checked-start "$X" "$VM_ID" "$UPDATE_H"'), 2
        )
        self.assertNotIn('_box_run start "" "$VM_ID"', update)

    def test_box_update_uses_one_snapshot_and_checked_stop_upgrade_start(self) -> None:
        helper = (ROOT / "deploy" / "sandboxd-node-box.py").read_text()
        compose_function = helper.index("def app_compose_and_hash(")
        snapshot = helper.index("Path(COMPOSE_PATH).read_text()", compose_function)
        self.assertEqual(helper.count("Path(COMPOSE_PATH).read_text()"), 1)
        self.assertNotIn("open(COMPOSE_PATH)", helper)

        update = helper.index("def checked_update_vm(")
        rendered = helper.index(
            "compose_file, compose_hash = app_compose_and_hash", update
        )
        target_gate = helper.index("if compose_hash != target_hash:", rendered)
        stop = helper.index("stop_vm_for_checked_update(", target_gate)
        upgrade = helper.index('"UpgradeApp"', stop)
        target_readback = helper.index('context="post-upgrade"', upgrade)
        checked_start = helper.index("start = checked_start_vm(", target_readback)
        self.assertLess(snapshot, update)
        self.assertLess(rendered, target_gate)
        self.assertLess(target_gate, stop)
        self.assertLess(stop, upgrade)
        self.assertLess(upgrade, target_readback)
        self.assertLess(target_readback, checked_start)

    def test_box_inputs_are_unique_root_owned_and_node_locked(self) -> None:
        deploy_script = (ROOT / "deploy" / "sandboxd-node.sh").read_text()
        box_run = deploy_script.index("_box_run() {")
        box_run_end = deploy_script.index("\ndeploy_cvm()", box_run)
        body = deploy_script[box_run:box_run_end]
        self.assertIn("mktemp -d /tmp/sandboxd-node.XXXXXXXX", body)
        self.assertIn("sudo install -o root -g root -m 0400", body)
        self.assertIn("remote sandboxd helper integrity mismatch", body)
        self.assertIn('lock_key="${NODE//[^a-zA-Z0-9_.-]/_}"', body)
        self.assertIn("flock -w 60", body)
        self.assertNotIn("/tmp/${NODE}.yaml", body)
        self.assertNotIn("/tmp/sandboxd-node-box.py", body)

    def test_previous_vm_retirement_is_explicit_durable_and_checked(self) -> None:
        deploy_script = (ROOT / "deploy" / "sandboxd-node.sh").read_text()
        helper = (ROOT / "deploy" / "sandboxd-node-box.py").read_text()
        retirement = deploy_script.index("retire_previous_cvm() {")
        update = deploy_script.index("\nupdate_member() {", retirement)
        body = deploy_script[retirement:update]
        self.assertIn("SANDBOXD_RETIRE_PREVIOUS_VM_ID", body)
        for field in (
            "PREVIOUS_RETIRE_PHASE",
            "PREVIOUS_RETIRE_APP_ID",
            "PREVIOUS_RETIRE_CURRENT_VM_ID",
            "PREVIOUS_RETIRE_VM_ID",
            "PREVIOUS_RETIRE_CURRENT_H",
            "PREVIOUS_RETIRE_H",
            "PREVIOUS_RETIRE_CURRENT_VCPU",
            "PREVIOUS_RETIRE_CURRENT_MEM",
            "PREVIOUS_RETIRE_CURRENT_DISK",
            "PREVIOUS_RETIRE_VCPU",
            "PREVIOUS_RETIRE_MEM",
            "PREVIOUS_RETIRE_DISK",
        ):
            self.assertIn(f"{field}=${{{field}:-}}", deploy_script)
        intent = body.index("PREVIOUS_RETIRE_PHASE=prepared")
        intent_save = body.index("      _save", intent)
        removing = body.index("PREVIOUS_RETIRE_PHASE=removing", intent_save)
        removing_save = body.index("    _save", removing)
        mutation = body.index("_box_run retire-previous", removing_save)
        clear = body.index("  PREVIOUS_VM_ID=", mutation)
        clear_save = body.index("  _save", clear)
        self.assertLess(intent, intent_save)
        self.assertLess(intent_save, removing)
        self.assertLess(removing, removing_save)
        self.assertLess(removing_save, mutation)
        self.assertLess(mutation, clear)
        self.assertLess(clear, clear_save)

        checked = helper.index("def checked_retire_previous_vm(")
        inventory = helper.index("require_retirement_inventory(", checked)
        remove = helper.index('m.vmm("RemoveVm"', inventory)
        post = helper.index('context="post-retirement"', remove)
        self.assertLess(inventory, remove)
        self.assertLess(remove, post)

    def test_replacement_cannot_orphan_an_existing_predecessor(self) -> None:
        deploy_script = (ROOT / "deploy" / "sandboxd-node.sh").read_text()
        replacement = deploy_script.index("replace_cvm() {")
        smoke = deploy_script.index("\nsmoke() {", replacement)
        body = deploy_script[replacement:smoke]
        previous_gate = body.index('[ -z "${PREVIOUS_VM_ID:-}" ]')
        inventory_gate = body.index(
            '_inventory_exact_single "$VM_ID" steady', previous_gate
        )
        hash_measure = body.index("nh=$(_box_run hash", inventory_gate)
        create = body.index("_box_run create-replacement", hash_measure)
        self.assertLess(previous_gate, inventory_gate)
        self.assertLess(inventory_gate, hash_measure)
        self.assertLess(hash_measure, create)

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
        inventory = deploy_script.index('_inventory_exact_single "$VM_ID"', reread)
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
