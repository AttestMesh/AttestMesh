#!/usr/bin/env python3
"""Fail-closed OpenAI-compatible budget proxy for production memory workloads.

The provider key has a total dollar ceiling but no calendar-month scopes.  This
proxy supplies the missing local controls for:

* Qwen embeddings: a UTC calendar-month budget;
* Hindsight steady state: a UTC calendar-month budget;
* Hindsight rebuilds: a one-time backfill budget; and
* all traffic combined: a safety margin below the provider key's total limit.

Request bodies and credentials are never logged.  An ambiguous in-flight call
found after restart opens the circuit and requires an explicit operator reset.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx


HINDSIGHT_MODEL = os.getenv("HINDSIGHT_MODEL", "openai/gpt-oss-120b").strip()
QWEN_MODEL = os.getenv("QWEN_MODEL", "qwen/qwen3-embedding-8b").strip()
MAX_BODY_BYTES = 4_000_000
MAX_COMPLETION_TOKENS = 8192


class GuardViolation(RuntimeError):
    """A request would cross a cost or payload safety guard."""


class BudgetViolation(GuardViolation):
    """A request would cross a monetary guard or the circuit is already open."""


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def _positive_float(name: str, default: str) -> float:
    try:
        value = float(os.getenv(name, default))
    except ValueError as exc:
        raise SystemExit(f"{name} must be numeric") from exc
    if value <= 0:
        raise SystemExit(f"{name} must be positive")
    return value


def _nonnegative_float(name: str, default: str) -> float:
    try:
        value = float(os.getenv(name, default))
    except ValueError as exc:
        raise SystemExit(f"{name} must be numeric") from exc
    if value < 0:
        raise SystemExit(f"{name} must not be negative")
    return value


def _positive_int(name: str, default: str) -> int:
    try:
        value = int(os.getenv(name, default))
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer") from exc
    if value <= 0:
        raise SystemExit(f"{name} must be positive")
    return value


@dataclass(frozen=True)
class Settings:
    provider_base_url: str
    provider_api_key: str
    proxy_api_key: str
    provider_egress_enabled: bool
    provider_total_limit_usd: float
    provider_safety_margin_usd: float
    hindsight_phase: str
    hindsight_backfill_limit_usd: float
    hindsight_monthly_limit_usd: float
    qwen_monthly_limit_usd: float
    rpm_limit: int
    tpm_limit: int
    prices: dict[str, tuple[float, float]]
    initial_provider_spend_usd: float

    @classmethod
    def from_env(cls) -> "Settings":
        provider_api_key = os.getenv("PROVIDER_API_KEY", "").strip()
        proxy_api_key = os.getenv("PROXY_API_KEY", "").strip()
        if not provider_api_key:
            raise SystemExit("PROVIDER_API_KEY is required")
        if not proxy_api_key:
            raise SystemExit("PROXY_API_KEY is required")
        if hmac.compare_digest(provider_api_key, proxy_api_key):
            raise SystemExit("PROXY_API_KEY must differ from PROVIDER_API_KEY")
        phase = os.getenv("HINDSIGHT_BUDGET_PHASE", "backfill").strip().lower()
        if phase not in {"backfill", "monthly"}:
            raise SystemExit("HINDSIGHT_BUDGET_PHASE must be backfill or monthly")
        total = _positive_float("PROVIDER_TOTAL_LIMIT_USD", "50")
        margin = _nonnegative_float("PROVIDER_SAFETY_MARGIN_USD", "0.50")
        if margin >= total:
            raise SystemExit("PROVIDER_SAFETY_MARGIN_USD must be below the total limit")
        settings = cls(
            provider_base_url=os.getenv(
                "PROVIDER_BASE_URL", "https://api.redpill.ai/v1"
            ).rstrip("/"),
            provider_api_key=provider_api_key,
            proxy_api_key=proxy_api_key,
            provider_egress_enabled=os.getenv("PROVIDER_EGRESS_ENABLED", "false")
            .strip()
            .lower()
            in {"1", "true", "yes", "on"},
            provider_total_limit_usd=total,
            provider_safety_margin_usd=margin,
            hindsight_phase=phase,
            hindsight_backfill_limit_usd=_positive_float(
                "HINDSIGHT_BACKFILL_LIMIT_USD", "30"
            ),
            hindsight_monthly_limit_usd=_positive_float(
                "HINDSIGHT_MONTHLY_LIMIT_USD", "10"
            ),
            qwen_monthly_limit_usd=_positive_float("QWEN_MONTHLY_LIMIT_USD", "5"),
            rpm_limit=_positive_int("PROXY_RPM_LIMIT", "30"),
            tpm_limit=_positive_int("PROXY_TPM_LIMIT", "1000000"),
            prices={
                HINDSIGHT_MODEL: (
                    _positive_float(
                        "HINDSIGHT_INPUT_USD_PER_M",
                        os.getenv("GPT_OSS_120B_INPUT_USD_PER_M", "0.15"),
                    ),
                    _positive_float(
                        "HINDSIGHT_OUTPUT_USD_PER_M",
                        os.getenv("GPT_OSS_120B_OUTPUT_USD_PER_M", "0.60"),
                    ),
                ),
                QWEN_MODEL: (
                    _positive_float("QWEN_INPUT_USD_PER_M", "0.01"),
                    _nonnegative_float("QWEN_OUTPUT_USD_PER_M", "0"),
                ),
            },
            initial_provider_spend_usd=_nonnegative_float(
                "INITIAL_PROVIDER_SPEND_USD", "0"
            ),
        )
        if settings.hindsight_backfill_limit_usd > settings.effective_provider_limit:
            raise SystemExit("backfill limit exceeds the effective provider limit")
        return settings

    @property
    def effective_provider_limit(self) -> float:
        return self.provider_total_limit_usd - self.provider_safety_margin_usd

    def guard_fingerprint(self) -> dict[str, Any]:
        return {
            "provider_total_limit_usd": self.provider_total_limit_usd,
            "provider_safety_margin_usd": self.provider_safety_margin_usd,
            "hindsight_backfill_limit_usd": self.hindsight_backfill_limit_usd,
            "hindsight_monthly_limit_usd": self.hindsight_monthly_limit_usd,
            "qwen_monthly_limit_usd": self.qwen_monthly_limit_usd,
            "prices": {
                model: {"input_usd_per_m": prices[0], "output_usd_per_m": prices[1]}
                for model, prices in sorted(self.prices.items())
            },
        }


def utc_month(moment: datetime | None = None) -> str:
    return (
        (moment or datetime.now(timezone.utc))
        .astimezone(timezone.utc)
        .strftime("%Y-%m")
    )


def _token_cost(
    prices: tuple[float, float], input_tokens: int, output_tokens: int
) -> float:
    return (input_tokens * prices[0] + output_tokens * prices[1]) / 1_000_000


def prepare_chat_payload(value: Any) -> tuple[dict[str, Any], int, int]:
    if not isinstance(value, dict):
        raise GuardViolation("request body must be a JSON object")
    payload = dict(value)
    if payload.get("model") != HINDSIGHT_MODEL:
        raise GuardViolation("chat model is outside the production allowlist")
    if payload.get("stream"):
        raise GuardViolation("streaming is disabled so usage can be metered")
    maximum = payload.get("max_completion_tokens", payload.get("max_tokens", 8192))
    try:
        max_tokens = int(maximum)
    except (TypeError, ValueError) as exc:
        raise GuardViolation("completion-token ceiling must be an integer") from exc
    if not 0 < max_tokens <= MAX_COMPLETION_TOKENS:
        raise GuardViolation("completion-token ceiling must be between 1 and 8192")
    if payload.get("reasoning_effort", "low") != "low":
        raise GuardViolation("reasoning_effort must be low")
    if payload.get("include_reasoning") not in (None, False):
        raise GuardViolation("include_reasoning must be false")
    response_format = payload.get("response_format")
    if isinstance(response_format, dict):
        schema = response_format.get("json_schema")
        if isinstance(schema, dict) and schema.get("strict") is True:
            raise GuardViolation("strict structured output is disabled")
    payload["reasoning_effort"] = "low"
    payload["include_reasoning"] = False
    payload["max_completion_tokens"] = max_tokens
    payload.pop("max_tokens", None)
    prompt_bytes = len(
        json.dumps(payload.get("messages") or [], ensure_ascii=False).encode("utf-8")
    )
    return payload, max(1, prompt_bytes), max_tokens


def prepare_embedding_payload(value: Any) -> tuple[dict[str, Any], int, int]:
    if not isinstance(value, dict):
        raise GuardViolation("request body must be a JSON object")
    payload = dict(value)
    if payload.get("model") != QWEN_MODEL:
        raise GuardViolation("embedding model is outside the production allowlist")
    inputs = payload.get("input")
    if isinstance(inputs, str):
        materialized = [inputs]
    elif (
        isinstance(inputs, list)
        and inputs
        and all(isinstance(item, str) for item in inputs)
    ):
        materialized = inputs
    else:
        raise GuardViolation(
            "embedding input must be a non-empty string or string list"
        )
    if len(materialized) > 64:
        raise GuardViolation("embedding batch exceeds 64 texts")
    dimensions = payload.get("dimensions")
    if dimensions is not None:
        try:
            dimensions = int(dimensions)
        except (TypeError, ValueError) as exc:
            raise GuardViolation("embedding dimensions must be an integer") from exc
        if not 1 <= dimensions <= 4096:
            raise GuardViolation("embedding dimensions are outside 1..4096")
        payload["dimensions"] = dimensions
    prompt_bytes = len(json.dumps(materialized, ensure_ascii=False).encode("utf-8"))
    return payload, max(1, prompt_bytes), 0


class BudgetState:
    def __init__(self, ledger_path: Path, state_path: Path, settings: Settings) -> None:
        self.ledger_path = ledger_path
        self.state_path = state_path
        self.settings = settings
        self.lock = threading.Lock()
        if ledger_path.exists():
            self.ledger = json.loads(ledger_path.read_text())
        else:
            self.ledger = {
                "version": 1,
                "guard": settings.guard_fingerprint(),
                "initial_provider_spend_usd": settings.initial_provider_spend_usd,
                "provider_spent_usd": settings.initial_provider_spend_usd,
                "calls": [],
            }
            atomic_json(ledger_path, self.ledger)
        if self.ledger.get("guard") != settings.guard_fingerprint():
            raise SystemExit(
                "persisted ledger guard does not match configured limits/prices"
            )
        if (
            abs(
                float(self.ledger.get("initial_provider_spend_usd", 0))
                - settings.initial_provider_spend_usd
            )
            > 1e-12
        ):
            raise SystemExit(
                "persisted initial provider spend does not match configuration"
            )
        if (
            float(self.ledger.get("provider_spent_usd", 0))
            > settings.effective_provider_limit
        ):
            raise SystemExit(
                "persisted provider spend already exceeds the effective limit"
            )

        if state_path.exists():
            self.value = json.loads(state_path.read_text())
        else:
            self.value = {"circuit_open": False, "in_flight": {}}
            atomic_json(state_path, self.value)
        self.value.setdefault("in_flight", {})
        if self.value["in_flight"]:
            self.value.update(
                {
                    "circuit_open": True,
                    "reason": "ambiguous in-flight requests found after proxy restart",
                }
            )
            atomic_json(state_path, self.value)

    def save_state(self) -> None:
        atomic_json(self.state_path, self.value)

    def _scope_for(self, endpoint: str) -> tuple[str, str, float]:
        if endpoint == "embeddings":
            return "qwen_monthly", utc_month(), self.settings.qwen_monthly_limit_usd
        if self.settings.hindsight_phase == "backfill":
            return (
                "hindsight_backfill",
                "all",
                self.settings.hindsight_backfill_limit_usd,
            )
        return (
            "hindsight_monthly",
            utc_month(),
            self.settings.hindsight_monthly_limit_usd,
        )

    def _scope_spend(self, scope: str, period: str) -> float:
        return sum(
            float(call.get("cost_usd", 0))
            for call in self.ledger.get("calls") or []
            if call.get("scope") == scope and call.get("period") == period
        )

    def _pending_cost(
        self, *, scope: str | None = None, period: str | None = None
    ) -> float:
        values = self.value.get("in_flight") or {}
        return sum(
            float(item.get("reservation", {}).get("cost_usd", 0))
            for item in values.values()
            if (scope is None or item.get("scope") == scope)
            and (period is None or item.get("period") == period)
        )

    def _rate_delay(self, reservation_tokens: int, now: datetime) -> float:
        recent: list[tuple[float, int]] = []
        for call in self.ledger.get("calls") or []:
            raw = call.get("started_at") or call.get("at")
            try:
                timestamp = datetime.fromisoformat(str(raw)).timestamp()
            except (TypeError, ValueError):
                continue
            age = now.timestamp() - timestamp
            if 0 <= age < 60:
                recent.append(
                    (
                        timestamp,
                        int(call.get("input_tokens", 0))
                        + int(call.get("output_tokens", 0)),
                    )
                )
        for item in (self.value.get("in_flight") or {}).values():
            try:
                timestamp = datetime.fromisoformat(str(item["started_at"])).timestamp()
            except (KeyError, TypeError, ValueError):
                continue
            age = now.timestamp() - timestamp
            if 0 <= age < 60:
                recent.append(
                    (timestamp, int(item.get("reservation", {}).get("total_tokens", 0)))
                )
        if reservation_tokens > self.settings.tpm_limit:
            raise GuardViolation("single request exceeds the configured TPM limit")
        rpm_blocked = len(recent) >= self.settings.rpm_limit
        tpm_blocked = (
            sum(tokens for _, tokens in recent) + reservation_tokens
            > self.settings.tpm_limit
        )
        if not rpm_blocked and not tpm_blocked:
            return 0.0
        if not recent:
            return 1.0
        return max(
            0.05, 60.0 - (now.timestamp() - min(timestamp for timestamp, _ in recent))
        )

    def reserve(
        self,
        endpoint: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> tuple[str, dict[str, Any]]:
        prices = self.settings.prices[model]
        cost = _token_cost(prices, input_tokens, output_tokens)
        scope, period, scope_limit = self._scope_for(endpoint)
        while True:
            with self.lock:
                if self.value.get("circuit_open"):
                    raise BudgetViolation(
                        str(self.value.get("reason") or "circuit open")
                    )
                total_reserved = self._pending_cost()
                provider_spend = float(self.ledger.get("provider_spent_usd", 0))
                if (
                    provider_spend + total_reserved + cost
                    > self.settings.effective_provider_limit
                ):
                    raise BudgetViolation(
                        "provider-total local safety ceiling would be exceeded"
                    )
                if (
                    self._scope_spend(scope, period)
                    + self._pending_cost(scope=scope, period=period)
                    + cost
                    > scope_limit
                ):
                    raise BudgetViolation(f"{scope} local budget would be exceeded")
                now = datetime.now(timezone.utc)
                delay = self._rate_delay(input_tokens + output_tokens, now)
                if delay <= 0:
                    request_id = str(uuid.uuid4())
                    reservation = {
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "total_tokens": input_tokens + output_tokens,
                        "cost_usd": cost,
                    }
                    self.value["in_flight"][request_id] = {
                        "model": model,
                        "endpoint": endpoint,
                        "scope": scope,
                        "period": period,
                        "started_at": now.isoformat(),
                        "reservation": reservation,
                    }
                    self.save_state()
                    return request_id, self.value["in_flight"][request_id]
            time.sleep(min(delay + 0.05, 1.0))

    def open_circuit(
        self, reason: str, *, keep_request: bool = True, request_id: str | None = None
    ) -> None:
        with self.lock:
            if not keep_request and request_id:
                self.value.get("in_flight", {}).pop(request_id, None)
            self.value.update({"circuit_open": True, "reason": reason})
            self.save_state()

    def discard(self, request_id: str) -> None:
        with self.lock:
            self.value.get("in_flight", {}).pop(request_id, None)
            self.save_state()

    def record(self, request_id: str, usage: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            item = self.value.get("in_flight", {}).get(request_id)
            if not item:
                raise GuardViolation(
                    "in-flight reservation disappeared before recording"
                )
            reservation = item["reservation"]
            reported_input = usage.get("prompt_tokens", usage.get("input_tokens"))
            reported_output = usage.get("completion_tokens", usage.get("output_tokens"))
            reported_total = usage.get("total_tokens")
            input_tokens = int(
                reservation["input_tokens"]
                if reported_input is None
                else reported_input
            )
            output_tokens = int(
                reservation["output_tokens"]
                if reported_output is None
                else reported_output
            )
            if reported_total is not None:
                output_tokens = max(
                    output_tokens,
                    max(0, int(reported_total) - input_tokens),
                )
            calculated = _token_cost(
                self.settings.prices[item["model"]], input_tokens, output_tokens
            )
            try:
                provider_reported = float(usage.get("cost"))
            except (TypeError, ValueError):
                provider_reported = 0.0
            cost = max(calculated, provider_reported)
            call = {
                "at": datetime.now(timezone.utc).isoformat(),
                "started_at": item["started_at"],
                "request_id": request_id,
                "endpoint": item["endpoint"],
                "model": item["model"],
                "scope": item["scope"],
                "period": item["period"],
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": cost,
                "provider_reported_cost_usd": provider_reported or None,
                "estimated_from_reservation": reported_input is None
                or reported_output is None,
            }
            self.ledger.setdefault("calls", []).append(call)
            self.ledger["provider_spent_usd"] = (
                float(self.ledger.get("provider_spent_usd", 0)) + cost
            )
            self.value["in_flight"].pop(request_id, None)
            atomic_json(self.ledger_path, self.ledger)
            self.save_state()
            return call

    def health(self) -> dict[str, Any]:
        month = utc_month()
        with self.lock:
            if self.value.get("circuit_open"):
                status = "circuit_open"
            elif not self.settings.provider_egress_enabled:
                status = "maintenance"
            else:
                status = "healthy"
            return {
                "status": status,
                "circuit_open": bool(self.value.get("circuit_open")),
                "circuit_reason": self.value.get("reason"),
                "in_flight": len(self.value.get("in_flight") or {}),
                "provider_spent_usd": float(self.ledger.get("provider_spent_usd", 0)),
                "provider_effective_limit_usd": self.settings.effective_provider_limit,
                "hindsight_phase": self.settings.hindsight_phase,
                "provider_egress_enabled": self.settings.provider_egress_enabled,
                "hindsight_backfill_spent_usd": self._scope_spend(
                    "hindsight_backfill", "all"
                ),
                "hindsight_monthly_spent_usd": self._scope_spend(
                    "hindsight_monthly", month
                ),
                "qwen_monthly_spent_usd": self._scope_spend("qwen_monthly", month),
                "month_utc": month,
                "hindsight_model": HINDSIGHT_MODEL,
            }


class BudgetProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(
        self, address: tuple[str, int], state: BudgetState, settings: Settings
    ) -> None:
        super().__init__(address, ProxyHandler)
        self.state = state
        self.settings = settings


class ProxyHandler(BaseHTTPRequestHandler):
    server: BudgetProxyServer

    def log_message(self, format: str, *args: Any) -> None:
        _ = format, args

    def send_json(self, status: int, value: dict[str, Any]) -> None:
        body = json.dumps(value, sort_keys=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def authorized(self) -> bool:
        expected = "Bearer " + self.server.settings.proxy_api_key
        return hmac.compare_digest(expected, self.headers.get("Authorization", ""))

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") == "/health":
            self.send_json(200, self.server.state.health())
            return
        if self.path.rstrip("/") == "/upstream-health":
            if not self.authorized():
                self.send_json(401, {"error": {"message": "invalid proxy key"}})
                return
            if not self.server.settings.provider_egress_enabled:
                self.send_json(503, {"status": "maintenance"})
                return
            base = self.server.settings.provider_base_url.rstrip("/")
            root = base[:-3] if base.endswith("/v1") else base
            try:
                response = httpx.get(
                    root + "/health/process",
                    headers={
                        "Authorization": "Bearer "
                        + self.server.settings.provider_api_key
                    },
                    timeout=15,
                )
                self.send_json(
                    200 if response.status_code < 400 else 502,
                    {
                        "status": "healthy" if response.status_code < 400 else "unhealthy",
                        "upstream_status": response.status_code,
                    },
                )
            except Exception as exc:
                self.send_json(
                    502,
                    {
                        "status": "unreachable",
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:500],
                    },
                )
            return
        if self.path.rstrip("/") == "/v1/models":
            if not self.authorized():
                self.send_json(401, {"error": {"message": "invalid proxy key"}})
                return
            self.send_json(
                200,
                {
                    "object": "list",
                    "data": [
                        {"id": HINDSIGHT_MODEL, "object": "model"},
                        {"id": QWEN_MODEL, "object": "model"},
                    ],
                },
            )
            return
        self.send_json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.rstrip("/")
        if path == "/v1/chat/completions":
            endpoint = "chat"
            prepare = prepare_chat_payload
        elif path == "/v1/embeddings":
            endpoint = "embeddings"
            prepare = prepare_embedding_payload
        else:
            self.send_json(404, {"error": {"message": "not found"}})
            return
        if not self.authorized():
            self.send_json(401, {"error": {"message": "invalid proxy key"}})
            return
        if not self.server.settings.provider_egress_enabled:
            self.send_json(503, {"error": {"message": "provider egress disabled"}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY_BYTES:
                raise GuardViolation("request body length is outside the proxy limit")
            payload, input_tokens, output_tokens = prepare(
                json.loads(self.rfile.read(length))
            )
            model = str(payload["model"])
            request_id, reservation = self.server.state.reserve(
                endpoint, model, input_tokens, output_tokens
            )
        except BudgetViolation as exc:
            self.server.state.open_circuit(str(exc), keep_request=False)
            self.send_json(402, {"error": {"message": str(exc)}})
            return
        except (GuardViolation, ValueError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": {"message": str(exc)}})
            return

        try:
            response = httpx.post(
                self.server.settings.provider_base_url
                + ("/chat/completions" if endpoint == "chat" else "/embeddings"),
                headers={
                    "Authorization": "Bearer " + self.server.settings.provider_api_key
                },
                json=payload,
                timeout=300,
            )
            if response.status_code in {401, 402}:
                self.server.state.open_circuit(
                    f"provider returned {response.status_code}",
                    keep_request=False,
                    request_id=request_id,
                )
            elif response.status_code < 400:
                body = response.json()
                call = self.server.state.record(request_id, body.get("usage") or {})
                print(
                    json.dumps(
                        {
                            "event": "provider_call_recorded",
                            "request_id": request_id,
                            "endpoint": endpoint,
                            "model": model,
                            "cost_usd": call["cost_usd"],
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
            else:
                self.server.state.discard(request_id)
        except Exception as exc:
            self.server.state.open_circuit(
                f"ambiguous upstream failure: {type(exc).__name__}",
                keep_request=True,
                request_id=request_id,
            )
            self.send_json(502, {"error": {"message": "ambiguous upstream failure"}})
            return

        self.send_response(response.status_code)
        self.send_header(
            "Content-Type", response.headers.get("content-type", "application/json")
        )
        self.send_header("Content-Length", str(len(response.content)))
        self.end_headers()
        try:
            self.wfile.write(response.content)
        except BrokenPipeError:
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=28080)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    settings = Settings.from_env()
    state = BudgetState(args.ledger, args.state, settings)
    server = BudgetProxyServer((args.host, args.port), state, settings)
    server.serve_forever(poll_interval=0.5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
