# Filter / Query DSL spec

Implements planning-doc §2. The DSL surface is Mongo-shaped because Mongo's
operator dict is already a clean, serializable AST: the SQL family adapter
compiles that AST into fully parameterized SQL via `polydb/compilers/sql_compiler.py`,
while Mongo's adapter mostly passes it straight to the driver.

Every method taking a `filter` argument (`find_one`, `find`, `count`, `exists`,
`update_one`, `update_many`, `replace_one`, and `$match` stages inside
`aggregate`) accepts the same grammar.

## Grammar

```
filter        := {} | { field_expr (, field_expr)* }
field_expr    := field ":" (scalar | operator_expr | logical_expr)
operator_expr := { operator ":" scalar (, operator ":" scalar)* }
logical_expr  := "$and" | "$or" | "$nor" : [ filter (, filter)* ]
              |  "$not" : filter
scalar        := str | int | float | bool | null | list  (list only valid with $in/$nin)
```

Plain `{"status": "open"}` is shorthand for `{"status": {"$eq": "open"}}`.
An empty filter (`{}` or `None`) matches every row.

## Comparison / matching operators

| Operator | Meaning | SQL translation |
| --- | --- | --- |
| `$eq` | equals | `field = ?` — a `None` argument compiles to `IS NULL` |
| `$ne` | not equals | `field <> ?` — a `None` argument compiles to `IS NOT NULL` |
| `$gt` / `$gte` / `$lt` / `$lte` | ordered comparisons | `>`, `>=`, `<`, `<=` |
| `$in` | value in list | `field IN (?, ?, ...)` — empty list matches **nothing** (Mongo semantics) |
| `$nin` | value not in list | `field NOT IN (?, ?, ...)` — empty list matches **everything** |
| `$exists` | field present / not present | `field IS NOT NULL` / `field IS NULL`; requires a bool |
| `$regex` | pattern match (`re.search` semantics) | `field REGEXP ?` — SQLite resolves it through the `REGEXP` user function registered at `connect()` time; MySQL has `REGEXP` natively |
| `$like` | SQL-style wildcard match (`%`, `_`) | `field LIKE ?` |

Rules enforced by the compiler:

- Multiple operators on one field AND together:
  `{"amount": {"$gte": 100, "$lt": 5000}}` → `(amount >= ? AND amount < ?)`.
- `$gt`-style operators require scalar arguments; lists are only valid as the
  argument of `$in`/`$nin`.
- Only `$eq`/`$ne` accept `null`. Other comparison operators raise
  `InvalidFilterError` — use `$exists` to test for missing fields.
- A bare list value (`{"tags": ["a", "b"]}`) raises `InvalidFilterError`.

## Logical operators

```python
{
    "$and": [
        {"status": {"$in": ["open", "pending"]}},
        {"amount": {"$gte": 100, "$lt": 5000}},
        {"$or": [{"region": "UK"}, {"priority": {"$eq": "high"}}]},
    ]
}
```

**SQL family compiled output** (SQLite dialect shown):

```sql
WHERE (`status` IN (?, ?) AND (`amount` >= ? AND `amount` < ?) AND ((`region` = ?) OR (`priority` = ?)))
-- params: ["open", "pending", 100, 5000, "UK", "high"]
```

- Top-level keys of one filter dict AND together.
- `$and` / `$or` / `$nor` take a non-empty list of filter dicts; `$not` takes a
  single filter dict.
- `$nor` compiles to `NOT ((cond) OR (cond))` — none of the sub-filters match.

## Safety guarantees

- The compiler always emits **parameterized** SQL — user values never touch the
  SQL string.
- Field/table names are validated against `^[A-Za-z_][A-Za-z0-9_]*$` and
  identifier-quoted before being placed in the query, closing the injection
  door on the column-name side. Violations raise `InvalidFilterError`.
- SQLite quotes identifiers with **backticks**, not double quotes: a
  double-quoted token that doesn't resolve to a column silently degrades to a
  string literal in SQLite (legacy DQS misfeature), so `WHERE "nope" = 'x'`
  would match *every* row instead of raising. Backticks always raise
  `no such column`, surfacing typos as `PolydbQueryError` rather than silently
  wrong results.

Malformed filters raise `InvalidFilterError`; operations with no translation for
the backend (e.g. `$push`, unsupported aggregate stages) raise
`UnsupportedOperationError`.

## Update operators (`update_one` / `update_many`)

Updates are operator dicts, never bare replacement documents (use
`replace_one()` for wholesale replacement).

| Operator | Meaning | SQL translation |
| --- | --- | --- |
| `$set` | set field(s) to a value | `SET field = ?` |
| `$unset` | clear field(s); payload value is ignored | `SET field = NULL` |
| `$inc` | increment numeric field | `SET field = field + ?` — requires an int/float argument |
| `$push` | append to array field | ❌ raises `UnsupportedOperationError` — no portable array-append in relational SQL without a JSON-column convention |

Constraints:

- Bare field keys (`{"visits": 5}` instead of `{"$set": {"visits": 5}}`) raise
  `InvalidFilterError` pointing at `replace_one()`.
- One column may not be assigned by two different operators in one update.
- An update whose SET clause compiles empty (e.g. `{}`) still reports an honest
  `matched_count` with `modified_count=0`.

Known divergence from Mongo (documented, not hidden): `update_many` takes both
counts from the statement's rowcount, so rows whose values were already equal
still count as modified.

## Sort / limit / offset (`find`)

- `sort` is a list of `(field, direction)` pairs, direction `1` (asc) or `-1`
  (desc); later pairs break ties of earlier ones → `ORDER BY f1 ASC, f2 DESC`.
- `limit` / `offset` must be non-negative ints. Offset without limit compiles
  `LIMIT -1 OFFSET n` (SQL requires a LIMIT before OFFSET).

## Aggregation subset (`aggregate`)

Supported stages, in canonical order — `$match` is the only repeatable stage,
everything else at most once:

```
$match* → $group? → $sort? → $limit? → $count?
```

- `$match`: any filter from this spec; repeatable, ANDed into the WHERE clause.
- `$group`: `_id` may be `null` (one global group), a `"$field"` reference
  (scalar `_id`), or a non-empty dict of name → `"$field"` references
  (composite groups come back Mongo-shaped with a nested `_id` dict).
  Accumulators: `$sum`, `$avg`, `$min`, `$max` over a `"$field"` reference or
  numeric constant (e.g. `{"total": {"$sum": 1}}` counts rows), plus `$count`
  (`{}`, `1`, or `"*"`).
- `$sort`: after `$group`, addressable keys are `"_id"`, `"_id.<part>"` paths,
  and accumulator output aliases.
- `$limit`: positive int.
- `$count`: output field name; standalone it yields `[{"<name>": n}]`.

Anything outside the subset (unknown/out-of-order stages, other accumulators,
other `_id` shapes) raises `UnsupportedOperationError`. Malformed stage payloads
raise `InvalidFilterError`.

Mongo-compatibility notes:

- A global `_id: null` group over zero input rows returns `[]`, not SQL's
  phantom `NULL` row.
- Known divergence: `NULL` handling inside `MIN`/`MAX`/`AVG` follows SQL
  semantics (NULLs ignored) rather than Mongo's missing-field semantics.
