from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class SynclaveNodeContractTests(unittest.TestCase):
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
