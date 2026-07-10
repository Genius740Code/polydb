from __future__ import annotations

from typing import Any

from polydb.base import BaseAdapter
from polydb.exceptions import InvalidConnectionStringError
from polydb.url_parser import ConnectionConfig, parse_connection_string

# Build order (planning doc §6): SQLite leg of the SQL family adapter first,
# then Postgres, then Mongo, then the MySQL leg. Entries below are added as
# each adapter actually lands — nothing is pre-registered speculatively.
_NOT_YET_BUILT: dict[str, str] = {
    "postgres": "PostgresAdapter (build order step 3 — not started)",
    "mongo": "MongoAdapter (build order step 4 — not started)",
}
_NOT_YET_BUILT_DIALECT: dict[str, str] = {
    "mysql": "SqlAdapter's MySQL leg (build order step 5 — not started)",
}


def _build_adapter(config: ConnectionConfig) -> BaseAdapter:
    if config.family == "sql":
        if config.dialect in _NOT_YET_BUILT_DIALECT:
            raise NotImplementedError(
                f"{_NOT_YET_BUILT_DIALECT[config.dialect]} for scheme "
                f"{config.scheme!r}. See planning doc §6."
            )
        from polydb.adapters.sql.base import SqlAdapter

        return SqlAdapter(config)

    if config.family in _NOT_YET_BUILT:
        raise NotImplementedError(
            f"{_NOT_YET_BUILT[config.family]} for scheme {config.scheme!r}. "
            f"See planning doc §6."
        )

    # Should be unreachable — url_parser only ever produces known families.
    raise InvalidConnectionStringError(f"No adapter registered for family {config.family!r}")


class Database:
    """Thin façade holding the concrete adapter chosen at ``from_url()``-time.

    Every public method not defined here (find_one, insert_one, transaction, ...)
    is delegated straight through to the underlying adapter via ``__getattr__``,
    so ``Database`` never has to be kept in lockstep with ``BaseAdapter``'s
    method list by hand.
    """

    def __init__(self, adapter: BaseAdapter) -> None:
        self._adapter = adapter

    @classmethod
    def from_url(cls, url: str) -> Database:
        """Parse a connection string and return the correct adapter, unconnected.

        Args:
            url: e.g. ``"postgres://user:pass@host:5432/db"``,
                ``"mongodb://host:27017/db"``, ``"sqlite:///path/to/file.db"``,
                or ``"mysql://user:pass@host:3306/db"``.

        Returns:
            A ``Database`` wrapping the resolved adapter. Call ``await db.connect()``
            (or use ``async with``) before issuing any other calls.

        Raises:
            InvalidConnectionStringError: Unparseable or unrecognized scheme.
            NotImplementedError: The scheme is recognized but that adapter
                hasn't been built yet (see planning doc §6, build order).
        """
        config = parse_connection_string(url)
        adapter = _build_adapter(config)
        return cls(adapter)

    async def connect(self) -> None:
        await self._adapter.connect()

    async def disconnect(self) -> None:
        await self._adapter.disconnect()

    async def ping(self) -> bool:
        return await self._adapter.ping()

    async def __aenter__(self) -> Database:
        await self.connect()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.disconnect()

    def __getattr__(self, name: str) -> Any:
        # Only reached for attributes not found on Database itself, so this
        # never shadows connect/disconnect/ping/from_url above.
        return getattr(self._adapter, name)

    def __repr__(self) -> str:
        state = "connected" if getattr(self._adapter, "_connected", False) else "not connected"
        return f"Database({type(self._adapter).__name__}, {state})"
