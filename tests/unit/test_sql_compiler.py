from __future__ import annotations

import pytest

from polydb.adapters.sql.dialects import MysqlDialect, SqliteDialect
from polydb.compilers.sql_compiler import SqlCompiler
from polydb.exceptions import InvalidFilterError, UnsupportedOperationError


@pytest.fixture
def compiler():
    return SqlCompiler(SqliteDialect)


# -- compile_where: scalar shorthand -------------------------------------------------


def test_empty_filter_matches_all(compiler):
    assert compiler.compile_where(None) == ("", [])
    assert compiler.compile_where({}) == ("", [])


def test_scalar_equality_shorthand(compiler):
    where, params = compiler.compile_where({"status": "open"})
    assert where == " WHERE `status` = ?"
    assert params == ["open"]


def test_none_scalar_compiles_to_is_null(compiler):
    where, params = compiler.compile_where({"deleted_at": None})
    assert where == " WHERE `deleted_at` IS NULL"
    assert params == []


def test_multiple_fields_are_anded(compiler):
    where, params = compiler.compile_where({"a": 1, "b": "x"})
    assert where == " WHERE `a` = ? AND `b` = ?"
    assert params == [1, "x"]


def test_bare_list_value_rejected(compiler):
    with pytest.raises(InvalidFilterError):
        compiler.compile_where({"tag": ["a", "b"]})


# -- comparison operators -------------------------------------------------------------


@pytest.mark.parametrize(
    ("op", "sql_op"),
    [("$eq", "="), ("$ne", "<>"), ("$gt", ">"), ("$gte", ">="), ("$lt", "<"), ("$lte", "<=")],
)
def test_comparison_operators(compiler, op, sql_op):
    where, params = compiler.compile_where({"age": {op: 21}})
    assert where == f" WHERE `age` {sql_op} ?"
    assert params == [21]


def test_operator_dict_with_two_ops_is_anded_and_parenthesized(compiler):
    where, params = compiler.compile_where({"amount": {"$gte": 100, "$lt": 5000}})
    assert where == " WHERE (`amount` >= ? AND `amount` < ?)"
    assert params == [100, 5000]


def test_eq_null_becomes_is_null(compiler):
    where, params = compiler.compile_where({"x": {"$eq": None}})
    assert where == " WHERE `x` IS NULL"
    assert params == []


def test_ne_null_becomes_is_not_null(compiler):
    where, params = compiler.compile_where({"x": {"$ne": None}})
    assert where == " WHERE `x` IS NOT NULL"
    assert params == []


def test_range_operator_with_null_rejected(compiler):
    with pytest.raises(InvalidFilterError):
        compiler.compile_where({"age": {"$gt": None}})


def test_comparison_with_list_rejected(compiler):
    with pytest.raises(InvalidFilterError):
        compiler.compile_where({"age": {"$gt": [1]}})


# -- $in / $nin -----------------------------------------------------------------------


def test_in_expands_placeholders(compiler):
    where, params = compiler.compile_where({"status": {"$in": ["open", "pending"]}})
    assert where == " WHERE `status` IN (?, ?)"
    assert params == ["open", "pending"]


def test_nin_negates(compiler):
    where, params = compiler.compile_where({"status": {"$nin": [1, 2, 3]}})
    assert where == " WHERE `status` NOT IN (?, ?, ?)"
    assert params == [1, 2, 3]


def test_empty_in_matches_nothing(compiler):
    where, params = compiler.compile_where({"status": {"$in": []}})
    assert where == " WHERE 0 = 1"
    assert params == []


def test_empty_nin_matches_everything(compiler):
    assert compiler.compile_where({"status": {"$nin": []}}) == ("", [])


def test_in_requires_list(compiler):
    with pytest.raises(InvalidFilterError):
        compiler.compile_where({"status": {"$in": "open"}})


# -- $exists / $regex / $like -----------------------------------------------------------


@pytest.mark.parametrize(
    ("arg", "expected"),
    [(True, " WHERE `email` IS NOT NULL"), (False, " WHERE `email` IS NULL")],
)
def test_exists_translates_to_nullness(compiler, arg, expected):
    where, params = compiler.compile_where({"email": {"$exists": arg}})
    assert (where, params) == (expected, [])


def test_exists_requires_bool(compiler):
    with pytest.raises(InvalidFilterError):
        compiler.compile_where({"email": {"$exists": 1}})


