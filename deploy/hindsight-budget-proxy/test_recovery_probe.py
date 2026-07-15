from __future__ import annotations

import copy
import hashlib
import threading
import uuid
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

import budget_proxy
import control_marker
import reconcile_ambiguous


def settings(**overrides) -> budget_proxy.Settings:
    base = budget_proxy.Settings(
        provider_base_url="https://provider.invalid/v1",
        provider_api_key="provider-secret",
        proxy_api_key="proxy-secret",
        admin_api_key="admin-secret",
        provider_egress_enabled=True,
        provider_total_limit_usd=50,
        provider_safety_margin_usd=0.5,
        hindsight_phase="backfill",
        hindsight_backfill_limit_usd=30,
        hindsight_monthly_limit_usd=10,
        qwen_monthly_limit_usd=5,
        hindsight_llm_max_concurrent=3,
        provider_max_in_flight=6,
        rpm_limit=30,
        tpm_limit=1_000_000,
        prices={
            budget_proxy.HINDSIGHT_MODEL: (0.15, 0.60),
            budget_proxy.QWEN_MODEL: (0.01, 0.0),
        },
        initial_provider_spend_usd=0.0,
    )
    return replace(base, **overrides)


def state(
    tmp_path: Path, configured: budget_proxy.Settings | None = None
) -> budget_proxy.BudgetState:
    return budget_proxy.BudgetState(
        tmp_path / "ledger.json",
        tmp_path / "state.json",
        configured or settings(),
    )


def seed_auth_incident(current: budget_proxy.BudgetState, status: int = 401) -> str:
    request_id, _ = current.reserve(
        "chat", budget_proxy.HINDSIGHT_MODEL, 100, 1
    )
    current.record_provider_failure(
        request_id,
        status,
        retry_after=None,
        circuit_reason=f"provider returned {status}",
    )
    return request_id


def probe_payload(
    incident_id: str, route: str = "gpt_oss", marker: str = "a"
) -> dict[str, object]:
    return {
        "route": route,
        "provenance": f"hindsight-recovery-{marker * 24}-{route}",
        "auth_incident_request_id": incident_id,
        "agent_circuit_open": True,
        "pocket_circuit_open": True,
        "provider_in_flight": 0,
        "projected_full_cost_usd": 26.43,
    }


def probe_headers() -> dict[str, str]:
    return {
        "Authorization": "Bearer admin-secret",
        "X-Proxy-Authorization": "Bearer proxy-secret",
    }


@contextmanager
def running(
    current: budget_proxy.BudgetState, configured: budget_proxy.Settings
):
    server = budget_proxy.BudgetProxyServer(("127.0.0.1", 0), current, configured)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/admin/recovery-probe"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_recovery_probe_spec_is_fixed_and_deterministic() -> None:
    provenance = "hindsight-recovery-" + "a" * 24 + "-gpt_oss"
    first = budget_proxy.recovery_probe_spec("gpt_oss", provenance)
    second = budget_proxy.recovery_probe_spec("gpt_oss", provenance)
    assert second["request_id"] == first["request_id"]
    assert first["model"] == budget_proxy.HINDSIGHT_MODEL
    assert first["endpoint"] == "chat"
    assert first["agent_id"] == budget_proxy.FUGU_HINDSIGHT_AGENT_ID
    assert first["payload"]["messages"] == [
        {"role": "user", "content": "recovery auth preflight"}
    ]
    assert first["payload"]["max_completion_tokens"] == 1
    assert first["payload"]["metadata"]["recovery_preflight_id"] == provenance
    with pytest.raises(budget_proxy.GuardViolation, match="route-mismatched"):
        budget_proxy.recovery_probe_spec("qwen", provenance)
    with pytest.raises(budget_proxy.GuardViolation, match="allowlist"):
        budget_proxy.recovery_probe_spec("arbitrary", provenance)
    assert budget_proxy.recovery_probe_response_is_exact(
        "gpt_oss", {"choices": [{"message": {"content": ""}}]}
    )
    assert budget_proxy.recovery_probe_response_is_exact(
        "gpt_oss",
        {
            "usage": {"completion_tokens": 1},
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {"role": "assistant", "content": None},
                }
            ],
        },
    )
    assert budget_proxy.recovery_probe_response_is_exact(
        "qwen", {"data": [{"embedding": [0.1]}]}
    )
    assert not budget_proxy.recovery_probe_response_is_exact(
        "gpt_oss", {"choices": []}
    )


