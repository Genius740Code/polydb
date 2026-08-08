from __future__ import annotations

from polydb.adapters._pending import _PendingMixin
from polydb.base import BaseAdapter


class PostgresAdapter(_PendingMixin, BaseAdapter):
    """PostgreSQL adapter (backed by ``asyncpg``).

    Factory-only for now: ``Database.from_url("postgres://...")`` returns an
    *unconnected* ``PostgresAdapter``, but ``connect()`` and every DSL method
    raise ``NotImplementedError`` until the Postgres build step lands. See the
    planning doc §6, build order step 3.
    """

    _pending_note = (
        "PostgresAdapter lands in planning doc §6 build order step 3 "
        "(asyncpg pool, Postgres-dialect SQL compiler)."
    )