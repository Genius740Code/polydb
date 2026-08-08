# Connection management

Implements planning-doc §1.1, feature list rows #1–#6.

## `Database.from_url(url) -> Database`

The factory parses a connection string and returns a `Database` wrapping the
correct adapter class — always **unconnected**. Call `await db.connect()` (or
use `async with`) before issuing any other calls.

Supported schemes (adapted from `polydb/url_parser.py`):

| Scheme | Family | Adapter class | Dialect | `connect()` |
| --- | --- | --- | --- | --- |
| `postgres://` | `postgres` | `polydb.adapters.postgres.PostgresAdapter` | — | build step 3 (`NotImplementedError`) |
| `postgresql://` | `postgres` | `PostgresAdapter` | — | build step 3 |
| `mongodb://` | `mongo` | `polydb.adapters.mongo.MongoAdapter` | — | build step 4 |
| `mongodb+srv://` | `mongo` | `MongoAdapter` | — | build step 4 |
| `sqlite:///...` | `sql` | `polydb.adapters.sql.base.SqlAdapter` | `sqlite` | ✅ implemented |
| `mysql://` | `sql` | `SqlAdapter` | `mysql` | build step 5 (`NotImplementedError`) |

The SQL family is **one adapter class** (`SqlAdapter`) serving both SQLite and
MySQL, differing only by its `Dialect` — confirmed by open question §8.9.

### Errors

- Unknown / malformed URL → `InvalidConnectionStringError`.
- Scheme recognized but its build step hasn't landed → the resolved adapter
  instance is still returned; its methods raise `NotImplementedError` naming the
  planning-doc build step.

### Why adapters resolve even before they're built

`from_url` only needs to *construct* the right adapter class. Importing `polydb`
never pulls in a driver: each adapter module imports its driver (`asyncpg`,
`motor`, `aiosqlite`, `asyncmy`) lazily inside `connect()`, and the factory
imports adapter modules lazily inside `_build_adapter()`.

## Connection-string format

Components mirror the standard scheme syntax; the following query params are
understood by polydb itself:

| Parameter | Default | Meaning |
| --- | --- | --- |
| `pool_size` | `10` | Pool/client size. 🟡 SQLite has no real pool (single-writer file) — accepted but ignored, with a logged warning. |
| `timeout` | `30.0` | Connection timeout in seconds. |

E.g. `sqlite:///app.db?pool_size=5&timeout=3.0`.

All other query params (e.g. `sslmode=require`) are preserved verbatim in
`ConnectionConfig.options` for adapter-specific use.

## Connection lifecycle (`§1.1 #2`–`#6`)

```python
from polydb import Database

db = Database.from_url("sqlite:///:memory:")
await db.connect()        # idempotent — calling twice is a no-op
assert await db.ping()    # cheap round-trip health check
await db.disconnect()     # no-op if never connected
```

Or use the sugar:

```python
async with Database.from_url("sqlite:///:memory:") as db:
    assert await db.ping() is True
```

Calls that require an open connection raise `ConnectionNotOpenError` otherwise.

## Relationship to planning-doc §6 (build order)

- Step 1 (contract: `BaseAdapter`, `Database`, `url_parser`, exceptions) — **done**.
- Step 2 (SQL family, SQLite leg, connection management) — **done**.
- Steps 3–5 (Postgres connect, Mongo connect, MySQL connect) — **pending**.