from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Any, NoReturn

from polydb.adapters.sql.dialects import DIALECTS, Dialect
from polydb.base import BaseAdapter, Transaction
from polydb.exceptions import InvalidConnectionStringError, InvalidFilterError, PolydbQueryError
from polydb.results import DeleteResult, InsertManyResult, InsertResult, UpdateResult, UpsertResult
from polydb.schema import Schema
from polydb.url_parser import ConnectionConfig, _DEFAULT_POOL_SIZE

logger = logging.getLogger("polydb.adapters.sql")

_NOT_BUILT_YET = (
    "{name}() is not implemented yet — the shared SQL compiler (sql_compiler.py) "
    "hasn't been built. See planning doc §6, build order step 2."
)

# §2.3: field/table names are validated against this pattern before being
# identifier-quoted into SQL — values are parameterized, names must be safe too.
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_identifier(name: str, kind: str) -> str:
    """Validate a column or table name against the DSL identifier pattern.

    Args:
        name: The identifier as supplied by application code.
        kind: ``"table"`` or ``"column"`` — used only in error messages.

    Returns:
        The validated name, unchanged.

    Raises:
        InvalidFilterError: If the name is empty, not a string, or contains
            characters outside ``[A-Za-z0-9_]``.
    """
    if not isinstance(name, str) or not _IDENTIFIER_PATTERN.match(name):
        raise InvalidFilterError(
            f"Invalid {kind} name {name!r}: must match ^[A-Za-z_][A-Za-z0-9_]*$"
        )
    return name


class SqlAdapter(BaseAdapter):
    """SQL-generic adapter: SQLite (via ``aiosqlite``) and MySQL (via ``asyncmy``)
    behind one class, differing only by ``self.dialect``.

    Connection management (§1.1) and the Create operations (§1.2) are
    implemented. Everything else raises ``NotImplementedError`` with a pointer
    to the build order — this is intentional scope for the current step, not a
    bug.
    """

    def __init__(self, config: ConnectionConfig) -> None:
        super().__init__(config)
        self.dialect: Dialect = DIALECTS[config.dialect]  # type: ignore[index]
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

    # -- everything below: not built yet (see class docstring) --------------------

    async def find_one(
        self, collection: str, filter: dict[str, Any]
    ) -> dict[str, Any] | None:
        raise NotImplementedError(_NOT_BUILT_YET.format(name="find_one"))

    async def find(
        self,
        collection: str,
        filter: dict[str, Any] | None = None,
        *,
        sort: list[tuple[str, int]] | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError(_NOT_BUILT_YET.format(name="find"))

    async def count(self, collection: str, filter: dict[str, Any] | None = None) -> int:
        raise NotImplementedError(_NOT_BUILT_YET.format(name="count"))

    async def exists(self, collection: str, filter: dict[str, Any]) -> bool:
        raise NotImplementedError(_NOT_BUILT_YET.format(name="exists"))

    async def aggregate(
        self, collection: str, pipeline: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        raise NotImplementedError(_NOT_BUILT_YET.format(name="aggregate"))

    async def update_one(
        self, collection: str, filter: dict[str, Any], update: dict[str, Any]
    ) -> UpdateResult:
        raise NotImplementedError(_NOT_BUILT_YET.format(name="update_one"))

    async def update_many(
        self, collection: str, filter: dict[str, Any], update: dict[str, Any]
    ) -> UpdateResult:
        raise NotImplementedError(_NOT_BUILT_YET.format(name="update_many"))

    async def replace_one(
        self, collection: str, filter: dict[str, Any], doc: dict[str, Any]
    ) -> UpdateResult:
        raise NotImplementedError(_NOT_BUILT_YET.format(name="replace_one"))

    async def delete_one(self, collection: str, filter: dict[str, Any]) -> DeleteResult:
        raise NotImplementedError(_NOT_BUILT_YET.format(name="delete_one"))

    async def delete_many(self, collection: str, filter: dict[str, Any]) -> DeleteResult:
        raise NotImplementedError(_NOT_BUILT_YET.format(name="delete_many"))

    async def create_collection(self, name: str, schema: Schema | None = None) -> None:
        raise NotImplementedError(_NOT_BUILT_YET.format(name="create_collection"))

    async def drop_collection(self, name: str) -> None:
        raise NotImplementedError(_NOT_BUILT_YET.format(name="drop_collection"))

    async def list_collections(self) -> list[str]:
        raise NotImplementedError(_NOT_BUILT_YET.format(name="list_collections"))

    async def create_index(
        self, collection: str, fields: list[str], *, unique: bool = False
    ) -> None:
        raise NotImplementedError(_NOT_BUILT_YET.format(name="create_index"))

    async def add_field(
        self, collection: str, field: str, type_: Any, default: Any = None
    ) -> None:
        raise NotImplementedError(_NOT_BUILT_YET.format(name="add_field"))

    def transaction(self) -> Transaction:
        raise NotImplementedError(_NOT_BUILT_YET.format(name="transaction"))

    async def raw(self, query: Any, params: Any = None) -> Any:
        raise NotImplementedError(_NOT_BUILT_YET.format(name="raw"))

    async def explain(self, collection: str, filter: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError(_NOT_BUILT_YET.format(name="explain"))
