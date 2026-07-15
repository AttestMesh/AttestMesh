from __future__ import annotations

import copy
import hashlib
import importlib.util
import inspect
import json
import sys
import threading
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest


MODULE = Path(__file__).resolve().parents[1] / "recovery.py"
SPEC = importlib.util.spec_from_file_location(
    "hindsight_recovery_adversarial", MODULE
)
assert SPEC and SPEC.loader
recovery = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = recovery
SPEC.loader.exec_module(recovery)


def config() -> recovery.Config:
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
        budget_maintenance_hold_url=(
            "http://127.0.0.1:5/admin/maintenance-hold"
        ),
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


def test_guard_excludes_only_its_own_prior_unhealthy_heartbeat(
    tmp_path: Path,
) -> None:
    paths = {
        label: tmp_path / f"{label}.json"
        for label in ("guard", "watchdog", "controller")
    }
    now = time.time()
    paths["guard"].write_text(
        json.dumps({"at": now, "healthy": False})
    )
    paths["watchdog"].write_text(
        json.dumps({"at": now, "healthy": False})
    )
    paths["controller"].write_text(
        json.dumps({"at": now, "healthy": True})
    )

    failures = recovery.heartbeat_failures(
        tuple((path, label) for label, path in paths.items()),
        60,
        excluded_heartbeat_labels=("guard",),
    )

    assert failures == ["watchdog heartbeat reports unhealthy"]
    with pytest.raises(ValueError, match="unknown excluded heartbeat"):
        recovery.heartbeat_failures(
            (),
            60,
            excluded_heartbeat_labels=("typo",),
        )
    assert "excluded_heartbeat_labels" in inspect.signature(
        recovery.Recovery.quick_failures
    ).parameters
    assert "excluded_heartbeat_labels" in inspect.signature(
        recovery.Recovery.full_audit
    ).parameters
    guard_source = inspect.getsource(recovery.run_guard)
    assert guard_source.count('excluded_heartbeat_labels=("guard",)') == 2
    assert 'audit_heartbeat_label="guard"' in guard_source
    assert "last_full = time.monotonic()" in guard_source


def test_bounded_audit_heartbeat_pulses_and_stops(tmp_path: Path) -> None:
    path = tmp_path / "controller-heartbeat.json"
    with recovery._BoundedAuditHeartbeat(
        path,
        "controller",
        deadline_seconds=1,
        pulse_seconds=0.02,
        fields={"status": "full_audit"},
    ):
        first = json.loads(path.read_text())
        limit = time.monotonic() + 0.5
        current = first
        while current["at"] == first["at"] and time.monotonic() < limit:
            time.sleep(0.01)
            current = json.loads(path.read_text())
        assert current["at"] > first["at"]
        assert current["healthy"] is True
        assert current["status"] == "full_audit"

    stopped = json.loads(path.read_text())
    time.sleep(0.05)
    assert json.loads(path.read_text()) == stopped


def test_bounded_audit_heartbeat_deadline_fails_closed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "guard-heartbeat.json"
    with pytest.raises(
        recovery.InvariantError,
        match="guard full audit exceeded bounded 0.08s deadline",
    ):
        with recovery._BoundedAuditHeartbeat(
            path,
            "guard",
            deadline_seconds=0.08,
            pulse_seconds=0.01,
            fields={"full_audit": "running"},
        ):
            time.sleep(0.15)

    failed = json.loads(path.read_text())
    assert failed["healthy"] is False
    assert failed["status"] == "full_audit_deadline_exceeded"
    assert failed["reason"] == (
        "guard full audit exceeded bounded 0.08s deadline"
    )


