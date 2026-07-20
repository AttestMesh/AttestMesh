import datetime as dt
import pytest
from protocol import Command

def valid(**changes):
    now=dt.datetime.now(dt.timezone.utc)
    data=dict(cluster="andrew-xyn-pg", target="pg1", issued_at=now,
              expires_at=now+dt.timedelta(minutes=5), mode="explain",
              command="patroni.status", reason="health check")
    data.update(changes); return Command(**data)

def test_canonical_is_stable():
    assert valid().canonical().startswith(b'{"arguments":{}')

def test_rejects_shell():
    with pytest.raises(ValueError): valid(command="shell.exec")

def test_rejects_long_window():
    now=dt.datetime.now(dt.timezone.utc)
    with pytest.raises(ValueError): valid(issued_at=now, expires_at=now+dt.timedelta(minutes=6))
