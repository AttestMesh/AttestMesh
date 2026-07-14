"""OpenAI-compatible front door for fugu-router.

Public callers use model=fugu or model=fugu-ultra. This process makes a sticky
subscription choice per session, forwards to the account-specific LiteLLM model,
and retries another subscription on quota/rate-limit-shaped failures. It also
serves a small mesh-only dashboard backed by the fugu credit ledger.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import re
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable

import httpx
import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from psycopg.rows import dict_row

from fugu_credit import MODEL_PUBLIC_NAMES, ensure_db, upstream_model, write_ledger


DATABASE_URL = os.environ["DATABASE_URL"]
UPSTREAM = os.environ.get("FUGU_LITELLM_UPSTREAM", "http://127.0.0.1:4000").rstrip("/")
MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY", "")
COOLDOWN_SECONDS = int(os.environ.get("FUGU_ACCOUNT_COOLDOWN_SECONDS", "300"))
TIE_EPSILON = Decimal(os.environ.get("FUGU_ROUTING_TIE_EPSILON", "0.000001"))
DB_CONNECT_ATTEMPTS = max(1, int(os.environ.get("FUGU_DB_CONNECT_ATTEMPTS", "4")))
DB_CONNECT_BACKOFF_SECONDS = max(
    0.0, float(os.environ.get("FUGU_DB_CONNECT_BACKOFF_SECONDS", "0.1"))
)
DB_READ_ATTEMPTS = max(1, int(os.environ.get("FUGU_DB_READ_ATTEMPTS", "2")))
CONTENT_POLICY_FALLBACK_MODEL = os.environ.get("FUGU_CONTENT_POLICY_FALLBACK_MODEL", "glm-5.2").strip()
CONTENT_POLICY_FALLBACK_ENABLED = os.environ.get("FUGU_CONTENT_POLICY_FALLBACK_ENABLED", "true").lower() not in {
    "0",
    "false",
    "no",
}
ACCOUNT_COUNT = int(os.environ.get("FUGU_ACCOUNT_COUNT", "3"))
PUBLIC_MODELS = (
    {"id": "fugu-ultra", "label": "Fugu Ultra", "owned_by": "fugu-router", "kind": "fugu"},
    {"id": "fugu", "label": "Fugu", "owned_by": "fugu-router", "kind": "fugu"},
    {"id": "glm-5.2", "label": "GLM 5.2", "owned_by": "redpill", "kind": "chat"},
    {"id": "qwen/qwen3-embedding-8b", "label": "Qwen3 Embedding 8B", "owned_by": "redpill", "kind": "embedding"},
    {"id": "openai/gpt-oss-120b", "label": "GPT-OSS 120B", "owned_by": "redpill", "kind": "chat"},
    {"id": "grok-4.5", "label": "Grok 4.5", "owned_by": "xai", "kind": "chat"},
    {"id": "grok-4.3", "label": "Grok 4.3", "owned_by": "xai", "kind": "chat"},
    {"id": "grok-imagine-image-quality", "label": "Grok Imagine Image Quality", "owned_by": "xai", "kind": "image_generation"},
    {"id": "grok-imagine-image", "label": "Grok Imagine Image", "owned_by": "xai", "kind": "image_generation"},
)
DIRECT_PUBLIC_NAMES = tuple(model["id"] for model in PUBLIC_MODELS if model["kind"] != "fugu")

app = FastAPI(title="fugu-session-router", docs_url=None, redoc_url=None)


def _conn():
    for attempt in range(DB_CONNECT_ATTEMPTS):
        try:
            return psycopg.connect(
                DATABASE_URL,
                autocommit=True,
                row_factory=dict_row,
                connect_timeout=1,
                options="-c statement_timeout=5000 -c lock_timeout=3000",
            )
        except psycopg.OperationalError:
            if attempt + 1 == DB_CONNECT_ATTEMPTS:
                raise
            time.sleep(DB_CONNECT_BACKOFF_SECONDS * (2**attempt))
    raise RuntimeError("unreachable")


def _retry_read_operation(func: Callable[..., Any]) -> Callable[..., Any]:
    """Retry a read-only handler when a mesh handoff drops its DB socket."""

    @functools.wraps(func)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        for attempt in range(DB_READ_ATTEMPTS):
            try:
                return func(*args, **kwargs)
            except psycopg.OperationalError:
                if attempt + 1 == DB_READ_ATTEMPTS:
                    raise
                time.sleep(DB_CONNECT_BACKOFF_SECONDS * (2**attempt))
        raise RuntimeError("unreachable")

    return wrapped


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    return ""


def _require_admin(request: Request) -> JSONResponse | None:
    if MASTER_KEY and _bearer(request) != MASTER_KEY:
        return JSONResponse({"error": {"message": "unauthorized"}}, status_code=401)
    return None


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return str(value)


def _parse_interval(value: Any) -> timedelta:
    if isinstance(value, timedelta):
        return value
    text = str(value)
    if "month" in text:
        return timedelta(days=31)
    if "day" in text:
        return timedelta(days=int(float(text.split()[0])))
    if ":" in text:
        hours, minutes, seconds = text.split(":")
        return timedelta(hours=int(hours), minutes=int(minutes), seconds=float(seconds))
    if "hour" in text:
        return timedelta(hours=int(float(text.split()[0])))
    return timedelta(seconds=float(text))


def _window_start(anchor: datetime, interval: Any, now: datetime | None = None) -> datetime:
    now = now or _utcnow()
    interval_delta = _parse_interval(interval)
    if interval_delta.total_seconds() <= 0:
        return anchor
    elapsed = max((now - anchor).total_seconds(), 0)
    periods = int(elapsed // interval_delta.total_seconds())
    return anchor + periods * interval_delta


def _session_key(request: Request, body: dict[str, Any]) -> str:
    meta = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
    for value in (
        request.headers.get("x-session-id"),
        request.headers.get("x-conversation-id"),
        meta.get("session_id"),
        meta.get("fugu_session_id"),
        meta.get("conversation_id"),
        body.get("user"),
    ):
        if value:
            return str(value)
    return f"request:{datetime.now(UTC).timestamp()}"


def _limit_error(status_code: int, text: str) -> bool:
    lowered = text.lower()
    if status_code in (402, 408, 409, 425, 429, 529):
        return True
    if status_code in (400, 403, 500, 502, 503, 504):
        return any(word in lowered for word in ("quota", "rate", "limit", "credit", "billing", "exhaust", "capacity"))
    return False


def _content_policy_error(status_code: int, text: str) -> bool:
    lowered = text.lower()
    return status_code == 451 or ("content policy" in lowered and "blocked" in lowered)


def _chat_completion_path(path: str) -> bool:
    return path.strip("/") in {"v1/chat/completions", "chat/completions"}


def _apply_glm_plain_text_defaults(body: dict[str, Any], path: str) -> None:
    if not _chat_completion_path(path) or _normalize_public_model(body.get("model")) != "glm-5.2":
        return
    extra_body = body.get("extra_body") if isinstance(body.get("extra_body"), dict) else {}
    if any(key in body for key in ("thinking", "reasoning_effort")):
        return
    if any(key in extra_body for key in ("thinking", "reasoning_effort")):
        return
    updated = dict(extra_body)
    updated["thinking"] = {"type": "disabled"}
    updated["reasoning_effort"] = "none"
    body["extra_body"] = updated


def _headers_for_upstream(request: Request) -> dict[str, str]:
    skip = {"host", "content-length", "accept-encoding"}
    return {k: v for k, v in request.headers.items() if k.lower() not in skip}


def _decimal_header(headers: dict[str, str], name: str) -> str | None:
    value = headers.get(name)
    if value is None:
        return None
    try:
        Decimal(str(value))
    except Exception:
        return None
    return str(value)


def _stream_event_metadata(obj: Any) -> dict[str, Any]:
    if not isinstance(obj, dict):
        return {}
    out: dict[str, Any] = {}
    for key in ("id", "object", "created", "model"):
        if obj.get(key) is not None:
            out[key] = obj.get(key)
    usage = obj.get("usage")
    if isinstance(usage, dict) and usage:
        out["usage"] = usage
    choices = obj.get("choices")
    if isinstance(choices, list):
        out["choice_count"] = len(choices)
        finish_reasons = [
            choice.get("finish_reason")
            for choice in choices
            if isinstance(choice, dict) and choice.get("finish_reason") is not None
        ]
        if finish_reasons:
            out["finish_reasons"] = finish_reasons
    return out


def _stream_response_obj(events: list[dict[str, Any]]) -> dict[str, Any]:
    response: dict[str, Any] = {"object": "chat.completion.stream"}
    usage_events = []
    for event in events:
        for key in ("id", "created", "model"):
            if event.get(key) is not None:
                response.setdefault(key, event[key])
        if isinstance(event.get("usage"), dict) and event["usage"]:
            response["usage"] = event["usage"]
            usage_events.append({"id": event.get("id"), "model": event.get("model"), "usage": event["usage"]})
    response["metadata"] = {
        "stream": True,
        "stream_chunk_count": len(events),
        "stream_usage_seen": bool(usage_events),
        "stream_usage_events": usage_events,
    }
    return response


def _response_text(response: Response) -> str:
    body = getattr(response, "body", b"") or b""
    if isinstance(body, bytes):
        return body.decode("utf-8", errors="replace")
    return str(body)


def _load_json(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return None


def _error_summary(status_code: int, text: str) -> dict[str, Any]:
    obj = _load_json(text)
    summary: dict[str, Any] = {"status_code": status_code}
    if isinstance(obj, dict):
        error = obj.get("error")
        if isinstance(error, dict):
            for key in ("message", "type", "code", "param"):
                if error.get(key) is not None:
                    summary[key] = error.get(key)
        elif error is not None:
            summary["message"] = str(error)
        elif obj.get("message") is not None:
            summary["message"] = str(obj.get("message"))
    if "message" not in summary:
        summary["message"] = text.strip()[:1000] if text.strip() else f"HTTP {status_code}"
    if text and text.strip():
        summary["raw_body"] = text.strip()[:2000]
    return summary


def _content_policy_detail(
    requested_model: str,
    candidate: dict[str, Any],
    response: Response,
) -> dict[str, Any]:
    text = _response_text(response)
    detail = _error_summary(response.status_code, text)
    detail.update(
        {
            "requested_model": requested_model,
            "upstream_model": candidate.get("upstream_model"),
            "account_id": candidate.get("account_id"),
        }
    )
    return detail


def _fallback_notice(detail: dict[str, Any], fallback_model: str) -> str:
    status = detail.get("status_code", "unknown")
    requested = detail.get("requested_model") or "fugu"
    upstream = detail.get("upstream_model") or requested
    account = detail.get("account_id")
    account_part = f" on {account}" if account else ""
    message = str(detail.get("message") or "content policy block").strip().rstrip(".")
    return (
        f"Notice: Fugu returned HTTP {status} for `{requested}` via `{upstream}`{account_part}: "
        f"{message}. Retrying with fallback model `{fallback_model}`; the answer below is from `{fallback_model}`."
    )


def _visible_notice_allowed(body: dict[str, Any]) -> bool:
    response_format = body.get("response_format")
    if isinstance(response_format, dict) and response_format.get("type") in {"json_object", "json_schema"}:
        return False
    return not any(body.get(key) for key in ("tools", "tool_choice", "functions", "function_call"))


def _notice_chunk(notice: str, fallback_model: str) -> dict[str, Any]:
    return {
        "id": f"fugu-fallback-notice-{int(datetime.now(UTC).timestamp())}",
        "object": "chat.completion.chunk",
        "created": int(datetime.now(UTC).timestamp()),
        "model": fallback_model,
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": f"{notice}\n\n"},
                "finish_reason": None,
            }
        ],
    }


def _fallback_headers(detail: dict[str, Any], fallback_model: str) -> dict[str, str]:
    return {
        "x-fugu-fallback": "content-policy",
        "x-fugu-fallback-model": fallback_model,
        "x-fugu-original-status": str(detail.get("status_code", "")),
        "x-fugu-original-model": str(detail.get("upstream_model") or ""),
        "x-fugu-original-account": str(detail.get("account_id") or ""),
    }


def _with_headers(response: Response, headers: dict[str, str]) -> Response:
    for key, value in headers.items():
        if value:
            response.headers[key] = value
    return response


def _annotate_fallback_response(
    response: Response,
    body: dict[str, Any],
    detail: dict[str, Any],
    fallback_model: str,
    notice: str,
) -> Response:
    headers = dict(response.headers)
    headers.update(_fallback_headers(detail, fallback_model))
    text = _response_text(response)
    obj = _load_json(text)
    if not isinstance(obj, dict):
        return Response(text, status_code=response.status_code, headers=headers, media_type=response.media_type)

    metadata = obj.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    metadata["fugu_content_policy_fallback"] = {
        "reason": "content-policy",
        "fallback_model": fallback_model,
        "original": _jsonable(detail),
        "visible_notice_injected": False,
    }
    obj["metadata"] = metadata

    if _visible_notice_allowed(body):
        choices = obj.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") if isinstance(choices[0], dict) else None
            if isinstance(message, dict) and not message.get("tool_calls"):
                content = message.get("content")
                if isinstance(content, str):
                    message["content"] = f"{notice}\n\n{content}" if content else notice
                    metadata["fugu_content_policy_fallback"]["visible_notice_injected"] = True
                elif content is None:
                    message["content"] = notice
                    metadata["fugu_content_policy_fallback"]["visible_notice_injected"] = True
                elif isinstance(content, list):
                    message["content"] = [{"type": "text", "text": f"{notice}\n\n"}] + content
                    metadata["fugu_content_policy_fallback"]["visible_notice_injected"] = True

    return JSONResponse(obj, status_code=response.status_code, headers=headers)


def _content_policy_failure_response(
    requested_model: str,
    original_detail: dict[str, Any],
    fallback_model: str | None = None,
    fallback_response: Response | None = None,
) -> JSONResponse:
    payload: dict[str, Any] = {
        "error": {
            "message": f"Fugu returned HTTP {original_detail.get('status_code')} for {requested_model}: {original_detail.get('message')}",
            "type": "fugu_content_policy_block",
            "code": "fugu_content_policy_block",
            "fugu": _jsonable(original_detail),
        }
    }
    if fallback_model:
        payload["error"]["fallback_model"] = fallback_model
    if fallback_response is not None:
        payload["error"]["fallback"] = _error_summary(fallback_response.status_code, _response_text(fallback_response))
        payload["error"]["message"] = (
            f"Fugu returned HTTP {original_detail.get('status_code')} for {requested_model}; "
            f"fallback model {fallback_model} also failed with HTTP {fallback_response.status_code}."
        )
    headers = {key: value for key, value in _fallback_headers(original_detail, fallback_model or "").items() if value}
    return JSONResponse(payload, status_code=int(original_detail.get("status_code") or 451), headers=headers)


def _public_models() -> dict[str, Any]:
    now = int(datetime.now(UTC).timestamp())
    return {
        "object": "list",
        "data": [
            {"id": model["id"], "object": "model", "created": now, "owned_by": model["owned_by"]}
            for model in PUBLIC_MODELS
        ],
    }


def _dashboard_window_start(window: str, now: datetime) -> datetime:
    if window == "5h":
        return now - timedelta(hours=5)
    if window == "week":
        return now - timedelta(days=7)
    return now - timedelta(days=30 if window == "30d" else 31)


def _model_label(model: str) -> str:
    model = _normalize_public_model(model)
    for item in PUBLIC_MODELS:
        if item["id"] == model:
            return item["label"]
    return model


def _normalize_public_model(model: str | None) -> str:
    if not model:
        return "unknown"
    if re.match(r"^fugu-ultra-sub-\d+$", model):
        return "fugu-ultra"
    if re.match(r"^fugu-sub-\d+$", model):
        return "fugu"
    aliases = {
        "openai/z-ai/glm-5.2": "glm-5.2",
        "z-ai/glm-5.2": "glm-5.2",
        "openai/qwen/qwen3-embedding-8b": "qwen/qwen3-embedding-8b",
        "openai/openai/gpt-oss-120b": "openai/gpt-oss-120b",
        "xai/grok-4.5": "grok-4.5",
        "xai/grok-4.3": "grok-4.3",
        "xai/grok-imagine-image-quality": "grok-imagine-image-quality",
        "xai/grok-imagine-image": "grok-imagine-image",
    }
    return aliases.get(model, model)


def _dashboard_models(cur) -> list[dict[str, Any]]:
    models = [{"id": "all", "label": "All models", "kind": "all"}]
    seen = {"all"}
    for item in PUBLIC_MODELS:
        models.append({"id": item["id"], "label": item["label"], "kind": item["kind"]})
        seen.add(item["id"])
    cur.execute(
        """
        SELECT DISTINCT COALESCE(NULLIF(upstream_model, ''), NULLIF(model_group, ''), NULLIF(model, ''), NULLIF(requested_model, '')) AS model
        FROM fugu_credit_ledger
        WHERE COALESCE(NULLIF(upstream_model, ''), NULLIF(model_group, ''), NULLIF(model, ''), NULLIF(requested_model, '')) IS NOT NULL
        ORDER BY model
        """
    )
    for row in cur.fetchall():
        model = _normalize_public_model(row["model"])
        if model not in seen:
            models.append({"id": model, "label": model, "kind": "observed"})
            seen.add(model)
    return models


def _direct_route_id(requested_model: str | None, upstream_model: str | None) -> str:
    requested = _normalize_public_model(requested_model)
    upstream = _normalize_public_model(upstream_model or requested)
    if requested in MODEL_PUBLIC_NAMES and upstream != requested:
        return f"fallback:{upstream}"
    return f"direct:{upstream}"


def _direct_route_name(requested_model: str | None, upstream_model: str | None) -> str:
    requested = _normalize_public_model(requested_model)
    upstream = _normalize_public_model(upstream_model or requested)
    if requested in MODEL_PUBLIC_NAMES and upstream != requested:
        return f"Fallback: {_model_label(upstream)} for {_model_label(requested)}"
    return f"Direct: {_model_label(upstream)}"


def _model_filter_values(model_filter: str | None) -> list[str]:
    if not model_filter:
        return []
    normalized = _normalize_public_model(model_filter)
    values = {
        model_filter,
        normalized,
    }
    if normalized == "fugu-ultra":
        values.update({f"fugu-ultra-sub-{i}" for i in range(1, ACCOUNT_COUNT + 1)})
    if normalized == "fugu":
        values.update({f"fugu-sub-{i}" for i in range(1, ACCOUNT_COUNT + 1)})
    if normalized == "glm-5.2":
        values.update({"z-ai/glm-5.2", "openai/z-ai/glm-5.2"})
    if normalized == "qwen/qwen3-embedding-8b":
        values.add("openai/qwen/qwen3-embedding-8b")
    if normalized == "openai/gpt-oss-120b":
        values.add("openai/openai/gpt-oss-120b")
    if normalized == "grok-4.5":
        values.add("xai/grok-4.5")
    if normalized == "grok-4.3":
        values.add("xai/grok-4.3")
    if normalized == "grok-imagine-image-quality":
        values.add("xai/grok-imagine-image-quality")
    if normalized == "grok-imagine-image":
        values.add("xai/grok-imagine-image")
    return sorted(values)


def _model_filter_params(model_filter: str | None) -> tuple[Any, list[str]]:
    values = _model_filter_values(model_filter)
    return (model_filter, values)


def _ledger_model_filter_sql() -> str:
    return (
        "(%s::text IS NULL OR "
        "COALESCE(NULLIF(upstream_model, ''), NULLIF(model_group, ''), NULLIF(model, ''), NULLIF(requested_model, '')) = ANY(%s::text[]))"
    )


def _usage_totals() -> dict[str, Any]:
    return {
        "usage_units": Decimal(0),
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_input_tokens": 0,
        "orchestration_input_tokens": 0,
        "orchestration_output_tokens": 0,
        "total_tokens": 0,
        "estimated_cost_usd": Decimal(0),
        "litellm_spend": Decimal(0),
    }


def _routing_candidates(requested_model: str) -> list[dict[str, Any]]:
    now = _utcnow()
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.account_id, a.display_name, a.priority, d.upstream_model
            FROM fugu_accounts a
            JOIN fugu_model_deployments d ON d.account_id = a.account_id
            LEFT JOIN fugu_account_cooldowns c
              ON c.account_id = a.account_id
             AND c.requested_model = d.requested_model
             AND c.cooldown_until > now()
            WHERE a.enabled = true
              AND d.enabled = true
              AND d.requested_model = %s
              AND c.account_id IS NULL
            ORDER BY a.priority, a.account_id
            """,
            (requested_model,),
        )
        accounts = cur.fetchall()
        if not accounts:
            return []
        cur.execute(
            """
            SELECT account_id, window_kind, reset_anchor_at, reset_interval, allowance_usage_units
            FROM fugu_account_windows
            WHERE account_id = ANY(%s)
            """,
            ([row["account_id"] for row in accounts],),
        )
        windows = cur.fetchall()
        by_account: dict[str, dict[str, dict[str, Any]]] = {}
        for row in windows:
            by_account.setdefault(row["account_id"], {})[row["window_kind"]] = row

        candidates = []
        for account in accounts:
            account_id = account["account_id"]
            scores: dict[str, Any] = {}
            sort_key = []
            chosen_by = "5h"
            for kind in ("5h", "week", "month"):
                window = by_account.get(account_id, {}).get(kind)
                if not window:
                    used = Decimal(0)
                    allowance = None
                    start = now
                else:
                    start = _window_start(window["reset_anchor_at"], window["reset_interval"], now)
                    allowance = window["allowance_usage_units"]
                    cur.execute(
                        """
                        SELECT COALESCE(sum(usage_units), 0) AS used
                        FROM fugu_credit_ledger
                        WHERE account_id = %s
                          AND requested_model = %s
                          AND status = 'success'
                          AND start_time >= %s
                        """,
                        (account_id, requested_model, start),
                    )
                    used = Decimal(str(cur.fetchone()["used"] or 0))
                if allowance:
                    ratio = used / Decimal(str(allowance))
                    score = ratio
                else:
                    ratio = None
                    score = used
                scores[kind] = {
                    "start": start,
                    "used_units": used,
                    "allowance_usage_units": allowance,
                    "ratio": ratio,
                }
                sort_key.append(score)
                if kind == "5h":
                    chosen_by = "5h"
            candidates.append(
                {
                    "account_id": account_id,
                    "display_name": account["display_name"],
                    "upstream_model": account["upstream_model"],
                    "priority": account["priority"],
                    "windows": scores,
                    "sort_key": tuple(sort_key + [Decimal(account["priority"])]),
                    "chosen_by_window": chosen_by,
                }
            )
        candidates.sort(key=lambda row: row["sort_key"])
        return candidates


