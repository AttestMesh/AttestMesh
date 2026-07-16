from __future__ import annotations

import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class SynclaveNodeContractTests(unittest.TestCase):
    def test_compose_preserves_the_existing_dstack_project_identity(self) -> None:
        compose = (ROOT / "deploy/compose/synclave-node.yaml").read_text(
            encoding="utf-8"
        )

        self.assertIn("\nname: dstack\n", compose)
        self.assertNotIn("\nname: attestmesh-synclave\n", compose)

    def test_prelaunch_removes_only_the_stopped_compose_sidecar_tombstone(self) -> None:
        box = (ROOT / "deploy/synclave-node-box.py").read_text(encoding="utf-8")

        self.assertIn("????????????_dstack-sidecar-1", box)
        self.assertIn('tombstone_running" != false', box)
        self.assertIn('tombstone_project" != dstack', box)
        self.assertIn('tombstone_service" != sidecar', box)
        self.assertIn('docker rm "$tombstone_id"', box)
        self.assertNotIn('docker rm -f "$tombstone_id"', box)

    def test_failed_app_diagnostic_is_bounded_redacted_and_has_no_volumes(self) -> None:
        box = (ROOT / "deploy/synclave-node-box.py").read_text(encoding="utf-8")

        self.assertIn("diagnostic_name='dstack-app-diagnostic'", box)
        self.assertIn("app_name='dstack-app-1'", box)
        self.assertIn('app_project" = dstack', box)
        self.assertIn('app_service" = app', box)
        self.assertIn('app_image" = "$diagnostic_image"', box)
        self.assertIn('app_restarting" = true', box)
        self.assertIn('app_health" != healthy', box)
        self.assertIn('app_network="${app_networks[0]}"', box)
        self.assertIn("| tr -d '\\r' | sed '/^[[:space:]]*$/d'", box)
        self.assertIn('[ -z "$network_id" ] || [ -z "$endpoint_id" ]', box)
        self.assertIn('network_actual_id" != "$network_id', box)
        self.assertIn('network_project" != dstack', box)
        self.assertIn('network_role" != default', box)
        self.assertIn("mktemp /run/dstack-app-diagnostic.env.", box)
        self.assertIn('chmod 600 "$diagnostic_env"', box)
        self.assertIn("PASSWORD|SECRET|TOKEN|AUTHORIZATION|API_KEY|DATABASE_URL", box)
        self.assertNotIn("timeout ", box)
        self.assertIn('docker create --name "$diagnostic_name"', box)
        self.assertIn('docker start "$diagnostic_name"', box)
        self.assertIn('docker logs --follow "$diagnostic_name"', box)
        self.assertIn("diagnostic_deadline=$((SECONDS + 45))", box)
        self.assertIn("[ \"$SECONDS\" -lt \"$diagnostic_deadline\" ]", box)
        self.assertIn('--network "$app_network"', box)
        self.assertIn('--memory 4g', box)
        self.assertIn('--cpus 1.0', box)
        self.assertIn('--pids-limit 1024', box)
        self.assertIn('>/dev/null 2> >(redact_diagnostic_stderr', box)
        self.assertIn('docker rm --force "$diagnostic_name"', box)

        diagnostic_create = box.split("docker create --name", 1)[1].split(
            '"$diagnostic_image" >/dev/null', 1
        )[0]
        self.assertNotIn("--volume", diagnostic_create)
        self.assertNotIn(" -v ", diagnostic_create)

    def test_failed_app_diagnostic_redactor_removes_secret_markers(self) -> None:
        box = (ROOT / "deploy/synclave-node-box.py").read_text(encoding="utf-8")
        redactor = "redact_diagnostic_stderr() {" + box.split(
            "redact_diagnostic_stderr() {", 1
        )[1].split("\n}\n\nif docker inspect", 1)[0] + "\n}"
        database_marker = "SYNCLAVE_DIAGNOSTIC_DB_SECRET_9fc3"
        token_marker = "SYNCLAVE_DIAGNOSTIC_TOKEN_SECRET_10ad"

        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as env_file:
            env_file.write(
                "DATABASE_URL="
                f"postgresql://synclave:{database_marker}@sidecar:15431/synclave\n"
                f"TEE_DAEMON_TOKEN={token_marker}\n"
            )
            env_file.flush()
            command = (
                redactor
                + "\nprintf '%s\\n' "
                + shlex.quote(
                    "migration failed: "
                    f"postgresql://synclave:{database_marker}@sidecar:15431/synclave "
                    f"token={token_marker} safe-context"
                )
                + " | redact_diagnostic_stderr "
                + shlex.quote(env_file.name)
            )
            result = subprocess.run(
                ["bash", "-c", command],
                check=True,
                capture_output=True,
                text=True,
            )

        self.assertNotIn(database_marker, result.stdout)
        self.assertNotIn(token_marker, result.stdout)
        self.assertIn("[REDACTED]", result.stdout)
        self.assertIn("migration failed", result.stdout)
        self.assertIn("safe-context", result.stdout)

    def test_network_parser_drops_cr_and_whitespace_only_records(self) -> None:
        result = subprocess.run(
            [
                "bash",
                "-c",
                "mapfile -t networks < <(tr -d '\\r' | sed '/^[[:space:]]*$/d'); "
                "printf '%s:%s\\n' \"${#networks[@]}\" \"${networks[0]}\"",
            ],
            input="dstack_default\r\n \t\r\n\r\n",
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.stdout, "1:dstack_default\n")


if __name__ == "__main__":
    unittest.main()
