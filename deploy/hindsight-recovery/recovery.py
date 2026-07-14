#!/usr/bin/env python3
"""Restart-safe, fail-closed controller for the staged Hindsight backfill.

The guard may open the Agent circuit but never closes it.  Circuit closure is
available only through the explicit ``release`` subcommand after ten clean
checks.  Existing submitted operations are never submitted again: the only
automatic terminal reconciliation path calls the application's own
``OutboxWorker._refresh_submitted`` after proving completion and document
presence.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

import psycopg
from psycopg.rows import dict_row


BASELINE_FUGU_FAILURES = (
    ("2026-07-13T10:17:39.904527+00:00", 502, "openai/gpt-oss-120b"),
    ("2026-07-13T10:47:44.852290+00:00", 401, "openai/gpt-oss-120b"),
    ("2026-07-13T10:47:47.376422+00:00", 401, "openai/gpt-oss-120b"),
    ("2026-07-13T12:58:02.360195+00:00", 401, "openai/gpt-oss-120b"),
    ("2026-07-13T14:29:05.181051+00:00", 401, "openai/gpt-oss-120b"),
    ("2026-07-13T14:29:05.565413+00:00", 401, "openai/gpt-oss-120b"),
    ("2026-07-13T15:10:15.314732+00:00", 401, "openai/gpt-oss-120b"),
    ("2026-07-14T05:08:06.566399+00:00", 401, "openai/gpt-oss-120b"),
)
HELD_DOCUMENT = "a58b5c6d950f7e0ebd79e6b166639f2b9cb6ff931092ec00cc6dacd9ad2ddea4"
HELD_PARENT = "2856afaf-3c10-4f3d-9759-d9add33e1823"
HELD_CHILD = "87f56f9b-8b06-4b68-9e8a-91e83313c696"
AMBIGUOUS_CONSOLIDATION = "64b63e8e-82e6-4f09-8840-7c7c3a3a97ed"
OPEN_BUDGET_REASON = "ambiguous upstream failure: ReadTimeout"
HOLD_AGENT_REASON = "stage3823 incident hold: provider fan-out5 and ambiguous ReadTimeout"
PROVIDER_AUTH_FAILURE_ROWS = (
    (
        "b351931e6c08d4912e84885c45c819761463992a1dc22f38391950ffddedf6ff",
        "d4068c32-d268-4115-bfbb-c9cfd3941582",
        "24947151-ce00-40a4-9a4c-6a74593f4261",
        "0acec449b504c8ed1c043a36f0eedc90b578c06e482ba63d18c45cba7202d339",
        3867,
    ),
    (
        "b35f93dd699cca294d7f90c12a76cb47e934db391409233fff1dc592d331648a",
        "694de3a3-9e9c-41ed-af9c-93e8f635da9d",
        "3b18b42c-fc1c-44f1-8177-d21d1d88503e",
        "fc9ca8e9a53f76d0a0e6dc5aa6f67320c4fafdd61b3127784c1a86c5a34eda78",
        2279,
    ),
)
ATTEMPTED_STATES = ("submitting", "submitted", "succeeded", "failed", "blocked")
ACTIVE_STATES = ("submitting", "submitted")
ERROR_STATES = ("failed", "blocked")


class InvariantError(RuntimeError):
    pass


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
    expected_hindsight_failed: int
    conservative_remaining_cost: Decimal
    hard_budget: Decimal
    quick_seconds: float
    full_seconds: float
    watchdog_seconds: float
    heartbeat_max_age: float

    @classmethod
    def from_env(cls) -> "Config":
        root = Path(os.getenv("RECOVERY_ROOT", "/home/ubuntu/teemesh"))
        state_dir = Path(
            os.getenv(
                "RECOVERY_STATE_DIR",
                str(Path.home() / ".local/state/hindsight-recovery"),
            )
        )
        pg_ports = tuple(int(v) for v in os.getenv("PG_LOCAL_PORTS", "55439,55440,55441").split(","))
        pg_hosts = tuple(os.getenv("PG_REMOTE_HOSTS", "10.18.147.86,10.18.251.71,10.18.172.186").split(","))
        patroni_ports = tuple(int(v) for v in os.getenv("PATRONI_LOCAL_PORTS", "18081,18082,18083").split(","))
        if len(pg_ports) != 3 or len(pg_hosts) != 3 or len(patroni_ports) != 3:
            raise ValueError("PG and Patroni host/port lists must each contain exactly three entries")
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
            stage_cap=_int("STAGE_CAP", 3823),
            max_agent_active=_int("MAX_AGENT_ACTIVE", 2),
            max_provider_in_flight=_int("MAX_PROVIDER_IN_FLIGHT", 6),
            expected_hindsight_failed=_int("EXPECTED_HINDSIGHT_FAILED", 60),
            conservative_remaining_cost=Decimal(os.getenv("CONSERVATIVE_REMAINING_COST_USD", "0.00543")),
            hard_budget=Decimal(os.getenv("HARD_BUDGET_USD", "30")),
            quick_seconds=float(os.getenv("GUARD_QUICK_SECONDS", "2")),
            full_seconds=float(os.getenv("GUARD_FULL_SECONDS", "60")),
            watchdog_seconds=float(os.getenv("WATCHDOG_SECONDS", "5")),
            # A normal full provenance audit takes longer than one 5-second
            # watchdog interval.  Sixty seconds still fails closed promptly,
            # while allowing a full audit to finish under transient host load.
            heartbeat_max_age=float(os.getenv("HEARTBEAT_MAX_AGE_SECONDS", "60")),
        )

    @property
    def phase_file(self) -> Path:
        return self.state_dir / "phase.json"

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


DEFAULT_PHASE: dict[str, Any] = {
    "name": "maintenance",
    "budget": "open",
    "expected_retry": 17,
    "expected_succeeded": 3263,
    "watchdog_armed": False,
}


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".new")
    temporary.write_text(json.dumps(value, sort_keys=True) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def load_phase(cfg: Config) -> dict[str, Any]:
    if not cfg.phase_file.exists():
        _atomic_json(cfg.phase_file, DEFAULT_PHASE)
    return {**DEFAULT_PHASE, **json.loads(cfg.phase_file.read_text())}


def save_phase(cfg: Config, phase: dict[str, Any]) -> None:
    phase = dict(phase)
    phase["updated_at"] = time.time()
    _atomic_json(cfg.phase_file, phase)


def event(cfg: Config, kind: str, **fields: Any) -> None:
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    payload = {"at": time.time(), "kind": kind, **fields}
    with cfg.event_log.open("a") as handle:
        handle.write(json.dumps(payload, default=str, sort_keys=True) + "\n")


def compute_projection(spent: Any, succeeded: int, cfg: Config) -> Decimal:
    remaining = max(cfg.approved_total - succeeded, 0)
    return Decimal(str(spent)) + Decimal(remaining) * cfg.conservative_remaining_cost


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
    return None


class Recovery:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.agent_env = _env_file(Path.home() / ".attestmesh/agent-session-mcp.env")
        self.pocket_env = _env_file(Path.home() / ".attestmesh/pocket-mcp.env")
        self.agent_runtime = _env_file(Path.home() / ".attestmesh/agent-session-hindsight.env")
        self.hindsight_state = _env_file(cfg.root / "deploy/logs/hindsight-node-hindsight-node.state")
        self.fugu_env = _env_file(Path.home() / ".attestmesh/fugu-router.env")

    def _connect(self, user: str, password: str, dbname: str, *, ports: Iterable[int] | None = None):
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
        return self._connect("agent_sessions", self.agent_env["APP_DB_PASSWORD"], "agent_sessions", ports=ports)

    def pocket_db(self):
        return self._connect("pocket", self.pocket_env["APP_DB_PASSWORD"], "pocket")

    def hindsight_db(self):
        return self._connect("hindsight", self.hindsight_state["DB_PASSWORD"], "hindsight")

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

    @staticmethod
    def _counts(cur: Any, table: str = "hindsight_outbox") -> dict[str, int]:
        cur.execute(f"SELECT state, count(*)::int AS n FROM {table} GROUP BY state ORDER BY state")
        return {str(row["state"]): int(row["n"]) for row in cur.fetchall()}

    @staticmethod
    def _http_json(url: str, *, headers: dict[str, str] | None = None) -> dict[str, Any]:
        request = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(request, timeout=4) as response:
            if response.status != 200:
                raise InvariantError(f"{url} returned HTTP {response.status}")
            return json.loads(response.read())

    def quick_snapshot(self) -> dict[str, Any]:
        result: dict[str, Any] = {"runtime": dict(self.agent_runtime)}
        with self.agent_db() as conn, conn.cursor() as cur:
            counts = self._counts(cur)
            cur.execute("SELECT circuit_open, reason FROM hindsight_sync_state WHERE bank_id='agent-sessions'")
            result["agent"] = {"counts": counts, "circuit": cur.fetchone()}
        with self.pocket_db() as conn, conn.cursor() as cur:
            counts = self._counts(cur)
            cur.execute("SELECT circuit_open, reason FROM hindsight_sync_state WHERE bank_id='dans-pocket'")
            result["pocket"] = {"counts": counts, "circuit": cur.fetchone()}
        with self.hindsight_db() as conn, conn.cursor() as cur:
            cur.execute("SELECT status, count(*)::int AS n FROM async_operations WHERE bank_id='agent-sessions' GROUP BY status")
            result["hindsight_operations"] = {str(row["status"]): int(row["n"]) for row in cur.fetchall()}
            cur.execute("SELECT count(*)::int AS n FROM documents WHERE bank_id='agent-sessions'")
            result["hindsight_documents"] = int(cur.fetchone()["n"])
        result["hindsight_health"] = self._http_json(f"http://127.0.0.1:{self.cfg.hindsight_port}/health")
        result["fugu_health"] = self._http_json(f"http://127.0.0.1:{self.cfg.fugu_port}/health/process")
        result["budget"] = self._http_json(f"http://127.0.0.1:{self.cfg.budget_port}/health")
        return result

    def quick_failures(self, snap: dict[str, Any], phase: dict[str, Any]) -> list[str]:
        failures: list[str] = []
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
        if name != "running" and not bool(snap["agent"]["circuit"]["circuit_open"]):
            failures.append("Agent circuit is closed outside running phase")
        if name == "running" and attempted >= self.cfg.stage_cap and not bool(snap["agent"]["circuit"]["circuit_open"]):
            failures.append("Agent circuit remained closed at stage cap")
        if not bool(snap["pocket"]["circuit"]["circuit_open"]):
            failures.append("Pocket circuit is closed")
        if sum(int(pc.get(state, 0)) for state in ACTIVE_STATES + ERROR_STATES):
            failures.append("Pocket has active/error work")
        if self.agent_runtime.get("HINDSIGHT_OUTBOX_MAX_IN_FLIGHT") != str(
            self.cfg.max_agent_active
        ):
            failures.append(
                f"Agent persistent max_in_flight is not {self.cfg.max_agent_active}"
            )
        if self.agent_runtime.get("HINDSIGHT_OUTBOX_RUN_LIMIT") != str(self.cfg.stage_cap):
            failures.append("Agent persistent cap drift")
        hop = snap["hindsight_operations"]
        if int(hop.get("failed", 0)) != self.cfg.expected_hindsight_failed:
            failures.append("Hindsight failed-operation count drift")
        if name != "running" and sum(int(hop.get(state, 0)) for state in ("pending", "processing")):
            failures.append("Hindsight active work outside running phase")
        budget = snap["budget"]
        in_flight = int(budget.get("in_flight") or 0)
        if in_flight > self.cfg.max_provider_in_flight:
            failures.append(
                f"provider in_flight exceeds configured limit {self.cfg.max_provider_in_flight}"
            )
        if phase.get("budget") == "open":
            if not bool(budget.get("circuit_open")) or budget.get("circuit_reason") != OPEN_BUDGET_REASON:
                failures.append("budget open-circuit reason drift")
        elif phase.get("budget") == "closed":
            if bool(budget.get("circuit_open")) or budget.get("status") not in {"healthy", "ok"}:
                failures.append("budget circuit is not healthy/closed")
        projection = compute_projection(budget.get("hindsight_backfill_spent_usd", 0), int(ac.get("succeeded", 0)), self.cfg)
        if projection > self.cfg.hard_budget:
            failures.append(f"projected full batch {projection} exceeds hard budget")
        if snap["hindsight_health"].get("status") != "healthy" or snap["hindsight_health"].get("database") != "connected":
            failures.append("Hindsight health degraded")
        return failures

    def full_audit(self, phase: dict[str, Any]) -> dict[str, Any]:
        snap = self.quick_snapshot()
        failures = self.quick_failures(snap, phase)
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
            cur.execute("SELECT count(*)::int AS n FROM hindsight_outbox_claim_recovery_history")
            claim = int(cur.fetchone()["n"])
            cur.execute("SELECT count(*)::int AS n FROM hindsight_outbox_operation_adoption_history")
            adoption = int(cur.fetchone()["n"])
        documents = [str(row["document_id"]) for row in rows]
        if len(documents) != len(set(documents)) or len(documents) != self.cfg.approved_total:
            failures.append("Agent duplicate/missing document ID")
        operation_ids = [str(row["operation_id"]) for row in rows if row.get("operation_id")]
        if len(operation_ids) != len(set(operation_ids)):
            failures.append("duplicate Agent operation ID")
        for row in rows:
            item = dict(row["item"])
            encoded = json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            digest = hashlib.sha256(encoded.encode()).hexdigest()
            if digest != row["payload_hash"]:
                failures.append(f"payload hash mismatch for {row['document_id']}")
                break
            if item.get("document_id") != row["document_id"]:
                failures.append(f"item/document mismatch for {row['document_id']}")
                break
            context = str(item.get("context") or "")
            if "source:agent_sessions" not in context or f"document:{row['document_id']}" not in context:
                failures.append(f"item source/context mismatch for {row['document_id']}")
                break
            if row["source_kind"] != "agent_session" or row["source_document"] != row["document_id"]:
                failures.append(f"source lineage mismatch for {row['document_id']}")
                break
            if row["state"] in ACTIVE_STATES and row["payload_hash"] != row["submitted_payload_hash"]:
                failures.append(f"active submitted hash mismatch for {row['document_id']}")
                break
        expected_histories = (int(phase["expected_retry"]), 12, 1)
        if (retry, claim, adoption) != expected_histories:
            failures.append(f"history drift: {(retry, claim, adoption)} != {expected_histories}")
        with self.hindsight_db() as conn, conn.cursor() as cur:
            cur.execute("SELECT id FROM documents WHERE bank_id='agent-sessions'")
            remote_documents = [str(row["id"]) for row in cur.fetchall()]
        if len(remote_documents) != len(set(remote_documents)):
            failures.append("duplicate Hindsight document ID")
        approved = set(documents)
        remote = set(remote_documents)
        if remote - approved:
            failures.append("Hindsight contains document outside approved batch")
        succeeded = {str(row["document_id"]) for row in rows if row["state"] == "succeeded"}
        if succeeded - remote:
            failures.append("succeeded Agent document missing from Hindsight")
        permitted_remote = {str(row["document_id"]) for row in rows if row["state"] in {"succeeded", "submitting", "submitted"}}
        if remote - permitted_remote:
            failures.append("Hindsight document has invalid Agent state provenance")
        with self.fugu_db() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT start_time,http_status,requested_model
                   FROM fugu_credit_ledger
                   WHERE start_time >= '2026-07-13 10:17:39+00' AND status <> 'success'
                   ORDER BY start_time"""
            )
            actual_failures = tuple(
                (row["start_time"].isoformat(), int(row["http_status"]), str(row["requested_model"]))
                for row in cur.fetchall()
            )
        if actual_failures != BASELINE_FUGU_FAILURES:
            failures.append("authenticated Fugu failure ledger drift")
        patroni: list[dict[str, Any]] = []
        for port in self.cfg.patroni_ports:
            body = self._http_json(f"http://127.0.0.1:{port}/patroni")
            patroni.append(body)
            if body.get("state") != "running" or body.get("role") not in {"primary", "replica"}:
                failures.append(f"Patroni degradation on local forward {port}")
        if sum(node.get("role") == "primary" for node in patroni) != 1:
            failures.append("Patroni does not have exactly one primary")
        snap["histories"] = {"retry": retry, "claim": claim, "adoption": adoption}
        snap["remote_documents"] = len(remote_documents)
        snap["patroni"] = [{"role": node.get("role"), "state": node.get("state"), "timeline": node.get("timeline")} for node in patroni]
        snap["failures"] = failures
        return snap

    def prove_held_failure(self) -> None:
        phase = load_phase(self.cfg)
        audit = self.full_audit(phase)
        if audit["failures"]:
            raise InvariantError("; ".join(audit["failures"]))
        if phase.get("budget") != "closed" or int(audit["budget"].get("in_flight") or 0) != 0:
            raise InvariantError("budget must be healthy/closed with zero in-flight before retry")
        with self.agent_db() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT state,operation_id::text,payload_hash,submitted_payload_hash,item
                   FROM hindsight_outbox WHERE document_id=%s""",
                (HELD_DOCUMENT,),
            )
            row = cur.fetchone()
        if not row or row["state"] != "submitted" or row["operation_id"] != HELD_PARENT:
            raise InvariantError("preserved Agent row is not the exact submitted parent")
        if row["payload_hash"] != row["submitted_payload_hash"] or row["item"].get("document_id") != HELD_DOCUMENT:
            raise InvariantError("preserved Agent row hash/item proof failed")
        with self.hindsight_db() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT operation_id::text,operation_type,status,error_message,result_metadata,task_payload,created_at,updated_at
                   FROM async_operations WHERE operation_id=ANY(%s::uuid[])""",
                ([HELD_PARENT, HELD_CHILD],),
            )
            operations = {str(row["operation_id"]): row for row in cur.fetchall()}
            cur.execute("SELECT EXISTS(SELECT 1 FROM documents WHERE bank_id='agent-sessions' AND id=%s) AS present", (HELD_DOCUMENT,))
            present = bool(cur.fetchone()["present"])
        parent = operations.get(HELD_PARENT)
        child = operations.get(HELD_CHILD)
        if not parent or not child or parent["operation_type"] != "batch_retain" or child["operation_type"] != "retain":
            raise InvariantError("preserved parent/child operation types are not exact")
        if parent["status"] != "failed" or child["status"] != "failed" or present:
            raise InvariantError("preserved parent/child must be failed and document absent")
        if "APITimeoutError" not in str(parent["error_message"]) or "APITimeoutError" not in str(child["error_message"]):
            raise InvariantError("preserved parent/child failure class drift")
        metadata = dict(child["result_metadata"] or {})
        if metadata.get("parent_operation_id") != HELD_PARENT or metadata.get("document_ids") != [HELD_DOCUMENT]:
            raise InvariantError("preserved child lineage drift")
        with self.fugu_db() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT status,http_status FROM fugu_credit_ledger
                   WHERE start_time >= %s AND start_time <= %s ORDER BY start_time""",
                (parent["created_at"], child["updated_at"]),
            )
            calls = list(cur.fetchall())
        if not calls or any(row["status"] != "success" or int(row["http_status"] or 0) != 200 for row in calls):
            raise InvariantError("upstream calls in the failed-operation window are not terminal HTTP200 successes")

    def _app_worker(self):
        server = Path.home() / "agent-session-mcp/server"
        sys.path.insert(0, str(server))
        from config import Settings  # type: ignore
        from hindsight import HindsightClient  # type: ignore
        from outbox import OutboxWorker  # type: ignore

        settings = Settings(
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
        return OutboxWorker(
            settings,
            HindsightClient(
                base_url=settings.hindsight_url,
                token=settings.hindsight_token,
                bank_id=settings.hindsight_bank,
                timeout=10,
            ),
        )

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
            cur.execute("SELECT pg_try_advisory_lock(hashtextextended('agent-sessions',0)) AS locked")
            if not cur.fetchone()["locked"]:
                conn.close()
                raise InvariantError("could not acquire Agent bank advisory lock")
        try:
            yield conn
        finally:
            with contextlib.suppress(Exception):
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(hashtextextended('agent-sessions',0))")
            conn.close()

    def refresh_retry(self) -> dict[str, Any]:
        self.prove_held_failure()
        worker = self._app_worker()
        with self.bank_lock() as conn:
            completed, failed = worker._refresh_submitted(conn)
            if (completed, failed) != (0, 1):
                raise InvariantError(f"refresh-only result {(completed, failed)} != (0, 1)")
            from outbox import retry_document  # type: ignore

            with conn.transaction():
                retried = retry_document(
                    conn,
                    HELD_DOCUMENT,
                    reason="authorized one-shot retry after max-charge ReadTimeout reconciliation",
                )
            if not retried:
                raise InvariantError("exact failed document was not requeued")
        phase = load_phase(self.cfg)
        phase.update({"name": "ready", "budget": "closed", "expected_retry": 18, "expected_succeeded": 3263})
        save_phase(self.cfg, phase)
        audit = self.full_audit(phase)
        counts = audit["agent"]["counts"]
        expected = {"queued": 1879, "succeeded": 3263}
        if counts != expected or audit["histories"] != {"retry": 18, "claim": 12, "adoption": 1} or audit["remote_documents"] != 3263:
            raise InvariantError(f"post-retry baseline mismatch: counts={counts} histories={audit['histories']} docs={audit['remote_documents']}")
        if audit["failures"]:
            raise InvariantError("; ".join(audit["failures"]))
        event(self.cfg, "refresh_retry_complete", document_id=HELD_DOCUMENT)
        return audit

    def recover_provider_auth_failures(self) -> dict[str, Any]:
        """Requeue the two exactly-proven terminal provider-401 documents.

        The controller is restart-idempotent across the submitted -> failed ->
        queued transitions.  It never calls retain directly and acknowledges
        circuit-class failures only by their exact document/operation pairs.
        """
        phase = load_phase(self.cfg)
        audit = self.full_audit(phase)
        tolerated = {
            "Agent failed/blocked row present",
        }
        unexpected = [
            failure
            for failure in audit["failures"]
            if failure not in tolerated and not failure.startswith("history drift:")
        ]
        if unexpected:
            raise InvariantError("; ".join(unexpected))
        if phase.get("budget") != "closed":
            raise InvariantError("provider failure recovery requires closed budget phase")
        if not bool(audit["agent"]["circuit"]["circuit_open"]):
            raise InvariantError("provider failure recovery requires Agent circuit open")
        if int(audit["budget"].get("in_flight") or 0):
            raise InvariantError("provider failure recovery requires zero provider in-flight")
        if bool(audit["budget"].get("circuit_open")) or audit["budget"].get("status") not in {"healthy", "ok"}:
            raise InvariantError("provider failure recovery requires healthy budget circuit")
        if sum(
            int(audit["hindsight_operations"].get(state, 0))
            for state in ("pending", "processing")
        ):
            raise InvariantError("provider failure recovery requires zero Hindsight activity")

        expected = {
            document_id: {
                "parent": parent,
                "child": child,
                "payload_hash": payload_hash,
                "source_id": source_id,
            }
            for document_id, parent, child, payload_hash, source_id
            in PROVIDER_AUTH_FAILURE_ROWS
        }
        document_ids = sorted(expected)
        parent_ids = [value["parent"] for value in expected.values()]
        operation_ids = [
            operation_id
            for value in expected.values()
            for operation_id in (value["parent"], value["child"])
        ]

        with self.agent_db() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT o.document_id,o.operation_id::text,o.state,o.payload_hash,
                          o.submitted_payload_hash,o.item,o.source_kind,o.source_id,
                          o.available_at,s.content_hash AS source_document
                   FROM hindsight_outbox o
                   LEFT JOIN sessions s ON s.id=o.source_id
                   WHERE o.document_id=ANY(%s::text[])
                   ORDER BY o.document_id""",
                (document_ids,),
            )
            rows = {str(row["document_id"]): row for row in cur.fetchall()}
            cur.execute(
                """SELECT document_id,operation_id::text,previous_state,payload_hash,
                          submitted_payload_hash,item
                   FROM hindsight_outbox_retry_history
                   WHERE document_id=ANY(%s::text[])
                   ORDER BY document_id,recorded_at""",
                (document_ids,),
            )
            histories = list(cur.fetchall())
            cur.execute(
                """SELECT document_id,operation_id::text
                   FROM hindsight_outbox
                   WHERE state IN ('submitting','submitted')
                     AND document_id<>ALL(%s::text[])""",
                (document_ids,),
            )
            if cur.fetchone():
                raise InvariantError("unexpected active Agent row during provider recovery")

        if set(rows) != set(document_ids):
            raise InvariantError("provider recovery document set drift")
        histories_by_document: dict[str, list[dict[str, Any]]] = {
            document_id: [] for document_id in document_ids
        }
        for history in histories:
            histories_by_document[str(history["document_id"])].append(history)

        for document_id, row in rows.items():
            proof = expected[document_id]
            if row["state"] not in {"submitted", "failed", "queued"}:
                raise InvariantError(f"provider recovery state drift for {document_id}")
            if (
                row["operation_id"] not in {proof["parent"], None}
                or row["payload_hash"] != proof["payload_hash"]
                or row["source_kind"] != "agent_session"
                or int(row["source_id"]) != proof["source_id"]
                or row["source_document"] != document_id
                or row["item"].get("document_id") != document_id
            ):
                raise InvariantError(f"provider recovery Agent provenance drift for {document_id}")
            if row["state"] in {"submitted", "failed"}:
                if row["operation_id"] != proof["parent"] or row["submitted_payload_hash"] != proof["payload_hash"]:
                    raise InvariantError(f"provider recovery submitted hash/operation drift for {document_id}")
                if histories_by_document[document_id]:
                    raise InvariantError(f"provider recovery premature history for {document_id}")
            else:
                exact_history = histories_by_document[document_id]
                if len(exact_history) != 1:
                    raise InvariantError(f"provider recovery history count drift for {document_id}")
                history = exact_history[0]
                if (
                    history["operation_id"] != proof["parent"]
                    or history["previous_state"] != "failed"
                    or history["payload_hash"] != proof["payload_hash"]
                    or history["submitted_payload_hash"] != proof["payload_hash"]
                    or history["item"] != row["item"]
                ):
                    raise InvariantError(f"provider recovery history provenance drift for {document_id}")

        with self.hindsight_db() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT operation_id::text,operation_type,status,error_message,
                          result_metadata,task_payload
                   FROM async_operations
                   WHERE operation_id=ANY(%s::uuid[])""",
                (operation_ids,),
            )
            operations = {
                str(row["operation_id"]): row for row in cur.fetchall()
            }
            cur.execute(
                """SELECT id FROM documents
                   WHERE bank_id='agent-sessions' AND id=ANY(%s::text[])""",
                (document_ids,),
            )
            present = [str(row["id"]) for row in cur.fetchall()]
        if set(operations) != set(operation_ids) or present:
            raise InvariantError("provider recovery remote operation/document set drift")

        for document_id, row in rows.items():
            proof = expected[document_id]
            parent = operations[proof["parent"]]
            child = operations[proof["child"]]
            if (
                parent["operation_type"] != "batch_retain"
                or child["operation_type"] != "retain"
                or parent["status"] != "failed"
                or child["status"] != "failed"
                or "provider returned 401" not in str(parent["error_message"])
                or "provider returned 401" not in str(child["error_message"])
            ):
                raise InvariantError(f"provider recovery terminal failure drift for {document_id}")
            metadata = dict(child["result_metadata"] or {})
            payload = dict(child["task_payload"] or {})
            contents = list(payload.get("contents") or [])
            canonical_item = dict(contents[0]) if len(contents) == 1 else {}
            # Hindsight normalizes the public retain-item ``timestamp`` field
            # to ``event_date`` in its internal async-operation payload.
            if "event_date" in canonical_item and "timestamp" not in canonical_item:
                canonical_item["timestamp"] = canonical_item.pop("event_date")
            if (
                metadata.get("parent_operation_id") != proof["parent"]
                or metadata.get("document_ids") != [document_id]
                or payload.get("bank_id") != "agent-sessions"
                or payload.get("operation_id") != proof["child"]
                or len(contents) != 1
                or canonical_item != row["item"]
            ):
                raise InvariantError(f"provider recovery remote lineage drift for {document_id}")
            encoded = json.dumps(
                canonical_item, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
            if hashlib.sha256(encoded.encode()).hexdigest() != proof["payload_hash"]:
                raise InvariantError(f"provider recovery canonical hash drift for {document_id}")

        worker = self._app_worker()
        original_positions = {
            document_id: rows[document_id]["available_at"]
            for document_id in document_ids
        }
        with self.bank_lock() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT circuit_open FROM hindsight_sync_state WHERE bank_id='agent-sessions'"
                )
                circuit = cur.fetchone()
                cur.execute(
                    """SELECT document_id,operation_id::text,state,payload_hash,
                              submitted_payload_hash
                       FROM hindsight_outbox
                       WHERE document_id=ANY(%s::text[])
                       ORDER BY document_id""",
                    (document_ids,),
                )
                locked_rows = list(cur.fetchall())
                cur.execute(
                    """SELECT document_id,operation_id::text
                       FROM hindsight_outbox WHERE state='submitted'"""
                )
                all_submitted = list(cur.fetchall())
            if not circuit or not circuit["circuit_open"]:
                raise InvariantError("Agent circuit closed before provider recovery")
            submitted_pairs = frozenset(
                (str(row["document_id"]), str(row["operation_id"]))
                for row in locked_rows
                if row["state"] == "submitted"
            )
            if set(submitted_pairs) != {
                (str(row["document_id"]), str(row["operation_id"]))
                for row in all_submitted
            }:
                raise InvariantError("submitted row set changed before provider recovery")
            if submitted_pairs:
                completed, failed = worker._refresh_submitted(
                    conn,
                    allowed_terminal_circuit_failures=submitted_pairs,
                )
                if (completed, failed) != (0, len(submitted_pairs)):
                    raise InvariantError(
                        f"provider recovery refresh result {(completed, failed)}"
                    )

            from outbox import retry_document  # type: ignore

            for document_id in document_ids:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT state FROM hindsight_outbox WHERE document_id=%s",
                        (document_id,),
                    )
                    current = cur.fetchone()
                if current and current["state"] == "failed":
                    with conn.transaction():
                        retried = retry_document(
                            conn,
                            document_id,
                            reason="authorized retry after terminal provider-401 recovery",
                            preserve_queue_position=True,
                        )
                    if not retried:
                        raise InvariantError(f"provider recovery retry failed for {document_id}")

            with conn.cursor() as cur:
                cur.execute(
                    """SELECT document_id,state,available_at
                       FROM hindsight_outbox
                       WHERE document_id=ANY(%s::text[])
                       ORDER BY document_id""",
                    (document_ids,),
                )
                final_rows = list(cur.fetchall())
                cur.execute(
                    """SELECT document_id,operation_id::text
                       FROM hindsight_outbox_retry_history
                       WHERE document_id=ANY(%s::text[])
                       ORDER BY document_id""",
                    (document_ids,),
                )
                final_histories = list(cur.fetchall())
        if any(
            row["state"] != "queued"
            or row["available_at"] != original_positions[str(row["document_id"])]
            for row in final_rows
        ):
            raise InvariantError("provider recovery queue position/state drift")
        if {
            (str(row["document_id"]), str(row["operation_id"]))
            for row in final_histories
        } != {
            (document_id, expected[document_id]["parent"])
            for document_id in document_ids
        }:
            raise InvariantError("provider recovery final history drift")

        phase.update(
            {
                "name": "ready",
                "budget": "closed",
                "expected_retry": 20,
                "expected_succeeded": int(audit["agent"]["counts"].get("succeeded", 0)),
            }
        )
        save_phase(self.cfg, phase)
        settled = self.full_audit(phase)
        expected_succeeded = int(phase["expected_succeeded"])
        expected_counts = {
            "queued": self.cfg.approved_total - expected_succeeded,
            "succeeded": expected_succeeded,
        }
        if settled["agent"]["counts"] != expected_counts:
            raise InvariantError(
                f"provider recovery counts mismatch: {settled['agent']['counts']} != {expected_counts}"
            )
        if (
            settled["histories"] != {"retry": 20, "claim": 12, "adoption": 1}
            or settled["remote_documents"] != expected_succeeded
            or settled["failures"]
        ):
            raise InvariantError(f"provider recovery audit failed: {settled['failures']}")
        event(
            self.cfg,
            "provider_auth_failures_requeued",
            document_ids=document_ids,
            operation_ids=parent_ids,
        )
        return settled

    def close_agent(self, reason: str = "authorized staged recovery release") -> None:
        with self.bank_lock() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(
                """UPDATE hindsight_sync_state
                   SET circuit_open=false,reason=NULL,opened_at=NULL,updated_at=now()
                   WHERE bank_id='agent-sessions' AND circuit_open=true"""
            )
            if cur.rowcount != 1:
                raise InvariantError("Agent circuit was not exactly one open row")
        event(self.cfg, "agent_closed", reason=reason)

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

        with self.agent_db() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT document_id,operation_id::text,payload_hash,submitted_payload_hash
                   FROM hindsight_outbox WHERE state='submitted'"""
            )
            rows = list(cur.fetchall())
        if not rows or any(
            not row["operation_id"]
            or row["payload_hash"] != row["submitted_payload_hash"]
            for row in rows
        ):
            raise InvariantError("pre-cap submitted-row operation/hash proof failed")
        proofs = [
            (str(row["document_id"]), str(row["operation_id"]))
            for row in rows
        ]
        with self.hindsight_db() as conn, conn.cursor() as cur:
            for document_id, operation_id in proofs:
                cur.execute(
                    "SELECT status FROM async_operations WHERE operation_id=%s::uuid",
                    (operation_id,),
                )
                operation = cur.fetchone()
                cur.execute(
                    "SELECT EXISTS(SELECT 1 FROM documents WHERE bank_id='agent-sessions' AND id=%s) AS present",
                    (document_id,),
                )
                present = bool(cur.fetchone()["present"])
                if not operation or operation["status"] != "completed" or not present:
                    raise InvariantError(
                        "pre-cap submitted operation is not completed with its document present"
                    )

        worker = self._app_worker()
        with self.bank_lock() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT circuit_open FROM hindsight_sync_state WHERE bank_id='agent-sessions'"
                )
                circuit = cur.fetchone()
                cur.execute(
                    """SELECT document_id,operation_id::text,payload_hash,submitted_payload_hash
                       FROM hindsight_outbox WHERE state='submitted'"""
                )
                locked_rows = list(cur.fetchall())
            if not circuit or not circuit["circuit_open"]:
                raise InvariantError("Agent circuit closed before pre-cap refresh")
            if len(locked_rows) != len(proofs):
                raise InvariantError("submitted-row count changed before pre-cap refresh")
            locked_proofs = {
                (str(row["document_id"]), str(row["operation_id"]))
                for row in locked_rows
                if row["payload_hash"] == row["submitted_payload_hash"]
            }
            if locked_proofs != set(proofs):
                raise InvariantError("submitted row changed before pre-cap refresh")
            completed, failed = worker._refresh_submitted(conn)
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
            raise InvariantError(f"pre-cap reconciliation audit failed: {settled['failures']}")
        event(
            self.cfg,
            "precap_hold_reconciled",
            document_ids=[document_id for document_id, _ in proofs],
            operation_ids=[operation_id for _, operation_id in proofs],
            succeeded=expected_succeeded,
        )
        return settled

    def release(self) -> dict[str, Any]:
        phase = load_phase(self.cfg)
        if phase["name"] != "ready" or phase.get("budget") != "closed":
            raise InvariantError("release requires ready phase and closed budget")
        prior_spend: Any = None
        for index in range(10):
            snap = self.quick_snapshot()
            failures = self.quick_failures(snap, phase)
            if failures:
                raise InvariantError(f"release check {index + 1}: {'; '.join(failures)}")
            current_spend = snap["budget"].get("hindsight_backfill_spent_usd")
            if prior_spend is not None and current_spend != prior_spend:
                raise InvariantError("spend changed during release stability window")
            prior_spend = current_spend
            if index in {0, 9}:
                full = self.full_audit(phase)
                if full["failures"]:
                    raise InvariantError("; ".join(full["failures"]))
            if index < 9:
                time.sleep(2)
        baseline = int(snap["agent"]["counts"].get("succeeded", 0))
        self.close_agent()
        phase.update({"name": "running", "watchdog_armed": True, "released_at": time.time()})
        save_phase(self.cfg, phase)
        deadline = time.monotonic() + 90
        target_active = self.cfg.max_agent_active
        while time.monotonic() < deadline:
            current = self.quick_snapshot()
            counts = current["agent"]["counts"]
            attempted = sum(int(counts.get(state, 0)) for state in ATTEMPTED_STATES)
            active = sum(int(counts.get(state, 0)) for state in ACTIVE_STATES)
            provider = int(current["budget"].get("in_flight") or 0)
            if attempted == baseline + target_active:
                if active != target_active or provider > self.cfg.max_provider_in_flight:
                    emergency_open(self.cfg, f"first-claim bound failed: active={active} provider={provider}")
                    raise InvariantError(
                        "first post-release wave did not reach the configured Agent/provider bounds"
                    )
                event(
                    self.cfg,
                    "first_claim_verified",
                    attempted=attempted,
                    active=active,
                    provider_in_flight=provider,
                )
                return current
            if (
                attempted > baseline + target_active
                or active > target_active
                or provider > self.cfg.max_provider_in_flight
            ):
                emergency_open(self.cfg, "first-claim count/fan-out exceeded")
                raise InvariantError("post-release work exceeded first-claim bound")
            time.sleep(2)
        emergency_open(self.cfg, "first claim did not appear within 90 seconds")
        raise InvariantError("first post-release claim timed out")

    def preprove_cap_refresh(self) -> bool:
        snap = self.quick_snapshot()
        counts = snap["agent"]["counts"]
        if sum(int(counts.get(state, 0)) for state in ATTEMPTED_STATES) != self.cfg.stage_cap:
            return False
        if sum(int(snap["hindsight_operations"].get(state, 0)) for state in ("pending", "processing")):
            return False
        if int(snap["budget"].get("in_flight") or 0):
            return False
        with self.agent_db() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT document_id,operation_id::text,payload_hash,submitted_payload_hash
                   FROM hindsight_outbox WHERE state='submitted' ORDER BY document_id"""
            )
            rows = list(cur.fetchall())
        if not rows:
            return int(counts.get("succeeded", 0)) == self.cfg.stage_cap
        with self.hindsight_db() as conn, conn.cursor() as cur:
            for row in rows:
                if not row["operation_id"] or row["payload_hash"] != row["submitted_payload_hash"]:
                    return False
                cur.execute("SELECT status FROM async_operations WHERE operation_id=%s::uuid", (row["operation_id"],))
                operation = cur.fetchone()
                cur.execute("SELECT EXISTS(SELECT 1 FROM documents WHERE bank_id='agent-sessions' AND id=%s) AS present", (row["document_id"],))
                if not operation or operation["status"] != "completed" or not cur.fetchone()["present"]:
                    return False
        return True

    def settle_cap(self) -> dict[str, Any] | None:
        phase = load_phase(self.cfg)
        if phase["name"] != "cap_hold" or not self.preprove_cap_refresh():
            return None
        worker = self._app_worker()
        with self.bank_lock() as conn:
            completed, failed = worker._refresh_submitted(conn)
            if failed:
                raise InvariantError("cap refresh encountered a failed remote operation")
        phase.update({"name": "settled", "expected_succeeded": self.cfg.stage_cap})
        save_phase(self.cfg, phase)
        audit = self.full_audit(phase)
        counts = audit["agent"]["counts"]
        if counts != {"queued": self.cfg.approved_total - self.cfg.stage_cap, "succeeded": self.cfg.stage_cap}:
            raise InvariantError(f"cap settlement counts mismatch: {counts}")
        if audit["remote_documents"] != self.cfg.stage_cap or audit["failures"]:
            raise InvariantError(f"cap settlement audit failed: {audit['failures']}")
        event(self.cfg, "stage_settled", completed=completed, succeeded=self.cfg.stage_cap)
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


def emergency_open(cfg: Config, reason: str) -> None:
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    with cfg.lock_file.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ports = _free_ports(3)
        command = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=8",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=5",
            "-o", "ServerAliveCountMax=2",
            "-N",
        ]
        for port, host in zip(ports, cfg.pg_remote_hosts):
            command.extend(["-L", f"127.0.0.1:{port}:{host}:5432"])
        command.append(cfg.ssh_target)
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            if not _wait_ports(ports) or process.poll() is not None:
                detail = process.stderr.read(500) if process.stderr else ""
                raise RuntimeError(f"emergency PG tunnel failed: {detail}")
            recovery = Recovery(cfg)
            with recovery.agent_db(ports=ports) as conn, conn.transaction(), conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO hindsight_sync_state(bank_id,circuit_open,reason,opened_at,updated_at)
                       VALUES('agent-sessions',true,%s,now(),now())
                       ON CONFLICT(bank_id) DO UPDATE SET circuit_open=true,reason=EXCLUDED.reason,
                         opened_at=COALESCE(hindsight_sync_state.opened_at,now()),updated_at=now()""",
                    (reason[:1000],),
                )
            event(cfg, "emergency_open", reason=reason[:1000])
        finally:
            process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=3)
            if process.poll() is None:
                process.kill()


