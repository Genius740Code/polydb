from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

from polydb.exceptions import InvalidConnectionStringError

# scheme -> (family, dialect)
# family picks which adapter class handles it; dialect is passed through for the
# sql family so one adapter class can serve both sqlite and mysql.
_SCHEME_TABLE: dict[str, tuple[str, str | None]] = {
    "postgres": ("postgres", None),
    "postgresql": ("postgres", None),
    "mongodb": ("mongo", None),
    "mongodb+srv": ("mongo", None),
    "sqlite": ("sql", "sqlite"),
    "mysql": ("sql", "mysql"),
}

_DEFAULT_TIMEOUT_SECONDS = 30.0
_DEFAULT_POOL_SIZE = 10


@dataclass
class ConnectionConfig:
    """Parsed, backend-agnostic view of a connection string.

    Attributes:
        raw_url: The original, unmodified connection string.
        scheme: The scheme exactly as written in the URL (e.g. ``"postgresql"``).
        family: Which adapter family handles this scheme: ``"postgres"``, ``"mongo"``,
            or ``"sql"``.
        dialect: For ``family == "sql"``, which dialect to compile for: ``"sqlite"``
            or ``"mysql"``. ``None`` for the other families.
        host: Hostname, or ``None`` for file-based backends (SQLite).
        port: Port number, or ``None`` if not specified / not applicable.
        user: Username, or ``None`` if not specified.
        password: Password, or ``None`` if not specified.
        database: Database name (Postgres/Mongo/MySQL) or file path (SQLite).
            For ``sqlite:///path/to/file.db`` this is ``path/to/file.db``.
            For ``sqlite:///:memory:`` this is ``:memory:``.
        pool_size: Pool size, from the ``pool_size`` query param, default 10.
        timeout: Timeout in seconds, from the ``timeout`` query param, default 30.0.
        options: Every other query param, kept verbatim for adapter-specific use.
    """

    raw_url: str
    scheme: str
    family: str
    dialect: str | None
    host: str | None
    port: int | None
    user: str | None
    password: str | None
    database: str | None
    pool_size: int = _DEFAULT_POOL_SIZE
    timeout: float = _DEFAULT_TIMEOUT_SECONDS
    options: dict[str, Any] = field(default_factory=dict)


def parse_connection_string(url: str) -> ConnectionConfig:
    """Parse a connection string into a :class:`ConnectionConfig`.

    Args:
        url: A connection string, e.g. ``"postgres://user:pass@host:5432/dbname"``,
            ``"mongodb://host:27017/dbname"``, ``"sqlite:///path/to/file.db"``, or
            ``"mysql://user:pass@host:3306/dbname"``.

    Returns:
        The parsed, backend-agnostic connection config.

    Raises:
        InvalidConnectionStringError: If the URL has no scheme, or the scheme is not
            one polydb recognizes.
    """
    if not url or "://" not in url:
        raise InvalidConnectionStringError(
            f"Not a valid connection string (missing '://'): {url!r}"
        )

    parts = urlsplit(url)
    scheme = parts.scheme.lower()

    if scheme not in _SCHEME_TABLE:
        supported = ", ".join(sorted(_SCHEME_TABLE))
        raise InvalidConnectionStringError(
            f"Unrecognized scheme {scheme!r} in {url!r}. Supported schemes: {supported}."
        )

    family, dialect = _SCHEME_TABLE[scheme]

    query = {k: v[-1] for k, v in parse_qs(parts.query).items()}

    pool_size = int(query.pop("pool_size", _DEFAULT_POOL_SIZE))
    timeout = float(query.pop("timeout", _DEFAULT_TIMEOUT_SECONDS))

    if scheme == "sqlite":
        # sqlite:///relative/path.db  -> netloc="" path="/relative/path.db"  (3 slashes = relative)
        # sqlite:////abs/path.db      -> netloc="" path="//abs/path.db"      (4 slashes = absolute)
        # sqlite:///:memory:          -> path="/:memory:"
        raw_path = parts.path
        if raw_path == "/:memory:":
            database = ":memory:"
        elif raw_path.startswith("//"):
            database = raw_path[1:]  # keep exactly one leading '/' -> absolute path
        else:
            database = raw_path.lstrip("/")  # relative to cwd
        if database == "":
            raise InvalidConnectionStringError(
                f"sqlite URL must include a file path or ':memory:': {url!r}"
            )
        host, port, user, password = None, None, None, None
    else:
        host = parts.hostname
        port = parts.port
        user = parts.username
        password = parts.password
        database = parts.path.lstrip("/") or None

    return ConnectionConfig(
        raw_url=url,
        scheme=scheme,
        family=family,
        dialect=dialect,
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        pool_size=pool_size,
        timeout=timeout,
        options=query,
    )
