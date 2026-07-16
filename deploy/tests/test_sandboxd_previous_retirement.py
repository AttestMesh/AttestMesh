from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SCRIPT = (ROOT / "deploy" / "sandboxd-node.sh").read_text()
START = DEPLOY_SCRIPT.index("retire_previous_cvm() {")
END = DEPLOY_SCRIPT.index("\nupdate_member() {", START)
RETIRE_PREVIOUS = DEPLOY_SCRIPT[START:END]


class SandboxdPreviousRetirementTests(unittest.TestCase):
    app_id = "0x" + "44" * 20
    current_vm = "vm-current"
    previous_vm = "vm-previous"
    current_hash = "11" * 32
    previous_hash = "22" * 32

    def run_retirement(
        self,
        *,
        opt_in: str = "",
        phase: str = "",
        retire_app_id: str = "",
        retire_current_vm: str = "",
        retire_vm: str = "",
        retire_current_hash: str = "",
        retire_hash: str = "",
        retire_current_profile: tuple[str, str, str] = ("", "", ""),
        retire_previous_profile: tuple[str, str, str] = ("", "", ""),
    ) -> tuple[subprocess.CompletedProcess[str], str, str]:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            calls_file = root / "calls"
            saves_file = root / "saves"
            calls_file.write_text("")
            saves_file.write_text("")
            values = {
                "X": self.app_id,
                "VM_ID": self.current_vm,
                "PREVIOUS_VM_ID": self.previous_vm,
                "H": "33" * 32,
                "UPDATE_PHASE": "upgraded",
                "UPDATE_VM_ID": self.current_vm,
                "UPDATE_H": self.current_hash,
                "REPLACEMENT_PHASE": "",
                "BOX_VCPU": "8",
                "BOX_MEM": "16384",
                "BOX_DISK": "300",
                "STATE": "/unused",
                "NODE": "test",
                "SANDBOXD_RETIRE_PREVIOUS_VM_ID": opt_in,
                "PREVIOUS_RETIRE_PHASE": phase,
                "PREVIOUS_RETIRE_APP_ID": retire_app_id,
                "PREVIOUS_RETIRE_CURRENT_VM_ID": retire_current_vm,
                "PREVIOUS_RETIRE_VM_ID": retire_vm,
                "PREVIOUS_RETIRE_CURRENT_H": retire_current_hash,
                "PREVIOUS_RETIRE_CURRENT_VCPU": retire_current_profile[0],
                "PREVIOUS_RETIRE_CURRENT_MEM": retire_current_profile[1],
                "PREVIOUS_RETIRE_CURRENT_DISK": retire_current_profile[2],
                "PREVIOUS_RETIRE_H": retire_hash,
                "PREVIOUS_RETIRE_VCPU": retire_previous_profile[0],
                "PREVIOUS_RETIRE_MEM": retire_previous_profile[1],
                "PREVIOUS_RETIRE_DISK": retire_previous_profile[2],
                "CALLS_FILE": str(calls_file),
                "SAVES_FILE": str(saves_file),
            }
            assignments = "\n".join(
                f"{name}={shlex.quote(value)}" for name, value in values.items()
            )
            harness = f"""set -uo pipefail
{RETIRE_PREVIOUS}
{assignments}
_load() {{ :; }}
_require_env() {{ :; }}
log() {{ :; }}
die() {{ echo "DIE:$*" >&2; exit 91; }}
_save() {{
  printf '%s|%s|%s|%s\n' "$PREVIOUS_RETIRE_PHASE" "$PREVIOUS_VM_ID" \
    "$PREVIOUS_RETIRE_VM_ID" "$PREVIOUS_RETIRE_CURRENT_H" >> "$SAVES_FILE"
}}
_box_run() {{
  local mode="$1"
  case "$mode" in
    describe)
      if [ "$3" = "$VM_ID" ]; then
        printf '{{"vm_id":"%s","found":true,"status":"running","vcpu":8,"memory":16384,"disk_size":300,"app_id":"%s","compose_hash":"%s"}}\n' \
          "$VM_ID" "$X" "$UPDATE_H"
      else
        printf '{{"vm_id":"%s","found":true,"status":"stopped","vcpu":4,"memory":8192,"disk_size":80,"app_id":"%s","compose_hash":"%s"}}\n' \
          "$PREVIOUS_VM_ID" "$X" "{self.previous_hash}"
      fi
      ;;
    retire-previous)
      printf 'retire:%s:%s:%s\n' "$2" "$3" "$5" >> "$CALLS_FILE"
      printf '{{"app_id":"%s","current_vm_id":"%s","previous_vm_id":"%s","already_removed":false}}\n' \
        "$X" "$VM_ID" "$PREVIOUS_VM_ID"
      ;;
    *) return 1 ;;
  esac
}}
retire_previous_cvm
printf 'RESULT:%s|%s|%s\n' "$PREVIOUS_VM_ID" "$PREVIOUS_RETIRE_PHASE" "$PREVIOUS_RETIRE_VM_ID"
"""
            result = subprocess.run(
                ["bash", "-c", harness],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={"PATH": os.environ["PATH"]},
                check=False,
            )
            return result, calls_file.read_text(), saves_file.read_text()

    def test_exact_id_opt_in_is_required_before_discovery_or_intent(self) -> None:
        result, calls, saves = self.run_retirement()
        self.assertEqual(result.returncode, 91)
        self.assertEqual(calls, "")
        self.assertEqual(saves, "")
        self.assertIn("SANDBOXD_RETIRE_PREVIOUS_VM_ID", result.stderr)

    def test_intent_and_removing_are_saved_before_checked_remove_and_clear(
        self,
    ) -> None:
        result, calls, saves = self.run_retirement(opt_in=self.previous_vm)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            calls, f"retire:{self.app_id}:{self.current_vm}:{self.previous_vm}\n"
        )
        prepared = (
            f"prepared|{self.previous_vm}|{self.previous_vm}|{self.current_hash}\n"
        )
        removing = (
            f"removing|{self.previous_vm}|{self.previous_vm}|{self.current_hash}\n"
        )
        cleared = "|||\n"
        self.assertIn(prepared, saves)
        self.assertIn(removing, saves)
        self.assertTrue(saves.endswith(cleared), saves)
        self.assertLess(saves.index(prepared), saves.index(removing))
        self.assertIn("RESULT:||", result.stdout)

    def test_removing_phase_retries_without_rediscovering_predecessor(self) -> None:
        result, calls, saves = self.run_retirement(
            opt_in=self.previous_vm,
            phase="removing",
            retire_app_id=self.app_id,
            retire_current_vm=self.current_vm,
            retire_vm=self.previous_vm,
            retire_current_hash=self.current_hash,
            retire_hash=self.previous_hash,
            retire_current_profile=("8", "16384", "300"),
            retire_previous_profile=("4", "8192", "80"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            calls, f"retire:{self.app_id}:{self.current_vm}:{self.previous_vm}\n"
        )
        self.assertEqual(saves, "|||\n")


if __name__ == "__main__":
    unittest.main()
