from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Dialect:
    """Per-dialect knobs the shared SQL compiler (not yet built) will consume.

    Kept minimal for now — only what connection management and the §1.2 Create
    operations need. Remaining compiler-facing fields (regexp handling,
    upsert-conflict syntax) get filled in when sql_compiler.py lands
    (planning doc §6 step 2/5).
    """

    name: str
    placeholder: str  # "?" for sqlite, "%s" for mysql
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
    name="postgres", placeholder="$1", supports_pool=True, identifier_quote='"'
)

DIALECTS: dict[str, Dialect] = {
    "sqlite": SqliteDialect,
    "mysql": MysqlDialect,
    "postgres": PostgresDialect,
}
