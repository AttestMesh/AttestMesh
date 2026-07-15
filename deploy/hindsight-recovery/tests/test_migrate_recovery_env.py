from __future__ import annotations

import hashlib
import importlib.util
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[3]
MODULE = ROOT / "deploy/hindsight-recovery/migrate-recovery-env.py"
SPEC = importlib.util.spec_from_file_location("migrate_recovery_env", MODULE)
assert SPEC and SPEC.loader
migration = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = migration
SPEC.loader.exec_module(migration)


def _identities(source: Path) -> dict[str, str]:
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    return {
        "expected_image_digest": "sha256:" + "1" * 64,
        "expected_compose_hash": "2" * 64,
        "expected_build_id": f"outbox.py:sha256:{digest}",
    }


def test_migration_is_complete_sourceable_and_config_validated(tmp_path: Path) -> None:
    template = (ROOT / "deploy/hindsight-recovery/recovery.env.example").read_text()
    current = (
        template.replace("STAGE_CAP=5142", "STAGE_CAP=3823").replace(
            "RECOVERY_SSH_TARGET=attestmesh-mesh-node",
            "RECOVERY_SSH_TARGET=preserved-target",
        )
        + "EXPECTED_HINDSIGHT_FAILED=60\n"
    )
    source = tmp_path / "outbox.py"
    source.write_text("reviewed source\n")

    rendered = migration.render_migration(
        current,
        template,
        outbox_source=source,
        recovery_module=ROOT / "deploy/hindsight-recovery/recovery.py",
        recovery_root=ROOT,
        **_identities(source),
    )
    values, _ = migration.parse_env(rendered, source="rendered")
    assert values["STAGE_CAP"] == "5142"
    assert values["MAX_AGENT_ACTIVE"] == "2"
    assert values["MAX_PROVIDER_IN_FLIGHT"] == "6"
    assert values["RECOVERY_SSH_TARGET"] == "preserved-target"
    assert values["RECOVERY_ROOT"] == str(ROOT.resolve())
    assert values["AGENT_RUNTIME_VERIFIER_COMMAND"] == ""
    assert (
        values["EXPECTED_RECOVERY_OUTBOX_BUILD_ID"]
        == values["EXPECTED_AGENT_OUTBOX_BUILD_ID"]
    )
    assert "EXPECTED_HINDSIGHT_FAILED" not in values

    env_file = tmp_path / "recovery.env"
    env_file.write_text(rendered)
    retired_root = "/home/ubuntu/" + "teemesh"
    old_root = template.replace("RECOVERY_ROOT=", f"RECOVERY_ROOT={retired_root}")
    repointed = migration.render_migration(
        old_root,
        template,
        outbox_source=source,
        recovery_module=ROOT / "deploy/hindsight-recovery/recovery.py",
        recovery_root=ROOT,
        **_identities(source),
    )
    repointed_values, _ = migration.parse_env(repointed, source="repointed")
    assert repointed_values["RECOVERY_ROOT"] == str(ROOT.resolve())
    assert retired_root not in repointed


def test_invalid_timing_or_source_digest_fails_before_write(tmp_path: Path) -> None:
    template = (ROOT / "deploy/hindsight-recovery/recovery.env.example").read_text()
    source = tmp_path / "outbox.py"
    source.write_text("reviewed source\n")
    identities = _identities(source)

    with pytest.raises(migration.MigrationError, match="Config.from_env rejected"):
        migration.render_migration(
            template,
            template.replace("GUARD_FULL_SECONDS=60", "GUARD_FULL_SECONDS=nan"),
            outbox_source=source,
            recovery_module=ROOT / "deploy/hindsight-recovery/recovery.py",
            recovery_root=ROOT,
            **identities,
        )

    identities["expected_build_id"] = "outbox.py:sha256:" + "f" * 64
    with pytest.raises(migration.MigrationError, match="does not match reviewed"):
        migration.render_migration(
            template,
            template,
            outbox_source=source,
            recovery_module=ROOT / "deploy/hindsight-recovery/recovery.py",
            recovery_root=ROOT,
            **identities,
        )


def test_atomic_replace_uses_mode_0600(tmp_path: Path) -> None:
    target = tmp_path / "recovery.env"
    target.write_text("OLD=1\n")
    os.chmod(target, 0o644)
    migration.atomic_replace(target, "NEW=2\n")
    assert target.read_text() == "NEW=2\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert list(tmp_path.glob(".recovery.env.*.tmp")) == []


def test_check_only_identity_mode_parses_config_without_sourcing_it(
    tmp_path: Path,
) -> None:
    template_path = ROOT / "deploy/hindsight-recovery/recovery.env.example"
    template = template_path.read_text()
    source = tmp_path / "outbox.py"
    source.write_text("reviewed source\n")
    identities = _identities(source)
    marker = tmp_path / "must-not-exist"
    config = tmp_path / "recovery.env"
    configured = template
    for key, value in (
        ("EXPECTED_AGENT_IMAGE_DIGEST", identities["expected_image_digest"]),
        ("EXPECTED_AGENT_COMPOSE_HASH", identities["expected_compose_hash"]),
        ("EXPECTED_AGENT_OUTBOX_BUILD_ID", identities["expected_build_id"]),
        ("EXPECTED_RECOVERY_OUTBOX_BUILD_ID", identities["expected_build_id"]),
    ):
        configured = configured.replace(f"{key}=\n", f"{key}={value}\n")
    config.write_text(configured + f"UNTRUSTED_VALUE=$(touch {marker})\n")

    result = subprocess.run(
        [
            sys.executable,
            str(MODULE),
            "--config",
            str(config),
            "--template",
            str(template_path),
            "--outbox-source",
            str(source),
            "--recovery-module",
            str(ROOT / "deploy/hindsight-recovery/recovery.py"),
            "--recovery-root",
            str(ROOT),
            "--identities-from-config",
            "--check-only",
        ],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
