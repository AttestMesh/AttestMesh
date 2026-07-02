from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import secrets
import signal
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import psycopg
from nio import (
    AsyncClient,
    AsyncClientConfig,
    JoinError,
    JoinResponse,
    LoginError,
    LoginResponse,
    MatrixRoom,
    RoomMessageText,
)
from openai import AsyncOpenAI

from postgres_admin_agent.formatting import matrix_text_content


LOG = logging.getLogger("postgres_admin_agent")


def _env(name: str, default: str = "", *, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise SystemExit(f"missing required environment variable: {name}")
    return value


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _mxid_localpart(mxid: str) -> str:
    if mxid.startswith("@") and ":" in mxid:
        return mxid[1:].split(":", 1)[0]
    return mxid.lstrip("@")


def _mention_aliases(mxid: str, extra_aliases: tuple[str, ...]) -> tuple[str, ...]:
    localpart = _mxid_localpart(mxid)
    aliases = [mxid, f"@{localpart}", localpart, *extra_aliases]
    out: list[str] = []
    seen: set[str] = set()
    for alias in aliases:
        alias = alias.strip()
        key = alias.lower()
        if alias and key not in seen:
            out.append(alias)
            seen.add(key)
    return tuple(out)


@dataclass(frozen=True)
class Settings:
    pg_dsn: str
    pg_password_file: str
    patroni_url: str
    patroni_username: str
    patroni_password_file: str
    matrix_homeserver_url: str
    matrix_user_id: str
    matrix_password: str
    matrix_room_id: str
    matrix_admin_mxids: tuple[str, ...]
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    prometheus_url: str = ""
    metrics_window_minutes: int = 60
    health_http_addr: str = "0.0.0.0:9100"
    response_timeout_seconds: int = 60
    llm_timeout_seconds: int = 25
    matrix_ssl_verify: bool = True
    statement_timeout_ms: int = 15_000
    row_limit: int = 50
    confirm_writes: bool = True
    announce_on_start: bool = False
    require_mention: bool = True
    mention_aliases: tuple[str, ...] = ()

    @classmethod
    def load(cls) -> "Settings":
        return cls(
            pg_dsn=_env("PG_DSN", required=True),
            pg_password_file=_env("PG_PASSWORD_FILE"),
            patroni_url=_env("PATRONI_URL").rstrip("/"),
            patroni_username=_env("PATRONI_USERNAME", "patroni"),
            patroni_password_file=_env("PATRONI_PASSWORD_FILE"),
            matrix_homeserver_url=_env("MATRIX_HOMESERVER_URL", required=True).rstrip("/"),
            matrix_user_id=_env("MATRIX_USER_ID", required=True),
            matrix_password=_env("MATRIX_PASSWORD", required=True),
            matrix_room_id=_env("MATRIX_ROOM_ID", required=True),
            matrix_admin_mxids=tuple(_csv(_env("MATRIX_ADMIN_MXIDS"))),
            llm_base_url=_env("LLM_BASE_URL"),
            llm_api_key=_env("LLM_API_KEY"),
            llm_model=_env("LLM_MODEL"),
            prometheus_url=_env("PROMETHEUS_URL").rstrip("/"),
            metrics_window_minutes=int(_env("METRICS_WINDOW_MINUTES", "60")),
            health_http_addr=_env("HEALTH_HTTP_ADDR", "0.0.0.0:9100"),
            response_timeout_seconds=int(_env("RESPONSE_TIMEOUT_SECONDS", "60")),
            llm_timeout_seconds=int(_env("LLM_TIMEOUT_SECONDS", "25")),
            matrix_ssl_verify=_bool_env("MATRIX_SSL_VERIFY", True),
            statement_timeout_ms=int(_env("STATEMENT_TIMEOUT_MS", "15000")),
            row_limit=int(_env("ROW_LIMIT", "50")),
            confirm_writes=_bool_env("CONFIRM_WRITES", True),
            announce_on_start=_bool_env("ANNOUNCE_ON_START", False),
            require_mention=_bool_env("MATRIX_REQUIRE_MENTION", True),
            mention_aliases=tuple(_csv(_env("MATRIX_MENTION_ALIASES"))),
        )

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_base_url and self.llm_api_key and self.llm_model)


