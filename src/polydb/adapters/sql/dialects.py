from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Dialect:
    """Per-dialect knobs the shared SQL compiler (not yet built) will consume.

    Kept minimal for now — only what connection management needs. Compiler-facing
    fields (upsert syntax, regexp handling) get filled in when sql_compiler.py
    lands (planning doc §6 step 2/5).
    """

    name: str
    placeholder: str  # "?" for sqlite, "%s" for mysql
    supports_pool: bool


SqliteDialect = Dialect(name="sqlite", placeholder="?", supports_pool=False)
MysqlDialect = Dialect(name="mysql", placeholder="%s", supports_pool=True)

DIALECTS: dict[str, Dialect] = {
    "sqlite": SqliteDialect,
    "mysql": MysqlDialect,
}
