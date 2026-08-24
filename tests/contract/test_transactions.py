from __future__ import annotations

import pytest

from polydb import Database
from polydb.exceptions import (
    ConnectionNotOpenError,
    PolydbQueryError,
    TransactionInactiveError,
    UnsupportedOperationError,
)


async def _make_table(db: Database, sql: str) -> None:
    """Scaffold a table directly, outside any polydb-managed transaction."""
    await db._adapter._conn.execute(sql)
    await db._adapter._conn.commit()


async def _fetch_all(db: Database, table: str) -> list[dict]:
    cursor = await db._adapter._conn.execute(f"SELECT * FROM {table} ORDER BY rowid")
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


@pytest.fixture
async def db(tmp_path):
    database = Database.from_url(f"sqlite:////{tmp_path}/transactions.db")
    await database.connect()
    yield database
    await database.disconnect()


# -- §1.7 #25: opening a transaction ------------------------------------------------


async def test_transaction_requires_connection(tmp_path):
    database = Database.from_url(f"sqlite:////{tmp_path}/notconnected.db")
    with pytest.raises(ConnectionNotOpenError):
        database.transaction()


async def test_nested_transaction_raises_unsupported(db):
    await _make_table(db, "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
    async with db.transaction() as tx:
        with pytest.raises(UnsupportedOperationError):
            db.transaction()
        # the first transaction keeps working after the rejected second one
        await tx.insert_one("users", {"name": "alice"})
    assert len(await _fetch_all(db, "users")) == 1


async def test_transaction_handle_reuse_raises(db):
    await _make_table(db, "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
    handle = db.transaction()
    async with handle:
        pass
    with pytest.raises(TransactionInactiveError):
        await handle.__aenter__()


async def test_sequential_transactions_work_independently(db):
    await _make_table(db, "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
    async with db.transaction() as tx:
        await tx.insert_one("users", {"name": "alice"})
    async with db.transaction() as tx:
        await tx.insert_one("users", {"name": "bob"})
    rows = await _fetch_all(db, "users")
    assert [r["name"] for r in rows] == ["alice", "bob"]


# -- §1.7 #25/#26: atomicity on context-manager exit ---------------------------------


async def test_clean_exit_commits_all_operations(db):
    await _make_table(db, "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
    async with db.transaction() as tx:
        await tx.insert_one("users", {"name": "alice"})
        await tx.insert_one("users", {"name": "bob"})
        await tx.update_many("users", {"name": "alice"}, {"$set": {"name": "ALICE"}})
    rows = {r["name"] for r in await _fetch_all(db, "users")}
    assert rows == {"ALICE", "bob"}


async def test_exception_rolls_back_every_operation(db):
    await _make_table(db, "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")

    with pytest.raises(RuntimeError, match="boom"):
        async with db.transaction() as tx:
            await tx.insert_one("users", {"name": "alice"})
            await tx.insert_many("users", [{"name": "bob"}, {"name": "carol"}])
            raise RuntimeError("boom")

    assert await _fetch_all(db, "users") == []
    # adapter stays fully usable after the rollback
    await db.insert_one("users", {"name": "post"})
    assert [r["name"] for r in await _fetch_all(db, "users")] == ["post"]


async def test_reads_inside_transaction_see_uncommitted_writes(db):
    await _make_table(db, "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
    async with db.transaction() as tx:
        await tx.insert_one("users", {"name": "alice"})
        assert await tx.count("users") == 1
        found = await tx.find_one("users", {"name": "alice"})
        assert found is not None and found["name"] == "alice"
    assert await db.count("users") == 1


async def test_direct_adapter_calls_join_the_open_transaction(db):
    # The adapter's single connection means calls made directly on `db` while
    # a transaction is open execute inside it — their per-call commits are
    # suppressed until the transaction ends.
    await _make_table(db, "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")

    with pytest.raises(RuntimeError):
        async with db.transaction():
            await db.insert_one("users", {"name": "direct"})
            raise RuntimeError

    assert await _fetch_all(db, "users") == []


# -- §1.7 #26: explicit commit / rollback ---------------------------------------------


async def test_explicit_commit_persists_and_finalizes(db):
    await _make_table(db, "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
    async with db.transaction() as tx:
        await tx.insert_one("users", {"name": "alice"})
        await tx.commit()
        assert [r["name"] for r in await _fetch_all(db, "users")] == ["alice"]
        with pytest.raises(TransactionInactiveError):
            await tx.insert_one("users", {"name": "late"})


async def test_explicit_commit_then_clean_exit_does_not_double_commit(db):
    await _make_table(db, "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
    async with db.transaction() as tx:
        await tx.insert_one("users", {"name": "alice"})
        await tx.commit()
    assert [r["name"] for r in await _fetch_all(db, "users")] == ["alice"]


async def test_explicit_rollback_discards_writes(db):
    await _make_table(db, "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
    async with db.transaction() as tx:
        await tx.insert_one("users", {"name": "alice"})
        await tx.rollback()
        assert await _fetch_all(db, "users") == []
        with pytest.raises(TransactionInactiveError):
            await tx.delete_many("users", {})


async def test_operations_on_unentered_transaction_raise(db):
    await _make_table(db, "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
    tx = db.transaction()
    with pytest.raises(TransactionInactiveError):
        await tx.insert_one("users", {"name": "alice"})
    with pytest.raises(TransactionInactiveError):
        await tx.commit()


# -- failed statements abort the whole transaction -------------------------------------


async def test_failed_statement_aborts_transaction(db):
    await _make_table(db, "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")

    async with db.transaction() as tx:
        await tx.insert_one("users", {"id": 1, "name": "first"})
        with pytest.raises(PolydbQueryError):
            await tx.insert_one("users", {"id": 1, "name": "duplicate"})

        # poisoned like a Postgres transaction: further ops refuse
        with pytest.raises(TransactionInactiveError):
            await tx.insert_one("users", {"id": 2, "name": "after"})

    # everything inside was rolled back; clean exit must not re-commit anything
    assert await _fetch_all(db, "users") == []


async def test_failure_is_atomic_across_collections(db):
    await _make_table(db, "CREATE TABLE orders (id INTEGER PRIMARY KEY, item TEXT)")
    await _make_table(
        db,
        "CREATE TABLE ledger (order_id INTEGER PRIMARY KEY, amount REAL)",
    )

    with pytest.raises(PolydbQueryError):
        async with db.transaction() as tx:
            await tx.insert_one("orders", {"id": 1, "item": "widget"})
            await tx.insert_one("ledger", {"amount": 9.99})
            await tx.insert_one("orders", {"id": 1, "item": "bad"})  # PK violation

    assert await db.count("orders") == 0
    assert await db.count("ledger") == 0


# -- DDL is transactional too (SQLite rolls back CREATE/DROP) ---------------------------


async def test_create_collection_rolls_back_with_transaction(db):
    from polydb.schema import Field, FieldType, Schema

    schema = Schema(fields=[Field(name="name", type=FieldType.STR)])
    with pytest.raises(RuntimeError):
        async with db.transaction() as tx:
            await tx.create_collection("temp_things", schema)
            assert "temp_things" in await tx.list_collections()
            raise RuntimeError

    assert "temp_things" not in await db.list_collections()
