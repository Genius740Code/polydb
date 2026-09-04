# Connection management

Implements planning-doc §1.1, feature list rows #1–#6.

## `Database.from_url(url) -> Database`

The factory parses a connection string and returns a `Database` wrapping the
correct adapter class — always **unconnected**. Call `await db.connect()` (or
use `async with`) before issuing any other calls.

Supported schemes (adapted from `polydb/url_parser.py`):

| Scheme | Family | Adapter class | Dialect | `connect()` |
| --- | --- | --- | --- | --- |
| `postgres://` | `postgres` | `polydb.adapters.postgres.PostgresAdapter` | `postgres` | ✅ implemented (asyncpg pool, `$1` placeholders, `~` for `$regex`) |
| `postgresql://` | `postgres` | `PostgresAdapter` | `postgres` | ✅ implemented |
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
| `pool_size` | `10` | Pool/client size. Must be an integer ≥ 1. 🟡 SQLite has no real pool (single-writer file) — accepted but ignored, with a logged warning when a non-default value is given. |
| `timeout` | `30.0` | Connection timeout in seconds. Must be a number > 0. |

E.g. `sqlite:///app.db?pool_size=5&timeout=3.0`.

Malformed values raise `InvalidConnectionStringError` at `from_url()` time — a
non-integer `pool_size`, a non-numeric `timeout`, or either out of its valid
range never reaches an adapter (§3.4 convention: polydb exceptions only).

The parsed knobs are also readable directly off any adapter instance
(`db.pool_size`, `db.timeout`) — Postgres/Mongo/MySQL wire them into their
native pools during their respective build steps.

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

### `ping()` semantics (`§1.1 #4`)

- **Before `connect()`** → raises `ConnectionNotOpenError` (same guard clause as
  every other method — pinging an unopened connection is a caller bug).
- **Healthy round-trip** → returns `True`.
- **Round-trip fails** (backend down, disk error, driver in a bad state) →
  returns `False`. The driver error is logged at WARNING level and swallowed:
  a health check reports, it never throws. A later ping can return `True`
  again — a failed ping never poisons adapter state.

### Lifecycle guarantees

- **`connect()` is idempotent.** Calling it twice opens a single handle; the
  second call is a no-op.
- **`disconnect()` is idempotent in both directions.** Disconnecting before
  connecting, or disconnecting twice, is a no-op. A failed driver `close()`
  can never strand the adapter "connected": the handle is detached (and state
  cleared) *before* `close()` runs.
- **`connect()` failure leaves the adapter cleanly disconnected.** If the
  driver open raises, the adapter remains at its initial "not connected"
  state and a retry is safe.
- **A failed `connect()` after resolution is a clean failure** — if the
  `from_url` parse step would produce a database-less SQLite URL, `connect()`
  raises `InvalidConnectionStringError` rather than opening something
  nonsensical. (The parser itself already rejects `sqlite:///`.)
- **The `async with` block releases the connection on every exit path** — a
  clean exit, an in-body exception, or a `disconnect()` failure. This also
  means a connection is never leaked when the body raises.
- **Cleanup never masks an in-body exception.** If the body raises and
  `disconnect()` fails too, the body's exception propagates and the cleanup
  failure is logged as a warning instead of replacing it. If only cleanup
  fails (no body error), the `disconnect()` error propagates.

## Relationship to planning-doc §6 (build order)

- Step 1 (contract: `BaseAdapter`, `Database`, `url_parser`, exceptions) — **done**.
- Step 2 (SQL family, SQLite leg) — **done**: connection management plus the
  Create (`insert_one`/`insert_many`/`upsert_one`), Read
  (`find_one`/`find`/`count`/`exists`/`aggregate`), and Update
  (`update_one`/`update_many`/`replace_one`) surfaces. See
  [supported_operations_matrix.md](supported_operations_matrix.md) for exactly
  what is live, and [dsl_spec.md](dsl_spec.md) for the filter/update DSL those
  methods accept.
- Step 3 (Postgres adapter, asyncpg) — **done**: full CRUD, schema, transactions,
  `raw`/`explain`, reusing the shared `SqlCompiler` with Postgres dialect
  (numbered `$1` placeholders via `number_placeholders()`, `"` quoting, `~` for
  `$regex`, `GROUP BY ()` for global groups, `OFFSET` without `LIMIT -1`).
- Steps 4–5 (Mongo connect, MySQL connect) — **pending**;
  until then their adapters raise `NotImplementedError` from `connect()`.