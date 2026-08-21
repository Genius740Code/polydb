from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from polydb.exceptions import ConnectionNotOpenError
from polydb.results import (
    DeleteResult,
    InsertManyResult,
    InsertResult,
    UpdateResult,
    UpsertResult,
)
from polydb.schema import Schema
from polydb.url_parser import ConnectionConfig

logger = logging.getLogger("polydb.base")


class Transaction(ABC):
    """Handle yielded by ``BaseAdapter.transaction()``.

    All calls made through a ``Transaction`` object (rather than directly on the
    adapter) are atomic together. Auto-commits on clean context-manager exit,
    auto-rolls-back if an exception propagates out of the ``with`` block.
    """

    @abstractmethod
    async def commit(self) -> None:
        """Explicitly commit the transaction."""

    @abstractmethod
    async def rollback(self) -> None:
        """Explicitly roll back the transaction."""

    async def __aenter__(self) -> Transaction:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if exc_type is None:
            await self.commit()
        else:
            await self.rollback()


class BaseAdapter(ABC):
    """Abstract contract every polydb backend adapter implements.

    Concrete subclasses: ``PostgresAdapter``, ``MongoAdapter``, ``SqlAdapter``
    (SQLite + MySQL, dialect-switched). See the planning doc §1 for the full
    per-backend support matrix (✅/🟡/❌) — this class only defines *signatures*,
    not per-backend behavior differences.

    Every concrete method follows the same five-step shape: (1) guard clause via
    ``self._ensure_connected()``, (2) translate/compile the DSL, (3) execute
    against the native driver, (4) normalize the result, (5) return a polydb
    result type. See §3.5 of the planning doc.
    """

    def __init__(self, config: ConnectionConfig) -> None:
        self.config = config
        self._connected = False

    @property
    def pool_size(self) -> int:
        """Pool-size tuning knob (``?pool_size=`` in the connection string).

        Backends without a real pool (SQLite's single-writer file) accept but
        ignore it, with a logged warning at construction time.
        """
        return self.config.pool_size

    @property
    def timeout(self) -> float:
        """Connection/query timeout in seconds (``?timeout=`` in the URL)."""
        return self.config.timeout

    def _ensure_connected(self) -> None:
        """Guard clause used at the top of every method that needs an open connection.

        Raises:
            ConnectionNotOpenError: If ``connect()`` has not yet succeeded.
        """
        if not self._connected:
            raise ConnectionNotOpenError(
                f"{type(self).__name__} is not connected. Call `await connect()` "
                f"first, or use `async with Database.from_url(...) as db:`."
            )

    # -- 1.1 Connection management -------------------------------------------------

    @abstractmethod
    async def connect(self) -> None:
        """Open the underlying pool/client. Idempotent — calling twice is a no-op."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Close the pool/client, releasing sockets/file handles."""

    @abstractmethod
    async def ping(self) -> bool:
        """Cheap round-trip health check.

        Returns:
            ``True`` if the backend answered the round-trip, ``False`` if it
            did not (the failure is logged, never raised — a health check
            reports, it doesn't throw).

        Raises:
            ConnectionNotOpenError: If ``connect()`` has not yet succeeded.
        """

    async def __aenter__(self) -> BaseAdapter:
        await self.connect()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        # Always release the connection, but never mask an in-body exception
        # with a cleanup failure — a failing disconnect during error handling
        # is logged/discarded in favor of the original error.
        try:
            await self.disconnect()
        except Exception as cleanup_error:
            if exc_type is None:
                raise
            logger.warning(
                "Ignoring disconnect failure while handling an error in "
                "async with block: %s",
                cleanup_error,
            )

    # -- 1.2 Create -------------------------------------------------------------

    @abstractmethod
    async def insert_one(self, collection: str, doc: dict[str, Any]) -> InsertResult:
        """Insert a single record."""

    @abstractmethod
    async def insert_many(
        self, collection: str, docs: list[dict[str, Any]]
    ) -> InsertManyResult:
        """Bulk insert."""

    @abstractmethod
    async def upsert_one(
        self, collection: str, filter: dict[str, Any], doc: dict[str, Any]
    ) -> UpsertResult:
        """Insert-or-update by filter match."""

    # -- 1.3 Read -----------------------------------------------------------------

    @abstractmethod
    async def find_one(
        self, collection: str, filter: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Fetch the first document matching ``filter``, or ``None``."""

    @abstractmethod
    async def find(
        self,
        collection: str,
        filter: dict[str, Any] | None = None,
        *,
        sort: list[tuple[str, int]] | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch every document matching ``filter``."""

    @abstractmethod
    async def count(
        self, collection: str, filter: dict[str, Any] | None = None
    ) -> int:
        """Count documents matching ``filter``."""

    @abstractmethod
    async def exists(self, collection: str, filter: dict[str, Any]) -> bool:
        """Existence check. Short-circuits rather than counting every match."""

    @abstractmethod
    async def aggregate(
        self, collection: str, pipeline: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Grouping/aggregation. See §1.3 #14 for the supported pipeline subset."""

    # -- 1.4 Update -----------------------------------------------------------------

    @abstractmethod
    async def update_one(
        self, collection: str, filter: dict[str, Any], update: dict[str, Any]
    ) -> UpdateResult:
        """Update the first document matching ``filter``. See §2.4 for update operators."""

    @abstractmethod
    async def update_many(
        self, collection: str, filter: dict[str, Any], update: dict[str, Any]
    ) -> UpdateResult:
        """Update every document matching ``filter``."""

    @abstractmethod
    async def replace_one(
        self, collection: str, filter: dict[str, Any], doc: dict[str, Any]
    ) -> UpdateResult:
        """Replace the first document matching ``filter`` wholesale."""

    # -- 1.5 Delete -----------------------------------------------------------------

    @abstractmethod
    async def delete_one(self, collection: str, filter: dict[str, Any]) -> DeleteResult:
        """Delete the first document matching ``filter``."""

    @abstractmethod
    async def delete_many(self, collection: str, filter: dict[str, Any]) -> DeleteResult:
        """Delete every document matching ``filter``."""

    # -- 1.6 Schema / structure ------------------------------------------------------

    @abstractmethod
    async def create_collection(self, name: str, schema: Schema | None = None) -> None:
        """Create a table/collection. ``schema`` is required on relational backends."""

    @abstractmethod
    async def drop_collection(self, name: str) -> None:
        """Drop a table/collection if it exists."""

    @abstractmethod
    async def list_collections(self) -> list[str]:
        """Enumerate every table/collection."""

    @abstractmethod
    async def create_index(
        self, collection: str, fields: list[str], *, unique: bool = False
    ) -> None:
        """Create an index over ``fields``."""

    @abstractmethod
    async def add_field(
        self,
        collection: str,
        field: str,
        type_: Any,
        default: Any = None,
    ) -> None:
        """Add a column (relational) / no-op with a logged warning (Mongo)."""

    # -- 1.7 Transactions --------------------------------------------------------------

    @abstractmethod
    def transaction(self) -> Transaction:
        """Open a transaction. Use as ``async with db.transaction() as tx:``."""

    # -- 1.8 Escape hatch (raw queries) --------------------------------------------------

    @abstractmethod
    async def raw(self, query: Any, params: Any = None) -> Any:
        """Pass-through to the native driver. Opts *out* of the abstraction."""

    @abstractmethod
    async def explain(self, collection: str, filter: dict[str, Any]) -> dict[str, Any]:
        """Return the backend's native query plan for a translated filter."""
