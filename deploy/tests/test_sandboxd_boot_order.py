from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]


class SandboxdBootOrder(unittest.TestCase):
    def test_dockerd_cannot_restore_sandboxd_before_xfs_mount(self):
        compose = (ROOT / "deploy/compose/sandboxd-node.yaml").read_text()
        service = re.search(r"(?ms)^  sandboxd:\n(?P<body>.*?)(?=^  [a-zA-Z0-9_-]+:|^volumes:)", compose)
        self.assertIsNotNone(service)
        self.assertRegex(service.group("body"), r'(?m)^    restart: ["\']no["\']$')

    def test_upgrade_neutralizes_legacy_restart_before_dockerd_restart(self):
        helper = (ROOT / "deploy/sandboxd-node-box.py").read_text()
        neutralize = helper.index('docker update --restart=no "$container_id"')
        restart = helper.index("systemctl restart docker")
        mount = helper.index('mount -t xfs -o prjquota,nosuid,nodev "$QUOTA_DEVICE"')
        self.assertLess(neutralize, restart)
        self.assertLess(restart, mount)

    def test_host_firewall_uses_shared_lock_and_atomic_chain_restore(self):
        helper = (ROOT / "deploy/sandboxd-node-box.py").read_text()
        self.assertIn("touch /run/xtables.lock", helper)
        self.assertIn(
            "--mount type=bind,src=/run/xtables.lock,dst=/run/xtables.lock", helper
        )
        self.assertIn('"$IPTR" -w 5 --noflush < "$restore_file"', helper)
        self.assertIn("net.bridge.bridge-nf-call-iptables=1", helper)
        self.assertNotIn("--physdev-is-bridged", helper)

    def test_guest_dashboard_is_closed_by_measured_port_policy(self):
        helper = (ROOT / "deploy/sandboxd-node-box.py").read_text()
        self.assertIn('"public_logs": False', helper)
        self.assertIn('"public_sysinfo": False', helper)
        self.assertIn('"restrict_mode": True', helper)
        for port in (443, 8080, 51900):
            self.assertIn(f'{{"port": {port}, "pp": False}}', helper)
        for forbidden in (8090, 9090, 9092):
            self.assertNotIn(f'{{"port": {forbidden}, "pp": False}}', helper)

        compose = (ROOT / "deploy/compose/sandboxd-node.yaml").read_text()
        self.assertNotRegex(compose, r"(?m)^\s+log:\s*$")


if __name__ == "__main__":
    unittest.main()
