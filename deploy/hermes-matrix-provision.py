#!/usr/bin/env python3
"""Provision a Hermes agent's Matrix account via the matrix-admin-agent LLM bot.

Invoked by deploy/hermes-node.sh (provision-matrix) with the Synapse
client-server API reachable at HS_URL (an ssh -L tunnel through the mesh shell
to the matrix node's mesh-only listener). It logs in as the admin operator,
opens a DM with the admin bot, and asks it to `ensure_user` + issue a
`create_login_token` for the agent; the bot's confirm-first flow is answered
with a yes. The freshly minted token is the ONE sanctioned secret-bearing bot
reply (see docs/specs/matrix-admin-agent.md §7.4).

Secrets arrive via env (never argv): ADMIN_PASSWORD. Inputs: HS_URL,
MATRIX_SERVER, AGENT_USERNAME, ADMIN_USER (default lsdan), BOT_LOCALPART
(default matrix-admin-agent). Prints {"user_id": …, "access_token": …} JSON.

NOTE: this drives an LLM operator channel, so it is a best-effort automation —
the deterministic upgrade is the on-chain command channel, which needs
MATRIX_ADMIN_SENDERS sealed on the matrix node (spec §8.1).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

HS = os.environ["HS_URL"].rstrip("/")
SERVER = os.environ["MATRIX_SERVER"]
AGENT = os.environ["AGENT_USERNAME"]
ADMIN_USER = os.environ.get("ADMIN_USER", "lsdan")
ADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]
BOT = f"@{os.environ.get('BOT_LOCALPART', 'matrix-admin-agent')}:{SERVER}"
TIMEOUT_S = int(os.environ.get("PROVISION_TIMEOUT_S", "420"))
TOKEN_RE = re.compile(r"\b(syt_[A-Za-z0-9_~-]+)\b")


def api(method: str, path: str, body: dict | None = None, token: str | None = None) -> dict:
    url = f"{HS}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode() or "{}")


def say(token: str, room: str, body: str) -> None:
    api(
        "PUT",
        f"/_matrix/client/v3/rooms/{urllib.parse.quote(room)}/send/m.room.message/{uuid.uuid4()}",
        {
            "msgtype": "m.text",
            "body": body,
            "m.mentions": {"user_ids": [BOT]},
        },
        token,
    )


def main() -> None:
    login = api(
        "POST",
        "/_matrix/client/v3/login",
        {
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": ADMIN_USER},
            "password": ADMIN_PASSWORD,
            "initial_device_display_name": "hermes-node provisioner",
        },
    )
    tok = login["access_token"]
    me = login["user_id"]

    room = api(
        "POST",
        "/_matrix/client/v3/createRoom",
        {"invite": [BOT], "is_direct": True, "preset": "trusted_private_chat"},
        tok,
    )["room_id"]
    print(f"provision: DM room {room} (as {me})", file=sys.stderr)

    deadline = time.time() + TIMEOUT_S
    while time.time() < deadline:
        members = api(
            "GET", f"/_matrix/client/v3/rooms/{urllib.parse.quote(room)}/joined_members", None, tok
        ).get("joined", {})
        if BOT in members:
            break
        time.sleep(2)
    else:
        raise SystemExit(f"bot {BOT} never joined the DM")

    say(
        tok,
        room,
        f"{BOT} please do the following, in order: "
        f"1) ensure_user username={AGENT} (a normal non-admin user; generate a random password). "
        f"2) create_login_token username={AGENT} with no expiry. "
        f"Reply with the minted access token. I confirm both actions in advance — yes, proceed.",
    )

    since = None
    confirms = 0
    while time.time() < deadline:
        qs = {"timeout": "25000"}
        if since:
            qs["since"] = since
        sync = api("GET", "/_matrix/client/v3/sync?" + urllib.parse.urlencode(qs), None, tok)
        since = sync.get("next_batch")
        events = (
            sync.get("rooms", {}).get("join", {}).get(room, {}).get("timeline", {}).get("events", [])
        )
        for ev in events:
            if ev.get("type") != "m.room.message" or ev.get("sender") != BOT:
                continue
            body = ev.get("content", {}).get("body", "")
            print(f"bot: {body[:200]}", file=sys.stderr)
            match = TOKEN_RE.search(body)
            if match:
                print(json.dumps({"user_id": f"@{AGENT}:{SERVER}", "access_token": match.group(1)}))
                return
            # Confirm-first flow: nudge past confirmation prompts, bounded.
            if confirms < 3:
                confirms += 1
                say(tok, room, f"{BOT} yes — confirmed, proceed.")
    raise SystemExit("timed out waiting for the bot to mint the token")


if __name__ == "__main__":
    main()