def _assignment(session_key: str, requested_model: str) -> tuple[str, str, list[dict[str, Any]], str]:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.account_id, s.upstream_model
            FROM fugu_session_assignments s
            JOIN fugu_accounts a ON a.account_id = s.account_id AND a.enabled = true
            JOIN fugu_model_deployments d
              ON d.account_id = s.account_id
             AND d.requested_model = s.requested_model
             AND d.enabled = true
            LEFT JOIN fugu_account_cooldowns c
              ON c.account_id = s.account_id
             AND c.requested_model = s.requested_model
             AND c.cooldown_until > now()
            WHERE s.session_key = %s
              AND s.requested_model = %s
              AND s.state = 'active'
              AND c.account_id IS NULL
            """,
            (session_key, requested_model),
        )
        existing = cur.fetchone()
    candidates = _routing_candidates(requested_model)
    if existing:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE fugu_session_assignments
                SET last_used_at = now()
                WHERE session_key = %s AND requested_model = %s
                """,
                (session_key, requested_model),
            )
        for idx, candidate in enumerate(candidates):
            if candidate["account_id"] == existing["account_id"]:
                candidates.insert(0, candidates.pop(idx))
                break
        return existing["account_id"], existing["upstream_model"], candidates, "sticky"
    if not candidates:
        fallback_account = "sub1"
        return fallback_account, upstream_model(requested_model, fallback_account), [], "fallback-no-db-candidates"
    chosen = candidates[0]
    _upsert_assignment(session_key, requested_model, chosen["account_id"], chosen["upstream_model"], "new")
    _record_decision(session_key, requested_model, chosen, candidates, "new")
    return chosen["account_id"], chosen["upstream_model"], candidates, "new"