@pytest.mark.parametrize(
    "value",
    [
        {"choices": [{}]},
        {"choices": [{"message": {}}]},
        {"choices": [{"message": {"content": None}}]},
        {"choices": [{"message": {"content": {}}}]},
        {
            "usage": {"completion_tokens": 1},
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": None},
                }
            ],
        },
        {
            "usage": {"completion_tokens": True},
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {"role": "assistant", "content": None},
                }
            ],
        },
        {
            "usage": {"completion_tokens": 0},
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {"role": "assistant", "content": None},
                }
            ],
        },
        {
            "usage": {"completion_tokens": 2},
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {"role": "assistant", "content": None},
                }
            ],
        },
        {
            "usage": {"completion_tokens": 1},
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {"role": "user", "content": None},
                }
            ],
        },
        {
            "usage": {"completion_tokens": 1},
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {"role": "assistant"},
                }
            ],
        },
        {
            "usage": {"completion_tokens": 1},
            "choices": [
                {"message": {"role": "assistant", "content": None}}
            ],
        },
        {
            "usage": {"completion_tokens": 1},
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{"id": "unexpected"}],
                    },
                }
            ],
        },
        {
            "usage": {"completion_tokens": 1},
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {"role": "assistant", "content": None},
                },
                {
                    "finish_reason": "length",
                    "message": {"role": "assistant", "content": None},
                },
            ],
        },
    ],
)
def test_gpt_recovery_probe_rejects_bogus_success_shapes(value) -> None:
    assert not budget_proxy.recovery_probe_response_is_exact("gpt_oss", value)


@pytest.mark.parametrize(
    "embedding",
    [[], [True], [False], [float("nan")], [float("inf")], [float("-inf")], [{}]],
)
def test_qwen_recovery_probe_rejects_bogus_embedding_vectors(embedding) -> None:
    assert not budget_proxy.recovery_probe_response_is_exact(
        "qwen", {"data": [{"embedding": embedding}]}
    )


