from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path
import ast
from contextlib import nullcontext
from dataclasses import dataclass, replace
import importlib.util
import json
import math
import subprocess
import sys
from types import SimpleNamespace


MODULE = Path(__file__).resolve().parents[1] / "recovery.py"
SPEC = importlib.util.spec_from_file_location("hindsight_recovery", MODULE)
assert SPEC and SPEC.loader
recovery = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = recovery
SPEC.loader.exec_module(recovery)


@dataclass(frozen=True)
class _LineageProof:
    document_id: str
    parent_operation_id: str
    child_operation_id: str
    payload_hash: str


class _ScriptedCursor:
    def __init__(self, result_sets):
        self._result_sets = iter(result_sets)
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, _query, _params=None):
        self._rows = list(next(self._result_sets))

    def fetchall(self):
        return list(self._rows)


class _ScriptedConnection:
    def __init__(self, result_sets):
        self._cursor = _ScriptedCursor(result_sets)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self._cursor


def config():
    return recovery.Config(
        root=Path("/tmp/repo"),
        state_dir=Path("/tmp/state"),
        ssh_target="mesh",
        pg_local_ports=(1, 2, 3),
        pg_remote_hosts=("a", "b", "c"),
        hindsight_port=4,
        budget_port=5,
        fugu_port=6,
        patroni_ports=(7, 8, 9),
        approved_total=5142,
        stage_cap=5142,
        max_agent_active=2,
        max_provider_in_flight=6,
        conservative_remaining_cost=Decimal("0.00543"),
        hard_budget=Decimal("30"),
        quick_seconds=2,
        full_seconds=60,
        watchdog_seconds=5,
        heartbeat_max_age=7,
        controller_seconds=5,
        controller_initial_backoff=5,
        controller_max_backoff=300,
        controller_stable_checks=10,
        controller_full_audits=2,
        budget_maintenance_hold_url="http://127.0.0.1:5/admin/maintenance-hold",
        agent_runtime_proof_file=Path("/tmp/state/agent-runtime-proof.json"),
        agent_runtime_proof_max_age=120,
        agent_progress_timeout=180,
        expected_agent_image_digest="sha256:test",
        expected_agent_compose_hash="abc123",
        expected_hindsight_model="openai/gpt-oss-120b",
        expected_hindsight_phase="backfill",
        expected_hindsight_llm_concurrency=3,
        expected_provider_effective_limit=Decimal("49.5"),
        agent_runtime_verifier_command="",
        expected_agent_outbox_build_id="outbox.py:sha256:test",
    )


def test_projection_matches_authoritative_hold():
    assert recovery.compute_projection("16.267805999999982", 3263, config()) == Decimal(
        "26.470775999999982"
    )


def test_every_budget_projection_gate_is_strictly_below_thirty():
    source = MODULE.read_text()
    assert "projection > self.cfg.hard_budget" not in source
    assert "maximum_projection > self.cfg.hard_budget" not in source
    projection_gates = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Name)
        and node.left.id in {"projection", "maximum_projection"}
        and any(
            isinstance(value, ast.Attribute) and value.attr == "hard_budget"
            for value in node.comparators
        )
    ]
    assert len(projection_gates) >= 6
    assert all(
        len(node.ops) == 1 and isinstance(node.ops[0], ast.GtE)
        for node in projection_gates
    )
    assert (
        recovery.controller_action(
            phase_name="ready",
            failures=["projected full batch 30 exceeds hard budget"],
            agent_open=True,
            attempted=5142,
            cap=5142,
            active=0,
            errors=0,
            hindsight_active=0,
            provider_in_flight=0,
        )
        == "hard_hold"
    )


def _final_hold_quick_proof():
    cfg = config()
    instance = object.__new__(recovery.Recovery)
    instance.cfg = cfg
    instance.touch_controller = lambda *_args, **_kwargs: None
    instance.agent_runtime_proof_failures = lambda _proof: []
    phase = {
        "name": "finalizing",
        "finalization_stage": "hold_started",
        "budget": "closed",
    }
    checkpoint = {
        "accepted": {
            "hindsight_failed": 60,
            "pocket_counts": {"succeeded": 62},
        },
        "stage": {
            "cap": 5142,
            "max_agent_active": 2,
            "max_provider_in_flight": 6,
        },
    }
    budget = {
        "status": "circuit_open",
        "circuit_open": True,
        "circuit_reason": recovery.FINAL_BUDGET_HOLD_REASON,
        "in_flight": 0,
        "final_maintenance_hold": {
            "reason": recovery.FINAL_BUDGET_HOLD_REASON,
            "entered_at": "2026-07-15T00:00:00Z",
        },
        "provider_egress_enabled": True,
        "hindsight_phase": "backfill",
        "hindsight_model": "openai/gpt-oss-120b",
        "provider_effective_limit_usd": "49.5",
        "hindsight_backfill_limit_usd": "30",
        "hindsight_llm_max_concurrent": 3,
        "provider_max_in_flight": 6,
        "hindsight_backfill_spent_usd": "27",
    }
    snap = {
        "agent": {
            "counts": {"succeeded": 5142},
            "circuit": {"circuit_open": True},
        },
        "pocket": {
            "counts": {"succeeded": 62},
            "circuit": {"circuit_open": True},
        },
        "runtime": {
            "HINDSIGHT_OUTBOX_MAX_IN_FLIGHT": "2",
            "HINDSIGHT_OUTBOX_RUN_LIMIT": "5142",
        },
        "agent_runtime_proof": {},
        "hindsight_operations": {"completed": 1, "failed": 60},
        "budget": budget,
        "budget_recovery": {"reservations": [], "total_max_usd": "0"},
        "hindsight_health": {"status": "healthy", "database": "connected"},
        "fugu_health": {"status": "router-alive"},
        "patroni": [
            {"state": "running", "role": "primary", "timeline": 7},
            {"state": "running", "role": "replica", "timeline": 7},
            {"state": "running", "role": "replica", "timeline": 7},
        ],
    }
    return instance, phase, checkpoint, snap


def test_exact_final_hold_post_crash_reenters_idempotent_finalization():
    instance, phase, checkpoint, snap = _final_hold_quick_proof()
    failures = instance.quick_failures(snap, phase, checkpoint)
    assert failures == []
    assert (
        recovery.controller_action(
            phase_name=phase["name"],
            failures=failures,
            agent_open=True,
            attempted=5142,
            cap=5142,
            active=0,
            errors=0,
            hindsight_active=0,
            provider_in_flight=0,
        )
        == "finalize"
    )


def test_final_hold_resume_rejects_every_non_exact_or_wrong_substage_hold():
    instance, phase, checkpoint, snap = _final_hold_quick_proof()
    cases = []
    wrong_stage = json.loads(json.dumps(phase))
    wrong_stage["finalization_stage"] = "seal_started"
    cases.append((wrong_stage, json.loads(json.dumps(snap))))
    wrong_reason = json.loads(json.dumps(snap))
    wrong_reason["budget"]["circuit_reason"] = "some other hold"
    cases.append((json.loads(json.dumps(phase)), wrong_reason))
    wrong_record = json.loads(json.dumps(snap))
    wrong_record["budget"]["final_maintenance_hold"]["reason"] = "other"
    cases.append((json.loads(json.dumps(phase)), wrong_record))
    missing_time = json.loads(json.dumps(snap))
    missing_time["budget"]["final_maintenance_hold"]["entered_at"] = None
    cases.append((json.loads(json.dumps(phase)), missing_time))
    wrong_status = json.loads(json.dumps(snap))
    wrong_status["budget"]["status"] = "healthy"
    cases.append((json.loads(json.dumps(phase)), wrong_status))
    non_boolean_circuit = json.loads(json.dumps(snap))
    non_boolean_circuit["budget"]["circuit_open"] = 1
    cases.append((json.loads(json.dumps(phase)), non_boolean_circuit))
    string_in_flight = json.loads(json.dumps(snap))
    string_in_flight["budget"]["in_flight"] = "0"
    cases.append((json.loads(json.dumps(phase)), string_in_flight))
    naive_time = json.loads(json.dumps(snap))
    naive_time["budget"]["final_maintenance_hold"]["entered_at"] = "2026-07-15T00:00:00"
    cases.append((json.loads(json.dumps(phase)), naive_time))
    not_drained = json.loads(json.dumps(snap))
    not_drained["budget"]["in_flight"] = 1
    not_drained["budget_recovery"]["reservations"] = [{"request_id": "live"}]
    cases.append((json.loads(json.dumps(phase)), not_drained))
    for candidate_phase, candidate_snap in cases:
        failures = instance.quick_failures(candidate_snap, candidate_phase, checkpoint)
        assert "budget circuit is not healthy/closed" in failures
        assert (
            recovery.controller_action(
                phase_name=candidate_phase["name"],
                failures=failures,
                agent_open=True,
                attempted=5142,
                cap=5142,
                active=0,
                errors=0,
                hindsight_active=0,
                provider_in_flight=0,
            )
            == "retry_audit"
        )


def test_exact_final_hold_resume_does_not_mask_unrelated_failures(monkeypatch):
    instance, phase, checkpoint, snap = _final_hold_quick_proof()
    router = json.loads(json.dumps(snap))
    router["fugu_health"]["status"] = "down"
    projection = json.loads(json.dumps(snap))
    projection["budget"]["hindsight_backfill_spent_usd"] = "30"
    provenance = json.loads(json.dumps(snap))
    provenance["hindsight_operations"]["failed"] = 61
    for candidate, failure, action in (
        (router, "Fugu router health degraded", "retry_audit"),
        (projection, "projected full batch", "hard_hold"),
        (provenance, "Hindsight failed-operation count drift", "hard_hold"),
    ):
        failures = instance.quick_failures(candidate, phase, checkpoint)
        assert any(failure in value for value in failures)
        assert (
            recovery.controller_action(
                phase_name=phase["name"],
                failures=failures,
                agent_open=True,
                attempted=5142,
                cap=5142,
                active=0,
                errors=0,
                hindsight_active=0,
                provider_in_flight=0,
            )
            == action
        )

    phase["controller_required"] = True
    monkeypatch.setattr(
        recovery,
        "heartbeat_failures",
        lambda *_args, **_kwargs: ["controller heartbeat missing or stale"],
    )
    monkeypatch.setattr(
        recovery,
        "_service_evidence",
        lambda service: {
            "Id": service,
            "ActiveState": "active",
            "SubState": "running",
            "MainPID": 1,
            "NRestarts": 0,
        },
    )
    monkeypatch.setattr(recovery, "_port_ready", lambda _port: True)
    failures = instance.quick_failures(snap, phase, checkpoint)
    assert "controller heartbeat missing or stale" in failures
    assert (
        recovery.controller_action(
            phase_name=phase["name"],
            failures=failures,
            agent_open=True,
            attempted=5142,
            cap=5142,
            active=0,
            errors=0,
            hindsight_active=0,
            provider_in_flight=0,
        )
        == "retry_audit"
    )


def test_finalize_resumes_without_reposting_after_exact_hold(monkeypatch):
    cfg = config()
    phase = {
        "revision": 9,
        "name": "finalizing",
        "finalization_stage": "hold_started",
        "budget": "closed",
        "final_spend": "27",
    }
    saved = []
    events = []
    monkeypatch.setattr(recovery, "load_phase", lambda _cfg: phase)
    monkeypatch.setattr(
        recovery, "save_phase", lambda _cfg, value: saved.append(dict(value))
    )
    monkeypatch.setattr(
        recovery,
        "event",
        lambda _cfg, kind, **fields: events.append((kind, fields)),
    )
    instance = object.__new__(recovery.Recovery)
    instance.cfg = cfg
    instance.persist_agent_open = lambda _reason: None
    instance.ensure_pocket_open = lambda _reason: None
    marker = {
        "RECOVERY_CONTROL_NONCE": "a" * 32,
        "RECOVERY_CONTROL_ROLL_SHA256": "b" * 64,
    }
    instance._hindsight_deployment_marker = lambda: marker
    instance._hindsight_recovery_roll = lambda **_kwargs: (_ for _ in ()).throw(
        AssertionError("hold_started resume must not roll again")
    )
    proved = []
    instance.prove_recovery_controls = lambda **kwargs: proved.append(kwargs)
    instance.budget_final_hold_is_exact = lambda: True
    instance.request_budget_final_hold = lambda: (_ for _ in ()).throw(
        AssertionError("exact final hold must not be POSTed twice")
    )
    audited = []

    def full_audit(audit_phase):
        audited.append(dict(audit_phase))
        return {
            "failures": [],
            "budget": {"hindsight_backfill_spent_usd": "27"},
        }

    instance.full_audit = full_audit
    result = instance.finalize()
    assert result["failures"] == []
    assert proved == [
        {
            "nonce": "a" * 32,
            "roll_sha256": "b" * 64,
            "reconcile_enabled": False,
            "auth_reset_enabled": False,
        }
    ]
    assert audited[0]["budget"] == "open"
    assert audited[0]["budget_reason"] == recovery.FINAL_BUDGET_HOLD_REASON
    assert phase["name"] == "finalized"
    assert phase["expected_succeeded"] == 5142
    assert saved[-1]["name"] == "finalized"
    assert events[-1] == (
        "backfill_finalized",
        {"succeeded": 5142, "spend": "27"},
    )


def test_finalize_retries_a_crash_after_final_audit_without_reposting(monkeypatch):
    cfg = config()
    persisted = {
        "revision": 9,
        "name": "finalizing",
        "finalization_stage": "hold_started",
        "budget": "closed",
        "final_spend": "27",
    }
    monkeypatch.setattr(
        recovery,
        "load_phase",
        lambda _cfg: json.loads(json.dumps(persisted)),
    )
    save_attempts = []

    def save_phase(_cfg, value):
        save_attempts.append(dict(value))
        if len(save_attempts) == 1:
            raise RuntimeError("simulated crash before final phase commit")
        persisted.clear()
        persisted.update(value)

    monkeypatch.setattr(recovery, "save_phase", save_phase)
    monkeypatch.setattr(recovery, "event", lambda *_args, **_kwargs: None)
    instance = object.__new__(recovery.Recovery)
    instance.cfg = cfg
    instance.persist_agent_open = lambda _reason: None
    instance.ensure_pocket_open = lambda _reason: None
    instance._hindsight_deployment_marker = lambda: {
        "RECOVERY_CONTROL_NONCE": "a" * 32,
        "RECOVERY_CONTROL_ROLL_SHA256": "b" * 64,
    }
    instance.prove_recovery_controls = lambda **_kwargs: None
    instance.budget_final_hold_is_exact = lambda: True
    posts = []
    instance.request_budget_final_hold = lambda: posts.append(1)
    audits = []
    instance.full_audit = lambda audit_phase: (
        audits.append(dict(audit_phase))
        or {
            "failures": [],
            "budget": {"hindsight_backfill_spent_usd": "27"},
        }
    )
    try:
        instance.finalize()
    except RuntimeError as exc:
        assert "simulated crash" in str(exc)
    else:
        raise AssertionError("simulated final phase crash did not interrupt commit")
    assert persisted["name"] == "finalizing"
    assert posts == []
    result = instance.finalize()
    assert result["failures"] == []
    assert persisted["name"] == "finalized"
    assert posts == []
    assert len(audits) == 2


def test_stage_cap_opens_only_at_exact_cap():
    assert recovery.stage_action(3822, "running", 3823) == "continue"
    assert recovery.stage_action(3823, "running", 3823) == "open"
    assert recovery.stage_action(3823, "ready", 3823) == "continue"
    assert recovery.stage_action(3824, "running", 3823) == "violation"


def test_precap_resume_accepts_one_completed_remote_submission():
    assert (
        recovery.precap_resume_failure(
            phase_name="running",
            agent_open=True,
            attempted=3405,
            succeeded=3404,
            submitting=0,
            submitted=1,
            errors=0,
            hindsight_active=0,
            provider_in_flight=0,
            remote_documents=3405,
            max_agent_active=2,
            cap=3823,
        )
        is None
    )


def test_precap_resume_accepts_two_completed_remote_submissions():
    assert (
        recovery.precap_resume_failure(
            phase_name="running",
            agent_open=True,
            attempted=3406,
            succeeded=3404,
            submitting=0,
            submitted=2,
            errors=0,
            hindsight_active=0,
            provider_in_flight=0,
            remote_documents=3406,
            max_agent_active=2,
            cap=3823,
        )
        is None
    )


def test_precap_resume_rejects_cap_and_ambiguous_activity():
    common = {
        "phase_name": "running",
        "agent_open": True,
        "succeeded": 3404,
        "submitting": 0,
        "submitted": 1,
        "errors": 0,
        "hindsight_active": 0,
        "provider_in_flight": 0,
        "remote_documents": 3405,
        "max_agent_active": 2,
        "cap": 3823,
    }
    assert recovery.precap_resume_failure(attempted=3823, **common) == (
        "pre-cap resume cannot run at or above stage cap"
    )
    assert (
        recovery.precap_resume_failure(
            attempted=3405,
            **{**common, "provider_in_flight": 1},
        )
        == "pre-cap resume requires zero provider in-flight"
    )


def test_watchdog_stale_heartbeat_fails_closed():
    assert (
        recovery.watchdog_failure(
            armed=True,
            heartbeat_age=8,
            tunnel_ports_ready=True,
            guard_active=True,
            tunnel_active=True,
            max_age=7,
        )
        == "guard heartbeat missing or stale"
    )


def test_watchdog_unarmed_allows_ordered_startup():
    assert (
        recovery.watchdog_failure(
            armed=False,
            heartbeat_age=None,
            tunnel_ports_ready=False,
            guard_active=False,
            tunnel_active=False,
            max_age=7,
        )
        is None
    )


def test_default_watchdog_age_allows_a_normal_full_audit(monkeypatch):
    monkeypatch.delenv("HEARTBEAT_MAX_AGE_SECONDS", raising=False)
    assert recovery.Config.from_env().heartbeat_max_age == 60


def test_permanent_concurrency_is_parameterized_at_three():
    root = Path(__file__).resolve().parents[2]
    driver = (root / "hindsight-node.sh").read_text()
    compose = (root / "compose/hindsight-node.yaml").read_text()
    box = (root / "hindsight-node-box.py").read_text()
    assert 'HINDSIGHT_LLM_MAX_CONCURRENT="${HINDSIGHT_LLM_MAX_CONCURRENT:-3}"' in driver
    assert (
        "HINDSIGHT_API_LLM_MAX_CONCURRENT: ${HINDSIGHT_API_LLM_MAX_CONCURRENT}"
        in compose
    )
    assert "HINDSIGHT_LLM_MAX_CONCURRENT=${HINDSIGHT_API_LLM_MAX_CONCURRENT}" in compose
    assert "HINDSIGHT_LLM_MAX_CONCURRENT=${HINDSIGHT_LLM_MAX_CONCURRENT}" not in compose
    assert '"HINDSIGHT_API_LLM_MAX_CONCURRENT"' in box


def test_provider_guard_default_is_six(monkeypatch):
    monkeypatch.delenv("MAX_PROVIDER_IN_FLIGHT", raising=False)
    assert recovery.Config.from_env().max_provider_in_flight == 6


def test_legacy_held_config_is_only_available_to_explicit_read_gate(monkeypatch):
    monkeypatch.setenv("APPROVED_TOTAL", "5142")
    monkeypatch.setenv("STAGE_CAP", "3823")
    monkeypatch.setenv("MAX_AGENT_ACTIVE", "2")
    monkeypatch.setenv("MAX_PROVIDER_IN_FLIGHT", "6")
    monkeypatch.setenv("HARD_BUDGET_USD", "30")
    monkeypatch.setenv("EXPECTED_HINDSIGHT_LLM_CONCURRENCY", "3")
    try:
        recovery.Config.from_env()
    except ValueError as exc:
        assert "recovery authority" in str(exc)
    else:
        raise AssertionError("normal controller configuration accepted old cap")
    cfg = recovery.Config.from_env(allow_legacy_held_stage=True)
    assert cfg.stage_cap == 3823
    assert cfg.allow_legacy_held_stage is True


def test_readme_exports_assignment_only_recovery_environment():
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    source = "source ~/.config/hindsight-recovery/recovery.env"
    assert f"set -a\n{source}\nset +a" in readme
    assert "audit --full --legacy-held" in readme