def _upsert_assignment(session_key: str, requested_model: str, account_id: str, upstream: str, reason: str) -> None:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO fugu_session_assignments
              (session_key, requested_model, account_id, upstream_model, chosen_by_window, reason)
            VALUES (%s, %s, %s, %s, '5h', %s)
            ON CONFLICT (session_key, requested_model) DO UPDATE SET
              account_id = EXCLUDED.account_id,
              upstream_model = EXCLUDED.upstream_model,
              chosen_by_window = EXCLUDED.chosen_by_window,
              last_used_at = now(),
              state = 'active',
              reason = EXCLUDED.reason
            """,
            (session_key, requested_model, account_id, upstream, reason),
        )


def _mark_failover_assignment(
    session_key: str,
    requested_model: str,
    account_id: str,
    upstream: str,
) -> None:
    _upsert_assignment(
        session_key,
        requested_model,
        account_id,
        upstream,
        "limit-failover",
    )
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE fugu_session_assignments
            SET failover_count = failover_count + 1
            WHERE session_key = %s AND requested_model = %s
            """,
            (session_key, requested_model),
        )


def _record_decision(
    session_key: str,
    requested_model: str,
    chosen: dict[str, Any],
    candidates: list[dict[str, Any]],
    reason: str,
) -> None:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO fugu_routing_decisions
              (session_key, requested_model, selected_account_id, selected_upstream_model,
               chosen_by_window, reason, candidates)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
            """,
            (
                session_key,
                requested_model,
                chosen["account_id"],
                chosen["upstream_model"],
                chosen.get("chosen_by_window") or "5h",
                reason,
                json.dumps(_jsonable(candidates)),
            ),
        )


def _cooldown(account_id: str, requested_model: str, reason: str, error: dict[str, Any]) -> None:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO fugu_account_cooldowns
              (account_id, requested_model, cooldown_until, reason, last_error, updated_at)
            VALUES (%s, %s, now() + (%s || ' seconds')::interval, %s, %s::jsonb, now())
            ON CONFLICT (account_id, requested_model) DO UPDATE SET
              cooldown_until = EXCLUDED.cooldown_until,
              reason = EXCLUDED.reason,
              last_error = EXCLUDED.last_error,
              updated_at = now()
            """,
            (account_id, requested_model, COOLDOWN_SECONDS, reason, json.dumps(_jsonable(error))),
        )


