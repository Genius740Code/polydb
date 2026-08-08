from __future__ import annotations

import pytest

from polydb import Database
from polydb.adapters.mongo import MongoAdapter
from polydb.adapters.postgres import PostgresAdapter
from polydb.adapters.sql.base import SqlAdapter
from polydb.exceptions import ConnectionNotOpenError, InvalidConnectionStringError


# -- 1.1 issue #1: Database.from_url resolves schemes to adapter instances -----


def test_from_url_resolves_each_supported_scheme():
    postgres = Database.from_url("postgres://user:pass@localhost:5432/db")
    assert isinstance(postgres._adapter, PostgresAdapter)

    postgresql = Database.from_url("postgresql://user:pass@localhost:5432/db")
    assert isinstance(postgresql._adapter, PostgresAdapter)

    mongo = Database.from_url("mongodb://localhost:27017/db")
    assert isinstance(mongo._adapter, MongoAdapter)

    mongo_srv = Database.from_url("mongodb+srv://user:pass@cluster.example.com/db")
    assert isinstance(mongo_srv._adapter, MongoAdapter)

    sqlite = Database.from_url("sqlite:///:memory:")
    assert isinstance(sqlite._adapter, SqlAdapter)
    assert sqlite._adapter.dialect.name == "sqlite"

    mysql = Database.from_url("mysql://user:pass@localhost:3306/db")
    assert isinstance(mysql._adapter, SqlAdapter)
    assert mysql._adapter.dialect.name == "mysql"


def test_from_url_returns_unconnected_instances():
    urls = [
        "postgres://user:pass@localhost:5432/db",
        "mongodb://localhost:27017/db",
        "sqlite:///:memory:",
        "mysql://user:pass@localhost:3306/db",
    ]
    for url in urls:
        adapter = Database.from_url(url)._adapter
        assert adapter._connected is False, url


def test_from_url_rejects_unknown_scheme():
    with pytest.raises(InvalidConnectionStringError):
        Database.from_url("redis://localhost:6379/0")


def test_from_url_adapter_instance_per_call():
    first = Database.from_url("sqlite:///:memory:")
    second = Database.from_url("sqlite:///:memory:")
    assert first is not second
    assert first._adapter is not second._adapter


# -- adapters that exist as factory targets but aren't built yet -----------------

async def test_postgres_connect_not_implemented_yet():
    db = Database.from_url("postgres://user:pass@localhost:5432/db")
    with pytest.raises(NotImplementedError):
        await db.connect()


async def test_mongo_connect_not_implemented_yet():
    db = Database.from_url("mongodb://localhost:27017/db")
    with pytest.raises(NotImplementedError):
        await db.connect()


async def test_mysql_connect_not_implemented_yet():
    db = Database.from_url("mysql://user:pass@localhost:3306/db")
    assert db._adapter.dialect.name == "mysql"  # factory works
    with pytest.raises(NotImplementedError):
        await db.connect()


# -- SQLite (the one live leg of the SQL family): §1.1 #2–#6 -------------------

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