#!/usr/bin/env python3
"""Provision the @pgha-pg1/2/3 Matrix bot users via the matrix-admin-agent.

Logs in as the human admin, opens a DM with the admin bot, and issues one
`ensure_user{username,password}` per pg-ha node (the bot's idempotent named
verb → PUT /_synapse/admin/v2/users). Answers a confirm-first turn if the bot
stages one. Success is verified definitively by logging in as each new user
with the shared password — not by parsing bot replies.

Env: HS_URL, MATRIX_SERVER, ADMIN_USER, ADMIN_PASSWORD, BOT_LOCALPART,
BOTPASSWORD, PGHA_USERS (comma-separated localparts). All secrets via env.
"""
from __future__ import annotations
import json, os, sys, time, urllib.error, urllib.parse, urllib.request, uuid

HS = os.environ["HS_URL"].rstrip("/")
SERVER = os.environ["MATRIX_SERVER"]
ADMIN_USER = os.environ.get("ADMIN_USER", "lsdan")
ADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]
BOT = f"@{os.environ.get('BOT_LOCALPART','matrix-admin-agent')}:{SERVER}"
BOTPW = os.environ["BOTPASSWORD"]
USERS = [u.strip() for u in os.environ["PGHA_USERS"].split(",") if u.strip()]


def api(method, path, body=None, token=None, timeout=30):
    for _ in range(8):
        req = urllib.request.Request(
            HS + path, data=json.dumps(body).encode() if body is not None else None, method=method
        )
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read() or "{}")
        except urllib.error.HTTPError as e:
            body_txt = e.read().decode("utf-8", "replace")
            if e.code == 429:
                try:
                    wait = min(30, max(2, int(json.loads(body_txt).get("retry_after_ms", 5000) / 1000)))
                except Exception:
                    wait = 5
                time.sleep(wait)
                continue
            return e.code, {"errcode": "HTTP", "error": body_txt[:200]}
    return 0, {"error": "rate-limited out"}


def login(user, pw):
    s, r = api("POST", "/_matrix/client/v3/login", {
        "type": "m.login.password",
        "identifier": {"type": "m.id.user", "user": user},
        "password": pw,
    })
    return r.get("access_token") if s == 200 else None


def main():
    tok = login(ADMIN_USER, ADMIN_PASSWORD)
    if not tok:
        print("FATAL: admin login failed", file=sys.stderr); sys.exit(1)
    print(f"admin {ADMIN_USER} logged in", file=sys.stderr)

    _, room = api("POST", "/_matrix/client/v3/createRoom",
                  {"invite": [BOT], "is_direct": True, "preset": "trusted_private_chat"}, tok)
    room_id = room.get("room_id")
    print(f"DM room {room_id}", file=sys.stderr)

    def say(text):
        api("PUT", f"/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/send/m.room.message/{uuid.uuid4()}",
            {"msgtype": "m.text", "body": text, "m.mentions": {"user_ids": [BOT]}}, tok)

    # Issue ensure_user for every bot up front (idempotent).
    for u in USERS:
        say(f"{BOT}: ensure_user username={u} password={BOTPW} displayname=\"pg-ha {u} admin\"")
        time.sleep(2)

    # Poll: a user is done when we can log in as it. Answer a confirm-turn if the bot asks.
    pending = set(USERS)
    deadline = time.time() + 420
    confirmed_once = False
    while pending and time.time() < deadline:
        for u in list(pending):
            if login(u, BOTPW):
                print(f"OK {u}", file=sys.stderr); pending.discard(u)
        if not pending:
            break
        # look for a confirm prompt from the bot and answer it (once)
        if not confirmed_once:
            _, msgs = api("GET",
                f"/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/messages?dir=b&limit=30", token=tok)
            for ev in msgs.get("chunk", []):
                if ev.get("sender") == BOT:
                    b = str(ev.get("content", {}).get("body", "")).lower()
                    if "confirm" in b or "yes/no" in b or "reply" in b:
                        say("yes"); confirmed_once = True
                        break
        time.sleep(10)

    print(json.dumps({"provisioned": [u for u in USERS if u not in pending],
                      "pending": sorted(pending)}))
    sys.exit(0 if not pending else 2)


if __name__ == "__main__":
    main()