@dataclass
class RuntimeState:
    started_at: float = field(default_factory=time.time)
    matrix_logged_in: bool = False
    room_joined: bool = False
    last_error: str = ""
    last_message_at: float = 0.0
    last_reply_at: float = 0.0


class Db:
    READ_PREFIXES = ("select", "with", "show", "explain", "values")
    WRITE_WORDS = re.compile(
        r"\b(insert|update|delete|merge|drop|alter|create|grant|revoke|truncate|"
        r"copy|call|do|vacuum|analyze|refresh|reindex)\b",
        re.IGNORECASE,
    )
    SECRET_WORDS = re.compile(r"\b(pg_authid|pg_shadow|rolpassword|password|passwd)\b", re.IGNORECASE)

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _connect(self) -> psycopg.Connection[Any]:
        # On pg-ha nodes the superuser password is CSK-derived and written to a shared
        # volume by the patroni entrypoint — read it lazily so agent start order doesn't
        # matter (connects simply fail with a clear error until the file appears).
        kwargs: dict[str, Any] = {"connect_timeout": 5}
        if self._settings.pg_password_file:
            kwargs["password"] = _read_secret_file(self._settings.pg_password_file)
        conn = psycopg.connect(self._settings.pg_dsn, **kwargs)
        conn.autocommit = True
        with conn.cursor() as cur:
            timeout_ms = max(1, min(300_000, int(self._settings.statement_timeout_ms)))
            cur.execute(f"SET statement_timeout = {timeout_ms}")
        return conn

    def ping(self) -> bool:
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    return cur.fetchone() == (1,)
        except Exception as exc:
            LOG.warning("db ping failed: %s", exc)
            return False

    def status(self) -> dict[str, Any]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT version()")
                version = cur.fetchone()[0]
                cur.execute("SELECT current_database()")
                current_database = cur.fetchone()[0]
                cur.execute("SELECT pg_size_pretty(pg_database_size(current_database()))")
                database_size = cur.fetchone()[0]
                cur.execute("SELECT count(*) FROM pg_database WHERE NOT datistemplate")
                database_count = cur.fetchone()[0]
                cur.execute("SELECT count(*) FROM pg_stat_activity")
                connection_count = cur.fetchone()[0]
                cur.execute(
                    "SELECT coalesce(array_agg(datname ORDER BY datname), '{}') "
                    "FROM pg_database WHERE NOT datistemplate"
                )
                databases = cur.fetchone()[0]
        return {
            "ok": True,
            "version": version,
            "current_database": current_database,
            "database_size": database_size,
            "database_count": database_count,
            "connection_count": connection_count,
            "databases": databases,
        }

    def query(self, sql: str) -> dict[str, Any]:
        sql = self._clean_sql(sql)
        if not self._is_read_only(sql):
            raise ValueError("read-only query required; use !pg exec for writes/admin SQL")
        if self.SECRET_WORDS.search(sql):
            raise ValueError("query refused because it targets password/role-secret fields")
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                columns = [desc.name for desc in cur.description or []]
                rows = cur.fetchmany(self._settings.row_limit + 1) if columns else []
        truncated = len(rows) > self._settings.row_limit
        rows = rows[: self._settings.row_limit]
        return {
            "ok": True,
            "columns": columns,
            "rows": [[_jsonable(value) for value in row] for row in rows],
            "row_count": len(rows),
            "truncated": truncated,
        }

    def execute(self, sql: str) -> dict[str, Any]:
        sql = self._clean_sql(sql)
        if self.SECRET_WORDS.search(sql):
            raise ValueError("statement refused because it targets password/role-secret fields")
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                status = cur.statusmessage
                if cur.description:
                    columns = [desc.name for desc in cur.description]
                    rows = cur.fetchmany(self._settings.row_limit + 1)
                else:
                    columns, rows = [], []
        truncated = len(rows) > self._settings.row_limit
        rows = rows[: self._settings.row_limit]
        return {
            "ok": True,
            "status": status,
            "columns": columns,
            "rows": [[_jsonable(value) for value in row] for row in rows],
            "row_count": len(rows),
            "truncated": truncated,
        }

    def _clean_sql(self, sql: str) -> str:
        sql = sql.strip()
        if sql.endswith(";"):
            sql = sql[:-1].strip()
        if not sql:
            raise ValueError("empty SQL")
        if ";" in sql:
            raise ValueError("only one SQL statement is allowed")
        return sql

    def _is_read_only(self, sql: str) -> bool:
        lowered = sql.lstrip(" \n\t(").lower()
        return lowered.startswith(self.READ_PREFIXES) and not self.WRITE_WORDS.search(sql)


