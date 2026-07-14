from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).with_name("canary.py")
SPEC = importlib.util.spec_from_file_location("hindsight_canary", MODULE_PATH)
assert SPEC and SPEC.loader
canary = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(canary)
sys.modules["canary"] = canary
EVALUATION_PATH = Path(__file__).with_name("evaluation.py")
EVALUATION_SPEC = importlib.util.spec_from_file_location("hindsight_evaluation", EVALUATION_PATH)
assert EVALUATION_SPEC and EVALUATION_SPEC.loader
evaluation = importlib.util.module_from_spec(EVALUATION_SPEC)
EVALUATION_SPEC.loader.exec_module(evaluation)
BUDGET_PROXY_PATH = Path(__file__).with_name("budget_proxy.py")
BUDGET_PROXY_SPEC = importlib.util.spec_from_file_location(
    "hindsight_budget_proxy", BUDGET_PROXY_PATH
)
assert BUDGET_PROXY_SPEC and BUDGET_PROXY_SPEC.loader
budget_proxy = importlib.util.module_from_spec(BUDGET_PROXY_SPEC)
BUDGET_PROXY_SPEC.loader.exec_module(budget_proxy)


def test_stratified_pick_is_deterministic_and_exact() -> None:
    rows = [
        {
            "content_hash": f"hash-{index}",
            "tool": "codex" if index % 2 else "claude",
            "project_path": f"/p/{index % 4}",
            "started_at": f"2026-07-{index % 9 + 1:02d}T00:00:00Z",
            "transcript_chars": 100 + index * 10,
            "title": f"title-{index}",
            "session_id": str(index),
            "summary": f"supplied-{index}",
            "first_user": "hello",
            "last_assistant": "done",
        }
        for index in range(40)
    ]
    first = canary.stratified_pick([dict(row) for row in rows], 17)
    second = canary.stratified_pick([dict(row) for row in rows], 17)
    assert [row["content_hash"] for row in first] == [row["content_hash"] for row in second]
    assert len(first) == 17
    assert {row["tool"] for row in first} == {"codex", "claude"}


def test_message_chunks_preserve_boundaries_and_card_is_bounded() -> None:
    messages = [
        {"seq": index, "role": "user", "text": "x" * 80}
        for index in range(5)
    ]
    chunks = canary.message_chunks(messages, max_chars=200)
    assert [message["seq"] for chunk in chunks for message in chunk] == list(range(5))
    text = canary.card_text(
        {
            "objective": "o" * 4000,
            "outcome_status": "done",
            "decisions": ["d"],
            "errors_blockers": [],
            "artifacts": ["a"],
            "follow_ups": ["f"],
            "source_sequences": [1, 2],
        }
    )
    assert len(text) <= 3000
    assert "Source sequences" in text


def test_parse_json_object_accepts_fence_and_trailing_material() -> None:
    value = canary.parse_json_object(
        '```json\n{"objective":"recover","source_sequences":[1]}\n```\n'
        '{"ignored":"trailing block"}'
    )
    assert value == {"objective": "recover", "source_sequences": [1]}


def test_parse_json_object_rejects_non_object_output() -> None:
    with pytest.raises(ValueError, match="does not contain a JSON object"):
        canary.parse_json_object('["not", "an", "object"]')


def test_explicit_retry_is_visible_and_single_use() -> None:
    value = {
        "state": "failed",
        "operation_id": "operation-1",
        "error": "402 ambiguous upstream failure",
        "payload_hash": "hash-1",
    }
    canary.stage_explicit_retry(value)
    assert value["state"] == "retry_pending"
    assert value["explicit_retries"] == 1
    assert value["retry_history"][0]["operation_id"] == "operation-1"
    assert value["retry_history"][0]["error"] == "402 ambiguous upstream failure"
    assert "operation_id" not in value
    assert "error" not in value

    canary.stage_explicit_retry(value)
    assert value["explicit_retries"] == 1
    assert len(value["retry_history"]) == 1


