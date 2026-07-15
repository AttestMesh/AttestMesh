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


if __name__ == "__main__":
    unittest.main()
