from __future__ import annotations

from polydb.database import Database
from polydb.exceptions import (
    ConnectionNotOpenError,
    InvalidConnectionStringError,
    InvalidFilterError,
    PolydbError,
    PolydbQueryError,
    SchemaRequiredError,
    TransactionsUnavailableError,
    UnsupportedOperationError,
)
from polydb.results import (
    DeleteResult,
    InsertManyResult,
    InsertResult,
    UpdateResult,
    UpsertResult,
)
from polydb.schema import Field, FieldType, Schema

__all__ = [
    "Database",
    "PolydbError",
    "ConnectionNotOpenError",
    "UnsupportedOperationError",
    "SchemaRequiredError",
    "TransactionsUnavailableError",
    "PolydbQueryError",
    "InvalidConnectionStringError",
    "InvalidFilterError",
    "InsertResult",
    "InsertManyResult",
    "UpsertResult",
    "UpdateResult",
    "DeleteResult",
    "Schema",
    "Field",
    "FieldType",
]

__version__ = "0.1.0"
