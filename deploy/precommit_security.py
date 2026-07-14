#!/usr/bin/env python3
"""Fail-closed staged-change guard for backups and credentials.

The guard reads Git's index, never the worktree. High-confidence findings stop
locally before any model is called. A redacted staged snapshot is then reviewed
by the Smithers workflow in ``workflows/precommit-security.tsx``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Sequence


MAX_BLOB_BYTES = 20 * 1024 * 1024
MAX_ADDED_LINE_BYTES = 8 * 1024
MAX_AGENT_PAYLOAD_BYTES = 512 * 1024
EXPECTED_SMITHERS_VERSION = "0.22.0"
SAFE_SMITHERS_ENV_KEYS = (
    "CODEX_HOME",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "NODE_EXTRA_CA_CERTS",
    "PATH",
    "SHELL",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TERM",
    "TMPDIR",
    "TZ",
    "USER",
)


class GuardError(RuntimeError):
    """A fail-closed guard error safe to print to the terminal."""


@dataclass(frozen=True)
class Finding:
    category: str
    rule: str
    path: str
    message: str
    line: int | None = None
    fingerprint: str | None = None


@dataclass(frozen=True)
class AddedLine:
    line: int
    text: str


@dataclass(frozen=True)
class StagedFile:
    status: str
    path: str
    old_path: str | None
    mode: str
    blob_oid: str
    size: int
    kind: str
    added_lines: tuple[AddedLine, ...]


@dataclass(frozen=True)
class StagedSnapshot:
    index_tree: str
    files: tuple[StagedFile, ...]


@dataclass(frozen=True)
class SecretPattern:
    rule: str
    regex: re.Pattern[str]
    message: str


SECRET_PATTERNS = (
    SecretPattern(
        "github-token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
        "GitHub access token shape is staged",
    ),
    SecretPattern(
        "openai-api-key",
        re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}\b"),
        "OpenAI API key shape is staged",
    ),
    SecretPattern(
        "aws-access-key",
        re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
        "AWS access key ID shape is staged",
    ),
    SecretPattern(
        "google-api-key",
        re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
        "Google API key shape is staged",
    ),
    SecretPattern(
        "slack-token",
        re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{16,}\b"),
        "Slack token shape is staged",
    ),
    SecretPattern(
        "stripe-live-key",
        re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{16,}\b"),
        "Stripe live key shape is staged",
    ),
    SecretPattern(
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
        "JWT shape is staged",
    ),
    SecretPattern(
        "private-key",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
        "private-key material is staged",
    ),
    SecretPattern(
        "authorization-bearer",
        re.compile(r"(?i)\bAuthorization\s*[:=]\s*[\"']?Bearer\s+[A-Za-z0-9._~+/=-]{16,}"),
        "literal bearer credential is staged",
    ),
    SecretPattern(
        "credential-url",
        re.compile(r"\b[a-z][a-z0-9+.-]{1,20}://[^\s/:@]+:[^\s/@]{6,}@", re.IGNORECASE),
        "URL containing literal credentials is staged",
    ),
)

SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)[\"']?(?P<name>"
    r"password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|"
    r"client[_-]?secret|auth[_-]?token|bearer[_-]?token"
    r")[\"']?\s*[:=]\s*(?P<value>.+)$"
)

PLACEHOLDER_VALUES = {
    "",
    "changeme",
    "example",
    "fake",
    "placeholder",
    "redacted",
    "secret",
    "test",
    "test-token",
    "todo",
    "your-key-here",
    "your-token-here",
}

ENV_REFERENCE_MARKERS = (
    "${",
    "process.env",
    "os.environ",
    "os.getenv",
    "getenv(",
    "secretkeyref",
    "secrets.",
    "from_env",
    "vault",
)

BACKUP_DIR_COMPONENTS = {"backup", "backups", "dump", "dumps"}
BLOCKED_SUFFIXES = (
    ".7z",
    ".bak",
    ".backup",
    ".bz2",
    ".db",
    ".dump",
    ".gz",
    ".img",
    ".jks",
    ".key",
    ".mdb",
    ".orig",
    ".p12",
    ".pem",
    ".pfx",
    ".pgdump",
    ".qcow",
    ".qcow2",
    ".rar",
    ".rdb",
    ".sqlite",
    ".sqlite3",
    ".tar",
    ".tgz",
    ".vmdk",
    ".xz",
    ".zip",
    ".zst",
)

BACKUP_NAME = re.compile(
    r"(?i)(?:^|[-_.])(?:backup|dump|export|precutover|pre-cutover|snapshot)"
    r"(?:[-_.]|$)"
)

SOURCE_OR_DOC_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".go",
    ".h",
    ".hpp",
    ".java",
    ".js",
    ".jsx",
    ".md",
    ".mjs",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".sql",
    ".ts",
    ".tsx",
}

HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<line>\d+)(?:,\d+)? @@")
TOKENISH = re.compile(r"[A-Za-z0-9+/=_-]{20,}")
DUMP_SIGNATURES = (
    re.compile(r"^-- PostgreSQL database dump", re.IGNORECASE),
    re.compile(r"^-- MySQL dump", re.IGNORECASE),
    re.compile(r"^SQLite format 3"),
)


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    env = os.environ.copy()
    env.setdefault("LC_ALL", "C")
    env["GIT_OPTIONAL_LOCKS"] = "0"
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    if check and result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise GuardError(f"git {' '.join(args[:2])} failed: {detail or 'unknown error'}")
    return result


def index_tree(repo: Path) -> str:
    return git(repo, "write-tree").stdout.decode("ascii").strip()


def decode_path(raw: bytes) -> str:
    return os.fsdecode(raw)


def changed_index_paths(repo: Path) -> list[tuple[str, str | None, str]]:
    raw = git(
        repo,
        "diff",
        "--cached",
        "--name-status",
        "-z",
        "--find-renames",
        "--find-copies",
        "--diff-filter=ACMRT",
    ).stdout
    fields = raw.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()

    changes: list[tuple[str, str | None, str]] = []
    cursor = 0
    while cursor < len(fields):
        status = fields[cursor].decode("ascii", "replace")
        cursor += 1
        if not status:
            raise GuardError("git returned an empty staged status")
        if status[0] in {"R", "C"}:
            if cursor + 1 >= len(fields):
                raise GuardError("git returned a truncated staged rename/copy record")
            old_path = decode_path(fields[cursor])
            path = decode_path(fields[cursor + 1])
            cursor += 2
        else:
            if cursor >= len(fields):
                raise GuardError("git returned a truncated staged path record")
            old_path = None
            path = decode_path(fields[cursor])
            cursor += 1
        changes.append((status, old_path, path))
    return changes


def index_entry(repo: Path, path: str) -> tuple[str, str]:
    raw = git(repo, "ls-files", "--stage", "-z", "--", path).stdout
    records = [record for record in raw.split(b"\0") if record]
    if len(records) != 1:
        raise GuardError(f"expected one stage-0 index entry for {path!r}")
    prefix, separator, _ = records[0].partition(b"\t")
    if not separator:
        raise GuardError(f"could not parse index entry for {path!r}")
    parts = prefix.split()
    if len(parts) != 3 or parts[2] != b"0":
        raise GuardError(f"unmerged or malformed index entry for {path!r}")
    return parts[0].decode("ascii"), parts[1].decode("ascii")


def added_lines_for_path(repo: Path, path: str) -> tuple[AddedLine, ...]:
    patch = git(
        repo,
        "diff",
        "--cached",
        "--unified=0",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--text",
        "--diff-filter=ACMRT",
        "--",
        path,
    ).stdout.decode("utf-8", "replace")
    added: list[AddedLine] = []
    next_line: int | None = None
    for raw_line in patch.splitlines():
        header = HUNK_HEADER.match(raw_line)
        if header:
            next_line = int(header.group("line"))
            continue
        if next_line is None:
            continue
        if raw_line.startswith("+"):
            added.append(AddedLine(next_line, raw_line[1:]))
            next_line += 1
        elif raw_line.startswith("-"):
            continue
        elif raw_line.startswith(" "):
            next_line += 1
        elif raw_line.startswith("\\ No newline"):
            continue
        else:
            next_line = None
    return tuple(added)


def collect_staged_snapshot(repo: Path) -> StagedSnapshot:
    tree = index_tree(repo)
    staged_files: list[StagedFile] = []
    for status, old_path, path in changed_index_paths(repo):
        mode, oid = index_entry(repo, path)
        if mode == "160000":
            staged_files.append(
                StagedFile(status, path, old_path, mode, oid, 0, "gitlink", ())
            )
            continue
        size = int(git(repo, "cat-file", "-s", oid).stdout.decode("ascii").strip())
        if size > MAX_BLOB_BYTES:
            staged_files.append(
                StagedFile(status, path, old_path, mode, oid, size, "oversize", ())
            )
            continue
        blob = git(repo, "cat-file", "blob", oid).stdout
        kind = "binary" if b"\0" in blob else ("symlink" if mode == "120000" else "text")
        added = () if kind == "binary" else added_lines_for_path(repo, path)
        staged_files.append(StagedFile(status, path, old_path, mode, oid, size, kind, added))
    return StagedSnapshot(tree, tuple(staged_files))


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()[:12]


def path_findings(file: StagedFile) -> list[Finding]:
    findings: list[Finding] = []
    normalized = file.path.replace("\\", "/")
    lowered = normalized.lower()
    parts = [part.lower() for part in PurePosixPath(normalized).parts]
    basename = parts[-1] if parts else lowered

    for label, candidate in (("path", file.path), ("rename source path", file.old_path)):
        if candidate is None:
            continue
        for secret_pattern in SECRET_PATTERNS:
            match = secret_pattern.regex.search(candidate)
            if match:
                findings.append(
                    Finding(
                        "credential",
                        f"{secret_pattern.rule}-in-path",
                        file.path,
                        f"{label} contains a {secret_pattern.rule} credential shape",
                        fingerprint=fingerprint(match.group(0)),
                    )
                )
        assignment = SENSITIVE_ASSIGNMENT.search(candidate)
        if assignment:
            value = extract_assignment_value(assignment.group("value"))
            if len(value) >= 8 and not is_placeholder_or_reference(value):
                findings.append(
                    Finding(
                        "credential",
                        "literal-sensitive-assignment-in-path",
                        file.path,
                        f"{label} contains a literal value assigned to a sensitive field",
                        fingerprint=fingerprint(value),
                    )
                )

    if any(part in BACKUP_DIR_COMPONENTS for part in parts):
        findings.append(
            Finding(
                "backup",
                "backup-directory",
                file.path,
                "files under backup/dump directories must never be committed",
            )
        )
    if lowered.endswith(BLOCKED_SUFFIXES):
        findings.append(
            Finding(
                "backup",
                "blocked-artifact-suffix",
                file.path,
                "backup, archive, database, disk, or key artifact suffix is staged",
            )
        )
    if BACKUP_NAME.search(basename) and PurePosixPath(basename).suffix not in SOURCE_OR_DOC_SUFFIXES:
        findings.append(
            Finding(
                "backup",
                "backup-filename",
                file.path,
                "filename looks like an operational backup, dump, export, or snapshot",
            )
        )
    if basename == ".env" or (
        basename.startswith(".env.")
        and not basename.endswith((".example", ".sample", ".template"))
    ):
        findings.append(
            Finding(
                "credential",
                "environment-file",
                file.path,
                "non-template environment files must never be committed",
            )
        )
    if basename in {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}:
        findings.append(
            Finding(
                "credential",
                "private-key-filename",
                file.path,
                "private-key filename is staged",
            )
        )
    if file.kind == "binary":
        findings.append(
            Finding(
                "unreviewable",
                "binary-change",
                file.path,
                "binary changes cannot be safely inspected as staged-only text",
            )
        )
    elif file.kind == "oversize":
        findings.append(
            Finding(
                "unreviewable",
                "oversize-blob",
                file.path,
                f"staged blob exceeds the {MAX_BLOB_BYTES // (1024 * 1024)} MiB inspection limit",
            )
        )
    return findings


def extract_assignment_value(raw: str) -> str:
    value = raw.strip()
    if not value:
        return ""
    if value[0] in {"\"", "'", "`"}:
        quote = value[0]
        end = value.find(quote, 1)
        return value[1:end] if end >= 1 else value[1:]
    return re.split(r"[\s,;#]", value, maxsplit=1)[0].strip()


def is_placeholder_or_reference(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in PLACEHOLDER_VALUES:
        return True
    if lowered.startswith(("<", "$", "{")):
        return True
    return any(marker in lowered for marker in ENV_REFERENCE_MARKERS)


def content_findings(file: StagedFile) -> list[Finding]:
    findings: list[Finding] = []
    for added in file.added_lines:
        encoded_length = len(added.text.encode("utf-8", "surrogatepass"))
        if encoded_length > MAX_ADDED_LINE_BYTES:
            findings.append(
                Finding(
                    "unreviewable",
                    "oversize-line",
                    file.path,
                    f"staged line exceeds the {MAX_ADDED_LINE_BYTES // 1024} KiB inspection limit",
                    added.line,
                )
            )
            continue
        if any(pattern.search(added.text) for pattern in DUMP_SIGNATURES):
            findings.append(
                Finding(
                    "backup",
                    "database-dump-content",
                    file.path,
                    "database dump signature is staged",
                    added.line,
                )
            )
        for secret_pattern in SECRET_PATTERNS:
            match = secret_pattern.regex.search(added.text)
            if match:
                findings.append(
                    Finding(
                        "credential",
                        secret_pattern.rule,
                        file.path,
                        secret_pattern.message,
                        added.line,
                        fingerprint(match.group(0)),
                    )
                )
        assignment = SENSITIVE_ASSIGNMENT.search(added.text)
        if assignment:
            value = extract_assignment_value(assignment.group("value"))
            if len(value) >= 8 and not is_placeholder_or_reference(value):
                findings.append(
                    Finding(
                        "credential",
                        "literal-sensitive-assignment",
                        file.path,
                        f"literal value assigned to sensitive field {assignment.group('name')!r}",
                        added.line,
                        fingerprint(value),
                    )
                )
    return findings


def local_findings(snapshot: StagedSnapshot) -> list[Finding]:
    findings: list[Finding] = []
    seen: set[tuple[str, str, int | None]] = set()
    for file in snapshot.files:
        for finding in (*path_findings(file), *content_findings(file)):
            key = (finding.rule, finding.path, finding.line)
            if key not in seen:
                seen.add(key)
                findings.append(finding)
    return findings


def shannon_entropy(value: str) -> float:
    if not value:
        return 0.0
    counts = {character: value.count(character) for character in set(value)}
    return -sum((count / len(value)) * math.log2(count / len(value)) for count in counts.values())


def redact_tokenish(match: re.Match[str]) -> str:
    value = match.group(0)
    character_classes = sum(
        bool(regex.search(value))
        for regex in (re.compile(r"[a-z]"), re.compile(r"[A-Z]"), re.compile(r"[0-9]"), re.compile(r"[_+/=-]"))
    )
    if character_classes >= 2 and shannon_entropy(value) >= 3.5:
        return f"<redacted-token-like length={len(value)}>"
    return value


def redact_line(line: str) -> str:
    redacted = line
    for secret_pattern in SECRET_PATTERNS:
        redacted = secret_pattern.regex.sub(
            lambda match: f"<redacted-{secret_pattern.rule} length={len(match.group(0))}>",
            redacted,
        )
    assignment = SENSITIVE_ASSIGNMENT.search(redacted)
    if assignment:
        value = extract_assignment_value(assignment.group("value"))
        if value and not is_placeholder_or_reference(value):
            start = assignment.start("value")
            redacted = redacted[:start] + "<redacted-sensitive-value>"
    return TOKENISH.sub(redact_tokenish, redacted)


def agent_payload(snapshot: StagedSnapshot) -> dict[str, object]:
    files: list[dict[str, object]] = []
    for file in snapshot.files:
        files.append(
            {
                "status": file.status,
                "path": redact_line(file.path),
                "oldPath": redact_line(file.old_path) if file.old_path is not None else None,
                "mode": file.mode,
                "size": file.size,
                "kind": file.kind,
                "addedLines": [
                    {"line": added.line, "text": redact_line(added.text)}
                    for added in file.added_lines
                ],
            }
        )
    payload: dict[str, object] = {
        "schemaVersion": 1,
        "indexTree": snapshot.index_tree,
        "scope": "Git index versus HEAD; added staged lines only; no worktree content",
        "files": files,
    }
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_AGENT_PAYLOAD_BYTES:
        raise GuardError(
            f"redacted staged payload is {len(encoded)} bytes, above the "
            f"{MAX_AGENT_PAYLOAD_BYTES} byte review limit; split the commit"
        )
    return payload


def write_private_json(path: Path, payload: object) -> str:
    serialized = json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(serialized)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def smithers_entrypoint(repo: Path) -> Path:
    package_root = repo / "deploy" / "node_modules" / "smithers-orchestrator"
    package_json = package_root / "package.json"
    entrypoint = package_root / "src" / "bin" / "smithers.js"
    if not package_json.is_file() or not entrypoint.is_file():
        raise GuardError(
            "Smithers dependencies are not installed; run deploy/install-precommit-security.sh"
        )
    try:
        version = json.loads(package_json.read_text(encoding="utf-8"))["version"]
    except (OSError, KeyError, json.JSONDecodeError) as error:
        raise GuardError("could not verify the installed Smithers package") from error
    if version != EXPECTED_SMITHERS_VERSION:
        raise GuardError(
            f"expected smithers-orchestrator {EXPECTED_SMITHERS_VERSION}, found {version}; "
            "run deploy/install-precommit-security.sh"
        )
    return entrypoint


def smithers_environment(temp_dir: Path, db_path: Path, agent_dir: Path) -> dict[str, str]:
    """Build a minimal agent environment without ambient credentials."""

    environment = {
        key: os.environ[key]
        for key in SAFE_SMITHERS_ENV_KEYS
        if key in os.environ and os.environ[key]
    }
    environment.setdefault("PATH", os.defpath)
    environment.update(
        {
            "ATTESTMESH_PRECOMMIT_AGENT_CWD": str(agent_dir),
            "ATTESTMESH_PRECOMMIT_INPUT_DIR": str(temp_dir),
            "SMITHERS_DB_PATH": str(db_path),
        }
    )
    return environment


def parse_json_array(value: object, field: str) -> list[object]:
    if not isinstance(value, str):
        raise GuardError(f"Smithers stored an invalid {field} value")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise GuardError(f"Smithers stored malformed {field} JSON") from error
    if not isinstance(parsed, list):
        raise GuardError(f"Smithers stored a non-array {field} value")
    return parsed


def verify_smithers_result(db_path: Path, run_id: str) -> None:
    """Require an explicit clean panel verdict from the private Smithers DB.

    Smithers 0.22 can log a compute-task exception while still exiting zero, so
    the CLI status is not the security decision. The durable structured rows are
    verified independently before the hook can pass.
    """

    expected_reviewers = {
        "adversarial-review",
        "backup-review",
        "credential-review",
    }
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            review_rows = connection.execute(
                'SELECT node_id, clear, findings FROM "review" '
                "WHERE run_id = ? AND iteration = 0",
                (run_id,),
            ).fetchall()
            reviewer_ids = {row[0] for row in review_rows}
            if reviewer_ids != expected_reviewers or len(review_rows) != len(expected_reviewers):
                raise GuardError("Smithers did not persist every required specialist review")
            for _, clear, findings in review_rows:
                parsed_findings = parse_json_array(findings, "specialist findings")
                if clear != 1 or parsed_findings:
                    raise GuardError("a Smithers specialist rejected the staged changes")

            verdict_rows = connection.execute(
                'SELECT allowed, findings FROM "verdict" '
                "WHERE run_id = ? AND node_id = 'verdict' AND iteration = 0",
                (run_id,),
            ).fetchall()
            if len(verdict_rows) != 1:
                raise GuardError("Smithers did not persist exactly one final verdict")
            allowed, findings = verdict_rows[0]
            if allowed != 1 or parse_json_array(findings, "verdict findings"):
                raise GuardError("Smithers agentic review rejected the staged changes")

            enforcement_rows = connection.execute(
                'SELECT ok FROM "enforce" '
                "WHERE run_id = ? AND node_id = 'enforce' AND iteration = 0",
                (run_id,),
            ).fetchall()
            if enforcement_rows != [(1,)]:
                raise GuardError("Smithers did not persist a successful enforcement decision")
        finally:
            connection.close()
    except GuardError:
        raise
    except sqlite3.Error as error:
        raise GuardError("could not verify the private Smithers result database") from error


def run_smithers_review(
    repo: Path,
    snapshot: StagedSnapshot,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    bun = shutil.which("bun")
    codex = shutil.which("codex")
    if not bun:
        raise GuardError("Bun is required; install Bun and run deploy/install-precommit-security.sh")
    if not codex:
        raise GuardError("Codex CLI is required; install it and authenticate with codex login")
    entrypoint = smithers_entrypoint(repo)
    workflow = repo / "deploy" / "workflows" / "precommit-security.tsx"
    if not workflow.is_file():
        raise GuardError("the Smithers pre-commit security workflow is missing")

    with tempfile.TemporaryDirectory(prefix="attestmesh-precommit-") as temp_name:
        temp_dir = Path(temp_name)
        os.chmod(temp_dir, stat.S_IRWXU)
        agent_dir = temp_dir / "agent-cwd"
        agent_dir.mkdir(mode=0o700)
        snapshot_path = temp_dir / "staged-snapshot.json"
        snapshot_hash = write_private_json(snapshot_path, agent_payload(snapshot))
        db_path = temp_dir / "smithers.db"
        run_id = f"precommit-{snapshot.index_tree[:12]}-{secrets.token_hex(4)}"
        input_json = json.dumps(
            {
                "snapshotPath": str(snapshot_path),
                "snapshotSha256": snapshot_hash,
                "indexTree": snapshot.index_tree,
            },
            separators=(",", ":"),
        )
        env = smithers_environment(temp_dir, db_path, agent_dir)
        result = runner(
            [
                bun,
                str(entrypoint),
                "up",
                str(workflow),
                "--run-id",
                run_id,
                "--input",
                input_json,
                "--root",
                str(agent_dir),
                "--max-concurrency",
                "4",
                "--no-log",
            ],
            cwd=repo / "deploy",
            env=env,
            check=False,
            text=True,
        )
        if result.returncode != 0:
            raise GuardError(
                "Smithers gpt-5.6-sol/ultra review failed or rejected the staged changes"
            )
        verify_smithers_result(db_path, run_id)


def format_finding(finding: Finding) -> str:
    location = redact_line(finding.path)
    if finding.line is not None:
        location += f":{finding.line}"
    suffix = f"; fingerprint={finding.fingerprint}" if finding.fingerprint else ""
    return f"  - {location} [{finding.rule}] {finding.message}{suffix}"


def has_agent_reviewable_changes(snapshot: StagedSnapshot) -> bool:
    return any(file.added_lines for file in snapshot.files)


def guard(repo: Path) -> int:
    snapshot = collect_staged_snapshot(repo)
    if not snapshot.files:
        print("pre-commit security guard: no staged additions or modifications")
        return 0

    findings = local_findings(snapshot)
    if findings:
        print("pre-commit security guard blocked the staged changes:", file=sys.stderr)
        for finding in findings:
            print(format_finding(finding), file=sys.stderr)
        print("No suspected secret values were printed or sent to a model.", file=sys.stderr)
        return 1

    if not has_agent_reviewable_changes(snapshot):
        print("pre-commit security guard: staged path/deletion checks passed")
        return 0

    before_agent = snapshot.index_tree
    run_smithers_review(repo, snapshot)
    after_agent = index_tree(repo)
    if after_agent != before_agent:
        raise GuardError("the Git index changed during review; rerun the commit")
    print(
        "pre-commit security guard: staged changes passed local checks and "
        "Smithers gpt-5.6-sol/ultra review"
    )
    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="Git repository root")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    repo = args.repo.resolve()
    try:
        top_level = Path(
            git(repo, "rev-parse", "--show-toplevel").stdout.decode("utf-8", "replace").strip()
        ).resolve()
        if top_level != repo:
            raise GuardError(f"--repo must be the Git top level ({top_level})")
        return guard(repo)
    except GuardError as error:
        print(f"pre-commit security guard failed closed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