def test_recovery_probe_requires_distinct_admin_bat(
    tmp_path: Path,
) -> None:
    configured = settings()
    current = state(tmp_path, configured)
    incident_id = seed_auth_incident(current)
    body = probe_payload(incident_id)
    with running(current, configured) as url:
        assert httpx.post(url, json=body, timeout=5).status_code == 401
        assert (
            httpx.post(
                url,
                headers={"Authorization": "Bearer proxy-secret"},
                json=body,
                timeout=5,
            ).status_code
            == 401
        )
        assert (
            httpx.post(
                url,
                headers={"Authorization": "Bearer admin-secret"},
                json=body,
                timeout=5,
            ).status_code
            == 401
        )
    assert current.health()["in_flight"] == 0
    assert len(current.value["provider_failure_history"]) == 1


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("agent_circuit_open", False, "Agent and Pocket open"),
        ("agent_circuit_open", 1, "Agent and Pocket open"),
        ("pocket_circuit_open", False, "Agent and Pocket open"),
        ("provider_in_flight", 1, "zero provider in-flight"),
        ("provider_in_flight", False, "zero provider in-flight"),
        ("projected_full_cost_usd", "nan", "projection is not numeric"),
    ],
)
def test_recovery_probe_rejects_unproven_safety_gates(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    configured = settings()
    current = state(tmp_path, configured)
    body = probe_payload(seed_auth_incident(current))
    body[field] = value
    with running(current, configured) as url:
        response = httpx.post(
            url,
            headers=probe_headers(),
            json=body,
            timeout=5,
        )
    assert response.status_code == 400
    assert message in response.text
    assert current.health()["in_flight"] == 0


def test_recovery_probe_requires_exact_auth_circuit_and_sub_30_projection(
    tmp_path: Path,
) -> None:
    configured = settings()
    current = state(tmp_path, configured)
    incident_id = seed_auth_incident(current)
    current.open_circuit("provider returned 503", keep_request=False)
    with running(current, configured) as url:
        response = httpx.post(
            url,
            headers=probe_headers(),
            json=probe_payload(incident_id),
            timeout=5,
        )
    assert response.status_code == 409
    assert "exact provider-auth circuit" in response.text

    current.open_circuit("provider returned 401", keep_request=False)
    body = probe_payload(incident_id)
    body["projected_full_cost_usd"] = 29.99999999
    with running(current, configured) as url:
        response = httpx.post(
            url,
            headers=probe_headers(),
            json=body,
            timeout=5,
        )
    assert response.status_code == 409
    assert "$30 hard budget" in response.text
    assert current.health()["in_flight"] == 0


def test_recovery_probe_rejects_historical_same_status_auth_incident(
    tmp_path: Path,
) -> None:
    configured = settings()
    current = state(tmp_path, configured)
    historical_id = seed_auth_incident(current, 401)

    closed = copy.deepcopy(current.value)
    closed["circuit_open"] = False
    closed.pop("reason", None)
    closed.pop("provider_auth_circuit_incident_request_id", None)
    closed.pop("provider_auth_circuit_incident_status", None)
    budget_proxy.atomic_json(current.state_path, closed)

    restarted = state(tmp_path, configured)
    current_id = seed_auth_incident(restarted, 401)
    assert historical_id != current_id
    manifest = restarted.exact_reservation_manifest()
    assert manifest["provider_auth_circuit_incident_request_id"] == current_id
    assert manifest["provider_auth_circuit_incident_status"] == 401

    with pytest.raises(
        budget_proxy.BudgetViolation, match="exact provider-auth circuit"
    ):
        restarted.reserve_recovery_probe(
            **probe_payload(historical_id, "gpt_oss", "7")
        )

    spec, replay = restarted.reserve_recovery_probe(
        **probe_payload(current_id, "gpt_oss", "8")
    )
    assert replay is None
    restarted.discard(str(spec["request_id"]))


@pytest.mark.parametrize(
    "missing_field",
    [
        "provider_auth_circuit_incident_request_id",
        "provider_auth_circuit_incident_status",
    ],
)
def test_provider_auth_circuit_root_id_and_status_are_atomic(
    tmp_path: Path, missing_field: str
) -> None:
    configured = settings()
    current = state(tmp_path, configured)
    seed_auth_incident(current, 402)
    malformed = copy.deepcopy(current.value)
    malformed.pop(missing_field)
    budget_proxy.atomic_json(current.state_path, malformed)

    with pytest.raises(
        SystemExit, match="ID/status pairing is malformed"
    ):
        state(tmp_path, configured)


def test_final_maintenance_hold_forbids_recovery_probe(tmp_path: Path) -> None:
    configured = settings()
    current = state(tmp_path, configured)
    current.enter_final_maintenance_hold(
        budget_proxy.FINAL_MAINTENANCE_HOLD_REASON
    )
    body = probe_payload(str(uuid.uuid4()))
    with running(current, configured) as url:
        response = httpx.post(
            url,
            headers=probe_headers(),
            json=body,
            timeout=5,
        )
    assert response.status_code == 409
    assert "final maintenance hold" in response.text
    assert current.health()["circuit_reason"] == (
        budget_proxy.FINAL_MAINTENANCE_HOLD_REASON
    )


@pytest.mark.parametrize(
    ("route", "expected_path", "expected_agent"),
    [
        ("gpt_oss", "/chat/completions", budget_proxy.FUGU_HINDSIGHT_AGENT_ID),
        ("qwen", "/embeddings", budget_proxy.FUGU_QWEN_AGENT_ID),
    ],
)
def test_expected_auth_failure_is_reserved_released_and_restart_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    expected_path: str,
    expected_agent: str,
) -> None:
    configured = settings()
    current = state(tmp_path, configured)
    incident_id = seed_auth_incident(current)
    body = probe_payload(incident_id, route, "b" if route == "qwen" else "a")
    spec = budget_proxy.recovery_probe_spec(route, body["provenance"])
    real_post = budget_proxy.httpx.post
    calls: list[dict[str, object]] = []

    def routed_post(url, *args, **kwargs):
        if str(url).startswith(configured.provider_base_url):
            assert str(url).endswith(expected_path)
            assert spec["request_id"] in current.value["in_flight"]
            assert kwargs["headers"]["x-session-id"] == spec["request_id"]
            assert kwargs["headers"]["x-agent-id"] == expected_agent
            assert kwargs["json"] == spec["payload"]
            calls.append(dict(kwargs))
            return httpx.Response(
                401,
                json={"error": {"message": "unauthorized"}},
                request=httpx.Request("POST", str(url)),
            )
        return real_post(url, *args, **kwargs)

    monkeypatch.setattr(budget_proxy.httpx, "post", routed_post)
    with running(current, configured) as url:
        first = real_post(
            url,
            headers=probe_headers(),
            json=body,
            timeout=5,
        )
    assert first.status_code == 200
    assert first.json()["result"] == "expected_auth_failure"
    assert first.json()["reused"] is False
    assert current.health()["in_flight"] == 0
    assert current.health()["circuit_reason"] == "provider returned 401"
    probe_failure = current.value["provider_failure_history"][-1]
    assert probe_failure["request_id"] == spec["request_id"]
    assert probe_failure["request_provenance_sha256"] == spec[
        "provenance_sha256"
    ]
    assert probe_failure["recovery_probe_route"] == route
    assert probe_failure["recovery_auth_incident_request_id"] == incident_id
    assert len(probe_failure["recovery_gate_sha256"]) == 64

    restarted = state(tmp_path, configured)
    with running(restarted, configured) as url:
        replay = real_post(
            url,
            headers=probe_headers(),
            json=body,
            timeout=5,
        )
    assert replay.status_code == 200
    assert replay.json()["result"] == "expected_auth_failure"
    assert replay.json()["reused"] is True
    assert len(calls) == 1

    changed_gate = dict(body)
    changed_gate["projected_full_cost_usd"] = 26.44
    with running(restarted, configured) as url:
        drift = real_post(
            url,
            headers=probe_headers(),
            json=changed_gate,
            timeout=5,
        )
    assert drift.status_code == 400
    assert "incident lineage drift" in drift.text
    assert len(calls) == 1


