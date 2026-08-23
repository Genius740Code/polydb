from __future__ import annotations

import pytest

from polydb import Database
from polydb.exceptions import (
    ConnectionNotOpenError,
    InvalidFilterError,
    PolydbQueryError,
    SchemaRequiredError,
)
from polydb.schema import Field, FieldType, Schema


@pytest.fixture
async def db(tmp_path):
    database = Database.from_url(f"sqlite:///{tmp_path}/schema.db")
    await database.connect()
    yield database
    await database.disconnect()


def _schema() -> Schema:
    return Schema(
        fields=[
            Field(name="id", type=FieldType.INT, primary_key=True),
            Field(name="name", type=FieldType.STR),
            Field(name="age", type=FieldType.INT, nullable=False),
            Field(name="score", type=FieldType.FLOAT, default=0.0),
            Field(name="active", type=FieldType.BOOL, default=True),
            Field(name="email", type=FieldType.STR, unique=True),
        ]
    )


# -- §1.6 #20: create_collection --------------------------------------------------


async def test_create_collection_creates_usable_table(db):
    await db.create_collection("users", _schema())

    result = await db.insert_one(
        "users",
        {"id": 1, "name": "alice", "age": 30, "email": "a@x.com"},
    )
    assert result.inserted_id == 1  # INTEGER PRIMARY KEY aliases rowid

    doc = await db.find_one("users", {"id": 1})
    assert doc["name"] == "alice"
    assert doc["score"] == 0.0  # DEFAULT applied for the omitted column
    assert doc["active"] == 1  # bool defaults render as 1/0


async def test_create_collection_round_trips_all_field_types(db):
    schema = Schema(
        fields=[
            Field(name="id", type=FieldType.INT, primary_key=True),
            Field(name="s", type=FieldType.STR),
            Field(name="i", type=FieldType.INT),
            Field(name="f", type=FieldType.FLOAT),
            Field(name="b", type=FieldType.BOOL),
            Field(name="ts", type=FieldType.DATETIME),
            Field(name="payload", type=FieldType.JSON),
        ]
    )
    await db.create_collection("mixed", schema)
    await db.insert_one(
        "mixed",
        {
            "id": 7,
            "s": "text",
            "i": -3,
            "f": 2.5,
            "b": True,
            "ts": "2026-08-23T12:00:00",  # datetime columns are TEXT ISO-8601
            "payload": '{"k": [1, 2]}',  # json columns are TEXT holding JSON
        },
    )
    assert await db.find_one("mixed", {"id": 7}) == {
        "id": 7,
        "s": "text",
        "i": -3,
        "f": 2.5,
        "b": 1,
        "ts": "2026-08-23T12:00:00",
        "payload": '{"k": [1, 2]}',
    }


async def test_create_collection_without_schema_raises(db):
    with pytest.raises(SchemaRequiredError):
        await db.create_collection("users", None)


async def test_create_collection_with_empty_schema_raises(db):
    # A zero-field schema cannot define a table — same failure mode as None.
    with pytest.raises(SchemaRequiredError):
        await db.create_collection("users", Schema(fields=[]))


async def test_create_collection_existing_name_raises(db):
    await db.create_collection("users", _schema())
    with pytest.raises(PolydbQueryError):
        await db.create_collection("users", _schema())


async def test_create_collection_not_null_enforced(db):
    await db.create_collection("users", _schema())
    with pytest.raises(PolydbQueryError):
        await db.insert_one("users", {"id": 1, "name": "alice"})  # age missing


async def test_create_collection_unique_enforced(db):
    await db.create_collection("users", _schema())
    base = {"id": 1, "name": "alice", "age": 30, "email": "a@x.com"}
    await db.insert_one("users", base)
    with pytest.raises(PolydbQueryError):
        await db.insert_one("users", {**base, "id": 2})  # duplicate email


async def test_create_collection_primary_key_enforced(db):
    await db.create_collection("users", _schema())
    row = {"id": 1, "name": "alice", "age": 30, "email": "a@x.com"}
    await db.insert_one("users", row)
    with pytest.raises(PolydbQueryError):
        await db.insert_one("users", {**row, "email": "other@x.com"})