def test_regex_emits_regexp_udf_call(compiler):
    where, params = compiler.compile_where({"city": {"$regex": "^Par"}})
    assert where == " WHERE `city` REGEXP ?"
    assert params == ["^Par"]


def test_like_passes_pattern_through(compiler):
    where, params = compiler.compile_where({"name": {"$like": "Ali%"}})
    assert where == " WHERE `name` LIKE ?"
    assert params == ["Ali%"]


# -- logical operators ------------------------------------------------------------------


def test_and_joins_subfilters(compiler):
    f = {"$and": [{"a": 1}, {"b": {"$gt": 2}}]}
    where, params = compiler.compile_where(f)
    assert where == " WHERE ((`a` = ?) AND (`b` > ?))"
    assert params == [1, 2]


def test_or_wraps_in_parens(compiler):
    where, _ = compiler.compile_where({"$or": [{"region": "UK"}, {"region": "EU"}]})
    assert where == " WHERE ((`region` = ?) OR (`region` = ?))"


def test_nor_negates_the_or(compiler):
    where, params = compiler.compile_where({"$nor": [{"a": 1}, {"b": 2}]})
    assert where == " WHERE (NOT ((`a` = ?) OR (`b` = ?)))"
    assert params == [1, 2]


def test_not_negates_a_subfilter(compiler):
    where, params = compiler.compile_where({"$not": {"status": "closed"}})
    assert where == " WHERE (NOT (`status` = ?))"
    assert params == ["closed"]


def test_worked_example_from_plan_section_2_3(compiler):
    filter_ = {
        "$and": [
            {"status": {"$in": ["open", "pending"]}},
            {"amount": {"$gte": 100, "$lt": 5000}},
            {"$or": [{"region": "UK"}, {"priority": {"$eq": "high"}}]},
        ]
    }
    where, params = compiler.compile_where(filter_)
    assert where == (
        " WHERE ((`status` IN (?, ?)) AND (`amount` >= ? AND `amount` < ?) "
        "AND ((`region` = ?) OR (`priority` = ?)))"
    )
    assert params == ["open", "pending", 100, 5000, "UK", "high"]


def test_logical_ops_require_lists(compiler):
    for op in ("$and", "$or", "$nor"):
        with pytest.raises(InvalidFilterError):
            compiler.compile_where({op: {"a": 1}})


def test_unknown_logical_operator_rejected(compiler):
    with pytest.raises(InvalidFilterError):
        compiler.compile_where({"$xor": [{"a": 1}]})


# -- identifier safety --------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["users; DROP TABLE users", "has space", "", "9start", 42])
def test_invalid_field_names_rejected(compiler, bad):
    with pytest.raises(InvalidFilterError):
        compiler.compile_where({bad: 1})


def test_mysql_dialect_swaps_placeholder():
    compiler = SqlCompiler(MysqlDialect)
    where, params = compiler.compile_where({"a": 1})
    assert where == " WHERE `a` = %s"
    assert params == [1]


# -- find / count / exists compilation ------------------------------------------------------


def test_compile_find_full_clause_order(compiler):
    q = compiler.compile_find("`t`", {"a": 1}, sort=[("b", -1)], limit=10, offset=20)
    assert q.sql == (
        "SELECT * FROM `t` WHERE `a` = ? ORDER BY `b` DESC LIMIT ? OFFSET ?"
    )
    assert q.params == [1, 10, 20]


def test_compile_find_offset_without_limit_uses_limit_minus_one(compiler):
    q = compiler.compile_find("`t`", None, sort=None, limit=None, offset=5)
    assert q.sql == "SELECT * FROM `t` LIMIT -1 OFFSET ?"
    assert q.params == [5]


def test_compile_find_one_is_limit_1(compiler):
    q = compiler.compile_find("`t`", {"id": 7}, sort=None, limit=1, offset=None)
    assert q.sql == "SELECT * FROM `t` WHERE `id` = ? LIMIT ?"
    assert q.params == [7, 1]


def test_sort_validates_direction(compiler):
    with pytest.raises(InvalidFilterError):
        compiler.compile_find("`t`", None, sort=[("a", 2)], limit=None, offset=None)


