from __future__ import annotations

import pytest

from polydb import Database
from polydb.exceptions import (
    ConnectionNotOpenError,
    InvalidFilterError,
    PolydbQueryError,
    UnsupportedOperationError,
)


@pytest.fixture
async def db(tmp_path):
    database = Database.from_url(f"sqlite:////{tmp_path}/update.db")
    await database.connect()
    yield database
    await database.disconnect()


@pytest.fixture
async def seeded_db(tmp_path):
    database = Database.from_url(f"sqlite:////{tmp_path}/seeded_update.db")
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


# -- §1.4 #15: update_one ---------------------------------------------------------


async def test_update_one_set_updates_first_match_only(seeded_db):
    result = await seeded_db.update_one(
        "users", {"status": "open"}, {"$set": {"status": "held"}}
    )
    assert result.matched_count == 1
    assert result.modified_count == 1
    rows = await _rows(seeded_db)
    statuses = [r["status"] for r in rows]
    assert statuses == ["held", "open", "closed"]  # only the first match changed


async def test_update_one_returns_zero_counts_when_no_match(seeded_db):
    result = await seeded_db.update_one(
        "users", {"name": "nobody"}, {"$set": {"status": "x"}}
    )
    assert result.matched_count == 0
    assert result.modified_count == 0
    assert len(await _rows(seeded_db)) == 3


async def test_update_one_accepts_full_filter_dsl(seeded_db):
    # §2.2 operators apply to update filters too ($gte here).
    result = await seeded_db.update_one(
        "users",
        {"visits": {"$gte": 5}, "$or": [{"name": "bob"}, {"name": "carol"}]},
        {"$set": {"note": "seen"}},
    )
    assert result.matched_count == 1
    rows = await _rows(seeded_db)
    assert [r["note"] for r in rows] == [None, "seen", None]  # bob (first match)


async def test_update_one_inc_adds_to_existing_value(seeded_db):
    await seeded_db.update_one("users", {"name": "bob"}, {"$inc": {"visits": 10}})
    row = await seeded_db.find_one("users", {"name": "bob"})
    assert row["visits"] == 15


async def test_update_one_inc_on_null_column_stays_null(seeded_db):
    # Known SQL-semantics divergence from Mongo: NULL + x is NULL in SQL,
    # while Mongo treats a missing field as 0 before incrementing.
    await seeded_db.update_one("users", {"name": "alice"}, {"$inc": {"note": 1}})
    row = await seeded_db.find_one("users", {"name": "alice"})
    assert row["note"] is None


async def test_update_one_unset_sets_column_null(seeded_db):
    await seeded_db.update_one("users", {"name": "carol"}, {"$unset": {"status": True}})
    row = await seeded_db.find_one("users", {"name": "carol"})
    assert row["status"] is None


async def test_update_one_combined_operators_apply_together(seeded_db):
    await seeded_db.update_one(
        "users",
        {"name": "alice"},
        {"$set": {"status": "active"}, "$inc": {"visits": 4}, "$unset": {"note": ""}},
    )
    row = await seeded_db.find_one("users", {"name": "alice"})
    assert row["status"] == "active"
    assert row["visits"] == 5
    assert row["note"] is None


async def test_update_one_empty_update_reports_match_without_modifying(seeded_db):
    result = await seeded_db.update_one("users", {"name": "alice"}, {})
    assert result.matched_count == 1
    assert result.modified_count == 0
    rows = await _rows(seeded_db)
    assert rows[0]["status"] == "open"


async def test_update_one_setting_same_values_still_reports_modified(seeded_db):
    # Divergence from Mongo documented on the method: SQL counts the row the
    # UPDATE ran against even when values were already equal.
    result = await seeded_db.update_one(
        "users", {"name": "alice"}, {"$set": {"status": "open"}}
    )
    assert result.modified_count == 1


# -- §1.4 #16: update_many --------------------------------------------------------


async def test_update_many_updates_every_match_and_counts(seeded_db):
    result = await seeded_db.update_many(
        "users", {"visits": 5}, {"$set": {"status": "reviewed"}}
    )
    assert result.matched_count == 2
    assert result.modified_count == 2
    rows = await _rows(seeded_db)
    assert [r["status"] for r in rows] == ["open", "reviewed", "reviewed"]


async def test_update_many_empty_filter_matches_all_rows(seeded_db):
    result = await seeded_db.update_many("users", {}, {"$inc": {"visits": 1}})
    assert result.matched_count == 3
    assert result.modified_count == 3
    assert [r["visits"] for r in await _rows(seeded_db)] == [2, 6, 6]


async def test_update_many_no_match_returns_zeros(seeded_db):
    result = await seeded_db.update_many(
        "users", {"name": "nobody"}, {"$set": {"status": "x"}}
    )
    assert result.matched_count == 0
    assert result.modified_count == 0


async def test_update_many_empty_update_counts_matches_only(seeded_db):
    result = await seeded_db.update_many("users", {"status": "open"}, {})
    assert result.matched_count == 2
    assert result.modified_count == 0


# -- §1.4 #17: replace_one ----------------------------------------------------------