def test_agent_worker_default_is_two(monkeypatch):
    monkeypatch.delenv("MAX_AGENT_ACTIVE", raising=False)
    assert recovery.Config.from_env().max_agent_active == 2
    root = Path(__file__).resolve().parents[2]
    driver = (root / "agent-session-mcp-node.sh").read_text()
    assert (
        'HINDSIGHT_OUTBOX_MAX_IN_FLIGHT="${HINDSIGHT_OUTBOX_MAX_IN_FLIGHT:-2}"'
        in driver
    )
    assert (
        'E_HINDSIGHT_OUTBOX_MAX_IN_FLIGHT=%q\\n\' "${HINDSIGHT_OUTBOX_MAX_IN_FLIGHT:-2}"'
        in driver
    )


def test_explicit_recovery_controls_override_sealed_production_defaults():
    root = Path(__file__).resolve().parents[2]
    driver = (root / "hindsight-node.sh").read_text()
    capture = driver.index(
        '_CALLER_RECONCILE_SET="${HINDSIGHT_RECONCILE_AMBIGUOUS_ENABLED+x}"'
    )
    production_import = driver.index('source "$PRODUCTION_ENV_FILE"')
    restore = driver.index(
        'HINDSIGHT_RECONCILE_AMBIGUOUS_ENABLED="$_CALLER_RECONCILE_VALUE"'
    )
    assert capture < production_import < restore


def test_controller_adopts_exact_pending_recovery_control_identity(monkeypatch):
    instance = object.__new__(recovery.Recovery)
    instance.cfg = config()
    nonce = "a" * 32
    roll_sha256 = "b" * 64
    compose_sha256 = "c" * 64
    vm_id = "11111111-2222-3333-4444-555555555555"
    pending = {
        "H": "old-compose",
        "VM_ID": "00000000-0000-0000-0000-000000000000",
        "RECOVERY_CONTROL_PENDING_NONCE": nonce,
        "RECOVERY_CONTROL_PENDING_ROLL_SHA256": roll_sha256,
        "RECOVERY_CONTROL_PENDING_COMPOSE_HASH": compose_sha256,
        "RECOVERY_CONTROL_PENDING_VM_ID": vm_id,
    }
    promoted = {
        "H": compose_sha256,
        "VM_ID": vm_id,
        "RECOVERY_CONTROL_NONCE": nonce,
        "RECOVERY_CONTROL_ROLL_SHA256": roll_sha256,
        "RECOVERY_CONTROL_COMPOSE_HASH": compose_sha256,
        "RECOVERY_CONTROL_VM_ID": vm_id,
        "RECOVERY_CONTROL_PENDING_NONCE": "",
        "RECOVERY_CONTROL_PENDING_ROLL_SHA256": "",
        "RECOVERY_CONTROL_PENDING_COMPOSE_HASH": "",
        "RECOVERY_CONTROL_PENDING_VM_ID": "",
    }
    instance.recovery_control_status = lambda: {
        "expected_nonce_sha256": recovery.hashlib.sha256(nonce.encode()).hexdigest()
    }
    proven = []
    instance.prove_recovery_controls = lambda **values: proven.append(values)
    instance._hindsight_deployment_marker = lambda: promoted
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        return type("Result", (), {"returncode": 0, "stdout": "adopted"})()

    monkeypatch.setattr(recovery.subprocess, "run", run)
    assert (
        instance._adopt_deployed_pending_recovery_control(
            pending,
            expected_roll_sha256=roll_sha256,
            reconcile_enabled=False,
            auth_reset_enabled=True,
        )
        == promoted
    )
    assert commands == [
        [
            "bash",
            "deploy/hindsight-node.sh",
            "hindsight-node",
            "adopt-recovery-control",
            nonce,
            roll_sha256,
            compose_sha256,
            vm_id,
        ]
    ]
    assert proven == [
        {
            "nonce": nonce,
            "roll_sha256": roll_sha256,
            "reconcile_enabled": False,
            "auth_reset_enabled": True,
        }
    ]


def test_controller_refuses_unbound_deployed_pending_control():
    instance = object.__new__(recovery.Recovery)
    instance.cfg = config()
    nonce = "a" * 32
    roll_sha256 = "b" * 64
    instance.recovery_control_status = lambda: {
        "expected_nonce_sha256": recovery.hashlib.sha256(nonce.encode()).hexdigest()
    }
    try:
        instance._adopt_deployed_pending_recovery_control(
            {
                "RECOVERY_CONTROL_PENDING_NONCE": nonce,
                "RECOVERY_CONTROL_PENDING_ROLL_SHA256": roll_sha256,
                "RECOVERY_CONTROL_PENDING_COMPOSE_HASH": "",
                "RECOVERY_CONTROL_PENDING_VM_ID": "",
            },
            expected_roll_sha256=roll_sha256,
            reconcile_enabled=False,
            auth_reset_enabled=False,
        )
    except recovery.InvariantError as exc:
        assert "compose/VM identity" in str(exc)
    else:
        raise AssertionError("deployed pending control without identity was adopted")


def test_deployment_marker_rejects_partial_or_unversioned_control_state(tmp_path):
    cfg = config()
    object.__setattr__(cfg, "root", tmp_path)
    state_path = tmp_path / "deploy/logs/hindsight-node-hindsight-node.state"
    state_path.parent.mkdir(parents=True)
    instance = object.__new__(recovery.Recovery)
    instance.cfg = cfg

    def write(**values):
        defaults = {
            "H": "c" * 64,
            "VM_ID": "11111111-2222-3333-4444-555555555555",
        }
        defaults.update(values)
        state_path.write_text(
            "".join(f"{key}={value}\n" for key, value in defaults.items())
        )

    write()
    assert instance._hindsight_deployment_marker()["H"] == "c" * 64
    for values in (
        {"RECOVERY_CONTROL_NONCE": "a" * 32},
        {
            "RECOVERY_CONTROL_STATE_VERSION": "2",
            "RECOVERY_CONTROL_NONCE": "a" * 32,
        },
        {
            "RECOVERY_CONTROL_STATE_VERSION": "2",
            "RECOVERY_CONTROL_PENDING_NONCE": "a" * 32,
        },
    ):
        write(**values)
        try:
            instance._hindsight_deployment_marker()
        except recovery.InvariantError:
            pass
        else:
            raise AssertionError("partial recovery-control marker was accepted")


def _probe_status(root_id, root_status, root_at, *, calls=None, failures=None):
    failure_values = [
        {
            "request_id": root_id,
            "status_code": root_status,
            "at": root_at,
            "recovery_probe_route": None,
        },
        *(failures or []),
    ]
    return {
        "reservations": [],
        "provider_failure_history": failure_values,
        "provider_failure_manifest": [
            {"request_id": value["request_id"]} for value in failures or []
        ],
        "provider_call_history": list(calls or []),
        "provider_auth_circuit_incident_request_id": root_id,
        "provider_auth_circuit_incident_status": root_status,
        "provider_auth_recovery": {
            "version": 1,
            "auth_incident_request_id": root_id,
            "auth_incident_status": root_status,
            "auth_incident_at": root_at,
        },
    }


def _probe_success_call(route, provenance, root_id, gate_sha256):
    return {
        "request_id": str(
            recovery.uuid.uuid5(recovery.RECOVERY_PROBE_NAMESPACE, provenance)
        ),
        "model": (
            config().expected_hindsight_model
            if route == "gpt_oss"
            else recovery.QWEN_MODEL
        ),
        "endpoint": "chat" if route == "gpt_oss" else "embeddings",
        "provider_call_recorded": True,
        "ambiguous_upstream_reconciled": False,
        "request_provenance_sha256": recovery.hashlib.sha256(
            provenance.encode()
        ).hexdigest(),
        "recovery_probe_route": route,
        "recovery_auth_incident_request_id": root_id,
        "recovery_gate_sha256": gate_sha256,
        "cost_usd": 0.000001,
    }


def _legacy_budget_call(request_id, *, reconciled=False):
    value = {
        "at": "2026-07-13T10:00:01+00:00",
        "started_at": "2026-07-13T10:00:00+00:00",
        "request_id": request_id,
        "endpoint": "chat",
        "model": config().expected_hindsight_model,
        "scope": "hindsight_backfill",
        "period": "all",
        "input_tokens": 80,
        "output_tokens": 120,
        "cost_usd": 0.000084,
        "provider_reported_cost_usd": None,
        "estimated_from_reservation": bool(reconciled),
    }
    if reconciled:
        value["ambiguous_upstream_reconciled"] = True
    return value


def _budget_admin_status(calls, *, version=1):
    return {
        "health": {"in_flight": 0},
        "reservations": [],
        "sha256": recovery.hashlib.sha256(b"[]").hexdigest(),
        "total_max_usd": 0,
        "provider_failure_history": [],
        "provider_call_history": list(calls),
        "provider_ledger_version": version,
    }


def _budget_status_with_reservations(reservations, total):
    canonical = sorted(
        (dict(value) for value in reservations),
        key=lambda value: str(value["request_id"]),
    )
    encoded = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    status = _budget_admin_status([])
    status.update(
        {
            "health": {"in_flight": len(canonical)},
            "reservations": canonical,
            "sha256": recovery.hashlib.sha256(encoded).hexdigest(),
            "total_max_usd": total,
        }
    )
    return status


def test_budget_reservation_total_reproduces_proxy_float_sum_exactly():
    reservations = [
        {"request_id": f"request-{index}", "cost_usd": 0.005}
        for index in range(6)
    ]
    proxy_total = 0.0
    for value in reservations:
        proxy_total += value["cost_usd"]
    compensated_total = math.fsum(
        value["cost_usd"] for value in reservations
    )
    assert proxy_total == 0.030000000000000002
    assert compensated_total == 0.03
    instance = object.__new__(recovery.Recovery)
    instance.cfg = config()
    instance.hindsight_state = {"BAT": "admin"}
    status = _budget_status_with_reservations(reservations, proxy_total)
    instance._http_json = lambda *_args, **_kwargs: status

    accepted = instance.budget_recovery_status()
    assert accepted["total_max_usd"] == proxy_total

    status["total_max_usd"] = compensated_total
    try:
        instance.budget_recovery_status()
    except recovery.InvariantError as exc:
        assert "maximum-cost total drift" in str(exc)
    else:
        raise AssertionError("tampered reservation float total was accepted")


def test_budget_reservation_total_matches_live_cross_runtime_incident():
    costs = [0.01245255, 0.007404, 0.00889845]
    proxy_total = 0.0
    for cost in costs:
        proxy_total = float(proxy_total + cost)

    assert proxy_total == 0.028755000000000003
    assert proxy_total.hex() == "0x1.d71f36262cba8p-6"
    assert math.fsum(costs) == 0.028755
    assert math.fsum(costs).hex() == "0x1.d71f36262cba7p-6"
    assert recovery.canonical_reservation_total(
        {"cost_usd": cost} for cost in costs
    ) == proxy_total


def test_budget_admin_snapshot_requires_exact_coherent_health_count():
    instance = object.__new__(recovery.Recovery)
    instance.cfg = config()
    instance.hindsight_state = {"BAT": "admin"}

    malformed = []
    missing = _budget_admin_status([])
    missing.pop("health")
    malformed.append((missing, "health snapshot is absent"))
    for value in ([], {"in_flight": True}, {"in_flight": "0"}, {"in_flight": -1}):
        status = _budget_admin_status([])
        status["health"] = value
        expected = (
            "health snapshot is absent"
            if not isinstance(value, dict)
            else "in-flight count is malformed"
        )
        malformed.append((status, expected))
    drifted = _budget_status_with_reservations(
        [{"request_id": "one", "cost_usd": 0.1}], 0.1
    )
    drifted["health"]["in_flight"] = 0
    malformed.append((drifted, "in-flight count drift"))

    for status, expected in malformed:
        instance._http_json = lambda *_args, value=status, **_kwargs: value
        try:
            instance.budget_recovery_status()
        except recovery.InvariantError as exc:
            assert expected in str(exc)
        else:
            raise AssertionError(f"malformed coherent budget snapshot passed: {status}")


def test_quick_snapshot_consumes_one_lock_coherent_budget_response(monkeypatch):
    instance = object.__new__(recovery.Recovery)
    instance.cfg = config()
    instance.hindsight_state = {"BAT": "admin"}
    budget_status = _budget_admin_status([])
    budget_status["health"] = {
        "status": "healthy",
        "circuit_open": False,
        "in_flight": 0,
    }
    requested_urls: list[str] = []

    class Cursor:
        def __init__(self, database):
            self.database = database
            self.query = ""

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, _params=None):
            self.query = query

        def fetchone(self):
            if "hindsight_sync_state" in self.query:
                return {"circuit_open": True, "reason": "hold"}
            if "FROM documents" in self.query:
                return {"n": 3579}
            raise AssertionError(f"unexpected fetchone query: {self.query}")

        def fetchall(self):
            if "FROM async_operations" in self.query:
                return [{"status": "completed", "n": 3579}]
            raise AssertionError(f"unexpected fetchall query: {self.query}")

    class Connection:
        def __init__(self, database):
            self.database = database

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return Cursor(self.database)

    instance.agent_db = lambda: Connection("agent")
    instance.pocket_db = lambda: Connection("pocket")
    instance.hindsight_db = lambda: Connection("hindsight")
    instance._counts = lambda cur: (
        {"succeeded": 3579, "queued": 1563}
        if cur.database == "agent"
        else {"succeeded": 62}
    )
    instance.agent_runtime_proof = lambda: {"proof": "measured"}
    monkeypatch.setattr(
        recovery,
        "_env_file",
        lambda _path: {
            "HINDSIGHT_OUTBOX_MAX_IN_FLIGHT": "2",
            "HINDSIGHT_OUTBOX_RUN_LIMIT": "5142",
        },
    )

    def http_json(url, *_args, **_kwargs):
        requested_urls.append(url)
        if url.endswith("/admin/reservations"):
            return budget_status
        if url == "http://127.0.0.1:4/health":
            return {"status": "healthy", "database": "connected"}
        if url == "http://127.0.0.1:6/health/process":
            return {"status": "router-alive"}
        if url.startswith("http://127.0.0.1:") and url.endswith("/patroni"):
            return {"state": "running", "role": "replica", "timeline": 1}
        raise AssertionError(f"unexpected HTTP read: {url}")

    instance._http_json = http_json
    snap = instance._quick_snapshot_unlocked()

    assert snap["budget"] == budget_status["health"]
    assert snap["budget_recovery"]["health"] == budget_status["health"]
    assert requested_urls.count("http://127.0.0.1:5/admin/reservations") == 1
    assert "http://127.0.0.1:5/health" not in requested_urls


def test_budget_reservation_costs_must_be_positive_finite_json_numbers():
    for invalid in (
        0,
        -0.1,
        True,
        "0.1",
        float("nan"),
        float("inf"),
        float("-inf"),
    ):
        status = _budget_status_with_reservations(
            [{"request_id": "a", "cost_usd": invalid}],
            1.0,
        )
        instance = object.__new__(recovery.Recovery)
        instance.cfg = config()
        instance.hindsight_state = {"BAT": "admin"}
        instance._http_json = lambda *_args, **_kwargs: status
        try:
            instance.budget_recovery_status()
        except recovery.InvariantError as exc:
            assert "maximum cost is malformed" in str(exc)
        else:
            raise AssertionError(f"invalid reservation cost was accepted: {invalid!r}")


def test_budget_audit_accepts_only_an_initial_exact_v1_legacy_prefix():
    instance = object.__new__(recovery.Recovery)
    instance.cfg = config()
    instance.hindsight_state = {"BAT": "admin"}
    calls = [
        _legacy_budget_call("10000000-0000-4000-8000-000000000001"),
        _legacy_budget_call(
            "10000000-0000-4000-8000-000000000002",
            reconciled=True,
        ),
        _legacy_budget_call("10000000-0000-4000-8000-000000000003"),
    ]
    strict = _legacy_budget_call("10000000-0000-4000-8000-000000000004")
    strict.update(
        {
            "provider_call_recorded": True,
            "reservation_manifest_sha256": "a" * 64,
        }
    )
    calls.append(strict)
    status = _budget_admin_status(calls)
    instance._http_json = lambda *_args, **_kwargs: status

    accepted = instance.budget_recovery_status()
    assert len(accepted["provider_call_manifest"]) == 4

    status["provider_call_history"].append(
        _legacy_budget_call("10000000-0000-4000-8000-000000000005")
    )
    try:
        instance.budget_recovery_status()
    except recovery.InvariantError as exc:
        assert "provenance" in str(exc)
    else:
        raise AssertionError("late legacy provider call was accepted")


def test_budget_audit_rejects_legacy_shape_in_v2_ledger():
    instance = object.__new__(recovery.Recovery)
    instance.cfg = config()
    instance.hindsight_state = {"BAT": "admin"}
    status = _budget_admin_status(
        [_legacy_budget_call("20000000-0000-4000-8000-000000000001")],
        version=recovery.PROVIDER_LEDGER_VERSION,
    )
    instance._http_json = lambda *_args, **_kwargs: status
    try:
        instance.budget_recovery_status()
    except recovery.InvariantError as exc:
        assert "provenance" in str(exc)
    else:
        raise AssertionError("v2 ledger accepted a legacy provider call")


def test_probe_restart_adopts_durable_first_route_success_without_recalling(
    monkeypatch,
):
    instance = object.__new__(recovery.Recovery)
    instance.cfg = config()
    instance.hindsight_state = {"BAT": "admin", "BPT": "proxy"}
    root_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    root_at = "2026-07-14T00:00:00+00:00"
    token = "1" * 24
    provenance = f"hindsight-recovery-{token}-gpt_oss"
    gate = {
        "agent_circuit_open": True,
        "pocket_circuit_open": True,
        "provider_in_flight": 0,
        "projected_full_cost_usd": 20.0,
    }
    payload = {
        "route": "gpt_oss",
        "provenance": provenance,
        "auth_incident_request_id": root_id,
        **gate,
    }
    call = _probe_success_call(
        "gpt_oss", provenance, root_id, recovery._json_digest(payload)
    )
    status = _probe_status(root_id, 401, root_at, calls=[call])
    instance.budget_recovery_status = lambda: status
    instance._recovery_probe_gate = lambda: (_ for _ in ()).throw(
        AssertionError("durably successful probe was called again")
    )
    instance._http_post_json_status = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("durably successful probe was called again")
    )
    state = {
        "kind": "auth",
        "stage": "recovery_probes_started",
        "probe_auth_incident_request_id": root_id,
        "probe_auth_incident_status": 401,
        "probe_auth_incident_at": root_at,
        "probe_history": [],
        "probe_routes": {
            "gpt_oss": {
                "status": "started",
                "token": token,
                "provenance": provenance,
                "request_gate": gate,
                "request_gate_sha256": recovery._json_digest(payload),
            },
            "qwen": {"terminal": True, "result": "authenticated"},
        },
    }
    incident = {"provider_recovery": state}
    monkeypatch.setattr(recovery, "save_incident", lambda *_args, **_kwargs: None)
    assert (
        instance.provider_auth_recovery_probes(
            state, incident, authorized_incident_ids=[root_id]
        )
        is True
    )
    assert (
        state["probe_routes"]["gpt_oss"]["success"]["request_id"] == call["request_id"]
    )


