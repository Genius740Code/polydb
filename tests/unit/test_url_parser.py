from __future__ import annotations

import pytest

from polydb.exceptions import InvalidConnectionStringError
from polydb.url_parser import (
    _DEFAULT_POOL_SIZE,
    _DEFAULT_TIMEOUT_SECONDS,
    parse_connection_string,
)


def test_postgres_defaults():
    cfg = parse_connection_string("postgres://user:pass@localhost:5432/db")
    assert cfg.family == "postgres"
    assert cfg.scheme == "postgres"
    assert cfg.dialect is None
    assert cfg.host == "localhost"
    assert cfg.port == 5432
    assert cfg.user == "user"
    assert cfg.password == "pass"
    assert cfg.database == "db"
    assert cfg.pool_size == _DEFAULT_POOL_SIZE
    assert cfg.timeout == _DEFAULT_TIMEOUT_SECONDS


def test_postgresql_alias_maps_to_same_family():
    assert parse_connection_string("postgresql://h/db").family == "postgres"


def test_mongodb_and_srv_scheme():
    cfg = parse_connection_string("mongodb+srv://u:p@cluster.example.com/x")
    assert cfg.family == "mongo"
    assert cfg.scheme == "mongodb+srv"
    assert cfg.port is None  # SRV records carry the port


def test_sqlite_in_memory():
    cfg = parse_connection_string("sqlite:///:memory:")
    assert cfg.family == "sql"
    assert cfg.dialect == "sqlite"
    assert cfg.database == ":memory:"
    assert cfg.host is None


def test_sqlite_relative_path():
    cfg = parse_connection_string("sqlite:///data/app.db")
    assert cfg.database == "data/app.db"


def test_sqlite_absolute_path():
    cfg = parse_connection_string("sqlite:////var/lib/app.db")
    assert cfg.database == "/var/lib/app.db"


def test_mysql_dialect():
    cfg = parse_connection_string("mysql://user:pass@localhost:3306/db")
    assert cfg.family == "sql"
    assert cfg.dialect == "mysql"
    assert cfg.port == 3306


def test_pool_size_and_timeout_query_params():
    cfg = parse_connection_string("sqlite:///x.db?pool_size=3&timeout=5.5")
    assert cfg.pool_size == 3
    assert cfg.timeout == 5.5
    assert cfg.options == {}


def test_unknown_options_kept_verbatim():
    cfg = parse_connection_string("postgres://u@h/db?sslmode=require")
    assert cfg.options == {"sslmode": "require"}


def test_missing_scheme_rejected():
    with pytest.raises(InvalidConnectionStringError):
        parse_connection_string("localhost:5432/db")


def test_unknown_scheme_rejected():
    with pytest.raises(InvalidConnectionStringError):
        parse_connection_string("redis://localhost:6379/0")


def test_sqlite_without_path_rejected():
    with pytest.raises(InvalidConnectionStringError):
        parse_connection_string("sqlite:///")