def test_count_sql(compiler):
    q = compiler.compile_count("`t`", {"a": 1})
    assert q.sql == "SELECT COUNT(*) FROM `t` WHERE `a` = ?"


def test_exists_short_circuits_with_limit_1(compiler):
    q = compiler.compile_exists("`t`", {})
    assert q.sql == "SELECT 1 FROM `t` LIMIT 1"
    assert q.params == []


# -- aggregate compilation -------------------------------------------------------------------


def test_aggregate_match_only_selects_documents(compiler):
    plan = compiler.compile_aggregate("`t`", [{"$match": {"a": 1}}, {"$limit": 3}])
    assert plan.sql == "SELECT * FROM `t` WHERE `a` = ? LIMIT ?"
    assert plan.params == [1, 3]
    assert not plan.grouped and plan.count_field is None


def test_group_by_field_with_accumulators(compiler):
    plan = compiler.compile_aggregate(
        "`orders`",
        [
            {"$match": {"amount": {"$gte": 100}}},
            {
                "$group": {
                    "_id": "$region",
                    "total": {"$sum": "$amount"},
                    "n": {"$count": {}},
                }
            },
            {"$sort": {"total": -1}},
        ],
    )
    assert plan.sql == (
        "SELECT `region` AS `_id`, SUM(`amount`) AS `total`, COUNT(*) AS `n` "
        "FROM `orders` WHERE `amount` >= ? GROUP BY `region` ORDER BY `total` DESC"
    )
    assert plan.params == [100]
    assert plan.grouped


def test_global_group_filters_phantom_row_on_empty_input(compiler):
    plan = compiler.compile_aggregate(
        "`t`", [{"$match": {"region": "ZZ"}}, {"$group": {"_id": None, "n": {"$sum": 1}}}]
    )
    assert "GROUP BY NULL HAVING COUNT(*) > 0" in plan.sql
    assert "NULL AS `_id`" in plan.sql


def test_composite_id_parts_alias_as_dotted_paths(compiler):
    plan = compiler.compile_aggregate(
        "`t`",
        [{"$group": {"_id": {"city": "$city", "state": "$state"}, "n": {"$max": "$v"}}}],
    )
    assert "`city` AS `_id.city`" in plan.sql
    assert "`state` AS `_id.state`" in plan.sql
    assert "GROUP BY `city`, `state`" in plan.sql


def test_constant_sum_becomes_parameterized_sum(compiler):
    plan = compiler.compile_aggregate("`t`", [{"$group": {"_id": None, "c": {"$sum": 5}}}])
    assert "SUM(?) AS `c`" in plan.sql
    assert plan.params == [5]


def test_count_stage_standalone_counts_matched_docs(compiler):
    plan = compiler.compile_aggregate("`t`", [{"$match": {"a": 1}}, {"$count": "docs"}])
    assert plan.sql == "SELECT COUNT(*) AS `docs` FROM `t` WHERE `a` = ?"
    assert plan.count_field == "docs"


def test_count_after_group_wraps_a_subquery(compiler):
    plan = compiler.compile_aggregate(
        "`t`",
        [{"$group": {"_id": "$g"}}, {"$sort": {"_id": 1}}, {"$limit": 4}, {"$count": "groups"}],
    )
    assert plan.sql == (
        "SELECT COUNT(*) AS `groups` FROM (SELECT `g` AS `_id` FROM `t` "
        "GROUP BY `g` ORDER BY `_id` ASC LIMIT ?)"
    )


def test_unsupported_stage_raises_unsupported_operation(compiler):
    with pytest.raises(UnsupportedOperationError):
        compiler.compile_aggregate("`t`", [{"$lookup": {"from": "other"}}])


def test_out_of_order_stage_raises_unsupported_operation(compiler):
    with pytest.raises(UnsupportedOperationError):
        compiler.compile_aggregate("`t`", [{"$limit": 2}, {"$match": {"a": 1}}])


def test_repeat_of_non_match_stage_raises(compiler):
    with pytest.raises(UnsupportedOperationError):
        compiler.compile_aggregate("`t`", [{"$limit": 1}, {"$limit": 2}])


def test_unsupported_accumulator_raises(compiler):
    with pytest.raises(UnsupportedOperationError):
        compiler.compile_aggregate("`t`", [{"$group": {"_id": None, "tags": {"$push": "$tag"}}}])


