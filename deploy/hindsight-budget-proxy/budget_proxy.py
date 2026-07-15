#!/usr/bin/env python3
"""OpenAI-compatible accounting proxy for production memory workloads.

The provider key has a total dollar ceiling but no calendar-month scopes.  This
proxy supplies the missing local controls for:

* Qwen embeddings: a UTC calendar-month budget;
* Hindsight steady state: a UTC calendar-month budget;
* Hindsight rebuilds: a one-time backfill budget; and
* all traffic combined: a safety margin below the provider key's total limit.

Request bodies and credentials are never logged.  The optional observation
mode preserves exact accounting, pricing, rate and concurrency controls while
treating monetary ceilings as telemetry.  In that mode an ambiguous call is
charged at its unchanged reservation maximum and only proven upstream credit
exhaustion opens the circuit.
"""

from __future__ import annotations

import argparse
import copy
import email.utils
import hashlib
import hmac
import json
import math
import os
import re
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
MAX_RETRY_AFTER_SECONDS = 300
CREDIT_EXHAUSTED_CIRCUIT_REASON = "upstream Redpill credit exhausted"
FINAL_MAINTENANCE_HOLD_REASON = "hindsight backfill complete: maintenance hold"
FUGU_HINDSIGHT_AGENT_ID = "hindsight-backfill"
FUGU_QWEN_AGENT_ID = "hindsight-qwen-budget"
RECOVERY_PROVENANCE_RE = re.compile(
    r"hindsight-recovery-[0-9a-f]{24}-(?:gpt_oss|qwen)"
)
RECOVERY_PROBE_NAMESPACE = uuid.UUID("bf0f7e3e-92c5-4c41-aada-9b7abbe20df8")
RECOVERY_PROBE_HARD_BUDGET_USD = 30.0
RECOVERY_PROBE_SUCCESS_HOLD_REASON = (
    "recovery probe authenticated: controller reset required"
)
LEDGER_VERSION = 2
LEGACY_LEDGER_VERSION = 1
# The two previously deployed, digest-pinned writers emitted ordinary provider
# successes with exactly this schema.  Keep the allowlist exact: accepting a
# superset here would let a malformed current record masquerade as history.
LEGACY_PROVIDER_CALL_KEYS = frozenset(
    {
        "at",
        "started_at",
        "request_id",
        "endpoint",
        "model",
        "scope",
        "period",
        "input_tokens",
        "output_tokens",
        "cost_usd",
        "provider_reported_cost_usd",
        "estimated_from_reservation",
    }
)


class GuardViolation(RuntimeError):
    """A request would cross a cost or payload safety guard."""


class BudgetViolation(GuardViolation):
    """A request would cross a monetary guard or the circuit is already open."""


class ProviderCapacityViolation(BudgetViolation):
    """The bounded provider concurrency is temporarily saturated."""


class RecoveryProbeAmbiguity(BudgetViolation):
    """A deterministic recovery probe already has unresolved upstream state."""


def provider_credit_exhausted(status_code: int, response_body: bytes | str) -> bool:
    """Recognize only direct Redpill credit exhaustion, without retaining text."""
    # The continuous backfill treats every HTTP 429 as retry pressure, even if
    # an upstream overload response happens to contain credit-like wording.
    # Only a non-429 direct response may become the durable credit stop.
    if status_code == 429:
        return False
    if status_code == 402:
        return True
    if isinstance(response_body, bytes):
        text = response_body[:64_000].decode("utf-8", errors="replace")
    else:
        text = str(response_body)[:64_000]
    normalized = " ".join(text.casefold().split())
    patterns = (
        r"\bout of credits?\b",
        r"\binsufficient credits?\b",
        r"\binsufficient (?:credit )?balance\b",
        r"\binsufficient funds?\b",
        r"\bcredits? exhausted\b",
        r"\bpayment required\b",
    )
    return any(re.search(pattern, normalized) is not None for pattern in patterns)


def is_exact_legacy_provider_call(call: Any) -> bool:
    """Recognize one immutable v1 normal-call row without rewriting it."""
    return (
        isinstance(call, dict)
        and frozenset(call) == LEGACY_PROVIDER_CALL_KEYS
        and type(call.get("estimated_from_reservation")) is bool
    )


def _price_tuple(prices: Any, model: str) -> tuple[float, float]:
    try:
        value = prices[model]
        if isinstance(value, dict):
            raw_input = value["input_usd_per_m"]
            raw_output = value["output_usd_per_m"]
        else:
            raw_input, raw_output = value
        input_price = float(raw_input)
        output_price = float(raw_output)
    except (KeyError, TypeError, ValueError) as exc:
        raise GuardViolation(f"price guard is malformed for {model}") from exc
    if (
        not math.isfinite(input_price)
        or input_price <= 0
        or not math.isfinite(output_price)
        or output_price < 0
        or (model == HINDSIGHT_MODEL and output_price <= 0)
    ):
        raise GuardViolation(f"price guard is invalid for {model}")
    return input_price, output_price


def ordered_float_total(costs: list[float]) -> float:
    """Use one runtime-independent binary64 left fold for money manifests.

    CPython 3.12+ changed ``sum()`` for floats.  The recovery controller and
    proxy can run different pinned Python minors, so the wire aggregate must
    not depend on either runtime's built-in summation implementation.
    """
    total = 0.0
    for cost in costs:
        total = float(total + float(cost))
    return total