def test_collect_recalls_preserves_question_order() -> None:
    class FakeAPI:
        def request(self, _method, _endpoint, **kwargs):
            return {"query": kwargs["json"]["query"]}

    questions = [{"question": f"question-{index}"} for index in range(12)]
    recalls = evaluation.collect_recalls(FakeAPI(), questions, concurrency=4)
    assert [row["query"] for row in recalls] == [
        row["question"] for row in questions
    ]


def test_rate_capacity_delay_enforces_rolling_rpm_and_tpm() -> None:
    now = datetime(2026, 7, 10, tzinfo=timezone.utc).timestamp()
    calls = [
        {
            "started_at": datetime.fromtimestamp(
                now - 59 + index / 100, timezone.utc
            ).isoformat(),
            "input_tokens": 10,
            "output_tokens": 5,
        }
        for index in range(8)
    ]
    reservation = {"input_tokens": 100, "output_tokens": 50}
    assert canary.rate_capacity_delay(calls, reservation, 8, 1000, now) > 0
    assert canary.rate_capacity_delay(calls[:7], reservation, 8, 1000, now) == 0

    token_heavy = [
        {
            "started_at": datetime.fromtimestamp(now - 10, timezone.utc).isoformat(),
            "input_tokens": 900,
            "output_tokens": 50,
        }
    ]
    assert canary.rate_capacity_delay(token_heavy, reservation, 8, 1000, now) > 0


def test_budget_proxy_enforces_model_and_completion_guards() -> None:
    payload, prompt_bytes, maximum = budget_proxy.prepare_payload(
        {
            "model": "openai/gpt-oss-20b",
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 512,
        }
    )
    assert payload["reasoning_effort"] == "low"
    assert payload["include_reasoning"] is False
    assert payload["max_completion_tokens"] == 512
    assert "max_tokens" not in payload
    assert prompt_bytes > 0
    assert maximum == 512

    with pytest.raises(ValueError, match="outside the canary allowlist"):
        budget_proxy.prepare_payload({"model": "unapproved", "messages": []})
    with pytest.raises(ValueError, match="between 1 and 8192"):
        budget_proxy.prepare_payload(
            {
                "model": "openai/gpt-oss-120b",
                "messages": [],
                "max_completion_tokens": 8193,
            }
        )


def test_canary_items_detect_stable_variants() -> None:
    corpus = [
        {
            "source": "agent_sessions",
            "document_id": "abc",
            "summary": "summary",
            "started_at": "2026-07-01T00:00:00Z",
        }
    ]
    cards = [{"document_id": "abc", "status": "ok", "card": "card"}]
    summary = canary.canary_items(corpus, cards, "summary")[0]
    card = canary.canary_items(corpus, cards, "card")[0]
    assert summary["document_id"] == "canary:summary:agent_sessions:abc"
    assert card["document_id"] == "canary:card:agent_sessions:abc"


def test_validate_card_rejects_unknown_source_sequences() -> None:
    record = {"messages": [{"seq": 1}, {"seq": 2}]}
    value = {
        "objective": "test",
        "outcome_status": "done",
        "decisions": [],
        "errors_blockers": [],
        "artifacts": [],
        "follow_ups": [],
        "source_sequences": [3],
    }
    with pytest.raises(ValueError, match="unknown source sequence"):
        canary.validate_card(record, value)


def test_validate_questions_discards_invalid_candidates_and_keeps_twenty() -> None:
    candidates = [
        {
            "question": f"Question {index}",
            "answer": f"Answer {index}",
            "critical": False,
            "evidence": [
                {
                    "document_id": "doc-1",
                    "source_sequences": [999 if index < 5 else 1],
                }
            ],
        }
        for index in range(25)
    ]
    result = canary.validate_questions(
        "decisions_outcomes", candidates, {"doc-1"}, {"doc-1": {1}}
    )
    assert len(result) == 20
    assert all(row["evidence"][0]["source_sequences"] == [1] for row in result)


