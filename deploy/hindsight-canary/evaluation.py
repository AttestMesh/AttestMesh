#!/usr/bin/env python3
"""Collect and score Postgres, Hindsight, and Hindsight→Postgres routes."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from canary import (
    HindsightAPI,
    QWEN_QUERY_INSTRUCTION,
    atomic_json,
    read_jsonl,
    write_jsonl,
)


RRF_K = 60


def vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(str(float(value)) for value in vector) + "]"


def fit_vector(vector: list[float], dim: int = 1024) -> list[float]:
    if len(vector) < dim:
        raise ValueError(f"embedding has {len(vector)} dimensions, expected at least {dim}")
    head = vector[:dim]
    norm = math.sqrt(sum(value * value for value in head)) or 1.0
    return [value / norm for value in head]


def embed_queries(queries: list[str]) -> list[list[float]]:
    import httpx

    key = os.getenv("CANARY_QWEN_API_KEY", "").strip()
    if not key:
        raise SystemExit("CANARY_QWEN_API_KEY is required")
    base = os.getenv("CANARY_QWEN_BASE_URL", "https://api.redpill.ai/v1").rstrip("/")
    model = os.getenv("CANARY_QWEN_MODEL", "qwen/qwen3-embedding-8b")
    output: list[list[float]] = []
    with httpx.Client(timeout=180) as client:
        for start in range(0, len(queries), 16):
            batch = [QWEN_QUERY_INSTRUCTION.format(query=query) for query in queries[start : start + 16]]
            response = client.post(
                f"{base}/embeddings",
                headers={"Authorization": f"Bearer {key}"},
                json={"model": model, "input": batch},
            )
            if response.status_code in {401, 402}:
                raise RuntimeError(f"Qwen preflight/call returned {response.status_code}")
            response.raise_for_status()
            data = sorted(response.json().get("data") or [], key=lambda item: item.get("index", 0))
            if len(data) != len(batch):
                raise ValueError("embedding response count mismatch")
            output.extend(fit_vector([float(value) for value in item["embedding"]]) for item in data)
    return output


def _fuse(arms: list[list[dict[str, Any]]], limit: int = 12) -> list[dict[str, Any]]:
    fused: dict[tuple[str, str], dict[str, Any]] = {}
    for arm_index, rows in enumerate(arms):
        seen: set[tuple[str, str]] = set()
        for rank, row in enumerate(rows, start=1):
            key = (row["source"], row["document_id"])
            if key in seen:
                continue
            seen.add(key)
            item = fused.setdefault(key, {**row, "score": 0.0, "arms": {}})
            item["score"] += 1.0 / (RRF_K + rank)
            item["arms"][str(arm_index)] = rank
            if not item.get("context") and row.get("context"):
                item["context"] = row["context"]
    return sorted(fused.values(), key=lambda row: (-row["score"], row["source"], row["document_id"]))[:limit]


def agent_search(
    dsn: str,
    vector: list[float],
    query: str,
    limit: int = 12,
    document_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    import psycopg
    from psycopg.rows import dict_row

    value = vector_literal(vector)
    if document_ids is not None and not document_ids:
        return []
    document_filter = " AND s.content_hash = ANY(%s)" if document_ids is not None else ""
    common = """
        s.content_hash AS document_id, s.id AS source_id, s.started_at,
        s.source_path, s.tool, s.host_id
    """
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {common}, m.id AS evidence_id, m.seq,
                       m.text AS context, m.embedding <=> %s::vector AS distance
                FROM messages m JOIN sessions s ON s.id = m.session_id
                WHERE m.embedding IS NOT NULL
                {document_filter}
                ORDER BY m.embedding <=> %s::vector LIMIT 48
                """,
                [value, *([document_ids] if document_ids is not None else []), value],
            )
            message_vector = list(cur.fetchall())
            cur.execute(
                f"""
                SELECT {common}, NULL::bigint AS evidence_id, -1 AS seq,
                       coalesce(s.summary, s.title, '') AS context,
                       s.summary_embedding <=> %s::vector AS distance
                FROM sessions s WHERE s.summary_embedding IS NOT NULL
                {document_filter}
                ORDER BY s.summary_embedding <=> %s::vector LIMIT 48
                """,
                [value, *([document_ids] if document_ids is not None else []), value],
            )
            summary_vector = list(cur.fetchall())
            cur.execute(
                f"""
                WITH q AS (SELECT websearch_to_tsquery('english', %s) query)
                SELECT {common}, m.id AS evidence_id, m.seq, m.text AS context,
                       ts_rank_cd(m.tsv, q.query) AS rank
                FROM messages m JOIN sessions s ON s.id = m.session_id CROSS JOIN q
                WHERE m.tsv @@ q.query {document_filter}
                ORDER BY rank DESC, m.id LIMIT 48
                """,
                [query, *([document_ids] if document_ids is not None else [])],
            )
            fts = list(cur.fetchall())
    arms: list[list[dict[str, Any]]] = []
    for rows in (message_vector, summary_vector, fts):
        arms.append([{**dict(row), "source": "agent_sessions"} for row in rows])
    return _fuse(arms, limit)