class Metrics:
    SUMMARY_QUERIES = (
        ("CPU used", '100 * (1 - avg(rate(node_cpu_seconds_total{mode="idle"}[5m])))', "%"),
        ("Memory used", "100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)", "%"),
        (
            "Root disk used",
            '100 * (1 - node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"})',
            "%",
        ),
        ("Load 1m", "node_load1", ""),
        ("Containers seen", "count(container_last_seen)", ""),
    )

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def enabled(self) -> bool:
        return bool(self._settings.prometheus_url)

    def query(self, promql: str) -> dict[str, Any]:
        if not self.enabled:
            raise ValueError("Prometheus is not configured")
        promql = promql.strip()
        if not promql:
            raise ValueError("empty PromQL")
        if len(promql) > 500:
            raise ValueError("PromQL is too long")
        url = (
            self._settings.prometheus_url
            + "/api/v1/query?"
            + urllib.parse.urlencode({"query": promql})
        )
        with urllib.request.urlopen(url, timeout=2) as response:
            payload = json.loads(response.read().decode())
        if payload.get("status") != "success":
            raise ValueError(payload.get("error") or "Prometheus query failed")
        data = payload.get("data") or {}
        result = data.get("result") or []
        return {
            "ok": True,
            "result_type": data.get("resultType"),
            "result": [
                {
                    "metric": item.get("metric") or {},
                    "value": (item.get("value") or [None, None])[1],
                }
                for item in result[:10]
            ],
            "truncated": len(result) > 10,
        }

    def summary(self) -> list[tuple[str, float | None, str]]:
        out: list[tuple[str, float | None, str]] = []
        for label, promql, unit in self.SUMMARY_QUERIES:
            try:
                result = self.query(promql)
                value = result["result"][0]["value"] if result["result"] else None
                out.append((label, float(value) if value is not None else None, unit))
            except Exception:
                LOG.debug("metrics summary query failed: %s", label, exc_info=True)
                out.append((label, None, unit))
        return out


def _read_secret_file(path: str) -> str:
    try:
        with open(path, encoding="ascii") as handle:
            value = handle.read().strip()
    except OSError as exc:
        raise ValueError(f"secret file not readable yet: {path} ({exc.__class__.__name__})") from exc
    if not value:
        raise ValueError(f"secret file is empty: {path}")
    return value


