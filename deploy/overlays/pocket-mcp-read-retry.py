"""Apply the narrow Pocket MCP read-retry overlay at image build time."""

from pathlib import Path

path = Path("/app/server/mcp_server.py")
source = path.read_text()

old_import = "import inspect\nimport logging\n"
new_import = "import inspect\nimport logging\nimport time\n"
assert source.count(old_import) == 1
source = source.replace(old_import, new_import)

old_function = '''def _with_conn(fn: Callable[[Any], _T]) -> _T:
    """Run a read-only DB operation with a short-lived connection."""
    settings = get_settings()
    conn = db.connect(settings.DATABASE_URL)
    try:
        return fn(conn)
    finally:
        close = getattr(conn, "close", None)
        if callable(close):
            close()
'''
new_function = '''def _with_conn(fn: Callable[[Any], _T]) -> _T:
    """Run a read-only DB operation with bounded failover retries."""
    settings = get_settings()
    deadline = time.monotonic() + 8.0
    while True:
        conn = None
        try:
            conn = db.connect(settings.DATABASE_URL)
            return fn(conn)
        except (db.psycopg.OperationalError, db.psycopg.InterfaceError) as exc:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            logger.warning("transient PostgreSQL read failure; retrying: %s", exc)
            time.sleep(min(0.5, remaining))
        finally:
            close = getattr(conn, "close", None)
            if callable(close):
                try:
                    close()
                except db.psycopg.Error:
                    pass
'''
assert source.count(old_function) == 1
path.write_text(source.replace(old_function, new_function))
