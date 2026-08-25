from __future__ import annotations

import pytest

from polydb.compilers.mongo_compiler import MongoCompiler, _like_to_regex
from polydb.exceptions import InvalidFilterError


@pytest.fixture
def compiler():
    return MongoCompiler()


# -- compile_filter: pass-through basics ------------------------------------------------


def test_none_and_empty_filter_match_all(compiler):
    assert compiler.compile_filter(None) == {}
    assert compiler.compile_filter({}) == {}


@pytest.mark.parametrize("bad", [["status"], "open", 42])
def test_non_dict_filters_rejected(compiler, bad):
    with pytest.raises(InvalidFilterError):
        compiler.compile_filter(bad)  # type: ignore[arg-type]


def test_scalar_shorthand_passes_through(compiler):
    assert compiler.compile_filter({"status": "open"}) == {"status": "open"}


def test_none_scalar_keeps_native_null_equality(compiler):
    assert compiler.compile_filter({"deleted_at": None}) == {"deleted_at": None}


def test_plain_filters_pass_through_unchanged(compiler):
    filter_ = {"region": "UK", "amount": {"$gte": 100}}
    assert compiler.compile_filter(filter_) == filter_


def test_worked_example_section_2_3_passes_through_unchanged(compiler):
    # §2.3 worked example contains no $like — the AST is already a valid
    # Mongo query, so normalization must not reshape it.
    filter_ = {
        "$and": [
            {"status": {"$in": ["open", "pending"]}},
            {"amount": {"$gte": 100, "$lt": 5000}},
            {"$or": [{"region": "UK"}, {"priority": {"$eq": "high"}}]},
        ]
    }
    assert compiler.compile_filter(filter_) == filter_


# -- comparison operators ----------------------------------------------------------------


@pytest.mark.parametrize("op", ["$eq", "$ne", "$gt", "$gte", "$lt", "$lte"])
def test_comparison_operators_pass_through(compiler, op):
    assert compiler.compile_filter({"age": {op: 21}}) == {"age": {op: 21}}


def test_eq_and_ne_accept_null_natively(compiler):
    assert compiler.compile_filter({"a": {"$eq": None}}) == {"a": {"$eq": None}}
    assert compiler.compile_filter({"a": {"$ne": None}}) == {"a": {"$ne": None}}


@pytest.mark.parametrize("op", ["$gt", "$gte", "$lt", "$lte"])
def test_range_operator_with_null_rejected(compiler, op):
    with pytest.raises(InvalidFilterError):
        compiler.compile_filter({"age": {op: None}})


@pytest.mark.parametrize("arg", [[1, 2], {"x": 1}])
def test_comparison_with_list_or_dict_rejected(compiler, arg):
    with pytest.raises(InvalidFilterError):
        compiler.compile_filter({"age": {"$eq": arg}})


def test_bare_list_value_rejected(compiler):
    with pytest.raises(InvalidFilterError):
        compiler.compile_filter({"tag": ["a", "b"]})


# -- $in / $nin / $exists / $regex --------------------------------------------------------


def test_in_and_nin_pass_through(compiler):
    filter_ = {"status": {"$in": ["open", "pending"]}, "age": {"$nin": [1, 2]}}
    assert compiler.compile_filter(filter_) == filter_


def test_empty_in_and_empty_nin_keep_native_semantics(compiler):
    # Mongo natively: $in [] matches nothing, $nin [] matches everything.
    assert compiler.compile_filter({"s": {"$in": []}}) == {"s": {"$in": []}}
    assert compiler.compile_filter({"s": {"$nin": []}}) == {"s": {"$nin": []}}


def test_in_requires_list(compiler):
    with pytest.raises(InvalidFilterError):
        compiler.compile_filter({"status": {"$in": "open"}})


def test_exists_passes_through_bools(compiler):
    assert compiler.compile_filter({"note": {"$exists": True}}) == {
        "note": {"$exists": True}
    }
    assert compiler.compile_filter({"note": {"$exists": False}}) == {
        "note": {"$exists": False}
    }


def test_exists_requires_bool(compiler):
    with pytest.raises(InvalidFilterError):
        compiler.compile_filter({"note": {"$exists": 1}})


