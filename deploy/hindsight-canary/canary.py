#!/usr/bin/env python3
"""Cost-gated corpus selection, card generation, canary retain, and decisions.

The script is deliberately resumable and conservative.  It records state before
every external write, never blindly retries an ambiguous retain, and refuses to
use the normal Redpill key or a configured budget above two dollars.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import statistics
import time
from collections import Counter, defaultdict, deque
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable


CATEGORIES = (
    "exact_identifiers_errors",
    "semantic_troubleshooting",
    "decisions_outcomes",
    "temporal_updates",
    "cross_session_patterns",
)
QWEN_QUERY_INSTRUCTION = (
    "Instruct: Retrieve authoritative session evidence that answers the query. Preserve exact "
    "identifiers, errors, decisions, outcomes, artifacts, and temporal context.\nQuery: {query}"
)
MAX_CONTENT_CHARS = 3000
CARD_CHUNK_MAX_CHARS = 80_000
MAX_IN_FLIGHT = 8


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with Path(path).open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{number}: expected a JSON object")
            rows.append(value)
    return rows


def write_jsonl(path: str | Path, rows: Iterable[dict[str, Any]]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(_jsonable(row), ensure_ascii=False, sort_keys=True) + "\n")
    os.chmod(target, 0o600)


def append_jsonl(path: str | Path, row: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(_jsonable(row), ensure_ascii=False, sort_keys=True) + "\n")
    os.chmod(target, 0o600)


def atomic_json(path: str | Path, value: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(_jsonable(value), handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
    os.replace(temporary, target)
    os.chmod(target, 0o600)


def stable_key(value: Any) -> str:
    encoded = json.dumps(_jsonable(value), sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _one_line(text: str, limit: int) -> str:
    compact = " ".join((text or "").split())
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


def fallback_summary(row: dict[str, Any]) -> str:
    title = row.get("title") or f"{row.get('tool')} session {row.get('session_id')}"
    parts = [title]
    if row.get("first_user"):
        parts.append(f"First user turn: {_one_line(row['first_user'], 600)}")
    if row.get("last_assistant"):
        parts.append(f"Last assistant turn: {_one_line(row['last_assistant'], 900)}")
    return "\n".join(parts)


def quantile_bucket(value: int, cuts: list[float]) -> str:
    for index, cut in enumerate(cuts):
        if value <= cut:
            return str(index)
    return str(len(cuts))


def annotate_summary(row: dict[str, Any]) -> str:
    fallback = fallback_summary(row).strip()
    summary = str(row.get("summary") or "").strip()
    if not summary:
        row["summary"] = fallback
        return "fallback"
    return "fallback" if summary == fallback else "supplied"


def stratified_pick(rows: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    """Deterministic round-robin over tool/project/recency/length/summary strata."""
    if count >= len(rows):
        for row in rows:
            supplied = annotate_summary(row)
            row["summary_source"] = supplied
            row["stratum"] = [
                str(row.get("tool") or "unknown"),
                str(row.get("project_path") or "(none)"),
                "all",
                "all",
                supplied,
            ]
        return sorted(rows, key=lambda row: stable_key(row.get("content_hash")))

    lengths = sorted(int(row.get("transcript_chars") or 0) for row in rows)
    timestamps = sorted(_epoch(row.get("started_at")) for row in rows)
    length_cuts = [statistics.quantiles(lengths, n=4, method="inclusive")[i] for i in range(3)]
    time_cuts = [statistics.quantiles(timestamps, n=4, method="inclusive")[i] for i in range(3)]
    projects = Counter(row.get("project_path") or "(none)" for row in rows)
    top_projects = {project for project, _ in projects.most_common(20)}

    groups: dict[tuple[str, ...], deque[dict[str, Any]]] = defaultdict(deque)
    for row in rows:
        project = row.get("project_path") or "(none)"
        if project not in top_projects:
            project = f"other-{int(stable_key(project)[:2], 16) % 8}"
        supplied = annotate_summary(row)
        stratum = (
            str(row.get("tool") or "unknown"),
            project,
            quantile_bucket(_epoch(row.get("started_at")), time_cuts),
            quantile_bucket(int(row.get("transcript_chars") or 0), length_cuts),
            supplied,
        )
        row["summary_source"] = supplied
        row["stratum"] = list(stratum)
        groups[stratum].append(row)

    for key, queue in groups.items():
        groups[key] = deque(sorted(queue, key=lambda row: stable_key(row.get("content_hash"))))

    chosen: list[dict[str, Any]] = []
    keys = sorted(groups, key=stable_key)
    while len(chosen) < count:
        progressed = False
        for key in keys:
            if groups[key]:
                chosen.append(groups[key].popleft())
                progressed = True
                if len(chosen) == count:
                    break
        if not progressed:
            break
    if len(chosen) != count:
        raise RuntimeError(f"selected {len(chosen)} rows, expected {count}")
    return chosen


def _epoch(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, datetime):
        return int(value.timestamp())
    text = str(value).replace("Z", "+00:00")
    try:
        return int(datetime.fromisoformat(text).timestamp())
    except ValueError:
        return 0


def select_corpus(agent_dsn: str, pocket_dsn: str, agent_count: int) -> list[dict[str, Any]]:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - runtime dependency message
        raise SystemExit("select requires psycopg[binary]") from exc

    with psycopg.connect(agent_dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.id, s.content_hash, s.tool, s.project_path, s.host_id,
                       s.started_at, s.ended_at, s.title, s.summary, s.source_path,
                       s.meta,
                       coalesce((SELECT sum(length(m.text)) FROM messages m
                                 WHERE m.session_id = s.id), 0) AS transcript_chars,
                       (SELECT m.text FROM messages m WHERE m.session_id = s.id
                          AND m.role = 'user' ORDER BY m.seq LIMIT 1) AS first_user,
                       (SELECT m.text FROM messages m WHERE m.session_id = s.id
                          AND m.role = 'assistant' AND m.text <> ''
                          ORDER BY m.seq DESC LIMIT 1) AS last_assistant
                FROM sessions s
                ORDER BY s.id
                """
            )
            features = list(cur.fetchall())
        chosen = stratified_pick(features, min(agent_count, len(features)))
        ids = [row["id"] for row in chosen]
        messages: dict[int, list[dict[str, Any]]] = defaultdict(list)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT session_id, seq, role, ts, text, tool_name
                FROM messages WHERE session_id = ANY(%s)
                ORDER BY session_id, seq
                """,
                (ids,),
            )
            for message in cur.fetchall():
                messages[message["session_id"]].append(_jsonable(dict(message)))

    corpus: list[dict[str, Any]] = []
    for row in chosen:
        corpus.append(
            {
                "source": "agent_sessions",
                "source_id": row["id"],
                "document_id": row["content_hash"],
                "tool": row["tool"],
                "project_path": row["project_path"],
                "host_id": row["host_id"],
                "started_at": _jsonable(row["started_at"]),
                "ended_at": _jsonable(row["ended_at"]),
                "title": row["title"],
                "summary": (row["summary"] or "")[:MAX_CONTENT_CHARS],
                "summary_source": row["summary_source"],
                "source_path": row["source_path"],
                "meta": row["meta"] or {},
                "transcript_chars": row["transcript_chars"],
                "stratum": row["stratum"],
                "messages": messages[row["id"]],
            }
        )

    with psycopg.connect(pocket_dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, pocket_id, title, created_at, updated_at, summary,
                       tags, speakers, action_items, mind_map
                FROM recordings ORDER BY created_at, id
                """
            )
            recordings = list(cur.fetchall())
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT recording_id, seq, speaker, start_ms, end_ms, text
                FROM segments ORDER BY recording_id, seq
                """
            )
            segments: dict[int, list[dict[str, Any]]] = defaultdict(list)
            for segment in cur.fetchall():
                segments[segment["recording_id"]].append(_jsonable(dict(segment)))
    for row in recordings:
        corpus.append(
            {
                "source": "pocket",
                "source_id": row["id"],
                "document_id": f"pocket:{row['pocket_id']}",
                "pocket_id": row["pocket_id"],
                "title": row["title"],
                "started_at": _jsonable(row["created_at"]),
                "ended_at": _jsonable(row["updated_at"]),
                "summary": (row["summary"] or "")[:MAX_CONTENT_CHARS],
                "summary_source": "supplied",
                "meta": {
                    "tags": row["tags"],
                    "speakers": row["speakers"],
                    "action_items": row["action_items"],
                    "mind_map": row["mind_map"],
                },
                "transcript_chars": sum(len(segment["text"] or "") for segment in segments[row["id"]]),
                "stratum": ["pocket", "all", "all", "all", "supplied"],
                "messages": [
                    {
                        "seq": segment["seq"],
                        "role": segment.get("speaker") or "speaker",
                        "ts": None,
                        "text": segment["text"],
                        "tool_name": None,
                    }
                    for segment in segments[row["id"]]
                ],
            }
        )
    return corpus


def key_preflight(*, require_stack: bool = True) -> dict[str, Any]:
    key = os.getenv("CANARY_REDPILL_API_KEY", "").strip()
    if not key:
        raise SystemExit("CANARY_REDPILL_API_KEY is required")
    normal_key_path = Path(os.getenv("NORMAL_REDPILL_KEY_FILE", "~/.attestmesh/redpill-key")).expanduser()
    if normal_key_path.exists() and key == normal_key_path.read_text().strip():
        raise SystemExit("refusing the normal Redpill key; create a dedicated canary virtual key")
    hard_budget = float(os.getenv("CANARY_HARD_BUDGET_USD", "0"))
    if hard_budget <= 0 or hard_budget > 2:
        raise SystemExit("CANARY_HARD_BUDGET_USD must be >0 and <=2")
    safety_margin = float(os.getenv("CANARY_SAFETY_MARGIN_USD", "0.05"))
    if safety_margin < 0 or safety_margin >= hard_budget:
        raise SystemExit(
            "CANARY_SAFETY_MARGIN_USD must be >=0 and below the hard budget"
        )
    rpm = int(os.getenv("CANARY_RPM_LIMIT", "0"))
    tpm = int(os.getenv("CANARY_TPM_LIMIT", "0"))
    if rpm <= 0 or tpm <= 0:
        raise SystemExit("CANARY_RPM_LIMIT and CANARY_TPM_LIMIT must be positive")
    if require_stack:
        for name in (
            "CANARY_QWEN_API_KEY",
            "CANARY_POSTGRES_PASSWORD",
            "CANARY_HINDSIGHT_TOKEN",
        ):
            if not os.getenv(name, "").strip():
                raise SystemExit(f"{name} is required")
    prices: dict[str, float] = {}
    for family in ("20B", "120B"):
        family_total = 0.0
        for direction in ("INPUT", "OUTPUT"):
            name = f"CANARY_{family}_{direction}_USD_PER_M"
            raw = os.getenv(name, "").strip()
            try:
                price = float(raw)
            except ValueError as exc:
                raise SystemExit(f"{name} must be an explicit numeric price") from exc
            if price < 0:
                raise SystemExit(f"{name} must not be negative")
            prices[name] = price
            family_total += price
        if family_total <= 0:
            raise SystemExit(f"CANARY_{family} prices cannot both be zero")
    return {
        "hard_budget_usd": hard_budget,
        "safety_margin_usd": safety_margin,
        "effective_local_budget_usd": hard_budget - safety_margin,
        "rpm_limit": rpm,
        "tpm_limit": tpm,
        "prices": prices,
    }


def provider_preflight() -> dict[str, Any]:
    """Validate the dedicated key and live catalog without running inference."""
    import httpx

    guard = key_preflight()
    key = os.environ["CANARY_REDPILL_API_KEY"].strip()
    base = os.getenv(
        "CANARY_REDPILL_BASE_URL", "https://api.redpill.ai/v1"
    ).rstrip("/")
    headers = {"Authorization": f"Bearer {key}"}
    with httpx.Client(timeout=30) as client:
        response = client.get(f"{base}/models", headers=headers)
        response.raise_for_status()
        body = response.json()
        rows = body.get("data") if isinstance(body, dict) else body
        if not isinstance(rows, list):
            raise SystemExit("Redpill model catalog returned an unexpected shape")
        by_id = {
            str(row.get("id")): row
            for row in rows
            if isinstance(row, dict) and row.get("id")
        }
        verified_prices: dict[str, dict[str, float]] = {}
        for family, model in (
            ("20B", "openai/gpt-oss-20b"),
            ("120B", "openai/gpt-oss-120b"),
        ):
            row = by_id.get(model)
            if not row:
                raise SystemExit(f"Redpill catalog omitted {model}")
            pricing = row.get("pricing") or {}
            live_input = float(pricing.get("input") or pricing.get("prompt")) * 1_000_000
            live_output = float(
                pricing.get("output") or pricing.get("completion")
            ) * 1_000_000
            configured_input = float(
                guard["prices"][f"CANARY_{family}_INPUT_USD_PER_M"]
            )
            configured_output = float(
                guard["prices"][f"CANARY_{family}_OUTPUT_USD_PER_M"]
            )
            if abs(live_input - configured_input) > 1e-9 or abs(
                live_output - configured_output
            ) > 1e-9:
                raise SystemExit(
                    f"live pricing changed for {model}; update the explicit ledger prices"
                )
            verified_prices[model] = {
                "input_usd_per_m": live_input,
                "output_usd_per_m": live_output,
            }

        attestation = client.get(
            f"{base}/attestation/report",
            headers=headers,
            params={
                "model": "openai/gpt-oss-20b",
                "nonce": secrets.token_hex(32),
            },
        )
        if attestation.status_code in {401, 402}:
            raise SystemExit(
                f"dedicated Redpill key preflight returned {attestation.status_code}"
            )
        attestation.raise_for_status()
    return {
        **guard,
        "provider_key_active": True,
        "live_prices": verified_prices,
    }


def retain_preflight() -> dict[str, Any]:
    mode = os.getenv("CANARY_RETAIN_GUARD", "redpill").strip().lower()
    if mode == "redpill":
        return key_preflight()
    if mode != "frontier":
        raise SystemExit("CANARY_RETAIN_GUARD must be redpill or frontier")

    import httpx

    url = os.getenv("CANARY_FRONTIER_PROXY_HEALTH_URL", "").strip()
    if not url:
        raise SystemExit(
            "CANARY_FRONTIER_PROXY_HEALTH_URL is required for the frontier guard"
        )
    response = httpx.get(url, timeout=10)
    response.raise_for_status()
    health = response.json()
    if health.get("status") != "healthy":
        raise SystemExit(f"frontier proxy is not healthy: {health.get('status')}")
    if health.get("circuit_open") or not health.get("provider_egress_enabled"):
        raise SystemExit("frontier proxy circuit/egress guard is closed")
    return health


class BudgetLedger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.guard = key_preflight(require_stack=False)
        if self.path.exists():
            self.data = json.loads(self.path.read_text())
        else:
            self.data = {"spent_usd": 0.0, "calls": [], "hard_budget_usd": self.guard["hard_budget_usd"]}
        if float(self.data.get("hard_budget_usd", 0)) != float(self.guard["hard_budget_usd"]):
            raise RuntimeError("ledger hard budget does not match the current canary guard")
        if float(self.data.get("spent_usd", 0)) > float(self.guard["hard_budget_usd"]):
            raise RuntimeError("existing ledger already exceeds the canary guard")

    def prices(self, model: str) -> tuple[float, float]:
        normalized = model.lower()
        if "20b" in normalized:
            family = "20B"
        elif "120b" in normalized:
            family = "120B"
        else:
            raise SystemExit(f"unsupported canary model for explicit pricing: {model}")
        try:
            input_price = float(os.environ[f"CANARY_{family}_INPUT_USD_PER_M"])
            output_price = float(os.environ[f"CANARY_{family}_OUTPUT_USD_PER_M"])
        except KeyError as exc:
            raise SystemExit(f"missing explicit canary price: {exc.args[0]}") from exc
        return input_price, output_price

    def effective_budget(self) -> float:
        return float(
            self.guard.get(
                "effective_local_budget_usd", self.guard["hard_budget_usd"]
            )
        )

    def reserve(
        self, model: str, prompt_bytes: int, max_completion_tokens: int
    ) -> dict[str, Any]:
        input_price, output_price = self.prices(model)
        # A byte cannot encode more than one token, so using UTF-8 bytes is a
        # deliberately conservative reservation for arbitrary code/transcript
        # content. Actual provider usage replaces it after a successful call.
        input_tokens = max(1, prompt_bytes)
        estimate = (
            input_tokens * input_price + max_completion_tokens * output_price
        ) / 1_000_000
        if float(self.data["spent_usd"]) + estimate > self.effective_budget():
            raise RuntimeError(
                f"local worst-case reservation ${estimate:.4f} would exceed the ${self.effective_budget():.2f} effective guard"
            )
        return {
            "input_tokens": input_tokens,
            "output_tokens": max_completion_tokens,
            "cost_usd": estimate,
        }

    def record(
        self,
        model: str,
        usage: dict[str, Any],
        purpose: str,
        reservation: dict[str, Any],
        started_at: str,
    ) -> None:
        reported_input = usage.get("prompt_tokens") or usage.get("input_tokens")
        reported_output = usage.get("completion_tokens") or usage.get("output_tokens")
        estimated = reported_input is None or reported_output is None
        input_tokens = int(
            reservation["input_tokens"] if reported_input is None else reported_input
        )
        output_tokens = int(
            reservation["output_tokens"] if reported_output is None else reported_output
        )
        input_price, output_price = self.prices(model)
        cost = (input_tokens * input_price + output_tokens * output_price) / 1_000_000
        self.data["spent_usd"] = float(self.data["spent_usd"]) + cost
        self.data["calls"].append(
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "started_at": started_at,
                "model": model,
                "purpose": purpose,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": cost,
                "estimated_from_reservation": estimated,
            }
        )
        if float(self.data["spent_usd"]) > self.effective_budget():
            raise RuntimeError("local token ledger crossed the hard canary budget")
        atomic_json(self.path, self.data)

    def wait_for_rate_capacity(self, reservation: dict[str, Any]) -> None:
        """Enforce the configured rolling RPM and conservative TPM locally."""
        while True:
            delay = rate_capacity_delay(
                self.data.get("calls") or [],
                reservation,
                int(self.guard["rpm_limit"]),
                int(self.guard["tpm_limit"]),
                datetime.now(timezone.utc).timestamp(),
            )
            if delay <= 0:
                return
            time.sleep(min(delay + 0.05, 1.0))


def rate_capacity_delay(
    calls: list[dict[str, Any]],
    reservation: dict[str, Any],
    rpm_limit: int,
    tpm_limit: int,
    now: float,
) -> float:
    recent: list[tuple[float, int]] = []
    for call in calls:
        raw_timestamp = call.get("started_at") or call.get("at")
        try:
            timestamp = datetime.fromisoformat(str(raw_timestamp)).timestamp()
        except (TypeError, ValueError):
            continue
        age = now - timestamp
        if 0 <= age < 60:
            tokens = int(call.get("input_tokens") or 0) + int(
                call.get("output_tokens") or 0
            )
            recent.append((timestamp, tokens))
    reserved_tokens = int(reservation["input_tokens"]) + int(
        reservation["output_tokens"]
    )
    if len(recent) < rpm_limit and (
        sum(tokens for _timestamp, tokens in recent) + reserved_tokens
        <= tpm_limit
    ):
        return 0.0
    if not recent:
        raise RuntimeError(
            "one request reservation exceeds CANARY_TPM_LIMIT; reduce the request"
        )
    return max(0.05, min(timestamp + 60 - now for timestamp, _tokens in recent))


class LLMClient:
    def __init__(self, ledger: BudgetLedger, model: str = "openai/gpt-oss-20b") -> None:
        self.ledger = ledger
        self.model = model
        self.key = os.environ["CANARY_REDPILL_API_KEY"].strip()
        self.url = os.getenv("CANARY_REDPILL_BASE_URL", "https://api.redpill.ai/v1").rstrip("/")

    def json_completion(
        self,
        system: str,
        user: str,
        purpose: str,
        *,
        schema_name: str | None = None,
        schema: dict[str, Any] | None = None,
        max_completion_tokens: int = 8192,
    ) -> dict[str, Any]:
        import httpx

        prompt_bytes = len(system.encode("utf-8")) + len(user.encode("utf-8"))
        reservation = self.ledger.reserve(
            self.model, prompt_bytes, max_completion_tokens
        )
        self.ledger.wait_for_rate_capacity(reservation)
        response_format: dict[str, Any] = {"type": "json_object"}
        if schema is not None:
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name or "canary_output",
                    "strict": False,
                    "schema": schema,
                },
            }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "reasoning_effort": "low",
            "include_reasoning": False,
            "max_completion_tokens": max_completion_tokens,
            "response_format": response_format,
        }
        started_at = datetime.now(timezone.utc).isoformat()
        with httpx.Client(timeout=180) as client:
            response = client.post(
                f"{self.url}/chat/completions",
                headers={"Authorization": f"Bearer {self.key}"},
                json=payload,
            )
        if response.status_code in {401, 402}:
            raise RuntimeError(f"circuit open: Redpill returned {response.status_code}")
        response.raise_for_status()
        body = response.json()
        self.ledger.record(
            self.model,
            body.get("usage") or {},
            purpose,
            reservation,
            started_at,
        )
        content = body["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
        return parse_json_object(str(content))


def parse_json_object(content: str) -> dict[str, Any]:
    """Parse one JSON object from a non-strict structured-output response.

    GPT-OSS can append a second fenced block or a short explanation even when
    ``response_format`` requests JSON.  The canary deliberately runs with
    strict schema disabled, so accept the first complete JSON object while
    still rejecting prose-only, truncated, array, or scalar responses.
    """
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, count=1, flags=re.IGNORECASE)
    decoder = json.JSONDecoder()
    errors: list[json.JSONDecodeError] = []
    for offset, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, _end = decoder.raw_decode(text, offset)
        except json.JSONDecodeError as exc:
            errors.append(exc)
            continue
        if isinstance(value, dict):
            return value
    if errors:
        raise errors[-1]
    raise ValueError("LLM response does not contain a JSON object")


def message_chunks(messages: list[dict[str, Any]], max_chars: int = 100_000) -> list[list[dict[str, Any]]]:
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    size = 0
    for message in messages:
        message_size = len(str(message.get("text") or "")) + 80
        if current and size + message_size > max_chars:
            chunks.append(current)
            current = []
            size = 0
        # Preserve message boundaries. A pathological single message is capped
        # with an explicit marker rather than silently split into fake turns.
        item = dict(message)
        if message_size > max_chars:
            item["text"] = str(item.get("text") or "")[: max_chars - 200] + "\n[TRUNCATED SINGLE MESSAGE]"
            message_size = max_chars
        current.append(item)
        size += message_size
    if current:
        chunks.append(current)
    return chunks or [[]]


def format_messages(messages: list[dict[str, Any]]) -> str:
    return "\n\n".join(
        f"[seq={message.get('seq')} role={message.get('role')} tool={message.get('tool_name') or ''}]\n{message.get('text') or ''}"
        for message in messages
    )


def card_text(value: dict[str, Any]) -> str:
    fields = (
        ("Objective", value.get("objective")),
        ("Outcome/status", value.get("outcome_status") or value.get("outcome")),
        ("Decisions", value.get("decisions")),
        ("Errors/blockers", value.get("errors_blockers")),
        ("Artifacts", value.get("artifacts")),
        ("Follow-ups", value.get("follow_ups")),
        ("Source sequences", value.get("source_sequences")),
    )
    lines: list[str] = []
    for label, raw in fields:
        if isinstance(raw, list):
            rendered = "; ".join(str(item) for item in raw)
        elif isinstance(raw, dict):
            rendered = json.dumps(raw, ensure_ascii=False, sort_keys=True)
        else:
            rendered = str(raw or "")
        lines.append(f"{label}: {rendered}")
    text = "\n".join(lines)
    if len(text) <= MAX_CONTENT_CHARS:
        return text
    # Preserve the source-reference line even when descriptive fields are long.
    source = lines[-1][:500]
    return ("\n".join(lines[:-1])[: MAX_CONTENT_CHARS - len(source) - 2].rstrip() + "\n" + source)[:MAX_CONTENT_CHARS]


CARD_SYSTEM = """You create concise, evidence-grounded archival session cards. Return one JSON object only.
Required keys: objective, outcome_status, decisions, errors_blockers, artifacts, follow_ups, source_sequences.
Every factual item must be supported by the supplied messages. source_sequences must list the supporting message seq values. Do not invent completion, success, files, identifiers, or decisions.
Keep the entire JSON response under 2,000 characters. Keep objective and outcome_status under 300 characters. Use at most 8 short strings in each array and no more than 160 characters per string. Summarize; never copy logs or long source passages. Do not include analysis or markdown."""

CARD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "objective": {"type": "string"},
        "outcome_status": {"type": "string"},
        "decisions": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        "errors_blockers": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        "artifacts": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        "follow_ups": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        "source_sequences": {"type": "array", "items": {"type": "integer"}},
    },
    "required": [
        "objective",
        "outcome_status",
        "decisions",
        "errors_blockers",
        "artifacts",
        "follow_ups",
        "source_sequences",
    ],
    "additionalProperties": False,
}


def validate_card(record: dict[str, Any], value: dict[str, Any]) -> None:
    required = set(CARD_SCHEMA["required"])
    missing = required - set(value)
    if missing:
        raise ValueError(
            "session card is missing required fields: " + ", ".join(sorted(missing))
        )
    for field in (
        "decisions",
        "errors_blockers",
        "artifacts",
        "follow_ups",
        "source_sequences",
    ):
        if not isinstance(value.get(field), list):
            raise ValueError(f"session card field {field} must be a list")
    sequences = value["source_sequences"]
    known = {message.get("seq") for message in record.get("messages") or []}
    if known and not sequences:
        raise ValueError("session card must cite at least one source sequence")
    if any(sequence not in known for sequence in sequences):
        raise ValueError("session card cited an unknown source sequence")


def generate_card(
    record: dict[str, Any],
    client: LLMClient,
    partial_cache: dict[str, dict[str, Any]] | None = None,
    partial_path: str | Path | None = None,
) -> tuple[str, dict[str, Any]]:
    chunks = message_chunks(
        record.get("messages") or [], max_chars=CARD_CHUNK_MAX_CHARS
    )
    partials: list[dict[str, Any]] = []
    cache = partial_cache if partial_cache is not None else {}
    for index, chunk in enumerate(chunks):
        prompt = (
            f"Source={record['source']} document_id={record['document_id']} title={record.get('title') or ''}\n"
            f"Summarize chunk {index + 1}/{len(chunks)} without losing exact errors, identifiers, artifacts, or sequence references.\n\n"
            + format_messages(chunk)
        )
        cache_key = f"{record['document_id']}:{index}"
        prompt_hash = stable_key([CARD_SYSTEM, CARD_SCHEMA, prompt])
        cached = cache.get(cache_key)
        if cached:
            if cached.get("prompt_hash") != prompt_hash:
                raise SafetyStop(f"partial-card prompt changed for {cache_key}")
            partial = cached.get("structured")
            if not isinstance(partial, dict):
                raise SafetyStop(f"partial-card cache is malformed for {cache_key}")
        else:
            partial = client.json_completion(
                CARD_SYSTEM,
                prompt,
                f"card-chunk:{record['document_id']}:{index}",
                schema_name="session_card_chunk",
                schema=CARD_SCHEMA,
                max_completion_tokens=8192,
            )
            cached = {
                "cache_key": cache_key,
                "document_id": record["document_id"],
                "chunk_index": index,
                "chunk_count": len(chunks),
                "prompt_hash": prompt_hash,
                "structured": partial,
                "status": "ok",
            }
            if partial_path is not None:
                append_jsonl(partial_path, cached)
            cache[cache_key] = cached
        partials.append(partial)
    if len(partials) == 1:
        final = partials[0]
    else:
        final = client.json_completion(
            CARD_SYSTEM,
            "Merge these bounded chunk cards into one session card. Resolve later updates by sequence order and retain all source sequence references:\n"
            + json.dumps(partials, ensure_ascii=False),
            f"card-merge:{record['document_id']}",
            schema_name="session_card",
            schema=CARD_SCHEMA,
            max_completion_tokens=8192,
        )
    validate_card(record, final)
    return card_text(final), final


def generate_cards(corpus_path: str, output_path: str, ledger_path: str, model: str) -> None:
    existing = (
        {
            row["document_id"]
            for row in read_jsonl(output_path)
            if row.get("status") == "ok"
        }
        if Path(output_path).exists()
        else set()
    )
    partial_path = Path(str(output_path) + ".partials.jsonl")
    partial_cache = (
        {
            row["cache_key"]: row
            for row in read_jsonl(partial_path)
            if row.get("status") == "ok" and row.get("cache_key")
        }
        if partial_path.exists()
        else {}
    )
    client = LLMClient(BudgetLedger(ledger_path), model=model)
    for record in read_jsonl(corpus_path):
        if record["document_id"] in existing:
            continue
        try:
            text, structured = generate_card(
                record, client, partial_cache, partial_path
            )
            row = {
                "document_id": record["document_id"],
                "source": record["source"],
                "card": text,
                "structured": structured,
                "status": "ok",
            }
        except Exception as exc:
            row = {
                "document_id": record["document_id"],
                "source": record["source"],
                "status": "failed",
                "error": str(exc),
            }
            append_jsonl(output_path, row)
            raise
        append_jsonl(output_path, row)


def question_source(
    corpus: list[dict[str, Any]],
    cards: dict[str, str],
    category: str,
    aliases: dict[str, str] | None = None,
) -> str:
    pieces: list[str] = []
    for record in sorted(corpus, key=lambda row: stable_key([category, row["document_id"]])):
        messages = record.get("messages") or []
        if category == "exact_identifiers_errors":
            selected = [
                message for message in messages
                if re.search(r"(?i)\b(error|failed|exception|0x[0-9a-f]+|[A-Z_]{3,}|/[\w./-]+)\b", str(message.get("text") or ""))
            ][:4]
        else:
            selected = messages[:2] + messages[-2:]
        document_id = record["document_id"]
        rendered_id = document_id
        if aliases is not None:
            rendered_id = f"D{len(aliases) + 1:03d}"
            aliases[rendered_id] = document_id
        piece = {
            "document_id": rendered_id,
            "source": record["source"],
            "started_at": record.get("started_at"),
            "summary": record.get("summary"),
            "card": cards.get(record["document_id"]),
            "allowed_source_sequences": [
                message.get("seq") for message in selected
            ],
            "messages": selected,
        }
        rendered = json.dumps(piece, ensure_ascii=False)
        if sum(len(value) for value in pieces) + len(rendered) > 90_000:
            break
        pieces.append(rendered)
    return "\n".join(pieces)


QUESTION_SYSTEM = """Create evidence-backed retrieval evaluation questions. Return JSON only as {"questions": [...]}.
Every question object must have: question, answer, evidence (a nonempty list of {document_id, source_sequences}), critical (boolean).
Use only supplied evidence; every cited sequence must appear in that document's allowed_source_sequences list. Answers must be concise and independently checkable. Do not refer to 'the transcript' in the question."""

QUESTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "minItems": 25,
            "maxItems": 25,
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "answer": {"type": "string"},
                    "evidence": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "document_id": {"type": "string"},
                                "source_sequences": {
                                    "type": "array",
                                    "minItems": 1,
                                    "items": {"type": "integer"},
                                },
                            },
                            "required": ["document_id", "source_sequences"],
                            "additionalProperties": False,
                        },
                    },
                    "critical": {"type": "boolean"},
                },
                "required": ["question", "answer", "evidence", "critical"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["questions"],
    "additionalProperties": False,
}


def validate_questions(
    category: str,
    questions: Any,
    known: set[str],
    known_sequences: dict[str, set[Any]],
) -> list[dict[str, Any]]:
    if not isinstance(questions, list) or len(questions) not in {20, 25}:
        raise ValueError(
            f"{category}: expected 25 candidates or 20 cached validated questions"
        )
    validated: list[dict[str, Any]] = []
    seen_questions: set[str] = set()
    for question in questions:
        evidence = question.get("evidence") if isinstance(question, dict) else None
        if not isinstance(evidence, list) or not evidence:
            continue
        document_ids: set[str] = set()
        clean_evidence: list[dict[str, Any]] = []
        valid = True
        for item in evidence:
            if not isinstance(item, dict) or not item.get("document_id"):
                valid = False
                break
            document_id = str(item["document_id"])
            sequences = item.get("source_sequences")
            if not isinstance(sequences, list) or not sequences:
                valid = False
                break
            if document_id not in known or any(
                sequence not in known_sequences.get(document_id, set())
                for sequence in sequences
            ):
                valid = False
                break
            document_ids.add(document_id)
            clean_evidence.append(
                {"document_id": document_id, "source_sequences": sequences}
            )
        if not valid or not document_ids:
            continue
        if category == "cross_session_patterns" and len(document_ids) < 2:
            continue
        normalized_question = _one_line(str(question.get("question") or ""), 2000).lower()
        if not normalized_question or normalized_question in seen_questions:
            continue
        seen_questions.add(normalized_question)
        validated.append({**dict(question), "evidence": clean_evidence})
    if len(validated) < 20:
        raise ValueError(
            f"{category}: only {len(validated)} of 25 candidates had valid evidence"
        )
    return validated[:20]


