from __future__ import annotations

import logging
from typing import Any

from polydb.adapters.sql.dialects import DIALECTS, Dialect
from polydb.base import BaseAdapter, Transaction
from polydb.exceptions import InvalidConnectionStringError
from polydb.results import DeleteResult, InsertManyResult, InsertResult, UpdateResult, UpsertResult
from polydb.schema import Schema
from polydb.url_parser import ConnectionConfig, _DEFAULT_POOL_SIZE

logger = logging.getLogger("polydb.adapters.sql")

_NOT_BUILT_YET = (
    "{name}() is not implemented yet — the shared SQL compiler (sql_compiler.py) "
    "hasn't been built. See planning doc §6, build order step 2."
)


class SqlAdapter(BaseAdapter):
    """SQL-generic adapter: SQLite (via ``aiosqlite``) and MySQL (via ``asyncmy``)
    behind one class, differing only by ``self.dialect``.

    Only connection management (§1.1) is implemented so far. Everything else
    raises ``NotImplementedError`` with a pointer to the build order — this is
    intentional scope for the current step, not a bug.
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

    # -- everything below: not built yet (see class docstring) --------------------

    async def insert_one(self, collection: str, doc: dict[str, Any]) -> InsertResult:
        raise NotImplementedError(_NOT_BUILT_YET.format(name="insert_one"))

    async def insert_many(
        self, collection: str, docs: list[dict[str, Any]]
    ) -> InsertManyResult:
        raise NotImplementedError(_NOT_BUILT_YET.format(name="insert_many"))

    async def upsert_one(
        self, collection: str, filter: dict[str, Any], doc: dict[str, Any]
    ) -> UpsertResult:
        raise NotImplementedError(_NOT_BUILT_YET.format(name="upsert_one"))

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