def test_regex_passes_through_pattern_strings(compiler):
    assert compiler.compile_filter({"city": {"$regex": "^L"}}) == {
        "city": {"$regex": "^L"}
    }


def test_regex_requires_string(compiler):
    with pytest.raises(InvalidFilterError):
        compiler.compile_filter({"city": {"$regex": 42}})


# -- $like: the one operator Mongo has no native form for (§2.2) ---------------------------


def test_like_to_regex_wildcard_translation():
    assert _like_to_regex("London") == r"^London$"
    assert _like_to_regex("%on%") == r"^.*on.*$"
    assert _like_to_regex("L_ndo_") == r"^L.ndo.$"
    assert _like_to_regex("") == r"^$"


def test_like_to_regex_escapes_metacharacters():
    # Everything that is regex-special must survive as a literal: LIKE never
    # treats . + ( ) etc. as wildcards.
    assert _like_to_regex("50%") == r"^50.*$"
    assert _like_to_regex("a.b+c(d)") == r"^a\.b\+c\(d\)$"


def test_like_becomes_expr_regex_match(compiler):
    compiled = compiler.compile_filter({"city": {"$like": "%on%"}})
    assert compiled == {
        "$expr": {"$regexMatch": {"input": "$city", "regex": r"^.*on.*$"}}
    }


def test_like_requires_string(compiler):
    with pytest.raises(InvalidFilterError):
        compiler.compile_filter({"city": {"$like": 42}})


def test_like_inside_logical_subfilter_is_normalized(compiler):
    compiled = compiler.compile_filter(
        {"$or": [{"city": {"$like": "%on%"}}, {"region": "UK"}]}
    )
    assert compiled == {
        "$or": [
            {"$expr": {"$regexMatch": {"input": "$city", "regex": r"^.*on.*$"}}},
            {"region": "UK"},
        ]
    }


def test_like_combined_with_sibling_operator_splits_into_and(compiler):
    # An operator dict cannot host a bare "$expr" key, so the field's native
    # operators and its regex clause are ANDed explicitly.
    compiled = compiler.compile_filter({"city": {"$like": "L%", "$ne": "Leeds"}})
    assert compiled == {
        "$and": [
            {"city": {"$ne": "Leeds"}},
            {"$expr": {"$regexMatch": {"input": "$city", "regex": r"^L.*$"}}},
        ]
    }


def test_two_like_fields_wrap_in_explicit_and(compiler):
    # Two $expr clauses cannot share the top-level "$expr" key.
    compiled = compiler.compile_filter({"a": {"$like": "x%"}, "b": {"$like": "%y"}})
    assert compiled == {
        "$and": [
            {"$expr": {"$regexMatch": {"input": "$a", "regex": r"^x.*$"}}},
            {"$expr": {"$regexMatch": {"input": "$b", "regex": r"^.*y$"}}},
        ]
    }


def test_like_plus_plain_fields_stay_single_document_where_possible(compiler):
    compiled = compiler.compile_filter({"a": {"$like": "x"}, "b": 1})
    assert set(compiled.keys()) == {"$and"}


# -- logical operators ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filter_", "expected"),
    [
        ({"$and": [{"a": 1}, {"b": 2}]}, {"$and": [{"a": 1}, {"b": 2}]}),
        ({"$or": [{"a": 1}]}, {"$or": [{"a": 1}]}),
        ({"$nor": [{"a": 1}, {"b": 2}]}, {"$nor": [{"a": 1}, {"b": 2}]}),
    ],
)
def test_list_logical_operators_pass_through(compiler, filter_, expected):
    assert compiler.compile_filter(filter_) == expected


def test_not_is_rewritten_as_single_element_nor(compiler):
    # Mongo only allows $not as a field-level operator; a standalone negation
    # must surface as $nor over one element.
    assert compiler.compile_filter({"$not": {"region": "UK"}}) == {
        "$nor": [{"region": "UK"}]
    }


def test_nested_not_compiles_to_nested_nor(compiler):
    assert compiler.compile_filter({"$not": {"$not": {"a": 1}}}) == {
        "$nor": [{"$nor": [{"a": 1}]}]
    }