def test_probe_accepts_matched_existing_route_only_from_exact_paid_call(monkeypatch):
    instance = object.__new__(recovery.Recovery)
    instance.cfg = config()
    instance.hindsight_state = {"BAT": "admin", "BPT": "proxy"}
    root_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    root_at = "2026-07-14T00:00:00+00:00"
    old_provenance = "hindsight-recovery-111111111111111111111111-gpt_oss"
    call = _probe_success_call("gpt_oss", old_provenance, root_id, "c" * 64)
    status = _probe_status(root_id, 401, root_at, calls=[call])
    instance.budget_recovery_status = lambda: status
    instance._recovery_probe_gate = lambda: {
        "agent_circuit_open": True,
        "pocket_circuit_open": True,
        "provider_in_flight": 0,
        "projected_full_cost_usd": 20.0,
    }
    instance._http_post_json_status = lambda *_args, **_kwargs: (
        200,
        {},
        {
            "result": "authenticated",
            "route": "gpt_oss",
            "request_id": call["request_id"],
            "provenance_sha256": call["request_provenance_sha256"],
            "upstream_status": 200,
            "cost_usd": call["cost_usd"],
            "reused": True,
            "matched_existing_route": True,
        },
    )
    state = {
        "kind": "auth",
        "stage": "recovery_probes_started",
        "probe_auth_incident_request_id": root_id,
        "probe_auth_incident_status": 401,
        "probe_auth_incident_at": root_at,
        "probe_history": [],
        "probe_routes": {"qwen": {"terminal": True, "result": "authenticated"}},
    }
    incident = {"provider_recovery": state}
    monkeypatch.setattr(recovery.secrets, "token_hex", lambda _n: "2" * 24)
    monkeypatch.setattr(recovery, "save_incident", lambda *_args, **_kwargs: None)
    assert (
        instance.provider_auth_recovery_probes(
            state, incident, authorized_incident_ids=[root_id]
        )
        is True
    )
    success = state["probe_routes"]["gpt_oss"]["success"]
    assert success["request_provenance_sha256"] == call["request_provenance_sha256"]
    assert state["probe_routes"]["gpt_oss"]["matched_existing_route"] is True


def test_definitive_probe_failure_retries_with_fresh_provenance(monkeypatch):
    instance = object.__new__(recovery.Recovery)
    instance.cfg = config()
    instance.hindsight_state = {"BAT": "admin", "BPT": "proxy"}
    root_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    root_at = "2026-07-14T00:00:00+00:00"
    status = _probe_status(root_id, 401, root_at)
    instance.budget_recovery_status = lambda: status
    instance._recovery_probe_gate = lambda: {
        "agent_circuit_open": True,
        "pocket_circuit_open": True,
        "provider_in_flight": 0,
        "projected_full_cost_usd": 20.0,
    }
    provenances = []

    def post(_url, payload, **_kwargs):
        provenance = str(payload["provenance"])
        provenances.append(provenance)
        request_id = str(
            recovery.uuid.uuid5(recovery.RECOVERY_PROBE_NAMESPACE, provenance)
        )
        if len(provenances) == 1:
            failure = {
                "request_id": request_id,
                "status_code": 429,
                "retry_after": "0",
                "recovery_probe_route": "gpt_oss",
                "recovery_auth_incident_request_id": root_id,
                "request_provenance_sha256": recovery.hashlib.sha256(
                    provenance.encode()
                ).hexdigest(),
            }
            status["provider_failure_history"].append(failure)
            status["provider_failure_manifest"].append({"request_id": request_id})
            return (
                200,
                {},
                {
                    "result": "retryable_failure",
                    "route": "gpt_oss",
                    "request_id": request_id,
                    "provenance_sha256": failure["request_provenance_sha256"],
                    "upstream_status": 429,
                    "retry_after": "0",
                    "reused": False,
                },
            )
        call = _probe_success_call(
            "gpt_oss", provenance, root_id, recovery._json_digest(payload)
        )
        status["provider_call_history"].append(call)
        return (
            200,
            {},
            {
                "result": "authenticated",
                "route": "gpt_oss",
                "request_id": call["request_id"],
                "provenance_sha256": call["request_provenance_sha256"],
                "upstream_status": 200,
                "cost_usd": call["cost_usd"],
                "reused": False,
            },
        )

    instance._http_post_json_status = post
    tokens = iter(("1" * 24, "2" * 24))
    monkeypatch.setattr(recovery.secrets, "token_hex", lambda _n: next(tokens))
    monkeypatch.setattr(recovery, "save_incident", lambda *_args, **_kwargs: None)
    state = {
        "kind": "auth",
        "stage": "recovery_probes_started",
        "probe_auth_incident_request_id": root_id,
        "probe_auth_incident_status": 401,
        "probe_auth_incident_at": root_at,
        "probe_history": [],
        "probe_routes": {"qwen": {"terminal": True, "result": "authenticated"}},
    }
    incident = {"provider_recovery": state}
    assert (
        instance.provider_auth_recovery_probes(
            state, incident, authorized_incident_ids=[root_id]
        )
        is False
    )
    state["probe_routes"]["gpt_oss"]["retry_at"] = 0
    assert (
        instance.provider_auth_recovery_probes(
            state, incident, authorized_incident_ids=[root_id]
        )
        is True
    )
    assert len(provenances) == 2
    assert provenances[0] != provenances[1]


def test_ambiguous_probe_stops_before_starting_the_next_route(monkeypatch):
    instance = object.__new__(recovery.Recovery)
    instance.cfg = config()
    instance.hindsight_state = {"BAT": "admin", "BPT": "proxy"}
    root_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    root_at = "2026-07-14T00:00:00+00:00"
    status = _probe_status(root_id, 401, root_at)
    instance.budget_recovery_status = lambda: status
    instance._http_json = lambda _url: {"circuit_reason": "provider returned 401"}
    gate_calls = []

    def gate():
        gate_calls.append(1)
        if len(gate_calls) > 1:
            raise AssertionError("next route read a gate with an active reservation")
        return {
            "agent_circuit_open": True,
            "pocket_circuit_open": True,
            "provider_in_flight": 0,
            "projected_full_cost_usd": 20.0,
        }

    posted_routes = []

    def post(_url, payload, **_kwargs):
        posted_routes.append(payload["route"])
        provenance = str(payload["provenance"])
        return (
            502,
            {},
            {
                "result": "ambiguous_reservation",
                "route": "gpt_oss",
                "request_id": str(
                    recovery.uuid.uuid5(
                        recovery.RECOVERY_PROBE_NAMESPACE, provenance
                    )
                ),
                "provenance_sha256": recovery.hashlib.sha256(
                    provenance.encode()
                ).hexdigest(),
                "upstream_status": None,
                "reused": False,
            },
        )

    def start_ambiguity(state, incident, **kwargs):
        state["probe_ambiguity"] = {
            "stage": "snapshot",
            "route": kwargs["route"],
            "provenance": kwargs["provenance"],
            "request_id": kwargs["response"]["request_id"],
        }
        incident["provider_recovery"] = state

    instance._recovery_probe_gate = gate
    instance._http_post_json_status = post
    instance.start_auth_probe_ambiguity = start_ambiguity
    monkeypatch.setattr(recovery.secrets, "token_hex", lambda _n: "1" * 24)
    monkeypatch.setattr(recovery, "save_incident", lambda *_args, **_kwargs: None)
    state = {"kind": "auth", "stage": "recovery_probes_started"}
    incident = {"provider_recovery": state}

    assert (
        instance.provider_auth_recovery_probes(
            state, incident, authorized_incident_ids=[root_id]
        )
        is False
    )
    assert posted_routes == ["gpt_oss"]
    assert len(gate_calls) == 1
    assert state["probe_ambiguity"]["request_id"]
    assert "qwen" not in state["probe_routes"]


def test_restart_discovered_probe_reservation_persists_ambiguity_before_return(
    monkeypatch,
):
    instance = object.__new__(recovery.Recovery)
    instance.cfg = config()
    instance.hindsight_state = {"BAT": "admin", "BPT": "proxy"}
    root_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    root_at = "2026-07-14T00:00:00+00:00"
    token = "1" * 24
    route = "gpt_oss"
    provenance = f"hindsight-recovery-{token}-{route}"
    request_id = str(
        recovery.uuid.uuid5(recovery.RECOVERY_PROBE_NAMESPACE, provenance)
    )
    provenance_sha256 = recovery.hashlib.sha256(provenance.encode()).hexdigest()
    gate = {
        "agent_circuit_open": True,
        "pocket_circuit_open": True,
        "provider_in_flight": 0,
        "projected_full_cost_usd": 20.0,
    }
    payload = {
        "route": route,
        "provenance": provenance,
        "auth_incident_request_id": root_id,
        **gate,
    }
    gate_sha256 = recovery._json_digest(payload)
    reservation = {
        "request_id": request_id,
        "model": instance.cfg.expected_hindsight_model,
        "endpoint": "chat",
        "request_provenance_sha256": provenance_sha256,
        "recovery_probe_route": route,
        "recovery_auth_incident_request_id": root_id,
        "recovery_gate_sha256": gate_sha256,
        "cost_usd": 0.0001,
    }
    status = _probe_status(root_id, 401, root_at)
    status.update(
        {
            "reservations": [reservation],
            "sha256": "b" * 64,
            "total_max_usd": "0.0001",
        }
    )
    instance.budget_recovery_status = lambda: status
    instance._http_json = lambda _url: {
        "circuit_reason": "ambiguous upstream failure: ReadTimeout",
        "in_flight": 1,
        "hindsight_backfill_spent_usd": "17.0",
        "provider_spent_usd": "17.0",
    }
    instance._recovery_probe_gate_for_spend_only = lambda _budget: Decimal("20")
    instance._http_post_json_status = lambda *_args, **_kwargs: (
        _ for _ in ()
    ).throw(AssertionError("persisted started route was submitted again"))
    persisted = []

    def save(_cfg, value, **_kwargs):
        persisted.append(json.loads(json.dumps(value)))

    monkeypatch.setattr(recovery, "save_incident", save)
    state = {
        "kind": "auth",
        "stage": "recovery_probes_started",
        "probe_auth_incident_request_id": root_id,
        "probe_auth_incident_status": 401,
        "probe_auth_incident_at": root_at,
        "probe_history": [],
        "probe_routes": {
            route: {
                "status": "started",
                "token": token,
                "provenance": provenance,
                "request_gate": gate,
                "request_gate_sha256": gate_sha256,
            }
        },
    }
    incident = {"provider_recovery": state}

    assert (
        instance.provider_auth_recovery_probes(
            state, incident, authorized_incident_ids=[root_id]
        )
        is False
    )
    assert len(persisted) == 1
    ambiguity = persisted[0]["provider_recovery"]["probe_ambiguity"]
    assert ambiguity["stage"] == "snapshot"
    assert ambiguity["request_id"] == request_id
    assert ambiguity["reservation_manifest"]["reservations"] == [reservation]


def test_probe_provider_failure_allowlist_covers_all_definitive_5xx():
    instance = object.__new__(recovery.Recovery)
    incident_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    history = []
    live = {}
    for ordinal, status_code in enumerate((501, 599), 1):
        provenance = f"hindsight-recovery-{ordinal:024x}-gpt_oss"
        request_id = f"request-{ordinal}"
        history.append(
            {
                "route": "gpt_oss",
                "provenance": provenance,
                "result": "retryable_failure",
                "request_id": request_id,
            }
        )
        live[request_id] = {
            "request_id": request_id,
            "status_code": status_code,
            "request_provenance_sha256": recovery.hashlib.sha256(
                provenance.encode()
            ).hexdigest(),
            "recovery_probe_route": "gpt_oss",
            "recovery_auth_incident_request_id": incident_id,
        }
    incident = {
        "provider_recovery": {
            "kind": "auth",
            "probe_auth_incident_request_id": incident_id,
            "probe_routes": {},
            "probe_history": history,
        }
    }
    assert instance._recovery_probe_provider_failure_records(incident, live) == live


def test_probe_ambiguity_rejects_unrelated_single_reservation():
    instance = object.__new__(recovery.Recovery)
    instance.cfg = config()
    root_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    provenance = "hindsight-recovery-111111111111111111111111-gpt_oss"
    state = {
        "probe_auth_incident_request_id": root_id,
        "probe_routes": {"gpt_oss": {"request_gate_sha256": "b" * 64}},
    }
    instance.budget_recovery_status = lambda: {
        "reservations": [
            {
                "request_id": str(
                    recovery.uuid.uuid5(recovery.RECOVERY_PROBE_NAMESPACE, provenance)
                ),
                "model": instance.cfg.expected_hindsight_model,
                "endpoint": "chat",
                "request_provenance_sha256": "0" * 64,
                "recovery_probe_route": "gpt_oss",
                "recovery_auth_incident_request_id": root_id,
                "recovery_gate_sha256": "b" * 64,
            }
        ]
    }
    instance._http_json = lambda *_args, **_kwargs: {
        "in_flight": 1,
        "circuit_reason": "ambiguous upstream failure: timeout",
    }
    try:
        instance.start_auth_probe_ambiguity(
            state,
            {"provider_recovery": state},
            route="gpt_oss",
            provenance=provenance,
            response={
                "request_id": str(
                    recovery.uuid.uuid5(recovery.RECOVERY_PROBE_NAMESPACE, provenance)
                )
            },
        )
    except recovery.InvariantError as exc:
        assert "exact durable reservation" in str(exc)
    else:
        raise AssertionError("unrelated singleton reservation was accepted")


def test_checkpoint_revisions_are_monotonic_and_append_only(tmp_path):
    cfg = config()
    object.__setattr__(cfg, "state_dir", tmp_path)
    phase = dict(recovery.DEFAULT_PHASE)
    checkpoint = recovery.load_checkpoint(cfg, phase)
    assert checkpoint["accepted"]["histories"] == {
        "retry": 0,
        "claim": 0,
        "adoption": 0,
    }
    updated = json.loads(json.dumps(checkpoint))
    updated["accepted"]["histories"]["retry"] = 1
    updated["recovered_attempts"].append({"key": "doc:op"})
    appended = recovery.append_checkpoint(
        cfg, checkpoint, updated, reason="test exact retry"
    )
    assert appended["revision"] == 2
    assert len(cfg.checkpoint_log.read_text().splitlines()) == 2

    invalid = json.loads(json.dumps(appended))
    invalid["accepted"]["histories"]["retry"] = 0
    try:
        recovery.append_checkpoint(cfg, appended, invalid, reason="invalid")
    except recovery.InvariantError as exc:
        assert "decreased" in str(exc)
    else:
        raise AssertionError("decreasing a checkpoint baseline must fail closed")


def test_exact_legacy_checkpoint_schema_appends_migration_and_repairs_crash(tmp_path):
    """The sealed 3,823 journal remains valid without rewriting old records."""
    cfg = config()
    object.__setattr__(cfg, "state_dir", tmp_path)
    legacy = {
        "version": 1,
        "revision": 1,
        "accepted": {
            "fugu_failures": [],
            "hindsight_failed": 60,
            "histories": {"retry": 20, "claim": 12, "adoption": 1},
            "hindsight_failures": [],
            "history_manifests": {
                "retry": [],
                "claim": [],
                "adoption": [],
            },
        },
        "recovered_attempts": [],
        "stage": {
            "approved_total": 5142,
            "cap": 3823,
            "max_agent_active": 2,
            "max_provider_in_flight": 6,
        },
        "created_at": 1.0,
        "updated_at": 1.0,
    }
    revision_two = json.loads(json.dumps(legacy))
    revision_two["revision"] = 2
    revision_two["updated_at"] = 2.0
    recovery._validate_checkpoint_transition(cfg, legacy, revision_two)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    entries = [
        recovery._checkpoint_entry(legacy, kind="checkpoint_created"),
        recovery._checkpoint_entry(
            revision_two,
            kind="checkpoint_appended",
            previous=legacy,
            reason="sealed legacy baseline",
        ),
    ]
    cfg.checkpoint_log.write_text(
        "".join(json.dumps(value, separators=(",", ":")) + "\n" for value in entries)
    )
    cfg.checkpoint_file.write_text(json.dumps(revision_two))

    loaded = recovery.load_checkpoint(cfg, recovery.DEFAULT_PHASE)
    assert loaded == revision_two
    assert "operation_manifest" not in loaded["accepted"]
    assert "bootstrap_pending" not in loaded

    migrated = json.loads(json.dumps(loaded))
    migrated["accepted"].update(
        {
            "fugu_failure_manifest": [],
            "provider_failure_manifest": [],
            "exact_hindsight_failures": [],
            "exact_history_manifests": {
                "retry": [],
                "claim": [],
                "adoption": [],
            },
            "pocket_counts": {"queued": 9, "succeeded": 62},
            "operation_manifest": [],
        }
    )
    migrated["bootstrap_pending"] = False
    migrated["revision"] = 3
    migrated["updated_at"] = 3.0
    recovery._validate_checkpoint_transition(cfg, loaded, migrated)

    # Simulate the journal-first append crashing before checkpoint.json replace.
    with cfg.checkpoint_log.open("a") as handle:
        handle.write(
            json.dumps(
                recovery._checkpoint_entry(
                    migrated,
                    kind="checkpoint_appended",
                    previous=loaded,
                    reason="append exact migration",
                ),
                separators=(",", ":"),
            )
            + "\n"
        )
    repaired = recovery.load_checkpoint(cfg, recovery.DEFAULT_PHASE)
    assert repaired == migrated
    assert json.loads(cfg.checkpoint_file.read_text()) == migrated
    assert len(cfg.checkpoint_log.read_text().splitlines()) == 3


def test_checkpoint_manifests_reject_reorder_and_duplicate_identity():
    cfg = config()
    previous = recovery._default_checkpoint(cfg, recovery.DEFAULT_PHASE)
    previous["accepted"]["fugu_failures"] = [
        ["2026-07-14T00:00:00+00:00", 401, "model-a"],
        ["2026-07-14T00:00:01+00:00", 402, "model-b"],
    ]
    candidate = json.loads(json.dumps(previous))
    candidate["revision"] = 2
    candidate["accepted"]["fugu_failures"].reverse()
    try:
        recovery._validate_checkpoint_transition(cfg, previous, candidate)
    except recovery.InvariantError as exc:
        assert "Fugu baseline" in str(exc)
    else:
        raise AssertionError("reordered Fugu checkpoint prefix was accepted")

    duplicate = recovery._default_checkpoint(cfg, recovery.DEFAULT_PHASE)
    record = {"request_id": "request", "record_sha256": "a" * 64}
    duplicate["accepted"]["fugu_failure_manifest"] = [record, record]
    try:
        recovery._validate_checkpoint(cfg, duplicate)
    except recovery.InvariantError as exc:
        assert "identities are duplicated" in str(exc)
    else:
        raise AssertionError("duplicate exact request identity was accepted")


def test_failure_manifests_append_chronologically_not_by_random_request_id():
    cfg = config()
    previous = recovery._default_checkpoint(cfg, recovery.DEFAULT_PHASE)
    previous["accepted"]["fugu_failure_manifest"] = [
        {"request_id": "z-later-lexically", "record_sha256": "a" * 64}
    ]
    previous["accepted"]["provider_failure_manifest"] = [
        {"request_id": "z-provider", "record_sha256": "b" * 64}
    ]
    candidate = json.loads(json.dumps(previous))
    candidate["revision"] = 2
    candidate["accepted"]["fugu_failure_manifest"].append(
        {"request_id": "a-newer-chronologically", "record_sha256": "c" * 64}
    )
    candidate["accepted"]["provider_failure_manifest"].append(
        {"request_id": "a-newer-provider", "record_sha256": "d" * 64}
    )
    recovery._validate_checkpoint_transition(cfg, previous, candidate)
    source = MODULE.read_text()
    assert source.count("ORDER BY start_time,request_id") >= 2


def test_hindsight_failure_authority_is_checkpoint_only():
    source = MODULE.read_text()
    example = (Path(__file__).resolve().parents[1] / "recovery.env.example").read_text()
    assert "EXPECTED_HINDSIGHT_FAILED" not in source
    assert "expected_hindsight_failed" not in source
    assert "EXPECTED_HINDSIGHT_FAILED" not in example


def test_checkpoint_pocket_baseline_is_immutable():
    cfg = config()
    previous = recovery._default_checkpoint(cfg, recovery.DEFAULT_PHASE)
    previous["accepted"]["pocket_counts"] = {"queued": 9, "succeeded": 62}
    candidate = json.loads(json.dumps(previous))
    candidate["revision"] = 2
    candidate["accepted"]["pocket_counts"]["queued"] = 8
    try:
        recovery._validate_checkpoint_transition(cfg, previous, candidate)
    except recovery.InvariantError as exc:
        assert "Pocket count baseline" in str(exc)
    else:
        raise AssertionError("Pocket baseline drift was accepted")


def test_controller_does_not_interrupt_a_healthy_running_wave():
    assert (
        recovery.controller_action(
            phase_name="running",
            failures=[],
            agent_open=False,
            attempted=3531,
            cap=5142,
            active=2,
            errors=0,
            hindsight_active=2,
            provider_in_flight=6,
        )
        == "monitor"
    )


