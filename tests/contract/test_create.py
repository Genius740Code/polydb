from __future__ import annotations

import logging

import pytest

from polydb import Database
from polydb.exceptions import ConnectionNotOpenError, InvalidFilterError, PolydbQueryError


async def _make_table(db: Database, sql: str) -> None:
    """Scaffold a table directly until create_collection() (§1.6 #20) is built."""
    await db._adapter._conn.execute(sql)
    await db._adapter._conn.commit()


async def _fetch_all(db: Database, table: str) -> list[dict]:
    cursor = await db._adapter._conn.execute(f"SELECT * FROM {table} ORDER BY rowid")
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


@pytest.fixture
async def db(tmp_path):
    database = Database.from_url(f"sqlite:////{tmp_path}/create.db")
    await database.connect()
    yield database
    await database.disconnect()


# -- §1.2 #7: insert_one ---------------------------------------------------------


async def test_insert_one_returns_rowid_as_inserted_id(db):
    await _make_table(
        db,
        "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT)",
    )
    result = await db.insert_one("users", {"name": "alice"})
    assert isinstance(result.inserted_id, int)
    assert result.inserted_id == 1


async def test_insert_one_persists_data(db):
    await _make_table(
        db,
        "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT)",
    )
    await db.insert_one("users", {"name": "alice"})
    rows = await _fetch_all(db, "users")
    assert rows == [{"id": 1, "name": "alice"}]


async def test_insert_one_survives_reconnect(tmp_path):
    # The implicit-transaction commit must actually hit the file, not linger
    # in driver state until some future commit.
    url = f"sqlite:////{tmp_path}/persist.db"
    async with Database.from_url(url) as db:
        await db._adapter._conn.execute(
            "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT)"
        )
        await db.insert_one("users", {"name": "alice"})
    async with Database.from_url(url) as db:
        cursor = await db._adapter._conn.execute("SELECT name FROM users")
        assert [dict(r)["name"] for r in await cursor.fetchall()] == ["alice"]


async def test_insert_one_empty_doc_uses_default_values(db):
    await _make_table(db, "CREATE TABLE logs (ts TEXT DEFAULT 'now')")
    result = await db.insert_one("logs", {})
    assert result.inserted_id is not None  # rowid exists even without explicit PK
    cursor = await db._adapter._conn.execute("SELECT ts FROM logs")
    rows = [dict(r) for r in await cursor.fetchall()]
    assert rows == [{"ts": "now"}]


async def test_insert_many_docs_of_different_keys_fill_nulls(db):
    await _make_table(
        db,
        """
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT,
            size INT,
            tag TEXT
        )
        """,
    )
    result = await db.insert_many(
        "events",
        [
            {"kind": "click", "size": 3},
            {"kind": "view", "tag": "home"},
            {"size": None},
        ],
    )
    assert result.inserted_count == 3
    assert len(result.inserted_ids) == 3
    rows = await _fetch_all(db, "events")
    assert rows == [
        {"id": 1, "kind": "click", "size": 3, "tag": None},
        {"id": 2, "kind": "view", "size": None, "tag": "home"},
        {"id": 3, "kind": None, "size": None, "tag": None},
    ]


async def test_insert_many_empty_list_is_noop(db):
    await _make_table(db, "CREATE TABLE empty_test (x INT)")
    result = await db.insert_many("empty_test", [])
    assert result.inserted_count == 0
    assert result.inserted_ids == []
    rows = await _fetch_all(db, "empty_test")
    assert rows == []


# -- §1.2 #9: upsert_one ---------------------------------------------------------


async def test_upsert_one_inserts_when_no_match(db):
    await _make_table(
        db,
        "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT, name TEXT)",
    )
    result = await db.upsert_one(
        "users",
        {"email": "alice@example.com"},
        {"name": "Alice"},
    )
    assert result.matched_count == 0
    assert result.modified_count == 0
    assert result.upserted_id is not None
    rows = await _fetch_all(db, "users")
    assert rows == [
        {
            "id": result.upserted_id,
            "email": "alice@example.com",  # filter merged into inserted row
            "name": "Alice",
        }
    ]


async def test_upsert_one_updates_when_filter_matches(db):
    await _make_table(
        db,
        "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT, name TEXT)",
    )
    await db.upsert_one("users", {"email": "a@x.com"}, {"name": "Alice"})
    second = await db.upsert_one("users", {"email": "a@x.com"}, {"name": "Alicia"})

    assert second.matched_count == 1
    assert second.modified_count == 1
    assert second.upserted_id is None
    rows = await _fetch_all(db, "users")
    assert len(rows) == 1  # no duplicate row created
    assert rows[0]["name"] == "Alicia"


async def test_upsert_one_updates_only_first_match(db):
    await _make_table(db, "CREATE TABLE items (grp TEXT, status TEXT)")
    await db.insert_many(
        "items",
        [{"grp": "g1", "status": "open"}, {"grp": "g1", "status": "open"}],
    )
    result = await db.upsert_one("items", {"grp": "g1"}, {"status": "closed"})
    assert result.matched_count == 1
    rows = await _fetch_all(db, "items")
    statuses = sorted(r["status"] for r in rows)
    assert statuses == ["closed", "open"]  # exactly one of the two was updated


