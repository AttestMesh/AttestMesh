# telegram-admin-agent

Matrix chat-ops agent co-located with the `telegram-sync` daemon on an
AttestMesh node. Its job is the Telethon session lifecycle: the operator DMs it
to **auth** (`!tg login` → code → optional 2FA password) and **re-auth**
(`!tg logout` → `confirm <token>` → `!tg login`), plus sync-daemon control and
health inspection. Free-text messages go to a pinned LLM with the same
operations exposed as tools — except the 2FA password, which is deliberately a
deterministic-command-only path (`!tg password <pw>`) so it never enters model
context.

Session-file safety: the Telethon SQLite session is shared with the sync
daemon, so every agent operation that opens it first pauses the daemon through
the shared control volume (`/control/pause`; the sync container's wrapper
SIGTERMs the daemon and reports `paused`), and resumes it when the login flow
finishes, fails, or is cancelled.

Deterministic commands: `!tg status | health | login [phone] | code <code> |
password <pw> | logout | cancel | pause | resume | llm`, and `confirm <token>`
for staged actions. `/healthz` (:9100) reports matrix/room/egress-canary/LLM
state — 200 requires the egress firewall to be engaged, but NOT an authorized
Telegram session (a logged-out session is the normal starting state).

Config via env: `TG_API_ID`, `TG_API_HASH`, `TG_PHONE` (optional default),
`TG_SESSION_PATH`, `SYNC_HEALTH_URL`, `CONTROL_DIR`, `MATRIX_HOMESERVER_URL`,
`MATRIX_USER_ID`, `MATRIX_PASSWORD`, `MATRIX_ROOM_ID`, `MATRIX_ADMIN_MXIDS`,
`MATRIX_REQUIRE_MENTION`, `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`,
`EGRESS_CANARY`, `HEALTH_HTTP_ADDR`. See `deploy/compose/telegram-sync-node.yaml`.