def _service_active(name: str) -> bool:
    return subprocess.run(
        ["systemctl", "--user", "is-active", "--quiet", name],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0


def run_guard(cfg: Config) -> None:
    recovery = Recovery(cfg)
    last_full = 0.0
    last_failure_reason: str | None = None
    while True:
        phase = load_phase(cfg)
        try:
            snap = recovery.quick_snapshot()
            failures = recovery.quick_failures(snap, phase)
            counts = snap["agent"]["counts"]
            attempted = sum(int(counts.get(state, 0)) for state in ATTEMPTED_STATES)
            action = stage_action(attempted, str(phase["name"]), cfg.stage_cap)
            if action in {"open", "violation"}:
                emergency_open(cfg, f"stage cap guard: attempted={attempted} cap={cfg.stage_cap}")
                if action == "open":
                    phase.update({"name": "cap_hold", "expected_succeeded": int(counts.get("succeeded", 0))})
                    save_phase(cfg, phase)
            now = time.monotonic()
            if now - last_full >= cfg.full_seconds:
                # Publish liveness immediately before the heavier provenance
                # audit.  The watchdog still opens Agent if that audit exceeds
                # heartbeat_max_age, but does not mistake a healthy audit for a
                # dead guard.
                _atomic_json(
                    cfg.heartbeat_file,
                    {
                        "at": time.time(),
                        "healthy": True,
                        "phase": phase["name"],
                        "attempted": attempted,
                        "full_audit": "running",
                    },
                )
                full = recovery.full_audit(load_phase(cfg))
                failures.extend(full["failures"])
                last_full = now
            if failures:
                raise InvariantError("; ".join(sorted(set(failures))))
            if load_phase(cfg)["name"] == "cap_hold":
                recovery.settle_cap()
            _atomic_json(cfg.heartbeat_file, {"at": time.time(), "healthy": True, "phase": load_phase(cfg)["name"], "attempted": attempted})
            last_failure_reason = None
        except Exception as exc:
            reason = f"guard failure: {type(exc).__name__}: {exc}"
            circuit_proven_open = False
            with contextlib.suppress(Exception):
                circuit_proven_open = recovery.agent_circuit_is_open()
            if not circuit_proven_open:
                with contextlib.suppress(Exception):
                    emergency_open(cfg, reason)
            _atomic_json(cfg.heartbeat_file, {"at": time.time(), "healthy": False, "reason": reason})
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
                heartbeat_age = time.time() - float(json.loads(cfg.heartbeat_file.read_text())["at"])
            except Exception:
                heartbeat_age = None
        ports = (*cfg.pg_local_ports, cfg.hindsight_port, cfg.budget_port, cfg.fugu_port, *cfg.patroni_ports)
        failure = watchdog_failure(
            armed=bool(phase.get("watchdog_armed")),
            heartbeat_age=heartbeat_age,
            tunnel_ports_ready=all(_port_ready(port) for port in ports),
            guard_active=_service_active("hindsight-recovery-guard.service"),
            tunnel_active=_service_active("hindsight-recovery-tunnel.service"),
            max_age=cfg.heartbeat_max_age,
        )
        if failure:
            with contextlib.suppress(Exception):
                emergency_open(cfg, f"watchdog failure: {failure}")
            event(cfg, "watchdog_failure", reason=failure)
        _atomic_json(cfg.watchdog_file, {"at": time.time(), "healthy": failure is None, "reason": failure, "armed": bool(phase.get("watchdog_armed"))})
        time.sleep(cfg.watchdog_seconds)


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
        "failures": snap.get("failures", []),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    audit = sub.add_parser("audit")
    audit.add_argument("--full", action="store_true")
    opened = sub.add_parser("emergency-open")
    opened.add_argument("--reason", required=True)
    phase_cmd = sub.add_parser("phase")
    phase_cmd.add_argument("--name")
    phase_cmd.add_argument("--budget", choices=("open", "closed"))
    phase_cmd.add_argument("--expected-retry", type=int)
    phase_cmd.add_argument("--expected-succeeded", type=int)
    phase_cmd.add_argument("--arm", action="store_true")
    phase_cmd.add_argument("--disarm", action="store_true")
    sub.add_parser("refresh-retry")
    sub.add_parser("recover-provider-failures")
    sub.add_parser("prepare-resume")
    sub.add_parser("release")
    sub.add_parser("settle-cap")
    sub.add_parser("guard")
    sub.add_parser("watchdog")
    args = parser.parse_args()
    cfg = Config.from_env()
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    if args.command == "emergency-open":
        emergency_open(cfg, args.reason)
        print(json.dumps({"agent_circuit_open": True}))
        return
    if args.command == "phase":
        phase = load_phase(cfg)
        for key in ("name", "budget", "expected_retry", "expected_succeeded"):
            value = getattr(args, key)
            if value is not None:
                phase[key] = value
        if args.arm and args.disarm:
            raise SystemExit("--arm and --disarm are mutually exclusive")
        if args.arm:
            phase["watchdog_armed"] = True
        if args.disarm:
            phase["watchdog_armed"] = False
        save_phase(cfg, phase)
        print(json.dumps(phase, sort_keys=True))
        return
    if args.command == "guard":
        run_guard(cfg)
        return
    if args.command == "watchdog":
        run_watchdog(cfg)
        return
    recovery = Recovery(cfg)
    if args.command == "audit":
        phase = load_phase(cfg)
        snap = recovery.full_audit(phase) if args.full else recovery.quick_snapshot()
        print(json.dumps(_summary(snap), default=str, sort_keys=True))
        if snap.get("failures"):
            raise SystemExit(2)
    elif args.command == "refresh-retry":
        print(json.dumps(_summary(recovery.refresh_retry()), default=str, sort_keys=True))
    elif args.command == "recover-provider-failures":
        print(json.dumps(_summary(recovery.recover_provider_auth_failures()), default=str, sort_keys=True))
    elif args.command == "prepare-resume":
        print(json.dumps(_summary(recovery.prepare_resume()), default=str, sort_keys=True))
    elif args.command == "release":
        print(json.dumps(_summary(recovery.release()), default=str, sort_keys=True))
    elif args.command == "settle-cap":
        result = recovery.settle_cap()
        print(json.dumps(_summary(result or {}), default=str, sort_keys=True))


if __name__ == "__main__":
    main()
