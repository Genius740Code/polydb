from __future__ import annotations

import re
from typing import Any

from polydb.compilers.sql_compiler import validate_identifier
from polydb.exceptions import InvalidFilterError

#: §2.2 comparison operators — all native Mongo operators, passed through.
_COMPARISON_OPERATORS = ("$eq", "$ne", "$gt", "$gte", "$lt", "$lte")

_LOGICAL_OPERATORS = ("$and", "$or", "$nor", "$not")

#: §2.4 update operators — all four are native on Mongo, including $push
#: (which the SQL family rejects; see sql_compiler.compile_update_set).
_UPDATE_OPERATORS = ("$set", "$unset", "$inc", "$push")


def _like_to_regex(pattern: str) -> str:
    """Translate one SQL ``LIKE`` pattern into its anchored regex equivalent.

    ``%`` → ``.*`` (any sequence), ``_`` → ``.`` (exactly one character),
    every other character is escaped literally. The result is anchored with
    ``^``/``$`` so, like SQL ``LIKE``, the whole value must match — Mongo's
    ``$regex`` alone would otherwise search anywhere in the string.
    """
    pieces = ["^"]
    for ch in pattern:
        if ch == "%":
            pieces.append(".*")
        elif ch == "_":
            pieces.append(".")
        else:
            pieces.append(re.escape(ch))
    pieces.append("$")
    return "".join(pieces)