def test_controller_full_audit_uses_four_heartbeat_windows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg = config()
    object.__setattr__(cfg, "state_dir", tmp_path)
    instance = object.__new__(recovery.Recovery)
    instance.cfg = cfg
    instance.controller_owned = True
    instance._full_audit = lambda phase, **_kwargs: {"phase": phase["name"]}
    observed: dict[str, object] = {}

    class _Pulse:
        def __init__(self, path, label, **kwargs):
            observed.update(path=path, label=label, **kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(recovery, "_BoundedAuditHeartbeat", _Pulse)

    assert instance.full_audit({"name": "ready"}) == {"phase": "ready"}
    assert observed["path"] == cfg.controller_file
    assert observed["label"] == "controller"
    assert observed["deadline_seconds"] == cfg.heartbeat_max_age * 4


def test_controller_incident_authorization_pulses_beyond_one_heartbeat_window(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg = config()
    object.__setattr__(cfg, "state_dir", tmp_path)
    object.__setattr__(cfg, "heartbeat_max_age", 0.04)
    writes: list[dict[str, Any]] = []
    real_atomic_json = recovery._atomic_json

    def recording_atomic_json(path, value):
        if path == cfg.controller_file:
            writes.append(copy.deepcopy(value))
        return real_atomic_json(path, value)

    monkeypatch.setattr(recovery, "_atomic_json", recording_atomic_json)
    instance = object.__new__(recovery.Recovery)
    instance.cfg = cfg
    instance.controller_owned = True

    def prepare(incident, rows):
        time.sleep(cfg.heartbeat_max_age * 2.5)
        return {"incident": incident, "rows": rows}

    instance._prepare_incident_authorization = prepare
    result = instance.prepare_incident_authorization(
        {"kind": "outbox"}, [{"document_id": "one"}]
    )

    assert result["rows"] == [{"document_id": "one"}]
    healthy = [
        value
        for value in writes
        if value.get("healthy") is True
        and value.get("status") == "incident_recovery"
    ]
    assert len(healthy) >= 3
    assert healthy[-1]["at"] - healthy[0]["at"] > cfg.heartbeat_max_age
    assert all(value["incident_stage"] == "authorization" for value in healthy)


def test_controller_recovered_attempt_acceptance_pulses_beyond_one_heartbeat_window(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg = config()
    object.__setattr__(cfg, "state_dir", tmp_path)
    object.__setattr__(cfg, "heartbeat_max_age", 0.04)
    writes: list[dict[str, Any]] = []
    real_atomic_json = recovery._atomic_json

    def recording_atomic_json(path, value):
        if path == cfg.controller_file:
            writes.append(copy.deepcopy(value))
        return real_atomic_json(path, value)

    monkeypatch.setattr(recovery, "_atomic_json", recording_atomic_json)
    instance = object.__new__(recovery.Recovery)
    instance.cfg = cfg
    instance.controller_owned = True

    def accept(attempt):
        time.sleep(cfg.heartbeat_max_age * 2.5)
        return {"attempt": attempt}

    instance._accept_recovered_attempt = accept
    attempt = {"document_id": "doc", "kind": "completed_refresh"}
    assert instance.accept_recovered_attempt(attempt) == {"attempt": attempt}

    healthy = [
        value
        for value in writes
        if value.get("healthy") is True
        and value.get("status") == "incident_recovery"
    ]
    assert len(healthy) >= 3
    assert healthy[-1]["at"] - healthy[0]["at"] > cfg.heartbeat_max_age
    assert all(value["incident_stage"] == "acceptance" for value in healthy)
    assert all(value["document_id"] == "doc" for value in healthy)
    assert all(value["recovery_kind"] == "completed_refresh" for value in healthy)


def test_expired_authorization_lease_rejects_inner_healthy_overwrite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg = config()
    object.__setattr__(cfg, "state_dir", tmp_path)
    object.__setattr__(cfg, "heartbeat_max_age", 0.02)
    instance = object.__new__(recovery.Recovery)
    instance.cfg = cfg
    instance.controller_owned = True
    writes: list[dict[str, Any]] = []
    real_atomic_json = recovery._atomic_json

    def recording_atomic_json(path, value):
        if path == cfg.controller_file:
            writes.append(copy.deepcopy(value))
        return real_atomic_json(path, value)

    monkeypatch.setattr(recovery, "_atomic_json", recording_atomic_json)

    def prepare(_incident, _rows):
        time.sleep(cfg.heartbeat_max_age * 5)
        instance.touch_controller("inner_should_not_mask_expiry")
        return {"status": "should-not-pass"}

    instance._prepare_incident_authorization = prepare
    with pytest.raises(
        recovery.InvariantError,
        match="controller incident recovery exceeded bounded 0.08s deadline",
    ):
        instance.prepare_incident_authorization({}, [])

    failed = json.loads(cfg.controller_file.read_text())
    assert failed["healthy"] is False
    assert failed["status"] == "incident_recovery_deadline_exceeded"
    assert not any(
        value.get("status") == "inner_should_not_mask_expiry" for value in writes
    )


def test_controller_touch_republishes_unhealthy_after_delayed_healthy_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg = config()
    object.__setattr__(cfg, "state_dir", tmp_path)
    instance = object.__new__(recovery.Recovery)
    instance.cfg = cfg
    instance.controller_owned = True
    unhealthy_written = threading.Event()
    real_atomic_json = recovery._atomic_json

    def delayed_inner_write(path, value):
        if path == cfg.controller_file and value.get("status") == "inner_write":
            assert unhealthy_written.wait(1)
        real_atomic_json(path, value)
        if path == cfg.controller_file and value.get("healthy") is False:
            unhealthy_written.set()

    monkeypatch.setattr(recovery, "_atomic_json", delayed_inner_write)

    with pytest.raises(
        recovery.InvariantError,
        match="controller incident recovery exceeded bounded 0.08s deadline",
    ):
        with recovery._BoundedAuditHeartbeat(
            cfg.controller_file,
            "controller",
            deadline_seconds=0.08,
            pulse_seconds=0.01,
            fields={"status": "incident_recovery"},
        ):
            instance.touch_controller("inner_write")

    final = json.loads(cfg.controller_file.read_text())
    assert final["healthy"] is False
    assert final["status"] == "incident_recovery_deadline_exceeded"


def test_long_authorized_roll_heartbeat_is_outside_bounded_row_lease(
    tmp_path: Path,
) -> None:
    cfg = config()
    object.__setattr__(cfg, "state_dir", tmp_path)
    object.__setattr__(cfg, "heartbeat_max_age", 0.02)
    instance = object.__new__(recovery.Recovery)
    instance.cfg = cfg
    instance.controller_owned = True

    # The real 1,200-second roll owns its explicit sleep/touch heartbeat loop;
    # it must not inherit the 4-window authorization lease.
    recover_source = inspect.getsource(recovery.Recovery.recover_incident_rows)
    assert "_BoundedAuditHeartbeat" not in recover_source
    assert "recover_provider_condition" in recover_source
    instance.sleep_with_heartbeat(
        cfg.heartbeat_max_age * 5, "hindsight_recovery_roll"
    )
    heartbeat = json.loads(cfg.controller_file.read_text())
    assert heartbeat["healthy"] is True
    assert heartbeat["status"] == "hindsight_recovery_roll"


def test_incident_row_proofs_and_bank_mutations_pulse_controller() -> None:
    instance = object.__new__(recovery.Recovery)
    pulses: list[tuple[str, dict[str, Any]]] = []
    instance.touch_controller = lambda status, **fields: pulses.append(
        (status, fields)
    )

    @recovery.contextlib.contextmanager
    def bank_lock():
        yield object()

    instance.bank_lock = bank_lock
    with instance.incident_bank_mutation("doc", "completed_refresh"):
        pass

    assert [value[1]["incident_stage"] for value in pulses] == [
        "bank_mutation_before",
        "bank_mutation_after",
    ]
    row_source = inspect.getsource(recovery.Recovery.recover_incident_rows)
    pending_source = inspect.getsource(recovery.Recovery.resume_pending_acceptance)
    assert row_source.count('incident_stage="row_proof"') >= 2
    assert 'incident_stage="row_proof"' in pending_source
    assert "with self.incident_bank_mutation" in row_source
    assert "with self.incident_bank_mutation" in pending_source


def test_full_audits_are_serialized_across_recovery_instances(
    tmp_path: Path,
) -> None:
    cfg = config()
    object.__setattr__(cfg, "state_dir", tmp_path)
    entered_first = threading.Event()
    release_first = threading.Event()
    entered_second = threading.Event()
    active = 0
    maximum_active = 0
    state_lock = threading.Lock()

    def make_instance(name: str) -> recovery.Recovery:
        instance = object.__new__(recovery.Recovery)
        instance.cfg = cfg
        instance.controller_owned = False

        def audit(phase, **_kwargs):
            nonlocal active, maximum_active
            with state_lock:
                active += 1
                maximum_active = max(maximum_active, active)
            try:
                if name == "first":
                    entered_first.set()
                    assert release_first.wait(1)
                else:
                    entered_second.set()
                return {"phase": phase["name"]}
            finally:
                with state_lock:
                    active -= 1

        instance._full_audit = audit
        return instance

    results: list[dict[str, Any]] = []
    first = threading.Thread(
        target=lambda: results.append(
            make_instance("first").full_audit({"name": "ready"})
        )
    )
    second = threading.Thread(
        target=lambda: results.append(
            make_instance("second").full_audit({"name": "ready"})
        )
    )
    first.start()
    assert entered_first.wait(1)
    second.start()
    assert not entered_second.wait(0.05)
    release_first.set()
    first.join(1)
    second.join(1)

    assert not first.is_alive()
    assert not second.is_alive()
    assert entered_second.is_set()
    assert maximum_active == 1
    assert results == [{"phase": "ready"}, {"phase": "ready"}]
    assert cfg.authoritative_read_lock_file == (
        tmp_path / "authoritative-read.lock"
    )


def test_quick_snapshot_waits_for_full_authoritative_read(
    tmp_path: Path,
) -> None:
    cfg = config()
    object.__setattr__(cfg, "state_dir", tmp_path)
    full_entered = threading.Event()
    release_full = threading.Event()
    quick_entered = threading.Event()

    full_instance = object.__new__(recovery.Recovery)
    full_instance.cfg = cfg
    full_instance.controller_owned = False

    def full_audit(phase, **_kwargs):
        full_entered.set()
        assert release_full.wait(1)
        return {"phase": phase["name"]}

    full_instance._full_audit = full_audit
    quick_instance = object.__new__(recovery.Recovery)
    quick_instance.cfg = cfg
    quick_instance.controller_owned = False
    quick_instance._quick_snapshot_unlocked = (
        lambda: quick_entered.set() or {"quick": True}
    )
    results: list[dict[str, Any]] = []
    full_thread = threading.Thread(
        target=lambda: results.append(
            full_instance.full_audit({"name": "ready"})
        )
    )
    quick_thread = threading.Thread(
        target=lambda: results.append(quick_instance.quick_snapshot())
    )

    full_thread.start()
    assert full_entered.wait(1)
    quick_thread.start()
    assert not quick_entered.wait(0.05)
    release_full.set()
    full_thread.join(1)
    quick_thread.join(1)

    assert not full_thread.is_alive()
    assert not quick_thread.is_alive()
    assert quick_entered.is_set()
    assert results == [{"phase": "ready"}, {"quick": True}]


def test_http_json_timeout_names_the_authoritative_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "http://127.0.0.1:1234/health"
    monkeypatch.setattr(
        recovery.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            TimeoutError("timed out")
        ),
    )

    with pytest.raises(
        recovery.InvariantError,
        match=r"127\.0\.0\.1:1234/health.*TimeoutError: timed out",
    ):
        recovery.Recovery._http_json(url)


def test_release_gate_places_two_full_audits_at_first_and_tenth_checks() -> None:
    full_audits = 0
    observed: list[int] = []
    for clean_checks in range(10):
        if recovery.release_gate_requires_full_audit(
            clean_checks=clean_checks,
            full_audits=full_audits,
            required_clean_checks=10,
            required_full_audits=2,
        ):
            observed.append(clean_checks + 1)
            full_audits += 1

    assert observed == [1, 10]
    assert full_audits == 2
    assert recovery.release_gate_requires_full_audit(
        clean_checks=9,
        full_audits=0,
        required_clean_checks=10,
        required_full_audits=2,
    )


def test_operation_inventory_acceptance_is_count_gated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = object.__new__(recovery.Recovery)
    instance.cfg = config()
    manifest = [{"operation_id": str(index)} for index in range(4)]
    monkeypatch.setattr(
        recovery,
        "read_checkpoint",
        lambda _cfg: {"accepted": {"operation_manifest": manifest}},
    )

    assert not instance.operation_inventory_needs_acceptance(
        {"hindsight_operations": {"completed": 3, "failed": 1}}
    )
    assert instance.operation_inventory_needs_acceptance(
        {
            "hindsight_operations": {
                "completed": 3,
                "failed": 1,
                "cancelled": 1,
            }
        }
    )
    with pytest.raises(
        recovery.InvariantError,
        match="operation count regressed below checkpoint",
    ):
        instance.operation_inventory_needs_acceptance(
            {"hindsight_operations": {"completed": 2, "failed": 1}}
        )


def original_failure(request_id: str, status: int, at: str) -> dict[str, object]:
    return {
        "request_id": request_id,
        "status_code": status,
        "at": at,
        "recovery_probe_route": None,
    }


def active_root(request_id: str, status: int, at: str) -> dict[str, object]:
    return {
        "version": 1,
        "auth_incident_request_id": request_id,
        "auth_incident_status": status,
        "auth_incident_at": at,
    }


def success_call(
    route: str, provenance: str, incident_id: str
) -> dict[str, object]:
    return {
        "request_id": str(
            uuid.uuid5(recovery.RECOVERY_PROBE_NAMESPACE, provenance)
        ),
        "model": (
            config().expected_hindsight_model
            if route == "gpt_oss"
            else recovery.QWEN_MODEL
        ),
        "endpoint": "chat" if route == "gpt_oss" else "embeddings",
        "provider_call_recorded": True,
        "ambiguous_upstream_reconciled": False,
        "request_provenance_sha256": hashlib.sha256(
            provenance.encode()
        ).hexdigest(),
        "recovery_probe_route": route,
        "recovery_auth_incident_request_id": incident_id,
        "recovery_gate_sha256": "a" * 64,
        "cost_usd": 0.000001,
    }


def test_controller_cannot_select_historical_same_status_failure() -> None:
    instance = object.__new__(recovery.Recovery)
    instance.cfg = config()
    instance.hindsight_state = {"BAT": "admin", "BPT": "proxy"}
    historical_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    current_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    at = "2026-07-14T00:00:00+00:00"
    status = {
        "provider_failure_history": [
            original_failure(historical_id, 401, at),
            original_failure(current_id, 401, at),
        ],
        "provider_auth_circuit_incident_request_id": current_id,
        "provider_auth_circuit_incident_status": 401,
        "provider_auth_recovery": active_root(current_id, 401, at),
    }
    instance.budget_recovery_status = lambda: status
    instance._http_json = lambda _url: {"circuit_reason": "provider returned 401"}

    state = {"kind": "auth", "stage": "recovery_probes_started"}
    incident = {"provider_recovery": state}
    with pytest.raises(
        recovery.InvariantError, match="exact current incident ID"
    ):
        instance.provider_auth_recovery_probes(
            state,
            incident,
            authorized_incident_ids=[historical_id],
        )


def test_controller_requires_exact_proxy_root_and_active_episode() -> None:
    current_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    at = "2026-07-14T00:00:00+00:00"
    status = {
        "provider_auth_circuit_incident_request_id": current_id,
        "provider_auth_circuit_incident_status": 402,
        "provider_auth_recovery": active_root(current_id, 402, at),
    }
    assert recovery.Recovery._provider_auth_recovery_root(
        status,
        incident_id=current_id,
        incident_status=402,
        incident_at=at,
        required=True,
    ) == active_root(current_id, 402, at)

    drifted = copy.deepcopy(status)
    drifted["provider_auth_recovery"] = active_root(
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", 402, at
    )
    with pytest.raises(recovery.InvariantError, match="active provider-auth"):
        recovery.Recovery._provider_auth_recovery_root(
            drifted,
            incident_id=current_id,
            incident_status=402,
            incident_at=at,
            required=True,
        )


def test_maximum_charged_probe_call_cannot_prove_authentication() -> None:
    instance = object.__new__(recovery.Recovery)
    instance.cfg = config()
    incident_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    provenance = "hindsight-recovery-" + "a" * 24 + "-gpt_oss"
    call = success_call("gpt_oss", provenance, incident_id)
    call["ambiguous_upstream_reconciled"] = True
    with pytest.raises(recovery.InvariantError, match="success provenance drift"):
        instance._recovery_probe_success_record(
            {"provider_call_history": [call]},
            route="gpt_oss",
            request_id=str(call["request_id"]),
            provenance_sha256=str(call["request_provenance_sha256"]),
            incident_id=incident_id,
        )


def test_reset_proof_requires_root_removal_and_exact_paid_routes() -> None:
    instance = object.__new__(recovery.Recovery)
    instance.cfg = config()
    incident_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    reset_token = "reset-token"
    calls: dict[str, dict[str, object]] = {}
    routes: dict[str, dict[str, object]] = {}
    proof_successes: dict[str, dict[str, object]] = {}
    for route, marker in (("gpt_oss", "c"), ("qwen", "d")):
        provenance = f"hindsight-recovery-{marker * 24}-{route}"
        call = success_call(route, provenance, incident_id)
        calls[route] = call
        success = {
            "request_id": call["request_id"],
            "request_provenance_sha256": call["request_provenance_sha256"],
            "recovery_gate_sha256": call["recovery_gate_sha256"],
            "cost_usd": call["cost_usd"],
        }
        routes[route] = {"terminal": True, "success": success}
        proof_successes[route] = success
    state = {
        "probe_auth_incident_request_id": incident_id,
        "probe_auth_incident_status": 401,
        "probe_routes": routes,
    }
    proof = {
        "reset_token_sha256": hashlib.sha256(reset_token.encode()).hexdigest(),
        "credential_validated_by_exact_paid_probes": True,
        "auth_incident_request_id": incident_id,
        "auth_incident_status": 401,
        "probe_successes": proof_successes,
    }
    status = {
        "last_provider_auth_reset": proof,
        "provider_call_history": list(calls.values()),
    }
    assert instance._auth_reset_proof(
        status, reset_token, state=state
    ) == proof

    retained_root = copy.deepcopy(status)
    retained_root["provider_auth_circuit_incident_request_id"] = incident_id
    retained_root["provider_auth_circuit_incident_status"] = 401
    assert instance._auth_reset_proof(
        retained_root, reset_token, state=state
    ) is None
