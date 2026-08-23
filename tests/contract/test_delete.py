from __future__ import annotations

import pytest

from polydb import Database
from polydb.exceptions import ConnectionNotOpenError, InvalidFilterError, PolydbQueryError


@pytest.fixture
async def db(tmp_path):
    database = Database.from_url(f"sqlite:////{tmp_path}/delete.db")
    await database.connect()
    yield database
    await database.disconnect()


@pytest.fixture
async def seeded_db(tmp_path):
    database = Database.from_url(f"sqlite:////{tmp_path}/seeded_delete.db")
    await database.connect()
    await database._adapter._conn.execute(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, status TEXT, visits INT, note TEXT
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


async def _rows(db: Database) -> list[dict]:
    cursor = await db._adapter._conn.execute("SELECT * FROM users ORDER BY id")
    return [dict(r) for r in await cursor.fetchall()]


# -- §1.5 #18: delete_one ---------------------------------------------------------


async def test_delete_one_removes_first_match_only(seeded_db):
    result = await seeded_db.delete_one("users", {"status": "open"})
    assert result.deleted_count == 1
    rows = await _rows(seeded_db)
    assert [r["name"] for r in rows] == ["bob", "carol"]  # alice (first match) gone


async def test_delete_one_no_match_deletes_nothing(seeded_db):
    result = await seeded_db.delete_one("users", {"name": "nobody"})
    assert result.deleted_count == 0
    assert len(await _rows(seeded_db)) == 3


async def test_delete_one_accepts_full_filter_dsl(seeded_db):
    # §2.2 operators apply to delete filters too ($gte + $or here).
    result = await seeded_db.delete_one(
        "users",
        {"visits": {"$gte": 5}, "$or": [{"name": "bob"}, {"name": "carol"}]},
    )
    assert result.deleted_count == 1
    rows = await _rows(seeded_db)
    assert [r["name"] for r in rows] == ["alice", "carol"]  # bob (first match) gone


async def test_delete_one_null_filter_matches_null_column(seeded_db):
    # None compiles to IS NULL — plain equality never matches NULL.
    result = await seeded_db.delete_one("users", {"note": None})
    assert result.deleted_count == 1
    rows = await _rows(seeded_db)
    assert [r["name"] for r in rows] == ["bob", "carol"]


# -- §1.5 #19: delete_many --------------------------------------------------------


async def test_delete_many_removes_every_match_and_counts(seeded_db):
    result = await seeded_db.delete_many("users", {"visits": 5})
    assert result.deleted_count == 2
    rows = await _rows(seeded_db)
    assert [r["name"] for r in rows] == ["alice"]


async def test_delete_many_empty_filter_clears_the_table(seeded_db):
    # Mongo parity: delete_many({}) empties the collection.
    result = await seeded_db.delete_many("users", {})
    assert result.deleted_count == 3
    assert await _rows(seeded_db) == []


async def test_delete_many_no_match_returns_zero(seeded_db):
    result = await seeded_db.delete_many("users", {"name": "nobody"})
    assert result.deleted_count == 0
    assert len(await _rows(seeded_db)) == 3


async def test_delete_many_dsl_filter_deletes_only_matches(seeded_db):
    result = await seeded_db.delete_many(
        "users", {"status": {"$ne": "closed"}, "name": {"$in": ["alice", "carol"]}}
    )
    assert result.deleted_count == 1
    rows = await _rows(seeded_db)
    assert [r["name"] for r in rows] == ["bob", "carol"]


# -- error handling / guards ------------------------------------------------------


async def test_delete_one_before_connect_raises(tmp_path):
    database = Database.from_url(f"sqlite:////{tmp_path}/guard.db")
    with pytest.raises(ConnectionNotOpenError):
        await database.delete_one("t", {})


async def test_delete_many_before_connect_raises(tmp_path):
    database = Database.from_url(f"sqlite:////{tmp_path}/guard.db")
    with pytest.raises(ConnectionNotOpenError):
        await database.delete_many("t", {})


async def test_invalid_column_name_in_filter_rejected(seeded_db):
    with pytest.raises(InvalidFilterError):
        await seeded_db.delete_one("users", {"bad name; DROP": 1})
    with pytest.raises(InvalidFilterError):
        await seeded_db.delete_many("users", {"bad name; DROP": 1})


async def test_driver_failure_wrapped_in_polydb_query_error(seeded_db):
    with pytest.raises(PolydbQueryError):
        await seeded_db.delete_one("no_such_table", {})
    with pytest.raises(PolydbQueryError):
        await seeded_db.delete_many("no_such_table", {})


async def test_failed_delete_leaves_transaction_clean_for_next_write(db):
    await db._adapter._conn.execute(
        "CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, a INT)"
    )
    await db.insert_many("t", [{"a": 1}, {"a": 2}])
    with pytest.raises(PolydbQueryError):
        await db.delete_many("no_such_table", {})  # fails mid-transaction
    # Rollback must have freed the transaction: the next write succeeds and
    # no partial work from the failed statement survives.
    await db.delete_one("t", {"a": 1})
    rows = await db.find("t")
    assert [r["a"] for r in rows] == [2]