def test_release_verification_waits_for_remote_and_provider_drain():
    for hindsight_active, provider_in_flight in ((1, 0), (0, 1), (1, 1)):
        assert (
            recovery.controller_action(
                phase_name="release_verifying",
                failures=[],
                agent_open=True,
                attempted=3531,
                cap=5142,
                active=2,
                errors=0,
                hindsight_active=hindsight_active,
                provider_in_flight=provider_in_flight,
            )
            == "wait"
        )
    assert (
        recovery.controller_action(
            phase_name="release_verifying",
            failures=[],
            agent_open=True,
            attempted=3531,
            cap=5142,
            active=2,
            errors=0,
            hindsight_active=0,
            provider_in_flight=0,
        )
        == "recover"
    )


def test_incident_row_recovery_waits_for_remote_and_provider_drain():
    for phase_name in ("ready", "running", "release_verifying", "finalizing"):
        assert (
            recovery.controller_action(
                phase_name=phase_name,
                failures=["Agent failed/blocked row present"],
                agent_open=True,
                attempted=3531,
                cap=5142,
                active=0,
                errors=1,
                hindsight_active=1,
                provider_in_flight=1,
            )
            == "wait"
        )
    assert (
        recovery.controller_action(
            phase_name="running",
            failures=["Agent failed/blocked row present"],
            agent_open=True,
            attempted=3531,
            cap=5142,
            active=0,
            errors=1,
            hindsight_active=0,
            provider_in_flight=0,
        )
        == "recover"
    )


def test_running_audit_tolerates_only_exact_completed_operation_suffix():
    accepted = [
        {
            "operation_id": "old",
            "operation_type": "batch_retain",
            "status": "completed",
            "record_sha256": "old-sha",
        }
    ]
    completed_suffix = [
        {
            "operation_id": "new-parent",
            "operation_type": "batch_retain",
            "status": "completed",
            "record_sha256": "parent-sha",
        },
        {
            "operation_id": "new-child",
            "operation_type": "retain",
            "status": "completed",
            "record_sha256": "child-sha",
        },
    ]
    for phase_name in ("running", "release_verifying"):
        assert (
            recovery.terminal_operation_inventory_failure(
                phase_name=phase_name,
                accepted_operations=accepted,
                observed_operations=accepted + completed_suffix,
            )
            is None
        )
    assert (
        recovery.terminal_operation_inventory_failure(
            phase_name="cap_hold",
            accepted_operations=accepted,
            observed_operations=accepted + completed_suffix,
        )
        == "terminal operation inventory has unaccepted append-only additions"
    )


def test_running_operation_suffix_fails_closed_on_terminal_or_provenance_drift():
    accepted = [
        {
            "operation_id": "old",
            "operation_type": "batch_retain",
            "status": "completed",
            "record_sha256": "old-sha",
        }
    ]
    completed = {
        "operation_id": "new",
        "operation_type": "retain",
        "status": "completed",
        "record_sha256": "new-sha",
    }
    for status in ("failed", "cancelled"):
        assert (
            recovery.terminal_operation_inventory_failure(
                phase_name="running",
                accepted_operations=accepted,
                observed_operations=accepted + [{**completed, "status": status}],
            )
            == "terminal operation inventory has failed/cancelled append-only additions"
        )
    for observed in (
        [{**accepted[0], "record_sha256": "mutated"}, completed],
        [completed, accepted[0]],
        [],
    ):
        assert (
            recovery.terminal_operation_inventory_failure(
                phase_name="running",
                accepted_operations=accepted,
                observed_operations=observed,
            )
            == "terminal operation inventory provenance drift"
        )
    assert (
        recovery.terminal_operation_inventory_failure(
            phase_name="running",
            accepted_operations=accepted,
            observed_operations=accepted + [completed],
            inventory_failures=["duplicate completed pair"],
        )
        == "terminal operation inventory provenance drift"
    )


def test_controller_recovers_only_after_agent_is_open():
    assert (
        recovery.controller_action(
            phase_name="running",
            failures=[],
            agent_open=True,
            attempted=3531,
            cap=5142,
            active=2,
            errors=0,
            hindsight_active=0,
            provider_in_flight=0,
        )
        == "recover"
    )


def test_controller_hard_holds_provenance_and_budget_failures():
    for failure in (
        "payload hash mismatch for doc",
        "projected full batch 31 exceeds hard budget",
        "Hindsight document has invalid Agent state provenance",
    ):
        assert (
            recovery.controller_action(
                phase_name="ready",
                failures=[failure],
                agent_open=True,
                attempted=3529,
                cap=5142,
                active=0,
                errors=0,
                hindsight_active=0,
                provider_in_flight=0,
            )
            == "hard_hold"
        )


def test_controller_infrastructure_failures_dominate_agent_error_recovery():
    for failure in (
        "Hindsight health degraded",
        "hindsight-recovery-tunnel.service inactive",
        "Agent runtime proof is stale",
        "budget health/reservation in-flight count drift",
        "budget Hindsight model drift",
        "Pocket circuit is closed",
        "checkpoint stage configuration drift",
    ):
        assert (
            recovery.controller_action(
                phase_name="running",
                failures=[failure, "Agent failed/blocked row present"],
                agent_open=True,
                attempted=3531,
                cap=5142,
                active=0,
                errors=1,
                hindsight_active=0,
                provider_in_flight=0,
            )
            == "retry_audit"
        )


def test_exact_provider_circuit_can_enter_recovery_from_closed_phase():
    failures = recovery.provider_incident_failures(
        ["budget circuit is not healthy/closed", "Agent failed/blocked row present"],
        errors=1,
        budget={
            "circuit_open": True,
            "circuit_reason": "provider returned HTTP 401",
        },
        budget_recovery={"reservations": []},
    )
    assert failures == ["Agent failed/blocked row present"]
    assert (
        recovery.controller_action(
            phase_name="running",
            failures=failures,
            agent_open=True,
            attempted=3531,
            cap=5142,
            active=0,
            errors=1,
            hindsight_active=0,
            provider_in_flight=0,
        )
        == "recover"
    )


def test_exact_provider_circuit_can_enter_recovery_with_held_submitted_rows():
    assert recovery.has_recoverable_incident_rows(errors=0, active=2, agent_open=True)
    failures = recovery.provider_incident_failures(
        ["budget circuit is not healthy/closed"],
        errors=0,
        active=2,
        agent_open=True,
        budget={
            "circuit_open": True,
            "circuit_reason": "provider returned HTTP 401",
        },
        budget_recovery={"reservations": []},
    )
    assert failures == []
    assert (
        recovery.controller_action(
            phase_name="running",
            failures=failures,
            agent_open=True,
            attempted=3571,
            cap=5142,
            active=2,
            errors=0,
            hindsight_active=0,
            provider_in_flight=0,
        )
        == "recover"
    )


def test_exact_snapshotted_probe_reservation_can_enter_reconciliation():
    provenance = "hindsight-recovery-111111111111111111111111-gpt_oss"
    request_id = str(
        recovery.uuid.uuid5(recovery.RECOVERY_PROBE_NAMESPACE, provenance)
    )
    reservation = {
        "request_id": request_id,
        "recovery_probe_route": "gpt_oss",
        "request_provenance_sha256": recovery.hashlib.sha256(
            provenance.encode()
        ).hexdigest(),
    }
    incident = {
        "provider_recovery": {
            "probe_ambiguity": {
                "stage": "snapshot",
                "route": "gpt_oss",
                "provenance": provenance,
                "request_id": request_id,
                "reason": "ambiguous upstream failure: ReadTimeout",
                "reservation_manifest": {
                    "reservations": [reservation],
                    "sha256": "a" * 64,
                    "total_max_usd": "0.0001",
                },
            }
        }
    }
    budget = {
        "circuit_open": True,
        "circuit_reason": "ambiguous upstream failure: ReadTimeout",
        "in_flight": 1,
    }
    budget_recovery = {
        "reservations": [reservation],
        "sha256": "a" * 64,
        "total_max_usd": "0.0001",
    }
    assert recovery.exact_probe_reservation_recovery(
        incident, budget=budget, budget_recovery=budget_recovery
    )
    assert (
        recovery.controller_action(
            phase_name="running",
            failures=["Agent failed/blocked row present"],
            agent_open=True,
            attempted=3571,
            cap=5142,
            active=2,
            errors=0,
            hindsight_active=0,
            provider_in_flight=1,
            probe_ambiguity_recovery=True,
        )
        == "reconcile_probe_reservation"
    )
    assert (
        recovery.controller_action(
            phase_name="running",
            failures=[],
            agent_open=True,
            attempted=3571,
            cap=5142,
            active=0,
            errors=0,
            hindsight_active=0,
            provider_in_flight=1,
            probe_ambiguity_recovery=False,
        )
        == "wait"
    )

    drifted = json.loads(json.dumps(budget_recovery))
    drifted["reservations"][0]["request_id"] = "other"
    try:
        recovery.exact_probe_reservation_recovery(
            incident, budget=budget, budget_recovery=drifted
        )
    except recovery.InvariantError as exc:
        assert "differs" in str(exc)
    else:
        raise AssertionError("drifted probe reservation entered reconciliation")


def test_probe_only_reconciliation_cannot_touch_completed_agent_row():
    instance = object.__new__(recovery.Recovery)
    instance.cfg = config()
    provenance = "hindsight-recovery-111111111111111111111111-gpt_oss"
    request_id = str(
        recovery.uuid.uuid5(recovery.RECOVERY_PROBE_NAMESPACE, provenance)
    )
    reservation = {
        "request_id": request_id,
        "recovery_probe_route": "gpt_oss",
        "request_provenance_sha256": recovery.hashlib.sha256(
            provenance.encode()
        ).hexdigest(),
    }
    state = {
        "probe_ambiguity": {
            "stage": "snapshot",
            "route": "gpt_oss",
            "provenance": provenance,
            "request_id": request_id,
            "reason": "ambiguous upstream failure: ReadTimeout",
            "reservation_manifest": {
                "reservations": [reservation],
                "sha256": "a" * 64,
                "total_max_usd": "0.0001",
            },
        }
    }
    incident = {"provider_recovery": state}
    before_budget = {
        "circuit_open": True,
        "circuit_reason": "ambiguous upstream failure: ReadTimeout",
        "in_flight": 1,
    }
    after_budget = {
        "circuit_open": True,
        "circuit_reason": "provider returned 401",
        "in_flight": 0,
        "recovery_controls_sealed_disabled": True,
    }
    statuses = iter(
        (
            {
                "reservations": [reservation],
                "sha256": "a" * 64,
                "total_max_usd": "0.0001",
            },
            {"reservations": []},
        )
    )
    budgets = iter((before_budget, after_budget))
    instance._http_json = lambda _url: next(budgets)
    instance.budget_recovery_status = lambda: next(statuses)

    def reconcile(recovery_state, recovery_incident):
        recovery_state.pop("probe_ambiguity")
        recovery_incident["provider_recovery"] = recovery_state
        return True

    instance.recover_auth_probe_ambiguity = reconcile
    instance._incident_rows = lambda: (_ for _ in ()).throw(
        AssertionError("probe-only action inspected Agent rows")
    )
    result = instance.reconcile_probe_ambiguity_only(incident)
    assert result == {"status": "probe_reconciliation_sealed"}
    assert "probe_ambiguity" not in state


def test_staged_recovery_helper_is_admitted_only_for_exact_held_rows():
    staged = recovery.STAGED_RECOVERY_HELPER_FAILURE
    provider = "budget circuit is not healthy/closed"
    unrelated = "Hindsight health degraded"
    budget = {
        "circuit_open": True,
        "circuit_reason": "provider returned HTTP 401",
    }
    budget_recovery = {"reservations": []}

    assert recovery.provider_incident_failures(
        [staged, provider, unrelated],
        errors=0,
        active=2,
        agent_open=True,
        budget=budget,
        budget_recovery=budget_recovery,
    ) == [unrelated]
    assert recovery.provider_incident_failures(
        [staged, provider],
        errors=0,
        active=0,
        agent_open=True,
        budget=budget,
        budget_recovery=budget_recovery,
    ) == [staged, provider]
    assert recovery.provider_incident_failures(
        [staged, provider],
        errors=0,
        active=2,
        agent_open=False,
        budget=budget,
        budget_recovery=budget_recovery,
    ) == [staged, provider]


def test_recovery_helper_identity_falls_back_to_agent_and_detects_divergence():
    cfg = config()
    assert recovery.recovery_helper_identity_failure(cfg) is None
    object.__setattr__(
        cfg,
        "expected_recovery_outbox_build_id",
        "outbox.py:sha256:" + "1" * 64,
    )
    assert (
        recovery.recovery_helper_identity_failure(cfg)
        == recovery.STAGED_RECOVERY_HELPER_FAILURE
    )


def test_provider_circuit_is_not_suppressed_for_unheld_submitted_rows():
    assert not recovery.has_recoverable_incident_rows(
        errors=0, active=2, agent_open=False
    )
    failures = recovery.provider_incident_failures(
        ["budget circuit is not healthy/closed"],
        errors=0,
        active=2,
        agent_open=False,
        budget={
            "circuit_open": True,
            "circuit_reason": "provider returned HTTP 401",
        },
        budget_recovery={"reservations": []},
    )
    assert failures == ["budget circuit is not healthy/closed"]


def test_provider_circuit_never_suppresses_other_infrastructure_drift():
    failures = recovery.provider_incident_failures(
        [
            "budget circuit is not healthy/closed",
            "budget health/reservation in-flight count drift",
        ],
        errors=1,
        budget={
            "circuit_open": True,
            "circuit_reason": "ambiguous upstream timeout",
        },
        budget_recovery={
            "reservations": [{"request_id": "one"}, {"request_id": "two"}]
        },
    )
    assert failures == ["budget health/reservation in-flight count drift"]
    assert (
        recovery.controller_action(
            phase_name="running",
            failures=failures,
            agent_open=True,
            attempted=3531,
            cap=5142,
            active=0,
            errors=1,
            hindsight_active=0,
            provider_in_flight=1,
        )
        == "retry_audit"
    )


def test_controller_manifest_drift_and_concurrency_bounds_hard_hold():
    for failure in (
        "Hindsight failed-operation count drift",
        "authenticated Fugu failure ledger drift",
        "exact Agent history manifest drift",
        "full Hindsight failure provenance manifest drift",
    ):
        assert (
            recovery.controller_action(
                phase_name="running",
                failures=[failure],
                agent_open=True,
                attempted=3531,
                cap=5142,
                active=0,
                errors=0,
                hindsight_active=0,
                provider_in_flight=0,
            )
            == "hard_hold"
        )
    assert (
        recovery.controller_action(
            phase_name="running",
            failures=[],
            agent_open=True,
            attempted=3531,
            cap=5142,
            active=3,
            errors=0,
            hindsight_active=0,
            provider_in_flight=0,
        )
        == "hard_hold"
    )


def test_exact_hindsight_failure_delta_can_enter_incident_recovery():
    accepted_failures = [
        {"operation_id": "old", "status": "failed", "record_sha256": "a"}
    ]
    parent = {
        "operation_id": "parent",
        "status": "failed",
        "record_sha256": "b",
    }
    child = {
        "operation_id": "child",
        "status": "failed",
        "record_sha256": "c",
    }
    accepted_operations = [
        {
            "operation_id": "old",
            "operation_type": "consolidation",
            "status": "failed",
            "record_sha256": "d",
        }
    ]
    observed_operations = accepted_operations + [
        {**parent, "operation_type": "batch_retain"},
        {**child, "operation_type": "retain"},
    ]
    failures = recovery.authorized_hindsight_incident_failures(
        [
            "Agent failed/blocked row present",
            "Hindsight failed-operation count drift",
        ],
        accepted_failures=accepted_failures,
        observed_failures=accepted_failures + [parent, child],
        accepted_operations=accepted_operations,
        observed_operations=observed_operations,
        authorized_operation_ids={"parent", "child"},
    )
    assert failures == ["Agent failed/blocked row present"]
    assert (
        recovery.controller_action(
            phase_name="running",
            failures=failures,
            agent_open=True,
            attempted=3531,
            cap=5142,
            active=0,
            errors=1,
            hindsight_active=0,
            provider_in_flight=0,
        )
        == "recover"
    )


def test_unrelated_hindsight_failure_delta_remains_a_hard_hold():
    accepted = [{"operation_id": "old", "status": "failed", "record_sha256": "a"}]
    parent = {
        "operation_id": "parent",
        "status": "failed",
        "record_sha256": "b",
    }
    unrelated = {
        "operation_id": "unrelated",
        "status": "failed",
        "record_sha256": "c",
    }
    failures = recovery.authorized_hindsight_incident_failures(
        ["Hindsight failed-operation count drift"],
        accepted_failures=accepted,
        observed_failures=accepted + [parent, unrelated],
        accepted_operations=[],
        observed_operations=[
            {**parent, "operation_type": "batch_retain"},
            {**unrelated, "operation_type": "consolidation"},
        ],
        authorized_operation_ids={"parent"},
    )
    assert failures == ["Hindsight failed-operation count drift"]
    assert (
        recovery.controller_action(
            phase_name="running",
            failures=[],
            agent_open=True,
            attempted=3531,
            cap=5142,
            active=0,
            errors=0,
            hindsight_active=0,
            provider_in_flight=7,
        )
        == "hard_hold"
    )


