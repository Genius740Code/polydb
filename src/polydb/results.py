from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class InsertResult:
    """Result of insert_one()."""

    inserted_id: Any


@dataclass
class InsertManyResult:
    """Result of insert_many()."""

    inserted_ids: list[Any] = field(default_factory=list)
    inserted_count: int = 0


@dataclass
class UpsertResult:
    """Result of upsert_one()."""

    matched_count: int
    modified_count: int
    upserted_id: Any | None = None


@dataclass
class UpdateResult:
    """Result of update_one() / update_many() / replace_one()."""

    matched_count: int
    modified_count: int


@dataclass
class DeleteResult:
    """Result of delete_one() / delete_many()."""

    deleted_count: int
