# Supported operations matrix

Mirrors planning-doc §1. Two independent legends:

**Support legend** (design intent — what each backend *can* do):

- ✅ Full
- 🟡 Partial (works, with the noted caveat)
- ❌ Not supported (raises `UnsupportedOperationError`)

**Status legend** (implementation progress):

- ⬜ Not started · 🔨 In progress · ✅ Done

**Progress summary: 28 / 28 features implemented (100%) on three legs.**
SQLite (SQL family, `sqlite` dialect), **Postgres** (`asyncpg`), and
**MongoDB** (`motor`) are all live — every feature below is implemented on all
three backends (see [connection.md](connection.md) for scheme resolution).
MySQL still resolves to its adapter instance but its methods raise
`NotImplementedError` until its build step lands.

## 1.1 Connection management

| # | Signature | Postgres | Mongo | SQL family | Status |
| --- | --- | --- | --- | --- | --- |
| 1 | `Database.from_url(url: str) -> Database` | ✅ | ✅ | ✅ | ✅ Done |
| 2 | `async def connect(self) -> None` | ✅ | ✅ | ✅ | ✅ Done |
| 3 | `async def disconnect(self) -> None` | ✅ | ✅ | ✅ | ✅ Done |
| 4 | `async def ping(self) -> bool` | ✅ | ✅ | ✅ | ✅ Done |
| 5 | `async def __aenter__` / `__aexit__` | ✅ | ✅ | ✅ | ✅ Done |
| 6 | `self.pool_size`, `self.timeout` (config) | ✅ (wired — `max_size`/`command_timeout` on `asyncpg` pool) | ✅ (wired — `maxPoolSize`/`serverSelectionTimeoutMS` on `motor` client) | 🟡 SQLite has no real pool — non-default `pool_size` accepted but ignored with a logged warning; MySQL wires it at build step 5 | ✅ Done |

## 1.2 Create

| # | Signature | Postgres | Mongo | SQL family | Status |
| --- | --- | --- | --- | --- | --- |
| 7 | `async def insert_one(collection, doc) -> InsertResult` | ✅ | ✅ | ✅ | ✅ Done (SQLite: `lastrowid`; Postgres: `RETURNING *` + PK pick; Mongo: `insert_one` native) |
| 8 | `async def insert_many(collection, docs) -> InsertManyResult` | ✅ | ✅ | ✅ | ✅ Done (SQLite + Postgres: heterogeneous docs fill missing columns with `NULL`, one shared commit = all-or-nothing; Mongo: `insert_many` native, empty list is no-op) |
| 9 | `async def upsert_one(collection, filter, doc) -> UpsertResult` | ✅ (`ON CONFLICT`) | ✅ (`upsert=True`) | ✅ (`ON CONFLICT` / `ON DUPLICATE KEY`) | ✅ Done (SQLite + Postgres + Mongo; Mongo-style find-first-match-or-insert so arbitrary filters work without unique-index knowledge — filter merges into the inserted row, `doc` wins on key conflicts; Mongo uses `find_one` + `update_one` by `_id` / `insert_one` to mirror the SQL legs) |

## 1.3 Read

| # | Signature | Postgres | Mongo | SQL family | Status |
| --- | --- | --- | --- | --- | --- |
| 10 | `async def find_one(collection, filter) -> dict \| None` | ✅ | ✅ | ✅ | ✅ Done (SQLite + Postgres + Mongo; full DSL filter support via `sql_compiler.py` / `mongo_compiler.py` — `$like` via `$expr`/`$regexMatch` on Mongo) |
| 11 | `async def find(collection, filter=None, *, sort=None, limit=None, offset=None) -> list[dict]` | ✅ | ✅ | ✅ | ✅ Done (SQLite + Postgres + Mongo; `sort` is `(field, 1\|-1)` pairs, later pairs break ties; offset without limit compiles `LIMIT -1 OFFSET n` on SQLite, `OFFSET n` on Postgres, `skip` on Mongo) |
| 12 | `async def count(collection, filter=None) -> int` | ✅ | ✅ | ✅ | ✅ Done (SQLite + Postgres + Mongo; `count_documents` on Mongo) |
| 13 | `async def exists(collection, filter) -> bool` | ✅ | ✅ | ✅ | ✅ Done (SQLite + Postgres + Mongo; short-circuits via `SELECT 1 … LIMIT 1` / `find_one` projection on Mongo) |
| 14 | `async def aggregate(collection, pipeline) -> list[dict]` | 🟡 restricted subset (`$match`, `$group`, `$sort`, `$limit`, `$count`) → `GROUP BY`; beyond that raises `UnsupportedOperationError` | ✅ native | 🟡 same restricted subset as Postgres | ✅ Done (SQLite + Postgres: repeatable `$match`, then at most one `$group`/`$sort`/`$limit`/`$count` in canonical order, accumulators `$sum`/`$avg`/`$min`/`$max`/`$count`; Mongo-shaped output incl. nested composite `_id`; global `_id: null` group over zero rows yields `[]`. Known divergence: `NULL` handling inside `MIN`/`MAX`/`AVG` follows SQL semantics. Mongo: native pipeline, no restriction.) |

