"""An in-memory stand-in for the parts of motor's collection API the mood-learning code uses, so tests never need (or
reach) a real database. Collections are created on first use, like MongoDB's."""

from copy import deepcopy
from contextlib import ExitStack
from typing import Any, Dict
from unittest.mock import patch

from pymongo.errors import DuplicateKeyError


def _get(doc: Dict[str, Any], path: str):
    cur: Any = doc
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _match(doc: Dict[str, Any], flt: Dict[str, Any]) -> bool:
    for key, cond in flt.items():
        value = _get(doc, key)
        if isinstance(cond, dict) and any(k.startswith("$") for k in cond):
            if "$in" in cond and value not in cond["$in"]:
                return False
            if "$ne" in cond and value == cond["$ne"]:
                return False
            if "$exists" in cond and (value is not None) != cond["$exists"]:
                return False
        elif value != cond:
            return False
    return True


class Cursor:
    def __init__(self, docs):
        self.docs = docs

    def sort(self, spec, direction=None):
        keys = [(spec, direction or 1)] if isinstance(spec, str) else spec
        for key, d in reversed(keys):
            self.docs.sort(key=lambda x: (_get(x, key) is None, _get(x, key)), reverse=(d == -1))
        return self

    def limit(self, n):
        self.docs = self.docs[:n] if n else self.docs
        return self

    def __aiter__(self):
        async def gen():
            for d in self.docs:
                yield deepcopy(d)
        return gen()


class Result:
    def __init__(self, matched=0, modified=0, deleted=0):
        self.matched_count, self.modified_count, self.deleted_count = matched, modified, deleted


class Collection:
    def __init__(self):
        self.docs: Dict[Any, Dict[str, Any]] = {}

    async def create_index(self, *a, **k):
        return "idx"

    async def insert_one(self, doc):
        if doc["_id"] in self.docs:
            raise DuplicateKeyError("duplicate key")
        self.docs[doc["_id"]] = deepcopy(doc)

    async def insert_many(self, docs, ordered=True):
        for d in docs:
            await self.insert_one(d)

    async def replace_one(self, flt, doc, upsert=False):
        existing = next((i for i, d in self.docs.items() if _match(d, flt)), None)
        if existing is None and not upsert:
            return Result()
        self.docs[doc["_id"]] = deepcopy(doc)
        return Result(matched=1 if existing is not None else 0, modified=1)

    async def update_one(self, flt, update):
        for d in self.docs.values():
            if _match(d, flt):
                d.update(deepcopy(update.get("$set", {})))
                return Result(1, 1)
        return Result()

    async def update_many(self, flt, update):
        n = 0
        for d in self.docs.values():
            if _match(d, flt):
                d.update(deepcopy(update.get("$set", {})))
                n += 1
        return Result(n, n)

    async def delete_many(self, flt):
        gone = [i for i, d in self.docs.items() if _match(d, flt)]
        for i in gone:
            del self.docs[i]
        return Result(deleted=len(gone))

    async def find_one(self, flt, projection=None):
        return next((deepcopy(d) for d in self.docs.values() if _match(d, flt)), None)

    def find(self, flt=None):
        return Cursor([deepcopy(d) for d in self.docs.values() if _match(d, flt or {})])

    async def count_documents(self, flt):
        return sum(1 for d in self.docs.values() if _match(d, flt))


class FakeMongo:
    """Use as a context manager: the mood store is switched on and every collection lives in memory."""

    def __init__(self):
        self.collections: Dict[str, Collection] = {}
        self._stack = ExitStack()

    def __getitem__(self, name: str) -> Collection:
        return self.collections.setdefault(name, Collection())

    def docs(self, name: str):
        return list(self[name].docs.values())

    def __enter__(self):
        from services.ai.core import mood_store

        mood_store._indexed.clear()
        self._stack.enter_context(patch.object(mood_store.store, "is_enabled", return_value=True))
        self._stack.enter_context(patch.object(mood_store, "collection", lambda name: self[name]))
        return self

    def __exit__(self, *exc):
        return self._stack.__exit__(*exc)
