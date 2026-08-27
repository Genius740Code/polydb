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

async def test_mongo_connect_not_implemented_yet():
    db = Database.from_url("mongodb://localhost:27017/db")
    with pytest.raises(NotImplementedError):
        await db.connect()


async def test_mysql_connect_not_implemented_yet():
    db = Database.from_url("mysql://user:pass@localhost:3306/db")
    assert db._adapter.dialect.name == "mysql"  # factory works
    with pytest.raises(NotImplementedError):
        await db.connect()


# -- PostgreSQL adapter specifics -------------------------------------------------

def test_postgres_adapter_factory_creates_correct_instance():
    db = Database.from_url("postgres://user:pass@localhost:5432/db")
    assert isinstance(db._adapter, PostgresAdapter)
    assert db._adapter.dialect.name == "postgres"
    assert db._adapter.dialect.placeholder == "$1"
    assert db._adapter.dialect.identifier_quote == '"'
    assert db._adapter.dialect.supports_pool is True


def test_postgres_adapter_pool_config_exposed():
    db = Database.from_url("postgres://user:pass@localhost:5432/db?pool_size=5&timeout=60")
    assert db.pool_size == 5
    assert db.timeout == 60.0


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


async def test_disconnect_twice_is_noop(tmp_path):
    db = Database.from_url(f"sqlite:////{tmp_path}/test.db")
    await db.connect()
    await db.disconnect()
    await db.disconnect()  # should not raise


async def test_reconnect_after_disconnect(tmp_path):
    db = Database.from_url(f"sqlite:////{tmp_path}/test.db")
    await db.connect()
    await db.disconnect()
    await db.connect()  # same adapter, second open is a fresh connection
    assert await db.ping() is True
    await db.disconnect()


async def test_connect_failure_leaves_adapter_disconnected(tmp_path, monkeypatch):
    # A failing driver connect() must leave the adapter cleanly NOT connected.
    # (We simulate the driver failure rather than pointing at a bad file, which
    # makes aiosqlite's worker thread report an unhandled-thread warning.)
    import aiosqlite

    def _boom(*args, **kwargs):
        raise RuntimeError("driver connect failed")

    monkeypatch.setattr(aiosqlite, "connect", _boom)
    db = Database.from_url(f"sqlite:////{tmp_path}/test.db")
    with pytest.raises(RuntimeError, match="driver connect failed"):
        await db.connect()
    assert db._adapter._connected is False
    assert db._adapter._conn is None


async def test_ping_before_connect_raises(tmp_path):
    db = Database.from_url(f"sqlite:////{tmp_path}/test.db")
    with pytest.raises(ConnectionNotOpenError):
        await db.ping()


async def test_async_context_manager(tmp_path):
    async with Database.from_url(f"sqlite:////{tmp_path}/test.db") as db:
        assert await db.ping() is True


async def test_async_context_manager_disconnects_on_exit(tmp_path):
    db = Database.from_url(f"sqlite:////{tmp_path}/test.db")
    async with db:
        assert db._adapter._connected is True
    assert db._adapter._connected is False


async def test_async_context_manager_survives_body_exception(tmp_path):
    db = Database.from_url(f"sqlite:////{tmp_path}/test.db")
    with pytest.raises(LookupError):
        async with db:
            raise LookupError("boom")
    # The connection must be released even when the body raised.
    assert db._adapter._connected is False


async def test_context_manager_cleanup_error_propagates_without_body_error(tmp_path, monkeypatch):
    # A disconnect() failure with NO in-body exception must propagate.
    async def _boom_disconnect():
        raise OSError("driver close failed")

    db = Database.from_url(f"sqlite:////{tmp_path}/test.db")
    monkeypatch.setattr(db._adapter, "disconnect", _boom_disconnect)
    with pytest.raises(OSError, match="driver close failed"):
        async with db:
            pass


async def test_context_manager_body_error_not_masked_by_cleanup_error(tmp_path, monkeypatch):
    # When BOTH the body and disconnect() fail, the body's exception must win —
    # a cleanup failure is never allowed to replace it.
    async def _boom_disconnect():
        raise OSError("driver close failed")

    db = Database.from_url(f"sqlite:////{tmp_path}/test.db")
    monkeypatch.setattr(db._adapter, "disconnect", _boom_disconnect)
    with pytest.raises(LookupError, match="boom"):
        async with db:
            raise LookupError("boom")


