from __future__ import annotations

import logging
from typing import Any

from polydb.base import BaseAdapter
from polydb.exceptions import InvalidConnectionStringError
from polydb.url_parser import ConnectionConfig, parse_connection_string

_cleanup_logger = logging.getLogger("polydb.database")


def _build_adapter(config: ConnectionConfig) -> BaseAdapter:
    """Resolve a parsed connection string to the correct adapter class.

    Adapters are imported lazily inside this function so importing polydb never
    pulls in a driver (asyncpg/motor/aiosqlite/asyncmy) unless you actually use
    that backend. Every supported scheme resolves to an *unconnected* instance —
    ``connect()`` is what opens the pool/client (and, for the not-yet-built
    build-order steps, currently raises ``NotImplementedError``).
    """
    if config.family == "sql":
        from polydb.adapters.sql.base import SqlAdapter

        return SqlAdapter(config)

    if config.family == "postgres":
        from polydb.adapters.postgres import PostgresAdapter

        return PostgresAdapter(config)

    if config.family == "mongo":
        from polydb.adapters.mongo import MongoAdapter

        return MongoAdapter(config)

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
            A ``Database`` wrapping the resolved adapter — always *unconnected*.
            Call ``await db.connect()`` (or use ``async with``) before issuing
            any other calls. Backends whose build step hasn't landed yet return
            their adapter instance all the same, but ``connect()`` raises
            ``NotImplementedError`` (see planning doc §6, build order).

        Raises:
            InvalidConnectionStringError: Unparseable or unrecognized scheme.
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
        # Always release the connection, but never mask an in-body exception
        # with a cleanup failure — a failing disconnect during error handling
        # is logged/discarded in favor of the original error.
        try:
            await self.disconnect()
        except Exception as cleanup_error:
            if exc_type is None:
                raise
            _cleanup_logger.warning(
                "Ignoring disconnect failure while handling an error in "
                "async with block: %s",
                cleanup_error,
            )

    def __getattr__(self, name: str) -> Any:
        # Only reached for attributes not found on Database itself, so this
        # never shadows connect/disconnect/ping/from_url above.
        return getattr(self._adapter, name)

    def __repr__(self) -> str:
        state = "connected" if getattr(self._adapter, "_connected", False) else "not connected"
        return f"Database({type(self._adapter).__name__}, {state})"
