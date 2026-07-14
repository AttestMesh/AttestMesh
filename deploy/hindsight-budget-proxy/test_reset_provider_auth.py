import json

import pytest

from reset_provider_auth import reset_provider_auth_circuit


def write(path, value):
    path.write_text(json.dumps(value))


def test_reset_clears_only_reservation_free_auth_circuit(tmp_path):
    state = tmp_path / "state.json"
    write(
        state,
        {
            "circuit_open": True,
            "reason": "provider returned 401",
            "in_flight": {},
        },
    )

    result = reset_provider_auth_circuit(
        state, acknowledgement=True, reset_token="one-shot-a"
    )

    assert result["reset"] is True
    saved = json.loads(state.read_text())
    assert saved["circuit_open"] is False
    assert saved["in_flight"] == {}
    assert "reason" not in saved
    assert saved["last_provider_auth_reset"]["previous_reason"] == "provider returned 401"
    assert len(saved["consumed_provider_auth_reset_tokens"]) == 1
    assert state.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "value,match",
    [
        (
            {"circuit_open": True, "reason": "provider returned 401", "in_flight": {"x": {}}},
            "ambiguous",
        ),
        (
            {"circuit_open": True, "reason": "local budget exceeded", "in_flight": {}},
            "not a provider-auth",
        ),
        (
            {"circuit_open": False, "in_flight": {}},
            "not open",
        ),
    ],
)
def test_reset_fails_closed_for_other_states(tmp_path, value, match):
    state = tmp_path / "state.json"
    write(state, value)
    with pytest.raises(ValueError, match=match):
        reset_provider_auth_circuit(
            state, acknowledgement=True, reset_token="one-shot-b"
        )


def test_reset_requires_explicit_acknowledgement(tmp_path):
    state = tmp_path / "state.json"
    write(
        state,
        {"circuit_open": True, "reason": "provider returned 402", "in_flight": {}},
    )
    with pytest.raises(ValueError, match="acknowledgement"):
        reset_provider_auth_circuit(
            state, acknowledgement=False, reset_token="one-shot-c"
        )


def test_reset_token_is_one_shot(tmp_path):
    state = tmp_path / "state.json"
    write(
        state,
        {
            "circuit_open": True,
            "reason": "provider returned 401",
            "in_flight": {},
            "consumed_provider_auth_reset_tokens": [],
        },
    )
    reset_provider_auth_circuit(state, acknowledgement=True, reset_token="single-use")
    saved = json.loads(state.read_text())
    saved.update(
        {"circuit_open": True, "reason": "provider returned 401", "in_flight": {}}
    )
    write(state, saved)
    with pytest.raises(ValueError, match="already been consumed"):
        reset_provider_auth_circuit(
            state, acknowledgement=True, reset_token="single-use"
        )
