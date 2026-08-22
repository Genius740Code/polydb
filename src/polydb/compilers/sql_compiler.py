from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from polydb.adapters.sql.dialects import Dialect
from polydb.exceptions import InvalidFilterError, UnsupportedOperationError

#: §2.3: field/table names are validated against this pattern before being
#: identifier-quoted into SQL — values are parameterized, names must be safe too.
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: §2.2 comparison operators and their SQL translations.
_COMPARISON_SQL = {
    "$eq": "=",
    "$ne": "<>",
    "$gt": ">",
    "$gte": ">=",
    "$lt": "<",
    "$lte": "<=",
}

_LOGICAL_OPERATORS = ("$and", "$or", "$nor", "$not")

#: §2.4 update operators accepted by update_one()/update_many().
_UPDATE_OPERATORS = ("$set", "$unset", "$inc")

_ACCUMULATORS = ("$sum", "$avg", "$min", "$max", "$count")

_ACCUMULATOR_SQL = {"$sum": "SUM", "$avg": "AVG", "$min": "MIN", "$max": "MAX"}

_SUPPORTED_STAGES = ("$match", "$group", "$sort", "$limit", "$count")

# Canonical pipeline order (§1.3 #14): $match* → $group? → $sort? → $limit?
# → $count?. $match is the only repeatable stage.
_STAGE_ORDER = {"$match": 0, "$group": 1, "$sort": 2, "$limit": 3, "$count": 4}


def validate_identifier(name: str, kind: str) -> str:
    """Validate a column or table name against the DSL identifier pattern.

    Args:
        name: The identifier as supplied by application code.
        kind: ``"table"`` or ``"column"`` — used only in error messages.

    Returns:
        The validated name, unchanged.

    Raises:
        InvalidFilterError: If the name is empty, not a string, or contains
            characters outside ``[A-Za-z0-9_]``.
    """
    if not isinstance(name, str) or not IDENTIFIER_PATTERN.match(name):
        raise InvalidFilterError(
            f"Invalid {kind} name {name!r}: must match ^[A-Za-z_][A-Za-z0-9_]*$"
        )
    return name


@dataclass
class CompiledQuery:
    """A compiled SQL statement plus its ordered bind parameters."""

    sql: str
    params: list[Any] = field(default_factory=list)


@dataclass
class AggregatePlan(CompiledQuery):
    """Compiled aggregate pipeline plus the metadata needed to reshape rows.

    The adapter executes ``sql``/``params``, then uses ``grouped`` and
    ``count_field`` to turn raw rows back into Mongo-shaped documents
    (nested ``_id``, single-doc ``$count`` output).
    """

    grouped: bool = False
    count_field: str | None = None