def test_group_without_id_key_raises_invalid_filter(compiler):
    with pytest.raises(InvalidFilterError):
        compiler.compile_aggregate("`t`", [{"$group": {"n": {"$sum": 1}}}])


def test_bad_limit_stage_raises_invalid_filter(compiler):
    with pytest.raises(InvalidFilterError):
        compiler.compile_aggregate("`t`", [{"$limit": 0}])


def test_grouped_sort_may_only_address_aliases(compiler):
    with pytest.raises(InvalidFilterError):
        compiler.compile_aggregate(
            "`t`",
            [
                {"$group": {"_id": "$g", "n": {"$sum": 1}}},
                {"$sort": {"raw_column": 1}},
            ],
        )


def test_multi_match_stages_are_anded(compiler):
    plan = compiler.compile_aggregate("`t`", [{"$match": {"a": 1}}, {"$match": {"b": 2}}])
    assert plan.sql == "SELECT * FROM `t` WHERE `a` = ? AND `b` = ?"
    assert plan.params == [1, 2]


def test_non_list_pipeline_rejected(compiler):
    with pytest.raises(InvalidFilterError):
        compiler.compile_aggregate("`t`", {"$match": {}})


# -- compile_update_set: §2.4 ---------------------------------------------------------


def test_update_set_compiles_assignments(compiler):
    set_sql, params = compiler.compile_update_set({"$set": {"status": "open", "n": 3}})
    assert set_sql == " SET `status` = ?, `n` = ?"
    assert params == ["open", 3]


def test_update_inc_compiles_to_self_addition(compiler):
    set_sql, params = compiler.compile_update_set({"$inc": {"counter": 2}})
    assert set_sql == " SET `counter` = `counter` + ?"
    assert params == [2]


def test_update_unset_compiles_to_null_and_ignores_value(compiler):
    for payload in (True, "", None, 1):
        assert compiler.compile_update_set({"$unset": {"note": payload}}) == (
            " SET `note` = NULL",
            [],
        )


def test_update_combined_operators_keep_order_and_params(compiler):
    update = {"$unset": {"note": True}, "$set": {"a": 1}, "$inc": {"b": 5}}
    set_sql, params = compiler.compile_update_set(update)
    assert set_sql == " SET `note` = NULL, `a` = ?, `b` = `b` + ?"
    assert params == [1, 5]


def test_update_empty_dict_and_empty_payloads_are_noops(compiler):
    assert compiler.compile_update_set({}) == ("", [])
    assert compiler.compile_update_set({"$set": {}}) == ("", [])


def test_update_push_is_unsupported(compiler):
    with pytest.raises(UnsupportedOperationError):
        compiler.compile_update_set({"$push": {"tags": "x"}})


def test_update_unknown_operator_rejected(compiler):
    with pytest.raises(InvalidFilterError):
        compiler.compile_update_set({"$rename": {"a": "b"}})


def test_update_bare_field_rejected_replacement_belongs_to_replace_one(compiler):
    with pytest.raises(InvalidFilterError):
        compiler.compile_update_set({"a": 1})


def test_update_non_dict_rejected(compiler):
    with pytest.raises(InvalidFilterError):
        compiler.compile_update_set("SET a = 1")


def test_update_operator_with_non_dict_payload_rejected(compiler):
    with pytest.raises(InvalidFilterError):
        compiler.compile_update_set({"$set": 3})


def test_update_inc_requires_numeric_argument(compiler):
    with pytest.raises(InvalidFilterError):
        compiler.compile_update_set({"$inc": {"n": "2"}})
    with pytest.raises(InvalidFilterError):  # bool is not a number here
        compiler.compile_update_set({"$inc": {"n": True}})


def test_update_column_in_two_operators_rejected(compiler):
    with pytest.raises(InvalidFilterError):
        compiler.compile_update_set({"$set": {"a": 1}, "$unset": {"a": True}})


def test_update_invalid_field_name_rejected(compiler):
    with pytest.raises(InvalidFilterError):
        compiler.compile_update_set({"$set": {"bad name": 1}})


def test_update_mysql_placeholder_style():
    set_sql, params = SqlCompiler(MysqlDialect).compile_update_set(
        {"$set": {"a": 1}, "$inc": {"b": 2}}
    )
    assert set_sql == " SET `a` = %s, `b` = `b` + %s"
    assert params == [1, 2]