def reservation_manifest(
    in_flight: dict[str, Any], prices: Any
) -> dict[str, Any]:
    """Return the canonical, payload-free maximum-cost reservation manifest."""
    reservations: list[dict[str, Any]] = []
    for request_id, item in sorted(in_flight.items()):
        reservation = dict(item.get("reservation") or {})
        request_id = str(request_id)
        try:
            uuid.UUID(request_id)
        except (ValueError, AttributeError) as exc:
            raise GuardViolation("reservation request ID is not an exact UUID") from exc
        model = str(item.get("model") or "")
        endpoint = str(item.get("endpoint") or "")
        scope = str(item.get("scope") or "")
        period = str(item.get("period") or "")
        started_at = str(item.get("started_at") or "")
        request_provenance_sha256 = item.get("request_provenance_sha256")
        if request_provenance_sha256 is not None and re.fullmatch(
            r"[0-9a-f]{64}", str(request_provenance_sha256)
        ) is None:
            raise GuardViolation("reservation request provenance is malformed")
        try:
            parsed_started_at = datetime.fromisoformat(started_at)
        except ValueError as exc:
            raise GuardViolation("reservation start timestamp is invalid") from exc
        if parsed_started_at.tzinfo is None:
            raise GuardViolation("reservation start timestamp lacks a timezone")
        recovery_probe_route = item.get("recovery_probe_route")
        recovery_auth_incident_request_id = item.get(
            "recovery_auth_incident_request_id"
        )
        recovery_gate_sha256 = item.get("recovery_gate_sha256")
        if recovery_probe_route is not None:
            if recovery_probe_route not in {"gpt_oss", "qwen"}:
                raise GuardViolation("reservation recovery probe route is malformed")
            if request_provenance_sha256 is None:
                raise GuardViolation("recovery probe reservation lacks provenance")
            try:
                uuid.UUID(str(recovery_auth_incident_request_id or ""))
            except (ValueError, AttributeError) as exc:
                raise GuardViolation(
                    "recovery probe reservation auth incident is malformed"
                ) from exc
            if re.fullmatch(r"[0-9a-f]{64}", str(recovery_gate_sha256 or "")) is None:
                raise GuardViolation("recovery probe reservation gate proof is malformed")
        elif recovery_auth_incident_request_id is not None:
            raise GuardViolation("non-probe reservation has recovery incident lineage")
        elif recovery_gate_sha256 is not None:
            raise GuardViolation("non-probe reservation has recovery gate lineage")
        combinations = {
            (HINDSIGHT_MODEL, "chat", "hindsight_backfill", "all"),
            (HINDSIGHT_MODEL, "chat", "hindsight_monthly", "month"),
            (QWEN_MODEL, "embeddings", "qwen_monthly", "month"),
        }
        normalized_period = (
            "month" if re.fullmatch(r"\d{4}-\d{2}", period) else period
        )
        if (model, endpoint, scope, normalized_period) not in combinations:
            raise GuardViolation("reservation model/endpoint/scope/period is invalid")
        token_values = [
            reservation.get("input_tokens"),
            reservation.get("output_tokens"),
            reservation.get("total_tokens"),
        ]
        if any(type(value) is not int or value < 0 for value in token_values):
            raise GuardViolation("reservation token counts are not exact nonnegative integers")
        input_tokens, output_tokens, total_tokens = token_values
        if input_tokens <= 0 or total_tokens != input_tokens + output_tokens:
            raise GuardViolation("reservation token total is inconsistent")
        raw_cost = reservation.get("cost_usd")
        if isinstance(raw_cost, bool) or not isinstance(raw_cost, (int, float)):
            raise GuardViolation("reservation maximum cost is not numeric")
        cost_usd = float(raw_cost)
        if not math.isfinite(cost_usd) or cost_usd <= 0:
            raise GuardViolation("reservation maximum cost must be finite and positive")
        input_price, output_price = _price_tuple(prices, model)
        expected_cost = (
            input_tokens * input_price + output_tokens * output_price
        ) / 1_000_000
        if not math.isclose(
            cost_usd, expected_cost, rel_tol=1e-12, abs_tol=1e-12
        ):
            raise GuardViolation(
                "reservation maximum cost does not match exact tokens/prices"
            )
        manifest_item = {
                "request_id": request_id,
                "model": model,
                "endpoint": endpoint,
                "scope": scope,
                "period": period,
                "started_at": started_at,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
                "cost_usd": cost_usd,
            }
        if request_provenance_sha256 is not None:
            manifest_item["request_provenance_sha256"] = str(
                request_provenance_sha256
            )
        if recovery_probe_route is not None:
            manifest_item["recovery_probe_route"] = str(recovery_probe_route)
            manifest_item["recovery_auth_incident_request_id"] = str(
                recovery_auth_incident_request_id
            )
            manifest_item["recovery_gate_sha256"] = str(recovery_gate_sha256)
        reservations.append(manifest_item)
    encoded = json.dumps(
        reservations, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return {
        "reservations": reservations,
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "total_max_usd": ordered_float_total(
            [float(value["cost_usd"]) for value in reservations]
        ),
    }


def exact_record_commit_matches(
    call: dict[str, Any],
    request_id: str,
    item: dict[str, Any],
    *,
    prices: tuple[float, float] | None = None,
) -> bool:
    """Prove a ledger-first normal record belongs to one exact reservation."""
    try:
        if prices is None:
            return False
        manifest_sha256 = reservation_manifest(
            {request_id: item}, {str(item["model"]): prices}
        )["sha256"]
        reservation = dict(item["reservation"])
        input_tokens = int(call["input_tokens"])
        output_tokens = int(call["output_tokens"])
        recorded_cost = float(call["cost_usd"])
        provider_reported = float(call.get("provider_reported_cost_usd") or 0)
    except (GuardViolation, KeyError, TypeError, ValueError):
        return False
    lineage_matches = (
        call.get("provider_call_recorded") is True
        and call.get("ambiguous_upstream_reconciled") is not True
        and str(call.get("request_id") or "") == request_id
        and call.get("reservation_manifest_sha256") == manifest_sha256
        and call.get("started_at") == item.get("started_at")
        and call.get("endpoint") == item.get("endpoint")
        and call.get("model") == item.get("model")
        and call.get("scope") == item.get("scope")
        and call.get("period") == item.get("period")
        and call.get("request_provenance_sha256")
        == item.get("request_provenance_sha256")
        and call.get("recovery_probe_route") == item.get("recovery_probe_route")
        and call.get("recovery_auth_incident_request_id")
        == item.get("recovery_auth_incident_request_id")
        and call.get("recovery_gate_sha256") == item.get("recovery_gate_sha256")
        and 0 <= input_tokens <= int(reservation["input_tokens"])
        and 0 <= output_tokens <= int(reservation["output_tokens"])
        and math.isfinite(recorded_cost)
        and recorded_cost >= 0
        and math.isfinite(provider_reported)
        and provider_reported >= 0
    )
    if not lineage_matches or prices is None:
        return lineage_matches
    expected_cost = max(
        (input_tokens * prices[0] + output_tokens * prices[1]) / 1_000_000,
        provider_reported,
    )
    return math.isclose(
        recorded_cost, expected_cost, rel_tol=1e-12, abs_tol=1e-12
    )


def exact_reconciliation_commit_matches(
    call: dict[str, Any],
    request_id: str,
    item: dict[str, Any],
    *,
    prices: tuple[float, float],
) -> bool:
    """Prove one ledger-first maximum charge belongs to the reservation."""
    try:
        reservation_manifest(
            {request_id: item}, {str(item["model"]): prices}
        )
        reservation = dict(item["reservation"])
        input_tokens = call["input_tokens"]
        output_tokens = call["output_tokens"]
        recorded_cost = float(call["cost_usd"])
        reservation_cost = float(reservation["cost_usd"])
    except (GuardViolation, KeyError, TypeError, ValueError):
        return False
    return (
        call.get("ambiguous_upstream_reconciled") is True
        and call.get("estimated_from_reservation") is True
        and call.get("provider_reported_cost_usd") is None
        and str(call.get("request_id") or "") == request_id
        and call.get("started_at") == item.get("started_at")
        and call.get("endpoint") == item.get("endpoint")
        and call.get("model") == item.get("model")
        and call.get("scope") == item.get("scope")
        and call.get("period") == item.get("period")
        and call.get("request_provenance_sha256")
        == item.get("request_provenance_sha256")
        and call.get("recovery_probe_route") == item.get("recovery_probe_route")
        and call.get("recovery_auth_incident_request_id")
        == item.get("recovery_auth_incident_request_id")
        and call.get("recovery_gate_sha256") == item.get("recovery_gate_sha256")
        and type(input_tokens) is int
        and input_tokens == reservation.get("input_tokens")
        and type(output_tokens) is int
        and output_tokens == reservation.get("output_tokens")
        and math.isfinite(recorded_cost)
        and math.isclose(
            recorded_cost, reservation_cost, rel_tol=1e-12, abs_tol=1e-12
        )
    )


def reconciliation_call(
    request_id: str, item: dict[str, Any], *, at: str
) -> dict[str, Any]:
    """Build the exact payload-free maximum charge for one reservation."""
    reservation = dict(item.get("reservation") or {})
    call: dict[str, Any] = {
        "at": at,
        "started_at": item.get("started_at"),
        "request_id": request_id,
        "endpoint": item.get("endpoint"),
        "model": item.get("model"),
        "scope": item.get("scope"),
        "period": item.get("period"),
        "request_provenance_sha256": item.get("request_provenance_sha256"),
        "input_tokens": int(reservation.get("input_tokens") or 0),
        "output_tokens": int(reservation.get("output_tokens") or 0),
        "cost_usd": float(reservation.get("cost_usd") or 0),
        "provider_reported_cost_usd": None,
        "estimated_from_reservation": True,
        "ambiguous_upstream_reconciled": True,
    }
    if item.get("recovery_probe_route") is not None:
        call["recovery_probe_route"] = item["recovery_probe_route"]
        call["recovery_auth_incident_request_id"] = item[
            "recovery_auth_incident_request_id"
        ]
        call["recovery_gate_sha256"] = item["recovery_gate_sha256"]
    return call


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _positive_float(name: str, default: str) -> float:
    try:
        value = float(os.getenv(name, default))
    except ValueError as exc:
        raise SystemExit(f"{name} must be numeric") from exc
    if not math.isfinite(value) or value <= 0:
        raise SystemExit(f"{name} must be finite and positive")
    return value


def _nonnegative_float(name: str, default: str) -> float:
    try:
        value = float(os.getenv(name, default))
    except ValueError as exc:
        raise SystemExit(f"{name} must be numeric") from exc
    if not math.isfinite(value) or value < 0:
        raise SystemExit(f"{name} must be finite and nonnegative")
    return value


def _env_flag(name: str, default: str = "false") -> bool:
    value = os.getenv(name, default).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise SystemExit(f"{name} must be an exact boolean")


def configured_budget_guard() -> tuple[dict[str, Any], float]:
    """Return the exact persisted guard and initial spend configured by env.

    The standalone crash reconciler uses this without receiving provider or
    proxy credentials.  Keeping the calculation here also prevents the
    one-shot recovery container and the long-running proxy from interpreting
    the same limits differently.
    """
    total = _positive_float("PROVIDER_TOTAL_LIMIT_USD", "50")
    margin = _nonnegative_float("PROVIDER_SAFETY_MARGIN_USD", "0.50")
    if margin >= total:
        raise SystemExit("PROVIDER_SAFETY_MARGIN_USD must be below the total limit")
    guard = {
        "provider_total_limit_usd": total,
        "provider_safety_margin_usd": margin,
        "hindsight_backfill_limit_usd": _positive_float(
            "HINDSIGHT_BACKFILL_LIMIT_USD", "30"
        ),
        "hindsight_monthly_limit_usd": _positive_float(
            "HINDSIGHT_MONTHLY_LIMIT_USD", "10"
        ),
        "qwen_monthly_limit_usd": _positive_float(
            "QWEN_MONTHLY_LIMIT_USD", "5"
        ),
        "prices": {
            HINDSIGHT_MODEL: {
                "input_usd_per_m": _positive_float(
                    "HINDSIGHT_INPUT_USD_PER_M",
                    os.getenv("GPT_OSS_120B_INPUT_USD_PER_M", "0.15"),
                ),
                "output_usd_per_m": _positive_float(
                    "HINDSIGHT_OUTPUT_USD_PER_M",
                    os.getenv("GPT_OSS_120B_OUTPUT_USD_PER_M", "0.60"),
                ),
            },
            QWEN_MODEL: {
                "input_usd_per_m": _positive_float(
                    "QWEN_INPUT_USD_PER_M", "0.01"
                ),
                "output_usd_per_m": _nonnegative_float(
                    "QWEN_OUTPUT_USD_PER_M", "0"
                ),
            },
        },
    }
    if (
        not _env_flag("BUDGET_OBSERVATION_MODE")
        and guard["hindsight_backfill_limit_usd"] > total - margin
    ):
        raise SystemExit("backfill limit exceeds the effective provider limit")
    initial = _nonnegative_float("INITIAL_PROVIDER_SPEND_USD", "0")
    return guard, initial


def validate_ledger_invariants(
    ledger: Any,
    *,
    expected_guard: dict[str, Any] | None = None,
    expected_initial_provider_spend_usd: float | None = None,
    enforce_monetary_limits: bool = True,
) -> None:
    """Fail closed on ledger, provenance, balance, or persisted-cap drift."""
    if not isinstance(ledger, dict):
        raise GuardViolation("persisted provider ledger is malformed")
    ledger_version = ledger.get("version")
    if type(ledger_version) is not int or ledger_version not in {
        LEGACY_LEDGER_VERSION,
        LEDGER_VERSION,
    }:
        raise GuardViolation("persisted provider ledger version is unsupported")
    guard = ledger.get("guard")
    if not isinstance(guard, dict):
        raise GuardViolation("persisted ledger guard is malformed")
    if expected_guard is not None and guard != expected_guard:
        raise GuardViolation(
            "persisted ledger guard does not match configured limits/prices"
        )

    def exact_number(value: Any, description: str, *, positive: bool = False) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise GuardViolation(f"{description} is malformed")
        result = float(value)
        if not math.isfinite(result) or (result <= 0 if positive else result < 0):
            raise GuardViolation(f"{description} is invalid")
        return result

    total_limit = exact_number(
        guard.get("provider_total_limit_usd"),
        "persisted provider total limit",
        positive=True,
    )
    safety_margin = exact_number(
        guard.get("provider_safety_margin_usd"),
        "persisted provider safety margin",
    )
    if safety_margin >= total_limit:
        raise GuardViolation("persisted provider safety margin is invalid")
    scope_limits = {
        "hindsight_backfill": exact_number(
            guard.get("hindsight_backfill_limit_usd"),
            "persisted Hindsight backfill limit",
            positive=True,
        ),
        "hindsight_monthly": exact_number(
            guard.get("hindsight_monthly_limit_usd"),
            "persisted Hindsight monthly limit",
            positive=True,
        ),
        "qwen_monthly": exact_number(
            guard.get("qwen_monthly_limit_usd"),
            "persisted Qwen monthly limit",
            positive=True,
        ),
    }
    prices = guard.get("prices")
    if not isinstance(prices, dict) or set(prices) != {HINDSIGHT_MODEL, QWEN_MODEL}:
        raise GuardViolation("persisted price guard is malformed")
    for model, model_prices in prices.items():
        if not isinstance(model_prices, dict):
            raise GuardViolation(f"persisted price guard is malformed for {model}")
        exact_number(
            model_prices.get("input_usd_per_m"),
            f"persisted input price for {model}",
            positive=True,
        )
        output_price = exact_number(
            model_prices.get("output_usd_per_m"),
            f"persisted output price for {model}",
        )
        if model == HINDSIGHT_MODEL and output_price <= 0:
            raise GuardViolation("persisted Hindsight output price is invalid")

    initial_spend = exact_number(
        ledger.get("initial_provider_spend_usd"),
        "persisted initial provider spend",
    )
    if expected_initial_provider_spend_usd is not None and not math.isclose(
        initial_spend,
        expected_initial_provider_spend_usd,
        rel_tol=1e-12,
        abs_tol=1e-12,
    ):
        raise GuardViolation(
            "persisted initial provider spend does not match configuration"
        )
    provider_spend = exact_number(
        ledger.get("provider_spent_usd"), "persisted provider spend"
    )
    calls = ledger.get("calls")
    if not isinstance(calls, list) or not all(isinstance(call, dict) for call in calls):
        raise GuardViolation("persisted provider call ledger is malformed")
    request_ids = [str(call.get("request_id") or "") for call in calls]
    if not all(request_ids) or len(request_ids) != len(set(request_ids)):
        raise GuardViolation("persisted provider call IDs are absent or duplicated")
    try:
        for request_id in request_ids:
            uuid.UUID(request_id)
    except (ValueError, AttributeError) as exc:
        raise GuardViolation("persisted provider call ID is not an exact UUID") from exc

    costs: list[float] = []
    scope_spend: dict[tuple[str, str], list[float]] = {}
    legacy_prefix_open = ledger_version == LEGACY_LEDGER_VERSION
    for call in calls:
        try:
            started_at = datetime.fromisoformat(str(call.get("started_at") or ""))
            recorded_at = datetime.fromisoformat(str(call.get("at") or ""))
        except (TypeError, ValueError) as exc:
            raise GuardViolation("persisted provider call timestamp is malformed") from exc
        if (
            started_at.tzinfo is None
            or recorded_at.tzinfo is None
            or recorded_at < started_at
        ):
            raise GuardViolation("persisted provider call timestamp is invalid")
        cost = exact_number(call.get("cost_usd"), "persisted provider call cost")
        costs.append(cost)
        scope = str(call.get("scope") or "")
        period = str(call.get("period") or "")
        if scope == "hindsight_backfill":
            valid_period = (
                period == "all"
                and call.get("model") == HINDSIGHT_MODEL
                and call.get("endpoint") == "chat"
            )
        elif scope in {"hindsight_monthly", "qwen_monthly"}:
            expected_model = (
                HINDSIGHT_MODEL if scope == "hindsight_monthly" else QWEN_MODEL
            )
            expected_endpoint = (
                "chat" if scope == "hindsight_monthly" else "embeddings"
            )
            valid_period = (
                re.fullmatch(r"\d{4}-\d{2}", period) is not None
                and call.get("model") == expected_model
                and call.get("endpoint") == expected_endpoint
            )
        else:
            valid_period = False
        if not valid_period:
            raise GuardViolation(
                "persisted provider call model/endpoint/scope/period is invalid"
            )
        if any(
            type(call.get(name)) is not int or int(call[name]) < 0
            for name in ("input_tokens", "output_tokens")
        ):
            raise GuardViolation("persisted provider call token counts are invalid")
        request_provenance_sha256 = call.get("request_provenance_sha256")
        if request_provenance_sha256 is not None and re.fullmatch(
            r"[0-9a-f]{64}", str(request_provenance_sha256)
        ) is None:
            raise GuardViolation(
                "persisted provider call provenance digest is malformed"
            )
        recovery_probe_route = call.get("recovery_probe_route")
        recovery_auth_incident_request_id = call.get(
            "recovery_auth_incident_request_id"
        )
        recovery_gate_sha256 = call.get("recovery_gate_sha256")
        if recovery_probe_route is not None:
            expected_probe_route = (
                "gpt_oss" if call.get("model") == HINDSIGHT_MODEL else "qwen"
            )
            if (
                recovery_probe_route != expected_probe_route
                or request_provenance_sha256 is None
            ):
                raise GuardViolation("persisted recovery probe call lineage is malformed")
            try:
                uuid.UUID(str(recovery_auth_incident_request_id or ""))
            except (ValueError, AttributeError) as exc:
                raise GuardViolation(
                    "persisted recovery probe auth incident is malformed"
                ) from exc
            if re.fullmatch(r"[0-9a-f]{64}", str(recovery_gate_sha256 or "")) is None:
                raise GuardViolation(
                    "persisted recovery probe gate proof is malformed"
                )
        elif recovery_auth_incident_request_id is not None:
            raise GuardViolation("non-probe call has recovery incident lineage")
        elif recovery_gate_sha256 is not None:
            raise GuardViolation("non-probe call has recovery gate lineage")
        raw_provider_reported = call.get("provider_reported_cost_usd")
        if raw_provider_reported is None:
            provider_reported = 0.0
        else:
            provider_reported = exact_number(
                raw_provider_reported,
                "persisted provider-reported call cost",
            )
        call_prices = _price_tuple(prices, str(call["model"]))
        expected_cost = max(
            _token_cost(
                call_prices,
                int(call["input_tokens"]),
                int(call["output_tokens"]),
            ),
            provider_reported,
        )
        if not math.isclose(
            cost, expected_cost, rel_tol=1e-12, abs_tol=1e-12
        ):
            raise GuardViolation(
                "persisted provider call cost does not match usage/prices"
            )
        if call.get("ambiguous_upstream_reconciled") is True:
            if (
                raw_provider_reported is not None
                or call.get("estimated_from_reservation") is not True
                or call.get("provider_call_recorded") is not None
                or call.get("reservation_manifest_sha256") is not None
            ):
                raise GuardViolation(
                    "persisted reconciliation call provenance is malformed"
                )
        elif call.get("provider_call_recorded") is True:
            legacy_prefix_open = False
            if (
                type(call.get("estimated_from_reservation")) is not bool
                or re.fullmatch(
                    r"[0-9a-f]{64}",
                    str(call.get("reservation_manifest_sha256") or ""),
                )
                is None
            ):
                raise GuardViolation(
                    "persisted provider call provenance is malformed"
                )
        elif not (
            legacy_prefix_open and is_exact_legacy_provider_call(call)
        ):
            raise GuardViolation("persisted provider call provenance is malformed")
        scope_spend.setdefault((scope, period), []).append(cost)

    balanced_spend = initial_spend + math.fsum(costs)
    if not math.isclose(
        provider_spend, balanced_spend, rel_tol=1e-12, abs_tol=1e-12
    ):
        raise GuardViolation("persisted provider spend/call ledger does not balance")
    if enforce_monetary_limits:
        if provider_spend > total_limit - safety_margin + 1e-12:
            raise GuardViolation("persisted provider spend exceeds the effective limit")
        for (scope, _period), values in scope_spend.items():
            if math.fsum(values) > scope_limits[scope] + 1e-12:
                raise GuardViolation(f"persisted {scope} spend exceeds its limit")


def validate_state_invariants(
    state: Any, *, prices: Any, provider_max_in_flight: int
) -> None:
    """Validate durable circuit/hold coupling without discarding ambiguity."""
    if not isinstance(state, dict):
        raise GuardViolation("persisted budget state is malformed")
    if type(state.get("circuit_open")) is not bool:
        raise GuardViolation("persisted budget circuit bit is malformed")
    in_flight = state.get("in_flight")
    if not isinstance(in_flight, dict):
        raise GuardViolation("persisted in-flight reservation set is malformed")
    if len(in_flight) > provider_max_in_flight:
        raise GuardViolation("persisted provider in-flight ceiling is exceeded")
    reservation_manifest(in_flight, prices)

    reason = state.get("reason")
    if state["circuit_open"]:
        if not isinstance(reason, str) or not reason.strip():
            raise GuardViolation("open budget circuit lacks an exact reason")
    elif reason is not None and reason != "":
        raise GuardViolation("closed budget circuit retains an incident reason")

    hold = state.get("final_maintenance_hold")
    if hold is not None:
        if not isinstance(hold, dict):
            raise GuardViolation("final maintenance hold proof is malformed")
        entered_at = hold.get("entered_at")
        try:
            parsed = datetime.fromisoformat(str(entered_at))
        except ValueError as exc:
            raise GuardViolation(
                "final maintenance hold timestamp is malformed"
            ) from exc
        if parsed.tzinfo is None:
            raise GuardViolation("final maintenance hold timestamp lacks a timezone")
        if (
            hold.get("reason") != FINAL_MAINTENANCE_HOLD_REASON
            or state["circuit_open"] is not True
            or reason != FINAL_MAINTENANCE_HOLD_REASON
            or in_flight
        ):
            raise GuardViolation(
                "final maintenance hold is not exactly coupled to a drained open circuit"
            )

    consumed = state.get("consumed_provider_auth_reset_tokens", [])
    if (
        not isinstance(consumed, list)
        or not all(
            isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
            for value in consumed
        )
        or len(consumed) != len(set(consumed))
    ):
        raise GuardViolation("provider auth reset token history is malformed")
    for name in (
        "last_provider_auth_reset",
        "last_record_commit_repair",
        "last_ambiguous_reconciliation",
    ):
        if state.get(name) is not None and not isinstance(state[name], dict):
            raise GuardViolation(f"{name} proof is malformed")
    failure_history = state.get("provider_failure_history", [])
    if not isinstance(failure_history, list) or not all(
        isinstance(value, dict) for value in failure_history
    ):
        raise GuardViolation("provider failure history is malformed")
    failure_ids: list[str] = []
    for failure in failure_history:
        request_id = str(failure.get("request_id") or "")
        try:
            uuid.UUID(request_id)
        except (ValueError, AttributeError) as exc:
            raise GuardViolation(
                "provider failure request ID is malformed"
            ) from exc
        status_code = failure.get("status_code")
        if type(status_code) is not int or not 400 <= status_code <= 599:
            raise GuardViolation("provider failure status is malformed")
        if failure.get("model") == HINDSIGHT_MODEL and failure.get(
            "endpoint"
        ) == "chat":
            expected_agent = FUGU_HINDSIGHT_AGENT_ID
        elif failure.get("model") == QWEN_MODEL and failure.get(
            "endpoint"
        ) == "embeddings":
            expected_agent = FUGU_QWEN_AGENT_ID
        else:
            raise GuardViolation("provider failure model/endpoint is malformed")
        if (
            failure.get("fugu_session_id") != request_id
            or failure.get("fugu_agent_id") != expected_agent
        ):
            raise GuardViolation("provider failure Fugu lineage is malformed")
        request_provenance_sha256 = failure.get("request_provenance_sha256")
        if request_provenance_sha256 is not None and re.fullmatch(
            r"[0-9a-f]{64}", str(request_provenance_sha256)
        ) is None:
            raise GuardViolation(
                "provider failure request provenance is malformed"
            )
        recovery_probe_route = failure.get("recovery_probe_route")
        recovery_auth_incident_request_id = failure.get(
            "recovery_auth_incident_request_id"
        )
        recovery_gate_sha256 = failure.get("recovery_gate_sha256")
        if recovery_probe_route is not None:
            expected_probe_route = (
                "gpt_oss" if failure.get("model") == HINDSIGHT_MODEL else "qwen"
            )
            if (
                recovery_probe_route != expected_probe_route
                or request_provenance_sha256 is None
            ):
                raise GuardViolation(
                    "provider recovery probe failure lineage is malformed"
                )
            try:
                uuid.UUID(str(recovery_auth_incident_request_id or ""))
            except (ValueError, AttributeError) as exc:
                raise GuardViolation(
                    "provider recovery probe auth incident is malformed"
                ) from exc
            if re.fullmatch(r"[0-9a-f]{64}", str(recovery_gate_sha256 or "")) is None:
                raise GuardViolation(
                    "provider recovery probe gate proof is malformed"
                )
        elif recovery_auth_incident_request_id is not None:
            raise GuardViolation("non-probe failure has recovery incident lineage")
        elif recovery_gate_sha256 is not None:
            raise GuardViolation("non-probe failure has recovery gate lineage")
        try:
            failure_at = datetime.fromisoformat(str(failure.get("at") or ""))
        except ValueError as exc:
            raise GuardViolation("provider failure timestamp is malformed") from exc
        if failure_at.tzinfo is None:
            raise GuardViolation("provider failure timestamp lacks a timezone")
        retry_after = failure.get("retry_after")
        if retry_after is not None and validated_retry_after(retry_after) != retry_after:
            raise GuardViolation("provider failure Retry-After proof is malformed")
        reserved_total_tokens = failure.get("reservation_total_tokens")
        if reserved_total_tokens is not None and (
            type(reserved_total_tokens) is not int or reserved_total_tokens <= 0
        ):
            raise GuardViolation(
                "provider failure reservation token proof is malformed"
            )
        failure_ids.append(request_id)
    if len(failure_ids) != len(set(failure_ids)):
        raise GuardViolation("provider failure request IDs are duplicated")

    circuit_incident_id = state.get(
        "provider_auth_circuit_incident_request_id"
    )
    circuit_incident_status = state.get(
        "provider_auth_circuit_incident_status"
    )
    if bool(circuit_incident_id is not None) != bool(
        circuit_incident_status is not None
    ):
        raise GuardViolation(
            "provider auth circuit incident ID/status pairing is malformed"
        )
    if circuit_incident_id is not None:
        try:
            uuid.UUID(str(circuit_incident_id))
        except (ValueError, AttributeError) as exc:
            raise GuardViolation(
                "provider auth circuit incident ID is malformed"
            ) from exc
        incidents = [
            value
            for value in failure_history
            if str(value.get("request_id") or "") == str(circuit_incident_id)
        ]
        if (
            len(incidents) != 1
            or incidents[0].get("status_code") not in {401, 402}
            or incidents[0].get("status_code") != circuit_incident_status
            or incidents[0].get("recovery_probe_route") is not None
            or state["circuit_open"] is not True
        ):
            raise GuardViolation(
                "provider auth circuit does not bind one original incident"
            )

    active_auth_recovery = state.get("provider_auth_recovery")
    if active_auth_recovery is not None:
        if not isinstance(active_auth_recovery, dict) or set(
            active_auth_recovery
        ) != {
            "version",
            "auth_incident_request_id",
            "auth_incident_status",
            "auth_incident_at",
        }:
            raise GuardViolation("active provider auth recovery proof is malformed")
        incident_request_id = str(
            active_auth_recovery.get("auth_incident_request_id") or ""
        )
        try:
            uuid.UUID(incident_request_id)
            incident_at = datetime.fromisoformat(
                str(active_auth_recovery.get("auth_incident_at") or "")
            )
        except (ValueError, AttributeError) as exc:
            raise GuardViolation(
                "active provider auth recovery lineage is malformed"
            ) from exc
        if incident_at.tzinfo is None:
            raise GuardViolation(
                "active provider auth recovery timestamp lacks a timezone"
            )
        status = active_auth_recovery.get("auth_incident_status")
        if (
            active_auth_recovery.get("version") != 1
            or status not in {401, 402}
            or state["circuit_open"] is not True
        ):
            raise GuardViolation("active provider auth recovery state is invalid")
        incidents = [
            value
            for value in failure_history
            if str(value.get("request_id") or "") == incident_request_id
        ]
        if (
            len(incidents) != 1
            or incidents[0].get("status_code") != status
            or incidents[0].get("at") != active_auth_recovery["auth_incident_at"]
            or incidents[0].get("recovery_probe_route") is not None
            or incidents[0].get("recovery_auth_incident_request_id") is not None
        ):
            raise GuardViolation(
                "active provider auth recovery does not bind its root incident"
            )
        if str(circuit_incident_id or "") != incident_request_id:
            raise GuardViolation(
                "active provider auth recovery differs from the circuit incident"
            )


def exact_recovery_auth_incident(
    state: dict[str, Any], request_id: str
) -> dict[str, Any]:
    """Return one original, non-probe 401/402 recovery episode."""
    matches = [
        value
        for value in state.get("provider_failure_history") or []
        if str(value.get("request_id") or "") == request_id
    ]
    if len(matches) != 1:
        raise GuardViolation(
            "recovery auth incident is not one exact durable failure"
        )
    incident = dict(matches[0])
    if (
        incident.get("status_code") not in {401, 402}
        or incident.get("recovery_probe_route") is not None
        or incident.get("recovery_auth_incident_request_id") is not None
        or incident.get("recovery_gate_sha256") is not None
    ):
        raise GuardViolation(
            "recovery auth incident is not an original provider-auth failure"
        )
    return incident


def successful_recovery_probe_calls(
    ledger: dict[str, Any], auth_incident_request_id: str
) -> dict[str, dict[str, Any]]:
    """Return at most one real, non-reconciled paid success per probe route."""
    result: dict[str, dict[str, Any]] = {}
    for raw in ledger.get("calls") or []:
        call = dict(raw)
        if (
            str(call.get("recovery_auth_incident_request_id") or "")
            != auth_incident_request_id
            or call.get("ambiguous_upstream_reconciled") is True
        ):
            continue
        route = call.get("recovery_probe_route")
        if route not in {"gpt_oss", "qwen"}:
            continue
        if call.get("provider_call_recorded") is not True:
            raise GuardViolation(
                "recovery probe success lacks a real provider-call proof"
            )
        if route in result:
            raise GuardViolation(
                f"duplicate successful recovery probe route for {route}"
            )
        result[str(route)] = call
    return result


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
    admin_api_key: str
    provider_egress_enabled: bool
    provider_total_limit_usd: float
    provider_safety_margin_usd: float
    hindsight_phase: str
    hindsight_backfill_limit_usd: float
    hindsight_monthly_limit_usd: float
    qwen_monthly_limit_usd: float
    hindsight_llm_max_concurrent: int
    provider_max_in_flight: int
    rpm_limit: int
    tpm_limit: int
    prices: dict[str, tuple[float, float]]
    initial_provider_spend_usd: float
    budget_observation_mode: bool = False
    recovery_control_nonce: str = ""
    recovery_control_roll_sha256: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        provider_api_key = os.getenv("PROVIDER_API_KEY", "").strip()
        proxy_api_key = os.getenv("PROXY_API_KEY", "").strip()
        admin_api_key = os.getenv("ADMIN_API_KEY", "").strip()
        if not provider_api_key:
            raise SystemExit("PROVIDER_API_KEY is required")
        if not proxy_api_key:
            raise SystemExit("PROXY_API_KEY is required")
        if not admin_api_key:
            raise SystemExit("ADMIN_API_KEY is required")
        if hmac.compare_digest(provider_api_key, proxy_api_key):
            raise SystemExit("PROXY_API_KEY must differ from PROVIDER_API_KEY")
        if any(
            hmac.compare_digest(admin_api_key, value)
            for value in (provider_api_key, proxy_api_key)
        ):
            raise SystemExit(
                "ADMIN_API_KEY must differ from provider and inference keys"
            )
        phase = os.getenv("HINDSIGHT_BUDGET_PHASE", "backfill").strip().lower()
        if phase not in {"backfill", "monthly"}:
            raise SystemExit("HINDSIGHT_BUDGET_PHASE must be backfill or monthly")
        guard, initial_spend = configured_budget_guard()
        recovery_control_nonce = os.getenv("RECOVERY_CONTROL_NONCE", "").strip()
        recovery_control_roll_sha256 = os.getenv(
            "RECOVERY_CONTROL_ROLL_SHA256", ""
        ).strip()
        if recovery_control_nonce and re.fullmatch(
            r"[0-9a-f]{32}", recovery_control_nonce
        ) is None:
            raise SystemExit(
                "RECOVERY_CONTROL_NONCE must be 32 lowercase hexadecimal characters"
            )
        if recovery_control_roll_sha256 and re.fullmatch(
            r"[0-9a-f]{64}", recovery_control_roll_sha256
        ) is None:
            raise SystemExit(
                "RECOVERY_CONTROL_ROLL_SHA256 must be 64 lowercase hexadecimal characters"
            )
        if bool(recovery_control_nonce) != bool(recovery_control_roll_sha256):
            raise SystemExit(
                "RECOVERY_CONTROL_NONCE and RECOVERY_CONTROL_ROLL_SHA256 must be configured together"
            )
        total = float(guard["provider_total_limit_usd"])
        margin = float(guard["provider_safety_margin_usd"])
        settings = cls(
            provider_base_url=os.getenv(
                "PROVIDER_BASE_URL", "https://api.redpill.ai/v1"
            ).rstrip("/"),
            provider_api_key=provider_api_key,
            proxy_api_key=proxy_api_key,
            admin_api_key=admin_api_key,
            provider_egress_enabled=os.getenv("PROVIDER_EGRESS_ENABLED", "false")
            .strip()
            .lower()
            in {"1", "true", "yes", "on"},
            provider_total_limit_usd=total,
            provider_safety_margin_usd=margin,
            hindsight_phase=phase,
            hindsight_backfill_limit_usd=float(
                guard["hindsight_backfill_limit_usd"]
            ),
            hindsight_monthly_limit_usd=float(
                guard["hindsight_monthly_limit_usd"]
            ),
            qwen_monthly_limit_usd=float(guard["qwen_monthly_limit_usd"]),
            hindsight_llm_max_concurrent=_positive_int(
                "HINDSIGHT_LLM_MAX_CONCURRENT", "3"
            ),
            provider_max_in_flight=_positive_int(
                "PROVIDER_MAX_IN_FLIGHT", "6"
            ),
            rpm_limit=_positive_int("PROXY_RPM_LIMIT", "30"),
            tpm_limit=_positive_int("PROXY_TPM_LIMIT", "1000000"),
            prices={
                HINDSIGHT_MODEL: (
                    float(guard["prices"][HINDSIGHT_MODEL]["input_usd_per_m"]),
                    float(guard["prices"][HINDSIGHT_MODEL]["output_usd_per_m"]),
                ),
                QWEN_MODEL: (
                    float(guard["prices"][QWEN_MODEL]["input_usd_per_m"]),
                    float(guard["prices"][QWEN_MODEL]["output_usd_per_m"]),
                ),
            },
            initial_provider_spend_usd=initial_spend,
            budget_observation_mode=_env_flag("BUDGET_OBSERVATION_MODE"),
            recovery_control_nonce=recovery_control_nonce,
            recovery_control_roll_sha256=recovery_control_roll_sha256,
        )
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


def validated_retry_after(value: Any) -> str | None:
    """Accept only a bounded HTTP Retry-After delay or HTTP-date."""
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > 128 or "\r" in candidate or "\n" in candidate:
        return None
    try:
        seconds = float(candidate)
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(candidate)
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed.tzinfo is None:
            return None
        delay = (
            parsed.astimezone(timezone.utc) - datetime.now(timezone.utc)
        ).total_seconds()
        return candidate if delay <= MAX_RETRY_AFTER_SECONDS else None
    return (
        candidate
        if math.isfinite(seconds) and 0 <= seconds <= MAX_RETRY_AFTER_SECONDS
        else None
    )


def prepare_chat_payload(value: Any) -> tuple[dict[str, Any], int, int]:
    if not isinstance(value, dict):
        raise GuardViolation("request body must be a JSON object")
    payload = dict(value)
    if payload.get("model") != HINDSIGHT_MODEL:
        raise GuardViolation("chat model is outside the production allowlist")
    if payload.get("stream", False) is not False:
        raise GuardViolation("streaming is disabled so usage can be metered")
    maximum = payload.get("max_completion_tokens", payload.get("max_tokens", 8192))
    if type(maximum) is not int:
        raise GuardViolation("completion-token ceiling must be an exact integer")
    max_tokens = maximum
    if not 0 < max_tokens <= MAX_COMPLETION_TOKENS:
        raise GuardViolation("completion-token ceiling must be between 1 and 8192")
    if payload.get("reasoning_effort", "low") != "low":
        raise GuardViolation("reasoning_effort must be low")
    if payload.get("include_reasoning") is not None and payload.get(
        "include_reasoning"
    ) is not False:
        raise GuardViolation("include_reasoning must be false")
    n = payload.get("n", 1)
    if type(n) is not int or n != 1:
        raise GuardViolation("n must be the exact integer 1")
    response_format = payload.get("response_format")
    if isinstance(response_format, dict):
        schema = response_format.get("json_schema")
        if isinstance(schema, dict) and schema.get("strict") is True:
            raise GuardViolation("strict structured output is disabled")
    payload["reasoning_effort"] = "low"
    payload["include_reasoning"] = False
    payload["n"] = 1
    payload["max_completion_tokens"] = max_tokens
    payload.pop("max_tokens", None)
    prompt_bytes = len(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
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
        if type(dimensions) is not int:
            raise GuardViolation("embedding dimensions must be an exact integer")
        if not 1 <= dimensions <= 4096:
            raise GuardViolation("embedding dimensions are outside 1..4096")
        payload["dimensions"] = dimensions
    prompt_bytes = len(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    )
    return payload, max(1, prompt_bytes), 0


def recovery_probe_spec(route: Any, provenance: Any) -> dict[str, Any]:
    """Return the one fixed provider request allowed for a recovery route."""
    if not isinstance(route, str) or route not in {"gpt_oss", "qwen"}:
        raise GuardViolation("recovery probe route is outside the exact allowlist")
    if (
        not isinstance(provenance, str)
        or RECOVERY_PROVENANCE_RE.fullmatch(provenance) is None
        or not provenance.endswith("-" + route)
    ):
        raise GuardViolation("recovery probe provenance is malformed or route-mismatched")
    metadata = {
        "recovery_preflight_id": provenance,
        "session_id": provenance,
    }
    if route == "gpt_oss":
        endpoint = "chat"
        model = HINDSIGHT_MODEL
        agent_id = FUGU_HINDSIGHT_AGENT_ID
        payload, input_tokens, output_tokens = prepare_chat_payload(
            {
                "model": model,
                "messages": [
                    {"role": "user", "content": "recovery auth preflight"}
                ],
                "max_completion_tokens": 1,
                "reasoning_effort": "low",
                "include_reasoning": False,
                "metadata": metadata,
            }
        )
        provider_path = "/chat/completions"
    else:
        endpoint = "embeddings"
        model = QWEN_MODEL
        agent_id = FUGU_QWEN_AGENT_ID
        payload, input_tokens, output_tokens = prepare_embedding_payload(
            {
                "model": model,
                "input": ["recovery auth preflight"],
                "metadata": metadata,
            }
        )
        provider_path = "/embeddings"
    return {
        "route": route,
        "provenance": provenance,
        "provenance_sha256": hashlib.sha256(provenance.encode()).hexdigest(),
        "request_id": str(uuid.uuid5(RECOVERY_PROBE_NAMESPACE, provenance)),
        "endpoint": endpoint,
        "model": model,
        "agent_id": agent_id,
        "payload": payload,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "provider_path": provider_path,
    }


def recovery_probe_response_is_exact(route: str, value: Any) -> bool:
    """Require the minimal real success shape for the selected provider route."""
    if not isinstance(value, dict):
        return False
    if route == "gpt_oss":
        choices = value.get("choices")
        if not isinstance(choices, list) or not choices:
            return False
        first = choices[0]
        message = first.get("message") if isinstance(first, dict) else None
        if not isinstance(message, dict):
            return False
        if isinstance(message.get("content"), str):
            return True
        usage = value.get("usage")
        return (
            len(choices) == 1
            and "content" in message
            and message.get("content") is None
            and message.get("role") == "assistant"
            and message.get("tool_calls") in (None, [])
            and first.get("finish_reason") == "length"
            and isinstance(usage, dict)
            and type(usage.get("completion_tokens")) is int
            and usage["completion_tokens"] == 1
        )
    if route == "qwen":
        data = value.get("data")
        if not (
            isinstance(data, list)
            and data
            and isinstance(data[0], dict)
        ):
            return False
        embedding = data[0].get("embedding")
        return bool(embedding) and isinstance(embedding, list) and all(
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(float(value))
            for value in embedding
        )
    return False


class BudgetState:
    def __init__(self, ledger_path: Path, state_path: Path, settings: Settings) -> None:
        self.ledger_path = ledger_path
        self.state_path = state_path
        self.settings = settings
        self.lock = threading.Lock()
        # A transport-ambiguous request is normally maximum-charged inline. If
        # either durable replace transiently fails, retain the exact request ID
        # in memory so every subsequent reserve retries settlement before the
        # provider-capacity check. The reservation itself remains the durable
        # restart proof; startup settles all such reservations independently.
        self._observation_settlement_pending: set[str] = set()
        if ledger_path.exists():
            self.ledger = json.loads(ledger_path.read_text())
        else:
            self.ledger = {
                "version": LEDGER_VERSION,
                "guard": settings.guard_fingerprint(),
                "initial_provider_spend_usd": settings.initial_provider_spend_usd,
                "provider_spent_usd": settings.initial_provider_spend_usd,
                "calls": [],
            }
            atomic_json(ledger_path, self.ledger)
        try:
            validate_ledger_invariants(
                self.ledger,
                expected_guard=settings.guard_fingerprint(),
                expected_initial_provider_spend_usd=(
                    settings.initial_provider_spend_usd
                ),
                enforce_monetary_limits=not settings.budget_observation_mode,
            )
        except GuardViolation as exc:
            raise SystemExit(str(exc)) from exc

        if state_path.exists():
            self.value = json.loads(state_path.read_text())
        else:
            self.value = {"circuit_open": False, "in_flight": {}}
            atomic_json(state_path, self.value)
        try:
            validate_state_invariants(
                self.value,
                prices=settings.prices,
                provider_max_in_flight=settings.provider_max_in_flight,
            )
        except GuardViolation as exc:
            raise SystemExit(str(exc)) from exc
        self._repair_record_commit_boundary()
        if settings.budget_observation_mode:
            try:
                for request_id in sorted(self.value.get("in_flight") or {}):
                    self.maximum_charge_ambiguous(request_id)
                self._clear_non_credit_circuit_for_observation()
            except GuardViolation as exc:
                raise SystemExit(str(exc)) from exc
        elif self.value["in_flight"] and not self.value.get("circuit_open"):
            self.value.update(
                {
                    "circuit_open": True,
                    "reason": "ambiguous in-flight requests found after proxy restart",
                }
            )
            atomic_json(state_path, self.value)
        try:
            validate_state_invariants(
                self.value,
                prices=settings.prices,
                provider_max_in_flight=settings.provider_max_in_flight,
            )
        except GuardViolation as exc:
            raise SystemExit(str(exc)) from exc

    def _clear_non_credit_circuit_for_observation(self) -> None:
        """Migrate legacy fail-closed state without clearing a credit stop."""
        if not self.value.get("circuit_open"):
            return
        reason = str(self.value.get("reason") or "")
        if reason == CREDIT_EXHAUSTED_CIRCUIT_REASON:
            return
        candidate = copy.deepcopy(self.value)
        candidate["circuit_open"] = False
        candidate.pop("reason", None)
        candidate.pop("final_maintenance_hold", None)
        candidate.pop("provider_auth_recovery", None)
        candidate.pop("provider_auth_circuit_incident_request_id", None)
        candidate.pop("provider_auth_circuit_incident_status", None)
        candidate["last_observation_mode_circuit_clear"] = {
            "at": datetime.now(timezone.utc).isoformat(),
            "previous_reason": reason,
        }
        validate_state_invariants(
            candidate,
            prices=self.settings.prices,
            provider_max_in_flight=self.settings.provider_max_in_flight,
        )
        atomic_json(self.state_path, candidate)
        self.value = candidate

    def _record_matches_reservation(
        self,
        call: dict[str, Any], request_id: str, item: dict[str, Any]
    ) -> bool:
        reservation = dict(item.get("reservation") or {})
        try:
            input_tokens = int(call["input_tokens"])
            output_tokens = int(call["output_tokens"])
            maximum_input = int(reservation["input_tokens"])
            maximum_output = int(reservation["output_tokens"])
            recorded_cost = float(call["cost_usd"])
            provider_reported = float(call.get("provider_reported_cost_usd") or 0)
            prices = self.settings.prices[str(item["model"])]
        except (KeyError, TypeError, ValueError):
            return False
        expected_cost = max(
            _token_cost(prices, input_tokens, output_tokens), provider_reported
        )
        return (
            exact_record_commit_matches(
                call, request_id, item, prices=prices
            )
            and 0 <= input_tokens <= maximum_input
            and 0 <= output_tokens <= maximum_output
            and math.isfinite(recorded_cost)
            and abs(recorded_cost - expected_cost) <= 1e-12
        )

    def _repair_record_commit_boundary(self) -> None:
        """Finish exact ledger-first normal records or maximum charges."""
        original_in_flight = copy.deepcopy(self.value.get("in_flight", {}))
        repaired: list[str] = []
        maximum_charged: list[str] = []
        contradiction: list[str] = []
        calls = list(self.ledger.get("calls") or [])
        for request_id, item in list(self.value.get("in_flight", {}).items()):
            matches = [
                call
                for call in calls
                if str(call.get("request_id") or "") == str(request_id)
            ]
            if not matches:
                continue
            model_prices = self.settings.prices.get(str(item.get("model") or ""))
            if len(matches) != 1 or model_prices is None:
                contradiction.append(str(request_id))
                continue
            if self._record_matches_reservation(
                matches[0], str(request_id), dict(item)
            ):
                self.value["in_flight"].pop(request_id, None)
                repaired.append(str(request_id))
            elif exact_reconciliation_commit_matches(
                matches[0],
                str(request_id),
                dict(item),
                prices=model_prices,
            ):
                self.value["in_flight"].pop(request_id, None)
                maximum_charged.append(str(request_id))
            else:
                contradiction.append(str(request_id))
        covered = sorted(repaired + maximum_charged)
        if covered:
            proof_manifest = reservation_manifest(
                {
                    request_id: original_in_flight[request_id]
                    for request_id in covered
                },
                self.settings.prices,
            )
            drained = not self.value.get("in_flight")
            if repaired and maximum_charged:
                result = "record_commits_repaired_and_maximum_charged"
            elif repaired:
                result = "record_commits_repaired_and_drained"
            else:
                result = "maximum_charged_and_drained"
            if not drained:
                result += "_with_remaining_ambiguity"
            now = datetime.now(timezone.utc).isoformat()
            if repaired:
                self.value["last_record_commit_repair"] = {
                    "at": now,
                    "requests": len(covered),
                    "request_ids": sorted(repaired),
                    "reservation_ids": covered,
                    "reservations": proof_manifest["reservations"],
                    "manifest_sha256": proof_manifest["sha256"],
                    "total_max_usd": proof_manifest["total_max_usd"],
                    "result": result,
                }
            if maximum_charged:
                self.value["last_ambiguous_reconciliation"] = {
                    "at": now,
                    "requests": len(covered),
                    "reservation_ids": covered,
                    "record_commit_repaired_ids": sorted(repaired),
                    "maximum_charged_ids": sorted(maximum_charged),
                    "reservations": proof_manifest["reservations"],
                    "manifest_sha256": proof_manifest["sha256"],
                    "total_max_usd": proof_manifest["total_max_usd"],
                    "charged_maximum_usd": math.fsum(
                        float(original_in_flight[request_id]["reservation"]["cost_usd"])
                        for request_id in maximum_charged
                    ),
                    "provider_spent_usd": float(
                        self.ledger["provider_spent_usd"]
                    ),
                    "result": result,
                }
            ambiguity_reason = str(self.value.get("reason") or "")
            repaired_recovery_probes = [
                request_id
                for request_id in repaired
                if original_in_flight[request_id].get("recovery_probe_route")
                is not None
            ]
            reconciled_recovery_probes = [
                request_id
                for request_id in maximum_charged
                if original_in_flight[request_id].get("recovery_probe_route")
                is not None
            ]
            recovered_probe_ids = repaired_recovery_probes + reconciled_recovery_probes
            if recovered_probe_ids:
                try:
                    incident_ids = {
                        str(
                            original_in_flight[request_id][
                                "recovery_auth_incident_request_id"
                            ]
                        )
                        for request_id in recovered_probe_ids
                    }
                    if len(incident_ids) != 1:
                        raise GuardViolation(
                            "recovered probe reservations span auth incidents"
                        )
                    incident = exact_recovery_auth_incident(
                        self.value, incident_ids.pop()
                    )
                    self.value["circuit_open"] = True
                    self.value["reason"] = (
                        "provider returned " + str(incident["status_code"])
                    )
                except (GuardViolation, KeyError):
                    self.value["circuit_open"] = True
                    self.value["reason"] = RECOVERY_PROBE_SUCCESS_HOLD_REASON
            elif (
                not contradiction
                and drained
                and self.value.get("circuit_open") is True
                and (
                    ambiguity_reason == "ambiguous upstream failure"
                    or ambiguity_reason.startswith("ambiguous upstream failure:")
                    or ambiguity_reason
                    == "ambiguous in-flight requests found after proxy restart"
                    or ambiguity_reason
                    == "operator-authorized stale reservation reconciliation"
                )
            ):
                self.value["circuit_open"] = False
                self.value.pop("reason", None)
                if repaired:
                    self.value["last_record_commit_repair"][
                        "cleared_exact_record_ambiguity"
                    ] = True
                if maximum_charged:
                    self.value["last_ambiguous_reconciliation"][
                        "cleared_exact_ambiguity"
                    ] = True
        if contradiction:
            self.value.update(
                {
                    "circuit_open": True,
                    "reason": (
                        "ledger/state provenance contradiction for request IDs: "
                        + ",".join(sorted(contradiction))
                    ),
                }
            )
        if covered or contradiction:
            validate_state_invariants(
                self.value,
                prices=self.settings.prices,
                provider_max_in_flight=self.settings.provider_max_in_flight,
            )
            atomic_json(self.state_path, self.value)

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
        for failure in self.value.get("provider_failure_history") or []:
            try:
                timestamp = datetime.fromisoformat(str(failure["at"])).timestamp()
                tokens = int(failure["reservation_total_tokens"])
            except (KeyError, TypeError, ValueError):
                continue
            age = now.timestamp() - timestamp
            if 0 <= age < 60 and tokens > 0:
                recent.append((timestamp, tokens))
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
        *,
        request_provenance_sha256: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        if request_provenance_sha256 is not None and re.fullmatch(
            r"[0-9a-f]{64}", request_provenance_sha256
        ) is None:
            raise GuardViolation("request provenance digest is malformed")
        prices = self.settings.prices[model]
        cost = _token_cost(prices, input_tokens, output_tokens)
        scope, period, scope_limit = self._scope_for(endpoint)
        if self.settings.budget_observation_mode:
            self.retry_pending_observation_settlements()
        while True:
            with self.lock:
                if self.value.get("circuit_open"):
                    raise BudgetViolation(
                        str(self.value.get("reason") or "circuit open")
                    )
                if (
                    len(self.value.get("in_flight") or {})
                    >= self.settings.provider_max_in_flight
                ):
                    raise ProviderCapacityViolation(
                        "provider concurrent in-flight ceiling would be exceeded"
                    )
                if not self.settings.budget_observation_mode:
                    total_reserved = self._pending_cost()
                    provider_spend = float(
                        self.ledger.get("provider_spent_usd", 0)
                    )
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
                        raise BudgetViolation(
                            f"{scope} local budget would be exceeded"
                        )
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
                    candidate = copy.deepcopy(self.value)
                    item = {
                        "model": model,
                        "endpoint": endpoint,
                        "scope": scope,
                        "period": period,
                        "started_at": now.isoformat(),
                        "reservation": reservation,
                    }
                    if request_provenance_sha256 is not None:
                        item["request_provenance_sha256"] = (
                            request_provenance_sha256
                        )
                    candidate["in_flight"][request_id] = item
                    atomic_json(self.state_path, candidate)
                    self.value = candidate
                    return request_id, candidate["in_flight"][request_id]
            time.sleep(min(delay + 0.05, 1.0))

    def reserve_recovery_probe(
        self,
        *,
        route: Any,
        provenance: Any,
        auth_incident_request_id: Any,
        agent_circuit_open: Any,
        pocket_circuit_open: Any,
        provider_in_flight: Any,
        projected_full_cost_usd: Any,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Reserve or replay one deterministic, admin-authorized auth probe.

        Unlike ordinary inference this transition is allowed only while the
        budget circuit is already open for one exact, durable 401/402.  It
        never changes that circuit.  The separately authenticated controller
        attests the Agent/Pocket/provider gates in this request; the proxy
        independently proves its own circuit, reservation, and spend gates.
        """
        spec = recovery_probe_spec(route, provenance)
        if agent_circuit_open is not True or pocket_circuit_open is not True:
            raise GuardViolation("recovery probe requires Agent and Pocket open")
        if type(provider_in_flight) is not int or provider_in_flight != 0:
            raise GuardViolation("recovery probe requires zero provider in-flight")
        if isinstance(projected_full_cost_usd, bool) or not isinstance(
            projected_full_cost_usd, (int, float)
        ):
            raise GuardViolation("recovery probe projection is not numeric")
        projection = float(projected_full_cost_usd)
        if not math.isfinite(projection) or projection < 0:
            raise GuardViolation("recovery probe projection is invalid")
        auth_incident_request_id = str(auth_incident_request_id or "")
        try:
            uuid.UUID(auth_incident_request_id)
        except (ValueError, AttributeError) as exc:
            raise GuardViolation("recovery probe auth incident ID is malformed") from exc
        gate_sha256 = hashlib.sha256(
            json.dumps(
                {
                    "agent_circuit_open": True,
                    "auth_incident_request_id": auth_incident_request_id,
                    "pocket_circuit_open": True,
                    "projected_full_cost_usd": projection,
                    "provider_in_flight": 0,
                    "provenance": spec["provenance"],
                    "route": spec["route"],
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()

        request_id = str(spec["request_id"])
        provenance_sha256 = str(spec["provenance_sha256"])
        with self.lock:
            calls = [
                value
                for value in self.ledger.get("calls") or []
                if str(value.get("request_id") or "") == request_id
            ]
            failures = [
                value
                for value in self.value.get("provider_failure_history") or []
                if str(value.get("request_id") or "") == request_id
            ]
            if len(calls) > 1 or len(failures) > 1 or (calls and failures):
                raise GuardViolation(
                    "recovery probe deterministic terminal lineage is contradictory"
                )

            def exact_terminal(value: dict[str, Any]) -> None:
                if (
                    value.get("model") != spec["model"]
                    or value.get("endpoint") != spec["endpoint"]
                    or value.get("request_provenance_sha256")
                    != provenance_sha256
                ):
                    raise GuardViolation(
                        "recovery probe deterministic terminal provenance drift"
                    )
                if (
                    value.get("recovery_probe_route") != spec["route"]
                    or value.get("recovery_auth_incident_request_id")
                    != auth_incident_request_id
                    or value.get("recovery_gate_sha256") != gate_sha256
                ):
                    raise GuardViolation(
                        "recovery probe deterministic incident lineage drift"
                    )

            replay = {
                "route": spec["route"],
                "request_id": request_id,
                "provenance_sha256": provenance_sha256,
                "reused": True,
            }
            if calls:
                call = dict(calls[0])
                reconciled = call.get("ambiguous_upstream_reconciled") is True
                exact_terminal(call)
                replay.update(
                    {
                        "result": (
                            "ambiguous_reconciled"
                            if reconciled
                            else "authenticated"
                        ),
                        "upstream_status": None if reconciled else 200,
                        "cost_usd": float(call.get("cost_usd") or 0),
                    }
                )
                return spec, replay
            if failures:
                failure = dict(failures[0])
                exact_terminal(failure)
                status_code = int(failure["status_code"])
                replay.update(
                    {
                        "result": (
                            "expected_auth_failure"
                            if status_code in {401, 402}
                            else (
                                "retryable_failure"
                                if status_code == 429
                                or 500 <= status_code <= 599
                                else "unexpected_failure"
                            )
                        ),
                        "upstream_status": status_code,
                        "retry_after": failure.get("retry_after"),
                    }
                )
                return spec, replay

            existing = (self.value.get("in_flight") or {}).get(request_id)
            if existing is not None:
                if (
                    existing.get("model") != spec["model"]
                    or existing.get("endpoint") != spec["endpoint"]
                    or existing.get("request_provenance_sha256")
                    != provenance_sha256
                    or existing.get("recovery_probe_route") != spec["route"]
                    or existing.get("recovery_auth_incident_request_id")
                    != auth_incident_request_id
                    or existing.get("recovery_gate_sha256") != gate_sha256
                ):
                    raise GuardViolation(
                        "recovery probe deterministic reservation provenance drift"
                    )
                replay.update(
                    {
                        "result": "ambiguous_reservation",
                        "upstream_status": None,
                    }
                )
                return spec, replay

            if not self.settings.provider_egress_enabled:
                raise BudgetViolation("recovery probe provider egress is disabled")
            if self.value.get("final_maintenance_hold") is not None:
                raise BudgetViolation("final maintenance hold forbids recovery probes")
            if self.value.get("in_flight"):
                raise RecoveryProbeAmbiguity(
                    "recovery probe requires zero durable provider reservations"
                )
            try:
                incident = exact_recovery_auth_incident(
                    self.value, auth_incident_request_id
                )
            except GuardViolation as exc:
                raise BudgetViolation(str(exc)) from exc
            incident_status = incident["status_code"]
            expected_reason = f"provider returned {incident_status}"
            if (
                incident_status not in {401, 402}
                or self.value.get("circuit_open") is not True
                or str(
                    self.value.get(
                        "provider_auth_circuit_incident_request_id"
                    )
                    or ""
                )
                != auth_incident_request_id
                or self.value.get("provider_auth_circuit_incident_status")
                != incident_status
                or not hmac.compare_digest(
                    str(self.value.get("reason") or ""), expected_reason
                )
            ):
                raise BudgetViolation(
                    "recovery probe requires the exact provider-auth circuit"
                )
            active_auth_recovery = self.value.get("provider_auth_recovery")
            expected_active = {
                "version": 1,
                "auth_incident_request_id": auth_incident_request_id,
                "auth_incident_status": incident_status,
                "auth_incident_at": incident["at"],
            }
            if active_auth_recovery is not None and active_auth_recovery != expected_active:
                raise BudgetViolation(
                    "another provider-auth recovery episode is already active"
                )
            try:
                successes = successful_recovery_probe_calls(
                    self.ledger, auth_incident_request_id
                )
            except GuardViolation as exc:
                raise BudgetViolation(str(exc)) from exc
            previous_success = successes.get(str(spec["route"]))
            if previous_success is not None:
                return spec, {
                    "result": "authenticated",
                    "route": spec["route"],
                    "request_id": str(previous_success["request_id"]),
                    "provenance_sha256": str(
                        previous_success["request_provenance_sha256"]
                    ),
                    "upstream_status": 200,
                    "cost_usd": float(previous_success["cost_usd"]),
                    "reused": True,
                    "matched_existing_route": True,
                }

            input_tokens = int(spec["input_tokens"])
            output_tokens = int(spec["output_tokens"])
            cost = _token_cost(
                self.settings.prices[str(spec["model"])],
                input_tokens,
                output_tokens,
            )
            scope, period, scope_limit = self._scope_for(str(spec["endpoint"]))
            if not self.settings.budget_observation_mode:
                if projection + cost >= RECOVERY_PROBE_HARD_BUDGET_USD:
                    raise BudgetViolation(
                        "recovery probe maximum would reach the $30 hard budget"
                    )
                provider_spend = float(
                    self.ledger.get("provider_spent_usd", 0)
                )
                if provider_spend + cost > self.settings.effective_provider_limit:
                    raise BudgetViolation(
                        "recovery probe would exceed the provider safety ceiling"
                    )
                if self._scope_spend(scope, period) + cost > scope_limit:
                    raise BudgetViolation(
                        f"recovery probe would exceed the {scope} budget"
                    )
            now = datetime.now(timezone.utc)
            delay = self._rate_delay(input_tokens + output_tokens, now)
            if delay > 0:
                raise ProviderCapacityViolation(
                    "recovery probe rate guard requires bounded backoff"
                )
            reservation = {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "cost_usd": cost,
            }
            item = {
                "model": spec["model"],
                "endpoint": spec["endpoint"],
                "scope": scope,
                "period": period,
                "started_at": now.isoformat(),
                "reservation": reservation,
                "request_provenance_sha256": provenance_sha256,
                "recovery_probe_route": spec["route"],
                "recovery_auth_incident_request_id": auth_incident_request_id,
                "recovery_gate_sha256": gate_sha256,
            }
            candidate = copy.deepcopy(self.value)
            candidate["provider_auth_recovery"] = expected_active
            candidate["in_flight"][request_id] = item
            validate_state_invariants(
                candidate,
                prices=self.settings.prices,
                provider_max_in_flight=self.settings.provider_max_in_flight,
            )
            atomic_json(self.state_path, candidate)
            self.value = candidate
            return spec, None

    def hold_after_recovery_probe(self, reason: str) -> None:
        """Persist a non-clearing hold after a definitive probe outcome."""
        with self.lock:
            if self.value.get("final_maintenance_hold") is not None:
                raise BudgetViolation("final maintenance hold cannot be replaced")
            if self.value.get("in_flight"):
                raise RecoveryProbeAmbiguity(
                    "recovery probe hold requires a drained reservation"
                )
            candidate = copy.deepcopy(self.value)
            candidate.update({"circuit_open": True, "reason": reason})
            validate_state_invariants(
                candidate,
                prices=self.settings.prices,
                provider_max_in_flight=self.settings.provider_max_in_flight,
            )
            atomic_json(self.state_path, candidate)
            self.value = candidate

    def open_circuit(
        self, reason: str, *, keep_request: bool = True, request_id: str | None = None
    ) -> None:
        with self.lock:
            candidate = copy.deepcopy(self.value)
            if not keep_request and request_id:
                candidate.get("in_flight", {}).pop(request_id, None)
            candidate.update({"circuit_open": True, "reason": reason})
            atomic_json(self.state_path, candidate)
            self.value = candidate

    def enter_final_maintenance_hold(self, reason: str) -> dict[str, Any]:
        """Permanently enter the exact, drained final-backfill hold.

        This transition has deliberately narrow semantics: it cannot mask an
        existing incident circuit, cannot discard an ambiguous reservation,
        and has no inverse endpoint.  Repeating the exact request is
        restart-idempotent.
        """
        if not hmac.compare_digest(reason, FINAL_MAINTENANCE_HOLD_REASON):
            raise GuardViolation("final maintenance hold reason is not exact")
        with self.lock:
            if self.value.get("in_flight"):
                raise BudgetViolation(
                    "final maintenance hold requires zero in-flight reservations"
                )
            if self.value.get("circuit_open") and not hmac.compare_digest(
                str(self.value.get("reason") or ""), reason
            ):
                raise BudgetViolation(
                    "an existing circuit must be reconciled before the final hold"
                )
            previous = self.value.get("final_maintenance_hold")
            if previous and not hmac.compare_digest(
                str(previous.get("reason") or ""), reason
            ):
                raise BudgetViolation("final maintenance hold provenance drift")
            if not previous:
                previous = {
                    "reason": reason,
                    "entered_at": datetime.now(timezone.utc).isoformat(),
                }
            candidate = copy.deepcopy(self.value)
            candidate.update(
                {
                    "circuit_open": True,
                    "reason": reason,
                    "final_maintenance_hold": previous,
                }
            )
            atomic_json(self.state_path, candidate)
            self.value = candidate
            return dict(previous)

    def exact_reservation_manifest(self) -> dict[str, Any]:
        controls = self.recovery_control_status()
        with self.lock:
            manifest = reservation_manifest(
                dict(self.value.get("in_flight") or {}), self.settings.prices
            )
            # The recovery guard must compare the public budget accounting to
            # this exact reservation set.  Returning both from this one
            # authenticated, lock-held snapshot prevents a normal reserve or
            # record transition from being misclassified as durable drift.
            manifest["health"] = self._health_locked(controls)
            # These payload-free records are the authoritative restart proof for
            # one-shot recovery rolls.  They let the controller distinguish a
            # completed mutation from a crash before it reissues that mutation.
            manifest["last_ambiguous_reconciliation"] = self.value.get(
                "last_ambiguous_reconciliation"
            )
            manifest["last_provider_auth_reset"] = self.value.get(
                "last_provider_auth_reset"
            )
            manifest["provider_auth_recovery"] = copy.deepcopy(
                self.value.get("provider_auth_recovery")
            )
            manifest["provider_auth_circuit_incident_request_id"] = (
                self.value.get("provider_auth_circuit_incident_request_id")
            )
            manifest["provider_auth_circuit_incident_status"] = (
                self.value.get("provider_auth_circuit_incident_status")
            )
            manifest["last_record_commit_repair"] = self.value.get(
                "last_record_commit_repair"
            )
            manifest["provider_failure_history"] = list(
                self.value.get("provider_failure_history") or []
            )
            # The append-only ledger is already fully validated at startup and
            # before every mutation. Exposing its payload-free call records to
            # the separately authenticated recovery controller provides an
            # exact restart proof keyed by the reservation/Fugu session UUID.
            # This prevents a controller crash after provider success from
            # issuing a second paid preflight.
            manifest["provider_call_history"] = copy.deepcopy(
                list(self.ledger.get("calls") or [])
            )
            manifest["provider_ledger_version"] = self.ledger.get("version")
            return manifest

    def discard(self, request_id: str) -> None:
        with self.lock:
            candidate = copy.deepcopy(self.value)
            candidate.get("in_flight", {}).pop(request_id, None)
            atomic_json(self.state_path, candidate)
            self.value = candidate

    def defer_observation_settlement(self, request_id: str) -> None:
        """Remember one ended ambiguous call for retry before the next reserve."""
        if not self.settings.budget_observation_mode:
            return
        with self.lock:
            if request_id in (self.value.get("in_flight") or {}):
                self._observation_settlement_pending.add(request_id)

    def retry_pending_observation_settlements(self) -> None:
        """Retry ended ambiguities before they can consume every capacity slot.

        Failures remain retryable: callers continue to the ordinary capacity
        decision, which returns a bounded local 429 when all slots are occupied.
        A later request repeats this exact settlement pass.
        """
        if not self.settings.budget_observation_mode:
            return
        with self.lock:
            pending = sorted(self._observation_settlement_pending)
        for request_id in pending:
            try:
                self.maximum_charge_ambiguous(request_id)
            except Exception:
                continue

    def maximum_charge_ambiguous(self, request_id: str) -> dict[str, Any]:
        """Settle one unchanged ambiguous reservation without a global hold.

        The ledger is committed first.  A restart between the two durable
        replaces is repaired by ``_repair_record_commit_boundary`` using the
        same request ID and reservation lineage.
        """
        if not self.settings.budget_observation_mode:
            raise GuardViolation(
                "automatic ambiguity settlement requires budget observation mode"
            )
        with self.lock:
            # A previous attempt may have crossed the ledger-first replace and
            # failed before updating in-memory state. Reload the durable ledger
            # under the same lock so retry recognizes that exact commit instead
            # of appending a second maximum charge.
            durable_ledger = json.loads(self.ledger_path.read_text())
            validate_ledger_invariants(
                durable_ledger,
                expected_guard=self.settings.guard_fingerprint(),
                expected_initial_provider_spend_usd=(
                    self.settings.initial_provider_spend_usd
                ),
                enforce_monetary_limits=False,
            )
            self.ledger = durable_ledger
            item = (self.value.get("in_flight") or {}).get(request_id)
            if not isinstance(item, dict):
                raise GuardViolation(
                    "ambiguous reservation disappeared before maximum charge"
                )
            manifest = reservation_manifest(
                {request_id: item}, self.settings.prices
            )
            matching = [
                value
                for value in self.ledger.get("calls") or []
                if str(value.get("request_id") or "") == request_id
            ]
            if len(matching) > 1:
                raise GuardViolation(
                    "ambiguous reservation has duplicate ledger lineage"
                )
            prices = self.settings.prices[str(item["model"])]
            repaired_normal = bool(
                matching
                and exact_record_commit_matches(
                    matching[0], request_id, item, prices=prices
                )
            )
            existing_maximum = bool(
                matching
                and exact_reconciliation_commit_matches(
                    matching[0], request_id, item, prices=prices
                )
            )
            if matching and not (repaired_normal or existing_maximum):
                raise GuardViolation(
                    "ambiguous reservation ledger lineage is contradictory"
                )

            now = datetime.now(timezone.utc).isoformat()
            ledger_candidate = copy.deepcopy(self.ledger)
            charged = 0.0
            if not matching:
                call = reconciliation_call(request_id, item, at=now)
                ledger_candidate.setdefault("calls", []).append(call)
                charged = float(call["cost_usd"])
                ledger_candidate["provider_spent_usd"] = (
                    float(ledger_candidate.get("provider_spent_usd") or 0)
                    + charged
                )
                validate_ledger_invariants(
                    ledger_candidate,
                    expected_guard=self.settings.guard_fingerprint(),
                    expected_initial_provider_spend_usd=(
                        self.settings.initial_provider_spend_usd
                    ),
                    enforce_monetary_limits=False,
                )
                atomic_json(self.ledger_path, ledger_candidate)
                self.ledger = ledger_candidate

            candidate = copy.deepcopy(self.value)
            candidate["in_flight"].pop(request_id, None)
            proof = {
                "at": now,
                "requests": 1,
                "reservation_ids": [request_id],
                "reservations": manifest["reservations"],
                "manifest_sha256": manifest["sha256"],
                "total_max_usd": manifest["total_max_usd"],
                "result": (
                    "record_commits_repaired_and_drained"
                    if repaired_normal
                    else "maximum_charged_and_drained"
                ),
            }
            if repaired_normal:
                candidate["last_record_commit_repair"] = {
                    **proof,
                    "request_ids": [request_id],
                    "cleared_exact_record_ambiguity": True,
                }
            else:
                candidate["last_ambiguous_reconciliation"] = {
                    **proof,
                    "record_commit_repaired_ids": [],
                    "maximum_charged_ids": [request_id],
                    "charged_maximum_usd": float(
                        item["reservation"]["cost_usd"]
                    ),
                    "newly_charged_usd": charged,
                    "provider_spent_usd": float(
                        ledger_candidate["provider_spent_usd"]
                    ),
                }
            validate_state_invariants(
                candidate,
                prices=self.settings.prices,
                provider_max_in_flight=self.settings.provider_max_in_flight,
            )
            atomic_json(self.state_path, candidate)
            self.value = candidate
            self._observation_settlement_pending.discard(request_id)
            return {
                "request_id": request_id,
                "charged_maximum_usd": float(
                    item["reservation"]["cost_usd"]
                ),
                "newly_charged_usd": charged,
                "repaired_normal_commit": repaired_normal,
            }

    def record_provider_failure(
        self,
        request_id: str,
        status_code: int,
        *,
        retry_after: str | None,
        circuit_reason: str | None = None,
    ) -> dict[str, Any]:
        """Atomically bind one definitive HTTP failure to its Fugu lineage."""
        with self.lock:
            item = self.value.get("in_flight", {}).get(request_id)
            if not item:
                raise GuardViolation(
                    "in-flight reservation disappeared before failure recording"
                )
            if type(status_code) is not int or not 400 <= status_code <= 599:
                raise GuardViolation("provider failure status is invalid")
            model = str(item["model"])
            failure = {
                "at": datetime.now(timezone.utc).isoformat(),
                "request_id": request_id,
                "status_code": status_code,
                "retry_after": retry_after,
                "model": model,
                "endpoint": item["endpoint"],
                "scope": item["scope"],
                "period": item["period"],
                "fugu_session_id": request_id,
                "fugu_agent_id": (
                    FUGU_HINDSIGHT_AGENT_ID
                    if model == HINDSIGHT_MODEL
                    else FUGU_QWEN_AGENT_ID
                ),
                "reservation_total_tokens": int(
                    item["reservation"]["total_tokens"]
                ),
            }
            if item.get("request_provenance_sha256") is not None:
                failure["request_provenance_sha256"] = item[
                    "request_provenance_sha256"
                ]
            if item.get("recovery_probe_route") is not None:
                failure["recovery_probe_route"] = item["recovery_probe_route"]
                failure["recovery_auth_incident_request_id"] = item[
                    "recovery_auth_incident_request_id"
                ]
                failure["recovery_gate_sha256"] = item[
                    "recovery_gate_sha256"
                ]
            candidate = copy.deepcopy(self.value)
            history = candidate.setdefault("provider_failure_history", [])
            if any(
                str(value.get("request_id") or "") == request_id
                for value in history
            ):
                raise GuardViolation("provider failure request ID is duplicated")
            history.append(failure)
            candidate["in_flight"].pop(request_id, None)
            if circuit_reason is not None:
                exact_auth_failure = (
                    status_code in {401, 402}
                    and circuit_reason == f"provider returned {status_code}"
                )
                existing_auth_root = candidate.get(
                    "provider_auth_circuit_incident_request_id"
                )
                if not (exact_auth_failure and existing_auth_root is not None):
                    candidate.update(
                        {"circuit_open": True, "reason": circuit_reason}
                    )
                if exact_auth_failure and existing_auth_root is None:
                    candidate[
                        "provider_auth_circuit_incident_request_id"
                    ] = request_id
                    candidate["provider_auth_circuit_incident_status"] = (
                        status_code
                    )
            validate_state_invariants(
                candidate,
                prices=self.settings.prices,
                provider_max_in_flight=self.settings.provider_max_in_flight,
            )
            atomic_json(self.state_path, candidate)
            self.value = candidate
            return failure

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

            def exact_usage_count(value: Any, name: str) -> int:
                if type(value) is not int or value < 0:
                    raise GuardViolation(
                        f"provider-reported {name} token usage is invalid"
                    )
                return value

            input_tokens = (
                int(reservation["input_tokens"])
                if reported_input is None
                else exact_usage_count(reported_input, "input")
            )
            output_tokens = (
                int(reservation["output_tokens"])
                if reported_output is None
                else exact_usage_count(reported_output, "output")
            )
            if reported_total is not None:
                total_tokens = exact_usage_count(reported_total, "total")
                if total_tokens < input_tokens or (
                    reported_output is not None
                    and total_tokens < input_tokens + output_tokens
                ):
                    raise GuardViolation(
                        "provider-reported total token usage is inconsistent"
                    )
                output_tokens = max(
                    output_tokens,
                    total_tokens - input_tokens,
                )
            calculated = _token_cost(
                self.settings.prices[item["model"]], input_tokens, output_tokens
            )
            try:
                raw_provider_reported = usage.get("cost")
                provider_reported = (
                    0.0
                    if raw_provider_reported is None
                    else float(raw_provider_reported)
                )
            except (TypeError, ValueError) as exc:
                raise GuardViolation("provider-reported cost is invalid") from exc
            if not math.isfinite(provider_reported) or provider_reported < 0:
                raise GuardViolation("provider-reported cost is invalid")
            if raw_provider_reported is None:
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
                "provider_call_recorded": True,
                "reservation_manifest_sha256": reservation_manifest(
                    {request_id: item}, self.settings.prices
                )["sha256"],
            }
            if item.get("request_provenance_sha256") is not None:
                call["request_provenance_sha256"] = item[
                    "request_provenance_sha256"
                ]
            if item.get("recovery_probe_route") is not None:
                call["recovery_probe_route"] = item["recovery_probe_route"]
                call["recovery_auth_incident_request_id"] = item[
                    "recovery_auth_incident_request_id"
                ]
                call["recovery_gate_sha256"] = item["recovery_gate_sha256"]
            ledger_candidate = copy.deepcopy(self.ledger)
            ledger_candidate.setdefault("calls", []).append(call)
            ledger_candidate["provider_spent_usd"] = (
                float(ledger_candidate.get("provider_spent_usd", 0)) + cost
            )
            # Ledger first.  Startup finishes the exact state removal if this
            # process dies after the durable charge but before the next replace.
            atomic_json(self.ledger_path, ledger_candidate)
            self.ledger = ledger_candidate
            state_candidate = copy.deepcopy(self.value)
            state_candidate["in_flight"].pop(request_id, None)
            scope, period, scope_limit = self._scope_for(item["endpoint"])
            reservation_overrun = (
                input_tokens > int(reservation["input_tokens"])
                or output_tokens > int(reservation["output_tokens"])
            )
            monetary_overrun = (
                float(ledger_candidate["provider_spent_usd"])
                > self.settings.effective_provider_limit
                or self._scope_spend(scope, period) > scope_limit
            )
            if self.settings.budget_observation_mode and (
                reservation_overrun or monetary_overrun
            ):
                state_candidate["last_observed_budget_overrun"] = {
                    "at": datetime.now(timezone.utc).isoformat(),
                    "request_id": request_id,
                    "provider_limit_exceeded": bool(
                        float(ledger_candidate["provider_spent_usd"])
                        > self.settings.effective_provider_limit
                    ),
                    "scope_limit_exceeded": bool(
                        self._scope_spend(scope, period) > scope_limit
                    ),
                    "reservation_tokens_exceeded": reservation_overrun,
                }
            elif monetary_overrun or reservation_overrun:
                state_candidate.update(
                    {
                        "circuit_open": True,
                        "reason": "recorded provider usage exceeded its durable reservation or budget",
                    }
                )
            atomic_json(self.state_path, state_candidate)
            self.value = state_candidate
            return call

    def recovery_control_status(self) -> dict[str, Any]:
        """Read the two payload-free, per-roll one-shot control markers."""
        expected_nonce = self.settings.recovery_control_nonce
        expected_roll_sha256 = self.settings.recovery_control_roll_sha256

        def marker(name: str, expected_control: str) -> dict[str, Any]:
            path = self.state_path.parent / name
            result: dict[str, Any] = {
                "present": path.exists(),
                "valid": False,
                "nonce_matches": False,
                "roll_matches": False,
                "enabled": None,
                "secret_present": None,
                "completed": None,
            }
            if not result["present"]:
                return result
            try:
                value = json.loads(path.read_text())
            except (OSError, ValueError, json.JSONDecodeError):
                return result
            if not isinstance(value, dict) or set(value) != {
                "version",
                "control",
                "nonce",
                "roll_sha256",
                "enabled",
                "secret_present",
                "completed",
            }:
                return result
            nonce = value.get("nonce")
            roll_sha256 = value.get("roll_sha256")
            valid = (
                value.get("version") == 2
                and value.get("control") == expected_control
                and isinstance(nonce, str)
                and re.fullmatch(r"[0-9a-f]{32}", nonce) is not None
                and isinstance(roll_sha256, str)
                and re.fullmatch(r"[0-9a-f]{64}", roll_sha256) is not None
                and type(value.get("enabled")) is bool
                and type(value.get("secret_present")) is bool
                and value.get("completed") is True
                and value.get("enabled") is value.get("secret_present")
            )
            if not valid:
                return result
            result.update(
                {
                    "valid": True,
                    "nonce_matches": bool(expected_nonce)
                    and hmac.compare_digest(nonce, expected_nonce),
                    "roll_matches": bool(expected_roll_sha256)
                    and hmac.compare_digest(
                        roll_sha256, expected_roll_sha256
                    ),
                    "enabled": value["enabled"],
                    "secret_present": value["secret_present"],
                    "completed": True,
                }
            )
            return result

        reconcile = marker("reconcile-control.json", "reconcile")
        auth_reset = marker("auth-reset-control.json", "auth_reset")
        sealed_disabled = (
            bool(expected_nonce)
            and bool(expected_roll_sha256)
            and all(
                value["valid"]
                and value["nonce_matches"]
                and value["roll_matches"]
                and value["enabled"] is False
                and value["secret_present"] is False
                and value["completed"] is True
                for value in (reconcile, auth_reset)
            )
        )
        return {
            "expected_nonce_sha256": (
                hashlib.sha256(expected_nonce.encode()).hexdigest()
                if expected_nonce
                else None
            ),
            "expected_roll_sha256": expected_roll_sha256 or None,
            "reconcile": reconcile,
            "auth_reset": auth_reset,
            "sealed_disabled": sealed_disabled,
        }

    def _health_locked(self, controls: dict[str, Any]) -> dict[str, Any]:
        """Serialize health fields while the caller holds ``self.lock``."""
        month = utc_month()
        provider_spent = float(self.ledger.get("provider_spent_usd", 0))
        backfill_spent = self._scope_spend("hindsight_backfill", "all")
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
            "provider_spent_usd": provider_spent,
            "provider_effective_limit_usd": self.settings.effective_provider_limit,
            "provider_limit_exceeded": (
                provider_spent > self.settings.effective_provider_limit
            ),
            "budget_observation_mode": self.settings.budget_observation_mode,
            "hindsight_phase": self.settings.hindsight_phase,
            "provider_egress_enabled": self.settings.provider_egress_enabled,
            "hindsight_backfill_spent_usd": backfill_spent,
            "hindsight_backfill_limit_usd": (
                self.settings.hindsight_backfill_limit_usd
            ),
            "hindsight_backfill_limit_exceeded": (
                backfill_spent > self.settings.hindsight_backfill_limit_usd
            ),
            "hindsight_llm_max_concurrent": (
                self.settings.hindsight_llm_max_concurrent
            ),
            "provider_max_in_flight": self.settings.provider_max_in_flight,
            "hindsight_monthly_spent_usd": self._scope_spend(
                "hindsight_monthly", month
            ),
            "qwen_monthly_spent_usd": self._scope_spend("qwen_monthly", month),
            "month_utc": month,
            "hindsight_model": HINDSIGHT_MODEL,
            "final_maintenance_hold": self.value.get("final_maintenance_hold"),
            "last_observed_budget_overrun": self.value.get(
                "last_observed_budget_overrun"
            ),
            "recovery_control_nonce_sha256": controls["expected_nonce_sha256"],
            "recovery_control_roll_sha256": controls["expected_roll_sha256"],
            "recovery_controls_sealed_disabled": controls["sealed_disabled"],
        }

    def health(self) -> dict[str, Any]:
        controls = self.recovery_control_status()
        with self.lock:
            return self._health_locked(controls)


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

    def admin_authorized(self) -> bool:
        expected = "Bearer " + self.server.settings.admin_api_key
        return hmac.compare_digest(expected, self.headers.get("Authorization", ""))

    def recovery_probe_authorized(self) -> bool:
        expected = "Bearer " + self.server.settings.proxy_api_key
        return self.admin_authorized() and hmac.compare_digest(
            expected, self.headers.get("X-Proxy-Authorization", "")
        )

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
        if self.path.rstrip("/") == "/admin/reservations":
            if not self.admin_authorized():
                self.send_json(401, {"error": {"message": "invalid admin key"}})
                return
            self.send_json(200, self.server.state.exact_reservation_manifest())
            return
        if self.path.rstrip("/") == "/admin/recovery-controls":
            if not self.admin_authorized():
                self.send_json(401, {"error": {"message": "invalid admin key"}})
                return
            self.send_json(200, self.server.state.recovery_control_status())
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

    def handle_recovery_probe(self) -> None:
        """Run one fixed, metered provider-auth probe without releasing holds."""
        if not self.recovery_probe_authorized():
            self.send_json(401, {"error": {"message": "invalid recovery credentials"}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise GuardViolation(
                    "recovery-probe body length is outside the limit"
                )
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise GuardViolation("recovery-probe body must be a JSON object")
            spec, replay = self.server.state.reserve_recovery_probe(
                route=payload.get("route"),
                provenance=payload.get("provenance"),
                auth_incident_request_id=payload.get(
                    "auth_incident_request_id"
                ),
                agent_circuit_open=payload.get("agent_circuit_open"),
                pocket_circuit_open=payload.get("pocket_circuit_open"),
                provider_in_flight=payload.get("provider_in_flight"),
                projected_full_cost_usd=payload.get(
                    "projected_full_cost_usd"
                ),
            )
        except ProviderCapacityViolation as exc:
            body = json.dumps({"error": {"message": str(exc)}}).encode()
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Retry-After", "2")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        except RecoveryProbeAmbiguity as exc:
            self.send_json(409, {"error": {"message": str(exc)}})
            return
        except BudgetViolation as exc:
            self.send_json(409, {"error": {"message": str(exc)}})
            return
        except (GuardViolation, ValueError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": {"message": str(exc)}})
            return

        if replay is not None:
            result = str(replay["result"])
            if result == "ambiguous_reservation":
                self.send_json(409, replay)
                return
            try:
                if result == "unexpected_failure":
                    self.server.state.hold_after_recovery_probe(
                        "recovery probe returned unexpected provider status "
                        + str(replay.get("upstream_status"))
                    )
            except BudgetViolation as exc:
                self.send_json(409, {"error": {"message": str(exc)}})
                return
            self.send_json(200, replay)
            return

        request_id = str(spec["request_id"])
        committed = False
        try:
            response = httpx.post(
                self.server.settings.provider_base_url
                + str(spec["provider_path"]),
                headers={
                    "Authorization": "Bearer "
                    + self.server.settings.provider_api_key,
                    "x-session-id": request_id,
                    "x-agent-id": str(spec["agent_id"]),
                },
                json=spec["payload"],
                timeout=60,
            )
            if 300 <= response.status_code < 400:
                raise GuardViolation("unexpected recovery probe redirect")
            if response.status_code >= 400:
                retry_after = (
                    validated_retry_after(response.headers.get("retry-after"))
                    if response.status_code == 429
                    or 500 <= response.status_code <= 599
                    else None
                )
                expected_auth_failure = response.status_code in {401, 402}
                retryable_failure = response.status_code == 429 or (
                    500 <= response.status_code <= 599
                )
                self.server.state.record_provider_failure(
                    request_id,
                    response.status_code,
                    retry_after=retry_after,
                    circuit_reason=(
                        None
                        if expected_auth_failure or retryable_failure
                        else (
                            "recovery probe returned unexpected provider status "
                            + str(response.status_code)
                        )
                    ),
                )
                committed = True
                result = {
                    "result": (
                        "expected_auth_failure"
                        if expected_auth_failure
                        else (
                            "retryable_failure"
                            if retryable_failure
                            else "unexpected_failure"
                        )
                    ),
                    "route": spec["route"],
                    "request_id": request_id,
                    "provenance_sha256": spec["provenance_sha256"],
                    "upstream_status": response.status_code,
                    "retry_after": retry_after,
                    "reused": False,
                }
            else:
                try:
                    provider_body = response.json()
                except (ValueError, json.JSONDecodeError):
                    raise GuardViolation(
                        "recovery probe success body is not JSON"
                    )
                if not recovery_probe_response_is_exact(
                    str(spec["route"]), provider_body
                ):
                    raise GuardViolation(
                        "recovery probe success body has the wrong route shape"
                    )
                usage = provider_body.get("usage")
                call = self.server.state.record(
                    request_id, usage if isinstance(usage, dict) else {}
                )
                committed = True
                result = {
                    "result": "authenticated",
                    "route": spec["route"],
                    "request_id": request_id,
                    "provenance_sha256": spec["provenance_sha256"],
                    "upstream_status": response.status_code,
                    "cost_usd": float(call["cost_usd"]),
                    "reused": False,
                }
        except Exception as exc:
            if not committed:
                if self.server.settings.budget_observation_mode:
                    try:
                        self.server.state.maximum_charge_ambiguous(request_id)
                    except Exception:
                        self.server.state.defer_observation_settlement(request_id)
                else:
                    try:
                        self.server.state.open_circuit(
                            "ambiguous upstream failure: recovery probe "
                            + type(exc).__name__,
                            keep_request=True,
                            request_id=request_id,
                        )
                    except Exception:
                        pass
                self.send_json(
                    502,
                    {
                        "result": "ambiguous_reservation",
                        "route": spec["route"],
                        "request_id": request_id,
                        "provenance_sha256": spec["provenance_sha256"],
                        "upstream_status": None,
                        "reused": False,
                    },
                )
            else:
                self.send_json(
                    500,
                    {
                        "error": {
                            "message": "recovery probe committed; replay required"
                        },
                        "request_id": request_id,
                    },
                )
            return
        self.send_json(200, result)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.rstrip("/")
        if path == "/admin/recovery-probe":
            self.handle_recovery_probe()
            return
        if path == "/admin/maintenance-hold":
            if not self.admin_authorized():
                self.send_json(401, {"error": {"message": "invalid admin key"}})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096:
                    raise GuardViolation(
                        "maintenance-hold body length is outside the limit"
                    )
                payload = json.loads(self.rfile.read(length))
                hold = self.server.state.enter_final_maintenance_hold(
                    str(payload.get("reason") or "")
                )
            except BudgetViolation as exc:
                self.send_json(409, {"error": {"message": str(exc)}})
                return
            except (GuardViolation, ValueError, json.JSONDecodeError) as exc:
                self.send_json(400, {"error": {"message": str(exc)}})
                return
            self.send_json(
                200,
                {
                    "status": "circuit_open",
                    "reason": FINAL_MAINTENANCE_HOLD_REASON,
                    "hold": hold,
                },
            )
            return
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
            recovery_provenance = self.headers.get("X-Recovery-Provenance")
            if recovery_provenance is not None:
                if RECOVERY_PROVENANCE_RE.fullmatch(recovery_provenance) is None:
                    raise GuardViolation("recovery provenance header is malformed")
                request_provenance_sha256 = hashlib.sha256(
                    recovery_provenance.encode()
                ).hexdigest()
            else:
                request_provenance_sha256 = None
            request_id, reservation = self.server.state.reserve(
                endpoint,
                model,
                input_tokens,
                output_tokens,
                request_provenance_sha256=request_provenance_sha256,
            )
        except ProviderCapacityViolation as exc:
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Retry-After", "2")
            body = json.dumps({"error": {"message": str(exc)}}).encode()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        except BudgetViolation as exc:
            self.server.state.open_circuit(str(exc), keep_request=False)
            self.send_json(402, {"error": {"message": str(exc)}})
            return
        except (GuardViolation, ValueError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": {"message": str(exc)}})
            return

        committed = False
        try:
            response = httpx.post(
                self.server.settings.provider_base_url
                + ("/chat/completions" if endpoint == "chat" else "/embeddings"),
                headers={
                    "Authorization": "Bearer " + self.server.settings.provider_api_key,
                    "x-session-id": request_id,
                    "x-agent-id": (
                        FUGU_HINDSIGHT_AGENT_ID
                        if model == HINDSIGHT_MODEL
                        else FUGU_QWEN_AGENT_ID
                    ),
                },
                json=payload,
                timeout=300,
            )
            if 300 <= response.status_code < 400:
                raise GuardViolation("unexpected provider redirect")
            upstream_retry_after = (
                validated_retry_after(response.headers.get("retry-after"))
                if response.status_code == 429
                or 500 <= response.status_code <= 599
                else None
            )
            if response.status_code >= 400:
                credit_exhausted = provider_credit_exhausted(
                    response.status_code, response.content
                )
                self.server.state.record_provider_failure(
                    request_id,
                    response.status_code,
                    retry_after=upstream_retry_after,
                    circuit_reason=(
                        CREDIT_EXHAUSTED_CIRCUIT_REASON
                        if (
                            self.server.settings.budget_observation_mode
                            and credit_exhausted
                        )
                        else (
                            f"provider returned {response.status_code}"
                            if (
                                not self.server.settings.budget_observation_mode
                                and response.status_code in {401, 402}
                            )
                            else None
                        )
                    ),
                )
                committed = True
            else:
                body = response.json()
                call = self.server.state.record(request_id, body.get("usage") or {})
                committed = True
                # The provider call and both durable local commits are already
                # complete.  A closed stdout during restart must never
                # reclassify that success as an ambiguous provider incident.
                try:
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
                except Exception:
                    pass
        except Exception as exc:
            if (
                self.server.settings.budget_observation_mode
                and not committed
            ):
                try:
                    self.server.state.maximum_charge_ambiguous(request_id)
                except Exception:
                    # The unchanged reservation remains durable.  Never discard
                    # or globally stop other work merely because settlement
                    # itself must be retried after a local write failure.
                    self.server.state.defer_observation_settlement(request_id)
            elif not committed:
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
        retry_after = upstream_retry_after if response.status_code >= 400 else None
        if retry_after is not None:
            self.send_header("Retry-After", retry_after)
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