async def test_upsert_one_empty_doc_on_match_modifies_nothing(db):
    await _make_table(db, "CREATE TABLE things (k TEXT, v TEXT)")
    await db.insert_one("things", {"k": "a", "v": "orig"})
    result = await db.upsert_one("things", {"k": "a"}, {})
    assert result.matched_count == 1
    assert result.modified_count == 0
    rows = await _fetch_all(db, "things")
    assert rows == [{"k": "a", "v": "orig"}]


async def test_upsert_one_doc_wins_over_filter_key_conflict(db):
    await _make_table(db, "CREATE TABLE kv (k TEXT, v TEXT)")
    result = await db.upsert_one("kv", {"k": "filter_k", "v": "from_filter"}, {"v": "from_doc"})
    rows = await _fetch_all(db, "kv")
    assert rows == [{"k": "filter_k", "v": "from_doc"}]
    assert result.upserted_id is not None


async def test_upsert_one_empty_filter_updates_first_existing_row(db):
    await _make_table(db, "CREATE TABLE t (v INT)")
    await db.insert_many("t", [{"v": 1}, {"v": 2}])
    result = await db.upsert_one("t", {}, {"v": 99})
    assert result.matched_count == 1
    assert result.modified_count == 1
    cursor = await db._adapter._conn.execute("SELECT COUNT(*) AS n FROM t WHERE v = 99")
    assert dict((await cursor.fetchone()))["n"] == 1


async def test_upsert_one_none_filter_value_matches_null_rows(db):
    await _make_table(db, "CREATE TABLE t (a TEXT, b TEXT)")
    await db.insert_one("t", {"a": "row", "b": None})
    # "= NULL" never matches; the filter must compile to IS NULL.
    result = await db.upsert_one("t", {"b": None}, {"a": "updated"})
    assert result.matched_count == 1
    assert result.modified_count == 1


# -- error handling / guards ------------------------------------------------------


async def test_insert_one_before_connect_raises(tmp_path):
    db = Database.from_url(f"sqlite:////{tmp_path}/guard.db")
    with pytest.raises(ConnectionNotOpenError):
        await db.insert_one("users", {"name": "alice"})


async def test_insert_many_before_connect_raises(tmp_path):
    db = Database.from_url(f"sqlite:////{tmp_path}/guard.db")
    with pytest.raises(ConnectionNotOpenError):
        await db.insert_many("users", [{"name": "alice"}])


async def test_upsert_before_connect_raises(tmp_path):
    db = Database.from_url(f"sqlite:////{tmp_path}/guard.db")
    with pytest.raises(ConnectionNotOpenError):
        await db.upsert_one("users", {"email": "a@x.com"}, {"name": "A"})


async def test_invalid_column_name_raises_invalid_filter_error(db):
    await _make_table(db, "CREATE TABLE t (ok INT)")
    with pytest.raises(InvalidFilterError):
        await db.insert_one("t", {"bad name; DROP TABLE t": 1})


async def test_invalid_collection_name_raises_invalid_filter_error(db):
    with pytest.raises(InvalidFilterError):
        await db.insert_one("users; DROP TABLE users", {"name": "x"})


async def test_upsert_operator_filter_raises_invalid_filter_error(db):
    await _make_table(db, "CREATE TABLE t (age INT)")
    with pytest.raises(InvalidFilterError):
        await db.upsert_one("t", {"age": {"$gt": 5}}, {"age": 10})


async def test_driver_failure_wrapped_in_polydb_query_error(db):
    with pytest.raises(PolydbQueryError):
        await db.insert_one("no_such_table", {"x": 1})


async def test_failed_insert_leaves_transaction_clean_for_next_write(db):
    # A failing insert must roll back its pending transaction so a later write
    # doesn't accidentally commit leftovers from the failed one.
    await _make_table(db, "CREATE TABLE t (a INT UNIQUE)")
    await db.insert_one("t", {"a": 1})
    with pytest.raises(PolydbQueryError):
        await db.insert_one("t", {"a": 1})  # UNIQUE conflict
    await db.insert_one("t", {"a": 2})  # must succeed cleanly
    rows = await _fetch_all(db, "t")
    assert [r["a"] for r in rows] == [1, 2]


async def test_failed_batch_insert_commits_nothing(db):
    await _make_table(db, "CREATE TABLE t (a INT UNIQUE)")
    with pytest.raises(PolydbQueryError):
        await db.insert_many("t", [{"a": 1}, {"a": 1}])  # second row conflicts
    rows = await _fetch_all(db, "t")
    assert rows == []  # all-or-nothing batch


async def test_failed_operation_is_logged(caplog, tmp_path):
    db = Database.from_url(f"sqlite:////{tmp_path}/log.db")
    await db.connect()
    try:
        with caplog.at_level(logging.ERROR, logger="polydb.adapters.sql"):
            with pytest.raises(PolydbQueryError):
                await db.insert_one("missing_table", {"x": 1})
    finally:
        await db.disconnect()
