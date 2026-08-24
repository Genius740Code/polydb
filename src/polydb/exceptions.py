from __future__ import annotations


class PolydbError(Exception):
    """Base class for all polydb exceptions."""


class ConnectionNotOpenError(PolydbError):
    """Raised when an operation is attempted before connect() succeeds."""


class UnsupportedOperationError(PolydbError):
    """Raised when a requested operation has no valid translation for this backend."""


class SchemaRequiredError(PolydbError):
    """Raised when create_collection() is called on a schema-requiring backend without a schema."""


class TransactionsUnavailableError(PolydbError):
    """Raised when transaction() is called on a topology that doesn't support transactions."""


class TransactionInactiveError(PolydbError):
    """Raised when a call is made through a Transaction that is not active.

    Covers operations attempted before the transaction was entered
    (``async with``), after it was committed/rolled back, or after a failed
    statement inside it aborted the whole transaction.
    """


class PolydbQueryError(PolydbError):
    """Raised when a native driver query fails. Wraps the original driver exception."""


class InvalidConnectionStringError(PolydbError):
    """Raised when Database.from_url() is given a URL it cannot parse or resolve to an adapter."""


class InvalidFilterError(PolydbError):
    """Raised when a filter/update DSL dict fails validation (bad operator, bad field name, etc.)."""