class MongoCompiler:
    """Validates and normalizes the Mongo-shaped filter DSL (planning doc §2)
    for MongoDB.

    Mongo's operator dict is already the DSL AST, so compilation is mostly
    validation plus two normalizations forced by Mongo's own query language:

    - ``$like`` has no native equivalent — it becomes an anchored regex run
      through ``$expr`` + ``$regexMatch`` (§2.2).
    - A top-level ``$not`` filter is rewritten as ``{"$nor": [<filter>]}``,
      because Mongo only allows ``$not`` as a field-level operator, never as a
      standalone query predicate.

    Everything else passes through unchanged once validated, so malformed
    filters surface as ``InvalidFilterError`` from polydb with the same
    wording as the SQL family instead of leaking driver errors.
    """

    # -- filter compilation (§2.2) -------------------------------------------------------

    def compile_filter(self, filter_: dict[str, Any] | None) -> dict[str, Any]:
        """Validate/normalize a filter dict into a Mongo query dict.

        Args:
            filter_: Query DSL dict per §2; ``None`` and ``{}`` match all
                documents.

        Returns:
            The Mongo query document. Identical to the input whenever no
            ``$like`` appears (validation-only pass-through).

        Raises:
            InvalidFilterError: If the filter violates the §2 grammar.
        """
        if filter_ is None:
            return {}
        if not isinstance(filter_, dict):
            raise InvalidFilterError(
                f"filter must be a dict, got {type(filter_).__name__}"
            )
        return self._compile_level(filter_)

    def _compile_level(self, doc: dict[str, Any]) -> dict[str, Any]:
        """Compile one nesting level of a filter document.

        Predicates are gathered into ``flat`` (one Mongo document — sibling
        keys AND implicitly) while ``$like`` contributes standalone ``$expr``
        clauses that cannot share a key. Only when such clauses exist does
        the output get restructured behind an explicit ``$and``.
        """
        flat: dict[str, Any] = {}
        expr_clauses: list[dict[str, Any]] = []
        for key, value in doc.items():
            if isinstance(key, str) and key.startswith("$"):
                # _compile_logical returns the full predicate document —
                # possibly under a rewritten key ($not becomes $nor) — so
                # merge rather than assign.
                flat.update(self._compile_logical(key, value))
                continue
            column = validate_identifier(key, "column")
            if not isinstance(value, dict):
                if isinstance(value, list):
                    raise InvalidFilterError(
                        f"List values are only valid with $in/$nin; got a bare "
                        f"list for {column!r}"
                    )
                flat[column] = value
                continue
            native_ops, likes = self._compile_operator_expr(column, value)
            if native_ops:
                flat[column] = native_ops
            expr_clauses.extend(likes)
        if not expr_clauses:
            return flat
        docs = ([flat] if flat else []) + expr_clauses
        return docs[0] if len(docs) == 1 else {"$and": docs}

    def _compile_operator_expr(
        self, column: str, ops: dict[str, Any]
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Compile one field's operator dict.

        Returns:
            ``(native_ops, like_clauses)`` — the Mongo operator dict for this
            field with ``$like`` stripped out (empty when none remain), plus
            one ``$expr``/``$regexMatch`` clause per ``$like`` operator.
        """
        native_ops: dict[str, Any] = {}
        like_clauses: list[dict[str, Any]] = []
        for op, arg in ops.items():
            if op == "$like":
                if not isinstance(arg, str):
                    raise InvalidFilterError(
                        f"$like requires a pattern string for {column!r}, got "
                        f"{type(arg).__name__}"
                    )
                like_clauses.append(
                    {
                        "$expr": {
                            "$regexMatch": {
                                "input": f"${column}",
                                "regex": _like_to_regex(arg),
                            }
                        }
                    }
                )
                continue
            self._validate_operator_arg(column, op, arg)
            native_ops[op] = arg
        if not native_ops and not like_clauses:
            raise InvalidFilterError(
                f"operator dict for {column!r} must contain at least one "
                f"operator; got {{}}"
            )
        return native_ops, like_clauses

    @staticmethod
    def _validate_operator_arg(column: str, op: Any, arg: Any) -> None:
        """Enforce the same argument rules as the SQL compiler (contract parity)."""
        if op in _COMPARISON_OPERATORS:
            if isinstance(arg, (list, dict)):
                raise InvalidFilterError(
                    f"{op} requires a scalar argument, got "
                    f"{type(arg).__name__} for {column!r}"
                )
            if arg is None and op not in ("$eq", "$ne"):
                raise InvalidFilterError(
                    f"{op} does not accept null for {column!r}; use $exists "
                    f"to test for missing fields"
                )
            return
        if op in ("$in", "$nin"):
            if not isinstance(arg, list):
                raise InvalidFilterError(
                    f"{op} requires a list argument for {column!r}"
                )
            return
        if op == "$exists":
            if not isinstance(arg, bool):
                raise InvalidFilterError(
                    f"$exists requires true or false for {column!r}, got {arg!r}"
                )
            return
        if op == "$regex":
            if not isinstance(arg, str):
                raise InvalidFilterError(
                    f"$regex requires a pattern string for {column!r}, got "
                    f"{type(arg).__name__}"
                )
            return
        raise InvalidFilterError(f"Unsupported operator {op!r} for {column!r}")

    def _compile_logical(self, op: str, arg: Any) -> dict[str, Any]:
        """Compile $and/$or/$nor/$not (§2.1 grammar).

        ``$not`` becomes ``$nor`` of a one-element list: Mongo rejects a
        standalone ``$not`` at query level, and ``$nor`` with one element is
        exactly its negation. An empty sub-filter stays ``{}`` — Mongo
        natively treats it as match-everything, so the §2.1 note about
        negation semantics survives untouched.
        """
        if op not in _LOGICAL_OPERATORS:
            raise InvalidFilterError(f"Unsupported operator {op!r}")

        if op == "$not":
            if not isinstance(arg, dict):
                raise InvalidFilterError("$not requires a filter dict")
            return {"$nor": [self._compile_level(arg)]}

        if not isinstance(arg, list) or not arg:
            raise InvalidFilterError(
                f"{op} requires a non-empty list of filter dicts, got {arg!r}"
            )
        compiled: list[dict[str, Any]] = []
        for sub_filter in arg:
            if not isinstance(sub_filter, dict):
                raise InvalidFilterError(
                    f"{op} entries must be filter dicts, got "
                    f"{type(sub_filter).__name__}"
                )
            compiled.append(self._compile_level(sub_filter))
        return {op: compiled}

    # -- update compilation (§2.4) ---------------------------------------------------------

    def compile_update(self, update: dict[str, Any]) -> dict[str, Any]:
        """Validate a §2.4 update-operator dict for Mongo.

        All four operators — ``$set``, ``$inc``, ``$unset`` and ``$push`` —
        are native Mongo update operators, so unlike the SQL family this is a
        validation-only pass-through: the same object goes back out, ready to
        hand to the driver. Validation mirrors
        :meth:`SqlCompiler.compile_update_set <polydb.compilers.sql_compiler.SqlCompiler.compile_update_set>`
        so both backends reject the same malformed updates with the same
        error type.

        Raises:
            InvalidFilterError: Non-dict update, bare field keys, unknown
                operators, invalid field names, non-numeric ``$inc``
                arguments, or one column assigned by two different operators.
        """
        if not isinstance(update, dict):
            raise InvalidFilterError(
                f"update must be an operator dict "
                f"($set/$inc/$unset/$push), got {type(update).__name__}"
            )

        assigned: set[str] = set()
        for op, payload in update.items():
            if op not in _UPDATE_OPERATORS:
                if isinstance(op, str) and op.startswith("$"):
                    raise InvalidFilterError(f"Unsupported update operator {op!r}")
                raise InvalidFilterError(
                    f"update must use $set/$inc/$unset/$push operators; got "
                    f"bare field {op!r} — use replace_one() for full-document "
                    f"replacement"
                )
            if not isinstance(payload, dict):
                raise InvalidFilterError(
                    f"{op} requires a field-to-value dict, got {type(payload).__name__}"
                )
            for name, value in payload.items():
                validate_identifier(name, "column")
                if name in assigned:
                    raise InvalidFilterError(
                        f"column {name!r} appears in more than one update operator"
                    )
                assigned.add(name)
                if op == "$inc":
                    if isinstance(value, bool) or not isinstance(value, (int, float)):
                        raise InvalidFilterError(
                            f"$inc requires a numeric argument for {name!r}, "
                            f"got {value!r}"
                        )
        return update
