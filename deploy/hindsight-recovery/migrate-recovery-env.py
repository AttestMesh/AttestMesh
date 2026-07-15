#!/usr/bin/env python3
"""Atomically migrate restart-persistent Hindsight recovery configuration."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
import re
import sys
import tempfile
from pathlib import Path


class MigrationError(RuntimeError):
    pass


_KEY = re.compile(r"[A-Z][A-Z0-9_]*")
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
_COMPOSE_HASH = re.compile(r"(?:0x)?[0-9a-f]{64}")
_BUILD_SHA = re.compile(r"(?:^|[@:])sha256:([0-9a-f]{64})$")
_DROP_KEYS = frozenset({"EXPECTED_HINDSIGHT_FAILED"})

# These describe the host/tunnel layout and may legitimately differ from the
# reviewed example. Every other known non-secret key is migrated to the
# reviewed template value before the explicit immutable Agent identities below
# are applied.
_PRESERVE_EXISTING = frozenset(
    {
        "RECOVERY_ROOT",
        "RECOVERY_STATE_DIR",
        "RECOVERY_SSH_TARGET",
        "PG_REMOTE_HOSTS",
        "PG_LOCAL_PORTS",
        "HINDSIGHT_LOCAL_PORT",
        "BUDGET_LOCAL_PORT",
        "FUGU_LOCAL_PORT",
        "PATRONI_LOCAL_PORTS",
    }
)


def parse_env(text: str, *, source: str) -> tuple[dict[str, str], list[str]]:
    values: dict[str, str] = {}
    lines = text.splitlines()
    for number, raw in enumerate(lines, 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in raw:
            raise MigrationError(f"{source}:{number}: malformed environment line")
        key, value = raw.split("=", 1)
        key = key.strip()
        if not _KEY.fullmatch(key):
            raise MigrationError(f"{source}:{number}: invalid key {key!r}")
        if key in values:
            raise MigrationError(f"{source}:{number}: duplicate key {key}")
        if "\x00" in value or "\r" in value:
            raise MigrationError(f"{source}:{number}: invalid value for {key}")
        values[key] = value.strip()
    return values, lines


def render_migration(
    current_text: str,
    template_text: str,
    *,
    expected_image_digest: str,
    expected_compose_hash: str,
    expected_build_id: str,
    outbox_source: Path,
    recovery_module: Path | None = None,
    recovery_root: Path | None = None,
) -> str:
    current, current_lines = parse_env(current_text, source="current recovery.env")
    template, _ = parse_env(template_text, source="recovery.env.example")
    if not current:
        raise MigrationError("current recovery.env is empty")

    merged = dict(current)
    for key in _DROP_KEYS:
        merged.pop(key, None)
    for key, value in template.items():
        if key not in _PRESERVE_EXISTING or key not in merged:
            merged[key] = value
    merged.update(
        {
            "EXPECTED_AGENT_IMAGE_DIGEST": expected_image_digest,
            "EXPECTED_AGENT_COMPOSE_HASH": expected_compose_hash,
            "EXPECTED_AGENT_OUTBOX_BUILD_ID": expected_build_id,
            "EXPECTED_RECOVERY_OUTBOX_BUILD_ID": expected_build_id,
        }
    )
    if recovery_root is not None:
        resolved_root = recovery_root.expanduser().resolve()
        if not resolved_root.is_absolute():
            raise MigrationError("recovery root must be an absolute path")
        merged["RECOVERY_ROOT"] = str(resolved_root)
    validate_migrated(merged, outbox_source=outbox_source)

    rendered: list[str] = []
    emitted: set[str] = set()
    for raw in current_lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            rendered.append(raw)
            continue
        key = raw.split("=", 1)[0].strip()
        if key in _DROP_KEYS:
            continue
        rendered.append(f"{key}={merged[key]}")
        emitted.add(key)

    missing = [key for key in template if key not in emitted]
    if missing:
        if rendered and rendered[-1].strip():
            rendered.append("")
        rendered.append("# Added by the validated full-batch recovery migration.")
        for key in missing:
            rendered.append(f"{key}={merged[key]}")
            emitted.add(key)
    for key in (
        "EXPECTED_AGENT_IMAGE_DIGEST",
        "EXPECTED_AGENT_COMPOSE_HASH",
        "EXPECTED_AGENT_OUTBOX_BUILD_ID",
        "EXPECTED_RECOVERY_OUTBOX_BUILD_ID",
    ):
        if key not in emitted:
            rendered.append(f"{key}={merged[key]}")
    result = "\n".join(rendered).rstrip() + "\n"
    validate_config_from_env(
        merged,
        recovery_module=recovery_module or Path(__file__).with_name("recovery.py"),
    )
    return result


def validate_migrated(values: dict[str, str], *, outbox_source: Path) -> None:
    exact = {
        "APPROVED_TOTAL": "5142",
        "STAGE_CAP": "5142",
        "MAX_AGENT_ACTIVE": "2",
        "MAX_PROVIDER_IN_FLIGHT": "6",
        "HARD_BUDGET_USD": "30",
        "EXPECTED_HINDSIGHT_MODEL": "openai/gpt-oss-120b",
        "EXPECTED_HINDSIGHT_PHASE": "backfill",
        "EXPECTED_HINDSIGHT_LLM_CONCURRENCY": "3",
    }
    for key, expected in exact.items():
        if values.get(key) != expected:
            raise MigrationError(f"{key} must be exactly {expected}")
    if not values.get("CONTROLLER_SECONDS"):
        raise MigrationError("controller configuration is incomplete")
    root = _semantic_value(values.get("RECOVERY_ROOT", ""))
    if not root or not Path(root).expanduser().is_absolute():
        raise MigrationError("RECOVERY_ROOT must be an absolute path")

    image = values.get("EXPECTED_AGENT_IMAGE_DIGEST", "")
    compose = values.get("EXPECTED_AGENT_COMPOSE_HASH", "")
    build = values.get("EXPECTED_AGENT_OUTBOX_BUILD_ID", "")
    recovery_build = values.get("EXPECTED_RECOVERY_OUTBOX_BUILD_ID", "")
    if not _SHA256.fullmatch(image):
        raise MigrationError("EXPECTED_AGENT_IMAGE_DIGEST is not a pinned SHA-256")
    if not _COMPOSE_HASH.fullmatch(compose):
        raise MigrationError("EXPECTED_AGENT_COMPOSE_HASH is not an exact digest")
    match = _BUILD_SHA.search(build)
    if not match:
        raise MigrationError(
            "EXPECTED_AGENT_OUTBOX_BUILD_ID must end in sha256:<64 lowercase hex>"
        )
    recovery_match = _BUILD_SHA.search(recovery_build)
    if not recovery_match:
        raise MigrationError(
            "EXPECTED_RECOVERY_OUTBOX_BUILD_ID must end in sha256:<64 lowercase hex>"
        )
    if not outbox_source.is_file():
        raise MigrationError(f"reviewed outbox source is absent: {outbox_source}")
    actual = hashlib.sha256(outbox_source.read_bytes()).hexdigest()
    if recovery_match.group(1) != actual:
        raise MigrationError(
            "EXPECTED_RECOVERY_OUTBOX_BUILD_ID does not match reviewed outbox.py"
        )


def _semantic_value(value: str) -> str:
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] in {"'", '"'} and stripped[-1] == stripped[0]:
        return stripped[1:-1]
    return stripped


def validate_config_from_env(values: dict[str, str], *, recovery_module: Path) -> None:
    """Instantiate the production Config against only the rendered values."""
    spec = importlib.util.spec_from_file_location(
        "hindsight_recovery_config_validation", recovery_module
    )
    if not spec or not spec.loader:
        raise MigrationError(f"cannot load production Config: {recovery_module}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        previous = dict(os.environ)
        os.environ.clear()
        os.environ.update(
            {key: _semantic_value(value) for key, value in values.items()}
        )
        try:
            config = module.Config.from_env()
        finally:
            os.environ.clear()
            os.environ.update(previous)
    except Exception as exc:
        raise MigrationError(
            f"production Config.from_env rejected migration: {exc}"
        ) from exc
    finally:
        sys.modules.pop(spec.name, None)

    # Config.__post_init__ owns these invariants; retaining explicit checks here
    # makes a future weakening of that class fail this migration closed.
    if str(config.conservative_remaining_cost) != "0.00543":
        raise MigrationError("production Config remaining-row cost drift")
    if str(config.expected_provider_effective_limit) != "49.5":
        raise MigrationError("production Config provider limit drift")


def atomic_replace(path: Path, content: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="validate and atomically migrate recovery.env to the full batch"
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--outbox-source", type=Path, required=True)
    parser.add_argument(
        "--recovery-module",
        type=Path,
        default=Path(__file__).with_name("recovery.py"),
    )
    parser.add_argument("--recovery-root", type=Path, required=True)
    parser.add_argument("--expected-agent-image-digest")
    parser.add_argument("--expected-agent-compose-hash")
    parser.add_argument("--expected-agent-outbox-build-id")
    parser.add_argument(
        "--identities-from-config",
        action="store_true",
        help="validate the immutable identities already present in recovery.env",
    )
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args(argv)

    supplied_identities = (
        args.expected_agent_image_digest,
        args.expected_agent_compose_hash,
        args.expected_agent_outbox_build_id,
    )
    if args.identities_from_config:
        if any(value is not None for value in supplied_identities):
            parser.error(
                "--identities-from-config cannot be combined with identity arguments"
            )
        current, _ = parse_env(args.config.read_text(), source=str(args.config))
        expected_image_digest = _semantic_value(
            current.get("EXPECTED_AGENT_IMAGE_DIGEST", "")
        )
        expected_compose_hash = _semantic_value(
            current.get("EXPECTED_AGENT_COMPOSE_HASH", "")
        )
        expected_build_id = _semantic_value(
            current.get("EXPECTED_AGENT_OUTBOX_BUILD_ID", "")
        )
    else:
        if any(value is None for value in supplied_identities):
            parser.error(
                "all three immutable Agent identity arguments are required"
            )
        expected_image_digest = str(args.expected_agent_image_digest)
        expected_compose_hash = str(args.expected_agent_compose_hash)
        expected_build_id = str(args.expected_agent_outbox_build_id)

    rendered = render_migration(
        args.config.read_text(),
        args.template.read_text(),
        expected_image_digest=expected_image_digest,
        expected_compose_hash=expected_compose_hash,
        expected_build_id=expected_build_id,
        outbox_source=args.outbox_source,
        recovery_module=args.recovery_module,
        recovery_root=args.recovery_root,
    )
    if not args.check_only:
        atomic_replace(args.config, rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
