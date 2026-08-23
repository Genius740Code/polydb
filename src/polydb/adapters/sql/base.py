from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Any, NoReturn

from polydb.adapters.sql.dialects import DIALECTS, Dialect
from polydb.base import BaseAdapter, Transaction
from polydb.compilers.sql_compiler import AggregatePlan, SqlCompiler, validate_identifier
from polydb.exceptions import (
    InvalidConnectionStringError,
    InvalidFilterError,
    PolydbQueryError,
    SchemaRequiredError,
)
from polydb.results import DeleteResult, InsertManyResult, InsertResult, UpdateResult, UpsertResult
from polydb.schema import FieldType, Schema
from polydb.url_parser import ConnectionConfig, _DEFAULT_POOL_SIZE

logger = logging.getLogger("polydb.adapters.sql")

_NOT_BUILT_YET = "{name}() is not implemented yet. See planning doc §6 build order."

_validate_identifier = validate_identifier

# Schema type -> column type for CREATE TABLE (§1.6 #20). Deliberately the
# small common set of §schema.FieldType: SQLite/MySQL both accept these names.
# DATETIME and JSON are stored as TEXT — ISO-8601 strings and serialized JSON
# documents respectively; Mongo keeps native types, so cross-backend code
# should normalize to strings before writing.
_FIELD_TYPE_TO_SQL: dict[FieldType, str] = {
    FieldType.STR: "TEXT",
    FieldType.INT: "INTEGER",
    FieldType.FLOAT: "REAL",
    FieldType.BOOL: "INTEGER",
    FieldType.DATETIME: "TEXT",
    FieldType.JSON: "TEXT",
}


def _sqlite_regexp(pattern: Any, value: Any) -> bool:
    """REGEXP user function backing the ``$regex`` translation (§2.2).

    SQLite resolves ``expr REGEXP pattern`` through this callback (registered
    at connect() time); it mirrors Mongo's ``$regex`` substring-search
    semantics via ``re.search``. NULLs never match, matching SQL comparison
    semantics for missing fields.
    """
    if pattern is None or value is None:
        return False
    return re.search(str(pattern), str(value)) is not None