def test_build_cross_session_questions_uses_two_grounded_documents() -> None:
    corpus = [
        {
            "document_id": f"doc-{index}",
            "tool": "codex",
            "project_path": "/shared",
        }
        for index in range(40)
    ]
    questions = [
        {
            "question": f"What happened to shared component {index}?",
            "answer": f"Component {index} was verified",
            "critical": False,
            "evidence": [
                {"document_id": f"doc-{index}", "source_sequences": [index]}
            ],
        }
        for index in range(40)
    ]
    result = canary.build_cross_session_questions(questions, corpus)
    assert len(result) == 20
    assert all(
        len({item["document_id"] for item in row["evidence"]}) >= 2
        for row in result
    )


def test_gate_prefers_20b_summaries_and_hybrid_only_when_all_gates_pass() -> None:
    metrics = {
        "models": {
            "20b": {"fact_coverage": 0.90, "schema_retry_failure_rate": 0.01, "critical_misses": 0},
            "120b": {"fact_coverage": 0.92, "schema_retry_failure_rate": 0.0, "critical_misses": 0},
        },
        "variants": {
            "summary": {"utility_pass": True, "score": 0.88},
            "session_card": {"utility_pass": True, "score": 0.90},
        },
        "routes": {
            "postgres": {
                "overall_correctness": 0.80,
                "exact_correctness": 0.90,
                "temporal_cross_correctness": 0.60,
                "mean_context_chars": 1000,
            },
            "hybrid": {
                "overall_correctness": 0.82,
                "exact_correctness": 0.90,
                "temporal_cross_correctness": 0.70,
                "mean_context_chars": 900,
                "unsupported_answers": 0,
                "provenance_completeness": 1.0,
            },
        },
    }
    decision = canary.gate_decision(metrics)
    assert decision["selected_model"] == "20b"
    assert decision["selected_variant"] == "summary"
    assert decision["keep_hindsight"] is True
    assert decision["production_backend"] == "hybrid"


def test_evaluation_scores_authoritative_provenance(tmp_path: Path) -> None:
    run = tmp_path / "run.jsonl"
    canary.write_jsonl(
        run,
        [
            {
                "question": {
                    "id": "exact-1",
                    "category": "exact_identifiers_errors",
                    "question": "What error occurred?",
                    "answer": "ERR_WIDGET_42",
                    "critical": True,
                    "evidence": [{"document_id": "doc-1", "source_sequences": [4]}],
                },
                "routes": {
                    "postgres": {
                        "contexts": ["failed with ERR_WIDGET_42"],
                        "provenance": [
                            {
                                "source": "agent_sessions",
                                "document_id": "doc-1",
                                "source_id": 1,
                            }
                        ],
                    },
                    "hindsight": {
                        "contexts": ["failed with ERR_WIDGET_42"],
                        "provenance": [{"document_id": "doc-1"}],
                    },
                    "hybrid": {
                        "contexts": ["failed with ERR_WIDGET_42"],
                        "provenance": [
                            {
                                "source": "agent_sessions",
                                "document_id": "doc-1",
                                "source_id": 1,
                            }
                        ],
                    },
                },
            }
        ],
    )
    scored = evaluation.score_run(str(run))
    assert scored["postgres"]["overall_correctness"] == 1.0
    assert scored["hybrid"]["exact_correctness"] == 1.0
    assert scored["hybrid"]["provenance_completeness"] == 1.0
    assert scored["hybrid"]["critical_misses"] == 0


def test_evaluation_requires_semantic_answer_evidence_not_only_document_id(
    tmp_path: Path,
) -> None:
    run = tmp_path / "semantic.jsonl"
    canary.write_jsonl(
        run,
        [
            {
                "question": {
                    "id": "decision-1",
                    "category": "decisions_outcomes",
                    "question": "What backend was selected?",
                    "answer": "Postgres-only mode was selected",
                    "critical": False,
                    "evidence": [
                        {"document_id": "doc-1", "source_sequences": [4]}
                    ],
                },
                "routes": {
                    "postgres": {
                        "contexts": ["The meeting started at noon."],
                        "provenance": [
                            {
                                "source": "agent_sessions",
                                "document_id": "doc-1",
                                "source_id": 1,
                            }
                        ],
                    }
                },
            }
        ],
    )
    scored = evaluation.score_run(str(run))
    assert scored["postgres"]["overall_correctness"] == 0.0


