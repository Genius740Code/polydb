from __future__ import annotations

import logging
from typing import Any, NoReturn
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from polydb.base import BaseAdapter, Transaction
from polydb.compilers.mongo_compiler import MongoCompiler
from polydb.compilers.sql_compiler import validate_identifier
from polydb.exceptions import (
    ConnectionNotOpenError,
    InvalidFilterError,
    PolydbQueryError,
    TransactionInactiveError,
    TransactionsUnavailableError,
    UnsupportedOperationError,
)
from polydb.results import DeleteResult, InsertManyResult, InsertResult, UpdateResult, UpsertResult
from polydb.schema import FieldType, Schema

logger = logging.getLogger("polydb.adapters.mongo")

_validate_identifier = validate_identifier


def _strip_polydb_params(url: str) -> str:
    """Remove polydb-specific query params (pool_size, timeout) before handing URL to motor.

    Motor/pymongo would see them as unknown options. We already validated them via
    url_parser, so stripping is safe.
    """
    parts = urlsplit(url)
    if not parts.query:
        return url
    qs = parse_qs(parts.query, keep_blank_values=True)
    # Remove polydb knobs
    qs.pop("pool_size", None)
    qs.pop("timeout", None)
    new_query = urlencode(qs, doseq=True)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


class MongoTransaction(Transaction):
    """MongoDB Transaction (§1.7 #25–#26) backed by a motor ClientSession.

    One explicit session + transaction block. Entering the context manager
    does ``await client.start_session()`` + ``session.start_transaction()``;
    while it is open every operation routed through this object — and any call
    made directly on the adapter, which shares the same session — executes
    inside the transaction via the ``session`` kwarg.

    A failed statement inside the transaction aborts the whole thing: the
    transaction is aborted immediately and the handle is marked aborted,
    so subsequent calls through it raise ``TransactionInactiveError`` rather
    than silently continuing. This mirrors the poisoned-transaction precedent
    from the SQL family / Postgres legs.

    State machine: ``new → active → committed | rolled_back | aborted``.
    """

    def __init__(self, adapter: MongoAdapter) -> None:
        self._adapter = adapter
        self._state = "new"
        self._session: Any = None  # motor ClientSession

    async def __aenter__(self) -> Transaction:
        if self._state != "new":
            raise TransactionInactiveError(
                f"this Transaction was already used (state: {self._state}); "
                f"call db.transaction() again for a fresh one"
            )
        if self._adapter._tx is not None:
            raise UnsupportedOperationError(
                "nested/concurrent transactions are not supported: this "
                "adapter already has an open transaction"
            )
        if not self._adapter._connected or self._adapter._client is None:
            raise ConnectionNotOpenError("MongoAdapter is not connected")

        # Try to start a session; if topology doesn't support it, surface as
        # TransactionsUnavailableError (§1.7 🟡).
        try:
            # motor's start_session is async (AsyncCommand)
            session = await self._adapter._client.start_session()
        except Exception as err:
            raise TransactionsUnavailableError(
                f"MongoDB transactions are unavailable (could not start session): {err}"
            ) from err

        try:
            # start_transaction is sync on motor (returns a context), not async
            session.start_transaction()
        except Exception as err:
            # Cleanup session
            try:
                await session.end_session()
            except Exception:
                pass
            # Transactions require replica set / Atlas; map InvalidOperation etc.
            raise TransactionsUnavailableError(
                f"MongoDB transactions are unavailable (could not start transaction — "
                f"requires replica set / Atlas): {err}"
            ) from err

        self._session = session
        self._state = "active"
        self._adapter._tx = self
        self._adapter._session = session
        return self

    async def commit(self) -> None:
        """Explicitly commit the transaction."""
        if self._state != "active":
            raise TransactionInactiveError(
                f"commit() requires an active transaction; current state is {self._state!r}"
            )
        try:
            await self._session.commit_transaction()
        finally:
            try:
                await self._session.end_session()
            except Exception as cleanup_error:  # pragma: no cover - defensive
                logger.warning("end_session after commit failed: %r", cleanup_error)
            self._finish("committed")

    async def rollback(self) -> None:
        """Explicitly roll back the transaction."""
        if self._state != "active":
            raise TransactionInactiveError(
                f"rollback() requires an active transaction; current state is {self._state!r}"
            )
        try:
            await self._session.abort_transaction()
        finally:
            try:
                await self._session.end_session()
            except Exception as cleanup_error:  # pragma: no cover - defensive
                logger.warning("end_session after abort failed: %r", cleanup_error)
            self._finish("rolled_back")

    def _finish(self, state: str) -> None:
        self._state = state
        if self._adapter._tx is self:
            self._adapter._tx = None
            self._adapter._session = None
        self._session = None

    async def abort(self) -> None:
        """Abort the transaction after a failed statement inside it.

        Called by MongoAdapter._rollback_and_raise which has already decided
        the transaction must die. Marks aborted and releases the session.
        """
        if self._state != "active":
            return
        self._state = "aborted"
        if self._adapter._tx is self:
            self._adapter._tx = None
            self._adapter._session = None
        # Try to abort the server-side transaction
        if self._session is not None:
            try:
                await self._session.abort_transaction()
            except Exception:
                pass
            try:
                await self._session.end_session()
            except Exception:
                pass
        self._session = None

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        if self.__dict__.get("_state") != "active":
            raise TransactionInactiveError(
                f"{name}() requires an active transaction — enter it first "
                f"via `async with db.transaction() as tx:` (current state: "
                f"{self.__dict__.get('_state', 'new')!r})"
            )
        return getattr(self._adapter, name)

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._state != "active":
            return
        if exc_type is None:
            await self.commit()
            return
        try:
            await self.rollback()
        except Exception as cleanup_error:  # pragma: no cover - defensive
            logger.warning(
                "Ignoring rollback failure while handling an error in the "
                "transaction body: %s",
                cleanup_error,
            )