class SqlCompiler:
    """Compiles the Mongo-shaped filter/query DSL (planning doc §2) into
    parameterized SQL.

    One instance per ``SqlAdapter``; dialect knobs (placeholder style,
    identifier quote char) come from ``self.dialect``. Every emitted statement
    is fully parameterized — user values never touch the SQL string, and every
    column/table name is validated against the §2.3 identifier pattern and
    quoted before being placed in the query.
    """

    def __init__(self, dialect: Dialect) -> None:
        self.dialect = dialect

    # -- primitives -----------------------------------------------------------------

    @property
    def _ph(self) -> str:
        return self.dialect.placeholder

    def _quote(self, name: str) -> str:
        q = self.dialect.identifier_quote
        return f"{q}{name}{q}"

    def _column(self, name: str) -> str:
        return self._quote(validate_identifier(name, "column"))

    # -- WHERE compilation (§2.2) -----------------------------------------------------

    def compile_where(self, filter_: dict[str, Any] | None) -> tuple[str, list[Any]]:
        """Compile a filter dict into ``(where_sql, params)``.

        Args:
            filter_: Query DSL dict per §2; ``None`` and ``{}`` match all rows.

        Returns:
            ``("", [])`` when the filter matches everything, otherwise a string
            starting with ``" WHERE "`` plus its ordered parameters.

        Raises:
            InvalidFilterError: If the filter violates the §2 grammar.
        """
        if not filter_:
            return "", []
        if not isinstance(filter_, dict):
            raise InvalidFilterError(
                f"filter must be a dict, got {type(filter_).__name__}"
            )
        clauses, params = self._compile_filter(filter_)
        if not clauses:
            return "", []
        return " WHERE " + " AND ".join(clauses), params

    def _compile_filter(
        self, filter_: dict[str, Any]
    ) -> tuple[list[str], list[Any]]:
        """Compile each field_expr / logical_expr in a filter; AND them together."""
        clauses: list[str] = []
        params: list[Any] = []
        for key, value in filter_.items():
            if isinstance(key, str) and key.startswith("$"):
                clause, key_params = self._compile_logical(key, value)
            else:
                column = self._column(key)
                clause, key_params = self._compile_field_expr(column, value)
            if clause:
                clauses.append(clause)
                params.extend(key_params)
        return clauses, params

    def _compile_field_expr(
        self, column: str, value: Any
    ) -> tuple[str, list[Any]]:
        """Compile one field's expression: scalar shorthand or operator dict."""
        if not isinstance(value, dict):
            return self._compile_scalar_predicate(column, value)
        return self._compile_operator_expr(column, value)

    def _compile_scalar_predicate(
        self, column: str, value: Any
    ) -> tuple[str, list[Any]]:
        """Plain ``{"field": value}`` shorthand → equality (None → IS NULL)."""
        if isinstance(value, list):
            raise InvalidFilterError(
                f"List values are only valid with $in/$nin; got a bare list for "
                f"{column}"
            )
        if value is None:
            return f"{column} IS NULL", []
        return f"{column} = {self._ph}", [value]

    def _compile_operator_expr(
        self, column: str, ops: Any
    ) -> tuple[str, list[Any]]:
        """Compile an operator dict such as ``{"$gte": 100, "$lt": 5000}``."""
        if not isinstance(ops, dict):
            raise InvalidFilterError(
                f"Value for {column} must be a scalar or an operator dict, got "
                f"{type(ops).__name__}"
            )
        clauses: list[str] = []
        params: list[Any] = []
        for op, arg in ops.items():
            clause, op_params = self._compile_operator(column, op, arg)
            if clause:
                clauses.append(clause)
                params.extend(op_params)
        if not clauses:
            return "", []
        if len(clauses) == 1:
            return clauses[0], params
        return "(" + " AND ".join(clauses) + ")", params

    def _compile_operator(
        self, column: str, op: str, arg: Any
    ) -> tuple[str, list[Any]]:
        """Compile a single operator within a field's operator dict."""
        if op in _COMPARISON_SQL:
            if isinstance(arg, (list, dict)):
                raise InvalidFilterError(
                    f"{op} requires a scalar argument, got "
                    f"{type(arg).__name__} for {column}"
                )
            if arg is None:
                if op == "$eq":
                    return f"{column} IS NULL", []
                if op == "$ne":
                    return f"{column} IS NOT NULL", []
                raise InvalidFilterError(
                    f"{op} does not accept null for {column}; use $exists to "
                    f"test for missing fields"
                )
            return f"{column} {_COMPARISON_SQL[op]} {self._ph}", [arg]

        if op in ("$in", "$nin"):
            if not isinstance(arg, list):
                raise InvalidFilterError(
                    f"{op} requires a list argument for {column}"
                )
            if not arg:
                # Mongo semantics: $in [] matches nothing, $nin [] matches all.
                return ("0 = 1", []) if op == "$in" else ("", [])
            joiner = "IN" if op == "$in" else "NOT IN"
            placeholder_sql = ", ".join([self._ph] * len(arg))
            return f"{column} {joiner} ({placeholder_sql})", list(arg)

        if op == "$exists":
            if not isinstance(arg, bool):
                raise InvalidFilterError(
                    f"$exists requires true or false for {column}, got {arg!r}"
                )
            nullness = "NOT NULL" if arg else "NULL"
            return f"{column} IS {nullness}", []

        if op == "$regex":
            if not isinstance(arg, str):
                raise InvalidFilterError(
                    f"$regex requires a pattern string for {column}, got "
                    f"{type(arg).__name__}"
                )
            # SQLite resolves REGEXP through the user-defined function
            # registered at connect() time (§2.2); MySQL has REGEXP natively.
            return f"{column} REGEXP {self._ph}", [arg]

        if op == "$like":
            if not isinstance(arg, str):
                raise InvalidFilterError(
                    f"$like requires a pattern string for {column}, got "
                    f"{type(arg).__name__}"
                )
            return f"{column} LIKE {self._ph}", [arg]

        raise InvalidFilterError(f"Unsupported operator {op!r} for {column}")

    @staticmethod
    def _parenthesize(clause: str) -> str:
        """Wrap a clause once; never stack redundant parens on an already-wrapped one."""
        if clause.startswith("(") and clause.endswith(")"):
            return clause
        return f"({clause})"

    def _compile_logical(self, op: str, arg: Any) -> tuple[str, list[Any]]:
        """Compile $and/$or/$nor/$not logical expressions (§2.1 grammar)."""
        if op not in _LOGICAL_OPERATORS:
            raise InvalidFilterError(f"Unsupported operator {op!r}")

        if op == "$not":
            if not isinstance(arg, dict):
                raise InvalidFilterError("$not requires a filter dict")
            not_clauses, not_params = self._compile_filter(arg)
            if not not_clauses:
                return "", []
            return (
                "(NOT " + self._parenthesize(" AND ".join(not_clauses)) + ")"
            ), not_params

        if not isinstance(arg, list) or not arg:
            raise InvalidFilterError(
                f"{op} requires a non-empty list of filter dicts, got {arg!r}"
            )

        sub_clauses: list[str] = []
        params: list[Any] = []
        for sub_filter in arg:
            if not isinstance(sub_filter, dict):
                raise InvalidFilterError(
                    f"{op} entries must be filter dicts, got "
                    f"{type(sub_filter).__name__}"
                )
            clauses, sub_params = self._compile_filter(sub_filter)
            if not clauses:
                continue  # e.g. {} — matches everything, adds no constraint
            sub_clauses.append(self._parenthesize(" AND ".join(clauses)))
            params.extend(sub_params)

        if not sub_clauses:
            return "", []

        if op == "$and":
            return "(" + " AND ".join(sub_clauses) + ")", params
        joined = " OR ".join(sub_clauses)
        if op == "$or":
            return "(" + joined + ")", params
        return "(NOT (" + joined + "))", params  # $nor

    # -- update compilation (§2.4) -------------------------------------------------------

    def compile_update_set(self, update: dict[str, Any]) -> tuple[str, list[Any]]:
        """Compile a §2.4 update-operator dict into ``SET`` SQL + params.

        Args:
            update: Operator dict — ``$set``, ``$inc``, ``$unset`` (§2.4). A
                bare replacement document is not valid here; use
                ``replace_one()`` for full-document replacement. ``$unset``
                ignores its value (any payload compiles to ``SET … = NULL``).

        Returns:
            ``(set_sql, params)`` where ``set_sql`` starts with ``" SET "`` and
            params are ordered to match; ``("", [])`` when nothing would
            change (empty dict or only empty operator payloads).

        Raises:
            InvalidFilterError: Non-dict update, bare field keys (operator-less
                update), unknown operators, non-numeric ``$inc`` arguments,
                or one column assigned by two different operators.
            UnsupportedOperationError: ``$push`` — no portable array-append in
                relational SQL without a JSON-column convention (§2.4, §8.5).
        """
        if not isinstance(update, dict):
            raise InvalidFilterError(
                f"update must be an operator dict ($set/$inc/$unset), got "
                f"{type(update).__name__}"
            )

        assignments: list[str] = []
        params: list[Any] = []
        assigned: set[str] = set()

        for op, payload in update.items():
            if op == "$push":
                raise UnsupportedOperationError(
                    "$push has no portable SQL translation (§2.4); store arrays "
                    "in a JSON column and update them via raw() instead"
                )
            if op not in _UPDATE_OPERATORS:
                if isinstance(op, str) and op.startswith("$"):
                    raise InvalidFilterError(f"Unsupported update operator {op!r}")
                raise InvalidFilterError(
                    f"update must use $set/$inc/$unset operators; got bare field "
                    f"{op!r} — use replace_one() for full-document replacement"
                )
            if not isinstance(payload, dict):
                raise InvalidFilterError(
                    f"{op} requires a field-to-value dict, got "
                    f"{type(payload).__name__}"
                )
            for name, value in payload.items():
                column = self._column(name)
                if column in assigned:
                    raise InvalidFilterError(
                        f"column {name!r} appears in more than one update operator"
                    )
                assigned.add(column)
                if op == "$unset":
                    assignments.append(f"{column} = NULL")
                elif op == "$set":
                    assignments.append(f"{column} = {self._ph}")
                    params.append(value)
                else:  # $inc
                    if isinstance(value, bool) or not isinstance(value, (int, float)):
                        raise InvalidFilterError(
                            f"$inc requires a numeric argument for {name!r}, "
                            f"got {value!r}"
                        )
                    assignments.append(f"{column} = {column} + {self._ph}")
                    params.append(value)

        if not assignments:
            return "", []
        return " SET " + ", ".join(assignments), params  # $nor

    # -- read operations (§1.3) ---------------------------------------------------------

    def compile_find(
        self,
        table: str,
        filter_: dict[str, Any] | None,
        sort: list[tuple[str, int]] | None,
        limit: int | None,
        offset: int | None,
    ) -> CompiledQuery:
        """Compile the SELECT behind find()/find_one()."""
        where_sql, params = self.compile_where(filter_)
        order_sql = self._compile_sort(sort, grouped=False)
        limit_sql, page_params = self._compile_limit_offset(limit, offset)
        sql = f"SELECT * FROM {table}{where_sql}{order_sql}{limit_sql}"
        return CompiledQuery(sql=sql, params=[*params, *page_params])

    def compile_count(
        self, table: str, filter_: dict[str, Any] | None
    ) -> CompiledQuery:
        """Compile the COUNT(*) behind count()."""
        where_sql, params = self.compile_where(filter_)
        sql = f"SELECT COUNT(*) FROM {table}{where_sql}"
        return CompiledQuery(sql=sql, params=params)

    def compile_exists(self, table: str, filter_: dict[str, Any]) -> CompiledQuery:
        """Compile the short-circuiting existence probe behind exists()."""
        where_sql, params = self.compile_where(filter_)
        sql = f"SELECT 1 FROM {table}{where_sql} LIMIT 1"
        return CompiledQuery(sql=sql, params=params)

    def _compile_sort(
        self,
        sort: list[tuple[str, int]] | None,
        *,
        grouped: bool,
        aliases: frozenset[str] | set[str] = frozenset(),
    ) -> str:
        """Compile a sort spec into ORDER BY SQL ("" when unsorted).

        Ungrouped sorts address real columns; grouped sorts address output
        aliases — ``"_id"``, ``"_id.<part>"`` paths, or accumulator names.
        """
        if not sort:
            return ""
        if not isinstance(sort, (list, tuple)):
            raise InvalidFilterError(
                f"sort must be a list of (field, direction) tuples, got "
                f"{type(sort).__name__}"
            )
        parts: list[str] = []
        for entry in sort:
            if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                raise InvalidFilterError(
                    f"sort entries must be (field, direction) pairs, got {entry!r}"
                )
            name, direction = entry
            if direction not in (1, -1):
                raise InvalidFilterError(
                    f"sort direction must be 1 (asc) or -1 (desc), got "
                    f"{direction!r} for {name!r}"
                )
            if grouped:
                key = self._grouped_sort_key(name, aliases)
            else:
                key = self._column(name)
            parts.append(f"{key} {'ASC' if direction == 1 else 'DESC'}")
        return " ORDER BY " + ", ".join(parts)

    def _grouped_sort_key(
        self, name: Any, aliases: frozenset[str] | set[str]
    ) -> str:
        """Validate/quote a sort key referencing group output aliases."""
        if isinstance(name, str) and (name == "_id" or name.startswith("_id.")):
            validate_identifier(name[4:] or "_id", "column")
            return self._quote(name)
        if isinstance(name, str) and name in aliases:
            return self._quote(name)
        raise InvalidFilterError(
            f"sort field {name!r} is not addressable after $group; use '_id', "
            f"an '_id.<part>' path, or one of the group output aliases "
            f"{sorted(aliases)}"
        )

    def _compile_limit_offset(
        self, limit: int | None, offset: int | None
    ) -> tuple[str, list[Any]]:
        """Compile LIMIT/OFFSET; SQLite needs ``LIMIT -1`` when only offset is set."""
        if limit is None and offset is None:
            return "", []
        params: list[Any] = []
        pieces: list[str] = []
        if limit is not None:
            self._validate_paging_int("limit", limit)
            pieces.append("LIMIT ?")
            params.append(limit)
        if offset is not None:
            self._validate_paging_int("offset", offset)
            if limit is None:
                pieces.append("LIMIT -1")  # SQL requires LIMIT before OFFSET
            pieces.append("OFFSET ?")
            params.append(offset)
        return " " + " ".join(pieces), params

    @staticmethod
    def _validate_paging_int(kind: str, value: Any) -> None:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise InvalidFilterError(
                f"{kind} must be a non-negative int, got {value!r}"
            )

    # -- aggregate compilation (§1.3 #14 restricted subset) -------------------------------

    def compile_aggregate(
        self, table: str, pipeline: list[dict[str, Any]]
    ) -> AggregatePlan:
        """Compile the supported pipeline subset into one SQL statement.

        Supported stages, in canonical order: ``$match`` (zero or more),
        ``$group``, ``$sort``, ``$limit``, ``$count`` (each at most once).
        Supported ``$group`` accumulators: ``$sum``/``$avg``/``$min``/``$max``
        over a ``"$field"`` reference or numeric constant, plus ``$count``.

        Returns:
            An :class:`AggregatePlan` the adapter executes and reshapes.

        Raises:
            UnsupportedOperationError: Unknown/out-of-order stages, unsupported
                accumulators, or unsupported ``_id`` shapes.
            InvalidFilterError: Malformed stage payloads.
        """
        if not isinstance(pipeline, list):
            raise InvalidFilterError(
                f"pipeline must be a list of stages, got {type(pipeline).__name__}"
            )

        state = _AggregateBuild(compiler=self, table=table)
        last_order = -1
        for stage in pipeline:
            if not isinstance(stage, dict) or len(stage) != 1:
                raise InvalidFilterError(
                    f"each pipeline stage must be a single-key dict, got {stage!r}"
                )
            ((name, spec),) = stage.items()
            if name not in _STAGE_ORDER:
                raise UnsupportedOperationError(
                    f"aggregate stage {name!r} is outside the supported subset "
                    f"{list(_SUPPORTED_STAGES)}"
                )
            order = _STAGE_ORDER[name]
            if order < last_order or (order == last_order and order != 0):
                raise UnsupportedOperationError(
                    f"pipeline stages must appear in $match → $group → $sort → "
                    f"$limit → $count order; {name!r} is out of place"
                )
            last_order = order
            getattr(state, f"_stage_{name.lstrip('$')}")(spec)

        return state.finish()