def _dashboard_html() -> str:
    return r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Fugu Credits</title>
  <style>
    :root { color-scheme: dark; --bg:#111; --panel:#1a1a1a; --line:#303030; --text:#eee; --muted:#aaa; --green:#65c18c; --amber:#d8a84f; --blue:#6aa9ff; --red:#e06c75; }
    * { box-sizing: border-box; }
    body { margin:0; font:14px/1.4 system-ui, -apple-system, Segoe UI, sans-serif; background:var(--bg); color:var(--text); }
    header { display:flex; align-items:center; justify-content:space-between; gap:16px; padding:18px 22px; border-bottom:1px solid var(--line); }
    h1 { font-size:20px; margin:0; font-weight:650; letter-spacing:0; }
    main { padding:18px 22px 28px; display:grid; gap:18px; }
    .controls { display:flex; flex-wrap:wrap; gap:10px; align-items:center; }
    select, input, button { background:#181818; color:var(--text); border:1px solid var(--line); border-radius:6px; padding:8px 10px; font:inherit; }
    button { cursor:pointer; }
    .metrics { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:10px; }
    .metric { border:1px solid var(--line); background:var(--panel); border-radius:8px; padding:14px; min-height:86px; }
    .metric .label { color:var(--muted); font-size:12px; }
    .metric .value { font-size:24px; font-weight:700; margin-top:8px; overflow-wrap:anywhere; }
    .grid { display:grid; grid-template-columns:minmax(0, 1.5fr) minmax(360px, .9fr); gap:18px; }
    section { border-top:1px solid var(--line); padding-top:16px; min-width:0; }
    h2 { font-size:15px; margin:0 0 12px; }
    canvas { width:100%; height:320px; display:block; background:#151515; border:1px solid var(--line); border-radius:8px; }
    .legend { display:flex; flex-wrap:wrap; gap:8px 16px; min-height:20px; margin:0 0 10px; color:var(--muted); font-size:12px; }
    .legend-item { display:inline-flex; align-items:center; gap:6px; min-width:0; max-width:260px; }
    .swatch { width:10px; height:10px; border-radius:2px; flex:0 0 10px; }
    .legend-label { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    table { width:100%; border-collapse:collapse; }
    th, td { border-bottom:1px solid var(--line); padding:9px 7px; text-align:right; white-space:nowrap; }
    th:first-child, td:first-child { text-align:left; }
    th { color:var(--muted); font-weight:600; }
    .muted { color:var(--muted); }
    .ok { color:var(--green); }
    .warn { color:var(--amber); }
    .bad { color:var(--red); }
    @media (max-width:900px) { .grid { grid-template-columns:1fr; } header { align-items:flex-start; flex-direction:column; } }
  </style>
</head>
<body>
  <header>
    <h1>Fugu Credit Usage</h1>
    <div class="controls">
      <select id="window"><option value="5h">5 hours</option><option value="week">Week</option><option value="month">Month</option><option value="30d">30 days</option></select>
      <select id="model"><option value="all">All models</option></select>
      <input id="key" type="password" placeholder="LiteLLM key" autocomplete="off">
      <button id="refresh">Refresh</button>
    </div>
  </header>
  <main>
    <div class="metrics" id="metrics"></div>
    <div class="grid">
      <section>
        <h2>Token Usage</h2>
        <div id="chartLegend" class="legend"></div>
        <canvas id="chart" width="1100" height="320"></canvas>
      </section>
      <section>
        <h2>Routes</h2>
        <div id="accounts"></div>
      </section>
    </div>
    <section>
      <h2>Recent Requests</h2>
      <div id="recent"></div>
    </section>
    <section>
      <h2>Console Snapshots</h2>
      <div id="snapshots"></div>
    </section>
  </main>
  <script>
    const $ = (id) => document.getElementById(id);
    const nf = new Intl.NumberFormat();
    const cf = new Intl.NumberFormat(undefined, {style:'currency', currency:'USD', maximumFractionDigits:4});
    const colors = ['#65c18c', '#6aa9ff', '#d8a84f', '#e06c75', '#9d7cd8'];
    let modelOptionsLoaded = false;
    $('key').value = sessionStorage.getItem('fuguKey') || '';
    $('key').addEventListener('change', () => sessionStorage.setItem('fuguKey', $('key').value));
    $('refresh').onclick = load;
    $('window').onchange = load;
    $('model').onchange = load;
    function fmt(n) { return nf.format(Math.round(Number(n || 0))); }
    function money(n) { return cf.format(Number(n || 0)); }
    function esc(v) { return String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
    async function load() {
      sessionStorage.setItem('fuguKey', $('key').value);
      const url = `/fugu/api/summary?window=${encodeURIComponent($('window').value)}&model=${encodeURIComponent($('model').value)}`;
      const res = await fetch(url, {headers: {Authorization: `Bearer ${$('key').value}`}});
      if (!res.ok) { $('metrics').innerHTML = `<div class="metric"><div class="label">Status</div><div class="value bad">${res.status}</div></div>`; return; }
      render(await res.json());
    }
    function render(data) {
      renderModelSelect(data.models || []);
      const totals = data.totals || {};
      $('metrics').innerHTML = [
        ['Usage units', fmt(totals.usage_units)],
        ['Input tokens', fmt(totals.input_tokens)],
        ['Output tokens', fmt(totals.output_tokens)],
        ['Cached input', fmt(totals.cached_input_tokens)],
        ['Orch input', fmt(totals.orchestration_input_tokens)],
        ['Orch output', fmt(totals.orchestration_output_tokens)],
        ['Estimated cost', money(totals.estimated_cost_usd)],
        ['LiteLLM spend', money(totals.litellm_spend)]
      ].map(([label, value]) => `<div class="metric"><div class="label">${label}</div><div class="value">${value}</div></div>`).join('');
      renderAccounts(data.accounts || []);
      renderTable('recent', data.recent || [], ['start_time','account_id','requested_model','upstream_model','session_id','total_tokens','usage_units','estimated_cost_usd','litellm_spend','status']);
      renderTable('snapshots', data.snapshots || [], ['captured_at','account_id','period_label','input_tokens','output_tokens','cached_input_tokens','orchestration_input_tokens','orchestration_output_tokens','usage_units']);
      drawChart(data.series || [], data.accounts || []);
    }
    function renderModelSelect(models) {
      if (!models.length) return;
      const current = $('model').value || 'all';
      $('model').innerHTML = models.map(m => `<option value="${esc(m.id)}">${esc(m.label || m.id)}</option>`).join('');
      $('model').value = models.some(m => m.id === current) ? current : 'all';
      modelOptionsLoaded = true;
    }
    function renderAccounts(accounts) {
      const rows = accounts.map(a => `<tr><td>${esc(a.account_id)}</td><td>${esc(a.display_name || '')}</td><td>${esc(a.account_kind || '')}</td><td>${fmt(a.window?.used_units)}</td><td>${a.window?.ratio == null ? '<span class="muted">n/a</span>' : (Number(a.window.ratio)*100).toFixed(2)+'%'}</td><td>${fmt(a.total_tokens)}</td><td>${money(a.estimated_cost_usd)}</td><td>${money(a.litellm_spend)}</td></tr>`).join('');
      $('accounts').innerHTML = `<table><thead><tr><th>Route</th><th>Name</th><th>Kind</th><th>Window units</th><th>Util</th><th>Tokens</th><th>Est.</th><th>Spend</th></tr></thead><tbody>${rows}</tbody></table>`;
    }
    function renderTable(id, rows, cols) {
      if (!rows.length) { $(id).innerHTML = '<p class="muted">No rows.</p>'; return; }
      $(id).innerHTML = `<table><thead><tr>${cols.map(c=>`<th>${c}</th>`).join('')}</tr></thead><tbody>` +
        rows.map(r => `<tr>${cols.map(c => `<td>${formatCell(c, r[c])}</td>`).join('')}</tr>`).join('') + '</tbody></table>';
    }
    function formatCell(c, v) {
      if (v == null) return '<span class="muted">n/a</span>';
      if (String(c).includes('cost') || String(c).includes('spend')) return money(v);
      if (String(c).includes('tokens') || String(c).includes('units')) return fmt(v);
      if (String(c).includes('time') || String(c).includes('_at')) return new Date(v).toLocaleString();
      return esc(v);
    }
    function drawChart(series, accounts) {
      const canvas = $('chart'), ctx = canvas.getContext('2d'), w = canvas.width, h = canvas.height;
      ctx.clearRect(0,0,w,h); ctx.fillStyle = '#151515'; ctx.fillRect(0,0,w,h);
      const padL = 74, padR = 18, padT = 18, padB = 42;
      const plotW = w - padL - padR, plotH = h - padT - padB;
      const buckets = [...new Set(series.map(s=>s.bucket))], routes = [...new Set(series.map(s=>s.account_id))];
      const names = Object.fromEntries(accounts.map(a => [a.account_id, a.display_name || a.account_id]));
      $('chartLegend').innerHTML = routes.length
        ? routes.map((a, i) => `<span class="legend-item"><span class="swatch" style="background:${colors[i % colors.length]}"></span><span class="legend-label">${esc(names[a] || a)}</span></span>`).join('')
        : '<span class="muted">No usage in this window.</span>';
      const max = Math.max(1, ...buckets.map(b => series.filter(s=>s.bucket===b).reduce((a,s)=>a+Number(s.usage_units||0),0)));
      ctx.strokeStyle = '#303030'; ctx.fillStyle = '#aaa'; ctx.font = '12px system-ui'; ctx.textAlign = 'right';
      for (let i=0;i<5;i++) { const y = padT + plotH*i/4; ctx.beginPath(); ctx.moveTo(padL,y); ctx.lineTo(w-padR,y); ctx.stroke(); ctx.fillText(fmt(max*(1-i/4)), padL - 8, y+4); }
      const slot = plotW / Math.max(1, buckets.length);
      const bw = Math.max(6, Math.min(54, slot * 0.68));
      const labelEvery = Math.max(1, Math.ceil(buckets.length / 7));
      buckets.forEach((b, i) => {
        let y = h-padB, x = padL + i*slot + (slot-bw)/2;
        routes.forEach((a, ai) => {
          const row = series.find(s=>s.bucket===b && s.account_id===a), val = Number(row?.usage_units || 0);
          const bh = plotH * val / max;
          ctx.fillStyle = colors[ai % colors.length]; ctx.fillRect(x, y-bh, bw, bh); y -= bh;
        });
        if (i % labelEvery === 0) {
          const d = new Date(b);
          const label = $('window').value === '5h'
            ? d.toLocaleTimeString(undefined,{hour:'numeric', minute:'2-digit'})
            : d.toLocaleDateString(undefined,{month:'short',day:'numeric'});
          ctx.fillStyle = '#aaa'; ctx.textAlign = 'center'; ctx.fillText(label, Math.min(w - padR, Math.max(padL, x + bw / 2)), h-16); ctx.textAlign = 'right';
        }
      });
    }
    load();
  </script>
</body>
</html>"""


@app.on_event("startup")
def startup() -> None:
    ensure_db()


@app.get("/health/process")
async def process_health() -> JSONResponse:
    """Process-only liveness for load balancers; never probes LiteLLM."""
    return JSONResponse({"status": "router-alive"})


@app.get("/health/liveliness")
async def health() -> Response:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            upstream = await client.get(f"{UPSTREAM}/health/liveliness")
        return Response(upstream.content, status_code=upstream.status_code, media_type=upstream.headers.get("content-type"))
    except Exception:
        return JSONResponse({"status": "router-alive", "upstream": "unreachable"}, status_code=503)


@app.get("/v1/models")
@app.get("/models")
async def models(request: Request) -> Response:
    # The master key sees the stable public aliases. Virtual keys are validated
    # by LiteLLM and receive its key-filtered model list instead of being
    # rejected by this front door.
    if not MASTER_KEY or _bearer(request) == MASTER_KEY:
        return JSONResponse(_public_models())
    return await _forward_once(request, "v1/models", b"", False)


@app.get("/fugu/dashboard")
async def dashboard() -> HTMLResponse:
    return HTMLResponse(_dashboard_html())


@app.get("/fugu/api/summary")
@_retry_read_operation
def summary(request: Request, window: str = "5h", model: str = "all") -> JSONResponse:
    denied = _require_admin(request)
    if denied:
        return denied
    window = window if window in ("5h", "week", "month", "30d") else "5h"
    model_filter = None if model == "all" else model
    now = _utcnow()
    global_start = _dashboard_window_start(window, now)
    filter_sql = _ledger_model_filter_sql()

    with _conn() as conn, conn.cursor() as cur:
        models = _dashboard_models(cur)
        cur.execute("SELECT account_id, display_name, account_kind FROM fugu_accounts WHERE enabled = true ORDER BY priority")
        account_rows = cur.fetchall()

        accounts = []
        totals = _usage_totals()
        min_start = global_start
        for account in account_rows:
            account_id = account["account_id"]
            if window == "30d":
                start = global_start
                allowance = None
            else:
                cur.execute(
                    """
                    SELECT reset_anchor_at, reset_interval, allowance_usage_units
                    FROM fugu_account_windows
                    WHERE account_id = %s AND window_kind = %s
                    """,
                    (account_id, window),
                )
                row = cur.fetchone()
                start = _window_start(row["reset_anchor_at"], row["reset_interval"], now) if row else now
                allowance = row["allowance_usage_units"] if row else None
            min_start = min(min_start, start)
            cur.execute(
                f"""
                SELECT
                  COALESCE(sum(usage_units), 0) AS usage_units,
                  COALESCE(sum(input_tokens), 0) AS input_tokens,
                  COALESCE(sum(output_tokens), 0) AS output_tokens,
                  COALESCE(sum(cached_input_tokens), 0) AS cached_input_tokens,
                  COALESCE(sum(orchestration_input_tokens), 0) AS orchestration_input_tokens,
                  COALESCE(sum(orchestration_output_tokens), 0) AS orchestration_output_tokens,
                  COALESCE(sum(total_tokens), 0) AS total_tokens,
                  COALESCE(sum(estimated_cost_usd), 0) AS estimated_cost_usd,
                  COALESCE(sum(litellm_spend), 0) AS litellm_spend
                FROM fugu_credit_ledger
                WHERE account_id = %s
                  AND status = 'success'
                  AND start_time >= %s
                  AND {filter_sql}
                """,
                (account_id, start, *_model_filter_params(model_filter)),
            )
            used = cur.fetchone()
            for key in totals:
                totals[key] += used[key] or 0
            has_usage = any(used[key] for key in ("usage_units", "total_tokens", "estimated_cost_usd", "litellm_spend"))
            if model_filter not in (None, *MODEL_PUBLIC_NAMES) and not has_usage:
                continue
            ratio = (Decimal(str(used["usage_units"])) / Decimal(str(allowance))) if allowance else None
            accounts.append(
                {
                    **account,
                    **used,
                    "window": {
                        "kind": window,
                        "start": start,
                        "used_units": used["usage_units"],
                        "allowance_usage_units": allowance,
                        "ratio": ratio,
                    },
                }
            )

        cur.execute(
            f"""
            SELECT requested_model, upstream_model, model_group, model,
                   COALESCE(sum(usage_units), 0) AS usage_units,
                   COALESCE(sum(input_tokens), 0) AS input_tokens,
                   COALESCE(sum(output_tokens), 0) AS output_tokens,
                   COALESCE(sum(cached_input_tokens), 0) AS cached_input_tokens,
                   COALESCE(sum(orchestration_input_tokens), 0) AS orchestration_input_tokens,
                   COALESCE(sum(orchestration_output_tokens), 0) AS orchestration_output_tokens,
                   COALESCE(sum(total_tokens), 0) AS total_tokens,
                   COALESCE(sum(estimated_cost_usd), 0) AS estimated_cost_usd,
                   COALESCE(sum(litellm_spend), 0) AS litellm_spend
            FROM fugu_credit_ledger
            WHERE account_id IS NULL
              AND status = 'success'
              AND start_time >= %s
              AND {filter_sql}
            GROUP BY 1, 2, 3, 4
            ORDER BY 1, 2, 3, 4
            """,
            (global_start, *_model_filter_params(model_filter)),
        )
        for row in cur.fetchall():
            upstream = row["upstream_model"] or row["model_group"] or row["model"] or row["requested_model"]
            route_id = _direct_route_id(row["requested_model"], upstream)
            used = {key: row[key] for key in totals}
            for key in totals:
                totals[key] += used[key] or 0
            accounts.append(
                {
                    "account_id": route_id,
                    "display_name": _direct_route_name(row["requested_model"], upstream),
                    "account_kind": "fallback" if route_id.startswith("fallback:") else "direct",
                    **used,
                    "window": {
                        "kind": window,
                        "start": global_start,
                        "used_units": used["usage_units"],
                        "allowance_usage_units": None,
                        "ratio": None,
                    },
                }
            )

        bucket = "hour" if window == "5h" else "day"
        cur.execute(
            f"""
            SELECT date_trunc('{bucket}', start_time) AS bucket,
                   account_id, requested_model, upstream_model, model_group, model,
                   COALESCE(sum(usage_units), 0) AS usage_units
            FROM fugu_credit_ledger
            WHERE status = 'success'
              AND start_time >= %s
              AND {filter_sql}
            GROUP BY 1, 2, 3, 4, 5, 6
            ORDER BY 1, 2, 3, 4, 5, 6
            """,
            (min_start, *_model_filter_params(model_filter)),
        )
        series_by_route: dict[tuple[Any, str], dict[str, Any]] = {}
        for row in cur.fetchall():
            item = dict(row)
            if not item.get("account_id"):
                upstream = item.get("upstream_model") or item.get("model_group") or item.get("model") or item.get("requested_model")
                item["account_id"] = _direct_route_id(item.get("requested_model"), upstream)
            key = (item["bucket"], item["account_id"])
            if key not in series_by_route:
                series_by_route[key] = {"bucket": item["bucket"], "account_id": item["account_id"], "usage_units": Decimal(0)}
            series_by_route[key]["usage_units"] += Decimal(str(item.get("usage_units") or 0))
        series = list(series_by_route.values())

        cur.execute(
            f"""
            SELECT start_time, account_id, requested_model, upstream_model, model_group, session_id, total_tokens,
                   usage_units, estimated_cost_usd, litellm_spend, status
            FROM fugu_credit_ledger
            WHERE {filter_sql}
            ORDER BY start_time DESC NULLS LAST, created_at DESC
            LIMIT 50
            """,
            _model_filter_params(model_filter),
        )
        recent = []
        for row in cur.fetchall():
            item = dict(row)
            if not item.get("account_id"):
                upstream = item.get("upstream_model") or item.get("model_group") or item.get("requested_model")
                item["account_id"] = _direct_route_id(item.get("requested_model"), upstream)
            recent.append(item)

        cur.execute(
            """
            SELECT captured_at, account_id, period_label, input_tokens, output_tokens,
                   cached_input_tokens, orchestration_input_tokens,
                   orchestration_output_tokens, orchestration_input_cached_tokens,
                   usage_units, estimated_cost_usd
            FROM fugu_console_snapshots
            ORDER BY captured_at DESC
            LIMIT 20
            """
        )
        snapshots = cur.fetchall()

    return JSONResponse(
        _jsonable(
            {
                "window": window,
                "model": model,
                "models": models,
                "totals": totals,
                "accounts": accounts,
                "series": series,
                "recent": recent,
                "snapshots": snapshots,
            }
        )
    )


async def _forward_once(
    request: Request,
    path: str,
    body: bytes,
    stream: bool,
    stream_complete: Callable[[dict[str, Any], int, dict[str, str], datetime], None] | None = None,
    sse_prefix_events: list[dict[str, Any]] | None = None,
    extra_response_headers: dict[str, str] | None = None,
) -> Response:
    headers = _headers_for_upstream(request)
    url = f"{UPSTREAM}/{path}"
    timeout = httpx.Timeout(600.0, connect=20.0)
    client = httpx.AsyncClient(timeout=timeout)
    req = client.build_request(request.method, url, content=body, headers=headers, params=request.query_params)
    resp = await client.send(req, stream=stream)
    passthrough_headers = {
        k: v
        for k, v in resp.headers.items()
        if k.lower() not in {"content-length", "content-encoding", "transfer-encoding", "connection"}
    }
    if extra_response_headers:
        passthrough_headers.update({k: v for k, v in extra_response_headers.items() if v})
    if stream and resp.status_code < 400:
        stream_events: list[dict[str, Any]] = []
        pending = b""

        def consume_sse(data: bytes) -> None:
            nonlocal pending
            pending += data
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                line = line.strip()
                if not line.startswith(b"data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == b"[DONE]":
                    continue
                try:
                    event = json.loads(payload.decode("utf-8"))
                except Exception:
                    continue
                metadata = _stream_event_metadata(event)
                if metadata:
                    stream_events.append(metadata)

        async def gen():
            try:
                for event in sse_prefix_events or []:
                    yield f"data: {json.dumps(event, separators=(',', ':'))}\n\n".encode("utf-8")
                async for chunk in resp.aiter_bytes():
                    consume_sse(chunk)
                    yield chunk
            finally:
                if pending.strip():
                    consume_sse(b"\n")
                if stream_complete:
                    try:
                        await asyncio.to_thread(
                            stream_complete,
                            _stream_response_obj(stream_events),
                            resp.status_code,
                            passthrough_headers,
                            _utcnow(),
                        )
                    except Exception as exc:
                        print(f"fugu_session_router: stream ledger write failed: {type(exc).__name__}: {exc}", flush=True)
                await resp.aclose()
                await client.aclose()
        return StreamingResponse(gen(), status_code=resp.status_code, headers=passthrough_headers)
    content = await resp.aread()
    await resp.aclose()
    await client.aclose()
    return Response(content, status_code=resp.status_code, headers=passthrough_headers)


def _write_proxy_ledger(
    request: Request,
    path: str,
    requested_model: str,
    candidate: dict[str, Any],
    metadata: dict[str, Any],
    status_code: int,
    response_headers: dict[str, str],
    response_obj: dict[str, Any],
    started_at: datetime,
    ended_at: datetime,
) -> None:
    headers = {k.lower(): v for k, v in request.headers.items()}
    response_headers = {k.lower(): v for k, v in response_headers.items()}
    if isinstance(response_obj, dict):
        metadata_obj = response_obj.get("metadata")
        if not isinstance(metadata_obj, dict):
            metadata_obj = {}
        metadata_obj["fugu_response_headers"] = response_headers
        response_obj["metadata"] = metadata_obj
    kwargs = {
        "model": candidate["upstream_model"],
        "call_type": path,
        "litellm_params": {"metadata": metadata},
        "proxy_server_request": {"headers": headers},
        "standard_logging_object": {
            "request_id": response_obj.get("id") if isinstance(response_obj, dict) else None,
            "model": requested_model,
            "model_group": candidate["upstream_model"],
            "status_code": status_code,
            "call_type": path,
            "response_cost": _decimal_header(response_headers, "x-litellm-response-cost"),
            "response_cost_original": _decimal_header(response_headers, "x-litellm-response-cost-original"),
            "response_duration_ms": _decimal_header(response_headers, "x-litellm-response-duration-ms"),
            "response_headers": response_headers,
        },
    }
    status = "success" if status_code < 400 else "failure"
    error = None if status_code < 400 else response_obj
    write_ledger(kwargs, response_obj, started_at, ended_at, status=status, error=error)


def _record_proxy_ledger(
    request: Request,
    path: str,
    requested_model: str,
    candidate: dict[str, Any],
    metadata: dict[str, Any],
    response: Response,
    started_at: datetime,
    ended_at: datetime,
) -> None:
    if isinstance(response, StreamingResponse):
        return
    body = getattr(response, "body", b"") or b""
    try:
        response_obj = json.loads(body.decode("utf-8"))
    except Exception:
        response_obj = {"raw_body": body.decode("utf-8", errors="replace")[:2000]}
    _write_proxy_ledger(
        request,
        path,
        requested_model,
        candidate,
        metadata,
        response.status_code,
        dict(response.headers),
        response_obj,
        started_at,
        ended_at,
    )


async def _direct_model_proxy(request: Request, path: str, body: dict[str, Any]) -> Response:
    requested_model = str(body.get("model") or "")
    routed = dict(body)
    if routed.get("stream") and _chat_completion_path(path):
        stream_options = dict(routed.get("stream_options") or {})
        stream_options["include_usage"] = True
        routed["stream_options"] = stream_options
    _apply_glm_plain_text_defaults(routed, path)

    has_request_metadata = isinstance(routed.get("metadata"), dict)
    meta = dict(routed.get("metadata") or {})
    meta.update(
        {
            "fugu_requested_model": requested_model,
            "fugu_direct_model": requested_model,
            "fugu_routing_reason": "direct-provider",
        }
    )
    if has_request_metadata:
        routed["metadata"] = meta
    candidate = {"account_id": "direct", "upstream_model": requested_model}
    started_at = _utcnow()

    def stream_complete(
        response_obj: dict[str, Any],
        status_code: int,
        response_headers: dict[str, str],
        ended_at: datetime,
    ) -> None:
        _write_proxy_ledger(
            request,
            path,
            requested_model,
            candidate,
            meta,
            status_code,
            response_headers,
            response_obj,
            started_at,
            ended_at,
        )

    is_stream = bool(routed.get("stream"))
    response = await _forward_once(
        request,
        path,
        json.dumps(routed).encode("utf-8"),
        is_stream,
        stream_complete if is_stream else None,
    )
    ended_at = _utcnow()
    try:
        await asyncio.to_thread(
            _record_proxy_ledger,
            request,
            path,
            requested_model,
            candidate,
            meta,
            response,
            started_at,
            ended_at,
        )
    except Exception as exc:
        print(f"fugu_session_router: direct provider ledger write failed: {type(exc).__name__}: {exc}", flush=True)
    return response


async def _content_policy_fallback(
    request: Request,
    path: str,
    body: dict[str, Any],
    requested_model: str,
    session_key: str,
    failed_candidate: dict[str, Any],
    failed_response: Response,
) -> Response:
    detail = _content_policy_detail(requested_model, failed_candidate, failed_response)
    fallback_model = CONTENT_POLICY_FALLBACK_MODEL
    if not CONTENT_POLICY_FALLBACK_ENABLED or not fallback_model or not _chat_completion_path(path):
        return _content_policy_failure_response(requested_model, detail, fallback_model or None)

    routed = dict(body)
    routed["model"] = fallback_model
    is_stream = bool(routed.get("stream"))
    if is_stream:
        stream_options = dict(routed.get("stream_options") or {})
        stream_options["include_usage"] = True
        routed["stream_options"] = stream_options
    _apply_glm_plain_text_defaults(routed, path)

    meta = dict(routed.get("metadata") or {})
    meta.update(
        {
            "fugu_session_id": session_key,
            "fugu_requested_model": requested_model,
            "fugu_routing_reason": "content-policy-fallback",
            "fugu_fallback_model": fallback_model,
            "fugu_fallback_from_model": detail.get("upstream_model"),
            "fugu_fallback_from_account": detail.get("account_id"),
            "fugu_fallback_original_status": detail.get("status_code"),
            "fugu_fallback_original_message": str(detail.get("message") or "")[:500],
        }
    )
    routed["metadata"] = meta

    fallback_candidate = {"account_id": "redpill", "upstream_model": fallback_model}
    notice = _fallback_notice(detail, fallback_model)
    response_headers = _fallback_headers(detail, fallback_model)
    prefix_events = [_notice_chunk(notice, fallback_model)] if is_stream and _visible_notice_allowed(body) else None

    started_at = _utcnow()

    def stream_complete(
        response_obj: dict[str, Any],
        status_code: int,
        response_headers: dict[str, str],
        ended_at: datetime,
    ) -> None:
        _write_proxy_ledger(
            request,
            path,
            requested_model,
            fallback_candidate,
            meta,
            status_code,
            response_headers,
            response_obj,
            started_at,
            ended_at,
        )

    fallback_response = await _forward_once(
        request,
        path,
        json.dumps(routed).encode("utf-8"),
        is_stream,
        stream_complete if is_stream else None,
        prefix_events,
        response_headers,
    )
    ended_at = _utcnow()
    try:
        await asyncio.to_thread(
            _record_proxy_ledger,
            request,
            path,
            requested_model,
            fallback_candidate,
            meta,
            fallback_response,
            started_at,
            ended_at,
        )
    except Exception as exc:
        print(f"fugu_session_router: content-policy fallback ledger write failed: {type(exc).__name__}: {exc}", flush=True)

    if fallback_response.status_code < 400:
        if isinstance(fallback_response, StreamingResponse):
            return _with_headers(fallback_response, response_headers)
        return _annotate_fallback_response(fallback_response, body, detail, fallback_model, notice)

    return _content_policy_failure_response(requested_model, detail, fallback_model, fallback_response)


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def proxy(path: str, request: Request) -> Response:
    raw = await request.body()
    content_type = request.headers.get("content-type", "")
    if request.method.upper() != "POST" or "json" not in content_type:
        return await _forward_once(request, path, raw, False)

    try:
        body = json.loads(raw.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return await _forward_once(request, path, raw, False)

    requested_model = body.get("model")
    if requested_model not in MODEL_PUBLIC_NAMES:
        if requested_model in DIRECT_PUBLIC_NAMES:
            return await _direct_model_proxy(request, path, body)
        return await _forward_once(request, path, raw, bool(body.get("stream")))

    session_key = _session_key(request, body)
    account_id, selected_upstream, candidates, reason = await asyncio.to_thread(
        _assignment,
        session_key,
        requested_model,
    )
    attempts = []
    seen = set()
    for candidate in ([{"account_id": account_id, "upstream_model": selected_upstream}] + candidates):
        key = (candidate["account_id"], candidate["upstream_model"])
        if key not in seen:
            attempts.append(candidate)
            seen.add(key)

    last_response: Response | None = None
    for attempt_index, candidate in enumerate(attempts):
        routed = dict(body)
        routed["model"] = candidate["upstream_model"]
        if routed.get("stream"):
            stream_options = dict(routed.get("stream_options") or {})
            stream_options["include_usage"] = True
            routed["stream_options"] = stream_options
        meta = dict(routed.get("metadata") or {})
        meta.update(
            {
                "fugu_session_id": session_key,
                "fugu_requested_model": requested_model,
                "fugu_account_id": candidate["account_id"],
                "fugu_upstream_model": candidate["upstream_model"],
                "fugu_routing_reason": reason if attempt_index == 0 else "limit-failover",
            }
        )
        routed["metadata"] = meta
        started_at = _utcnow()
        def stream_complete(
            response_obj: dict[str, Any],
            status_code: int,
            response_headers: dict[str, str],
            ended_at: datetime,
            *,
            candidate: dict[str, Any] = candidate,
            meta: dict[str, Any] = meta,
            started_at: datetime = started_at,
        ) -> None:
            _write_proxy_ledger(
                request,
                path,
                requested_model,
                candidate,
                meta,
                status_code,
                response_headers,
                response_obj,
                started_at,
                ended_at,
            )

        is_stream = bool(routed.get("stream"))
        response = await _forward_once(
            request,
            path,
            json.dumps(routed).encode("utf-8"),
            is_stream,
            stream_complete if is_stream else None,
        )
        ended_at = _utcnow()
        try:
            await asyncio.to_thread(
                _record_proxy_ledger,
                request,
                path,
                requested_model,
                candidate,
                meta,
                response,
                started_at,
                ended_at,
            )
        except Exception as exc:
            print(f"fugu_session_router: ledger write failed: {type(exc).__name__}: {exc}", flush=True)
        if response.status_code < 400:
            if attempt_index > 0 or candidate["account_id"] != account_id:
                await asyncio.to_thread(
                    _mark_failover_assignment,
                    session_key,
                    requested_model,
                    candidate["account_id"],
                    candidate["upstream_model"],
                )
            return response
        text = response.body.decode("utf-8", errors="replace") if hasattr(response, "body") else ""
        last_response = response
        if _content_policy_error(response.status_code, text):
            return await _content_policy_fallback(request, path, body, requested_model, session_key, candidate, response)
        if not _limit_error(response.status_code, text):
            return response
        await asyncio.to_thread(
            _cooldown,
            candidate["account_id"],
            requested_model,
            f"upstream status {response.status_code}",
            {"status_code": response.status_code, "body": text[:2000]},
        )

    return last_response or JSONResponse({"error": {"message": "no fugu subscription available"}}, status_code=503)