def test_authenticated_success_is_charged_auth_held_and_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = settings()
    current = state(tmp_path, configured)
    incident_id = seed_auth_incident(current)
    body = probe_payload(incident_id, "gpt_oss", "c")
    real_post = budget_proxy.httpx.post
    upstream_calls = 0

    def routed_post(url, *args, **kwargs):
        nonlocal upstream_calls
        if str(url).startswith(configured.provider_base_url):
            upstream_calls += 1
            return httpx.Response(
                200,
                json={
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                    "choices": [{"message": {"content": "ok"}}],
                },
                request=httpx.Request("POST", str(url)),
            )
        return real_post(url, *args, **kwargs)

    monkeypatch.setattr(budget_proxy.httpx, "post", routed_post)
    with running(current, configured) as url:
        first = real_post(
            url,
            headers=probe_headers(),
            json=body,
            timeout=5,
        )
        replay = real_post(
            url,
            headers=probe_headers(),
            json=body,
            timeout=5,
        )
    assert first.status_code == 200
    assert first.json()["result"] == "authenticated"
    assert first.json()["cost_usd"] > 0
    assert replay.status_code == 200
    assert replay.json()["result"] == "authenticated"
    assert replay.json()["reused"] is True
    assert upstream_calls == 1
    assert len(current.ledger["calls"]) == 1
    assert len(current.ledger["calls"][0]["recovery_gate_sha256"]) == 64
    assert current.health()["in_flight"] == 0
    assert current.health()["circuit_open"] is True
    assert current.health()["circuit_reason"] == "provider returned 401"


def test_truncated_reasoning_success_is_charged_auth_held_and_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = settings()
    current = state(tmp_path, configured)
    incident_id = seed_auth_incident(current)
    body = probe_payload(incident_id, "gpt_oss", "e")
    real_post = budget_proxy.httpx.post
    upstream_calls = 0

    def routed_post(url, *args, **kwargs):
        nonlocal upstream_calls
        if str(url).startswith(configured.provider_base_url):
            upstream_calls += 1
            return httpx.Response(
                200,
                json={
                    "usage": {"prompt_tokens": 74, "completion_tokens": 1},
                    "choices": [
                        {
                            "finish_reason": "length",
                            "message": {"role": "assistant", "content": None},
                        }
                    ],
                },
                request=httpx.Request("POST", str(url)),
            )
        return real_post(url, *args, **kwargs)

    monkeypatch.setattr(budget_proxy.httpx, "post", routed_post)
    with running(current, configured) as url:
        first = real_post(
            url,
            headers=probe_headers(),
            json=body,
            timeout=5,
        )
        replay = real_post(
            url,
            headers=probe_headers(),
            json=body,
            timeout=5,
        )
    assert first.status_code == 200
    assert first.json()["result"] == "authenticated"
    assert first.json()["cost_usd"] > 0
    assert replay.status_code == 200
    assert replay.json()["result"] == "authenticated"
    assert replay.json()["reused"] is True
    assert upstream_calls == 1
    assert len(current.ledger["calls"]) == 1
    assert current.health()["in_flight"] == 0
    assert current.health()["circuit_open"] is True
    assert current.health()["circuit_reason"] == "provider returned 401"


def test_gpt_success_then_qwen_success_share_original_auth_episode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = settings()
    current = state(tmp_path, configured)
    incident_id = seed_auth_incident(current, 401)
    gpt = probe_payload(incident_id, "gpt_oss", "1")
    qwen = probe_payload(incident_id, "qwen", "2")
    real_post = budget_proxy.httpx.post

    def routed_post(url, *args, **kwargs):
        if str(url).startswith(configured.provider_base_url):
            if str(url).endswith("/chat/completions"):
                body = {
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                    "choices": [{"message": {"content": "ok"}}],
                }
            else:
                body = {
                    "usage": {"prompt_tokens": 1, "completion_tokens": 0},
                    "data": [{"embedding": [0.1, -0.2]}],
                }
            return httpx.Response(
                200, json=body, request=httpx.Request("POST", str(url))
            )
        return real_post(url, *args, **kwargs)

    monkeypatch.setattr(budget_proxy.httpx, "post", routed_post)
    with running(current, configured) as url:
        first = real_post(url, headers=probe_headers(), json=gpt, timeout=5)
        second = real_post(url, headers=probe_headers(), json=qwen, timeout=5)
    assert first.status_code == second.status_code == 200
    assert first.json()["result"] == second.json()["result"] == "authenticated"
    assert current.health()["circuit_reason"] == "provider returned 401"
    assert current.health()["in_flight"] == 0
    assert [call["recovery_probe_route"] for call in current.ledger["calls"]] == [
        "gpt_oss",
        "qwen",
    ]
    assert all(
        call["recovery_auth_incident_request_id"] == incident_id
        for call in current.ledger["calls"]
    )


