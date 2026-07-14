"""Mesh-only MCP server: Postgres full-text search over the telegram-sync archive.

Exposes a streamable-HTTP MCP endpoint (default :8085/mcp) with read-only tools
that full-text-search the operator's synced Telegram MESSAGES and, once image
understanding is enabled, the IMAGE DESCRIPTIONS (OCR + captions) in the media
table — joined back to chat title, timestamp, and sender.

Safety: connects with a dedicated READ-ONLY role (SEARCH_DATABASE_URL →
telegram_search) that only holds SELECT; every query is parameterized (no string
interpolation of user input) and runs under a statement timeout with a row cap.
It reaches Postgres over the wireguard mesh via the sidecar-netns HAProxy
forwarders; it has no other egress.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

import asyncpg
from mcp.server.fastmcp import FastMCP

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
LOG = logging.getLogger("telegram_fts_mcp")

DATABASE_URL = os.environ.get("SEARCH_DATABASE_URL", "")
if not DATABASE_URL:
    raise SystemExit("missing required environment variable: SEARCH_DATABASE_URL")
HOST = os.environ.get("MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("MCP_PORT", "8085"))
MCP_PATH = os.environ.get("MCP_PATH", "/mcp")
ROW_CAP = int(os.environ.get("SEARCH_ROW_LIMIT", "50"))
STMT_TIMEOUT_MS = int(os.environ.get("SEARCH_STATEMENT_TIMEOUT_MS", "8000"))

mcp = FastMCP(
    "telegram-fts",
    host=HOST,
    port=PORT,
    streamable_http_path=MCP_PATH,
    stateless_http=True,
    json_response=True,
    instructions=(
        "Full-text search over the operator's synced Telegram archive (messages and, "
        "when image understanding is on, image descriptions/OCR). Use `list_chats` to "
        "discover chats, `search_messages` for text, `search_images` for content seen in "
        "images. All searches are read-only and newest-first."
    ),
)

_pool: Optional[asyncpg.Pool] = None


async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            DATABASE_URL, min_size=1, max_size=4, command_timeout=15,
            server_settings={"statement_timeout": str(STMT_TIMEOUT_MS)},
        )
        LOG.info("db pool ready")
    return _pool


def _clamp(limit: int) -> int:
    return max(1, min(ROW_CAP, int(limit)))


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return str(value)


async def _fetch(sql: str, *args: Any) -> list[dict[str, Any]]:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    return [{k: _jsonable(v) for k, v in r.items()} for r in rows]


@mcp.tool()
async def search_messages(
    query: str, limit: int = 20, chat: str = "", since: str = "", from_me: Optional[bool] = None
) -> list[dict[str, Any]]:
    """Full-text search over Telegram MESSAGE text, newest first.

    Args:
        query: words/phrase to search for (Postgres plainto_tsquery, 'simple' config).
        limit: max rows (capped by the server).
        chat: optional case-insensitive substring of a chat title/username to scope to.
        since: optional ISO date or timestamp lower bound, e.g. "2026-06-01".
        from_me: if true only your own messages, if false only others', if unset both.

    Returns rows of {timestamp, chat, sender_name, is_from_me, content, chat_id, message_id}.
    """
    return await _fetch(
        """
        SELECT m.timestamp, c.title AS chat, m.sender_name, m.is_from_me,
               m.content, m.chat_id, m.id AS message_id
        FROM messages m JOIN chats c ON c.id = m.chat_id
        WHERE to_tsvector('simple', m.content) @@ plainto_tsquery('simple', $1)
          AND ($2 = '' OR c.title ILIKE '%'||$2||'%' OR coalesce(c.username,'') ILIKE '%'||$2||'%')
          AND ($3 = '' OR m.timestamp >= $3::timestamptz)
          AND ($4::boolean IS NULL OR m.is_from_me = $4)
        ORDER BY m.timestamp DESC
        LIMIT $5
        """,
        query, chat, since, from_me, _clamp(limit),
    )


@mcp.tool()
async def search_images(
    query: str, limit: int = 20, chat: str = "", since: str = ""
) -> list[dict[str, Any]]:
    """Full-text search over IMAGE DESCRIPTIONS (AI captions + transcribed text/OCR)
    for images sent over Telegram, newest first. Only images that have been described
    (media.status='done') are searched; requires image understanding to be enabled.

    Args:
        query: words/phrase to search for inside image descriptions.
        limit: max rows (capped by the server).
        chat: optional case-insensitive substring of a chat title/username to scope to.
        since: optional ISO date or timestamp lower bound.

    Returns rows of {timestamp, chat, sender_name, description, model, chat_id, message_id}.
    """
    return await _fetch(
        """
        SELECT m.timestamp, c.title AS chat, m.sender_name, md.description, md.model,
               m.chat_id, m.id AS message_id
        FROM media md
        JOIN messages m ON (m.chat_id, m.id) = (md.chat_id, md.message_id)
        JOIN chats c ON c.id = m.chat_id
        WHERE md.status = 'done'
          AND to_tsvector('simple', coalesce(md.description,'')) @@ plainto_tsquery('simple', $1)
          AND ($2 = '' OR c.title ILIKE '%'||$2||'%' OR coalesce(c.username,'') ILIKE '%'||$2||'%')
          AND ($3 = '' OR m.timestamp >= $3::timestamptz)
        ORDER BY m.timestamp DESC
        LIMIT $4
        """,
        query, chat, since, _clamp(limit),
    )


@mcp.tool()
async def list_chats(limit: int = 50, name: str = "") -> list[dict[str, Any]]:
    """List synced chats (most recently active first) to discover names for scoping.

    Args:
        limit: max rows (capped by the server).
        name: optional case-insensitive substring filter on title/username.

    Returns rows of {chat_id, title, username, type, messages, last_message}.
    """
    return await _fetch(
        """
        SELECT c.id AS chat_id, c.title, c.username, c.type,
               count(m.*) AS messages, max(m.timestamp) AS last_message
        FROM chats c LEFT JOIN messages m ON m.chat_id = c.id
        WHERE ($2 = '' OR c.title ILIKE '%'||$2||'%' OR coalesce(c.username,'') ILIKE '%'||$2||'%')
        GROUP BY c.id, c.title, c.username, c.type
        ORDER BY last_message DESC NULLS LAST
        LIMIT $1
        """,
        _clamp(limit), name,
    )


@mcp.tool()
async def archive_stats() -> dict[str, Any]:
    """Overall archive stats: chat/message counts, newest message time, and image
    understanding progress (how many image descriptions exist and their status)."""
    rows = await _fetch(
        """
        SELECT
          (SELECT count(*) FROM chats)                         AS chats,
          (SELECT count(*) FROM messages)                      AS messages,
          (SELECT max(timestamp) FROM messages)                AS latest_message,
          (SELECT count(*) FROM media WHERE status='done')     AS images_described,
          (SELECT count(*) FROM media WHERE status='pending')  AS images_pending,
          (SELECT count(*) FROM media WHERE status='failed')   AS images_failed
        """
    )
    return rows[0] if rows else {}


def main() -> None:
    LOG.info("telegram-fts MCP on %s:%s%s (stateless streamable-http)", HOST, PORT, MCP_PATH)
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
