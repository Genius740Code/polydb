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
    database = Database.from_url(f"sqlite:////{tmp_path}/read.db")
    await database.connect()
    yield database
    await database.disconnect()


@pytest.fixture
async def seeded_db(tmp_path):
    database = Database.from_url(f"sqlite:////{tmp_path}/seeded.db")
    await database.connect()
    await database._adapter._conn.execute(
        """
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            region TEXT, city TEXT, amount INT, status TEXT, priority TEXT
        )
        """
    )
    await database.insert_many(
        "orders",
        [
            {"region": "UK", "city": "London", "amount": 100, "status": "open"},
            {"region": "UK", "city": "Leeds", "amount": 4999, "status": "pending"},
            {"region": "EU", "city": "Paris", "amount": 5000, "status": "open"},
            {"region": "EU", "city": "Berlin", "amount": 250, "status": "closed"},
        ],
    )
    yield database
    await database.disconnect()


# -- §1.3 #10: find_one -----------------------------------------------------------


async def test_find_one_returns_first_match(seeded_db):
    doc = await seeded_db.find_one("orders", {"region": "UK"})
    assert doc["city"] == "London"  # first inserted match wins
    assert set(doc.keys()) == {"id", "region", "city", "amount", "status", "priority"}


async def test_find_one_returns_none_when_no_match(seeded_db):
    result = await seeded_db.find_one("orders", {"city": "Nowhere"})
    assert result is None


async def test_find_one_applies_operator_filter(seeded_db):
    doc = await seeded_db.find_one("orders", {"amount": {"$gte": 1000}})
    assert doc["amount"] == 4999


async def test_find_one_before_connect_raises(tmp_path):
    database = Database.from_url(f"sqlite:////{tmp_path}/guard.db")
    with pytest.raises(ConnectionNotOpenError):
        await database.find_one("orders", {"region": "UK"})


# -- §1.3 #11: find ------------------------------------------------------------------


async def test_find_without_filter_returns_everything(seeded_db):
    assert len(await seeded_db.find("orders")) == 4


async def test_find_with_empty_filter_matches_all(seeded_db):
    assert len(await seeded_db.find("orders", {})) == 4


async def test_find_narrows_by_equality(seeded_db):
    rows = await seeded_db.find("orders", {"status": "open"})
    assert {r["city"] for r in rows} == {"London", "Paris"}


async def test_find_sort_ascending_and_descending(seeded_db):
    asc = [r["amount"] for r in await seeded_db.find("orders", sort=[("amount", 1)])]
    desc = [r["amount"] for r in await seeded_db.find("orders", sort=[("amount", -1)])]
    assert asc == sorted(asc) == [100, 250, 4999, 5000]
    assert desc == list(reversed(asc))


async def test_find_multi_key_sort_breaks_ties(seeded_db):
    rows = await seeded_db.find(
        "orders", sort=[("region", -1), ("amount", 1)]
    )
    assert [(r["region"], r["amount"]) for r in rows] == [
        ("UK", 100),
        ("UK", 4999),
        ("EU", 250),
        ("EU", 5000),
    ]


async def test_find_limit_and_offset_paginate(seeded_db):
    page_one = await seeded_db.find("orders", sort=[("id", 1)], limit=2)
    page_two = await seeded_db.find(
        "orders", sort=[("id", 1)], limit=2, offset=2
    )
    assert [r["id"] for r in page_one] == [1, 2]
    assert [r["id"] for r in page_two] == [3, 4]


async def test_find_offset_without_limit_returns_tail(seeded_db):
    rows = await seeded_db.find("orders", sort=[("id", 1)], offset=3)
    assert [r["id"] for r in rows] == [4]


# -- §2.2 DSL operator coverage (exercised through find) ------------------------------


async def test_find_ne_excludes_matches(seeded_db):
    rows = await seeded_db.find("orders", {"region": {"$ne": "UK"}})
    assert {r["city"] for r in rows} == {"Paris", "Berlin"}


async def test_find_in_and_nin(seeded_db):
    ins = await seeded_db.find("orders", {"status": {"$in": ["open", "pending"]}})
    nins = await seeded_db.find("orders", {"status": {"$nin": ["open", "pending"]}})
    assert len(ins) + len(nins) == 4
    assert {r["status"] for r in ins} == {"open", "pending"}
    assert {r["status"] for r in nins} == {"closed"}


async def test_find_empty_in_matches_nothing_and_empty_nin_matches_all(seeded_db):
    assert await seeded_db.find("orders", {"status": {"$in": []}}) == []
    assert len(await seeded_db.find("orders", {"status": {"$nin": []}})) == 4


