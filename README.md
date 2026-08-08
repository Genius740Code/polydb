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

Planned, not fully built. Implemented so far (planning doc §1.1, **Connection
management**):

| Feature | Status |
| ---- | ------ |
| `Database.from_url(url) -> Database` — factory resolves every supported scheme to its adapter instance | ✅ Done |
| `connect()` / `disconnect()` / `ping()` / async context manager | ✅ SQLite leg (`aiosqlite`) works; Postgres / Mongo / SQL-MySQL legs raise `NotImplementedError` (see build order §6) |
| `pool_size`, `timeout` query params | ✅ parsed; ignored-with-warning on SQLite (single-writer file) |
| CRUD, schema, transactions, raw queries | ⬜ Not started |

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