def test_exact_operation_proof_normalizes_event_date():
    item = {
        "document_id": "doc",
        "content": "value",
        "context": "source:agent_sessions | document:doc",
        "tags": ["agent-session"],
        "timestamp": "2026-07-14T00:00:00+00:00",
    }
    digest = recovery.hashlib.sha256(
        json.dumps(item, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    operation = {
        "task_payload": {
            "bank_id": "agent-sessions",
            "contents": [
                {
                    **{key: value for key, value in item.items() if key != "timestamp"},
                    "event_date": item["timestamp"],
                }
            ],
        },
        "result_metadata": {"document_ids": ["doc"]},
    }
    assert recovery.operation_matches_outbox(
        operation, document_id="doc", payload_hash=digest
    )
    assert not recovery.operation_matches_outbox(
        operation, document_id="other", payload_hash=digest
    )


def test_operationless_adoption_requires_every_exact_child_linked_to_parent():
    parent = {
        "operation_id": "parent",
        "operation_type": "batch_retain",
        "result_metadata": {},
    }
    child = {
        "operation_id": "child",
        "operation_type": "retain",
        "result_metadata": {"parent_operation_id": "parent"},
    }
    assert recovery.unique_exact_batch_parent([parent, child]) is parent
    unrelated = {
        "operation_id": "unrelated-child",
        "operation_type": "retain",
        "result_metadata": {"parent_operation_id": "other-parent"},
    }
    try:
        recovery.unique_exact_batch_parent([parent, child, unrelated])
    except recovery.InvariantError as exc:
        assert "unrelated exact remote child" in str(exc)
    else:
        raise AssertionError("unrelated exact child was accepted for adoption")
    wrong_type = {
        "operation_id": "consolidation",
        "operation_type": "consolidation",
        "result_metadata": {"parent_operation_id": "parent"},
    }
    try:
        recovery.unique_exact_batch_parent([parent, wrong_type])
    except recovery.InvariantError as exc:
        assert "unrelated exact remote child" in str(exc)
    else:
        raise AssertionError("non-retain child was accepted for adoption")


def test_operationless_second_proof_rejects_newly_appearing_operation():
    first = {
        "checked_at": 100.0,
        "document_present": False,
        "operation_count": 0,
        "hindsight_active": 0,
        "provider_in_flight_before": 0,
        "provider_in_flight_after": 0,
    }
    second = {**first, "checked_at": 161.0}
    assert recovery.stable_operationless_absence(
        first, second, minimum_interval_seconds=60
    )
    raced = {**second, "operation_count": 1}
    try:
        recovery.stable_operationless_absence(first, raced, minimum_interval_seconds=60)
    except recovery.InvariantError as exc:
        assert "document/operation activity" in str(exc)
    else:
        raise AssertionError("new remote operation did not invalidate absence proof")
    provider_race = {**second, "provider_in_flight_after": 1}
    try:
        recovery.stable_operationless_absence(
            first, provider_race, minimum_interval_seconds=60
        )
    except recovery.InvariantError as exc:
        assert "document/operation activity" in str(exc)
    else:
        raise AssertionError("provider activity did not invalidate absence proof")


def test_operationless_snapshot_uses_one_repeatable_read_transaction():
    source = MODULE.read_text()
    method = source[source.index("def _operationless_remote_snapshot") :]
    method = method[: method.index("def _explained_terminal_operation_ids")]
    assert "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY" in method
    assert method.count("with self.hindsight_db()") == 1
    assert "FROM documents" in method
    assert "FROM async_operations" in method


def _inventory_fixture(*, state="succeeded", status="completed", duplicate=False):
    item = {
        "document_id": "doc",
        "content": "value",
        "context": "source:agent_sessions | document:doc",
        "tags": ["agent-session"],
        "timestamp": "2026-07-14T00:00:00+00:00",
    }
    digest = recovery.hashlib.sha256(
        json.dumps(
            item, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()
    local = [
        {
            "document_id": "doc",
            "state": state,
            "operation_id": "parent-1",
            "payload_hash": digest,
            "submitted_payload_hash": digest,
            "item": item,
        }
    ]
    retry = []

    def pair(ordinal):
        parent_id = f"parent-{ordinal}"
        child_id = f"child-{ordinal}"
        created = f"2026-07-14T00:00:0{ordinal}+00:00"
        parent = {
            "operation_id": parent_id,
            "operation_type": "batch_retain",
            "status": status,
            "error_message": "provider 503" if status == "failed" else None,
            "result_metadata": {
                "is_parent": True,
                "items_count": 1,
                "num_sub_batches": 1,
            },
            "task_payload": {},
            "created_at": created,
            "updated_at": created,
        }
        child = {
            "operation_id": child_id,
            "operation_type": "retain",
            "status": status,
            "error_message": "provider 503" if status == "failed" else None,
            "result_metadata": {
                "document_ids": ["doc"],
                "parent_operation_id": parent_id,
            },
            "task_payload": {
                "bank_id": "agent-sessions",
                "operation_id": child_id,
                "type": "batch_retain",
                "contents": [
                    {
                        **{
                            key: value
                            for key, value in item.items()
                            if key != "timestamp"
                        },
                        "event_date": item["timestamp"],
                    }
                ],
            },
            "created_at": created,
            "updated_at": created,
        }
        return [parent, child]

    operations = pair(1)
    if duplicate:
        operations.extend(pair(2))
    return local, retry, operations


def _completed_remote_document(item):
    content = str(item["content"])
    return {
        "id": item["document_id"],
        "original_text": content,
        "content_hash": recovery.hashlib.sha256(content.encode()).hexdigest(),
        "retain_params": {
            "context": item["context"],
            "event_date": item["timestamp"],
        },
        "tags": list(item["tags"]),
    }


def _completed_parent_proof_instance(row, parent, children, remote):
    instance = object.__new__(recovery.Recovery)
    instance.cfg = config()
    instance._remote_operation = lambda operation_id: (
        parent if operation_id == row["operation_id"] else None
    )
    instance._retain_children_for_parent = lambda _parent_id: list(children)
    instance._remote_document = lambda document_id: (
        remote if document_id == row["document_id"] else None
    )
    return instance


def _terminal_parent_proof_instance(row, children, remote=None):
    instance = object.__new__(recovery.Recovery)
    instance.cfg = config()
    instance._retain_children_for_parent = lambda _parent_id: list(children)
    instance._remote_document = lambda _document_id: remote
    return instance


def test_terminal_parent_cleared_payload_requires_one_exact_failed_child():
    local, _retry, operations = _inventory_fixture(state="submitted", status="failed")
    row = local[0]
    parent, child = operations
    instance = _terminal_parent_proof_instance(row, [child])

    assert instance._terminal_lineage_records(row, parent) == [
        recovery._exact_record(parent, "operation_id", "operation_type", "status"),
        recovery._exact_record(child, "operation_id", "operation_type", "status"),
    ]


def test_terminal_parent_waits_for_one_exact_active_child():
    local, _retry, operations = _inventory_fixture(state="submitted", status="failed")
    row = local[0]
    parent, child = operations
    child["status"] = "processing"
    child["result_metadata"].pop("document_ids")
    child["result_metadata"].update(
        {"items_count": 1, "sub_batch_index": 1, "total_sub_batches": 1}
    )
    instance = _terminal_parent_proof_instance(row, [child])

    assert instance._terminal_lineage_records(row, parent) == []


def test_terminal_parent_cleared_payload_rejects_ambiguous_or_drifted_child():
    local, _retry, operations = _inventory_fixture(state="submitted", status="failed")
    row = local[0]
    parent, child = operations
    cases = []
    duplicate = json.loads(json.dumps(child))
    duplicate["operation_id"] = "child-2"
    duplicate["task_payload"]["operation_id"] = "child-2"
    cases.append((parent, [child, duplicate], None))
    wrong_hash = json.loads(json.dumps(child))
    wrong_hash["task_payload"]["contents"][0]["content"] = "tampered"
    cases.append((parent, [wrong_hash], None))
    wrong_parent = json.loads(json.dumps(parent))
    wrong_parent["result_metadata"]["items_count"] = 2
    cases.append((wrong_parent, [child], None))
    wrong_payload = json.loads(json.dumps(parent))
    wrong_payload["task_payload"] = {"bank_id": "other"}
    cases.append((wrong_payload, [child], None))
    cases.append((parent, [child], {"id": row["document_id"]}))

    for candidate_parent, children, remote in cases:
        instance = _terminal_parent_proof_instance(row, children, remote)
        try:
            instance._terminal_lineage_records(row, candidate_parent)
        except recovery.InvariantError:
            pass
        else:
            raise AssertionError("cleared terminal parent accepted drifted lineage")


def test_incident_authorization_accepts_cleared_failed_parent_via_exact_child(
    monkeypatch,
):
    local, _retry, operations = _inventory_fixture(state="submitted", status="failed")
    row = local[0]
    parent, child = operations
    instance = _completed_parent_proof_instance(row, parent, [child], None)
    instance.fugu_failure_manifest = lambda: []
    instance.budget_recovery_status = lambda: {
        "provider_failure_manifest": [],
        "provider_failure_history": [],
        "reservations": [],
    }
    instance.ensure_checkpoint_manifests = lambda: {
        "accepted": {
            "fugu_failure_manifest": [],
            "provider_failure_manifest": [],
        }
    }
    instance._provider_preflight_failure_records = lambda *_args: {}
    instance._recovery_probe_provider_failure_records = lambda *_args: {}
    monkeypatch.setattr(recovery, "save_incident", lambda *_args, **_kwargs: None)

    authorization = instance.prepare_incident_authorization({}, [row])
    assert authorization["terminal_operations"] == [
        recovery._exact_record(child, "operation_id", "operation_type", "status"),
        recovery._exact_record(parent, "operation_id", "operation_type", "status"),
    ]
    assert authorization["authorized_failure_operation_ids"] == [
        "child-1",
        "parent-1",
    ]


def test_recover_incident_rows_retries_cleared_failed_parent_exactly_once():
    local, _retry, operations = _inventory_fixture(state="submitted", status="failed")
    row = local[0]
    parent, child = operations
    refreshes = []
    retries = []
    accepted = []

    class Worker:
        def _refresh_submitted(self, _conn, **kwargs):
            refreshes.append(kwargs)
            return 0, 1

    def retry_terminal_document(
        _conn,
        document_id,
        operation_id,
        **kwargs,
    ):
        retries.append((document_id, operation_id, kwargs))
        return True

    instance = _terminal_parent_proof_instance(row, [child])
    instance.cfg = config()
    instance._incident_rows = lambda: [row]
    instance._app_worker = lambda: Worker()
    instance._reviewed_outbox_module = lambda _path: SimpleNamespace(
        OperationlessProof=object,
        CompletedLineageProof=object,
        TerminalLineageProof=_LineageProof,
        adopt_operation=lambda *_args, **_kwargs: True,
        recover_operationless_claim=lambda *_args, **_kwargs: True,
        retry_terminal_document=retry_terminal_document,
    )
    instance.prepare_incident_authorization = lambda *_args: {
        "terminal_pending_child_operation_ids": [],
        "authorized_failure_operation_ids": ["child-1", "parent-1"],
        "authorized_fugu_failures": [],
        "authorized_fugu_request_ids": [],
        "authorized_provider_failures": [],
        "authorized_provider_failure_ids": [],
        "provider_statuses": [],
        "provider_retry_after": [],
    }
    instance._remote_operation = lambda _operation_id: parent
    instance.recover_provider_condition = lambda *_args, **_kwargs: True
    instance.bank_lock = lambda: nullcontext(object())
    instance.mark_pending_acceptance = lambda *_args: None
    instance.accept_recovered_attempt = lambda attempt: accepted.append(attempt)
    instance.clear_pending_acceptance = lambda *_args: None
    instance.close_incident_authorization = lambda *_args, **_kwargs: None

    result = instance.recover_incident_rows({})
    assert result == {"status": "reconciled", "count": 1}
    assert refreshes == [
        {
            "allowed_terminal_circuit_failures": frozenset({("doc", "parent-1")}),
            "only_operations": frozenset({("doc", "parent-1")}),
            "terminal_lineage_proofs": frozenset(
                {
                    _LineageProof(
                        document_id="doc",
                        parent_operation_id="parent-1",
                        child_operation_id="child-1",
                        payload_hash=row["payload_hash"],
                    )
                }
            ),
        }
    ]
    assert len(retries) == 1
    assert retries[0][:2] == ("doc", "parent-1")
    assert retries[0][2]["expected_payload_hash"] == row["payload_hash"]
    assert [value["kind"] for value in accepted] == ["terminal_retry"]


def test_pending_terminal_retry_resumes_after_local_operation_id_is_cleared():
    local, _retry, operations = _inventory_fixture(state="queued", status="failed")
    source_row = local[0]
    parent, child = operations
    retries = []

    class Cursor:
        def __init__(self):
            self.query = ""

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, _params=None):
            self.query = " ".join(query.split())

        def fetchone(self):
            if "SELECT state,payload_hash" in self.query:
                return {
                    **source_row,
                    "state": "queued",
                    "operation_id": None,
                    "claimed_at": None,
                }
            if "SELECT state,operation_id" in self.query:
                return {"state": "queued", "operation_id": None}
            raise AssertionError(self.query)

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return Cursor()

    conn = Connection()

    def retry_terminal_document(
        _conn,
        document_id,
        operation_id,
        **kwargs,
    ):
        retries.append((document_id, operation_id, kwargs))
        return True

    incident = {
        "pending_acceptance": {
            "kind": "terminal_retry",
            "document_id": "doc",
            "operation_id": "parent-1",
            "payload_hash": source_row["payload_hash"],
            "authorized_provider_failure_ids": [],
            "expected_history_delta": {"retry": 1, "claim": 0, "adoption": 0},
        }
    }
    instance = _terminal_parent_proof_instance(source_row, [child])
    instance.cfg = config()
    instance.agent_db = lambda: conn
    instance.bank_lock = lambda: nullcontext(conn)
    instance._reviewed_outbox_module = lambda _path: SimpleNamespace(
        TerminalLineageProof=_LineageProof,
        retry_terminal_document=retry_terminal_document,
    )
    instance._app_worker = lambda: object()
    instance._remote_operation = lambda _operation_id: parent
    instance.mark_pending_acceptance = lambda *_args: None
    instance.accept_recovered_attempt = lambda _attempt: None
    instance.clear_pending_acceptance = lambda value: value.pop(
        "pending_acceptance", None
    )

    assert instance.resume_pending_acceptance(incident) is True
    assert len(retries) == 1
    assert retries[0][:2] == ("doc", "parent-1")
    assert retries[0][2]["expected_payload_hash"] == source_row["payload_hash"]
    assert "pending_acceptance" not in incident


def test_completed_parent_empty_payload_is_proved_by_exact_child_and_document(
    monkeypatch,
):
    local, _retry, operations = _inventory_fixture(
        state="submitted", status="completed"
    )
    row = local[0]
    parent, child = operations
    remote = _completed_remote_document(row["item"])
    instance = _completed_parent_proof_instance(row, parent, [child], remote)

    assert parent["task_payload"] == {}
    assert instance._prove_completed_batch_parent(row, parent) == (
        parent,
        child,
    )
    assert instance._prove_completed_submitted_rows([row]) == frozenset(
        {("doc", "parent-1", "child-1", row["payload_hash"])}
    )

    instance.fugu_failure_manifest = lambda: []
    instance.budget_recovery_status = lambda: {
        "provider_failure_manifest": [],
        "provider_failure_history": [],
        "reservations": [],
    }
    instance.ensure_checkpoint_manifests = lambda: {
        "accepted": {
            "fugu_failure_manifest": [],
            "provider_failure_manifest": [],
        }
    }
    instance._provider_preflight_failure_records = lambda *_args: {}
    instance._recovery_probe_provider_failure_records = lambda *_args: {}
    monkeypatch.setattr(recovery, "save_incident", lambda *_args, **_kwargs: None)

    authorization = instance.prepare_incident_authorization({}, [row])
    assert authorization["terminal_operations"] == []
    assert authorization["authorized_failure_operation_ids"] == []

    failed_parent = json.loads(json.dumps(parent))
    failed_parent["status"] = "failed"
    failed_instance = _completed_parent_proof_instance(
        row, failed_parent, [child], remote
    )
    failed_instance.fugu_failure_manifest = instance.fugu_failure_manifest
    failed_instance.budget_recovery_status = instance.budget_recovery_status
    failed_instance.ensure_checkpoint_manifests = instance.ensure_checkpoint_manifests
    failed_instance._provider_preflight_failure_records = (
        instance._provider_preflight_failure_records
    )
    failed_instance._recovery_probe_provider_failure_records = (
        instance._recovery_probe_provider_failure_records
    )
    try:
        failed_instance.prepare_incident_authorization({}, [row])
    except recovery.InvariantError as exc:
        assert "terminal operation has an existing document" in str(exc)
    else:
        raise AssertionError("completed-only child fallback authorized a failed parent")


def test_completed_parent_child_fallback_rejects_ambiguity_hash_and_metadata():
    cases = (
        "ambiguous_child",
        "child_hash",
        "parent_metadata",
        "parent_payload",
        "child_payload_type_missing",
        "child_payload_type_wrong",
        "child_metadata",
        "failed_parent",
    )
    for case in cases:
        local, _retry, operations = _inventory_fixture(
            state="submitted", status="completed"
        )
        row = local[0]
        parent, child = operations
        children = [child]
        if case == "ambiguous_child":
            duplicate = json.loads(json.dumps(child))
            duplicate["operation_id"] = "child-2"
            children.append(duplicate)
        elif case == "child_hash":
            child["task_payload"]["contents"][0]["content"] = "tampered"
        elif case == "parent_metadata":
            parent["result_metadata"]["items_count"] = 2
        elif case == "parent_payload":
            parent["task_payload"] = {
                "bank_id": "agent-sessions",
                "contents": [
                    {
                        **child["task_payload"]["contents"][0],
                        "document_id": "other",
                    }
                ],
            }
        elif case == "child_payload_type_missing":
            child["task_payload"].pop("type")
        elif case == "child_payload_type_wrong":
            child["task_payload"]["type"] = "retain"
        elif case == "child_metadata":
            child["result_metadata"]["document_ids"] = ["other"]
        elif case == "failed_parent":
            parent["status"] = "failed"
        instance = _completed_parent_proof_instance(
            row,
            parent,
            children,
            _completed_remote_document(row["item"]),
        )
        try:
            instance._prove_completed_batch_parent(row, parent)
        except recovery.InvariantError:
            pass
        else:
            raise AssertionError(f"completed parent fallback accepted {case}")


def test_completed_parent_nonempty_payload_must_independently_match():
    local, _retry, operations = _inventory_fixture(
        state="submitted", status="completed"
    )
    row = local[0]
    parent, child = operations
    parent["task_payload"] = json.loads(json.dumps(child["task_payload"]))
    instance = _completed_parent_proof_instance(
        row,
        parent,
        [child],
        _completed_remote_document(row["item"]),
    )

    assert instance._prove_completed_batch_parent(row, parent) == (
        parent,
        child,
    )


def _inventory(local, retry, operations):
    instance = object.__new__(recovery.Recovery)
    instance.agent_db = lambda: _ScriptedConnection([[*local], [*retry]])
    instance.hindsight_db = lambda: _ScriptedConnection([[*operations]])
    return instance.operation_inventory()


def test_operation_inventory_accepts_one_exact_completed_parent_child_pair():
    local, retry, operations = _inventory_fixture()
    inventory = _inventory(local, retry, operations)
    assert inventory["failures"] == []
    assert inventory["completed_by_document"] == {"doc": 1}
    assert inventory["failed_pairs"] == []
    assert [value["operation_id"] for value in inventory["manifest"]] == [
        "child-1",
        "parent-1",
    ]


def _active_inventory_fixture(*, status="processing"):
    local, retry, operations = _inventory_fixture(state="submitted", status=status)
    child = operations[1]
    child["result_metadata"].pop("document_ids")
    child["result_metadata"].update(
        {
            "items_count": 1,
            "sub_batch_index": 1,
            "total_sub_batches": 1,
        }
    )
    return local, retry, operations


def test_operation_inventory_accepts_active_child_before_document_ids_outcome():
    for status in ("pending", "processing"):
        local, retry, operations = _active_inventory_fixture(status=status)
        inventory = _inventory(local, retry, operations)
        assert inventory["failures"] == []
        assert inventory["manifest"] == []
        assert inventory["completed_by_document"] == {}
        assert inventory["failed_pairs"] == []


def test_operation_inventory_rejects_present_wrong_active_document_ids():
    local, retry, operations = _active_inventory_fixture()
    operations[1]["result_metadata"]["document_ids"] = ["other"]
    failures = _inventory(local, retry, operations)["failures"]
    assert any("lineage/hash drift child-1" in value for value in failures)
    assert any(
        "does not have exactly one retain child parent-1" in value for value in failures
    )


def test_operation_inventory_active_missing_document_ids_requires_exact_static_fanout():
    cases = (
        ("items_count", 2),
        ("items_count", True),
        ("items_count", 1.9),
        ("items_count", "1"),
        ("sub_batch_index", 2),
        ("sub_batch_index", True),
        ("sub_batch_index", 1.9),
        ("sub_batch_index", "1"),
        ("total_sub_batches", 2),
        ("total_sub_batches", True),
        ("total_sub_batches", 1.9),
        ("total_sub_batches", "1"),
    )
    for field, value in cases:
        local, retry, operations = _active_inventory_fixture()
        operations[1]["result_metadata"][field] = value
        failures = _inventory(local, retry, operations)["failures"]
        assert any("lineage/hash drift child-1" in item for item in failures)

    local, retry, operations = _active_inventory_fixture()
    operations[1]["task_payload"]["type"] = "retain"
    failures = _inventory(local, retry, operations)["failures"]
    assert any("lineage/hash drift child-1" in item for item in failures)


def test_operation_inventory_active_missing_document_ids_requires_exact_parent_fanout():
    cases = (
        ("items_count", True),
        ("items_count", 1.9),
        ("items_count", "1"),
        ("num_sub_batches", True),
        ("num_sub_batches", 1.9),
        ("num_sub_batches", "1"),
    )
    for field, value in cases:
        local, retry, operations = _active_inventory_fixture()
        operations[0]["result_metadata"][field] = value
        failures = _inventory(local, retry, operations)["failures"]
        assert any(
            "parent metadata provenance drift parent-1" in item for item in failures
        )


def test_operation_inventory_rejects_active_child_wrong_hash_or_parent_link():
    local, retry, operations = _active_inventory_fixture()
    operations[1]["task_payload"]["contents"][0]["content"] = "tampered"
    failures = _inventory(local, retry, operations)["failures"]
    assert any("lineage/hash drift child-1" in value for value in failures)

    local, retry, operations = _active_inventory_fixture()
    operations[1]["result_metadata"]["parent_operation_id"] = "missing-parent"
    failures = _inventory(local, retry, operations)["failures"]
    assert any("no exact batch parent child-1" in value for value in failures)
    assert any(
        "does not have exactly one retain child parent-1" in value for value in failures
    )


def test_operation_inventory_rejects_extra_exact_active_child():
    local, retry, operations = _active_inventory_fixture()
    duplicate = json.loads(json.dumps(operations[1]))
    duplicate["operation_id"] = "child-2"
    duplicate["task_payload"]["operation_id"] = "child-2"
    operations.append(duplicate)
    failures = _inventory(local, retry, operations)["failures"]
    assert any(
        "does not have exactly one retain child parent-1" in value for value in failures
    )


def test_operation_inventory_rejects_terminal_child_without_document_ids():
    for status in ("completed", "failed", "cancelled"):
        local, retry, operations = _inventory_fixture(state="submitted", status=status)
        operations[1]["result_metadata"].pop("document_ids")
        failures = _inventory(local, retry, operations)["failures"]
        assert any("lineage/hash drift child-1" in value for value in failures)
        assert any(
            "does not have exactly one retain child parent-1" in value
            for value in failures
        )


def test_operation_inventory_rejects_multiple_completed_pairs_for_document():
    local, retry, operations = _inventory_fixture(duplicate=True)
    inventory = _inventory(local, retry, operations)
    assert inventory["completed_by_document"] == {"doc": 2}
    assert any(
        "multiple completed operation pairs" in value for value in inventory["failures"]
    )


def test_operation_inventory_rejects_unlinked_child_and_unknown_operation():
    local, retry, operations = _inventory_fixture()
    operations[1]["result_metadata"]["parent_operation_id"] = "missing-parent"
    operations.append(
        {
            "operation_id": "unknown",
            "operation_type": "mystery",
            "status": "completed",
            "task_payload": {},
            "result_metadata": {},
            "created_at": "2026-07-14T00:00:03+00:00",
            "updated_at": "2026-07-14T00:00:03+00:00",
        }
    )
    failures = _inventory(local, retry, operations)["failures"]
    assert any("no exact batch parent" in value for value in failures)
    assert any(
        "unknown/duplicate Hindsight operation identity" in value for value in failures
    )


def test_operation_inventory_explains_exact_failed_pair_and_consolidation():
    local, retry, operations = _inventory_fixture(state="blocked", status="failed")
    retry.append({"document_id": "doc", "operation_id": "parent-1"})
    operations.append(
        {
            "operation_id": "consolidation-1",
            "operation_type": "consolidation",
            "status": "failed",
            "error_message": "provider 503",
            "result_metadata": {},
            "task_payload": {
                "bank_id": "agent-sessions",
                "type": "consolidation",
                "operation_id": "consolidation-1",
            },
            "created_at": "2026-07-14T00:00:02+00:00",
            "updated_at": "2026-07-14T00:00:02+00:00",
        }
    )
    inventory = _inventory(local, retry, operations)
    assert inventory["failures"] == []
    assert inventory["failed_pairs"] == [
        {
            "document_id": "doc",
            "parent_operation_id": "parent-1",
            "child_operation_id": "child-1",
            "explained": True,
        }
    ]
    assert {
        (value["operation_id"], value["operation_type"], value["status"])
        for value in inventory["manifest"]
    } == {
        ("parent-1", "batch_retain", "failed"),
        ("child-1", "retain", "failed"),
        ("consolidation-1", "consolidation", "failed"),
    }


def test_completed_refresh_proves_exact_parent_child_and_document():
    item = {
        "document_id": "doc",
        "content": "value",
        "context": "source:agent_sessions | document:doc",
        "tags": ["agent-session"],
        "timestamp": "2026-07-14T00:00:00+00:00",
    }
    digest = recovery.hashlib.sha256(
        json.dumps(
            item, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()

    def operation(operation_id, operation_type, *, parent_id=None):
        metadata = {"document_ids": ["doc"]}
        if operation_type == "batch_retain":
            metadata.update(
                {
                    "is_parent": True,
                    "items_count": 1,
                    "num_sub_batches": 1,
                }
            )
        if parent_id:
            metadata["parent_operation_id"] = parent_id
        return {
            "operation_id": operation_id,
            "operation_type": operation_type,
            "status": "completed",
            "task_payload": {
                "bank_id": "agent-sessions",
                "operation_id": operation_id,
                "type": "batch_retain",
                "contents": [
                    {
                        **{k: v for k, v in item.items() if k != "timestamp"},
                        "event_date": item["timestamp"],
                    }
                ],
            },
            "result_metadata": metadata,
        }

    parent = operation("parent", "batch_retain")
    child = operation("child", "retain", parent_id="parent")
    remote = {
        "id": "doc",
        "original_text": "value",
        "content_hash": recovery.hashlib.sha256(b"value").hexdigest(),
        "retain_params": {
            "context": item["context"],
            "event_date": item["timestamp"],
        },
        "tags": item["tags"],
    }
    instance = object.__new__(recovery.Recovery)
    instance._remote_operation = lambda operation_id: parent
    instance._retain_children_for_parent = lambda operation_id: [child]
    instance._remote_document = lambda document_id: remote
    rows = [
        {
            "document_id": "doc",
            "operation_id": "parent",
            "payload_hash": digest,
            "submitted_payload_hash": digest,
            "item": item,
        }
    ]
    assert instance._prove_completed_submitted_rows(rows) == frozenset(
        {("doc", "parent", "child", digest)}
    )
    child["status"] = "processing"
    try:
        instance._prove_completed_submitted_rows(rows)
    except recovery.InvariantError as exc:
        assert "child lineage" in str(exc)
    else:
        raise AssertionError("non-terminal child was accepted for refresh")


def test_cap_and_precap_refresh_are_always_targeted():
    source = MODULE.read_text()
    assert source.count("completed_lineage_proofs=completed_lineage_proofs") == 2
    assert source.count("only_operations=only_operations") == 2
    assert source.count("completed_lineage_proofs=frozenset(") >= 2
    assert "completed, failed = worker._refresh_submitted(conn)" not in source


def test_pending_completed_refresh_persists_child_before_mutation_and_resumes():
    @dataclass(frozen=True)
    class Proof:
        document_id: str
        parent_operation_id: str
        child_operation_id: str
        payload_hash: str

    class Cursor:
        def __init__(self, conn):
            self.conn = conn
            self.query = ""

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, _params=None):
            self.query = " ".join(query.split())

        def fetchone(self):
            if "SELECT state,payload_hash" in self.query:
                return {
                    "state": self.conn.state,
                    "payload_hash": "payload",
                    "submitted_payload_hash": "payload",
                    "operation_id": "parent",
                    "item": {"document_id": "doc", "content": "value"},
                    "claimed_at": None,
                }
            if "SELECT state,operation_id" in self.query:
                return {"state": self.conn.state, "operation_id": None}
            raise AssertionError(self.query)

    class Connection:
        def __init__(self):
            self.state = "submitted"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return Cursor(self)

    class Worker:
        def __init__(self):
            self.calls = []

        def _refresh_submitted(self, conn, **kwargs):
            self.calls.append(kwargs)
            conn.state = "succeeded"
            return 1, 0

    conn = Connection()
    worker = Worker()
    incident = {
        "pending_acceptance": {
            "kind": "completed_refresh",
            "document_id": "doc",
            "operation_id": "parent",
            "payload_hash": "payload",
            "expected_history_delta": {"adoption": 0},
        }
    }
    parent = {"operation_id": "parent", "status": "completed"}
    child = {"operation_id": "child", "status": "completed"}
    instance = object.__new__(recovery.Recovery)
    instance.cfg = config()
    instance.agent_db = lambda: conn
    instance.bank_lock = lambda: nullcontext(conn)
    instance._reviewed_outbox_module = lambda _path: SimpleNamespace(
        CompletedLineageProof=Proof
    )
    instance._app_worker = lambda: worker
    instance._remote_operation = lambda _operation_id: parent
    instance._prove_completed_batch_parent = lambda _row, _parent: (
        parent,
        child,
    )
    instance._remote_document = lambda _document_id: {"id": "doc"}
    instance._remote_document_matches_item = lambda _remote, _item: True
    instance.accept_recovered_attempt = lambda _attempt: None
    instance.clear_pending_acceptance = lambda value: value.pop(
        "pending_acceptance", None
    )

    def crash_after_persist(_incident, _attempt):
        raise RuntimeError("crash after child proof persistence")

    instance.mark_pending_acceptance = crash_after_persist
    try:
        instance.resume_pending_acceptance(incident)
    except RuntimeError as exc:
        assert "crash after child proof" in str(exc)
    else:
        raise AssertionError("child proof persistence crash was not observed")
    assert incident["pending_acceptance"]["child_operation_id"] == "child"
    assert worker.calls == []

    instance.mark_pending_acceptance = lambda *_args: None
    assert instance.resume_pending_acceptance(incident) is True
    assert worker.calls == [
        {
            "only_operations": frozenset({("doc", "parent")}),
            "completed_lineage_proofs": frozenset(
                {Proof("doc", "parent", "child", "payload")}
            ),
        }
    ]
    assert "pending_acceptance" not in incident


def test_pending_completed_refresh_rejects_persisted_child_drift():
    source = MODULE.read_text()
    assert "pending completed recovery child lineage drift" in source
    assert source.index(
        'attempt["child_operation_id"] = child_operation_id'
    ) < source.index("completed_lineage_proof = outbox.CompletedLineageProof(")


def test_release_polling_rechecks_complete_quick_audit():
    source = MODULE.read_text()
    loop = source[source.index("while time.monotonic() < deadline:") :]
    loop = loop[: loop.index('emergency_open(self.cfg, "release claim did not appear')]
    assert "loop_failures = self.quick_failures(current, loop_phase)" in loop
    assert "release polling audit failed" in loop


def test_reviewed_outbox_build_id_accepts_colon_or_at_sha256(tmp_path):
    source = tmp_path / "outbox.py"
    source.write_text("reviewed\n")
    digest = recovery.hashlib.sha256(source.read_bytes()).hexdigest()
    instance = object.__new__(recovery.Recovery)
    for build_id in (
        f"outbox.py:sha256:{digest}",
        f"outbox.py@sha256:{digest}",
        f"sha256:{digest}",
    ):
        cfg = config()
        object.__setattr__(cfg, "expected_agent_outbox_build_id", build_id)
        instance.cfg = cfg
        assert instance._prove_local_outbox_helper(source) == digest
    cfg = config()
    object.__setattr__(
        cfg,
        "expected_agent_outbox_build_id",
        "outbox.py:sha256:" + "0" * 64,
    )
    object.__setattr__(
        cfg,
        "expected_recovery_outbox_build_id",
        f"outbox.py:sha256:{digest}",
    )
    instance.cfg = cfg
    assert instance._prove_local_outbox_helper(source) == digest
    cfg = config()
    object.__setattr__(
        cfg,
        "expected_agent_outbox_build_id",
        f"outbox.py/sha256:{digest}",
    )
    instance.cfg = cfg
    try:
        instance._prove_local_outbox_helper(source)
    except recovery.InvariantError as exc:
        assert "must end" in str(exc)
    else:
        raise AssertionError("unapproved build-ID separator was accepted")


def test_provider_failure_classification_and_retry_after():
    assert recovery.provider_failure_kind("provider returned 401") == "auth"
    assert recovery.provider_failure_kind("HTTP 429 Retry-After: 17") == "rate_limit"
    assert (
        recovery.provider_failure_kind("ambiguous upstream failure: ReadTimeout")
        == "ambiguous"
    )
    assert recovery.provider_failure_kind("HTTP 503") == "transient_5xx"
    assert recovery.provider_retry_after("Retry-After: 17", 5) == 17
    assert recovery.provider_retry_after("no retry header", 5) == 5


def test_provider_retry_after_parses_rfc_date(monkeypatch):
    monkeypatch.setattr(recovery.time, "time", lambda: 0.0)
    assert (
        recovery.provider_retry_after("Retry-After: Thu, 01 Jan 1970 00:00:10 GMT", 5)
        == 10
    )
    assert recovery.provider_retry_after("Retry-After: not-a-date", 5) == 5
    assert (
        recovery.provider_retry_after("Retry-After: Wed, 21 Oct 2099 07:28:00 GMT", 5)
        > 0
    )
    assert recovery.bounded_backoff(9, 5, 300) == 300


def test_watchdog_requires_controller_when_stage_is_configured():
    assert (
        recovery.watchdog_failure(
            armed=True,
            heartbeat_age=1,
            tunnel_ports_ready=True,
            guard_active=True,
            tunnel_active=True,
            max_age=7,
            controller_required=True,
            controller_heartbeat_age=None,
            controller_active=False,
        )
        == "controller heartbeat missing or stale"
    )


def test_public_cli_has_no_phase_or_release_command():
    source = MODULE.read_text()
    assert 'sub.add_parser("phase")' not in source
    assert 'sub.add_parser("release")' not in source
    assert 'sub.add_parser("controller")' in source
    assert 'sub.add_parser("configure-stage")' in source
    assert 'sub.add_parser("incident-status")' in source
    assert 'sub.add_parser("refresh-retry")' not in source
    assert 'sub.add_parser("recover-provider-failures")' not in source
    assert 'sub.add_parser("prepare-resume")' not in source


def _completed_refresh_attempt(
    *, document_id="doc", operation_id="op", payload_hash="payload", accepted_at=20.0
):
    return {
        "key": f"completed:{document_id}:{operation_id}",
        "kind": "completed_refresh",
        "document_id": document_id,
        "operation_id": operation_id,
        "payload_hash": payload_hash,
        "remote_status": "completed",
        "remote_document_present": True,
        "expected_history_delta": {"retry": 0, "claim": 0, "adoption": 0},
        "accepted_at": accepted_at,
    }


def _completed_refresh_checkpoint(*attempts):
    return {
        "revision": 7,
        "accepted": {"fugu_failures": [], "fugu_failure_manifest": []},
        "recovered_attempts": list(attempts),
    }


def _completed_refresh_authorization():
    return {
        "row_lineages": [
            {
                "document_id": "doc",
                "operation_id": "op",
                "payload_hash": "payload",
            }
        ],
        "terminal_pending_child_operation_ids": [],
        "authorized_failure_operation_ids": [],
        "authorized_fugu_failures": [],
        "authorized_fugu_request_ids": [],
        "authorized_provider_failures": [],
        "authorized_provider_failure_ids": [],
        "provider_statuses": [],
    }


def test_completed_refresh_archive_proof_is_exact_and_non_spending():
    attempt = _completed_refresh_attempt()
    checkpoint = _completed_refresh_checkpoint(attempt)
    proof = recovery.completed_refresh_archive_proof(
        _completed_refresh_authorization(), checkpoint
    )
    assert proof == {
        "version": 1,
        "count": 1,
        "attempt_keys": [attempt["key"]],
        "lineage_sha256": proof["lineage_sha256"],
        "checkpoint_revision": 7,
        "checkpoint_sha256": recovery._json_digest(checkpoint),
    }

    terminal = dict(attempt, kind="terminal_retry")
    assert (
        recovery.completed_refresh_archive_proof(
            _completed_refresh_authorization(),
            _completed_refresh_checkpoint(terminal),
        )
        is None
    )
    provider = _completed_refresh_authorization()
    provider["provider_statuses"] = [503]
    assert recovery.completed_refresh_archive_proof(provider, checkpoint) is None
    adopted = json.loads(json.dumps(attempt))
    adopted["expected_history_delta"]["adoption"] = 1
    assert (
        recovery.completed_refresh_archive_proof(
            _completed_refresh_authorization(),
            _completed_refresh_checkpoint(adopted),
        )
        is None
    )


def test_post_incident_release_evidence_requires_verified_unconsumed_archive():
    attempt = _completed_refresh_attempt()
    checkpoint = _completed_refresh_checkpoint(attempt)
    proof = recovery.completed_refresh_archive_proof(
        _completed_refresh_authorization(), checkpoint
    )
    incident = {
        "authorization_archive": [
            {
                "authorization_sha256": "auth",
                "row_sha256": "rows",
                "closed_at": 21.0,
                "reason": "all authorized incident rows reconciled",
                "completed_refresh_proof": proof,
            }
        ],
        "absence_proofs": {},
    }
    phase = {"initial_wave_verified": True}
    evidence = recovery.post_incident_release_evidence(phase, incident, checkpoint)
    assert evidence["authorization_sha256"] == "auth"
    assert evidence["attempt_keys"] == [attempt["key"]]

    assert (
        recovery.post_incident_release_evidence(
            {"initial_wave_verified": False}, incident, checkpoint
        )
        is None
    )
    assert (
        recovery.post_incident_release_evidence(
            {
                "initial_wave_verified": True,
                "last_post_incident_release_authorization_sha256": "auth",
            },
            incident,
            checkpoint,
        )
        is None
    )
    assert (
        recovery.post_incident_release_evidence(
            {"initial_wave_verified": True, "released_at": 22.0},
            incident,
            checkpoint,
        )
        is None
    )
    changed = json.loads(json.dumps(checkpoint))
    changed["revision"] = 8
    assert recovery.post_incident_release_evidence(phase, incident, changed) is None
    for field, value in (
        ("expected_history_delta", {"retry": 0, "claim": 0, "adoption": 1}),
        ("authorized_provider_failure_ids", ["provider"]),
    ):
        tainted_checkpoint = json.loads(json.dumps(checkpoint))
        tainted_checkpoint["recovered_attempts"][0][field] = value
        intermediate_proof = json.loads(json.dumps(proof))
        intermediate_proof["checkpoint_sha256"] = recovery._json_digest(
            tainted_checkpoint
        )
        tainted_incident = json.loads(json.dumps(incident))
        tainted_incident["authorization_archive"][0]["completed_refresh_proof"] = (
            intermediate_proof
        )
        assert (
            recovery.post_incident_release_evidence(
                phase,
                tainted_incident,
                tainted_checkpoint,
            )
            is None
        ), field


def test_post_incident_release_legacy_window_is_bounded_to_latest_archive():
    older = _completed_refresh_attempt(
        document_id="older", operation_id="older-op", accepted_at=5.0
    )
    current = _completed_refresh_attempt(accepted_at=20.0)
    checkpoint = _completed_refresh_checkpoint(older, current)
    incident = {
        "last_result": {"status": "reconciled", "count": 1},
        "authorization_archive": [
            {
                "authorization_sha256": "old-auth",
                "row_sha256": "old-rows",
                "closed_at": 10.0,
                "reason": "all authorized incident rows reconciled",
            },
            {
                "authorization_sha256": "current-auth",
                "row_sha256": "current-rows",
                "closed_at": 21.0,
                "reason": "all authorized incident rows reconciled",
            },
        ],
        "absence_proofs": {},
    }
    evidence = recovery.post_incident_release_evidence(
        {"initial_wave_verified": True}, incident, checkpoint
    )
    assert evidence["attempt_keys"] == [current["key"]]
    adopted_refresh_checkpoint = json.loads(json.dumps(checkpoint))
    adopted_refresh_checkpoint["recovered_attempts"][-1]["expected_history_delta"][
        "adoption"
    ] = 1
    assert (
        recovery.post_incident_release_evidence(
            {"initial_wave_verified": True},
            incident,
            adopted_refresh_checkpoint,
        )
        is None
    )
    for field, value in (
        ("authorized_failure_operation_ids", ["failed-op"]),
        ("authorized_fugu_failures", [{"request_id": "fugu"}]),
        ("authorized_fugu_request_ids", ["fugu"]),
        ("authorized_provider_failures", [{"request_id": "provider"}]),
        ("authorized_provider_failure_ids", ["provider"]),
        ("provider_recovery_proof", {"kind": "auth"}),
        ("provider_statuses", [401]),
        ("allowed_http_statuses", [401]),
        ("allow_null_http_status", True),
    ):
        spending_checkpoint = json.loads(json.dumps(checkpoint))
        spending_checkpoint["recovered_attempts"][-1][field] = value
        assert (
            recovery.post_incident_release_evidence(
                {"initial_wave_verified": True},
                incident,
                spending_checkpoint,
            )
            is None
        ), field
    checkpoint["recovered_attempts"].append(
        dict(current, key="adopted:other", kind="operation_adoption")
    )
    assert (
        recovery.post_incident_release_evidence(
            {"initial_wave_verified": True}, incident, checkpoint
        )
        is None
    )


def _post_incident_budget_recovery():
    return {
        "total_max_usd": 0,
        "sha256": "a" * 64,
        "provider_ledger_version": recovery.PROVIDER_LEDGER_VERSION,
        "provider_failure_manifest": [],
        "provider_call_manifest": [],
        "last_ambiguous_reconciliation": None,
    }


def _post_incident_service_receipt(*, controller_pid=101, restarts=0):
    return {
        service: {
            "ActiveState": "active",
            "SubState": "running",
            "MainPID": controller_pid + index,
            "NRestarts": restarts,
        }
        for index, service in enumerate(recovery.POST_INCIDENT_RELEASE_SERVICES)
    }


def _post_incident_fugu_failure_receipt():
    return {
        "count": 0,
        "summary_sha256": "b" * 64,
        "manifest_sha256": "c" * 64,
    }


def _post_incident_audit():
    service_receipt = _post_incident_service_receipt()
    return {
        "failures": [],
        "agent": {
            "counts": {"queued": 1609, "succeeded": 3533},
            "circuit": {"circuit_open": True},
        },
        "pocket": {"circuit": {"circuit_open": True}},
        "hindsight_operations": {
            "pending": 0,
            "processing": 0,
            "completed": 8455,
            "failed": 64,
            "cancelled": 0,
        },
        "budget": {
            "in_flight": 0,
            "hindsight_backfill_spent_usd": "17.672",
        },
        "budget_recovery": _post_incident_budget_recovery(),
        "hindsight_documents": 3533,
        "remote_documents": 3533,
        "services": {
            service: {**value, "returncode": 0}
            for service, value in service_receipt.items()
        },
    }


def _post_incident_release_fixture(monkeypatch):
    attempt = _completed_refresh_attempt()
    checkpoint = _completed_refresh_checkpoint(attempt)
    proof = recovery.completed_refresh_archive_proof(
        _completed_refresh_authorization(), checkpoint
    )
    incident = {
        "authorization_archive": [
            {
                "authorization_sha256": "auth",
                "row_sha256": "rows",
                "closed_at": 21.0,
                "reason": "all authorized incident rows reconciled",
                "completed_refresh_proof": proof,
            }
        ],
        "absence_proofs": {},
    }
    phase = {
        **recovery.DEFAULT_PHASE,
        "name": "running",
        "budget": "closed",
        "initial_wave_verified": True,
        "expected_succeeded": 3533,
    }
    instance = object.__new__(recovery.Recovery)
    instance.cfg = config()
    audit = _post_incident_audit()
    checkpoint_box = {"value": checkpoint}
    incident_box = {"value": incident}
    service_box = {"value": _post_incident_service_receipt()}
    fugu_box = {"value": _post_incident_fugu_failure_receipt()}
    holds = []
    closes = []
    full_calls = []
    monkeypatch.setattr(recovery, "load_phase", lambda _cfg: phase)
    monkeypatch.setattr(recovery, "load_incident", lambda _cfg: incident_box["value"])
    monkeypatch.setattr(
        recovery, "read_checkpoint", lambda _cfg: checkpoint_box["value"]
    )
    monkeypatch.setattr(recovery, "save_phase", lambda _cfg, _phase: None)
    monkeypatch.setattr(recovery, "event", lambda *_args, **_kwargs: None)
    instance.full_audit = lambda _phase: full_calls.append(1) or audit
    instance.quick_snapshot = lambda: json.loads(json.dumps(audit))
    instance.quick_failures = lambda *_args: []
    instance.post_incident_service_receipt = lambda: service_box["value"]
    instance.post_incident_fugu_failure_receipt = lambda _checkpoint: fugu_box["value"]
    instance.persist_agent_open = lambda reason: holds.append(("agent", reason))
    instance.ensure_pocket_open = lambda reason: holds.append(("pocket", reason))
    instance.close_agent = lambda: closes.append(1)
    assert instance.prepare_post_incident_release() is audit
    return SimpleNamespace(
        instance=instance,
        phase=phase,
        audit=audit,
        checkpoint_box=checkpoint_box,
        incident_box=incident_box,
        service_box=service_box,
        fugu_box=fugu_box,
        holds=holds,
        closes=closes,
        full_calls=full_calls,
    )


def test_post_incident_fugu_failure_receipt_binds_summary_and_exact_record():
    row = {
        "request_id": "request",
        "status": "failed",
        "http_status": 502,
        "requested_model": "openai/gpt-oss-120b",
        "session_id": "session",
        "agent_id": "agent",
        "agent_role": "role",
        "start_time": datetime(2026, 7, 13, 10, 17, 39, tzinfo=timezone.utc),
    }
    summary = [
        row["start_time"].isoformat(),
        502,
        "openai/gpt-oss-120b",
    ]
    manifest = recovery._exact_record(
        row,
        "request_id",
        "status",
        "http_status",
        "requested_model",
        "session_id",
        "agent_id",
        "agent_role",
    )
    checkpoint = {
        "accepted": {
            "fugu_failures": [summary],
            "fugu_failure_manifest": [manifest],
        }
    }
    instance = object.__new__(recovery.Recovery)
    instance.fugu_db = lambda: _ScriptedConnection([[row]])
    receipt = instance.post_incident_fugu_failure_receipt(checkpoint)
    assert receipt == {
        "count": 1,
        "summary_sha256": recovery._json_digest({"values": [summary]}),
        "manifest_sha256": recovery._json_digest({"values": [manifest]}),
    }

    changed = json.loads(json.dumps(checkpoint))
    changed["accepted"]["fugu_failures"] = []
    try:
        instance.post_incident_fugu_failure_receipt(changed)
    except recovery.InvariantError as exc:
        assert "authenticated Fugu failure ledger drift" in str(exc)
    else:
        raise AssertionError("checkpoint/Fugu failure mismatch was accepted")


def test_completed_refresh_release_uses_one_full_and_two_quick_proofs(
    monkeypatch,
):
    attempt = _completed_refresh_attempt()
    checkpoint = _completed_refresh_checkpoint(attempt)
    proof = recovery.completed_refresh_archive_proof(
        _completed_refresh_authorization(), checkpoint
    )
    incident = {
        "authorization_archive": [
            {
                "authorization_sha256": "auth",
                "row_sha256": "rows",
                "closed_at": 21.0,
                "reason": "all authorized incident rows reconciled",
                "completed_refresh_proof": proof,
            }
        ],
        "absence_proofs": {},
    }
    phase = {
        **recovery.DEFAULT_PHASE,
        "name": "running",
        "budget": "closed",
        "initial_wave_verified": True,
        "expected_succeeded": 3533,
    }
    instance = object.__new__(recovery.Recovery)
    instance.cfg = config()
    audit = _post_incident_audit()
    full_calls = []
    monkeypatch.setattr(recovery, "load_phase", lambda _cfg: phase)
    monkeypatch.setattr(recovery, "load_incident", lambda _cfg: incident)
    monkeypatch.setattr(recovery, "read_checkpoint", lambda _cfg: checkpoint)
    monkeypatch.setattr(recovery, "save_phase", lambda _cfg, _phase: None)
    monkeypatch.setattr(recovery, "event", lambda *_args, **_kwargs: None)
    instance.full_audit = lambda _phase: full_calls.append(1) or audit
    service_receipt = _post_incident_service_receipt()
    instance.post_incident_service_receipt = lambda: service_receipt
    instance.post_incident_fugu_failure_receipt = lambda _checkpoint: (
        _post_incident_fugu_failure_receipt()
    )

    assert instance.prepare_post_incident_release() is audit
    assert phase["name"] == "ready"
    assert phase["post_incident_release"]["authorization_sha256"] == "auth"

    baseline = json.loads(json.dumps(audit))
    claimed = {
        "agent": {
            "counts": {"queued": 1607, "submitting": 2, "succeeded": 3533},
            "circuit": {"circuit_open": False},
        },
        "hindsight_operations": {"pending": 2, "processing": 0},
        "budget": {"in_flight": 2},
    }
    snapshots = iter((baseline, baseline, claimed))
    instance.quick_snapshot = lambda: next(snapshots)
    instance.quick_failures = lambda *_args: []
    instance.close_agent = lambda: None
    stability_sleeps = []
    instance.sleep_with_heartbeat = lambda *args: stability_sleeps.append(args)
    result = instance.release()

    assert result["release_gate_status"] == "verified"
    assert len(full_calls) == 1
    assert stability_sleeps == []
    assert phase["name"] == "running"
    assert "post_incident_release" not in phase
    assert phase["last_post_incident_release_authorization_sha256"] == "auth"


def test_disproved_post_incident_ticket_retires_to_full_gate(monkeypatch):
    attempt = _completed_refresh_attempt()
    checkpoint = _completed_refresh_checkpoint(attempt)
    proof = recovery.completed_refresh_archive_proof(
        _completed_refresh_authorization(), checkpoint
    )
    incident = {
        "authorization_archive": [
            {
                "authorization_sha256": "auth",
                "row_sha256": "rows",
                "closed_at": 21.0,
                "reason": "all authorized incident rows reconciled",
                "completed_refresh_proof": proof,
            }
        ],
        "absence_proofs": {},
    }
    phase = {
        **recovery.DEFAULT_PHASE,
        "name": "running",
        "budget": "closed",
        "initial_wave_verified": True,
        "expected_succeeded": 3533,
    }
    instance = object.__new__(recovery.Recovery)
    instance.cfg = config()
    good = _post_incident_audit()
    drifted = json.loads(json.dumps(good))
    drifted["budget"]["hindsight_backfill_spent_usd"] = "17.673"
    holds = []
    monkeypatch.setattr(recovery, "load_phase", lambda _cfg: phase)
    monkeypatch.setattr(recovery, "load_incident", lambda _cfg: incident)
    monkeypatch.setattr(recovery, "read_checkpoint", lambda _cfg: checkpoint)
    monkeypatch.setattr(recovery, "save_phase", lambda _cfg, _phase: None)
    monkeypatch.setattr(recovery, "event", lambda *_args, **_kwargs: None)
    instance.full_audit = lambda _phase: good
    instance.quick_snapshot = lambda: drifted
    instance.quick_failures = lambda *_args: []
    instance.post_incident_service_receipt = lambda: _post_incident_service_receipt()
    instance.post_incident_fugu_failure_receipt = lambda _checkpoint: (
        _post_incident_fugu_failure_receipt()
    )
    instance.persist_agent_open = lambda reason: holds.append(("agent", reason))
    instance.ensure_pocket_open = lambda reason: holds.append(("pocket", reason))

    assert instance.prepare_post_incident_release() is good
    try:
        instance.release()
    except recovery.PostIncidentReleaseInvalid as exc:
        assert "audit/count/spend/projection drift" in str(exc)
    else:
        raise AssertionError("drifted post-incident ticket was released")
    assert [name for name, _reason in holds] == ["agent", "pocket"]
    assert phase["name"] == "ready"
    assert "post_incident_release" not in phase
    assert phase["last_post_incident_release_outcome"] == "invalidated"
    assert instance.post_incident_release_candidate() is False


def test_post_incident_ticket_rejects_terminal_status_drift_between_proofs(
    monkeypatch,
):
    context = _post_incident_release_fixture(monkeypatch)
    drifted = json.loads(json.dumps(context.audit))
    drifted["hindsight_operations"]["cancelled"] += 1
    context.instance.quick_snapshot = lambda: drifted
    context.instance.quick_failures = lambda *_args: []

    try:
        context.instance.release()
    except recovery.PostIncidentReleaseInvalid as exc:
        assert "Hindsight operation status drift" in str(exc)
    else:
        raise AssertionError("terminal operation drift crossed abbreviated release")
    assert context.closes == []
    assert context.phase["last_post_incident_release_outcome"] == "invalidated"


def test_post_incident_ticket_ttl_is_bounded_and_exact(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(recovery.time, "time", lambda: clock[0])
    context = _post_incident_release_fixture(monkeypatch)
    ticket = context.phase["post_incident_release"]
    assert context.instance.cfg.heartbeat_max_age == 7
    assert ticket["ttl_seconds"] == recovery.POST_INCIDENT_RELEASE_TTL_SECONDS == 300
    assert ticket["expires_at"] == 400.0

    at_boundary = json.loads(json.dumps(context.audit))
    at_boundary["failures"] = []
    clock[0] = 400.0
    context.instance.validate_post_incident_release(context.phase, at_boundary)

    clock[0] = 400.001
    try:
        context.instance.release()
    except recovery.PostIncidentReleaseInvalid as exc:
        assert "expired or has timing drift" in str(exc)
    else:
        raise AssertionError("expired post-incident ticket was released")
    assert context.closes == []
    assert context.phase["last_post_incident_release_outcome"] == "invalidated"


def test_post_incident_ticket_remains_valid_after_old_sixty_second_window(
    monkeypatch,
):
    clock = [100.0]
    monkeypatch.setattr(recovery.time, "time", lambda: clock[0])
    context = _post_incident_release_fixture(monkeypatch)
    snap = json.loads(json.dumps(context.audit))
    snap["failures"] = []

    # The former 60-second ticket expired under legitimate serialized-read
    # contention.  The bounded 300-second ticket must still admit the exact
    # receipt-bound proof immediately after that old window.
    clock[0] = 160.001
    context.instance.validate_post_incident_release(context.phase, snap)

    assert "post_incident_release" in context.phase
    assert context.holds == []
    assert context.closes == []


def test_post_incident_ticket_after_three_hundred_seconds_falls_back_closed(
    monkeypatch,
):
    clock = [100.0]
    monkeypatch.setattr(recovery.time, "time", lambda: clock[0])
    context = _post_incident_release_fixture(monkeypatch)

    clock[0] = 400.001
    try:
        context.instance.release()
    except recovery.PostIncidentReleaseInvalid as exc:
        assert "expired or has timing drift" in str(exc)
    else:
        raise AssertionError("expired post-incident ticket was released")

    assert context.closes == []
    assert [name for name, _reason in context.holds] == ["agent", "pocket"]
    assert context.phase["name"] == "ready"
    assert "post_incident_release" not in context.phase
    assert context.phase["last_post_incident_release_outcome"] == "invalidated"
    assert context.instance.post_incident_release_candidate() is False


def test_post_incident_service_receipt_rejects_every_identity_field(monkeypatch):
    drift_values = {
        "ActiveState": "inactive",
        "SubState": "exited",
        "MainPID": 999999,
        "NRestarts": 1,
    }
    for service in recovery.POST_INCIDENT_RELEASE_SERVICES:
        for field, value in drift_values.items():
            context = _post_incident_release_fixture(monkeypatch)
            drifted = json.loads(json.dumps(context.service_box["value"]))
            drifted[service][field] = value
            context.service_box["value"] = drifted
            try:
                context.instance.release()
            except recovery.PostIncidentReleaseInvalid as exc:
                assert "service identity/restart drift" in str(exc)
            else:
                raise AssertionError(
                    f"service receipt drift released: {service}/{field}"
                )
            assert context.closes == []
            assert context.phase["last_post_incident_release_outcome"] == "invalidated"


def test_post_incident_read_failures_retire_ticket_before_close(monkeypatch):
    for source in ("quick", "service", "fugu"):
        context = _post_incident_release_fixture(monkeypatch)

        def failed_read():
            raise recovery.InvariantError(f"{source} authoritative read unavailable")

        if source == "quick":
            context.instance.quick_snapshot = failed_read
        elif source == "service":
            context.instance.post_incident_service_receipt = failed_read
        else:
            context.instance.post_incident_fugu_failure_receipt = lambda _checkpoint: (
                failed_read()
            )
        try:
            context.instance.release()
        except recovery.PostIncidentReleaseInvalid as exc:
            assert "read" in str(exc) or "service" in str(exc)
        else:
            raise AssertionError(f"{source} read failure released Agent")
        assert context.closes == []
        assert [name for name, _reason in context.holds] == ["agent", "pocket"]


def test_post_incident_fugu_failure_ledger_drift_retires_ticket(monkeypatch):
    context = _post_incident_release_fixture(monkeypatch)
    context.fugu_box["value"] = {
        "count": 1,
        "summary_sha256": "d" * 64,
        "manifest_sha256": "e" * 64,
    }
    try:
        context.instance.release()
    except recovery.PostIncidentReleaseInvalid as exc:
        assert "Fugu failure receipt drift" in str(exc)
    else:
        raise AssertionError("new authenticated Fugu failure released Agent")
    assert context.closes == []
    assert context.phase["last_post_incident_release_outcome"] == "invalidated"


def test_post_incident_ticket_rejects_digest_checkpoint_and_archive_drift(monkeypatch):
    for drift in ("ticket", "checkpoint", "archive"):
        context = _post_incident_release_fixture(monkeypatch)
        if drift == "ticket":
            context.phase["post_incident_release"]["queued"] -= 1
        elif drift == "checkpoint":
            changed = json.loads(json.dumps(context.checkpoint_box["value"]))
            changed["revision"] += 1
            context.checkpoint_box["value"] = changed
        else:
            changed = json.loads(json.dumps(context.incident_box["value"]))
            changed["authorization_archive"][-1]["row_sha256"] = "changed"
            context.incident_box["value"] = changed
        try:
            context.instance.release()
        except recovery.PostIncidentReleaseInvalid:
            pass
        else:
            raise AssertionError(f"{drift} drift released Agent")
        assert context.closes == []
        assert context.phase["last_post_incident_release_outcome"] == "invalidated"


def test_post_incident_quick_gate_rejects_all_release_state_drift(monkeypatch):
    def mutate_active(value):
        value["agent"]["counts"] = {
            "queued": 1608,
            "submitting": 1,
            "succeeded": 3533,
        }

    mutators = {
        "agent circuit": lambda value: value["agent"]["circuit"].update(
            circuit_open=False
        ),
        "pocket circuit": lambda value: value["pocket"]["circuit"].update(
            circuit_open=False
        ),
        "active Agent row": mutate_active,
        "queued count": lambda value: value["agent"]["counts"].update(queued=1608),
        "Hindsight activity": lambda value: value["hindsight_operations"].update(
            pending=1
        ),
        "provider activity": lambda value: value["budget"].update(in_flight=1),
        "remote document count": lambda value: value.update(
            remote_documents=3532, hindsight_documents=3532
        ),
        "spend/projection": lambda value: value["budget"].update(
            hindsight_backfill_spent_usd="29.99"
        ),
        "budget ledger": lambda value: value["budget_recovery"][
            "provider_call_manifest"
        ].append({"sha256": "changed"}),
    }
    for label, mutate in mutators.items():
        context = _post_incident_release_fixture(monkeypatch)
        drifted = json.loads(json.dumps(context.audit))
        mutate(drifted)
        context.instance.quick_snapshot = lambda value=drifted: value
        try:
            context.instance.release()
        except recovery.PostIncidentReleaseInvalid:
            pass
        else:
            raise AssertionError(f"{label} drift released Agent")
        assert context.closes == []
        assert context.phase["last_post_incident_release_outcome"] == "invalidated"


def test_release_verifying_restart_with_zero_delta_requires_fresh_receipt(monkeypatch):
    context = _post_incident_release_fixture(monkeypatch)
    context.phase.update(
        {
            "name": "release_verifying",
            "release_baseline_attempted": 3533,
            "release_target": 2,
            "release_started_at": 1.0,
        }
    )
    restarted = json.loads(json.dumps(context.service_box["value"]))
    controller = recovery.POST_INCIDENT_RELEASE_SERVICES[0]
    restarted[controller]["MainPID"] += 1000
    context.service_box["value"] = restarted
    try:
        context.instance.release()
    except recovery.PostIncidentReleaseInvalid as exc:
        assert "service identity/restart drift" in str(exc)
    else:
        raise AssertionError("delta-zero restart reused stale release ticket")
    assert context.closes == []
    assert context.phase["name"] == "ready"
    assert "release_baseline_attempted" not in context.phase
    assert "release_target" not in context.phase
    assert context.phase["last_post_incident_release_outcome"] == "invalidated"


def test_staged_helper_transition_blocks_every_release_entry_and_holds_banks(
    monkeypatch,
):
    cfg = config()
    object.__setattr__(
        cfg,
        "expected_recovery_outbox_build_id",
        "outbox.py:sha256:" + "1" * 64,
    )

    # Normal ready release and restart-resume release both stop before even
    # reading their durable phase.  This also covers the full and abbreviated
    # post-incident release gates because they share this entry point.
    for phase_name in ("ready", "release_verifying"):
        instance = object.__new__(recovery.Recovery)
        instance.cfg = cfg
        instance.controller_owned = True
        holds = []
        instance.persist_agent_open = lambda reason: holds.append(("agent", reason))
        instance.ensure_pocket_open = lambda reason: holds.append(("pocket", reason))
        monkeypatch.setattr(
            recovery,
            "load_phase",
            lambda _cfg, name=phase_name: (_ for _ in ()).throw(
                AssertionError(f"{name} phase was read before identity hold")
            ),
        )
        try:
            instance.release()
        except recovery.InvariantError as exc:
            assert str(exc) == recovery.STAGED_RECOVERY_HELPER_FAILURE
        else:
            raise AssertionError(f"{phase_name} release accepted divergent helpers")
        assert [name for name, _reason in holds] == ["agent", "pocket"]

    # Both preparation paths are independently gated, so the controller
    # cannot advance running -> ready or mint a post-incident fast ticket.
    for method_name in (
        "prepare_open_running_hold",
        "prepare_post_incident_release",
    ):
        instance = object.__new__(recovery.Recovery)
        instance.cfg = cfg
        instance.controller_owned = True
        holds = []
        instance.persist_agent_open = lambda reason: holds.append(("agent", reason))
        instance.ensure_pocket_open = lambda reason: holds.append(("pocket", reason))
        try:
            getattr(instance, method_name)()
        except recovery.InvariantError as exc:
            assert str(exc) == recovery.STAGED_RECOVERY_HELPER_FAILURE
        else:
            raise AssertionError(f"{method_name} accepted divergent helpers")
        assert [name for name, _reason in holds] == ["agent", "pocket"]

    # The only circuit-close transaction repeats the invariant adjacent to
    # mutation as final defense.
    instance = object.__new__(recovery.Recovery)
    instance.cfg = cfg
    instance.controller_owned = True
    holds = []
    instance.persist_agent_open = lambda reason: holds.append(("agent", reason))
    instance.ensure_pocket_open = lambda reason: holds.append(("pocket", reason))
    try:
        instance.close_agent()
    except recovery.InvariantError as exc:
        assert str(exc) == recovery.STAGED_RECOVERY_HELPER_FAILURE
    else:
        raise AssertionError("close_agent accepted divergent helpers")
    assert [name for name, _reason in holds] == ["agent", "pocket"]


def test_invalidated_fast_ticket_falls_back_to_ten_checks_and_two_full_audits(
    monkeypatch,
):
    context = _post_incident_release_fixture(monkeypatch)
    drifted_receipt = json.loads(json.dumps(context.service_box["value"]))
    drifted_receipt[recovery.POST_INCIDENT_RELEASE_SERVICES[1]]["NRestarts"] += 1
    context.service_box["value"] = drifted_receipt
    try:
        context.instance.release()
    except recovery.PostIncidentReleaseInvalid:
        pass
    else:
        raise AssertionError("service drift did not retire the fast ticket")

    baseline = json.loads(json.dumps(context.audit))
    claimed = {
        "agent": {
            "counts": {"queued": 1607, "submitting": 2, "succeeded": 3533},
            "circuit": {"circuit_open": False},
        },
        "hindsight_operations": {"pending": 2, "processing": 0},
        "budget": {"in_flight": 2},
    }
    snapshots = iter([baseline] * 11 + [claimed])
    fallback_full_calls = []
    sleeps = []
    context.instance.quick_snapshot = lambda: next(snapshots)
    context.instance.full_audit = lambda _phase: (
        fallback_full_calls.append(1) or {"failures": []}
    )
    context.instance.sleep_with_heartbeat = lambda *args: sleeps.append(args)

    result = context.instance.release()
    assert result["release_gate_status"] == "verified"
    assert len(fallback_full_calls) == 2
    assert len(sleeps) == 9
    assert context.closes == [1]


def test_orphan_incident_authorization_is_finalized_before_action_selection():
    instance = object.__new__(recovery.Recovery)
    instance.cfg = config()
    incident = {"incident_authorization": {"row_lineages": [{"x": 1}]}}
    calls = []
    instance.persist_agent_open = lambda reason: calls.append(("agent", reason))
    instance.ensure_pocket_open = lambda reason: calls.append(("pocket", reason))
    instance.recover_incident_rows = lambda value: (
        calls.append(("recover", value)) or {"status": "reconciled", "count": 1}
    )
    result = instance.finalize_drained_incident_authorization(
        incident,
        active=0,
        errors=0,
        hindsight_active=0,
        provider_in_flight=0,
    )
    assert result == {"status": "reconciled", "count": 1}
    assert [name for name, _value in calls] == ["agent", "pocket", "recover"]
    assert (
        instance.finalize_drained_incident_authorization(
            incident,
            active=0,
            errors=0,
            hindsight_active=1,
            provider_in_flight=0,
        )
        is None
    )
    loop = MODULE.read_text().split("def run_controller", 1)[1]
    assert loop.index("finalize_drained_incident_authorization(") < loop.index(
        "action = controller_action("
    )


def test_restart_verified_release_consumes_ticket_without_second_close(monkeypatch):
    ticket = {
        "version": 1,
        "authorization_sha256": "auth",
        "ticket_sha256": "not-revalidated-after-durable-release-start",
    }
    phase = {
        **recovery.DEFAULT_PHASE,
        "name": "release_verifying",
        "budget": "closed",
        "initial_wave_verified": True,
        "release_baseline_attempted": 3533,
        "release_target": 2,
        "post_incident_release": ticket,
    }
    instance = object.__new__(recovery.Recovery)
    instance.cfg = config()
    current = {
        "agent": {
            "counts": {"queued": 1607, "succeeded": 3535},
            "circuit": {"circuit_open": True},
        },
        "hindsight_operations": {"pending": 0, "processing": 0},
        "budget": {"in_flight": 0},
    }
    closes = []
    monkeypatch.setattr(recovery, "load_phase", lambda _cfg: phase)
    monkeypatch.setattr(recovery, "save_phase", lambda _cfg, _phase: None)
    monkeypatch.setattr(recovery, "event", lambda *_args, **_kwargs: None)
    instance.quick_snapshot = lambda: current
    instance.quick_failures = lambda *_args: []
    instance.close_agent = lambda: closes.append(1)

    result = instance.release()
    assert result["release_gate_status"] == "verified_after_restart"
    assert closes == []
    assert phase["name"] == "running"
    assert "post_incident_release" not in phase
    assert phase["last_post_incident_release_outcome"] == "verified_after_restart"
    try:
        instance.release()
    except recovery.InvariantError as exc:
        assert "ready or restart-verifying" in str(exc)
    else:
        raise AssertionError("verified restart release granted a second window")


def test_initial_release_still_runs_ten_checks_and_two_full_audits(monkeypatch):
    phase = {
        **recovery.DEFAULT_PHASE,
        "name": "ready",
        "budget": "closed",
        "initial_wave_verified": False,
        "expected_succeeded": 3533,
    }
    instance = object.__new__(recovery.Recovery)
    instance.cfg = config()
    baseline = {
        "agent": {
            "counts": {"queued": 1609, "succeeded": 3533},
            "circuit": {"circuit_open": True},
        },
        "hindsight_operations": {"pending": 0, "processing": 0},
        "budget": {
            "in_flight": 0,
            "hindsight_backfill_spent_usd": "17.672",
        },
    }
    claimed = {
        "agent": {
            "counts": {"queued": 1607, "submitting": 2, "succeeded": 3533},
            "circuit": {"circuit_open": False},
        },
        "hindsight_operations": {"pending": 2, "processing": 0},
        "budget": {"in_flight": 2},
    }
    snapshots = iter([baseline] * 11 + [claimed])
    quick_calls = []

    def quick_snapshot():
        quick_calls.append(1)
        return next(snapshots)

    full_calls = []
    sleeps = []
    monkeypatch.setattr(recovery, "load_phase", lambda _cfg: phase)
    monkeypatch.setattr(recovery, "save_phase", lambda _cfg, _phase: None)
    monkeypatch.setattr(recovery, "event", lambda *_args, **_kwargs: None)
    instance.quick_snapshot = quick_snapshot
    instance.quick_failures = lambda *_args: []
    instance.full_audit = lambda _phase: full_calls.append(1) or {"failures": []}
    instance.sleep_with_heartbeat = lambda *args: sleeps.append(args)
    instance.close_agent = lambda: None

    result = instance.release()
    assert result["release_gate_status"] == "verified"
    assert len(quick_calls) == 12
    assert len(full_calls) == 2
    assert len(sleeps) == 9


def _spot_snapshot(
    *,
    captured_at=1000.0,
    succeeded=100,
    queued=None,
    remote=None,
    ever_attempted=None,
    active=0,
    operations=200,
    fugu_rows=300,
    rate_limited=0,
    provider_calls=150,
    provider_failures=2,
    provider_spent=10.0,
    backfill_spent=9.0,
    provider_in_flight=0,
    histories=None,
    fugu_ooc=(),
    budget_ooc=(),
):
    total = 5142
    remote = succeeded if remote is None else remote
    ever_attempted = succeeded if ever_attempted is None else ever_attempted
    queued = total - succeeded - active if queued is None else queued
    counts = {"queued": queued, "succeeded": succeeded}
    if active:
        counts["submitted"] = active
    ids = [f"doc-{index:04d}" for index in range(total)]
    history_values = dict(histories or {"retry": 22, "claim": 12, "adoption": 1})
    return {
        "version": recovery.SPOTCHECK_VERSION,
        "captured_at": captured_at,
        "captured_at_iso": datetime.fromtimestamp(
            captured_at, tz=timezone.utc
        ).isoformat(),
        "agent": {
            "counts": counts,
            "total": total,
            "distinct_documents": total,
            "document_ids": ids,
            "queued": queued,
            "succeeded": succeeded,
            "active": active,
            "ever_attempted": ever_attempted,
            "circuit_open": False,
            "circuit_reason": "",
        },
        "histories": history_values,
        "history_max_ids": dict(history_values),
        "hindsight": {
            "remote_documents": remote,
            "remote_distinct_documents": remote,
            "document_ids": ids[:remote],
            "operations": operations,
            "operation_counts": {"completed": operations},
        },
        "fugu": {
            "rows": fugu_rows,
            "rate_limited": rate_limited,
            "redpill_ooc_request_ids": list(fugu_ooc),
        },
        "budget": {
            "provider_in_flight": provider_in_flight,
            "provider_spent_usd": provider_spent,
            "backfill_spent_usd": backfill_spent,
            "provider_calls": provider_calls,
            "provider_failures": provider_failures,
            "redpill_ooc_request_ids": list(budget_ooc),
        },
    }


def _json_copy(value):
    return json.loads(json.dumps(value))


def test_direct_redpill_credit_classifier_is_narrow_and_429_is_retryable():
    assert recovery.direct_redpill_out_of_credit(
        {"account_id": "redpill", "http_status": 402, "error_message": ""}
    )
    assert recovery.direct_redpill_out_of_credit(
        {
            "account_id": "redpill",
            "http_status": 403,
            "error_message": "Insufficient credit balance",
        }
    )
    assert not recovery.direct_redpill_out_of_credit(
        {
            "account_id": "redpill",
            "http_status": 429,
            "error_message": "insufficient credits",
        }
    )
    assert not recovery.direct_redpill_out_of_credit(
        {
            "account_id": "sub1",
            "http_status": 402,
            "error_message": "payment required",
        }
    )
    assert not recovery.direct_redpill_out_of_credit(
        {
            "account_id": "redpill",
            "http_status": 503,
            "error_message": "ordinary upstream failure",
        }
    )


def test_spotcheck_static_pause_predicates_require_both_reads():
    cfg = config()
    valid = _spot_snapshot()
    cases = []

    changed = _json_copy(valid)
    changed["agent"]["total"] = 5141
    cases.append(("agent_total", changed))
    changed = _json_copy(valid)
    changed["agent"]["distinct_documents"] = 5141
    cases.append(("agent_distinct", changed))
    changed = _json_copy(valid)
    changed["agent"]["counts"]["mystery"] = 1
    changed["agent"]["total"] += 1
    changed["agent"]["distinct_documents"] += 1
    cases.append(("agent_states", changed))
    changed = _json_copy(valid)
    changed["agent"]["counts"]["queued"] -= 1
    cases.append(("agent_state_sum", changed))
    changed = _json_copy(valid)
    changed["hindsight"]["remote_documents"] += 1
    cases.append(("remote_duplicates", changed))
    changed = _json_copy(valid)
    changed["hindsight"]["document_ids"][0] = "unapproved"
    cases.append(("remote_unapproved", changed))
    changed = _json_copy(valid)
    changed["agent"]["ever_attempted"] = 99
    cases.append(("aggregate_order", changed))
    changed = _spot_snapshot(active=3, succeeded=100, ever_attempted=103)
    cases.append(("agent_active", changed))
    changed = _spot_snapshot(provider_in_flight=7)
    cases.append(("provider_in_flight", changed))

    for expected, broken in cases:
        confirmed, _observed = recovery._confirmed_spotcheck_issues(
            broken, broken, None, cfg
        )
        assert expected in confirmed
        confirmed, observed = recovery._confirmed_spotcheck_issues(
            valid, broken, None, cfg
        )
        assert expected not in confirmed
        assert expected in observed


def test_spotcheck_transition_ledgers_queue_and_credit_predicates():
    cfg = config()
    previous = _spot_snapshot()
    cases = []

    changed = _spot_snapshot(captured_at=1600, succeeded=99, remote=100)
    cases.append(("succeeded_regression", changed))
    changed = _spot_snapshot(captured_at=1600, ever_attempted=99)
    cases.append(("ever_attempted_regression", changed))
    changed = _spot_snapshot(captured_at=1600, operations=199)
    cases.append(("operations_regression", changed))
    changed = _spot_snapshot(captured_at=1600, fugu_rows=299)
    cases.append(("fugu_rows_regression", changed))
    changed = _spot_snapshot(captured_at=1600, provider_calls=149)
    cases.append(("provider_calls_regression", changed))
    changed = _spot_snapshot(captured_at=1600, provider_failures=1)
    cases.append(("provider_failures_regression", changed))
    changed = _spot_snapshot(captured_at=1600, provider_spent=9.99)
    cases.append(("provider_spent_usd_regression", changed))
    changed = _spot_snapshot(
        captured_at=1600,
        histories={"retry": 21, "claim": 12, "adoption": 1},
    )
    cases.append(("history_retry_regression", changed))
    changed = _spot_snapshot(captured_at=1600, queued=5043)
    cases.append(("unexplained_queue_increase", changed))
    changed = _json_copy(_spot_snapshot(captured_at=1600))
    changed["agent"]["document_ids"][-1] = "replacement"
    cases.append(("agent_document_set", changed))
    changed = _json_copy(_spot_snapshot(captured_at=1600))
    changed["hindsight"]["document_ids"][0] = "doc-0100"
    cases.append(("remote_document_regression", changed))

    for expected, broken in cases:
        confirmed, _observed = recovery._confirmed_spotcheck_issues(
            broken, broken, previous, cfg
        )
        assert expected in confirmed

    tiny_rounding = _spot_snapshot(
        captured_at=1600,
        provider_spent=10.0 - 5e-10,
        backfill_spent=9.0 - 5e-10,
    )
    confirmed, observed = recovery._confirmed_spotcheck_issues(
        tiny_rounding, tiny_rounding, previous, cfg
    )
    assert not any("spent_usd_regression" in code for code in confirmed | observed)

    explained = _spot_snapshot(
        captured_at=1600,
        queued=5043,
        histories={"retry": 23, "claim": 12, "adoption": 1},
    )
    confirmed, _observed = recovery._confirmed_spotcheck_issues(
        explained, explained, previous, cfg
    )
    assert "unexplained_queue_increase" not in confirmed

    for key in ("fugu_ooc", "budget_ooc"):
        kwargs = {key: ("new-credit-event",), "captured_at": 1600}
        credit = _spot_snapshot(**kwargs)
        confirmed, _observed = recovery._confirmed_spotcheck_issues(
            credit, credit, previous, cfg
        )
        assert "redpill_out_of_credit" in confirmed

    one_read_credit = _spot_snapshot(
        captured_at=1605, budget_ooc=("new-credit-event",)
    )
    confirmed, observed = recovery._confirmed_spotcheck_issues(
        _spot_snapshot(captured_at=1600), one_read_credit, previous, cfg
    )
    assert "redpill_out_of_credit" not in confirmed
    assert "redpill_out_of_credit" in observed


def test_run_spotcheck_seeds_then_opens_only_agent_for_confirmed_credit(
    tmp_path, monkeypatch
):
    cfg = replace(config(), state_dir=tmp_path)
    instance = object.__new__(recovery.Recovery)
    instance.cfg = cfg
    snapshots = iter(
        [
            _spot_snapshot(captured_at=1000),
            _spot_snapshot(captured_at=1005, rate_limited=1),
        ]
    )
    instance.raw_spotcheck_snapshot = lambda: next(snapshots)
    opened = []
    instance.persist_agent_open = lambda reason: opened.append(reason)
    monkeypatch.setattr(recovery, "event", lambda *_args, **_kwargs: None)
    seeded = recovery.run_spotcheck(
        instance, apply_pause=True, interval_seconds=0, sleep=lambda _seconds: None
    )
    assert seeded["status"] == "ok"
    assert seeded["seeded"] is True
    assert seeded["new_429s"] == 0
    assert opened == []
    assert cfg.spotcheck_file.exists()

    snapshots = iter(
        [
            _spot_snapshot(
                captured_at=1600,
                rate_limited=5,
                budget_ooc=("credit-1",),
            ),
            _spot_snapshot(
                captured_at=1605,
                rate_limited=6,
                budget_ooc=("credit-1",),
            ),
        ]
    )
    instance.raw_spotcheck_snapshot = lambda: next(snapshots)
    paused = recovery.run_spotcheck(
        instance, apply_pause=True, interval_seconds=0, sleep=lambda _seconds: None
    )
    assert paused["status"] == "paused"
    assert paused["paused"] is True
    assert paused["new_429s"] == 5
    assert len(opened) == 1
    assert opened[0].startswith(recovery.SPOTCHECK_PAUSE_PREFIX)
    assert "redpill_out_of_credit" in opened[0]


def test_run_spotcheck_read_failure_is_nonpausing_unknown(tmp_path):
    cfg = replace(config(), state_dir=tmp_path)
    instance = object.__new__(recovery.Recovery)
    instance.cfg = cfg

    def fail_read():
        raise OSError("temporary tunnel read failure")

    instance.raw_spotcheck_snapshot = fail_read
    opened = []
    instance.persist_agent_open = lambda reason: opened.append(reason)
    result = recovery.run_spotcheck(
        instance, apply_pause=True, interval_seconds=0, sleep=lambda _seconds: None
    )
    assert result["status"] == "unknown"
    assert result["paused"] is False
    assert "temporary tunnel read failure" in result["reason"]
    assert opened == []
    assert not cfg.spotcheck_file.exists()


def test_spotcheck_cli_exposes_json_and_apply_pause_flags():
    output = subprocess.run(
        [sys.executable, str(MODULE), "spot-check", "--help"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout
    assert "--json" in output
    assert "--apply-pause" in output


def test_installer_defaults_to_tunnel_only_incident_containment():
    source = (MODULE.parent / "install.sh").read_text()
    assert "mode=containment" in source
    assert "render_unit hindsight-recovery-tunnel.service" in source
    controller = source.index("hindsight-recovery-controller.service")
    guard = source.index("hindsight-recovery-guard.service")
    watchdog = source.index("hindsight-recovery-watchdog.service")
    assert controller < guard < watchdog
    assert '"$SYSTEMCTL_BIN" --user disable --now "$unit"' in source
    assert 'rm -f -- "$UNIT_DIR/$unit"' in source
    assert 'ln -s /dev/null "$UNIT_DIR/$unit"' in source
    assert source.index('rm -f -- "$UNIT_DIR/$unit"') < source.index(
        'ln -s /dev/null "$UNIT_DIR/$unit"'
    )
    assert (
        '"$SYSTEMCTL_BIN" --user enable hindsight-recovery-tunnel.service'
        in source
    )
    assert '"$SYSTEMCTL_BIN" --user start' not in source
    assert "--stage-controller-services" in source
    assert "all remain disabled and stopped" in source
    assert source.index("Fail closed before dependency installation") < source.index(
        '"$VENV_DIR/bin/python" -m pip install'
    )