def test_new_provenance_after_route_success_reuses_paid_ledger_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = settings()
    current = state(tmp_path, configured)
    incident_id = seed_auth_incident(current)
    first_body = probe_payload(incident_id, "gpt_oss", "5")
    second_body = probe_payload(incident_id, "gpt_oss", "6")
    first_spec = budget_proxy.recovery_probe_spec(
        "gpt_oss", first_body["provenance"]
    )
    real_post = budget_proxy.httpx.post
    upstream_calls = 0

    def routed_post(url, *args, **kwargs):
        nonlocal upstream_calls
        if str(url).startswith(configured.provider_base_url):
            upstream_calls += 1
            return httpx.Response(
                200,
                json={
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                    "choices": [{"message": {"content": "ok"}}],
                },
                request=httpx.Request("POST", str(url)),
            )
        return real_post(url, *args, **kwargs)

    monkeypatch.setattr(budget_proxy.httpx, "post", routed_post)
    with running(current, configured) as url:
        first = real_post(
            url, headers=probe_headers(), json=first_body, timeout=5
        )
        second = real_post(
            url, headers=probe_headers(), json=second_body, timeout=5
        )
    assert first.json()["result"] == second.json()["result"] == "authenticated"
    assert second.json()["reused"] is True
    assert second.json()["matched_existing_route"] is True
    assert second.json()["request_id"] == first_spec["request_id"]
    assert second.json()["provenance_sha256"] == first_spec["provenance_sha256"]
    assert upstream_calls == 1
    assert len(current.ledger["calls"]) == 1


@pytest.mark.parametrize(("first_status", "second_status"), [(401, 402), (402, 401)])
def test_mixed_auth_failures_preserve_original_episode_for_both_routes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first_status: int,
    second_status: int,
) -> None:
    configured = settings()
    current = state(tmp_path, configured)
    incident_id = seed_auth_incident(current, 401)
    real_post = budget_proxy.httpx.post
    statuses = iter((first_status, second_status))

    def routed_post(url, *args, **kwargs):
        if str(url).startswith(configured.provider_base_url):
            status = next(statuses)
            return httpx.Response(
                status,
                json={"error": {"message": "auth"}},
                request=httpx.Request("POST", str(url)),
            )
        return real_post(url, *args, **kwargs)

    monkeypatch.setattr(budget_proxy.httpx, "post", routed_post)
    with running(current, configured) as url:
        first = real_post(
            url,
            headers=probe_headers(),
            json=probe_payload(incident_id, "gpt_oss", "3"),
            timeout=5,
        )
        second = real_post(
            url,
            headers=probe_headers(),
            json=probe_payload(incident_id, "qwen", "4"),
            timeout=5,
        )
    assert first.json()["result"] == second.json()["result"] == "expected_auth_failure"
    assert current.health()["circuit_reason"] == "provider returned 401"
    probe_failures = current.value["provider_failure_history"][-2:]
    assert [value["status_code"] for value in probe_failures] == [
        first_status,
        second_status,
    ]
    assert all(
        value["recovery_auth_incident_request_id"] == incident_id
        for value in probe_failures
    )
    assert current.value["provider_auth_circuit_incident_request_id"] == incident_id
    assert current.value["provider_auth_circuit_incident_status"] == 401


def test_malformed_2xx_is_ambiguous_and_keeps_maximum_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = settings()
    current = state(tmp_path, configured)
    body = probe_payload(seed_auth_incident(current), "gpt_oss", "9")
    real_post = budget_proxy.httpx.post

    def routed_post(url, *args, **kwargs):
        if str(url).startswith(configured.provider_base_url):
            return httpx.Response(
                200,
                json={"choices": []},
                request=httpx.Request("POST", str(url)),
            )
        return real_post(url, *args, **kwargs)

    monkeypatch.setattr(budget_proxy.httpx, "post", routed_post)
    with running(current, configured) as url:
        response = real_post(
            url,
            headers=probe_headers(),
            json=body,
            timeout=5,
        )
    assert response.status_code == 502
    assert response.json()["result"] == "ambiguous_reservation"
    assert current.health()["in_flight"] == 1
    assert current.ledger["calls"] == []
    assert current.health()["circuit_reason"].startswith(
        "ambiguous upstream failure: recovery probe"
    )


def test_transport_ambiguity_preserves_reservation_and_never_reforwards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = settings()
    current = state(tmp_path, configured)
    body = probe_payload(seed_auth_incident(current), "gpt_oss", "d")
    real_post = budget_proxy.httpx.post
    upstream_calls = 0

    def routed_post(url, *args, **kwargs):
        nonlocal upstream_calls
        if str(url).startswith(configured.provider_base_url):
            upstream_calls += 1
            raise httpx.ReadTimeout("ambiguous provider timeout")
        return real_post(url, *args, **kwargs)

    monkeypatch.setattr(budget_proxy.httpx, "post", routed_post)
    with running(current, configured) as url:
        first = real_post(
            url,
            headers=probe_headers(),
            json=body,
            timeout=5,
        )
    assert first.status_code == 502
    assert first.json()["result"] == "ambiguous_reservation"
    assert current.health()["in_flight"] == 1

    restarted = state(tmp_path, configured)
    with running(restarted, configured) as url:
        replay = real_post(
            url,
            headers=probe_headers(),
            json=body,
            timeout=5,
        )
    assert replay.status_code == 409
    assert replay.json()["result"] == "ambiguous_reservation"
    assert replay.json()["reused"] is True
    assert restarted.health()["in_flight"] == 1
    assert upstream_calls == 1


