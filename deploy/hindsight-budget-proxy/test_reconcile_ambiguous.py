import json

import pytest

from reconcile_ambiguous import reconcile


def write(path, value):
    path.write_text(json.dumps(value))


def test_reconcile_charges_maximum_and_clears_circuit(tmp_path):
    ledger = tmp_path / "ledger.json"
    state = tmp_path / "state.json"
    write(ledger, {"provider_spent_usd": 1.0, "calls": []})
    write(
        state,
        {
            "circuit_open": True,
            "reason": "ambiguous upstream failure",
            "in_flight": {
                "request-1": {
                    "model": "test-model",
                    "endpoint": "chat",
                    "scope": "hindsight_backfill",
                    "period": "all",
                    "started_at": "2026-01-01T00:00:00+00:00",
                    "reservation": {
                        "input_tokens": 100,
                        "output_tokens": 200,
                        "cost_usd": 0.25,
                    },
                }
            },
        },
    )

    result = reconcile(ledger, state, acknowledgement=True)

    assert result == {
        "requests": 1,
        "charged_maximum_usd": 0.25,
        "provider_spent_usd": 1.25,
    }
    saved_ledger = json.loads(ledger.read_text())
    assert saved_ledger["calls"][0]["ambiguous_upstream_reconciled"] is True
    assert saved_ledger["calls"][0]["estimated_from_reservation"] is True
    saved_state = json.loads(state.read_text())
    assert saved_state["circuit_open"] is False
    assert saved_state["in_flight"] == {}
    assert saved_state["last_ambiguous_reconciliation"]["requests"] == 1


def test_reconcile_requires_acknowledgement_and_ambiguity(tmp_path):
    ledger = tmp_path / "ledger.json"
    state = tmp_path / "state.json"
    write(ledger, {"provider_spent_usd": 0, "calls": []})
    write(state, {"circuit_open": False, "in_flight": {}})
    with pytest.raises(ValueError, match="acknowledgement"):
        reconcile(ledger, state, acknowledgement=False)
    with pytest.raises(ValueError, match="no ambiguous reservations"):
        reconcile(ledger, state, acknowledgement=True)


def test_reconcile_accepts_prestartup_reservations_before_circuit_materializes(tmp_path):
    ledger = tmp_path / "ledger.json"
    state = tmp_path / "state.json"
    write(ledger, {"provider_spent_usd": 2.0, "calls": []})
    write(
        state,
        {
            "circuit_open": False,
            "in_flight": {
                "request-prestartup": {
                    "model": "test-model",
                    "endpoint": "chat",
                    "scope": "hindsight_backfill",
                    "period": "all",
                    "started_at": "2026-01-01T00:00:00+00:00",
                    "reservation": {
                        "input_tokens": 10,
                        "output_tokens": 20,
                        "cost_usd": 0.5,
                    },
                }
            },
        },
    )

    result = reconcile(ledger, state, acknowledgement=True)

    assert result["requests"] == 1
    assert result["charged_maximum_usd"] == 0.5
    saved_state = json.loads(state.read_text())
    assert saved_state["circuit_open"] is False
    assert saved_state["in_flight"] == {}