class Patroni:
    """Bounded client for the node-local Patroni REST API (pg-ha nodes only).

    Health GETs (/cluster) are unauthenticated by design; unsafe POSTs (switchover)
    use the CSK-derived REST credential written by the patroni entrypoint.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def enabled(self) -> bool:
        return bool(self._settings.patroni_url)

    def cluster(self) -> dict[str, Any]:
        if not self.enabled:
            raise ValueError("Patroni is not configured on this node")
        url = self._settings.patroni_url + "/cluster"
        with urllib.request.urlopen(url, timeout=5) as response:
            return json.loads(response.read().decode())

    def switchover(self, candidate: str = "") -> str:
        if not self.enabled:
            raise ValueError("Patroni is not configured on this node")
        members = self.cluster().get("members") or []
        leader = next((m.get("name") for m in members if m.get("role") == "leader"), None)
        if not leader:
            raise ValueError("no current leader — cannot switch over (failover in progress?)")
        if candidate and candidate == leader:
            raise ValueError(f"{candidate} is already the leader")
        payload: dict[str, Any] = {"leader": leader}
        if candidate:
            payload["candidate"] = candidate
        password = _read_secret_file(self._settings.patroni_password_file)
        request = urllib.request.Request(
            self._settings.patroni_url + "/switchover",
            data=json.dumps(payload).encode(),
            headers={"content-type": "application/json"},
            method="POST",
        )
        credentials = f"{self._settings.patroni_username}:{password}".encode()
        request.add_header("authorization", "Basic " + base64.b64encode(credentials).decode())
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read().decode("utf-8", "replace").strip() or "Switchover accepted."
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise ValueError(f"Patroni switchover failed: HTTP {exc.code}: {detail}") from exc


@dataclass
class PendingExecution:
    token: str
    sql: str
    created_at: float
    kind: str = "sql"


class Llm:
    def __init__(
        self,
        settings: Settings,
        db: Db,
        metrics: Metrics,
        pending: dict[str, PendingExecution],
        patroni: Patroni | None = None,
    ) -> None:
        self._settings = settings
        self._db = db
        self._metrics = metrics
        self._pending = pending
        self._patroni = patroni
        self._client = (
            AsyncOpenAI(
                base_url=settings.llm_base_url,
                api_key=settings.llm_api_key,
                timeout=settings.llm_timeout_seconds,
            )
            if settings.llm_enabled
            else None
        )

    async def handle(self, sender: str, body: str) -> str:
        if self._client is None:
            return "LLM is not configured. Use `!pg status`, `!pg query <SQL>`, or `!pg exec <SQL>`."

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt()},
            {"role": "user", "content": body},
        ]
        tools = self._tools()
        for _ in range(5):
            response = await self._client.chat.completions.create(
                model=self._settings.llm_model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                temperature=0,
            )
            message = response.choices[0].message
            tool_calls = message.tool_calls or []
            if not tool_calls:
                return (message.content or "").strip() or "No response."
            messages.append(
                {
                    "role": "assistant",
                    "content": message.content,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.function.name,
                                "arguments": call.function.arguments,
                            },
                        }
                        for call in tool_calls
                    ],
                }
            )
            for call in tool_calls:
                result = await asyncio.to_thread(
                    self._call_tool, sender, call.function.name, call.function.arguments
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(result, default=str),
                    }
                )
        return "I hit the tool-step limit before finishing. Please narrow the request."

    def _system_prompt(self) -> str:
        if self._patroni is not None and self._patroni.enabled:
            flavor = (
                "You are a PostgreSQL administrative agent for one node of a Patroni-managed "
                "HA cluster on the AttestMesh wireguard mesh. You may inspect database status, "
                "the Patroni cluster topology (pgha_cluster), run read-only SQL, and inspect "
                "bounded machine metrics. Failover is automatic; a manual switchover requires "
                "the human to run `!pgha switchover` and confirm a token — you cannot do it. "
            )
        else:
            flavor = (
                "You are a PostgreSQL administrative agent for one standalone Postgres instance. "
                "You may inspect database status, run read-only SQL, and inspect bounded machine metrics. "
            )
        return flavor + (
            "For writes or admin SQL, "
            "call pg_execute; the runtime will stage the statement and require an explicit "
            "confirmation token before it is applied. Never reveal secrets, passwords, DSNs, "
            "tokens, or password hashes. Keep answers concise and include relevant query output."
        )

    def _tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        if self._patroni is not None and self._patroni.enabled:
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": "pgha_cluster",
                        "description": "Return the Patroni HA cluster topology: members, roles (leader/replica), states, timelines, and replication lag.",
                        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
                    },
                }
            )
        return tools + [
            {
                "type": "function",
                "function": {
                    "name": "pg_status",
                    "description": "Return server version, current database, size, database list, and connection count.",
                    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "pg_query",
                    "description": "Run one read-only SQL statement and return up to the configured row limit.",
                    "parameters": {
                        "type": "object",
                        "properties": {"sql": {"type": "string"}},
                        "required": ["sql"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "pg_execute",
                    "description": "Stage one write/admin SQL statement. The user must confirm the returned token before execution.",
                    "parameters": {
                        "type": "object",
                        "properties": {"sql": {"type": "string"}},
                        "required": ["sql"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "query_metrics",
                    "description": "Run one instant PromQL query against the node-local Prometheus.",
                    "parameters": {
                        "type": "object",
                        "properties": {"promql": {"type": "string"}},
                        "required": ["promql"],
                        "additionalProperties": False,
                    },
                },
            },
        ]

    def _call_tool(self, sender: str, name: str, raw_args: str) -> dict[str, Any]:
        try:
            args = json.loads(raw_args or "{}")
            if name == "pgha_cluster":
                if self._patroni is None or not self._patroni.enabled:
                    return {"ok": False, "error": "Patroni is not configured on this node"}
                return {"ok": True, **self._patroni.cluster()}
            if name == "pg_status":
                return self._db.status()
            if name == "pg_query":
                return self._db.query(str(args.get("sql", "")))
            if name == "pg_execute":
                return stage_execution(
                    self._settings, self._pending, sender, str(args.get("sql", "")), db=self._db
                )
            if name == "query_metrics":
                return self._metrics.query(str(args.get("promql", "")))
            return {"ok": False, "error": f"unknown tool: {name}"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


def stage_execution(
    settings: Settings,
    pending: dict[str, PendingExecution],
    sender: str,
    sql: str,
    *,
    db: Db | None = None,
) -> dict[str, Any]:
    sql = sql.strip()
    if not sql:
        raise ValueError("empty SQL")
    if settings.confirm_writes:
        token = secrets.token_hex(3)
        pending[sender] = PendingExecution(token=token, sql=sql, created_at=time.time())
        return {
            "ok": False,
            "confirmation_required": True,
            "confirm_with": f"confirm {token}",
            "sql": sql,
        }
    if db is None:
        raise ValueError("direct SQL execution requires a database handle")
    return {"confirmation_required": False, **db.execute(sql)}


class Bot:
    def __init__(self, settings: Settings, db: Db, state: RuntimeState) -> None:
        self._settings = settings
        self._db = db
        self._metrics = Metrics(settings)
        self._patroni = Patroni(settings)
        self._state = state
        self._pending: dict[str, PendingExecution] = {}
        self._llm = Llm(settings, db, self._metrics, self._pending, self._patroni)
        config = AsyncClientConfig(encryption_enabled=False, store_sync_tokens=False)
        self._client = AsyncClient(
            settings.matrix_homeserver_url,
            settings.matrix_user_id,
            config=config,
            ssl=settings.matrix_ssl_verify,
        )
        self._allow = set(settings.matrix_admin_mxids)
        self._mentions = _mention_aliases(settings.matrix_user_id, settings.mention_aliases)
        self._message_tasks: set[asyncio.Task[None]] = set()

    async def run(self) -> None:
        LOG.info("logging into Matrix as %s", self._settings.matrix_user_id)
        await self._login_with_retry()
        self._state.matrix_logged_in = True

        await self._join_with_retry()
        self._state.room_joined = True

        await self._client.sync(timeout=0, full_state=False)
        self._client.add_event_callback(self._on_message, RoomMessageText)
        LOG.info("joined room %s; syncing", self._settings.matrix_room_id)
        if self._settings.announce_on_start:
            await self._send("Postgres admin agent online. Use `!pg status` or ask a database question.")
        await self._client.sync_forever(timeout=30000, full_state=False)

    async def _login_with_retry(self) -> None:
        attempt = 0
        while True:
            attempt += 1
            try:
                response = await self._client.login(
                    self._settings.matrix_password, device_name="postgres-admin-agent"
                )
            except Exception as exc:
                delay = min(120, max(5, attempt * 5))
                self._state.last_error = (
                    f"Matrix login transport failed: {type(exc).__name__}: {exc}; "
                    f"retrying in {delay}s"
                )
                LOG.warning(self._state.last_error)
                await asyncio.sleep(delay)
                continue
            if isinstance(response, LoginResponse):
                return
            if not isinstance(response, LoginError):
                raise RuntimeError(f"unexpected Matrix login response: {type(response).__name__}")
            delay = _matrix_retry_delay(response, attempt)
            self._state.last_error = f"Matrix login failed: {response.message}; retrying in {delay}s"
            LOG.warning(self._state.last_error)
            await asyncio.sleep(delay)

    async def _join_with_retry(self) -> None:
        attempt = 0
        while True:
            attempt += 1
            try:
                response = await self._client.join(self._settings.matrix_room_id)
            except Exception as exc:
                delay = min(120, max(5, attempt * 5))
                self._state.last_error = (
                    f"Matrix room join transport failed: {type(exc).__name__}: {exc}; "
                    f"retrying in {delay}s"
                )
                LOG.warning(self._state.last_error)
                await asyncio.sleep(delay)
                continue
            if isinstance(response, JoinResponse):
                return
            if not isinstance(response, JoinError):
                raise RuntimeError(f"unexpected Matrix join response: {type(response).__name__}")
            delay = _matrix_retry_delay(response, attempt)
            self._state.last_error = f"Matrix room join failed: {response.message}; retrying in {delay}s"
            LOG.warning(self._state.last_error)
            await asyncio.sleep(delay)

    async def _on_message(self, room: MatrixRoom, event: RoomMessageText) -> None:
        if room.room_id != self._settings.matrix_room_id:
            return
        if event.sender == self._settings.matrix_user_id:
            return
        if self._allow and event.sender not in self._allow:
            LOG.debug("ignoring non-admin message from %s", event.sender)
            return
        body = event.body.strip()
        if self._settings.require_mention:
            activated = self._strip_mention(body)
            if activated is None:
                LOG.debug("ignoring untagged message from %s", event.sender)
                return
            body = activated

        task = asyncio.create_task(self._process_message(event.sender, body, event.event_id))
        self._message_tasks.add(task)
        task.add_done_callback(self._message_tasks.discard)
        task.add_done_callback(self._log_task_exception)

    def _strip_mention(self, body: str) -> str | None:
        for alias in sorted(self._mentions, key=len, reverse=True):
            escaped = re.escape(alias)
            if alias.startswith("@"):
                pattern = re.compile(rf"(?i)(^|\s){escaped}(?=$|[\s,:])")
                match = pattern.search(body)
                if match:
                    stripped = (body[: match.start()] + " " + body[match.end() :]).strip(" \t:,")
                    return stripped or "!pg help"
                continue
            pattern = re.compile(rf"(?i)^\s*{escaped}\s*[:,]\s*")
            match = pattern.match(body)
            if match:
                return body[match.end() :].strip() or "!pg help"
        return None

    def _log_task_exception(self, task: asyncio.Task[None]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            LOG.error(
                "message task failed outside normal handler",
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    async def _process_message(self, sender: str, body: str, event_id: str) -> None:
        self._state.last_message_at = time.time()
        await self._begin_feedback(event_id)
        try:
            reply = await asyncio.wait_for(
                self._handle(sender, body),
                timeout=max(1, self._settings.response_timeout_seconds),
            )
        except asyncio.TimeoutError:
            LOG.warning("message handling timed out after %ss", self._settings.response_timeout_seconds)
            reply = "Postgres admin error: request timed out."
        except Exception as exc:
            LOG.exception("message handling failed")
            reply = f"Postgres admin error: {type(exc).__name__}: {exc}"
        finally:
            await self._end_feedback()
        await self._send(reply)
        self._state.last_reply_at = time.time()

    async def _handle(self, sender: str, body: str) -> str:
        confirm = re.fullmatch(r"(?i)\s*confirm\s+([a-f0-9]{6,12})\s*", body)
        if confirm:
            return await asyncio.to_thread(self._confirm, sender, confirm.group(1).lower())

        lowered = body.lower()
        if lowered in {"!pg help", "pg help", "!pgha help", "pgha help"}:
            base = (
                "Commands: `!pg status`, `!pg metrics`, `!pg query <read-only SQL>`, `!pg exec <SQL>`, "
                "then `confirm <token>` for staged writes/admin SQL."
            )
            if self._patroni.enabled:
                base += (
                    "\nHA: `!pgha status` (cluster topology), `!pgha lag` (replication lag), "
                    "`!pgha switchover [candidate]` (staged; requires `confirm <token>`)."
                )
            return base
        if lowered in {"!pgha status", "pgha status", "!pgha cluster", "pgha cluster"}:
            return format_pgha_cluster(await asyncio.to_thread(self._patroni.cluster))
        if lowered in {"!pgha lag", "pgha lag"}:
            return format_pgha_lag(await asyncio.to_thread(self._patroni.cluster))
        if lowered.startswith("!pgha switchover") or lowered.startswith("pgha switchover"):
            if not self._patroni.enabled:
                return "Patroni is not configured on this node."
            # Split on the original body case-insensitively; splitting the original body on the
            # lowercase literal would IndexError when the user typed e.g. "Switchover".
            parts = re.split(r"(?i)switchover", body, maxsplit=1)
            candidate = parts[1].strip() if len(parts) > 1 else ""
            token = secrets.token_hex(3)
            self._pending[sender] = PendingExecution(
                token=token, sql=candidate, created_at=time.time(), kind="switchover"
            )
            target = f"to `{candidate}`" if candidate else "to the healthiest replica"
            return (
                f"Switchover {target} staged but not executed.\n"
                f"Reply `confirm {token}` within 15 minutes to apply it."
            )
        if lowered in {"!pg status", "pg status"}:
            return format_status(await asyncio.to_thread(self._db.status))
        if lowered in {"!pg metrics", "pg metrics"}:
            try:
                rows = await asyncio.wait_for(asyncio.to_thread(self._metrics.summary), timeout=12)
            except TimeoutError:
                return "Node metrics are unavailable: Prometheus query timed out."
            return format_metrics(rows)
        if lowered.startswith("!pg query "):
            result = await asyncio.to_thread(self._db.query, body[len("!pg query ") :])
            return format_query_result(result)
        if lowered.startswith("!pg exec "):
            staged = stage_execution(
                self._settings, self._pending, sender, body[len("!pg exec ") :], db=self._db
            )
            return format_staged(staged)

        try:
            return await asyncio.wait_for(
                self._llm.handle(sender, body),
                timeout=max(1, self._settings.llm_timeout_seconds + 5),
            )
        except asyncio.TimeoutError:
            return "The LLM request timed out. Direct commands like `!pg status` still work."

    def _confirm(self, sender: str, token: str) -> str:
        pending = self._pending.get(sender)
        if not pending or pending.token != token:
            return "No matching pending action for that confirmation token."
        if time.time() - pending.created_at > 900:
            self._pending.pop(sender, None)
            return "That pending action expired. Stage it again if still needed."
        self._pending.pop(sender, None)
        if pending.kind == "switchover":
            outcome = self._patroni.switchover(pending.sql)
            return f"Switchover requested: {outcome}\n" + format_pgha_cluster(self._patroni.cluster())
        result = self._db.execute(pending.sql)
        return "Executed.\n" + format_query_result(result)

    async def _send(self, body: str) -> None:
        redacted = redact(body, self._settings)
        await self._client.room_send(
            room_id=self._settings.matrix_room_id,
            message_type="m.room.message",
            content=matrix_text_content(redacted),
        )

    async def _begin_feedback(self, event_id: str) -> None:
        try:
            await self._client.room_read_markers(
                self._settings.matrix_room_id,
                fully_read_event=event_id,
                read_event=event_id,
            )
            await self._client.room_typing(self._settings.matrix_room_id, typing_state=True, timeout=30000)
        except Exception:
            LOG.debug("failed to send Matrix feedback", exc_info=True)

    async def _end_feedback(self) -> None:
        try:
            await self._client.room_typing(self._settings.matrix_room_id, typing_state=False)
        except Exception:
            LOG.debug("failed to clear Matrix typing", exc_info=True)


async def serve_health(settings: Settings, state: RuntimeState, db: Db) -> None:
    host, port_s = settings.health_http_addr.rsplit(":", 1)
    port = int(port_s)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await reader.readline()
            parts = line.decode("ascii", "replace").split()
            path = parts[1] if len(parts) >= 2 else "/"
            while True:
                header = await reader.readline()
                if not header or header == b"\r\n":
                    break
            if path != "/healthz":
                body = b"not found\n"
                writer.write(b"HTTP/1.1 404 Not Found\r\ncontent-length: 10\r\n\r\n" + body)
                await writer.drain()
                return
            db_ok = await asyncio.to_thread(db.ping)
            ok = bool(db_ok and state.matrix_logged_in and state.room_joined)
            payload = {
                "status": "ok" if ok else "starting",
                "db_ok": db_ok,
                "matrix_logged_in": state.matrix_logged_in,
                "room_joined": state.room_joined,
                "llm_configured": settings.llm_enabled,
                "metrics_configured": bool(settings.prometheus_url),
                "patroni_configured": bool(settings.patroni_url),
                "uptime_s": int(time.time() - state.started_at),
                "last_error": state.last_error,
            }
            body = (json.dumps(payload, sort_keys=True) + "\n").encode()
            status_line = b"HTTP/1.1 200 OK" if ok else b"HTTP/1.1 503 Service Unavailable"
            writer.write(
                status_line
                + b"\r\ncontent-type: application/json\r\ncontent-length: "
                + str(len(body)).encode()
                + b"\r\n\r\n"
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, host, port)
    LOG.info("health server listening on %s:%s", host, port)
    async with server:
        await server.serve_forever()


def format_status(status: dict[str, Any]) -> str:
    version = str(status["version"]).splitlines()[0]
    return (
        "Postgres is up.\n"
        f"Version: {version}\n"
        f"Current database: {status['current_database']} ({status['database_size']})\n"
        f"Databases: {', '.join(status['databases'])} ({status['database_count']})\n"
        f"Connections: {status['connection_count']}"
    )


def format_pgha_cluster(cluster: dict[str, Any]) -> str:
    members = cluster.get("members") or []
    if not members:
        return "Patroni reports no cluster members (bootstrap in progress?)."
    lines = [f"HA cluster `{cluster.get('scope', 'pg-ha')}`: {len(members)} members"]
    for member in members:
        role = member.get("role", "?")
        marker = "★" if role == "leader" else "·"
        lag = member.get("lag")
        lag_text = "" if role == "leader" or lag is None else f", lag {lag}"
        lines.append(
            f"{marker} {member.get('name', '?')}: {role}, {member.get('state', '?')}"
            f" (tl {member.get('timeline', '?')}{lag_text})"
        )
    return "\n".join(lines)


def format_pgha_lag(cluster: dict[str, Any]) -> str:
    members = cluster.get("members") or []
    replicas = [m for m in members if m.get("role") != "leader"]
    if not replicas:
        return "No replicas found."
    lines = ["Replication lag:"]
    for member in replicas:
        lag = member.get("lag")
        lag_text = "unknown" if lag in (None, "unknown") else f"{lag} bytes"
        lines.append(f"- {member.get('name', '?')} ({member.get('state', '?')}): {lag_text}")
    return "\n".join(lines)


def format_metrics(rows: list[tuple[str, float | None, str]]) -> str:
    lines = ["Node metrics:"]
    for label, value, unit in rows:
        if value is None:
            lines.append(f"- {label}: unavailable")
        elif unit == "%":
            lines.append(f"- {label}: {value:.1f}%")
        elif value >= 10:
            lines.append(f"- {label}: {value:.0f}{unit}")
        else:
            lines.append(f"- {label}: {value:.2f}{unit}")
    return "\n".join(lines)


def format_staged(result: dict[str, Any]) -> str:
    if result.get("confirmation_required"):
        return (
            "SQL staged but not executed.\n"
            f"Reply `{result['confirm_with']}` within 15 minutes to apply it.\n"
            f"Statement: `{result['sql']}`"
        )
    return format_query_result(result)


def format_query_result(result: dict[str, Any]) -> str:
    if result.get("status") and not result.get("columns"):
        return str(result["status"])
    columns = result.get("columns") or []
    rows = result.get("rows") or []
    if not columns:
        return "No row output."
    lines = [" | ".join(map(str, columns))]
    lines.append(" | ".join("---" for _ in columns))
    for row in rows:
        lines.append(" | ".join(_cell(value) for value in row))
    if result.get("truncated"):
        lines.append(f"... truncated at {len(rows)} rows")
    return "```\n" + "\n".join(lines[:80]) + "\n```"


def _cell(value: Any) -> str:
    text = str(value)
    text = text.replace("\n", " ")
    return text[:200]


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return "0x" + value.hex()
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return str(value)


def redact(text: str, settings: Settings) -> str:
    secrets_seen = [settings.pg_dsn, settings.matrix_password, settings.llm_api_key]
    # Also scrub the CSK-derived, file-based credentials on pg-ha nodes (superuser + Patroni
    # REST): they are identical across every node in the cluster, so a query like
    # pg_read_file('/pgha-secrets/...') or an echoed Patroni auth error must not reach Matrix.
    for path in (settings.pg_password_file, settings.patroni_password_file):
        if path:
            try:
                secrets_seen.append(_read_secret_file(path))
            except Exception:
                pass
    for secret in secrets_seen:
        if secret:
            text = text.replace(secret, "<redacted>")
    return text


def _matrix_retry_delay(response: Any, attempt: int) -> int:
    retry_ms = getattr(response, "retry_after_ms", None)
    if isinstance(retry_ms, int) and retry_ms > 0:
        return max(1, min(300, (retry_ms + 999) // 1000))
    return min(120, max(5, attempt * 5))


async def amain() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings.load()
    state = RuntimeState()
    db = Db(settings)
    bot = Bot(settings, db, state)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    tasks = [
        asyncio.create_task(serve_health(settings, state, db)),
        asyncio.create_task(bot.run()),
    ]
    stop_task = asyncio.create_task(stop.wait())
    done, pending = await asyncio.wait([*tasks, stop_task], return_when=asyncio.FIRST_COMPLETED)
    for task in done:
        if task is not stop_task and task.exception():
            state.last_error = str(task.exception())
            raise task.exception()
    for task in pending:
        task.cancel()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
