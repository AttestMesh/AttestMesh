#!/usr/bin/env python3
"""Provision ONE Matrix bot account + an operator DM room for it.

Used by telegram-sync-node.sh provision-matrix (pgha-provision-bots.py pattern):
logs in as the human admin, asks the matrix-admin-agent LLM bot to `ensure_user`
the new account (idempotent PUT /_synapse/admin/v2/users), answers a confirm turn
if staged, verifies by LOGGING IN as the new user (never by parsing bot prose),
then creates the admin<->bot DM room and has the bot account join it immediately
— so the room id can be sealed into the agent's env before its CVM exists.

Credential hygiene: the ensure_user message (visible in room history AND to the
pinned LLM, since it @-mentions the bot) carries only a THROWAWAY bootstrap
password. Once login as the new user succeeds, the real sealed NEW_PASSWORD is
set via a direct /_matrix/client/v3/account/password call that never touches a
room or the LLM, invalidating the bootstrap. Idempotent re-runs fast-path on the
final password.

Env: HS_URL (https tailnet FQDN — mesh-tunnel logins get per-source rate-limited),
MATRIX_SERVER (server_name), ADMIN_USER, ADMIN_PASSWORD, BOT_LOCALPART (the
matrix-admin-agent), NEW_USER, NEW_PASSWORD, NEW_DISPLAYNAME. All secrets via env.

Prints {"user_id": ..., "room_id": ...} on success.
"""
from __future__ import annotations
import json, os, secrets, sys, time, urllib.error, urllib.parse, urllib.request, uuid

HS = os.environ["HS_URL"].rstrip("/")
SERVER = os.environ["MATRIX_SERVER"]
ADMIN_USER = os.environ.get("ADMIN_USER", "lsdan")
ADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]
BOT = f"@{os.environ.get('BOT_LOCALPART', 'matrix-admin-agent')}:{SERVER}"
NEW_USER = os.environ["NEW_USER"]
NEW_PASSWORD = os.environ["NEW_PASSWORD"]
NEW_DISPLAYNAME = os.environ.get("NEW_DISPLAYNAME", NEW_USER)
NEW_MXID = f"@{NEW_USER}:{SERVER}"


# Synapse rate-limits login PER SOURCE IP, and a failed login (nonexistent user or
# wrong password) trips the same limiter — so hammering login() blocks EVERYTHING
# from this IP (pg-ha lesson 7). Keep each api() call short (few 429 retries, small
# cap) and let the callers space attempts so the limiter recovers between them.
def api(method, path, body=None, token=None, timeout=30, max_429=3, cap=12):
    for _ in range(max_429 + 1):
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
                    wait = min(cap, max(2, int(json.loads(body_txt).get("retry_after_ms", 4000) / 1000)))
                except Exception:
                    wait = 4
                time.sleep(wait)
                continue
            return e.code, {"errcode": "HTTP", "error": body_txt[:200]}
    return 429, {"error": "rate-limited"}


def login(user, pw, max_429=1):
    # max_429=1: give up fast on rate-limiting (return None) so the caller's spaced
    # poll loop drives retries instead of blocking inside one call.
    s, r = api("POST", "/_matrix/client/v3/login", {
        "type": "m.login.password",
        "identifier": {"type": "m.id.user", "user": user},
        "password": pw,
    }, max_429=max_429)
    return r.get("access_token") if s == 200 else None


def login_retry(user, pw, tries=12, gap=12):
    """For a login that MUST succeed (the admin): spaced attempts, gentle on the limiter."""
    for i in range(tries):
        tok = login(user, pw, max_429=1)
        if tok:
            return tok
        time.sleep(gap)
    return None


def change_password(user_token, user, old_pw, new_pw):
    """Set new_pw via user-interactive auth (the 401 body carries the UIA session).
    logout_devices=False keeps user_token valid for the room-join step below."""
    def raw(body):
        req = urllib.request.Request(
            HS + "/_matrix/client/v3/account/password",
            data=json.dumps(body).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + user_token)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read() or "{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or "{}")
            except Exception:
                return e.code, {}
    s, r = raw({"new_password": new_pw, "logout_devices": False})
    if s == 200:
        return True
    session = r.get("session")
    if not session:
        return False
    s2, _ = raw({
        "new_password": new_pw,
        "logout_devices": False,
        "auth": {
            "type": "m.login.password",
            "session": session,
            "identifier": {"type": "m.id.user", "user": user},
            "password": old_pw,
        },
    })
    return s2 == 200


