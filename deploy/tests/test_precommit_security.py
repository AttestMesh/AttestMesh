from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import precommit_security as guard_module
from precommit_security import (
    GuardError,
    agent_payload,
    collect_staged_snapshot,
    format_finding,
    has_agent_reviewable_changes,
    local_findings,
    run_smithers_review,
    smithers_environment,
)


class GitFixture:
    def __init__(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="precommit-security-test-")
        self.root = Path(self.temp.name)
        self.git("init", "--quiet")
        self.git("config", "user.name", "Guard Test")
        self.git("config", "user.email", "guard@example.invalid")

    def close(self) -> None:
        self.temp.cleanup()

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def write(self, path: str, content: str) -> Path:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def add(self, *paths: str) -> None:
        self.git("add", "--", *paths)

    def commit(self, message: str = "fixture") -> None:
        self.git("-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m", message)


class StagedSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = GitFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    @staticmethod
    def write_smithers_result(
        db_path: Path,
        run_id: str,
        *,
        allowed: bool,
        findings: list[dict[str, object]] | None = None,
        enforced: bool = True,
        denied_specialist: str | None = None,
    ) -> None:
        with sqlite3.connect(db_path) as connection:
            connection.execute(
                'CREATE TABLE "review" ('
                "run_id TEXT, node_id TEXT, iteration INTEGER, clear INTEGER, findings TEXT)"
            )
            for node_id in (
                "backup-review",
                "credential-review",
                "adversarial-review",
            ):
                specialist_findings = (
                    [
                        {
                            "category": "other",
                            "severity": "high",
                            "path": "safe.py",
                            "line": 1,
                            "reason": "synthetic specialist denial",
                        }
                    ]
                    if node_id == denied_specialist
                    else []
                )
                connection.execute(
                    'INSERT INTO "review" VALUES (?, ?, 0, ?, ?)',
                    (
                        run_id,
                        node_id,
                        int(node_id != denied_specialist),
                        json.dumps(specialist_findings),
                    ),
                )
            connection.execute(
                'CREATE TABLE "verdict" ('
                "run_id TEXT, node_id TEXT, iteration INTEGER, allowed INTEGER, findings TEXT)"
            )
            connection.execute(
                'INSERT INTO "verdict" VALUES (?, \'verdict\', 0, ?, ?)',
                (run_id, int(allowed), json.dumps(findings or [])),
            )
            connection.execute(
                'CREATE TABLE "enforce" ('
                "run_id TEXT, node_id TEXT, iteration INTEGER, ok INTEGER)"
            )
            if enforced:
                connection.execute(
                    'INSERT INTO "enforce" VALUES (?, \'enforce\', 0, 1)',
                    (run_id,),
                )

    def test_partial_stage_excludes_unstaged_secret(self) -> None:
        self.fixture.write("app.py", "value = 1\n")
        self.fixture.add("app.py")
        self.fixture.commit()

        self.fixture.write("app.py", "value = 2\n")
        self.fixture.add("app.py")
        synthetic = "ghp_" + ("A" * 36)
        self.fixture.write("app.py", f"value = 2\nTOKEN = {synthetic!r}\n")

        snapshot = collect_staged_snapshot(self.fixture.root)
        staged_text = "\n".join(line.text for file in snapshot.files for line in file.added_lines)
        self.assertIn("value = 2", staged_text)
        self.assertNotIn(synthetic, staged_text)
        self.assertEqual(local_findings(snapshot), [])

    def test_staged_github_token_is_blocked_and_redacted_in_output(self) -> None:
        synthetic = "ghp_" + ("B" * 36)
        self.fixture.write("config.txt", f"token={synthetic}\n")
        self.fixture.add("config.txt")

        findings = local_findings(collect_staged_snapshot(self.fixture.root))
        self.assertIn("github-token", {finding.rule for finding in findings})
        rendered = "\n".join(format_finding(finding) for finding in findings)
        self.assertNotIn(synthetic, rendered)
        self.assertIn("fingerprint=", rendered)

    def test_gitattributes_cannot_suppress_staged_text_review(self) -> None:
        synthetic = "ghp_" + ("D" * 36)
        self.fixture.write(".gitattributes", "hidden.txt -diff\n")
        self.fixture.write("hidden.txt", f"token={synthetic}\n")
        self.fixture.add(".gitattributes", "hidden.txt")

        snapshot = collect_staged_snapshot(self.fixture.root)
        staged_text = "\n".join(line.text for file in snapshot.files for line in file.added_lines)
        self.assertIn(synthetic, staged_text)
        self.assertIn("github-token", {finding.rule for finding in local_findings(snapshot)})

    def test_credential_in_staged_path_is_blocked_and_never_rendered(self) -> None:
        synthetic = "ghp_" + ("E" * 36)
        path = f"fixtures/{synthetic}.txt"
        self.fixture.write(path, "safe content\n")
        self.fixture.add(path)

        snapshot = collect_staged_snapshot(self.fixture.root)
        findings = local_findings(snapshot)
        self.assertIn("github-token-in-path", {finding.rule for finding in findings})
        self.assertNotIn(synthetic, "\n".join(format_finding(finding) for finding in findings))
        self.assertNotIn(synthetic, json.dumps(agent_payload(snapshot)))

    def test_credential_in_rename_source_path_is_blocked_and_never_rendered(self) -> None:
        synthetic = "ghp_" + ("F" * 36)
        old_path = f"fixtures/{synthetic}.txt"
        self.fixture.write(old_path, "safe content\n")
        self.fixture.add(old_path)
        self.fixture.commit()
        self.fixture.git("mv", old_path, "fixtures/safe.txt")

        snapshot = collect_staged_snapshot(self.fixture.root)
        self.assertEqual(snapshot.files[0].old_path, old_path)
        findings = local_findings(snapshot)
        self.assertIn("github-token-in-path", {finding.rule for finding in findings})
        self.assertNotIn(synthetic, "\n".join(format_finding(finding) for finding in findings))
        self.assertNotIn(synthetic, json.dumps(agent_payload(snapshot)))

    def test_removed_secret_is_not_evaluated(self) -> None:
        synthetic = "ghp_" + ("C" * 36)
        self.fixture.write("config.txt", f"token={synthetic}\nkeep=true\n")
        self.fixture.add("config.txt")
        self.fixture.commit()
        self.fixture.write("config.txt", "keep=true\n")
        self.fixture.add("config.txt")

        snapshot = collect_staged_snapshot(self.fixture.root)
        self.assertEqual(local_findings(snapshot), [])
        self.assertFalse(has_agent_reviewable_changes(snapshot))

    def test_backup_directory_and_archive_are_blocked(self) -> None:
        self.fixture.write("deploy/backups/database.sql", "select 1;\n")
        self.fixture.write("release.tar.gz", "not actually compressed\n")
        self.fixture.add("deploy/backups/database.sql", "release.tar.gz")

        rules = {finding.rule for finding in local_findings(collect_staged_snapshot(self.fixture.root))}
        self.assertIn("backup-directory", rules)
        self.assertIn("blocked-artifact-suffix", rules)

    def test_database_dump_signature_is_blocked_even_in_sql_source_suffix(self) -> None:
        self.fixture.write("export.sql", "-- PostgreSQL database dump\nselect 1;\n")
        self.fixture.add("export.sql")
        rules = {finding.rule for finding in local_findings(collect_staged_snapshot(self.fixture.root))}
        self.assertIn("database-dump-content", rules)

    def test_literal_sensitive_assignment_is_blocked(self) -> None:
        literal = "correct-horse-battery-staple"
        self.fixture.write("config.yaml", f"client_secret: {literal}\n")
        self.fixture.add("config.yaml")
        findings = local_findings(collect_staged_snapshot(self.fixture.root))
        self.assertIn("literal-sensitive-assignment", {finding.rule for finding in findings})
        self.assertNotIn(literal, "\n".join(format_finding(finding) for finding in findings))

    def test_backup_implementation_source_and_placeholders_are_allowed_locally(self) -> None:
        self.fixture.write(
            "deploy/backup_manager.py",
            'token = os.getenv("DEPLOY_TOKEN")\npassword = "changeme"\n',
        )
        self.fixture.add("deploy/backup_manager.py")
        self.assertEqual(local_findings(collect_staged_snapshot(self.fixture.root)), [])

    def test_private_key_header_is_blocked(self) -> None:
        header = "-----BEGIN " + "PRIVATE KEY-----"
        self.fixture.write("fixture.txt", f"{header}\nnot-a-real-key\n")
        self.fixture.add("fixture.txt")
        rules = {finding.rule for finding in local_findings(collect_staged_snapshot(self.fixture.root))}
        self.assertIn("private-key", rules)

    def test_paths_with_spaces_are_read_from_index(self) -> None:
        self.fixture.write("dir with spaces/safe file.py", "answer = 42\n")
        self.fixture.add("dir with spaces/safe file.py")
        snapshot = collect_staged_snapshot(self.fixture.root)
        self.assertEqual(snapshot.files[0].path, "dir with spaces/safe file.py")
        self.assertEqual(snapshot.files[0].added_lines[0].text, "answer = 42")

    def test_binary_staged_change_fails_closed(self) -> None:
        target = self.fixture.root / "asset.bin"
        target.write_bytes(b"safe-prefix\0binary")
        self.fixture.add("asset.bin")
        rules = {finding.rule for finding in local_findings(collect_staged_snapshot(self.fixture.root))}
        self.assertIn("binary-change", rules)

    def test_agent_payload_redacts_high_entropy_literals(self) -> None:
        synthetic = "aB3dE5fG7hJ9kL2mN4pQ6rS8tU0vW1xY"
        self.fixture.write("session.py", f"session_id = {synthetic!r}\n")
        self.fixture.add("session.py")
        snapshot = collect_staged_snapshot(self.fixture.root)
        self.assertEqual(local_findings(snapshot), [])
        encoded = json.dumps(agent_payload(snapshot))
        self.assertNotIn(synthetic, encoded)
        self.assertIn("redacted-token-like", encoded)

    def test_smithers_receives_only_private_snapshot_path_in_argv(self) -> None:
        self.fixture.write("safe.py", "enabled = True\n")
        self.fixture.add("safe.py")
        snapshot = collect_staged_snapshot(self.fixture.root)
        calls: list[tuple[list[str], dict[str, object]]] = []

        def fake_runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append((command, kwargs))
            input_value = command[command.index("--input") + 1]
            parsed = json.loads(input_value)
            self.assertEqual(set(parsed), {"snapshotPath", "snapshotSha256", "indexTree"})
            snapshot_path = Path(parsed["snapshotPath"])
            self.assertTrue(snapshot_path.is_file())
            self.assertEqual(snapshot_path.stat().st_mode & 0o077, 0)
            self.assertNotIn("enabled = True", input_value)
            environment = kwargs["env"]
            self.write_smithers_result(
                Path(environment["SMITHERS_DB_PATH"]),
                command[command.index("--run-id") + 1],
                allowed=True,
            )
            return subprocess.CompletedProcess(command, 0)

        run_smithers_review(
            Path(__file__).resolve().parents[2],
            snapshot,
            runner=fake_runner,
        )
        self.assertEqual(len(calls), 1)
        command, kwargs = calls[0]
        self.assertIn("--no-log", command)
        self.assertIn("ATTESTMESH_PRECOMMIT_AGENT_CWD", kwargs["env"])
        self.assertIn("SMITHERS_DB_PATH", kwargs["env"])

    def test_smithers_environment_drops_ambient_credentials(self) -> None:
        secret_environment = {
            "PATH": "/safe/bin",
            "HOME": "/safe/home",
            "AWS_SECRET_ACCESS_KEY": "<not-inherited>",
            "GITHUB_TOKEN": "<not-inherited>",
            "OPENAI_API_KEY": "<not-inherited>",
        }
        with mock.patch.dict(guard_module.os.environ, secret_environment, clear=True):
            environment = smithers_environment(
                Path("/private/input"),
                Path("/private/smithers.db"),
                Path("/private/agent"),
            )

        self.assertEqual(environment["PATH"], "/safe/bin")
        self.assertEqual(environment["HOME"], "/safe/home")
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", environment)
        self.assertNotIn("GITHUB_TOKEN", environment)
        self.assertNotIn("OPENAI_API_KEY", environment)

    def test_smithers_failure_fails_closed(self) -> None:
        self.fixture.write("safe.py", "enabled = True\n")
        self.fixture.add("safe.py")
        snapshot = collect_staged_snapshot(self.fixture.root)

        def fake_runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, 1)

        with self.assertRaisesRegex(GuardError, "failed or rejected"):
            run_smithers_review(
                Path(__file__).resolve().parents[2],
                snapshot,
                runner=fake_runner,
            )

    def test_zero_exit_with_denied_smithers_verdict_fails_closed(self) -> None:
        self.fixture.write("safe.py", "enabled = True\n")
        self.fixture.add("safe.py")
        snapshot = collect_staged_snapshot(self.fixture.root)

        def fake_runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            environment = kwargs["env"]
            self.write_smithers_result(
                Path(environment["SMITHERS_DB_PATH"]),
                command[command.index("--run-id") + 1],
                allowed=False,
                findings=[
                    {
                        "category": "credential",
                        "severity": "high",
                        "path": "safe.py",
                        "line": 1,
                        "reason": "synthetic denial",
                    }
                ],
                enforced=False,
            )
            return subprocess.CompletedProcess(command, 0)

        with self.assertRaisesRegex(GuardError, "agentic review rejected"):
            run_smithers_review(
                Path(__file__).resolve().parents[2],
                snapshot,
                runner=fake_runner,
            )

    def test_zero_exit_without_smithers_verdict_fails_closed(self) -> None:
        self.fixture.write("safe.py", "enabled = True\n")
        self.fixture.add("safe.py")
        snapshot = collect_staged_snapshot(self.fixture.root)

        def fake_runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, 0)

        with self.assertRaisesRegex(GuardError, "result database"):
            run_smithers_review(
                Path(__file__).resolve().parents[2],
                snapshot,
                runner=fake_runner,
            )

    def test_clean_arbiter_cannot_override_specialist_denial(self) -> None:
        self.fixture.write("safe.py", "enabled = True\n")
        self.fixture.add("safe.py")
        snapshot = collect_staged_snapshot(self.fixture.root)

        def fake_runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            environment = kwargs["env"]
            self.write_smithers_result(
                Path(environment["SMITHERS_DB_PATH"]),
                command[command.index("--run-id") + 1],
                allowed=True,
                denied_specialist="adversarial-review",
            )
            return subprocess.CompletedProcess(command, 0)

        with self.assertRaisesRegex(GuardError, "specialist rejected"):
            run_smithers_review(
                Path(__file__).resolve().parents[2],
                snapshot,
                runner=fake_runner,
            )

    def test_index_mutation_during_agent_review_fails_closed(self) -> None:
        self.fixture.write("safe.py", "enabled = True\n")
        self.fixture.add("safe.py")

        def mutate_index(repo: Path, _snapshot: object) -> None:
            self.fixture.write("second.py", "new = True\n")
            self.fixture.add("second.py")

        with mock.patch.object(guard_module, "run_smithers_review", side_effect=mutate_index):
            with self.assertRaisesRegex(GuardError, "index changed"):
                guard_module.guard(self.fixture.root)


if __name__ == "__main__":
    unittest.main()
