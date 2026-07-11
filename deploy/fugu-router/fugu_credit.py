"""Fugu credit ledger and LiteLLM telemetry callback.

The Sakana API exposes detailed token usage, but not subscription balances. This
module stores every token/cost-shaped field we see into Postgres so routing and
calibration can be replayed later against console snapshots.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from litellm.integrations.custom_logger import CustomLogger

try:
    import psycopg
    from psycopg.rows import dict_row
except Exception:  # pragma: no cover - import failure is reported at runtime.
    psycopg = None
    dict_row = None


DATABASE_URL = os.environ.get("DATABASE_URL", "")
ACCOUNT_COUNT = int(os.environ.get("FUGU_ACCOUNT_COUNT", "3"))
MODEL_PUBLIC_NAMES = ("fugu-ultra", "fugu")
WINDOWS = {
    "5h": "5 hours",
    "week": "7 days",
    "month": "1 month",
}

_SCHEMA_READY = False
_REDACT_KEYS = ("authorization", "api_key", "user_api_key", "secret", "password", "cookie", "bearer")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _jsonable(value: Any, *, redact: bool = True) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v, redact=redact) for v in value]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, val in value.items():
            skey = str(key)
            if redact and any(marker in skey.lower() for marker in _REDACT_KEYS):
                out[skey] = "[redacted]"
            else:
                out[skey] = _jsonable(val, redact=redact)
        return out
    for attr in ("model_dump", "dict"):
        fn = getattr(value, attr, None)
        if callable(fn):
            try:
                return _jsonable(fn(), redact=redact)
            except Exception:
                pass
    return str(value)


def _as_dict(value: Any) -> dict[str, Any]:
    value = _jsonable(value)
    return value if isinstance(value, dict) else {}


def _number(*values: Any) -> int | None:
    for value in values:
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float, Decimal)) and math.isfinite(float(value)):
            return int(value)
        if isinstance(value, str):
            try:
                return int(float(value))
            except ValueError:
                continue
    return None


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            return None
    return None


def account_from_model(model: str | None) -> str | None:
    if not model:
        return None
    match = re.search(r"(?:^|-)sub-?(\d+)$", model)
    if match:
        return f"sub{match.group(1)}"
    match = re.search(r"(?:^|-)payg$", model)
    return "payg" if match else None


def requested_model_from_upstream(model: str | None) -> str | None:
    if not model:
        return None
    if model.startswith("fugu-ultra"):
        return "fugu-ultra"
    if model.startswith("fugu"):
        return "fugu"
    return model


def upstream_model(requested_model: str, account_id: str) -> str:
    suffix = account_id.replace("sub", "sub-")
    return f"{requested_model}-{suffix}"


def extract_usage(response_obj: Any) -> dict[str, Any]:
    response = _as_dict(response_obj)
    usage = _as_dict(response.get("usage"))
    if not usage:
        return {}

    prompt_details = _as_dict(
        usage.get("prompt_tokens_details")
        or usage.get("input_tokens_details")
        or usage.get("input_token_details")
    )
    completion_details = _as_dict(
        usage.get("completion_tokens_details")
        or usage.get("output_tokens_details")
        or usage.get("output_token_details")
    )

    input_tokens = _number(usage.get("input_tokens"), usage.get("prompt_tokens"))
    output_tokens = _number(usage.get("output_tokens"), usage.get("completion_tokens"))
    total_tokens = _number(usage.get("total_tokens"))
    cached_input_tokens = _number(
        usage.get("cached_input_tokens"),
        usage.get("cache_read_input_tokens"),
        prompt_details.get("cached_tokens"),
        prompt_details.get("cache_read_input_tokens"),
        prompt_details.get("input_cached_tokens"),
    )
    orchestration_input_tokens = _number(
        usage.get("orchestration_input_tokens"),
        prompt_details.get("orchestration_input_tokens"),
    )
    orchestration_output_tokens = _number(
        usage.get("orchestration_output_tokens"),
        completion_details.get("orchestration_output_tokens"),
    )
    orchestration_input_cached_tokens = _number(
        usage.get("orchestration_input_cached_tokens"),
        prompt_details.get("orchestration_input_cached_tokens"),
        prompt_details.get("orchestration_cached_input_tokens"),
    )

    token_metadata = {
        "usage": usage,
        "prompt_tokens_details": prompt_details,
        "completion_tokens_details": completion_details,
    }
    orchestration_total = sum(
        v or 0
        for v in (
            orchestration_input_tokens,
            orchestration_output_tokens,
        )
    )
    visible_tokens = None
    if total_tokens is not None and orchestration_total:
        visible_tokens = max(total_tokens - orchestration_total, 0)

    noncached_input = max((input_tokens or 0) - (cached_input_tokens or 0), 0)
    noncached_orch_input = max(
        (orchestration_input_tokens or 0) - (orchestration_input_cached_tokens or 0),
        0,
    )
    # Input-token-equivalent units using the published Fugu Ultra <=272K rate card:
    # input=1, cached input=0.1, output=6. Standard Fugu is calibrated later, but
    # the same units are still useful for relative subscription load balancing.
    usage_units = (
        Decimal(noncached_input)
        + Decimal(cached_input_tokens or 0) * Decimal("0.1")
        + Decimal(output_tokens or 0) * Decimal("6")
        + Decimal(noncached_orch_input)
        + Decimal(orchestration_input_cached_tokens or 0) * Decimal("0.1")
        + Decimal(orchestration_output_tokens or 0) * Decimal("6")
    )

    estimated_cost_usd = usage_units * Decimal("0.000005")

    return {
        "raw_usage": usage,
        "raw_token_metadata": token_metadata,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_input_tokens": cached_input_tokens,
        "orchestration_input_tokens": orchestration_input_tokens,
        "orchestration_output_tokens": orchestration_output_tokens,
        "orchestration_input_cached_tokens": orchestration_input_cached_tokens,
        "orchestration_tokens_total": orchestration_total or None,
        "visible_tokens": visible_tokens,
        "usage_units": usage_units,
        "estimated_cost_usd": estimated_cost_usd,
        "prompt_tokens": _number(usage.get("prompt_tokens"), usage.get("input_tokens")),
        "completion_tokens": _number(usage.get("completion_tokens"), usage.get("output_tokens")),
        "total_tokens": total_tokens,
    }


def _conn():
    if psycopg is None:
        raise RuntimeError("psycopg is not installed")
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is empty")
    return psycopg.connect(
        DATABASE_URL,
        autocommit=True,
        row_factory=dict_row,
        connect_timeout=3,
        options="-c statement_timeout=5000 -c lock_timeout=3000",
    )


def init_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS fugu_accounts (
              account_id text PRIMARY KEY,
              display_name text NOT NULL,
              account_kind text NOT NULL DEFAULT 'subscription',
              billing_plan text,
              enabled boolean NOT NULL DEFAULT true,
              priority integer NOT NULL DEFAULT 100,
              notes text,
              created_at timestamptz NOT NULL DEFAULT now(),
              updated_at timestamptz NOT NULL DEFAULT now()
            );

            CREATE TABLE IF NOT EXISTS fugu_account_windows (
              account_id text NOT NULL REFERENCES fugu_accounts(account_id) ON DELETE CASCADE,
              window_kind text NOT NULL CHECK (window_kind IN ('5h', 'week', 'month')),
              reset_anchor_at timestamptz NOT NULL,
              reset_interval interval NOT NULL,
              allowance_usage_units numeric,
              allowance_input_tokens bigint,
              allowance_output_tokens bigint,
              allowance_cached_input_tokens bigint,
              allowance_credits numeric,
              source text NOT NULL DEFAULT 'default',
              updated_at timestamptz NOT NULL DEFAULT now(),
              PRIMARY KEY (account_id, window_kind)
            );

            CREATE TABLE IF NOT EXISTS fugu_model_deployments (
              requested_model text NOT NULL,
              account_id text NOT NULL REFERENCES fugu_accounts(account_id) ON DELETE CASCADE,
              upstream_model text NOT NULL UNIQUE,
              enabled boolean NOT NULL DEFAULT true,
              created_at timestamptz NOT NULL DEFAULT now(),
              PRIMARY KEY (requested_model, account_id)
            );

            CREATE TABLE IF NOT EXISTS fugu_account_cooldowns (
              account_id text NOT NULL REFERENCES fugu_accounts(account_id) ON DELETE CASCADE,
              requested_model text NOT NULL,
              cooldown_until timestamptz NOT NULL,
              reason text,
              last_error jsonb,
              updated_at timestamptz NOT NULL DEFAULT now(),
              PRIMARY KEY (account_id, requested_model)
            );

            CREATE TABLE IF NOT EXISTS fugu_session_assignments (
              session_key text NOT NULL,
              requested_model text NOT NULL,
              account_id text NOT NULL REFERENCES fugu_accounts(account_id),
              upstream_model text NOT NULL,
              chosen_by_window text,
              assigned_at timestamptz NOT NULL DEFAULT now(),
              last_used_at timestamptz NOT NULL DEFAULT now(),
              failover_count integer NOT NULL DEFAULT 0,
              state text NOT NULL DEFAULT 'active',
              reason text,
              PRIMARY KEY (session_key, requested_model)
            );

            CREATE TABLE IF NOT EXISTS fugu_routing_decisions (
              id bigserial PRIMARY KEY,
              decided_at timestamptz NOT NULL DEFAULT now(),
              session_key text,
              requested_model text NOT NULL,
              selected_account_id text NOT NULL,
              selected_upstream_model text NOT NULL,
              chosen_by_window text,
              reason text,
              candidates jsonb NOT NULL DEFAULT '[]'::jsonb
            );

            CREATE TABLE IF NOT EXISTS fugu_credit_ledger (
              request_id text PRIMARY KEY,
              created_at timestamptz NOT NULL DEFAULT now(),
              start_time timestamptz,
              end_time timestamptz,
              status text NOT NULL,
              http_status integer,
              error_message text,
              call_type text,
              requested_model text,
              upstream_model text,
              model text,
              model_group text,
              model_id text,
              custom_llm_provider text,
              account_id text REFERENCES fugu_accounts(account_id),
              session_id text,
              agent_id text,
              agent_role text,
              user_id text,
              input_tokens bigint,
              output_tokens bigint,
              cached_input_tokens bigint,
              orchestration_input_tokens bigint,
              orchestration_output_tokens bigint,
              orchestration_input_cached_tokens bigint,
              orchestration_tokens_total bigint,
              visible_tokens bigint,
              prompt_tokens bigint,
              completion_tokens bigint,
              total_tokens bigint,
              usage_units numeric,
              estimated_cost_usd numeric,
              litellm_spend numeric,
              raw_usage jsonb NOT NULL DEFAULT '{}'::jsonb,
              raw_token_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
              request_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
              response_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
              standard_logging_object jsonb NOT NULL DEFAULT '{}'::jsonb,
              proxy_server_request jsonb NOT NULL DEFAULT '{}'::jsonb
            );

            CREATE TABLE IF NOT EXISTS fugu_console_snapshots (
              id bigserial PRIMARY KEY,
              account_id text NOT NULL REFERENCES fugu_accounts(account_id),
              captured_at timestamptz NOT NULL DEFAULT now(),
              period_label text NOT NULL,
              window_start timestamptz,
              window_end timestamptz,
              input_tokens bigint,
              output_tokens bigint,
              cached_input_tokens bigint,
              orchestration_input_tokens bigint,
              orchestration_output_tokens bigint,
              orchestration_input_cached_tokens bigint,
              usage_units numeric,
              estimated_cost_usd numeric,
              remaining_credits numeric,
              total_credits numeric,
              source text NOT NULL DEFAULT 'manual',
              screenshot_path text,
              notes text,
              raw_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb
            );

            CREATE INDEX IF NOT EXISTS fugu_credit_ledger_account_time_idx
              ON fugu_credit_ledger (account_id, start_time DESC);
            CREATE INDEX IF NOT EXISTS fugu_credit_ledger_session_idx
              ON fugu_credit_ledger (session_id, requested_model, start_time DESC);
            CREATE INDEX IF NOT EXISTS fugu_credit_ledger_model_time_idx
              ON fugu_credit_ledger (requested_model, start_time DESC);
            CREATE INDEX IF NOT EXISTS fugu_routing_decisions_time_idx
              ON fugu_routing_decisions (decided_at DESC);
            """
        )


