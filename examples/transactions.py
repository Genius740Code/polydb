"""Transactions with polydb — atomic multi-operation blocks (§1.7 #25–#26).

Runs against SQLite (no services needed) and the same code works against
Postgres — both legs live as of planning doc §6 step 3:

    PYTHONPATH=src python3 examples/transactions.py  # SQLite
    # or: postgres://user:pass@localhost/db for Postgres

Everything routed through the yielded ``tx`` handle — inserts, reads, updates,
deletes, even DDL — commits together on a clean exit and rolls back wholesale
if an exception escapes the block.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from polydb import Database
from polydb.schema import Field, FieldType, Schema


async def commit_demo(db: Database) -> None:
    """Clean exit auto-commits: both writes land or neither does."""
    print("-- commit " + "-" * 50)

    async with db.transaction() as tx:
        await tx.insert_one("accounts", {"owner": "alice", "balance": 100.0})
        await tx.insert_one("accounts", {"owner": "bob", "balance": 50.0})
        await tx.update_many(
            "accounts", {"owner": "alice"}, {"$inc": {"balance": -25.0}}
        )

    print(f"after clean exit -> {await db.find('accounts', sort=[('owner', 1)])}")


async def rollback_demo(db: Database) -> None:
    """An exception inside the block rolls back everything."""
    print("-- rollback " + "-" * 48)

    try:
        async with db.transaction() as tx:
            await tx.insert_one("accounts", {"owner": "carol", "balance": 10.0})
            raise RuntimeError("payment failed")
    except RuntimeError as err:
        print(f"caught {err!r} -> rows now {await db.count('accounts')}")

    # Explicit control is available too; the tx object is finished afterwards.
    async with db.transaction() as tx:
        await tx.insert_one("accounts", {"owner": "dave", "balance": 5.0})
        await tx.rollback()
    print(f"after explicit rollback -> {await db.count('accounts')}")


async def cross_collection_demo(db: Database) -> None:
    """One transaction spans collections: order + ledger move together."""
    print("-- cross-collection " + "-" * 39)

    with tempfile.TemporaryDirectory() as tmp:
        url = f"sqlite:///{Path(tmp) / 'ledger.db'}"
        async with Database.from_url(url) as fresh:
            schema = Schema(fields=[Field(name="id", type=FieldType.INT,
                                         primary_key=True)])
            await fresh.create_collection("orders", schema)
            await fresh.create_collection("ledger", schema)

            try:
                async with fresh.transaction() as tx:
                    await tx.insert_one("orders", {"id": 1})
                    await tx.insert_one("ledger", {"id": 1})   # debit logged...
                    await tx.insert_one("ledger", {"id": 1})   # ...twice: PK clash
            except Exception as err:
                print(f"failed as expected ({type(err).__name__})")

            orders = await fresh.count("orders")
            ledger = await fresh.count("ledger")
            print(f"atomic across tables -> orders={orders} ledger={ledger}")


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        url = f"sqlite:///{Path(tmp) / 'tx.db'}"
        async with Database.from_url(url) as db:
            schema = Schema(
                fields=[
                    Field(name="id", type=FieldType.INT, primary_key=True),
                    Field(name="owner", type=FieldType.STR),
                    Field(name="balance", type=FieldType.FLOAT),
                ]
            )
            await db.create_collection("accounts", schema)

            await commit_demo(db)
            await rollback_demo(db)
            await cross_collection_demo(db)


if __name__ == "__main__":
    asyncio.run(main())
