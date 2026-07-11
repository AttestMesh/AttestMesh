#!/usr/bin/env python3
"""Matrix room probe: send a command as the verifier user and wait for a bot reply.

Extracted from the inline heredoc in postgres-node.sh so multi-bot drivers
(pg-ha-node.sh) can reuse it. All inputs come from MATRIX_PROBE_* env:

  MATRIX_PROBE_FQDN       homeserver FQDN (https:// is prepended)
  MATRIX_PROBE_ROOM_ID    room to speak in
  MATRIX_PROBE_USER       verifier localpart
  MATRIX_PROBE_PASSWORD   verifier password
  MATRIX_PROBE_BOT        bot MXID whose reply we await
  MATRIX_PROBE_COMMAND    message body to send
  MATRIX_PROBE_EXPECT_RE  regex the bot reply must match
  MATRIX_PROBE_FOLLOWUP_TEMPLATE  optional second message; ``{1}`` expands to
                                  capture group 1 from the first reply
  MATRIX_PROBE_FOLLOWUP_EXPECT_RE regex the second bot reply must match

Exit 0 after the requested matching reply/replies within 90s each; raises
SystemExit otherwise.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

base = "https://" + os.environ["MATRIX_PROBE_FQDN"].rstrip("/")
room_id = os.environ["MATRIX_PROBE_ROOM_ID"]
user = os.environ["MATRIX_PROBE_USER"]
password = os.environ["MATRIX_PROBE_PASSWORD"]
bot = os.environ["MATRIX_PROBE_BOT"]
command = os.environ["MATRIX_PROBE_COMMAND"]
expect = re.compile(os.environ["MATRIX_PROBE_EXPECT_RE"], re.I | re.S)


def request(method, path, payload=None, token=None, timeout=15):
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"content-type": "application/json"}
    if token:
        headers["authorization"] = "Bearer " + token
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    for _ in range(4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                return json.loads(body.decode() or "{}")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            if exc.code == 429:
                try:
                    retry_ms = int(json.loads(body).get("retry_after_ms") or 5000)
                except Exception:
                    retry_ms = 5000
                time.sleep(max(1, min(300, (retry_ms + 999) // 1000)))
                continue
            raise SystemExit(f"Matrix API {method} {path} failed: HTTP {exc.code}: {body[:500]}")
    raise SystemExit(f"Matrix API {method} {path} remained rate-limited")


login = request("POST", "/_matrix/client/v3/login", {
    "type": "m.login.password",
    "identifier": {"type": "m.id.user", "user": user},
    "password": password,
})
token = login.get("access_token")
if not token:
    raise SystemExit("Matrix login did not return an access token")

room_path = urllib.parse.quote(room_id, safe="")


def send_and_wait(message, expected):
    txn = uuid.uuid4().hex
    sent = request(
        "PUT",
        f"/_matrix/client/v3/rooms/{room_path}/send/m.room.message/{txn}",
        {"msgtype": "m.text", "body": message},
        token=token,
    )
    sent_event_id = sent.get("event_id")
    if not sent_event_id:
        raise SystemExit("Matrix send did not return an event_id")

    deadline = time.time() + 90
    while time.time() < deadline:
        qs = urllib.parse.urlencode({"dir": "b", "limit": "100"})
        events = request(
            "GET", f"/_matrix/client/v3/rooms/{room_path}/messages?" + qs, token=token, timeout=12
        ).get("chunk", [])
        for event in events:
            if event.get("event_id") == sent_event_id:
                break
            if event.get("type") != "m.room.message" or event.get("sender") != bot:
                continue
            body = str(event.get("content", {}).get("body", ""))
            match = expected.search(body)
            if match:
                print(body[:800])
                return match
        time.sleep(3)
    raise SystemExit(f"timed out waiting for {bot} reply matching /{expected.pattern}/")


first_match = send_and_wait(command, expect)
followup_template = os.environ.get("MATRIX_PROBE_FOLLOWUP_TEMPLATE", "")
if followup_template:
    followup_expect_raw = os.environ.get("MATRIX_PROBE_FOLLOWUP_EXPECT_RE", "")
    if not followup_expect_raw:
        raise SystemExit("MATRIX_PROBE_FOLLOWUP_EXPECT_RE is required with a follow-up template")
    followup = followup_template
    for index, value in enumerate(first_match.groups(), start=1):
        followup = followup.replace("{" + str(index) + "}", value or "")
    send_and_wait(followup, re.compile(followup_expect_raw, re.I | re.S))
