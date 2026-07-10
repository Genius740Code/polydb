from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class FieldType(str, Enum):
    """Small common type set for relational ``create_collection(schema=...)``.

    Open question §8.4: whether to also escape-hatch raw dialect-specific column
    types (e.g. ``VARCHAR(255)``) through here. Deferred until that's resolved.
    """

    STR = "str"
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    DATETIME = "datetime"
    JSON = "json"


@dataclass
class Field:
    """A single column definition."""

    name: str
    type: FieldType
    nullable: bool = True
    default: object | None = None
    primary_key: bool = False
    unique: bool = False


@dataclass
class Schema:
    """Column spec passed to ``create_collection()``.

    Required on relational backends (Postgres, the SQL family); ignored (with a
    logged info-level note) on MongoDB, since Mongo collections are schemaless.
    """

    fields: list[Field] = field(default_factory=list)