def test_not_of_like_negates_the_regex_clause(compiler):
    compiled = compiler.compile_filter({"$not": {"city": {"$like": "%on%"}}})
    assert compiled == {
        "$nor": [{"$expr": {"$regexMatch": {"input": "$city", "regex": r"^.*on.*$"}}}]
    }


def test_empty_subfilter_stays_empty_native_match_everything(compiler):
    # {} matches everything natively in Mongo — no 1=1-style literal needed,
    # and negation semantics fall out of $nor/$not on their own.
    assert compiler.compile_filter({"$and": [{}]}) == {"$and": [{}]}
    assert compiler.compile_filter({"$or": [{}, {"a": 1}]}) == {"$or": [{}, {"a": 1}]}


def test_logical_ops_require_lists(compiler):
    for op in ("$and", "$or", "$nor"):
        with pytest.raises(InvalidFilterError):
            compiler.compile_filter({op: {"a": 1}})
        with pytest.raises(InvalidFilterError):
            compiler.compile_filter({op: []})


def test_logical_entries_must_be_dicts(compiler):
    with pytest.raises(InvalidFilterError):
        compiler.compile_filter({"$or": ["open"]})


def test_not_requires_a_filter_dict(compiler):
    with pytest.raises(InvalidFilterError):
        compiler.compile_filter({"$not": [{"a": 1}]})


def test_unknown_dollar_key_rejected(compiler):
    with pytest.raises(InvalidFilterError):
        compiler.compile_filter({"$where": "this.a > 1"})
    with pytest.raises(InvalidFilterError):
        compiler.compile_filter({"$expr": {"$gt": ["$a", 1]}})


# -- grammar guards shared with the SQL leg --------------------------------------------------


def test_empty_operator_dict_rejected(compiler):
    with pytest.raises(InvalidFilterError):
        compiler.compile_filter({"age": {}})


@pytest.mark.parametrize("bad", ["bad name; DROP TABLE users", "a.b", "", "9lives"])
def test_invalid_field_names_rejected(compiler, bad):
    with pytest.raises(InvalidFilterError):
        compiler.compile_filter({bad: 1})


# -- compile_update (§2.4) --------------------------------------------------------------------


def test_update_all_four_operators_pass_through(compiler):
    update = {
        "$set": {"status": "open"},
        "$inc": {"views": 1},
        "$unset": {"note": ""},
        "$push": {"tags": "new"},
    }
    assert compiler.compile_update(update) is update


def test_update_push_is_native_on_mongo_unlike_sql(compiler):
    update = {"$push": {"tags": "new"}}
    assert compiler.compile_update(update) == update


def test_update_non_dict_rejected(compiler):
    with pytest.raises(InvalidFilterError):
        compiler.compile_update([("$set", {"a": 1})])  # type: ignore[arg-type]


def test_update_bare_field_rejected_replacement_belongs_to_replace_one(compiler):
    with pytest.raises(InvalidFilterError):
        compiler.compile_update({"visits": 5})


def test_update_unknown_operator_rejected(compiler):
    with pytest.raises(InvalidFilterError):
        compiler.compile_update({"$pullAll": {"tags": ["x"]}})


def test_update_operator_payload_must_be_a_dict(compiler):
    with pytest.raises(InvalidFilterError):
        compiler.compile_update({"$set": [("a", 1)]})


@pytest.mark.parametrize("value", [True, "3", None])
def test_update_inc_requires_numeric_argument(compiler, value):
    with pytest.raises(InvalidFilterError):
        compiler.compile_update({"$inc": {"views": value}})


def test_update_inc_accepts_int_and_float(compiler):
    compiler.compile_update({"$inc": {"a": 1, "b": -1.5}})


def test_update_column_in_two_operators_rejected(compiler):
    with pytest.raises(InvalidFilterError):
        compiler.compile_update({"$set": {"a": 1}, "$unset": {"a": ""}})


def test_update_invalid_field_name_rejected(compiler):
    with pytest.raises(InvalidFilterError):
        compiler.compile_update({"$set": {"bad name": 1}})