class SqlAdapter(BaseAdapter):
    """SQL-generic adapter: SQLite (via ``aiosqlite``) and MySQL (via ``asyncmy``)
    behind one class, differing only by ``self.dialect``.

    Connection management (§1.1), the Create operations (§1.2), the Read
    operations (§1.3), the Update operations (§1.4), the Delete
    operations (§1.5), and all of Schema/structure (§1.6 #20–#24) are
    implemented.
    Everything else raises ``NotImplementedError`` with a pointer to the build
    order — this is intentional scope for the current step, not a bug.
    """

    def __init__(self, config: ConnectionConfig) -> None:
        super().__init__(config)
        self.dialect: Dialect = DIALECTS[config.dialect]  # type: ignore[index]
        self._compiler = SqlCompiler(self.dialect)
        self._conn: Any = None  # aiosqlite.Connection once connected

        if config.dialect == "sqlite" and not self.dialect.supports_pool and config.pool_size != _DEFAULT_POOL_SIZE:
            logger.warning(
                "pool_size=%s was requested but SQLite has no real connection pool "
                "(single-writer file) — the value is accepted but ignored.",
                config.pool_size,
            )

    # -- 1.1 Connection management -------------------------------------------------

    async def connect(self) -> None:
        """Open the underlying connection. Idempotent — calling twice is a no-op."""
        if self._connected:
            return

        if self.config.dialect == "sqlite":
            database = self.config.database
            if database is None:
                raise InvalidConnectionStringError(
                    "sqlite connection string must include a file path or ':memory:'"
                )

            import aiosqlite

            conn = await aiosqlite.connect(database, timeout=self.config.timeout)
            conn.row_factory = aiosqlite.Row
            await conn.create_function("REGEXP", 2, _sqlite_regexp)
            self._conn = conn
        else:
            raise NotImplementedError(
                "MySQL leg (asyncmy) not implemented yet. See planning doc §6, "
                "build order step 5."
            )

        self._connected = True

    async def disconnect(self) -> None:
        """Close the connection, releasing the file handle / socket.

        Idempotent — calling on a never-connected or already-disconnected
        adapter is a no-op. The connection handle is detached *before* the
        ``close()`` await runs, so a failing close can never leave the adapter
        stuck in a "connected" state.
        """
        conn, self._conn = self._conn, None
        self._connected = False
        if conn is not None:
            await conn.close()

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
            cursor = await self._conn.execute("SELECT 1")
            await cursor.close()
        except Exception as err:
            logger.warning("ping() round-trip failed on %s: %r", self.dialect.name, err)
            return False
        return True

    # -- shared write-path helpers -------------------------------------------------

    def _quote_ident(self, name: str) -> str:
        """Identifier-quote an already-validated name using the dialect's quote char."""
        q = self.dialect.identifier_quote
        return f"{q}{name}{q}"

    def _validated_table(self, collection: str) -> str:
        """Validate a collection (table) name and return it quoted for SQL."""
        return self._quote_ident(_validate_identifier(collection, "table"))

    def _validated_columns(self, names: Iterable[str]) -> list[str]:
        """Return the given column names validated, preserving order."""
        return [_validate_identifier(name, "column") for name in names]

    def _equality_where(
        self, equality: dict[str, Any]
    ) -> tuple[str, list[Any]]:
        """Compile a plain-equality filter dict into ``WHERE`` SQL + params.

        ``None`` values compile to ``IS NULL`` (plain ``= NULL`` never matches).

        Returns:
            ``(where_sql, params)`` where ``where_sql`` is ``""`` for an empty
            filter (match any row) and otherwise starts with ``" WHERE "``.
        """
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in equality.items():
            quoted = self._quote_ident(column)
            if value is None:
                clauses.append(f"{quoted} IS NULL")
            else:
                clauses.append(f"{quoted} = {self.dialect.placeholder}")
                params.append(value)
        if not clauses:
            return "", []
        return " WHERE " + " AND ".join(clauses), params

    async def _commit_write(self) -> None:
        """Commit the current implicit transaction opened by a DML statement.

        The drivers run with implicit transactions (aiosqlite's default
        ``isolation_level=""``), so every successful write must be committed
        explicitly or the data stays uncommitted until some future commit —
        including surviving only in-memory for ``:memory:`` connections.
        """
        await self._conn.commit()

    async def _rollback_and_raise(
        self, operation: str, collection: str, err: Exception
    ) -> NoReturn:
        """Roll back the open implicit transaction and re-raise as PolydbQueryError.

        Without the rollback, statements executed before the failure would stay
        pending inside the driver's transaction and silently commit as part of
        the *next* unrelated write.
        """
        try:
            await self._conn.rollback()
        except Exception as rollback_error:  # pragma: no cover - defensive
            logger.warning("rollback after failed %s also failed: %r", operation, rollback_error)
        raise PolydbQueryError(
            f"{self.dialect.name} {operation} failed on collection {collection!r}: {err}"
        ) from err

    async def _table_layout(self, collection: str) -> tuple[bool, list[str], set[str]]:
        """Introspect the table behind ``collection`` (SQLite ``PRAGMA table_info``).

        Returns:
            ``(exists, columns, pk_columns)`` — every real SQLite table has at
            least one column, so an empty column list unambiguously means the
            table does not exist. Reads only; opens no transaction.
        """
        table = self._validated_table(collection)
        cursor = await self._conn.execute(f"PRAGMA table_info({table})")
        rows = await cursor.fetchall()
        columns = [row[1] for row in rows]
        pk_columns = {row[1] for row in rows if row[5]}
        return bool(columns), columns, pk_columns

    # -- 1.2 Create ----------------------------------------------------------------

    async def insert_one(self, collection: str, doc: dict[str, Any]) -> InsertResult:
        """Insert a single record. See BaseAdapter.insert_one."""
        self._ensure_connected()  # 1. guard
        table = self._validated_table(collection)
        columns = self._validated_columns(doc)  # 2. translate
        if columns:
            column_sql = ", ".join(self._quote_ident(c) for c in columns)
            placeholder_sql = ", ".join([self.dialect.placeholder] * len(columns))
            sql = f"INSERT INTO {table} ({column_sql}) VALUES ({placeholder_sql})"
            params: list[Any] = [doc[column] for column in columns]
        else:
            sql = f"INSERT INTO {table} DEFAULT VALUES"
            params = []

        try:  # 3. execute
            cursor = await self._conn.execute(sql, params)
            inserted_id = cursor.lastrowid
            await self._commit_write()
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("insert_one", collection, err)
        return InsertResult(inserted_id=inserted_id)  # 5. result type

    async def insert_many(
        self, collection: str, docs: list[dict[str, Any]]
    ) -> InsertManyResult:
        """Bulk insert. See BaseAdapter.insert_many.

        Documents may be heterogeneous: the column set is the sorted union of
        all documents' keys and documents missing a column insert ``NULL``
        there. Rows are inserted one at a time so each row's generated id can
        be reported honestly (``executemany`` exposes no per-row ids); the
        batch shares one commit, so it is all-or-nothing on failure.
        """
        self._ensure_connected()  # 1. guard
        if not docs:
            return InsertManyResult(inserted_ids=[], inserted_count=0)

        table = self._validated_table(collection)
        columns = sorted({key for doc in docs for key in doc})
        validated = self._validated_columns(columns)
        column_sql = ", ".join(self._quote_ident(c) for c in validated)
        placeholder_sql = ", ".join([self.dialect.placeholder] * len(validated))
        sql = f"INSERT INTO {table} ({column_sql}) VALUES ({placeholder_sql})"

        try:  # 3. execute
            inserted_ids: list[Any] = []
            for doc in docs:
                params = [doc.get(column) for column in columns]
                cursor = await self._conn.execute(sql, params)
                inserted_ids.append(cursor.lastrowid)
            await self._commit_write()
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("insert_many", collection, err)
        return InsertManyResult(  # 5. result type
            inserted_ids=inserted_ids, inserted_count=len(inserted_ids)
        )

    async def upsert_one(
        self, collection: str, filter: dict[str, Any], doc: dict[str, Any]
    ) -> UpsertResult:
        """Insert-or-update by filter match. See BaseAdapter.upsert_one.

        Follows Mongo's ``upsert=True`` semantics rather than SQL's
        ``ON CONFLICT`` syntax: the filter is matched against existing rows,
        the *first* match gets its ``doc`` fields updated, and no match
        inserts a new row built from the filter merged under the doc (doc
        wins on key conflicts). This keeps behavior identical across backends
        for arbitrary filters without requiring unique-index knowledge.

        Result shape: update path → ``matched_count=1, modified_count=1``;
        insert path → ``matched_count=0, modified_count=0, upserted_id=<rowid>``.
        A match whose ``doc`` is empty reports ``modified_count=0``.

        Raises:
            InvalidFilterError: If any filter value is not a plain scalar
                (operator dicts / lists cannot become column values).
        """
        self._ensure_connected()  # 1. guard
        table = self._validated_table(collection)
        equality: dict[str, Any] = {}
        for key, value in filter.items():
            column = _validate_identifier(key, "column")
            if isinstance(value, (dict, list)):
                raise InvalidFilterError(
                    f"upsert_one filter values must be plain scalars; "
                    f"got {type(value).__name__} for {key!r}"
                )
            equality[column] = value
        where_sql, where_params = self._equality_where(equality)  # 2. translate

        update_columns = self._validated_columns(doc)

        try:  # 3. execute
            # Target exactly the first matching row (Mongo update_one/upsert
            # semantics), not every match: resolve its rowid first, then
            # update by that handle. (MySQL lacks ``rowid``; its build step
            # wires a primary-key-based equivalent.)
            cursor = await self._conn.execute(
                f"SELECT rowid FROM {table}{where_sql} LIMIT 1", where_params
            )
            matched_row = await cursor.fetchone()

            if matched_row is not None:
                modified_count = 0
                if update_columns:
                    set_sql = ", ".join(
                        f"{self._quote_ident(column)} = {self.dialect.placeholder}"
                        for column in update_columns
                    )
                    await self._conn.execute(
                        f"UPDATE {table} SET {set_sql} "
                        f"WHERE rowid = {self.dialect.placeholder}",
                        [*doc.values(), matched_row[0]],
                    )
                    modified_count = 1
                await self._commit_write()
                return UpsertResult(matched_count=1, modified_count=modified_count)

            merged: dict[str, Any] = {**equality, **doc}
            merged_columns = self._validated_columns(merged)
            if merged_columns:
                column_sql = ", ".join(self._quote_ident(c) for c in merged_columns)
                placeholder_sql = ", ".join(
                    [self.dialect.placeholder] * len(merged_columns)
                )
                upsert_sql = (
                    f"INSERT INTO {table} ({column_sql}) "
                    f"VALUES ({placeholder_sql})"
                )
                params = [merged[column] for column in merged_columns]
            else:
                upsert_sql = f"INSERT INTO {table} DEFAULT VALUES"
                params = []
            insert_cursor = await self._conn.execute(upsert_sql, params)
            upserted_id = insert_cursor.lastrowid
            await self._commit_write()
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
            cursor = await self._conn.execute(query.sql, query.params)
            row = await cursor.fetchone()
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
        """Fetch every document matching ``filter``. See BaseAdapter.find_one.

        ``sort`` is a list of ``(field, direction)`` pairs with direction ``1``
        (asc) or ``-1`` (desc); later pairs break ties of earlier ones.
        """
        self._ensure_connected()  # 1. guard
        table = self._validated_table(collection)
        query = self._compiler.compile_find(  # 2. translate
            table, filter, sort=sort, limit=limit, offset=offset
        )
        try:  # 3. execute
            cursor = await self._conn.execute(query.sql, query.params)
            rows = await cursor.fetchall()
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("find", collection, err)
        return [dict(row) for row in rows]  # 5. normalize + return

    async def count(self, collection: str, filter: dict[str, Any] | None = None) -> int:
        """Count documents matching ``filter``. See BaseAdapter.count."""
        self._ensure_connected()  # 1. guard
        table = self._validated_table(collection)
        query = self._compiler.compile_count(table, filter)  # 2. translate
        try:  # 3. execute
            cursor = await self._conn.execute(query.sql, query.params)
            row = await cursor.fetchone()
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("count", collection, err)
        return int(row[0]) if row is not None else 0  # 5. normalize + return

    async def exists(self, collection: str, filter: dict[str, Any]) -> bool:
        """Existence check via ``SELECT 1 … LIMIT 1`` — short-circuits on the
        first match rather than counting. See BaseAdapter.exists."""
        self._ensure_connected()  # 1. guard
        table = self._validated_table(collection)
        query = self._compiler.compile_exists(table, filter)  # 2. translate
        try:  # 3. execute
            cursor = await self._conn.execute(query.sql, query.params)
            row = await cursor.fetchone()
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("exists", collection, err)
        return row is not None  # 5. normalize + return

    async def aggregate(
        self, collection: str, pipeline: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Run a restricted aggregation pipeline. See BaseAdapter.aggregate.

        Supported subset (§1.3 #14): ``$match`` (repeatable), then at most one
        each of ``$group``, ``$sort``, ``$limit``, ``$count`` in that order;
        accumulators ``$sum``/``$avg``/``$min``/``$max``/``$count``. Anything
        beyond that raises ``UnsupportedOperationError``.

        Group results are Mongo-shaped: single-key groups yield ``_id`` as a
        scalar, composite ``{"city": "$city"}`` groups nest under ``_id`` as a
        dict, and ``_id: null`` groups report ``"_id": None``.
        """
        self._ensure_connected()  # 1. guard
        table = self._validated_table(collection)
        plan: AggregatePlan = self._compiler.compile_aggregate(table, pipeline)  # 2. translate
        try:  # 3. execute
            cursor = await self._conn.execute(plan.sql, plan.params)
            rows = await cursor.fetchall()
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("aggregate", collection, err)
        return self._shape_aggregate_rows(plan, rows)  # 5. normalize + return

    @staticmethod
    def _shape_aggregate_rows(
        plan: AggregatePlan, rows: list[Any]
    ) -> list[dict[str, Any]]:
        """Reshape compiled-aggregate rows into Mongo-shaped documents.

        ``"_id.<part>"`` aliases fold back into a nested ``_id`` dict; plain
        ungrouped pipelines pass through as raw documents; ``$count`` output is
        a single-key doc per row.
        """
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

    # -- 1.4 Update ----------------------------------------------------------------

    async def update_one(
        self, collection: str, filter: dict[str, Any], update: dict[str, Any]
    ) -> UpdateResult:
        """Update the first document matching ``filter``. See BaseAdapter.update_one.

        ``update`` uses the §2.4 operator subset (``$set``/``$inc``/``$unset``;
        ``$push`` raises ``UnsupportedOperationError``). Targets exactly the
        first matching row (Mongo ``update_one`` semantics): the row's
        ``rowid`` is resolved first, then the UPDATE runs against that single
        handle. An update whose SET clause compiles empty (e.g. ``{}``) still
        reports an honest ``matched_count``, but ``modified_count=0``.

        Raises:
            InvalidFilterError: Malformed filter or update dict.
            UnsupportedOperationError: ``$push`` in the update.
        """
        self._ensure_connected()  # 1. guard
        table = self._validated_table(collection)
        where_sql, where_params = self._compiler.compile_where(filter)  # 2. translate
        set_sql, set_params = self._compiler.compile_update_set(update)

        try:  # 3. execute
            cursor = await self._conn.execute(
                f"SELECT rowid FROM {table}{where_sql} LIMIT 1", where_params
            )
            matched_row = await cursor.fetchone()
            if matched_row is None:
                return UpdateResult(matched_count=0, modified_count=0)
            modified_count = 0
            if set_sql:
                await self._conn.execute(
                    f"UPDATE {table}{set_sql} "
                    f"WHERE rowid = {self.dialect.placeholder}",
                    [*set_params, matched_row[0]],
                )
                modified_count = 1
            await self._commit_write()
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("update_one", collection, err)
        return UpdateResult(  # 5. result type
            matched_count=1, modified_count=modified_count
        )

    async def update_many(
        self, collection: str, filter: dict[str, Any], update: dict[str, Any]
    ) -> UpdateResult:
        """Update every document matching ``filter``. See BaseAdapter.update_many.

        One parameterized ``UPDATE … WHERE …`` statement. Both counts come from
        the statement's rowcount — SQL counts every row the UPDATE ran against,
        including rows whose values were already equal (a documented divergence
        from Mongo, which reports only genuinely-changed documents). With an
        empty SET clause the method still counts matches so ``matched_count``
        stays honest while ``modified_count=0``.

        Raises:
            InvalidFilterError: Malformed filter or update dict.
            UnsupportedOperationError: ``$push`` in the update.
        """
        self._ensure_connected()  # 1. guard
        table = self._validated_table(collection)
        where_sql, where_params = self._compiler.compile_where(filter)  # 2. translate
        set_sql, set_params = self._compiler.compile_update_set(update)

        try:  # 3. execute
            if not set_sql:
                cursor = await self._conn.execute(
                    f"SELECT COUNT(*) FROM {table}{where_sql}", where_params
                )
                matched = int((await cursor.fetchone())[0])
                return UpdateResult(matched_count=matched, modified_count=0)
            cursor = await self._conn.execute(
                f"UPDATE {table}{set_sql}{where_sql}",
                [*set_params, *where_params],
            )
            modified = max(cursor.rowcount, 0)  # rowcount is -1 when undeterminable
            await self._commit_write()
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("update_many", collection, err)
        return UpdateResult(  # 5. result type
            matched_count=modified, modified_count=modified
        )

    async def replace_one(
        self, collection: str, filter: dict[str, Any], doc: dict[str, Any]
    ) -> UpdateResult:
        """Full-document replace. See BaseAdapter.replace_one.

        Mongo replace semantics mapped onto a relational row: every column of
        the first matching row outside the primary key is rewritten — fields
        present in ``doc`` take their values, absent ones become ``NULL``.
        Primary-key columns are the relational identity handle and are always
        preserved; passing one in ``doc`` raises ``InvalidFilterError``. No
        match → no write (``replace_one`` never upserts; see ``upsert_one``).

        Raises:
            InvalidFilterError: Malformed filter or a primary-key column in doc.
            PolydbQueryError: The collection names no existing table, or the
                driver rejected a column/value (wrapped driver error).
        """
        self._ensure_connected()  # 1. guard
        table = self._validated_table(collection)
        self._validated_columns(doc)  # fail fast on bad field names

        exists, columns, pk_columns = await self._table_layout(collection)  # 2. translate
        if not exists:
            raise PolydbQueryError(
                f"{self.dialect.name} replace_one failed on collection "
                f"{collection!r}: no such table"
            )
        for key in doc:
            if key in pk_columns:
                raise InvalidFilterError(
                    f"replace_one cannot change primary-key column {key!r} — "
                    f"the relational identity of the row is preserved"
                )
        assignments: list[str] = []
        params: list[Any] = []
        for column in columns:
            if column in pk_columns:
                continue
            quoted = self._quote_ident(column)
            if column in doc:
                assignments.append(f"{quoted} = {self.dialect.placeholder}")
                params.append(doc[column])
            else:
                assignments.append(f"{quoted} = NULL")
        set_sql = " SET " + ", ".join(assignments) if assignments else ""

        where_sql, where_params = self._compiler.compile_where(filter)
        try:  # 3. execute
            cursor = await self._conn.execute(
                f"SELECT rowid FROM {table}{where_sql} LIMIT 1", where_params
            )
            matched_row = await cursor.fetchone()
            if matched_row is None:
                return UpdateResult(matched_count=0, modified_count=0)
            modified_count = 0
            if set_sql:
                await self._conn.execute(
                    f"UPDATE {table}{set_sql} "
                    f"WHERE rowid = {self.dialect.placeholder}",
                    [*params, matched_row[0]],
                )
                modified_count = 1
            await self._commit_write()
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("replace_one", collection, err)
        return UpdateResult(  # 5. result type
            matched_count=1, modified_count=modified_count
        )

    # -- 1.5 Delete -----------------------------------------------------------------

    async def delete_one(self, collection: str, filter: dict[str, Any]) -> DeleteResult:
        """Delete the first document matching ``filter``. See BaseAdapter.delete_one.

        Targets exactly the first matching row (Mongo ``delete_one``
        semantics): the row's ``rowid`` is resolved first, then the DELETE
        runs against that single handle, so co-filters that also match other
        rows leave them untouched. No match → no write,
        ``deleted_count=0``.

        Raises:
            InvalidFilterError: Malformed filter.
            PolydbQueryError: The driver rejected the statement (wrapped).
        """
        self._ensure_connected()  # 1. guard
        table = self._validated_table(collection)
        where_sql, where_params = self._compiler.compile_where(filter)  # 2. translate

        try:  # 3. execute
            cursor = await self._conn.execute(
                f"SELECT rowid FROM {table}{where_sql} LIMIT 1", where_params
            )
            matched_row = await cursor.fetchone()
            if matched_row is None:
                return DeleteResult(deleted_count=0)
            await self._conn.execute(
                f"DELETE FROM {table} "
                f"WHERE rowid = {self.dialect.placeholder}",
                [matched_row[0]],
            )
            deleted_count = 1
            await self._commit_write()
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("delete_one", collection, err)
        return DeleteResult(deleted_count=deleted_count)  # 5. result type

    async def delete_many(self, collection: str, filter: dict[str, Any]) -> DeleteResult:
        """Delete every document matching ``filter``. See BaseAdapter.delete_many.

        One parameterized ``DELETE … WHERE …`` statement; an empty filter
        compiles to a bare ``DELETE FROM`` and clears the whole table, the
        same way Mongo's ``delete_many({})`` empties a collection.
        ``deleted_count`` comes from the statement's rowcount.

        Raises:
            InvalidFilterError: Malformed filter.
            PolydbQueryError: The driver rejected the statement (wrapped).
        """
        self._ensure_connected()  # 1. guard
        table = self._validated_table(collection)
        where_sql, where_params = self._compiler.compile_where(filter)  # 2. translate

        try:  # 3. execute
            cursor = await self._conn.execute(
                f"DELETE FROM {table}{where_sql}", where_params
            )
            deleted_count = max(cursor.rowcount, 0)  # rowcount is -1 when undeterminable
            await self._commit_write()
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("delete_many", collection, err)
        return DeleteResult(deleted_count=deleted_count)  # 5. result type

    # -- 1.6 Schema / structure (collection lifecycle: #20–#22) ----------------------

    @staticmethod
    def _default_sql_literal(value: Any) -> str:
        """Render a schema field's ``default`` as a DDL literal.

        Booleans become ``1``/``0``; strings are single-quote-escaped. Only
        plain scalars are accepted — anything else would smuggle non-portable
        expressions into the DDL.

        Raises:
            InvalidFilterError: If the default is not ``str``/``int``/``float``/``bool``.
        """
        if isinstance(value, bool):
            return "1" if value else "0"
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
            column = _validate_identifier(spec.name, "column")
            parts = [
                self._quote_ident(column),
                _FIELD_TYPE_TO_SQL[spec.type],
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
        """Create a table from a structured schema. See BaseAdapter.create_collection.

        Relational backends require the schema — ``schema=None`` (or a schema
        with zero fields, which cannot define a table) raises
        ``SchemaRequiredError``. Field names are validated like any other DSL
        identifier; field types map per ``_FIELD_TYPE_TO_SQL`` (``datetime``
        and ``json`` columns are TEXT holding ISO-8601 / serialized JSON).

        Creating an already-existing name fails with ``PolydbQueryError`` from
        the driver (SQL has no silent ``IF NOT EXISTS`` here — dropping first
        is ``drop_collection()``'s explicit job).

        Raises:
            ConnectionNotOpenError: If ``connect()`` has not yet succeeded.
            SchemaRequiredError: ``schema`` missing or defining no fields.
            InvalidFilterError: Bad table/column name or non-scalar default.
            PolydbQueryError: The driver rejected the DDL (wrapped).
        """
        self._ensure_connected()  # 1. guard
        if schema is None or not schema.fields:
            raise SchemaRequiredError(
                f"{self.dialect.name} requires a schema to create collection "
                f"{name!r}: pass Schema(fields=[Field(...), ...]) with at least "
                f"one field."
            )
        sql = self._compile_create_table(name, schema)  # 2. translate

        try:  # 3. execute
            await self._conn.execute(sql)
            await self._commit_write()
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("create_collection", name, err)
        # 5. result type — None by contract

    async def drop_collection(self, name: str) -> None:
        """Drop a table if it exists. See BaseAdapter.drop_collection.

        Idempotent by contract (``DROP TABLE IF EXISTS``): dropping an absent
        name succeeds silently, so cleanup paths never need existence checks.

        Raises:
            ConnectionNotOpenError: If ``connect()`` has not yet succeeded.
            InvalidFilterError: Bad collection name.
            PolydbQueryError: The driver rejected the DDL (wrapped).
        """
        self._ensure_connected()  # 1. guard
        table = self._validated_table(name)

        try:  # 3. execute
            await self._conn.execute(f"DROP TABLE IF EXISTS {table}")
            await self._commit_write()
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("drop_collection", name, err)
        # 5. result type — None by contract

    async def list_collections(self) -> list[str]:
        """Enumerate user tables, sorted. See BaseAdapter.list_collections.

        Excludes SQLite's internal ``sqlite_*`` catalogs; views are not
        collections and never appear. (The MySQL leg will swap this query for
        an ``information_schema.tables`` lookup at its build step.)

        Returns:
            Plain (unquoted) table names in ascending order.

        Raises:
            ConnectionNotOpenError: If ``connect()`` has not yet succeeded.
        """
        self._ensure_connected()  # 1. guard
        try:  # 3. execute
            cursor = await self._conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' "
                "ORDER BY name"
            )
            rows = await cursor.fetchall()
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("list_collections", "*", err)
        return [row[0] for row in rows]  # 5. normalize + return

    # -- 1.6 Schema / structure (#23–#24) --------------------------------------------

    def _derived_index_name(
        self, raw_table: str, columns: list[str], unique: bool
    ) -> str:
        """Deterministic index name: ``[uq_|idx_]<table>__<col1>__<col2>``.

        The name embeds the table so indexes on different tables never collide,
        and the column list so re-issuing the same ``create_index`` call maps
        to the same object — which is what makes ``IF NOT EXISTS`` idempotence
        meaningful.
        """
        prefix = "uq" if unique else "idx"
        joined = "__".join(columns)
        return f"{prefix}_{raw_table}__{joined}"

    async def create_index(
        self, collection: str, fields: list[str], *, unique: bool = False
    ) -> None:
        """Create an index over ``fields``. See BaseAdapter.create_index.

        Compiles to one ``CREATE [UNIQUE] INDEX`` statement. The index name is
        derived deterministically from the table and field list
        (``idx_users__age__score``, ``uq_…`` for ``unique=True``), and creation
        is ``IF NOT EXISTS``-idempotent — re-creating an identical index is a
        no-op, matching Mongo's ``createIndex``. Caveat of the derived name:
        a *different* spec that hashes to the same name is silently ignored by
        ``IF NOT EXISTS`` rather than raising.

        An empty ``fields`` list cannot define an index and raises
        ``InvalidFilterError``. Indexing a column the table lacks surfaces as
        ``PolydbQueryError`` from the driver ("no such column").

        Raises:
            ConnectionNotOpenError: If ``connect()`` has not yet succeeded.
            InvalidFilterError: Bad collection/field name or empty ``fields``.
            PolydbQueryError: The driver rejected the DDL (wrapped).
        """
        self._ensure_connected()  # 1. guard
        if not fields:
            raise InvalidFilterError(
                "create_index requires at least one field to index"
            )
        raw_table = _validate_identifier(collection, "table")
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
            await self._conn.execute(sql)
            await self._commit_write()
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("create_index", collection, err)
        # 5. result type — None by contract

    async def add_field(
        self, collection: str, field: str, type_: Any, default: Any = None
    ) -> None:
        """Add a column to the table behind ``collection``. See BaseAdapter.add_field.

        Compiles to ``ALTER TABLE … ADD COLUMN`` with the same type mapping as
        ``create_collection`` (§1.6 #20). New columns are always nullable; a
        non-``None`` scalar ``default`` becomes a ``DEFAULT`` clause that also
        backfills existing rows. SQLite forbids adding ``PRIMARY KEY`` /
        ``UNIQUE`` columns via ALTER TABLE, so those constraints are not part
        of this method's contract.

        Adding an already-existing column or targeting an absent table
        surfaces as ``PolydbQueryError`` from the driver.

        Raises:
            ConnectionNotOpenError: If ``connect()`` has not yet succeeded.
            InvalidFilterError: Bad collection/column name, non-scalar default,
                or a ``type_`` that is not a ``FieldType``.
            PolydbQueryError: The driver rejected the DDL (wrapped).
        """
        self._ensure_connected()  # 1. guard
        table = self._validated_table(collection)
        column = self._quote_ident(_validate_identifier(field, "column"))
        if not isinstance(type_, FieldType):  # 2. translate
            raise InvalidFilterError(
                f"add_field got unsupported field type {type_!r}; use a "
                f"polydb.schema.FieldType value"
            )
        parts = [column, _FIELD_TYPE_TO_SQL[type_]]
        if default is not None:
            parts.append(f"DEFAULT {self._default_sql_literal(default)}")
        sql = f"ALTER TABLE {table} ADD COLUMN {' '.join(parts)}"

        try:  # 3. execute
            await self._conn.execute(sql)
            await self._commit_write()
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("add_field", collection, err)
        # 5. result type — None by contract

    # -- everything below: not built yet (see class docstring) --------------------

    def transaction(self) -> Transaction:
        raise NotImplementedError(_NOT_BUILT_YET.format(name="transaction"))

    async def raw(self, query: Any, params: Any = None) -> Any:
        raise NotImplementedError(_NOT_BUILT_YET.format(name="raw"))

    async def explain(self, collection: str, filter: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError(_NOT_BUILT_YET.format(name="explain"))
