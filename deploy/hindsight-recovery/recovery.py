#!/usr/bin/env python3
"""Restart-safe, fail-closed controller for the Hindsight backfill.

The guard and watchdog may open the Agent circuit but never close it.  The
persistent controller is the sole automatic circuit-close authority and does
so only after restart-safe clean gates.  Existing submitted operations are
never submitted again: automatic reconciliation calls the application's own
outbox refresh/adoption/retry helpers only after proving an exact remote
operation or durable, repeated operation/document absence.
"""

from __future__ import annotations

import argparse
import contextlib
from collections import Counter
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import re
import secrets
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from email.utils import parsedate_to_datetime
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Iterable

import psycopg
from psycopg.rows import dict_row


ATTEMPTED_STATES = ("submitting", "submitted", "succeeded", "failed", "blocked")
ACTIVE_STATES = ("submitting", "submitted")
ERROR_STATES = ("failed", "blocked")
OUTBOX_STATES = frozenset({"queued", *ATTEMPTED_STATES})
SPOTCHECK_VERSION = 1
SPOTCHECK_PAUSE_PREFIX = "continuous spot-check pause"
REDPILL_CREDIT_EXHAUSTED_REASON = "upstream Redpill credit exhausted"
POST_INCIDENT_RELEASE_SERVICES = (
    "hindsight-recovery-controller.service",
    "hindsight-recovery-guard.service",
    "hindsight-recovery-watchdog.service",
    "hindsight-recovery-tunnel.service",
)
POST_INCIDENT_RELEASE_TICKET_VERSION = 3
# A post-incident ticket is still revalidated against a fresh authoritative
# snapshot immediately before close.  Its lifetime only bounds replay of that
# exact evidence.  It must therefore outlive one serialized audit window: the
# guard may already own the shared read lock for up to four heartbeat windows
# before the controller can run its own bounded quick proof.  Sixty seconds
# was shorter than that legitimate wait in production and made every clean
# completed refresh fall back to the much slower full release gate.
POST_INCIDENT_RELEASE_TTL_SECONDS = 300.0
POST_INCIDENT_HINDSIGHT_STATUSES = (
    "pending",
    "processing",
    "completed",
    "failed",
    "cancelled",
)
CHECKPOINT_VERSION = 1
PHASE_VERSION = 1
INCIDENT_VERSION = 1
FINAL_HOLD_REASON = "hindsight backfill complete: maintenance hold"
FINAL_BUDGET_HOLD_REASON = "hindsight backfill complete: maintenance hold"
STAGED_RECOVERY_HELPER_FAILURE = (
    "controller recovery helper differs from Agent runtime helper"
)
CONTROLLER_TERMINAL_PHASES = {"finalized"}
QWEN_MODEL = "qwen/qwen3-embedding-8b"
RECOVERY_PROBE_NAMESPACE = uuid.UUID("bf0f7e3e-92c5-4c41-aada-9b7abbe20df8")
PROVIDER_LEDGER_VERSION = 2
LEGACY_PROVIDER_LEDGER_VERSION = 1
LEGACY_PROVIDER_CALL_KEYS = frozenset(
    {
        "at",
        "started_at",
        "request_id",
        "endpoint",
        "model",
        "scope",
        "period",
        "input_tokens",
        "output_tokens",
        "cost_usd",
        "provider_reported_cost_usd",
        "estimated_from_reservation",
    }
)


class InvariantError(RuntimeError):
    pass


class PostIncidentReleaseInvalid(InvariantError):
    """Fresh release evidence disproved a durable abbreviated-release ticket."""


def _recovery_root() -> Path:
    """Resolve the operational checkout without depending on its directory name."""

    configured = os.getenv("RECOVERY_ROOT", "").strip()
    if configured:
        root = Path(configured).expanduser()
    else:
        # This fallback is for direct execution from a source checkout and for
        # tests.  The installed runtime is given an explicit RECOVERY_ROOT by
        # install.sh because it intentionally lives outside the checkout.
        root = Path(__file__).resolve().parents[2]
        if not (root / "deploy").is_dir():
            raise ValueError(
                "RECOVERY_ROOT is required when recovery.py is run outside "
                "an AttestMesh checkout"
            )
    if not root.is_absolute():
        raise ValueError("RECOVERY_ROOT must be an absolute path")
    return root.resolve()


@dataclass(frozen=True)
class EmergencyConfig:
    """Minimal independently parsed authority for fail-closed circuit opens.

    This deliberately excludes every stage, budget, image, and controller
    setting.  A corrupt or still-sealed old stage configuration must never
    prevent the independent one-shot Agent/Pocket hold path from running.
    """

    root: Path
    state_dir: Path
    ssh_target: str
    pg_remote_hosts: tuple[str, str, str]

    @classmethod
    def from_env(cls) -> "EmergencyConfig":
        root = _recovery_root()
        state_dir = Path(
            os.getenv(
                "RECOVERY_STATE_DIR",
                str(Path.home() / ".local/state/hindsight-recovery"),
            )
            or str(Path.home() / ".local/state/hindsight-recovery")
        )
        hosts = tuple(
            value.strip()
            for value in os.getenv(
                "PG_REMOTE_HOSTS",
                "10.18.147.86,10.18.251.71,10.18.172.186",
            ).split(",")
        )
        if len(hosts) != 3 or any(not value for value in hosts):
            raise ValueError("emergency PG host list must contain exactly three hosts")
        return cls(
            root=root,
            state_dir=state_dir,
            ssh_target=os.getenv("RECOVERY_SSH_TARGET", "attestmesh-mesh-node"),
            pg_remote_hosts=hosts,  # type: ignore[arg-type]
        )

    @property
    def lock_file(self) -> Path:
        return self.state_dir / "emergency-open.lock"

    @property
    def event_log(self) -> Path:
        return self.state_dir / "events.jsonl"


def _env_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


@dataclass(frozen=True)
class Config:
    root: Path
    state_dir: Path
    ssh_target: str
    pg_local_ports: tuple[int, int, int]
    pg_remote_hosts: tuple[str, str, str]
    hindsight_port: int
    budget_port: int
    fugu_port: int
    patroni_ports: tuple[int, int, int]
    approved_total: int
    stage_cap: int
    max_agent_active: int
    max_provider_in_flight: int
    conservative_remaining_cost: Decimal
    hard_budget: Decimal
    quick_seconds: float
    full_seconds: float
    watchdog_seconds: float
    heartbeat_max_age: float
    controller_seconds: float
    controller_initial_backoff: float
    controller_max_backoff: float
    controller_stable_checks: int
    controller_full_audits: int
    budget_maintenance_hold_url: str
    agent_runtime_proof_file: Path
    agent_runtime_proof_max_age: float
    agent_progress_timeout: float
    expected_agent_image_digest: str
    expected_agent_compose_hash: str
    expected_hindsight_model: str
    expected_hindsight_phase: str
    expected_hindsight_llm_concurrency: int
    expected_provider_effective_limit: Decimal
    agent_runtime_verifier_command: str
    expected_agent_outbox_build_id: str
    expected_recovery_outbox_build_id: str = ""
    allow_legacy_held_stage: bool = False

    def __post_init__(self) -> None:
        approved = (
            self.approved_total,
            self.stage_cap,
            self.max_agent_active,
            self.max_provider_in_flight,
            self.hard_budget,
            self.expected_hindsight_llm_concurrency,
        )
        legacy_held = approved == (5142, 3823, 2, 6, Decimal("30"), 3)
        if approved != (5142, 5142, 2, 6, Decimal("30"), 3) and not (
            self.allow_legacy_held_stage and legacy_held
        ):
            raise ValueError(
                "recovery authority must remain exactly 5142/5142/2/6/$30/3"
            )
        if self.conservative_remaining_cost != Decimal("0.00543"):
            raise ValueError(
                "conservative remaining-row cost must remain exactly $0.00543"
            )
        if (
            not self.expected_provider_effective_limit.is_finite()
            or self.expected_provider_effective_limit != Decimal("49.5")
        ):
            raise ValueError(
                "provider effective limit must remain exactly the approved 49.5"
            )
        timings = (
            self.quick_seconds,
            self.full_seconds,
            self.watchdog_seconds,
            self.heartbeat_max_age,
            self.controller_seconds,
            self.controller_initial_backoff,
            self.controller_max_backoff,
            self.agent_runtime_proof_max_age,
            self.agent_progress_timeout,
        )
        if any(not math.isfinite(value) or value <= 0 for value in timings):
            raise ValueError(
                "recovery timing/backoff values must be finite and positive"
            )
        if self.controller_max_backoff < self.controller_initial_backoff:
            raise ValueError("controller maximum backoff is below its initial backoff")
        if self.controller_stable_checks < 10 or self.controller_full_audits < 2:
            raise ValueError("controller stability/audit gates cannot be weakened")

    @classmethod
    def from_env(cls, *, allow_legacy_held_stage: bool = False) -> "Config":
        root = _recovery_root()
        state_dir = Path(
            os.getenv(
                "RECOVERY_STATE_DIR",
                str(Path.home() / ".local/state/hindsight-recovery"),
            )
            or str(Path.home() / ".local/state/hindsight-recovery")
        )
        pg_ports = tuple(
            int(v) for v in os.getenv("PG_LOCAL_PORTS", "55439,55440,55441").split(",")
        )
        pg_hosts = tuple(
            os.getenv(
                "PG_REMOTE_HOSTS", "10.18.147.86,10.18.251.71,10.18.172.186"
            ).split(",")
        )
        patroni_ports = tuple(
            int(v)
            for v in os.getenv("PATRONI_LOCAL_PORTS", "18081,18082,18083").split(",")
        )
        if len(pg_ports) != 3 or len(pg_hosts) != 3 or len(patroni_ports) != 3:
            raise ValueError(
                "PG and Patroni host/port lists must each contain exactly three entries"
            )
        return cls(
            root=root,
            state_dir=state_dir,
            ssh_target=os.getenv("RECOVERY_SSH_TARGET", "attestmesh-mesh-node"),
            pg_local_ports=pg_ports,  # type: ignore[arg-type]
            pg_remote_hosts=pg_hosts,  # type: ignore[arg-type]
            hindsight_port=_int("HINDSIGHT_LOCAL_PORT", 18898),
            budget_port=_int("BUDGET_LOCAL_PORT", 18899),
            fugu_port=_int("FUGU_LOCAL_PORT", 18419),
            patroni_ports=patroni_ports,  # type: ignore[arg-type]
            approved_total=_int("APPROVED_TOTAL", 5142),
            stage_cap=_int("STAGE_CAP", 5142),
            max_agent_active=_int("MAX_AGENT_ACTIVE", 2),
            max_provider_in_flight=_int("MAX_PROVIDER_IN_FLIGHT", 6),
            conservative_remaining_cost=Decimal(
                os.getenv("CONSERVATIVE_REMAINING_COST_USD", "0.00543")
            ),
            hard_budget=Decimal(os.getenv("HARD_BUDGET_USD", "30")),
            quick_seconds=float(os.getenv("GUARD_QUICK_SECONDS", "2")),
            full_seconds=float(os.getenv("GUARD_FULL_SECONDS", "60")),
            watchdog_seconds=float(os.getenv("WATCHDOG_SECONDS", "5")),
            # A normal full provenance audit takes longer than one 5-second
            # watchdog interval.  Sixty seconds still fails closed promptly,
            # while allowing a full audit to finish under transient host load.
            heartbeat_max_age=float(os.getenv("HEARTBEAT_MAX_AGE_SECONDS", "60")),
            controller_seconds=float(os.getenv("CONTROLLER_SECONDS", "5")),
            controller_initial_backoff=float(
                os.getenv("CONTROLLER_INITIAL_BACKOFF_SECONDS", "5")
            ),
            controller_max_backoff=float(
                os.getenv("CONTROLLER_MAX_BACKOFF_SECONDS", "300")
            ),
            controller_stable_checks=_int("CONTROLLER_STABLE_CHECKS", 10),
            controller_full_audits=_int("CONTROLLER_FULL_AUDITS", 2),
            budget_maintenance_hold_url=os.getenv(
                "BUDGET_MAINTENANCE_HOLD_URL",
                "http://127.0.0.1:18899/admin/maintenance-hold",
            ),
            agent_runtime_proof_file=Path(
                os.getenv(
                    "AGENT_RUNTIME_PROOF_FILE",
                    str(state_dir / "agent-runtime-proof.json"),
                )
                or str(state_dir / "agent-runtime-proof.json")
            ),
            agent_runtime_proof_max_age=float(
                os.getenv("AGENT_RUNTIME_PROOF_MAX_AGE_SECONDS", "120")
            ),
            agent_progress_timeout=float(
                os.getenv("AGENT_PROGRESS_TIMEOUT_SECONDS", "180")
            ),
            expected_agent_image_digest=os.getenv(
                "EXPECTED_AGENT_IMAGE_DIGEST", ""
            ).strip(),
            expected_agent_compose_hash=os.getenv(
                "EXPECTED_AGENT_COMPOSE_HASH", ""
            ).strip(),
            expected_hindsight_model=os.getenv(
                "EXPECTED_HINDSIGHT_MODEL", "openai/gpt-oss-120b"
            ).strip(),
            expected_hindsight_phase=os.getenv(
                "EXPECTED_HINDSIGHT_PHASE", "backfill"
            ).strip(),
            expected_hindsight_llm_concurrency=_int(
                "EXPECTED_HINDSIGHT_LLM_CONCURRENCY", 3
            ),
            expected_provider_effective_limit=Decimal(
                os.getenv("EXPECTED_PROVIDER_EFFECTIVE_LIMIT_USD", "49.5")
            ),
            agent_runtime_verifier_command=(
                os.getenv("AGENT_RUNTIME_VERIFIER_COMMAND", "").strip()
                or " ".join(
                    (
                        shlex.quote(sys.executable),
                        shlex.quote(
                            str(Path(__file__).with_name("verify-agent-runtime.py"))
                        ),
                    )
                )
            ),
            expected_agent_outbox_build_id=os.getenv(
                "EXPECTED_AGENT_OUTBOX_BUILD_ID", ""
            ).strip(),
            expected_recovery_outbox_build_id=os.getenv(
                "EXPECTED_RECOVERY_OUTBOX_BUILD_ID",
                os.getenv("EXPECTED_AGENT_OUTBOX_BUILD_ID", ""),
            ).strip(),
            allow_legacy_held_stage=allow_legacy_held_stage,
        )

    @property
    def phase_file(self) -> Path:
        return self.state_dir / "phase.json"

    @property
    def phase_lock_file(self) -> Path:
        return self.state_dir / "phase.lock"

    @property
    def heartbeat_file(self) -> Path:
        return self.state_dir / "guard-heartbeat.json"

    @property
    def watchdog_file(self) -> Path:
        return self.state_dir / "watchdog-heartbeat.json"

    @property
    def event_log(self) -> Path:
        return self.state_dir / "events.jsonl"

    @property
    def lock_file(self) -> Path:
        return self.state_dir / "emergency-open.lock"

    @property
    def checkpoint_file(self) -> Path:
        return self.state_dir / "checkpoint.json"

    @property
    def checkpoint_log(self) -> Path:
        return self.state_dir / "checkpoints.jsonl"

    @property
    def checkpoint_lock_file(self) -> Path:
        return self.state_dir / "checkpoint.lock"

    @property
    def incident_file(self) -> Path:
        return self.state_dir / "incident.json"

    @property
    def incident_lock_file(self) -> Path:
        return self.state_dir / "incident.lock"

    @property
    def controller_file(self) -> Path:
        return self.state_dir / "controller-heartbeat.json"

    @property
    def controller_lock_file(self) -> Path:
        return self.state_dir / "controller.lock"

    @property
    def authoritative_read_lock_file(self) -> Path:
        return self.state_dir / "authoritative-read.lock"

    @property
    def spotcheck_file(self) -> Path:
        return self.state_dir / "spot-check.json"

    @property
    def spotcheck_lock_file(self) -> Path:
        return self.state_dir / "spot-check.lock"


DEFAULT_PHASE: dict[str, Any] = {
    "version": PHASE_VERSION,
    "revision": 1,
    "name": "maintenance",
    "budget": "open",
    "expected_retry": 17,
    "expected_succeeded": 0,
    "watchdog_armed": False,
}

DEFAULT_INCIDENT: dict[str, Any] = {
    "version": INCIDENT_VERSION,
    "revision": 1,
    "status": "startup_hold",
    "kind": "startup",
    "attempts": 0,
    "clean_checks": 0,
    "full_audits": 0,
    "next_retry_at": 0.0,
    "absence_proofs": {},
    "authorization_archive": [],
}


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    """Atomically replace one JSON file and durably commit its directory entry."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".new"
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(value, default=str, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def _append_jsonl_fsync(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, default=str, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


@contextlib.contextmanager
def _exclusive_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _load_revisioned_json(
    path: Path,
    lock_path: Path,
    default: dict[str, Any],
    *,
    version: int,
    label: str,
) -> dict[str, Any]:
    with _exclusive_lock(lock_path):
        if not path.exists():
            _atomic_json(path, default)
        raw = json.loads(path.read_text())
        value = {**default, **raw}
        if int(value.get("version", 0)) != version:
            raise InvariantError(f"unsupported {label} version")
        # Upgrade the pre-controller state files exactly once under the lock.
        if "revision" not in raw:
            value["revision"] = 1
            _atomic_json(path, value)
        if int(value.get("revision", 0)) < 1:
            raise InvariantError(f"invalid {label} revision")
        return value


def load_phase(cfg: Config) -> dict[str, Any]:
    return _load_revisioned_json(
        cfg.phase_file,
        cfg.phase_lock_file,
        DEFAULT_PHASE,
        version=PHASE_VERSION,
        label="recovery phase",
    )


def save_phase(cfg: Config, phase: dict[str, Any]) -> None:
    with _exclusive_lock(cfg.phase_lock_file):
        if not cfg.phase_file.exists():
            raise InvariantError("recovery phase disappeared before save")
        current = {**DEFAULT_PHASE, **json.loads(cfg.phase_file.read_text())}
        expected_revision = int(phase.get("revision", 0))
        if expected_revision != int(current.get("revision", 0)):
            raise InvariantError("recovery phase changed during attempted save")
        value = dict(phase)
        value.update(
            {
                "version": PHASE_VERSION,
                "revision": expected_revision + 1,
                "updated_at": time.time(),
            }
        )
        _atomic_json(cfg.phase_file, value)
        phase.clear()
        phase.update(value)


def event(cfg: Config, kind: str, **fields: Any) -> None:
    payload = {"at": time.time(), "kind": kind, **fields}
    _append_jsonl_fsync(cfg.event_log, payload)


def _json_digest(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _canonical_http_status(value: Any) -> int | None:
    return None if value is None else int(value)


def _canonical_datetime_text(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _record_sha256(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            default=str,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()


def _exact_record(value: dict[str, Any], *identity_fields: str) -> dict[str, Any]:
    raw = json.loads(json.dumps(dict(value), default=str, sort_keys=True))
    result = {field: raw.get(field) for field in identity_fields}
    result["record_sha256"] = _record_sha256(raw)
    return result


def _is_exact_legacy_provider_call(value: Any) -> bool:
    """Match only the immutable normal-call schema of the pinned v1 writer."""
    return (
        isinstance(value, dict)
        and frozenset(value) == LEGACY_PROVIDER_CALL_KEYS
        and type(value.get("estimated_from_reservation")) is bool
    )


def _default_checkpoint(cfg: Config, phase: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": CHECKPOINT_VERSION,
        "revision": 1,
        "accepted": {
            "fugu_failures": [],
            "fugu_failure_manifest": None,
            "provider_failure_manifest": None,
            "hindsight_failed": 0,
            "histories": {
                "retry": 0,
                "claim": 0,
                "adoption": 0,
            },
            # Exact manifests are bootstrapped only after their configured
            # aggregate/Fugu baselines have been independently matched.
            "hindsight_failures": None,
            "history_manifests": None,
            # v2 exact records coexist with the sealed legacy summaries.  This
            # permits a one-time independently proved migration without ever
            # rewriting an accepted rev2 value in place.
            "exact_hindsight_failures": None,
            "exact_history_manifests": None,
            # Pocket never participates in this recovery.  Its exact held
            # state is captured during the sealed configure-stage bootstrap
            # and must remain byte-for-byte stable thereafter.
            "pocket_counts": None,
            # Every terminal Agent-bank Hindsight operation, including
            # consolidation, is accepted by exact identity and full-record
            # digest.  The list is acceptance ordered and prefix append-only.
            "operation_manifest": None,
        },
        "recovered_attempts": [],
        "bootstrap_pending": True,
        "stage": {
            "approved_total": cfg.approved_total,
            "cap": cfg.stage_cap,
            "max_agent_active": cfg.max_agent_active,
            "max_provider_in_flight": cfg.max_provider_in_flight,
        },
        "created_at": time.time(),
        "updated_at": time.time(),
    }


def _validate_checkpoint(cfg: Config, checkpoint: dict[str, Any]) -> None:
    if int(checkpoint.get("version", 0)) != CHECKPOINT_VERSION:
        raise InvariantError("unsupported recovery checkpoint version")
    if int(checkpoint.get("revision", 0)) < 1:
        raise InvariantError("invalid recovery checkpoint revision")
    accepted = checkpoint.get("accepted") or {}
    histories = accepted.get("histories") or {}
    if any(int(histories.get(name, -1)) < 0 for name in ("retry", "claim", "adoption")):
        raise InvariantError("checkpoint contains an invalid history baseline")
    if int(accepted.get("hindsight_failed", -1)) < 0:
        raise InvariantError(
            "checkpoint contains an invalid Hindsight failure baseline"
        )
    failures = accepted.get("fugu_failures")
    if not isinstance(failures, list) or any(
        not isinstance(value, list) or len(value) != 3 for value in failures
    ):
        raise InvariantError("checkpoint contains an invalid Fugu ledger baseline")
    fugu_manifest = accepted.get("fugu_failure_manifest")
    if fugu_manifest is not None and (
        not isinstance(fugu_manifest, list)
        or any(
            not isinstance(value, dict)
            or not str(value.get("request_id") or "")
            or not str(value.get("record_sha256") or "")
            for value in fugu_manifest
        )
    ):
        raise InvariantError("checkpoint exact Fugu failure manifest is invalid")
    provider_failure_manifest = accepted.get("provider_failure_manifest")
    if provider_failure_manifest is not None and (
        not isinstance(provider_failure_manifest, list)
        or any(
            not isinstance(value, dict)
            or not str(value.get("request_id") or "")
            or not str(value.get("record_sha256") or "")
            for value in provider_failure_manifest
        )
    ):
        raise InvariantError("checkpoint provider failure manifest is invalid")
    for label, manifest in (
        ("Fugu", fugu_manifest),
        ("provider", provider_failure_manifest),
    ):
        if manifest is not None:
            identities = [str(value["request_id"]) for value in manifest]
            if len(identities) != len(set(identities)):
                raise InvariantError(
                    f"checkpoint exact {label} manifest identities are duplicated"
                )
    hindsight_manifest = accepted.get("hindsight_failures")
    history_manifests = accepted.get("history_manifests")
    exact_hindsight_manifest = accepted.get("exact_hindsight_failures")
    exact_history_manifests = accepted.get("exact_history_manifests")
    if hindsight_manifest is not None and not isinstance(hindsight_manifest, list):
        raise InvariantError("checkpoint Hindsight failure manifest is invalid")
    if history_manifests is not None and (
        not isinstance(history_manifests, dict)
        or any(
            not isinstance(history_manifests.get(name), list)
            for name in ("retry", "claim", "adoption")
        )
    ):
        raise InvariantError("checkpoint history manifests are invalid")
    if exact_hindsight_manifest is not None and (
        not isinstance(exact_hindsight_manifest, list)
        or any(
            not isinstance(value, dict)
            or not str(value.get("operation_id") or "")
            or not str(value.get("record_sha256") or "")
            for value in exact_hindsight_manifest
        )
    ):
        raise InvariantError("checkpoint exact Hindsight manifest is invalid")
    if exact_hindsight_manifest is not None:
        identities = [str(value["operation_id"]) for value in exact_hindsight_manifest]
        if len(identities) != len(set(identities)):
            raise InvariantError(
                "checkpoint exact Hindsight manifest identities are duplicated"
            )
    if exact_history_manifests is not None and (
        not isinstance(exact_history_manifests, dict)
        or any(
            not isinstance(exact_history_manifests.get(name), list)
            or any(
                not isinstance(value, dict)
                or int(value.get("id", 0)) <= 0
                or not str(value.get("record_sha256") or "")
                for value in exact_history_manifests.get(name, [])
            )
            for name in ("retry", "claim", "adoption")
        )
    ):
        raise InvariantError("checkpoint exact history manifests are invalid")
    if exact_history_manifests is not None:
        for name in ("retry", "claim", "adoption"):
            identities = [int(value["id"]) for value in exact_history_manifests[name]]
            if len(identities) != len(set(identities)):
                raise InvariantError(
                    f"checkpoint exact {name} history identities are duplicated"
                )
    pocket_counts = accepted.get("pocket_counts")
    if pocket_counts is not None and (
        not isinstance(pocket_counts, dict)
        or any(int(value) < 0 for value in pocket_counts.values())
        or any(not isinstance(key, str) for key in pocket_counts)
    ):
        raise InvariantError("checkpoint Pocket count baseline is invalid")
    operation_manifest = accepted.get("operation_manifest")
    if operation_manifest is not None and (
        not isinstance(operation_manifest, list)
        or any(
            not isinstance(value, dict)
            or not str(value.get("operation_id") or "")
            or str(value.get("operation_type") or "")
            not in {"batch_retain", "retain", "consolidation"}
            or str(value.get("status") or "")
            not in {"completed", "failed", "cancelled"}
            or not str(value.get("record_sha256") or "")
            for value in operation_manifest
        )
    ):
        raise InvariantError("checkpoint terminal operation manifest is invalid")
    if operation_manifest is not None:
        identities = [str(value["operation_id"]) for value in operation_manifest]
        if len(identities) != len(set(identities)):
            raise InvariantError(
                "checkpoint terminal operation identities are duplicated"
            )
    recovered = checkpoint.get("recovered_attempts")
    if not isinstance(recovered, list):
        raise InvariantError("checkpoint recovered-attempt manifest is invalid")
    keys = [str(value.get("key")) for value in recovered if isinstance(value, dict)]
    if len(keys) != len(recovered) or len(keys) != len(set(keys)):
        raise InvariantError(
            "checkpoint recovered-attempt keys are missing or duplicated"
        )
    stage = checkpoint.get("stage") or {}
    if int(stage.get("approved_total", -1)) != cfg.approved_total:
        raise InvariantError("checkpoint approved-total drift")
    cap = int(stage.get("cap", -1))
    if not 0 < cap <= cfg.stage_cap:
        raise InvariantError("checkpoint stage cap is outside configured authority")
    if int(stage.get("max_agent_active", -1)) != cfg.max_agent_active:
        raise InvariantError("checkpoint Agent concurrency drift")
    if int(stage.get("max_provider_in_flight", -1)) != cfg.max_provider_in_flight:
        raise InvariantError("checkpoint provider ceiling drift")


def _validate_checkpoint_transition(
    cfg: Config, previous: dict[str, Any], candidate: dict[str, Any]
) -> None:
    """Prove one exact monotonic checkpoint transition."""
    _validate_checkpoint(cfg, previous)
    _validate_checkpoint(cfg, candidate)
    if int(candidate["revision"]) != int(previous["revision"]) + 1:
        raise InvariantError("checkpoint revision is not consecutive")
    old = previous["accepted"]
    new = candidate["accepted"]
    old_failures = old["fugu_failures"]
    new_failures = new["fugu_failures"]
    if new_failures[: len(old_failures)] != old_failures:
        raise InvariantError("checkpoint Fugu baseline is not append-only")
    old_fugu_manifest = old.get("fugu_failure_manifest")
    new_fugu_manifest = new.get("fugu_failure_manifest")
    if old_fugu_manifest is not None:
        if new_fugu_manifest is None:
            raise InvariantError("checkpoint exact Fugu manifest was removed")
        if new_fugu_manifest[: len(old_fugu_manifest)] != old_fugu_manifest:
            raise InvariantError("checkpoint exact Fugu manifest is not append-only")
    old_provider_manifest = old.get("provider_failure_manifest")
    new_provider_manifest = new.get("provider_failure_manifest")
    if old_provider_manifest is not None:
        if new_provider_manifest is None:
            raise InvariantError("checkpoint provider failure manifest was removed")
        if new_provider_manifest[: len(old_provider_manifest)] != old_provider_manifest:
            raise InvariantError(
                "checkpoint provider failure manifest is not append-only"
            )
    if int(new["hindsight_failed"]) < int(old["hindsight_failed"]):
        raise InvariantError("checkpoint Hindsight failure baseline decreased")
    for name in ("retry", "claim", "adoption"):
        if int(new["histories"][name]) < int(old["histories"][name]):
            raise InvariantError(f"checkpoint {name} history baseline decreased")
    old_hindsight_manifest = old.get("hindsight_failures")
    new_hindsight_manifest = new.get("hindsight_failures")
    if old_hindsight_manifest is not None:
        if new_hindsight_manifest is None:
            raise InvariantError("checkpoint Hindsight failure manifest was removed")
        if (
            new_hindsight_manifest[: len(old_hindsight_manifest)]
            != old_hindsight_manifest
        ):
            raise InvariantError(
                "checkpoint Hindsight failure manifest is not append-only"
            )
    old_history_manifests = old.get("history_manifests")
    new_history_manifests = new.get("history_manifests")
    if old_history_manifests is not None:
        if new_history_manifests is None:
            raise InvariantError("checkpoint history manifests were removed")
        for name in ("retry", "claim", "adoption"):
            values = old_history_manifests[name]
            if new_history_manifests[name][: len(values)] != values:
                raise InvariantError(
                    f"checkpoint {name} history manifest is not append-only"
                )
    old_exact_hindsight = old.get("exact_hindsight_failures")
    new_exact_hindsight = new.get("exact_hindsight_failures")
    if old_exact_hindsight is not None:
        if new_exact_hindsight is None:
            raise InvariantError("checkpoint exact Hindsight manifest was removed")
        if new_exact_hindsight[: len(old_exact_hindsight)] != old_exact_hindsight:
            raise InvariantError(
                "checkpoint exact Hindsight manifest is not append-only"
            )
    old_exact_histories = old.get("exact_history_manifests")
    new_exact_histories = new.get("exact_history_manifests")
    if old_exact_histories is not None:
        if new_exact_histories is None:
            raise InvariantError("checkpoint exact history manifests were removed")
        for name in ("retry", "claim", "adoption"):
            if (
                new_exact_histories[name][: len(old_exact_histories[name])]
                != old_exact_histories[name]
            ):
                raise InvariantError(
                    f"checkpoint exact {name} history manifest is not append-only"
                )
    old_pocket = old.get("pocket_counts")
    new_pocket = new.get("pocket_counts")
    if old_pocket is not None and new_pocket != old_pocket:
        raise InvariantError("checkpoint Pocket count baseline changed")
    old_operations = old.get("operation_manifest")
    new_operations = new.get("operation_manifest")
    if old_operations is not None:
        if new_operations is None:
            raise InvariantError("checkpoint terminal operation manifest was removed")
        if new_operations[: len(old_operations)] != old_operations:
            raise InvariantError(
                "checkpoint terminal operation manifest is not append-only"
            )
    old_attempts = previous["recovered_attempts"]
    if candidate["recovered_attempts"][: len(old_attempts)] != old_attempts:
        raise InvariantError("checkpoint recovered-attempt manifest is not append-only")
    old_stage = previous["stage"]
    new_stage = candidate["stage"]
    if int(new_stage["cap"]) < int(old_stage["cap"]):
        raise InvariantError("checkpoint stage cap decreased")
    if int(new_stage["cap"]) > cfg.approved_total:
        raise InvariantError("checkpoint stage cap exceeds approved total")


def _checkpoint_log_entries(cfg: Config) -> list[dict[str, Any]]:
    if not cfg.checkpoint_log.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line_number, raw in enumerate(
        cfg.checkpoint_log.read_text().splitlines(), start=1
    ):
        if not raw.strip():
            raise InvariantError(
                f"checkpoint log contains a blank record at line {line_number}"
            )
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise InvariantError(
                f"checkpoint log is truncated/corrupt at line {line_number}"
            ) from exc
        if not isinstance(entry, dict) or not isinstance(entry.get("checkpoint"), dict):
            raise InvariantError(f"checkpoint log record {line_number} is invalid")
        checkpoint = entry["checkpoint"]
        _validate_checkpoint(cfg, checkpoint)
        revision = int(entry.get("revision", 0))
        digest = str(entry.get("digest") or "")
        if revision != line_number or revision != int(checkpoint["revision"]):
            raise InvariantError("checkpoint log revision chain is not consecutive")
        if digest != _json_digest(checkpoint):
            raise InvariantError("checkpoint log digest mismatch")
        if entries:
            previous = entries[-1]
            if entry.get("previous_digest") != previous["digest"]:
                raise InvariantError("checkpoint log previous-digest chain mismatch")
            _validate_checkpoint_transition(cfg, previous["checkpoint"], checkpoint)
        elif revision != 1 or entry.get("previous_digest") not in {None, ""}:
            raise InvariantError("checkpoint log genesis record is invalid")
        entries.append(entry)
    return entries


def _checkpoint_entry(
    checkpoint: dict[str, Any],
    *,
    kind: str,
    previous: dict[str, Any] | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "at": time.time(),
        "kind": kind,
        "revision": int(checkpoint["revision"]),
        "digest": _json_digest(checkpoint),
        "checkpoint": checkpoint,
    }
    if previous is not None:
        entry["previous_digest"] = _json_digest(previous)
    if reason:
        entry["reason"] = reason[:1000]
    return entry


def _load_checkpoint_locked(
    cfg: Config, current_phase: dict[str, Any]
) -> dict[str, Any]:
    """Validate the durable journal and repair only a proven crash boundary."""
    entries = _checkpoint_log_entries(cfg)
    if not entries and not cfg.checkpoint_file.exists():
        checkpoint = _default_checkpoint(cfg, current_phase)
        _append_jsonl_fsync(
            cfg.checkpoint_log,
            _checkpoint_entry(checkpoint, kind="checkpoint_created"),
        )
        _atomic_json(cfg.checkpoint_file, checkpoint)
        return checkpoint
    if not entries:
        raise InvariantError("checkpoint JSON exists without its append-only journal")
    journal_checkpoint = entries[-1]["checkpoint"]
    if not cfg.checkpoint_file.exists():
        # Journal-first commits are authoritative after their fsync.
        _atomic_json(cfg.checkpoint_file, journal_checkpoint)
        return journal_checkpoint
    checkpoint = json.loads(cfg.checkpoint_file.read_text())
    _validate_checkpoint(cfg, checkpoint)
    checkpoint_digest = _json_digest(checkpoint)
    journal_digest = str(entries[-1]["digest"])
    if checkpoint_digest == journal_digest:
        return checkpoint
    checkpoint_revision = int(checkpoint["revision"])
    journal_revision = int(journal_checkpoint["revision"])
    if (
        journal_revision == checkpoint_revision + 1
        and entries[-1].get("previous_digest") == checkpoint_digest
    ):
        # Crash after journal fsync but before checkpoint.json replacement.
        _validate_checkpoint_transition(cfg, checkpoint, journal_checkpoint)
        _atomic_json(cfg.checkpoint_file, journal_checkpoint)
        return journal_checkpoint
    if checkpoint_revision == journal_revision + 1:
        # Compatibility repair for the older checkpoint-first writer.  The
        # exact transition is independently revalidated before journaling it.
        _validate_checkpoint_transition(cfg, journal_checkpoint, checkpoint)
        _append_jsonl_fsync(
            cfg.checkpoint_log,
            _checkpoint_entry(
                checkpoint,
                kind="checkpoint_recovered",
                previous=journal_checkpoint,
                reason="recovered interrupted legacy checkpoint commit",
            ),
        )
        return checkpoint
    raise InvariantError("checkpoint JSON/journal divergence")


def load_checkpoint(cfg: Config, phase: dict[str, Any] | None = None) -> dict[str, Any]:
    current_phase = phase or load_phase(cfg)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    with _exclusive_lock(cfg.checkpoint_lock_file):
        return _load_checkpoint_locked(cfg, current_phase)


def read_checkpoint(cfg: Config) -> dict[str, Any]:
    """Read and validate the checkpoint without creating or repairing files."""
    with _exclusive_lock(cfg.checkpoint_lock_file):
        entries = _checkpoint_log_entries(cfg)
        if not entries or not cfg.checkpoint_file.exists():
            raise InvariantError("recovery checkpoint is not initialized")
        checkpoint = json.loads(cfg.checkpoint_file.read_text())
        _validate_checkpoint(cfg, checkpoint)
        if _json_digest(checkpoint) != str(entries[-1]["digest"]):
            raise InvariantError(
                "checkpoint requires recovery before a read-only audit"
            )
        return checkpoint


def append_checkpoint(
    cfg: Config,
    previous: dict[str, Any],
    updated: dict[str, Any],
    *,
    reason: str,
) -> dict[str, Any]:
    """Persist one monotonic checkpoint revision and its append-only proof."""
    _validate_checkpoint(cfg, previous)
    candidate = json.loads(json.dumps(updated, default=str))
    candidate["version"] = CHECKPOINT_VERSION
    candidate["revision"] = int(previous["revision"]) + 1
    candidate["created_at"] = previous.get("created_at", time.time())
    candidate["updated_at"] = time.time()
    _validate_checkpoint_transition(cfg, previous, candidate)
    with _exclusive_lock(cfg.checkpoint_lock_file):
        current = _load_checkpoint_locked(cfg, load_phase(cfg))
        if _json_digest(current) != _json_digest(previous):
            raise InvariantError("checkpoint changed during attempted append")
        # Journal first: a crash before the atomic JSON replacement is repaired
        # from this fsync'd, digest-linked record on the next load.
        _append_jsonl_fsync(
            cfg.checkpoint_log,
            _checkpoint_entry(
                candidate,
                kind="checkpoint_appended",
                previous=previous,
                reason=reason,
            ),
        )
        _atomic_json(cfg.checkpoint_file, candidate)
    event(
        cfg, "checkpoint_appended", revision=candidate["revision"], reason=reason[:1000]
    )
    return candidate


def load_incident(cfg: Config) -> dict[str, Any]:
    return _load_revisioned_json(
        cfg.incident_file,
        cfg.incident_lock_file,
        DEFAULT_INCIDENT,
        version=INCIDENT_VERSION,
        label="controller incident",
    )


def save_incident(
    cfg: Config, incident: dict[str, Any], *, kind: str | None = None
) -> None:
    with _exclusive_lock(cfg.incident_lock_file):
        if not cfg.incident_file.exists():
            raise InvariantError("controller incident disappeared before save")
        previous = {
            **DEFAULT_INCIDENT,
            **json.loads(cfg.incident_file.read_text()),
        }
        expected_revision = int(incident.get("revision", 0))
        if expected_revision != int(previous.get("revision", 0)):
            raise InvariantError("controller incident changed during attempted save")
        value = dict(incident)
        old_archive = list(previous.get("authorization_archive") or [])
        new_archive = list(value.get("authorization_archive") or [])
        if new_archive[: len(old_archive)] != old_archive:
            raise InvariantError(
                "controller incident authorization archive is not append-only"
            )
        value.update(
            {
                "version": INCIDENT_VERSION,
                "revision": expected_revision + 1,
                "updated_at": time.time(),
            }
        )
        _atomic_json(cfg.incident_file, value)
        incident.clear()
        incident.update(value)
    if kind or previous.get("status") != value.get("status"):
        event(
            cfg,
            kind or "incident_transition",
            previous_status=previous.get("status"),
            status=value.get("status"),
            incident_kind=value.get("kind"),
            attempts=value.get("attempts"),
            last_error=value.get("last_error"),
        )


def bounded_backoff(attempts: int, initial: float, maximum: float) -> float:
    return min(maximum, initial * (2 ** max(attempts - 1, 0)))


def release_gate_requires_full_audit(
    *,
    clean_checks: int,
    full_audits: int,
    required_clean_checks: int,
    required_full_audits: int,
) -> bool:
    """Place durable full audits at the start and end of a clean gate.

    Intermediate iterations remain authoritative quick checks.  If a
    restart resumes unusual but valid counters, perform any missing full
    audits before allowing the gate to complete.
    """
    if full_audits >= required_full_audits:
        return False
    next_clean_check = clean_checks + 1
    return (
        full_audits < required_full_audits - 1
        or next_clean_check >= required_clean_checks
    )


def _operation_document_ids(value: Any) -> set[str]:
    result: set[str] = set()
    if isinstance(value, dict):
        direct = value.get("document_id") or value.get("documentId")
        if direct:
            result.add(str(direct))
        many = value.get("document_ids")
        if isinstance(many, list):
            result.update(str(item) for item in many if item)
        for nested in value.values():
            result.update(_operation_document_ids(nested))
    elif isinstance(value, list):
        for nested in value:
            result.update(_operation_document_ids(nested))
    return result


def _canonical_operation_items(value: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if isinstance(value, dict):
        contents = value.get("contents")
        if isinstance(contents, list):
            for content in contents:
                if isinstance(content, dict):
                    candidate = dict(content)
                    if "event_date" in candidate and "timestamp" not in candidate:
                        candidate["timestamp"] = candidate.pop("event_date")
                    items.append(candidate)
        for nested in value.values():
            items.extend(_canonical_operation_items(nested))
    elif isinstance(value, list):
        for nested in value:
            items.extend(_canonical_operation_items(nested))
    return items


def operation_matches_outbox(
    operation: dict[str, Any],
    *,
    document_id: str,
    payload_hash: str,
    operation_id: str | None = None,
    operation_types: frozenset[str] | None = None,
) -> bool:
    """Prove that a remote operation contains exactly the local document/item."""
    payload = dict(operation.get("task_payload") or {})
    metadata = dict(operation.get("result_metadata") or {})
    if payload.get("bank_id") != "agent-sessions":
        return False
    if operation_id is not None and str(
        operation.get("operation_id") or operation.get("id") or ""
    ) != str(operation_id):
        return False
    operation_type = str(
        operation.get("operation_type") or operation.get("type") or ""
    ).lower()
    if operation_types is not None and operation_type not in operation_types:
        return False
    document_ids = _operation_document_ids({"payload": payload, "metadata": metadata})
    if document_ids != {document_id}:
        return False
    items = _canonical_operation_items(payload)
    if len(items) != 1 or items[0].get("document_id") != document_id:
        return False
    encoded = json.dumps(
        items[0], sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(encoded.encode()).hexdigest() == payload_hash


def unique_exact_batch_parent(
    matches: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return one batch parent only when every other match is its child."""
    parents = [
        value
        for value in matches
        if str(value.get("operation_type") or "").lower() == "batch_retain"
    ]
    if len(parents) != 1:
        raise InvariantError("operationless row has ambiguous remote lineage")
    parent = parents[0]
    parent_id = str(parent.get("operation_id") or "")
    operation_ids = [str(value.get("operation_id") or "") for value in matches]
    if (
        not parent_id
        or any(not value for value in operation_ids)
        or len(operation_ids) != len(set(operation_ids))
    ):
        raise InvariantError("operationless row has duplicate/empty remote lineage")
    for value in matches:
        if value is parent:
            continue
        metadata = dict(value.get("result_metadata") or {})
        if (
            str(value.get("operation_type") or "").lower() != "retain"
            or str(metadata.get("parent_operation_id") or "") != parent_id
        ):
            raise InvariantError(
                "operationless row has an unrelated exact remote child"
            )
    return parent


def stable_operationless_absence(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
    *,
    minimum_interval_seconds: float,
) -> bool:
    """Prove two stable, fully idle authoritative absence snapshots."""
    required_zero = (
        "operation_count",
        "hindsight_active",
        "provider_in_flight_before",
        "provider_in_flight_after",
    )
    if current.get("document_present") is not False or any(
        int(current.get(name, -1)) != 0 for name in required_zero
    ):
        raise InvariantError(
            "operationless absence snapshot observed document/operation activity"
        )
    if previous is None:
        return False
    if previous.get("document_present") is not False or any(
        int(previous.get(name, -1)) != 0 for name in required_zero
    ):
        raise InvariantError("prior operationless absence proof is not exact")
    return (
        float(current["checked_at"]) - float(previous["checked_at"])
        >= minimum_interval_seconds
    )


def provider_failure_kind(error: str) -> str:
    lowered = error.lower()
    if re.search(r"(?:http\s*)?(401|402)\b", lowered) or any(
        value in lowered for value in ("unauthorized", "payment required")
    ):
        return "auth"
    if "429" in lowered or "rate limit" in lowered:
        return "rate_limit"
    if "ambiguous" in lowered or any(
        value in lowered
        for value in (
            "readtimeout",
            "connecttimeout",
            "timeout",
            "timed out",
            "connecterror",
            "networkerror",
            "remoteprotocolerror",
            "connection reset",
            "connection aborted",
            "connection refused",
            "broken pipe",
            "unexpected eof",
            "end of file",
            "name resolution",
            "dns",
            "socket error",
        )
    ):
        return "ambiguous"
    if re.search(r"\b5\d\d\b", lowered):
        return "transient_5xx"
    return "terminal"


def authoritative_provider_failure_kind(
    error: str,
    budget: dict[str, Any],
    recovery_status: dict[str, Any],
) -> str:
    """Classify from the durable budget circuit before lossy operation text.

    The proxy intentionally returns a generic HTTP 502 for transport ambiguity,
    so the Hindsight operation error alone cannot distinguish a definitive 5xx
    from an ambiguous provider call.  A persisted open-circuit reason is the
    authority.  Contradictory reservation/circuit state fails closed.
    """
    operation_kind = provider_failure_kind(error)
    circuit_open = bool(budget.get("circuit_open"))
    reason = str(budget.get("circuit_reason") or budget.get("reason") or "")
    reason_kind = provider_failure_kind(reason)
    reservations = recovery_status.get("reservations") or []
    if not isinstance(reservations, list):
        raise InvariantError("budget recovery reservation state is invalid")
    if reservations and not circuit_open:
        raise InvariantError("budget has reservations without an open circuit")
    if circuit_open:
        if reason_kind == "ambiguous":
            if not reservations and not any(
                isinstance(recovery_status.get(name), dict)
                for name in (
                    "last_ambiguous_reconciliation",
                    "last_record_commit_repair",
                )
            ):
                raise InvariantError(
                    "ambiguous budget circuit lacks reservations or repair proof"
                )
            return "ambiguous"
        if reason_kind in {"auth", "rate_limit", "transient_5xx"}:
            if reservations:
                raise InvariantError(
                    "definitive provider circuit unexpectedly retains reservations"
                )
            return reason_kind
        raise InvariantError("budget circuit has an unclassified provider reason")
    if reservations:
        raise InvariantError("closed budget circuit retains reservations")
    return operation_kind


def provider_retry_after(error: str, default: float) -> float:
    match = re.search(
        r"retry[-_ ]?after(?:\s*[:=]\s*|\s+)([^\r\n;,]+(?:,[^\r\n;]+)?)",
        error,
        re.IGNORECASE,
    )
    if not match:
        return default
    raw = match.group(1).strip()
    numeric = re.fullmatch(r"\d+(?:\.\d+)?", raw)
    if numeric:
        return max(float(raw), 0.0)
    try:
        when = parsedate_to_datetime(raw)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(when.timestamp() - time.time(), 0.0)
    except (TypeError, ValueError, OverflowError):
        return default


def recovery_control_roll_sha256(
    *,
    reconcile_ambiguous: bool,
    reset_provider_auth: bool,
    reset_token: str = "",
    reconcile_manifest_sha256: str = "",
) -> str:
    """Match hindsight-node.sh's payload-free recovery roll fingerprint."""
    token_sha256 = hashlib.sha256(reset_token.encode()).hexdigest()
    value = "\n".join(
        (
            "version=1",
            f"reconcile_enabled={1 if reconcile_ambiguous else 0}",
            f"reconcile_manifest_sha256={reconcile_manifest_sha256}",
            f"auth_reset_enabled={1 if reset_provider_auth else 0}",
            f"auth_reset_token_sha256={token_sha256}",
            "",
        )
    )
    return hashlib.sha256(value.encode()).hexdigest()


def has_recoverable_incident_rows(
    *, errors: int, active: int, agent_open: bool
) -> bool:
    """Return whether held Agent rows are eligible for exact reconciliation."""
    return errors > 0 or (agent_open and active > 0)


def recovery_helper_identity_failure(cfg: Config) -> str | None:
    """Return the exact non-spending hold while a reviewed helper is staged.

    A newer local helper may be used to reconcile an already-held incident,
    but it must never become authority to release an older Agent worker.  The
    empty recovery value preserves the historical single-identity behavior.
    """
    recovery_build_id = (
        cfg.expected_recovery_outbox_build_id
        or cfg.expected_agent_outbox_build_id
    )
    if recovery_build_id != cfg.expected_agent_outbox_build_id:
        return STAGED_RECOVERY_HELPER_FAILURE
    return None


def exact_probe_reservation_recovery(
    incident: dict[str, Any],
    *,
    budget: dict[str, Any],
    budget_recovery: dict[str, Any],
) -> bool:
    """Authorize action selection for one controller-owned reservation.

    Ordinary provider activity must drain before incident mutation.  The one
    exception is a restart-safe auth probe ambiguity whose exact singleton
    reservation was already snapshotted durably.  This predicate grants only
    entry to the reconciliation routine; that routine independently waits a
    full interval and re-proves the unchanged manifest/spend before charging.
    """
    state = incident.get("provider_recovery")
    ambiguity = state.get("probe_ambiguity") if isinstance(state, dict) else None
    if not isinstance(ambiguity, dict):
        return False
    provider_in_flight = int(budget.get("in_flight") or 0)
    if provider_in_flight == 0:
        # A crash after reconciliation but before the next incident commit is
        # resumed idempotently by the ordinary drained recovery path.
        return False
    if provider_in_flight != 1:
        raise InvariantError(
            "recovery-probe ambiguity has non-singleton provider activity"
        )
    if str(ambiguity.get("stage") or "") not in {"snapshot", "reconcile_started"}:
        raise InvariantError(
            "recovery-probe reservation exists outside a reconcilable stage"
        )
    snapshot = ambiguity.get("reservation_manifest")
    if not isinstance(snapshot, dict):
        raise InvariantError("recovery-probe reservation snapshot is malformed")
    expected = snapshot.get("reservations")
    current = budget_recovery.get("reservations")
    route = str(ambiguity.get("route") or "")
    provenance = str(ambiguity.get("provenance") or "")
    request_id = str(ambiguity.get("request_id") or "")
    if (
        not isinstance(expected, list)
        or len(expected) != 1
        or route not in {"gpt_oss", "qwen"}
        or re.fullmatch(
            rf"hindsight-recovery-[0-9a-f]{{24}}-{re.escape(route)}",
            provenance,
        )
        is None
        or request_id
        != str(uuid.uuid5(RECOVERY_PROBE_NAMESPACE, provenance))
        or current != expected
        or str(budget_recovery.get("sha256") or "")
        != str(snapshot.get("sha256") or "")
        or str(budget_recovery.get("total_max_usd") or 0)
        != str(snapshot.get("total_max_usd") or 0)
        or str(expected[0].get("request_id") or "") != request_id
        or str(expected[0].get("recovery_probe_route") or "")
        != route
        or str(expected[0].get("request_provenance_sha256") or "")
        != hashlib.sha256(provenance.encode()).hexdigest()
        or not bool(budget.get("circuit_open"))
        or str(budget.get("circuit_reason") or "")
        != str(ambiguity.get("reason") or "")
    ):
        raise InvariantError(
            "recovery-probe live reservation differs from its durable snapshot"
        )
    return True


def has_probe_ambiguity(incident: dict[str, Any]) -> bool:
    state = incident.get("provider_recovery")
    return isinstance(state, dict) and isinstance(state.get("probe_ambiguity"), dict)


def provider_incident_failures(
    failures: list[str],
    *,
    errors: int,
    active: int = 0,
    agent_open: bool = False,
    budget: dict[str, Any],
    budget_recovery: dict[str, Any],
) -> list[str]:
    """Remove only exact failures needed to reconcile an already-held row.

    A definitive/ambiguous provider incident necessarily opens the budget
    circuit while the phase still says ``budget=closed``.  That one expected
    predicate is the recovery trigger, not infrastructure degradation.  Every
    other failure (reservation mismatch, health/config drift, service loss,
    etc.) is preserved and therefore continues to dominate row recovery.  A
    staged reviewed recovery helper is also admitted only while an exact
    failed/blocked or held-active row exists.  Its identity mismatch remains
    a hard release hold as soon as reconciliation drains those rows.
    """
    expected = "budget circuit is not healthy/closed"
    # A provider can fail a remote parent/child lineage before the Agent
    # worker refreshes its still-submitted row.  The guard opens Agent first,
    # so that held submitted row is every bit as much an incident row as an
    # already-refreshed failed/blocked row.  Do not extend this allowance to a
    # closed Agent: an ordinary running wave must never suppress a budget
    # circuit failure while it can still submit work.
    recoverable_rows = has_recoverable_incident_rows(
        errors=errors, active=active, agent_open=agent_open
    )
    if not recoverable_rows:
        return failures
    result = list(failures)
    if result.count(STAGED_RECOVERY_HELPER_FAILURE) == 1:
        result = [
            failure
            for failure in result
            if failure != STAGED_RECOVERY_HELPER_FAILURE
        ]
    if result.count(expected) != 1:
        return result
    try:
        kind = authoritative_provider_failure_kind("", budget, budget_recovery)
    except InvariantError:
        return result
    if kind not in {"auth", "rate_limit", "transient_5xx", "ambiguous"}:
        return result
    return [failure for failure in result if failure != expected]


def authorized_hindsight_incident_failures(
    failures: list[str],
    *,
    accepted_failures: list[dict[str, Any]],
    observed_failures: list[dict[str, Any]],
    accepted_operations: list[dict[str, Any]],
    observed_operations: list[dict[str, Any]],
    authorized_operation_ids: set[str],
) -> list[str]:
    """Remove only an exact append-only failure delta owned by one incident.

    The quick audit sees the aggregate Hindsight failure count before a row
    retry can append the new exact records to the checkpoint.  That expected
    drift may enter recovery only when both the full failed-operation manifest
    and terminal operation inventory prove that every addition belongs to the
    already authorized parent/child lineage.  Unrelated or reordered records
    remain a hard hold.
    """
    expected = "Hindsight failed-operation count drift"
    if failures.count(expected) != 1 or not authorized_operation_ids:
        return failures
    old_by_id = {
        str(value.get("operation_id") or ""): value for value in accepted_failures
    }
    new_by_id = {
        str(value.get("operation_id") or ""): value for value in observed_failures
    }
    if (
        "" in old_by_id
        or "" in new_by_id
        or len(old_by_id) != len(accepted_failures)
        or len(new_by_id) != len(observed_failures)
        or any(new_by_id.get(key) != value for key, value in old_by_id.items())
    ):
        return failures
    additions = [
        value
        for value in observed_failures
        if str(value.get("operation_id") or "") not in old_by_id
    ]
    addition_ids = {str(value.get("operation_id") or "") for value in additions}
    if not addition_ids or not addition_ids.issubset(authorized_operation_ids):
        return failures
    if observed_operations[: len(accepted_operations)] != accepted_operations:
        return failures
    terminal_additions = observed_operations[len(accepted_operations) :]
    terminal_failure_ids = {
        str(value.get("operation_id") or "")
        for value in terminal_additions
        if str(value.get("status") or "") in {"failed", "cancelled"}
    }
    if not addition_ids.issubset(terminal_failure_ids):
        return failures
    if not terminal_failure_ids.issubset(authorized_operation_ids):
        return failures
    return [failure for failure in failures if failure != expected]


def terminal_operation_inventory_failure(
    *,
    phase_name: str,
    accepted_operations: list[dict[str, Any]],
    observed_operations: list[dict[str, Any]],
    inventory_failures: list[str] | None = None,
) -> str | None:
    """Classify terminal-operation drift without interrupting healthy waves.

    A completed parent/child pair is expected to become terminal while the
    controller is running.  The checkpoint remains the durable acceptance
    boundary, so a running audit may *observe* only an exact completed suffix;
    it does not append that suffix.  Every other phase continues to require an
    exact checkpoint match, and any inventory, ordering, mutation, or terminal
    failure contradiction remains fail closed.
    """
    if inventory_failures:
        return "terminal operation inventory provenance drift"
    if observed_operations == accepted_operations:
        return None
    if (
        len(observed_operations) < len(accepted_operations)
        or observed_operations[: len(accepted_operations)] != accepted_operations
    ):
        return "terminal operation inventory provenance drift"
    additions = observed_operations[len(accepted_operations) :]
    if any(
        str(value.get("status") or "").lower() in {"failed", "cancelled"}
        for value in additions
    ):
        return "terminal operation inventory has failed/cancelled append-only additions"
    if (
        phase_name in {"running", "release_verifying"}
        and additions
        and all(
            str(value.get("status") or "").lower() == "completed" for value in additions
        )
    ):
        return None
    return "terminal operation inventory has unaccepted append-only additions"


def controller_action(
    *,
    phase_name: str,
    failures: list[str],
    agent_open: bool,
    attempted: int,
    cap: int,
    active: int,
    errors: int,
    hindsight_active: int,
    provider_in_flight: int,
    probe_ambiguity_recovery: bool = False,
) -> str:
    """Return a deterministic, non-mutating controller action."""
    if attempted > cap or active > 2 or provider_in_flight > 6:
        return "hard_hold"
    # Contradictions in accepted data/provenance are never incident-recovery
    # inputs.  Re-reading cannot make an append-only manifest contradiction
    # safe, and a row mutation would destroy the evidence needed to diagnose
    # it.  These remain non-spending holds until an operator changes the
    # authoritative state under a new reviewed rollout.
    hard_markers = (
        "projected full batch",
        "exceeds hard budget",
        "duplicate",
        "provenance",
        "hash mismatch",
        "hash drift",
        "lineage mismatch",
        "lineage drift",
        "source/context mismatch",
        "item/document mismatch",
        "outside approved",
        "outside the approved",
        "missing document id",
        "hindsight failed-operation count drift",
        "failure ledger drift",
        "failure manifest drift",
        "history drift",
        "history manifest drift",
        "remote content mismatch",
        "remote content hash mismatch",
        "remote context mismatch",
        "remote event-date mismatch",
        "remote tag mismatch",
        "invalid agent state provenance",
        "pocket count baseline drift",
    )
    if any(
        any(marker in failure.lower() for marker in hard_markers)
        for failure in failures
    ):
        return "hard_hold"
    # Infrastructure/configuration loss dominates row recovery.  The only
    # permitted controller mutation in this branch is opening/confirming the
    # Agent and Pocket circuits; thereafter it retries health and authoritative
    # reads.  In particular an Agent error row observed alongside one of these
    # failures must not be refreshed/retried until the infrastructure proof is
    # clean again.
    infrastructure_markers = (
        "heartbeat",
        ".service inactive",
        "tunnel",
        "health degraded",
        "router",
        "patroni",
        "timeline consistency",
        "runtime",
        "persistent cap",
        "persistent max_in_flight",
        "api health proof",
        "vm health proof",
        "image digest",
        "compose",
        "descriptor",
        "proof file",
        "proof is stale",
        "port set",
        "approved batch total drift",
        "checkpoint stage configuration drift",
        "circuit is closed",
        "circuit remained closed",
        "pocket has active/error work",
        "persistent max_in_flight",
        "budget health/reservation",
        "budget circuit",
        "budget provider egress",
        "budget hindsight phase",
        "budget hindsight model",
        "budget provider effective limit",
        "budget hindsight backfill limit",
        "budget hindsight llm concurrency",
        "budget provider max-in-flight ceiling",
    )
    if any(
        any(marker in failure.lower() for marker in infrastructure_markers)
        for failure in failures
    ):
        return "retry_audit"
    if phase_name in CONTROLLER_TERMINAL_PHASES:
        if (
            failures
            or not agent_open
            or active
            or errors
            or hindsight_active
            or provider_in_flight
        ):
            return "hard_hold"
        return "finalized"
    # A closed, healthy running wave is already being supervised by the
    # worker/guard and must not be interrupted merely because its remote work
    # is visible.  Every held/recovery phase, however, waits for complete
    # Hindsight/provider drain before any row refresh, adoption, or retry.
    if phase_name == "running" and not agent_open and not errors and not failures:
        return "monitor"
    # A controller-owned probe reservation is reconciled by a dedicated path
    # that cannot inspect or mutate Agent rows.  Never route this exception to
    # generic incident recovery: a sibling completed row could otherwise be
    # refreshed while the provider reservation is still live.
    if probe_ambiguity_recovery:
        if not agent_open or hindsight_active or provider_in_flight not in {0, 1}:
            return "hard_hold"
        return "reconcile_probe_reservation"
    if hindsight_active or provider_in_flight:
        return "wait"
    if phase_name == "finalizing":
        return "finalize"
    if phase_name == "release_verifying":
        # The first claimed wave can itself be the incident.  Keep the durable
        # release baseline, recover exact terminal rows only after all remote
        # and provider activity drained, and only then resume the idempotent
        # close/+N proof.
        if errors or (active and agent_open):
            return "recover"
        return "retry_audit" if failures else "resume_release"
    if errors:
        return "recover"
    if active and agent_open:
        return "recover"
    if failures:
        return "retry_audit"
    if phase_name == "running" and not agent_open:
        return "monitor"
    if active:
        return "wait"
    if phase_name == "cap_hold":
        if attempted < cap:
            return "resume_stage"
        return "settle"
    if phase_name == "settled" and cap == attempted:
        return "finalize"
    if phase_name == "ready":
        return "stabilize"
    if phase_name == "running" and agent_open and attempted < cap:
        return "prepare_release"
    if phase_name == "running":
        return "monitor"
    return "hold"


def compute_projection(spent: Any, succeeded: int, cfg: Config) -> Decimal:
    remaining = max(cfg.approved_total - succeeded, 0)
    return Decimal(str(spent)) + Decimal(remaining) * cfg.conservative_remaining_cost


def exact_final_budget_hold(budget: dict[str, Any]) -> bool:
    """Match only the irreversible, drained final maintenance hold."""
    hold = budget.get("final_maintenance_hold")
    entered_at = hold.get("entered_at") if isinstance(hold, dict) else None
    try:
        entered = datetime.fromisoformat(str(entered_at).replace("Z", "+00:00"))
    except ValueError:
        return False
    in_flight = budget.get("in_flight")
    return (
        budget.get("status") == "circuit_open"
        and budget.get("circuit_open") is True
        and budget.get("circuit_reason") == FINAL_BUDGET_HOLD_REASON
        and type(in_flight) is int
        and in_flight == 0
        and isinstance(hold, dict)
        and hold.get("reason") == FINAL_BUDGET_HOLD_REASON
        and isinstance(entered_at, str)
        and bool(entered_at)
        and entered.tzinfo is not None
        and entered.utcoffset() is not None
    )


def is_non_spending_completed_refresh(attempt: dict[str, Any]) -> bool:
    """Accept only a pure submitted-to-succeeded completed refresh attempt."""
    return (
        str(attempt.get("kind") or "") == "completed_refresh"
        and str(attempt.get("remote_status") or "").lower() == "completed"
        and attempt.get("remote_document_present") is True
        and all(
            int((attempt.get("expected_history_delta") or {}).get(name, -1)) == 0
            for name in ("retry", "claim", "adoption")
        )
        and not any(
            attempt.get(name)
            for name in (
                "authorized_failure_operation_ids",
                "authorized_fugu_failures",
                "authorized_fugu_request_ids",
                "authorized_provider_failures",
                "authorized_provider_failure_ids",
                "provider_recovery_proof",
                "provider_statuses",
                "allowed_http_statuses",
            )
        )
        and attempt.get("allow_null_http_status") is not True
    )


def completed_refresh_archive_proof(
    authorization: dict[str, Any], checkpoint: dict[str, Any]
) -> dict[str, Any] | None:
    """Bind one closed incident to exact, non-spending completed refreshes.

    This proof is deliberately narrower than the general recovery manifest.
    A terminal retry, operation adoption, provider recovery, or operationless
    requeue can never qualify for the abbreviated post-incident release gate.
    """
    lineages = list(authorization.get("row_lineages") or [])
    if not lineages or any(
        authorization.get(name)
        for name in (
            "terminal_pending_child_operation_ids",
            "authorized_failure_operation_ids",
            "authorized_fugu_failures",
            "authorized_fugu_request_ids",
            "authorized_provider_failures",
            "authorized_provider_failure_ids",
            "provider_statuses",
        )
    ):
        return None
    recovered = list(checkpoint.get("recovered_attempts") or [])
    attempts: list[dict[str, Any]] = []
    for lineage in lineages:
        document_id = str(lineage.get("document_id") or "")
        operation_id = str(lineage.get("operation_id") or "")
        payload_hash = str(lineage.get("payload_hash") or "")
        if not document_id or not operation_id or not payload_hash:
            return None
        matches = [
            value
            for value in recovered
            if is_non_spending_completed_refresh(value)
            and str(value.get("document_id") or "") == document_id
            and str(value.get("operation_id") or "") == operation_id
            and str(value.get("payload_hash") or "") == payload_hash
        ]
        if len(matches) != 1:
            return None
        attempts.append(matches[0])
    keys = sorted(str(value.get("key") or "") for value in attempts)
    if any(not key for key in keys) or len(keys) != len(set(keys)):
        return None
    lineage_descriptor = {
        "lineages": sorted(
            (
                {
                    "document_id": str(value["document_id"]),
                    "operation_id": str(value["operation_id"]),
                    "payload_hash": str(value["payload_hash"]),
                }
                for value in lineages
            ),
            key=lambda value: (
                value["document_id"],
                value["operation_id"],
                value["payload_hash"],
            ),
        )
    }
    return {
        "version": 1,
        "count": len(keys),
        "attempt_keys": keys,
        "lineage_sha256": _json_digest(lineage_descriptor),
        "checkpoint_revision": int(checkpoint["revision"]),
        "checkpoint_sha256": _json_digest(checkpoint),
    }


def post_incident_release_evidence(
    phase: dict[str, Any],
    incident: dict[str, Any],
    checkpoint: dict[str, Any],
) -> dict[str, Any] | None:
    """Return exact restart-stable evidence for a completed-refresh release.

    New incident archives carry a checkpoint-bound proof.  The legacy branch
    exists only so a controller upgrade can safely finish an incident that was
    already archived by the immediately preceding controller build.
    """
    if not bool(phase.get("initial_wave_verified")):
        return None
    if any(
        incident.get(name)
        for name in (
            "incident_authorization",
            "pending_acceptance",
            "provider_recovery",
        )
    ) or any((incident.get("absence_proofs") or {}).values()):
        return None
    archive = list(incident.get("authorization_archive") or [])
    if not archive:
        return None
    latest = dict(archive[-1])
    authorization_sha256 = str(latest.get("authorization_sha256") or "")
    row_sha256 = str(latest.get("row_sha256") or "")
    archive_closed_at = float(latest.get("closed_at") or 0)
    if (
        not authorization_sha256
        or not row_sha256
        or archive_closed_at <= float(phase.get("released_at") or 0)
        or str(latest.get("reason") or "") != "all authorized incident rows reconciled"
        or str(phase.get("last_post_incident_release_authorization_sha256") or "")
        == authorization_sha256
    ):
        return None

    proof = latest.get("completed_refresh_proof")
    if isinstance(proof, dict):
        if (
            int(proof.get("version", 0)) != 1
            or int(proof.get("count", 0)) < 1
            or int(proof.get("checkpoint_revision", -1))
            != int(checkpoint.get("revision", -2))
            or str(proof.get("checkpoint_sha256") or "") != _json_digest(checkpoint)
        ):
            return None
        attempt_keys = sorted(str(value) for value in proof.get("attempt_keys") or [])
        if len(attempt_keys) != int(proof["count"]) or len(attempt_keys) != len(
            set(attempt_keys)
        ):
            return None
        recovered = {
            str(value.get("key") or ""): value
            for value in checkpoint.get("recovered_attempts") or []
        }
        if any(
            key not in recovered
            or not is_non_spending_completed_refresh(recovered[key])
            for key in attempt_keys
        ):
            return None
        lineage_sha256 = str(proof.get("lineage_sha256") or "")
    else:
        # Compatibility for the incident that may already have completed at
        # controller-upgrade time.  Serial controller recovery means the
        # accepted-at interval between adjacent archives is an exact episode.
        result = incident.get("last_result")
        if (
            not isinstance(result, dict)
            or str(result.get("status") or "") != "reconciled"
        ):
            return None
        expected_count = int(result.get("count", 0))
        closed_at = float(latest.get("closed_at") or 0)
        prior_closed_at = (
            float(archive[-2].get("closed_at") or 0) if len(archive) > 1 else 0.0
        )
        attempts = [
            value
            for value in checkpoint.get("recovered_attempts") or []
            if prior_closed_at < float(value.get("accepted_at") or 0) <= closed_at
        ]
        if (
            expected_count < 1
            or len(attempts) != expected_count
            or any(not is_non_spending_completed_refresh(value) for value in attempts)
        ):
            return None
        attempt_keys = sorted(str(value.get("key") or "") for value in attempts)
        if any(not value for value in attempt_keys) or len(attempt_keys) != len(
            set(attempt_keys)
        ):
            return None
        lineage_sha256 = "legacy-archive-window"

    return {
        "authorization_sha256": authorization_sha256,
        "row_sha256": row_sha256,
        "archive_closed_at": archive_closed_at,
        "attempt_keys": attempt_keys,
        "lineage_sha256": lineage_sha256,
        "checkpoint_revision": int(checkpoint["revision"]),
        "checkpoint_sha256": _json_digest(checkpoint),
    }


def canonical_reservation_total(reservations: Iterable[dict[str, Any]]) -> float:
    """Reproduce the proxy's CPython 3.11 ordered float accumulation.

    CPython 3.12+ changed ``sum()`` for floats to a compensated algorithm.
    The pinned proxy runtime is 3.11, so an explicit left fold is required to
    compare its serialized aggregate exactly without relaxing the invariant.
    """
    total = 0.0
    for reservation in reservations:
        raw = reservation.get("cost_usd")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise InvariantError("budget reservation maximum cost is malformed")
        cost = float(raw)
        if not math.isfinite(cost) or cost <= 0:
            raise InvariantError("budget reservation maximum cost is malformed")
        total = float(total + cost)
    return total


def stage_action(attempted: int, phase_name: str, cap: int) -> str:
    if attempted > cap:
        return "violation"
    if phase_name == "running" and attempted == cap:
        return "open"
    return "continue"


def precap_resume_failure(
    *,
    phase_name: str,
    agent_open: bool,
    attempted: int,
    succeeded: int,
    submitting: int,
    submitted: int,
    errors: int,
    hindsight_active: int,
    provider_in_flight: int,
    remote_documents: int,
    max_agent_active: int,
    cap: int,
) -> str | None:
    """Return why an open pre-cap hold cannot be reconciled for release."""
    if phase_name != "running":
        return "pre-cap resume requires running phase"
    if not agent_open:
        return "pre-cap resume requires Agent circuit open"
    if attempted >= cap:
        return "pre-cap resume cannot run at or above stage cap"
    if submitting or not 1 <= submitted <= max_agent_active or errors:
        return (
            "pre-cap resume requires one to the configured maximum submitted "
            "Agent rows and no other active/error row"
        )
    if attempted != succeeded + submitted:
        return "pre-cap attempted count is not exactly succeeded plus submitted"
    if hindsight_active:
        return "pre-cap resume requires zero Hindsight pending/processing"
    if provider_in_flight:
        return "pre-cap resume requires zero provider in-flight"
    if remote_documents != succeeded + submitted:
        return "pre-cap remote-document count is not exactly succeeded plus submitted"
    return None


def watchdog_failure(
    *,
    armed: bool,
    heartbeat_age: float | None,
    tunnel_ports_ready: bool,
    guard_active: bool,
    tunnel_active: bool,
    max_age: float,
    controller_required: bool = False,
    controller_heartbeat_age: float | None = None,
    controller_active: bool = True,
) -> str | None:
    if not armed:
        return None
    if heartbeat_age is None or heartbeat_age > max_age:
        return "guard heartbeat missing or stale"
    if not tunnel_ports_ready:
        return "shared tunnel path unavailable"
    if not guard_active:
        return "guard service inactive"
    if not tunnel_active:
        return "tunnel service inactive"
    if controller_required and (
        controller_heartbeat_age is None or controller_heartbeat_age > max_age
    ):
        return "controller heartbeat missing or stale"
    if controller_required and not controller_active:
        return "controller service inactive"
    return None


def heartbeat_failure(path: Path, label: str, max_age: float) -> str | None:
    try:
        value = json.loads(path.read_text())
        age = time.time() - float(value["at"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return f"{label} heartbeat missing or invalid"
    if age < -5 or age > max_age:
        return f"{label} heartbeat missing or stale"
    if value.get("healthy") is not True:
        return f"{label} heartbeat reports unhealthy"
    return None


HEARTBEAT_LABELS = frozenset({"guard", "watchdog", "controller"})
AUDIT_HEARTBEAT_LABELS = frozenset({"guard", "controller"})
_BOUNDED_HEARTBEAT_LEASE_LOCK = threading.Lock()
_BOUNDED_HEARTBEAT_LEASES: dict[Path, "_BoundedAuditHeartbeat"] = {}


def _excluded_heartbeat_labels(labels: Iterable[str]) -> frozenset[str]:
    excluded = frozenset(str(label) for label in labels)
    unknown = excluded - HEARTBEAT_LABELS
    if unknown:
        raise ValueError(
            "unknown excluded heartbeat label(s): " + ", ".join(sorted(unknown))
        )
    return excluded


def heartbeat_failures(
    heartbeats: Iterable[tuple[Path, str]],
    max_age: float,
    *,
    excluded_heartbeat_labels: Iterable[str] = (),
) -> list[str]:
    """Validate peer heartbeats while allowing a service to omit only itself."""
    excluded = _excluded_heartbeat_labels(excluded_heartbeat_labels)
    failures: list[str] = []
    for path, label in heartbeats:
        if label in excluded:
            continue
        failure = heartbeat_failure(path, label, max_age)
        if failure:
            failures.append(failure)
    return failures


class _BoundedAuditHeartbeat:
    """Pulse one service heartbeat while a synchronous audit is healthy.

    The worker deliberately stops at a fixed monotonic deadline and publishes
    an unhealthy heartbeat.  A wedged audit therefore cannot manufacture
    liveness forever, while a slow but bounded audit does not look like a dead
    guard/controller to its peer.
    """

    def __init__(
        self,
        path: Path,
        label: str,
        *,
        deadline_seconds: float,
        pulse_seconds: float,
        fields: dict[str, Any] | None = None,
    ) -> None:
        if label not in AUDIT_HEARTBEAT_LABELS:
            raise ValueError(f"unsupported audit heartbeat label: {label}")
        if (
            not math.isfinite(deadline_seconds)
            or not math.isfinite(pulse_seconds)
            or deadline_seconds <= 0
            or pulse_seconds <= 0
            or pulse_seconds >= deadline_seconds
        ):
            raise ValueError("audit heartbeat timing is invalid")
        self.path = path
        self.label = label
        self.deadline_seconds = deadline_seconds
        self.pulse_seconds = pulse_seconds
        self.fields = dict(fields or {})
        self._stop = threading.Event()
        self._expired = threading.Event()
        self._failure_reason: str | None = None
        self._thread: threading.Thread | None = None
        self._deadline = 0.0

    def _deadline_status(self) -> str:
        subject = str(self.fields.get("status") or "full_audit")
        return f"{subject}_deadline_exceeded"

    def _deadline_reason(self) -> str:
        subject = str(self.fields.get("status") or "full_audit").replace("_", " ")
        return (
            f"{self.label} {subject} exceeded bounded "
            f"{self.deadline_seconds:g}s deadline"
        )

    def _payload(self, *, healthy: bool, reason: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "at": time.time(),
            "healthy": healthy,
            "pid": os.getpid(),
            **self.fields,
        }
        if reason is not None:
            payload.update(
                {
                    "reason": reason,
                    "status": self._deadline_status(),
                }
            )
        return payload

    def _fail(self, reason: str) -> None:
        self._failure_reason = reason
        self._expired.set()
        with contextlib.suppress(Exception):
            _atomic_json(self.path, self._payload(healthy=False, reason=reason))

    def _run(self) -> None:
        while True:
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                self._fail(self._deadline_reason())
                return
            if self._stop.wait(min(self.pulse_seconds, remaining)):
                return
            if time.monotonic() >= self._deadline:
                self._fail(self._deadline_reason())
                return
            try:
                _atomic_json(self.path, self._payload(healthy=True))
            except Exception as exc:
                self._fail(
                    f"{self.label} full audit heartbeat write failed: "
                    f"{type(exc).__name__}: {exc}"
                )
                return

    def __enter__(self) -> "_BoundedAuditHeartbeat":
        self._deadline = time.monotonic() + self.deadline_seconds
        with _BOUNDED_HEARTBEAT_LEASE_LOCK:
            if self.path in _BOUNDED_HEARTBEAT_LEASES:
                raise InvariantError(
                    f"{self.label} bounded heartbeat lease is already active"
                )
            _BOUNDED_HEARTBEAT_LEASES[self.path] = self
        try:
            _atomic_json(self.path, self._payload(healthy=True))
            self._thread = threading.Thread(
                target=self._run,
                name=f"hindsight-{self.label}-audit-heartbeat",
                daemon=True,
            )
            self._thread.start()
        except BaseException:
            with _BOUNDED_HEARTBEAT_LEASE_LOCK:
                if _BOUNDED_HEARTBEAT_LEASES.get(self.path) is self:
                    _BOUNDED_HEARTBEAT_LEASES.pop(self.path, None)
            raise
        return self

    def healthy_write_failure(self) -> str | None:
        """Reject an inner healthy write after this lease has expired."""
        if not self._expired.is_set() and time.monotonic() < self._deadline:
            return None
        # Always re-publish the unhealthy heartbeat.  The lease worker may
        # already have expired while an inner healthy atomic write was blocked;
        # in that ordering the delayed healthy replace lands after the worker's
        # first failure write and must be overwritten again here.
        reason = self._failure_reason or self._deadline_reason()
        self._fail(reason)
        return reason

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(self.pulse_seconds * 2, 1.0))
            if self._thread.is_alive():
                self._fail(f"{self.label} full audit heartbeat worker did not stop")
        if not self._expired.is_set() and time.monotonic() >= self._deadline:
            self._fail(self._deadline_reason())
        expired = self._expired.is_set()
        failure_reason = self._failure_reason
        with _BOUNDED_HEARTBEAT_LEASE_LOCK:
            if _BOUNDED_HEARTBEAT_LEASES.get(self.path) is self:
                _BOUNDED_HEARTBEAT_LEASES.pop(self.path, None)
        if exc_type is None and expired:
            raise InvariantError(
                failure_reason or f"{self.label} bounded heartbeat failed closed"
            )
        return False


def _bounded_heartbeat_write_failure(path: Path) -> str | None:
    with _BOUNDED_HEARTBEAT_LEASE_LOCK:
        lease = _BOUNDED_HEARTBEAT_LEASES.get(path)
        return lease.healthy_write_failure() if lease is not None else None


class _RecoveryHindsightClient:
    """Minimal sealed read-only client used by reviewed outbox refresh code."""

    def __init__(self, base_url: str, token: str, bank_id: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.bank_id = bank_id

    def _get(
        self, endpoint: str, params: dict[str, str] | None = None
    ) -> dict[str, Any]:
        root = f"{self.base_url}/v1/default/banks/{self.bank_id}/{endpoint}"
        url = root + ("?" + urllib.parse.urlencode(params) if params else "")
        request = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self.token}"}
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status != 200:
                raise InvariantError(f"{url} returned HTTP {response.status}")
            value = json.loads(response.read())
        if not isinstance(value, dict):
            raise InvariantError("Hindsight operation read returned a non-object")
        return value

    def operation_status(
        self, operation_id: str, *, include_payload: bool = False
    ) -> dict[str, Any]:
        return self._get(
            f"operations/{operation_id}",
            {"include_payload": "true"} if include_payload else None,
        )

    def document_exists(self, document_id: str) -> bool:
        try:
            self._get(f"documents/{urllib.parse.quote(document_id, safe='')}")
            return True
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return False
            raise


def direct_redpill_out_of_credit(value: dict[str, Any]) -> bool:
    """Return whether one direct Redpill result proves exhausted credit.

    HTTP 429 is intentionally excluded even when an upstream happens to put
    credit-like wording in the response. It is ordinary retry pressure for
    this run. Other statuses require either the unambiguous HTTP 402 signal or
    one of the narrow balance/credit phrases approved for the continuous run.
    """

    if str(value.get("account_id") or "").lower() != "redpill":
        return False
    try:
        status = int(_canonical_http_status(value.get("http_status")))
    except (TypeError, ValueError):
        status = 0
    if status == 429:
        return False
    if status == 402:
        return True
    message = " ".join(str(value.get("error_message") or "").lower().split())
    if not message:
        return False
    patterns = (
        r"\binsufficient (?:credit|credits|balance|funds)\b",
        r"\b(?:out of|no remaining) (?:credit|credits)\b",
        r"\b(?:credit|credits) (?:are |is )?exhausted\b",
        r"\bexhausted (?:credit|credits)\b",
        r"\bpayment required\b",
    )
    return any(re.search(pattern, message) is not None for pattern in patterns)


def _spotcheck_static_issues(
    snap: dict[str, Any], cfg: Config
) -> dict[str, str]:
    """Return stable-code aggregate contradictions within one raw snapshot."""

    issues: dict[str, str] = {}
    agent = dict(snap["agent"])
    hindsight = dict(snap["hindsight"])
    budget = dict(snap["budget"])
    total = int(agent["total"])
    distinct = int(agent["distinct_documents"])
    states = set(str(value) for value in dict(agent["counts"]))
    if total != cfg.approved_total:
        issues["agent_total"] = f"Agent total {total} != {cfg.approved_total}"
    if distinct != cfg.approved_total:
        issues["agent_distinct"] = (
            f"Agent distinct approved IDs {distinct} != {cfg.approved_total}"
        )
    state_total = sum(int(value) for value in dict(agent["counts"]).values())
    if state_total != total:
        issues["agent_state_sum"] = (
            f"Agent state counts sum to {state_total}, not total {total}"
        )
    unknown_states = sorted(states - OUTBOX_STATES)
    if unknown_states:
        issues["agent_states"] = f"unknown Agent states: {unknown_states}"

    remote_total = int(hindsight["remote_documents"])
    remote_distinct = int(hindsight["remote_distinct_documents"])
    if remote_total != remote_distinct:
        issues["remote_duplicates"] = (
            f"Hindsight document total/distinct {remote_total}/{remote_distinct}"
        )
    approved = set(str(value) for value in agent["document_ids"])
    remote = set(str(value) for value in hindsight["document_ids"])
    outside = sorted(remote - approved)
    if outside:
        issues["remote_unapproved"] = (
            f"Hindsight has {len(outside)} unapproved document IDs"
        )

    succeeded = int(agent["succeeded"])
    ever_attempted = int(agent["ever_attempted"])
    if not succeeded <= remote_distinct <= ever_attempted <= cfg.approved_total:
        issues["aggregate_order"] = (
            "aggregate order violated: "
            f"succeeded={succeeded} remote={remote_distinct} "
            f"ever_attempted={ever_attempted} cap={cfg.approved_total}"
        )
    active = int(agent["active"])
    if active > cfg.max_agent_active:
        issues["agent_active"] = (
            f"Agent active {active} > {cfg.max_agent_active}"
        )
    provider_in_flight = int(budget["provider_in_flight"])
    if provider_in_flight > cfg.max_provider_in_flight:
        issues["provider_in_flight"] = (
            f"provider in-flight {provider_in_flight} > "
            f"{cfg.max_provider_in_flight}"
        )
    return issues


def _spotcheck_transition_issues(
    previous: dict[str, Any], current: dict[str, Any]
) -> dict[str, str]:
    """Return stable-code contradictions relative to an accepted snapshot."""

    issues: dict[str, str] = {}
    previous_agent = dict(previous["agent"])
    current_agent = dict(current["agent"])
    previous_hindsight = dict(previous["hindsight"])
    current_hindsight = dict(current["hindsight"])
    previous_budget = dict(previous["budget"])
    current_budget = dict(current["budget"])

    if set(previous_agent["document_ids"]) != set(current_agent["document_ids"]):
        issues["agent_document_set"] = "Agent approved document ID set changed"
    if not set(previous_hindsight["document_ids"]).issubset(
        set(current_hindsight["document_ids"])
    ):
        issues["remote_document_regression"] = (
            "Hindsight document set lost a previously observed document"
        )

    monotonic = (
        ("succeeded", previous_agent, current_agent, "succeeded"),
        ("ever_attempted", previous_agent, current_agent, "ever_attempted"),
        (
            "remote_documents",
            previous_hindsight,
            current_hindsight,
            "remote_distinct_documents",
        ),
        ("operations", previous_hindsight, current_hindsight, "operations"),
        ("fugu_rows", previous["fugu"], current["fugu"], "rows"),
        (
            "provider_calls",
            previous_budget,
            current_budget,
            "provider_calls",
        ),
        (
            "provider_failures",
            previous_budget,
            current_budget,
            "provider_failures",
        ),
    )
    for code, before_group, after_group, key in monotonic:
        before = int(before_group[key])
        after = int(after_group[key])
        if after < before:
            issues[f"{code}_regression"] = f"{code} regressed {before} -> {after}"

    for name in ("retry", "claim", "adoption"):
        before = int(previous["histories"][name])
        after = int(current["histories"][name])
        if after < before:
            issues[f"history_{name}_regression"] = (
                f"{name} history regressed {before} -> {after}"
            )
        before_max = int(previous["history_max_ids"][name])
        after_max = int(current["history_max_ids"][name])
        if after_max < before_max:
            issues[f"history_{name}_id_regression"] = (
                f"{name} history max ID regressed {before_max} -> {after_max}"
            )

    for key in ("provider_spent_usd", "backfill_spent_usd"):
        before = float(previous_budget[key])
        after = float(current_budget[key])
        if after < before - 1e-9:
            issues[f"{key}_regression"] = f"{key} regressed {before} -> {after}"

    queued_increase = int(current_agent["queued"]) - int(previous_agent["queued"])
    retry_increase = int(current["histories"]["retry"]) - int(
        previous["histories"]["retry"]
    )
    claim_increase = int(current["histories"]["claim"]) - int(
        previous["histories"]["claim"]
    )
    if queued_increase > max(retry_increase, 0) + max(claim_increase, 0):
        issues["unexplained_queue_increase"] = (
            f"queued increased by {queued_increase} but retry/claim histories "
            f"increased by only {retry_increase}/{claim_increase}"
        )
    return issues


def _confirmed_spotcheck_issues(
    first: dict[str, Any],
    second: dict[str, Any],
    previous: dict[str, Any] | None,
    cfg: Config,
) -> tuple[dict[str, str], dict[str, str]]:
    """Return confirmed and one-read-only issues for a snapshot pair."""

    first_issues = _spotcheck_static_issues(first, cfg)
    second_issues = _spotcheck_static_issues(second, cfg)
    if previous is not None:
        first_issues.update(_spotcheck_transition_issues(previous, first))
        second_issues.update(_spotcheck_transition_issues(previous, second))

    def ooc_ids(value: dict[str, Any]) -> set[str]:
        return {
            str(item)
            for item in (
                list(value["fugu"]["redpill_ooc_request_ids"])
                + list(value["budget"]["redpill_ooc_request_ids"])
            )
        }

    baseline_ooc = ooc_ids(previous) if previous is not None else set()
    # The first invocation seeds the historical Redpill ledger. There is no
    # prior observation from which to prove that an event is new.
    if previous is not None:
        first_new_ooc = ooc_ids(first) - baseline_ooc
        second_new_ooc = ooc_ids(second) - baseline_ooc
        confirmed_ooc = sorted(first_new_ooc & second_new_ooc)
        if confirmed_ooc:
            detail = f"new direct Redpill out-of-credit requests: {confirmed_ooc}"
            first_issues["redpill_out_of_credit"] = detail
            second_issues["redpill_out_of_credit"] = detail
        elif first_new_ooc or second_new_ooc:
            observed_ooc = sorted(first_new_ooc | second_new_ooc)
            detail = (
                "direct Redpill out-of-credit observed once: "
                f"{observed_ooc}"
            )
            if first_new_ooc:
                first_issues["redpill_out_of_credit"] = detail
            if second_new_ooc:
                second_issues["redpill_out_of_credit"] = detail

    confirmed_codes = set(first_issues) & set(second_issues)
    confirmed = {code: second_issues[code] for code in sorted(confirmed_codes)}
    observed = {
        code: detail
        for code, detail in sorted({**first_issues, **second_issues}.items())
        if code not in confirmed_codes
    }
    # A regression visible only between the two raw reads is not enough to
    # pause. Retain it as an observation and refuse to advance the durable
    # baseline, so the next ten-minute run can confirm or dismiss it.
    for code, detail in _spotcheck_transition_issues(first, second).items():
        if code not in confirmed:
            observed.setdefault(code, detail)
    return confirmed, observed


class Recovery:
    def __init__(self, cfg: Config, *, controller_owned: bool = False):
        self.cfg = cfg
        self.controller_owned = controller_owned
        self.agent_env = _env_file(Path.home() / ".attestmesh/agent-session-mcp.env")
        self.pocket_env = _env_file(Path.home() / ".attestmesh/pocket-mcp.env")
        self.agent_runtime = _env_file(
            Path.home() / ".attestmesh/agent-session-hindsight.env"
        )
        self.hindsight_state = _env_file(
            cfg.root / "deploy/logs/hindsight-node-hindsight-node.state"
        )
        self.fugu_env = _env_file(Path.home() / ".attestmesh/fugu-router.env")
        self.production_env = _env_file(
            Path.home() / ".attestmesh/hindsight-production.env"
        )

    def touch_controller(self, status: str, **fields: Any) -> None:
        if not getattr(self, "controller_owned", False):
            return
        lease_failure = _bounded_heartbeat_write_failure(self.cfg.controller_file)
        if lease_failure is not None:
            raise InvariantError(lease_failure)
        _atomic_json(
            self.cfg.controller_file,
            {
                "at": time.time(),
                "healthy": True,
                "status": status,
                "pid": os.getpid(),
                **fields,
            },
        )
        # Close the deadline-edge race: the lease worker can expire after the
        # pre-check but before this healthy write lands.  A second check makes
        # that transition publish the lease's unhealthy heartbeat again and
        # prevents the inner write from masking the fail-closed deadline.
        lease_failure = _bounded_heartbeat_write_failure(self.cfg.controller_file)
        if lease_failure is not None:
            raise InvariantError(lease_failure)

    def sleep_with_heartbeat(self, seconds: float, status: str) -> None:
        deadline = time.monotonic() + max(seconds, 0)
        interval = max(min(self.cfg.heartbeat_max_age / 3, 5.0), 0.5)
        while time.monotonic() < deadline:
            self.touch_controller(status)
            time.sleep(min(interval, deadline - time.monotonic()))
        self.touch_controller(status)

    def _connect(
        self,
        user: str,
        password: str,
        dbname: str,
        *,
        ports: Iterable[int] | None = None,
    ):
        selected = tuple(ports or self.cfg.pg_local_ports)
        return psycopg.connect(
            host=",".join("127.0.0.1" for _ in selected),
            port=",".join(str(port) for port in selected),
            user=user,
            password=password,
            dbname=dbname,
            target_session_attrs="read-write",
            connect_timeout=2,
            row_factory=dict_row,
        )

    def agent_db(self, *, ports: Iterable[int] | None = None):
        return self._connect(
            "agent_sessions",
            self.agent_env["APP_DB_PASSWORD"],
            "agent_sessions",
            ports=ports,
        )

    def pocket_db(self, *, ports: Iterable[int] | None = None):
        return self._connect(
            "pocket",
            self.pocket_env["APP_DB_PASSWORD"],
            "pocket",
            ports=ports,
        )

    def hindsight_db(self):
        return self._connect(
            "hindsight", self.hindsight_state["DB_PASSWORD"], "hindsight"
        )

    def fugu_db(self):
        return self._connect("litellm", self.fugu_env["LITELLM_DB_PASSWORD"], "litellm")

    def agent_circuit_is_open(self) -> bool:
        """Return the persisted Agent circuit state through the shared PG path."""
        with self.agent_db() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT circuit_open FROM hindsight_sync_state WHERE bank_id='agent-sessions'"
            )
            row = cur.fetchone()
        return bool(row and row["circuit_open"])

    def _provider_preflight_failure_records(
        self,
        incident: dict[str, Any],
        live_fugu: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """Return exact controller-preflight failures owned outside the envelope."""
        state = incident.get("provider_recovery")
        if not isinstance(state, dict) or state.get("kind") != "auth":
            return {}
        records: dict[str, dict[str, Any]] = {}
        proofs = [state.get("pre_reset_preflights")]
        provider_proof = state.get("provider_proof")
        if isinstance(provider_proof, dict):
            proofs.append(provider_proof.get("pre_reset_preflights"))
        for proof in proofs:
            if not isinstance(proof, dict):
                continue
            for value in proof.get("authorized_fugu_failures", []):
                if not isinstance(value, dict) or not value.get("request_id"):
                    raise InvariantError("provider preflight proof is malformed")
                request_id = str(value["request_id"])
                if live_fugu.get(request_id) != value:
                    raise InvariantError("provider preflight Fugu proof drift")
                records[request_id] = value

        # A crash can land after the deterministic request but before the proof
        # save. The reset token and baseline were durably stored first.
        reset_token = str(state.get("reset_token") or "")
        baseline = state.get("pre_reset_fugu_baseline")
        if reset_token and isinstance(baseline, list):
            old_ids = {
                str(value.get("request_id") or "")
                for value in baseline
                if isinstance(value, dict)
            }
            digest = hashlib.sha256(f"pre-reset:{reset_token}".encode()).hexdigest()
            root = f"hindsight-recovery-{digest[:24]}"
            expected = {
                f"{root}-gpt_oss": self.cfg.expected_hindsight_model,
                f"{root}-qwen": QWEN_MODEL,
            }
            for session_id, model in expected.items():
                matches = [
                    value
                    for request_id, value in live_fugu.items()
                    if request_id not in old_ids
                    and value.get("session_id") == session_id
                ]
                if len(matches) > 1:
                    raise InvariantError(
                        "duplicate deterministic provider preflight Fugu records"
                    )
                for value in matches:
                    if (
                        value.get("requested_model") != model
                        or value.get("agent_id") != "hindsight-recovery-controller"
                        or value.get("http_status") not in {401, 402}
                    ):
                        raise InvariantError(
                            "uncommitted provider preflight provenance drift"
                        )
                    records[str(value["request_id"])] = value
        return records

    def _recovery_probe_provider_failure_records(
        self,
        incident: dict[str, Any],
        live_provider: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """Recover deterministic probe failures across response-save crashes."""
        state = incident.get("provider_recovery")
        if not isinstance(state, dict) or state.get("kind") != "auth":
            return {}
        incident_id = str(state.get("probe_auth_incident_request_id") or "")
        if not incident_id:
            return {}
        expected: dict[tuple[str, str], str] = {}
        for route, value in dict(state.get("probe_routes") or {}).items():
            if not isinstance(value, dict):
                raise InvariantError("recovery-probe route state is malformed")
            provenance = str(value.get("provenance") or "")
            if provenance:
                expected[
                    (str(route), hashlib.sha256(provenance.encode()).hexdigest())
                ] = incident_id
        for value in list(state.get("probe_history") or []):
            if not isinstance(value, dict):
                raise InvariantError("recovery-probe history is malformed")
            if value.get("result") not in {
                "expected_auth_failure",
                "retryable_failure",
            }:
                continue
            route = str(value.get("route") or "")
            provenance = str(value.get("provenance") or "")
            if route not in {"gpt_oss", "qwen"} or not provenance:
                raise InvariantError("recovery-probe failure history drift")
            expected[(route, hashlib.sha256(provenance.encode()).hexdigest())] = (
                incident_id
            )
        records: dict[str, dict[str, Any]] = {}
        for request_id, value in live_provider.items():
            route = str(value.get("recovery_probe_route") or "")
            provenance_sha = str(value.get("request_provenance_sha256") or "")
            if (route, provenance_sha) not in expected:
                continue
            status = int(value.get("status_code") or 0)
            if value.get("recovery_auth_incident_request_id") != incident_id or (
                status not in {401, 402, 429} and not 500 <= status <= 599
            ):
                raise InvariantError("recovery-probe provider failure provenance drift")
            records[request_id] = value
        return records

    def _terminal_lineage_records(
        self,
        row: dict[str, Any],
        parent: dict[str, Any],
        *,
        expected_operation_id: str | None = None,
        allow_cleared_local_operation_id: bool = False,
    ) -> list[dict[str, Any]]:
        """Prove one terminal batch parent and its exact retain child.

        Hindsight clears a one-item batch parent's task payload after fan-out,
        including on provider failure.  A cleared parent therefore derives
        payload identity only from its single exact retain child; a non-empty
        parent must independently match the outbox row.  Parent identity,
        terminal status, immutable fan-out metadata, local canonical hash,
        child linkage/hash, and document absence are all still required.
        """
        document_id = str(row["document_id"])
        payload_hash = str(row["payload_hash"])
        submitted_payload_hash = str(row.get("submitted_payload_hash") or "")
        item = dict(row.get("item") or {})
        encoded = json.dumps(
            item,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        parent_id = str(parent.get("operation_id") or "")
        local_operation_id = str(row.get("operation_id") or "")
        expected_parent_id = str(expected_operation_id or local_operation_id)
        parent_status = str(parent.get("status") or "").lower()
        parent_metadata = dict(parent.get("result_metadata") or {})
        if (
            not document_id
            or not payload_hash
            or item.get("document_id") != document_id
            or hashlib.sha256(encoded.encode()).hexdigest() != payload_hash
            or (submitted_payload_hash and submitted_payload_hash != payload_hash)
            or not parent_id
            or parent_id != expected_parent_id
            or (
                local_operation_id != expected_parent_id
                and not (allow_cleared_local_operation_id and not local_operation_id)
            )
            or str(parent.get("operation_type") or "").lower() != "batch_retain"
            or parent_status not in {"failed", "cancelled"}
            or parent_metadata.get("is_parent") is not True
            or type(parent_metadata.get("items_count")) is not int
            or parent_metadata.get("items_count") != 1
            or type(parent_metadata.get("num_sub_batches")) is not int
            or parent_metadata.get("num_sub_batches") != 1
        ):
            raise InvariantError("terminal parent operation lineage is not exact")
        parent_payload = parent.get("task_payload")
        parent_payload_cleared = parent_payload is None or (
            isinstance(parent_payload, dict) and not parent_payload
        )
        if not parent_payload_cleared and (
            not isinstance(parent_payload, dict)
            or not operation_matches_outbox(
                parent,
                document_id=document_id,
                payload_hash=payload_hash,
                operation_id=parent_id,
                operation_types=frozenset({"batch_retain"}),
            )
        ):
            raise InvariantError("terminal parent payload lineage is not exact")
        if self._remote_document(document_id) is not None:
            raise InvariantError("terminal operation has an existing document")
        children = self._retain_children_for_parent(parent_id)
        if len(children) != 1:
            raise InvariantError(
                "terminal batch parent does not have exactly one retain child"
            )
        child = children[0]
        metadata = dict(child.get("result_metadata") or {})
        child_status = str(child.get("status") or "").lower()
        child_id = str(child.get("operation_id") or "")
        child_payload = child.get("task_payload")
        active_child_metadata = (
            child_status in {"pending", "processing"}
            and "document_ids" not in metadata
            and type(metadata.get("items_count")) is int
            and metadata.get("items_count") == 1
            and type(metadata.get("sub_batch_index")) is int
            and metadata.get("sub_batch_index") == 1
            and type(metadata.get("total_sub_batches")) is int
            and metadata.get("total_sub_batches") == 1
        )
        if (
            not child_id
            or not isinstance(child_payload, dict)
            or str(child_payload.get("operation_id") or "") != child_id
            or str(child_payload.get("type") or "").lower() != "batch_retain"
            or str(metadata.get("parent_operation_id") or "") != parent_id
            or (
                metadata.get("document_ids") != [document_id]
                and not active_child_metadata
            )
            or child_status not in {"pending", "processing", "failed", "cancelled"}
            or not operation_matches_outbox(
                child,
                document_id=document_id,
                payload_hash=payload_hash,
                operation_id=child_id,
                operation_types=frozenset({"retain"}),
            )
        ):
            raise InvariantError("terminal retain child lineage/status is not exact")
        if child_status in {"pending", "processing"}:
            return []
        return [
            _exact_record(value, "operation_id", "operation_type", "status")
            for value in (parent, child)
        ]

    @staticmethod
    def _counts(cur: Any, table: str = "hindsight_outbox") -> dict[str, int]:
        cur.execute(
            f"SELECT state, count(*)::int AS n FROM {table} GROUP BY state ORDER BY state"
        )
        return {str(row["state"]): int(row["n"]) for row in cur.fetchall()}

    @staticmethod
    def _http_json(
        url: str, *, headers: dict[str, str] | None = None
    ) -> dict[str, Any]:
        request = urllib.request.Request(url, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=4) as response:
                if response.status != 200:
                    raise InvariantError(f"{url} returned HTTP {response.status}")
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise InvariantError(f"{url} returned HTTP {exc.code}") from exc
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            raise InvariantError(
                f"{url} authoritative read failed: {type(exc).__name__}: {exc}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise InvariantError(f"{url} returned invalid JSON") from exc

    @staticmethod
    def _http_post_json(
        url: str,
        payload: dict[str, Any],
        *,
        headers: dict[str, str],
        timeout: float = 30,
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", **headers},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read())
                if response.status != 200:
                    raise InvariantError(f"{url} returned HTTP {response.status}")
                return body
        except urllib.error.HTTPError as exc:
            detail = exc.read(500).decode(errors="replace")
            raise InvariantError(f"{url} returned HTTP {exc.code}: {detail}") from exc

    @staticmethod
    def _http_post_json_status(
        url: str,
        payload: dict[str, Any],
        *,
        headers: dict[str, str],
        timeout: float = 30,
    ) -> tuple[int, dict[str, str], dict[str, Any]]:
        """POST JSON while preserving audited non-2xx response semantics."""
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", **headers},
            method="POST",
        )
        try:
            response = urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            raw = exc.read(4096)
            try:
                body = json.loads(raw)
            except json.JSONDecodeError as decode_exc:
                raise InvariantError(
                    f"{url} returned non-JSON HTTP {exc.code}"
                ) from decode_exc
            return int(exc.code), dict(exc.headers.items()), body
        with response:
            raw = response.read()
            try:
                body = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise InvariantError(
                    f"{url} returned non-JSON HTTP {response.status}"
                ) from exc
            return int(response.status), dict(response.headers.items()), body

    def agent_runtime_proof(self) -> dict[str, Any]:
        """Read a fresh, read-only proof of the actual Agent VM/API runtime.

        The optional verifier command must print one JSON object and must not
        mutate deployment state.  Without a command, an external verifier must
        atomically refresh AGENT_RUNTIME_PROOF_FILE.  The controller validates
        the measured descriptor rather than trusting the local runtime env.
        """
        if self.cfg.agent_runtime_verifier_command:
            result = subprocess.run(
                shlex.split(self.cfg.agent_runtime_verifier_command),
                cwd=self.cfg.root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=30,
                check=False,
            )
            if result.returncode:
                raise InvariantError(
                    f"Agent runtime verifier failed ({result.returncode}): "
                    f"{result.stdout[-1000:]}"
                )
            proof: dict[str, Any] | None = None
            for raw in reversed(result.stdout.splitlines()):
                with contextlib.suppress(json.JSONDecodeError):
                    value = json.loads(raw)
                    if isinstance(value, dict):
                        proof = value
                        break
            if proof is None:
                raise InvariantError("Agent runtime verifier omitted its JSON proof")
            _atomic_json(self.cfg.agent_runtime_proof_file, proof)
            return proof
        if not self.cfg.agent_runtime_proof_file.exists():
            raise InvariantError("Agent runtime proof file is absent")
        value = json.loads(self.cfg.agent_runtime_proof_file.read_text())
        if not isinstance(value, dict):
            raise InvariantError("Agent runtime proof is not a JSON object")
        return value

    def agent_runtime_proof_failures(self, proof: dict[str, Any]) -> list[str]:
        failures: list[str] = []
        if proof.get("read_only") is not True:
            failures.append("Agent runtime proof is not marked read-only")
        try:
            age = time.time() - float(proof["verified_at"])
        except (KeyError, TypeError, ValueError):
            failures.append("Agent runtime proof timestamp is invalid")
        else:
            if age < -5 or age > self.cfg.agent_runtime_proof_max_age:
                failures.append("Agent runtime proof is stale")
        if proof.get("api_healthy") is not True:
            failures.append("Agent API health proof failed")
        if proof.get("vm_healthy") is not True:
            failures.append("Agent VM health proof failed")
        if not self.cfg.expected_agent_image_digest:
            failures.append("expected Agent image digest is not configured")
        elif str(proof.get("image_digest") or "") != str(
            self.cfg.expected_agent_image_digest
        ):
            failures.append("Agent running image digest drift")
        expected_compose = self.cfg.expected_agent_compose_hash.removeprefix("0x")
        actual_compose = str(proof.get("compose_hash") or "").removeprefix("0x")
        if not expected_compose:
            failures.append("expected Agent compose hash is not configured")
        elif actual_compose != expected_compose:
            failures.append("Agent measured compose descriptor drift")
        if int(proof.get("run_limit") or -1) != self.cfg.stage_cap:
            failures.append("Agent proven runtime cap drift")
        if int(proof.get("max_in_flight") or -1) != self.cfg.max_agent_active:
            failures.append("Agent proven runtime max-in-flight drift")
        if not self.cfg.expected_agent_outbox_build_id:
            failures.append("expected Agent outbox build identifier is not configured")
        elif str(proof.get("outbox_build_id") or "") != str(
            self.cfg.expected_agent_outbox_build_id
        ):
            failures.append("Agent proven outbox build identifier drift")
        if not str(proof.get("app_id") or "") or not str(proof.get("vm_id") or ""):
            failures.append("Agent runtime proof lacks app/VM identity")
        descriptor = proof.get("measured_descriptor")
        if not isinstance(descriptor, dict):
            failures.append("Agent measured runtime descriptor is absent")
        else:
            descriptor_digest = hashlib.sha256(
                json.dumps(descriptor, separators=(",", ":"), sort_keys=True).encode()
            ).hexdigest()
            if descriptor_digest != str(proof.get("descriptor_sha256") or ""):
                failures.append("Agent runtime descriptor digest mismatch")
            exact = {
                "app_id": proof.get("app_id"),
                "vm_id": proof.get("vm_id"),
                "image_digest": proof.get("image_digest"),
                "compose_hash": proof.get("compose_hash"),
                "run_limit": proof.get("run_limit"),
                "max_in_flight": proof.get("max_in_flight"),
                "outbox_build_id": proof.get("outbox_build_id"),
            }
            if any(descriptor.get(key) != value for key, value in exact.items()):
                failures.append("Agent runtime descriptor/top-level proof mismatch")
        return failures

    def raw_spotcheck_snapshot(self) -> dict[str, Any]:
        """Read only the continuous-run aggregates, with no audit policy.

        This deliberately does not call ``quick_snapshot``, ``quick_failures``,
        ``full_audit``, phase/checkpoint readers, Pocket, service health, or any
        controller helper. A caller treats every exception as a non-pausing
        unknown observation.
        """

        captured_at = time.time()
        with self.agent_db() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT document_id,state,attempts
                   FROM hindsight_outbox ORDER BY document_id"""
            )
            rows = list(cur.fetchall())
            cur.execute(
                """SELECT circuit_open,reason
                   FROM hindsight_sync_state WHERE bank_id='agent-sessions'"""
            )
            circuit = cur.fetchone()
            histories: dict[str, int] = {}
            history_max_ids: dict[str, int] = {}
            for name, table in (
                ("retry", "hindsight_outbox_retry_history"),
                ("claim", "hindsight_outbox_claim_recovery_history"),
                ("adoption", "hindsight_outbox_operation_adoption_history"),
            ):
                cur.execute(
                    f"SELECT count(*)::int AS n, COALESCE(max(id),0)::bigint AS max_id FROM {table}"
                )
                value = cur.fetchone()
                histories[name] = int(value["n"])
                history_max_ids[name] = int(value["max_id"])

        document_ids = [str(row["document_id"]) for row in rows]
        counts = Counter(str(row["state"]) for row in rows)
        active = sum(int(counts.get(state, 0)) for state in ACTIVE_STATES)
        ever_attempted = sum(int(row.get("attempts") or 0) > 0 for row in rows)

        with self.hindsight_db() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT id FROM documents
                   WHERE bank_id='agent-sessions' ORDER BY id"""
            )
            remote_ids = [str(row["id"]) for row in cur.fetchall()]
            cur.execute(
                """SELECT status,count(*)::int AS n
                   FROM async_operations WHERE bank_id='agent-sessions'
                   GROUP BY status ORDER BY status"""
            )
            operation_counts = {
                str(row["status"]): int(row["n"]) for row in cur.fetchall()
            }

        with self.fugu_db() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT count(*)::bigint AS n,
                          count(*) FILTER (WHERE http_status::text='429')::bigint AS rate_limited
                   FROM fugu_credit_ledger"""
            )
            fugu_aggregate = cur.fetchone()
            cur.execute(
                """SELECT request_id,start_time,status,http_status,error_message,
                          requested_model,upstream_model,account_id
                   FROM fugu_credit_ledger
                   WHERE account_id='redpill' AND status <> 'success'
                   ORDER BY start_time,request_id"""
            )
            redpill_failures = [dict(row) for row in cur.fetchall()]
        ooc_ids = sorted(
            str(value["request_id"])
            for value in redpill_failures
            if value.get("request_id") and direct_redpill_out_of_credit(value)
        )

        budget_status = self.budget_recovery_status()
        health = dict(budget_status["health"])
        budget_ooc_ids = sorted(
            str(value["request_id"])
            for value in budget_status["provider_failure_manifest"]
            if value.get("request_id")
            and int(value.get("status_code") or 0) == 402
        )
        if str(health.get("circuit_reason") or "") == REDPILL_CREDIT_EXHAUSTED_REASON:
            budget_ooc_ids.append(
                "budget-circuit:" + REDPILL_CREDIT_EXHAUSTED_REASON
            )

        def finite_number(name: str) -> float:
            value = health.get(name)
            if isinstance(value, bool):
                raise InvariantError(f"spot-check budget {name} is malformed")
            try:
                numeric = float(value)
            except (TypeError, ValueError) as exc:
                raise InvariantError(
                    f"spot-check budget {name} is malformed"
                ) from exc
            if not math.isfinite(numeric) or numeric < 0:
                raise InvariantError(f"spot-check budget {name} is malformed")
            return numeric

        return {
            "version": SPOTCHECK_VERSION,
            "captured_at": captured_at,
            "captured_at_iso": datetime.fromtimestamp(
                captured_at, tz=timezone.utc
            ).isoformat(),
            "agent": {
                "counts": dict(sorted(counts.items())),
                "total": len(rows),
                "distinct_documents": len(set(document_ids)),
                "document_ids": document_ids,
                "queued": int(counts.get("queued", 0)),
                "succeeded": int(counts.get("succeeded", 0)),
                "active": active,
                "ever_attempted": ever_attempted,
                "circuit_open": bool(circuit and circuit["circuit_open"]),
                "circuit_reason": str(circuit["reason"] or "") if circuit else "",
            },
            "histories": histories,
            "history_max_ids": history_max_ids,
            "hindsight": {
                "remote_documents": len(remote_ids),
                "remote_distinct_documents": len(set(remote_ids)),
                "document_ids": remote_ids,
                "operations": sum(operation_counts.values()),
                "operation_counts": operation_counts,
            },
            "fugu": {
                "rows": int(fugu_aggregate["n"]),
                "rate_limited": int(fugu_aggregate["rate_limited"]),
                "redpill_ooc_request_ids": ooc_ids,
            },
            "budget": {
                "provider_in_flight": int(health["in_flight"]),
                "provider_spent_usd": finite_number("provider_spent_usd"),
                "backfill_spent_usd": finite_number(
                    "hindsight_backfill_spent_usd"
                ),
                "provider_calls": len(budget_status["provider_call_manifest"]),
                "provider_failures": len(
                    budget_status["provider_failure_manifest"]
                ),
                "redpill_ooc_request_ids": sorted(set(budget_ooc_ids)),
            },
        }

    def quick_snapshot(
        self,
        *,
        audit_heartbeat_label: str | None = None,
        audit_heartbeat_fields: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        def serialized_read() -> dict[str, Any]:
            with _exclusive_lock(self.cfg.authoritative_read_lock_file):
                return self._quick_snapshot_unlocked()

        label = audit_heartbeat_label
        if label is None and self.controller_owned:
            label = "controller"
        if label is None:
            return serialized_read()
        path = {
            "guard": self.cfg.heartbeat_file,
            "controller": self.cfg.controller_file,
        }.get(label)
        if path is None:
            raise ValueError(f"unsupported audit heartbeat label: {label}")
        fields = dict(audit_heartbeat_fields or {})
        fields.setdefault("status", "authoritative_read")
        pulse_seconds = max(
            min(self.cfg.heartbeat_max_age / 3, 5.0),
            0.5,
        )
        with _BoundedAuditHeartbeat(
            path,
            label,
            deadline_seconds=self.cfg.heartbeat_max_age * 4,
            pulse_seconds=pulse_seconds,
            fields=fields,
        ):
            return serialized_read()

    def _quick_snapshot_unlocked(self) -> dict[str, Any]:
        # Never trust the Recovery object's construction-time environment.
        # A roll/restart can change the persistent cap, concurrency, image
        # build identifier, or compose binding while this controller process
        # remains alive.
        runtime = _env_file(Path.home() / ".attestmesh/agent-session-hindsight.env")
        result: dict[str, Any] = {"runtime": runtime}
        with self.agent_db() as conn, conn.cursor() as cur:
            counts = self._counts(cur)
            cur.execute(
                "SELECT circuit_open, reason FROM hindsight_sync_state WHERE bank_id='agent-sessions'"
            )
            result["agent"] = {"counts": counts, "circuit": cur.fetchone()}
        with self.pocket_db() as conn, conn.cursor() as cur:
            counts = self._counts(cur)
            cur.execute(
                "SELECT circuit_open, reason FROM hindsight_sync_state WHERE bank_id='dans-pocket'"
            )
            result["pocket"] = {"counts": counts, "circuit": cur.fetchone()}
        with self.hindsight_db() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT status, count(*)::int AS n FROM async_operations WHERE bank_id='agent-sessions' GROUP BY status"
            )
            result["hindsight_operations"] = {
                str(row["status"]): int(row["n"]) for row in cur.fetchall()
            }
            cur.execute(
                "SELECT count(*)::int AS n FROM documents WHERE bank_id='agent-sessions'"
            )
            result["hindsight_documents"] = int(cur.fetchone()["n"])
        result["hindsight_health"] = self._http_json(
            f"http://127.0.0.1:{self.cfg.hindsight_port}/health"
        )
        result["fugu_health"] = self._http_json(
            f"http://127.0.0.1:{self.cfg.fugu_port}/health/process"
        )
        # Budget health and the exact reservation manifest must come from one
        # proxy lock acquisition.  Separate /health and /admin/reservations
        # calls race every normal reservation start/finish and can otherwise
        # produce a false fail-closed count drift.
        result["budget_recovery"] = self.budget_recovery_status()
        result["budget"] = dict(result["budget_recovery"]["health"])
        result["agent_runtime_proof"] = self.agent_runtime_proof()
        result["patroni"] = [
            self._http_json(f"http://127.0.0.1:{port}/patroni")
            for port in self.cfg.patroni_ports
        ]
        return result

    def quick_failures(
        self,
        snap: dict[str, Any],
        phase: dict[str, Any],
        checkpoint: dict[str, Any] | None = None,
        *,
        excluded_heartbeat_labels: Iterable[str] = (),
    ) -> list[str]:
        self.touch_controller("quick_audit")
        failures: list[str] = []
        checkpoint_value = checkpoint or read_checkpoint(self.cfg)
        accepted = checkpoint_value["accepted"]
        ac = snap["agent"]["counts"]
        pc = snap["pocket"]["counts"]
        attempted = sum(int(ac.get(state, 0)) for state in ATTEMPTED_STATES)
        active = sum(int(ac.get(state, 0)) for state in ACTIVE_STATES)
        errors = sum(int(ac.get(state, 0)) for state in ERROR_STATES)
        if sum(int(v) for v in ac.values()) != self.cfg.approved_total:
            failures.append("Agent approved batch total drift")
        if attempted > self.cfg.stage_cap:
            failures.append("Agent attempted count exceeds stage cap")
        if active > self.cfg.max_agent_active:
            failures.append(
                f"Agent active rows exceed configured limit {self.cfg.max_agent_active}"
            )
        if errors:
            failures.append("Agent failed/blocked row present")
        name = str(phase["name"])
        if phase.get("controller_required"):
            stage = checkpoint_value["stage"]
            if (
                int(stage.get("cap", -1)) != self.cfg.stage_cap
                or int(stage.get("max_agent_active", -1)) != self.cfg.max_agent_active
                or int(stage.get("max_provider_in_flight", -1))
                != self.cfg.max_provider_in_flight
            ):
                failures.append("checkpoint stage configuration drift")
        running_names = {"running", "release_verifying"}
        if name not in running_names and not bool(
            snap["agent"]["circuit"]["circuit_open"]
        ):
            failures.append("Agent circuit is closed outside running phase")
        if (
            name in running_names
            and attempted >= self.cfg.stage_cap
            and not bool(snap["agent"]["circuit"]["circuit_open"])
        ):
            failures.append("Agent circuit remained closed at stage cap")
        if not bool(snap["pocket"]["circuit"]["circuit_open"]):
            failures.append("Pocket circuit is closed")
        if sum(int(pc.get(state, 0)) for state in ACTIVE_STATES + ERROR_STATES):
            failures.append("Pocket has active/error work")
        expected_pocket = accepted.get("pocket_counts")
        if expected_pocket is not None and pc != expected_pocket:
            failures.append("Pocket count baseline drift")
        runtime = snap["runtime"]
        if runtime.get("HINDSIGHT_OUTBOX_MAX_IN_FLIGHT") != str(
            self.cfg.max_agent_active
        ):
            failures.append(
                f"Agent persistent max_in_flight is not {self.cfg.max_agent_active}"
            )
        if runtime.get("HINDSIGHT_OUTBOX_RUN_LIMIT") != str(self.cfg.stage_cap):
            failures.append("Agent persistent cap drift")
        failures.extend(self.agent_runtime_proof_failures(snap["agent_runtime_proof"]))
        helper_identity_failure = recovery_helper_identity_failure(self.cfg)
        if helper_identity_failure:
            failures.append(helper_identity_failure)
        hop = snap["hindsight_operations"]
        if int(hop.get("failed", 0)) != int(accepted["hindsight_failed"]):
            failures.append("Hindsight failed-operation count drift")
        if name not in running_names and sum(
            int(hop.get(state, 0)) for state in ("pending", "processing")
        ):
            failures.append("Hindsight active work outside running phase")
        budget = snap["budget"]
        budget_recovery = snap["budget_recovery"]
        # The maintenance-hold POST is irreversible.  A crash after that POST
        # but before the finalized phase commit leaves the durable substage at
        # hold_started with its older budget=closed expectation.  Admit only
        # the exact drained final hold so the idempotent finalize() path can
        # verify sealed controls, re-audit, and commit the terminal phase.
        final_hold_resume = (
            name == "finalizing"
            and phase.get("finalization_stage") == "hold_started"
            and exact_final_budget_hold(budget)
        )
        in_flight = int(budget.get("in_flight") or 0)
        reservations = list(budget_recovery.get("reservations") or [])
        if in_flight != len(reservations):
            failures.append("budget health/reservation in-flight count drift")
        if in_flight > self.cfg.max_provider_in_flight:
            failures.append(
                f"provider in_flight exceeds configured limit {self.cfg.max_provider_in_flight}"
            )
        if phase.get("budget") == "open":
            expected_budget_reason = str(phase.get("budget_reason") or "")
            if not expected_budget_reason:
                failures.append("budget open phase omitted its exact reason")
            if (
                not bool(budget.get("circuit_open"))
                or budget.get("circuit_reason") != expected_budget_reason
            ):
                failures.append("budget open-circuit reason drift")
        elif phase.get("budget") == "closed":
            if not final_hold_resume and (
                bool(budget.get("circuit_open"))
                or budget.get("status")
                not in {
                    "healthy",
                    "ok",
                }
            ):
                failures.append("budget circuit is not healthy/closed")
        if budget.get("provider_egress_enabled") is not True:
            failures.append("budget provider egress is not enabled")
        if budget.get("hindsight_phase") != self.cfg.expected_hindsight_phase:
            failures.append("budget Hindsight phase drift")
        if budget.get("hindsight_model") != self.cfg.expected_hindsight_model:
            failures.append("budget Hindsight model drift")
        try:
            effective_limit = Decimal(str(budget["provider_effective_limit_usd"]))
        except (KeyError, ValueError):
            failures.append("budget provider effective limit is absent")
        else:
            if effective_limit != self.cfg.expected_provider_effective_limit:
                failures.append("budget provider effective limit drift")
        try:
            backfill_limit = Decimal(str(budget["hindsight_backfill_limit_usd"]))
        except (KeyError, ValueError):
            failures.append("budget Hindsight backfill limit is absent")
        else:
            if backfill_limit != self.cfg.hard_budget:
                failures.append("budget Hindsight backfill limit drift")
        if int(budget.get("hindsight_llm_max_concurrent") or -1) != int(
            self.cfg.expected_hindsight_llm_concurrency
        ):
            failures.append("budget Hindsight LLM concurrency drift")
        if int(budget.get("provider_max_in_flight") or -1) != int(
            self.cfg.max_provider_in_flight
        ):
            failures.append("budget provider max-in-flight ceiling drift")
        projection = compute_projection(
            budget.get("hindsight_backfill_spent_usd", 0),
            int(ac.get("succeeded", 0)),
            self.cfg,
        ) + Decimal(str(budget_recovery.get("total_max_usd") or 0))
        if projection >= self.cfg.hard_budget:
            failures.append(f"projected full batch {projection} exceeds hard budget")
        if (
            snap["hindsight_health"].get("status") != "healthy"
            or snap["hindsight_health"].get("database") != "connected"
        ):
            failures.append("Hindsight health degraded")
        if snap["fugu_health"].get("status") != "router-alive":
            failures.append("Fugu router health degraded")
        patroni = list(snap.get("patroni") or [])
        if len(patroni) != 3 or any(
            node.get("state") != "running"
            or node.get("role") not in {"primary", "replica"}
            for node in patroni
        ):
            failures.append("Patroni quick health degradation")
        elif sum(node.get("role") == "primary" for node in patroni) != 1:
            failures.append("Patroni quick proof does not have exactly one primary")
        else:
            timelines = [node.get("timeline") for node in patroni]
            if (
                any(value is None for value in timelines)
                or len({str(value) for value in timelines}) != 1
            ):
                failures.append("Patroni quick timeline consistency proof failed")
        if phase.get("controller_required"):
            failures.extend(
                heartbeat_failures(
                    (
                        (self.cfg.heartbeat_file, "guard"),
                        (self.cfg.watchdog_file, "watchdog"),
                        (self.cfg.controller_file, "controller"),
                    ),
                    self.cfg.heartbeat_max_age,
                    excluded_heartbeat_labels=excluded_heartbeat_labels,
                )
            )
            services = {
                service: _service_evidence(service)
                for service in POST_INCIDENT_RELEASE_SERVICES
            }
            snap["services"] = services
            for service, evidence in services.items():
                if (
                    evidence.get("ActiveState") != "active"
                    or int(evidence.get("MainPID") or 0) <= 0
                    or int(evidence.get("NRestarts", -1)) < 0
                ):
                    failures.append(f"{service} inactive")
            tunnel_ports = (
                *self.cfg.pg_local_ports,
                self.cfg.hindsight_port,
                self.cfg.budget_port,
                self.cfg.fugu_port,
                *self.cfg.patroni_ports,
            )
            if not all(_port_ready(port) for port in tunnel_ports):
                failures.append("recovery tunnel port set is incomplete")
        return failures

    def legacy_held_audit(self, phase: dict[str, Any]) -> dict[str, Any]:
        """Authoritative read-only audit for the sealed 3,823 migration hold.

        The old deployment predates the budget admin endpoint and measured
        Agent descriptor.  This gate therefore cannot release work; it exists
        only to prove the databases, public health/accounting state, old
        checkpoint, and both circuits are clean while the atomic 5,142 rollout
        is installed.  Configure-stage must subsequently bootstrap exact
        manifests and pass normal full audits before any release.
        """
        if not self.cfg.allow_legacy_held_stage or self.cfg.stage_cap != 3823:
            raise InvariantError(
                "legacy-held audit is restricted to the sealed 3,823 migration"
            )
        checkpoint = read_checkpoint(self.cfg)
        if checkpoint.get("stage") != {
            "approved_total": 5142,
            "cap": 3823,
            "max_agent_active": 2,
            "max_provider_in_flight": 6,
        }:
            raise InvariantError("legacy held checkpoint stage is not exact")
        failures: list[str] = []
        if phase.get("name") not in {"ready", "running", "cap_hold"}:
            failures.append("legacy migration phase is not a recognized hold")
        with self.agent_db() as conn, conn.cursor() as cur:
            agent_counts = self._counts(cur)
            cur.execute(
                "SELECT circuit_open,reason FROM hindsight_sync_state WHERE bank_id='agent-sessions'"
            )
            agent_circuit = cur.fetchone()
            cur.execute(
                """SELECT o.document_id,o.source_kind,o.source_id,o.payload_hash,
                          o.submitted_payload_hash,o.item,o.state,
                          s.content_hash AS source_document
                   FROM hindsight_outbox o
                   LEFT JOIN sessions s ON s.id=o.source_id
                   ORDER BY o.document_id"""
            )
            rows = list(cur.fetchall())
            history_counts: dict[str, int] = {}
            for name, table in (
                ("retry", "hindsight_outbox_retry_history"),
                ("claim", "hindsight_outbox_claim_recovery_history"),
                ("adoption", "hindsight_outbox_operation_adoption_history"),
            ):
                cur.execute(f"SELECT count(*)::int AS n FROM {table}")
                history_counts[name] = int(cur.fetchone()["n"])
        with self.pocket_db() as conn, conn.cursor() as cur:
            pocket_counts = self._counts(cur)
            cur.execute(
                "SELECT circuit_open,reason FROM hindsight_sync_state WHERE bank_id='dans-pocket'"
            )
            pocket_circuit = cur.fetchone()
        documents = [str(row["document_id"]) for row in rows]
        if len(rows) != 5142 or len(documents) != len(set(documents)):
            failures.append("legacy Agent approved document set is not exact")
        for row in rows:
            item = dict(row["item"])
            if hashlib.sha256(
                json.dumps(
                    item,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode()
            ).hexdigest() != str(row["payload_hash"]):
                failures.append(
                    f"legacy payload hash mismatch for {row['document_id']}"
                )
                break
            if (
                item.get("document_id") != row["document_id"]
                or row["source_kind"] != "agent_session"
                or row["source_document"] != row["document_id"]
            ):
                failures.append(
                    f"legacy source lineage mismatch for {row['document_id']}"
                )
                break
        if not agent_circuit or not bool(agent_circuit["circuit_open"]):
            failures.append("legacy Agent circuit is not open")
        if not pocket_circuit or not bool(pocket_circuit["circuit_open"]):
            failures.append("legacy Pocket circuit is not open")
        if sum(
            int(agent_counts.get(value, 0)) for value in ACTIVE_STATES + ERROR_STATES
        ):
            failures.append("legacy Agent hold is not drained")
        if sum(
            int(pocket_counts.get(value, 0)) for value in ACTIVE_STATES + ERROR_STATES
        ):
            failures.append("legacy Pocket hold is not drained")
        if history_counts != checkpoint["accepted"]["histories"]:
            failures.append("legacy exact history counts drift")
        with self.hindsight_db() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT status,count(*)::int AS n FROM async_operations WHERE bank_id='agent-sessions' GROUP BY status"
            )
            hindsight_operations = {
                str(row["status"]): int(row["n"]) for row in cur.fetchall()
            }
            cur.execute(
                """SELECT id,original_text,content_hash,retain_params,tags
                   FROM documents WHERE bank_id='agent-sessions'"""
            )
            remote_rows = list(cur.fetchall())
        if int(hindsight_operations.get("failed", 0)) != int(
            checkpoint["accepted"]["hindsight_failed"]
        ):
            failures.append("legacy Hindsight failure count drift")
        if sum(
            int(hindsight_operations.get(value, 0))
            for value in ("pending", "processing")
        ):
            failures.append("legacy Hindsight activity is not drained")
        local_by_id = {str(row["document_id"]): row for row in rows}
        remote_ids = [str(row["id"]) for row in remote_rows]
        succeeded = {
            str(row["document_id"]) for row in rows if row["state"] == "succeeded"
        }
        if set(remote_ids) != succeeded or len(remote_ids) != len(set(remote_ids)):
            failures.append("legacy remote/succeeded document set drift")
        for remote in remote_rows:
            local = local_by_id.get(str(remote["id"]))
            if local is None or not self._remote_document_matches_item(
                remote, dict(local["item"])
            ):
                failures.append(
                    f"legacy remote document provenance drift for {remote['id']}"
                )
                break
        with self.fugu_db() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT start_time,http_status,requested_model
                   FROM fugu_credit_ledger
                   WHERE start_time >= '2026-07-13 10:17:39+00'
                     AND status <> 'success'
                   ORDER BY start_time"""
            )
            fugu_failures = [
                [
                    row["start_time"].isoformat(),
                    _canonical_http_status(row["http_status"]),
                    str(row["requested_model"]),
                ]
                for row in cur.fetchall()
            ]
        if fugu_failures != checkpoint["accepted"]["fugu_failures"]:
            failures.append("legacy authenticated Fugu failure ledger drift")
        hindsight_health = self._http_json(
            f"http://127.0.0.1:{self.cfg.hindsight_port}/health"
        )
        fugu_health = self._http_json(
            f"http://127.0.0.1:{self.cfg.fugu_port}/health/process"
        )
        budget = self._http_json(f"http://127.0.0.1:{self.cfg.budget_port}/health")
        if (
            hindsight_health.get("status") != "healthy"
            or hindsight_health.get("database") != "connected"
        ):
            failures.append("legacy Hindsight health degraded")
        if fugu_health.get("status") != "router-alive":
            failures.append("legacy Fugu router health degraded")
        if (
            budget.get("circuit_open")
            or budget.get("status") not in {"healthy", "ok"}
            or int(budget.get("in_flight") or 0)
        ):
            failures.append("legacy budget is not healthy/closed/drained")
        projection = compute_projection(
            budget.get("hindsight_backfill_spent_usd", 0),
            int(agent_counts.get("succeeded", 0)),
            self.cfg,
        )
        if projection >= self.cfg.hard_budget:
            failures.append("legacy projected full cost exceeds hard budget")
        runtime = _env_file(Path.home() / ".attestmesh/agent-session-hindsight.env")
        runtime_cap = runtime.get("HINDSIGHT_OUTBOX_RUN_LIMIT")
        if runtime_cap not in {"3823", "5142"}:
            failures.append("legacy migration Agent cap is neither sealed nor staged")
        if runtime.get("HINDSIGHT_OUTBOX_MAX_IN_FLIGHT") != "2":
            failures.append("legacy persistent Agent concurrency drift")
        patroni: list[dict[str, Any]] = []
        for port in self.cfg.patroni_ports:
            body = self._http_json(f"http://127.0.0.1:{port}/patroni")
            patroni.append(body)
            if body.get("state") != "running" or body.get("role") not in {
                "primary",
                "replica",
            }:
                failures.append(f"legacy Patroni degradation on local forward {port}")
        if sum(node.get("role") == "primary" for node in patroni) != 1:
            failures.append("legacy Patroni does not have exactly one primary")
        timelines = [node.get("timeline") for node in patroni]
        if (
            any(value is None for value in timelines)
            or len({str(value) for value in timelines}) != 1
        ):
            failures.append("legacy Patroni timeline consistency proof failed")
        services = {
            service: _service_evidence(service)
            for service in (
                "hindsight-recovery-watchdog.service",
                "hindsight-recovery-tunnel.service",
                "hindsight-recovery-guard.service",
            )
        }
        for service, evidence in services.items():
            if (
                evidence.get("ActiveState") != "active"
                or int(evidence.get("MainPID") or 0) <= 0
                or int(evidence.get("NRestarts", -1)) < 0
            ):
                failures.append(f"legacy {service} inactive")
        heartbeat_evidence: dict[str, Any] = {}
        for path, label in (
            (self.cfg.heartbeat_file, "guard"),
            (self.cfg.watchdog_file, "watchdog"),
        ):
            failure = heartbeat_failure(path, label, self.cfg.heartbeat_max_age)
            if failure:
                failures.append("legacy " + failure)
            with contextlib.suppress(OSError, json.JSONDecodeError):
                heartbeat_evidence[label] = json.loads(path.read_text())
        tunnel_ports = (
            *self.cfg.pg_local_ports,
            self.cfg.hindsight_port,
            self.cfg.budget_port,
            self.cfg.fugu_port,
            *self.cfg.patroni_ports,
        )
        ready_ports = {str(port): _port_ready(port) for port in tunnel_ports}
        if not all(ready_ports.values()):
            failures.append("legacy recovery tunnel port set is incomplete")
        return {
            "agent": {"counts": agent_counts, "circuit": agent_circuit},
            "pocket": {"counts": pocket_counts, "circuit": pocket_circuit},
            "budget": budget,
            "hindsight_operations": hindsight_operations,
            "hindsight_documents": len(remote_rows),
            "histories": history_counts,
            "remote_documents": len(remote_rows),
            "failures": failures,
            "migration_only": True,
            "migration_runtime_cap": runtime_cap,
            "patroni": [
                {
                    "role": node.get("role"),
                    "state": node.get("state"),
                    "timeline": node.get("timeline"),
                }
                for node in patroni
            ],
            "services": services,
            "heartbeats": heartbeat_evidence,
            "tunnel_ports": ready_ports,
        }

    def full_audit(
        self,
        phase: dict[str, Any],
        *,
        excluded_heartbeat_labels: Iterable[str] = (),
        audit_heartbeat_label: str | None = None,
        audit_heartbeat_fields: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        def serialized_audit() -> dict[str, Any]:
            # Guard and controller both run the same expensive authoritative
            # reads.  Serializing them prevents one healthy audit from
            # starving the other's bounded network/database calls.  flock is
            # released automatically on process exit, so this remains
            # restart-safe.  Service callers acquire it inside their bounded
            # heartbeat context and therefore continue advertising liveness
            # while waiting, but still fail closed at the existing deadline.
            with _exclusive_lock(self.cfg.authoritative_read_lock_file):
                return self._full_audit(
                    phase,
                    excluded_heartbeat_labels=excluded_heartbeat_labels,
                )

        label = audit_heartbeat_label
        if label is None and self.controller_owned:
            label = "controller"
        if label is None:
            return serialized_audit()
        path = {
            "guard": self.cfg.heartbeat_file,
            "controller": self.cfg.controller_file,
        }.get(label)
        if path is None:
            raise ValueError(f"unsupported audit heartbeat label: {label}")
        fields = dict(audit_heartbeat_fields or {})
        if label == "controller":
            fields.setdefault("status", "full_audit")
            fields.setdefault("phase", str(phase["name"]))
        pulse_seconds = max(
            min(self.cfg.heartbeat_max_age / 3, 5.0),
            0.5,
        )
        # Four heartbeat windows cover the observed worst-case exact 5,142-row
        # provenance audit under concurrent load, without allowing a wedged
        # audit to advertise liveness indefinitely.
        deadline_seconds = self.cfg.heartbeat_max_age * 4
        with _BoundedAuditHeartbeat(
            path,
            label,
            deadline_seconds=deadline_seconds,
            pulse_seconds=pulse_seconds,
            fields=fields,
        ):
            return serialized_audit()

    def _full_audit(
        self,
        phase: dict[str, Any],
        *,
        excluded_heartbeat_labels: Iterable[str] = (),
    ) -> dict[str, Any]:
        self.touch_controller("full_audit")
        checkpoint = read_checkpoint(self.cfg)
        if (
            checkpoint["accepted"].get("hindsight_failures") is None
            or checkpoint["accepted"].get("history_manifests") is None
            or checkpoint["accepted"].get("fugu_failure_manifest") is None
            or checkpoint["accepted"].get("provider_failure_manifest") is None
            or checkpoint["accepted"].get("exact_hindsight_failures") is None
            or checkpoint["accepted"].get("exact_history_manifests") is None
            or checkpoint["accepted"].get("pocket_counts") is None
            or checkpoint["accepted"].get("operation_manifest") is None
        ):
            raise InvariantError(
                "exact checkpoint manifests require configure-stage bootstrap"
            )
        # full_audit() already owns the shared authoritative-read lock.  Use
        # the unlocked primitive here to avoid a recursive flock deadlock.
        snap = self._quick_snapshot_unlocked()
        failures = self.quick_failures(
            snap,
            phase,
            checkpoint,
            excluded_heartbeat_labels=excluded_heartbeat_labels,
        )
        with self.agent_db() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT o.document_id,o.source_kind,o.source_id,o.payload_hash,
                          o.submitted_payload_hash,o.item,o.state,o.operation_id::text,
                          s.content_hash AS source_document
                   FROM hindsight_outbox o LEFT JOIN sessions s ON s.id=o.source_id
                   ORDER BY o.document_id"""
            )
            rows = list(cur.fetchall())
            cur.execute("SELECT count(*)::int AS n FROM hindsight_outbox_retry_history")
            retry = int(cur.fetchone()["n"])
            cur.execute(
                "SELECT count(*)::int AS n FROM hindsight_outbox_claim_recovery_history"
            )
            claim = int(cur.fetchone()["n"])
            cur.execute(
                "SELECT count(*)::int AS n FROM hindsight_outbox_operation_adoption_history"
            )
            adoption = int(cur.fetchone()["n"])
        documents = [str(row["document_id"]) for row in rows]
        if (
            len(documents) != len(set(documents))
            or len(documents) != self.cfg.approved_total
        ):
            failures.append("Agent duplicate/missing document ID")
        operation_ids = [
            str(row["operation_id"]) for row in rows if row.get("operation_id")
        ]
        if len(operation_ids) != len(set(operation_ids)):
            failures.append("duplicate Agent operation ID")
        for row in rows:
            item = dict(row["item"])
            encoded = json.dumps(
                item, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
            digest = hashlib.sha256(encoded.encode()).hexdigest()
            if digest != row["payload_hash"]:
                failures.append(f"payload hash mismatch for {row['document_id']}")
                break
            if item.get("document_id") != row["document_id"]:
                failures.append(f"item/document mismatch for {row['document_id']}")
                break
            context = str(item.get("context") or "")
            if (
                "source:agent_sessions" not in context
                or f"document:{row['document_id']}" not in context
            ):
                failures.append(
                    f"item source/context mismatch for {row['document_id']}"
                )
                break
            if (
                row["source_kind"] != "agent_session"
                or row["source_document"] != row["document_id"]
            ):
                failures.append(f"source lineage mismatch for {row['document_id']}")
                break
            if (
                row["state"] in ACTIVE_STATES
                and row["payload_hash"] != row["submitted_payload_hash"]
            ):
                failures.append(
                    f"active submitted hash mismatch for {row['document_id']}"
                )
                break
        checkpoint_histories = checkpoint["accepted"]["histories"]
        expected_histories = tuple(
            int(checkpoint_histories[name]) for name in ("retry", "claim", "adoption")
        )
        if (retry, claim, adoption) != expected_histories:
            failures.append(
                f"history drift: {(retry, claim, adoption)} != {expected_histories}"
            )
        with self.hindsight_db() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT id,original_text,content_hash,retain_params,tags
                   FROM documents WHERE bank_id='agent-sessions'"""
            )
            remote_rows = list(cur.fetchall())
            remote_documents = [str(row["id"]) for row in remote_rows]
        if len(remote_documents) != len(set(remote_documents)):
            failures.append("duplicate Hindsight document ID")
        approved = set(documents)
        remote = set(remote_documents)
        if remote - approved:
            failures.append("Hindsight contains document outside approved batch")
        succeeded = {
            str(row["document_id"]) for row in rows if row["state"] == "succeeded"
        }
        if succeeded - remote:
            failures.append("succeeded Agent document missing from Hindsight")
        permitted_remote = {
            str(row["document_id"])
            for row in rows
            if row["state"] in {"succeeded", "submitting", "submitted"}
        }
        if remote - permitted_remote:
            failures.append("Hindsight document has invalid Agent state provenance")
        local_rows = {str(row["document_id"]): row for row in rows}
        for remote_row in remote_rows:
            document_id = str(remote_row["id"])
            local = local_rows.get(document_id)
            if local is None:
                continue
            item = dict(local["item"])
            original_text = str(remote_row["original_text"] or "")
            content_hash = str(remote_row["content_hash"] or "")
            retain_params = dict(remote_row["retain_params"] or {})
            remote_tags = set(str(value) for value in (remote_row["tags"] or []))
            expected_tags = set(str(value) for value in (item.get("tags") or []))
            if original_text != str(item.get("content") or ""):
                failures.append(f"remote content mismatch for {document_id}")
                break
            if content_hash != hashlib.sha256(original_text.encode()).hexdigest():
                failures.append(f"remote content hash mismatch for {document_id}")
                break
            if str(retain_params.get("context") or "") != str(
                item.get("context") or ""
            ):
                failures.append(f"remote context mismatch for {document_id}")
                break
            remote_event_date = retain_params.get("event_date")
            if (remote_event_date or item.get("timestamp")) and str(
                remote_event_date or ""
            ) != str(item.get("timestamp") or ""):
                failures.append(f"remote event-date mismatch for {document_id}")
                break
            if remote_tags != expected_tags:
                failures.append(f"remote tag mismatch for {document_id}")
                break
        with self.fugu_db() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT start_time,http_status,requested_model
                   FROM fugu_credit_ledger
                   WHERE start_time >= '2026-07-13 10:17:39+00' AND status <> 'success'
                   ORDER BY start_time"""
            )
            actual_failures = tuple(
                (
                    row["start_time"].isoformat(),
                    _canonical_http_status(row["http_status"]),
                    str(row["requested_model"]),
                )
                for row in cur.fetchall()
            )
        accepted_fugu_failures = tuple(
            (str(value[0]), _canonical_http_status(value[1]), str(value[2]))
            for value in checkpoint["accepted"]["fugu_failures"]
        )
        if actual_failures != accepted_fugu_failures:
            failures.append("authenticated Fugu failure ledger drift")
        observed_manifests = self.observed_baselines()
        if (
            observed_manifests["fugu_failure_manifest"]
            != checkpoint["accepted"]["fugu_failure_manifest"]
        ):
            failures.append("exact authenticated Fugu failure manifest drift")
        if (
            observed_manifests["provider_failure_manifest"]
            != checkpoint["accepted"]["provider_failure_manifest"]
        ):
            failures.append("exact budget provider failure manifest drift")
        if (
            observed_manifests["hindsight_failures"]
            != checkpoint["accepted"]["hindsight_failures"]
        ):
            failures.append("exact Hindsight failure manifest drift")
        if (
            observed_manifests["history_manifests"]
            != checkpoint["accepted"]["history_manifests"]
        ):
            failures.append("exact Agent history manifest drift")
        if (
            observed_manifests["exact_hindsight_failures"]
            != checkpoint["accepted"]["exact_hindsight_failures"]
        ):
            failures.append("full Hindsight failure provenance manifest drift")
        if (
            observed_manifests["exact_history_manifests"]
            != checkpoint["accepted"]["exact_history_manifests"]
        ):
            failures.append("full Agent history provenance manifest drift")
        operation_failure = terminal_operation_inventory_failure(
            phase_name=str(phase.get("name") or ""),
            accepted_operations=checkpoint["accepted"]["operation_manifest"],
            observed_operations=observed_manifests["operation_manifest"],
        )
        if operation_failure:
            failures.append(operation_failure)
        patroni: list[dict[str, Any]] = []
        for port in self.cfg.patroni_ports:
            body = self._http_json(f"http://127.0.0.1:{port}/patroni")
            patroni.append(body)
            if body.get("state") != "running" or body.get("role") not in {
                "primary",
                "replica",
            }:
                failures.append(f"Patroni degradation on local forward {port}")
        if sum(node.get("role") == "primary" for node in patroni) != 1:
            failures.append("Patroni does not have exactly one primary")
        timelines = [node.get("timeline") for node in patroni]
        if (
            any(value is None for value in timelines)
            or len({str(value) for value in timelines}) != 1
        ):
            failures.append("Patroni timeline consistency proof failed")
        snap["histories"] = {"retry": retry, "claim": claim, "adoption": adoption}
        snap["fugu_failures"] = [list(value) for value in actual_failures]
        snap["checkpoint"] = checkpoint
        snap["remote_documents"] = len(remote_documents)
        snap["patroni"] = [
            {
                "role": node.get("role"),
                "state": node.get("state"),
                "timeline": node.get("timeline"),
            }
            for node in patroni
        ]
        snap["failures"] = failures
        return snap

    def _app_worker(self):
        server = Path.home() / "agent-session-mcp/server"
        outbox = self._reviewed_outbox_module(server / "outbox.py")
        settings = SimpleNamespace(
            database_url=self.agent_dsn(),
            ingest_token=self.agent_env.get("INGEST_TOKEN", "recovery-only"),
            hindsight_url=f"http://127.0.0.1:{self.cfg.hindsight_port}",
            hindsight_token=self.hindsight_state["TAK"],
            hindsight_bank="agent-sessions",
            hindsight_sync_enabled=True,
            hindsight_outbox_max_in_flight=self.cfg.max_agent_active,
            hindsight_outbox_tick_seconds=30,
            hindsight_outbox_run_limit=self.cfg.stage_cap,
        )
        return outbox.OutboxWorker(
            settings,
            _RecoveryHindsightClient(
                settings.hindsight_url,
                settings.hindsight_token,
                settings.hindsight_bank,
            ),
        )

    def _prove_local_outbox_helper(self, path: Path) -> str:
        """Bind controller mutations to the exact reviewed outbox source."""
        expected = (
            self.cfg.expected_recovery_outbox_build_id
            or self.cfg.expected_agent_outbox_build_id
        )
        match = re.search(r"(?:^|[@:])sha256:([0-9a-f]{64})$", expected)
        if not match:
            raise InvariantError(
                "expected Agent outbox build identifier must end in sha256:<digest>"
            )
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != match.group(1):
            raise InvariantError("local recovery outbox helper digest drift")
        return actual

    def _reviewed_outbox_module(self, path: Path) -> ModuleType:
        cached = getattr(self, "_reviewed_outbox_cache", None)
        if isinstance(cached, ModuleType):
            self._prove_local_outbox_helper(path)
            return cached
        self._prove_local_outbox_helper(path)
        stubs = {
            "config": ModuleType("config"),
            "db": ModuleType("db"),
            "hindsight": ModuleType("hindsight"),
        }
        stubs["config"].Settings = object  # type: ignore[attr-defined]
        stubs["config"].load_settings = lambda: None  # type: ignore[attr-defined]
        stubs["db"].connect = lambda *_args, **_kwargs: (_ for _ in ()).throw(  # type: ignore[attr-defined]
            InvariantError("reviewed recovery helper may not open its own DB")
        )
        stubs["hindsight"].HindsightClient = object  # type: ignore[attr-defined]
        stubs["hindsight"].create_hindsight_client = lambda *_a, **_k: None  # type: ignore[attr-defined]
        previous = {name: sys.modules.get(name) for name in stubs}
        try:
            sys.modules.update(stubs)
            spec = importlib.util.spec_from_file_location(
                "hindsight_recovery_reviewed_outbox", path
            )
            if not spec or not spec.loader:
                raise InvariantError("could not load reviewed outbox helper")
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
        finally:
            for name, value in previous.items():
                if value is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = value
        self._reviewed_outbox_cache = module
        return module

    def agent_dsn(self, ports: Iterable[int] | None = None) -> str:
        selected = tuple(ports or self.cfg.pg_local_ports)
        password = self.agent_env["APP_DB_PASSWORD"]
        hosts = ",".join("127.0.0.1" for _ in selected)
        local_ports = ",".join(str(port) for port in selected)
        return (
            f"postgresql://agent_sessions:{password}@/agent_sessions?"
            f"host={hosts}&port={local_ports}&target_session_attrs=read-write&connect_timeout=2"
        )

    @contextlib.contextmanager
    def bank_lock(self):
        conn = self.agent_db()
        conn.commit()
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_try_advisory_lock(hashtextextended('agent-sessions',0)) AS locked"
            )
            if not cur.fetchone()["locked"]:
                conn.close()
                raise InvariantError("could not acquire Agent bank advisory lock")
        try:
            yield conn
        finally:
            with contextlib.suppress(Exception):
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT pg_advisory_unlock(hashtextextended('agent-sessions',0))"
                    )
            conn.close()

    @contextlib.contextmanager
    def incident_bank_mutation(self, document_id: str, recovery_kind: str):
        """Pulse immediately around one bounded local outbox mutation."""
        fields = {
            "document_id": document_id,
            "recovery_kind": recovery_kind,
        }
        self.touch_controller(
            "incident_recovery", incident_stage="bank_mutation_before", **fields
        )
        try:
            with self.bank_lock() as conn:
                yield conn
        finally:
            self.touch_controller(
                "incident_recovery", incident_stage="bank_mutation_after", **fields
            )

    def ensure_pocket_open(self, reason: str) -> None:
        """Persist Pocket open and prove that it has no active/error work."""
        with self.pocket_db() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(
                """INSERT INTO hindsight_sync_state(
                       bank_id,circuit_open,reason,opened_at,updated_at
                   ) VALUES('dans-pocket',true,%s,now(),now())
                   ON CONFLICT(bank_id) DO UPDATE SET
                     circuit_open=true,reason=EXCLUDED.reason,
                     opened_at=COALESCE(hindsight_sync_state.opened_at,now()),
                     updated_at=now()""",
                (reason[:1000],),
            )
        # This audit is deliberately a second transaction: a failure must not
        # roll back the fail-close write above.
        with self.pocket_db() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT count(*)::int AS n FROM hindsight_outbox
                   WHERE state=ANY(%s)""",
                (list(ACTIVE_STATES + ERROR_STATES),),
            )
            if int(cur.fetchone()["n"]):
                raise InvariantError(
                    "Pocket has active/error work after its circuit was opened"
                )
        event(self.cfg, "pocket_open_confirmed", reason=reason[:1000])

    def persist_agent_open(self, reason: str) -> None:
        """Open Agent through the shared path after startup's independent hold."""
        with self.agent_db() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(
                """INSERT INTO hindsight_sync_state(
                       bank_id,circuit_open,reason,opened_at,updated_at
                   ) VALUES('agent-sessions',true,%s,now(),now())
                   ON CONFLICT(bank_id) DO UPDATE SET
                     circuit_open=true,reason=EXCLUDED.reason,
                     opened_at=COALESCE(hindsight_sync_state.opened_at,now()),
                     updated_at=now()""",
                (reason[:1000],),
            )
        event(self.cfg, "agent_open_confirmed", reason=reason[:1000])

    def fugu_failure_manifest(self) -> list[dict[str, Any]]:
        """Return the exact, identity-bearing failed Fugu request manifest."""
        with self.fugu_db() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT *
                   FROM fugu_credit_ledger
                   WHERE start_time >= '2026-07-13 10:17:39+00'
                     AND status <> 'success'
                   ORDER BY start_time,request_id"""
            )
            rows = list(cur.fetchall())
        return [
            _exact_record(
                row,
                "request_id",
                "status",
                "http_status",
                "requested_model",
                "session_id",
                "agent_id",
                "agent_role",
            )
            for row in rows
        ]

    def fugu_session_manifest(self, session_ids: set[str]) -> list[dict[str, Any]]:
        if not session_ids:
            return []
        with self.fugu_db() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT * FROM fugu_credit_ledger
                   WHERE session_id=ANY(%s::text[])
                   ORDER BY session_id,request_id""",
                (sorted(session_ids),),
            )
            rows = list(cur.fetchall())
        return [
            _exact_record(
                row,
                "request_id",
                "session_id",
                "status",
                "http_status",
                "requested_model",
                "agent_id",
            )
            for row in rows
        ]

    def operation_inventory(self) -> dict[str, Any]:
        """Prove every Agent-bank retain parent/child and consolidation row.

        Terminal parent/child rows enter the returned manifest only as a
        complete pair.  This avoids accepting a child that happens to commit
        before its parent during normal asynchronous processing.  Active pairs
        are still lineage-validated but are not checkpoint candidates.
        """
        failures: list[str] = []
        with self.agent_db() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT document_id,state,operation_id::text,payload_hash,
                          submitted_payload_hash,item
                   FROM hindsight_outbox ORDER BY document_id"""
            )
            local_rows = list(cur.fetchall())
            cur.execute(
                """SELECT document_id,operation_id::text
                   FROM hindsight_outbox_retry_history
                   WHERE operation_id IS NOT NULL ORDER BY id"""
            )
            retry_rows = list(cur.fetchall())
        local = {str(row["document_id"]): row for row in local_rows}
        retry_pairs = {
            (str(row["document_id"]), str(row["operation_id"])) for row in retry_rows
        }
        with self.hindsight_db() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT operation_id::text,operation_type,status,error_message,
                          result_metadata,task_payload,created_at,updated_at
                   FROM async_operations
                   WHERE bank_id='agent-sessions'
                   ORDER BY created_at,operation_id"""
            )
            operations = list(cur.fetchall())
        by_id: dict[str, dict[str, Any]] = {}
        for operation in operations:
            operation_id = str(operation.get("operation_id") or "")
            operation_type = str(operation.get("operation_type") or "").lower()
            status = str(operation.get("status") or "").lower()
            if (
                not operation_id
                or operation_id in by_id
                or operation_type not in {"batch_retain", "retain", "consolidation"}
                or status
                not in {
                    "pending",
                    "processing",
                    "completed",
                    "failed",
                    "cancelled",
                }
            ):
                failures.append(
                    f"unknown/duplicate Hindsight operation identity {operation_id}"
                )
                continue
            by_id[operation_id] = operation

        terminal_statuses = {"completed", "failed", "cancelled"}
        eligible: list[tuple[Any, str, dict[str, Any]]] = []
        completed_by_document: Counter[str] = Counter()
        failed_pairs: list[dict[str, Any]] = []
        children_by_parent: dict[str, list[dict[str, Any]]] = {}
        for operation in operations:
            if str(operation.get("operation_type") or "").lower() != "retain":
                continue
            operation_id = str(operation.get("operation_id") or "")
            status = str(operation.get("status") or "").lower()
            payload = dict(operation.get("task_payload") or {})
            metadata = dict(operation.get("result_metadata") or {})
            items = _canonical_operation_items(payload)
            document_ids = metadata.get("document_ids")
            parent_id = str(metadata.get("parent_operation_id") or "")
            document_id = (
                str(items[0].get("document_id") or "") if len(items) == 1 else ""
            )
            local_row = local.get(document_id)
            child_items_count = metadata.get("items_count")
            sub_batch_index = metadata.get("sub_batch_index")
            total_sub_batches = metadata.get("total_sub_batches")
            # Hindsight creates a pending child with its immutable fan-out
            # metadata and exact payload, then adds document_ids as mutable
            # retain outcome metadata later.  A full audit can therefore see
            # one legitimate active child before document_ids exists.  Permit
            # only that exact absence window: a present-but-wrong value still
            # fails, and every terminal child must carry the exact list.
            active_document_ids_pending = (
                status in {"pending", "processing"}
                and "document_ids" not in metadata
                and payload.get("type") == "batch_retain"
                and type(child_items_count) is int
                and child_items_count == 1
                and type(sub_batch_index) is int
                and sub_batch_index == 1
                and type(total_sub_batches) is int
                and total_sub_batches == 1
            )
            if (
                local_row is None
                or payload.get("bank_id") != "agent-sessions"
                or str(payload.get("operation_id") or "") != operation_id
                or (document_ids != [document_id] and not active_document_ids_pending)
                or not parent_id
                or str(local_row["payload_hash"] or "")
                != hashlib.sha256(
                    json.dumps(
                        items[0],
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ).encode()
                ).hexdigest()
            ):
                failures.append(
                    f"retain child operation lineage/hash drift {operation_id}"
                )
                continue
            parent = by_id.get(parent_id)
            if (
                parent is None
                or str(parent.get("operation_type") or "").lower() != "batch_retain"
            ):
                failures.append(
                    f"retain child has no exact batch parent {operation_id}"
                )
                continue
            children_by_parent.setdefault(parent_id, []).append(operation)

        for operation in operations:
            operation_id = str(operation.get("operation_id") or "")
            operation_type = str(operation.get("operation_type") or "").lower()
            status = str(operation.get("status") or "").lower()
            if operation_type == "consolidation":
                payload = dict(operation.get("task_payload") or {})
                if (
                    payload.get("bank_id") != "agent-sessions"
                    or payload.get("type") != "consolidation"
                    or str(payload.get("operation_id") or "") != operation_id
                ):
                    failures.append(
                        f"consolidation operation provenance drift {operation_id}"
                    )
                elif status in terminal_statuses:
                    eligible.append(
                        (operation.get("created_at"), operation_id, operation)
                    )
                continue
            if operation_type != "batch_retain":
                continue
            children = children_by_parent.get(operation_id, [])
            if len(children) != 1:
                failures.append(
                    f"batch parent does not have exactly one retain child {operation_id}"
                )
                continue
            child = children[0]
            child_status = str(child.get("status") or "").lower()
            child_items = _canonical_operation_items(
                dict(child.get("task_payload") or {})
            )
            document_id = str(child_items[0]["document_id"])
            parent_metadata = dict(operation.get("result_metadata") or {})
            if (
                parent_metadata.get("is_parent") is not True
                or type(parent_metadata.get("items_count")) is not int
                or parent_metadata.get("items_count") != 1
                or type(parent_metadata.get("num_sub_batches")) is not int
                or parent_metadata.get("num_sub_batches") != 1
            ):
                failures.append(
                    f"batch parent metadata provenance drift {operation_id}"
                )
                continue
            both_terminal = (
                status in terminal_statuses and child_status in terminal_statuses
            )
            if not both_terminal:
                # Parent/child status commits are not atomic.  A mixed
                # terminal/active pair is valid only while at least one side
                # remains genuinely active and is never checkpointed yet.
                if status not in {"pending", "processing"} and child_status not in {
                    "pending",
                    "processing",
                }:
                    failures.append(
                        f"batch parent/child terminal status contradiction {operation_id}"
                    )
                continue
            if (status == "completed") != (child_status == "completed"):
                failures.append(
                    f"batch parent/child completion status contradiction {operation_id}"
                )
                continue
            local_row = local[document_id]
            if status == "completed":
                completed_by_document[document_id] += 1
                if str(local_row["state"]) not in {"succeeded", "submitted"}:
                    failures.append(
                        f"completed operation pair has invalid local state {document_id}"
                    )
            else:
                failed_pairs.append(
                    {
                        "document_id": document_id,
                        "parent_operation_id": operation_id,
                        "child_operation_id": str(child["operation_id"]),
                        "explained": (
                            (document_id, operation_id) in retry_pairs
                            or str(local_row.get("operation_id") or "") == operation_id
                        ),
                    }
                )
            eligible.extend(
                (
                    (value.get("created_at"), str(value["operation_id"]), value)
                    for value in (operation, child)
                )
            )

        for document_id, row in local.items():
            completed = int(completed_by_document.get(document_id, 0))
            if str(row["state"]) == "succeeded" and completed != 1:
                failures.append(
                    f"succeeded document lacks exactly one completed pair {document_id}"
                )
            if str(row["state"]) == "queued" and completed:
                failures.append(
                    f"queued document has a completed operation pair {document_id}"
                )
            if completed > 1:
                failures.append(
                    f"document has multiple completed operation pairs {document_id}"
                )

        eligible.sort(key=lambda value: (str(value[0]), value[1]))
        manifest = [
            _exact_record(
                operation,
                "operation_id",
                "operation_type",
                "status",
            )
            for _created_at, _operation_id, operation in eligible
        ]
        return {
            "manifest": manifest,
            "failures": failures,
            "failed_pairs": failed_pairs,
            "completed_by_document": dict(completed_by_document),
        }

    def prove_remote_document_set_exact(self) -> None:
        """Prove every present Agent document against its canonical outbox row."""
        with self.agent_db() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT document_id,state,item FROM hindsight_outbox
                   ORDER BY document_id"""
            )
            local_rows = list(cur.fetchall())
        with self.hindsight_db() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT id,original_text,content_hash,retain_params,tags
                   FROM documents WHERE bank_id='agent-sessions'
                   ORDER BY id"""
            )
            remote_rows = list(cur.fetchall())
        local = {str(row["document_id"]): row for row in local_rows}
        expected = {
            str(row["document_id"])
            for row in local_rows
            if str(row["state"]) in {"succeeded", "submitting", "submitted"}
        }
        observed = {str(row["id"]) for row in remote_rows}
        if observed != expected or len(remote_rows) != len(observed):
            raise InvariantError(
                "remote document set is not exact for current Agent states"
            )
        for remote in remote_rows:
            row = local[str(remote["id"])]
            if not self._remote_document_matches_item(remote, dict(row["item"])):
                raise InvariantError(
                    f"remote document provenance drift for {remote['id']}"
                )

    def observed_baselines(self) -> dict[str, Any]:
        with self.agent_db() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM hindsight_outbox_retry_history ORDER BY id")
            retry_rows = list(cur.fetchall())
            cur.execute(
                "SELECT * FROM hindsight_outbox_claim_recovery_history ORDER BY id"
            )
            claim_rows = list(cur.fetchall())
            cur.execute(
                "SELECT * FROM hindsight_outbox_operation_adoption_history ORDER BY id"
            )
            adoption_rows = list(cur.fetchall())
        with self.pocket_db() as conn, conn.cursor() as cur:
            pocket_counts = self._counts(cur)
        with self.hindsight_db() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT operation_id::text,operation_type,status,error_message,
                          result_metadata,task_payload,created_at,updated_at
                   FROM async_operations
                   WHERE bank_id='agent-sessions' AND status='failed'
                   ORDER BY created_at,operation_id"""
            )
            hindsight_rows = list(cur.fetchall())
        with self.fugu_db() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT *
                   FROM fugu_credit_ledger
                   WHERE start_time >= '2026-07-13 10:17:39+00'
                     AND status <> 'success'
                   ORDER BY start_time,request_id"""
            )
            fugu_rows = list(cur.fetchall())
            fugu_failures = sorted(
                [
                    [
                        row["start_time"].isoformat(),
                        _canonical_http_status(row["http_status"]),
                        str(row["requested_model"]),
                    ]
                    for row in fugu_rows
                ]
            )
        provider_status = self.budget_recovery_status()
        provider_failure_manifest = list(
            provider_status.get("provider_failure_manifest") or []
        )
        operation_inventory = self.operation_inventory()
        if operation_inventory["failures"]:
            raise InvariantError("; ".join(operation_inventory["failures"]))
        return {
            "pocket_counts": pocket_counts,
            "operation_manifest": operation_inventory["manifest"],
            "fugu_failures": fugu_failures,
            "fugu_failure_manifest": [
                _exact_record(
                    row,
                    "request_id",
                    "status",
                    "http_status",
                    "requested_model",
                    "session_id",
                    "agent_id",
                    "agent_role",
                )
                for row in fugu_rows
            ],
            "provider_failure_manifest": provider_failure_manifest,
            "hindsight_failed": len(hindsight_rows),
            "histories": {
                "retry": len(retry_rows),
                "claim": len(claim_rows),
                "adoption": len(adoption_rows),
            },
            "hindsight_failures": [
                {
                    "operation_id": str(row["operation_id"]),
                    "operation_type": str(row["operation_type"]),
                    "error_sha256": hashlib.sha256(
                        str(row["error_message"] or "").encode()
                    ).hexdigest(),
                    "created_at": row["created_at"].isoformat(),
                }
                for row in hindsight_rows
            ],
            "exact_hindsight_failures": [
                _exact_record(
                    row,
                    "operation_id",
                    "operation_type",
                    "status",
                )
                for row in hindsight_rows
            ],
            "history_manifests": {
                "retry": [
                    {
                        "id": int(row["id"]),
                        "document_id": str(row["document_id"]),
                        "operation_id": (
                            str(row["operation_id"]) if row["operation_id"] else None
                        ),
                        "previous_state": str(row["previous_state"]),
                        "payload_hash": str(row["payload_hash"]),
                        "recorded_at": row["recorded_at"].isoformat(),
                    }
                    for row in retry_rows
                ],
                "claim": [
                    {
                        "id": int(row["id"]),
                        "document_id": str(row["document_id"]),
                        "payload_hash": str(row["payload_hash"]),
                        "previous_claimed_at": row["previous_claimed_at"].isoformat(),
                        "recorded_at": row["recorded_at"].isoformat(),
                    }
                    for row in claim_rows
                ],
                "adoption": [
                    {
                        "id": int(row["id"]),
                        "document_id": str(row["document_id"]),
                        "operation_id": str(row["operation_id"]),
                        "payload_hash": str(row["payload_hash"]),
                        "remote_status": str(row["remote_status"]),
                        "remote_document_present": bool(row["remote_document_present"]),
                        "recorded_at": row["recorded_at"].isoformat(),
                    }
                    for row in adoption_rows
                ],
            },
            "exact_history_manifests": {
                "retry": [
                    _exact_record(
                        row,
                        "id",
                        "document_id",
                        "operation_id",
                        "previous_state",
                        "payload_hash",
                        "submitted_payload_hash",
                        "retry_reason",
                    )
                    for row in retry_rows
                ],
                "claim": [
                    _exact_record(
                        row,
                        "id",
                        "document_id",
                        "payload_hash",
                        "previous_claimed_at",
                        "recovery_reason",
                    )
                    for row in claim_rows
                ],
                "adoption": [
                    _exact_record(
                        row,
                        "id",
                        "document_id",
                        "operation_id",
                        "previous_state",
                        "payload_hash",
                        "submitted_payload_hash",
                        "remote_status",
                        "remote_document_present",
                        "adoption_reason",
                    )
                    for row in adoption_rows
                ],
            },
        }

    def ensure_checkpoint_manifests(
        self, phase: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        checkpoint = load_checkpoint(self.cfg, phase)
        accepted = checkpoint["accepted"]
        if checkpoint.get("bootstrap_pending"):
            observed = self.observed_baselines()
            candidate = json.loads(json.dumps(checkpoint))
            candidate["accepted"] = observed
            candidate["bootstrap_pending"] = False
            return append_checkpoint(
                self.cfg,
                checkpoint,
                candidate,
                reason="bootstrapped append-only checkpoint from held authoritative state",
            )
        if (
            accepted.get("hindsight_failures") is not None
            and accepted.get("history_manifests") is not None
            and accepted.get("fugu_failure_manifest") is not None
            and accepted.get("provider_failure_manifest") is not None
            and accepted.get("exact_hindsight_failures") is not None
            and accepted.get("exact_history_manifests") is not None
            and accepted.get("pocket_counts") is not None
            and accepted.get("operation_manifest") is not None
        ):
            return checkpoint
        observed = self.observed_baselines()
        for key in ("fugu_failures", "hindsight_failed", "histories"):
            if observed[key] != accepted[key]:
                raise InvariantError(
                    f"cannot bootstrap exact checkpoint manifests: {key} drift"
                )
        for key in ("hindsight_failures", "history_manifests"):
            if accepted.get(key) is not None and observed[key] != accepted[key]:
                raise InvariantError(
                    f"cannot migrate exact checkpoint manifests: {key} drift"
                )
        candidate = json.loads(json.dumps(checkpoint))
        # Preserve every already accepted legacy summary byte-for-byte while
        # filling only previously absent exact manifests.
        candidate["accepted"].update(
            {
                key: observed[key]
                for key in (
                    "hindsight_failures",
                    "history_manifests",
                    "fugu_failure_manifest",
                    "provider_failure_manifest",
                    "exact_hindsight_failures",
                    "exact_history_manifests",
                    "pocket_counts",
                    "operation_manifest",
                )
                if candidate["accepted"].get(key) is None
            }
        )
        return append_checkpoint(
            self.cfg,
            checkpoint,
            candidate,
            reason="bootstrapped exact operation/history manifests",
        )

    def operation_inventory_needs_acceptance(
        self,
        snap: dict[str, Any],
    ) -> bool:
        """Use terminal counts to avoid rebuilding an unchanged manifest.

        Exact identity/provenance remains enforced by the first/last full
        audits and by accept_drained_operation_inventory() whenever the
        append-only terminal count advances.
        """
        checkpoint = read_checkpoint(self.cfg)
        manifest = checkpoint["accepted"].get("operation_manifest")
        if not isinstance(manifest, list):
            return True
        operations = snap.get("hindsight_operations")
        if not isinstance(operations, dict):
            raise InvariantError("Hindsight operation counts are absent")
        observed_terminal = sum(
            int(operations.get(status, 0) or 0)
            for status in ("completed", "failed", "cancelled")
        )
        accepted_terminal = len(manifest)
        if observed_terminal < accepted_terminal:
            raise InvariantError(
                "terminal Hindsight operation count regressed below checkpoint"
            )
        return observed_terminal > accepted_terminal

    def accept_drained_operation_inventory(self) -> bool:
        def serialized_acceptance() -> bool:
            with _exclusive_lock(self.cfg.authoritative_read_lock_file):
                return self._accept_drained_operation_inventory_unlocked()

        if not getattr(self, "controller_owned", False):
            return serialized_acceptance()
        pulse_seconds = max(
            min(self.cfg.heartbeat_max_age / 3, 5.0),
            0.5,
        )
        with _BoundedAuditHeartbeat(
            self.cfg.controller_file,
            "controller",
            deadline_seconds=self.cfg.heartbeat_max_age * 4,
            pulse_seconds=pulse_seconds,
            fields={"status": "operation_inventory_acceptance"},
        ):
            return serialized_acceptance()

    def _accept_drained_operation_inventory_unlocked(self) -> bool:
        """Append newly proven normal terminal operations while fully held.

        Failed/cancelled retain pairs always require a document incident.
        A failed consolidation may be accepted without a duplicate retain only
        when every currently present document remains exact and no provider,
        Fugu, history, Pocket, or other failure baseline changed with it.
        """
        checkpoint = self.ensure_checkpoint_manifests()
        inventory = self.operation_inventory()
        if inventory["failures"]:
            raise InvariantError("; ".join(inventory["failures"]))
        old = checkpoint["accepted"]["operation_manifest"] or []
        current = inventory["manifest"]
        if current[: len(old)] != old:
            raise InvariantError("terminal operation inventory provenance drift")
        additions = current[len(old) :]
        if not additions:
            return False
        failed = [
            value
            for value in additions
            if str(value.get("status") or "") in {"failed", "cancelled"}
        ]
        if any(
            str(value.get("operation_type") or "") != "consolidation"
            for value in failed
        ):
            raise InvariantError(
                "failed retain operation inventory requires exact row incident"
            )
        self.prove_remote_document_set_exact()
        observed = self.observed_baselines()
        accepted = checkpoint["accepted"]
        for key in (
            "fugu_failures",
            "fugu_failure_manifest",
            "provider_failure_manifest",
            "histories",
            "history_manifests",
            "exact_history_manifests",
            "pocket_counts",
        ):
            if observed[key] != accepted[key]:
                raise InvariantError(
                    f"normal operation acceptance observed unrelated {key} drift"
                )
        old_failed = accepted["exact_hindsight_failures"] or []
        new_failed = observed["exact_hindsight_failures"]
        if new_failed[: len(old_failed)] != old_failed:
            raise InvariantError(
                "normal operation acceptance observed failure provenance drift"
            )
        failure_additions = new_failed[len(old_failed) :]
        expected_failed_ids = {str(value["operation_id"]) for value in failed}
        if {
            str(value["operation_id"]) for value in failure_additions
        } != expected_failed_ids:
            raise InvariantError(
                "consolidation failure set is not the exact inventory delta"
            )
        candidate = json.loads(json.dumps(checkpoint))
        candidate["accepted"]["operation_manifest"] = current
        if failed:
            candidate["accepted"]["hindsight_failed"] = observed["hindsight_failed"]
            candidate["accepted"]["hindsight_failures"] = observed["hindsight_failures"]
            candidate["accepted"]["exact_hindsight_failures"] = new_failed
        append_checkpoint(
            self.cfg,
            checkpoint,
            candidate,
            reason=(
                "accepted exact drained consolidation terminal inventory"
                if failed
                else "accepted exact drained completed operation inventory"
            ),
        )
        return True

    def authorize_hindsight_incident_drift(
        self,
        incident: dict[str, Any],
        rows: list[dict[str, Any]],
        failures: list[str],
    ) -> list[str]:
        """Prove the exact failed-operation delta before row recovery.

        This is deliberately read-only with respect to Agent/Hindsight.  It
        persists only the incident authorization envelope so a controller
        crash cannot widen the set of terminal operations later accepted by
        the retry transaction.
        """
        if "Hindsight failed-operation count drift" not in failures:
            return failures
        authorization = self.prepare_incident_authorization(incident, rows)
        checkpoint = self.ensure_checkpoint_manifests()
        observed = self.observed_baselines()
        return authorized_hindsight_incident_failures(
            failures,
            accepted_failures=list(
                checkpoint["accepted"].get("exact_hindsight_failures") or []
            ),
            observed_failures=list(observed["exact_hindsight_failures"]),
            accepted_operations=list(
                checkpoint["accepted"].get("operation_manifest") or []
            ),
            observed_operations=list(observed["operation_manifest"]),
            authorized_operation_ids={
                str(value)
                for value in authorization.get("authorized_failure_operation_ids", [])
            },
        )

    def accept_recovered_attempt(self, attempt: dict[str, Any]) -> dict[str, Any]:
        """Append one recovery while bounding its authoritative acceptance scan."""
        if not getattr(self, "controller_owned", False):
            return self._accept_recovered_attempt(attempt)
        with _BoundedAuditHeartbeat(
            self.cfg.controller_file,
            "controller",
            deadline_seconds=self.cfg.heartbeat_max_age * 4,
            pulse_seconds=min(self.cfg.heartbeat_max_age / 3, 5.0),
            fields={
                "status": "incident_recovery",
                "incident_stage": "acceptance",
                "document_id": str(attempt.get("document_id") or ""),
                "recovery_kind": str(attempt.get("kind") or ""),
            },
        ):
            return self._accept_recovered_attempt(attempt)

    def _accept_recovered_attempt(
        self, attempt: dict[str, Any]
    ) -> dict[str, Any]:
        """Append exactly one proven recovery and its observed monotonic baselines."""
        checkpoint = self.ensure_checkpoint_manifests()
        key = str(attempt.get("key") or "")
        if not key:
            raise InvariantError("recovered attempt omitted its durable key")
        existing = {
            str(value["key"]): value for value in checkpoint["recovered_attempts"]
        }
        observed = self.observed_baselines()
        if key in existing:
            if checkpoint["accepted"] != observed:
                raise InvariantError(
                    "an already accepted recovery key has different observed baselines"
                )
            phase = load_phase(self.cfg)
            expected_retry = int(observed["histories"]["retry"])
            if int(phase.get("expected_retry", -1)) != expected_retry:
                phase["expected_retry"] = expected_retry
                save_phase(self.cfg, phase)
            return checkpoint
        old_fugu = checkpoint["accepted"]["fugu_failure_manifest"] or []
        new_fugu = observed["fugu_failure_manifest"]
        new_fugu_by_id = {str(value["request_id"]): value for value in new_fugu}
        if any(
            new_fugu_by_id.get(str(value["request_id"])) != value for value in old_fugu
        ):
            raise InvariantError("observed Fugu ledger is not an append-only extension")
        allowed_statuses = {
            int(value) for value in attempt.get("allowed_http_statuses", [])
        }
        allow_null_status = bool(attempt.get("allow_null_http_status"))
        old_fugu_ids = {str(value["request_id"]) for value in old_fugu}
        additions = [
            value for value in new_fugu if str(value["request_id"]) not in old_fugu_ids
        ]
        authorized_fugu = {
            str(value["request_id"]): value
            for value in attempt.get("authorized_fugu_failures", [])
            if isinstance(value, dict) and value.get("request_id")
        }
        authorized_ids = {
            str(value) for value in attempt.get("authorized_fugu_request_ids", [])
        }
        if authorized_ids != set(authorized_fugu):
            raise InvariantError(
                "recovered attempt Fugu ID/record authorization is inconsistent"
            )
        if additions and any(
            authorized_fugu.get(str(value["request_id"])) != value
            for value in additions
        ):
            raise InvariantError(
                "new Fugu failures are not exact request-ID/provenance authorized"
            )
        if additions and (
            (not allowed_statuses and not allow_null_status)
            or any(
                (value.get("http_status") is None and not allow_null_status)
                or (
                    value.get("http_status") is not None
                    and int(value["http_status"]) not in allowed_statuses
                )
                for value in additions
            )
        ):
            raise InvariantError(
                "new Fugu failures have an unauthorized failure classification"
            )
        old_provider = checkpoint["accepted"].get("provider_failure_manifest") or []
        new_provider = observed.get("provider_failure_manifest") or []
        new_provider_by_id = {str(value["request_id"]): value for value in new_provider}
        if any(
            new_provider_by_id.get(str(value["request_id"])) != value
            for value in old_provider
        ):
            raise InvariantError(
                "observed budget provider failure history is not append-only"
            )
        old_provider_ids = {str(value["request_id"]) for value in old_provider}
        provider_additions = [
            value
            for value in new_provider
            if str(value["request_id"]) not in old_provider_ids
        ]
        authorized_provider = {
            str(value["request_id"]): value
            for value in attempt.get("authorized_provider_failures", [])
            if isinstance(value, dict) and value.get("request_id")
        }
        authorized_provider_ids = {
            str(value) for value in attempt.get("authorized_provider_failure_ids", [])
        }
        if authorized_provider_ids != set(authorized_provider):
            raise InvariantError(
                "recovered attempt provider failure ID/record authorization is inconsistent"
            )
        if provider_additions and any(
            authorized_provider.get(str(value["request_id"])) != value
            for value in provider_additions
        ):
            raise InvariantError(
                "new budget provider failures are not exact incident-authorized"
            )
        old_hindsight = checkpoint["accepted"]["exact_hindsight_failures"] or []
        new_hindsight = observed["exact_hindsight_failures"]
        new_hindsight_by_id = {
            str(value["operation_id"]): value for value in new_hindsight
        }
        if any(
            new_hindsight_by_id.get(str(value["operation_id"])) != value
            for value in old_hindsight
        ):
            raise InvariantError(
                "observed Hindsight failure manifest is not append-only"
            )
        allowed_operations = {
            str(value) for value in attempt.get("authorized_failure_operation_ids", [])
        }
        old_hindsight_ids = {str(value["operation_id"]) for value in old_hindsight}
        hindsight_additions = [
            value
            for value in new_hindsight
            if str(value["operation_id"]) not in old_hindsight_ids
        ]
        if hindsight_additions and (
            not allowed_operations
            or any(
                str(value["operation_id"]) not in allowed_operations
                for value in hindsight_additions
            )
        ):
            raise InvariantError(
                "new Hindsight failures are not tied to the exact recovered lineage"
            )
        expected_history_delta = {
            name: int((attempt.get("expected_history_delta") or {}).get(name, 0))
            for name in ("retry", "claim", "adoption")
        }
        old_manifests = checkpoint["accepted"]["exact_history_manifests"] or {
            "retry": [],
            "claim": [],
            "adoption": [],
        }
        new_manifests = observed["exact_history_manifests"]
        for name in ("retry", "claim", "adoption"):
            old_values = old_manifests[name]
            new_values = new_manifests[name]
            new_by_id = {int(value["id"]): value for value in new_values}
            if any(new_by_id.get(int(value["id"])) != value for value in old_values):
                raise InvariantError(f"observed {name} history is not append-only")
            old_ids = {int(value["id"]) for value in old_values}
            history_additions = [
                value for value in new_values if int(value["id"]) not in old_ids
            ]
            if len(history_additions) != expected_history_delta[name]:
                raise InvariantError(
                    f"unexpected {name} history delta for recovered attempt"
                )
            if any(
                str(value["document_id"]) != str(attempt.get("document_id"))
                for value in history_additions
            ):
                raise InvariantError(
                    f"{name} history delta belongs to another document"
                )
            for value in history_additions:
                if str(value.get("payload_hash") or "") != str(
                    attempt.get("payload_hash") or ""
                ):
                    raise InvariantError(
                        f"{name} history payload is not the authorized attempt"
                    )
                if name == "retry" and (
                    str(value.get("operation_id") or "")
                    != str(attempt.get("operation_id") or "")
                    or str(value.get("previous_state") or "")
                    not in {"failed", "blocked"}
                    or str(value.get("retry_reason") or "")
                    != "controller retry after exact terminal failure proof"
                ):
                    raise InvariantError(
                        "retry history identity/reason is not exactly authorized"
                    )
                if name == "adoption" and (
                    str(value.get("operation_id") or "")
                    != str(attempt.get("operation_id") or "")
                    or str(value.get("previous_state") or "")
                    not in {"submitting", "blocked"}
                    or str(value.get("remote_status") or "")
                    != str(attempt.get("remote_status") or "")
                    or bool(value.get("remote_document_present"))
                    != bool(attempt.get("remote_document_present"))
                    or str(value.get("adoption_reason") or "")
                    != str(attempt.get("adoption_reason") or "")
                ):
                    raise InvariantError(
                        "adoption history lineage is not exactly authorized"
                    )
                if name == "claim":
                    previous_claimed_at = _canonical_datetime_text(
                        value.get("previous_claimed_at")
                    )
                    expected_claimed_at = _canonical_datetime_text(
                        attempt.get("claimed_at")
                    )
                    reason = str(value.get("recovery_reason") or "")
                    proof_times = [
                        datetime.fromtimestamp(
                            float(proof["checked_at"]), timezone.utc
                        ).isoformat()
                        for proof in attempt.get("proofs", [])
                    ]
                    if (
                        previous_claimed_at != expected_claimed_at
                        or len(proof_times) != 2
                        or any(timestamp not in reason for timestamp in proof_times)
                    ):
                        raise InvariantError(
                            "claim history timestamp/proof lineage is not authorized"
                        )
        old_operations = checkpoint["accepted"].get("operation_manifest") or []
        new_operations = observed.get("operation_manifest") or []
        if new_operations[: len(old_operations)] != old_operations:
            raise InvariantError(
                "observed terminal operation inventory is not append-only"
            )
        operation_additions = new_operations[len(old_operations) :]
        failed_operation_additions = {
            str(value["operation_id"])
            for value in operation_additions
            if str(value.get("status") or "") in {"failed", "cancelled"}
        }
        if failed_operation_additions - allowed_operations:
            raise InvariantError(
                "new terminal operation failures are not incident-authorized"
            )
        candidate = json.loads(json.dumps(checkpoint))
        candidate["accepted"] = observed
        candidate["recovered_attempts"].append(
            {**attempt, "key": key, "accepted_at": time.time()}
        )
        accepted = append_checkpoint(
            self.cfg,
            checkpoint,
            candidate,
            reason=f"accepted recovered attempt {key}",
        )
        phase = load_phase(self.cfg)
        phase["expected_retry"] = int(observed["histories"]["retry"])
        save_phase(self.cfg, phase)
        return accepted

    def mark_pending_acceptance(
        self, incident: dict[str, Any], attempt: dict[str, Any]
    ) -> None:
        incident["pending_acceptance"] = attempt
        incident["status"] = "recovering"
        save_incident(self.cfg, incident, kind="recovery_attempt_prepared")

    def clear_pending_acceptance(self, incident: dict[str, Any]) -> None:
        incident.pop("pending_acceptance", None)
        save_incident(self.cfg, incident, kind="recovery_attempt_accepted")

    def resume_pending_acceptance(self, incident: dict[str, Any]) -> bool:
        """Resume every exact mutation/checkpoint crash boundary idempotently."""
        attempt = incident.get("pending_acceptance")
        if not isinstance(attempt, dict):
            return False
        document_id = str(attempt.get("document_id") or "")
        operation_id = str(attempt.get("operation_id") or "")
        kind = str(attempt.get("kind") or "")
        expected_payload_hash = str(attempt.get("payload_hash") or "")
        self.touch_controller(
            "incident_recovery",
            incident_stage="row_proof",
            document_id=document_id,
            recovery_kind=kind or "pending_acceptance",
        )
        with self.agent_db() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT state,payload_hash,submitted_payload_hash,
                          operation_id::text,item,claimed_at
                   FROM hindsight_outbox WHERE document_id=%s""",
                (document_id,),
            )
            row = cur.fetchone()
            if not row:
                raise InvariantError("pending recovery document disappeared")
            if (
                not expected_payload_hash
                or row["payload_hash"] != expected_payload_hash
            ):
                raise InvariantError("pending recovery payload hash drift")
        item = dict(row["item"])
        outbox = self._reviewed_outbox_module(
            Path.home() / "agent-session-mcp/server/outbox.py"
        )
        worker = self._app_worker()
        completed_lineage_proof: Any | None = None
        terminal_lineage_proof: Any | None = None

        if kind in {"completed_refresh", "operation_adoption", "terminal_retry"}:
            if not operation_id:
                raise InvariantError("pending operation recovery lacks operation ID")
            operation = self._remote_operation(operation_id)
            if not operation:
                raise InvariantError("pending operation lineage/hash proof drift")
            remote_status = str(operation.get("status") or "").lower()
            if kind == "completed_refresh" and remote_status == "completed":
                _parent, child = self._prove_completed_batch_parent(
                    {**row, "document_id": document_id}, operation
                )
                child_operation_id = str(child.get("operation_id") or "")
                if not child_operation_id:
                    raise InvariantError(
                        "pending completed recovery child lacks operation ID"
                    )
                persisted_child = str(attempt.get("child_operation_id") or "")
                if persisted_child and persisted_child != child_operation_id:
                    raise InvariantError(
                        "pending completed recovery child lineage drift"
                    )
                if not persisted_child:
                    # Upgrade a durable attempt created before completed-child
                    # lineage was persisted.  The exact unique child was just
                    # re-proved and is durably recorded before any mutation.
                    attempt["child_operation_id"] = child_operation_id
                    self.mark_pending_acceptance(incident, attempt)
                completed_lineage_proof = outbox.CompletedLineageProof(
                    document_id=document_id,
                    parent_operation_id=operation_id,
                    child_operation_id=child_operation_id,
                    payload_hash=expected_payload_hash,
                )
            elif kind == "terminal_retry" and remote_status in {
                "failed",
                "cancelled",
            }:
                terminal_lineage = self._terminal_lineage_records(
                    {**row, "document_id": document_id},
                    operation,
                    expected_operation_id=operation_id,
                    allow_cleared_local_operation_id=row["state"] == "queued",
                )
                if not terminal_lineage:
                    raise InvariantError(
                        "pending terminal recovery child is not terminal"
                    )
                terminal_children = [
                    value
                    for value in terminal_lineage
                    if str(value.get("operation_type") or "").lower() == "retain"
                    and str(value.get("status") or "").lower()
                    in {"failed", "cancelled"}
                ]
                if len(terminal_children) != 1:
                    raise InvariantError(
                        "pending terminal recovery child lineage is not exact"
                    )
                child_operation_id = str(terminal_children[0].get("operation_id") or "")
                persisted_child = str(attempt.get("child_operation_id") or "")
                if persisted_child and persisted_child != child_operation_id:
                    raise InvariantError(
                        "pending terminal recovery child lineage drift"
                    )
                if not persisted_child:
                    attempt["child_operation_id"] = child_operation_id
                    self.mark_pending_acceptance(incident, attempt)
                terminal_lineage_proof = outbox.TerminalLineageProof(
                    document_id=document_id,
                    parent_operation_id=operation_id,
                    child_operation_id=child_operation_id,
                    payload_hash=expected_payload_hash,
                )
            elif not operation_matches_outbox(
                operation,
                document_id=document_id,
                payload_hash=expected_payload_hash,
                operation_id=operation_id,
                operation_types=frozenset({"batch_retain"}),
            ):
                raise InvariantError("pending operation lineage/hash proof drift")
            remote_document = self._remote_document(document_id)
            document_present = remote_document is not None

        if kind == "completed_refresh":
            if (
                remote_status != "completed"
                or not document_present
                or not self._remote_document_matches_item(remote_document, item)
            ):
                raise InvariantError("pending completed operation proof drift")
            expected_adoption = int(
                (attempt.get("expected_history_delta") or {}).get("adoption", 0)
            )
            if row["state"] in {"blocked", "submitting"}:
                if expected_adoption != 1:
                    raise InvariantError("unexpected pending completed adoption state")
                with self.incident_bank_mutation(document_id, kind) as conn:
                    if not outbox.adopt_operation(
                        conn,
                        document_id,
                        operation_id,
                        expected_payload_hash=expected_payload_hash,
                        remote_status=remote_status,
                        remote_document_present=True,
                        reason=str(attempt.get("adoption_reason") or ""),
                    ):
                        raise InvariantError(
                            "pending completed adoption did not resume"
                        )
                row = {**row, "state": "submitted", "operation_id": operation_id}
            if row["state"] == "submitted":
                with self.incident_bank_mutation(document_id, kind) as conn:
                    completed, failed = worker._refresh_submitted(
                        conn,
                        only_operations=frozenset({(document_id, operation_id)}),
                        completed_lineage_proofs=frozenset({completed_lineage_proof}),
                    )
                if (completed, failed) != (1, 0):
                    raise InvariantError(
                        "pending completed refresh did not resume exactly"
                    )
            elif row["state"] != "succeeded":
                raise InvariantError(
                    "pending completed recovery has invalid live state"
                )

        elif kind == "terminal_retry":
            if remote_status not in {"failed", "cancelled"} or document_present:
                raise InvariantError("pending terminal operation proof drift")
            provider_proof = attempt.get("provider_recovery_proof")
            if attempt.get("authorized_provider_failure_ids") and not isinstance(
                provider_proof, dict
            ):
                raise InvariantError("pending terminal retry lost provider proof")
            expected_adoption = int(
                (attempt.get("expected_history_delta") or {}).get("adoption", 0)
            )
            if row["state"] in {"blocked", "submitting"}:
                if expected_adoption != 1:
                    raise InvariantError("unexpected pending terminal adoption state")
                with self.incident_bank_mutation(document_id, kind) as conn:
                    if not outbox.adopt_operation(
                        conn,
                        document_id,
                        operation_id,
                        expected_payload_hash=expected_payload_hash,
                        remote_status=remote_status,
                        remote_document_present=False,
                        reason=str(attempt.get("adoption_reason") or ""),
                    ):
                        raise InvariantError("pending terminal adoption did not resume")
                row = {**row, "state": "submitted", "operation_id": operation_id}
            if row["state"] == "submitted":
                with self.incident_bank_mutation(document_id, kind) as conn:
                    completed, failed = worker._refresh_submitted(
                        conn,
                        allowed_terminal_circuit_failures=frozenset(
                            {(document_id, operation_id)}
                        ),
                        only_operations=frozenset({(document_id, operation_id)}),
                        terminal_lineage_proofs=frozenset({terminal_lineage_proof}),
                    )
                if (completed, failed) != (0, 1):
                    raise InvariantError(
                        "pending terminal refresh did not resume exactly"
                    )
                row = {**row, "state": "failed", "operation_id": operation_id}
            if row["state"] in {"failed", "queued"}:
                with self.incident_bank_mutation(document_id, kind) as conn:
                    if not outbox.retry_terminal_document(
                        conn,
                        document_id,
                        operation_id,
                        expected_payload_hash=expected_payload_hash,
                        reason="controller retry after exact terminal failure proof",
                        preserve_queue_position=True,
                    ):
                        raise InvariantError("pending terminal retry did not resume")
            else:
                raise InvariantError("pending terminal recovery has invalid live state")

        elif kind == "operationless_claim":
            proofs = list(attempt.get("proofs") or [])
            if len(proofs) != 2:
                raise InvariantError("pending operationless proof set is invalid")
            claimed_at = datetime.fromisoformat(str(attempt.get("claimed_at") or ""))
            with self.incident_bank_mutation(document_id, kind) as conn:
                if not outbox.recover_operationless_claim(
                    conn,
                    document_id,
                    expected_payload_hash=expected_payload_hash,
                    expected_claimed_at=claimed_at,
                    proofs=tuple(
                        outbox.OperationlessProof(
                            checked_at=datetime.fromtimestamp(
                                float(value["checked_at"]), timezone.utc
                            ),
                            document_present=bool(value["document_present"]),
                            operation_count=int(value["operation_count"]),
                        )
                        for value in proofs
                    ),
                    reason="controller recovery after two exact absence proofs",
                    minimum_proof_interval_seconds=self.cfg.full_seconds,
                    preserve_queue_position=True,
                ):
                    raise InvariantError(
                        "pending operationless recovery did not resume"
                    )

        elif kind == "operation_adoption":
            persisted_status = str(attempt.get("remote_status") or "").lower()
            persisted_present = bool(attempt.get("remote_document_present"))
            allowed_advances = {
                "pending": {
                    "pending",
                    "processing",
                    "completed",
                    "failed",
                    "cancelled",
                },
                "processing": {"processing", "completed", "failed", "cancelled"},
                "completed": {"completed"},
                "failed": {"failed"},
                "cancelled": {"cancelled"},
            }
            if remote_status not in allowed_advances.get(persisted_status, set()):
                raise InvariantError(
                    "pending adoption remote status regressed/contradicted"
                )
            if persisted_present and not document_present:
                raise InvariantError("pending adoption remote document disappeared")
            if document_present and remote_status != "completed":
                raise InvariantError(
                    "pending adoption document exists before completion"
                )
            if document_present and not self._remote_document_matches_item(
                remote_document, item
            ):
                raise InvariantError("pending adoption remote document/hash drift")
            if row["state"] in {"blocked", "submitting", "submitted"}:
                with self.incident_bank_mutation(document_id, kind) as conn:
                    if not outbox.adopt_operation(
                        conn,
                        document_id,
                        operation_id,
                        expected_payload_hash=expected_payload_hash,
                        remote_status=persisted_status,
                        remote_document_present=persisted_present,
                        reason=str(attempt.get("adoption_reason") or ""),
                    ):
                        raise InvariantError(
                            "pending operation adoption did not resume"
                        )
            else:
                raise InvariantError("pending adoption has invalid live state")
        else:
            raise InvariantError(f"unknown pending recovery kind {kind}")

        with self.agent_db() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT state,operation_id::text FROM hindsight_outbox WHERE document_id=%s",
                (document_id,),
            )
            final_row = cur.fetchone()
        expected_state = {
            "completed_refresh": "succeeded",
            "terminal_retry": "queued",
            "operationless_claim": "queued",
            "operation_adoption": "submitted",
        }[kind]
        if not final_row or final_row["state"] != expected_state:
            raise InvariantError("pending recovery final state proof failed")
        if (
            kind == "operation_adoption"
            and str(final_row.get("operation_id") or "") != operation_id
        ):
            raise InvariantError("pending adoption final operation proof failed")
        self.accept_recovered_attempt(attempt)
        self.clear_pending_acceptance(incident)
        return True

    def _incident_rows(self) -> list[dict[str, Any]]:
        with self.agent_db() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT document_id,operation_id::text,state,payload_hash,
                          submitted_payload_hash,item,claimed_at,last_error
                   FROM hindsight_outbox
                   WHERE state=ANY(%s)
                   ORDER BY created_at,document_id""",
                (list(ACTIVE_STATES + ERROR_STATES),),
            )
            return list(cur.fetchall())

    def _remote_operation(self, operation_id: str) -> dict[str, Any] | None:
        with self.hindsight_db() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT operation_id::text,operation_type,status,error_message,
                          result_metadata,task_payload,created_at,updated_at
                   FROM async_operations
                   WHERE bank_id='agent-sessions' AND operation_id=%s::uuid""",
                (operation_id,),
            )
            return cur.fetchone()

    def _remote_document_present(self, document_id: str) -> bool:
        with self.hindsight_db() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT EXISTS(
                       SELECT 1 FROM documents
                       WHERE bank_id='agent-sessions' AND id=%s
                   ) AS present""",
                (document_id,),
            )
            return bool(cur.fetchone()["present"])

    def _remote_document(self, document_id: str) -> dict[str, Any] | None:
        with self.hindsight_db() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT id,original_text,content_hash,retain_params,tags
                   FROM documents
                   WHERE bank_id='agent-sessions' AND id=%s""",
                (document_id,),
            )
            return cur.fetchone()

    def prepare_incident_authorization(
        self, incident: dict[str, Any], rows: list[dict[str, Any]]
    ) -> dict[str, Any]:
        if not getattr(self, "controller_owned", False):
            return self._prepare_incident_authorization(incident, rows)
        with _BoundedAuditHeartbeat(
            self.cfg.controller_file,
            "controller",
            deadline_seconds=self.cfg.heartbeat_max_age * 4,
            pulse_seconds=min(self.cfg.heartbeat_max_age / 3, 5.0),
            fields={
                "status": "incident_recovery",
                "incident_stage": "authorization",
            },
        ):
            return self._prepare_incident_authorization(incident, rows)

    def _prepare_incident_authorization(
        self, incident: dict[str, Any], rows: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Create or append the exact concurrent incident authorization."""
        existing_raw = incident.get("incident_authorization")
        existing = dict(existing_raw) if isinstance(existing_raw, dict) else {}
        existing_lineages = {
            (
                str(value.get("document_id") or ""),
                str(value.get("operation_id") or ""),
                str(value.get("payload_hash") or ""),
            )
            for value in existing.get("row_lineages", [])
        }
        current_lineages = {
            (
                str(row["document_id"]),
                str(row.get("operation_id") or ""),
                str(row["payload_hash"]),
            )
            for row in rows
        }
        if existing:
            permitted_extensions: set[tuple[str, str, str]] = set()
            pending = incident.get("pending_acceptance")
            attempts = list(read_checkpoint(self.cfg).get("recovered_attempts") or [])
            if isinstance(pending, dict):
                attempts.append(pending)
            for attempt in attempts:
                if (
                    isinstance(attempt, dict)
                    and attempt.get("kind") == "operation_adoption"
                ):
                    permitted_extensions.add(
                        (
                            str(attempt.get("document_id") or ""),
                            str(attempt.get("operation_id") or ""),
                            str(attempt.get("payload_hash") or ""),
                        )
                    )
            if not current_lineages.issubset(existing_lineages | permitted_extensions):
                raise InvariantError(
                    "incident gained an unapproved row/operation lineage"
                )
            existing_lineages |= current_lineages

        live_fugu_values = self.fugu_failure_manifest()
        live_fugu = {str(value["request_id"]): value for value in live_fugu_values}
        budget_status = self.budget_recovery_status()
        live_provider_values = list(
            budget_status.get("provider_failure_manifest") or []
        )
        live_provider = {
            str(value["request_id"]): value for value in live_provider_values
        }
        if any(
            live_fugu.get(str(value["request_id"])) != value
            for value in existing.get("authorized_fugu_failures", [])
        ) or any(
            live_provider.get(str(value["request_id"])) != value
            for value in existing.get("authorized_provider_failures", [])
        ):
            raise InvariantError("durable incident authorization manifest drift")

        terminal = {
            str(value["operation_id"]): value
            for value in existing.get("terminal_operations", [])
        }
        terminal_pending: list[str] = []
        for operation_id, value in terminal.items():
            operation = self._remote_operation(operation_id)
            if (
                not operation
                or _exact_record(operation, "operation_id", "operation_type", "status")
                != value
            ):
                raise InvariantError("durable terminal operation authorization drift")
        for row in rows:
            operation_id = str(row.get("operation_id") or "")
            if not operation_id:
                continue
            operation = self._remote_operation(operation_id)
            if not operation:
                raise InvariantError("incident operation cannot be exact-authorized")
            operation_status = str(operation.get("status") or "").lower()
            if operation_status == "completed":
                # Completed batch parents may have a deliberately cleared
                # task payload.  Their exact payload lineage is proved only
                # through the single completed retain child and document; no
                # failure/retry authorization is created by this branch.
                self._prove_completed_batch_parent(row, operation)
                lineage_records: list[dict[str, Any]] = []
            elif operation_status in {"failed", "cancelled"}:
                lineage_records = self._terminal_lineage_records(row, operation)
            elif operation_status in {"pending", "processing"}:
                if not operation_matches_outbox(
                    operation,
                    document_id=str(row["document_id"]),
                    payload_hash=str(row["payload_hash"]),
                    operation_id=operation_id,
                    operation_types=frozenset({"batch_retain"}),
                ):
                    raise InvariantError(
                        "incident operation cannot be exact-authorized"
                    )
                lineage_records = []
            else:
                raise InvariantError("incident operation has an unknown status")
            if operation_status in {"failed", "cancelled"} and not lineage_records:
                terminal_pending.append(operation_id)
            for value in lineage_records:
                terminal[str(value["operation_id"])] = value

        checkpoint = self.ensure_checkpoint_manifests()
        accepted = checkpoint["accepted"]
        accepted_fugu_ids = {
            str(value["request_id"])
            for value in accepted.get("fugu_failure_manifest") or []
        }
        accepted_provider_ids = {
            str(value["request_id"])
            for value in accepted.get("provider_failure_manifest") or []
        }
        preflights = self._provider_preflight_failure_records(incident, live_fugu)
        probe_provider = self._recovery_probe_provider_failure_records(
            incident, live_provider
        )
        fugu_additions = [
            value
            for value in live_fugu_values
            if str(value["request_id"]) not in accepted_fugu_ids
            and str(value["request_id"]) not in preflights
        ]
        provider_additions = [
            value
            for value in live_provider_values
            if str(value["request_id"]) not in accepted_provider_ids
            and str(value["request_id"]) not in probe_provider
        ]
        raw_provider = {
            str(value.get("request_id") or ""): dict(value)
            for value in budget_status.get("provider_failure_history", [])
        }
        provider_addition_ids = {
            str(value["request_id"]) for value in provider_additions
        }
        reservation_ids = {
            str(value.get("request_id") or "")
            for value in budget_status.get("reservations", [])
        }
        correlated: list[dict[str, Any]] = []
        for value in fugu_additions:
            session_id = str(value.get("session_id") or "")
            model = str(value.get("requested_model") or "")
            agent_id = str(value.get("agent_id") or "")
            provider = raw_provider.get(session_id)
            if session_id in provider_addition_ids:
                expected_agent = (
                    "hindsight-backfill"
                    if model == self.cfg.expected_hindsight_model
                    else "hindsight-qwen-budget"
                )
                if (
                    provider is None
                    or model != str(provider.get("model") or "")
                    or agent_id != expected_agent
                    or _canonical_http_status(value.get("http_status"))
                    != int(provider["status_code"])
                ):
                    raise InvariantError(
                        "Fugu/provider failure cross-ledger provenance mismatch"
                    )
            elif session_id in reservation_ids:
                if (
                    model != self.cfg.expected_hindsight_model
                    or agent_id != "hindsight-backfill"
                ):
                    raise InvariantError(
                        "ambiguous Fugu reservation provenance mismatch"
                    )
            else:
                raise InvariantError(
                    "new Fugu failure has no exact budget or preflight lineage"
                )
            correlated.append(value)
        for request_id in provider_addition_ids:
            if (
                sum(
                    str(value.get("session_id") or "") == request_id
                    for value in correlated
                )
                != 1
            ):
                raise InvariantError(
                    "budget provider failure does not map to exactly one Fugu failure"
                )

        authorized_fugu = {
            str(value["request_id"]): value
            for value in existing.get("authorized_fugu_failures", [])
        }
        authorized_fugu.update(
            {str(value["request_id"]): value for value in correlated}
        )
        authorized_provider = {
            str(value["request_id"]): value
            for value in existing.get("authorized_provider_failures", [])
        }
        authorized_provider.update(
            {str(value["request_id"]): value for value in provider_additions}
        )
        authorized_provider.update(probe_provider)
        statuses = sorted(
            {
                int(raw_provider[request_id]["status_code"])
                for request_id in authorized_provider
                if request_id in raw_provider
            }
        )
        unsupported = [
            status
            for status in statuses
            if status not in {401, 402, 429} and not 500 <= status <= 599
        ]
        if unsupported:
            raise InvariantError(
                "unsupported definitive provider status requires hold: "
                + ",".join(str(value) for value in unsupported)
            )
        retry_after = [
            raw_provider[request_id].get("retry_after")
            for request_id in sorted(authorized_provider)
            if request_id in raw_provider
        ]
        lineages = existing_lineages or current_lineages
        row_descriptor = {
            "rows": [
                {
                    "document_id": str(row["document_id"]),
                    "operation_id": str(row.get("operation_id") or ""),
                    "state": str(row["state"]),
                    "payload_hash": str(row["payload_hash"]),
                }
                for row in rows
            ]
        }
        authorization = {
            "row_sha256": existing.get("row_sha256") or _json_digest(row_descriptor),
            "row_lineages": [
                {
                    "document_id": document_id,
                    "operation_id": operation_id,
                    "payload_hash": payload_hash,
                }
                for document_id, operation_id, payload_hash in sorted(lineages)
            ],
            "terminal_operations": [terminal[key] for key in sorted(terminal)],
            "terminal_pending_child_operation_ids": sorted(terminal_pending),
            "authorized_failure_operation_ids": sorted(
                operation_id
                for operation_id, value in terminal.items()
                if str(value.get("status") or "") in {"failed", "cancelled"}
            ),
            "authorized_fugu_failures": [
                authorized_fugu[key] for key in sorted(authorized_fugu)
            ],
            "authorized_fugu_request_ids": sorted(authorized_fugu),
            "authorized_provider_failures": [
                authorized_provider[key] for key in sorted(authorized_provider)
            ],
            "authorized_provider_failure_ids": sorted(authorized_provider),
            "provider_statuses": statuses,
            "provider_retry_after": retry_after,
            "prepared_at": float(existing.get("prepared_at") or time.time()),
            "revision": int(existing.get("revision") or 0) + (1 if existing else 0),
        }
        comparable, prior = dict(authorization), dict(existing)
        comparable.pop("revision", None)
        prior.pop("revision", None)
        if existing and comparable == prior:
            return existing
        incident["incident_authorization"] = authorization
        save_incident(
            self.cfg,
            incident,
            kind="incident_authorization_extended"
            if existing
            else "incident_authorization_prepared",
        )
        return authorization

    def close_incident_authorization(
        self, incident: dict[str, Any], *, reason: str
    ) -> dict[str, Any] | None:
        authorization = incident.pop("incident_authorization", None)
        if not isinstance(authorization, dict):
            return None
        checkpoint = read_checkpoint(self.cfg)
        refresh_proof = completed_refresh_archive_proof(authorization, checkpoint)
        archive = list(incident.get("authorization_archive") or [])
        entry = {
            "authorization_sha256": _json_digest(authorization),
            "row_sha256": authorization.get("row_sha256"),
            "closed_at": time.time(),
            "reason": reason,
        }
        if refresh_proof is not None:
            entry["completed_refresh_proof"] = refresh_proof
        archive.append(entry)
        incident["authorization_archive"] = archive
        incident["absence_proofs"] = {}
        save_incident(self.cfg, incident, kind="incident_authorization_archived")
        return entry

    @staticmethod
    def _remote_document_matches_item(
        remote: dict[str, Any], item: dict[str, Any]
    ) -> bool:
        original = str(remote.get("original_text") or "")
        params = dict(remote.get("retain_params") or {})
        return (
            str(remote.get("id") or "") == str(item.get("document_id") or "")
            and original == str(item.get("content") or "")
            and str(remote.get("content_hash") or "")
            == hashlib.sha256(original.encode()).hexdigest()
            and str(params.get("context") or "") == str(item.get("context") or "")
            and str(params.get("event_date") or "") == str(item.get("timestamp") or "")
            and set(str(value) for value in (remote.get("tags") or []))
            == set(str(value) for value in (item.get("tags") or []))
        )

    def _matching_remote_operations(
        self, document_id: str, payload_hash: str
    ) -> list[dict[str, Any]]:
        # The text predicate is only a read-volume bound.  Every returned row
        # must pass exact recursive document and canonical-payload hash proof.
        pattern = f"%{document_id}%"
        with self.hindsight_db() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT operation_id::text,operation_type,status,error_message,
                          result_metadata,task_payload,created_at,updated_at
                   FROM async_operations
                   WHERE bank_id='agent-sessions'
                     AND (task_payload::text LIKE %s OR result_metadata::text LIKE %s)
                   ORDER BY created_at,operation_id""",
                (pattern, pattern),
            )
            rows = list(cur.fetchall())
        return [
            row
            for row in rows
            if operation_matches_outbox(
                row, document_id=document_id, payload_hash=payload_hash
            )
        ]

    def _operationless_remote_snapshot(
        self, document_id: str, payload_hash: str
    ) -> dict[str, Any]:
        """Read document presence and exact operations in one stable snapshot."""
        budget_before = self._http_json(
            f"http://127.0.0.1:{self.cfg.budget_port}/health"
        )
        provider_before = int(budget_before.get("in_flight") or 0)
        pattern = f"%{document_id}%"
        with self.hindsight_db() as conn, conn.cursor() as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            cur.execute(
                """SELECT id,original_text,content_hash,retain_params,tags
                   FROM documents
                   WHERE bank_id='agent-sessions' AND id=%s""",
                (document_id,),
            )
            remote_document = cur.fetchone()
            cur.execute(
                """SELECT operation_id::text,operation_type,status,error_message,
                          result_metadata,task_payload,created_at,updated_at
                   FROM async_operations
                   WHERE bank_id='agent-sessions'
                     AND (task_payload::text LIKE %s OR result_metadata::text LIKE %s)
                   ORDER BY created_at,operation_id""",
                (pattern, pattern),
            )
            rows = list(cur.fetchall())
            cur.execute(
                """SELECT count(*)::int AS n FROM async_operations
                   WHERE bank_id='agent-sessions'
                     AND status IN ('pending','processing')"""
            )
            hindsight_active = int(cur.fetchone()["n"])
        matches = [
            row
            for row in rows
            if operation_matches_outbox(
                row, document_id=document_id, payload_hash=payload_hash
            )
        ]
        budget_after = self._http_json(
            f"http://127.0.0.1:{self.cfg.budget_port}/health"
        )
        provider_after = int(budget_after.get("in_flight") or 0)
        return {
            "checked_at": time.time(),
            "document": remote_document,
            "document_present": remote_document is not None,
            "matches": matches,
            "operation_count": len(matches),
            "hindsight_active": hindsight_active,
            "provider_in_flight_before": provider_before,
            "provider_in_flight_after": provider_after,
        }

    def _explained_terminal_operation_ids(self, document_id: str) -> set[str]:
        """Return exact historical terminal parents already consumed by retry."""
        with self.agent_db() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT operation_id::text
                   FROM hindsight_outbox_retry_history
                   WHERE document_id=%s AND operation_id IS NOT NULL""",
                (document_id,),
            )
            return {str(row["operation_id"]) for row in cur.fetchall()}

    def _prove_paid_preflight_fugu(
        self, call: dict[str, Any], model: str
    ) -> dict[str, Any]:
        request_id = str(call.get("request_id") or "")
        rows = self.fugu_session_manifest({request_id})
        expected_agent = (
            "hindsight-backfill"
            if model == self.cfg.expected_hindsight_model
            else "hindsight-qwen-budget"
        )
        if len(rows) != 1:
            raise InvariantError("paid preflight lacks one exact Fugu request")
        row = rows[0]
        if (
            row.get("session_id") != request_id
            or row.get("requested_model") != model
            or row.get("agent_id") != expected_agent
            or row.get("status") != "success"
            or not 200 <= int(row.get("http_status") or 0) <= 299
        ):
            raise InvariantError("paid preflight Fugu success provenance drift")
        return row

    def _recovery_probe_gate(self) -> dict[str, Any]:
        """Read the exact non-spending safety predicate for one auth probe."""
        with self.agent_db() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT circuit_open FROM hindsight_sync_state
                   WHERE bank_id='agent-sessions'"""
            )
            agent = cur.fetchone()
            cur.execute(
                "SELECT count(*)::int AS n FROM hindsight_outbox WHERE state='succeeded'"
            )
            succeeded = int(cur.fetchone()["n"])
        with self.pocket_db() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT circuit_open FROM hindsight_sync_state
                   WHERE bank_id='dans-pocket'"""
            )
            pocket = cur.fetchone()
        budget = self._http_json(f"http://127.0.0.1:{self.cfg.budget_port}/health")
        projection = compute_projection(
            budget.get("hindsight_backfill_spent_usd", 0), succeeded, self.cfg
        )
        if projection >= self.cfg.hard_budget:
            raise InvariantError("recovery probe projection reaches hard budget")
        return {
            "agent_circuit_open": bool(agent and agent["circuit_open"]),
            "pocket_circuit_open": bool(pocket and pocket["circuit_open"]),
            "provider_in_flight": int(budget.get("in_flight") or 0),
            "projected_full_cost_usd": float(projection),
        }

    def start_auth_probe_ambiguity(
        self,
        state: dict[str, Any],
        incident: dict[str, Any],
        *,
        route: str,
        provenance: str,
        response: dict[str, Any],
    ) -> None:
        """Snapshot the exact unchanged reservation created by one probe."""
        existing = state.get("probe_ambiguity")
        request_id = str(response.get("request_id") or "")
        if isinstance(existing, dict):
            if (
                existing.get("route") != route
                or existing.get("provenance") != provenance
                or existing.get("request_id") != request_id
            ):
                raise InvariantError("probe ambiguity lineage changed")
            return
        status = self.budget_recovery_status()
        budget = self._http_json(f"http://127.0.0.1:{self.cfg.budget_port}/health")
        reservations = list(status.get("reservations") or [])
        route_state = dict(dict(state.get("probe_routes") or {}).get(route) or {})
        expected_gate_sha256 = str(route_state.get("request_gate_sha256") or "")
        expected_model, expected_endpoint = {
            "gpt_oss": (self.cfg.expected_hindsight_model, "chat"),
            "qwen": (QWEN_MODEL, "embeddings"),
        }[route]
        if (
            not request_id
            or request_id != str(uuid.uuid5(RECOVERY_PROBE_NAMESPACE, provenance))
            or len(reservations) != 1
            or str(reservations[0].get("request_id") or "") != request_id
            or str(reservations[0].get("model") or "") != expected_model
            or str(reservations[0].get("endpoint") or "") != expected_endpoint
            or str(reservations[0].get("request_provenance_sha256") or "")
            != hashlib.sha256(provenance.encode()).hexdigest()
            or str(reservations[0].get("recovery_probe_route") or "") != route
            or str(reservations[0].get("recovery_auth_incident_request_id") or "")
            != str(state.get("probe_auth_incident_request_id") or "")
            or not expected_gate_sha256
            or str(reservations[0].get("recovery_gate_sha256") or "")
            != expected_gate_sha256
            or int(budget.get("in_flight") or 0) != 1
            or provider_failure_kind(str(budget.get("circuit_reason") or ""))
            != "ambiguous"
        ):
            raise InvariantError(
                "recovery-probe ambiguity lacks one exact durable reservation"
            )
        gate = self._recovery_probe_gate_for_spend_only(budget)
        maximum_projection = gate + Decimal(str(status.get("total_max_usd") or 0))
        if maximum_projection >= self.cfg.hard_budget:
            raise InvariantError("recovery-probe maximum charge reaches hard budget")
        state["probe_ambiguity"] = {
            "stage": "snapshot",
            "route": route,
            "provenance": provenance,
            "request_id": request_id,
            "snapshot_at": time.time(),
            "reservation_manifest": {
                "reservations": reservations,
                "sha256": str(status.get("sha256") or ""),
                "total_max_usd": str(status.get("total_max_usd") or 0),
            },
            "spent_before": str(budget.get("hindsight_backfill_spent_usd", 0)),
            "provider_spent_before": str(budget.get("provider_spent_usd", 0)),
            "reason": str(budget.get("circuit_reason") or ""),
        }
        incident["provider_recovery"] = state
        # Own the durable boundary here rather than relying on the caller.
        # Both a restart that rediscovers a persisted ``started`` route and a
        # transport exception return immediately after this helper; without
        # this atomic save they would rediscover the same reservation forever
        # and never advance to reconcile/seal it.  Repeated calls with the
        # exact already-persisted ambiguity are intentionally no-ops above.
        save_incident(
            self.cfg,
            incident,
            kind="provider_auth_probe_ambiguity_snapshot",
        )

    def _recovery_probe_gate_for_spend_only(self, budget: dict[str, Any]) -> Decimal:
        with self.agent_db() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count(*)::int AS n FROM hindsight_outbox WHERE state='succeeded'"
            )
            succeeded = int(cur.fetchone()["n"])
        return compute_projection(
            budget.get("hindsight_backfill_spent_usd", 0), succeeded, self.cfg
        )

    def recover_auth_probe_ambiguity(
        self, state: dict[str, Any], incident: dict[str, Any]
    ) -> bool:
        """Max-charge one probe ambiguity, seal controls, and retry fresh."""
        ambiguity = dict(state.get("probe_ambiguity") or {})
        stage = str(ambiguity.get("stage") or "")
        snapshot = dict(ambiguity.get("reservation_manifest") or {})
        if stage == "snapshot":
            if (
                time.time() - float(ambiguity.get("snapshot_at") or 0)
                < self.cfg.full_seconds
            ):
                return False
            status = self.budget_recovery_status()
            budget = self._http_json(f"http://127.0.0.1:{self.cfg.budget_port}/health")
            if (
                list(status.get("reservations") or [])
                != list(snapshot.get("reservations") or [])
                or str(status.get("sha256") or "") != str(snapshot.get("sha256") or "")
                or str(status.get("total_max_usd") or 0)
                != str(snapshot.get("total_max_usd") or 0)
                or str(budget.get("hindsight_backfill_spent_usd", 0))
                != str(ambiguity.get("spent_before"))
                or str(budget.get("provider_spent_usd", 0))
                != str(ambiguity.get("provider_spent_before"))
                or str(budget.get("circuit_reason") or "")
                != str(ambiguity.get("reason") or "")
            ):
                raise InvariantError(
                    "recovery-probe ambiguous reservation snapshot changed"
                )
            ambiguity["stage"] = "reconcile_started"
            state["probe_ambiguity"] = ambiguity
            incident["provider_recovery"] = state
            save_incident(
                self.cfg,
                incident,
                kind="provider_auth_probe_reconciliation_started",
            )
            stage = "reconcile_started"
        if stage == "reconcile_started":
            self._hindsight_recovery_roll(
                reconcile_ambiguous=True,
                reset_provider_auth=False,
                reconcile_manifest_sha256=str(snapshot.get("sha256") or ""),
            )
            status = self.budget_recovery_status()
            proof = self._reconciliation_proof(status, snapshot)
            if proof is None or status.get("reservations"):
                raise InvariantError(
                    "recovery-probe ambiguity lacks exact reconciliation proof"
                )
            after = self._http_json(f"http://127.0.0.1:{self.cfg.budget_port}/health")
            delta = Decimal(
                str(after.get("hindsight_backfill_spent_usd", 0))
            ) - Decimal(str(ambiguity["spent_before"]))
            provider_delta = Decimal(str(after.get("provider_spent_usd", 0))) - Decimal(
                str(ambiguity["provider_spent_before"])
            )
            charged = Decimal(str(proof.get("charged_maximum_usd") or 0))
            tolerance = Decimal("0.000000001")
            if (
                abs(delta - charged) > tolerance
                or abs(provider_delta - charged) > tolerance
                or int(after.get("in_flight") or 0)
                or str(after.get("circuit_reason") or "")
                != "provider returned " + str(state.get("probe_auth_incident_status"))
            ):
                raise InvariantError(
                    "recovery-probe reconciliation spend/auth hold drift"
                )
            ambiguity.update(
                {
                    "stage": "seal_started",
                    "charged_maximum_usd": str(charged),
                    "reconciliation_proof": proof,
                }
            )
            state["probe_ambiguity"] = ambiguity
            incident["provider_recovery"] = state
            save_incident(
                self.cfg,
                incident,
                kind="provider_auth_probe_reconciliation_complete",
            )
            stage = "seal_started"
        if stage == "seal_started":
            self._hindsight_recovery_roll(
                reconcile_ambiguous=False,
                reset_provider_auth=False,
            )
            route = str(ambiguity["route"])
            route_states = dict(state.get("probe_routes") or {})
            route_state = dict(route_states.get(route) or {})
            route_state.update(
                {
                    "status": "ambiguity_reconciled_backoff",
                    "retry_at": time.time() + self.cfg.controller_initial_backoff,
                    "same_provenance_retry": False,
                }
            )
            route_state.pop("provenance", None)
            route_state.pop("token", None)
            route_states[route] = route_state
            history = list(state.get("probe_history") or [])
            history.append(
                {
                    "route": route,
                    "provenance": ambiguity["provenance"],
                    "result": "ambiguous_reconciled",
                    "request_id": ambiguity["request_id"],
                    "reconciliation_sha256": _record_sha256(
                        dict(ambiguity["reconciliation_proof"])
                    ),
                }
            )
            state["probe_routes"] = route_states
            state["probe_history"] = history
            state.pop("probe_ambiguity", None)
            incident["provider_recovery"] = state
            save_incident(
                self.cfg,
                incident,
                kind="provider_auth_probe_reconciliation_sealed",
            )
            return True
        raise InvariantError("unsupported recovery-probe ambiguity stage")

    def reconcile_probe_ambiguity_only(
        self, incident: dict[str, Any]
    ) -> dict[str, Any]:
        """Reconcile/seal one probe reservation without touching Agent rows."""
        state = incident.get("provider_recovery")
        if not isinstance(state, dict) or not isinstance(
            state.get("probe_ambiguity"), dict
        ):
            raise InvariantError("probe-only reconciliation lacks durable ambiguity")
        budget = self._http_json(
            f"http://127.0.0.1:{self.cfg.budget_port}/health"
        )
        recovery_status = self.budget_recovery_status()
        in_flight = int(budget.get("in_flight") or 0)
        if in_flight:
            if not exact_probe_reservation_recovery(
                incident,
                budget=budget,
                budget_recovery=recovery_status,
            ):
                raise InvariantError(
                    "probe-only reconciliation lacks exact live reservation"
                )
        elif recovery_status.get("reservations"):
            raise InvariantError(
                "probe-only reconciliation has reservations without in-flight state"
            )
        if not self.recover_auth_probe_ambiguity(state, incident):
            return {"status": "waiting_probe_reconciliation"}

        # Re-read after the reconciliation and seal transitions.  Generic row
        # recovery is selected only on a later controller iteration, so this
        # fresh zero-activity proof can never be reused across row mutation.
        after_budget = self._http_json(
            f"http://127.0.0.1:{self.cfg.budget_port}/health"
        )
        after_status = self.budget_recovery_status()
        after_state = incident.get("provider_recovery")
        if (
            int(after_budget.get("in_flight") or 0)
            or after_status.get("reservations")
            or not isinstance(after_state, dict)
            or isinstance(after_state.get("probe_ambiguity"), dict)
            or after_budget.get("recovery_controls_sealed_disabled") is not True
        ):
            raise InvariantError(
                "probe-only reconciliation did not reach a sealed drained state"
            )
        return {"status": "probe_reconciliation_sealed"}

    @staticmethod
    def _provider_auth_recovery_root(
        status: dict[str, Any],
        *,
        incident_id: str,
        incident_status: int,
        incident_at: str,
        required: bool,
    ) -> dict[str, Any]:
        expected = {
            "version": 1,
            "auth_incident_request_id": incident_id,
            "auth_incident_status": incident_status,
            "auth_incident_at": incident_at,
        }
        if (
            str(status.get("provider_auth_circuit_incident_request_id") or "")
            != incident_id
            or int(status.get("provider_auth_circuit_incident_status") or 0)
            != incident_status
        ):
            raise InvariantError("provider-auth circuit root incident drift")
        active = status.get("provider_auth_recovery")
        if active is None and not required:
            return expected
        if active != expected:
            raise InvariantError("active provider-auth recovery root incident drift")
        return expected

    def _recovery_probe_success_record(
        self,
        status: dict[str, Any],
        *,
        route: str,
        request_id: str,
        provenance_sha256: str,
        incident_id: str,
    ) -> dict[str, Any]:
        expected_model, expected_endpoint = {
            "gpt_oss": (self.cfg.expected_hindsight_model, "chat"),
            "qwen": (QWEN_MODEL, "embeddings"),
        }[route]
        matches = [
            dict(value)
            for value in status.get("provider_call_history") or []
            if str(value.get("request_id") or "") == request_id
        ]
        if len(matches) != 1:
            raise InvariantError("recovery-probe success lacks one exact provider call")
        call = matches[0]
        gate_sha256 = str(call.get("recovery_gate_sha256") or "")
        if (
            call.get("provider_call_recorded") is not True
            or call.get("ambiguous_upstream_reconciled") is True
            or str(call.get("model") or "") != expected_model
            or str(call.get("endpoint") or "") != expected_endpoint
            or str(call.get("recovery_probe_route") or "") != route
            or str(call.get("recovery_auth_incident_request_id") or "") != incident_id
            or str(call.get("request_provenance_sha256") or "") != provenance_sha256
            or re.fullmatch(r"[0-9a-f]{64}", gate_sha256) is None
        ):
            raise InvariantError("recovery-probe success provenance drift")
        return {
            "request_id": request_id,
            "request_provenance_sha256": provenance_sha256,
            "recovery_gate_sha256": gate_sha256,
            "cost_usd": float(call.get("cost_usd") or 0),
        }

    def provider_auth_recovery_probes(
        self,
        state: dict[str, Any],
        incident: dict[str, Any],
        *,
        authorized_incident_ids: list[str],
    ) -> bool:
        """Run/replay both deterministic, dual-auth, metered auth probes."""
        admin_token = str(self.hindsight_state.get("BAT") or "")
        proxy_token = str(self.hindsight_state.get("BPT") or "")
        if not admin_token or not proxy_token or admin_token == proxy_token:
            raise InvariantError(
                "recovery probe requires distinct budget admin/inference credentials"
            )
        budget_status = self.budget_recovery_status()
        provider_history = list(budget_status.get("provider_failure_history") or [])
        by_id = {
            str(value.get("request_id") or ""): dict(value)
            for value in provider_history
        }
        if "probe_auth_incident_request_id" not in state:
            root_id = str(
                budget_status.get("provider_auth_circuit_incident_request_id") or ""
            )
            root_status = int(
                budget_status.get("provider_auth_circuit_incident_status") or 0
            )
            selected = by_id.get(root_id)
            health = self._http_json(f"http://127.0.0.1:{self.cfg.budget_port}/health")
            reason = str(health.get("circuit_reason") or "")
            if (
                not root_id
                or root_id not in set(authorized_incident_ids)
                or root_status not in {401, 402}
                or selected is None
                or int(selected.get("status_code") or 0) != root_status
                or selected.get("recovery_probe_route") is not None
                or reason != f"provider returned {root_status}"
            ):
                raise InvariantError(
                    "auth recovery probe lacks an exact current incident ID"
                )
            state["probe_auth_incident_request_id"] = str(selected["request_id"])
            state["probe_auth_incident_status"] = int(selected["status_code"])
            state["probe_auth_incident_at"] = str(selected.get("at") or "")
            state["probe_routes"] = {}
            state["probe_history"] = []
            state["stage"] = "recovery_probes_started"
            incident["provider_recovery"] = state
            save_incident(
                self.cfg, incident, kind="provider_auth_recovery_probes_started"
            )
        incident_id = str(state["probe_auth_incident_request_id"])
        original = by_id.get(incident_id)
        if (
            original is None
            or int(original.get("status_code") or 0)
            != int(state["probe_auth_incident_status"])
            or int(original.get("status_code") or 0) not in {401, 402}
            or str(original.get("at") or "")
            != str(state.get("probe_auth_incident_at") or "")
        ):
            raise InvariantError("durable recovery-probe auth incident drift")
        self._provider_auth_recovery_root(
            budget_status,
            incident_id=incident_id,
            incident_status=int(state["probe_auth_incident_status"]),
            incident_at=str(state["probe_auth_incident_at"]),
            required=False,
        )
        if isinstance(state.get("probe_ambiguity"), dict):
            if not self.recover_auth_probe_ambiguity(state, incident):
                return False
        route_states = dict(state.get("probe_routes") or {})
        retry_pending = False
        for route in ("gpt_oss", "qwen"):
            route_state = dict(route_states.get(route) or {})
            if route_state.get("terminal") is True:
                if route_state.get("result") != "authenticated":
                    raise InvariantError(
                        "durable recovery-probe terminal result is unsafe"
                    )
                continue
            retry_at = float(route_state.get("retry_at") or 0)
            if retry_at > time.time():
                retry_pending = True
                continue
            persisted_started = (
                route_state.get("status") in {"started", "response_ambiguous_replay"}
                and bool(route_state.get("token"))
                and bool(route_state.get("provenance"))
            )
            same_provenance = (
                bool(route_state.pop("same_provenance_retry", False))
                or persisted_started
            )
            if not same_provenance:
                route_state["token"] = secrets.token_hex(12)
                route_state["attempt"] = int(route_state.get("attempt") or 0) + 1
                route_state.pop("response", None)
                route_state.pop("http_status", None)
            token = str(route_state.get("token") or "")
            if not re.fullmatch(r"[0-9a-f]{24}", token):
                raise InvariantError("durable recovery-probe route token is invalid")
            provenance = f"hindsight-recovery-{token}-{route}"
            if route_state.get("provenance") not in {None, provenance}:
                if same_provenance:
                    raise InvariantError("durable recovery-probe provenance drift")
            durable_response: tuple[int, dict[str, str], dict[str, Any]] | None = None
            if persisted_started:
                unresolved = self.budget_recovery_status()
                reservations = list(unresolved.get("reservations") or [])
                if reservations:
                    if len(reservations) != 1:
                        raise InvariantError(
                            "recovery-probe response ambiguity has multiple reservations"
                        )
                    self.start_auth_probe_ambiguity(
                        state,
                        incident,
                        route=route,
                        provenance=provenance,
                        response={
                            "request_id": str(reservations[0].get("request_id") or "")
                        },
                    )
                    return False
                provenance_sha256 = hashlib.sha256(provenance.encode()).hexdigest()
                calls = [
                    dict(value)
                    for value in unresolved.get("provider_call_history") or []
                    if value.get("recovery_probe_route") == route
                    and value.get("recovery_auth_incident_request_id") == incident_id
                    and value.get("request_provenance_sha256") == provenance_sha256
                ]
                failures = [
                    dict(value)
                    for value in unresolved.get("provider_failure_history") or []
                    if value.get("recovery_probe_route") == route
                    and value.get("recovery_auth_incident_request_id") == incident_id
                    and value.get("request_provenance_sha256") == provenance_sha256
                ]
                if len(calls) + len(failures) > 1:
                    raise InvariantError(
                        "recovery-probe response ambiguity has contradictory terminals"
                    )
                if calls:
                    call = calls[0]
                    reconciled = call.get("ambiguous_upstream_reconciled") is True
                    durable_response = (
                        200,
                        {},
                        {
                            "result": (
                                "ambiguous_reconciled"
                                if reconciled
                                else "authenticated"
                            ),
                            "route": route,
                            "request_id": str(call["request_id"]),
                            "provenance_sha256": provenance_sha256,
                            "upstream_status": None if reconciled else 200,
                            "cost_usd": float(call.get("cost_usd") or 0),
                            "reused": True,
                        },
                    )
                elif failures:
                    failure = failures[0]
                    upstream_status = int(failure["status_code"])
                    durable_response = (
                        200,
                        {},
                        {
                            "result": (
                                "expected_auth_failure"
                                if upstream_status in {401, 402}
                                else (
                                    "retryable_failure"
                                    if upstream_status == 429
                                    or 500 <= upstream_status <= 599
                                    else "unexpected_failure"
                                )
                            ),
                            "route": route,
                            "request_id": str(failure["request_id"]),
                            "provenance_sha256": provenance_sha256,
                            "upstream_status": upstream_status,
                            "retry_after": failure.get("retry_after"),
                            "reused": True,
                        },
                    )
            gate = dict(route_state.get("request_gate") or {})
            if durable_response is None:
                current_gate = self._recovery_probe_gate()
                if gate and same_provenance and current_gate != gate:
                    raise InvariantError(
                        "recovery-probe replay gate changed before provider call"
                    )
                gate = current_gate
            if (
                gate.get("agent_circuit_open") is not True
                or gate.get("pocket_circuit_open") is not True
                or int(gate.get("provider_in_flight") or 0) != 0
            ):
                raise InvariantError(
                    "recovery-probe Agent/Pocket/provider gate is not exact"
                )
            payload = {
                "route": route,
                "provenance": provenance,
                "auth_incident_request_id": incident_id,
                **gate,
            }
            route_state.update(
                {
                    "provenance": provenance,
                    "request_gate": gate,
                    "request_gate_sha256": _json_digest(payload),
                    "started_at": float(route_state.get("started_at") or time.time()),
                    "status": "started",
                }
            )
            route_states[route] = route_state
            state["probe_routes"] = route_states
            incident["provider_recovery"] = state
            save_incident(
                self.cfg,
                incident,
                kind=f"provider_auth_recovery_probe_{route}_started",
            )
            try:
                if durable_response is not None:
                    status, headers, body = durable_response
                else:
                    status, headers, body = self._http_post_json_status(
                        f"http://127.0.0.1:{self.cfg.budget_port}/admin/recovery-probe",
                        payload,
                        headers={
                            "Authorization": f"Bearer {admin_token}",
                            "X-Proxy-Authorization": f"Bearer {proxy_token}",
                        },
                        timeout=75,
                    )
            except Exception as exc:
                route_state["status"] = "response_ambiguous_replay"
                route_state["same_provenance_retry"] = True
                route_state["last_error"] = (f"{type(exc).__name__}: {exc}")[:1000]
                route_states[route] = route_state
                state["probe_routes"] = route_states
                incident["provider_recovery"] = state
                save_incident(
                    self.cfg,
                    incident,
                    kind=f"provider_auth_recovery_probe_{route}_response_ambiguous",
                )
                unresolved = self.budget_recovery_status()
                reservations = list(unresolved.get("reservations") or [])
                if reservations:
                    if len(reservations) != 1:
                        raise InvariantError(
                            "recovery-probe response ambiguity has multiple reservations"
                        ) from exc
                    self.start_auth_probe_ambiguity(
                        state,
                        incident,
                        route=route,
                        provenance=provenance,
                        response={
                            "request_id": str(reservations[0].get("request_id") or "")
                        },
                    )
                return False
            result = str(body.get("result") or "")
            expected_sha = hashlib.sha256(provenance.encode()).hexdigest()
            matched_existing = body.get("matched_existing_route") is True
            if result and (
                body.get("route") != route
                or not str(body.get("request_id") or "")
                or (
                    not matched_existing
                    and str(body.get("provenance_sha256") or "") != expected_sha
                )
                or (
                    matched_existing
                    and (result != "authenticated" or body.get("reused") is not True)
                )
            ):
                raise InvariantError("recovery-probe response lineage drift")
            route_state["http_status"] = status
            route_state["response"] = body
            route_state["retry_at"] = 0.0
            history = list(state.get("probe_history") or [])
            if status == 429 and not result:
                # This is the proxy's pre-forward capacity response, so no
                # reservation/provider attempt exists and the exact request
                # provenance remains safe to replay.  A definitive upstream
                # 429 is returned as ``retryable_failure`` below and gets a
                # fresh provenance after backoff.
                delay = provider_retry_after(
                    f"Retry-After: {headers.get('Retry-After') or headers.get('retry-after') or ''}",
                    self.cfg.controller_initial_backoff,
                )
                route_state["status"] = "rate_backoff"
                route_state["retry_at"] = time.time() + min(
                    delay, self.cfg.controller_max_backoff
                )
                route_state["same_provenance_retry"] = True
                retry_pending = True
            elif result == "authenticated":
                upstream = int(body.get("upstream_status") or 0)
                if not 200 <= upstream <= 299:
                    raise InvariantError(
                        "recovery-probe terminal status/result contradiction"
                    )
                success_status = self.budget_recovery_status()
                self._provider_auth_recovery_root(
                    success_status,
                    incident_id=incident_id,
                    incident_status=int(state["probe_auth_incident_status"]),
                    incident_at=str(state["probe_auth_incident_at"]),
                    required=True,
                )
                success = self._recovery_probe_success_record(
                    success_status,
                    route=route,
                    request_id=str(body["request_id"]),
                    provenance_sha256=str(body["provenance_sha256"]),
                    incident_id=incident_id,
                )
                if "cost_usd" in body and abs(
                    Decimal(str(body["cost_usd"])) - Decimal(str(success["cost_usd"]))
                ) > Decimal("0.000000001"):
                    raise InvariantError("recovery-probe success cost drift")
                route_state.update(
                    {
                        "status": "terminal",
                        "terminal": True,
                        "result": result,
                        "success": success,
                        "matched_existing_route": matched_existing,
                        "completed_at": time.time(),
                    }
                )
                history.append(
                    {
                        "route": route,
                        "provenance": provenance,
                        "result": result,
                        "request_id": str(body["request_id"]),
                        "request_provenance_sha256": str(body["provenance_sha256"]),
                        "upstream_status": upstream,
                        "response_sha256": _record_sha256(body),
                    }
                )
            elif result in {"expected_auth_failure", "retryable_failure"}:
                upstream = int(body.get("upstream_status") or 0)
                if upstream not in {401, 402, 429} and not 500 <= upstream <= 599:
                    raise InvariantError(
                        "recovery-probe retryable status/result contradiction"
                    )
                delay = provider_retry_after(
                    " ".join(
                        "Retry-After: " + str(value)
                        for value in (
                            body.get("retry_after"),
                            headers.get("Retry-After"),
                            headers.get("retry-after"),
                        )
                        if value
                    ),
                    bounded_backoff(
                        int(route_state.get("attempt") or 1),
                        self.cfg.controller_initial_backoff,
                        self.cfg.controller_max_backoff,
                    ),
                )
                route_state["status"] = "provider_backoff"
                route_state["result"] = result
                route_state["retry_at"] = time.time() + min(
                    delay, self.cfg.controller_max_backoff
                )
                history.append(
                    {
                        "route": route,
                        "provenance": provenance,
                        "result": result,
                        "request_id": str(body["request_id"]),
                        "upstream_status": upstream,
                        "response_sha256": _record_sha256(body),
                    }
                )
                retry_pending = True
            elif result == "ambiguous_reservation":
                route_state["status"] = result
                self.start_auth_probe_ambiguity(
                    state,
                    incident,
                    route=route,
                    provenance=provenance,
                    response=body,
                )
            elif result == "ambiguous_reconciled":
                route_state.update(
                    {
                        "status": "ambiguity_reconciled_backoff",
                        "retry_at": time.time() + self.cfg.controller_initial_backoff,
                    }
                )
                route_state.pop("token", None)
                route_state.pop("provenance", None)
                history.append(
                    {
                        "route": route,
                        "provenance": provenance,
                        "result": result,
                        "request_id": str(body["request_id"]),
                        "response_sha256": _record_sha256(body),
                    }
                )
                retry_pending = True
            else:
                route_state["status"] = "unsafe_terminal"
            state["probe_history"] = history
            route_states[route] = route_state
            state["probe_routes"] = route_states
            incident["provider_recovery"] = state
            save_incident(
                self.cfg,
                incident,
                kind=f"provider_auth_recovery_probe_{route}_{route_state['status']}",
            )
            if route_state.get("terminal") is not True:
                # One ambiguous probe owns the proxy's sole durable
                # reservation.  Stop the multi-route loop immediately so the
                # next route cannot evaluate (or spend against) a nonzero
                # in-flight gate.  The restart-safe ambiguity state above is
                # reconciled and sealed before any fresh provenance is used.
                if result == "ambiguous_reservation":
                    return False
                if status in {500, 502} and not result:
                    retry_pending = True
                    continue
                if status == 429 or result in {
                    "ambiguous_reservation",
                    "ambiguous_reconciled",
                    "expected_auth_failure",
                    "retryable_failure",
                }:
                    retry_pending = True
                    continue
                raise InvariantError(
                    f"recovery probe {route} returned unsafe {status}/{result}"
                )
        if retry_pending or isinstance(state.get("probe_ambiguity"), dict):
            return False
        terminal_status = self.budget_recovery_status()
        self._provider_auth_recovery_root(
            terminal_status,
            incident_id=incident_id,
            incident_status=int(state["probe_auth_incident_status"]),
            incident_at=str(state["probe_auth_incident_at"]),
            required=True,
        )
        probe_request_ids = {
            str(value["request_id"])
            for value in state.get("probe_history") or []
            if value.get("result") in {"expected_auth_failure", "retryable_failure"}
        }
        provider_failures = [
            dict(value)
            for value in terminal_status.get("provider_failure_manifest") or []
            if str(value.get("request_id") or "") in probe_request_ids
        ]
        if {
            str(value.get("request_id") or "") for value in provider_failures
        } != probe_request_ids:
            raise InvariantError("recovery probes lack exact provider failure records")
        state["pre_reset_preflights"] = {
            "through_budget_recovery_probe": True,
            "auth_incident_request_id": incident_id,
            "routes": [route_states[value] for value in ("gpt_oss", "qwen")],
            "authorized_provider_failures": provider_failures,
            "authorized_provider_failure_ids": sorted(probe_request_ids),
        }
        state["stage"] = "preflights_passed"
        incident["provider_recovery"] = state
        save_incident(self.cfg, incident, kind="provider_auth_recovery_probes_complete")
        return True

    def provider_model_preflights(
        self,
        *,
        provenance_token: str,
        baseline_manifest: list[dict[str, Any]],
        through_budget: bool = False,
        provider_call_baseline: list[dict[str, Any]] | None = None,
        spend_before: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Exercise both authenticated production model routes with tiny calls.

        During an auth incident the pre-reset calls are expected to be rejected
        with a definitive 401/402.  Both routes are still exercised, and every
        result is persisted in incident state before the one-shot reset.
        """
        if not through_budget:
            raise InvariantError(
                "direct provider preflight is forbidden; use recovery-probe"
            )
        if through_budget:
            key = str(self.hindsight_state.get("BPT") or "")
            if not key:
                raise InvariantError("budget proxy inference credential is absent")
        else:
            key_file = Path.home() / ".attestmesh/hindsight-router-key.json"
            key = str(json.loads(key_file.read_text()).get("key") or "")
            if not key:
                raise InvariantError("Hindsight production router key is absent")
        token_digest = hashlib.sha256(provenance_token.encode()).hexdigest()
        session_root = f"hindsight-recovery-{token_digest[:24]}"
        agent_id = "hindsight-recovery-controller"
        results: list[dict[str, Any]] = []
        calls = (
            (
                "gpt_oss",
                "provider_gpt_oss_preflight",
                "chat",
                self.cfg.expected_hindsight_model,
                {
                    "model": self.cfg.expected_hindsight_model,
                    "messages": [
                        {"role": "user", "content": "recovery auth preflight"}
                    ],
                    "max_tokens": 1,
                    "reasoning_effort": "low",
                    "include_reasoning": False,
                },
            ),
            (
                "qwen",
                "provider_qwen_preflight",
                "embeddings",
                QWEN_MODEL,
                {
                    "model": QWEN_MODEL,
                    "input": ["recovery auth preflight"],
                },
            ),
        )
        old_failure_by_id = {
            str(value["request_id"]): value for value in baseline_manifest
        }
        baseline_calls = provider_call_baseline or []
        old_call_ids = {str(value["request_id"]) for value in baseline_calls}

        for name, heartbeat, endpoint, model, original_payload in calls:
            self.touch_controller(heartbeat)
            session_id = f"{session_root}-{name}"
            provenance_sha256 = hashlib.sha256(session_id.encode()).hexdigest()
            payload = json.loads(json.dumps(original_payload))
            payload["metadata"] = {
                "recovery_preflight_id": session_id,
                "session_id": session_id,
            }
            if through_budget:
                status = self.budget_recovery_status()
                new_calls = [
                    value
                    for value in status["provider_call_manifest"]
                    if str(value["request_id"]) not in old_call_ids
                    and value.get("model") == model
                    and value.get("endpoint") == endpoint
                    and value.get("request_provenance_sha256") == provenance_sha256
                ]
                if len(new_calls) > 1:
                    raise InvariantError(
                        f"duplicate paid {name} recovery preflight calls"
                    )
                if new_calls:
                    call = new_calls[0]
                    self._prove_paid_preflight_fugu(call, model)
                    results.append(
                        {
                            "route": name,
                            "result": "success",
                            "reused": True,
                            "request_id": call["request_id"],
                            "provenance_session_id": session_id,
                        }
                    )
                    continue
                url = f"http://127.0.0.1:{self.cfg.budget_port}/v1/" + (
                    "chat/completions" if endpoint == "chat" else "embeddings"
                )
                headers = {
                    "Authorization": f"Bearer {key}",
                    "X-Recovery-Provenance": session_id,
                }
            else:
                existing = [
                    value
                    for value in self.fugu_failure_manifest()
                    if str(value["request_id"]) not in old_failure_by_id
                    and value.get("session_id") == session_id
                ]
                if existing:
                    if len(existing) != 1:
                        raise InvariantError(
                            f"duplicate {name} auth preflight Fugu records"
                        )
                    if any(
                        value.get("requested_model") != model
                        or value.get("agent_id") != agent_id
                        or value.get("http_status") not in {401, 402}
                        for value in existing
                    ):
                        raise InvariantError(
                            f"existing {name} auth preflight provenance drift"
                        )
                    results.append(
                        {
                            "route": name,
                            "result": "expected_auth_failure",
                            "kind": "auth",
                            "session_id": session_id,
                            "reused": True,
                            "request_ids": sorted(
                                str(value["request_id"]) for value in existing
                            ),
                        }
                    )
                    continue
                url = f"http://127.0.0.1:{self.cfg.fugu_port}/v1/" + (
                    "chat/completions" if endpoint == "chat" else "embeddings"
                )
                headers = {
                    "Authorization": f"Bearer {key}",
                    "x-session-id": session_id,
                    "x-agent-id": agent_id,
                }
            try:
                body = self._http_post_json(url, payload, headers=headers, timeout=60)
                if name == "gpt_oss":
                    valid = isinstance(body.get("choices"), list) and bool(
                        body["choices"]
                    )
                else:
                    data = body.get("data")
                    valid = (
                        isinstance(data, list)
                        and bool(data)
                        and isinstance(data[0].get("embedding"), list)
                        and bool(data[0]["embedding"])
                    )
                if not valid:
                    raise InvariantError(f"{name} preflight response is incomplete")
                if through_budget:
                    status = self.budget_recovery_status()
                    new_calls = [
                        value
                        for value in status["provider_call_manifest"]
                        if str(value["request_id"]) not in old_call_ids
                        and value.get("model") == model
                        and value.get("endpoint") == endpoint
                        and value.get("request_provenance_sha256") == provenance_sha256
                    ]
                    if len(new_calls) != 1:
                        raise InvariantError(
                            f"paid {name} preflight lacks one exact budget call"
                        )
                    self._prove_paid_preflight_fugu(new_calls[0], model)
                    results.append(
                        {
                            "route": name,
                            "result": "success",
                            "reused": False,
                            "request_id": new_calls[0]["request_id"],
                            "provenance_session_id": session_id,
                        }
                    )
                else:
                    results.append(
                        {
                            "route": name,
                            "result": "success",
                            "session_id": session_id,
                        }
                    )
            except InvariantError:
                raise
        current_manifest = self.fugu_failure_manifest()
        current_by_id = {str(value["request_id"]): value for value in current_manifest}
        if any(
            current_by_id.get(request_id) != value
            for request_id, value in old_failure_by_id.items()
        ):
            raise InvariantError(
                "Fugu failure ledger changed an existing preflight baseline"
            )
        additions = [
            value
            for value in current_manifest
            if str(value["request_id"]) not in old_failure_by_id
        ]
        expected_sessions = {
            f"{session_root}-gpt_oss",
            f"{session_root}-qwen",
        }
        if not through_budget and any(
            value.get("session_id") not in expected_sessions
            or value.get("agent_id") != agent_id
            for value in additions
        ):
            raise InvariantError(
                "Fugu failure additions are not bound to the exact recovery preflight"
            )
        if additions:
            raise InvariantError(
                "successful recovery preflight unexpectedly added Fugu failures"
            )
        provider_calls: list[dict[str, Any]] = []
        provider_fugu_successes: list[dict[str, Any]] = []
        if through_budget:
            status = self.budget_recovery_status()
            if status.get("reservations") or self._http_json(
                f"http://127.0.0.1:{self.cfg.budget_port}/health"
            ).get("circuit_open"):
                raise InvariantError("paid preflight budget is not drained/closed")
            result_ids = {str(value["request_id"]) for value in results}
            provider_calls = [
                value
                for value in status["provider_call_manifest"]
                if str(value["request_id"]) in result_ids
            ]
            if len(provider_calls) != 2:
                raise InvariantError("paid preflights lack two exact call proofs")
            calls_by_id = {str(value["request_id"]): value for value in provider_calls}
            for result in results:
                call = calls_by_id.get(str(result.get("request_id") or ""))
                provenance_session_id = str(result.get("provenance_session_id") or "")
                if (
                    not call
                    or call.get("request_provenance_sha256")
                    != hashlib.sha256(provenance_session_id.encode()).hexdigest()
                ):
                    raise InvariantError(
                        "paid preflight request provenance digest drift"
                    )
            provider_fugu_successes = [
                self._prove_paid_preflight_fugu(value, str(value.get("model") or ""))
                for value in provider_calls
            ]
            if spend_before is None:
                raise InvariantError("paid preflight spend baseline is absent")
            health = self._http_json(f"http://127.0.0.1:{self.cfg.budget_port}/health")
            expected_provider_delta = sum(
                (Decimal(str(value.get("cost_usd") or 0)) for value in provider_calls),
                Decimal("0"),
            )
            provider_delta = Decimal(
                str(health.get("provider_spent_usd") or 0)
            ) - Decimal(str(spend_before.get("provider_spent_usd") or 0))
            if abs(provider_delta - expected_provider_delta) > Decimal("0.000000001"):
                raise InvariantError("paid preflight provider spend delta drift")
        proof = {
            "results": results,
            "session_root": session_root,
            "agent_id": agent_id,
            "through_budget": through_budget,
            "baseline_sha256": hashlib.sha256(
                json.dumps(
                    baseline_manifest, separators=(",", ":"), sort_keys=True
                ).encode()
            ).hexdigest(),
            "authorized_fugu_failures": additions,
            "authorized_fugu_request_ids": sorted(
                str(value["request_id"]) for value in additions
            ),
            "provider_calls": provider_calls,
            "provider_fugu_successes": provider_fugu_successes,
        }
        event(
            self.cfg,
            "provider_model_preflights_complete",
            results=results,
            authorized_fugu_request_ids=proof["authorized_fugu_request_ids"],
        )
        return proof

    def budget_recovery_status(self) -> dict[str, Any]:
        # BAT is the host-side deploy-state name for the generated, distinct
        # HINDSIGHT_BUDGET_ADMIN_API_KEY injected into the budget proxy VM.
        # It is deliberately not HINDSIGHT_PROXY_TOKEN (the inference key).
        token = str(self.hindsight_state.get("BAT") or "")
        if not token:
            raise InvariantError("budget admin API credential is absent")
        status = self._http_json(
            f"http://127.0.0.1:{self.cfg.budget_port}/admin/reservations",
            headers={"Authorization": f"Bearer {token}"},
        )
        health = status.get("health")
        if not isinstance(health, dict):
            raise InvariantError("budget lock-coherent health snapshot is absent")
        health_in_flight = health.get("in_flight")
        if type(health_in_flight) is not int or health_in_flight < 0:
            raise InvariantError(
                "budget lock-coherent health in-flight count is malformed"
            )
        reservations = status.get("reservations")
        if not isinstance(reservations, list):
            raise InvariantError("budget reservation manifest is absent")
        canonical = sorted(
            (dict(value) for value in reservations),
            key=lambda value: str(value.get("request_id") or ""),
        )
        if canonical != reservations:
            raise InvariantError(
                "budget reservation manifest is not canonically sorted"
            )
        request_ids = [str(value.get("request_id") or "") for value in canonical]
        if not all(request_ids) or len(request_ids) != len(set(request_ids)):
            raise InvariantError("budget reservation IDs are missing or duplicated")
        if health_in_flight != len(canonical):
            raise InvariantError(
                "budget health/reservation in-flight count drift"
            )
        encoded = json.dumps(
            canonical,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        digest = hashlib.sha256(encoded).hexdigest()
        if digest != str(status.get("sha256") or ""):
            raise InvariantError("budget reservation manifest digest mismatch")
        total = canonical_reservation_total(canonical)
        reported_total = status.get("total_max_usd")
        if (
            isinstance(reported_total, bool)
            or not isinstance(reported_total, (int, float))
            or not math.isfinite(float(reported_total))
        ):
            raise InvariantError("budget reservation maximum-cost total is malformed")
        # The proxy serializes its direct Python-float sum.  Comparing that
        # exact JSON number avoids a false drift from independently summing
        # Decimal(str(cost)) values (for example, 0.1 + 0.2).
        if total != reported_total:
            raise InvariantError("budget reservation maximum-cost total drift")
        provider_history = status.get("provider_failure_history")
        if not isinstance(provider_history, list):
            raise InvariantError("budget provider failure history is absent")
        history_ids: list[str] = []
        provider_manifest: list[dict[str, Any]] = []
        for value in provider_history:
            if not isinstance(value, dict):
                raise InvariantError("budget provider failure history is malformed")
            request_id = str(value.get("request_id") or "")
            session_id = str(value.get("fugu_session_id") or "")
            agent_id = str(value.get("fugu_agent_id") or "")
            model = str(value.get("model") or "")
            try:
                status_code = int(value["status_code"])
            except (KeyError, TypeError, ValueError) as exc:
                raise InvariantError(
                    "budget provider failure history status is malformed"
                ) from exc
            if (
                not request_id
                or session_id != request_id
                or not 400 <= status_code <= 599
                or (
                    model == self.cfg.expected_hindsight_model
                    and agent_id != "hindsight-backfill"
                )
                or (model == QWEN_MODEL and agent_id != "hindsight-qwen-budget")
                or model not in {self.cfg.expected_hindsight_model, QWEN_MODEL}
            ):
                raise InvariantError(
                    "budget provider failure history provenance is invalid"
                )
            history_ids.append(request_id)
            provider_manifest.append(
                _exact_record(
                    value,
                    "request_id",
                    "status_code",
                    "retry_after",
                    "model",
                    "endpoint",
                    "scope",
                    "period",
                    "fugu_session_id",
                    "fugu_agent_id",
                    "request_provenance_sha256",
                    "recovery_probe_route",
                    "recovery_auth_incident_request_id",
                    "recovery_gate_sha256",
                )
            )
        if len(history_ids) != len(set(history_ids)):
            raise InvariantError("budget provider failure history IDs are duplicated")
        status["provider_failure_manifest"] = provider_manifest
        provider_calls = status.get("provider_call_history")
        if not isinstance(provider_calls, list):
            raise InvariantError("budget provider call history is absent")
        provider_ledger_version = status.get("provider_ledger_version")
        if type(provider_ledger_version) is not int or provider_ledger_version not in {
            LEGACY_PROVIDER_LEDGER_VERSION,
            PROVIDER_LEDGER_VERSION,
        }:
            raise InvariantError("budget provider ledger version is unsupported")
        call_ids: list[str] = []
        call_manifest: list[dict[str, Any]] = []
        legacy_prefix_open = provider_ledger_version == LEGACY_PROVIDER_LEDGER_VERSION
        for value in provider_calls:
            if not isinstance(value, dict):
                raise InvariantError("budget provider call history is malformed")
            request_id = str(value.get("request_id") or "")
            reconciled = value.get("ambiguous_upstream_reconciled") is True
            strict = value.get("provider_call_recorded") is True
            legacy = legacy_prefix_open and _is_exact_legacy_provider_call(value)
            if reconciled:
                provenance_valid = (
                    value.get("provider_call_recorded") is None
                    and value.get("reservation_manifest_sha256") is None
                    and value.get("estimated_from_reservation") is True
                )
            elif strict:
                legacy_prefix_open = False
                provenance_valid = (
                    type(value.get("estimated_from_reservation")) is bool
                    and re.fullmatch(
                        r"[0-9a-f]{64}",
                        str(value.get("reservation_manifest_sha256") or ""),
                    )
                    is not None
                )
            else:
                provenance_valid = legacy
            if (
                not request_id
                or not provenance_valid
                or str(value.get("model") or "")
                not in {self.cfg.expected_hindsight_model, QWEN_MODEL}
                or str(value.get("endpoint") or "") not in {"chat", "embeddings"}
            ):
                raise InvariantError("budget provider call provenance is invalid")
            call_ids.append(request_id)
            call_manifest.append(
                _exact_record(
                    value,
                    "request_id",
                    "model",
                    "endpoint",
                    "scope",
                    "period",
                    "cost_usd",
                    "provider_call_recorded",
                    "ambiguous_upstream_reconciled",
                    "reservation_manifest_sha256",
                    "request_provenance_sha256",
                )
            )
        if len(call_ids) != len(set(call_ids)):
            raise InvariantError("budget provider call history IDs are duplicated")
        status["provider_call_manifest"] = call_manifest
        return status

    def _wait_hindsight_ready(self, timeout: float = 180) -> None:
        deadline = time.monotonic() + timeout
        last_error = "health did not respond"
        while time.monotonic() < deadline:
            self.touch_controller("waiting_hindsight_ready")
            try:
                health = self._http_json(
                    f"http://127.0.0.1:{self.cfg.hindsight_port}/health"
                )
                if (
                    health.get("status") == "healthy"
                    and health.get("database") == "connected"
                ):
                    return
                last_error = str(health)
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            self.sleep_with_heartbeat(2, "waiting_hindsight_ready")
        raise InvariantError(f"Hindsight did not become ready after roll: {last_error}")

    def budget_final_hold_is_exact(self) -> bool:
        budget = self._http_json(f"http://127.0.0.1:{self.cfg.budget_port}/health")
        return exact_final_budget_hold(budget)

    def request_budget_final_hold(self) -> None:
        token = str(self.hindsight_state.get("BAT") or "")
        if not token:
            raise InvariantError("budget admin API credential is absent")
        self._http_post_json(
            self.cfg.budget_maintenance_hold_url,
            {"reason": FINAL_BUDGET_HOLD_REASON},
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        if not self.budget_final_hold_is_exact():
            raise InvariantError(
                "authoritative final budget maintenance hold was not observed"
            )
        event(self.cfg, "budget_final_hold_confirmed", reason=FINAL_BUDGET_HOLD_REASON)

    def recovery_control_status(self) -> dict[str, Any]:
        token = str(self.hindsight_state.get("BAT") or "")
        if not token:
            raise InvariantError("budget admin API credential is absent")
        return self._http_json(
            f"http://127.0.0.1:{self.cfg.budget_port}/admin/recovery-controls",
            headers={"Authorization": f"Bearer {token}"},
        )

    def prove_recovery_controls(
        self,
        *,
        nonce: str,
        roll_sha256: str,
        reconcile_enabled: bool,
        auth_reset_enabled: bool,
    ) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-f]{32}", nonce) or not re.fullmatch(
            r"[0-9a-f]{64}", roll_sha256
        ):
            raise InvariantError("recovery-control nonce/fingerprint is invalid")
        status = self.recovery_control_status()
        if (
            status.get("expected_nonce_sha256")
            != hashlib.sha256(nonce.encode()).hexdigest()
            or status.get("expected_roll_sha256") != roll_sha256
        ):
            raise InvariantError(
                "deployed recovery-control nonce/fingerprint does not match"
            )
        expected = {
            "reconcile": reconcile_enabled,
            "auth_reset": auth_reset_enabled,
        }
        for name, enabled in expected.items():
            marker = status.get(name)
            if not isinstance(marker, dict) or any(
                (
                    marker.get("present") is not True,
                    marker.get("valid") is not True,
                    marker.get("nonce_matches") is not True,
                    marker.get("roll_matches") is not True,
                    marker.get("enabled") is not enabled,
                    marker.get("secret_present") is not enabled,
                    marker.get("completed") is not True,
                )
            ):
                raise InvariantError(
                    f"deployed {name} recovery-control marker is not exact"
                )
        should_be_sealed = not reconcile_enabled and not auth_reset_enabled
        if bool(status.get("sealed_disabled")) is not should_be_sealed:
            raise InvariantError("recovery-control sealed-disabled proof drift")
        return status

    def _adopt_deployed_pending_recovery_control(
        self,
        marker: dict[str, str],
        *,
        expected_roll_sha256: str,
        reconcile_enabled: bool,
        auth_reset_enabled: bool,
    ) -> dict[str, str] | None:
        pending_nonce = str(marker.get("RECOVERY_CONTROL_PENDING_NONCE") or "")
        pending_sha = str(marker.get("RECOVERY_CONTROL_PENDING_ROLL_SHA256") or "")
        pending_compose = str(marker.get("RECOVERY_CONTROL_PENDING_COMPOSE_HASH") or "")
        pending_vm = str(marker.get("RECOVERY_CONTROL_PENDING_VM_ID") or "")
        if not any((pending_nonce, pending_sha, pending_compose, pending_vm)):
            return None
        if (
            not re.fullmatch(r"[0-9a-f]{32}", pending_nonce)
            or pending_sha != expected_roll_sha256
            or (pending_compose and not re.fullmatch(r"[0-9a-f]{64}", pending_compose))
            or (
                pending_vm
                and not re.fullmatch(
                    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                    pending_vm,
                )
            )
            or (pending_vm and not pending_compose)
        ):
            raise InvariantError(
                "pending recovery-control state does not match requested roll"
            )
        raw_status = self.recovery_control_status()
        if (
            raw_status.get("expected_nonce_sha256")
            != hashlib.sha256(pending_nonce.encode()).hexdigest()
        ):
            # A pending nonce is written before any remote mutation.  Seeing
            # an older VM with a different nonce is the one safe signal to
            # retry the same fingerprint.
            return None
        if not pending_compose or not pending_vm:
            raise InvariantError(
                "deployed pending recovery-control state lacks exact compose/VM identity"
            )
        self.prove_recovery_controls(
            nonce=pending_nonce,
            roll_sha256=pending_sha,
            reconcile_enabled=reconcile_enabled,
            auth_reset_enabled=auth_reset_enabled,
        )
        result = subprocess.run(
            [
                "bash",
                "deploy/hindsight-node.sh",
                "hindsight-node",
                "adopt-recovery-control",
                pending_nonce,
                pending_sha,
                pending_compose,
                pending_vm,
            ],
            cwd=self.cfg.root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=120,
            check=False,
        )
        if result.returncode:
            raise InvariantError(
                "recovery-control adoption failed: " + result.stdout[-1000:]
            )
        promoted = self._hindsight_deployment_marker()
        if (
            promoted.get("RECOVERY_CONTROL_NONCE") != pending_nonce
            or promoted.get("RECOVERY_CONTROL_ROLL_SHA256") != pending_sha
            or promoted.get("RECOVERY_CONTROL_COMPOSE_HASH") != pending_compose
            or promoted.get("RECOVERY_CONTROL_VM_ID") != pending_vm
            or promoted.get("H") != pending_compose
            or promoted.get("VM_ID") != pending_vm
            or promoted.get("RECOVERY_CONTROL_PENDING_NONCE")
            or promoted.get("RECOVERY_CONTROL_PENDING_ROLL_SHA256")
            or promoted.get("RECOVERY_CONTROL_PENDING_COMPOSE_HASH")
            or promoted.get("RECOVERY_CONTROL_PENDING_VM_ID")
        ):
            raise InvariantError("recovery-control pending promotion was not exact")
        return promoted

    def _hindsight_recovery_roll(
        self,
        *,
        reconcile_ambiguous: bool,
        reset_provider_auth: bool,
        reset_token: str = "",
        reconcile_manifest_sha256: str = "",
    ) -> str:
        expected_roll_sha256 = recovery_control_roll_sha256(
            reconcile_ambiguous=reconcile_ambiguous,
            reset_provider_auth=reset_provider_auth,
            reset_token=reset_token,
            reconcile_manifest_sha256=reconcile_manifest_sha256,
        )
        before_marker = self._hindsight_deployment_marker()
        if (
            not before_marker.get("RECOVERY_CONTROL_PENDING_NONCE")
            and not before_marker.get("RECOVERY_CONTROL_PENDING_ROLL_SHA256")
            and before_marker.get("RECOVERY_CONTROL_ROLL_SHA256")
            == expected_roll_sha256
            and before_marker.get("RECOVERY_CONTROL_NONCE")
        ):
            if before_marker.get("RECOVERY_CONTROL_COMPOSE_HASH") != before_marker.get(
                "H"
            ) or before_marker.get("RECOVERY_CONTROL_VM_ID") != before_marker.get(
                "VM_ID"
            ):
                raise InvariantError(
                    "deployed recovery-control compose/VM identity drift"
                )
            self.prove_recovery_controls(
                nonce=before_marker["RECOVERY_CONTROL_NONCE"],
                roll_sha256=expected_roll_sha256,
                reconcile_enabled=reconcile_ambiguous,
                auth_reset_enabled=reset_provider_auth,
            )
            self._wait_hindsight_ready()
            return "reused exact deployed recovery-control roll"
        adopted = self._adopt_deployed_pending_recovery_control(
            before_marker,
            expected_roll_sha256=expected_roll_sha256,
            reconcile_enabled=reconcile_ambiguous,
            auth_reset_enabled=reset_provider_auth,
        )
        if adopted is not None:
            self._wait_hindsight_ready()
            event(
                self.cfg,
                "hindsight_recovery_roll_adopted",
                roll_sha256=expected_roll_sha256,
            )
            return "adopted deployed pending recovery-control roll"
        env = dict(os.environ)
        env.update(
            {
                "HINDSIGHT_RECONCILE_AMBIGUOUS_ENABLED": (
                    "1" if reconcile_ambiguous else "0"
                ),
                "HINDSIGHT_RESET_PROVIDER_AUTH_CIRCUIT_ENABLED": (
                    "1" if reset_provider_auth else "0"
                ),
                "HINDSIGHT_PROVIDER_AUTH_RESET_TOKEN": reset_token,
                "HINDSIGHT_RECONCILE_MANIFEST_SHA256": (reconcile_manifest_sha256),
                "HINDSIGHT_LLM_MAX_CONCURRENT": "3",
            }
        )
        if reconcile_ambiguous and not reconcile_manifest_sha256:
            raise InvariantError(
                "ambiguous reconciliation roll lacks an exact manifest digest"
            )
        command = (
            "set -euo pipefail; source deploy/env.sh >/dev/null; "
            "exec deploy/hindsight-node.sh hindsight-node update"
        )
        output_file = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        process = subprocess.Popen(
            ["bash", "-lc", command],
            cwd=self.cfg.root,
            env=env,
            text=True,
            stdout=output_file,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 1200
        try:
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    process.terminate()
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        process.wait(timeout=10)
                    if process.poll() is None:
                        process.kill()
                    raise InvariantError("Hindsight recovery roll timed out")
                self.sleep_with_heartbeat(2, "hindsight_recovery_roll")
            output_file.seek(0)
            output = output_file.read()
        finally:
            output_file.close()
        if process.returncode:
            marker = self._hindsight_deployment_marker()
            adopted = self._adopt_deployed_pending_recovery_control(
                marker,
                expected_roll_sha256=expected_roll_sha256,
                reconcile_enabled=reconcile_ambiguous,
                auth_reset_enabled=reset_provider_auth,
            )
            if adopted is None:
                raise InvariantError(
                    f"Hindsight recovery roll failed ({process.returncode}): "
                    f"{output[-2000:]}"
                )
        self.touch_controller("hindsight_recovery_roll_health_gate")
        self._wait_hindsight_ready()
        marker = self._hindsight_deployment_marker()
        nonce = str(marker.get("RECOVERY_CONTROL_NONCE") or "")
        if (
            marker.get("RECOVERY_CONTROL_ROLL_SHA256") != expected_roll_sha256
            or marker.get("RECOVERY_CONTROL_COMPOSE_HASH") != marker.get("H")
            or marker.get("RECOVERY_CONTROL_VM_ID") != marker.get("VM_ID")
            or marker.get("RECOVERY_CONTROL_PENDING_NONCE")
            or marker.get("RECOVERY_CONTROL_PENDING_ROLL_SHA256")
            or marker.get("RECOVERY_CONTROL_PENDING_COMPOSE_HASH")
            or marker.get("RECOVERY_CONTROL_PENDING_VM_ID")
        ):
            raise InvariantError(
                "completed recovery roll did not promote its exact control state"
            )
        self.prove_recovery_controls(
            nonce=nonce,
            roll_sha256=expected_roll_sha256,
            reconcile_enabled=reconcile_ambiguous,
            auth_reset_enabled=reset_provider_auth,
        )
        event(
            self.cfg,
            "hindsight_recovery_roll_complete",
            reconcile_ambiguous=reconcile_ambiguous,
            reset_provider_auth=reset_provider_auth,
            recovery_control_roll_sha256=expected_roll_sha256,
        )
        return output

    def _hindsight_deployment_marker(self) -> dict[str, str]:
        state_path = self.cfg.root / "deploy/logs/hindsight-node-hindsight-node.state"
        state = _env_file(state_path)
        marker = {
            key: str(state.get(key) or "")
            for key in (
                "UPDATE_SEQUENCE",
                "UPDATED_AT",
                "H",
                "VM_ID",
                "RECOVERY_CONTROL_SEQUENCE",
                "RECOVERY_CONTROL_STATE_VERSION",
                "RECOVERY_CONTROL_NONCE",
                "RECOVERY_CONTROL_ROLL_SHA256",
                "RECOVERY_CONTROL_COMPOSE_HASH",
                "RECOVERY_CONTROL_VM_ID",
                "RECOVERY_CONTROL_PENDING_NONCE",
                "RECOVERY_CONTROL_PENDING_ROLL_SHA256",
                "RECOVERY_CONTROL_PENDING_COMPOSE_HASH",
                "RECOVERY_CONTROL_PENDING_VM_ID",
            )
        }
        # Existing sealed deployments predate UPDATE_SEQUENCE/UPDATED_AT.  H
        # and VM_ID form the exact legacy before-marker; the first successful
        # reviewed roll must introduce/change one of the durable roll fields.
        if not marker["H"] or not marker["VM_ID"]:
            raise InvariantError("Hindsight deployment marker is incomplete")
        current_keys = (
            "RECOVERY_CONTROL_NONCE",
            "RECOVERY_CONTROL_ROLL_SHA256",
            "RECOVERY_CONTROL_COMPOSE_HASH",
            "RECOVERY_CONTROL_VM_ID",
        )
        pending_keys = (
            "RECOVERY_CONTROL_PENDING_NONCE",
            "RECOVERY_CONTROL_PENDING_ROLL_SHA256",
            "RECOVERY_CONTROL_PENDING_COMPOSE_HASH",
            "RECOVERY_CONTROL_PENDING_VM_ID",
        )
        current = [marker[key] for key in current_keys]
        pending = [marker[key] for key in pending_keys]
        if any(current) or any(pending) or marker["RECOVERY_CONTROL_STATE_VERSION"]:
            if marker["RECOVERY_CONTROL_STATE_VERSION"] != "2":
                raise InvariantError(
                    "recovery-control deployment state version is not exactly 2"
                )
            if any(current) and (
                re.fullmatch(r"[0-9a-f]{32}", current[0]) is None
                or re.fullmatch(r"[0-9a-f]{64}", current[1]) is None
                or re.fullmatch(r"[0-9a-f]{64}", current[2]) is None
                or re.fullmatch(
                    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                    current[3],
                )
                is None
            ):
                raise InvariantError(
                    "deployed recovery-control current identity is partial"
                )
            if any(pending) and (
                re.fullmatch(r"[0-9a-f]{32}", pending[0]) is None
                or re.fullmatch(r"[0-9a-f]{64}", pending[1]) is None
                or (pending[2] and re.fullmatch(r"[0-9a-f]{64}", pending[2]) is None)
                or (
                    pending[3]
                    and (
                        not pending[2]
                        or re.fullmatch(
                            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                            pending[3],
                        )
                        is None
                    )
                )
            ):
                raise InvariantError(
                    "deployed recovery-control pending identity is partial"
                )
            if not any(current) and not any(pending):
                raise InvariantError("recovery-control version lacks an identity")
        return marker

    def _auth_reset_proof(
        self,
        status: dict[str, Any],
        reset_token: str,
        *,
        state: dict[str, Any],
    ) -> dict[str, Any] | None:
        proof = status.get("last_provider_auth_reset")
        if not isinstance(proof, dict):
            return None
        expected = hashlib.sha256(reset_token.encode()).hexdigest()
        incident_id = str(state.get("probe_auth_incident_request_id") or "")
        incident_status = int(state.get("probe_auth_incident_status") or 0)
        if (
            str(proof.get("reset_token_sha256") or "") != expected
            or proof.get("credential_validated_by_exact_paid_probes") is not True
            or str(proof.get("auth_incident_request_id") or "") != incident_id
            or int(proof.get("auth_incident_status") or 0) != incident_status
            or status.get("provider_auth_recovery") is not None
            or status.get("provider_auth_circuit_incident_request_id") is not None
            or status.get("provider_auth_circuit_incident_status") is not None
        ):
            return None
        expected_successes: dict[str, dict[str, Any]] = {}
        for route in ("gpt_oss", "qwen"):
            route_state = dict(dict(state.get("probe_routes") or {}).get(route) or {})
            success = dict(route_state.get("success") or {})
            if route_state.get("terminal") is not True or not success:
                return None
            call = self._recovery_probe_success_record(
                status,
                route=route,
                request_id=str(success.get("request_id") or ""),
                provenance_sha256=str(success.get("request_provenance_sha256") or ""),
                incident_id=incident_id,
            )
            expected_successes[route] = call
        if proof.get("probe_successes") != expected_successes:
            return None
        return dict(proof)

    @staticmethod
    def _reconciliation_proof(
        status: dict[str, Any], snapshot: dict[str, Any]
    ) -> dict[str, Any] | None:
        reservations = list(snapshot.get("reservations") or [])
        expected_ids = [str(value["request_id"]) for value in reservations]
        expected_by_id = {
            str(value["request_id"]): Decimal(str(value["cost_usd"]))
            for value in reservations
        }
        raw = status.get("last_ambiguous_reconciliation")
        if isinstance(raw, dict) and str(raw.get("manifest_sha256") or "") == str(
            snapshot.get("sha256") or ""
        ):
            proof = dict(raw)
            reservation_ids = list(proof.get("reservation_ids") or [])
            repaired_ids = list(proof.get("record_commit_repaired_ids") or [])
            charged_ids = list(proof.get("maximum_charged_ids") or [])
            result = str(proof.get("result") or "")
            if (
                reservation_ids != expected_ids
                or int(proof.get("requests") or -1) != len(expected_ids)
                or repaired_ids != sorted(repaired_ids)
                or charged_ids != sorted(charged_ids)
                or set(repaired_ids) & set(charged_ids)
                or sorted(repaired_ids + charged_ids) != expected_ids
                or result
                not in {
                    "maximum_charged_and_drained",
                    "record_commits_repaired_and_drained",
                    "record_commits_repaired_and_maximum_charged",
                }
            ):
                return None
            expected_charge = sum(
                (expected_by_id[value] for value in charged_ids), Decimal("0")
            )
            observed_charge = Decimal(str(proof.get("charged_maximum_usd") or 0))
            if abs(observed_charge - expected_charge) > Decimal("0.000000001"):
                return None
            return proof

        raw = status.get("last_record_commit_repair")
        if not isinstance(raw, dict):
            return None
        proof = dict(raw)
        repaired_ids = list(proof.get("request_ids") or [])
        if (
            str(proof.get("manifest_sha256") or "") != str(snapshot.get("sha256") or "")
            or int(proof.get("requests") or -1) != len(expected_ids)
            or repaired_ids != expected_ids
            or proof.get("cleared_exact_record_ambiguity") is not True
            or proof.get("result") != "record_commits_repaired_and_drained"
        ):
            return None
        return {
            **proof,
            "reservation_ids": expected_ids,
            "record_commit_repaired_ids": expected_ids,
            "maximum_charged_ids": [],
            "charged_maximum_usd": 0,
        }

    def recover_provider_condition(
        self,
        error: str,
        incident: dict[str, Any],
        *,
        recovery_key: str,
        proven_kind: str | None = None,
        proven_retry_after: str | None = None,
        authorized_provider_failure_ids: list[str] | None = None,
    ) -> bool:
        """Recover provider controls before a proven terminal document retry."""
        state = dict(incident.get("provider_recovery") or {})
        current_kind = state.get("kind")
        current_key = state.get("key")
        covered_keys = {
            str(value)
            for value in state.get("covered_keys")
            or ([current_key] if current_key else [])
        }
        if current_key is not None and recovery_key not in covered_keys:
            operation_kind = proven_kind or provider_failure_kind(error)
            reusable = (
                state.get("stage") == "sealed"
                and (
                    operation_kind == current_kind
                    or (
                        current_kind == "ambiguous"
                        and operation_kind in {"ambiguous", "transient_5xx"}
                    )
                )
                and isinstance(state.get("provider_proof"), dict)
            )
            if reusable:
                covered_keys.add(recovery_key)
                state["covered_keys"] = sorted(covered_keys)
                incident["provider_recovery"] = state
                save_incident(
                    self.cfg,
                    incident,
                    kind="provider_global_proof_reused",
                )
                return True
            if state.get("stage") != "sealed":
                raise InvariantError("provider recovery key changed mid-incident")
            state = {}
            current_kind = None
        budget = self._http_json(f"http://127.0.0.1:{self.cfg.budget_port}/health")
        recovery_status = self.budget_recovery_status()
        kind = str(
            current_kind
            or proven_kind
            or authoritative_provider_failure_kind(error, budget, recovery_status)
        )
        if kind == "terminal":
            return True
        state["kind"] = kind
        state["key"] = recovery_key
        state["covered_keys"] = sorted(covered_keys | {recovery_key})

        if kind in {"rate_limit", "transient_5xx"}:
            if "retry_at" not in state:
                document_id = recovery_key.split(":", 1)[0]
                with self.agent_db() as conn, conn.cursor() as cur:
                    cur.execute(
                        """SELECT count(*)::int AS n
                           FROM hindsight_outbox_retry_history
                           WHERE document_id=%s""",
                        (document_id,),
                    )
                    retry_ordinal = int(cur.fetchone()["n"]) + 1
                default = bounded_backoff(
                    retry_ordinal,
                    self.cfg.controller_initial_backoff,
                    self.cfg.controller_max_backoff,
                )
                delay = min(
                    provider_retry_after(
                        " ".join(
                            value
                            for value in (error, proven_retry_after or "")
                            if value
                        ),
                        default,
                    ),
                    self.cfg.controller_max_backoff,
                )
                state["retry_ordinal"] = retry_ordinal
                state["retry_after_seconds"] = delay
                state["retry_at"] = time.time() + delay
                state["stage"] = "backoff"
                incident["provider_recovery"] = state
                save_incident(self.cfg, incident, kind="provider_backoff_started")
                return False
            if time.time() < float(state["retry_at"]):
                return False
            state["stage"] = "sealed"
            state["provider_proof"] = {
                "kind": kind,
                "retry_ordinal": int(state["retry_ordinal"]),
                "retry_after_seconds": float(state["retry_after_seconds"]),
            }
            incident["provider_recovery"] = state
            save_incident(self.cfg, incident, kind="provider_backoff_completed")
            return True

        if kind == "auth":
            stage = str(state.get("stage") or "new")
            if stage == "new":
                reset_token = secrets.token_hex(32)
                state.update(
                    {
                        "stage": "recovery_probes_started",
                        "reset_token": reset_token,
                    }
                )
                incident["provider_recovery"] = state
                save_incident(
                    self.cfg,
                    incident,
                    kind="provider_auth_recovery_probe_state_started",
                )
                stage = "recovery_probes_started"
            if stage == "recovery_probes_started":
                if not self.provider_auth_recovery_probes(
                    state,
                    incident,
                    authorized_incident_ids=list(authorized_provider_failure_ids or []),
                ):
                    return False
                state = dict(incident.get("provider_recovery") or state)
                stage = str(state.get("stage") or "")
            if stage == "preflights_passed":
                state["reset_before_marker"] = self._hindsight_deployment_marker()
                state["stage"] = "reset_started"
                incident["provider_recovery"] = state
                save_incident(self.cfg, incident, kind="provider_auth_reset_started")
                stage = "reset_started"
            if stage == "reset_started":
                reset_token = str(state["reset_token"])
                before = dict(state.get("reset_before_marker") or {})
                if not before:
                    raise InvariantError(
                        "provider-auth reset lacks its pre-roll deployment marker"
                    )
                pre_reset_status = self.budget_recovery_status()
                self._provider_auth_recovery_root(
                    pre_reset_status,
                    incident_id=str(state["probe_auth_incident_request_id"]),
                    incident_status=int(state["probe_auth_incident_status"]),
                    incident_at=str(state["probe_auth_incident_at"]),
                    required=True,
                )
                self._hindsight_recovery_roll(
                    reconcile_ambiguous=False,
                    reset_provider_auth=True,
                    reset_token=reset_token,
                )
                current = self._hindsight_deployment_marker()
                proof = self._auth_reset_proof(
                    self.budget_recovery_status(),
                    reset_token,
                    state=state,
                )
                if proof is None:
                    raise InvariantError(
                        "provider-auth reset lacks an exact durable endpoint proof"
                    )
                budget = self._http_json(
                    f"http://127.0.0.1:{self.cfg.budget_port}/health"
                )
                if (
                    budget.get("status") not in {"healthy", "ok"}
                    or budget.get("circuit_open")
                    or int(budget.get("in_flight") or 0)
                ):
                    raise InvariantError(
                        "provider-auth reset did not close a drained budget"
                    )
                state["auth_reset_proof"] = proof
                state["reset_after_marker"] = current
                state["reset_deployment_transition_observed"] = current != before
                state["reset_token_sha256"] = hashlib.sha256(
                    reset_token.encode()
                ).hexdigest()
                state["seal_before_marker"] = current
                state["stage"] = "seal_started"
                incident["provider_recovery"] = state
                save_incident(self.cfg, incident, kind="provider_auth_reset_complete")
                stage = "seal_started"
            if stage == "seal_started":
                before = dict(state.get("seal_before_marker") or {})
                if not before:
                    raise InvariantError(
                        "provider-auth seal lacks its pre-roll deployment marker"
                    )
                self._hindsight_recovery_roll(
                    reconcile_ambiguous=False,
                    reset_provider_auth=False,
                )
                current = self._hindsight_deployment_marker()
                budget = self._http_json(
                    f"http://127.0.0.1:{self.cfg.budget_port}/health"
                )
                if budget.get("circuit_open") or int(budget.get("in_flight") or 0):
                    raise InvariantError(
                        "provider-auth sealed deployment is not drained"
                    )
                state["seal_after_marker"] = current
                state["post_reset_fugu_baseline"] = self.fugu_failure_manifest()
                post_reset_budget = self.budget_recovery_status()
                state["post_reset_provider_call_baseline"] = list(
                    post_reset_budget.get("provider_call_manifest") or []
                )
                post_reset_health = self._http_json(
                    f"http://127.0.0.1:{self.cfg.budget_port}/health"
                )
                state["post_reset_spend_before"] = {
                    "provider_spent_usd": post_reset_health.get("provider_spent_usd"),
                    "hindsight_backfill_spent_usd": post_reset_health.get(
                        "hindsight_backfill_spent_usd"
                    ),
                }
                state["stage"] = "post_reset_preflight_started"
                incident["provider_recovery"] = state
                save_incident(self.cfg, incident, kind="provider_auth_sealed_disabled")
                stage = "post_reset_preflight_started"
            if stage == "post_reset_preflight_started":
                state["post_reset_preflights"] = self.provider_model_preflights(
                    provenance_token=f"post-reset:{state['reset_token']}",
                    baseline_manifest=list(state["post_reset_fugu_baseline"]),
                    through_budget=True,
                    provider_call_baseline=list(
                        state["post_reset_provider_call_baseline"]
                    ),
                    spend_before=dict(state["post_reset_spend_before"]),
                )
                state.pop("reset_token", None)
                preflight_proofs = [
                    dict(state.get("pre_reset_preflights") or {}),
                    dict(state.get("post_reset_preflights") or {}),
                ]
                authorized_failures = [
                    dict(value)
                    for proof_value in preflight_proofs
                    for value in proof_value.get("authorized_fugu_failures", [])
                ]
                authorized_provider_failures = [
                    dict(value)
                    for value in dict(state.get("pre_reset_preflights") or {}).get(
                        "authorized_provider_failures", []
                    )
                ]
                state["provider_proof"] = {
                    "kind": "auth",
                    "reset_token_sha256": state["reset_token_sha256"],
                    "auth_reset_proof": state["auth_reset_proof"],
                    "pre_reset_preflights": state.get("pre_reset_preflights", []),
                    "post_reset_preflights": state.get("post_reset_preflights", []),
                    "authorized_fugu_failures": authorized_failures,
                    "authorized_fugu_request_ids": sorted(
                        str(value["request_id"]) for value in authorized_failures
                    ),
                    "authorized_provider_failures": authorized_provider_failures,
                    "authorized_provider_failure_ids": sorted(
                        str(value["request_id"])
                        for value in authorized_provider_failures
                    ),
                    "seal_before_marker": state["seal_before_marker"],
                    "seal_after_marker": state["seal_after_marker"],
                }
                state["stage"] = "sealed"
                incident["provider_recovery"] = state
                save_incident(
                    self.cfg,
                    incident,
                    kind="provider_auth_post_reset_preflight_passed_and_sealed",
                )
            return state.get("stage") == "sealed"

        if kind == "ambiguous":
            stage = str(state.get("stage") or "new")
            if stage == "new":
                reservations = list(recovery_status.get("reservations") or [])
                if (
                    not budget.get("circuit_open")
                    or provider_failure_kind(str(budget.get("circuit_reason") or ""))
                    != "ambiguous"
                    or not reservations
                    or int(budget.get("in_flight") or 0) != len(reservations)
                ):
                    raise InvariantError(
                        "ambiguous recovery requires an exact open reservation manifest"
                    )
                with self.agent_db() as conn, conn.cursor() as cur:
                    cur.execute(
                        "SELECT count(*)::int AS n FROM hindsight_outbox WHERE state='succeeded'"
                    )
                    succeeded = int(cur.fetchone()["n"])
                projection = compute_projection(
                    budget.get("hindsight_backfill_spent_usd", 0),
                    succeeded,
                    self.cfg,
                )
                maximum_projection = projection + Decimal(
                    str(recovery_status.get("total_max_usd") or 0)
                )
                if maximum_projection >= self.cfg.hard_budget:
                    raise InvariantError(
                        "maximum-charge reconciliation exceeds hard budget"
                    )
                snapshot = {
                    "reservations": reservations,
                    "sha256": str(recovery_status.get("sha256") or ""),
                    "total_max_usd": str(recovery_status.get("total_max_usd") or 0),
                }
                state.update(
                    {
                        "stage": "snapshot",
                        "snapshot_at": time.time(),
                        "reservation_manifest": snapshot,
                        "spent_before": str(
                            budget.get("hindsight_backfill_spent_usd", 0)
                        ),
                        "provider_spent_before": str(
                            budget.get("provider_spent_usd", 0)
                        ),
                        "reason": budget.get("circuit_reason"),
                    }
                )
                incident["provider_recovery"] = state
                save_incident(
                    self.cfg, incident, kind="ambiguous_reservations_snapshotted"
                )
                return False
            if stage == "snapshot":
                if time.time() - float(state["snapshot_at"]) < self.cfg.full_seconds:
                    return False
                budget = self._http_json(
                    f"http://127.0.0.1:{self.cfg.budget_port}/health"
                )
                recovery_status = self.budget_recovery_status()
                snapshot = dict(state.get("reservation_manifest") or {})
                if (
                    list(recovery_status.get("reservations") or [])
                    != list(snapshot.get("reservations") or [])
                    or str(recovery_status.get("sha256") or "")
                    != str(snapshot.get("sha256") or "")
                    or Decimal(str(recovery_status.get("total_max_usd") or 0))
                    != Decimal(str(snapshot.get("total_max_usd") or 0))
                    or int(budget.get("in_flight") or 0)
                    != len(snapshot.get("reservations") or [])
                    or str(budget.get("hindsight_backfill_spent_usd", 0))
                    != str(state["spent_before"])
                    or budget.get("circuit_reason") != state["reason"]
                ):
                    raise InvariantError("ambiguous reservation snapshot changed")
                state["stage"] = "reconcile_started"
                incident["provider_recovery"] = state
                save_incident(
                    self.cfg, incident, kind="ambiguous_reconciliation_started"
                )
                stage = "reconcile_started"
            if stage == "reconcile_started":
                snapshot = dict(state.get("reservation_manifest") or {})
                status = self.budget_recovery_status()
                proof = self._reconciliation_proof(status, snapshot)
                if proof is None:
                    if list(status.get("reservations") or []) != list(
                        snapshot.get("reservations") or []
                    ) or str(status.get("sha256") or "") != str(
                        snapshot.get("sha256") or ""
                    ):
                        raise InvariantError(
                            "ambiguous reservation manifest changed before roll"
                        )
                self._hindsight_recovery_roll(
                    reconcile_ambiguous=True,
                    reset_provider_auth=False,
                    reconcile_manifest_sha256=str(snapshot["sha256"]),
                )
                status = self.budget_recovery_status()
                proof = self._reconciliation_proof(status, snapshot)
                if proof is None:
                    raise InvariantError(
                        "ambiguous reconciliation lacks an exact durable endpoint proof"
                    )
                if status.get("reservations"):
                    raise InvariantError("reconciled reservation set was not drained")
                after = self._http_json(
                    f"http://127.0.0.1:{self.cfg.budget_port}/health"
                )
                if after.get("circuit_open") or int(after.get("in_flight") or 0):
                    raise InvariantError(
                        "ambiguous reconciliation did not drain/close budget"
                    )
                delta = Decimal(
                    str(after.get("hindsight_backfill_spent_usd", 0))
                ) - Decimal(str(state["spent_before"]))
                provider_delta = Decimal(
                    str(after.get("provider_spent_usd", 0))
                ) - Decimal(str(state["provider_spent_before"]))
                charged = Decimal(str(proof.get("charged_maximum_usd") or 0))
                tolerance = Decimal("0.000000001")
                if (
                    abs(delta - charged) > tolerance
                    or abs(provider_delta - charged) > tolerance
                ):
                    raise InvariantError("reconciled maximum-charge spend delta drift")
                state.update(
                    {
                        "stage": "reconciled",
                        "charged_maximum_usd": str(charged),
                        "reconciliation_proof": proof,
                    }
                )
                incident["provider_recovery"] = state
                save_incident(
                    self.cfg, incident, kind="ambiguous_reconciliation_complete"
                )
                stage = "reconciled"
            if stage == "reconciled":
                state["seal_before_marker"] = self._hindsight_deployment_marker()
                state["stage"] = "seal_started"
                incident["provider_recovery"] = state
                save_incident(
                    self.cfg, incident, kind="ambiguous_reconciliation_seal_started"
                )
                stage = "seal_started"
            if stage == "seal_started":
                before = dict(state.get("seal_before_marker") or {})
                if not before:
                    raise InvariantError(
                        "ambiguous reconciliation seal lacks pre-roll marker"
                    )
                self._hindsight_recovery_roll(
                    reconcile_ambiguous=False,
                    reset_provider_auth=False,
                )
                current = self._hindsight_deployment_marker()
                status = self.budget_recovery_status()
                if status.get("reservations"):
                    raise InvariantError("sealed reconciliation regained reservations")
                proof = self._reconciliation_proof(
                    status, dict(state["reservation_manifest"])
                )
                if proof is None:
                    raise InvariantError(
                        "sealed reconciliation lost its durable endpoint proof"
                    )
                state["seal_after_marker"] = current
                state["provider_proof"] = {
                    "kind": "ambiguous",
                    "reservation_manifest": state["reservation_manifest"],
                    "reconciliation_proof": proof,
                    "seal_before_marker": before,
                    "seal_after_marker": current,
                }
                state["stage"] = "sealed"
                incident["provider_recovery"] = state
                save_incident(
                    self.cfg, incident, kind="ambiguous_reconciliation_sealed"
                )
            return state.get("stage") == "sealed"
        raise InvariantError(f"unsupported provider failure kind {kind}")

    def recover_incident_rows(self, incident: dict[str, Any]) -> dict[str, Any]:
        """Reconcile one exact incident without ever calling retain_item."""
        rows = self._incident_rows()
        if not rows:
            if incident.pop("provider_recovery", None) is not None:
                save_incident(
                    self.cfg, incident, kind="provider_recovery_episode_complete"
                )
            archived = self.close_incident_authorization(
                incident, reason="all authorized incident rows reconciled"
            )
            proof = (
                archived.get("completed_refresh_proof")
                if isinstance(archived, dict)
                else None
            )
            if isinstance(proof, dict):
                return {"status": "reconciled", "count": int(proof["count"])}
            return {"status": "no_incident_rows"}
        if len(rows) > self.cfg.max_agent_active:
            raise InvariantError("incident row count exceeds Agent concurrency bound")
        worker = self._app_worker()
        server = Path.home() / "agent-session-mcp/server"
        outbox = self._reviewed_outbox_module(server / "outbox.py")
        OperationlessProof = outbox.OperationlessProof
        adopt_operation = outbox.adopt_operation
        recover_operationless_claim = outbox.recover_operationless_claim
        retry_terminal_document = outbox.retry_terminal_document

        authorization = self.prepare_incident_authorization(incident, rows)
        if authorization.get("terminal_pending_child_operation_ids"):
            return {
                "status": "waiting_remote_lineage",
                "operation_ids": list(
                    authorization["terminal_pending_child_operation_ids"]
                ),
            }
        # Never reconcile a global reservation set while any sibling lineage
        # can still be producing provider work.
        pending_siblings: list[str] = []
        for row in rows:
            self.touch_controller(
                "incident_recovery",
                incident_stage="row_proof",
                document_id=str(row["document_id"]),
                recovery_kind="sibling_activity",
            )
            operation_id = str(row.get("operation_id") or "")
            if not operation_id:
                continue
            operation = self._remote_operation(operation_id)
            if operation and str(operation.get("status") or "").lower() in {
                "pending",
                "processing",
            }:
                pending_siblings.append(operation_id)
        if pending_siblings:
            return {
                "status": "waiting_remote",
                "operation_ids": sorted(pending_siblings),
            }

        durable_kinds = {
            (
                "auth"
                if int(status) in {401, 402}
                else "rate_limit"
                if int(status) == 429
                else "transient_5xx"
            )
            for status in authorization.get("provider_statuses", [])
        }
        durable_provider_kind = (
            next(iter(durable_kinds)) if len(durable_kinds) == 1 else None
        )
        retry_after_values = [
            str(value)
            for value in authorization.get("provider_retry_after", [])
            if value not in {None, ""}
        ]
        durable_retry_after = (
            str(
                min(
                    self.cfg.controller_max_backoff,
                    max(provider_retry_after(value, 0) for value in retry_after_values),
                )
            )
            if retry_after_values
            else None
        )
        base_attempt_authorization = {
            "authorized_failure_operation_ids": list(
                authorization.get("authorized_failure_operation_ids", [])
            ),
            "authorized_fugu_failures": list(
                authorization.get("authorized_fugu_failures", [])
            ),
            "authorized_fugu_request_ids": list(
                authorization.get("authorized_fugu_request_ids", [])
            ),
            "authorized_provider_failures": list(
                authorization.get("authorized_provider_failures", [])
            ),
            "authorized_provider_failure_ids": list(
                authorization.get("authorized_provider_failure_ids", [])
            ),
            "allowed_http_statuses": sorted(
                {
                    int(value["http_status"])
                    for value in authorization.get("authorized_fugu_failures", [])
                    if value.get("http_status") is not None
                }
            ),
            "allow_null_http_status": any(
                value.get("http_status") is None
                for value in authorization.get("authorized_fugu_failures", [])
            ),
        }

        waiting: list[dict[str, Any]] = []
        for row in rows:
            document_id = str(row["document_id"])
            self.touch_controller(
                "incident_recovery",
                incident_stage="row_proof",
                document_id=document_id,
                recovery_kind="classification",
            )
            payload_hash = str(row["payload_hash"])
            item = dict(row["item"])
            encoded = json.dumps(
                item, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
            if (
                hashlib.sha256(encoded.encode()).hexdigest() != payload_hash
                or item.get("document_id") != document_id
                or (
                    row.get("submitted_payload_hash")
                    and row["submitted_payload_hash"] != payload_hash
                )
            ):
                raise InvariantError(f"incident row provenance drift for {document_id}")

            operation_id = str(row.get("operation_id") or "")
            operation = self._remote_operation(operation_id) if operation_id else None
            completed_lineage_proof: Any | None = None
            terminal_lineage_proof: Any | None = None
            if operation_id:
                if not operation:
                    raise InvariantError(
                        f"incident operation lineage/hash drift for {document_id}"
                    )
                remote_status = str(operation["status"]).lower()
                if remote_status == "completed":
                    _parent, child = self._prove_completed_batch_parent(row, operation)
                    child_operation_id = str(child.get("operation_id") or "")
                    if not child_operation_id:
                        raise InvariantError(
                            f"completed retain child lacks operation ID for {document_id}"
                        )
                    completed_lineage_proof = outbox.CompletedLineageProof(
                        document_id=document_id,
                        parent_operation_id=operation_id,
                        child_operation_id=child_operation_id,
                        payload_hash=payload_hash,
                    )
                elif remote_status in {"failed", "cancelled"}:
                    terminal_lineage = self._terminal_lineage_records(row, operation)
                    if not terminal_lineage:
                        waiting.append(
                            {
                                "status": "waiting_remote_lineage",
                                "document_id": document_id,
                            }
                        )
                        continue
                    terminal_children = [
                        value
                        for value in terminal_lineage
                        if str(value.get("operation_type") or "").lower() == "retain"
                        and str(value.get("status") or "").lower()
                        in {"failed", "cancelled"}
                    ]
                    if len(terminal_children) != 1:
                        raise InvariantError(
                            f"terminal retain child lineage is not exact for {document_id}"
                        )
                    child_operation_id = str(
                        terminal_children[0].get("operation_id") or ""
                    )
                    terminal_lineage_proof = outbox.TerminalLineageProof(
                        document_id=document_id,
                        parent_operation_id=operation_id,
                        child_operation_id=child_operation_id,
                        payload_hash=payload_hash,
                    )
                elif not operation_matches_outbox(
                    operation,
                    document_id=document_id,
                    payload_hash=payload_hash,
                    operation_id=operation_id,
                    operation_types=frozenset({"batch_retain"}),
                ):
                    # Active parents still carry the canonical task payload.
                    # Terminal parents are proved above through their exact
                    # single child when Hindsight clears the parent payload.
                    raise InvariantError(
                        f"incident operation lineage/hash drift for {document_id}"
                    )
                remote_document = self._remote_document(document_id)
                document_present = remote_document is not None
                if remote_status in {"pending", "processing"}:
                    waiting.append(
                        {"status": "waiting_remote", "document_id": document_id}
                    )
                    continue
                if remote_status == "completed":
                    if not document_present:
                        raise InvariantError(
                            f"completed operation document absent for {document_id}"
                        )
                    if not self._remote_document_matches_item(remote_document, item):
                        raise InvariantError(
                            f"completed operation document/hash drift for {document_id}"
                        )
                    attempt = {
                        **base_attempt_authorization,
                        "key": f"completed:{document_id}:{operation_id}",
                        "kind": "completed_refresh",
                        "document_id": document_id,
                        "operation_id": operation_id,
                        "child_operation_id": child_operation_id,
                        "payload_hash": payload_hash,
                        "remote_status": remote_status,
                        "remote_document_present": True,
                        "adoption_reason": (
                            "controller adoption of exact completed operation"
                        ),
                        "expected_history_delta": {
                            "retry": 0,
                            "claim": 0,
                            "adoption": (
                                1 if row["state"] in {"blocked", "submitting"} else 0
                            ),
                        },
                    }
                    self.mark_pending_acceptance(incident, attempt)
                    with self.incident_bank_mutation(
                        document_id, "completed_refresh"
                    ) as conn:
                        if row["state"] in {"blocked", "submitting"}:
                            adopted = adopt_operation(
                                conn,
                                document_id,
                                operation_id,
                                expected_payload_hash=payload_hash,
                                remote_status=remote_status,
                                remote_document_present=True,
                                reason="controller adoption of exact completed operation",
                            )
                            if not adopted:
                                raise InvariantError(
                                    "exact completed operation was not adopted"
                                )
                        completed, failed = worker._refresh_submitted(
                            conn,
                            only_operations=frozenset({(document_id, operation_id)}),
                            completed_lineage_proofs=frozenset(
                                {completed_lineage_proof}
                            ),
                        )
                        if failed or completed != 1:
                            raise InvariantError(
                                f"completed refresh result {(completed, failed)}"
                            )
                    self.accept_recovered_attempt(attempt)
                    self.clear_pending_acceptance(incident)
                    continue
                if remote_status not in {"failed", "cancelled"} or document_present:
                    raise InvariantError(
                        f"terminal recovery proof failed for {document_id}: "
                        f"status={remote_status} present={document_present}"
                    )
                error = str(
                    operation.get("error_message") or row.get("last_error") or ""
                )
                if not self.recover_provider_condition(
                    error,
                    incident,
                    recovery_key=f"{document_id}:{operation_id}",
                    proven_kind=(
                        durable_provider_kind
                        if provider_failure_kind(error) in {"ambiguous", "terminal"}
                        else None
                    ),
                    proven_retry_after=durable_retry_after,
                    authorized_provider_failure_ids=list(
                        base_attempt_authorization["authorized_provider_failure_ids"]
                    ),
                ):
                    return {
                        "status": "waiting_provider_recovery",
                        "document_id": document_id,
                        "provider_kind": provider_failure_kind(error),
                    }
                provider_state = dict(incident.get("provider_recovery") or {})
                provider_proof = None
                if f"{document_id}:{operation_id}" in {
                    str(value)
                    for value in provider_state.get("covered_keys")
                    or [provider_state.get("key")]
                }:
                    if provider_state.get("stage") != "sealed":
                        raise InvariantError(
                            "terminal retry lacks sealed provider recovery state"
                        )
                    provider_proof = json.loads(
                        json.dumps(provider_state.get("provider_proof") or {})
                    )
                    if not provider_proof:
                        raise InvariantError(
                            "terminal retry lacks durable provider recovery proof"
                        )
                authorized_fugu = {
                    str(value["request_id"]): dict(value)
                    for value in base_attempt_authorization["authorized_fugu_failures"]
                }
                if provider_proof:
                    for value in provider_proof.get("authorized_fugu_failures", []):
                        authorized_fugu[str(value["request_id"])] = dict(value)
                authorized_provider = {
                    str(value["request_id"]): dict(value)
                    for value in base_attempt_authorization[
                        "authorized_provider_failures"
                    ]
                }
                if provider_proof:
                    for value in provider_proof.get("authorized_provider_failures", []):
                        authorized_provider[str(value["request_id"])] = dict(value)
                allowed_http = sorted(
                    set(base_attempt_authorization["allowed_http_statuses"])
                    | {
                        int(value)
                        for value in re.findall(r"\b([1-5]\d\d)\b", error)
                        if int(value) in {401, 402, 429} or 500 <= int(value) <= 599
                    }
                )
                attempt = {
                    **base_attempt_authorization,
                    "key": f"terminal:{document_id}:{operation_id}",
                    "kind": "terminal_retry",
                    "document_id": document_id,
                    "operation_id": operation_id,
                    "child_operation_id": child_operation_id,
                    "payload_hash": payload_hash,
                    "remote_status": remote_status,
                    "remote_error": error[:1000],
                    "allowed_http_statuses": allowed_http,
                    "allow_null_http_status": bool(
                        base_attempt_authorization["allow_null_http_status"]
                        or (
                            provider_proof and provider_proof.get("kind") == "ambiguous"
                        )
                    ),
                    "provider_recovery_proof": provider_proof,
                    "remote_document_present": False,
                    "adoption_reason": (
                        "controller adoption of exact terminal operation"
                    ),
                    "authorized_fugu_failures": list(authorized_fugu.values()),
                    "authorized_fugu_request_ids": sorted(authorized_fugu),
                    "authorized_provider_failures": list(authorized_provider.values()),
                    "authorized_provider_failure_ids": sorted(authorized_provider),
                    "expected_history_delta": {
                        "retry": 1,
                        "claim": 0,
                        "adoption": (
                            1 if row["state"] in {"blocked", "submitting"} else 0
                        ),
                    },
                }
                self.mark_pending_acceptance(incident, attempt)
                with self.incident_bank_mutation(
                    document_id, "terminal_retry"
                ) as conn:
                    if row["state"] == "submitted":
                        completed, failed = worker._refresh_submitted(
                            conn,
                            allowed_terminal_circuit_failures=frozenset(
                                {(document_id, operation_id)}
                            ),
                            only_operations=frozenset({(document_id, operation_id)}),
                            terminal_lineage_proofs=frozenset({terminal_lineage_proof}),
                        )
                        if completed or failed != 1:
                            raise InvariantError(
                                f"terminal refresh result {(completed, failed)}"
                            )
                    elif row["state"] in {"blocked", "submitting"}:
                        adopted = adopt_operation(
                            conn,
                            document_id,
                            operation_id,
                            expected_payload_hash=payload_hash,
                            remote_status=remote_status,
                            remote_document_present=False,
                            reason="controller adoption of exact terminal operation",
                        )
                        if not adopted:
                            raise InvariantError("terminal operation was not adopted")
                        completed, failed = worker._refresh_submitted(
                            conn,
                            allowed_terminal_circuit_failures=frozenset(
                                {(document_id, operation_id)}
                            ),
                            only_operations=frozenset({(document_id, operation_id)}),
                            terminal_lineage_proofs=frozenset({terminal_lineage_proof}),
                        )
                        if completed or failed != 1:
                            raise InvariantError(
                                f"adopted terminal refresh result {(completed, failed)}"
                            )
                    retried = retry_terminal_document(
                        conn,
                        document_id,
                        operation_id,
                        expected_payload_hash=payload_hash,
                        reason="controller retry after exact terminal failure proof",
                        preserve_queue_position=True,
                    )
                    if not retried:
                        raise InvariantError("exact terminal document was not requeued")
                self.accept_recovered_attempt(attempt)
                self.clear_pending_acceptance(incident)
                continue

            operationless_snapshot = self._operationless_remote_snapshot(
                document_id, payload_hash
            )
            remote_document = operationless_snapshot["document"]
            document_present = bool(operationless_snapshot["document_present"])
            historical_terminal = self._explained_terminal_operation_ids(document_id)
            matches = [
                value
                for value in operationless_snapshot["matches"]
                if not (
                    (
                        str(value["operation_id"]) in historical_terminal
                        or str(
                            (value.get("result_metadata") or {}).get(
                                "parent_operation_id"
                            )
                            or ""
                        )
                        in historical_terminal
                    )
                    and str(value.get("status") or "").lower()
                    in {"failed", "cancelled"}
                )
            ]
            if matches:
                # Parent and child operations can both contain the document.
                # Every exact non-parent match must be a retain child linked
                # to the one exact batch parent; a unique parent alone does
                # not make an unrelated exact child safe to adopt.
                try:
                    parent = unique_exact_batch_parent(matches)
                except InvariantError as exc:
                    raise InvariantError(f"{exc} for {document_id}") from exc
                parent_id = str(parent["operation_id"])
                if document_present and not self._remote_document_matches_item(
                    remote_document, item
                ):
                    raise InvariantError(
                        f"operationless remote document/hash drift for {document_id}"
                    )
                attempt = {
                    **base_attempt_authorization,
                    "key": f"adopted:{document_id}:{parent_id}",
                    "kind": "operation_adoption",
                    "document_id": document_id,
                    "operation_id": parent_id,
                    "payload_hash": payload_hash,
                    "remote_status": str(parent["status"]),
                    "remote_document_present": document_present,
                    "adoption_reason": (
                        "controller adopted unique exact remote parent"
                    ),
                    "expected_history_delta": {
                        "retry": 0,
                        "claim": 0,
                        "adoption": 1,
                    },
                }
                self.mark_pending_acceptance(incident, attempt)
                with self.incident_bank_mutation(
                    document_id, "operation_adoption"
                ) as conn:
                    adopted = adopt_operation(
                        conn,
                        document_id,
                        parent_id,
                        expected_payload_hash=payload_hash,
                        remote_status=str(parent["status"]),
                        remote_document_present=document_present,
                        reason="controller adopted unique exact remote parent",
                    )
                    if not adopted:
                        raise InvariantError(
                            "unique exact remote parent was not adopted"
                        )
                self.accept_recovered_attempt(attempt)
                self.clear_pending_acceptance(incident)
                continue

            if document_present:
                raise InvariantError(
                    f"operationless row has document without operation for {document_id}"
                )
            checked_at = float(operationless_snapshot["checked_at"])
            proof_key = f"{document_id}:{payload_hash}:{row.get('claimed_at')}"
            proofs = dict(incident.get("absence_proofs") or {})
            previous = proofs.get(proof_key)
            current = {
                "checked_at": checked_at,
                "document_present": False,
                "operation_count": 0,
                "hindsight_active": int(operationless_snapshot["hindsight_active"]),
                "provider_in_flight_before": int(
                    operationless_snapshot["provider_in_flight_before"]
                ),
                "provider_in_flight_after": int(
                    operationless_snapshot["provider_in_flight_after"]
                ),
            }
            if not previous:
                stable_operationless_absence(
                    None,
                    current,
                    minimum_interval_seconds=self.cfg.full_seconds,
                )
                proofs[proof_key] = [current]
                incident["absence_proofs"] = proofs
                save_incident(self.cfg, incident, kind="operationless_absence_proved")
                waiting.append(
                    {"status": "first_absence_proof", "document_id": document_id}
                )
                continue
            values = list(previous)
            if not stable_operationless_absence(
                values[-1],
                current,
                minimum_interval_seconds=self.cfg.full_seconds,
            ):
                waiting.append(
                    {
                        "status": "waiting_second_absence_proof",
                        "document_id": document_id,
                    }
                )
                continue
            values.append(current)
            proofs[proof_key] = values[-2:]
            incident["absence_proofs"] = proofs
            save_incident(self.cfg, incident, kind="operationless_absence_proved")
            attempt = {
                **base_attempt_authorization,
                "key": f"operationless:{proof_key}",
                "kind": "operationless_claim",
                "document_id": document_id,
                "payload_hash": payload_hash,
                "claimed_at": (
                    row["claimed_at"].isoformat() if row.get("claimed_at") else None
                ),
                "expected_history_delta": {
                    "retry": 0,
                    "claim": 1,
                    "adoption": 0,
                },
                "proofs": values[-2:],
            }
            self.mark_pending_acceptance(incident, attempt)
            with self.incident_bank_mutation(
                document_id, "operationless_claim"
            ) as conn:
                recovered = recover_operationless_claim(
                    conn,
                    document_id,
                    expected_payload_hash=payload_hash,
                    expected_claimed_at=row["claimed_at"],
                    proofs=tuple(
                        OperationlessProof(
                            checked_at=datetime.fromtimestamp(
                                float(value["checked_at"]), timezone.utc
                            ),
                            document_present=bool(value["document_present"]),
                            operation_count=int(value["operation_count"]),
                        )
                        for value in values[-2:]
                    ),
                    reason="controller recovery after two exact absence proofs",
                    minimum_proof_interval_seconds=self.cfg.full_seconds,
                    preserve_queue_position=True,
                )
                if not recovered:
                    raise InvariantError("operationless claim was not recovered")
            self.accept_recovered_attempt(attempt)
            self.clear_pending_acceptance(incident)
        if incident.pop("provider_recovery", None) is not None:
            save_incident(self.cfg, incident, kind="provider_recovery_episode_complete")
        if waiting:
            return {"status": "waiting", "rows": waiting}
        self.close_incident_authorization(
            incident, reason="all authorized incident rows reconciled"
        )
        return {"status": "reconciled", "count": len(rows)}

    def finalize_drained_incident_authorization(
        self,
        incident: dict[str, Any],
        *,
        active: int,
        errors: int,
        hindsight_active: int,
        provider_in_flight: int,
    ) -> dict[str, Any] | None:
        """Close an orphan authorization after a crash at the final boundary."""
        if (
            not isinstance(incident.get("incident_authorization"), dict)
            or active
            or errors
            or hindsight_active
            or provider_in_flight
        ):
            return None
        reason = "controller finalizing drained incident authorization"
        self.persist_agent_open(reason)
        self.ensure_pocket_open(reason)
        return self.recover_incident_rows(incident)

    def configure_stage(
        self,
        *,
        cap: int,
        max_agent_active: int,
        max_provider_in_flight: int,
    ) -> dict[str, Any]:
        if (cap, max_agent_active, max_provider_in_flight) != (
            self.cfg.stage_cap,
            self.cfg.max_agent_active,
            self.cfg.max_provider_in_flight,
        ):
            raise InvariantError(
                "requested stage does not match sealed process configuration"
            )
        if not 0 < cap <= self.cfg.approved_total:
            raise InvariantError("stage cap is outside the approved batch")
        phase = load_phase(self.cfg)
        if not self.agent_circuit_is_open():
            raise InvariantError(
                "stage configuration requires Agent open before checkpoint bootstrap"
            )
        with self.pocket_db() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT circuit_open FROM hindsight_sync_state WHERE bank_id='dans-pocket'"
            )
            pocket = cur.fetchone()
        if not pocket or not pocket["circuit_open"]:
            raise InvariantError(
                "stage configuration requires Pocket open before checkpoint bootstrap"
            )
        checkpoint = self.ensure_checkpoint_manifests(phase)
        expected_stage = {
            "approved_total": self.cfg.approved_total,
            "cap": cap,
            "max_agent_active": max_agent_active,
            "max_provider_in_flight": max_provider_in_flight,
        }
        if checkpoint.get("stage") != expected_stage:
            candidate = json.loads(json.dumps(checkpoint))
            candidate["stage"] = expected_stage
            checkpoint = append_checkpoint(
                self.cfg,
                checkpoint,
                candidate,
                reason=f"configured approved stage {cap}",
            )
        audit = self.full_audit(phase)
        if audit["failures"]:
            raise InvariantError("; ".join(audit["failures"]))
        counts = audit["agent"]["counts"]
        attempted = sum(int(counts.get(state, 0)) for state in ATTEMPTED_STATES)
        if attempted >= cap or not audit["agent"]["circuit"]["circuit_open"]:
            raise InvariantError(
                "stage configuration requires an open pre-cap Agent hold"
            )
        if not audit["pocket"]["circuit"]["circuit_open"]:
            raise InvariantError("stage configuration requires Pocket open")
        phase.update(
            {
                "name": "ready",
                "budget": "closed",
                "expected_succeeded": int(counts.get("succeeded", 0)),
                "watchdog_armed": True,
                "controller_required": True,
                "configured_cap": cap,
            }
        )
        save_phase(self.cfg, phase)
        event(self.cfg, "stage_configured", cap=cap)
        return {"phase": phase, "checkpoint": checkpoint, "audit": audit}

    def post_incident_release_candidate(self) -> bool:
        phase = load_phase(self.cfg)
        if phase["name"] not in {"running", "ready"}:
            return False
        if isinstance(phase.get("post_incident_release"), dict):
            return True
        return (
            post_incident_release_evidence(
                phase,
                load_incident(self.cfg),
                read_checkpoint(self.cfg),
            )
            is not None
        )

    def _post_incident_projection(
        self, audit: dict[str, Any], succeeded: int
    ) -> Decimal:
        return compute_projection(
            audit["budget"].get("hindsight_backfill_spent_usd", 0),
            succeeded,
            self.cfg,
        ) + Decimal(str(audit["budget_recovery"].get("total_max_usd") or 0))

    @staticmethod
    def normalize_post_incident_service_receipt(
        evidence_by_service: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        """Validate and normalize exact writer/monitor process identities."""
        receipt: dict[str, dict[str, Any]] = {}
        for service in POST_INCIDENT_RELEASE_SERVICES:
            evidence = evidence_by_service.get(service)
            if not isinstance(evidence, dict):
                raise InvariantError(
                    f"post-incident service receipt is absent for {service}"
                )
            main_pid = evidence.get("MainPID")
            restarts = evidence.get("NRestarts")
            normalized = {
                "ActiveState": str(evidence.get("ActiveState") or ""),
                "SubState": str(evidence.get("SubState") or ""),
                "MainPID": main_pid if type(main_pid) is int else 0,
                "NRestarts": restarts if type(restarts) is int else -1,
            }
            if (
                evidence.get("returncode") != 0
                or normalized["ActiveState"] != "active"
                or normalized["SubState"] != "running"
                or normalized["MainPID"] <= 0
                or normalized["NRestarts"] < 0
            ):
                raise InvariantError(
                    f"post-incident service receipt is unhealthy for {service}"
                )
            receipt[service] = normalized
        return receipt

    def post_incident_service_receipt(self) -> dict[str, dict[str, Any]]:
        """Capture the current exact writer/monitor process identities."""
        return self.normalize_post_incident_service_receipt(
            {
                service: _service_evidence(service)
                for service in POST_INCIDENT_RELEASE_SERVICES
            }
        )

    @staticmethod
    def post_incident_budget_recovery_receipt(
        status: dict[str, Any],
    ) -> dict[str, Any]:
        """Bind cheap exact budget ledgers that can change after audit A."""
        reservation_sha256 = str(status.get("sha256") or "")
        provider_ledger_version = status.get("provider_ledger_version")
        provider_failures = status.get("provider_failure_manifest")
        provider_calls = status.get("provider_call_manifest")
        if (
            not re.fullmatch(r"[0-9a-f]{64}", reservation_sha256)
            or type(provider_ledger_version) is not int
            or not isinstance(provider_failures, list)
            or not isinstance(provider_calls, list)
        ):
            raise InvariantError("post-incident budget recovery receipt is incomplete")
        return {
            "reservation_sha256": reservation_sha256,
            "total_max_usd": str(status.get("total_max_usd") or 0),
            "provider_ledger_version": provider_ledger_version,
            "provider_failure_manifest_sha256": _json_digest(
                {"values": provider_failures}
            ),
            "provider_call_manifest_sha256": _json_digest({"values": provider_calls}),
            "last_ambiguous_reconciliation_sha256": _json_digest(
                {"value": status.get("last_ambiguous_reconciliation")}
            ),
        }

    def post_incident_fugu_failure_receipt(
        self, checkpoint: dict[str, Any]
    ) -> dict[str, Any]:
        """Re-read and bind the independent authenticated Fugu failure ledger."""
        with self.fugu_db() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT *
                   FROM fugu_credit_ledger
                   WHERE start_time >= '2026-07-13 10:17:39+00'
                     AND status <> 'success'
                   ORDER BY start_time,request_id"""
            )
            rows = list(cur.fetchall())
        summaries = sorted(
            [
                [
                    row["start_time"].isoformat(),
                    _canonical_http_status(row["http_status"]),
                    str(row["requested_model"]),
                ]
                for row in rows
            ]
        )
        manifest = [
            _exact_record(
                row,
                "request_id",
                "status",
                "http_status",
                "requested_model",
                "session_id",
                "agent_id",
                "agent_role",
            )
            for row in rows
        ]
        accepted = checkpoint.get("accepted")
        if not isinstance(accepted, dict):
            raise InvariantError(
                "post-incident checkpoint lacks accepted Fugu failures"
            )
        accepted_summaries = [
            [str(value[0]), _canonical_http_status(value[1]), str(value[2])]
            for value in accepted.get("fugu_failures") or []
        ]
        accepted_manifest = accepted.get("fugu_failure_manifest")
        if summaries != accepted_summaries or manifest != accepted_manifest:
            raise InvariantError(
                "post-incident authenticated Fugu failure ledger drift"
            )
        return {
            "count": len(rows),
            "summary_sha256": _json_digest({"values": summaries}),
            "manifest_sha256": _json_digest({"values": manifest}),
        }

    @staticmethod
    def _post_incident_remote_documents(snap: dict[str, Any]) -> int:
        value = snap.get("remote_documents", snap.get("hindsight_documents", -1))
        return int(value)

    @staticmethod
    def post_incident_hindsight_operation_receipt(
        operations: dict[str, Any],
    ) -> dict[str, int]:
        """Bind every Hindsight operation status across the abbreviated gate."""
        if not isinstance(operations, dict) or set(operations) - set(
            POST_INCIDENT_HINDSIGHT_STATUSES
        ):
            raise InvariantError(
                "post-incident Hindsight operation status receipt is malformed"
            )
        receipt: dict[str, int] = {}
        for status in POST_INCIDENT_HINDSIGHT_STATUSES:
            value = operations.get(status, 0)
            if type(value) is not int or value < 0:
                raise InvariantError(
                    "post-incident Hindsight operation count is malformed"
                )
            receipt[status] = value
        return receipt

    def prepare_post_incident_release(self) -> dict[str, Any] | None:
        """Persist exact full audit A for a receipt-bound completed-refresh gate."""
        self.require_release_helper_identity()
        phase = load_phase(self.cfg)
        if phase["name"] not in {"running", "ready"}:
            raise InvariantError(
                "post-incident release preparation requires running/ready phase"
            )
        if isinstance(phase.get("post_incident_release"), dict):
            return None
        incident = load_incident(self.cfg)
        checkpoint = read_checkpoint(self.cfg)
        evidence = post_incident_release_evidence(phase, incident, checkpoint)
        if evidence is None:
            return None

        audit = self.full_audit(phase)
        if audit["failures"]:
            raise InvariantError("; ".join(audit["failures"]))
        current_phase = load_phase(self.cfg)
        if (
            int(current_phase["revision"]) != int(phase["revision"])
            or current_phase["name"] != phase["name"]
        ):
            raise InvariantError(
                "phase changed during post-incident release preparation"
            )
        current_incident = load_incident(self.cfg)
        current_checkpoint = read_checkpoint(self.cfg)
        current_evidence = post_incident_release_evidence(
            current_phase, current_incident, current_checkpoint
        )
        if current_evidence != evidence:
            raise InvariantError(
                "completed-refresh evidence changed during release preparation"
            )

        counts = audit["agent"]["counts"]
        attempted = sum(int(counts.get(state, 0)) for state in ATTEMPTED_STATES)
        succeeded = int(counts.get("succeeded", 0))
        active_or_error = sum(
            int(counts.get(state, 0)) for state in ACTIVE_STATES + ERROR_STATES
        )
        if (
            not audit["agent"]["circuit"]["circuit_open"]
            or not audit["pocket"]["circuit"]["circuit_open"]
            or active_or_error
            or attempted != succeeded
            or sum(
                int(audit["hindsight_operations"].get(state, 0))
                for state in ("pending", "processing")
            )
            or int(audit["budget"].get("in_flight") or 0)
            or int(audit["remote_documents"]) != succeeded
        ):
            raise InvariantError(
                "post-incident release preparation is not open and fully drained"
            )
        projection = self._post_incident_projection(audit, succeeded)
        if projection >= self.cfg.hard_budget:
            raise InvariantError(
                f"post-incident projected full batch {projection} exceeds hard budget"
            )
        budget_recovery_receipt = self.post_incident_budget_recovery_receipt(
            audit["budget_recovery"]
        )
        hindsight_operation_receipt = self.post_incident_hindsight_operation_receipt(
            audit["hindsight_operations"]
        )
        fugu_failure_receipt = self.post_incident_fugu_failure_receipt(
            current_checkpoint
        )
        audit_service_receipt = self.normalize_post_incident_service_receipt(
            audit.get("services") or {}
        )
        service_receipt = self.post_incident_service_receipt()
        if service_receipt != audit_service_receipt:
            raise InvariantError(
                "post-incident service identity changed during full audit A"
            )
        prepared_at = time.time()
        ticket = {
            "version": POST_INCIDENT_RELEASE_TICKET_VERSION,
            **evidence,
            "attempted": attempted,
            "succeeded": succeeded,
            "queued": int(counts.get("queued", 0)),
            "remote_documents": int(audit["remote_documents"]),
            "spend": str(audit["budget"].get("hindsight_backfill_spent_usd", 0)),
            "projection": str(projection),
            "budget_recovery_receipt": budget_recovery_receipt,
            "hindsight_operation_receipt": hindsight_operation_receipt,
            "fugu_failure_receipt": fugu_failure_receipt,
            "service_receipt": service_receipt,
            "prepared_at": prepared_at,
            "ttl_seconds": POST_INCIDENT_RELEASE_TTL_SECONDS,
            "expires_at": prepared_at + POST_INCIDENT_RELEASE_TTL_SECONDS,
        }
        ticket["ticket_sha256"] = _json_digest(ticket)
        phase.update(
            {
                "name": "ready",
                "expected_succeeded": succeeded,
                "post_incident_release": ticket,
            }
        )
        for name in (
            "release_baseline_attempted",
            "release_target",
            "release_started_at",
        ):
            phase.pop(name, None)
        save_phase(self.cfg, phase)
        event(
            self.cfg,
            "post_incident_release_prepared",
            authorization_sha256=evidence["authorization_sha256"],
            recovered_attempt_keys=evidence["attempt_keys"],
            succeeded=succeeded,
            projection=str(projection),
        )
        return audit

    def validate_post_incident_release(
        self, phase: dict[str, Any], snap: dict[str, Any]
    ) -> None:
        """Validate a short-lived quick proof immediately before release."""
        ticket = phase.get("post_incident_release")
        if (
            not isinstance(ticket, dict)
            or int(ticket.get("version", 0)) != POST_INCIDENT_RELEASE_TICKET_VERSION
        ):
            raise PostIncidentReleaseInvalid(
                "post-incident release ticket is absent or invalid"
            )
        digest_body = dict(ticket)
        ticket_sha256 = str(digest_body.pop("ticket_sha256", ""))
        if not ticket_sha256 or ticket_sha256 != _json_digest(digest_body):
            raise PostIncidentReleaseInvalid(
                "post-incident release ticket digest drift"
            )
        failures = list(snap.get("failures") or [])
        if failures:
            raise PostIncidentReleaseInvalid("; ".join(failures))
        try:
            prepared_at = float(ticket["prepared_at"])
            ttl_seconds = float(ticket["ttl_seconds"])
            expires_at = float(ticket["expires_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PostIncidentReleaseInvalid(
                "post-incident release ticket timing is malformed"
            ) from exc
        now = time.time()
        if (
            not math.isfinite(prepared_at)
            or not math.isfinite(ttl_seconds)
            or not math.isfinite(expires_at)
            or ttl_seconds != POST_INCIDENT_RELEASE_TTL_SECONDS
            or expires_at != prepared_at + ttl_seconds
            or now < prepared_at
            or now > expires_at
        ):
            raise PostIncidentReleaseInvalid(
                "post-incident release ticket is expired or has timing drift"
            )
        checkpoint = read_checkpoint(self.cfg)
        evidence = post_incident_release_evidence(
            phase, load_incident(self.cfg), checkpoint
        )
        evidence_fields = (
            "authorization_sha256",
            "row_sha256",
            "archive_closed_at",
            "attempt_keys",
            "lineage_sha256",
            "checkpoint_revision",
            "checkpoint_sha256",
        )
        if evidence is None or any(
            evidence.get(name) != ticket.get(name) for name in evidence_fields
        ):
            raise PostIncidentReleaseInvalid("post-incident release evidence drift")
        try:
            service_receipt = self.post_incident_service_receipt()
            budget_recovery_receipt = self.post_incident_budget_recovery_receipt(
                snap["budget_recovery"]
            )
            hindsight_operation_receipt = (
                self.post_incident_hindsight_operation_receipt(
                    snap["hindsight_operations"]
                )
            )
            fugu_failure_receipt = self.post_incident_fugu_failure_receipt(checkpoint)
        except (InvariantError, KeyError, TypeError, ValueError) as exc:
            raise PostIncidentReleaseInvalid(str(exc)) from exc
        if service_receipt != ticket.get("service_receipt"):
            raise PostIncidentReleaseInvalid(
                "post-incident release service identity/restart drift"
            )
        if budget_recovery_receipt != ticket.get("budget_recovery_receipt"):
            raise PostIncidentReleaseInvalid(
                "post-incident release budget recovery ledger drift"
            )
        if hindsight_operation_receipt != ticket.get("hindsight_operation_receipt"):
            raise PostIncidentReleaseInvalid(
                "post-incident release Hindsight operation status drift"
            )
        if fugu_failure_receipt != ticket.get("fugu_failure_receipt"):
            raise PostIncidentReleaseInvalid(
                "post-incident authenticated Fugu failure receipt drift"
            )
        counts = snap["agent"]["counts"]
        attempted = sum(int(counts.get(state, 0)) for state in ATTEMPTED_STATES)
        succeeded = int(counts.get("succeeded", 0))
        projection = self._post_incident_projection(snap, succeeded)
        remote_documents = self._post_incident_remote_documents(snap)
        if (
            not bool(phase.get("initial_wave_verified"))
            or not snap["agent"]["circuit"]["circuit_open"]
            or not snap["pocket"]["circuit"]["circuit_open"]
            or sum(int(counts.get(state, 0)) for state in ACTIVE_STATES + ERROR_STATES)
            or attempted != succeeded
            or sum(
                int(snap["hindsight_operations"].get(state, 0))
                for state in ("pending", "processing")
            )
            or int(snap["budget"].get("in_flight") or 0)
            or remote_documents != succeeded
            or attempted != int(ticket.get("attempted", -1))
            or succeeded != int(ticket.get("succeeded", -1))
            or int(counts.get("queued", 0)) != int(ticket.get("queued", -1))
            or remote_documents != int(ticket.get("remote_documents", -1))
            or str(snap["budget"].get("hindsight_backfill_spent_usd", 0))
            != str(ticket.get("spend"))
            or str(projection) != str(ticket.get("projection"))
            or projection >= self.cfg.hard_budget
        ):
            raise PostIncidentReleaseInvalid(
                "post-incident release audit/count/spend/projection drift"
            )

    @staticmethod
    def retire_post_incident_release(phase: dict[str, Any], *, outcome: str) -> None:
        ticket = phase.pop("post_incident_release", None)
        if not isinstance(ticket, dict):
            return
        phase.update(
            {
                "last_post_incident_release_authorization_sha256": str(
                    ticket.get("authorization_sha256") or ""
                ),
                "last_post_incident_release_outcome": outcome,
                "last_post_incident_release_at": time.time(),
            }
        )

    def invalidate_post_incident_release(
        self, phase: dict[str, Any], *, reason: str
    ) -> None:
        """Retire a disproved fast-path ticket and fall back to the full gate."""
        hold_reason = "post-incident release validation failed: " + reason[:800]
        self.persist_agent_open(hold_reason)
        self.ensure_pocket_open(hold_reason)
        current = load_phase(self.cfg)
        expected_ticket = phase.get("post_incident_release")
        current_ticket = current.get("post_incident_release")
        if (
            current["name"] not in {"ready", "release_verifying"}
            or not isinstance(expected_ticket, dict)
            or not isinstance(current_ticket, dict)
            or _json_digest(expected_ticket) != _json_digest(current_ticket)
        ):
            raise InvariantError(
                "phase/ticket changed during post-incident invalidation"
            )
        if current["name"] == "release_verifying":
            current["name"] = "ready"
            for name in (
                "release_baseline_attempted",
                "release_target",
                "release_started_at",
            ):
                current.pop(name, None)
        self.retire_post_incident_release(current, outcome="invalidated")
        current["post_incident_release_invalid_reason"] = reason[:1000]
        save_phase(self.cfg, current)
        if phase is not current:
            phase.clear()
            phase.update(current)
        event(
            self.cfg,
            "post_incident_release_invalidated",
            reason=reason[:1000],
            authorization_sha256=current.get(
                "last_post_incident_release_authorization_sha256"
            ),
        )

    def post_incident_quick_release_audit(
        self,
        phase: dict[str, Any],
        *,
        snap: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Fail closed and retire a ticket on any quick-proof read or drift."""
        try:
            current = self.quick_snapshot() if snap is None else snap
            current["failures"] = self.quick_failures(current, phase)
            self.validate_post_incident_release(phase, current)
            return current
        except Exception as exc:
            invalid = (
                exc
                if isinstance(exc, PostIncidentReleaseInvalid)
                else PostIncidentReleaseInvalid(
                    f"post-incident quick proof read failed: {type(exc).__name__}: {exc}"
                )
            )
            self.invalidate_post_incident_release(phase, reason=str(invalid))
            raise invalid from exc

    def prepare_open_running_hold(self) -> dict[str, Any]:
        self.require_release_helper_identity()
        phase = load_phase(self.cfg)
        if phase["name"] != "running":
            raise InvariantError("open-running preparation requires running phase")
        audit = self.full_audit(phase)
        if audit["failures"]:
            raise InvariantError("; ".join(audit["failures"]))
        counts = audit["agent"]["counts"]
        if not audit["agent"]["circuit"]["circuit_open"]:
            raise InvariantError("open-running preparation requires Agent open")
        if sum(int(counts.get(state, 0)) for state in ACTIVE_STATES + ERROR_STATES):
            return self.prepare_resume()
        if (
            sum(
                int(audit["hindsight_operations"].get(state, 0))
                for state in ("pending", "processing")
            )
            or int(audit["budget"].get("in_flight") or 0)
            or int(audit["remote_documents"]) != int(counts.get("succeeded", 0))
        ):
            raise InvariantError("open-running hold is not fully drained")
        phase.update(
            {
                "name": "ready",
                "expected_succeeded": int(counts.get("succeeded", 0)),
            }
        )
        save_phase(self.cfg, phase)
        event(self.cfg, "running_hold_prepared", succeeded=phase["expected_succeeded"])
        return self.full_audit(phase)

    def finalize(self) -> dict[str, Any]:
        phase = load_phase(self.cfg)
        if self.cfg.stage_cap != self.cfg.approved_total:
            raise InvariantError("finalization requires the full approved stage cap")
        if phase["name"] not in {"settled", "finalizing"}:
            raise InvariantError("finalization requires settled/finalizing phase")
        self.persist_agent_open(FINAL_HOLD_REASON)
        self.ensure_pocket_open(FINAL_HOLD_REASON)
        if phase["name"] == "settled":
            first = self.full_audit(phase)
            if first["failures"]:
                raise InvariantError("; ".join(first["failures"]))
            counts = first["agent"]["counts"]
            if counts != {"succeeded": self.cfg.approved_total}:
                raise InvariantError(f"final Agent counts are not exact: {counts}")
            if (
                first["remote_documents"] != self.cfg.approved_total
                or sum(
                    int(first["hindsight_operations"].get(state, 0))
                    for state in ("pending", "processing")
                )
                or int(first["budget"].get("in_flight") or 0)
            ):
                raise InvariantError("final remote/provider drain proof failed")
            spend = first["budget"].get("hindsight_backfill_spent_usd")
            phase.update(
                {
                    "name": "finalizing",
                    "finalization_stage": "seal_started",
                    "final_spend": spend,
                    "final_seal_before_marker": self._hindsight_deployment_marker(),
                    "finalization_started_at": time.time(),
                }
            )
            save_phase(self.cfg, phase)
        spend = phase.get("final_spend")
        if spend is None:
            raise InvariantError("finalization spend baseline is absent")
        stage = str(phase.get("finalization_stage") or "")
        if stage == "seal_started":
            before = dict(phase.get("final_seal_before_marker") or {})
            if not before:
                raise InvariantError("final seal lacks its pre-roll marker")
            self._hindsight_recovery_roll(
                reconcile_ambiguous=False,
                reset_provider_auth=False,
            )
            current = self._hindsight_deployment_marker()
            self.sleep_with_heartbeat(10, "final_spend_stability")
            self.touch_controller("final_post_roll_audit")
            second = self.full_audit(phase)
            if (
                second["failures"]
                or second["budget"].get("hindsight_backfill_spent_usd") != spend
            ):
                raise InvariantError("final spend/audit stability proof failed")
            phase.update(
                {
                    "finalization_stage": "hold_started",
                    "final_seal_after_marker": current,
                    "final_pre_hold_audited_at": time.time(),
                }
            )
            save_phase(self.cfg, phase)
            stage = "hold_started"
        if stage != "hold_started":
            raise InvariantError("unsupported finalization substage")
        current = self._hindsight_deployment_marker()
        self.prove_recovery_controls(
            nonce=str(current.get("RECOVERY_CONTROL_NONCE") or ""),
            roll_sha256=str(current.get("RECOVERY_CONTROL_ROLL_SHA256") or ""),
            reconcile_enabled=False,
            auth_reset_enabled=False,
        )
        if not self.budget_final_hold_is_exact():
            self.request_budget_final_hold()
        audit_phase = dict(phase)
        audit_phase.update(
            {"budget": "open", "budget_reason": FINAL_BUDGET_HOLD_REASON}
        )
        second = self.full_audit(audit_phase)
        if (
            second["failures"]
            or second["budget"].get("hindsight_backfill_spent_usd") != spend
        ):
            raise InvariantError("final maintenance-hold audit failed")
        projection = compute_projection(spend, self.cfg.approved_total, self.cfg)
        if projection >= self.cfg.hard_budget:
            raise InvariantError("final spend exceeds hard budget")
        phase.update(
            {
                "name": "finalized",
                "budget": "open",
                "budget_reason": FINAL_BUDGET_HOLD_REASON,
                "watchdog_armed": True,
                "expected_succeeded": self.cfg.approved_total,
                "finalized_at": time.time(),
                "final_hold_reason": FINAL_HOLD_REASON,
            }
        )
        save_phase(self.cfg, phase)
        event(
            self.cfg,
            "backfill_finalized",
            succeeded=self.cfg.approved_total,
            spend=spend,
        )
        return second

    def require_release_helper_identity(self) -> None:
        """Hold both banks open until controller and Agent helpers converge."""
        failure = recovery_helper_identity_failure(self.cfg)
        if not failure:
            return
        hold_reason = f"controller staged-helper transition hold: {failure}"
        self.persist_agent_open(hold_reason)
        self.ensure_pocket_open(hold_reason)
        raise InvariantError(failure)

    def close_agent(self, reason: str = "authorized staged recovery release") -> None:
        if not self.controller_owned:
            raise InvariantError(
                "Agent circuit close is restricted to the persistent controller"
            )
        # Final defense adjacent to the only circuit-close transaction.  Even
        # a stale release_verifying phase or a direct method call cannot let a
        # newer controller helper release an older Agent runtime.
        self.require_release_helper_identity()
        with self.bank_lock() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(
                """UPDATE hindsight_sync_state
                   SET circuit_open=false,reason=NULL,opened_at=NULL,updated_at=now()
                   WHERE bank_id='agent-sessions' AND circuit_open=true"""
            )
            if cur.rowcount != 1:
                raise InvariantError("Agent circuit was not exactly one open row")
        event(self.cfg, "agent_closed", reason=reason)

    def _retain_children_for_parent(self, parent_id: str) -> list[dict[str, Any]]:
        with self.hindsight_db() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT operation_id::text,operation_type,status,error_message,
                          result_metadata,task_payload,created_at,updated_at
                   FROM async_operations
                   WHERE bank_id='agent-sessions' AND operation_type='retain'
                     AND result_metadata->>'parent_operation_id'=%s
                   ORDER BY operation_id""",
                (parent_id,),
            )
            return list(cur.fetchall())

    def _prove_completed_batch_parent(
        self,
        row: dict[str, Any],
        parent: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Prove one completed parent through its exact retained child.

        Hindsight may clear a completed batch parent's task payload after it
        fans out.  Only an exactly cleared parent payload may therefore derive
        payload identity from the single retain child.  A non-empty parent
        payload must independently match the outbox row.
        """
        document_id = str(row.get("document_id") or "")
        parent_id = str(row.get("operation_id") or "")
        payload_hash = str(row.get("payload_hash") or "")
        item = dict(row.get("item") or {})
        encoded = json.dumps(
            item,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        parent_metadata = dict(parent.get("result_metadata") or {})
        try:
            items_count = int(parent_metadata.get("items_count"))
            num_sub_batches = int(parent_metadata.get("num_sub_batches"))
        except (TypeError, ValueError):
            items_count = -1
            num_sub_batches = -1
        if (
            not document_id
            or not parent_id
            or not payload_hash
            or item.get("document_id") != document_id
            or hashlib.sha256(encoded.encode()).hexdigest() != payload_hash
            or str(parent.get("operation_id") or "") != parent_id
            or str(parent.get("operation_type") or "").lower() != "batch_retain"
            or str(parent.get("status") or "").lower() != "completed"
            or parent_metadata.get("is_parent") is not True
            or items_count != 1
            or num_sub_batches != 1
        ):
            raise InvariantError(
                f"completed batch parent identity/metadata proof failed for {document_id}"
            )
        parent_payload = parent.get("task_payload")
        parent_payload_cleared = parent_payload is None or (
            isinstance(parent_payload, dict) and not parent_payload
        )
        if not parent_payload_cleared and (
            not isinstance(parent_payload, dict)
            or not operation_matches_outbox(
                parent,
                document_id=document_id,
                payload_hash=payload_hash,
                operation_id=parent_id,
                operation_types=frozenset({"batch_retain"}),
            )
        ):
            raise InvariantError(
                f"completed batch parent payload proof failed for {document_id}"
            )
        children = self._retain_children_for_parent(parent_id)
        if len(children) != 1:
            raise InvariantError(
                f"completed batch parent does not have exactly one retain child for {document_id}"
            )
        child = children[0]
        child_id = str(child.get("operation_id") or "")
        child_payload = child.get("task_payload")
        child_metadata = dict(child.get("result_metadata") or {})
        if (
            not child_id
            or str(child.get("status") or "").lower() != "completed"
            or not isinstance(child_payload, dict)
            or str(child_payload.get("operation_id") or "") != child_id
            or str(child_payload.get("type") or "").lower() != "batch_retain"
            or str(child_metadata.get("parent_operation_id") or "") != parent_id
            or child_metadata.get("document_ids") != [document_id]
            or not operation_matches_outbox(
                child,
                document_id=document_id,
                payload_hash=payload_hash,
                operation_id=child_id,
                operation_types=frozenset({"retain"}),
            )
        ):
            raise InvariantError(
                f"completed retain child lineage/hash proof failed for {document_id}"
            )
        remote_document = self._remote_document(document_id)
        if remote_document is None or not self._remote_document_matches_item(
            remote_document, item
        ):
            raise InvariantError(
                f"completed remote document provenance drift for {document_id}"
            )
        return parent, child

    def _prove_completed_submitted_rows(
        self, rows: list[dict[str, Any]]
    ) -> frozenset[tuple[str, str, str, str]]:
        """Prove exact completed parent+child+document lineage for refresh.

        This is deliberately repeated while the Agent advisory lock is held
        immediately before the reviewed targeted refresh.  The reviewed
        helper then re-reads each exact parent and document once more, making
        a stale pre-lock proof incapable of authorizing a mutation.
        """
        proofs: set[tuple[str, str, str, str]] = set()
        pairs: set[tuple[str, str]] = set()
        for row in rows:
            document_id = str(row.get("document_id") or "")
            operation_id = str(row.get("operation_id") or "")
            payload_hash = str(row.get("payload_hash") or "")
            submitted_hash = str(row.get("submitted_payload_hash") or "")
            item = dict(row.get("item") or {})
            encoded = json.dumps(
                item,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            if (
                not document_id
                or not operation_id
                or item.get("document_id") != document_id
                or hashlib.sha256(encoded.encode()).hexdigest() != payload_hash
                or submitted_hash != payload_hash
            ):
                raise InvariantError(
                    "completed submitted row local payload/hash proof failed"
                )
            pair = (document_id, operation_id)
            if pair in pairs:
                raise InvariantError("duplicate completed submitted operation pair")
            parent = self._remote_operation(operation_id)
            if not parent:
                raise InvariantError(
                    f"completed parent lineage/hash proof failed for {document_id}"
                )
            _parent, child = self._prove_completed_batch_parent(row, parent)
            child_id = str(child.get("operation_id") or "")
            if not child_id:
                raise InvariantError(
                    f"completed retain child lacks operation ID for {document_id}"
                )
            pairs.add(pair)
            proofs.add((document_id, operation_id, child_id, payload_hash))
        return frozenset(proofs)

    def _submitted_rows(self, conn: Any) -> list[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT document_id,operation_id::text,payload_hash,
                          submitted_payload_hash,item
                   FROM hindsight_outbox
                   WHERE state='submitted'
                   ORDER BY document_id"""
            )
            return list(cur.fetchall())

    def prepare_resume(self) -> dict[str, Any]:
        """Refresh proven completed submitted rows while Agent remains open."""
        phase = load_phase(self.cfg)
        audit = self.full_audit(phase)
        if audit["failures"]:
            raise InvariantError("; ".join(audit["failures"]))
        counts = audit["agent"]["counts"]
        attempted = sum(int(counts.get(state, 0)) for state in ATTEMPTED_STATES)
        hindsight_active = sum(
            int(audit["hindsight_operations"].get(state, 0))
            for state in ("pending", "processing")
        )
        gate_failure = precap_resume_failure(
            phase_name=str(phase["name"]),
            agent_open=bool(audit["agent"]["circuit"]["circuit_open"]),
            attempted=attempted,
            succeeded=int(counts.get("succeeded", 0)),
            submitting=int(counts.get("submitting", 0)),
            submitted=int(counts.get("submitted", 0)),
            errors=sum(int(counts.get(state, 0)) for state in ERROR_STATES),
            hindsight_active=hindsight_active,
            provider_in_flight=int(audit["budget"].get("in_flight") or 0),
            remote_documents=int(audit["remote_documents"]),
            max_agent_active=self.cfg.max_agent_active,
            cap=self.cfg.stage_cap,
        )
        if gate_failure:
            raise InvariantError(gate_failure)

        with self.agent_db() as conn:
            rows = self._submitted_rows(conn)
        if not rows:
            raise InvariantError("pre-cap submitted-row operation/hash proof failed")
        proofs = self._prove_completed_submitted_rows(rows)

        outbox = self._reviewed_outbox_module(
            Path.home() / "agent-session-mcp/server/outbox.py"
        )
        worker = self._app_worker()
        with self.bank_lock() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT circuit_open FROM hindsight_sync_state WHERE bank_id='agent-sessions'"
                )
                circuit = cur.fetchone()
            locked_rows = self._submitted_rows(conn)
            if not circuit or not circuit["circuit_open"]:
                raise InvariantError("Agent circuit closed before pre-cap refresh")
            if len(locked_rows) != len(rows):
                raise InvariantError(
                    "submitted-row count changed before pre-cap refresh"
                )
            locked_proofs = self._prove_completed_submitted_rows(locked_rows)
            if locked_proofs != proofs:
                raise InvariantError("submitted row changed before pre-cap refresh")
            adjacent = self.quick_snapshot()
            if sum(
                int(adjacent["hindsight_operations"].get(state, 0))
                for state in ("pending", "processing")
            ) or int(adjacent["budget"].get("in_flight") or 0):
                raise InvariantError(
                    "remote/provider activity appeared before targeted refresh"
                )
            only_operations = frozenset(
                (document_id, parent_id)
                for document_id, parent_id, _child_id, _payload_hash in locked_proofs
            )
            completed_lineage_proofs = frozenset(
                outbox.CompletedLineageProof(
                    document_id=document_id,
                    parent_operation_id=parent_id,
                    child_operation_id=child_id,
                    payload_hash=payload_hash,
                )
                for document_id, parent_id, child_id, payload_hash in locked_proofs
            )
            completed, failed = worker._refresh_submitted(
                conn,
                only_operations=only_operations,
                completed_lineage_proofs=completed_lineage_proofs,
            )
            if (completed, failed) != (len(proofs), 0):
                raise InvariantError(
                    f"pre-cap refresh-only result {(completed, failed)} != ({len(proofs)}, 0)"
                )

        expected_succeeded = int(counts.get("succeeded", 0)) + len(proofs)
        phase.update(
            {
                "name": "ready",
                "budget": "closed",
                "expected_succeeded": expected_succeeded,
            }
        )
        save_phase(self.cfg, phase)
        settled = self.full_audit(phase)
        expected_counts = {
            "queued": self.cfg.approved_total - expected_succeeded,
            "succeeded": expected_succeeded,
        }
        if settled["agent"]["counts"] != expected_counts:
            raise InvariantError(
                f"pre-cap reconciliation counts mismatch: {settled['agent']['counts']} != {expected_counts}"
            )
        if settled["remote_documents"] != expected_succeeded or settled["failures"]:
            raise InvariantError(
                f"pre-cap reconciliation audit failed: {settled['failures']}"
            )
        event(
            self.cfg,
            "precap_hold_reconciled",
            document_ids=sorted(
                document_id for document_id, _parent, _child, _hash in proofs
            ),
            operation_ids=sorted(
                parent_id for _document, parent_id, _child, _hash in proofs
            ),
            succeeded=expected_succeeded,
        )
        return settled

    def release(self) -> dict[str, Any]:
        # Check before reading or advancing a ready/release_verifying phase so
        # every normal, abbreviated, and restart-resume release path remains a
        # non-spending open-circuit hold during a staged helper transition.
        self.require_release_helper_identity()
        phase = load_phase(self.cfg)
        current: dict[str, Any] | None = None
        if phase.get("budget") != "closed" or phase["name"] not in {
            "ready",
            "release_verifying",
        }:
            raise InvariantError(
                "release requires ready or restart-verifying phase and closed budget"
            )
        if phase["name"] == "ready":
            post_incident = isinstance(phase.get("post_incident_release"), dict)
            if post_incident:
                # Audit A is durably bound to the exact completed-refresh
                # archive.  A short-lived quick B must now prove the same
                # counts, spend, budget ledgers, service identities, and
                # zero-activity state.  Initial rollout and every non-refresh
                # recovery retain the ordinary ten-check/two-full gate.
                snap = self.post_incident_quick_release_audit(phase)
            else:
                prior_spend: Any = None
                for index in range(self.cfg.controller_stable_checks):
                    snap = self.quick_snapshot()
                    failures = self.quick_failures(snap, phase)
                    if failures:
                        raise InvariantError(
                            f"release check {index + 1}: {'; '.join(failures)}"
                        )
                    current_spend = snap["budget"].get("hindsight_backfill_spent_usd")
                    if prior_spend is not None and current_spend != prior_spend:
                        raise InvariantError(
                            "spend changed during release stability window"
                        )
                    prior_spend = current_spend
                    if index in {
                        0,
                        self.cfg.controller_stable_checks - 1,
                    }:
                        full = self.full_audit(phase)
                        if full["failures"]:
                            raise InvariantError("; ".join(full["failures"]))
                    if index < self.cfg.controller_stable_checks - 1:
                        self.sleep_with_heartbeat(2, "release_stability")
            counts = snap["agent"]["counts"]
            baseline = sum(int(counts.get(state, 0)) for state in ATTEMPTED_STATES)
            queued = int(counts.get("queued", 0))
            target = min(
                self.cfg.max_agent_active,
                self.cfg.stage_cap - baseline,
                queued,
            )
            if target <= 0:
                raise InvariantError("release has no authorized claim capacity")
            if (
                not bool(phase.get("initial_wave_verified"))
                and target != self.cfg.max_agent_active
            ):
                raise InvariantError(
                    "initial rollout must prove exactly two Agent claims"
                )
            if post_incident:
                # Preserve the existing independent pre-close quick read.  It
                # remains in ready phase until it passes, so no crash before
                # the durable transition can later close Agent.
                current = self.post_incident_quick_release_audit(phase)
            phase.update(
                {
                    "name": "release_verifying",
                    "watchdog_armed": True,
                    "release_baseline_attempted": baseline,
                    "release_target": target,
                    "release_started_at": time.time(),
                }
            )
            save_phase(self.cfg, phase)
        else:
            baseline = int(phase.get("release_baseline_attempted", -1))
            target = int(phase.get("release_target", -1))
            if (
                baseline < 0
                or target < 1
                or target > self.cfg.max_agent_active
                or baseline + target > self.cfg.stage_cap
            ):
                raise InvariantError("restart-verifying release baseline is invalid")

        # A controller restart opens Agent before reaching this branch. Inspect
        # the realized wave before any close; never grant a second capacity
        # window after a partial or complete first wave.  A delta-zero fast
        # release must still carry a live service-bound ticket; a controller
        # restart therefore falls back instead of closing from stale proof.
        release_ticket = isinstance(phase.get("post_incident_release"), dict)
        if current is None:
            try:
                current = self.quick_snapshot()
                failures = self.quick_failures(current, phase)
                if failures:
                    raise InvariantError(
                        "release pre-close audit: " + "; ".join(failures)
                    )
            except Exception as exc:
                if not release_ticket:
                    raise
                invalid = PostIncidentReleaseInvalid(
                    f"post-incident pre-close read failed: {type(exc).__name__}: {exc}"
                )
                self.invalidate_post_incident_release(phase, reason=str(invalid))
                raise invalid from exc
        counts = current["agent"]["counts"]
        attempted = sum(int(counts.get(state, 0)) for state in ATTEMPTED_STATES)
        active = sum(int(counts.get(state, 0)) for state in ACTIVE_STATES)
        hindsight_active = sum(
            int(current["hindsight_operations"].get(state, 0))
            for state in ("pending", "processing")
        )
        provider = int(current["budget"].get("in_flight") or 0)
        delta = attempted - baseline
        if (
            delta > target
            or active > self.cfg.max_agent_active
            or provider > self.cfg.max_provider_in_flight
        ):
            self.persist_agent_open("release realized-wave bound exceeded")
            raise InvariantError("release realized-wave bound exceeded before close")
        if delta < 0:
            if active or hindsight_active or provider:
                raise InvariantError("release retry reset is not drained")
            self.retire_post_incident_release(phase, outcome="reset_after_retry")
            phase.update(
                {
                    "name": "ready",
                    "expected_succeeded": int(counts.get("succeeded", 0)),
                    "release_reset_at": time.time(),
                }
            )
            phase.pop("release_baseline_attempted", None)
            phase.pop("release_target", None)
            save_phase(self.cfg, phase)
            current["release_gate_status"] = "reset_after_retry"
            return current
        if delta == target:
            event(
                self.cfg,
                "release_wave_verified_after_restart",
                attempted=attempted,
                target=target,
                active=active,
                provider_in_flight=provider,
            )
            phase.update(
                {
                    "name": "running",
                    "released_at": time.time(),
                    "release_verified_attempted": attempted,
                    "release_max_provider_observed": provider,
                    "initial_wave_verified": bool(
                        phase.get("initial_wave_verified")
                        or target == self.cfg.max_agent_active
                    ),
                }
            )
            self.retire_post_incident_release(phase, outcome="verified_after_restart")
            save_phase(self.cfg, phase)
            current["release_gate_status"] = "verified_after_restart"
            return current
        if 0 < delta < target:
            if active or hindsight_active or provider:
                raise InvariantError("partial release wave is still draining")
            self.retire_post_incident_release(phase, outcome="partial_wave_reset")
            phase.update(
                {
                    "name": "ready",
                    "expected_succeeded": int(counts.get("succeeded", 0)),
                    "partial_release_reset_at": time.time(),
                }
            )
            phase.pop("release_baseline_attempted", None)
            phase.pop("release_target", None)
            save_phase(self.cfg, phase)
            current["release_gate_status"] = "partial_wave_reset"
            return current

        if release_ticket:
            # Delta is exactly zero.  Recheck the receipt after persisting
            # release_verifying and immediately before the only close
            # authority below.  A restart changes the controller PID and
            # therefore retires the stale ticket instead of issuing a close.
            current = self.post_incident_quick_release_audit(phase, snap=current)

        if bool(current["agent"]["circuit"]["circuit_open"]):
            self.close_agent()
        deadline = time.monotonic() + 90
        maximum_provider_observed = provider
        while time.monotonic() < deadline:
            current = self.quick_snapshot()
            loop_phase = load_phase(self.cfg)
            if loop_phase["name"] != "release_verifying":
                self.persist_agent_open(
                    "release phase changed during claim verification"
                )
                raise InvariantError("release phase changed during claim verification")
            loop_failures = self.quick_failures(current, loop_phase)
            if loop_failures:
                self.persist_agent_open(
                    "release polling audit failed: " + "; ".join(loop_failures)[:900]
                )
                raise InvariantError(
                    "release polling audit failed: " + "; ".join(loop_failures)
                )
            counts = current["agent"]["counts"]
            attempted = sum(int(counts.get(state, 0)) for state in ATTEMPTED_STATES)
            active = sum(int(counts.get(state, 0)) for state in ACTIVE_STATES)
            provider = int(current["budget"].get("in_flight") or 0)
            maximum_provider_observed = max(maximum_provider_observed, provider)
            delta = attempted - baseline
            if delta == target:
                if (
                    active > self.cfg.max_agent_active
                    or provider > self.cfg.max_provider_in_flight
                ):
                    emergency_open(
                        self.cfg,
                        f"release wave bound failed: active={active} provider={provider}",
                    )
                    raise InvariantError("release wave exceeded configured bounds")
                event(
                    self.cfg,
                    "release_wave_verified",
                    attempted=attempted,
                    target=target,
                    active=active,
                    provider_in_flight=provider,
                )
                phase = load_phase(self.cfg)
                if phase["name"] != "release_verifying":
                    raise InvariantError("release phase changed during wave proof")
                phase.update(
                    {
                        "name": "running",
                        "released_at": time.time(),
                        "release_verified_attempted": attempted,
                        "release_max_provider_observed": maximum_provider_observed,
                        "initial_wave_verified": bool(
                            phase.get("initial_wave_verified")
                            or target == self.cfg.max_agent_active
                        ),
                    }
                )
                self.retire_post_incident_release(phase, outcome="verified")
                save_phase(self.cfg, phase)
                current["release_gate_status"] = "verified"
                return current
            if (
                delta > target
                or active > self.cfg.max_agent_active
                or provider > self.cfg.max_provider_in_flight
            ):
                emergency_open(self.cfg, "release claim count/fan-out exceeded")
                raise InvariantError("post-release work exceeded claim bound")
            self.sleep_with_heartbeat(2, "release_claim_gate")
        emergency_open(self.cfg, "release claim did not appear within 90 seconds")
        raise InvariantError("post-release claim timed out")

    def preprove_cap_refresh(self) -> bool:
        snap = self.quick_snapshot()
        counts = snap["agent"]["counts"]
        if (
            sum(int(counts.get(state, 0)) for state in ATTEMPTED_STATES)
            != self.cfg.stage_cap
        ):
            return False
        if sum(
            int(snap["hindsight_operations"].get(state, 0))
            for state in ("pending", "processing")
        ):
            return False
        if int(snap["budget"].get("in_flight") or 0):
            return False
        with self.agent_db() as conn:
            rows = self._submitted_rows(conn)
        if not rows:
            return int(counts.get("succeeded", 0)) == self.cfg.stage_cap
        try:
            return len(self._prove_completed_submitted_rows(rows)) == len(rows)
        except InvariantError:
            return False

    def settle_cap(self) -> dict[str, Any] | None:
        phase = load_phase(self.cfg)
        if phase["name"] != "cap_hold" or not self.preprove_cap_refresh():
            return None
        outbox = self._reviewed_outbox_module(
            Path.home() / "agent-session-mcp/server/outbox.py"
        )
        worker = self._app_worker()
        with self.bank_lock() as conn:
            adjacent = self.quick_snapshot()
            counts = adjacent["agent"]["counts"]
            if (
                sum(int(counts.get(state, 0)) for state in ATTEMPTED_STATES)
                != self.cfg.stage_cap
                or sum(
                    int(adjacent["hindsight_operations"].get(state, 0))
                    for state in ("pending", "processing")
                )
                or int(adjacent["budget"].get("in_flight") or 0)
                or not bool(adjacent["agent"]["circuit"]["circuit_open"])
            ):
                raise InvariantError("cap refresh adjacent drain/open proof changed")
            rows = self._submitted_rows(conn)
            if not rows:
                if int(counts.get("succeeded", 0)) != self.cfg.stage_cap:
                    raise InvariantError(
                        "cap has no submitted rows but is not fully succeeded"
                    )
                completed, failed = 0, 0
            else:
                proofs = self._prove_completed_submitted_rows(rows)
                if len(proofs) != len(rows):
                    raise InvariantError("cap completed proof set is incomplete")
                only_operations = frozenset(
                    (document_id, parent_id)
                    for document_id, parent_id, _child_id, _payload_hash in proofs
                )
                completed_lineage_proofs = frozenset(
                    outbox.CompletedLineageProof(
                        document_id=document_id,
                        parent_operation_id=parent_id,
                        child_operation_id=child_id,
                        payload_hash=payload_hash,
                    )
                    for document_id, parent_id, child_id, payload_hash in proofs
                )
                completed, failed = worker._refresh_submitted(
                    conn,
                    only_operations=only_operations,
                    completed_lineage_proofs=completed_lineage_proofs,
                )
                if completed != len(proofs):
                    raise InvariantError(
                        "cap targeted refresh did not update every exact pair"
                    )
            if failed:
                raise InvariantError(
                    "cap refresh encountered a failed remote operation"
                )
        phase.update({"name": "settled", "expected_succeeded": self.cfg.stage_cap})
        save_phase(self.cfg, phase)
        audit = self.full_audit(phase)
        counts = audit["agent"]["counts"]
        expected_counts = {"succeeded": self.cfg.stage_cap}
        remaining = self.cfg.approved_total - self.cfg.stage_cap
        if remaining:
            expected_counts["queued"] = remaining
        if counts != expected_counts:
            raise InvariantError(f"cap settlement counts mismatch: {counts}")
        if audit["remote_documents"] != self.cfg.stage_cap or audit["failures"]:
            raise InvariantError(f"cap settlement audit failed: {audit['failures']}")
        event(
            self.cfg, "stage_settled", completed=completed, succeeded=self.cfg.stage_cap
        )
        return audit

    def resume_stage_below_cap(self) -> dict[str, Any]:
        """Return a post-retry cap hold to the normal audited release gate."""
        phase = load_phase(self.cfg)
        if phase["name"] != "cap_hold":
            raise InvariantError("below-cap resume requires cap_hold phase")
        audit = self.full_audit(phase)
        if audit["failures"]:
            raise InvariantError("; ".join(audit["failures"]))
        counts = audit["agent"]["counts"]
        attempted = sum(int(counts.get(state, 0)) for state in ATTEMPTED_STATES)
        active_or_error = sum(
            int(counts.get(state, 0)) for state in ACTIVE_STATES + ERROR_STATES
        )
        if (
            attempted >= self.cfg.stage_cap
            or active_or_error
            or not bool(audit["agent"]["circuit"]["circuit_open"])
            or int(audit["budget"].get("in_flight") or 0)
            or sum(
                int(audit["hindsight_operations"].get(state, 0))
                for state in ("pending", "processing")
            )
        ):
            raise InvariantError("below-cap resume is not fully drained/open")
        phase.update(
            {
                "name": "ready",
                "budget": "closed",
                "expected_succeeded": int(counts.get("succeeded", 0)),
            }
        )
        save_phase(self.cfg, phase)
        event(self.cfg, "stage_resumed_below_cap", attempted=attempted)
        return audit


def _free_ports(count: int) -> list[int]:
    sockets: list[socket.socket] = []
    ports: list[int] = []
    try:
        for _ in range(count):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
            ports.append(int(sock.getsockname()[1]))
    finally:
        for sock in sockets:
            sock.close()
    return ports


def _wait_ports(ports: Iterable[int], timeout: float = 8) -> bool:
    deadline = time.monotonic() + timeout
    ports = tuple(ports)
    while time.monotonic() < deadline:
        if all(_port_ready(port) for port in ports):
            return True
        time.sleep(0.1)
    return False


def _port_ready(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def emergency_open(cfg: Config | EmergencyConfig, reason: str) -> None:
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    with cfg.lock_file.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ports = _free_ports(3)
        command = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=8",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ServerAliveInterval=5",
            "-o",
            "ServerAliveCountMax=2",
            "-N",
        ]
        for port, host in zip(ports, cfg.pg_remote_hosts):
            command.extend(["-L", f"127.0.0.1:{port}:{host}:5432"])
        command.append(cfg.ssh_target)
        process = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
        )
        try:
            if not _wait_ports(ports) or process.poll() is not None:
                detail = process.stderr.read(500) if process.stderr else ""
                raise RuntimeError(f"emergency PG tunnel failed: {detail}")
            agent_env = _env_file(Path.home() / ".attestmesh/agent-session-mcp.env")
            pocket_env = _env_file(Path.home() / ".attestmesh/pocket-mcp.env")

            def connect(database: str, user: str, password: str):
                return psycopg.connect(
                    host=",".join("127.0.0.1" for _ in ports),
                    port=",".join(str(port) for port in ports),
                    user=user,
                    password=password,
                    dbname=database,
                    target_session_attrs="read-write",
                    connect_timeout=2,
                    row_factory=dict_row,
                )

            with (
                connect(
                    "agent_sessions",
                    "agent_sessions",
                    agent_env["APP_DB_PASSWORD"],
                ) as conn,
                conn.transaction(),
                conn.cursor() as cur,
            ):
                cur.execute(
                    """INSERT INTO hindsight_sync_state(bank_id,circuit_open,reason,opened_at,updated_at)
                       VALUES('agent-sessions',true,%s,now(),now())
                       ON CONFLICT(bank_id) DO UPDATE SET circuit_open=true,reason=EXCLUDED.reason,
                         opened_at=COALESCE(hindsight_sync_state.opened_at,now()),updated_at=now()""",
                    (reason[:1000],),
                )
            with (
                connect(
                    "agent_sessions",
                    "agent_sessions",
                    agent_env["APP_DB_PASSWORD"],
                ) as conn,
                conn.cursor() as cur,
            ):
                cur.execute(
                    "SELECT circuit_open FROM hindsight_sync_state WHERE bank_id='agent-sessions'"
                )
                agent = cur.fetchone()
            if not agent or not agent["circuit_open"]:
                raise InvariantError("independent Agent emergency-open readback failed")
            with (
                connect("pocket", "pocket", pocket_env["APP_DB_PASSWORD"]) as conn,
                conn.transaction(),
                conn.cursor() as cur,
            ):
                cur.execute(
                    """INSERT INTO hindsight_sync_state(
                           bank_id,circuit_open,reason,opened_at,updated_at
                       ) VALUES('dans-pocket',true,%s,now(),now())
                       ON CONFLICT(bank_id) DO UPDATE SET
                         circuit_open=true,reason=EXCLUDED.reason,
                         opened_at=COALESCE(hindsight_sync_state.opened_at,now()),
                         updated_at=now()""",
                    (reason[:1000],),
                )
            with (
                connect("pocket", "pocket", pocket_env["APP_DB_PASSWORD"]) as conn,
                conn.cursor() as cur,
            ):
                cur.execute(
                    "SELECT circuit_open FROM hindsight_sync_state WHERE bank_id='dans-pocket'"
                )
                pocket = cur.fetchone()
            if not pocket or not pocket["circuit_open"]:
                raise InvariantError(
                    "independent Pocket emergency-open readback failed"
                )
            event(
                cfg,
                "emergency_open",
                reason=reason[:1000],
                agent_open=True,
                pocket_open=True,
            )
        finally:
            process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=3)
            if process.poll() is None:
                process.kill()


def _service_active(name: str) -> bool:
    return _service_evidence(name).get("ActiveState") == "active"


def _service_evidence(name: str) -> dict[str, Any]:
    """Return exact user-systemd liveness/restart evidence for one unit."""
    result = subprocess.run(
        [
            "systemctl",
            "--user",
            "show",
            name,
            "--property=Id",
            "--property=ActiveState",
            "--property=SubState",
            "--property=MainPID",
            "--property=NRestarts",
            "--no-pager",
        ],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=10,
    )
    evidence: dict[str, Any] = {
        "Id": name,
        "ActiveState": "unknown",
        "SubState": "unknown",
        "MainPID": 0,
        "NRestarts": -1,
        "returncode": result.returncode,
    }
    for raw in result.stdout.splitlines():
        key, separator, value = raw.partition("=")
        if not separator or key not in {
            "Id",
            "ActiveState",
            "SubState",
            "MainPID",
            "NRestarts",
        }:
            continue
        if key in {"MainPID", "NRestarts"}:
            with contextlib.suppress(ValueError):
                evidence[key] = int(value)
        else:
            evidence[key] = value
    return evidence


def run_guard(cfg: Config) -> None:
    recovery = Recovery(cfg)
    last_full = 0.0
    last_failure_reason: str | None = None
    while True:
        phase = load_phase(cfg)
        try:
            snap = recovery.quick_snapshot(
                audit_heartbeat_label="guard",
                audit_heartbeat_fields={
                    "phase": phase["name"],
                    "authoritative_read": "running",
                },
            )
            # The guard's previous result must not poison its next audit.  Its
            # liveness/result is independently consumed by the watchdog and
            # controller; the guard continues to validate both peer
            # heartbeats itself.
            failures = recovery.quick_failures(
                snap,
                phase,
                excluded_heartbeat_labels=("guard",),
            )
            counts = snap["agent"]["counts"]
            attempted = sum(int(counts.get(state, 0)) for state in ATTEMPTED_STATES)
            action = stage_action(attempted, str(phase["name"]), cfg.stage_cap)
            if action in {"open", "violation"}:
                emergency_open(
                    cfg, f"stage cap guard: attempted={attempted} cap={cfg.stage_cap}"
                )
                if action == "open":
                    phase.update(
                        {
                            "name": "cap_hold",
                            "expected_succeeded": int(counts.get("succeeded", 0)),
                        }
                    )
                    save_phase(cfg, phase)
            now = time.monotonic()
            if now - last_full >= cfg.full_seconds:
                full = recovery.full_audit(
                    load_phase(cfg),
                    excluded_heartbeat_labels=("guard",),
                    audit_heartbeat_label="guard",
                    audit_heartbeat_fields={
                        "phase": phase["name"],
                        "attempted": attempted,
                        "full_audit": "running",
                    },
                )
                failures.extend(full["failures"])
                # Schedule the next full audit from completion.  Measuring
                # from the start causes back-to-back audits whenever one audit
                # itself runs longer than GUARD_FULL_SECONDS.
                last_full = time.monotonic()
            if failures:
                raise InvariantError("; ".join(sorted(set(failures))))
            _atomic_json(
                cfg.heartbeat_file,
                {
                    "at": time.time(),
                    "healthy": True,
                    "phase": load_phase(cfg)["name"],
                    "attempted": attempted,
                },
            )
            last_failure_reason = None
        except Exception as exc:
            reason = f"guard failure: {type(exc).__name__}: {exc}"
            # Re-run the independent idempotent hold for *both* banks on every
            # guard failure.  Proving Agent open is not evidence that Pocket
            # remained open, and Pocket must never be allowed to process while
            # this recovery is armed.
            with contextlib.suppress(Exception):
                emergency_open(cfg, reason)
            _atomic_json(
                cfg.heartbeat_file,
                {"at": time.time(), "healthy": False, "reason": reason},
            )
            if reason != last_failure_reason:
                event(cfg, "guard_failure", reason=reason)
            last_failure_reason = reason
        time.sleep(cfg.quick_seconds)


def run_watchdog(cfg: Config) -> None:
    emergency_open(cfg, "restart-safe watchdog startup hold")
    while True:
        phase = load_phase(cfg)
        heartbeat_age: float | None = None
        if cfg.heartbeat_file.exists():
            try:
                heartbeat_age = time.time() - float(
                    json.loads(cfg.heartbeat_file.read_text())["at"]
                )
            except Exception:
                heartbeat_age = None
        controller_heartbeat_age: float | None = None
        if cfg.controller_file.exists():
            try:
                controller_heartbeat_age = time.time() - float(
                    json.loads(cfg.controller_file.read_text())["at"]
                )
            except Exception:
                controller_heartbeat_age = None
        ports = (
            *cfg.pg_local_ports,
            cfg.hindsight_port,
            cfg.budget_port,
            cfg.fugu_port,
            *cfg.patroni_ports,
        )
        failure = watchdog_failure(
            armed=bool(phase.get("watchdog_armed")),
            heartbeat_age=heartbeat_age,
            tunnel_ports_ready=all(_port_ready(port) for port in ports),
            guard_active=_service_active("hindsight-recovery-guard.service"),
            tunnel_active=_service_active("hindsight-recovery-tunnel.service"),
            max_age=cfg.heartbeat_max_age,
            controller_required=bool(phase.get("controller_required")),
            controller_heartbeat_age=controller_heartbeat_age,
            controller_active=_service_active("hindsight-recovery-controller.service"),
        )
        if failure:
            with contextlib.suppress(Exception):
                emergency_open(cfg, f"watchdog failure: {failure}")
            event(cfg, "watchdog_failure", reason=failure)
        _atomic_json(
            cfg.watchdog_file,
            {
                "at": time.time(),
                "healthy": failure is None,
                "reason": failure,
                "armed": bool(phase.get("watchdog_armed")),
                "controller_required": bool(phase.get("controller_required")),
            },
        )
        time.sleep(cfg.watchdog_seconds)


@contextlib.contextmanager
def controller_singleton(cfg: Config):
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    with cfg.controller_lock_file.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise InvariantError(
                "another recovery controller is already running"
            ) from exc
        yield


def _controller_snapshot(recovery: Recovery, phase: dict[str, Any]) -> dict[str, Any]:
    checkpoint = read_checkpoint(recovery.cfg)
    snap = recovery.quick_snapshot()
    snap["failures"] = recovery.quick_failures(snap, phase, checkpoint)
    return snap


def run_controller(cfg: Config) -> None:
    """Run the only restart-persistent automatic release/recovery authority."""
    with controller_singleton(cfg):
        # This independent one-shot route happens before any shared-path read.
        emergency_open(cfg, "recovery controller startup hold")
        recovery = Recovery(cfg, controller_owned=True)
        recovery.ensure_pocket_open("recovery controller startup hold")
        _atomic_json(
            cfg.controller_file,
            {"at": time.time(), "healthy": True, "status": "startup_hold"},
        )
        phase = load_phase(cfg)
        phase["watchdog_armed"] = True
        phase["controller_required"] = True
        save_phase(cfg, phase)
        incident = load_incident(cfg)
        incident.update(
            {
                "status": "startup_hold",
                "kind": "startup",
                "clean_checks": 0,
                "full_audits": 0,
                "next_retry_at": 0.0,
            }
        )
        save_incident(cfg, incident, kind="controller_started")

        while True:
            incident = load_incident(cfg)
            now = time.time()
            if now < float(incident.get("next_retry_at") or 0):
                _atomic_json(
                    cfg.controller_file,
                    {
                        "at": now,
                        "healthy": True,
                        "status": "backoff",
                        "next_retry_at": incident["next_retry_at"],
                    },
                )
                time.sleep(min(cfg.controller_seconds, incident["next_retry_at"] - now))
                continue
            try:
                _atomic_json(
                    cfg.controller_file,
                    {
                        "at": time.time(),
                        "healthy": True,
                        "status": "working",
                        "phase": load_phase(cfg)["name"],
                    },
                )
                if recovery.resume_pending_acceptance(incident):
                    incident = load_incident(cfg)
                phase = load_phase(cfg)
                snap = _controller_snapshot(recovery, phase)
                counts = snap["agent"]["counts"]
                attempted = sum(int(counts.get(state, 0)) for state in ATTEMPTED_STATES)
                active = sum(int(counts.get(state, 0)) for state in ACTIVE_STATES)
                errors = sum(int(counts.get(state, 0)) for state in ERROR_STATES)
                hindsight_active = sum(
                    int(snap["hindsight_operations"].get(state, 0))
                    for state in ("pending", "processing")
                )
                provider_in_flight = int(snap["budget"].get("in_flight") or 0)
                resumed_result = recovery.finalize_drained_incident_authorization(
                    incident,
                    active=active,
                    errors=errors,
                    hindsight_active=hindsight_active,
                    provider_in_flight=provider_in_flight,
                )
                if resumed_result is not None:
                    # A crash after acceptance/clear but before archival has
                    # no row for controller_action to classify.  Finalize the
                    # already-durable episode while both source circuits are
                    # held, then re-read every action input.
                    incident = load_incident(cfg)
                    incident.update(
                        {
                            "status": str(resumed_result["status"]),
                            "kind": "outbox_resume",
                            "last_result": resumed_result,
                        }
                    )
                    save_incident(
                        cfg,
                        incident,
                        kind="recovery_attempt_resume_reconciled",
                    )
                    phase = load_phase(cfg)
                    snap = _controller_snapshot(recovery, phase)
                    counts = snap["agent"]["counts"]
                    attempted = sum(
                        int(counts.get(state, 0)) for state in ATTEMPTED_STATES
                    )
                    active = sum(int(counts.get(state, 0)) for state in ACTIVE_STATES)
                    errors = sum(int(counts.get(state, 0)) for state in ERROR_STATES)
                    hindsight_active = sum(
                        int(snap["hindsight_operations"].get(state, 0))
                        for state in ("pending", "processing")
                    )
                    provider_in_flight = int(snap["budget"].get("in_flight") or 0)
                if (
                    bool(snap["agent"]["circuit"]["circuit_open"])
                    and not active
                    and not errors
                    and not hindsight_active
                    and not provider_in_flight
                    and not bool(snap["budget"].get("circuit_open"))
                    and recovery.operation_inventory_needs_acceptance(snap)
                    and recovery.accept_drained_operation_inventory()
                ):
                    # The checkpoint append changes the authoritative audit
                    # baseline.  Re-read every input before selecting an
                    # action; never reuse the pre-acceptance snapshot.
                    snap = _controller_snapshot(recovery, phase)
                    counts = snap["agent"]["counts"]
                    attempted = sum(
                        int(counts.get(state, 0)) for state in ATTEMPTED_STATES
                    )
                    active = sum(int(counts.get(state, 0)) for state in ACTIVE_STATES)
                    errors = sum(int(counts.get(state, 0)) for state in ERROR_STATES)
                    hindsight_active = sum(
                        int(snap["hindsight_operations"].get(state, 0))
                        for state in ("pending", "processing")
                    )
                    provider_in_flight = int(snap["budget"].get("in_flight") or 0)
                failures = list(snap.get("failures") or [])
                recoverable_incident_rows = has_recoverable_incident_rows(
                    errors=errors,
                    active=active,
                    agent_open=bool(snap["agent"]["circuit"]["circuit_open"]),
                )
                if (
                    recoverable_incident_rows
                    and "Hindsight failed-operation count drift" in failures
                ):
                    # Outbox blocked/failed rows open Agent themselves, but
                    # independently persist that hold before authorizing an
                    # unaccepted remote-failure delta.  No row or remote
                    # mutation occurs in this proof step.
                    recovery.persist_agent_open(
                        "controller exact Hindsight failure authorization"
                    )
                    recovery.ensure_pocket_open(
                        "controller exact Hindsight failure authorization"
                    )
                    failures = recovery.authorize_hindsight_incident_drift(
                        incident,
                        recovery._incident_rows(),
                        failures,
                    )
                failures = provider_incident_failures(
                    failures,
                    errors=errors,
                    active=active,
                    agent_open=bool(snap["agent"]["circuit"]["circuit_open"]),
                    budget=snap["budget"],
                    budget_recovery=snap["budget_recovery"],
                )
                progress_marker = _json_digest(
                    {
                        "counts": counts,
                        "hindsight_active": hindsight_active,
                        "provider_in_flight": provider_in_flight,
                    }
                )
                if incident.get("progress_marker") != progress_marker:
                    incident["progress_marker"] = progress_marker
                    incident["progress_at"] = time.time()
                    incident["progress_attempted"] = attempted
                    incident["progress_succeeded"] = int(counts.get("succeeded", 0))
                elif (
                    str(phase["name"]) == "running"
                    and not bool(snap["agent"]["circuit"]["circuit_open"])
                    and attempted < cfg.stage_cap
                    and (int(counts.get("queued", 0)) > 0 or active > 0)
                    and hindsight_active == 0
                    and provider_in_flight == 0
                    and time.time() - float(incident.get("progress_at") or 0)
                    > cfg.agent_progress_timeout
                ):
                    failures.append(
                        "Agent worker progress stalled beyond the configured deadline"
                    )
                probe_ambiguity = has_probe_ambiguity(incident)
                if probe_ambiguity and provider_in_flight:
                    # Validate the durable/live singleton immediately before
                    # selecting the dedicated non-row reconciliation action.
                    exact_probe_reservation_recovery(
                        incident,
                        budget=snap["budget"],
                        budget_recovery=snap["budget_recovery"],
                    )
                action = controller_action(
                    phase_name=str(phase["name"]),
                    failures=failures,
                    agent_open=bool(snap["agent"]["circuit"]["circuit_open"]),
                    attempted=attempted,
                    cap=cfg.stage_cap,
                    active=active,
                    errors=errors,
                    hindsight_active=hindsight_active,
                    provider_in_flight=provider_in_flight,
                    probe_ambiguity_recovery=probe_ambiguity,
                )

                if action == "hard_hold":
                    recovery.persist_agent_open(
                        "controller hard hold: " + "; ".join(failures)[:900]
                    )
                    recovery.ensure_pocket_open("controller hard hold")
                    incident.update(
                        {
                            "status": "hard_hold",
                            "kind": "invariant",
                            "last_error": "; ".join(failures),
                            "clean_checks": 0,
                            "full_audits": 0,
                            "next_retry_at": time.time() + cfg.controller_max_backoff,
                        }
                    )
                elif action == "recover":
                    recovery.persist_agent_open("controller incident recovery")
                    recovery.ensure_pocket_open("controller incident recovery")
                    result = recovery.recover_incident_rows(incident)
                    incident = load_incident(cfg)
                    incident.update(
                        {
                            "status": str(result["status"]),
                            "kind": "outbox",
                            "last_result": result,
                            "attempts": 0,
                            "clean_checks": 0,
                            "full_audits": 0,
                            "next_retry_at": time.time() + cfg.controller_seconds,
                        }
                    )
                elif action == "reconcile_probe_reservation":
                    recovery.persist_agent_open(
                        "controller probe-reservation reconciliation"
                    )
                    recovery.ensure_pocket_open(
                        "controller probe-reservation reconciliation"
                    )
                    result = recovery.reconcile_probe_ambiguity_only(incident)
                    incident = load_incident(cfg)
                    incident.update(
                        {
                            "status": str(result["status"]),
                            "kind": "provider_probe_ambiguity",
                            "last_result": result,
                            "attempts": 0,
                            "clean_checks": 0,
                            "full_audits": 0,
                            "next_retry_at": time.time() + cfg.controller_seconds,
                        }
                    )
                elif action == "wait":
                    if not bool(snap["agent"]["circuit"]["circuit_open"]):
                        recovery.persist_agent_open("controller waiting for drain")
                    recovery.ensure_pocket_open("controller waiting for drain")
                    incident.update(
                        {
                            "status": "waiting",
                            "kind": "remote_activity",
                            "clean_checks": 0,
                            "full_audits": 0,
                            "next_retry_at": time.time() + cfg.controller_seconds,
                        }
                    )
                elif action == "retry_audit":
                    recovery.persist_agent_open(
                        "controller audit hold: " + "; ".join(failures)[:900]
                    )
                    recovery.ensure_pocket_open("controller audit hold")
                    raise InvariantError("; ".join(failures))
                elif action == "prepare_release":
                    completed_refresh_release = (
                        recovery.post_incident_release_candidate()
                    )
                    if completed_refresh_release:
                        recovery.prepare_post_incident_release()
                    else:
                        recovery.prepare_open_running_hold()
                    incident.update(
                        {
                            "status": "ready",
                            "kind": (
                                "completed_refresh_release"
                                if completed_refresh_release
                                else "restart_reconciliation"
                            ),
                            "clean_checks": 0,
                            "full_audits": 0,
                            "next_retry_at": time.time() + cfg.controller_seconds,
                        }
                    )
                elif action == "resume_release":
                    release_result = recovery.release()
                    release_phase = load_phase(cfg)
                    release_status = str(
                        release_result.get("release_gate_status") or "unknown"
                    )
                    if release_phase["name"] not in {"ready", "running"}:
                        raise InvariantError(
                            "release resume returned an unexpected durable phase"
                        )
                    incident.update(
                        {
                            "status": str(release_phase["name"]),
                            "kind": f"release_{release_status}",
                            "clean_checks": 0,
                            "full_audits": 0,
                            "next_retry_at": time.time() + cfg.controller_seconds,
                        }
                    )
                elif action == "settle":
                    settled = recovery.settle_cap()
                    if settled is None:
                        raise InvariantError("cap settlement proof is not ready")
                    incident.update(
                        {
                            "status": "settled",
                            "kind": "cap",
                            "clean_checks": 0,
                            "full_audits": 0,
                            "next_retry_at": time.time() + cfg.controller_seconds,
                        }
                    )
                elif action == "resume_stage":
                    recovery.resume_stage_below_cap()
                    incident.update(
                        {
                            "status": "ready",
                            "kind": "cap_retry_resume",
                            "clean_checks": 0,
                            "full_audits": 0,
                            "next_retry_at": time.time() + cfg.controller_seconds,
                        }
                    )
                elif action == "finalize":
                    recovery.finalize()
                    incident.update(
                        {
                            "status": "finalized",
                            "kind": "complete",
                            "attempts": 0,
                            "clean_checks": cfg.controller_stable_checks,
                            "full_audits": cfg.controller_full_audits,
                            "next_retry_at": 0.0,
                        }
                    )
                elif action == "stabilize":
                    if recovery.post_incident_release_candidate():
                        phase = load_phase(cfg)
                        if not isinstance(phase.get("post_incident_release"), dict):
                            recovery.prepare_post_incident_release()
                        release_result = recovery.release()
                        release_phase = load_phase(cfg)
                        release_status = str(
                            release_result.get("release_gate_status") or "unknown"
                        )
                        if release_phase["name"] not in {"ready", "running"}:
                            raise InvariantError(
                                "completed-refresh release returned an unexpected phase"
                            )
                        incident.update(
                            {
                                "status": str(release_phase["name"]),
                                "kind": f"completed_refresh_release_{release_status}",
                                "clean_checks": 0,
                                "full_audits": 0,
                                "stable_spend": None,
                                "attempts": 0,
                                "next_retry_at": time.time() + cfg.controller_seconds,
                            }
                        )
                    else:
                        # Every iteration already has an authoritative quick
                        # snapshot.  Run the configured full provenance audits
                        # at the beginning and end of the durable ten-check
                        # window instead of issuing ten identical, competing
                        # full inventory reads.  The ordinary release path
                        # still performs its transaction-adjacent ten checks
                        # and first/last full audits.
                        clean_checks = int(incident.get("clean_checks", 0))
                        full_audits = int(incident.get("full_audits", 0))
                        gate_snapshot = snap
                        if release_gate_requires_full_audit(
                            clean_checks=clean_checks,
                            full_audits=full_audits,
                            required_clean_checks=cfg.controller_stable_checks,
                            required_full_audits=cfg.controller_full_audits,
                        ):
                            gate_snapshot = recovery.full_audit(phase)
                            if gate_snapshot["failures"]:
                                raise InvariantError(
                                    "; ".join(gate_snapshot["failures"])
                                )
                            full_audits += 1
                        prior_spend = incident.get("stable_spend")
                        spend = gate_snapshot["budget"].get(
                            "hindsight_backfill_spent_usd"
                        )
                        if prior_spend is not None and spend != prior_spend:
                            raise InvariantError(
                                "spend changed during controller stability gate"
                            )
                        incident["stable_spend"] = spend
                        incident["clean_checks"] = clean_checks + 1
                        incident["full_audits"] = full_audits
                        incident["status"] = "stabilizing"
                        incident["kind"] = "release_gate"
                        incident["attempts"] = 0
                        incident["next_retry_at"] = time.time() + cfg.controller_seconds
                        if (
                            int(incident["clean_checks"])
                            >= cfg.controller_stable_checks
                            and int(incident["full_audits"])
                            >= cfg.controller_full_audits
                        ):
                            recovery.release()
                            incident.update(
                                {
                                    "status": "running",
                                    "kind": "released",
                                    "clean_checks": 0,
                                    "full_audits": 0,
                                    "stable_spend": None,
                                }
                            )
                elif action == "finalized":
                    recovery.persist_agent_open(FINAL_HOLD_REASON)
                    recovery.ensure_pocket_open(FINAL_HOLD_REASON)
                    if not recovery.budget_final_hold_is_exact():
                        raise InvariantError("finalized budget maintenance hold drift")
                    incident.update(
                        {
                            "status": "finalized",
                            "kind": "complete_monitor",
                            "attempts": 0,
                            "next_retry_at": time.time() + cfg.controller_seconds,
                        }
                    )
                elif action == "monitor":
                    incident.update(
                        {
                            "status": action,
                            "kind": "healthy",
                            "attempts": 0,
                            "next_retry_at": time.time() + cfg.controller_seconds,
                        }
                    )
                else:
                    recovery.persist_agent_open(f"controller hold: action={action}")
                    recovery.ensure_pocket_open(f"controller hold: action={action}")
                    incident.update(
                        {
                            "status": "hold",
                            "kind": action,
                            "clean_checks": 0,
                            "full_audits": 0,
                            "next_retry_at": time.time() + cfg.controller_seconds,
                        }
                    )
                # Preserve the last concrete incident across passive waits and
                # holds.  Clear it only after a genuinely healthy transition.
                if action in {
                    "monitor",
                    "stabilize",
                    "prepare_release",
                    "resume_release",
                    "resume_stage",
                    "settle",
                    "finalize",
                    "finalized",
                }:
                    incident["last_error"] = None
                save_incident(cfg, incident)
                _atomic_json(
                    cfg.controller_file,
                    {
                        "at": time.time(),
                        "healthy": True,
                        "status": incident["status"],
                        "phase": load_phase(cfg)["name"],
                        "attempted": attempted,
                    },
                )
            except Exception as exc:
                reason = f"controller failure: {type(exc).__name__}: {exc}"
                # A read failure is itself a reason to use the independent
                # one-shot path.  Retry only after Agent is durably open.
                emergency_open(cfg, reason)
                with contextlib.suppress(Exception):
                    Recovery(cfg).ensure_pocket_open(reason)
                incident = load_incident(cfg)
                attempts = int(incident.get("attempts", 0)) + 1
                delay = bounded_backoff(
                    attempts,
                    cfg.controller_initial_backoff,
                    cfg.controller_max_backoff,
                )
                incident.update(
                    {
                        "status": "backoff",
                        "kind": "controller_failure",
                        "attempts": attempts,
                        "clean_checks": 0,
                        "full_audits": 0,
                        "stable_spend": None,
                        "last_error": reason,
                        "next_retry_at": time.time() + delay,
                    }
                )
                save_incident(cfg, incident, kind="controller_failure")
                _atomic_json(
                    cfg.controller_file,
                    {
                        "at": time.time(),
                        "healthy": False,
                        "status": "backoff",
                        "reason": reason,
                        "next_retry_at": incident["next_retry_at"],
                    },
                )
            time.sleep(cfg.controller_seconds)


def _summary(snap: dict[str, Any]) -> dict[str, Any]:
    return {
        "agent": snap.get("agent"),
        "pocket": snap.get("pocket"),
        "budget": snap.get("budget"),
        "hindsight_operations": snap.get("hindsight_operations"),
        "hindsight_documents": snap.get("hindsight_documents"),
        "histories": snap.get("histories"),
        "remote_documents": snap.get("remote_documents"),
        "patroni": snap.get("patroni"),
        "services": snap.get("services"),
        "heartbeats": snap.get("heartbeats"),
        "tunnel_ports": snap.get("tunnel_ports"),
        "failures": snap.get("failures", []),
    }


def incident_status(recovery: Recovery, *, full: bool) -> dict[str, Any]:
    cfg = recovery.cfg
    phase = load_phase(cfg)
    snap = recovery.full_audit(phase) if full else _controller_snapshot(recovery, phase)
    controller_heartbeat = None
    if cfg.controller_file.exists():
        controller_heartbeat = json.loads(cfg.controller_file.read_text())
    return {
        "phase": phase,
        "checkpoint": load_checkpoint(cfg, phase),
        "incident": load_incident(cfg),
        "controller_heartbeat": controller_heartbeat,
        "audit": _summary(snap),
    }


def _spotcheck_public_snapshot(snap: dict[str, Any]) -> dict[str, Any]:
    def ids_digest(values: Iterable[Any]) -> str:
        encoded = json.dumps(
            sorted(str(value) for value in values),
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    agent = dict(snap["agent"])
    hindsight = dict(snap["hindsight"])
    agent_ids = list(agent.pop("document_ids"))
    remote_ids = list(hindsight.pop("document_ids"))
    agent["document_ids_sha256"] = ids_digest(agent_ids)
    hindsight["document_ids_sha256"] = ids_digest(remote_ids)
    fugu = dict(snap["fugu"])
    fugu["redpill_ooc_events"] = len(fugu.pop("redpill_ooc_request_ids"))
    budget = dict(snap["budget"])
    budget["redpill_ooc_events"] = len(
        budget.pop("redpill_ooc_request_ids")
    )
    return {
        "captured_at": snap["captured_at"],
        "captured_at_iso": snap["captured_at_iso"],
        "agent": agent,
        "histories": dict(snap["histories"]),
        "hindsight": hindsight,
        "fugu": fugu,
        "budget": budget,
    }


def _spotcheck_progress(
    previous: dict[str, Any] | None, current: dict[str, Any], cfg: Config
) -> dict[str, Any]:
    if previous is None:
        return {
            "delta": None,
            "rows_per_hour": None,
            "eta_seconds": None,
            "new_429s": 0,
        }
    elapsed = float(current["captured_at"]) - float(previous["captured_at"])
    succeeded_delta = int(current["agent"]["succeeded"]) - int(
        previous["agent"]["succeeded"]
    )
    rate = (
        succeeded_delta * 3600.0 / elapsed
        if elapsed > 0 and succeeded_delta > 0
        else 0.0
    )
    remaining = max(cfg.approved_total - int(current["agent"]["succeeded"]), 0)
    eta = remaining * 3600.0 / rate if rate > 0 else (0.0 if not remaining else None)
    return {
        "delta": {
            "succeeded": succeeded_delta,
            "queued": int(current["agent"]["queued"])
            - int(previous["agent"]["queued"]),
            "ever_attempted": int(current["agent"]["ever_attempted"])
            - int(previous["agent"]["ever_attempted"]),
            "remote_documents": int(
                current["hindsight"]["remote_distinct_documents"]
            )
            - int(previous["hindsight"]["remote_distinct_documents"]),
            "operations": int(current["hindsight"]["operations"])
            - int(previous["hindsight"]["operations"]),
            "fugu_rows": int(current["fugu"]["rows"])
            - int(previous["fugu"]["rows"]),
            "provider_calls": int(current["budget"]["provider_calls"])
            - int(previous["budget"]["provider_calls"]),
            "provider_spent_usd": float(
                current["budget"]["provider_spent_usd"]
            )
            - float(previous["budget"]["provider_spent_usd"]),
        },
        "rows_per_hour": rate,
        "eta_seconds": eta,
        "new_429s": max(
            int(current["fugu"]["rate_limited"])
            - int(previous["fugu"]["rate_limited"]),
            0,
        ),
    }


def run_spotcheck(
    recovery: Recovery,
    *,
    apply_pause: bool,
    interval_seconds: float = 5.0,
    sleep: Any = time.sleep,
) -> dict[str, Any]:
    """Run the raw two-read spot check and optionally open only Agent."""

    cfg = recovery.cfg
    with _exclusive_lock(cfg.spotcheck_lock_file):
        previous: dict[str, Any] | None = None
        if cfg.spotcheck_file.exists():
            try:
                previous_value = json.loads(cfg.spotcheck_file.read_text())
                if (
                    not isinstance(previous_value, dict)
                    or previous_value.get("version") != SPOTCHECK_VERSION
                ):
                    raise InvariantError("durable spot-check snapshot is incompatible")
                previous = previous_value
            except Exception as exc:
                return {
                    "status": "unknown",
                    "paused": False,
                    "reason": (
                        "durable prior snapshot read failed: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                }

        try:
            first = recovery.raw_spotcheck_snapshot()
        except Exception as exc:
            return {
                "status": "unknown",
                "paused": False,
                "reason": f"first raw read failed: {type(exc).__name__}: {exc}",
            }
        sleep(max(interval_seconds, 0.0))
        try:
            second = recovery.raw_spotcheck_snapshot()
        except Exception as exc:
            return {
                "status": "unknown",
                "paused": False,
                "reason": f"second raw read failed: {type(exc).__name__}: {exc}",
                "first": _spotcheck_public_snapshot(first),
            }

        try:
            confirmed, observed = _confirmed_spotcheck_issues(
                first, second, previous, cfg
            )
            progress = _spotcheck_progress(previous, second, cfg)
        except Exception as exc:
            return {
                "status": "unknown",
                "paused": False,
                "reason": (
                    "raw snapshot interpretation failed: "
                    f"{type(exc).__name__}: {exc}"
                ),
            }

        result: dict[str, Any] = {
            "status": "ok",
            "paused": False,
            "seeded": previous is None,
            "snapshot": _spotcheck_public_snapshot(second),
            **progress,
        }
        if confirmed:
            reason = SPOTCHECK_PAUSE_PREFIX + ": " + "; ".join(
                f"{code}: {detail}" for code, detail in confirmed.items()
            )
            result.update(
                {
                    "status": "pause_required",
                    "pause_reasons": confirmed,
                    "pause_reason": reason,
                }
            )
            if apply_pause:
                try:
                    recovery.persist_agent_open(reason)
                except Exception as exc:
                    result.update(
                        {
                            "status": "pause_failed",
                            "pause_error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    return result
                result.update({"status": "paused", "paused": True})
            return result
        if observed:
            result.update({"status": "observing", "observed_once": observed})
            return result

        _atomic_json(cfg.spotcheck_file, second)
        return result


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    audit = sub.add_parser("audit")
    audit.add_argument("--full", action="store_true")
    audit.add_argument(
        "--legacy-held",
        action="store_true",
        help="read-only audit of the sealed 3,823 hold during atomic migration",
    )
    opened = sub.add_parser("emergency-open")
    opened.add_argument("--reason", required=True)
    configured = sub.add_parser("configure-stage")
    configured.add_argument("--cap", type=int, required=True)
    configured.add_argument("--max-agent-active", type=int, required=True)
    configured.add_argument("--max-provider-in-flight", type=int, required=True)
    status = sub.add_parser("incident-status")
    status.add_argument("--full", action="store_true")
    spotcheck = sub.add_parser("spot-check")
    spotcheck.add_argument("--json", action="store_true")
    spotcheck.add_argument("--apply-pause", action="store_true")
    sub.add_parser("finalize")
    sub.add_parser("guard")
    sub.add_parser("watchdog")
    sub.add_parser("controller")
    args = parser.parse_args()
    if args.command == "emergency-open":
        emergency_cfg = EmergencyConfig.from_env()
        emergency_open(emergency_cfg, args.reason)
        print(json.dumps({"agent_circuit_open": True, "pocket_circuit_open": True}))
        return
    cfg = Config.from_env(
        allow_legacy_held_stage=args.command
        in {
            "audit",
            "configure-stage",
            "incident-status",
        }
    )
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    if args.command == "guard":
        run_guard(cfg)
        return
    if args.command == "watchdog":
        run_watchdog(cfg)
        return
    if args.command == "controller":
        run_controller(cfg)
        return
    recovery = Recovery(cfg)
    if args.command == "spot-check":
        print(
            json.dumps(
                run_spotcheck(recovery, apply_pause=args.apply_pause),
                default=str,
                sort_keys=True,
            )
        )
    elif args.command == "audit":
        phase = load_phase(cfg)
        if args.legacy_held:
            snap = recovery.legacy_held_audit(phase)
        else:
            snap = (
                recovery.full_audit(phase) if args.full else recovery.quick_snapshot()
            )
        if not args.full:
            snap["failures"] = recovery.quick_failures(snap, phase)
        print(json.dumps(_summary(snap), default=str, sort_keys=True))
        if snap.get("failures"):
            raise SystemExit(2)
    elif args.command == "configure-stage":
        result = recovery.configure_stage(
            cap=args.cap,
            max_agent_active=args.max_agent_active,
            max_provider_in_flight=args.max_provider_in_flight,
        )
        print(json.dumps(result, default=str, sort_keys=True))
    elif args.command == "incident-status":
        result = incident_status(recovery, full=args.full)
        print(json.dumps(result, default=str, sort_keys=True))
        if result["audit"].get("failures"):
            raise SystemExit(2)
    elif args.command == "finalize":
        print(json.dumps(_summary(recovery.finalize()), default=str, sort_keys=True))


if __name__ == "__main__":
    main()