def _default_anchor(kind: str) -> datetime:
    now = _utcnow()
    if kind == "5h":
        hour = (now.hour // 5) * 5
        return now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if kind == "week":
        start = now - timedelta(days=now.weekday())
        return start.replace(hour=0, minute=0, second=0, microsecond=0)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _env_prefix(account_index: int) -> str:
    return f"FUGU_SUB_{account_index}_"


def _window_env_name(kind: str) -> str:
    return {"5h": "5H", "week": "WEEKLY", "month": "MONTHLY"}[kind]


def _env_time(name: str) -> datetime | None:
    return _parse_time(os.environ.get(name))


def _env_decimal(name: str) -> Decimal | None:
    return _decimal(os.environ.get(name))


def seed_accounts(conn) -> None:
    with conn.cursor() as cur:
        for index in range(1, ACCOUNT_COUNT + 1):
            enabled_flag = os.environ.get(f"FUGU_SUB_{index}_ENABLED", "").lower()
            has_key = bool(os.environ.get(f"SAKANA_SUB_{index}_KEY"))
            if not has_key and enabled_flag not in ("1", "true", "yes", "on"):
                continue
            account_id = f"sub{index}"
            prefix = _env_prefix(index)
            display_name = os.environ.get(prefix + "LABEL") or f"Subscription {index}"
            billing_plan = os.environ.get(prefix + "BILLING_PLAN") or None
            cur.execute(
                """
                INSERT INTO fugu_accounts (account_id, display_name, billing_plan, priority, updated_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (account_id) DO UPDATE SET
                  display_name = EXCLUDED.display_name,
                  billing_plan = COALESCE(EXCLUDED.billing_plan, fugu_accounts.billing_plan),
                  enabled = true,
                  priority = EXCLUDED.priority,
                  updated_at = now()
                """,
                (account_id, display_name, billing_plan, index),
            )
            for requested_model in MODEL_PUBLIC_NAMES:
                cur.execute(
                    """
                    INSERT INTO fugu_model_deployments (requested_model, account_id, upstream_model, enabled)
                    VALUES (%s, %s, %s, true)
                    ON CONFLICT (requested_model, account_id) DO UPDATE SET
                      upstream_model = EXCLUDED.upstream_model,
                      enabled = true
                    """,
                    (requested_model, account_id, upstream_model(requested_model, account_id)),
                )
            for kind, interval_text in WINDOWS.items():
                env_name = _window_env_name(kind)
                env_anchor = _env_time(prefix + env_name + "_RESET_ANCHOR")
                anchor = env_anchor or _default_anchor(kind)
                source = "env" if env_anchor else "default"
                allowance = _env_decimal(prefix + env_name + "_ALLOWANCE_USAGE_UNITS")
                cur.execute(
                    """
                    INSERT INTO fugu_account_windows
                      (account_id, window_kind, reset_anchor_at, reset_interval, allowance_usage_units, source, updated_at)
                    VALUES (%s, %s, %s, %s::interval, %s, %s, now())
                    ON CONFLICT (account_id, window_kind) DO UPDATE SET
                      reset_anchor_at = CASE
                        WHEN EXCLUDED.source = 'env' THEN EXCLUDED.reset_anchor_at
                        ELSE fugu_account_windows.reset_anchor_at
                      END,
                      allowance_usage_units = COALESCE(EXCLUDED.allowance_usage_units, fugu_account_windows.allowance_usage_units),
                      source = CASE
                        WHEN EXCLUDED.source = 'env' THEN 'env'
                        ELSE fugu_account_windows.source
                      END,
                      updated_at = now()
                    """,
                    (account_id, kind, anchor, interval_text, allowance, source),
                )


def ensure_db() -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    with _conn() as conn:
        init_schema(conn)
        seed_accounts(conn)
    _SCHEMA_READY = True


def _metadata(kwargs: dict[str, Any]) -> dict[str, Any]:
    litellm_params = _as_dict(kwargs.get("litellm_params"))
    return _as_dict(litellm_params.get("metadata"))


def _standard(kwargs: dict[str, Any]) -> dict[str, Any]:
    return _as_dict(kwargs.get("standard_logging_object"))


def _request_headers(kwargs: dict[str, Any]) -> dict[str, Any]:
    proxy_req = _as_dict(kwargs.get("proxy_server_request"))
    return _as_dict(proxy_req.get("headers"))


def _annotate_metadata(kwargs: dict[str, Any], response_obj: Any | None = None) -> dict[str, Any]:
    params = kwargs.get("litellm_params") or {}
    kwargs["litellm_params"] = params
    meta = params.get("metadata") or {}
    params["metadata"] = meta

    usage = extract_usage(response_obj) if response_obj is not None else {}
    if usage:
        meta["fugu_token_metadata"] = _jsonable(usage.get("raw_token_metadata"))

    std = _standard(kwargs)
    model = kwargs.get("model") or std.get("model") or std.get("model_group")
    account_id = (
        meta.get("fugu_account_id")
        or account_from_model(str(model))
        or account_from_model(str(std.get("model_group") or ""))
    )
    if account_id:
        meta["fugu_account_id"] = account_id
    if "fugu_requested_model" not in meta:
        meta["fugu_requested_model"] = requested_model_from_upstream(str(model))

    headers = _request_headers(kwargs)
    for header, key in (
        ("x-agent-id", "agent_id"),
        ("x-agent-role", "agent_role"),
        ("x-session-id", "session_id"),
    ):
        val = headers.get(header) or headers.get(header.replace("-", "_"))
        if val:
            meta[key] = val
    return meta


def _request_id(kwargs: dict[str, Any], response_obj: Any | None) -> str:
    std = _standard(kwargs)
    response = _as_dict(response_obj)
    for value in (
        std.get("request_id"),
        kwargs.get("litellm_call_id"),
        kwargs.get("call_id"),
        response.get("id"),
    ):
        if value:
            return str(value)
    return f"missing-{datetime.now(UTC).timestamp()}"


def write_ledger(
    kwargs: dict[str, Any],
    response_obj: Any | None,
    start_time: Any,
    end_time: Any,
    *,
    status: str,
    error: Any = None,
) -> None:
    ensure_db()
    meta = _annotate_metadata(kwargs, response_obj)
    std = _standard(kwargs)
    usage = extract_usage(response_obj)
    usage_values = {
        "input_tokens": None,
        "output_tokens": None,
        "cached_input_tokens": None,
        "orchestration_input_tokens": None,
        "orchestration_output_tokens": None,
        "orchestration_input_cached_tokens": None,
        "orchestration_tokens_total": None,
        "visible_tokens": None,
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
        "usage_units": None,
        "estimated_cost_usd": None,
    }
    usage_values.update({key: usage.get(key) for key in usage_values})
    request_id = _request_id(kwargs, response_obj)
    upstream = str(kwargs.get("model") or std.get("model_group") or meta.get("fugu_upstream_model") or "")
    requested_model = str(meta.get("fugu_requested_model") or requested_model_from_upstream(upstream) or "")
    account_id = str(meta.get("fugu_account_id") or account_from_model(upstream) or "") or None
    headers = _request_headers(kwargs)
    proxy_req = _jsonable(kwargs.get("proxy_server_request"))

    response = _as_dict(response_obj)
    response_metadata = _as_dict(response.get("metadata"))
    error_message = str(error or kwargs.get("exception") or "")[:2000] or None
    http_status = _number(std.get("status_code"), kwargs.get("status_code"))

    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO fugu_credit_ledger (
              request_id, start_time, end_time, status, http_status, error_message,
              call_type, requested_model, upstream_model, model, model_group, model_id,
              custom_llm_provider, account_id, session_id, agent_id, agent_role, user_id,
              input_tokens, output_tokens, cached_input_tokens,
              orchestration_input_tokens, orchestration_output_tokens,
              orchestration_input_cached_tokens, orchestration_tokens_total, visible_tokens,
              prompt_tokens, completion_tokens, total_tokens, usage_units,
              estimated_cost_usd, litellm_spend, raw_usage, raw_token_metadata,
              request_metadata, response_metadata, standard_logging_object, proxy_server_request
            )
            VALUES (
              %(request_id)s, %(start_time)s, %(end_time)s, %(status)s, %(http_status)s, %(error_message)s,
              %(call_type)s, %(requested_model)s, %(upstream_model)s, %(model)s, %(model_group)s, %(model_id)s,
              %(custom_llm_provider)s, %(account_id)s, %(session_id)s, %(agent_id)s, %(agent_role)s, %(user_id)s,
              %(input_tokens)s, %(output_tokens)s, %(cached_input_tokens)s,
              %(orchestration_input_tokens)s, %(orchestration_output_tokens)s,
              %(orchestration_input_cached_tokens)s, %(orchestration_tokens_total)s, %(visible_tokens)s,
              %(prompt_tokens)s, %(completion_tokens)s, %(total_tokens)s, %(usage_units)s,
              %(estimated_cost_usd)s, %(litellm_spend)s, %(raw_usage)s::jsonb, %(raw_token_metadata)s::jsonb,
              %(request_metadata)s::jsonb, %(response_metadata)s::jsonb,
              %(standard_logging_object)s::jsonb, %(proxy_server_request)s::jsonb
            )
            ON CONFLICT (request_id) DO UPDATE SET
              end_time = EXCLUDED.end_time,
              status = EXCLUDED.status,
              http_status = EXCLUDED.http_status,
              error_message = EXCLUDED.error_message,
              call_type = COALESCE(EXCLUDED.call_type, fugu_credit_ledger.call_type),
              requested_model = COALESCE(EXCLUDED.requested_model, fugu_credit_ledger.requested_model),
              upstream_model = COALESCE(EXCLUDED.upstream_model, fugu_credit_ledger.upstream_model),
              model = COALESCE(EXCLUDED.model, fugu_credit_ledger.model),
              model_group = COALESCE(EXCLUDED.model_group, fugu_credit_ledger.model_group),
              model_id = COALESCE(EXCLUDED.model_id, fugu_credit_ledger.model_id),
              custom_llm_provider = COALESCE(EXCLUDED.custom_llm_provider, fugu_credit_ledger.custom_llm_provider),
              account_id = COALESCE(EXCLUDED.account_id, fugu_credit_ledger.account_id),
              session_id = COALESCE(EXCLUDED.session_id, fugu_credit_ledger.session_id),
              agent_id = COALESCE(EXCLUDED.agent_id, fugu_credit_ledger.agent_id),
              agent_role = COALESCE(EXCLUDED.agent_role, fugu_credit_ledger.agent_role),
              user_id = COALESCE(EXCLUDED.user_id, fugu_credit_ledger.user_id),
              input_tokens = COALESCE(EXCLUDED.input_tokens, fugu_credit_ledger.input_tokens),
              output_tokens = COALESCE(EXCLUDED.output_tokens, fugu_credit_ledger.output_tokens),
              cached_input_tokens = COALESCE(EXCLUDED.cached_input_tokens, fugu_credit_ledger.cached_input_tokens),
              orchestration_input_tokens = COALESCE(EXCLUDED.orchestration_input_tokens, fugu_credit_ledger.orchestration_input_tokens),
              orchestration_output_tokens = COALESCE(EXCLUDED.orchestration_output_tokens, fugu_credit_ledger.orchestration_output_tokens),
              orchestration_input_cached_tokens = COALESCE(EXCLUDED.orchestration_input_cached_tokens, fugu_credit_ledger.orchestration_input_cached_tokens),
              orchestration_tokens_total = COALESCE(EXCLUDED.orchestration_tokens_total, fugu_credit_ledger.orchestration_tokens_total),
              visible_tokens = COALESCE(EXCLUDED.visible_tokens, fugu_credit_ledger.visible_tokens),
              prompt_tokens = COALESCE(EXCLUDED.prompt_tokens, fugu_credit_ledger.prompt_tokens),
              completion_tokens = COALESCE(EXCLUDED.completion_tokens, fugu_credit_ledger.completion_tokens),
              total_tokens = COALESCE(EXCLUDED.total_tokens, fugu_credit_ledger.total_tokens),
              usage_units = COALESCE(EXCLUDED.usage_units, fugu_credit_ledger.usage_units),
              estimated_cost_usd = COALESCE(EXCLUDED.estimated_cost_usd, fugu_credit_ledger.estimated_cost_usd),
              litellm_spend = COALESCE(EXCLUDED.litellm_spend, fugu_credit_ledger.litellm_spend),
              raw_usage = EXCLUDED.raw_usage,
              raw_token_metadata = EXCLUDED.raw_token_metadata,
              request_metadata = EXCLUDED.request_metadata,
              response_metadata = EXCLUDED.response_metadata,
              standard_logging_object = EXCLUDED.standard_logging_object,
              proxy_server_request = EXCLUDED.proxy_server_request
            """,
            {
                "request_id": request_id,
                "start_time": _parse_time(start_time),
                "end_time": _parse_time(end_time),
                "status": status,
                "http_status": http_status,
                "error_message": error_message,
                "call_type": std.get("call_type") or kwargs.get("call_type"),
                "requested_model": requested_model,
                "upstream_model": upstream,
                "model": std.get("model") or upstream,
                "model_group": std.get("model_group"),
                "model_id": std.get("model_id"),
                "custom_llm_provider": std.get("custom_llm_provider"),
                "account_id": account_id,
                "session_id": meta.get("session_id") or meta.get("fugu_session_id") or headers.get("x-session-id"),
                "agent_id": meta.get("agent_id"),
                "agent_role": meta.get("agent_role"),
                "user_id": std.get("user") or kwargs.get("user"),
                "litellm_spend": _decimal(std.get("response_cost") or std.get("spend") or kwargs.get("response_cost")),
                "request_metadata": json.dumps(_jsonable(meta)),
                "response_metadata": json.dumps(_jsonable(response_metadata)),
                "standard_logging_object": json.dumps(_jsonable(std)),
                "proxy_server_request": json.dumps(proxy_req),
                **usage_values,
                "raw_usage": json.dumps(_jsonable(usage.get("raw_usage") or {})),
                "raw_token_metadata": json.dumps(_jsonable(usage.get("raw_token_metadata") or {})),
            },
        )


class FuguCreditTelemetry(CustomLogger):
    def log_success_event(self, kwargs, response_obj, start_time, end_time):
        try:
            write_ledger(kwargs, response_obj, start_time, end_time, status="success")
        except Exception as exc:
            print(f"fugu_credit: swallow success {type(exc).__name__}: {exc}", flush=True)

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        self.log_success_event(kwargs, response_obj, start_time, end_time)

    def log_failure_event(self, kwargs, response_obj, start_time, end_time):
        try:
            write_ledger(
                kwargs,
                response_obj,
                start_time,
                end_time,
                status="failure",
                error=kwargs.get("exception"),
            )
        except Exception as exc:
            print(f"fugu_credit: swallow failure {type(exc).__name__}: {exc}", flush=True)

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        self.log_failure_event(kwargs, response_obj, start_time, end_time)


fugu_credit_handler = FuguCreditTelemetry()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["init-db"])
    args = parser.parse_args()
    if args.command == "init-db":
        ensure_db()
        print("fugu_credit: schema ready", flush=True)


if __name__ == "__main__":
    main()