def pocket_search(
    dsn: str,
    vector: list[float],
    query: str,
    limit: int = 12,
    document_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    import psycopg
    from psycopg.rows import dict_row

    value = vector_literal(vector)
    if document_ids is not None and not document_ids:
        return []
    native_ids = (
        [document_id.removeprefix("pocket:") for document_id in document_ids]
        if document_ids is not None
        else None
    )
    document_filter = " AND r.pocket_id = ANY(%s)" if native_ids is not None else ""
    common = """
        ('pocket:' || r.pocket_id) AS document_id, r.id AS source_id,
        r.created_at AS started_at, r.pocket_id AS source_path,
        'pocket'::text AS tool, NULL::text AS host_id
    """
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {common}, s.id AS evidence_id, s.seq, s.text AS context,
                       s.embedding <=> %s::vector AS distance
                FROM segments s JOIN recordings r ON r.id = s.recording_id
                WHERE s.embedding IS NOT NULL
                {document_filter}
                ORDER BY s.embedding <=> %s::vector LIMIT 48
                """,
                [value, *([native_ids] if native_ids is not None else []), value],
            )
            segment_vector = list(cur.fetchall())
            cur.execute(
                f"""
                SELECT {common}, NULL::bigint AS evidence_id, -1 AS seq,
                       coalesce(r.summary, r.title, '') AS context,
                       r.summary_embedding <=> %s::vector AS distance
                FROM recordings r WHERE r.summary_embedding IS NOT NULL
                {document_filter}
                ORDER BY r.summary_embedding <=> %s::vector LIMIT 48
                """,
                [value, *([native_ids] if native_ids is not None else []), value],
            )
            summary_vector = list(cur.fetchall())
            cur.execute(
                f"""
                WITH q AS (SELECT websearch_to_tsquery('english', %s) query)
                SELECT {common}, s.id AS evidence_id, s.seq, s.text AS context,
                       ts_rank_cd(s.tsv, q.query) AS rank
                FROM segments s JOIN recordings r ON r.id = s.recording_id CROSS JOIN q
                WHERE s.tsv @@ q.query {document_filter}
                ORDER BY rank DESC, s.id LIMIT 48
                """,
                [query, *([native_ids] if native_ids is not None else [])],
            )
            fts = list(cur.fetchall())
    arms: list[list[dict[str, Any]]] = []
    for rows in (segment_vector, summary_vector, fts):
        arms.append([{**dict(row), "source": "pocket"} for row in rows])
    return _fuse(arms, limit)


def postgres_search(
    agent_dsn: str,
    pocket_dsn: str,
    vector: list[float],
    query: str,
    limit: int = 12,
    document_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    agent_ids = (
        [document_id for document_id in document_ids if not document_id.startswith("pocket:")]
        if document_ids is not None
        else None
    )
    pocket_ids = (
        [document_id for document_id in document_ids if document_id.startswith("pocket:")]
        if document_ids is not None
        else None
    )
    agent = agent_search(agent_dsn, vector, query, limit, agent_ids)
    pocket = pocket_search(pocket_dsn, vector, query, limit, pocket_ids)
    return _fuse([agent, pocket], limit)


def recall_texts(value: Any) -> list[str]:
    texts: list[str] = []
    if isinstance(value, dict):
        for key in ("content", "text", "fact", "memory"):
            if isinstance(value.get(key), str) and value[key].strip():
                texts.append(value[key].strip())
        for item in value.values():
            texts.extend(recall_texts(item))
    elif isinstance(value, list):
        for item in value:
            texts.extend(recall_texts(item))
    return list(dict.fromkeys(texts))


def recall_document_ids(value: Any) -> list[str]:
    ids: list[str] = []
    if isinstance(value, dict):
        for key in ("document_id", "documentId", "source_document_id"):
            if value.get(key):
                ids.append(_strip_canary_id(str(value[key])))
        for item in value.values():
            ids.extend(recall_document_ids(item))
    elif isinstance(value, list):
        for item in value:
            ids.extend(recall_document_ids(item))
    return list(dict.fromkeys(ids))


def _strip_canary_id(value: str) -> str:
    match = re.match(r"^canary:(?:summary|card):(?:agent_sessions|pocket):(.*)$", value)
    return match.group(1) if match else value


def route_from_postgres(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "contexts": [str(row.get("context") or "") for row in rows],
        "provenance": [
            {
                "source": row["source"],
                "document_id": row["document_id"],
                "source_id": row.get("source_id"),
                "evidence_id": row.get("evidence_id"),
                "seq": row.get("seq"),
                "source_path": row.get("source_path"),
            }
            for row in rows
        ],
    }


def build_postgres_baseline(
    questions_path: str,
    corpus_path: str,
    agent_dsn: str,
    pocket_dsn: str,
    output_path: str,
) -> None:
    questions = read_jsonl(questions_path)
    corpus_document_ids = [
        str(row["document_id"]) for row in read_jsonl(corpus_path)
    ]
    vectors = embed_queries([str(question["question"]) for question in questions])
    output: list[dict[str, Any]] = []
    for question, vector in zip(questions, vectors, strict=True):
        rows = postgres_search(
            agent_dsn,
            pocket_dsn,
            vector,
            str(question["question"]),
            document_ids=corpus_document_ids,
        )
        output.append(
            {"question_id": str(question["id"]), "route": route_from_postgres(rows)}
        )
    write_jsonl(output_path, output)


def collect_recalls(
    api: HindsightAPI,
    questions: list[dict[str, Any]],
    concurrency: int | None = None,
) -> list[dict[str, Any]]:
    workers = concurrency or int(os.getenv("CANARY_RECALL_CONCURRENCY", "8"))
    if not 1 <= workers <= 16:
        raise ValueError("CANARY_RECALL_CONCURRENCY must be between 1 and 16")

    def recall(question: dict[str, Any]) -> dict[str, Any]:
        return api.request(
            "POST",
            "memories/recall",
            json={"query": question["question"], "budget": "mid", "max_tokens": 4096},
        )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        return list(executor.map(recall, questions))


def collect(
    questions_path: str,
    corpus_path: str,
    agent_dsn: str,
    pocket_dsn: str,
    hindsight_url: str,
    bank: str,
    output_path: str,
    baseline_path: str | None = None,
) -> None:
    questions = read_jsonl(questions_path)
    corpus_document_ids = [
        str(row["document_id"]) for row in read_jsonl(corpus_path)
    ]
    corpus_document_id_set = set(corpus_document_ids)
    baseline_by_id: dict[str, dict[str, Any]] = {}
    if baseline_path:
        baseline_by_id = {
            str(row["question_id"]): row["route"]
            for row in read_jsonl(baseline_path)
        }
        expected_ids = {str(question["id"]) for question in questions}
        if set(baseline_by_id) != expected_ids:
            raise ValueError("Postgres baseline cache does not match the question set")
    token = os.getenv("CANARY_HINDSIGHT_TOKEN", "").strip()
    if not token:
        raise SystemExit("CANARY_HINDSIGHT_TOKEN is required")
    api = HindsightAPI(hindsight_url, bank, token)
    recalls = collect_recalls(api, questions)
    expanded_queries: list[str] = []
    for question, raw in zip(questions, recalls, strict=True):
        hints = "\n".join(recall_texts(raw))[:4000]
        expanded_queries.append(question["question"] + ("\nMemory hints:\n" + hints if hints else ""))

    raw_vectors: list[list[float] | None] = (
        [None] * len(questions)
        if baseline_by_id
        else embed_queries([question["question"] for question in questions])
    )
    expanded_vectors = embed_queries(expanded_queries)
    output: list[dict[str, Any]] = []
    for question, raw, raw_vector, expanded_vector in zip(
        questions, recalls, raw_vectors, expanded_vectors, strict=True
    ):
        if baseline_by_id:
            postgres_route = baseline_by_id[str(question["id"])]
        else:
            if raw_vector is None:
                raise AssertionError("raw query vector is missing")
            postgres_rows = postgres_search(
                agent_dsn,
                pocket_dsn,
                raw_vector,
                question["question"],
                document_ids=corpus_document_ids,
            )
            postgres_route = route_from_postgres(postgres_rows)
        candidate_ids = [
            document_id
            for document_id in recall_document_ids(raw)
            if document_id in corpus_document_id_set
        ]
        hybrid_rows = postgres_search(
            agent_dsn,
            pocket_dsn,
            expanded_vector,
            question["question"],
            document_ids=candidate_ids or corpus_document_ids,
        )
        if not hybrid_rows:
            hybrid_route = postgres_route
        else:
            hybrid_route = route_from_postgres(hybrid_rows)
        memory_texts = recall_texts(raw)
        output.append(
            {
                "question": question,
                "routes": {
                    "postgres": postgres_route,
                    "hindsight": {
                        "contexts": memory_texts,
                        "provenance": [
                            {"source": "hindsight", "document_id": document_id}
                            for document_id in recall_document_ids(raw)
                        ],
                        "raw": raw,
                    },
                    "hybrid": {
                        **hybrid_route,
                        "hindsight_candidate_context_chars": sum(len(text) for text in memory_texts),
                    },
                },
            }
        )
    write_jsonl(output_path, output)


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w./:#@-]+", " ", value.lower())).strip()


ANSWER_STOPWORDS = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "to",
    "was",
    "were",
    "with",
}


def answer_supported(answer: str, contexts: list[str], *, exact: bool) -> bool:
    normalized_answer = normalize_text(answer)
    normalized_context = normalize_text("\n".join(contexts))
    if not normalized_answer or not normalized_context:
        return False
    if normalized_answer in normalized_context:
        return True
    if exact:
        return False
    answer_terms = {
        term
        for term in normalized_answer.split()
        if term not in ANSWER_STOPWORDS and len(term) > 1
    }
    if not answer_terms:
        return False
    context_terms = set(normalized_context.split())
    coverage = len(answer_terms & context_terms) / len(answer_terms)
    return coverage >= 0.70 - 1e-9


def score_run(path: str) -> dict[str, Any]:
    rows = read_jsonl(path)
    route_scores: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        question = row["question"]
        expected = {
            str(item["document_id"])
            for item in question.get("evidence") or []
            if isinstance(item, dict) and item.get("document_id")
        }
        category = question["category"]
        for route_name, route in row["routes"].items():
            provenance = route.get("provenance") or []
            retrieved = {str(item.get("document_id")) for item in provenance if item.get("document_id")}
            document_correct = bool(expected) and expected.issubset(retrieved)
            contexts = [str(context) for context in route.get("contexts") or []]
            is_exact = category == "exact_identifiers_errors"
            supported = answer_supported(
                str(question.get("answer") or ""), contexts, exact=is_exact
            )
            exact_correct = supported if is_exact else True
            correct = document_correct and supported
            route_scores[route_name].append(
                {
                    "correct": correct,
                    "exact_correct": exact_correct and document_correct,
                    "category": category,
                    "critical_miss": bool(question.get("critical")) and not correct,
                    "context_chars": sum(len(context) for context in contexts),
                    "provenance_complete": bool(provenance)
                    and all(
                        item.get("document_id")
                        and item.get("source") in {"agent_sessions", "pocket"}
                        and item.get("source_id") is not None
                        for item in provenance
                    ),
                }
            )
    metrics: dict[str, Any] = {}
    for route_name, scores in route_scores.items():
        exact = [score for score in scores if score["category"] == "exact_identifiers_errors"]
        temporal = [score for score in scores if score["category"] in {"temporal_updates", "cross_session_patterns"}]
        metrics[route_name] = {
            "overall_correctness": _mean(score["correct"] for score in scores),
            "exact_correctness": _mean(score["exact_correct"] for score in exact),
            "temporal_cross_correctness": _mean(score["correct"] for score in temporal),
            "mean_context_chars": _mean(score["context_chars"] for score in scores),
            "provenance_completeness": _mean(score["provenance_complete"] for score in scores),
            # Routes return evidence, not an ungrounded synthesized answer.
            "unsupported_answers": 0,
            "critical_misses": sum(score["critical_miss"] for score in scores),
            "fact_coverage": _mean(score["correct"] for score in scores),
            "questions": len(scores),
        }
    return metrics


def _mean(values: Any) -> float:
    materialized = [float(value) for value in values]
    return sum(materialized) / len(materialized) if materialized else 0.0


def utility_pass(routes: dict[str, Any]) -> bool:
    postgres = routes["postgres"]
    hybrid = routes["hybrid"]
    temporal_gain = hybrid["temporal_cross_correctness"] - postgres["temporal_cross_correctness"]
    context_reduction = 1.0 - hybrid["mean_context_chars"] / max(postgres["mean_context_chars"], 1.0)
    return bool(
        hybrid["overall_correctness"] >= postgres["overall_correctness"]
        and hybrid["exact_correctness"] >= postgres["exact_correctness"]
        and hybrid["unsupported_answers"] == 0
        and hybrid["provenance_completeness"] == 1.0
        and (temporal_gain >= 0.10 - 1e-9 or context_reduction >= 0.30 - 1e-9)
    )


def evaluate_spec(spec_path: str) -> dict[str, Any]:
    """Build the exact metrics shape consumed by ``canary.py gate``.

    Spec shape::

      {"runs": {"20b": {"summary": {"path": "...", "schema_retry_failure_rate": 0},
                           "session_card": {...}}, "120b": {...}}}
    """
    spec = json.loads(Path(spec_path).read_text())
    disqualified_models = spec.get("disqualified_models") or {}
    runs: dict[str, dict[str, dict[str, Any]]] = {}
    for model, variants in spec["runs"].items():
        runs[model] = {}
        for variant, config in variants.items():
            if "schema_retry_failure_rate" not in config:
                raise ValueError(
                    f"{model}/{variant} must report schema_retry_failure_rate"
                )
            failure_rate = float(config["schema_retry_failure_rate"])
            if not 0.0 <= failure_rate <= 1.0:
                raise ValueError(
                    f"{model}/{variant} schema_retry_failure_rate is outside [0, 1]"
                )
            runs[model][variant] = {
                "routes": score_run(config["path"]),
                "schema_retry_failure_rate": failure_rate,
            }

    model_metrics: dict[str, Any] = {}
    for model in ("20b", "120b"):
        if not runs.get(model):
            disqualification = disqualified_models.get(model)
            if not disqualification:
                raise ValueError(f"spec has no usable run for {model}")
            model_metrics[model] = {
                "fact_coverage": 0.0,
                "schema_retry_failure_rate": float(
                    disqualification.get("schema_retry_failure_rate", 1.0)
                ),
                "critical_misses": int(disqualification.get("critical_misses", 1)),
                "qualified": False,
                "reason": str(disqualification.get("reason") or "candidate did not complete"),
            }
            continue
        hindsight_routes = [run["routes"]["hindsight"] for run in runs[model].values()]
        model_metrics[model] = {
            "fact_coverage": _mean(route["fact_coverage"] for route in hindsight_routes),
            "schema_retry_failure_rate": max(
                run["schema_retry_failure_rate"] for run in runs[model].values()
            ),
            "critical_misses": max(route["critical_misses"] for route in hindsight_routes),
            "qualified": True,
        }

    selected_model = "20b" if (
        model_metrics["20b"].get("qualified", True)
        and model_metrics["20b"]["fact_coverage"] >= model_metrics["120b"]["fact_coverage"] - 0.02
        and model_metrics["20b"]["schema_retry_failure_rate"] <= 0.01
        and model_metrics["20b"]["critical_misses"] == 0
    ) else "120b"
    variant_metrics: dict[str, Any] = {}
    for external, internal in (("summary", "summary"), ("session_card", "session_card")):
        if internal not in runs[selected_model]:
            variant_metrics[external] = {
                "utility_pass": False,
                "score": 0.0,
                "available": False,
                "reason": "variant did not pass artifact/schema gate",
            }
            continue
        routes = runs[selected_model][internal]["routes"]
        variant_metrics[external] = {
            "utility_pass": utility_pass(routes),
            "score": routes["hybrid"]["overall_correctness"],
            "available": True,
        }
    if variant_metrics["summary"]["utility_pass"] and (
        not variant_metrics["session_card"]["available"]
        or variant_metrics["summary"]["score"]
        >= variant_metrics["session_card"]["score"] - 0.02
    ):
        selected_variant = "summary"
    elif variant_metrics["session_card"]["utility_pass"]:
        selected_variant = "session_card"
    else:
        selected_variant = "summary"  # gate will reject Hindsight via utility_pass=false

    return {
        "models": model_metrics,
        "variants": variant_metrics,
        "routes": runs[selected_model][selected_variant]["routes"],
        "selection_basis": {"model": selected_model, "variant": selected_variant},
        "disqualified_models": disqualified_models,
        "unavailable_variants": spec.get("unavailable_variants") or {},
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    collect_parser = sub.add_parser("collect")
    collect_parser.add_argument("--questions", required=True)
    collect_parser.add_argument("--corpus", required=True)
    collect_parser.add_argument("--agent-dsn")
    collect_parser.add_argument("--pocket-dsn")
    collect_parser.add_argument("--hindsight-url", required=True)
    collect_parser.add_argument("--bank", required=True)
    collect_parser.add_argument("--output", required=True)
    collect_parser.add_argument("--baseline")
    baseline_parser = sub.add_parser("baseline")
    baseline_parser.add_argument("--questions", required=True)
    baseline_parser.add_argument("--corpus", required=True)
    baseline_parser.add_argument("--agent-dsn")
    baseline_parser.add_argument("--pocket-dsn")
    baseline_parser.add_argument("--output", required=True)
    score_parser = sub.add_parser("score")
    score_parser.add_argument("--run", required=True)
    score_parser.add_argument("--output")
    evaluate_parser = sub.add_parser("evaluate")
    evaluate_parser.add_argument("--spec", required=True)
    evaluate_parser.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in {"collect", "baseline"}:
        agent_dsn = args.agent_dsn or os.getenv("CANARY_AGENT_DSN", "")
        pocket_dsn = args.pocket_dsn or os.getenv("CANARY_POCKET_DSN", "")
        if not agent_dsn or not pocket_dsn:
            raise SystemExit(
                "--agent-dsn/--pocket-dsn or CANARY_AGENT_DSN/CANARY_POCKET_DSN are required"
            )
    if args.command == "collect":
        collect(
            args.questions,
            args.corpus,
            agent_dsn,
            pocket_dsn,
            args.hindsight_url,
            args.bank,
            args.output,
            args.baseline,
        )
    elif args.command == "baseline":
        build_postgres_baseline(
            args.questions,
            args.corpus,
            agent_dsn,
            pocket_dsn,
            args.output,
        )
    elif args.command == "score":
        result = score_run(args.run)
        if args.output:
            atomic_json(args.output, result)
        print(json.dumps(result, sort_keys=True))
    elif args.command == "evaluate":
        result = evaluate_spec(args.spec)
        atomic_json(args.output, result)
        print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
