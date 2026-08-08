"""Switching backends with polydb — one connection string per backend.

The whole point of ``Database.from_url``: the same application code talks to
Postgres, MongoDB, SQLite, or MySQL and the only difference is the URL.

As of this build step (planning doc §6) only the SQLite leg can actually
``connect()`` — the Postgres, Mongo, and MySQL builds raise
``NotImplementedError``. This script is written so it keeps working (and stays
honest) as those steps land: it reports which adapter each URL resolved to, and
only performs real I/O where the backend can connect.
"""

from __future__ import annotations

import asyncio

from polydb import Database
from polydb.adapters.mongo import MongoAdapter
from polydb.adapters.postgres import PostgresAdapter
from polydb.adapters.sql.base import SqlAdapter

BACKENDS = {
    "postgres": "postgres://user:pass@localhost:5432/appdb",
    "mongo": "mongodb://localhost:27017/appdb",
    "mysql": "mysql://user:pass@localhost:3306/appdb",
    "sqlite": "sqlite:///:memory:",
}


async def probe(name: str, url: str) -> None:
    db = Database.from_url(url)
    adapter = db._adapter
    kind = type(adapter).__name__
    dialect = getattr(adapter, "dialect", None)
    label = f"{kind} ({dialect.name})" if dialect is not None else kind

    if isinstance(adapter, SqlAdapter) and dialect and dialect.name == "sqlite":
        async with db:
            status = f"connected; ping={await db.ping()}"
    else:
        try:
            await db.connect()
            status = "connected"
        except NotImplementedError as err:
            status = f"not built yet: {err}"

    print(f"{name:<8} -> {kind:<16} {status}")


async def main() -> None:
    for name, url in BACKENDS.items():
        await probe(name, url)


if __name__ == "__main__":
    asyncio.run(main())