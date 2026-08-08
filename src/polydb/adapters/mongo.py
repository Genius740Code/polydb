from __future__ import annotations

from polydb.adapters._pending import _PendingMixin
from polydb.base import BaseAdapter


class MongoAdapter(_PendingMixin, BaseAdapter):
    """MongoDB adapter (backed by ``motor``).

    Factory-only for now: ``Database.from_url("mongodb://...")`` returns an
    *unconnected* ``MongoAdapter``, but ``connect()`` and every DSL method raise
    ``NotImplementedError`` until the build step lands. See the planning doc §6,
    build order step 4.
    """

    _pending_note = (
        "MongoAdapter lands in planning doc §6 build order step 4 "
        "(motor client, schemaless semantics)."
    )