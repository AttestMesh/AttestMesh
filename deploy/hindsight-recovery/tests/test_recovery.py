from decimal import Decimal
from pathlib import Path
import importlib.util
import sys


MODULE = Path(__file__).resolve().parents[1] / "recovery.py"
SPEC = importlib.util.spec_from_file_location("hindsight_recovery", MODULE)
assert SPEC and SPEC.loader
recovery = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = recovery
SPEC.loader.exec_module(recovery)


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
        stage_cap=3823,
        max_agent_active=2,
        max_provider_in_flight=6,
        expected_hindsight_failed=60,
        conservative_remaining_cost=Decimal("0.00543"),
        hard_budget=Decimal("30"),
        quick_seconds=2,
        full_seconds=60,
        watchdog_seconds=5,
        heartbeat_max_age=7,
    )


def test_projection_matches_authoritative_hold():
    assert recovery.compute_projection("16.267805999999982", 3263, config()) == Decimal("26.470775999999982")


def test_stage_cap_opens_only_at_exact_cap():
    assert recovery.stage_action(3822, "running", 3823) == "continue"
    assert recovery.stage_action(3823, "running", 3823) == "open"
    assert recovery.stage_action(3823, "ready", 3823) == "continue"
    assert recovery.stage_action(3824, "running", 3823) == "violation"


def test_precap_resume_accepts_one_completed_remote_submission():
    assert recovery.precap_resume_failure(
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
    ) is None


def test_precap_resume_accepts_two_completed_remote_submissions():
    assert recovery.precap_resume_failure(
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
    ) is None


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
    assert recovery.precap_resume_failure(
        attempted=3405,
        **{**common, "provider_in_flight": 1},
    ) == "pre-cap resume requires zero provider in-flight"


def test_watchdog_stale_heartbeat_fails_closed():
    assert recovery.watchdog_failure(
        armed=True,
        heartbeat_age=8,
        tunnel_ports_ready=True,
        guard_active=True,
        tunnel_active=True,
        max_age=7,
    ) == "guard heartbeat missing or stale"


def test_watchdog_unarmed_allows_ordered_startup():
    assert recovery.watchdog_failure(
        armed=False,
        heartbeat_age=None,
        tunnel_ports_ready=False,
        guard_active=False,
        tunnel_active=False,
        max_age=7,
    ) is None


def test_default_watchdog_age_allows_a_normal_full_audit(monkeypatch):
    monkeypatch.delenv("HEARTBEAT_MAX_AGE_SECONDS", raising=False)
    assert recovery.Config.from_env().heartbeat_max_age == 60


def test_permanent_concurrency_is_parameterized_at_three():
    root = Path(__file__).resolve().parents[2]
    driver = (root / "hindsight-node.sh").read_text()
    compose = (root / "compose/hindsight-node.yaml").read_text()
    box = (root / "hindsight-node-box.py").read_text()
    assert 'HINDSIGHT_LLM_MAX_CONCURRENT="${HINDSIGHT_LLM_MAX_CONCURRENT:-3}"' in driver
    assert "HINDSIGHT_API_LLM_MAX_CONCURRENT: ${HINDSIGHT_API_LLM_MAX_CONCURRENT}" in compose
    assert '"HINDSIGHT_API_LLM_MAX_CONCURRENT"' in box


def test_provider_guard_default_is_six(monkeypatch):
    monkeypatch.delenv("MAX_PROVIDER_IN_FLIGHT", raising=False)
    assert recovery.Config.from_env().max_provider_in_flight == 6


def test_agent_worker_default_is_two(monkeypatch):
    monkeypatch.delenv("MAX_AGENT_ACTIVE", raising=False)
    assert recovery.Config.from_env().max_agent_active == 2
    root = Path(__file__).resolve().parents[2]
    driver = (root / "agent-session-mcp-node.sh").read_text()
    assert 'HINDSIGHT_OUTBOX_MAX_IN_FLIGHT="${HINDSIGHT_OUTBOX_MAX_IN_FLIGHT:-2}"' in driver
    assert 'E_HINDSIGHT_OUTBOX_MAX_IN_FLIGHT=%q\\n\' "${HINDSIGHT_OUTBOX_MAX_IN_FLIGHT:-2}"' in driver


def test_explicit_recovery_controls_override_sealed_production_defaults():
    root = Path(__file__).resolve().parents[2]
    driver = (root / "hindsight-node.sh").read_text()
    capture = driver.index('_CALLER_RECONCILE_SET="${HINDSIGHT_RECONCILE_AMBIGUOUS_ENABLED+x}"')
    production_import = driver.index('source "$PRODUCTION_ENV_FILE"')
    restore = driver.index('HINDSIGHT_RECONCILE_AMBIGUOUS_ENABLED="$_CALLER_RECONCILE_VALUE"')
    assert capture < production_import < restore
