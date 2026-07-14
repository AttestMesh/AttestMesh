#!/usr/bin/env python3
"""Metered OpenAI-compatible proxy for the isolated Hindsight canary.

The proxy gives Hindsight the same persistent local ledger used by card and
question generation. It serializes provider calls, enforces the configured
rolling RPM/TPM limits, and fails closed after an ambiguous proxy crash.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

from canary import BudgetLedger, atomic_json, stable_key


ALLOWED_MODELS = {"openai/gpt-oss-20b", "openai/gpt-oss-120b"}


def prepare_payload(value: Any) -> tuple[dict[str, Any], int, int]:
    if not isinstance(value, dict):
        raise ValueError("request body must be a JSON object")
    payload = dict(value)
    model = str(payload.get("model") or "")
    if model not in ALLOWED_MODELS:
        raise ValueError(f"model is outside the canary allowlist: {model}")
    if payload.get("stream"):
        raise ValueError("streaming is disabled so usage can be metered")

    maximum = payload.get("max_completion_tokens", payload.get("max_tokens", 8192))
    try:
        max_completion_tokens = int(maximum)
    except (TypeError, ValueError) as exc:
        raise ValueError("completion-token ceiling must be an integer") from exc
    if max_completion_tokens <= 0 or max_completion_tokens > 8192:
        raise ValueError("completion-token ceiling must be between 1 and 8192")

    effort = payload.get("reasoning_effort", "low")
    if effort != "low":
        raise ValueError("reasoning_effort must be low")
    if payload.get("include_reasoning") not in (None, False):
        raise ValueError("include_reasoning must be false")
    response_format = payload.get("response_format")
    if isinstance(response_format, dict):
        json_schema = response_format.get("json_schema")
        if isinstance(json_schema, dict) and json_schema.get("strict") is True:
            raise ValueError("strict structured output is disabled for this canary")

    payload["reasoning_effort"] = "low"
    payload["include_reasoning"] = False
    payload["max_completion_tokens"] = max_completion_tokens
    payload.pop("max_tokens", None)
    prompt_bytes = len(
        json.dumps(payload.get("messages") or [], ensure_ascii=False).encode("utf-8")
    )
    return payload, prompt_bytes, max_completion_tokens


class ProxyState:
    def __init__(self, ledger_path: Path, state_path: Path) -> None:
        self.ledger = BudgetLedger(ledger_path)
        self.state_path = state_path
        self.lock = threading.Lock()
        self.value: dict[str, Any] = (
            json.loads(state_path.read_text())
            if state_path.exists()
            else {"circuit_open": False, "in_flight": None}
        )
        if self.value.get("in_flight"):
            self.value["circuit_open"] = True
            self.value["reason"] = "ambiguous request found after proxy restart"
            atomic_json(self.state_path, self.value)

    def save(self) -> None:
        atomic_json(self.state_path, self.value)


class CanaryProxy(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], state: ProxyState) -> None:
        super().__init__(address, ProxyHandler)
        self.state = state
        self.upstream = os.getenv(
            "CANARY_REDPILL_BASE_URL", "https://api.redpill.ai/v1"
        ).rstrip("/")
        self.api_key = os.environ["CANARY_REDPILL_API_KEY"].strip()


class ProxyHandler(BaseHTTPRequestHandler):
    server: CanaryProxy

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
        expected = "Bearer " + self.server.api_key
        supplied = self.headers.get("Authorization", "")
        return hmac.compare_digest(expected, supplied)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") == "/health":
            state = self.server.state
            self.send_json(
                200,
                {
                    "status": "circuit_open"
                    if state.value.get("circuit_open")
                    else "healthy",
                    "spent_usd": state.ledger.data.get("spent_usd", 0),
                },
            )
            return
        if self.path.rstrip("/") == "/v1/models":
            if not self.authorized():
                self.send_json(401, {"error": {"message": "invalid canary key"}})
                return
            self.send_json(
                200,
                {
                    "object": "list",
                    "data": [
                        {"id": model, "object": "model"}
                        for model in sorted(ALLOWED_MODELS)
                    ],
                },
            )
            return
        self.send_json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") != "/v1/chat/completions":
            self.send_json(404, {"error": {"message": "not found"}})
            return
        if not self.authorized():
            self.send_json(401, {"error": {"message": "invalid canary key"}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 4_000_000:
                raise ValueError("request body length is outside the canary limit")
            payload, prompt_bytes, max_tokens = prepare_payload(
                json.loads(self.rfile.read(length))
            )
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": {"message": str(exc)}})
            return

        state = self.server.state
        model = str(payload["model"])
        request_hash = stable_key(payload)
        with state.lock:
            if state.value.get("circuit_open"):
                self.send_json(
                    402,
                    {"error": {"message": str(state.value.get("reason") or "circuit open")}},
                )
                return
            try:
                reservation = state.ledger.reserve(model, prompt_bytes, max_tokens)
                state.ledger.wait_for_rate_capacity(reservation)
            except RuntimeError as exc:
                state.value.update({"circuit_open": True, "reason": str(exc)})
                state.save()
                self.send_json(402, {"error": {"message": str(exc)}})
                return

            started_at = __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ).isoformat()
            state.value["in_flight"] = {
                "request_hash": request_hash,
                "model": model,
                "started_at": started_at,
                "reservation": reservation,
            }
            state.save()
            try:
                response = httpx.post(
                    self.server.upstream + "/chat/completions",
                    headers={"Authorization": "Bearer " + self.server.api_key},
                    json=payload,
                    timeout=300,
                )
                if response.status_code in {401, 402}:
                    state.value.update(
                        {
                            "circuit_open": True,
                            "reason": f"Redpill returned {response.status_code}",
                        }
                    )
                    state.save()
                elif response.status_code < 400:
                    body = response.json()
                    state.ledger.record(
                        model,
                        body.get("usage") or {},
                        f"hindsight:{request_hash[:16]}",
                        reservation,
                        started_at,
                    )
                    state.value["in_flight"] = None
                    state.save()
                else:
                    state.value["in_flight"] = None
                    state.save()
            except Exception as exc:
                state.value.update(
                    {
                        "circuit_open": True,
                        "reason": f"ambiguous upstream failure: {type(exc).__name__}",
                    }
                )
                state.save()
                self.send_json(502, {"error": {"message": state.value["reason"]}})
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
    server = CanaryProxy((args.host, args.port), ProxyState(args.ledger, args.state))
    server.serve_forever(poll_interval=0.5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
