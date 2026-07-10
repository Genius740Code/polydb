from __future__ import annotations

import pytest

from polydb import Database
from polydb.exceptions import ConnectionNotOpenError, InvalidConnectionStringError


async def test_connect_ping_disconnect(tmp_path):
    db = Database.from_url(f"sqlite:////{tmp_path}/test.db")
    await db.connect()
    assert await db.ping() is True
    await db.disconnect()


async def test_connect_is_idempotent(tmp_path):
    db = Database.from_url(f"sqlite:////{tmp_path}/test.db")
    await db.connect()
    await db.connect()  # should not raise / should not open a second handle
    assert await db.ping() is True
    await db.disconnect()


async def test_disconnect_before_connect_is_noop(tmp_path):
    db = Database.from_url(f"sqlite:////{tmp_path}/test.db")
    await db.disconnect()  # should not raise


async def test_ping_before_connect_raises(tmp_path):
    db = Database.from_url(f"sqlite:////{tmp_path}/test.db")
    with pytest.raises(ConnectionNotOpenError):
        await db.ping()


async def test_async_context_manager(tmp_path):
    async with Database.from_url(f"sqlite:////{tmp_path}/test.db") as db:
        assert await db.ping() is True


async def test_in_memory_sqlite():
    async with Database.from_url("sqlite:///:memory:") as db:
        assert await db.ping() is True


def test_from_url_rejects_unknown_scheme():
    with pytest.raises(InvalidConnectionStringError):
        Database.from_url("redis://localhost:6379/0")


def test_from_url_postgres_not_built_yet():
    with pytest.raises(NotImplementedError):
        Database.from_url("postgres://user:pass@localhost:5432/db")


def test_from_url_mongo_not_built_yet():
    with pytest.raises(NotImplementedError):
        Database.from_url("mongodb://localhost:27017/db")


def test_from_url_mysql_not_built_yet():
    with pytest.raises(NotImplementedError):
        Database.from_url("mysql://user:pass@localhost:3306/db")
