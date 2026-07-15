from __future__ import annotations

import re
from pathlib import Path


PACKAGE = Path(__file__).resolve().parents[1]


def test_runtime_and_test_dependencies_are_exactly_pinned() -> None:
    runtime = (PACKAGE / "requirements.lock").read_text().splitlines()
    runtime_requirements = [
        line for line in runtime if line and not line.startswith("#")
    ]
    assert runtime_requirements == [
        "psycopg[binary]==3.3.4",
        'typing_extensions==4.15.0; python_version < "3.13"',
    ]

    test = (PACKAGE / "requirements-test.lock").read_text().splitlines()
    assert test == [
        "-r requirements.lock",
        "iniconfig==2.3.0",
        "packaging==26.0",
        "pluggy==1.6.0",
        "Pygments==2.19.2",
        "pytest==9.1.1",
    ]


def test_production_package_has_no_checkout_specific_home_path() -> None:
    paths = [
        PACKAGE / "README.md",
        PACKAGE / "install.sh",
        PACKAGE / "recovery.env.example",
        PACKAGE / "recovery.py",
        PACKAGE / "verify-agent-runtime.py",
        *sorted((PACKAGE / "systemd").glob("*.service")),
    ]
    retired = re.compile(r"/home/ubuntu/(?:teemesh|attestmesh)")
    for path in paths:
        assert retired.search(path.read_text()) is None, path


def test_units_execute_only_the_installed_content_addressed_runtime() -> None:
    units = {path.name: path.read_text() for path in (PACKAGE / "systemd").glob("*.service")}
    for name, source in units.items():
        assert "%h/.local/share/hindsight-recovery/current/" in source, name
        assert "/deploy/hindsight-recovery/" not in source, name
    assert "current/runtime/tunnel.sh" in units["hindsight-recovery-tunnel.service"]
    for name in (
        "hindsight-recovery-controller.service",
        "hindsight-recovery-guard.service",
        "hindsight-recovery-watchdog.service",
    ):
        assert "current/venv/bin/python" in units[name]
        assert "current/runtime/recovery.py" in units[name]


def test_readme_declares_quarantine_and_manual_rollback() -> None:
    readme = (PACKAGE / "README.md").read_text()
    assert "Only the read tunnel is approved for continuous operation" in readme
    assert "still does not start or enable a\nservice" in readme
    assert "emergency-open --reason 'manual controller rollback'" in readme
