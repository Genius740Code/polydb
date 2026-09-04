from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Dialect:
    """Per-dialect knobs the shared SQL compiler will consume.

    ``placeholder`` is the *base* placeholder token the compiler emits
    (``"?"`` for sqlite, ``"%s"`` for mysql, ``"?"`` for postgres before it
    is numbered to ``$1``/``$2``). Postgres needs numbered ``$N``
    placeholders (asyncpg) — the adapter renumbers the ``"?"`` tokens after
    compilation (see :func:`polydb.compilers.sql_compiler.number_placeholders`).
    """

    name: str
    placeholder: str  # "?" for sqlite, "%s" for mysql, "?" (numbered later) for postgres
    supports_pool: bool
    # SQLite deliberately uses backticks (not '"'): a double-quoted token that
    # doesn't resolve to a column silently degrades to a string literal in
    # SQLite (legacy DQS misfeature) — e.g. WHERE "nope" REGEXP '.' would
    # match every row. Backtick quoting always raises "no such column".
    identifier_quote: str  # '`' for sqlite/mysql, '"' for postgres


SqliteDialect = Dialect(
    name="sqlite", placeholder="?", supports_pool=False, identifier_quote="`"
)
MysqlDialect = Dialect(
    name="mysql", placeholder="%s", supports_pool=True, identifier_quote="`"
)
PostgresDialect = Dialect(
    name="postgres", placeholder="?", supports_pool=True, identifier_quote='"'
)

DIALECTS: dict[str, Dialect] = {
    "sqlite": SqliteDialect,
    "mysql": MysqlDialect,
    "postgres": PostgresDialect,
}
