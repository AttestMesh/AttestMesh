# telegram-fts-mcp

Mesh-only MCP server exposing Postgres full-text search over the `telegram-sync`
archive. Runs as a container in the telegram-sync node CVM, reachable by
AttestMesh cluster members at `http://<node-mesh-ip>:18085/mcp` (streamable-HTTP
MCP) — nothing is published publicly.

Tools:
- `search_messages(query, limit, chat, since, from_me)` — FTS over message text.
- `search_images(query, limit, chat, since)` — FTS over AI image descriptions/OCR
  (requires image understanding enabled on the sync daemon).
- `list_chats(limit, name)` — discover synced chats for scoping.
- `archive_stats()` — chat/message counts + image-understanding progress.

Read-only by construction: it connects with a dedicated `telegram_search` role
that only holds `SELECT` (provisioned at node boot alongside the `telegram_sync`
owner role), every query is parameterized, and queries run under a statement
timeout with a row cap. Its only egress is the pg-ha HAProxy forwarders over the
wireguard mesh; the sidecar egress firewall denies everything else.

Config via env: `SEARCH_DATABASE_URL` (DSN for the read-only role), `MCP_HOST`,
`MCP_PORT` (8085), `MCP_PATH` (/mcp), `SEARCH_ROW_LIMIT` (50),
`SEARCH_STATEMENT_TIMEOUT_MS` (8000), `LOG_LEVEL`.
