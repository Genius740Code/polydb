from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import Any, NoReturn

from polydb.adapters.sql.dialects import PostgresDialect
from polydb.base import BaseAdapter, Transaction
from polydb.compilers.sql_compiler import (
    AggregatePlan,
    SqlCompiler,
    number_placeholders,
    validate_identifier,
)
from polydb.exceptions import (
    ConnectionNotOpenError,
    InvalidFilterError,
    PolydbQueryError,
    SchemaRequiredError,
    TransactionInactiveError,
    UnsupportedOperationError,
)
from polydb.results import (
    DeleteResult,
    InsertManyResult,
    InsertResult,
    UpdateResult,
    UpsertResult,
)
from polydb.schema import FieldType, Schema

logger = logging.getLogger("polydb.adapters.postgres")

_validate_identifier = validate_identifier

_FIELD_TYPE_TO_SQL_PG: dict[FieldType, str] = {
    FieldType.STR: "TEXT",
    FieldType.INT: "INTEGER",
    FieldType.FLOAT: "DOUBLE PRECISION",
    FieldType.BOOL: "BOOLEAN",
    FieldType.DATETIME: "TEXT",
    FieldType.JSON: "TEXT",
}


class PostgresTransaction(Transaction):
    """Postgres ``Transaction`` (planning doc §1.7 #25–#26).

    Backed by an ``asyncpg`` connection + ``Transaction`` object acquired
    from the adapter's pool. Entering the context manager does
    ``conn.transaction()`` / ``start()``; while it is open every operation
    routed through this object — and any call made directly on the adapter,
    which shares the same connection — executes *inside* the block.

    A failed statement inside the transaction aborts the whole thing: the
    transaction is rolled back immediately and the handle is marked
    ``aborted``, so subsequent calls through it raise
    ``TransactionInactiveError`` rather than silently continuing.

    State machine: ``new → active → committed | rolled_back | aborted``.
    """

    def __init__(self, adapter: PostgresAdapter) -> None:
        self._adapter = adapter
        self._state = "new"
        self._conn: Any = None  # asyncpg.Connection once entered
        self._trx: Any = None  # asyncpg.Transaction

    async def __aenter__(self) -> Transaction:
        if self._state != "new":
            raise TransactionInactiveError(
                f"this Transaction was already used (state: {self._state}); "
                f"call db.transaction() again for a fresh one"
            )
        if self._adapter._tx is not None:
            raise UnsupportedOperationError(
                "nested/concurrent transactions are not supported: this "
                "adapter already has an open transaction"
            )
        if self._adapter._pool is None:
            raise ConnectionNotOpenError("PostgresAdapter is not connected")
        conn = await self._adapter._pool.acquire()
        trx = conn.transaction()
        try:
            await trx.start()
        except Exception as err:  # pragma: no cover - defensive
            await self._adapter._pool.release(conn)
            raise PolydbQueryError(f"postgres BEGIN failed: {err}") from err
        self._conn = conn
        self._trx = trx
        self._state = "active"
        self._adapter._tx = self
        self._adapter._tx_conn = conn
        return self

    async def commit(self) -> None:
        """Explicitly commit the transaction."""
        if self._state != "active":
            raise TransactionInactiveError(
                f"commit() requires an active transaction; current state is {self._state!r}"
            )
        try:
            await self._trx.commit()
        finally:
            await self._adapter._pool.release(self._conn)
            self._finish("committed")

    async def rollback(self) -> None:
        """Explicitly roll back the transaction."""
        if self._state != "active":
            raise TransactionInactiveError(
                f"rollback() requires an active transaction; current state is {self._state!r}"
            )
        try:
            await self._trx.rollback()
        finally:
            await self._adapter._pool.release(self._conn)
            self._finish("rolled_back")

    def _finish(self, state: str) -> None:
        self._state = state
        if self._adapter._tx is self:
            self._adapter._tx = None
            self._adapter._tx_conn = None
        self._conn = None
        self._trx = None

    def abort_sync(self) -> None:
        """Mark aborted without async work (called when adapter handles rollback)."""
        self._state = "aborted"
        if self._adapter._tx is self:
            self._adapter._tx = None
            self._adapter._tx_conn = None

    async def abort(self) -> None:
        """Abort the transaction after a failed statement inside it."""
        if self._state != "active":
            return
        self._state = "aborted"
        if self._adapter._tx is self:
            self._adapter._tx = None
            self._adapter._tx_conn = None
        try:
            await self._trx.rollback()
        except Exception as rollback_error:  # pragma: no cover - defensive
            logger.warning("rollback after failed op also failed: %r", rollback_error)
        try:
            await self._adapter._pool.release(self._conn)
        except Exception:  # pragma: no cover - defensive
            pass
        self._conn = None
        self._trx = None

    def __getattr__(self, name: str) -> Any:
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
            return
        if exc_type is None:
            await self.commit()
            return
        try:
            await self.rollback()
        except Exception as cleanup_error:  # pragma: no cover - defensive
            logger.warning(
                "Ignoring rollback failure while handling an error in the "
                "transaction body: %s",
                cleanup_error,
            )