async def test_replace_one_rewrites_row_and_nulls_missing_columns(db):
    await db._adapter._conn.execute(
        "CREATE TABLE docs (id INTEGER PRIMARY KEY AUTOINCREMENT, a TEXT, b INT)"
    )
    await db.insert_one("docs", {"a": "old", "b": 7})
    result = await db.replace_one("docs", {"a": "old"}, {"b": 42})

    assert result.matched_count == 1
    assert result.modified_count == 1
    row = await db.find_one("docs", {"b": 42})
    assert row == {"id": 1, "a": None, "b": 42}  # absent column replaced with NULL


async def test_replace_one_preserves_primary_key(db):
    await db._adapter._conn.execute(
        "CREATE TABLE docs (id INTEGER PRIMARY KEY AUTOINCREMENT, a TEXT)"
    )
    await db.insert_many("docs", [{"a": "keep"}, {"a": "gone"}])
    target_id = (await db.find_one("docs", {"a": "gone"}))["id"]

    await db.replace_one("docs", {"a": "gone"}, {"a": "new"})

    replaced = await db.find_one("docs", {"id": target_id})
    assert replaced == {"id": target_id, "a": "new"}  # same identity, new content
    assert len(await db.find("docs")) == 2  # no row deleted or duplicated
    assert await db.exists("docs", {"a": "keep"})  # the other row untouched


async def test_replace_one_rejects_primary_key_in_doc(db):
    await db._adapter._conn.execute(
        "CREATE TABLE docs (id INTEGER PRIMARY KEY AUTOINCREMENT, a TEXT)"
    )
    await db.insert_one("docs", {"a": "x"})
    with pytest.raises(InvalidFilterError):
        await db.replace_one("docs", {"a": "x"}, {"id": 99})


async def test_replace_one_no_match_writes_nothing(db):
    await db._adapter._conn.execute(
        "CREATE TABLE docs (id INTEGER PRIMARY KEY AUTOINCREMENT, a TEXT, b INT)"
    )
    await db.insert_one("docs", {"a": "x"})
    result = await db.replace_one("docs", {"a": "missing"}, {"a": "y"})
    assert result.matched_count == 0
    assert result.modified_count == 0
    row = await db.find_one("docs", {"a": "x"})
    assert row == {"id": 1, "a": "x", "b": None}  # untouched — never upserts


async def test_replace_one_on_missing_table_raises_query_error(db):
    with pytest.raises(PolydbQueryError):
        await db.replace_one("no_such_table", {}, {"a": 1})


# -- error handling / guards ------------------------------------------------------


async def test_update_one_before_connect_raises(tmp_path):
    database = Database.from_url(f"sqlite:////{tmp_path}/guard.db")
    with pytest.raises(ConnectionNotOpenError):
        await database.update_one("t", {}, {"$set": {"a": 1}})


async def test_update_many_before_connect_raises(tmp_path):
    database = Database.from_url(f"sqlite:////{tmp_path}/guard.db")
    with pytest.raises(ConnectionNotOpenError):
        await database.update_many("t", {}, {"$set": {"a": 1}})


async def test_replace_one_before_connect_raises(tmp_path):
    database = Database.from_url(f"sqlite:////{tmp_path}/guard.db")
    with pytest.raises(ConnectionNotOpenError):
        await database.replace_one("t", {}, {"a": 1})


async def test_push_update_raises_unsupported_operation(seeded_db):
    with pytest.raises(UnsupportedOperationError):
        await seeded_db.update_one("users", {"name": "alice"}, {"$push": {"tags": "x"}})
    with pytest.raises(UnsupportedOperationError):
        await seeded_db.update_many("users", {}, {"$push": {"tags": "x"}})


async def test_operatorless_update_rejected(seeded_db):
    with pytest.raises(InvalidFilterError):  # replacement docs belong to replace_one
        await seeded_db.update_one("users", {"name": "alice"}, {"status": "closed"})


async def test_invalid_column_name_in_update_rejected(seeded_db):
    with pytest.raises(InvalidFilterError):
        await seeded_db.update_one("users", {}, {"$set": {"bad name; DROP": 1}})


async def test_driver_failure_wrapped_in_polydb_query_error(seeded_db):
    with pytest.raises(PolydbQueryError):
        await seeded_db.update_one("no_such_table", {}, {"$set": {"a": 1}})
    with pytest.raises(PolydbQueryError):
        await seeded_db.update_many("no_such_table", {}, {"$set": {"a": 1}})


async def test_failed_update_leaves_transaction_clean_for_next_write(db):
    await db._adapter._conn.execute(
        "CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, a INT UNIQUE)"
    )
    await db.insert_many("t", [{"a": 1}, {"a": 2}])
    with pytest.raises(PolydbQueryError):
        await db.update_many("t", {"a": 2}, {"$set": {"a": 1}})  # UNIQUE conflict
    # Rollback must have freed the transaction: the next write succeeds and
    # the failed batch's partial work is gone.
    await db.update_one("t", {"a": 1}, {"$inc": {"a": 10}})
    rows = await db.find("t")
    assert sorted(r["a"] for r in rows) == [2, 11]
