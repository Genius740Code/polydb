# Supported operations matrix

Mirrors planning-doc §1. Two independent legends:

**Support legend** (design intent — what each backend *can* do):

- ✅ Full
- 🟡 Partial (works, with the noted caveat)
- ❌ Not supported (raises `UnsupportedOperationError`)

**Status legend** (implementation progress):

- ⬜ Not started · 🔨 In progress · ✅ Done

**Progress summary: 28 / 28 features implemented (100%).** All completed work is
on the SQLite leg of the SQL family — the only backend whose `connect()` opens
(see [connection.md](connection.md) for scheme resolution). Postgres, Mongo,
and MySQL resolve to their adapter instances but their methods raise
`NotImplementedError` until their build steps land.

## 1.1 Connection management

| # | Signature | Postgres | Mongo | SQL family | Status |
| --- | --- | --- | --- | --- | --- |
| 1 | `Database.from_url(url: str) -> Database` | ✅ | ✅ | ✅ | ✅ Done |
| 2 | `async def connect(self) -> None` | ✅ | ✅ | ✅ | ✅ Done |
| 3 | `async def disconnect(self) -> None` | ✅ | ✅ | ✅ | ✅ Done |
| 4 | `async def ping(self) -> bool` | ✅ | ✅ | ✅ | ✅ Done |
| 5 | `async def __aenter__` / `__aexit__` | ✅ | ✅ | ✅ | ✅ Done |
| 6 | `self.pool_size`, `self.timeout` (config) | ✅ (wired at build step 3) | ✅ (wired at build step 4) | 🟡 SQLite has no real pool — non-default `pool_size` accepted but ignored with a logged warning; MySQL wires it at build step 5 | ✅ Done |

## 1.2 Create