class MongoAdapter(BaseAdapter):
    """MongoDB adapter (backed by ``motor``).

    Uses :class:`polydb.compilers.mongo_compiler.MongoCompiler` for DSL
    validation/normalization (``$like`` → ``$expr``+``$regexMatch``, standalone
    ``$not`` → ``$nor``). All other operators pass through natively.
    """

    def __init__(self, config: Any) -> None:
        super().__init__(config)
        self._compiler = MongoCompiler()
        self._client: Any = None  # motor.motor_asyncio.AsyncIOMotorClient
        self._db: Any = None  # motor.motor_asyncio.AsyncIOMotorDatabase
        self._tx: MongoTransaction | None = None
        self._session: Any = None
        # Database name: from URL path, default to 'test' for schemaless usage
        # if not provided (motor requires a name for get_database).
        self._database_name: str = config.database or "test"

    # -- helpers ---------------------------------------------------------------

    def _validated_collection(self, collection: str) -> str:
        """Validate collection name and return it.

        Mongo allows many collection names, but polydb restricts to the same
        DSL identifier pattern used for SQL tables to keep cross-backend code
        portable and to prevent injection of $-operators via collection names.
        """
        return _validate_identifier(collection, "table")

    def _validated_fields(self, names: Any) -> list[str]:
        if not isinstance(names, (list, tuple)):
            raise InvalidFilterError(f"fields must be a list, got {type(names).__name__}")
        return [_validate_identifier(n, "column") for n in names]

    def _get_session(self) -> Any | None:
        """Return the active session for transactional operations, if any."""
        if self._tx is not None and getattr(self._tx, "_state", None) == "active":
            return self._tx._session
        return None

    def _derived_index_name(self, raw_table: str, columns: list[str], unique: bool) -> str:
        prefix = "uq" if unique else "idx"
        joined = "__".join(columns)
        return f"{prefix}_{raw_table}__{joined}"

    @staticmethod
    def _validate_sort(sort: Any) -> list[tuple[str, int]] | None:
        if sort is None:
            return None
        if not isinstance(sort, (list, tuple)):
            raise InvalidFilterError(f"sort must be a list of (field, direction) tuples, got {type(sort).__name__}")
        validated: list[tuple[str, int]] = []
        for entry in sort:
            if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                raise InvalidFilterError(f"sort entries must be (field, direction) pairs, got {entry!r}")
            name, direction = entry
            if direction not in (1, -1):
                raise InvalidFilterError(f"sort direction must be 1 (asc) or -1 (desc), got {direction!r} for {name!r}")
            _validate_identifier(name, "column")
            validated.append((name, direction))
        return validated

    @staticmethod
    def _validate_paging(name: str, value: Any) -> None:
        if value is None:
            return
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise InvalidFilterError(f"{name} must be a non-negative int, got {value!r}")

    async def _rollback_and_raise(
        self, operation: str, collection: str, err: Exception
    ) -> NoReturn:
        """Abort open transaction (poisoned) and re-raise as PolydbQueryError."""
        if self._tx is not None:
            logger.info("Aborting open transaction after failed %s on %r", operation, collection)
            try:
                await self._tx.abort()
            except Exception as abort_error:  # pragma: no cover - defensive
                logger.warning("abort after failed %s also failed: %r", operation, abort_error)
        # Don't double-wrap PolydbError subclasses — they already carry correct type
        if isinstance(err, (InvalidFilterError, UnsupportedOperationError, TransactionInactiveError, TransactionsUnavailableError)):
            raise err
        raise PolydbQueryError(f"mongo {operation} failed on collection {collection!r}: {err}") from err

    # -- 1.1 Connection management ---------------------------------------------

    async def connect(self) -> None:
        """Open the underlying client. Idempotent — calling twice is a no-op."""
        if self._connected:
            return
        try:
            import motor.motor_asyncio
        except ImportError as err:
            raise ImportError(
                'motor is required for MongoAdapter: pip install "genius74o-polydb[mongo]"'
            ) from err

        # Build a clean URL without polydb-specific params
        clean_url = _strip_polydb_params(self.config.raw_url)

        # motor options from polydb's generic pool_size/timeout knobs
        # serverSelectionTimeoutMS is the cheap health-check timeout used by ping()
        kwargs: dict[str, Any] = {
            "maxPoolSize": self.config.pool_size,
            "serverSelectionTimeoutMS": int(self.config.timeout * 1000),
            "connectTimeoutMS": int(self.config.timeout * 1000),
            "socketTimeoutMS": int(self.config.timeout * 1000),
        }
        # Pass through any extra options (e.g. replicaSet, ssl) from URL query
        # that url_parser kept verbatim.
        for key, value in self.config.options.items():
            if key not in kwargs:
                # Motor/pymongo expects specific option names; pass as-is and let it validate
                kwargs[key] = value

        try:
            # Motor's AsyncIOMotorClient is lazy — it won't connect until first op,
            # but we create it now.
            # Use clean_url as first arg (URI) plus kwargs overrides.
            self._client = motor.motor_asyncio.AsyncIOMotorClient(clean_url, **kwargs)
            # Resolve database handle
            # If database is specified in URI, get_default_database would pick it up,
            # but we force our parsed name for determinism (covers :memory:-like edge).
            self._db = self._client[self._database_name]
        except Exception as err:
            raise PolydbQueryError(f"mongo connect failed: {err}") from err

        self._connected = True

    async def disconnect(self) -> None:
        client, self._client = self._client, None
        self._db = None
        self._connected = False
        # Abort any open transaction handle without raising
        tx, self._tx = self._tx, None
        self._session = None
        if tx is not None and getattr(tx, "_state", None) == "active":
            try:
                await tx.abort()
            except Exception:
                pass
        if client is not None:
            try:
                client.close()
            except Exception:  # pragma: no cover - defensive
                pass

    async def ping(self) -> bool:
        """Cheap round-trip health check: ``db.command('ping')``."""
        self._ensure_connected()
        try:
            # Use admin ping if available, fallback to db ping
            if self._client is not None:
                try:
                    await self._client.admin.command("ping")
                except Exception:
                    # Some deployments restrict admin; try database ping
                    await self._db.command("ping")
            else:
                await self._db.command("ping")
        except Exception as err:
            logger.warning("ping() round-trip failed on mongo: %r", err)
            return False
        return True

    # -- 1.2 Create ------------------------------------------------------------

    async def insert_one(self, collection: str, doc: dict[str, Any]) -> InsertResult:
        """Insert a single document. See BaseAdapter.insert_one."""
        self._ensure_connected()  # 1. guard
        coll_name = self._validated_collection(collection)
        # 2. translate — validate field names
        if not isinstance(doc, dict):
            raise InvalidFilterError(f"doc must be a dict, got {type(doc).__name__}")
        for key in doc:
            _validate_identifier(key, "column")
        # 3. execute
        try:
            coll = self._db[coll_name]
            # Motor mutates doc to add _id; copy to avoid caller side-effect
            doc_copy = dict(doc)
            session = self._get_session()
            if session is not None:
                result = await coll.insert_one(doc_copy, session=session)
            else:
                result = await coll.insert_one(doc_copy)
            inserted_id = result.inserted_id
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("insert_one", collection, err)
        return InsertResult(inserted_id=inserted_id)  # 5. result type

    async def insert_many(
        self, collection: str, docs: list[dict[str, Any]]
    ) -> InsertManyResult:
        """Bulk insert. See BaseAdapter.insert_many."""
        self._ensure_connected()  # 1. guard
        if not docs:
            return InsertManyResult(inserted_ids=[], inserted_count=0)
        if not isinstance(docs, list):
            raise InvalidFilterError(f"docs must be a list, got {type(docs).__name__}")
        coll_name = self._validated_collection(collection)
        # 2. translate — validate each doc field names
        for doc in docs:
            if not isinstance(doc, dict):
                raise InvalidFilterError(f"each doc must be a dict, got {type(doc).__name__}")
            for key in doc:
                _validate_identifier(key, "column")
        # 3. execute
        try:
            coll = self._db[coll_name]
            # Copy docs to avoid mutation side-effects
            docs_copy = [dict(d) for d in docs]
            session = self._get_session()
            if session is not None:
                result = await coll.insert_many(docs_copy, session=session)
            else:
                result = await coll.insert_many(docs_copy)
            inserted_ids = list(result.inserted_ids)
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("insert_many", collection, err)
        return InsertManyResult(inserted_ids=inserted_ids, inserted_count=len(inserted_ids))  # 5.

    async def upsert_one(
        self, collection: str, filter: dict[str, Any], doc: dict[str, Any]
    ) -> UpsertResult:
        """Insert-or-update by filter match. See BaseAdapter.upsert_one.

        Follows Mongo-style semantics: first match gets its ``doc`` fields
        updated via ``$set``; no match inserts a new document built from
        filter merged under doc (doc wins on key conflicts). This keeps
        behavior identical to the SQL legs for arbitrary filters.
        """
        self._ensure_connected()  # 1. guard
        coll_name = self._validated_collection(collection)
        # 2. translate — validate filter is plain equality (no operators/lists)
        if not isinstance(filter, dict):
            raise InvalidFilterError(f"filter must be a dict, got {type(filter).__name__}")
        if not isinstance(doc, dict):
            raise InvalidFilterError(f"doc must be a dict, got {type(doc).__name__}")
        for key, value in filter.items():
            _validate_identifier(key, "column")
            if isinstance(value, (dict, list)):
                raise InvalidFilterError(
                    f"upsert_one filter values must be plain scalars; got {type(value).__name__} for {key!r}"
                )
        for key in doc:
            _validate_identifier(key, "column")
        # Compile filter for Mongo (handles validation + pass-through)
        mongo_filter = self._compiler.compile_filter(filter)  # 2. translate
        # Also validate doc via compiler's update validation? Not needed; doc is plain.

        try:  # 3. execute
            coll = self._db[coll_name]
            session = self._get_session()
            # Find first match
            if session is not None:
                matched_doc = await coll.find_one(mongo_filter, session=session)
            else:
                matched_doc = await coll.find_one(mongo_filter)

            if matched_doc is not None:
                modified_count = 0
                if doc:
                    # Update first match by _id
                    update = {"$set": doc}
                    # Validate via compiler
                    self._compiler.compile_update(update)
                    if session is not None:
                        result = await coll.update_one(
                            {"_id": matched_doc["_id"]}, update, session=session
                        )
                    else:
                        result = await coll.update_one({"_id": matched_doc["_id"]}, update)
                    # For parity with SQL legs, a matched doc with non-empty doc counts as modified=1
                    # even if values were equal; but Mongo reports modified_count accurately.
                    # We keep Mongo's native count for correctness, except empty doc case.
                    # To match SQL's "matched=1 modified=1" for non-empty, we ensure 1 when update sent.
                    # Use result.modified_count but ensure 1 if we sent an update and matched.
                    # However to be precise, mirror SQL's behavior: if we sent an update, report 1.
                    # Let's follow native: if result.matched_count==1, modified is as reported,
                    # but for empty doc we already handle.
                    # For consistency with existing contract tests that expect modified=1 when
                    # updating with same values? SQL reports 1, Mongo reports 0. We could normalize
                    # to 1 to match SQL. The plan notes known divergence for update_many but
                    # for upsert_one the SQL leg reports modified_count=1 on update path.
                    # To keep parity, we report 1 when doc non-empty and matched.
                    modified_count = 1
                # No explicit commit needed; transaction handles it.
                return UpsertResult(matched_count=1, modified_count=modified_count)
            # Insert path: merged filter + doc, doc wins
            merged: dict[str, Any] = {**filter, **doc}
            for key in merged:
                _validate_identifier(key, "column")
            merged_copy = dict(merged)
            if session is not None:
                result = await coll.insert_one(merged_copy, session=session)
            else:
                result = await coll.insert_one(merged_copy)
            upserted_id = result.inserted_id
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("upsert_one", collection, err)
        return UpsertResult(matched_count=0, modified_count=0, upserted_id=upserted_id)  # 5.

    # -- 1.3 Read --------------------------------------------------------------

    async def find_one(
        self, collection: str, filter: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Fetch the first document matching ``filter``. See BaseAdapter.find_one."""
        self._ensure_connected()  # 1. guard
        coll_name = self._validated_collection(collection)
        mongo_filter = self._compiler.compile_filter(filter)  # 2. translate
        try:  # 3. execute
            coll = self._db[coll_name]
            session = self._get_session()
            if session is not None:
                doc = await coll.find_one(mongo_filter, session=session)
            else:
                doc = await coll.find_one(mongo_filter)
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("find_one", collection, err)
        return dict(doc) if doc is not None else None  # 5. normalize + return

    async def find(
        self,
        collection: str,
        filter: dict[str, Any] | None = None,
        *,
        sort: list[tuple[str, int]] | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch every document matching ``filter``. See BaseAdapter.find."""
        self._ensure_connected()  # 1. guard
        coll_name = self._validated_collection(collection)
        mongo_filter = self._compiler.compile_filter(filter)  # 2. translate
        validated_sort = self._validate_sort(sort)
        self._validate_paging("limit", limit)
        self._validate_paging("offset", offset)
        try:  # 3. execute
            coll = self._db[coll_name]
            session = self._get_session()
            if session is not None:
                cursor = coll.find(mongo_filter, session=session)
            else:
                cursor = coll.find(mongo_filter)
            if validated_sort:
                cursor = cursor.sort(validated_sort)
            if offset is not None:
                cursor = cursor.skip(offset)
            if limit is not None:
                cursor = cursor.limit(limit)
            # to_list with no limit fetches all
            docs = await cursor.to_list(length=None)
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("find", collection, err)
        return [dict(d) for d in docs]  # 5. normalize + return

    async def count(self, collection: str, filter: dict[str, Any] | None = None) -> int:
        """Count documents matching ``filter``. See BaseAdapter.count."""
        self._ensure_connected()  # 1. guard
        coll_name = self._validated_collection(collection)
        mongo_filter = self._compiler.compile_filter(filter)  # 2. translate
        try:  # 3. execute
            coll = self._db[coll_name]
            session = self._get_session()
            if session is not None:
                n = await coll.count_documents(mongo_filter, session=session)
            else:
                n = await coll.count_documents(mongo_filter)
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("count", collection, err)
        return int(n)  # 5. normalize + return

    async def exists(self, collection: str, filter: dict[str, Any]) -> bool:
        """Existence check. See BaseAdapter.exists."""
        self._ensure_connected()  # 1. guard
        coll_name = self._validated_collection(collection)
        mongo_filter = self._compiler.compile_filter(filter)  # 2. translate
        try:  # 3. execute
            coll = self._db[coll_name]
            session = self._get_session()
            if session is not None:
                doc = await coll.find_one(mongo_filter, projection={"_id": 1}, session=session)
            else:
                doc = await coll.find_one(mongo_filter, projection={"_id": 1})
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("exists", collection, err)
        return doc is not None  # 5. normalize + return

    async def aggregate(
        self, collection: str, pipeline: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Run an aggregation pipeline. See BaseAdapter.aggregate.

        On Mongo this is native — the pipeline is passed directly to the
        server. No restricted subset.
        """
        self._ensure_connected()  # 1. guard
        coll_name = self._validated_collection(collection)
        # 2. translate — validate pipeline shape
        if not isinstance(pipeline, list):
            raise InvalidFilterError(f"pipeline must be a list of stages, got {type(pipeline).__name__}")
        for stage in pipeline:
            if not isinstance(stage, dict) or len(stage) != 1:
                raise InvalidFilterError(f"each pipeline stage must be a single-key dict, got {stage!r}")
        try:  # 3. execute
            coll = self._db[coll_name]
            session = self._get_session()
            if session is not None:
                cursor = coll.aggregate(pipeline, session=session)
            else:
                cursor = coll.aggregate(pipeline)
            docs = await cursor.to_list(length=None)
        except Exception as err:  # 4. normalize errors
            # Check for PolydbError already
            if isinstance(err, (InvalidFilterError, UnsupportedOperationError)):
                raise
            # For debugging, wrap driver errors
            # If pipeline contains unsupported stage for mongo, let it surface
            # as PolydbQueryError
            await self._rollback_and_raise("aggregate", collection, err)
        return [dict(d) for d in docs]  # 5. normalize + return

    # -- 1.4 Update ------------------------------------------------------------

    async def update_one(
        self, collection: str, filter: dict[str, Any], update: dict[str, Any]
    ) -> UpdateResult:
        """Update the first document matching ``filter``. See BaseAdapter.update_one."""
        self._ensure_connected()  # 1. guard
        coll_name = self._validated_collection(collection)
        mongo_filter = self._compiler.compile_filter(filter)  # 2. translate
        mongo_update = self._compiler.compile_update(update)  # validates operators
        # Empty update is allowed by spec: report matched but modified 0
        if not mongo_update:
            # Need to count matches without modifying
            try:
                coll = self._db[coll_name]
                session = self._get_session()
                if session is not None:
                    doc = await coll.find_one(mongo_filter, projection={"_id": 1}, session=session)
                else:
                    doc = await coll.find_one(mongo_filter, projection={"_id": 1})
            except Exception as err:
                await self._rollback_and_raise("update_one", collection, err)
            matched = 1 if doc is not None else 0
            return UpdateResult(matched_count=matched, modified_count=0)

        try:  # 3. execute
            coll = self._db[coll_name]
            session = self._get_session()
            if session is not None:
                result = await coll.update_one(mongo_filter, mongo_update, session=session)
            else:
                result = await coll.update_one(mongo_filter, mongo_update)
            matched_count = int(result.matched_count)
            modified_count = int(result.modified_count)
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("update_one", collection, err)
        return UpdateResult(matched_count=matched_count, modified_count=modified_count)  # 5.

    async def update_many(
        self, collection: str, filter: dict[str, Any], update: dict[str, Any]
    ) -> UpdateResult:
        """Update every document matching ``filter``. See BaseAdapter.update_many."""
        self._ensure_connected()  # 1. guard
        coll_name = self._validated_collection(collection)
        mongo_filter = self._compiler.compile_filter(filter)  # 2. translate
        mongo_update = self._compiler.compile_update(update)
        if not mongo_update:
            # Empty update: count matches, no modification
            try:
                coll = self._db[coll_name]
                session = self._get_session()
                if session is not None:
                    n = await coll.count_documents(mongo_filter, session=session)
                else:
                    n = await coll.count_documents(mongo_filter)
            except Exception as err:
                await self._rollback_and_raise("update_many", collection, err)
            return UpdateResult(matched_count=int(n), modified_count=0)

        try:  # 3. execute
            coll = self._db[coll_name]
            session = self._get_session()
            if session is not None:
                result = await coll.update_many(mongo_filter, mongo_update, session=session)
            else:
                result = await coll.update_many(mongo_filter, mongo_update)
            matched_count = int(result.matched_count)
            modified_count = int(result.modified_count)
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("update_many", collection, err)
        return UpdateResult(matched_count=matched_count, modified_count=modified_count)  # 5.

    async def replace_one(
        self, collection: str, filter: dict[str, Any], doc: dict[str, Any]
    ) -> UpdateResult:
        """Full-document replace. See BaseAdapter.replace_one."""
        self._ensure_connected()  # 1. guard
        coll_name = self._validated_collection(collection)
        if not isinstance(doc, dict):
            raise InvalidFilterError(f"doc must be a dict, got {type(doc).__name__}")
        # Validate doc field names; bare _id is allowed for mongo but we keep validation
        for key in doc:
            # Allow _id (mongo's primary key) even though pattern would reject?
            # _id matches pattern ^[A-Za-z_][A-Za-z0-9_]*$ (starts with _), so ok.
            _validate_identifier(key, "column")
        mongo_filter = self._compiler.compile_filter(filter)  # 2. translate

        # Forbid _id in replacement doc if caller tries to change identity?
        # Relational legs forbid PK changes; for mongo we allow _id but warn.
        # Keep parity: if doc contains _id, we let mongo handle (it will fail if mismatch).

        try:  # 3. execute
            coll = self._db[coll_name]
            session = self._get_session()
            if session is not None:
                result = await coll.replace_one(mongo_filter, doc, session=session)
            else:
                result = await coll.replace_one(mongo_filter, doc)
            matched_count = int(result.matched_count)
            modified_count = int(result.modified_count)
            # Mongo's replace_one reports modified only if doc actually changed.
            # For parity with SQL's "matched=1 modified=1 when SET exists", we keep native.
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("replace_one", collection, err)
        return UpdateResult(matched_count=matched_count, modified_count=modified_count)  # 5.

    # -- 1.5 Delete ------------------------------------------------------------

    async def delete_one(self, collection: str, filter: dict[str, Any]) -> DeleteResult:
        """Delete the first document matching ``filter``. See BaseAdapter.delete_one."""
        self._ensure_connected()  # 1. guard
        coll_name = self._validated_collection(collection)
        mongo_filter = self._compiler.compile_filter(filter)  # 2. translate
        try:  # 3. execute
            coll = self._db[coll_name]
            session = self._get_session()
            if session is not None:
                result = await coll.delete_one(mongo_filter, session=session)
            else:
                result = await coll.delete_one(mongo_filter)
            deleted_count = int(result.deleted_count)
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("delete_one", collection, err)
        return DeleteResult(deleted_count=deleted_count)  # 5.

    async def delete_many(self, collection: str, filter: dict[str, Any]) -> DeleteResult:
        """Delete every document matching ``filter``. See BaseAdapter.delete_many."""
        self._ensure_connected()  # 1. guard
        coll_name = self._validated_collection(collection)
        mongo_filter = self._compiler.compile_filter(filter)  # 2. translate
        try:  # 3. execute
            coll = self._db[coll_name]
            session = self._get_session()
            if session is not None:
                result = await coll.delete_many(mongo_filter, session=session)
            else:
                result = await coll.delete_many(mongo_filter)
            deleted_count = int(result.deleted_count)
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("delete_many", collection, err)
        return DeleteResult(deleted_count=deleted_count)  # 5.

    # -- 1.6 Schema / structure ------------------------------------------------

    async def create_collection(self, name: str, schema: Schema | None = None) -> None:
        """Create a collection. ``schema`` is ignored on Mongo (schemaless).

        Logs an info-level note when a schema is supplied, matching the
        planning doc §1.6 contract.
        """
        self._ensure_connected()  # 1. guard
        coll_name = self._validated_collection(name)
        # 2. translate — log schema handling
        if schema is not None:
            logger.info(
                "create_collection on Mongo ignores schema for %r (schemaless) — %d fields",
                coll_name,
                len(schema.fields),
            )
        else:
            logger.info("create_collection on Mongo is schemaless for %r", coll_name)
        # 3. execute — ensure collection exists (idempotent)
        try:
            # Check existence first for idempotence
            existing = await self._db.list_collection_names()
            if coll_name in existing:
                return
            # Use create_collection; if it races and already exists, ignore
            try:
                session = self._get_session()
                if session is not None:
                    await self._db.create_collection(coll_name, session=session)
                else:
                    await self._db.create_collection(coll_name)
            except Exception as create_err:
                # If already exists due to race, treat as success
                # pymongo.errors.CollectionInvalid with code 48
                if "already exists" in str(create_err).lower():
                    return
                raise
        except Exception as err:
            # If err is already handled above, re-check
            if "already exists" in str(err).lower():
                return
            # List failure is a driver error
            if isinstance(err, (InvalidFilterError,)):
                raise
            await self._rollback_and_raise("create_collection", name, err)
        # 5. result None

    async def drop_collection(self, name: str) -> None:
        """Drop a collection if it exists. Idempotent by contract."""
        self._ensure_connected()  # 1. guard
        coll_name = self._validated_collection(name)
        try:  # 3. execute
            session = self._get_session()
            if session is not None:
                await self._db.drop_collection(coll_name, session=session)
            else:
                await self._db.drop_collection(coll_name)
        except Exception as err:  # 4. normalize errors
            # Dropping a non-existent collection is a no-op in Mongo (ok), but
            # some driver versions may raise NamespaceNotFound; treat as success
            if "ns not found" in str(err).lower() or "not found" in str(err).lower():
                return
            await self._rollback_and_raise("drop_collection", name, err)
        # 5. None

    async def list_collections(self) -> list[str]:
        """Enumerate collections, sorted, excluding system internals."""
        self._ensure_connected()  # 1. guard
        try:  # 3. execute
            session = self._get_session()
            if session is not None:
                names = await self._db.list_collection_names(session=session)
            else:
                names = await self._db.list_collection_names()
        except Exception as err:  # 4. normalize errors
            await self._rollback_and_raise("list_collections", "*", err)
        # Exclude system collections
        filtered = [n for n in names if not n.startswith("system.")]
        return sorted(filtered)  # 5. normalize + return

    async def create_index(
        self, collection: str, fields: list[str], *, unique: bool = False
    ) -> None:
        """Create an index over ``fields``. See BaseAdapter.create_index."""
        self._ensure_connected()  # 1. guard
        if not fields:
            raise InvalidFilterError("create_index requires at least one field to index")
        raw_table = _validate_identifier(collection, "table")
        coll_name = raw_table
        columns = self._validated_fields(fields)  # 2. translate
        index_name = self._derived_index_name(raw_table, columns, unique)
        # Mongo index spec: [(field, 1), ...]
        key_spec = [(c, 1) for c in columns]
        try:  # 3. execute
            coll = self._db[coll_name]
            session = self._get_session()
            # Use create_index which is idempotent like Mongo's createIndex
            if session is not None:
                await coll.create_index(key_spec, name=index_name, unique=unique, session=session)
            else:
                await coll.create_index(key_spec, name=index_name, unique=unique)
        except Exception as err:  # 4. normalize errors
            # If index already exists with same spec, it's a no-op; driver may
            # raise IndexOptionsConflict if spec differs but name same.
            # For parity with SQL's "different spec colliding on derived name is
            # silently ignored", we could ignore that error. But we choose to
            # surface as PolydbQueryError for now — keep simple.
            if isinstance(err, InvalidFilterError):
                raise
            await self._rollback_and_raise("create_index", collection, err)
        # 5. None

    async def add_field(
        self, collection: str, field: str, type_: Any, default: Any = None
    ) -> None:
        """Add a column — no-op with warning on Mongo (docs are dynamic)."""
        self._ensure_connected()  # 1. guard
        coll_name = self._validated_collection(collection)
        validated_field = _validate_identifier(field, "column")  # 2. translate
        if not isinstance(type_, FieldType):
            raise InvalidFilterError(
                f"add_field got unsupported field type {type_!r}; use a polydb.schema.FieldType value"
            )
        # Validate default is scalar if not None
        if default is not None and not isinstance(default, (str, int, float, bool)):
            raise InvalidFilterError(
                f"add_field default must be a plain str/int/float/bool scalar, got {default!r}"
            )
        # Mongo is schemaless: just warn
        logger.warning(
            "add_field on Mongo is a no-op (schemaless) — collection %r field %r will appear on next write",
            coll_name,
            validated_field,
        )
        # Check collection exists? Not required; just return
        return

    # -- 1.7 Transactions ------------------------------------------------------

    def transaction(self) -> Transaction:
        """Open a transaction. See MongoTransaction."""
        self._ensure_connected()
        if self._tx is not None:
            raise UnsupportedOperationError(
                "nested/concurrent transactions are not supported: this "
                "adapter already has an open transaction"
            )
        return MongoTransaction(self)

    # -- 1.8 Escape hatch ------------------------------------------------------

    async def raw(self, query: Any, params: Any = None) -> Any:
        """Pass-through to the native driver. For Mongo, ``query`` is a dict command.

        ``params`` is ignored (present for API parity with SQL legs where it
        carries positional/named binds). If supplied and not None, it must be
        None or a dict (future extensibility); otherwise raise InvalidFilterError.
        """
        self._ensure_connected()  # 1. guard
        if not isinstance(query, dict) or not query:
            raise InvalidFilterError(f"raw() on Mongo requires a non-empty dict command, got {query!r}")
        if params is not None and not isinstance(params, dict):
            # Allow None or dict for mongo; SQL legs allow list as well.
            # But to keep parity, we accept dict for session options etc., else error.
            # For now, if params is a sequence on mongo, it's likely a mistake.
            raise InvalidFilterError(
                f"raw() on Mongo: params must be None or a dict (got {type(params).__name__}); "
                f"SQL-style positional params are not applicable"
            )
        # 2. translate — none beyond validation
        # 3. execute
        try:
            session = self._get_session()
            # Merge params into command if provided (e.g., extra options)
            cmd = dict(query)
            if isinstance(params, dict):
                cmd.update(params)
            if session is not None:
                result = await self._db.command(cmd, session=session)
            else:
                result = await self._db.command(cmd)
        except Exception as err:  # 4. normalize errors
            if isinstance(err, InvalidFilterError):
                raise
            await self._rollback_and_raise("raw", "*", err)
        return result  # 5. normalize + return (Any)

    async def explain(self, collection: str, filter: dict[str, Any]) -> dict[str, Any]:
        """Return Mongo's plan for the translated filter. See BaseAdapter.explain."""
        self._ensure_connected()  # 1. guard
        coll_name = self._validated_collection(collection)
        mongo_filter = self._compiler.compile_filter(filter)  # 2. translate
        try:  # 3. execute
            coll = self._db[coll_name]
            session = self._get_session()
            # Use find().explain() — motor's explain returns a dict
            if session is not None:
                plan = await coll.find(mongo_filter, session=session).explain()
            else:
                plan = await coll.find(mongo_filter).explain()
        except Exception as err:  # 4. normalize errors
            if isinstance(err, InvalidFilterError):
                raise
            await self._rollback_and_raise("explain", collection, err)
        return {"backend": "mongo", "filter": mongo_filter, "plan": plan}  # 5.