class _AggregateBuild:
    """Mutable build state threaded through one aggregate pipeline."""

    def __init__(self, compiler: SqlCompiler, table: str) -> None:
        self.compiler = compiler
        self.table = table
        self.where_sql = ""
        self.where_params: list[Any] = []
        self.group_spec: dict[str, Any] | None = None
        self.sort: list[tuple[str, int]] | None = None
        self.limit: int | None = None
        self.count_field: str | None = None

    # -- stage handlers -----------------------------------------------------------------

    def _stage_match(self, spec: Any) -> None:
        if not isinstance(spec, dict):
            raise InvalidFilterError(
                f"$match requires a filter dict, got {type(spec).__name__}"
            )
        where_sql, params = self.compiler.compile_where(spec)
        if where_sql:
            rest = where_sql.removeprefix(" WHERE ")
            self.where_sql = f"{self.where_sql} AND {rest}" if self.where_sql else where_sql
            self.where_params.extend(params)

    def _stage_group(self, spec: Any) -> None:
        if not isinstance(spec, dict) or "_id" not in spec:
            raise InvalidFilterError('$group requires an "_id" key')
        self.group_spec = spec

    def _stage_sort(self, spec: Any) -> None:
        if not isinstance(spec, dict) or not spec:
            raise InvalidFilterError(
                "$sort requires a non-empty dict like {'field': 1}"
            )
        self.sort = list(spec.items())

    def _stage_limit(self, spec: Any) -> None:
        if not isinstance(spec, int) or isinstance(spec, bool) or spec < 1:
            raise InvalidFilterError(f"$limit requires a positive int, got {spec!r}")
        self.limit = spec

    def _stage_count(self, spec: Any) -> None:
        if not isinstance(spec, str):
            raise InvalidFilterError(
                f"$count requires an output field name string, got {spec!r}"
            )
        self.count_field = validate_identifier(spec, "column")

    # -- final assembly --------------------------------------------------------------------

    def finish(self) -> AggregatePlan:
        """Assemble the visited stages into one compiled statement."""
        if self.count_field is not None and self.group_spec is None:
            # Standalone $count: total documents surviving the $match stages.
            sql = (
                f"SELECT COUNT(*) AS {self.compiler._quote(self.count_field)} "
                f"FROM {self.table}{self.where_sql}"
            )
            return AggregatePlan(
                sql=sql, params=self.where_params, count_field=self.count_field
            )

        limit_sql, limit_params = self.compiler._compile_limit_offset(self.limit, None)

        if self.group_spec is not None:
            select_sql, select_params, group_by_sql, aliases = (
                self._build_grouped_head(self.group_spec)
            )
            order_sql = self.compiler._compile_sort(
                self.sort, grouped=True, aliases=aliases
            )
            core = (
                f"{select_sql} FROM {self.table}{self.where_sql}"
                f"{group_by_sql}{order_sql}{limit_sql}"
            )
            # Placeholder order in the SQL string: SELECT constants, then
            # WHERE values, then paging — keep the bind list aligned.
            params = [*select_params, *self.where_params, *limit_params]
            if self.count_field is not None:
                sql = (
                    f"SELECT COUNT(*) AS {self.compiler._quote(self.count_field)} "
                    f"FROM ({core})"
                )
                return AggregatePlan(sql=sql, params=params, count_field=self.count_field)
            return AggregatePlan(sql=core, params=params, grouped=True)

        order_sql = self.compiler._compile_sort(self.sort, grouped=False)
        sql = f"SELECT * FROM {self.table}{self.where_sql}{order_sql}{limit_sql}"
        return AggregatePlan(sql=sql, params=[*self.where_params, *limit_params])

    def _build_grouped_head(
        self, spec: dict[str, Any]
    ) -> tuple[str, list[Any], str, set[str]]:
        """Build ``SELECT …`` / ``GROUP BY …`` for the ``$group`` spec.

        Returns:
            ``(select_sql, select_params, group_by_sql, output_aliases)``.
            Composite ``_id`` parts are aliased as ``"_id.<part>"`` so the
            adapter can reassemble Mongo's nested ``_id`` document.
        """
        select_parts: list[str] = []
        select_params: list[Any] = []
        group_columns: list[str] = []
        aliases: set[str] = set()

        id_spec = spec["_id"]
        if id_spec is None:
            # One global group; HAVING drops the phantom row SQL aggregates
            # over empty input (verified against SQLite/Mongo semantics).
            select_parts.append(f"NULL AS {self.compiler._quote('_id')}")
        elif isinstance(id_spec, str) and id_spec.startswith("$"):
            column = self.compiler._column(id_spec[1:])
            select_parts.append(f"{column} AS {self.compiler._quote('_id')}")
            group_columns.append(column)
        elif isinstance(id_spec, dict) and id_spec:
            for part_name, ref in id_spec.items():
                validate_identifier(part_name, "column")
                if not (isinstance(ref, str) and ref.startswith("$")):
                    raise UnsupportedOperationError(
                        f'$group composite _id parts must be "$field" '
                        f"references; got {ref!r} for {part_name!r}"
                    )
                column = self.compiler._column(ref[1:])
                select_parts.append(
                    f"{column} AS {self.compiler._quote(f'_id.{part_name}')}"
                )
                group_columns.append(column)
        else:
            raise UnsupportedOperationError(
                '$group _id must be null, a "$field" reference, or a non-empty '
                'dict of name → "$field" references'
            )

        for out_name, accumulator in spec.items():
            if out_name == "_id":
                continue
            validate_identifier(out_name, "column")
            if not isinstance(accumulator, dict) or len(accumulator) != 1:
                raise InvalidFilterError(
                    f"group output {out_name!r} must be a single-accumulator "
                    f"dict such as {{'$sum': '$amount'}}"
                )
            ((acc_op, acc_arg),) = accumulator.items()
            if acc_op not in _ACCUMULATORS:
                raise UnsupportedOperationError(
                    f"accumulator {acc_op!r} is outside the supported subset "
                    f"{list(_ACCUMULATORS)} (output {out_name!r})"
                )
            if acc_op == "$count":
                if acc_arg not in ({}, 1, "*"):
                    raise InvalidFilterError(
                        f"$count accumulator for {out_name!r} takes {{}} "
                        f"(or 1 / '*'), got {acc_arg!r}"
                    )
                agg_sql = "COUNT(*)"
            else:
                agg_sql, acc_params = self._accumulator_arg(out_name, acc_op, acc_arg)
                select_params.extend(acc_params)
            select_parts.append(f"{agg_sql} AS {self.compiler._quote(out_name)}")
            aliases.add(out_name)

        select_sql = "SELECT " + ", ".join(select_parts)
        if group_columns:
            group_by_sql = " GROUP BY " + ", ".join(group_columns)
        else:
            group_by_sql = " GROUP BY NULL HAVING COUNT(*) > 0"
        return select_sql, select_params, group_by_sql, aliases

    def _accumulator_arg(
        self, out_name: str, acc_op: str, acc_arg: Any
    ) -> tuple[str, list[Any]]:
        """Translate one $sum/$avg/$min/$max argument into aggregate SQL."""
        if isinstance(acc_arg, str) and acc_arg.startswith("$"):
            func = _ACCUMULATOR_SQL[acc_op]
            return f"{func}({self.compiler._column(acc_arg[1:])})", []
        if isinstance(acc_arg, (int, float)) and not isinstance(acc_arg, bool):
            # e.g. {"total": {"$sum": 1}} — SUM(constant) counts rows × constant.
            return f"{_ACCUMULATOR_SQL[acc_op]}({self.compiler._ph})", [acc_arg]
        raise InvalidFilterError(
            f"{acc_op} for output {out_name!r} takes a \"$field\" reference or "
            f"a numeric constant, got {acc_arg!r}"
        )