def test_evaluate_spec_handles_a_schema_failed_card_variant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    routes = {
        "postgres": {
            "overall_correctness": 0.8,
            "exact_correctness": 0.8,
            "temporal_cross_correctness": 0.5,
            "mean_context_chars": 1000,
            "unsupported_answers": 0,
            "provenance_completeness": 1.0,
            "critical_misses": 0,
            "fact_coverage": 0.8,
        },
        "hindsight": {
            "overall_correctness": 0.85,
            "exact_correctness": 0.8,
            "temporal_cross_correctness": 0.65,
            "mean_context_chars": 500,
            "unsupported_answers": 0,
            "provenance_completeness": 0.0,
            "critical_misses": 0,
            "fact_coverage": 0.85,
        },
        "hybrid": {
            "overall_correctness": 0.85,
            "exact_correctness": 0.8,
            "temporal_cross_correctness": 0.65,
            "mean_context_chars": 500,
            "unsupported_answers": 0,
            "provenance_completeness": 1.0,
            "critical_misses": 0,
            "fact_coverage": 0.85,
        },
    }
    monkeypatch.setattr(evaluation, "score_run", lambda _path: routes)
    spec = tmp_path / "spec.json"
    spec.write_text(
        json.dumps(
            {
                "runs": {
                    "20b": {
                        "summary": {
                            "path": "20b.jsonl",
                            "schema_retry_failure_rate": 0,
                        }
                    },
                    "120b": {
                        "summary": {
                            "path": "120b.jsonl",
                            "schema_retry_failure_rate": 0,
                        }
                    },
                },
                "unavailable_variants": {"session_card": {"reason": "schema"}},
            }
        )
    )
    result = evaluation.evaluate_spec(str(spec))
    assert result["selection_basis"]["variant"] == "summary"
    assert result["variants"]["session_card"]["available"] is False


def test_evaluate_spec_allows_explicitly_disqualified_candidate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    routes = {
        "postgres": {
            "overall_correctness": 0.35,
            "exact_correctness": 0.15,
            "temporal_cross_correctness": 0.325,
            "mean_context_chars": 1000,
            "unsupported_answers": 0,
            "provenance_completeness": 1.0,
            "critical_misses": 1,
            "fact_coverage": 0.35,
        },
        "hindsight": {
            "overall_correctness": 0.54,
            "exact_correctness": 0.65,
            "temporal_cross_correctness": 0.375,
            "mean_context_chars": 500,
            "unsupported_answers": 0,
            "provenance_completeness": 0.0,
            "critical_misses": 6,
            "fact_coverage": 0.54,
        },
        "hybrid": {
            "overall_correctness": 0.46,
            "exact_correctness": 0.25,
            "temporal_cross_correctness": 0.45,
            "mean_context_chars": 800,
            "unsupported_answers": 0,
            "provenance_completeness": 1.0,
            "critical_misses": 4,
            "fact_coverage": 0.46,
        },
    }
    monkeypatch.setattr(evaluation, "score_run", lambda _path: routes)
    spec = tmp_path / "spec.json"
    spec.write_text(
        json.dumps(
            {
                "runs": {
                    "120b": {
                        "summary": {
                            "path": "120b.jsonl",
                            "schema_retry_failure_rate": 0,
                        }
                    }
                },
                "disqualified_models": {
                    "20b": {
                        "reason": "stopped early to select the completed control",
                        "schema_retry_failure_rate": 0.02,
                        "critical_misses": 1,
                    }
                },
                "unavailable_variants": {"session_card": {"reason": "schema"}},
            }
        )
    )

    result = evaluation.evaluate_spec(str(spec))

    assert result["selection_basis"]["model"] == "120b"
    assert result["models"]["20b"]["qualified"] is False
    assert result["models"]["20b"]["reason"] == "stopped early to select the completed control"