class PostgresAdapter(BaseAdapter):
    """PostgreSQL adapter (backed by ``asyncpg``).

    Uses the shared :class:`polydb.compilers.sql_compiler.SqlCompiler`
    with the Postgres dialect (``"`` quoting, ``~`` for ``$regex``,
    numbered ``$N`` placeholders via :func:`number_placeholders`).
    Each non-transactional operation acquires a connection from the pool;
    transactional operations share the ``PostgresTransaction``'s single
    connection.
    """

    def __init__(self, config: Any) -> None:
        super().__init__(config)
        self.dialect = PostgresDialect
        self._compiler = SqlCompiler(self.dialect)
        self._pool: Any = None  # asyncpg.Pool once connected
        self._tx: PostgresTransaction | None = None
        self._tx_conn: Any = None

    # -- helpers ---------------------------------------------------------------

    def _quote_ident(self, name: str) -> str:
        q = self.dialect.identifier_quote
        return f"{q}{name}{q}"

    def _validated_table(self, collection: str) -> str:
        return self._quote_ident(_validate_identifier(collection, "table"))

    def _validated_columns(self, names: Iterable[str]) -> list[str]:
        return [_validate_identifier(name, "column") for name in names]

    def _equality_where(self, equality: dict[str, Any]) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in equality.items():
            quoted = self._quote_ident(column)
            if value is None:
                clauses.append(f"{quoted} IS NULL")
            else:
                clauses.append(f"{quoted} = ?")
                params.append(value)
        if not clauses:
            return "", []
        return " WHERE " + " AND ".join(clauses), params

    def _pg(self, sql: str) -> str:
        """Number ``?`` placeholders to ``$N`` for asyncpg."""
        return number_placeholders(sql)

    async def _fetchrow(self, sql: str, params: list[Any]) -> Any:
        pg_sql = self._pg(sql)
        if self._tx is not None:
            return await self._tx._conn.fetchrow(pg_sql, *params)
        async with self._pool.acquire() as conn:
            return await conn.fetchrow(pg_sql, *params)

    async def _fetch(self, sql: str, params: list[Any]) -> list[Any]:
        pg_sql = self._pg(sql)
        if self._tx is not None:
            return await self._tx._conn.fetch(pg_sql, *params)
        async with self._pool.acquire() as conn:
            return await conn.fetch(pg_sql, *params)

    async def _execute(self, sql: str, params: list[Any] | None = None) -> str:
        pg_sql = self._pg(sql)
        params = params or []
        if self._tx is not None:
            return await self._tx._conn.execute(pg_sql, *params)
        async with self._pool.acquire() as conn:
            return await conn.execute(pg_sql, *params)

    def _rowcount_from_status(self, status: str) -> int:
        # asyncpg returns e.g. "UPDATE 2", "DELETE 3", "INSERT 0 1"
        try:
            return int(status.split()[-1])
        except Exception:
            return 0

    async def _rollback_and_raise(
        self, operation: str, collection: str, err: Exception
    ) -> NoReturn:
        if self._tx is not None:
            logger.info("Aborting open transaction after failed %s on %r", operation, collection)
            # Abort the transaction handle.
            tx = self._tx
            # Flip state first so later calls via tx raise TransactionInactiveError.
            tx._state = "aborted"
            self._tx = None
            self._tx_conn = None
            try:
                await tx._trx.rollback()
            except Exception as rollback_error:  # pragma: no cover
                logger.warning("rollback after failed %s also failed: %r", operation, rollback_error)
            try:
                await self._pool.release(tx._conn)
            except Exception:  # pragma: no cover
                pass
            tx._conn = None
            tx._trx = None
        raise PolydbQueryError(
            f"postgres {operation} failed on collection {collection!r}: {err}"
        ) from err

    async def _table_layout(self, collection: str) -> tuple[bool, list[str], set[str]]:
        """Introspect columns + PK for ``collection`` via information_schema.

        Returns:
            ``(exists, columns, pk_columns)``.
        """
        table = collection  # raw name for WHERE binding
        # Use information_schema for portability; schemaname = public by default.
        try:
            rows = await self._fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = ? ORDER BY ordinal_position",
                [table],
            )
            columns = [r["column_name"] for r in rows]
            if not columns:
                return False, [], set()
            # PK lookup.
            pk_rows = await self._fetch(
                "SELECT kcu.column_name FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu "
                "ON tc.constraint_name = kcu.constraint_name "
                "AND tc.table_schema = kcu.table_schema "
                "WHERE tc.table_schema = 'public' AND tc.table_name = ? "
                "AND tc.constraint_type = 'PRIMARY KEY'",
                [table],
            )
            pk_columns = {r["column_name"] for r in pk_rows}
            return True, columns, pk_columns
        except Exception as err:  # pragma: no cover - treat introspection failure as no table
            # If table doesn't exist, information_schema query returns empty; but
            # if we get an error (e.g. connection issue), re-raise as PolydbQueryError elsewhere.
            # For layout helper, distinguish "no such table" vs empty: caller checks exists.
            # If we failed to query, re-raise to be wrapped.
            raise err

    # -- 1.1 Connection management ---------------------------------------------

    async def connect(self) -> None:
        """Open the underlying pool. Idempotent — calling twice is a no-op."""
        if self._connected:
            return
        try:
            import asyncpg
        except ImportError as err:
            raise ImportError(
                "asyncpg is required for PostgresAdapter: pip install \"genius74o-polydb[postgres]\""
            ) from err

        # Build pool kwargs from parsed config.
        kwargs: dict[str, Any] = {}
        if self.config.host is not None:
            kwargs["host"] = self.config.host
        if self.config.port is not None:
            kwargs["port"] = self.config.port
        if self.config.user is not None:
            kwargs["user"] = self.config.user
        if self.config.password is not None:
            kwargs["password"] = self.config.password
        if self.config.database is not None:
            kwargs["database"] = self.config.database
        # asyncpg pool sizing: min_size=1, max_size=pool_size
        kwargs["min_size"] = 1
        kwargs["max_size"] = self.config.pool_size
        kwargs["command_timeout"] = self.config.timeout
        # Pass through extra options like sslmode if present (asyncpg supports sslmode via DSN
        # but also via kwargs for some; we pass them as-is and let asyncpg validate).
        for key, value in self.config.options.items():
            # Don't override explicit kwargs.
            if key not in kwargs:
                kwargs[key] = value

        try:
            self._pool = await asyncpg.create_pool(**kwargs)
        except Exception as err:
            raise PolydbQueryError(f"postgres connect failed: {err}") from err

        self._connected = True

    async def disconnect(self) -> None:
        pool, self._pool = self._pool, None
        self._connected = False
        self._tx = None
        self._tx_conn = None
        if pool is not None:
            try:
                await pool.close()
            except Exception:  # pragma: no cover - defensive
                pass

    async def ping(self) -> bool:
        self._ensure_connected()
        try:
            # cheap round-trip
            if self._tx is not None:
                await self._tx._conn.fetchval("SELECT 1")
            else:
                async with self._pool.acquire() as conn:
                    await conn.fetchval("SELECT 1")
        except Exception as err:
            logger.warning("ping() round-trip failed on postgres: %r", err)
            return False
        return True

    # -- 1.2 Create ------------------------------------------------------------

    async def insert_one(self, collection: str, doc: dict[str, Any]) -> InsertResult:
        self._ensure_connected()
        table = self._validated_table(collection)
        columns = self._validated_columns(doc)
        if columns:
            column_sql = ", ".join(self._quote_ident(c) for c in columns)
            placeholder_sql = ", ".join(["?"] * len(columns))
            sql = f"INSERT INTO {table} ({column_sql}) VALUES ({placeholder_sql}) RETURNING *"
            params: list[Any] = [doc[c] for c in columns]
        else:
            sql = f"INSERT INTO {table} DEFAULT VALUES RETURNING *"
            params = []
        try:
            row = await self._fetchrow(sql, params)
            # Determine inserted_id: prefer PK column value if known, else first column.
            inserted_id: Any = None
            if row is not None:
                # introspect PK to pick correct column; fall back to first.
                try:
                    exists, cols, pk_cols = await self._table_layout(collection)
                    if pk_cols:
                        # choose first PK col (single PK common)
                        pk = next(iter(pk_cols))
                        inserted_id = row[pk] if pk in row else list(row.values())[0]
                    else:
                        # no PK — return first value or None
                        inserted_id = list(row.values())[0] if row else None
                except Exception:
                    inserted_id = list(row.values())[0] if row else None
            # No explicit commit needed outside tx; inside tx defer.
        except Exception as err:
            await self._rollback_and_raise("insert_one", collection, err)
        return InsertResult(inserted_id=inserted_id)

    async def insert_many(
        self, collection: str, docs: list[dict[str, Any]]
    ) -> InsertManyResult:
        self._ensure_connected()
        if not docs:
            return InsertManyResult(inserted_ids=[], inserted_count=0)
        table = self._validated_table(collection)
        columns = sorted({key for doc in docs for key in doc})
        validated = self._validated_columns(columns)
        column_sql = ", ".join(self._quote_ident(c) for c in validated)
        placeholder_sql = ", ".join(["?"] * len(validated))
        sql = f"INSERT INTO {table} ({column_sql}) VALUES ({placeholder_sql}) RETURNING *"
        # Determine PK for id extraction.
        try:
            exists, cols, pk_cols = await self._table_layout(collection)
            pk = next(iter(pk_cols)) if pk_cols else None
        except Exception:
            pk = None
        try:
            inserted_ids: list[Any] = []
            # Use transaction to ensure all-or-nothing.
            if self._tx is None:
                # Acquire single connection and explicit transaction for batch.
                async with self._pool.acquire() as conn:
                    trx = conn.transaction()
                    await trx.start()
                    try:
                        for doc in docs:
                            params = [doc.get(c) for c in columns]
                            pg_sql = self._pg(sql)
                            row = await conn.fetchrow(pg_sql, *params)
                            if row is not None:
                                if pk and pk in row:
                                    inserted_ids.append(row[pk])
                                else:
                                    inserted_ids.append(list(row.values())[0] if row else None)
                            else:
                                inserted_ids.append(None)
                        await trx.commit()
                    except Exception as batch_err:
                        try:
                            await trx.rollback()
                        except Exception:
                            pass
                        raise batch_err
            else:
                for doc in docs:
                    params = [doc.get(c) for c in columns]
                    row = await self._fetchrow(sql, params)
                    if row is not None:
                        if pk and pk in row:
                            inserted_ids.append(row[pk])
                        else:
                            inserted_ids.append(list(row.values())[0] if row else None)
                    else:
                        inserted_ids.append(None)
        except Exception as err:
            await self._rollback_and_raise("insert_many", collection, err)
        return InsertManyResult(inserted_ids=inserted_ids, inserted_count=len(inserted_ids))

    async def upsert_one(
        self, collection: str, filter: dict[str, Any], doc: dict[str, Any]
    ) -> UpsertResult:
        self._ensure_connected()
        table = self._validated_table(collection)
        equality: dict[str, Any] = {}
        for key, value in filter.items():
            col = _validate_identifier(key, "column")
            if isinstance(value, (dict, list)):
                raise InvalidFilterError(
                    f"upsert_one filter values must be plain scalars; got {type(value).__name__} for {key!r}"
                )
            equality[col] = value
        where_sql, where_params = self._equality_where(equality)
        update_columns = self._validated_columns(doc)
        try:
            # Find first match via ctid.
            row = await self._fetchrow(f"SELECT ctid FROM {table}{where_sql} LIMIT 1", where_params)
            if row is not None:
                ctid = row["ctid"]
                modified_count = 0
                if update_columns:
                    set_sql = ", ".join(f"{self._quote_ident(c)} = ?" for c in update_columns)
                    await self._execute(
                        f"UPDATE {table} SET {set_sql} WHERE ctid = ?::tid", [*doc.values(), ctid]
                    )
                    modified_count = 1
                return UpsertResult(matched_count=1, modified_count=modified_count)
            # Insert path.
            merged: dict[str, Any] = {**equality, **doc}
            merged_columns = self._validated_columns(merged)
            if merged_columns:
                column_sql = ", ".join(self._quote_ident(c) for c in merged_columns)
                placeholder_sql = ", ".join(["?"] * len(merged_columns))
                upsert_sql = f"INSERT INTO {table} ({column_sql}) VALUES ({placeholder_sql}) RETURNING *"
                params = [merged[c] for c in merged_columns]
            else:
                upsert_sql = f"INSERT INTO {table} DEFAULT VALUES RETURNING *"
                params = []
            row2 = await self._fetchrow(upsert_sql, params)
            upserted_id: Any = None
            if row2 is not None:
                try:
                    exists, cols, pk_cols = await self._table_layout(collection)
                    if pk_cols:
                        pk = next(iter(pk_cols))
                        upserted_id = row2[pk] if pk in row2 else list(row2.values())[0]
                    else:
                        upserted_id = list(row2.values())[0] if row2 else None
                except Exception:
                    upserted_id = list(row2.values())[0] if row2 else None
        except Exception as err:
            await self._rollback_and_raise("upsert_one", collection, err)
        return UpsertResult(matched_count=0, modified_count=0, upserted_id=upserted_id)

    # -- 1.3 Read --------------------------------------------------------------

    async def find_one(
        self, collection: str, filter: dict[str, Any]
    ) -> dict[str, Any] | None:
        self._ensure_connected()
        table = self._validated_table(collection)
        query = self._compiler.compile_find(table, filter, sort=None, limit=1, offset=None)
        try:
            row = await self._fetchrow(query.sql, query.params)
        except Exception as err:
            await self._rollback_and_raise("find_one", collection, err)
        return dict(row) if row is not None else None

    async def find(
        self,
        collection: str,
        filter: dict[str, Any] | None = None,
        *,
        sort: list[tuple[str, int]] | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        self._ensure_connected()
        table = self._validated_table(collection)
        query = self._compiler.compile_find(table, filter, sort=sort, limit=limit, offset=offset)
        try:
            rows = await self._fetch(query.sql, query.params)
        except Exception as err:
            await self._rollback_and_raise("find", collection, err)
        return [dict(r) for r in rows]

    async def count(self, collection: str, filter: dict[str, Any] | None = None) -> int:
        self._ensure_connected()
        table = self._validated_table(collection)
        query = self._compiler.compile_count(table, filter)
        try:
            row = await self._fetchrow(query.sql, query.params)
        except Exception as err:
            await self._rollback_and_raise("count", collection, err)
        if row is None:
            return 0
        try:
            return int(row["count"])
        except Exception:
            try:
                return int(list(row.values())[0])
            except Exception:
                return 0

    async def exists(self, collection: str, filter: dict[str, Any]) -> bool:
        self._ensure_connected()
        table = self._validated_table(collection)
        query = self._compiler.compile_exists(table, filter)
        try:
            row = await self._fetchrow(query.sql, query.params)
        except Exception as err:
            await self._rollback_and_raise("exists", collection, err)
        return row is not None

    async def aggregate(
        self, collection: str, pipeline: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        self._ensure_connected()
        table = self._validated_table(collection)
        plan: AggregatePlan = self._compiler.compile_aggregate(table, pipeline)
        try:
            rows = await self._fetch(plan.sql, plan.params)
        except Exception as err:
            await self._rollback_and_raise("aggregate", collection, err)
        return self._shape_aggregate_rows(plan, rows)

    @staticmethod
    def _shape_aggregate_rows(plan: AggregatePlan, rows: list[Any]) -> list[dict[str, Any]]:
        if plan.count_field is not None:
            return [{plan.count_field: row[plan.count_field]} for row in rows]
        if not plan.grouped:
            return [dict(row) for row in rows]
        shaped: list[dict[str, Any]] = []
        for row in rows:
            doc: dict[str, Any] = {}
            id_value: Any = None
            id_parts: dict[str, Any] = {}
            for key, value in dict(row).items():
                if key == "_id":
                    id_value = value
                elif key.startswith("_id."):
                    id_parts[key[len("_id.") :]] = value
                else:
                    doc[key] = value
            shaped.append({"_id": id_parts if id_parts else id_value, **doc})
        return shaped

    # -- 1.4 Update ------------------------------------------------------------

    async def update_one(
        self, collection: str, filter: dict[str, Any], update: dict[str, Any]
    ) -> UpdateResult:
        self._ensure_connected()
        table = self._validated_table(collection)
        where_sql, where_params = self._compiler.compile_where(filter)
        set_sql, set_params = self._compiler.compile_update_set(update)
        try:
            row = await self._fetchrow(f"SELECT ctid FROM {table}{where_sql} LIMIT 1", where_params)
            if row is None:
                return UpdateResult(matched_count=0, modified_count=0)
            modified_count = 0
            if set_sql:
                ctid = row["ctid"]
                await self._execute(
                    f"UPDATE {table}{set_sql} WHERE ctid = ?::tid", [*set_params, ctid]
                )
                modified_count = 1
        except Exception as err:
            await self._rollback_and_raise("update_one", collection, err)
        return UpdateResult(matched_count=1, modified_count=modified_count)

    async def update_many(
        self, collection: str, filter: dict[str, Any], update: dict[str, Any]
    ) -> UpdateResult:
        self._ensure_connected()
        table = self._validated_table(collection)
        where_sql, where_params = self._compiler.compile_where(filter)
        set_sql, set_params = self._compiler.compile_update_set(update)
        try:
            if not set_sql:
                row = await self._fetchrow(f"SELECT COUNT(*) AS cnt FROM {table}{where_sql}", where_params)
                matched = int(row["cnt"]) if row is not None else 0
                return UpdateResult(matched_count=matched, modified_count=0)
            status = await self._execute(f"UPDATE {table}{set_sql}{where_sql}", [*set_params, *where_params])
            modified = self._rowcount_from_status(status)
        except Exception as err:
            await self._rollback_and_raise("update_many", collection, err)
        return UpdateResult(matched_count=modified, modified_count=modified)

    async def replace_one(
        self, collection: str, filter: dict[str, Any], doc: dict[str, Any]
    ) -> UpdateResult:
        self._ensure_connected()
        table = self._validated_table(collection)
        self._validated_columns(doc)
        exists, columns, pk_columns = await self._table_layout(collection)
        if not exists:
            raise PolydbQueryError(f"postgres replace_one failed on collection {collection!r}: no such table")
        for key in doc:
            if key in pk_columns:
                raise InvalidFilterError(
                    f"replace_one cannot change primary-key column {key!r} — the relational identity of the row is preserved"
                )
        assignments: list[str] = []
        params: list[Any] = []
        for column in columns:
            if column in pk_columns:
                continue
            quoted = self._quote_ident(column)
            if column in doc:
                assignments.append(f"{quoted} = ?")
                params.append(doc[column])
            else:
                assignments.append(f"{quoted} = NULL")
        set_sql = " SET " + ", ".join(assignments) if assignments else ""
        where_sql, where_params = self._compiler.compile_where(filter)
        try:
            row = await self._fetchrow(f"SELECT ctid FROM {table}{where_sql} LIMIT 1", where_params)
            if row is None:
                return UpdateResult(matched_count=0, modified_count=0)
            modified_count = 0
            if set_sql:
                ctid = row["ctid"]
                await self._execute(f"UPDATE {table}{set_sql} WHERE ctid = ?::tid", [*params, ctid])
                modified_count = 1
        except Exception as err:
            await self._rollback_and_raise("replace_one", collection, err)
        return UpdateResult(matched_count=1, modified_count=modified_count)

    # -- 1.5 Delete ------------------------------------------------------------

    async def delete_one(self, collection: str, filter: dict[str, Any]) -> DeleteResult:
        self._ensure_connected()
        table = self._validated_table(collection)
        where_sql, where_params = self._compiler.compile_where(filter)
        try:
            row = await self._fetchrow(f"SELECT ctid FROM {table}{where_sql} LIMIT 1", where_params)
            if row is None:
                return DeleteResult(deleted_count=0)
            ctid = row["ctid"]
            await self._execute(f"DELETE FROM {table} WHERE ctid = ?::tid", [ctid])
            deleted_count = 1
        except Exception as err:
            await self._rollback_and_raise("delete_one", collection, err)
        return DeleteResult(deleted_count=deleted_count)

    async def delete_many(self, collection: str, filter: dict[str, Any]) -> DeleteResult:
        self._ensure_connected()
        table = self._validated_table(collection)
        where_sql, where_params = self._compiler.compile_where(filter)
        try:
            status = await self._execute(f"DELETE FROM {table}{where_sql}", where_params)
            deleted_count = self._rowcount_from_status(status)
        except Exception as err:
            await self._rollback_and_raise("delete_many", collection, err)
        return DeleteResult(deleted_count=deleted_count)

    # -- 1.6 Schema ------------------------------------------------------------

    @staticmethod
    def _default_sql_literal(value: Any) -> str:
        if isinstance(value, bool):
            return "TRUE" if value else "FALSE"
        if isinstance(value, (int, float)):
            return repr(value)
        if isinstance(value, str):
            return "'" + value.replace("'", "''") + "'"
        raise InvalidFilterError(f"Schema default {value!r} must be a plain str/int/float/bool scalar")

    def _compile_create_table(self, name: str, schema: Schema) -> str:
        table = self._validated_table(name)
        definitions: list[str] = []
        for spec in schema.fields:
            column = _validate_identifier(spec.name, "column")
            # Postgres SERIAL for INT primary key auto-increment
            if spec.primary_key and spec.type == FieldType.INT:
                col_type = "SERIAL"
            else:
                col_type = _FIELD_TYPE_TO_SQL_PG[spec.type]
            parts = [self._quote_ident(column), col_type]
            if spec.primary_key and col_type != "SERIAL":
                parts.append("PRIMARY KEY")
            elif spec.primary_key and col_type == "SERIAL":
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
        self._ensure_connected()
        if schema is None or not schema.fields:
            raise SchemaRequiredError(
                f"postgres requires a schema to create collection {name!r}: pass Schema(fields=[Field(...), ...]) with at least one field."
            )
        sql = self._compile_create_table(name, schema)
        try:
            await self._execute(sql)
        except Exception as err:
            await self._rollback_and_raise("create_collection", name, err)

    async def drop_collection(self, name: str) -> None:
        self._ensure_connected()
        table = self._validated_table(name)
        try:
            await self._execute(f"DROP TABLE IF EXISTS {table}")
        except Exception as err:
            await self._rollback_and_raise("drop_collection", name, err)

    async def list_collections(self) -> list[str]:
        self._ensure_connected()
        try:
            rows = await self._fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename",
                [],
            )
        except Exception as err:
            await self._rollback_and_raise("list_collections", "*", err)
        return [row["tablename"] for row in rows]

    def _derived_index_name(self, raw_table: str, columns: list[str], unique: bool) -> str:
        prefix = "uq" if unique else "idx"
        joined = "__".join(columns)
        return f"{prefix}_{raw_table}__{joined}"

    async def create_index(
        self, collection: str, fields: list[str], *, unique: bool = False
    ) -> None:
        self._ensure_connected()
        if not fields:
            raise InvalidFilterError("create_index requires at least one field to index")
        raw_table = _validate_identifier(collection, "table")
        table = self._quote_ident(raw_table)
        columns = self._validated_columns(fields)
        index_name = self._quote_ident(self._derived_index_name(raw_table, columns, unique))
        unique_sql = "UNIQUE " if unique else ""
        column_sql = ", ".join(self._quote_ident(c) for c in columns)
        sql = f"CREATE {unique_sql}INDEX IF NOT EXISTS {index_name} ON {table} ({column_sql})"
        try:
            await self._execute(sql)
        except Exception as err:
            await self._rollback_and_raise("create_index", collection, err)

    async def add_field(
        self, collection: str, field: str, type_: Any, default: Any = None
    ) -> None:
        self._ensure_connected()
        table = self._validated_table(collection)
        column = self._quote_ident(_validate_identifier(field, "column"))
        if not isinstance(type_, FieldType):
            raise InvalidFilterError(
                f"add_field got unsupported field type {type_!r}; use a polydb.schema.FieldType value"
            )
        parts = [column, _FIELD_TYPE_TO_SQL_PG[type_]]
        if default is not None:
            parts.append(f"DEFAULT {self._default_sql_literal(default)}")
        sql = f"ALTER TABLE {table} ADD COLUMN {' '.join(parts)}"
        try:
            await self._execute(sql)
        except Exception as err:
            await self._rollback_and_raise("add_field", collection, err)

    # -- 1.7 Transactions ------------------------------------------------------

    def transaction(self) -> Transaction:
        self._ensure_connected()
        if self._tx is not None:
            raise UnsupportedOperationError(
                "nested/concurrent transactions are not supported: this adapter already has an open transaction"
            )
        return PostgresTransaction(self)

    # -- 1.8 Escape hatch ------------------------------------------------------

    @staticmethod
    def _normalize_raw_params(params: Any) -> list[Any]:
        if params is None:
            return []
        if isinstance(params, Mapping):
            # For raw with named params, asyncpg expects $1 style? But raw() is
            # escape hatch: user provides native SQL. For postgres, they would
            # use $N placeholders with tuple params, not :named. We'll support
            # tuple/list params primarily. Mapping is still allowed but we
            # convert to tuple via values? Better to reject mapping for postgres
            # since asyncpg doesn't support named :name params directly.
            # However keep compatibility: if mapping given, we treat as error
            # to avoid silent misbehaviour, unless it's for SQLite style.
            # For postgres, require sequence.
            raise InvalidFilterError(
                "postgres raw() params must be None or a sequence of positional values when using $N placeholders; got mapping"
            )
        if isinstance(params, (list, tuple)):
            return list(params)
        raise InvalidFilterError(
            f"raw() params must be None or a sequence of positional values, got {type(params).__name__}"
        )

    async def raw(self, query: Any, params: Any = None) -> Any:
        self._ensure_connected()
        if not isinstance(query, str) or not query.strip():
            raise InvalidFilterError(f"raw() requires a non-empty SQL string, got {query!r}")
        # For postgres, raw query is expected to use $N placeholders with positional params.
        # If query contains "?" we still support it by numbering, like the compiler path,
        # for convenience if user writes "?" style. But native asyncpg expects $N.
        # We'll auto-number "?" -> $N if present and params are positional.
        bind_params = self._normalize_raw_params(params) if not isinstance(params, dict) else params
        # Handle dict params as error for postgres — we already raised above.
        # Transform "?" to $N if query uses "?" style (convenience).
        pg_query = query
        if "?" in query and bind_params:
            # Rouhly: if query contains ?, number them; else leave as is (assume $N already)
            pg_query = number_placeholders(query)
        try:
            # Decide fetch vs execute based on params shape and query type.
            # Use fetch if query looks like SELECT.
            stripped = pg_query.strip().upper()
            if stripped.startswith("SELECT") or "RETURNING" in stripped:
                rows = await self._fetch(pg_query, bind_params if isinstance(bind_params, list) else [])
                return [dict(r) for r in rows]
            else:
                # For writes, execute and try to fetch if it returns rows (e.g. INSERT RETURNING)
                # Attempt fetch first to support RETURNING.
                if "RETURNING" in stripped:
                    rows = await self._fetch(pg_query, bind_params if isinstance(bind_params, list) else [])
                    return [dict(r) for r in rows]
                await self._execute(pg_query, bind_params if isinstance(bind_params, list) else [])
                # Plain writes without RETURNING — like SQLite's raw() return [].
                return []
        except Exception as err:
            await self._rollback_and_raise("raw", "*", err)

    async def explain(self, collection: str, filter: dict[str, Any]) -> dict[str, Any]:
        self._ensure_connected()
        table = self._validated_table(collection)
        query = self._compiler.compile_find(table, filter, sort=None, limit=None, offset=None)
        try:
            # Run EXPLAIN (FORMAT JSON) for richer plan, fallback to EXPLAIN.
            # Use JSON format if available (PG 9.0+), else plain.
            pg_sql = self._pg(f"EXPLAIN (FORMAT JSON) {query.sql}")
            try:
                if self._tx is not None:
                    row = await self._tx._conn.fetchrow(pg_sql, *query.params)
                else:
                    async with self._pool.acquire() as conn:
                        row = await conn.fetchrow(pg_sql, *query.params)
                # asyncpg returns a Record with single column "QUERY PLAN" as JSON string or object
                # When FORMAT JSON, result is a list with one JSON value in first column.
                plan_data = list(row.values())[0] if row else None
                # asyncpg may return JSON string or already parsed
                import json
                if isinstance(plan_data, str):
                    try:
                        plan_data_parsed = json.loads(plan_data)
                    except Exception:
                        plan_data_parsed = [{"Plan": plan_data}]
                else:
                    plan_data_parsed = plan_data
                # Normalize to list of dicts
                if isinstance(plan_data_parsed, list):
                    plan = plan_data_parsed
                else:
                    plan = [plan_data_parsed] if plan_data_parsed else []
            except Exception:
                # Fallback to plain EXPLAIN
                pg_sql2 = self._pg(f"EXPLAIN {query.sql}")
                rows = await self._fetch(pg_sql2, query.params)
                plan = [dict(r) for r in rows]
        except Exception as err:
            await self._rollback_and_raise("explain", collection, err)
        return {"backend": self.dialect.name, "sql": self._pg(query.sql), "params": query.params, "plan": plan}
