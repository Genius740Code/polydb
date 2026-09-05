# polydb

[![PyPI](https://img.shields.io/pypi/v/genius74o-polydb)](https://pypi.org/project/genius74o-polydb/)
[![Python](https://img.shields.io/pypi/pyversions/genius74o-polydb)](https://pypi.org/project/genius74o-polydb/)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

One async API for **PostgreSQL**, **MongoDB**, **SQLite**, and **MySQL**.

Source: <https://github.com/Genius740Code/polydb>

`polydb` gives application code a single async API for CRUD, schema, transactions,
and raw-query escape hatches, backed by three adapters (Postgres, Mongo, and a
SQL-generic family covering SQLite + MySQL). Switching backends is a
connection-string change — see the [planning document](plan.md) for the full
support matrix and build order.

## Current status

Three legs live: **28 / 28 features implemented (100%)** on **SQLite** (`aiosqlite`), **Postgres** (`asyncpg`), and **MongoDB** (`motor`) — every feature Row #1–#28 in planning doc §1. The
full per-backend matrix lives in
[docs/supported_operations_matrix.md](docs/supported_operations_matrix.md).

| Feature | Status |
| ---- | ------ |
| `Database.from_url(url) -> Database` — factory resolves every supported scheme to its adapter instance | ✅ Done |
| `connect()` / `disconnect()` / `ping()` / async context manager | ✅ Done (SQLite + Postgres + Mongo; `ping` returns `False` instead of raising on an unreachable backend) |
| `pool_size`, `timeout` query params | ✅ Done — validated at parse time, exposed as `db.pool_size` / `db.timeout`; SQLite accepts-and-ignores non-default `pool_size` with a warning (single-writer file), Postgres (`max_size`/`command_timeout`) and Mongo (`maxPoolSize`/`serverSelectionTimeoutMS`) wire it |
| `insert_one(collection, doc)` → `InsertResult` | ✅ Done (SQLite: `lastrowid`; Postgres: `RETURNING *` + PK pick; Mongo: `insert_one` native) |
| `insert_many(collection, docs)` → `InsertManyResult` | ✅ Done (SQLite + Postgres: heterogeneous docs fill missing columns with `NULL`; one commit, all-or-nothing; Mongo: `insert_many` native, empty list is no-op) |
| `upsert_one(collection, filter, doc)` → `UpsertResult` | ✅ Done (SQLite + Postgres + Mongo; Mongo-style match-first-row-or-insert semantics — filter fields merge into the inserted row, `doc` wins on key conflicts) |
| `find_one(collection, filter)` → `dict \| None` | ✅ Done (SQLite + Postgres + Mongo; full Mongo-shaped filter DSL — `$eq/$ne/$gt/$gte/$lt/$lte`, `$in/$nin`, `$exists`, `$regex`, `$like` via `$expr`/`$regexMatch` on Mongo, `$and/$or/$nor/$not`) |
| `find(collection, filter, *, sort, limit, offset)` → `list[dict]` | ✅ Done (SQLite + Postgres + Mongo; `sort` takes `(field, 1 \| -1)` pairs) |
| `count(collection, filter)` → `int` | ✅ Done (SQLite + Postgres + Mongo) |
| `exists(collection, filter)` → `bool` | ✅ Done (SQLite + Postgres + Mongo; short-circuits via `LIMIT 1` / `find_one` projection) |
| `aggregate(collection, pipeline)` → `list[dict]` | ✅ Done (SQLite + Postgres: restricted subset: repeatable `$match`, then at most one `$group` (`$sum/$avg/$min/$max/$count`) / `$sort` / `$limit` / `$count`; anything else raises `UnsupportedOperationError`; Mongo: native pipeline) |
| `update_one(collection, filter, update)` → `UpdateResult` | ✅ Done (SQLite + Postgres + Mongo; full filter DSL + §2.4 update operators `$set`/`$inc`/`$unset` (+ `$push` native on Mongo) — `$push` raises `UnsupportedOperationError` on SQL; first match only) |
| `update_many(collection, filter, update)` → `UpdateResult` | ✅ Done (SQLite + Postgres + Mongo; one parameterized `UPDATE … WHERE` on SQL — counts come from rowcount, so already-equal values still count as modified; Mongo native `update_many`) |
| `replace_one(collection, filter, doc)` → `UpdateResult` | ✅ Done (SQLite + Postgres + Mongo; full-document replace — absent doc fields become `NULL` on SQL, native `replace_one` on Mongo; primary-key columns preserved and rejected in `doc` on SQL; never upserts) |
| `delete_one(collection, filter)` → `DeleteResult` | ✅ Done (SQLite + Postgres + Mongo; full filter DSL; deletes the first match only) |
| `delete_many(collection, filter)` → `DeleteResult` | ✅ Done (SQLite + Postgres + Mongo; one parameterized `DELETE … WHERE` on SQL; empty filter clears the collection; Mongo native) |
| `create_collection(name, schema=None)` | ✅ Done (SQLite + Postgres: Schema→`CREATE TABLE` compiler — schema required (`SchemaRequiredError` otherwise); Mongo: `schema` ignored with `logger.info` (schemaless); str/int/float/bool native types, datetime/json stored as TEXT on SQL) |
| `drop_collection(name)` | ✅ Done (SQLite + Postgres + Mongo; idempotent — dropping an absent name is a silent no-op) |
| `list_collections()` → `list[str]` | ✅ Done (SQLite + Postgres + Mongo; sorted user tables, `sqlite_*` internals and views excluded on SQL, `system.*` excluded on Mongo) |
| `create_index(collection, fields, *, unique=False)` | ✅ Done (SQLite + Postgres + Mongo; `CREATE [UNIQUE] INDEX` with deterministic derived name — `idx_<table>__<f1>__<f2>`, `uq_…` for unique; `IF NOT EXISTS`-idempotent like Mongo's `createIndex`; empty field list raises) |
| `add_field(collection, field, type_, default=None)` | ✅ Done (SQLite + Postgres: `ALTER TABLE … ADD COLUMN`, same type mapping as `create_collection`; non-None scalar default becomes a `DEFAULT` clause that backfills existing rows; new columns always nullable. Mongo: no-op + `logger.warning`.) |
| `transaction()` → `async with db.transaction() as tx:` | ✅ Done (SQLite + Postgres + Mongo; explicit `BEGIN`/`start_transaction`, one atomic block — writes through `tx.*` (and direct adapter calls inside the block) commit together on clean exit or roll back wholesale on error; a failed statement aborts the transaction like a poisoned Postgres tx and later calls raise `TransactionInactiveError`; nested transactions raise `UnsupportedOperationError`; SQLite's single-writer file means serialization, not true isolation; Mongo requires replica set / Atlas else `TransactionsUnavailableError`) |
| `raw(query, params=None)` → `list[dict]` | ✅ Done (SQLite + Postgres: escape hatch — plain SQL string, positional sequence or `:named` dict params, rows as plain dicts; Mongo: `dict` command via `db.command`; no DSL compilation; writes commit like every polydb write or join an open transaction; driver failures raise `PolydbQueryError`) |
| `explain(collection, filter)` → `dict` | ✅ Done (SQLite + Postgres + Mongo; filter compiles exactly as `find()` would, run under `EXPLAIN QUERY PLAN` (SQL) / `find().explain()` (Mongo); returns `{"backend", "sql", "params", "plan"}` on SQL, `{"backend", "filter", "plan"}` on Mongo) |

Filters are compiled by the shared `sql_compiler.py` (SQL) and `mongo_compiler.py` (Mongo) into fully parameterized SQL / native Mongo queries;
column/table names are validated against `[A-Za-z_][A-Za-z0-9_]*` and quoted.
SQLite quotes identifiers with backticks on purpose — double-quoted unknown
identifiers silently become string literals in SQLite, hiding typos — while
Postgres uses double quotes and numbered `$1` placeholders via `number_placeholders()`. Mongo passes through after `$like`→`$expr`/`$regexMatch` and `$not`→`$nor` normalization.

MySQL leg raises `NotImplementedError` until its build step lands (see build order §6); Postgres, SQLite, and Mongo are live.

## Installation

The PyPI distribution is `genius74o-polydb` (the plain `polydb` name is
squatted); the Python package you import is still `polydb`:

```bash
pip install genius74o-polydb              # core (no drivers)
pip install "genius74o-polydb[sqlite]"    # + a driver, or: postgres / mongo / mysql
pip install "genius74o-polydb[all]"       # every driver
```

## Quick start (SQLite + Postgres + Mongo — live legs)

```python
import asyncio

from polydb import Database


async def main() -> None:
    async with Database.from_url("sqlite:///:memory:") as db:  # or postgres://user:pass@localhost/db or mongodb://localhost:27017/db
        assert await db.ping() is True

asyncio.run(main())
```

Create a collection from a structured schema, then the full
Create/Read/Update/Delete surface works against it:

```python
from polydb.schema import Field, FieldType, Schema

await db.create_collection(
    "users",
    Schema(fields=[
        Field(name="id", type=FieldType.INT, primary_key=True),
        Field(name="name", type=FieldType.STR),
        Field(name="age", type=FieldType.INT),
    ]),
)
await db.insert_one("users", {"name": "ada", "age": 36})
adults = await db.find("users", {"age": {"$gt": 21}}, sort=[("age", -1)])
```

Filters use the Mongo-shaped DSL documented in
[docs/dsl_spec.md](docs/dsl_spec.md); see
[examples/basic_crud.py](examples/basic_crud.py) for a complete runnable tour.

## Connecting to other backends

`Database.from_url()` resolves all supported schemes to their adapter class,
returning an **unconnected** instance:

| URL scheme | Adapter class | `connect()` today |
| --- | --- | --- |
| `postgres://` / `postgresql://` | `PostgresAdapter` | ✅ live (`asyncpg` pool, `$1` placeholders, `~` for `$regex`) |
| `mongodb://` / `mongodb+srv://` | `MongoAdapter` | ✅ live (`motor` client, `maxPoolSize`/`serverSelectionTimeoutMS`, native aggregation) |
| `sqlite:///...` | `SqlAdapter` (dialect `sqlite`) | ✅ live |
| `mysql://` | `SqlAdapter` (dialect `mysql`) | `NotImplementedError` (build step 5) |

## Under the hood

- [docs/connection.md](docs/connection.md) — connection URL format, adapter resolution, tuning knobs.
- [docs/dsl_spec.md](docs/dsl_spec.md) — filter/update DSL: operators, grammar, aggregation subset, safety guarantees.
- [docs/supported_operations_matrix.md](docs/supported_operations_matrix.md) — per-backend support + build status for all 28 features.
- [examples/basic_crud.py](examples/basic_crud.py) — every implemented operation, runnable against SQLite.
- [examples/transactions.py](examples/transactions.py) — atomic multi-operation blocks: commit, rollback, cross-collection atomicity.
- [examples/switching_backends.py](examples/switching_backends.py) — one script, three connection strings.
- [plan.md](plan.md) — planning document: feature list, DSL spec, build order, testing strategy.

## Development

```bash
# install dev extras (pytest, pytest-asyncio, drivers)
pip install -e ".[dev]"

# run the test suite from the repo root
PYTHONPATH=src pytest
```

### Branching & releases

All development happens on the `development` branch; `master` is release-only.
The [`publish.yml`](.github/workflows/publish.yml) workflow automates publishing:

| Push to | Tests | Published to | Version |
| --- | --- | --- | --- |
| `development` | ✅ gate | [TestPyPI](https://test.pypi.org/project/genius74o-polydb/) | auto `<base>.dev<run>` (e.g. `0.1.0.dev42`) — every commit gets a unique installable version |
| `master` | ✅ gate | [PyPI](https://pypi.org/project/genius74o-polydb/) | exactly what's in `pyproject.toml` |

To cut a release: bump `version` in `pyproject.toml`, merge `development`
into `master`, and push — PyPI rejects re-uploaded versions, so bump first.

Publishing authenticates with PyPI API tokens stored as GitHub repo secrets
(Settings → Secrets and variables → Actions):

| Secret | Where to create it |
| --- | --- |
| `TESTPYPI_API_TOKEN` | [test.pypi.org](https://test.pypi.org) → Account settings → API tokens |
| `PYPI_API_TOKEN` | [pypi.org](https://pypi.org) → Account settings → API tokens |

or from the CLI: `gh secret set TESTPYPI_API_TOKEN` / `gh secret set PYPI_API_TOKEN`.