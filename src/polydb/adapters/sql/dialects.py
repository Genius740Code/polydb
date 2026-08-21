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
    identifier_quote: str  # '"' for sqlite/postgres, '`' for mysql


SqliteDialect = Dialect(
    name="sqlite", placeholder="?", supports_pool=False, identifier_quote='"'
)
MysqlDialect = Dialect(
    name="mysql", placeholder="%s", supports_pool=True, identifier_quote="`"
)

DIALECTS: dict[str, Dialect] = {
    "sqlite": SqliteDialect,
    "mysql": MysqlDialect,
}