## 1.4 Update

| # | Signature | Postgres | Mongo | SQL family | Status |
| --- | --- | --- | --- | --- | --- |
| 15 | `async def update_one(collection, filter, update) -> UpdateResult` | ✅ | ✅ | ✅ | ✅ Done (SQLite + Postgres + Mongo; full filter DSL + `$set`/`$inc`/`$unset` + `$push` on Mongo; first match targeted by rowid (SQLite) / ctid (Postgres) / native `update_one` on Mongo; empty update reports honest `matched_count` with `modified_count=0`) |
| 16 | `async def update_many(collection, filter, update) -> UpdateResult` | ✅ | ✅ | ✅ | ✅ Done (SQLite + Postgres: one parameterized `UPDATE … WHERE`. Divergence from Mongo: both counts come from rowcount — already-equal values still count as modified. Mongo: native `update_many`, counts from driver.) |
| 17 | `async def replace_one(collection, filter, doc) -> UpdateResult` | ✅ | ✅ | ✅ | ✅ Done (SQLite + Postgres: every non-PK column rewritten, absent doc fields become `NULL`, PK columns preserved and rejected if present in `doc` via `PRAGMA table_info` (SQLite) / `information_schema` (Postgres); never upserts. Mongo: native `replace_one`, never upserts.) |

## 1.5 Delete