async def test_in_memory_sqlite():
    async with Database.from_url("sqlite:///:memory:") as db:
        assert await db.ping() is True


# -- §1.1 issue #4: ping reports unhealthy as False, never a raw driver error ---


async def test_ping_returns_false_when_round_trip_fails(tmp_path):
    db = Database.from_url(f"sqlite:////{tmp_path}/test.db")
    await db.connect()
    try:
        async def _boom_execute(sql):
            raise OSError("disk I/O error")

        db._adapter._conn.execute = _boom_execute
        assert await db.ping() is False
    finally:
        await db.disconnect()


async def test_ping_failure_is_logged_not_raised(tmp_path, caplog):
    import logging

    db = Database.from_url(f"sqlite:////{tmp_path}/test.db")
    await db.connect()
    try:
        async def _boom_execute(sql):
            raise OSError("disk I/O error")

        db._adapter._conn.execute = _boom_execute
        with caplog.at_level(logging.WARNING, logger="polydb.adapters.sql"):
            assert await db.ping() is False
        assert "ping()" in caplog.text
        assert "disk I/O error" in caplog.text
    finally:
        await db.disconnect()


async def test_ping_recovers_after_failed_round_trip(tmp_path):
    db = Database.from_url(f"sqlite:////{tmp_path}/test.db")
    await db.connect()

    conn = db._adapter._conn
    healthy_execute = conn.execute

    async def _boom_execute(sql):
        raise OSError("disk I/O error")

    conn.execute = _boom_execute
    assert await db.ping() is False

    # A healthy round-trip after an unhealthy one must still report True —
    # ping() failure never poisons adapter state.
    conn.execute = healthy_execute
    assert await db.ping() is True
    await db.disconnect()


# -- §1.1 issue #6: pool_size / timeout knobs ------------------------------------


async def test_pool_size_and_timeout_exposed_on_adapter(tmp_path):
    db = Database.from_url(f"sqlite:////{tmp_path}/test.db?pool_size=3&timeout=7.5")
    assert db.pool_size == 3  # delegated through Database.__getattr__
    assert db.timeout == 7.5
    assert db._adapter.config.pool_size == 3


async def test_pool_size_and_timeout_defaults_when_absent(tmp_path):
    from polydb.url_parser import _DEFAULT_POOL_SIZE, _DEFAULT_TIMEOUT_SECONDS

    db = Database.from_url(f"sqlite:////{tmp_path}/test.db")
    assert db._adapter.pool_size == _DEFAULT_POOL_SIZE
    assert db._adapter.timeout == _DEFAULT_TIMEOUT_SECONDS


async def test_timeout_is_wired_into_sqlite_driver(tmp_path, monkeypatch):
    import aiosqlite

    captured_kwargs = {}
    real_connect = aiosqlite.connect

    def _recording_connect(database, **kwargs):
        captured_kwargs.update(kwargs)
        return real_connect(database, **kwargs)

    monkeypatch.setattr(aiosqlite, "connect", _recording_connect)
    db = Database.from_url(f"sqlite:////{tmp_path}/test.db?timeout=2.5")
    async with db:
        assert await db.ping() is True
    assert captured_kwargs.get("timeout") == 2.5


async def test_explicit_nondefault_pool_size_warns_on_sqlite(tmp_path, caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="polydb.adapters.sql"):
        Database.from_url(f"sqlite:////{tmp_path}/test.db?pool_size=4")
    assert any("pool_size" in record.message and "ignored" in record.message
               for record in caplog.records)


async def test_default_or_mysql_pool_size_does_not_warn(caplog):
    import logging

    # SQLite at default pool_size: no warning.
    with caplog.at_level(logging.WARNING, logger="polydb.adapters.sql"):
        Database.from_url("sqlite:///:memory:")
    assert not [r for r in caplog.records if "pool_size" in r.message]

    # MySQL supports pools (leg itself still pending build step 5).
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="polydb.adapters.sql"):
        Database.from_url("mysql://user:pass@localhost:3306/db?pool_size=4")
    assert not [r for r in caplog.records if "pool_size" in r.message]