async def test_find_exists_maps_null_semantics(tmp_path):
    database = Database.from_url(f"sqlite:////{tmp_path}/nulls.db")
    await database.connect()
    try:
        await database._adapter._conn.execute(
            "CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, note TEXT)"
        )
        await database.insert_many("t", [{"note": "x"}, {"note": None}])
        present = await database.find("t", {"note": {"$exists": True}})
        missing = await database.find("t", {"note": {"$exists": False}})
        assert [r["note"] for r in present] == ["x"]
        assert [r["note"] for r in missing] == [None]
    finally:
        await database.disconnect()


async def test_find_regex_uses_registered_sqlite_udf(seeded_db):
    rows = await seeded_db.find("orders", {"city": {"$regex": "^L"}})
    assert {r["city"] for r in rows} == {"London", "Leeds"}


async def test_find_regex_never_matches_null_values(tmp_path):
    database = Database.from_url(f"sqlite:////{tmp_path}/nulls.db")
    await database.connect()
    try:
        await database._adapter._conn.execute(
            "CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, note TEXT)"
        )
        await database.insert_many("t", [{"note": "x"}, {"note": None}])
        rows = await database.find("t", {"note": {"$regex": "."}})
        assert [r["note"] for r in rows] == ["x"]
    finally:
        await database.disconnect()


async def test_filter_on_missing_column_raises_instead_of_silently_matching(seeded_db):
    # SQLite's legacy double-quoted-string fallback would turn an unknown
    # quoted identifier into a string literal (silently matching nothing or
    # everything). The adapter quotes with backticks, which always raise.
    with pytest.raises(PolydbQueryError):
        await seeded_db.find("orders", {"missing_col": {"$regex": "."}})


async def test_find_like_wildcards(seeded_db):
    rows = await seeded_db.find("orders", {"city": {"$like": "%on%"}})
    assert {r["city"] for r in rows} == {"London"}


async def test_find_logical_operators_combine(seeded_db):
    or_rows = await seeded_db.find(
        "orders", {"$or": [{"region": "UK"}, {"amount": {"$gt": 3000}}]}
    )
    assert len(or_rows) == 3

    nor_rows = await seeded_db.find(
        "orders", {"$nor": [{"region": "UK"}, {"amount": {"$gt": 3000}}]}
    )
    assert {r["city"] for r in nor_rows} == {"Berlin"}

    not_rows = await seeded_db.find("orders", {"$not": {"region": "UK"}})
    assert {r["region"] for r in not_rows} == {"EU"}

    and_rows = await seeded_db.find(
        "orders",
        {"$and": [{"region": "UK"}, {"amount": {"$gte": 100, "$lt": 5000}}]},
    )
    assert {r["city"] for r in and_rows} == {"London", "Leeds"}


async def test_find_plan_worked_example_section_2_3(seeded_db):
    results = await seeded_db.find(
        "orders",
        {
            "$and": [
                {"status": {"$in": ["open", "pending"]}},
                {"amount": {"$gte": 100, "$lt": 5000}},
                {"$or": [{"region": "UK"}, {"priority": {"$eq": "high"}}]},
            ]
        },
    )
    # Paris is excluded by amount < 5000; Berlin by status; Leeds survives via UK.
    assert {r["city"] for r in results} == {"London", "Leeds"}


# -- filter validation -----------------------------------------------------------------


async def test_find_unknown_operator_raises_invalid_filter_error(seeded_db):
    with pytest.raises(InvalidFilterError):
        await seeded_db.find("orders", {"amount": {"$between": [1, 5]}})


async def test_find_invalid_field_name_raises_invalid_filter_error(seeded_db):
    with pytest.raises(InvalidFilterError):
        await seeded_db.find("orders", {"bad name; DROP TABLE orders": 1})


async def test_find_bad_sort_direction_raises_invalid_filter_error(seeded_db):
    with pytest.raises(InvalidFilterError):
        await seeded_db.find("orders", sort=[("amount", 2)])


async def test_find_negative_limit_raises_invalid_filter_error(seeded_db):
    with pytest.raises(InvalidFilterError):
        await seeded_db.find("orders", limit=-1)


# -- §1.3 #12: count / #13: exists -------------------------------------------------------


async def test_count_with_and_without_filter(seeded_db):
    assert await seeded_db.count("orders") == 4
    assert await seeded_db.count("orders", {"region": "UK"}) == 2


async def test_count_zero_when_nothing_matches(seeded_db):
    assert await seeded_db.count("orders", {"region": "APAC"}) == 0


async def test_exists_true_false_and_short_circuit_shape(seeded_db):
    assert await seeded_db.exists("orders", {"city": "Paris"}) is True
    assert await seeded_db.exists("orders", {"city": "Nowhere"}) is False


async def test_count_and_exists_before_connect_raise(tmp_path):
    database = Database.from_url(f"sqlite:////{tmp_path}/guard.db")
    with pytest.raises(ConnectionNotOpenError):
        await database.count("orders")
    with pytest.raises(ConnectionNotOpenError):
        await database.exists("orders", {})


# -- §1.3 #14: aggregate ------------------------------------------------------------------


