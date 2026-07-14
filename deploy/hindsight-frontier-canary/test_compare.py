from copy import deepcopy

from compare import candidate, compare


def routes(overall=0.46, exact=0.25, temporal=0.45, critical=4):
    hybrid = {
        "overall_correctness": overall,
        "exact_correctness": exact,
        "temporal_cross_correctness": temporal,
        "critical_misses": critical,
        "unsupported_answers": 0,
        "provenance_completeness": 1.0,
        "mean_context_chars": 1000,
    }
    return {"hybrid": hybrid, "hindsight": deepcopy(hybrid), "postgres": deepcopy(hybrid)}


def test_candidate_requires_meaningful_gain_without_regression():
    baseline = routes()
    assert candidate("better", routes(overall=0.51), baseline)["qualified"] is True
    assert candidate("same", routes(), baseline)["qualified"] is False
    assert candidate("critical", routes(critical=3), baseline)["qualified"] is True
    assert candidate("exact-regression", routes(overall=0.60, exact=0.20), baseline)["qualified"] is False


def test_compare_keeps_120b_when_no_candidate_qualifies():
    result = compare(routes(), routes(), routes(overall=0.45), {}, {})
    assert result["selected_model"] == "gpt-oss-120b"
    assert result["upgrade_from_120b"] is False


def test_compare_records_pre_score_disqualification():
    details = {"gate": "terminal_failure_rate", "observed_rate": 4 / 17}
    result = compare(
        routes(),
        routes(overall=0.51),
        None,
        {},
        {},
        fugu_disqualification=details,
    )
    assert result["selected_model"] == "grok-4.5"
    assert result["candidates"]["fugu"]["qualified"] is False
    assert result["candidates"]["fugu"]["routes"] is None
    assert result["candidates"]["fugu"]["disqualification"] == details


def test_compare_preserves_ultra_disqualification_alongside_plain_fugu():
    ultra = {"gate": "terminal_failure_rate", "observed_rate": 0.0132}
    result = compare(
        routes(),
        routes(),
        routes(overall=0.51),
        {},
        {},
        fugu_ultra_disqualification=ultra,
        fugu_ultra_ledger={"total_tokens": 123},
    )
    assert result["selected_model"] == "fugu"
    assert result["candidates"]["fugu-ultra"]["disqualified"] is True
    assert result["candidates"]["fugu-ultra"]["disqualification"] == ultra
    assert result["usage"]["fugu-ultra"] == {"total_tokens": 123}