def test_success_commit_crash_repairs_to_original_auth_episode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = settings()
    current = state(tmp_path, configured)
    body = probe_payload(seed_auth_incident(current), "gpt_oss", "8")
    spec, replay = current.reserve_recovery_probe(**body)
    assert replay is None
    real_atomic_json = budget_proxy.atomic_json
    failed = False

    def crash_state_commit(path, value):
        nonlocal failed
        if path == current.state_path and not failed:
            failed = True
            raise RuntimeError("simulated state commit crash")
        real_atomic_json(path, value)

    monkeypatch.setattr(budget_proxy, "atomic_json", crash_state_commit)
    with pytest.raises(RuntimeError, match="state commit crash"):
        current.record(
            str(spec["request_id"]),
            {"prompt_tokens": 1, "completion_tokens": 1},
        )
    monkeypatch.setattr(budget_proxy, "atomic_json", real_atomic_json)
    current.open_circuit(
        "ambiguous upstream failure: recovery probe RuntimeError",
        keep_request=True,
        request_id=str(spec["request_id"]),
    )

    restarted = state(tmp_path, configured)
    assert restarted.health()["in_flight"] == 0
    assert restarted.health()["circuit_open"] is True
    assert restarted.health()["circuit_reason"] == "provider returned 401"
    assert len(restarted.ledger["calls"]) == 1


def test_probe_maximum_reconciliation_restores_auth_episode_and_lineage(
    tmp_path: Path,
) -> None:
    configured = settings()
    current = state(tmp_path, configured)
    body = probe_payload(seed_auth_incident(current), "qwen", "7")
    spec, replay = current.reserve_recovery_probe(**body)
    assert replay is None
    current.open_circuit(
        "ambiguous upstream failure: recovery probe ReadTimeout",
        keep_request=True,
        request_id=str(spec["request_id"]),
    )
    manifest = current.exact_reservation_manifest()
    result = reconcile_ambiguous.reconcile(
        current.ledger_path,
        current.state_path,
        acknowledgement=True,
        expected_manifest_sha256=str(manifest["sha256"]),
        expected_guard=configured.guard_fingerprint(),
        expected_initial_provider_spend_usd=(
            configured.initial_provider_spend_usd
        ),
        provider_max_in_flight=configured.provider_max_in_flight,
    )
    assert result["maximum_charged_ids"] == [spec["request_id"]]
    restarted = state(tmp_path, configured)
    assert restarted.health()["in_flight"] == 0
    assert restarted.health()["circuit_open"] is True
    assert restarted.health()["circuit_reason"] == "provider returned 401"
    call = restarted.ledger["calls"][-1]
    assert call["recovery_probe_route"] == "qwen"
    assert call["recovery_auth_incident_request_id"] == body[
        "auth_incident_request_id"
    ]
    assert len(call["recovery_gate_sha256"]) == 64
    _, terminal = restarted.reserve_recovery_probe(**body)
    assert terminal is not None
    assert terminal["result"] == "ambiguous_reconciled"


def test_recovery_probe_uses_normal_rate_guard_without_forwarding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = settings(rpm_limit=1)
    current = state(tmp_path, configured)
    body = probe_payload(seed_auth_incident(current), "qwen", "e")

    def forbidden_forward(*args, **kwargs):
        raise AssertionError("rate-blocked probe reached provider")

    monkeypatch.setattr(budget_proxy.httpx, "post", forbidden_forward)
    with running(current, configured) as url:
        response = httpx.Client().post(
            url,
                headers=probe_headers(),
            json=body,
            timeout=5,
        )
    assert response.status_code == 429
    assert response.headers["retry-after"] == "2"
    assert current.health()["in_flight"] == 0