def main():
    tok = login_retry(ADMIN_USER, ADMIN_PASSWORD)
    if not tok:
        print("FATAL: admin login failed (rate-limited or bad password)", file=sys.stderr); sys.exit(1)
    print(f"admin {ADMIN_USER} logged in", file=sys.stderr)

    # Fast path: the account may already exist with the FINAL password (idempotent
    # re-run). Tolerate 429 here so we get a DEFINITIVE answer — a false "no such
    # user" would needlessly reset an already-correct password.
    newtok = login(NEW_USER, NEW_PASSWORD, max_429=6)
    if newtok:
        print(f"{NEW_USER} already exists with the final password", file=sys.stderr)
    else:
        # Bootstrap password: this is what appears in the ensure_user message (room
        # history + LLM). Throwaway — rotated to NEW_PASSWORD before we finish.
        bootstrap = secrets.token_hex(24)
        _, room = api("POST", "/_matrix/client/v3/createRoom",
                      {"invite": [BOT], "is_direct": True, "preset": "trusted_private_chat"}, tok)
        admin_room = room.get("room_id")
        if not admin_room:
            print(f"FATAL: could not open DM with {BOT}: {room}", file=sys.stderr); sys.exit(1)
        print(f"admin-bot DM room {admin_room}", file=sys.stderr)

        def say(text):
            api("PUT",
                f"/_matrix/client/v3/rooms/{urllib.parse.quote(admin_room)}/send/m.room.message/{uuid.uuid4()}",
                {"msgtype": "m.text", "body": text, "m.mentions": {"user_ids": [BOT]}}, tok)

        say(f"{BOT}: ensure_user username={NEW_USER} password={bootstrap} "
            f"displayname=\"{NEW_DISPLAYNAME}\"")
        # ensure_user is non-destructive (no confirm turn). Give the bot a grace
        # period to run the LLM tool call + PUT before probing — probing too early
        # just burns failed logins against the limiter.
        time.sleep(25)

        # Spaced poll: at most ~12 attempts, 20s apart, each a single quick login so
        # the per-IP limiter recovers between tries.
        boottok = None
        for i in range(12):
            boottok = login(NEW_USER, bootstrap)
            if boottok:
                print(f"OK {NEW_USER} (bootstrap) after {i+1} probe(s)", file=sys.stderr)
                break
            print(f"… {NEW_USER} not ready yet (probe {i+1}/12)", file=sys.stderr)
            time.sleep(20)
        if not boottok:
            print(json.dumps({"error": f"could not verify {NEW_USER} login within poll window"}))
            sys.exit(2)

        # Rotate the bootstrap → the real sealed password over the client-server API
        # (never a room event / LLM). The bootstrap is now dead.
        if not change_password(boottok, NEW_USER, bootstrap, NEW_PASSWORD):
            print(json.dumps({"error": f"password rotation failed for {NEW_USER}"}))
            sys.exit(2)
        newtok = login_retry(NEW_USER, NEW_PASSWORD)
        if not newtok:
            print(json.dumps({"error": f"{NEW_USER} login failed after rotation"}))
            sys.exit(2)
        print(f"rotated {NEW_USER} to the sealed password", file=sys.stderr)

    # Operator <-> bot DM room; the bot joins NOW so the sealed room id is live
    # before its agent ever boots.
    _, ops = api("POST", "/_matrix/client/v3/createRoom",
                 {"invite": [NEW_MXID], "is_direct": True, "preset": "trusted_private_chat",
                  "name": NEW_DISPLAYNAME}, tok)
    ops_room = ops.get("room_id")
    if not ops_room:
        print(json.dumps({"error": f"createRoom failed: {ops}"}))
        sys.exit(2)
    s, joined = api("POST", f"/_matrix/client/v3/join/{urllib.parse.quote(ops_room)}",
                    {}, newtok)
    if s != 200:
        print(json.dumps({"error": f"bot could not join {ops_room}: {joined}"}))
        sys.exit(2)

    print(json.dumps({"user_id": NEW_MXID, "room_id": ops_room}))
    sys.exit(0)


if __name__ == "__main__":
    main()
