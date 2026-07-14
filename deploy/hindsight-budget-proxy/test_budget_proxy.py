from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

import budget_proxy


def settings(**overrides) -> budget_proxy.Settings:
    base = budget_proxy.Settings(
        provider_base_url="https://provider.invalid/v1",
        provider_api_key="provider-secret",
        proxy_api_key="proxy-secret",
        provider_egress_enabled=False,
        provider_total_limit_usd=50,
        provider_safety_margin_usd=0.5,
        hindsight_phase="backfill",
        hindsight_backfill_limit_usd=30,
        hindsight_monthly_limit_usd=10,
        qwen_monthly_limit_usd=5,
        rpm_limit=30,
        tpm_limit=1_000_000,
        prices={
            budget_proxy.HINDSIGHT_MODEL: (0.15, 0.60),
            budget_proxy.QWEN_MODEL: (0.01, 0.0),
        },
        initial_provider_spend_usd=0.00000006,
    )
    return replace(base, **overrides)


def state(tmp_path: Path, configured: budget_proxy.Settings | None = None):
    return budget_proxy.BudgetState(
        tmp_path / "ledger.json",
        tmp_path / "state.json",
        configured or settings(),
    )


def test_payload_guards_pin_models_and_reasoning() -> None:
    payload, prompt_bytes, maximum = budget_proxy.prepare_chat_payload(
        {
            "model": budget_proxy.HINDSIGHT_MODEL,
            "messages": [{"role": "user", "content": "hello"}],
            "reasoning_effort": "low",
            "include_reasoning": False,
            "max_tokens": 1024,
        }
    )
    assert payload["max_completion_tokens"] == 1024
    assert "max_tokens" not in payload
    assert prompt_bytes > 0
    assert maximum == 1024
    with pytest.raises(budget_proxy.GuardViolation):
        budget_proxy.prepare_chat_payload(
            {"model": "other", "messages": [], "max_completion_tokens": 1}
        )
    with pytest.raises(budget_proxy.GuardViolation):
        budget_proxy.prepare_chat_payload(
            {
                "model": budget_proxy.HINDSIGHT_MODEL,
                "messages": [],
                "reasoning_effort": "high",
            }
        )


def test_embedding_guards_limit_model_batch_and_dimensions() -> None:
    payload, prompt_bytes, output = budget_proxy.prepare_embedding_payload(
        {"model": budget_proxy.QWEN_MODEL, "input": ["a", "b"], "dimensions": 1024}
    )
    assert payload["dimensions"] == 1024
    assert prompt_bytes > 0
    assert output == 0
    with pytest.raises(budget_proxy.GuardViolation):
        budget_proxy.prepare_embedding_payload(
            {"model": budget_proxy.QWEN_MODEL, "input": ["x"] * 65}
        )


def test_backfill_and_qwen_use_separate_scopes(tmp_path: Path) -> None:
    current = state(tmp_path)
    chat_id, chat = current.reserve("chat", budget_proxy.HINDSIGHT_MODEL, 1000, 1000)
    assert chat["scope"] == "hindsight_backfill"
    assert chat["period"] == "all"
    current.record(chat_id, {"prompt_tokens": 1000, "completion_tokens": 1000})
    qwen_id, qwen = current.reserve("embeddings", budget_proxy.QWEN_MODEL, 1000, 0)
    assert qwen["scope"] == "qwen_monthly"
    assert qwen["period"] == budget_proxy.utc_month()
    current.record(qwen_id, {"prompt_tokens": 1000, "completion_tokens": 0})
    assert current.health()["hindsight_backfill_spent_usd"] > 0
    assert current.health()["qwen_monthly_spent_usd"] > 0


def test_monthly_hindsight_scope_uses_utc_calendar_month(tmp_path: Path) -> None:
    current = state(tmp_path, settings(hindsight_phase="monthly"))
    request_id, reservation = current.reserve(
        "chat", budget_proxy.HINDSIGHT_MODEL, 100, 100
    )
    assert reservation["scope"] == "hindsight_monthly"
    assert reservation["period"] == datetime.now(timezone.utc).strftime("%Y-%m")
    current.discard(request_id)


def test_concurrent_reservations_count_against_scope_limit(tmp_path: Path) -> None:
    configured = settings(
        hindsight_backfill_limit_usd=0.001,
        provider_total_limit_usd=1,
        provider_safety_margin_usd=0.1,
    )
    current = state(tmp_path, configured)
    current.reserve("chat", budget_proxy.HINDSIGHT_MODEL, 1000, 1000)
    with pytest.raises(budget_proxy.BudgetViolation, match="backfill"):
        current.reserve("chat", budget_proxy.HINDSIGHT_MODEL, 1000, 1000)


def test_provider_total_includes_initial_and_pending_spend(tmp_path: Path) -> None:
    configured = settings(
        provider_total_limit_usd=0.002,
        provider_safety_margin_usd=0.0001,
        hindsight_backfill_limit_usd=0.0018,
        initial_provider_spend_usd=0.001,
    )
    current = state(tmp_path, configured)
    current.reserve("chat", budget_proxy.HINDSIGHT_MODEL, 100, 1000)
    with pytest.raises(budget_proxy.BudgetViolation, match="provider-total"):
        current.reserve("chat", budget_proxy.HINDSIGHT_MODEL, 100, 1000)


def test_provider_reported_cost_cannot_be_undercounted(tmp_path: Path) -> None:
    current = state(tmp_path)
    request_id, _ = current.reserve("embeddings", budget_proxy.QWEN_MODEL, 100, 0)
    call = current.record(
        request_id,
        {"prompt_tokens": 10, "completion_tokens": 0, "cost": 0.25},
    )
    assert call["cost_usd"] == 0.25
    assert current.ledger["provider_spent_usd"] == pytest.approx(0.25000006)


def test_reasoning_tokens_are_included_in_output_metering(tmp_path: Path) -> None:
    current = state(tmp_path)
    request_id, _ = current.reserve(
        "chat", budget_proxy.HINDSIGHT_MODEL, 100, 1000
    )
    call = current.record(
        request_id,
        {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 610},
    )
    assert call["input_tokens"] == 100
    assert call["output_tokens"] == 510


def test_restart_with_in_flight_request_opens_circuit(tmp_path: Path) -> None:
    current = state(tmp_path)
    current.reserve("chat", budget_proxy.HINDSIGHT_MODEL, 100, 100)
    restarted = state(tmp_path)
    assert restarted.value["circuit_open"] is True
    assert "ambiguous" in restarted.value["reason"]


def test_guard_configuration_mismatch_fails_closed(tmp_path: Path) -> None:
    state(tmp_path)
    with pytest.raises(SystemExit, match="guard"):
        state(tmp_path, settings(qwen_monthly_limit_usd=4))


def test_ledger_and_state_are_mode_600(tmp_path: Path) -> None:
    current = state(tmp_path)
    assert (tmp_path / "ledger.json").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "state.json").stat().st_mode & 0o777 == 0o600
    assert json.loads((tmp_path / "ledger.json").read_text())["version"] == 1
    assert current.health()["circuit_open"] is False
    assert current.health()["status"] == "maintenance"
