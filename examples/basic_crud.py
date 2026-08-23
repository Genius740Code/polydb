"""Basic CRUD with polydb — every implemented operation in one script.

Runs entirely against SQLite (the one backend whose build step has landed, see
planning doc §6), so you can execute it with no services running:

    PYTHONPATH=src python3 examples/basic_crud.py

Filters and updates use the Mongo-shaped DSL documented in
docs/dsl_spec.md; the SQL compiler turns them into fully parameterized SQL.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from polydb import Database


async def _bootstrap_table(db: Database) -> None:
    """Create the demo table directly.

    ``create_collection()`` is §1.6 and not built yet, so this example creates
    its table through the underlying driver the same way the contract tests
    do. This helper disappears once that build step lands.
    """
    cursor = await db._adapter._conn.execute(
        """
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer TEXT, region TEXT, status TEXT,
            amount REAL, visits INT, note TEXT
        )
        """
    )
    await cursor.close()


async def create_demo(db: Database) -> None:
    print("-- create " + "-" * 50)

    result = await db.insert_one(
        "orders",
        {"customer": "alice", "region": "UK", "status": "open", "amount": 120.0},
    )
    print(f"insert_one      -> inserted_id={result.inserted_id}")

    # Heterogeneous docs are fine: missing columns become NULL.
    bulk = await db.insert_many(
        "orders",
        [
            {"customer": "bob", "region": "US", "status": "open",
             "amount": 4800.0, "visits": 5},
            {"customer": "carol", "region": "UK", "status": "pending", "amount": 250.0},
            {"customer": "dave", "region": "EU", "status": "closed", "amount": 90.0},
        ],
    )
    print(f"insert_many     -> count={bulk.inserted_count} ids={bulk.inserted_ids}")

    # Mongo-style upsert: first match gets updated, no match inserts a row
    # merged from filter + doc (doc wins on key conflicts).
    updated = await db.upsert_one(
        "orders", {"customer": "alice"}, {"amount": 130.0}
    )
    print(f"upsert (update) -> matched={updated.matched_count} "
          f"modified={updated.modified_count}")

    inserted = await db.upsert_one(
        "orders", {"customer": "erin", "region": "US"}, {"status": "open"}
    )
    print(f"upsert (insert) -> upserted_id={inserted.upserted_id}")


async def read_demo(db: Database) -> None:
    print("-- read " + "-" * 52)

    first = await db.find_one("orders", {"customer": "bob"})
    print(f"find_one        -> {first}")

    # Full DSL: comparison ranges, $in lists, $or groups — all ANDed.
    matching = await db.find(
        "orders",
        {
            "$and": [
                {"status": {"$in": ["open", "pending"]}},
                {"amount": {"$gte": 100, "$lt": 5000}},
                {"$or": [{"region": "UK"}, {"region": "US"}]},
            ]
        },
        sort=[("amount", -1)],
    )
    print(f"find (DSL)      -> {[(r['customer'], r['amount']) for r in matching]}")

    page = await db.find("orders", sort=[("id", 1)], limit=2, offset=1)
    print(f"find (page)     -> {[r['customer'] for r in page]}")

    print(f"count           -> {await db.count('orders', {'status': 'open'})}")
    print(f"exists          -> {await db.exists('orders', {'customer': 'erin'})}")
    print(f"exists (miss)   -> {await db.exists('orders', {'customer': 'nobody'})}")

    # $regex uses re.search semantics on SQLite via a registered REGEXP function.
    uk_or_eu = await db.find("orders", {"region": {"$regex": "UK|EU"}})
    print(f"find ($regex)   -> {[r['customer'] for r in uk_or_eu]}")

    # Restricted aggregate subset: $match* then $group/$sort/$limit/$count.
    totals = await db.aggregate(
        "orders",
        [
            {"$match": {"status": {"$ne": "closed"}}},
            {"$group": {"_id": "$region", "total": {"$sum": "$amount"},
                        "avg": {"$avg": "$amount"}}},
            {"$sort": {"total": -1}},
        ],
    )
    print(f"aggregate       -> {totals}")

    open_count = await db.aggregate(
        "orders", [{"$match": {"status": "open"}}, {"$count": "n_open"}]
    )
    print(f"aggregate $count-> {open_count}")


async def update_demo(db: Database) -> None:
    print("-- update " + "-" * 50)

    # update_one targets exactly the first match; $set/$inc/$unset combine.
    result = await db.update_one(
        "orders", {"customer": "bob"}, {"$inc": {"visits": 1}}
    )
    print(f"update_one      -> matched={result.matched_count} "
          f"modified={result.modified_count}")

    # update_many rewrites every match (counts come from rowcount).
    result = await db.update_many(
        "orders", {"status": "open", "visits": None}, {"$set": {"visits": 0}}
    )
    print(f"update_many     -> matched={result.matched_count} "
          f"modified={result.modified_count}")

    # replace_one is a full-document replace: absent fields become NULL,
    # primary-key columns are preserved and may not appear in `doc`.
    result = await db.replace_one(
        "orders", {"customer": "dave"},
        {"customer": "dave", "status": "refunded", "amount": 0.0},
    )
    replaced = await db.find_one("orders", {"customer": "dave"})
    print(f"replace_one     -> modified={result.modified_count}; row now {replaced}")


async def delete_demo(db: Database) -> None:
    print("-- delete " + "-" * 50)

    # delete_one targets exactly the first match.
    result = await db.delete_one("orders", {"status": "open"})
    print(f"delete_one      -> deleted={result.deleted_count}")

    # delete_many rewrites every match; {} clears the whole collection.
    result = await db.delete_many("orders", {"status": "refunded"})
    print(f"delete_many     -> deleted={result.deleted_count}")


async def main() -> None:
    # A temp file keeps repeat runs clean; sqlite:///:memory: works too.
    with tempfile.TemporaryDirectory() as tmp:
        url = f"sqlite:///{Path(tmp) / 'crud.db'}"
        async with Database.from_url(url) as db:
            assert await db.ping() is True
            await _bootstrap_table(db)

            await create_demo(db)
            await read_demo(db)
            await update_demo(db)
            await delete_demo(db)


if __name__ == "__main__":
    asyncio.run(main())