@pytest.mark.parametrize("status", [429, 500, 503, 599])
def test_retryable_probe_failure_preserves_auth_episode_for_new_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    configured = settings()
    current = state(tmp_path, configured)
    incident_id = seed_auth_incident(current)
    first_body = probe_payload(incident_id, "qwen", "e")
    second_body = probe_payload(incident_id, "qwen", "f")
    real_post = budget_proxy.httpx.post
    upstream_calls = 0

    def routed_post(url, *args, **kwargs):
        nonlocal upstream_calls
        if str(url).startswith(configured.provider_base_url):
            upstream_calls += 1
            if upstream_calls == 2:
                return httpx.Response(
                    200,
                    json={
                        "usage": {"prompt_tokens": 1, "completion_tokens": 0},
                        "data": [{"embedding": [0.1, -0.2]}],
                    },
                    request=httpx.Request("POST", str(url)),
                )
            return httpx.Response(
                status,
                headers={"retry-after": "7"},
                json={"error": {"message": "unavailable"}},
                request=httpx.Request("POST", str(url)),
            )
        return real_post(url, *args, **kwargs)

    monkeypatch.setattr(budget_proxy.httpx, "post", routed_post)
    with running(current, configured) as url:
        first = real_post(
            url,
            headers=probe_headers(),
            json=first_body,
            timeout=5,
        )
        second = real_post(
            url,
            headers=probe_headers(),
            json=second_body,
            timeout=5,
        )
    assert first.status_code == 200
    assert first.json()["result"] == "retryable_failure"
    assert first.json()["upstream_status"] == status
    assert first.json()["retry_after"] == "7"
    assert second.status_code == 200
    assert second.json()["result"] == "authenticated"
    assert upstream_calls == 2
    assert current.health()["in_flight"] == 0
    assert current.health()["circuit_reason"] == "provider returned 401"
    assert current.value["provider_failure_history"][-1]["retry_after"] == "7"


def test_unexpected_nonretryable_probe_failure_releases_and_hard_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = settings()
    current = state(tmp_path, configured)
    body = probe_payload(seed_auth_incident(current), "qwen", "f")
    real_post = budget_proxy.httpx.post

    def routed_post(url, *args, **kwargs):
        if str(url).startswith(configured.provider_base_url):
            return httpx.Response(
                403,
                json={"error": {"message": "forbidden"}},
                request=httpx.Request("POST", str(url)),
            )
        return real_post(url, *args, **kwargs)

    monkeypatch.setattr(budget_proxy.httpx, "post", routed_post)
    with running(current, configured) as url:
        response = real_post(
            url, headers=probe_headers(), json=body, timeout=5
        )
    assert response.status_code == 200
    assert response.json()["result"] == "unexpected_failure"
    assert response.json()["upstream_status"] == 403
    assert current.health()["in_flight"] == 0
    assert current.health()["circuit_reason"].endswith("403")


def control_marker_value(
    *,
    control: str,
    nonce: str,
    roll_sha256: str = "a" * 64,
    enabled: bool = False,
) -> dict[str, object]:
    return {
        "version": 2,
        "control": control,
        "nonce": nonce,
        "roll_sha256": roll_sha256,
        "enabled": enabled,
        "secret_present": enabled,
        "completed": True,
    }


def test_control_marker_writer_is_atomic_exact_and_mode_600(tmp_path: Path) -> None:
    value = control_marker.write_control_marker(
        tmp_path,
        control="reconcile",
        nonce="0" * 32,
        roll_sha256="a" * 64,
        enabled=False,
        secret_present=False,
    )
    path = tmp_path / "reconcile-control.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.read_text().endswith("\n")
    assert value == control_marker_value(control="reconcile", nonce="0" * 32)
    with pytest.raises(budget_proxy.GuardViolation, match="bits disagree"):
        control_marker.write_control_marker(
            tmp_path,
            control="auth_reset",
            nonce="0" * 32,
            roll_sha256="a" * 64,
            enabled=False,
            secret_present=True,
        )


def test_recovery_control_markers_require_same_roll_and_both_disabled(
    tmp_path: Path,
) -> None:
    nonce = "1" * 32
    roll_sha256 = "a" * 64
    configured = settings(
        recovery_control_nonce=nonce,
        recovery_control_roll_sha256=roll_sha256,
    )
    current = state(tmp_path, configured)
    assert current.recovery_control_status()["sealed_disabled"] is False

    budget_proxy.atomic_json(
        tmp_path / "reconcile-control.json",
        control_marker_value(
            control="reconcile", nonce=nonce, roll_sha256=roll_sha256
        ),
    )
    assert current.recovery_control_status()["sealed_disabled"] is False
    budget_proxy.atomic_json(
        tmp_path / "auth-reset-control.json",
        control_marker_value(
            control="auth_reset",
            nonce="2" * 32,
            roll_sha256=roll_sha256,
        ),
    )
    stale = current.recovery_control_status()
    assert stale["sealed_disabled"] is False
    assert stale["reconcile"]["nonce_matches"] is True
    assert stale["auth_reset"]["nonce_matches"] is False

    budget_proxy.atomic_json(
        tmp_path / "auth-reset-control.json",
        control_marker_value(
            control="auth_reset",
            nonce=nonce,
            roll_sha256="b" * 64,
        ),
    )
    mixed_roll = current.recovery_control_status()
    assert mixed_roll["sealed_disabled"] is False
    assert mixed_roll["auth_reset"]["nonce_matches"] is True
    assert mixed_roll["auth_reset"]["roll_matches"] is False

    budget_proxy.atomic_json(
        tmp_path / "auth-reset-control.json",
        control_marker_value(
            control="auth_reset",
            nonce=nonce,
            roll_sha256=roll_sha256,
            enabled=True,
        ),
    )
    assert current.recovery_control_status()["sealed_disabled"] is False
    budget_proxy.atomic_json(
        tmp_path / "auth-reset-control.json",
        control_marker_value(
            control="auth_reset", nonce=nonce, roll_sha256=roll_sha256
        ),
    )
    sealed = current.recovery_control_status()
    assert sealed["sealed_disabled"] is True
    assert sealed["expected_nonce_sha256"] == hashlib.sha256(
        nonce.encode()
    ).hexdigest()
    assert sealed["expected_roll_sha256"] == roll_sha256
    health = current.health()
    assert health["recovery_controls_sealed_disabled"] is True
    assert health["recovery_control_nonce_sha256"] == sealed[
        "expected_nonce_sha256"
    ]
    assert health["recovery_control_roll_sha256"] == roll_sha256


