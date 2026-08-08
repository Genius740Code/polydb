from __future__ import annotations

from typing import Any

from polydb.base import Transaction
from polydb.results import (
    DeleteResult,
    InsertManyResult,
    InsertResult,
    UpdateResult,
    UpsertResult,
)
from polydb.schema import Schema


class _PendingMixin:
    """Concrete stubs for an adapter class whose build step hasn't landed yet.

    ``Database.from_url`` (planning doc §1.1, issue #1) returns *unconnected*
    adapter instances for every supported scheme — even ones whose build step
    (planning doc §6) hasn't arrived. Adapter shells inherit from this mixin so
    they can be constructed and handed back by the factory while every operation
    (including ``connect()``) raises ``NotImplementedError`` with a pointer to
    the build order. Subclasses set ``_pending_note`` describing the plan.
    """

    _pending_note = ""

    def _not_built(self, name: str) -> NotImplementedError:
        return NotImplementedError(
            f"{name}() is not implemented yet. {self._pending_note}"
        )

    # -- 1.1 Connection management -------------------------------------------------

    async def connect(self) -> None:
        raise self._not_built("connect")

    async def disconnect(self) -> None:
        raise self._not_built("disconnect")

    async def ping(self) -> bool:
        raise self._not_built("ping")

    # -- 1.2 Create -------------------------------------------------------------

    async def insert_one(self, collection: str, doc: dict[str, Any]) -> InsertResult:
        raise self._not_built("insert_one")

    async def insert_many(
        self, collection: str, docs: list[dict[str, Any]]
    ) -> InsertManyResult:
        raise self._not_built("insert_many")

    async def upsert_one(
        self, collection: str, filter: dict[str, Any], doc: dict[str, Any]
    ) -> UpsertResult:
        raise self._not_built("upsert_one")

    # -- 1.3 Read -----------------------------------------------------------------

    async def find_one(
        self, collection: str, filter: dict[str, Any]
    ) -> dict[str, Any] | None:
        raise self._not_built("find_one")

    async def find(
        self,
        collection: str,
        filter: dict[str, Any] | None = None,
        *,
        sort: list[tuple[str, int]] | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        raise self._not_built("find")

    async def count(
        self, collection: str, filter: dict[str, Any] | None = None
    ) -> int:
        raise self._not_built("count")

    async def exists(self, collection: str, filter: dict[str, Any]) -> bool:
        raise self._not_built("exists")

    async def aggregate(
        self, collection: str, pipeline: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        raise self._not_built("aggregate")

    # -- 1.4 Update -----------------------------------------------------------------

    async def update_one(
        self, collection: str, filter: dict[str, Any], update: dict[str, Any]
    ) -> UpdateResult:
        raise self._not_built("update_one")

    async def update_many(
        self, collection: str, filter: dict[str, Any], update: dict[str, Any]
    ) -> UpdateResult:
        raise self._not_built("update_many")

    async def replace_one(
        self, collection: str, filter: dict[str, Any], doc: dict[str, Any]
    ) -> UpdateResult:
        raise self._not_built("replace_one")

    # -- 1.5 Delete -----------------------------------------------------------------

    async def delete_one(self, collection: str, filter: dict[str, Any]) -> DeleteResult:
        raise self._not_built("delete_one")

    async def delete_many(self, collection: str, filter: dict[str, Any]) -> DeleteResult:
        raise self._not_built("delete_many")

    # -- 1.6 Schema / structure ------------------------------------------------------

    async def create_collection(self, name: str, schema: Schema | None = None) -> None:
        raise self._not_built("create_collection")

    async def drop_collection(self, name: str) -> None:
        raise self._not_built("drop_collection")

    async def list_collections(self) -> list[str]:
        raise self._not_built("list_collections")

    async def create_index(
        self, collection: str, fields: list[str], *, unique: bool = False
    ) -> None:
        raise self._not_built("create_index")

    async def add_field(
        self, collection: str, field: str, type_: Any, default: Any = None
    ) -> None:
        raise self._not_built("add_field")

    # -- 1.7 Transactions --------------------------------------------------------------

    def transaction(self) -> Transaction:
        raise self._not_built("transaction")

    # -- 1.8 Escape hatch (raw queries) --------------------------------------------------

    async def raw(self, query: Any, params: Any = None) -> Any:
        raise self._not_built("raw")

    async def explain(self, collection: str, filter: dict[str, Any]) -> dict[str, Any]:
        raise self._not_built("explain")