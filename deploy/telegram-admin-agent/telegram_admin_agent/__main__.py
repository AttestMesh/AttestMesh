from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import signal
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

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
from telethon import TelegramClient
from telethon.errors import (
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)

from telegram_admin_agent.formatting import matrix_text_content


LOG = logging.getLogger("telegram_admin_agent")


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
    tg_api_id: int
    tg_api_hash: str
    tg_phone: str
    tg_session_path: str
    sync_health_url: str
    control_dir: str
    matrix_homeserver_url: str
    matrix_user_id: str
    matrix_password: str
    matrix_room_id: str
    matrix_admin_mxids: tuple[str, ...]
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    egress_canary: str = "1.1.1.1:443"
    health_http_addr: str = "0.0.0.0:9100"
    response_timeout_seconds: int = 60
    llm_timeout_seconds: int = 25
    matrix_ssl_verify: bool = True
    announce_on_start: bool = False
    require_mention: bool = True
    mention_aliases: tuple[str, ...] = ()

    @classmethod
    def load(cls) -> "Settings":
        return cls(
            tg_api_id=int(_env("TG_API_ID", required=True)),
            tg_api_hash=_env("TG_API_HASH", required=True),
            tg_phone=_env("TG_PHONE"),
            tg_session_path=_env("TG_SESSION_PATH", "/data/telegram_session"),
            sync_health_url=_env("SYNC_HEALTH_URL", "http://telegram-sync:8082/health").rstrip("/"),
            control_dir=_env("CONTROL_DIR", "/control"),
            matrix_homeserver_url=_env("MATRIX_HOMESERVER_URL", required=True).rstrip("/"),
            matrix_user_id=_env("MATRIX_USER_ID", required=True),
            matrix_password=_env("MATRIX_PASSWORD", required=True),
            matrix_room_id=_env("MATRIX_ROOM_ID", required=True),
            matrix_admin_mxids=tuple(_csv(_env("MATRIX_ADMIN_MXIDS"))),
            llm_base_url=_env("LLM_BASE_URL"),
            llm_api_key=_env("LLM_API_KEY"),
            llm_model=_env("LLM_MODEL"),
            egress_canary=_env("EGRESS_CANARY", "1.1.1.1:443"),
            health_http_addr=_env("HEALTH_HTTP_ADDR", "0.0.0.0:9100"),
            response_timeout_seconds=int(_env("RESPONSE_TIMEOUT_SECONDS", "60")),
            llm_timeout_seconds=int(_env("LLM_TIMEOUT_SECONDS", "25")),
            matrix_ssl_verify=_bool_env("MATRIX_SSL_VERIFY", True),
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
    egress_locked: bool = False
    llm_ok: bool | None = None
    tg_authorized: bool | None = None
    last_error: str = ""
    last_message_at: float = 0.0
    last_reply_at: float = 0.0
    # Secrets learned at runtime (submitted 2FA passwords / login codes) that must
    # never echo back into the room.
    runtime_secrets: list[str] = field(default_factory=list)


class SyncService:
    """View of the co-located telegram-sync container via its health endpoint and
    the shared control volume (pause file + wrapper status lines)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def _pause_path(self) -> str:
        return os.path.join(self._settings.control_dir, "pause")

    @property
    def _status_path(self) -> str:
        return os.path.join(self._settings.control_dir, "status")

    @property
    def _provision_path(self) -> str:
        return os.path.join(self._settings.control_dir, "pg-provision.status")

    def paused(self) -> bool:
        return os.path.exists(self._pause_path)

    def wrapper_status(self) -> str:
        try:
            with open(self._status_path, encoding="utf-8", errors="replace") as handle:
                return handle.read().strip()
        except OSError:
            return "unknown (no status file yet)"

    def provision_status(self) -> str:
        try:
            with open(self._provision_path, encoding="utf-8", errors="replace") as handle:
                lines = [line.strip() for line in handle if line.strip()]
            return lines[-1] if lines else "no entries"
        except OSError:
            return "unknown (no provision status yet)"

    def health(self) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(self._settings.sync_health_url, timeout=5) as response:
                payload = json.loads(response.read().decode())
            return {"reachable": True, **payload}
        except Exception as exc:
            return {"reachable": False, "error": f"{type(exc).__name__}: {exc}"}

    def pause(self) -> None:
        with open(self._pause_path, "w", encoding="utf-8") as handle:
            handle.write(str(int(time.time())) + "\n")

    def resume(self) -> None:
        try:
            os.remove(self._pause_path)
        except FileNotFoundError:
            pass

    async def pause_and_wait(self, timeout_s: float = 60.0) -> dict[str, Any]:
        """Pause the sync wrapper and wait until it reports the daemon has exited,
        so the Telethon session file is free for the agent to use."""
        self.pause()
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            status = self.wrapper_status()
            if status.startswith("paused"):
                return {"ok": True, "paused": True, "status": status}
            await asyncio.sleep(1.5)
        return {
            "ok": False,
            "paused": self.paused(),
            "status": self.wrapper_status(),
            "error": f"sync did not report paused within {int(timeout_s)}s",
        }


@dataclass
class PendingLogin:
    client: TelegramClient
    phone: str
    phone_code_hash: str
    created_at: float
    phase: str = "code"  # code -> password


class TelegramAuth:
    """Drives the Telethon session lifecycle for the co-located sync daemon.

    The session file is shared with telegram-sync, so every operation that opens
    it first pauses the sync wrapper (SQLite sessions must not be shared between
    live clients). Login state (client + phone_code_hash) is held in memory
    between begin_login and submit_code/submit_password.
    """

    def __init__(self, settings: Settings, sync: SyncService, state: RuntimeState) -> None:
        self._settings = settings
        self._sync = sync
        self._state = state
        self._lock = asyncio.Lock()
        self._pending: PendingLogin | None = None

    def _new_client(self) -> TelegramClient:
        return TelegramClient(
            self._settings.tg_session_path,
            self._settings.tg_api_id,
            self._settings.tg_api_hash,
        )

    async def _drop_pending(self) -> None:
        if self._pending is not None:
            try:
                await self._pending.client.disconnect()
            except Exception:
                LOG.debug("pending client disconnect failed", exc_info=True)
            self._pending = None

    def pending_phase(self) -> str:
        if self._pending is None:
            return ""
        age = int(time.time() - self._pending.created_at)
        return f"{self._pending.phase} (started {age}s ago)"

    async def status(self) -> dict[str, Any]:
        async with self._lock:
            out: dict[str, Any] = {
                "sync_paused": self._sync.paused(),
                "sync_status": self._sync.wrapper_status(),
                "pending_login": self.pending_phase() or None,
            }
            health = await asyncio.to_thread(self._sync.health)
            out["sync_health"] = health
            if self._pending is not None:
                # The pending client owns the session right now — report from it.
                try:
                    out["authorized"] = await self._pending.client.is_user_authorized()
                except Exception as exc:
                    out["authorized"] = None
                    out["error"] = f"{type(exc).__name__}: {exc}"
            elif health.get("reachable") and health.get("connected"):
                # The daemon is up and connected — the session is authorized.
                out["authorized"] = True
            elif self._sync.wrapper_status().startswith("paused"):
                # Session file is free ONLY once the wrapper reports paused (i.e. the
                # daemon actually exited) — the pause file existing is not enough, the
                # daemon may still hold the SQLite session for up to a loop iteration.
                client = self._new_client()
                try:
                    await client.connect()
                    authorized = await client.is_user_authorized()
                    out["authorized"] = authorized
                    if authorized:
                        me = await client.get_me()
                        if me is not None:
                            out["user"] = {
                                "id": me.id,
                                "username": me.username,
                                "first_name": me.first_name,
                            }
                except Exception as exc:
                    out["authorized"] = None
                    out["error"] = f"{type(exc).__name__}: {exc}"
                finally:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
            else:
                # Daemon is between restarts; infer from the wrapper status line.
                out["authorized"] = False if "waiting-auth" in out["sync_status"] else None
            if isinstance(out.get("authorized"), bool):
                self._state.tg_authorized = out["authorized"]
            return out

    async def begin_login(self, phone: str = "") -> dict[str, Any]:
        phone = (phone or self._settings.tg_phone).strip()
        if not phone:
            return {
                "ok": False,
                "error": "no phone number: pass one, e.g. `!tg login +15551234567`",
            }
        async with self._lock:
            await self._drop_pending()
            paused = await self._sync.pause_and_wait()
            if not paused["ok"]:
                # Couldn't confirm the daemon stopped — don't leave sync parked; the
                # login never started so resume it and surface the failure.
                self._sync.resume()
                return {"ok": False, **paused}
            client = self._new_client()
            try:
                await client.connect()
                if await client.is_user_authorized():
                    await client.disconnect()
                    self._sync.resume()
                    self._state.tg_authorized = True
                    return {
                        "ok": True,
                        "already_authorized": True,
                        "note": "session is already authorized; sync resumed. Use `!tg logout` first to re-auth.",
                    }
                sent = await client.send_code_request(phone)
            except Exception as exc:
                try:
                    await client.disconnect()
                except Exception:
                    pass
                self._sync.resume()
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            self._pending = PendingLogin(
                client=client,
                phone=phone,
                phone_code_hash=sent.phone_code_hash,
                created_at=time.time(),
            )
            return {
                "ok": True,
                "code_sent": True,
                "phone": phone,
                "via": type(sent.type).__name__ if sent.type else "unknown",
                "note": "reply with `!tg code <code>` (sync stays paused until login finishes or `!tg cancel`)",
            }

    async def submit_code(self, code: str) -> dict[str, Any]:
        code = re.sub(r"[\s-]", "", code)
        if not code:
            return {"ok": False, "error": "empty code"}
        async with self._lock:
            if self._pending is None:
                return {"ok": False, "error": "no login in progress — start with `!tg login [phone]`"}
            if self._pending.phase != "code":
                return {"ok": False, "error": "a 2FA password is expected now: `!tg password <password>`"}
            self._state.runtime_secrets.append(code)
            try:
                me = await self._pending.client.sign_in(
                    phone=self._pending.phone,
                    code=code,
                    phone_code_hash=self._pending.phone_code_hash,
                )
            except SessionPasswordNeededError:
                self._pending.phase = "password"
                return {
                    "ok": True,
                    "needs_password": True,
                    "note": "two-step verification is on — reply with `!tg password <password>` (deterministic command; it never reaches the LLM)",
                }
            except PhoneCodeInvalidError:
                return {"ok": False, "error": "invalid code — try `!tg code <code>` again"}
            except PhoneCodeExpiredError:
                await self._drop_pending()
                self._sync.resume()
                return {"ok": False, "error": "code expired — start over with `!tg login`"}
            except Exception as exc:
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            return await self._finish_login(me)

    async def submit_password(self, password: str) -> dict[str, Any]:
        if not password:
            return {"ok": False, "error": "empty password"}
        async with self._lock:
            if self._pending is None:
                return {"ok": False, "error": "no login in progress — start with `!tg login [phone]`"}
            if self._pending.phase != "password":
                return {"ok": False, "error": "no 2FA password expected right now"}
            self._state.runtime_secrets.append(password)
            try:
                me = await self._pending.client.sign_in(password=password)
            except Exception as exc:
                return {"ok": False, "error": f"{type(exc).__name__}: {exc} — retry `!tg password <password>`"}
            return await self._finish_login(me)

    async def _finish_login(self, me: Any) -> dict[str, Any]:
        user = {
            "id": getattr(me, "id", None),
            "username": getattr(me, "username", None),
            "first_name": getattr(me, "first_name", None),
        }
        await self._drop_pending()
        self._sync.resume()
        self._state.tg_authorized = True
        return {
            "ok": True,
            "authorized": True,
            "user": user,
            "note": "session saved; sync resumed and will backfill shortly",
        }

    async def logout(self) -> dict[str, Any]:
        async with self._lock:
            await self._drop_pending()
            paused = await self._sync.pause_and_wait()
            if not paused["ok"]:
                self._sync.resume()
                return {"ok": False, **paused}
            client = self._new_client()
            try:
                await client.connect()
                if await client.is_user_authorized():
                    await client.log_out()
                    note = "logged out; the server-side session is revoked"
                else:
                    note = "session was not authorized; nothing to revoke"
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
            except Exception as exc:
                try:
                    await client.disconnect()
                except Exception:
                    pass
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            # Remove any leftover session file so the next login starts clean.
            for suffix in (".session", ".session-journal"):
                try:
                    os.remove(self._settings.tg_session_path + suffix)
                except FileNotFoundError:
                    pass
            self._state.tg_authorized = False
            return {
                "ok": True,
                "logged_out": True,
                "note": note + "; sync left PAUSED — `!tg login` to re-auth",
            }

    async def cancel_login(self) -> dict[str, Any]:
        async with self._lock:
            had = self._pending is not None
            await self._drop_pending()
            self._sync.resume()
            return {"ok": True, "cancelled": had, "note": "sync resumed"}


@dataclass
class PendingAction:
    token: str
    action: str
    created_at: float


class Llm:
    def __init__(
        self,
        settings: Settings,
        auth: TelegramAuth,
        sync: SyncService,
        state: RuntimeState,
        pending: dict[str, PendingAction],
    ) -> None:
        self._settings = settings
        self._auth = auth
        self._sync = sync
        self._state = state
        self._pending = pending
        self._client = (
            AsyncOpenAI(
                base_url=settings.llm_base_url,
                api_key=settings.llm_api_key,
                timeout=settings.llm_timeout_seconds,
            )
            if settings.llm_enabled
            else None
        )

    async def probe(self) -> None:
        """Lightweight reachability check so /healthz can report llm_ok (the TEE
        blocks logs; a dead route/key must be observable). Uses GET /models rather
        than a chat completion — the redpill TEE model's cold first inference can
        exceed the client timeout and give a false negative, whereas /models is cheap
        and fast. Real tool-calls in handle() set llm_ok=True on success regardless."""
        if self._client is None:
            self._state.llm_ok = None
            return
        try:
            await self._client.models.list()
            self._state.llm_ok = True
        except Exception as exc:
            self._state.llm_ok = False
            self._state.last_error = f"LLM probe failed: {type(exc).__name__}: {exc}"
            LOG.warning(self._state.last_error)

    async def handle(self, sender: str, body: str) -> str:
        if self._client is None:
            return "LLM is not configured. Use `!tg status`, `!tg login`, `!tg code <code>`."

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
            self._state.llm_ok = True
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
                result = await self._call_tool(sender, call.function.name, call.function.arguments)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(result, default=str),
                    }
                )
        return "I hit the tool-step limit before finishing. Please narrow the request."

    def _system_prompt(self) -> str:
        return (
            "You are the Telegram-sync administrative agent on an AttestMesh node. "
            "The node runs a headless daemon that syncs the operator's Telegram account "
            "into Postgres; you manage its Telethon session: check auth state, run the "
            "login flow (send code -> submit code -> maybe 2FA password), log out for "
            "re-auth, and pause/resume the sync daemon. Login flow: tg_begin_login sends "
            "a code to the operator's Telegram app/SMS; ask them for the code, then call "
            "tg_submit_code. If two-step verification is enabled, tell the operator to "
            "send `!tg password <password>` themselves — you have no password tool and "
            "must never ask them to give the 2FA password to you in plain conversation. "
            "Logging out revokes the session and interrupts syncing until re-auth, so it "
            "is staged behind a confirmation token. Never reveal secrets, API hashes, "
            "codes, passwords, or session contents. Keep answers concise."
        )

    def _tools(self) -> list[dict[str, Any]]:
        def tool(name: str, description: str, params: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
            return {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": {
                        "type": "object",
                        "properties": params or {},
                        **({"required": required} if required else {}),
                        "additionalProperties": False,
                    },
                },
            }

        return [
            tool("tg_status", "Auth + sync daemon status: authorized?, paused?, wrapper state, sync health/row counts."),
            tool("tg_sync_health", "Raw health JSON from the sync daemon (connection state, chats/messages counts, last reconcile)."),
            tool(
                "tg_begin_login",
                "Start Telegram login: pauses sync and sends a login code to the phone (defaults to the configured one).",
                {"phone": {"type": "string", "description": "phone number in international format, optional"}},
            ),
            tool(
                "tg_submit_code",
                "Submit the login code the operator received from Telegram.",
                {"code": {"type": "string"}},
                ["code"],
            ),
            tool("tg_logout", "Stage a logout (revokes the Telegram session). Returns a token the operator must confirm."),
            tool("tg_cancel_login", "Abort an in-progress login and resume the sync daemon."),
            tool("tg_pause_sync", "Pause the sync daemon (frees the session file)."),
            tool("tg_resume_sync", "Resume the sync daemon."),
        ]

    async def _call_tool(self, sender: str, name: str, raw_args: str) -> dict[str, Any]:
        try:
            args = json.loads(raw_args or "{}")
            if name == "tg_status":
                return {"ok": True, **(await self._auth.status())}
            if name == "tg_sync_health":
                return {"ok": True, **(await asyncio.to_thread(self._sync.health))}
            if name == "tg_begin_login":
                return await self._auth.begin_login(str(args.get("phone", "")))
            if name == "tg_submit_code":
                return await self._auth.submit_code(str(args.get("code", "")))
            if name == "tg_logout":
                return stage_action(self._pending, sender, "logout")
            if name == "tg_cancel_login":
                return await self._auth.cancel_login()
            if name == "tg_pause_sync":
                return await self._sync.pause_and_wait()
            if name == "tg_resume_sync":
                self._sync.resume()
                return {"ok": True, "resumed": True}
            return {"ok": False, "error": f"unknown tool: {name}"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


def stage_action(pending: dict[str, PendingAction], sender: str, action: str) -> dict[str, Any]:
    token = secrets.token_hex(3)
    pending[sender] = PendingAction(token=token, action=action, created_at=time.time())
    return {
        "ok": False,
        "confirmation_required": True,
        "confirm_with": f"confirm {token}",
        "action": action,
    }


class Bot:
    def __init__(self, settings: Settings, auth: TelegramAuth, sync: SyncService, state: RuntimeState) -> None:
        self._settings = settings
        self._auth = auth
        self._sync = sync
        self._state = state
        self._pending: dict[str, PendingAction] = {}
        self._llm = Llm(settings, auth, sync, state, self._pending)
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

    @property
    def llm(self) -> Llm:
        return self._llm

    async def run(self) -> None:
        LOG.info("logging into Matrix as %s", self._settings.matrix_user_id)
        await self._login_with_retry()
        self._state.matrix_logged_in = True

        await self._join_with_retry()
        self._state.room_joined = True

        if not self._allow:
            self._state.last_error = "MATRIX_ADMIN_MXIDS empty — refusing all senders (misconfigured)"
            LOG.warning(self._state.last_error)

        await self._client.sync(timeout=0, full_state=False)
        self._client.add_event_callback(self._on_message, RoomMessageText)
        LOG.info("joined room %s; syncing", self._settings.matrix_room_id)
        if self._settings.announce_on_start:
            await self._send("Telegram admin agent online. Use `!tg status` or just ask.")
        await self._client.sync_forever(timeout=30000, full_state=False)

    async def _login_with_retry(self) -> None:
        attempt = 0
        while True:
            attempt += 1
            try:
                response = await self._client.login(
                    self._settings.matrix_password, device_name="telegram-admin-agent"
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
        # Fail CLOSED: an empty allowlist denies everyone (never "allow anyone in the
        # room"). The driver always seals a non-empty MATRIX_ADMIN_MXIDS; an empty one
        # is a misconfiguration, surfaced on /healthz via last_error at startup.
        if event.sender not in self._allow:
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
                    return stripped or "!tg help"
                continue
            pattern = re.compile(rf"(?i)^\s*{escaped}\s*[:,]\s*")
            match = pattern.match(body)
            if match:
                return body[match.end() :].strip() or "!tg help"
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
            reply = "Telegram admin error: request timed out."
        except Exception as exc:
            LOG.exception("message handling failed")
            reply = f"Telegram admin error: {type(exc).__name__}: {exc}"
        finally:
            await self._end_feedback()
        await self._send(reply)
        self._state.last_reply_at = time.time()

    def _help_text(self) -> str:
        return (
            "Telegram-sync admin. Auth: `!tg login [phone]` -> `!tg code <code>` "
            "(-> `!tg password <2FA password>` if prompted). Re-auth: `!tg logout` "
            "(staged; needs `confirm <token>`), then `!tg login`.\n"
            "Also: `!tg status`, `!tg health`, `!tg pause`, `!tg resume`, `!tg cancel`, "
            "`!tg llm`. Free-text (not starting with `!tg`) goes to the LLM."
        )

    async def _handle(self, sender: str, body: str) -> str:
        confirm = re.fullmatch(r"(?i)\s*confirm\s+([a-f0-9]{6,12})\s*", body)
        if confirm:
            return await self._confirm(sender, confirm.group(1).lower())

        # Normalize internal whitespace for command ROUTING only; arguments (codes,
        # 2FA passwords which may contain spaces) are still extracted from the
        # original body so they survive verbatim.
        lowered = re.sub(r"\s+", " ", body).strip().lower()
        if lowered in {"!tg help", "tg help", "help"}:
            return self._help_text()
        if lowered in {"!tg status", "tg status"}:
            return format_status(await self._auth.status(), self._sync)
        if lowered in {"!tg health", "tg health"}:
            health = await asyncio.to_thread(self._sync.health)
            return "```\n" + json.dumps(health, indent=2, default=str)[:3000] + "\n```"
        if lowered in {"!tg login", "tg login"} or lowered.startswith(("!tg login ", "tg login ")):
            phone = body.split("login", 1)[1].strip() if "login" in lowered else ""
            return format_result(await self._auth.begin_login(phone))
        if lowered.startswith(("!tg code", "tg code")):
            code = re.split(r"(?i)code", body, maxsplit=1)[1].strip()
            return format_result(await self._auth.submit_code(code))
        if lowered.startswith(("!tg password", "tg password")):
            password = re.split(r"(?i)password", body, maxsplit=1)[1].strip()
            return format_result(await self._auth.submit_password(password))
        if lowered in {"!tg logout", "tg logout"}:
            staged = stage_action(self._pending, sender, "logout")
            return (
                "Logout staged but not executed (it revokes the Telegram session and stops syncing "
                f"until re-auth).\nReply `{staged['confirm_with']}` within 15 minutes to apply it."
            )
        if lowered in {"!tg cancel", "tg cancel"}:
            return format_result(await self._auth.cancel_login())
        if lowered in {"!tg pause", "tg pause"}:
            return format_result(await self._sync.pause_and_wait())
        if lowered in {"!tg resume", "tg resume"}:
            self._sync.resume()
            return "Sync resumed."
        if lowered in {"!tg llm", "tg llm"}:
            await self._llm.probe()
            return f"LLM probe: {'ok' if self._state.llm_ok else 'FAILED — ' + self._state.last_error}"

        # Backstop for the 2FA secret: while a login is pending in the password phase,
        # the operator is about to type the password — never forward free text to the
        # LLM. Only the deterministic `!tg password` / `!tg cancel` branches above run.
        if self._auth.pending_phase().startswith("password"):
            return (
                "A 2FA password is expected now. Reply `!tg password <password>` "
                "(handled locally, never sent to the LLM) or `!tg cancel` to abort."
            )
        # An explicit but unrecognized `!tg ...` command goes to help, not the model,
        # so a mistyped `!tg password` (e.g. wrong spacing) never becomes LLM context.
        if lowered.startswith("!tg"):
            return "Unrecognized command.\n" + self._help_text()

        try:
            return await asyncio.wait_for(
                self._llm.handle(sender, body),
                timeout=max(1, self._settings.llm_timeout_seconds * 4),
            )
        except asyncio.TimeoutError:
            return "The LLM request timed out. Direct commands like `!tg status` still work."

    async def _confirm(self, sender: str, token: str) -> str:
        pending = self._pending.get(sender)
        if not pending or pending.token != token:
            return "No matching pending action for that confirmation token."
        if time.time() - pending.created_at > 900:
            self._pending.pop(sender, None)
            return "That pending action expired. Stage it again if still needed."
        self._pending.pop(sender, None)
        if pending.action == "logout":
            return format_result(await self._auth.logout())
        return f"Unknown staged action: {pending.action}"

    async def _send(self, body: str) -> None:
        redacted = redact(body, self._settings, self._state)
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


async def egress_canary_loop(settings: Settings, state: RuntimeState) -> None:
    """The only in-TEE proof the egress firewall engaged: a non-allowlisted host
    must be unreachable. Checked continuously because the fw container races us."""
    host, port_s = settings.egress_canary.rsplit(":", 1)
    port = int(port_s)
    while True:
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=3)
            writer.close()
            if state.egress_locked:
                LOG.critical("egress canary %s became REACHABLE — firewall dropped?", settings.egress_canary)
            state.egress_locked = False
            state.last_error = f"egress NOT locked: canary {settings.egress_canary} reachable"
        except Exception:
            if not state.egress_locked:
                LOG.info("egress canary %s unreachable — firewall engaged", settings.egress_canary)
            state.egress_locked = True
        await asyncio.sleep(30)


async def llm_probe_loop(bot: Bot, state: RuntimeState) -> None:
    while True:
        await bot.llm.probe()
        await asyncio.sleep(1800)


async def serve_health(settings: Settings, state: RuntimeState, auth: TelegramAuth, sync: SyncService) -> None:
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
            # tg_authorized is a cached view; a logged-out session is a NORMAL state
            # for this agent (it exists to fix it), so it never gates the 200.
            ok = bool(state.matrix_logged_in and state.room_joined and state.egress_locked)
            payload = {
                "status": "ok" if ok else "starting",
                "matrix_logged_in": state.matrix_logged_in,
                "room_joined": state.room_joined,
                "egress_locked": state.egress_locked,
                "llm_configured": settings.llm_enabled,
                "llm_ok": state.llm_ok,
                "tg_authorized": state.tg_authorized,
                "pending_login": auth.pending_phase() or None,
                "sync_paused": sync.paused(),
                "sync_status": sync.wrapper_status(),
                "pg_provision": sync.provision_status(),
                "uptime_s": int(time.time() - state.started_at),
                # last_error can echo transport/exception text — scrub known secrets
                # before it reaches mesh peers.
                "last_error": redact(state.last_error, settings, state),
            }
            body = (json.dumps(payload, sort_keys=True, default=str) + "\n").encode()
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


def format_status(status: dict[str, Any], sync: SyncService) -> str:
    auth = status.get("authorized")
    auth_text = {True: "AUTHORIZED", False: "NOT authorized", None: "unknown"}[
        auth if isinstance(auth, bool) else None
    ]
    lines = [f"Telegram session: {auth_text}"]
    user = status.get("user")
    if user:
        lines.append(f"Account: {user.get('first_name')} (@{user.get('username')}, id {user.get('id')})")
    if status.get("pending_login"):
        lines.append(f"Login in progress: awaiting {status['pending_login']}")
    lines.append(f"Sync daemon: {'PAUSED' if status.get('sync_paused') else status.get('sync_status', '?')}")
    health = status.get("sync_health") or {}
    if health.get("reachable"):
        lines.append(
            f"Sync health: connected={health.get('connected')} chats={health.get('chats')} "
            f"messages={health.get('messages')} latest={health.get('latest_message')}"
        )
    else:
        lines.append(f"Sync health: unreachable ({health.get('error', '?')})")
    lines.append(f"DB provision: {sync.provision_status()}")
    if status.get("error"):
        lines.append(f"Error: {status['error']}")
    return "\n".join(lines)


def format_result(result: dict[str, Any]) -> str:
    if result.get("error") and not result.get("ok"):
        return f"Failed: {result['error']}"
    note = result.get("note", "")
    bits = []
    if result.get("code_sent"):
        bits.append(f"Login code sent to {result.get('phone')} (via {result.get('via')}).")
    if result.get("needs_password"):
        bits.append("Code accepted — two-step verification password required.")
    if result.get("already_authorized"):
        bits.append("Already authorized.")
    if result.get("authorized") and result.get("user"):
        user = result["user"]
        bits.append(
            f"Logged in as {user.get('first_name')} (@{user.get('username')}, id {user.get('id')})."
        )
    if result.get("logged_out"):
        bits.append("Logged out.")
    if result.get("cancelled") is not None:
        bits.append("Login cancelled." if result["cancelled"] else "No login was in progress.")
    if result.get("paused"):
        bits.append("Sync paused.")
    if result.get("resumed"):
        bits.append("Sync resumed.")
    if note:
        bits.append(note)
    return " ".join(bits) or json.dumps(result, default=str)


def redact(text: str, settings: Settings, state: RuntimeState) -> str:
    secrets_seen = [
        settings.matrix_password,
        settings.llm_api_key,
        settings.tg_api_hash,
        *state.runtime_secrets,
    ]
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
    sync = SyncService(settings)
    auth = TelegramAuth(settings, sync, state)
    bot = Bot(settings, auth, sync, state)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    tasks = [
        asyncio.create_task(serve_health(settings, state, auth, sync)),
        asyncio.create_task(egress_canary_loop(settings, state)),
        asyncio.create_task(llm_probe_loop(bot, state)),
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