def test_hybrid_postgres_search_splits_hindsight_candidate_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    def fake_agent(_dsn, _vector, _query, _limit, document_ids):
        calls["agent"] = document_ids
        return []

    def fake_pocket(_dsn, _vector, _query, _limit, document_ids):
        calls["pocket"] = document_ids
        return []

    monkeypatch.setattr(evaluation, "agent_search", fake_agent)
    monkeypatch.setattr(evaluation, "pocket_search", fake_pocket)
    evaluation.postgres_search(
        "agent",
        "pocket",
        [0.1],
        "query",
        document_ids=["agent-doc", "pocket:recording-1"],
    )

    assert calls == {
        "agent": ["agent-doc"],
        "pocket": ["pocket:recording-1"],
    }


def test_retain_reserves_unknown_remote_operations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = tmp_path / "corpus.jsonl"
    cards = tmp_path / "cards.jsonl"
    state = tmp_path / "state.json"
    records = [
        {
            "source": "agent_sessions",
            "document_id": f"doc-{index}",
            "summary": f"summary-{index}",
            "started_at": "2026-07-01T00:00:00Z",
        }
        for index in range(9)
    ]
    canary.write_jsonl(corpus, records)
    canary.write_jsonl(
        cards,
        [
            {"document_id": row["document_id"], "status": "ok", "card": "card"}
            for row in records
        ],
    )

    class FakeAPI:
        instance = None

        def __init__(self, *_args, **_kwargs):
            type(self).instance = self
            self.cycle = 0
            self.retained_by_cycle: dict[int, list[str]] = {}
            self.statuses: dict[str, dict[str, str]] = {}

        def active(self):
            self.cycle += 1
            return [
                {"id": f"remote-{index}", "status": "processing"}
                for index in range(3)
            ]

        def ensure_bank(self):
            return {}

        def status(self, operation_id, include_payload=False):
            _ = include_payload
            return self.statuses.get(operation_id, {"status": "processing"})

        def retain(self, item):
            self.retained_by_cycle.setdefault(self.cycle, []).append(item["document_id"])
            operation_id = "op-" + item["document_id"]
            self.statuses[operation_id] = {"status": "completed"}
            return operation_id

    monkeypatch.setattr(canary, "HindsightAPI", FakeAPI)
    monkeypatch.setattr(canary, "key_preflight", lambda: {"hard_budget_usd": 2})
    monkeypatch.setattr(canary.time, "sleep", lambda _seconds: None)
    monkeypatch.setenv("CANARY_HINDSIGHT_TOKEN", "token")

    result = canary.retain_canary(
        str(corpus), str(cards), "summary", "http://canary", "bank", str(state)
    )

    assert result == {
        "variant": "summary",
        "documents": 9,
        "succeeded": 9,
        "failed": 0,
        "schema_retry_failure_rate": 0.0,
    }
    assert [len(batch) for batch in FakeAPI.instance.retained_by_cycle.values()] == [5, 4]


def test_retain_stops_on_ambiguous_crash_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = tmp_path / "corpus.jsonl"
    cards = tmp_path / "cards.jsonl"
    state = tmp_path / "state.json"
    record = {
        "source": "agent_sessions",
        "document_id": "doc-1",
        "summary": "summary",
        "started_at": "2026-07-01T00:00:00Z",
    }
    canary.write_jsonl(corpus, [record])
    canary.write_jsonl(
        cards, [{"document_id": "doc-1", "status": "ok", "card": "card"}]
    )
    item = canary.canary_items([record], canary.read_jsonl(cards), "summary")[0]
    state.write_text(
        json.dumps(
            {
                "documents": {
                    item["document_id"]: {
                        "state": "submitting",
                        "payload_hash": canary.stable_key(item),
                    }
                }
            }
        )
    )

    class EmptyAPI:
        def __init__(self, *_args, **_kwargs):
            pass

        def active(self):
            return []

        def ensure_bank(self):
            return {}

    monkeypatch.setattr(canary, "HindsightAPI", EmptyAPI)
    monkeypatch.setattr(canary, "key_preflight", lambda: {"hard_budget_usd": 2})
    monkeypatch.setenv("CANARY_HINDSIGHT_TOKEN", "token")

    with pytest.raises(canary.SafetyStop, match="ambiguous submission"):
        canary.retain_canary(
            str(corpus), str(cards), "summary", "http://canary", "bank", str(state)
        )
