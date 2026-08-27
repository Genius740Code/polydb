from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from polydb.adapters.sql.base import SqlAdapter
from polydb.adapters.sql.dialects import PostgresDialect
from polydb.base import BaseAdapter, Transaction
from polydb.compilers.sql_compiler import SqlCompiler
from polydb.exceptions import (
    ConnectionNotOpenError,
    InvalidFilterError,
    PolydbQueryError,
    SchemaRequiredError,
    TransactionInactiveError,
)
from polydb.results import DeleteResult, InsertManyResult, InsertResult, UpdateResult, UpsertResult
from polydb.schema import FieldType, Schema
from polydb.url_parser import ConnectionConfig

logger = logging.getLogger("polydb.adapters.postgres")


class PostgresTransaction(Transaction):
    """PostgreSQL transaction handle (§1.7 #25–#26).

    One explicit ``BEGIN`` … ``COMMIT``/``ROLLBACK`` block on a connection
    from the asyncpg pool. Entering the context manager issues the ``BEGIN``;
    while it is open every operation routed through this object — and any
    call made directly on the adapter, which shares the same pool — executes
    *inside* the block because per-operation commits are suppressed until the
    transaction ends.

    A failed statement inside the transaction aborts the whole thing: the
    connection is rolled back immediately and the handle is marked aborted,
    so subsequent calls through it raise ``TransactionInactiveError`` rather
    than silently continuing in autocommit mode. This matches Postgres's
    poisoned-transaction behavior.

    State machine: ``new → active → committed | rolled_back | aborted``.
    """

    def __init__(self, adapter: PostgresAdapter) -> None:
        self._adapter = adapter
        self._state = "new"
        self._conn: Any = None

    async def __aenter__(self) -> Transaction:
        if self._state != "new":
            raise TransactionInactiveError(
                f"this Transaction was already used (state: {self._state}); "
                f"call db.transaction() again for a fresh one"
            )
        # Acquire a connection from the pool for the transaction
        self._conn = await self._adapter._pool.acquire()
        try:
            await self._conn.execute("BEGIN")
        except Exception as err:
            await self._adapter._pool.release(self._conn)
            self._conn = None
            raise PolydbQueryError(f"Postgres BEGIN failed: {err}") from err
        self._state = "active"
        self._adapter._tx = self
        return self

    async def commit(self) -> None:
        """Explicitly commit the transaction. See BaseAdapter/Transaction.commit."""
        if self._state != "active":
            raise TransactionInactiveError(
                f"commit() requires an active transaction; current state is "
                f"{self._state!r}"
            )
        try:
            await self._conn.execute("COMMIT")
        finally:
            await self._finish("committed")

    async def rollback(self) -> None:
        """Explicitly roll back the transaction. See Transaction.rollback."""
        if self._state != "active":
            raise TransactionInactiveError(
                f"rollback() requires an active transaction; current state "
                f"is {self._state!r}"
            )
        try:
            await self._conn.execute("ROLLBACK")
        finally:
            await self._finish("rolled_back")

    async def _finish(self, state: str) -> None:
        """Record the end state and release the connection back to the pool."""
        self._state = state
        if self._conn is not None:
            await self._adapter._pool.release(self._conn)
            self._conn = None
        if self._adapter._tx is self:
            self._adapter._tx = None

    def abort(self) -> None:
        """Mark the transaction dead after a failed statement inside it.

        Called by ``PostgresAdapter._rollback_and_raise``, which owns the actual
        connection rollback — this only flips state so later calls through
        the handle refuse instead of continuing outside the (now gone)
        transaction.
        """
        self._state = "aborted"
        if self._adapter._tx is self:
            self._adapter._tx = None

    def __getattr__(self, name: str) -> Any:
        """Delegate every adapter operation to the wrapped adapter.

        Only fires for attributes not defined on the class itself, so
        ``commit`` / ``rollback`` / ``__aenter__`` / ``__aexit__`` stay local.
        Operations are refused unless the transaction is currently active,
        which blocks both pre-enter usage and post-finalize usage.
        """
        if name.startswith("_"):
            raise AttributeError(name)
        if self.__dict__.get("_state") != "active":
            raise TransactionInactiveError(
                f"{name}() requires an active transaction — enter it first "
                f"via `async with db.transaction() as tx:` (current state: "
                f"{self.__dict__.get('_state', 'new')!r})"
            )
        return getattr(self._adapter, name)

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._state != "active":
            # Already finalized (explicit commit/rollback inside the body) or
            # aborted by a failed statement — there is nothing left to commit
            # or roll back, so exiting cleanly is the correct no-op either way.
            return
        if exc_type is None:
            await self.commit()
            return
        try:
            await self.rollback()
        except Exception as cleanup_error:
            logger.warning(
                "Ignoring rollback failure while handling an error in the "
                "transaction body: %s",
                cleanup_error,
            )


