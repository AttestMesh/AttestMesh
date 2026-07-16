from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SCRIPT = (ROOT / "deploy" / "sandboxd-node.sh").read_text()
START = DEPLOY_SCRIPT.index("update_member() {")
END = DEPLOY_SCRIPT.index("\n_validate_replacement_vm()", START)
UPDATE_MEMBER = DEPLOY_SCRIPT[START:END]


class SandboxdUpdateJournalTests(unittest.TestCase):
    old_hash = "11" * 32
    target_hash = "22" * 32
    third_hash = "33" * 32
    vm_id = "vm-test"

    def run_update(
        self,
        *,
        current_hash: str,
        current_status: str = "running",
        recorded_hash: str | None = None,
        phase: str = "",
        update_vm_id: str = "",
        update_hash: str = "",
        previous_hash: str = "",
        recovery_hash: str = "",
    ) -> tuple[subprocess.CompletedProcess[str], str, str, str]:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            hash_file = root / "hash"
            status_file = root / "status"
            calls_file = root / "calls"
            saves_file = root / "saves"
            hash_file.write_text(current_hash)
            status_file.write_text(current_status)
            calls_file.write_text("")
            saves_file.write_text("")
            values = {
                "H": recorded_hash or self.old_hash,
                "UPDATE_PHASE": phase,
                "UPDATE_VM_ID": update_vm_id,
                "UPDATE_H": update_hash,
                "UPDATE_PREVIOUS_H": previous_hash,
                "RECOVERY": recovery_hash,
                "HASH_FILE": str(hash_file),
                "STATUS_FILE": str(status_file),
                "CALLS_FILE": str(calls_file),
                "SAVES_FILE": str(saves_file),
            }
            assignments = "\n".join(
                f"{name}={shlex.quote(value)}" for name, value in values.items()
            )
            harness = f"""set -uo pipefail
{UPDATE_MEMBER}
{assignments}
X=0x{'44' * 20}
VM_ID={self.vm_id}
CLUSTER=0x{'55' * 20}
REPLACEMENT_PHASE=
BOX_VCPU=8
BOX_MEM=16384
BOX_DISK=300
STATE=/unused
NODE=test
TARGET_HASH={self.target_hash}
if [ -n "$RECOVERY" ]; then
  SANDBOXD_UPDATE_RECOVERY_FROM_HASH="$RECOVERY"
fi
_load() {{ :; }}
_require_env() {{ :; }}
log() {{ :; }}
die() {{ echo "DIE:$*" >&2; exit 91; }}
_save() {{
  printf '%s|%s|%s|%s|%s\n' "$UPDATE_PHASE" "$UPDATE_VM_ID" \
    "$UPDATE_H" "$UPDATE_PREVIOUS_H" "$H" >> "$SAVES_FILE"
}}
_inventory_matches() {{
  local expected="$1" status
  status=$(cat "$STATUS_FILE")
  if [ "$expected" = none ]; then
    [ "$status" = stopped ] || [ "$status" = exited ]
  else
    [ "$expected" = "$VM_ID" ] && [ "$status" != stopped ] && [ "$status" != exited ]
  fi
}}
_wait_inventory() {{ _inventory_matches "$1"; }}
_allowlist_compose_hash() {{ :; }}
_wait_health() {{
  [ "$(cat "$HASH_FILE")" = "$3" ] && [ "$(cat "$STATUS_FILE")" = running ]
}}
_box_run() {{
  local mode="$1" hash status
  case "$mode" in
    describe)
      hash=$(cat "$HASH_FILE")
      status=$(cat "$STATUS_FILE")
      printf '{{"vm_id":"%s","found":true,"status":"%s","vcpu":8,"memory":16384,"disk_size":300,"app_id":"%s","compose_hash":"%s"}}\n' \
        "$VM_ID" "$status" "$X" "$hash"
      ;;
    hash)
      printf '%s\n' "$TARGET_HASH"
      ;;
    update)
      printf 'update\n' >> "$CALLS_FILE"
      printf '%s' "$TARGET_HASH" > "$HASH_FILE"
      printf 'running' > "$STATUS_FILE"
      printf '{{"app_id":"%s","compose_hash":"%s","vm_id":"%s"}}\n' \
        "$X" "$TARGET_HASH" "$VM_ID"
      ;;
    start)
      printf 'start\n' >> "$CALLS_FILE"
      printf 'running' > "$STATUS_FILE"
      ;;
    *) return 1 ;;
  esac
}}
update_member
printf 'RESULT:%s|%s|%s|%s|%s\n' "$H" "$UPDATE_PHASE" "$UPDATE_VM_ID" \
  "$UPDATE_H" "$UPDATE_PREVIOUS_H"
"""
            result = subprocess.run(
                ["bash", "-c", harness],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={"PATH": os.environ["PATH"]},
                check=False,
            )
            return (
                result,
                calls_file.read_text(),
                saves_file.read_text(),
                status_file.read_text(),
            )

    def test_normal_update_journals_every_phase_before_commit(self) -> None:
        result, calls, saves, status = self.run_update(current_hash=self.old_hash)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, "update\n")
        self.assertEqual(status, "running")
        self.assertIn(f"prepared|{self.vm_id}|{self.target_hash}|{self.old_hash}|{self.old_hash}", saves)
        self.assertIn(f"mutating|{self.vm_id}|{self.target_hash}|{self.old_hash}|{self.old_hash}", saves)
        self.assertIn(f"upgraded|{self.vm_id}|{self.target_hash}|{self.old_hash}|{self.old_hash}", saves)
        self.assertIn(f"RESULT:{self.target_hash}||||", result.stdout)

    def test_resume_at_target_does_not_mutate_again(self) -> None:
        result, calls, _, _ = self.run_update(
            current_hash=self.target_hash,
            phase="mutating",
            update_vm_id=self.vm_id,
            update_hash=self.target_hash,
            previous_hash=self.old_hash,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, "")
        self.assertIn(f"RESULT:{self.target_hash}||||", result.stdout)

    def test_resume_starts_exact_stopped_target_without_upgrade(self) -> None:
        result, calls, _, status = self.run_update(
            current_hash=self.target_hash,
            current_status="stopped",
            phase="mutating",
            update_vm_id=self.vm_id,
            update_hash=self.target_hash,
            previous_hash=self.old_hash,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, "start\n")
        self.assertEqual(status, "running")

    def test_third_hash_fails_closed_without_mutation(self) -> None:
        result, calls, _, _ = self.run_update(
            current_hash=self.third_hash,
            phase="mutating",
            update_vm_id=self.vm_id,
            update_hash=self.target_hash,
            previous_hash=self.old_hash,
        )
        self.assertEqual(result.returncode, 91)
        self.assertEqual(calls, "")
        self.assertIn("is neither previous", result.stderr)

    def test_legacy_drift_requires_exact_hash_recovery_value(self) -> None:
        denied, denied_calls, denied_saves, _ = self.run_update(
            current_hash=self.third_hash,
        )
        self.assertEqual(denied.returncode, 91)
        self.assertEqual(denied_calls, "")
        self.assertEqual(denied_saves, "")
        self.assertIn("SANDBOXD_UPDATE_RECOVERY_FROM_HASH", denied.stderr)

        accepted, calls, saves, _ = self.run_update(
            current_hash=self.third_hash,
            recovery_hash=self.third_hash,
        )
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertEqual(calls, "update\n")
        self.assertIn(
            f"prepared|{self.vm_id}|{self.target_hash}|{self.third_hash}|{self.old_hash}",
            saves,
        )


if __name__ == "__main__":
    unittest.main()