async def test_create_collection_invalid_table_name_rejected(db):
    with pytest.raises(InvalidFilterError):
        await db.create_collection("bad name; DROP TABLE x", _schema())


async def test_create_collection_invalid_field_name_rejected(db):
    bad = Schema(fields=[Field(name="not ok", type=FieldType.STR)])
    with pytest.raises(InvalidFilterError):
        await db.create_collection("t", bad)


async def test_create_collection_non_scalar_default_rejected():
    schema = Schema(
        fields=[Field(name="d", type=FieldType.JSON, default={"nested": True})]
    )
    url = "sqlite:///:memory:"
    async with Database.from_url(url) as db:
        with pytest.raises(InvalidFilterError):
            await db.create_collection("t", schema)


async def test_create_collection_before_connect_raises(tmp_path):
    database = Database.from_url(f"sqlite:///{tmp_path}/guard.db")
    with pytest.raises(ConnectionNotOpenError):
        await database.create_collection("users", _schema())


# -- §1.6 #21: drop_collection ------------------------------------------------------


async def test_drop_collection_removes_the_table(db):
    await db.create_collection("users", _schema())
    await db.drop_collection("users")

    assert "users" not in await db.list_collections()
    with pytest.raises(PolydbQueryError):
        await db.find_one("users", {})


async def test_drop_collection_missing_name_is_a_noop(db):
    # Idempotent by contract — cleanup paths never need existence checks.
    await db.drop_collection("never_existed")
    assert await db.list_collections() == []


async def test_drop_collection_before_connect_raises(tmp_path):
    database = Database.from_url(f"sqlite:///{tmp_path}/guard.db")
    with pytest.raises(ConnectionNotOpenError):
        await database.drop_collection("users")


# -- §1.6 #22: list_collections -----------------------------------------------------


async def test_list_collections_empty_database_returns_empty_list(db):
    assert await db.list_collections() == []


async def test_list_collections_lists_user_tables_sorted(db):
    await db.create_collection("zeta", _schema())
    await db.create_collection("alpha", _schema())

    assert await db.list_collections() == ["alpha", "zeta"]


async def test_list_collections_excludes_sqlite_internals_and_views(db):
    await db.create_collection("real", _schema())
    cursor = await db._adapter._conn.execute(
        "CREATE VIEW ghost_view AS SELECT 1 AS one"
    )
    await cursor.close()
    await db._adapter._conn.commit()

    names = await db.list_collections()
    assert names == ["real"]  # no sqlite_* catalogs, no views


async def test_list_collections_before_connect_raises(tmp_path):
    database = Database.from_url(f"sqlite:///{tmp_path}/guard.db")
    with pytest.raises(ConnectionNotOpenError):
        await database.list_collections()


# -- §1.6 #23: create_index ----------------------------------------------------------


def _plain_schema() -> Schema:
    # No unique constraints — index tests build their own uniqueness.
    return Schema(
        fields=[
            Field(name="id", type=FieldType.INT, primary_key=True),
            Field(name="name", type=FieldType.STR),
            Field(name="age", type=FieldType.INT),
        ]
    )


async def _index_names(db, table: str) -> list[str]:
    cursor = await db._adapter._conn.execute(f"PRAGMA index_list({table})")
    rows = await cursor.fetchall()
    return [row[1] for row in rows]


async def test_create_index_creates_composite_index(db):
    await db.create_collection("users", _plain_schema())
    await db.create_index("users", ["age", "name"])

    assert "idx_users__age__name" in await _index_names(db, "users")


async def test_create_index_unique_prefixes_name_and_enforces_uniqueness(db):
    await db.create_collection("users", _plain_schema())
    row = {"id": 1, "name": "alice", "age": 30}
    await db.insert_one("users", row)

    await db.create_index("users", ["name"], unique=True)
    assert "uq_users__name" in await _index_names(db, "users")

    with pytest.raises(PolydbQueryError):
        await db.insert_one("users", {**row, "id": 2})  # duplicate indexed name


