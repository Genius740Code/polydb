# polydb

[![PyPI](https://img.shields.io/pypi/v/polydb)](https://pypi.org/project/polydb/)
[![Python](https://img.shields.io/pypi/pyversions/polydb)](https://pypi.org/project/polydb/)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

One async API for **PostgreSQL**, **MongoDB**, **SQLite**, and **MySQL**.

Source: <https://github.com/Genius740Code/poldb>

`polydb` gives application code a single async API for CRUD, schema, transactions,
and raw-query escape hatches, backed by three adapters (Postgres, Mongo, and a
SQL-generic family covering SQLite + MySQL). Switching backends is a
connection-string change — see the [planning document](plan.md) for the full
support matrix and build order.

## Current status

Planned, not fully built. Implemented so far (planning doc §1.1 **Connection
management** + §1.2 **Create** + §1.3 **Read**):

| Feature | Status |
| ---- | ------ |
| `Database.from_url(url) -> Database` — factory resolves every supported scheme to its adapter instance | ✅ Done |
| `connect()` / `disconnect()` / `ping()` / async context manager | ✅ Done (SQLite leg; `ping` returns `False` instead of raising on an unreachable backend) |
| `pool_size`, `timeout` query params | ✅ Done — validated at parse time, exposed as `db.pool_size` / `db.timeout`; SQLite accepts-and-ignores non-default `pool_size` with a warning (single-writer file) |
| `insert_one(collection, doc)` → `InsertResult` | ✅ Done (SQLite leg; `inserted_id` = rowid) |
| `insert_many(collection, docs)` → `InsertManyResult` | ✅ Done (SQLite leg; heterogeneous docs fill missing columns with `NULL`; one commit, all-or-nothing) |
| `upsert_one(collection, filter, doc)` → `UpsertResult` | ✅ Done (SQLite leg; Mongo-style match-first-row-or-insert semantics — filter fields merge into the inserted row, `doc` wins on key conflicts) |
| `find_one(collection, filter)` → `dict \| None` | ✅ Done (SQLite leg; full Mongo-shaped filter DSL — `$eq/$ne/$gt/$gte/$lt/$lte`, `$in/$nin`, `$exists`, `$regex`, `$like`, `$and/$or/$nor/$not`) |
| `find(collection, filter, *, sort, limit, offset)` → `list[dict]` | ✅ Done (SQLite leg; `sort` takes `(field, 1 \| -1)` pairs) |
| `count(collection, filter)` → `int` | ✅ Done (SQLite leg) |
| `exists(collection, filter)` → `bool` | ✅ Done (SQLite leg; short-circuits via `LIMIT 1`) |
| `aggregate(collection, pipeline)` → `list[dict]` | ✅ Done (SQLite leg; restricted subset: repeatable `$match`, then at most one `$group` (`$sum/$avg/$min/$max/$count`) / `$sort` / `$limit` / `$count`; anything else raises `UnsupportedOperationError`) |
| Update, delete, schema, transactions, raw queries | ⬜ Not started |

Filters are compiled by the shared `sql_compiler.py` into fully parameterized SQL;
column/table names are validated against `[A-Za-z_][A-Za-z0-9_]*` and quoted.
SQLite quotes identifiers with backticks on purpose — double-quoted unknown
identifiers silently become string literals in SQLite, hiding typos.

Postgres / Mongo / SQL-MySQL legs raise `NotImplementedError` until their
respective build steps land (see build order §6).

## Installation

```bash
pip install polydb
```

## Quick start (SQLite — the one working leg)

```python
import asyncio

from polydb import Database


async def main() -> None:
    async with Database.from_url("sqlite:///:memory:") as db:
        assert await db.ping() is True

asyncio.run(main())
```

## Connecting to other backends

`Database.from_url()` resolves all supported schemes to their adapter class,
returning an **unconnected** instance:

| URL scheme | Adapter class | `connect()` today |
| --- | --- | --- |
| `postgres://` / `postgresql://` | `PostgresAdapter` | `NotImplementedError` (build step 3) |
| `mongodb://` / `mongodb+srv://` | `MongoAdapter` | `NotImplementedError` (build step 4) |
| `sqlite:///...` | `SqlAdapter` (dialect `sqlite`) | ✅ live |
| `mysql://` | `SqlAdapter` (dialect `mysql`) | `NotImplementedError` (build step 5) |

## Under the hood

- [docs/connection.md](docs/connection.md) — connection URL format, adapter resolution, tuning knobs.
- [examples/switching_backends.py](examples/switching_backends.py) — one script, three connection strings.
- [plan.md](plan.md) — planning document: feature list, DSL spec, build order, testing strategy.

## Development

```bash
# install dev extras (pytest, pytest-asyncio, drivers)
pip install -e ".[dev]"

# run the test suite from the repo root
PYTHONPATH=src pytest
```