async def test_aggregate_match_only_returns_documents(seeded_db):
    docs = await seeded_db.aggregate(
        "orders", [{"$match": {"region": "UK"}}, {"$limit": 1}]
    )
    assert len(docs) == 1 and docs[0]["city"] == "London"


async def test_aggregate_group_by_field_with_accumulators(seeded_db):
    groups = await seeded_db.aggregate(
        "orders",
        [
            {"$match": {"amount": {"$gte": 100}}},
            {
                "$group": {
                    "_id": "$region",
                    "total": {"$sum": "$amount"},
                    "avg_amount": {"$avg": "$amount"},
                    "biggest": {"$max": "$amount"},
                    "smallest": {"$min": "$amount"},
                    "n": {"$count": {}},
                }
            },
            {"$sort": {"total": -1}},
        ],
    )
    assert groups == [
        {
            "_id": "EU",
            "total": 5250,
            "avg_amount": 2625.0,
            "biggest": 5000,
            "smallest": 250,
            "n": 2,
        },
        {
            "_id": "UK",
            "total": 5099,
            "avg_amount": 2549.5,
            "biggest": 4999,
            "smallest": 100,
            "n": 2,
        },
    ]


async def test_aggregate_global_group_reports_null_id(seeded_db):
    groups = await seeded_db.aggregate(
        "orders", [{"$group": {"_id": None, "grand_total": {"$sum": "$amount"}}}]
    )
    assert groups == [{"_id": None, "grand_total": 10349}]


async def test_aggregate_global_group_on_empty_input_yields_no_docs(seeded_db):
    groups = await seeded_db.aggregate(
        "orders",
        [{"$match": {"region": "APAC"}}, {"$group": {"_id": None, "n": {"$sum": 1}}}],
    )
    # SQL would happily report one row of NULLs over zero input rows — Mongo
    # semantics require no documents at all.
    assert groups == []


async def test_aggregate_composite_id_nests_under_id_key(seeded_db):
    groups = await seeded_db.aggregate(
        "orders",
        [
            {"$group": {"_id": {"region": "$region", "city": "$city"}}},
            {"$sort": {"_id.city": 1}},
        ],
    )
    assert groups == [
        {"_id": {"region": "EU", "city": "Berlin"}},
        {"_id": {"region": "UK", "city": "Leeds"}},
        {"_id": {"region": "UK", "city": "London"}},
        {"_id": {"region": "EU", "city": "Paris"}},
    ]


async def test_aggregate_group_plus_sort_alias_plus_limit(seeded_db):
    top = await seeded_db.aggregate(
        "orders",
        [
            {"$group": {"_id": "$region", "total": {"$sum": "$amount"}}},
            {"$sort": {"total": -1}},
            {"$limit": 1},
        ],
    )
    assert top == [{"_id": "EU", "total": 5250}]


async def test_aggregate_count_stage_standalone(seeded_db):
    result = await seeded_db.aggregate(
        "orders", [{"$match": {"region": "UK"}}, {"$count": "uk_docs"}]
    )
    assert result == [{"uk_docs": 2}]


async def test_aggregate_count_after_group_counts_groups_not_docs(seeded_db):
    result = await seeded_db.aggregate(
        "orders", [{"$group": {"_id": "$region"}}, {"$count": "regions"}]
    )
    assert result == [{"regions": 2}]


async def test_aggregate_unsupported_stage_raises(seeded_db):
    with pytest.raises(UnsupportedOperationError):
        await seeded_db.aggregate("orders", [{"$lookup": {"from": "customers"}}])


async def test_aggregate_out_of_order_stages_raise(seeded_db):
    with pytest.raises(UnsupportedOperationError):
        await seeded_db.aggregate(
            "orders", [{"$limit": 2}, {"$match": {"region": "UK"}}]
        )


async def test_aggregate_unsupported_accumulator_raises(seeded_db):
    with pytest.raises(UnsupportedOperationError):
        await seeded_db.aggregate(
            "orders",
            [{"$group": {"_id": None, "tags": {"$push": "$city"}}}],
        )


async def test_aggregate_before_connect_raises(tmp_path):
    database = Database.from_url(f"sqlite:////{tmp_path}/guard.db")
    with pytest.raises(ConnectionNotOpenError):
        await database.aggregate("orders", [{"$count": "n"}])


# -- error handling / guards -----------------------------------------------------------


async def test_find_missing_table_wrapped_in_polydb_query_error(db):
    with pytest.raises(PolydbQueryError):
        await db.find("no_such_table", {"x": 1})


async def test_failed_read_leaves_connection_usable_for_writes(seeded_db):
    with pytest.raises(PolydbQueryError):
        await seeded_db.find_one("no_such_table", {})
    result = await seeded_db.insert_one("orders", {"region": "APAC"})
    assert result.inserted_id is not None
    assert await seeded_db.count("orders") == 5