| # | Signature | Postgres | Mongo | SQL family | Status |
| --- | --- | --- | --- | --- | --- |
| 7 | `async def insert_one(collection, doc) -> InsertResult` | ✅ | ✅ | ✅ | ✅ Done (SQLite leg; `inserted_id` is the driver's `lastrowid`) |
| 8 | `async def insert_many(collection, docs) -> InsertManyResult` | ✅ | ✅ | ✅ | ✅ Done (SQLite leg; heterogeneous docs fill missing columns with `NULL`, one shared commit = all-or-nothing) |
| 9 | `async def upsert_one(collection, filter, doc) -> UpsertResult` | ✅ (`ON CONFLICT`) | ✅ (`upsert=True`) | ✅ (`ON CONFLICT` / `ON DUPLICATE KEY`) | ✅ Done (SQLite leg; Mongo-style find-first-match-or-insert so arbitrary filters work without unique-index knowledge — filter merges into the inserted row, `doc` wins on key conflicts) |

## 1.3 Read

| # | Signature | Postgres | Mongo | SQL family | Status |
| --- | --- | --- | --- | --- | --- |
| 10 | `async def find_one(collection, filter) -> dict \| None` | ✅ | ✅ | ✅ | ✅ Done (SQLite leg; full DSL filter support via `sql_compiler.py`) |
| 11 | `async def find(collection, filter=None, *, sort=None, limit=None, offset=None) -> list[dict]` | ✅ | ✅ | ✅ | ✅ Done (SQLite leg; `sort` is `(field, 1\|-1)` pairs, later pairs break ties; offset without limit compiles `LIMIT -1 OFFSET n`) |
| 12 | `async def count(collection, filter=None) -> int` | ✅ | ✅ | ✅ | ✅ Done (SQLite leg) |
| 13 | `async def exists(collection, filter) -> bool` | ✅ | ✅ | ✅ | ✅ Done (SQLite leg; short-circuits via `SELECT 1 … LIMIT 1`) |
| 14 | `async def aggregate(collection, pipeline) -> list[dict]` | 🟡 restricted subset (`$match`, `$group`, `$sort`, `$limit`, `$count`) → `GROUP BY`; beyond that raises `UnsupportedOperationError` | ✅ native | 🟡 same restricted subset as Postgres | ✅ Done (SQLite leg; repeatable `$match`, then at most one `$group`/`$sort`/`$limit`/`$count` in canonical order, accumulators `$sum`/`$avg`/`$min`/`$max`/`$count`; Mongo-shaped output incl. nested composite `_id`; global `_id: null` group over zero rows yields `[]`. Known divergence: `NULL` handling inside `MIN`/`MAX`/`AVG` follows SQL semantics) |

## 1.4 Update

| # | Signature | Postgres | Mongo | SQL family | Status |
| --- | --- | --- | --- | --- | --- |
| 15 | `async def update_one(collection, filter, update) -> UpdateResult` | ✅ | ✅ | ✅ | ✅ Done (SQLite leg; full filter DSL + `$set`/`$inc`/`$unset`; first match targeted by rowid; empty SET clause reports honest `matched_count` with `modified_count=0`) |
| 16 | `async def update_many(collection, filter, update) -> UpdateResult` | ✅ | ✅ | ✅ | ✅ Done (SQLite leg; one parameterized `UPDATE … WHERE`. Divergence from Mongo: both counts come from rowcount — already-equal values still count as modified) |
| 17 | `async def replace_one(collection, filter, doc) -> UpdateResult` | ✅ | ✅ | ✅ | ✅ Done (SQLite leg; every non-PK column rewritten, absent doc fields become `NULL`, PK columns preserved and rejected if present in `doc` via `PRAGMA table_info`; never upserts) |

## 1.5 Delete

| # | Signature | Postgres | Mongo | SQL family | Status |
| --- | --- | --- | --- | --- | --- |
| 18 | `async def delete_one(collection, filter) -> DeleteResult` | ✅ | ✅ | ✅ | ✅ Done (SQLite leg; full filter DSL; first match targeted by rowid; no match writes nothing) |
| 19 | `async def delete_many(collection, filter) -> DeleteResult` | ✅ | ✅ | ✅ | ✅ Done (SQLite leg; one parameterized `DELETE … WHERE`; empty filter clears the table like Mongo's `delete_many({})`; count from rowcount) |

## 1.6 Schema / structure

| # | Signature | Postgres | Mongo | SQL family | Status |
| --- | --- | --- | --- | --- | --- |
| 20 | `async def create_collection(name, schema=None) -> None` | ✅ required (`schema=None` raises `SchemaRequiredError`) | ✅ `schema` ignored with a logged info note | ✅ required, same as Postgres | ✅ Done (SQLite leg; Schema→`CREATE TABLE` compiler — `schema=None` or zero-field raises `SchemaRequiredError`; str/int/float/bool native types, datetime/json as TEXT (ISO-8601 / serialized JSON); nullable/default/primary_key/unique enforced by the DDL, `INTEGER PRIMARY KEY` aliases rowid so `inserted_id` works; existing name → `PolydbQueryError`) |
| 21 | `async def drop_collection(name) -> None` | ✅ | ✅ | ✅ | ✅ Done (SQLite leg; `DROP TABLE IF EXISTS`, idempotent no-op on absent names) |
| 22 | `async def list_collections() -> list[str]` | ✅ | ✅ | ✅ | ✅ Done (SQLite leg; sorted user tables from `sqlite_master`, excluding `sqlite_*` internals and views) |
| 23 | `async def create_index(collection, fields, *, unique=False) -> None` | ✅ | ✅ | ✅ | ✅ Done (SQLite leg; `CREATE [UNIQUE] INDEX` with deterministic derived name — `idx_<table>__<f1>__<f2>`, `uq_…` for unique; `IF NOT EXISTS`-idempotent like Mongo's `createIndex` — caveat: a different spec colliding on the derived name is silently ignored; empty field list → `InvalidFilterError`; missing column/table → `PolydbQueryError`) |
| 24 | `async def add_field(collection, field, type_, default=None) -> None` | ✅ | 🟡 no-op + logged warning (docs are dynamic) | ✅ | ✅ Done (SQLite leg; `ALTER TABLE … ADD COLUMN`, same type mapping as #20; non-None scalar default becomes a DDL `DEFAULT` that backfills existing rows; new columns always nullable — SQLite forbids PK/UNIQUE via ALTER TABLE; duplicate column / absent table → `PolydbQueryError`, bad name/type/default → `InvalidFilterError`) |

## 1.7 Transactions

| # | Signature | Postgres | Mongo | SQL family | Status |
| --- | --- | --- | --- | --- | --- |
| 25 | `async def transaction() -> Transaction` | ✅ full ACID | 🟡 requires replica set / Atlas — raises `TransactionsUnavailableError` otherwise | ✅ MySQL full ACID · 🟡 SQLite single-writer: transactions serialize rather than truly isolate under concurrency | ✅ Done (SQLite leg; explicit `BEGIN` on context-manager entry, one atomic block on the adapter's single connection — per-operation commits suppressed until the tx ends, so calls through the yielded handle and direct adapter calls inside the block commit/rollback together; a failed statement aborts the whole transaction (poisoned-tx precedent) and later calls through the handle raise `TransactionInactiveError`; nested/concurrent transactions raise `UnsupportedOperationError`; SQLite transactional DDL rolls back too) |
| 26 | `Transaction.commit()` / `Transaction.rollback()` | ✅ | 🟡 (see #25) | ✅ | ✅ Done (SQLite leg; explicit control plus auto-commit/rollback on context exit; state machine (`new → active → committed \| rolled_back \| aborted`) makes double-finalize and use-before-enter raise `TransactionInactiveError`) |

## 1.8 Escape hatch (raw queries)

| # | Signature | Postgres | Mongo | SQL family | Status |
| --- | --- | --- | --- | --- | --- |
| 27 | `async def raw(query, params=None) -> Any` | ✅ (`str`, params tuple/dict) | ✅ (`dict` command) | ✅ (`str`, params tuple/dict) | ✅ Done (SQLite leg; `query` must be a non-empty SQL string — no DSL compilation; `params` may be `None`, a positional sequence, or a named-parameter mapping; rows return as `list[dict]`; writes commit like every polydb write, or join an open §1.7 transaction instead; driver failures → `PolydbQueryError`, aborting an open transaction wholesale) |
| 28 | `async def explain(collection, filter) -> dict` | ✅ (`EXPLAIN`) | ✅ (`.explain()`) | ✅ (`EXPLAIN`) | ✅ Done (SQLite leg; filter compiles through the shared compiler exactly as `find()` would, then runs under `EXPLAIN QUERY PLAN`; returns `{"backend", "sql", "params", "plan"}` with native plan rows as dicts; read-only) |

## Filter DSL support per backend

The query DSL itself ([dsl_spec.md](dsl_spec.md)) is uniform across backends by
design. Backend-specific translation notes:

| Feature | Mongo | SQL family |
| --- | --- | --- |
| Comparison + logical operators | pass-through to driver | compiled by `sql_compiler.py` into parameterized SQL |
| `$regex` | native | `REGEXP` operator — SQLite resolves it via a registered user function (`re.search` semantics); MySQL native |
| `$like` | normalized to a regex-equivalent (Mongo has no native `LIKE`) | native `LIKE` |
| `$push` update | native | ❌ `UnsupportedOperationError` |
| Identifier safety | n/a (no string interpolation) | names validated against `[A-Za-z_][A-Za-z0-9_]*`, backtick-quoted on SQLite/MySQL, double-quote on Postgres |
