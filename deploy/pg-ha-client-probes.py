#!/usr/bin/env python3
"""Exercise every live pg-ha consumer and enforce the client recovery SLO."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client

Probe = Callable[[], Awaitable[None]]


def _http_json(url: str, headers: dict[str, str] | None = None) -> object:
    request_headers = {"User-Agent": "curl/8.0"}
    request_headers.update(headers or {})
    request = urllib.request.Request(url, headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            if response.status != 200:
                raise RuntimeError(f"{url} returned HTTP {response.status}")
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        # urllib otherwise hides the application response body behind a generic
        # HTTPError. Keep this bounded so a failed gate says which database path
        # failed without allowing an untrusted response to flood the JSONL log.
        body = exc.read(2048).decode(errors="replace").replace("\n", " ").strip()
        raise RuntimeError(f"{url} returned HTTP {exc.code}: {body[:1000]}") from exc


async def hindsight() -> None:
    body = await asyncio.to_thread(
        _http_json,
        os.environ["PROBE_HINDSIGHT_URL"] + "/v1/default/banks",
        {"Authorization": "Bearer " + os.environ["PROBE_HINDSIGHT_TOKEN"]},
    )
    if not isinstance(body, (dict, list)):
        raise RuntimeError("Hindsight bank listing returned an unexpected payload")


async def synclave() -> None:
    body = await asyncio.to_thread(_http_json, os.environ["PROBE_SYNCLAVE_URL"] + "/api/v1/healthz")
    if body.get("db") != "up":
        raise RuntimeError(f"Synclave database state is {body.get('db')!r}")


async def fugu(url: str) -> None:
    body = await asyncio.to_thread(
        _http_json,
        url + "/fugu/api/summary?window=5h&model=grok-4.5",
        {"Authorization": "Bearer " + os.environ["PROBE_FUGU_API_KEY"]},
    )
    if not isinstance(body, dict):
        raise RuntimeError("Fugu summary returned an unexpected payload")


async def streamable_tool(url: str, tool: str) -> None:
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, {})
            if getattr(result, "isError", False):
                raise RuntimeError(f"MCP tool {tool} returned an error")


async def sse_tool(url: str, tool: str) -> None:
    async with sse_client(url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, {})
            if getattr(result, "isError", False):
                raise RuntimeError(f"MCP tool {tool} returned an error")


async def probe_once(probes: dict[str, Probe]) -> dict[str, str | None]:
    names = list(probes)
    timeout = float(os.environ.get("PROBE_OPERATION_TIMEOUT", "12"))
    results = await asyncio.gather(
        *(asyncio.wait_for(probes[name](), timeout=timeout) for name in names),
        return_exceptions=True,
    )
    return {
        name: None if not isinstance(result, BaseException) else f"{type(result).__name__}: {result}"
        for name, result in zip(names, results, strict=True)
    }


async def main() -> int:
    probes: dict[str, Probe] = {
        "hindsight": hindsight,
        "telegram_fts": lambda: streamable_tool(os.environ["PROBE_TELEGRAM_MCP_URL"], "archive_stats"),
        "agent_session_mcp": lambda: streamable_tool(os.environ["PROBE_AGENT_MCP_URL"], "stats"),
        "pocket_mcp": lambda: sse_tool(os.environ["PROBE_POCKET_MCP_URL"], "stats"),
        "synclave": synclave,
        "fugu_lb": lambda: fugu(os.environ["PROBE_FUGU_LB_URL"]),
        "fugu_blue": lambda: fugu(os.environ["PROBE_FUGU_BLUE_URL"]),
        "fugu_green": lambda: fugu(os.environ["PROBE_FUGU_GREEN_URL"]),
    }
    selected = [name.strip() for name in os.environ.get("PROBE_NAMES", "").split(",") if name.strip()]
    if selected:
        unknown = sorted(set(selected) - set(probes))
        if unknown:
            raise ValueError(f"unknown probe names: {', '.join(unknown)}")
        probes = {name: probes[name] for name in selected}
    duration = float(os.environ.get("PROBE_DURATION", "0"))
    interval = float(os.environ.get("PROBE_INTERVAL", "1"))
    max_recovery = float(os.environ.get("PROBE_MAX_RECOVERY", "10"))
    deadline = time.monotonic() + duration
    failures = {name: 0 for name in probes}
    failure_started: dict[str, float | None] = {name: None for name in probes}
    max_outage = {name: 0.0 for name in probes}

    while True:
        iteration_started = time.monotonic()
        results = await probe_once(probes)
        now = time.monotonic()
        print(json.dumps({"ts": time.time(), "results": results}), flush=True)
        for name, error in results.items():
            if error is None:
                if failure_started[name] is not None:
                    max_outage[name] = max(max_outage[name], now - failure_started[name])
                    failure_started[name] = None
            else:
                failures[name] += 1
                if failure_started[name] is None:
                    failure_started[name] = now
        if duration <= 0 or now >= deadline:
            break
        await asyncio.sleep(max(0.0, interval - (time.monotonic() - iteration_started)))

    failed = False
    for name in probes:
        if failure_started[name] is not None:
            max_outage[name] = max(max_outage[name], time.monotonic() - failure_started[name])
        if failures[name] > 1 or failure_started[name] is not None or max_outage[name] > max_recovery:
            failed = True
    print(json.dumps({"summary": {"failures": failures, "max_outage_seconds": max_outage}}), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