| # | Signature | Postgres | Mongo | SQL family | Status |
| --- | --- | --- | --- | --- | --- |
| 18 | `async def delete_one(collection, filter) -> DeleteResult` | ✅ | ✅ | ✅ | ✅ Done (SQLite + Postgres + Mongo; full filter DSL; first match targeted by rowid (SQLite) / ctid (Postgres) / native `delete_one` on Mongo; no match writes nothing) |
| 19 | `async def delete_many(collection, filter) -> DeleteResult` | ✅ | ✅ | ✅ | ✅ Done (SQLite + Postgres + Mongo; one parameterized `DELETE … WHERE`; empty filter clears the table like Mongo's `delete_many({})`; count from rowcount / native `delete_many` on Mongo) |

## 1.6 Schema / structure

| # | Signature | Postgres | Mongo | SQL family | Status |
| --- | --- | --- | --- | --- | --- |
| 20 | `async def create_collection(name, schema=None) -> None` | ✅ required (`schema=None` raises `SchemaRequiredError`) | ✅ `schema` ignored with a logged info note | ✅ required, same as Postgres | ✅ Done (SQLite + Postgres + Mongo; Schema→`CREATE TABLE` compiler — `schema=None` or zero-field raises `SchemaRequiredError` on relational; str/int/float/bool native types, datetime/json as TEXT (ISO-8601 / serialized JSON); nullable/default/primary_key/unique enforced by the DDL, `INTEGER PRIMARY KEY` aliases rowid (SQLite) / `SERIAL PRIMARY KEY` (Postgres) so `inserted_id` works; existing name → `PolydbQueryError` on relational, no-op on Mongo (schemaless)) |
| 21 | `async def drop_collection(name) -> None` | ✅ | ✅ | ✅ | ✅ Done (SQLite + Postgres + Mongo; `DROP TABLE IF EXISTS` / `drop_collection`, idempotent no-op on absent names) |
| 22 | `async def list_collections() -> list[str]` | ✅ | ✅ | ✅ | ✅ Done (SQLite + Postgres + Mongo; sorted user tables from `sqlite_master` (SQLite) / `pg_tables` (Postgres) / `list_collection_names` (Mongo), excluding `sqlite_*` internals and views / `system.*` on Mongo) |
| 23 | `async def create_index(collection, fields, *, unique=False) -> None` | ✅ | ✅ | ✅ | ✅ Done (SQLite + Postgres + Mongo; `CREATE [UNIQUE] INDEX` with deterministic derived name — `idx_<table>__<f1>__<f2>`, `uq_…` for unique; `IF NOT EXISTS`-idempotent like Mongo's `createIndex` — caveat: a different spec colliding on the derived name is silently ignored on SQL; empty field list → `InvalidFilterError`; missing column/table → `PolydbQueryError` on SQL) |
| 24 | `async def add_field(collection, field, type_, default=None) -> None` | ✅ | 🟡 no-op + logged warning (docs are dynamic) | ✅ | ✅ Done (SQLite + Postgres + Mongo; `ALTER TABLE … ADD COLUMN`, same type mapping as #20; non-None scalar default becomes a DDL `DEFAULT` that backfills existing rows; new columns always nullable — SQLite forbids PK/UNIQUE via ALTER TABLE; duplicate column / absent table → `PolydbQueryError`, bad name/type/default → `InvalidFilterError`. Mongo: no-op + `logger.warning`.) |

## 1.7 Transactions

| # | Signature | Postgres | Mongo | SQL family | Status |
| --- | --- | --- | --- | --- | --- |
| 25 | `async def transaction() -> Transaction` | ✅ full ACID | 🟡 requires replica set / Atlas — raises `TransactionsUnavailableError` otherwise | ✅ MySQL full ACID · 🟡 SQLite single-writer: transactions serialize rather than truly isolate under concurrency | ✅ Done (SQLite + Postgres + Mongo; explicit `BEGIN`/`start_transaction` on context-manager entry, one atomic block — per-operation commits suppressed until the tx ends, so calls through the yielded handle and direct adapter calls inside the block commit/rollback together; a failed statement aborts the whole transaction (poisoned-tx precedent) and later calls through the handle raise `TransactionInactiveError`; nested/concurrent transactions raise `UnsupportedOperationError`; SQLite transactional DDL rolls back too; Mongo via `motor` `ClientSession`) |
| 26 | `Transaction.commit()` / `Transaction.rollback()` | ✅ | 🟡 (see #25) | ✅ | ✅ Done (SQLite + Postgres + Mongo; explicit control plus auto-commit/rollback on context exit; state machine (`new → active → committed \| rolled_back \| aborted`) makes double-finalize and use-before-enter raise `TransactionInactiveError`) |

## 1.8 Escape hatch (raw queries)

| # | Signature | Postgres | Mongo | SQL family | Status |
| --- | --- | --- | --- | --- | --- |
| 27 | `async def raw(query, params=None) -> Any` | ✅ (`str`, params tuple/dict) | ✅ (`dict` command) | ✅ (`str`, params tuple/dict) | ✅ Done (SQLite + Postgres + Mongo; `query` must be a non-empty SQL string — no DSL compilation; `params` may be `None`, a positional sequence, or a named-parameter mapping on SQL, `None`/`dict` on Mongo (`dict` merges into command); rows return as `list[dict]` on SQL, raw command result on Mongo; writes commit like every polydb write, or join an open §1.7 transaction instead; driver failures → `PolydbQueryError`, aborting an open transaction wholesale) |
| 28 | `async def explain(collection, filter) -> dict` | ✅ (`EXPLAIN`) | ✅ (`.explain()`) | ✅ (`EXPLAIN`) | ✅ Done (SQLite + Postgres + Mongo; filter compiles through the shared compiler exactly as `find()` would, then runs under `EXPLAIN QUERY PLAN` (SQL) / `find().explain()` (Mongo); returns `{"backend", "sql", "params", "plan"}` (SQL) or `{"backend", "filter", "plan"}` (Mongo) with native plan rows; read-only) |

## Filter DSL support per backend

The query DSL itself ([dsl_spec.md](dsl_spec.md)) is uniform across backends by
design. Backend-specific translation notes:

| Feature | Mongo | SQL family |
| --- | --- | --- |
| Comparison + logical operators | validated + passed through by `mongo_compiler.py` (standalone `$not` rewritten as one-element `$nor` — Mongo rejects top-level `$not`) | compiled by `sql_compiler.py` into parameterized SQL |
| `$regex` | native | Postgres: `"field" ~ $1` (POSIX `~`); SQLite: `REGEXP` via `re.search` UDF; MySQL: `REGEXP` native |
| `$like` | normalized to an anchored `$expr` + `$regexMatch` regex (`%` → `.*`, `_` → `.`, case-sensitive; Mongo has no native `LIKE`) | native `LIKE` |
| `$push` update | native (validated pass-through by `mongo_compiler.py`) | ❌ `UnsupportedOperationError` |
| Identifier safety | names validated against `[A-Za-z_][A-Za-z0-9_]*` before use | same validation, then identifier-quoted: backtick on SQLite/MySQL, double-quote on Postgres; Postgres placeholders renumbered `?` → `$N` via `number_placeholders()` |