class PostgresAdapter(BaseAdapter):
    """PostgreSQL adapter (backed by ``asyncpg``).

    Reuses the shared SQL compiler with the Postgres dialect (numbered
    placeholders, double-quote identifier quoting, native regex via ``~``).
    Pool configuration (``pool_size``, ``timeout``) is wired through from
    the connection string query parameters.

    All 28 features from §1 are implemented on the Postgres leg (planning
    doc §6, build order step 3).
    """

    def __init__(self, config: ConnectionConfig) -> None:
        super().__init__(config)
        self.dialect = PostgresDialect
        self._compiler = SqlCompiler(self.dialect)
        self._pool: Any = None  # asyncpg.Pool once connected
        self._tx: PostgresTransaction | None = None  # open transaction handle, if any

    # -- 1.1 Connection management -------------------------------------------------

    async def connect(self) -> None:
        """Open the asyncpg pool. Idempotent — calling twice is a no-op."""
        if self._connected:
            return

        import asyncpg

        # Parse connection string components from config
        if self.config.host is None:
            raise PolydbQueryError("PostgreSQL connection requires a host")
        if self.config.database is None:
            raise PolydbQueryError("PostgreSQL connection requires a database name")

        dsn = (
            f"postgres://{self.config.username or ''}:{self.config.password or ''}"
            f"@{self.config.host}:{self.config.port or 5432}/{self.config.database}"
        )

        try:
            self._pool = await asyncpg.create_pool(
                dsn,
                min_size=1,
                max_size=self.config.pool_size,
                command_timeout=self.config.timeout,
            )
        except Exception as err:
            raise PolydbQueryError(f"Failed to create Postgres pool: {err}") from err

        self._connected = True

    async def disconnect(self) -> None:
        """Close the pool, releasing all connections.

        Idempotent — calling on a never-connected or already-disconnected
        adapter is a no-op. The pool is closed *before* the await runs, so a
        failing close can never leave the adapter stuck in a "connected" state.
        """
        pool, self._pool = self._pool, None
        self._connected = False
        if pool is not None:
            await pool.close()

    async def ping(self) -> bool:
        """Cheap round-trip health check: ``SELECT 1``.

        Returns:
            ``True`` if the backend answered, ``False`` if the round-trip
            failed (the driver error is logged at WARNING level and swallowed —
            a health check reports, it doesn't throw).

        Raises:
            ConnectionNotOpenError: If ``connect()`` has not yet succeeded.
        """
        self._ensure_connected()
        try:
            async with self._pool.acquire() as conn:
                await conn.execute("SELECT 1")
        except Exception as err:
            logger.warning("ping() round-trip failed on postgres: %r", err)
            return False
        return True

    # -- shared write-path helpers -------------------------------------------------

    def _quote_ident(self, name: str) -> str:
        """Identifier-quote an already-validated name using the dialect's quote char."""
        q = self.dialect.identifier_quote
        return f"{q}{name}{q}"

    def _validated_table(self, collection: str) -> str:
        """Validate a collection (table) name and return it quoted for SQL."""
        from polydb.compilers.sql_compiler import validate_identifier
        return self._quote_ident(validate_identifier(collection, "table"))

    def _validated_columns(self, names: list[str]) -> list[str]:
        """Return the given column names validated, preserving order."""
        from polydb.compilers.sql_compiler import validate_identifier
        return [validate_identifier(name, "column") for name in names]

    async def _execute_write(self, sql: str, params: list[Any]) -> Any:
        """Execute a write statement, handling transaction vs. autocommit mode."""
        if self._tx is not None and self._tx._conn is not None:
            # Inside a transaction, use the transaction's dedicated connection
            return await self._tx._conn.execute(sql, *params)
        else:
            # Autocommit mode: acquire a connection, execute, and release
            async with self._pool.acquire() as conn:
                return await conn.execute(sql, *params)

    async def _execute_read(self, sql: str, params: list[Any]) -> Any:
        """Execute a read statement, handling transaction vs. autocommit mode."""
        if self._tx is not None and self._tx._conn is not None:
            return await self._tx._conn.fetch(sql, *params)
        else:
            async with self._pool.acquire() as conn:
                return await conn.fetch(sql, *params)

    async def _execute_fetchone(self, sql: str, params: list[Any]) -> Any:
        """Execute a read statement returning a single row."""
        if self._tx is not None and self._tx._conn is not None:
            return await self._tx._conn.fetchrow(sql, *params)
        else:
            async with self._pool.acquire() as conn:
                return await conn.fetchrow(sql, *params)

    async def _rollback_and_raise(
        self, operation: str, collection: str, err: Exception
    ) -> None:
        """Roll back the open transaction and re-raise as PolydbQueryError.

        When a user transaction is open, a failed statement aborts it
        wholesale (the Postgres poisoned-transaction precedent): the
        whole block is rolled back here and the handle is marked aborted, so
        later calls through it raise ``TransactionInactiveError`` instead of
        silently continuing in autocommit mode.
        """
        if self._tx is not None:
            logger.info(
                "Aborting open transaction after failed %s on %r", operation, collection
            )
            self._tx.abort()
            try:
                if self._tx._conn is not None:
                    await self._tx._conn.execute("ROLLBACK")
            except Exception as rollback_error:  # pragma: no cover - defensive
                logger.warning("rollback after failed %s also failed: %r", operation, rollback_error)
        raise PolydbQueryError(
            f"Postgres {operation} failed on collection {collection!r}: {err}"
        ) from err

    async def _table_layout(self, collection: str) -> tuple[bool, list[str], set[str]]:
        """Introspect the table behind ``collection`` (PostgreSQL information_schema).

        Returns:
            ``(exists, columns, pk_columns)`` — every real Postgres table has at
            least one column, so an empty column list unambiguously means the
            table does not exist. Reads only; opens no transaction.
        """
        table = self._validated_table(collection)
        self._compiler._reset_params()
        sql = """
            SELECT column_name, is_nullable
            FROM information_schema.columns
            WHERE table_name = $1
            ORDER BY ordinal_position
        """
        rows = await self._execute_read(sql, [collection])
        if not rows:
            return False, [], set()

        columns = [row["column_name"] for row in rows]

        # Get primary key columns
        pk_sql = """
            SELECT column_name
            FROM information_schema.key_column_usage
            WHERE table_name = $1 AND constraint_name = (
                SELECT constraint_name
                FROM information_schema.table_constraints
                WHERE table_name = $1 AND constraint_type = 'PRIMARY KEY'
            )
        """
        pk_rows = await self._execute_read(pk_sql, [collection])
        pk_columns = {row["column_name"] for row in pk_rows}

        return True, columns, pk_columns

    # -- 1.2 Create ----------------------------------------------------------------

    async def insert_one(self, collection: str, doc: dict[str, Any]) -> InsertResult:
        """Insert a single record. See BaseAdapter.insert_one."""
        self._ensure_connected()  # 1. guard
        table = self._validated_table(collection)
        columns = self._validated_columns(list(doc.keys()))  # 2. translate
        if columns:
            column_sql = ", ".join(self._quote_ident(c) for c in columns)
            # Build placeholders manually since insert is not compiled through the SQL compiler
            params = [doc[column] for column in columns]
            self._compiler._reset_params()
            placeholders = [self._compiler._ph() for _ in columns]
            sql = f"INSERT INTO {table} ({column_sql}) VALUES ({', '.join(placeholders)})"
        else:
            sql = f"INSERT INTO {table} DEFAULT VALUES"
            params = []

        try:  # 3. execute
            result = await self._execute_write(sql, params)
            inserted_id = result[-1]["oid"] if result else None
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("insert_one", collection, err)
        return InsertResult(inserted_id=inserted_id)  # 5. result type

    async def insert_many(
        self, collection: str, docs: list[dict[str, Any]]
    ) -> InsertManyResult:
        """Bulk insert. See BaseAdapter.insert_many.

        Documents may be heterogeneous: the column set is the sorted union of
        all documents' keys and documents missing a column insert ``NULL``
        there. Uses executemany for efficiency. The batch shares one commit,
        so it is all-or-nothing on failure.
        """
        self._ensure_connected()  # 1. guard
        if not docs:
            return InsertManyResult(inserted_ids=[], inserted_count=0)

        table = self._validated_table(collection)
        columns = sorted({key for doc in docs for key in doc})
        validated = self._validated_columns(columns)
        column_sql = ", ".join(self._quote_ident(c) for c in validated)
        self._compiler._reset_params()
        placeholder_sql = ", ".join([self._compiler._ph() for _ in validated])
        sql = f"INSERT INTO {table} ({column_sql}) VALUES ({placeholder_sql})"

        try:  # 3. execute
            async with self._pool.acquire() as conn:
                # executemany style insertion
                inserted_ids = []
                for doc in docs:
                    self._compiler._reset_params()
                    params = [doc.get(column) for column in columns]
                    result = await conn.execute(sql, *params)
                    inserted_ids.append(result[-1]["oid"] if result else None)
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("insert_many", collection, err)
        return InsertManyResult(  # 5. result type
            inserted_ids=inserted_ids, inserted_count=len(inserted_ids)
        )

    async def upsert_one(
        self, collection: str, filter: dict[str, Any], doc: dict[str, Any]
    ) -> UpsertResult:
        """Insert-or-update by filter match. See BaseAdapter.upsert_one.

        Uses Postgres's native ``ON CONFLICT`` syntax for efficient upserts.
        This differs from the SQLite leg's find-then-update approach but
        achieves the same Mongo-style semantics.
        """
        self._ensure_connected()  # 1. guard
        table = self._validated_table(collection)

        # For simplicity, we'll use the SQLite-style approach of find-then-update-or-insert
        # since ON CONFLICT requires a unique constraint on the conflict target
        # and our filter can be arbitrary
        equality: dict[str, Any] = {}
        from polydb.compilers.sql_compiler import validate_identifier

        for key, value in filter.items():
            column = validate_identifier(key, "column")
            if isinstance(value, (dict, list)):
                raise InvalidFilterError(
                    f"upsert_one filter values must be plain scalars; "
                    f"got {type(value).__name__} for {key!r}"
                )
            equality[column] = value

        where_sql, where_params = self._compiler.compile_where(filter)  # 2. translate
        update_columns = self._validated_columns(list(doc.keys()))

        try:  # 3. execute
            # Target exactly the first matching row (Mongo update_one/upsert semantics)
            # Postgres uses ctid instead of rowid
            select_sql = f"SELECT ctid FROM {table}{where_sql} LIMIT 1"
            row = await self._execute_fetchone(select_sql, where_params)

            if row is not None:
                # Update path
                modified_count = 0
                if update_columns:
                    self._compiler._reset_params()
                    set_sql = ", ".join(
                        f"{self._quote_ident(column)} = {self._compiler._ph()}"
                        for column in update_columns
                    )
                    self._compiler._reset_params()
                    update_sql = f"UPDATE {table} SET {set_sql} WHERE ctid = {self._compiler._ph()}"
                    params = [*doc.values(), row["ctid"]]
                    await self._execute_write(update_sql, params)
                    modified_count = 1
                return UpsertResult(matched_count=1, modified_count=modified_count)

            # Insert path
            merged: dict[str, Any] = {**equality, **doc}
            merged_columns = self._validated_columns(list(merged.keys()))
            if merged_columns:
                column_sql = ", ".join(self._quote_ident(c) for c in merged_columns)
                self._compiler._reset_params()
                placeholder_sql = ", ".join([self._compiler._ph() for _ in merged_columns])
                upsert_sql = (
                    f"INSERT INTO {table} ({column_sql}) "
                    f"VALUES ({placeholder_sql})"
                )
                params = [merged[column] for column in merged_columns]
            else:
                upsert_sql = f"INSERT INTO {table} DEFAULT VALUES"
                params = []
            result = await self._execute_write(upsert_sql, params)
            upserted_id = result[-1]["oid"] if result else None
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("upsert_one", collection, err)
        return UpsertResult(  # 5. result type
            matched_count=0, modified_count=0, upserted_id=upserted_id
        )

    # -- 1.3 Read -------------------------------------------------------------------

    async def find_one(
        self, collection: str, filter: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Fetch the first document matching ``filter``. See BaseAdapter.find_one."""
        self._ensure_connected()  # 1. guard
        table = self._validated_table(collection)
        query = self._compiler.compile_find(  # 2. translate
            table, filter, sort=None, limit=1, offset=None
        )
        try:  # 3. execute
            row = await self._execute_fetchone(query.sql, query.params)
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("find_one", collection, err)
        return dict(row) if row is not None else None  # 5. normalize + return

    async def find(
        self,
        collection: str,
        filter: dict[str, Any] | None = None,
        *,
        sort: list[tuple[str, int]] | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch every document matching ``filter``. See BaseAdapter.find."""
        self._ensure_connected()  # 1. guard
        table = self._validated_table(collection)
        query = self._compiler.compile_find(  # 2. translate
            table, filter, sort=sort, limit=limit, offset=offset
        )
        try:  # 3. execute
            rows = await self._execute_read(query.sql, query.params)
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("find", collection, err)
        return [dict(row) for row in rows]  # 5. normalize + return

    async def count(self, collection: str, filter: dict[str, Any] | None = None) -> int:
        """Count documents matching ``filter``. See BaseAdapter.count."""
        self._ensure_connected()  # 1. guard
        table = self._validated_table(collection)
        query = self._compiler.compile_count(table, filter)  # 2. translate
        try:  # 3. execute
            row = await self._execute_fetchone(query.sql, query.params)
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("count", collection, err)
        return int(row[0]) if row is not None else 0  # 5. normalize + return

    async def exists(self, collection: str, filter: dict[str, Any]) -> bool:
        """Existence check via ``SELECT 1 … LIMIT 1``. See BaseAdapter.exists."""
        self._ensure_connected()  # 1. guard
        table = self._validated_table(collection)
        query = self._compiler.compile_exists(table, filter)  # 2. translate
        try:  # 3. execute
            row = await self._execute_fetchone(query.sql, query.params)
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("exists", collection, err)
        return row is not None  # 5. normalize + return

    async def aggregate(
        self, collection: str, pipeline: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Run a restricted aggregation pipeline. See BaseAdapter.aggregate."""
        self._ensure_connected()  # 1. guard
        table = self._validated_table(collection)
        plan = self._compiler.compile_aggregate(table, pipeline)  # 2. translate
        try:  # 3. execute
            rows = await self._execute_read(plan.sql, plan.params)
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("aggregate", collection, err)
        # Reuse the same row shaping logic from SqlAdapter
        from polydb.adapters.sql.base import SqlAdapter
        return SqlAdapter._shape_aggregate_rows(plan, rows)  # 5. normalize + return

    # -- 1.4 Update ----------------------------------------------------------------

    async def update_one(
        self, collection: str, filter: dict[str, Any], update: dict[str, Any]
    ) -> UpdateResult:
        """Update the first document matching ``filter``. See BaseAdapter.update_one."""
        self._ensure_connected()  # 1. guard
        table = self._validated_table(collection)
        where_sql, where_params = self._compiler.compile_where(filter)  # 2. translate
        set_sql, set_params = self._compiler.compile_update_set(update)

        try:  # 3. execute
            # Target exactly the first matching row using ctid
            select_sql = f"SELECT ctid FROM {table}{where_sql} LIMIT 1"
            matched_row = await self._execute_fetchone(select_sql, where_params)
            if matched_row is None:
                return UpdateResult(matched_count=0, modified_count=0)
            modified_count = 0
            if set_sql:
                self._compiler._reset_params()
                update_sql = f"UPDATE {table}{set_sql} WHERE ctid = {self._compiler._ph()}"
                params = [*set_params, matched_row["ctid"]]
                await self._execute_write(update_sql, params)
                modified_count = 1
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("update_one", collection, err)
        return UpdateResult(  # 5. result type
            matched_count=1, modified_count=modified_count
        )

    async def update_many(
        self, collection: str, filter: dict[str, Any], update: dict[str, Any]
    ) -> UpdateResult:
        """Update every document matching ``filter``. See BaseAdapter.update_many."""
        self._ensure_connected()  # 1. guard
        table = self._validated_table(collection)
        where_sql, where_params = self._compiler.compile_where(filter)  # 2. translate
        set_sql, set_params = self._compiler.compile_update_set(update)

        try:  # 3. execute
            if not set_sql:
                count_sql = f"SELECT COUNT(*) FROM {table}{where_sql}"
                row = await self._execute_fetchone(count_sql, where_params)
                matched = int(row[0]) if row else 0
                return UpdateResult(matched_count=matched, modified_count=0)
            update_sql = f"UPDATE {table}{set_sql}{where_sql}"
            params = [*set_params, *where_params]
            result = await self._execute_write(update_sql, params)
            modified = int(result.split()[-1]) if result else 0
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("update_many", collection, err)
        return UpdateResult(  # 5. result type
            matched_count=modified, modified_count=modified
        )

    async def replace_one(
        self, collection: str, filter: dict[str, Any], doc: dict[str, Any]
    ) -> UpdateResult:
        """Full-document replace. See BaseAdapter.replace_one."""
        self._ensure_connected()  # 1. guard
        table = self._validated_table(collection)
        self._validated_columns(list(doc.keys()))  # fail fast on bad field names

        exists, columns, pk_columns = await self._table_layout(collection)  # 2. translate
        if not exists:
            raise PolydbQueryError(
                f"Postgres replace_one failed on collection {collection!r}: no such table"
            )
        for key in doc:
            if key in pk_columns:
                raise InvalidFilterError(
                    f"replace_one cannot change primary-key column {key!r} — "
                    f"the relational identity of the row is preserved"
                )
        assignments: list[str] = []
        params: list[Any] = []
        self._compiler._reset_params()
        for column in columns:
            if column in pk_columns:
                continue
            quoted = self._quote_ident(column)
            if column in doc:
                assignments.append(f"{quoted} = {self._compiler._ph()}")
                params.append(doc[column])
            else:
                assignments.append(f"{quoted} = NULL")
        set_sql = " SET " + ", ".join(assignments) if assignments else ""

        where_sql, where_params = self._compiler.compile_where(filter)
        try:  # 3. execute
            select_sql = f"SELECT ctid FROM {table}{where_sql} LIMIT 1"
            matched_row = await self._execute_fetchone(select_sql, where_params)
            if matched_row is None:
                return UpdateResult(matched_count=0, modified_count=0)
            modified_count = 0
            if set_sql:
                self._compiler._reset_params()
                update_sql = f"UPDATE {table}{set_sql} WHERE ctid = {self._compiler._ph()}"
                params.append(matched_row["ctid"])
                await self._execute_write(update_sql, params)
                modified_count = 1
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("replace_one", collection, err)
        return UpdateResult(  # 5. result type
            matched_count=1, modified_count=modified_count
        )

    # -- 1.5 Delete -----------------------------------------------------------------

    async def delete_one(self, collection: str, filter: dict[str, Any]) -> DeleteResult:
        """Delete the first document matching ``filter``. See BaseAdapter.delete_one."""
        self._ensure_connected()  # 1. guard
        table = self._validated_table(collection)
        where_sql, where_params = self._compiler.compile_where(filter)  # 2. translate

        try:  # 3. execute
            select_sql = f"SELECT ctid FROM {table}{where_sql} LIMIT 1"
            matched_row = await self._execute_fetchone(select_sql, where_params)
            if matched_row is None:
                return DeleteResult(deleted_count=0)
            self._compiler._reset_params()
            delete_sql = f"DELETE FROM {table} WHERE ctid = {self._compiler._ph()}"
            await self._execute_write(delete_sql, [matched_row["ctid"]])
            deleted_count = 1
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("delete_one", collection, err)
        return DeleteResult(deleted_count=deleted_count)  # 5. result type

    async def delete_many(self, collection: str, filter: dict[str, Any]) -> DeleteResult:
        """Delete every document matching ``filter``. See BaseAdapter.delete_many."""
        self._ensure_connected()  # 1. guard
        table = self._validated_table(collection)
        where_sql, where_params = self._compiler.compile_where(filter)  # 2. translate

        try:  # 3. execute
            delete_sql = f"DELETE FROM {table}{where_sql}"
            result = await self._execute_write(delete_sql, where_params)
            deleted_count = int(result.split()[-1]) if result else 0
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("delete_many", collection, err)
        return DeleteResult(deleted_count=deleted_count)  # 5. result type

    # -- 1.6 Schema / structure ------------------------------------------------------

    _FIELD_TYPE_TO_SQL: dict[FieldType, str] = {
        FieldType.STR: "TEXT",
        FieldType.INT: "INTEGER",
        FieldType.FLOAT: "REAL",
        FieldType.BOOL: "BOOLEAN",
        FieldType.DATETIME: "TIMESTAMP",
        FieldType.JSON: "JSONB",
    }

    @staticmethod
    def _default_sql_literal(value: Any) -> str:
        """Render a schema field's ``default`` as a DDL literal."""
        if isinstance(value, bool):
            return "TRUE" if value else "FALSE"
        if isinstance(value, (int, float)):
            return repr(value)
        if isinstance(value, str):
            return "'" + value.replace("'", "''") + "'"
        raise InvalidFilterError(
            f"Schema default {value!r} must be a plain str/int/float/bool scalar"
        )

    def _compile_create_table(self, name: str, schema: Schema) -> str:
        """Compile a ``Schema`` into one parameterless ``CREATE TABLE`` statement."""
        table = self._validated_table(name)
        definitions: list[str] = []
        for spec in schema.fields:
            from polydb.compilers.sql_compiler import validate_identifier
            column = validate_identifier(spec.name, "column")
            parts = [
                self._quote_ident(column),
                self._FIELD_TYPE_TO_SQL[spec.type],
            ]
            if spec.primary_key:
                parts.append("PRIMARY KEY")
            if not spec.nullable:
                parts.append("NOT NULL")
            if spec.unique and not spec.primary_key:
                parts.append("UNIQUE")
            if spec.default is not None:
                parts.append(f"DEFAULT {self._default_sql_literal(spec.default)}")
            definitions.append(" ".join(parts))
        return f"CREATE TABLE {table} ({', '.join(definitions)})"

    async def create_collection(self, name: str, schema: Schema | None = None) -> None:
        """Create a table from a structured schema. See BaseAdapter.create_collection."""
        self._ensure_connected()  # 1. guard
        if schema is None or not schema.fields:
            raise SchemaRequiredError(
                f"Postgres requires a schema to create collection {name!r}: "
                f"pass Schema(fields=[Field(...), ...]) with at least one field."
            )
        sql = self._compile_create_table(name, schema)  # 2. translate

        try:  # 3. execute
            await self._execute_write(sql, [])
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("create_collection", name, err)
        # 5. result type — None by contract

    async def drop_collection(self, name: str) -> None:
        """Drop a table if it exists. See BaseAdapter.drop_collection."""
        self._ensure_connected()  # 1. guard
        table = self._validated_table(name)

        try:  # 3. execute
            await self._execute_write(f"DROP TABLE IF EXISTS {table}", [])
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("drop_collection", name, err)
        # 5. result type — None by contract

    async def list_collections(self) -> list[str]:
        """Enumerate user tables, sorted. See BaseAdapter.list_collections."""
        self._ensure_connected()  # 1. guard
        try:  # 3. execute
            sql = """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
                ORDER BY table_name
            """
            rows = await self._execute_read(sql, [])
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("list_collections", "*", err)
        return [row["table_name"] for row in rows]  # 5. normalize + return

    def _derived_index_name(
        self, raw_table: str, columns: list[str], unique: bool
    ) -> str:
        """Deterministic index name: ``[uq_|idx_]<table>__<col1>__<col2>``."""
        prefix = "uq" if unique else "idx"
        joined = "__".join(columns)
        return f"{prefix}_{raw_table}__{joined}"

    async def create_index(
        self, collection: str, fields: list[str], *, unique: bool = False
    ) -> None:
        """Create an index over ``fields``. See BaseAdapter.create_index."""
        self._ensure_connected()  # 1. guard
        if not fields:
            raise InvalidFilterError(
                "create_index requires at least one field to index"
            )
        from polydb.compilers.sql_compiler import validate_identifier
        raw_table = validate_identifier(collection, "table")
        table = self._quote_ident(raw_table)
        columns = self._validated_columns(fields)  # 2. translate
        index_name = self._quote_ident(
            self._derived_index_name(raw_table, columns, unique)
        )
        unique_sql = "UNIQUE " if unique else ""
        column_sql = ", ".join(self._quote_ident(c) for c in columns)
        sql = (
            f"CREATE {unique_sql}INDEX IF NOT EXISTS {index_name} "
            f"ON {table} ({column_sql})"
        )

        try:  # 3. execute
            await self._execute_write(sql, [])
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("create_index", collection, err)
        # 5. result type — None by contract

    async def add_field(
        self, collection: str, field: str, type_: Any, default: Any = None
    ) -> None:
        """Add a column to the table behind ``collection``. See BaseAdapter.add_field."""
        self._ensure_connected()  # 1. guard
        table = self._validated_table(collection)
        from polydb.compilers.sql_compiler import validate_identifier
        column = self._quote_ident(validate_identifier(field, "column"))
        if not isinstance(type_, FieldType):  # 2. translate
            raise InvalidFilterError(
                f"add_field got unsupported field type {type_!r}; use a "
                f"polydb.schema.FieldType value"
            )
        parts = [column, self._FIELD_TYPE_TO_SQL[type_]]
        if default is not None:
            parts.append(f"DEFAULT {self._default_sql_literal(default)}")
        sql = f"ALTER TABLE {table} ADD COLUMN {' '.join(parts)}"

        try:  # 3. execute
            await self._execute_write(sql, [])
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("add_field", collection, err)
        # 5. result type — None by contract

    # -- 1.7 Transactions ------------------------------------------------------------

    def transaction(self) -> Transaction:
        """Open a transaction (§1.7 #25). See ``PostgresTransaction`` for semantics."""
        self._ensure_connected()
        if self._tx is not None:
            from polydb.exceptions import UnsupportedOperationError
            raise UnsupportedOperationError(
                "nested/concurrent transactions are not supported: this "
                "adapter already has an open transaction"
            )
        return PostgresTransaction(self)

    # -- 1.8 Escape hatch (raw queries) -----------------------------------------------

    @staticmethod
    def _normalize_raw_params(params: Any) -> list[Any] | dict[str, Any]:
        """Coerce ``raw()``'s untyped ``params`` into a driver-bindable shape."""
        if params is None:
            return []
        if isinstance(params, Mapping):
            return dict(params)
        if isinstance(params, (list, tuple)):
            return list(params)
        raise InvalidFilterError(
            f"raw() params must be None, a sequence of positional values, or "
            f"a dict of named parameters; got {type(params).__name__}"
        )

    async def raw(self, query: Any, params: Any = None) -> Any:
        """Execute a raw SQL statement against the driver. See BaseAdapter.raw."""
        self._ensure_connected()  # 1. guard
        if not isinstance(query, str) or not query.strip():
            raise InvalidFilterError(
                f"raw() requires a non-empty SQL string, got {query!r}"
            )
        bind_params = self._normalize_raw_params(params)  # 2. translate

        try:  # 3. execute
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(query, *bind_params)
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("raw", "*", err)
        return [dict(row) for row in rows]  # 5. normalize + return

    async def explain(self, collection: str, filter: dict[str, Any]) -> dict[str, Any]:
        """Return Postgres's plan for the compiled ``filter``. See BaseAdapter.explain."""
        self._ensure_connected()  # 1. guard
        table = self._validated_table(collection)
        query = self._compiler.compile_find(  # 2. translate
            table, filter, sort=None, limit=None, offset=None
        )
        try:  # 3. execute
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(f"EXPLAIN {query.sql}", *query.params)
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("explain", collection, err)
        return {  # 5. normalize + return
            "backend": "postgres",
            "sql": query.sql,
            "params": query.params,
            "plan": [dict(row) for row in rows],
        }