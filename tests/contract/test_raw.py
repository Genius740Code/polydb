from __future__ import annotations

import aiosqlite
import pytest

from polydb import Database
from polydb.exceptions import (
    ConnectionNotOpenError,
    InvalidFilterError,
    PolydbQueryError,
    TransactionInactiveError,
)


@pytest.fixture
async def db(tmp_path):
    database = Database.from_url(f"sqlite:////{tmp_path}/raw.db")
    await database.connect()
    yield database
    await database.disconnect()


@pytest.fixture
async def seeded_db(tmp_path):
    database = Database.from_url(f"sqlite:////{tmp_path}/seeded_raw.db")
    await database.connect()
    await database._adapter._conn.execute(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, status TEXT, visits INT
        )
        """
    )
    await database.insert_many(
        "users",
        [
            {"name": "alice", "status": "open", "visits": 1},
            {"name": "bob", "status": "open", "visits": 5},
            {"name": "carol", "status": "closed", "visits": 5},
        ],
    )
    yield database
    await database.disconnect()


# -- §1.8 #27: raw -----------------------------------------------------------------


async def test_raw_before_connect_raises(tmp_path):
    database = Database.from_url(f"sqlite:////{tmp_path}/guard.db")
    with pytest.raises(ConnectionNotOpenError):
        await database.raw("SELECT 1")


async def test_raw_select_returns_plain_dicts(seeded_db):
    rows = await seeded_db.raw("SELECT name FROM users ORDER BY id")
    assert rows == [{"name": "alice"}, {"name": "bob"}, {"name": "carol"}]


async def test_raw_positional_params(seeded_db):
    rows = await seeded_db.raw(
        "SELECT name FROM users WHERE visits > ? AND status = ? ORDER BY id",
        [4, "open"],
    )
    assert rows == [{"name": "bob"}]


async def test_raw_named_params_dict(seeded_db):
    rows = await seeded_db.raw(
        "SELECT name FROM users WHERE status = :status ORDER BY id",
        {"status": "closed"},
    )
    assert rows == [{"name": "carol"}]


async def test_raw_select_no_match_returns_empty_list(seeded_db):
    assert await seeded_db.raw("SELECT * FROM users WHERE name = ?", ["nobody"]) == []


async def test_raw_write_commits_durably(seeded_db, tmp_path):
    # The write must survive beyond this connection's implicit transaction:
    # a second, independent connection only sees committed data.
    await seeded_db.raw(
        "INSERT INTO users (name, status, visits) VALUES (?, ?, ?)",
        ["dave", "open", 9],
    )
    async with aiosqlite.connect(tmp_path / "seeded_raw.db") as witness:
        witness.row_factory = aiosqlite.Row
        cursor = await witness.execute(
            "SELECT COUNT(*) AS n FROM users WHERE name = 'dave'"
        )
        row = await cursor.fetchone()
    assert row["n"] == 1


async def test_raw_write_visible_through_the_abstraction(seeded_db):
    await seeded_db.raw("UPDATE users SET visits = visits + 10 WHERE status = ?", ["open"])
    names = {
        r["name"]: r["visits"] for r in await seeded_db.find("users", {"visits": {"$gt": 9}})
    }
    assert names == {"alice": 11, "bob": 15}


async def test_raw_joins_an_open_transaction_on_rollback(db):
    await db._adapter._conn.execute(
        "CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, a INT)"
    )
    async with db.transaction() as tx:
        await tx.raw("INSERT INTO t (a) VALUES (?)", [1])
        await tx.rollback()
    assert await db.count("t") == 0


async def test_raw_joins_an_open_transaction_on_commit(db):
    await db._adapter._conn.execute(
        "CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, a INT)"
    )
    async with db.transaction() as tx:
        await tx.raw("INSERT INTO t (a) VALUES (?)", [1])
    assert await db.count("t") == 1


async def test_failed_raw_aborts_open_transaction(seeded_db):
    with pytest.raises(PolydbQueryError):
        async with seeded_db.transaction() as tx:
            await tx.raw("UPDATE users SET visits = 0 WHERE status = 'open'")
            await tx.raw("SELECT * FROM no_such_table")
    # Poisoned-transaction precedent: the handle refuses further work and the
    # first statement's effects were rolled back wholesale.
    with pytest.raises(TransactionInactiveError):
        await tx.count("users", {})
    assert {r["name"] for r in await seeded_db.find("users", {"visits": 0})} == set()


async def test_raw_driver_error_wrapped_in_polydb_query_error(seeded_db):
    with pytest.raises(PolydbQueryError):
        await seeded_db.raw("SELECT * FROM no_such_table")


@pytest.mark.parametrize("bad_query", [None, 42, "", "   "])
async def test_raw_requires_non_empty_sql_string(seeded_db, bad_query):
    with pytest.raises(InvalidFilterError):
        await seeded_db.raw(bad_query)


@pytest.mark.parametrize("bad_params", [42, "alice", b"alice", {"too", "many"}])
async def test_raw_rejects_unsupported_param_shapes(seeded_db, bad_params):
    with pytest.raises(InvalidFilterError):
        await seeded_db.raw("SELECT * FROM users WHERE name = ?", bad_params)


# -- §1.8 #28: explain --------------------------------------------------------------


async def test_explain_before_connect_raises(tmp_path):
    database = Database.from_url(f"sqlite:////{tmp_path}/guard.db")
    with pytest.raises(ConnectionNotOpenError):
        await database.explain("users", {})


async def test_explain_reports_translated_filter(seeded_db):
    plan = await seeded_db.explain("users", {"status": "open", "visits": {"$gte": 2}})
    assert plan["backend"] == "sqlite"
    assert "`visits` >= ?" in plan["sql"]
    assert plan["params"] == ["open", 2]
    assert len(plan["plan"]) == 1
    detail = plan["plan"][0]["detail"]
    assert "users" in detail


async def test_explain_sees_a_created_index(seeded_db):
    await seeded_db.create_index("users", ["status"])
    plan = await seeded_db.explain("users", {"status": "open"})
    assert any("USING INDEX" in row["detail"] for row in plan["plan"])


async def test_explain_missing_table_wraps_driver_error(seeded_db):
    with pytest.raises(PolydbQueryError):
        await seeded_db.explain("no_such_table", {})


async def test_explain_rejects_malformed_filter_dsl(seeded_db):
    with pytest.raises(InvalidFilterError):
        await seeded_db.explain("users", {"visits": {"$bogus": 1}})