def build_cross_session_questions(
    questions: list[dict[str, Any]], corpus: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    metadata = {row["document_id"]: row for row in corpus}

    def document_ids(question: dict[str, Any]) -> set[str]:
        return {
            str(item.get("document_id"))
            for item in question.get("evidence") or []
            if isinstance(item, dict) and item.get("document_id")
        }

    def terms(question: dict[str, Any]) -> set[str]:
        text = _one_line(
            f"{question.get('question') or ''} {question.get('answer') or ''}",
            4000,
        ).lower()
        return {
            token
            for token in re.findall(r"[a-z0-9_./:#@-]+", text)
            if len(token) > 2
        }

    candidates: list[tuple[float, str, int, int]] = []
    for left_index, left in enumerate(questions):
        left_ids = document_ids(left)
        if not left_ids:
            continue
        for right_index in range(left_index + 1, len(questions)):
            right = questions[right_index]
            right_ids = document_ids(right)
            if len(left_ids | right_ids) < 2:
                continue
            left_terms = terms(left)
            right_terms = terms(right)
            overlap = len(left_terms & right_terms) / max(
                len(left_terms | right_terms), 1
            )
            related = 0.0
            for left_id in left_ids:
                for right_id in right_ids:
                    left_meta = metadata.get(left_id) or {}
                    right_meta = metadata.get(right_id) or {}
                    if left_meta.get("project_path") and left_meta.get(
                        "project_path"
                    ) == right_meta.get("project_path"):
                        related = max(related, 0.5)
                    elif left_meta.get("tool") == right_meta.get("tool"):
                        related = max(related, 0.2)
            tie = stable_key(
                [left.get("question"), right.get("question"), sorted(left_ids), sorted(right_ids)]
            )
            candidates.append((overlap + related, tie, left_index, right_index))

    selected: list[dict[str, Any]] = []
    used: set[int] = set()
    for _score, _tie, left_index, right_index in sorted(
        candidates, key=lambda item: (-item[0], item[1])
    ):
        if left_index in used or right_index in used:
            continue
        left = questions[left_index]
        right = questions[right_index]
        evidence_by_document: dict[str, set[Any]] = defaultdict(set)
        for item in (left.get("evidence") or []) + (right.get("evidence") or []):
            if not isinstance(item, dict) or not item.get("document_id"):
                continue
            evidence_by_document[str(item["document_id"])].update(
                item.get("source_sequences") or []
            )
        if len(evidence_by_document) < 2:
            continue
        selected.append(
            {
                "question": (
                    "Across two related sessions, what were both findings: "
                    f"(1) {_one_line(str(left.get('question') or ''), 350)} "
                    f"(2) {_one_line(str(right.get('question') or ''), 350)}"
                ),
                "answer": (
                    f"(1) {_one_line(str(left.get('answer') or ''), 500)}; "
                    f"(2) {_one_line(str(right.get('answer') or ''), 500)}"
                ),
                "evidence": [
                    {
                        "document_id": document_id,
                        "source_sequences": sorted(sequences),
                    }
                    for document_id, sequences in sorted(evidence_by_document.items())
                ],
                "critical": bool(left.get("critical") or right.get("critical")),
            }
        )
        used.update({left_index, right_index})
        if len(selected) == 20:
            return selected
    raise ValueError(
        f"could build only {len(selected)} grounded cross-session questions"
    )


def generate_questions(
    corpus_path: str,
    cards_path: str | None,
    output_path: str,
    ledger_path: str,
    model: str,
) -> None:
    corpus = read_jsonl(corpus_path)
    cards = (
        {
            row["document_id"]: row.get("card", "")
            for row in read_jsonl(cards_path)
            if row.get("status") == "ok"
        }
        if cards_path
        else {}
    )
    known = {row["document_id"] for row in corpus}
    known_sequences = {
        row["document_id"]: {
            message.get("seq") for message in row.get("messages") or []
        }
        for row in corpus
    }
    client = LLMClient(BudgetLedger(ledger_path), model=model)
    partial_path = Path(str(output_path) + ".categories.jsonl")
    cached_categories = (
        {
            row["category"]: row
            for row in read_jsonl(partial_path)
            if row.get("status") == "ok" and row.get("category")
        }
        if partial_path.exists()
        else {}
    )
    output: list[dict[str, Any]] = []
    for category in CATEGORIES:
        if category == "cross_session_patterns":
            base_questions = [
                question
                for cached_category, cached in cached_categories.items()
                if cached_category != category
                for question in cached.get("questions") or []
            ]
            prompt_hash = stable_key(
                ["deterministic-cross-session-v1", base_questions]
            )
            cached = cached_categories.get(category)
            if cached:
                if cached.get("prompt_hash") != prompt_hash:
                    raise SafetyStop("cross-session source questions changed")
                validated = validate_questions(
                    category,
                    cached.get("questions"),
                    known,
                    known_sequences,
                )
            else:
                validated = build_cross_session_questions(
                    base_questions, corpus
                )
                validated = validate_questions(
                    category, validated, known, known_sequences
                )
                cached = {
                    "category": category,
                    "prompt_hash": prompt_hash,
                    "questions": validated,
                    "status": "ok",
                }
                append_jsonl(partial_path, cached)
                cached_categories[category] = cached
            for question in validated:
                output.append(
                    {
                        **question,
                        "category": category,
                        "id": f"{category}-{len(output) % 20 + 1:02d}",
                    }
                )
            continue
        aliases: dict[str, str] | None = (
            {} if category == "cross_session_patterns" else None
        )
        source = question_source(corpus, cards, category, aliases)
        prompt = (
            f"Create exactly 25 candidate questions in category {category}; only 20 will be retained after evidence validation. "
            "For temporal questions, ground both old and new states. For cross-session questions, cite at least two document IDs.\n\n"
            + (
                "For this cross-session category, copy the short D### document aliases exactly into evidence.document_id; they will be mapped back to source IDs after validation.\n\n"
                if aliases is not None
                else ""
            )
            + source
        )
        prompt_hash = stable_key([QUESTION_SYSTEM, QUESTION_SCHEMA, prompt])
        cached = cached_categories.get(category)
        if cached:
            if cached.get("prompt_hash") != prompt_hash:
                raise SafetyStop(f"question prompt changed for {category}")
            questions = cached.get("questions")
        else:
            value = client.json_completion(
                QUESTION_SYSTEM,
                prompt,
                f"questions:{category}",
                schema_name=f"{category}_questions",
                schema=QUESTION_SCHEMA,
                max_completion_tokens=8192,
            )
            questions = value.get("questions")
        if aliases is not None and isinstance(questions, list):
            translated_questions: list[Any] = []
            for question in questions:
                translated = dict(question) if isinstance(question, dict) else question
                if isinstance(translated, dict) and isinstance(
                    translated.get("evidence"), list
                ):
                    translated["evidence"] = [
                        {
                            **item,
                            "document_id": aliases.get(
                                str(item.get("document_id")),
                                str(item.get("document_id")),
                            ),
                        }
                        if isinstance(item, dict)
                        else item
                        for item in translated["evidence"]
                    ]
                translated_questions.append(translated)
            questions = translated_questions
        validated = validate_questions(
            category, questions, known, known_sequences
        )
        if not cached:
            cached = {
                "category": category,
                "prompt_hash": prompt_hash,
                "questions": validated,
                "status": "ok",
            }
            append_jsonl(partial_path, cached)
            cached_categories[category] = cached
        for question in validated:
            output.append({**question, "category": category, "id": f"{category}-{len(output) % 20 + 1:02d}"})
    if len(output) != 100:
        raise AssertionError("question set must contain exactly 100 items")
    write_jsonl(output_path, output)


class HindsightAPI:
    def __init__(self, url: str, bank: str, token: str) -> None:
        import httpx

        self.httpx = httpx
        self.url = f"{url.rstrip('/')}/v1/default/banks/{bank}"
        self.headers = {"Authorization": f"Bearer {token}"}

    def request(self, method: str, endpoint: str, **kwargs: Any) -> dict[str, Any]:
        with self.httpx.Client(timeout=120) as client:
            response = client.request(method, f"{self.url}/{endpoint.lstrip('/')}", headers=self.headers, **kwargs)
        if response.status_code in {401, 402}:
            raise SafetyStop(f"Hindsight returned {response.status_code}; circuit opened")
        response.raise_for_status()
        return response.json() if response.content else {}

    def ensure_bank(self) -> dict[str, Any]:
        with self.httpx.Client(timeout=120) as client:
            response = client.put(self.url, headers=self.headers, json={})
        if response.status_code in {401, 402}:
            raise SafetyStop(f"Hindsight returned {response.status_code}; circuit opened")
        response.raise_for_status()
        return response.json() if response.content else {}

    def active(self) -> list[dict[str, Any]]:
        operations: list[dict[str, Any]] = []
        for status in ("pending", "processing"):
            page = self.request(
                "GET",
                "operations",
                params={"status": status, "type": "retain", "limit": 100, "offset": 0, "exclude_parents": "true"},
            )
            total = int(page.get("total") or 0)
            if total > MAX_IN_FLIGHT:
                raise SafetyStop(f"remote queue has {total} {status} retains; cap is {MAX_IN_FLIGHT}")
            operations.extend(page.get("operations") or [])
        if len(operations) > MAX_IN_FLIGHT:
            raise SafetyStop(f"remote queue has {len(operations)} active retains; cap is {MAX_IN_FLIGHT}")
        return operations

    def status(self, operation_id: str, include_payload: bool = False) -> dict[str, Any]:
        return self.request(
            "GET",
            f"operations/{operation_id}",
            params={"include_payload": "true"} if include_payload else None,
        )

    def retain(self, item: dict[str, Any]) -> str:
        body = self.request("POST", "memories", json={"async": True, "items": [item]})
        operation_id = str(body.get("operation_id") or body.get("id") or "")
        if not operation_id:
            raise SafetyStop("retain response omitted operation_id")
        return operation_id


class SafetyStop(RuntimeError):
    pass


def operation_document_id(value: Any) -> str | None:
    if isinstance(value, dict):
        direct = value.get("document_id") or value.get("documentId")
        if direct:
            return str(direct)
        ids = value.get("document_ids")
        if isinstance(ids, list) and len(ids) == 1:
            return str(ids[0])
        for key in ("result_metadata", "task_payload", "payload", "items", "contents"):
            found = operation_document_id(value.get(key))
            if found:
                return found
    elif isinstance(value, list):
        found = {operation_document_id(item) for item in value}
        found.discard(None)
        if len(found) == 1:
            return found.pop()
    return None


def child_operation_ids(value: Any) -> set[str]:
    if not isinstance(value, dict):
        return set()
    children = value.get("child_operations")
    if not isinstance(children, list):
        return set()
    return {
        str(child.get("operation_id") or child.get("id"))
        for child in children
        if isinstance(child, dict)
        and (child.get("operation_id") or child.get("id"))
    }


def canary_items(corpus: list[dict[str, Any]], cards: list[dict[str, Any]], variant: str) -> list[dict[str, Any]]:
    cards_by_id = {row["document_id"]: row for row in cards if row.get("status") == "ok"}
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in corpus:
        content = record.get("summary") if variant == "summary" else (cards_by_id.get(record["document_id"]) or {}).get("card")
        if not content:
            raise SafetyStop(f"{variant} content missing for {record['document_id']}")
        document_id = f"canary:{variant}:{record['source']}:{record['document_id']}"
        if document_id in seen:
            raise SafetyStop(f"duplicate document_id: {document_id}")
        seen.add(document_id)
        items.append(
            {
                "content": str(content)[:MAX_CONTENT_CHARS],
                "context": f"source:{record['source']} | source_document:{record['document_id']}",
                "document_id": document_id,
                "timestamp": record.get("started_at"),
                "tags": ["hindsight-canary", f"variant:{variant}", f"source:{record['source']}"],
            }
        )
    return items


def stage_explicit_retry(value: dict[str, Any]) -> None:
    """Move one terminal failure back to a visible, single-use retry state."""
    retries = int(value.get("explicit_retries") or 0)
    if retries >= 1:
        return
    history = list(value.get("retry_history") or [])
    history.append(
        {
            "operation_id": value.get("operation_id"),
            "error": value.get("error"),
            "failed_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    value.update(
        state="retry_pending",
        explicit_retries=retries + 1,
        retry_history=history,
    )
    for key in ("operation_id", "worker_operation_ids", "error", "completed_at"):
        value.pop(key, None)


def retain_canary(
    corpus_path: str,
    cards_path: str | None,
    variant: str,
    url: str,
    bank: str,
    state_path: str,
    limit: int | None = None,
    retry_failed: bool = False,
) -> dict[str, Any]:
    # The local Hindsight instances receive this same key through compose.  Do
    # not permit a retain run unless the dedicated-key/budget guard is present.
    retain_preflight()
    token = os.getenv("CANARY_HINDSIGHT_TOKEN", "").strip()
    if not token:
        raise SystemExit("CANARY_HINDSIGHT_TOKEN is required")
    cards = read_jsonl(cards_path) if cards_path else []
    if variant == "card" and not cards:
        raise SystemExit("--cards is required for the card variant")
    items = canary_items(read_jsonl(corpus_path), cards, variant)
    if limit is not None:
        if limit <= 0:
            raise SystemExit("--limit must be positive")
        items = items[:limit]
    items_by_id = {item["document_id"]: item for item in items}
    api = HindsightAPI(url, bank, token)
    api.ensure_bank()
    state_file = Path(state_path)
    state = json.loads(state_file.read_text()) if state_file.exists() else {"documents": {}}
    documents: dict[str, dict[str, Any]] = state.setdefault("documents", {})

    unknown_state = set(documents) - set(items_by_id)
    if unknown_state:
        raise SafetyStop(f"state contains documents outside this corpus: {len(unknown_state)}")
    for document_id, value in documents.items():
        expected_hash = stable_key(items_by_id[document_id])
        prior_hash = value.get("payload_hash")
        if prior_hash and prior_hash != expected_hash:
            raise SafetyStop(f"payload changed for stateful document {document_id}")
        value["payload_hash"] = expected_hash
        if retry_failed and value.get("state") == "failed":
            stage_explicit_retry(value)
    atomic_json(state_file, state)

    while True:
        # Re-read the remote queue every interval. Unknown work still consumes
        # capacity, and duplicate operations for one document stop the run.
        active_operations = api.active()
        active_ids: set[str] = set()
        active_by_document: dict[str, str] = {}
        for operation in active_operations:
            operation_id = str(operation.get("id") or operation.get("operation_id") or "")
            if not operation_id:
                raise SafetyStop("active Hindsight operation omitted its ID")
            active_ids.add(operation_id)
            document_id = operation_document_id(operation)
            if not document_id:
                document_id = operation_document_id(
                    api.status(operation_id, include_payload=True)
                )
            if document_id not in items_by_id:
                continue
            prior_operation = active_by_document.get(document_id)
            if prior_operation and prior_operation != operation_id:
                raise SafetyStop(f"duplicate active operations for {document_id}")
            active_by_document[document_id] = operation_id
            existing = documents.get(document_id) or {}
            if existing.get("state") in {"succeeded", "failed"}:
                raise SafetyStop(
                    f"active operation reappeared for terminal document {document_id}"
                )
            existing_operation = str(existing.get("operation_id") or "")
            if existing_operation and existing_operation != operation_id:
                parent = api.status(existing_operation)
                if operation_id not in child_operation_ids(parent):
                    raise SafetyStop(f"operation identity changed for {document_id}")
                worker_ids = set(existing.get("worker_operation_ids") or [])
                worker_ids.add(operation_id)
                documents[document_id] = {
                    **existing,
                    "state": "submitted",
                    "payload_hash": stable_key(items_by_id[document_id]),
                    "worker_operation_ids": sorted(worker_ids),
                    "adopted": True,
                }
            else:
                documents[document_id] = {
                    **existing,
                    "state": "submitted",
                    "operation_id": operation_id,
                    "payload_hash": stable_key(items_by_id[document_id]),
                    "adopted": True,
                }
        atomic_json(state_file, state)

        ambiguous = [
            document_id
            for document_id, value in documents.items()
            if value.get("state") == "submitting"
        ]
        if ambiguous:
            raise SafetyStop(
                f"{len(ambiguous)} ambiguous submission(s) require explicit review"
            )

        submitted = [
            (document_id, value) for document_id, value in documents.items()
            if value.get("state") == "submitted"
        ]
        for document_id, value in submitted:
            status = api.status(value["operation_id"])
            remote = str(status.get("status") or "").lower()
            if remote == "completed":
                value.update(state="succeeded", completed_at=datetime.now(timezone.utc).isoformat())
            elif remote in {"failed", "cancelled", "not_found"}:
                value.update(state="failed", error=status.get("error_message") or remote)
                if retry_failed and int(value.get("explicit_retries") or 0) < 1:
                    stage_explicit_retry(value)
                elif re.search(r"(?:^|\D)(401|402)(?:\D|$)", str(value["error"])):
                    atomic_json(state_file, state)
                    raise SafetyStop(str(value["error"]))
        atomic_json(state_file, state)

        terminal = [value for value in documents.values() if value.get("state") in {"succeeded", "failed"}]
        failures = [value for value in terminal if value.get("state") == "failed"]
        failure_rate = len(failures) / len(items)
        if failure_rate > 0.01:
            raise SafetyStop(
                f"terminal failure rate {failure_rate:.2%} exceeds 1%"
            )

        succeeded = sum(value.get("state") == "succeeded" for value in documents.values())
        if succeeded == len(items):
            return {
                "variant": variant,
                "documents": len(items),
                "succeeded": succeeded,
                "failed": len(failures),
                "schema_retry_failure_rate": failure_rate,
            }
        if len(terminal) == len(items):
            return {
                "variant": variant,
                "documents": len(items),
                "succeeded": succeeded,
                "failed": len(failures),
                "schema_retry_failure_rate": failure_rate,
            }

        represented_documents = set(active_by_document)
        local_unrepresented = sum(
            value.get("state") == "submitted"
            and document_id not in represented_documents
            for document_id, value in documents.items()
        )
        in_flight = len(active_ids) + local_unrepresented
        available = [
            item
            for item in items
            if item["document_id"] not in documents
            or documents[item["document_id"]].get("state") == "retry_pending"
        ]
        capacity = max(0, MAX_IN_FLIGHT - in_flight)
        for item in available[:capacity]:
            document_id = item["document_id"]
            prior = documents.get(document_id) or {}
            documents[document_id] = {
                "state": "submitting",
                "payload_hash": stable_key(item),
                "started_at": datetime.now(timezone.utc).isoformat(),
                "explicit_retries": int(prior.get("explicit_retries") or 0),
                "retry_history": list(prior.get("retry_history") or []),
            }
            atomic_json(state_file, state)
            try:
                operation_id = api.retain(item)
            except Exception as exc:
                documents[document_id].update(state="failed", error=f"ambiguous submission: {exc}")
                atomic_json(state_file, state)
                raise SafetyStop(str(exc)) from exc
            documents[document_id].update(state="submitted", operation_id=operation_id)
            atomic_json(state_file, state)
        time.sleep(5)


def gate_decision(metrics: dict[str, Any]) -> dict[str, Any]:
    models = metrics["models"]
    twenty = models["20b"]
    control = models["120b"]
    use_20b = (
        bool(twenty.get("qualified", True))
        and float(twenty["fact_coverage"]) >= float(control["fact_coverage"]) - 0.02
        and float(twenty["schema_retry_failure_rate"]) <= 0.01
        and int(twenty["critical_misses"]) == 0
    )
    selected_model = "20b" if use_20b else "120b"

    variants = metrics["variants"]
    summary = variants["summary"]
    card = variants["session_card"]
    if bool(summary["utility_pass"]) and float(summary["score"]) >= float(card["score"]) - 0.02:
        selected_variant: str | None = "summary"
    elif bool(card["utility_pass"]):
        selected_variant = "session_card"
    else:
        selected_variant = None

    routes = metrics["routes"]
    postgres = routes["postgres"]
    hybrid = routes["hybrid"]
    correctness_ok = (
        float(hybrid["overall_correctness"]) >= float(postgres["overall_correctness"])
        and float(hybrid["exact_correctness"]) >= float(postgres["exact_correctness"])
        and int(hybrid["unsupported_answers"]) == 0
        and float(hybrid["provenance_completeness"]) == 1.0
    )
    temporal_gain = float(hybrid["temporal_cross_correctness"]) - float(postgres["temporal_cross_correctness"])
    context_reduction = 1.0 - (
        float(hybrid["mean_context_chars"]) / max(float(postgres["mean_context_chars"]), 1.0)
    )
    keep_hindsight = bool(
        selected_variant
        and correctness_ok
        and (temporal_gain >= 0.10 - 1e-9 or context_reduction >= 0.30 - 1e-9)
    )
    return {
        "selected_model": selected_model,
        "selected_variant": selected_variant,
        "keep_hindsight": keep_hindsight,
        "production_backend": "hybrid" if keep_hindsight else "postgres",
        "checks": {
            "20b_model_gate": use_20b,
            "correctness_and_provenance": correctness_ok,
            "temporal_gain": temporal_gain,
            "context_reduction": context_reduction,
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight")

    select = sub.add_parser("select")
    select.add_argument("--agent-dsn", required=True)
    select.add_argument("--pocket-dsn", required=True)
    select.add_argument("--output", required=True)
    select.add_argument("--agent-count", type=int, default=250)

    cards = sub.add_parser("cards")
    cards.add_argument("--corpus", required=True)
    cards.add_argument("--output", required=True)
    cards.add_argument("--ledger", required=True)
    cards.add_argument("--model", default="openai/gpt-oss-20b")

    questions = sub.add_parser("questions")
    questions.add_argument("--corpus", required=True)
    questions.add_argument("--cards")
    questions.add_argument("--output", required=True)
    questions.add_argument("--ledger", required=True)
    questions.add_argument("--model", default="openai/gpt-oss-20b")

    retain = sub.add_parser("retain")
    retain.add_argument("--corpus", required=True)
    retain.add_argument("--cards")
    retain.add_argument("--variant", choices=("summary", "card"), required=True)
    retain.add_argument("--url", required=True)
    retain.add_argument("--bank", required=True)
    retain.add_argument("--state", required=True)
    retain.add_argument("--limit", type=int)
    retain.add_argument("--retry-failed", action="store_true")

    gate = sub.add_parser("gate")
    gate.add_argument("--metrics", required=True)
    gate.add_argument("--output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "preflight":
        print(json.dumps(provider_preflight(), sort_keys=True))
    elif args.command == "select":
        corpus = select_corpus(args.agent_dsn, args.pocket_dsn, args.agent_count)
        write_jsonl(args.output, corpus)
        print(json.dumps({"agent_sessions": sum(row["source"] == "agent_sessions" for row in corpus), "pocket": sum(row["source"] == "pocket" for row in corpus)}))
    elif args.command == "cards":
        generate_cards(args.corpus, args.output, args.ledger, args.model)
    elif args.command == "questions":
        generate_questions(args.corpus, args.cards, args.output, args.ledger, args.model)
    elif args.command == "retain":
        print(
            json.dumps(
                retain_canary(
                    args.corpus,
                    args.cards,
                    args.variant,
                    args.url,
                    args.bank,
                    args.state,
                    args.limit,
                    args.retry_failed,
                ),
                sort_keys=True,
            )
        )
    elif args.command == "gate":
        decision = gate_decision(json.loads(Path(args.metrics).read_text()))
        if args.output:
            atomic_json(args.output, decision)
        print(json.dumps(decision, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