async def test_create_index_recreating_identical_index_is_idempotent(db):
    await db.create_collection("users", _plain_schema())
    await db.create_index("users", ["age"])
    await db.create_index("users", ["age"])  # Mongo createIndex parity: no-op

    names = await _index_names(db, "users")
    assert names.count("idx_users__age") == 1


async def test_create_index_empty_fields_raises(db):
    await db.create_collection("users", _plain_schema())
    with pytest.raises(InvalidFilterError):
        await db.create_index("users", [])


async def test_create_index_invalid_field_name_rejected(db):
    await db.create_collection("users", _plain_schema())
    with pytest.raises(InvalidFilterError):
        await db.create_index("users", ["not ok"])


async def test_create_index_missing_column_raises(db):
    await db.create_collection("users", _plain_schema())
    with pytest.raises(PolydbQueryError):
        await db.create_index("users", ["ghost_column"])


async def test_create_index_missing_table_raises(db):
    with pytest.raises(PolydbQueryError):
        await db.create_index("never_created", ["age"])


async def test_create_index_before_connect_raises(tmp_path):
    database = Database.from_url(f"sqlite:///{tmp_path}/guard.db")
    with pytest.raises(ConnectionNotOpenError):
        await database.create_index("users", ["age"])


# -- §1.6 #24: add_field ---------------------------------------------------------------


async def test_add_field_adds_usable_column(db):
    await db.create_collection("users", _plain_schema())
    await db.insert_one("users", {"id": 1, "name": "alice", "age": 30})

    await db.add_field("users", "email", FieldType.STR)

    doc = await db.find_one("users", {"id": 1})
    assert doc["email"] is None  # existing rows read NULL in the new column

    await db.update_one("users", {"id": 1}, {"$set": {"email": "a@x.com"}})
    assert (await db.find_one("users", {"id": 1}))["email"] == "a@x.com"


async def test_add_field_default_backfills_existing_rows(db):
    await db.create_collection("users", _plain_schema())
    await db.insert_many(
        "users",
        [
            {"id": 1, "name": "alice", "age": 30},
            {"id": 2, "name": "bob", "age": 40},
        ],
    )

    await db.add_field("users", "score", FieldType.FLOAT, default=0.5)

    docs = await db.find("users", {"score": 0.5})
    assert [d["id"] for d in docs] == [1, 2]  # both backfilled


async def test_add_field_bool_default_renders_as_0_1(db):
    await db.create_collection("users", _plain_schema())
    await db.add_field("users", "active", FieldType.BOOL, default=True)

    await db.insert_one("users", {"id": 1, "name": "a", "age": 1})
    assert (await db.find_one("users", {"id": 1}))["active"] == 1


async def test_add_field_duplicate_column_raises(db):
    await db.create_collection("users", _plain_schema())
    with pytest.raises(PolydbQueryError):
        await db.add_field("users", "name", FieldType.STR)


async def test_add_field_missing_table_raises(db):
    with pytest.raises(PolydbQueryError):
        await db.add_field("never_created", "email", FieldType.STR)


async def test_add_field_invalid_column_name_rejected(db):
    await db.create_collection("users", _plain_schema())
    with pytest.raises(InvalidFilterError):
        await db.add_field("users", "not ok", FieldType.STR)


async def test_add_field_non_scalar_default_rejected(db):
    await db.create_collection("users", _plain_schema())
    with pytest.raises(InvalidFilterError):
        await db.add_field("users", "payload", FieldType.JSON, default={"k": 1})


async def test_add_field_raw_string_type_rejected(db):
    # FieldType is a str Enum but hash-identifies by identity — a bare "str"
    # must not silently pass as a type.
    await db.create_collection("users", _plain_schema())
    with pytest.raises(InvalidFilterError):
        await db.add_field("users", "s", "str")


async def test_add_field_before_connect_raises(tmp_path):
    database = Database.from_url(f"sqlite:///{tmp_path}/guard.db")
    with pytest.raises(ConnectionNotOpenError):
        await database.add_field("users", "email", FieldType.STR)