def test_recovery_control_marker_endpoint_is_bat_authenticated_and_secret_free(
    tmp_path: Path,
) -> None:
    nonce = "3" * 32
    roll_sha256 = "c" * 64
    configured = settings(
        recovery_control_nonce=nonce,
        recovery_control_roll_sha256=roll_sha256,
    )
    current = state(tmp_path, configured)
    budget_proxy.atomic_json(
        tmp_path / "reconcile-control.json",
        control_marker_value(
            control="reconcile", nonce=nonce, roll_sha256=roll_sha256
        ),
    )
    budget_proxy.atomic_json(
        tmp_path / "auth-reset-control.json",
        control_marker_value(
            control="auth_reset", nonce=nonce, roll_sha256=roll_sha256
        ),
    )
    server = budget_proxy.BudgetProxyServer(
        ("127.0.0.1", 0), current, configured
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/admin/recovery-controls"
    try:
        assert httpx.get(url, timeout=5).status_code == 401
        assert (
            httpx.get(
                url,
                headers={"Authorization": "Bearer proxy-secret"},
                timeout=5,
            ).status_code
            == 401
        )
        response = httpx.get(
            url,
            headers={"Authorization": "Bearer admin-secret"},
            timeout=5,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert response.status_code == 200
    assert response.json()["sealed_disabled"] is True
    assert response.json()["expected_roll_sha256"] == roll_sha256
    assert nonce not in response.text
    assert "token" not in response.text.lower()
    assert "manifest" not in response.text.lower()


def test_recovery_control_marker_malformed_or_mixed_content_fails_closed(
    tmp_path: Path,
) -> None:
    nonce = "4" * 32
    roll_sha256 = "d" * 64
    configured = settings(
        recovery_control_nonce=nonce,
        recovery_control_roll_sha256=roll_sha256,
    )
    current = state(tmp_path, configured)
    (tmp_path / "reconcile-control.json").write_text("not-json")
    mixed = control_marker_value(
        control="auth_reset", nonce=nonce, roll_sha256=roll_sha256
    )
    mixed["secret_present"] = True
    budget_proxy.atomic_json(tmp_path / "auth-reset-control.json", mixed)
    status = current.recovery_control_status()
    assert status["sealed_disabled"] is False
    assert status["reconcile"]["valid"] is False
    assert status["auth_reset"]["valid"] is False


def test_recovery_control_nonce_env_is_exact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROVIDER_API_KEY", "provider-secret")
    monkeypatch.setenv("PROXY_API_KEY", "proxy-secret")
    monkeypatch.setenv("ADMIN_API_KEY", "admin-secret")
    monkeypatch.setenv("RECOVERY_CONTROL_NONCE", "NOT-LOWER-HEX")
    monkeypatch.setenv("RECOVERY_CONTROL_ROLL_SHA256", "a" * 64)
    with pytest.raises(SystemExit, match="32 lowercase hexadecimal"):
        budget_proxy.Settings.from_env()


def test_recovery_control_roll_env_is_exact_and_paired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROVIDER_API_KEY", "provider-secret")
    monkeypatch.setenv("PROXY_API_KEY", "proxy-secret")
    monkeypatch.setenv("ADMIN_API_KEY", "admin-secret")
    monkeypatch.setenv("RECOVERY_CONTROL_NONCE", "1" * 32)
    monkeypatch.setenv("RECOVERY_CONTROL_ROLL_SHA256", "NOT-LOWER-HEX")
    with pytest.raises(SystemExit, match="64 lowercase hexadecimal"):
        budget_proxy.Settings.from_env()
    monkeypatch.setenv("RECOVERY_CONTROL_ROLL_SHA256", "")
    with pytest.raises(SystemExit, match="configured together"):
        budget_proxy.Settings.from_env()
