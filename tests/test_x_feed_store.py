"""The collected feed lives in the vector database's x_feed_data collection, and is read from there, not from a file."""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from services.ai.core import x_feed_store as xs
from services.compliance.analytics import listening_providers as lp


class FakeCursor:
    def __init__(self, docs):
        self.docs = docs

    def sort(self, spec, direction=None):
        keys = [(spec, direction or 1)] if isinstance(spec, str) else spec
        for key, d in reversed(keys):
            self.docs.sort(key=lambda x: x.get(key), reverse=(d == -1))
        return self

    def __aiter__(self):
        async def gen():
            for d in self.docs:
                yield d
        return gen()


def _match(doc, flt):
    for key, cond in flt.items():
        value = doc.get(key)
        if isinstance(cond, dict):
            if "$in" in cond and value not in cond["$in"]:
                return False
            if "$ne" in cond and value == cond["$ne"]:
                return False
        elif value != cond:
            return False
    return True


class FakeCollection:
    """Just enough of motor's collection API for x_feed_store, in memory, recording the order of writes."""

    def __init__(self):
        self.docs = {}
        self.log = []
        self.fail_insert = False

    async def create_index(self, *a, **k):
        self.log.append("index")

    async def insert_many(self, docs, ordered=True):
        if self.fail_insert:
            self.docs.update({d["_id"]: dict(d) for d in docs[:2]})  # half of them land before it fails
            raise RuntimeError("write failed")
        self.log.append("insert")
        self.docs.update({d["_id"]: dict(d) for d in docs})

    async def replace_one(self, flt, doc, upsert=False):
        self.log.append("pointer")
        self.docs[flt["_id"]] = dict(doc)

    async def delete_many(self, flt):
        gone = [i for i, d in self.docs.items() if _match(d, flt)]
        for i in gone:
            del self.docs[i]
        self.log.append("delete")
        return type("R", (), {"deleted_count": len(gone)})()

    async def find_one(self, flt, projection=None):
        return next((dict(d) for d in self.docs.values() if _match(d, flt)), None)

    def find(self, flt):
        return FakeCursor([dict(d) for d in self.docs.values() if _match(d, flt)])

    async def count_documents(self, flt):
        return sum(1 for d in self.docs.values() if _match(d, flt))


def _payload(tag="a", n=2):
    return {
        "source": "Playwright Live Scrape", "is_live_scraped": True, "collected_at": "2026-11-11T16:00:00+00:00",
        "collection": {"status": "PARTIAL", "items": 2 * n, "sources": [{"kind": "news", "name": "x", "status": "SUCCESS", "items": n}]},
        "topics": [
            {"external_id": f"t{i}", "name": f"Topic {i}", "description": "d", "keywords": ["k"], "positive": 10.0,
             "comments": [{"text": f"{tag} headline {i}-{j}", "author": "@a", "url": f"https://n/{i}/{j}", "sentiment": "NEUTRAL",
                           "published_at": "2026-11-10T10:00:00+00:00", "engagements": 0} for j in range(n)]}
            for i in range(2)
        ],
    }


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.col = FakeCollection()
        xs._cache.update(run_id=None, payload=None)
        for p in (patch.object(xs.store, "is_enabled", return_value=True),
                  patch.object(xs.store, "named_collection", lambda name: self.col if name == xs.collection_name() else None)):
            p.start()
            self.addCleanup(p.stop)

    def test_the_collection_is_called_x_feed_data(self):
        with patch.dict(os.environ, {"X_FEED_COLLECTION": ""}):
            self.assertEqual(xs.collection_name(), "x_feed_data")
        with patch.dict(os.environ, {"X_FEED_COLLECTION": "other"}):
            self.assertEqual(xs.collection_name(), "other")

    def test_what_is_written_is_read_back_unchanged_in_the_same_order(self):
        written = _payload()
        out = asyncio.run(xs.write_feed(written))
        self.assertEqual((out["topics"], out["items"]), (2, 4))
        got = asyncio.run(xs.read_feed())
        self.assertEqual([t["external_id"] for t in got["topics"]], ["t0", "t1"])
        for want, have in zip(written["topics"], got["topics"]):
            self.assertEqual(have["comments"], want["comments"])
            self.assertEqual({k: have[k] for k in ("name", "description", "keywords", "positive")},
                             {k: want[k] for k in ("name", "description", "keywords", "positive")})
        self.assertEqual(got["collection"]["status"], "PARTIAL")
        self.assertEqual(got["collection"]["sources"][0]["name"], "x")
        self.assertEqual(got["collected_at"], "2026-11-11T16:00:00+00:00")
        self.assertIsNotNone(got["written_at"].tzinfo)
        self.assertNotIn("_id", got["topics"][0])
        self.assertNotIn("kind", got["topics"][0]["comments"][0])

    def test_nothing_is_read_before_anything_is_written(self):
        self.assertIsNone(asyncio.run(xs.read_feed()))
        self.assertIsNone(asyncio.run(xs.written_at()))

    def test_readers_switch_to_the_new_run_only_after_it_is_fully_written_and_the_old_one_goes_afterwards(self):
        asyncio.run(xs.write_feed(_payload("old")))
        self.col.log.clear()
        out = asyncio.run(xs.write_feed(_payload("new")))
        self.assertEqual(self.col.log, ["index", "insert", "pointer", "delete"])
        self.assertEqual(out["removed_previous"], 6)  # the previous run's 2 topics + 4 items
        got = asyncio.run(xs.read_feed())
        self.assertTrue(all(c["text"].startswith("new") for t in got["topics"] for c in t["comments"]))
        self.assertEqual(asyncio.run(self.col.count_documents({})), 7)  # pointer + one run

    def test_a_failed_write_leaves_the_previous_feed_current_and_no_debris(self):
        asyncio.run(xs.write_feed(_payload("old")))
        before = dict(self.col.docs)
        self.col.fail_insert = True
        with self.assertRaises(RuntimeError):
            asyncio.run(xs.write_feed(_payload("new")))
        self.assertEqual(self.col.docs, before)
        xs._cache.update(run_id=None, payload=None)
        self.assertTrue(asyncio.run(xs.read_feed())["topics"][0]["comments"][0]["text"].startswith("old"))

    def test_an_unchanged_run_is_not_read_again_in_full(self):
        asyncio.run(xs.write_feed(_payload()))
        first = asyncio.run(xs.read_feed())
        with patch.object(self.col, "find", side_effect=AssertionError("full read")):
            self.assertIs(asyncio.run(xs.read_feed()), first)

    def test_writing_without_a_vector_database_is_an_error_not_a_silent_skip(self):
        with patch.object(xs.store, "named_collection", lambda name: None):
            with self.assertRaises(RuntimeError):
                asyncio.run(xs.write_feed(_payload()))
            self.assertIsNone(asyncio.run(xs.read_feed()))


class WhereTheFeedLivesTests(unittest.TestCase):
    """With a vector database the feed is stored and read there and the JSON file is ignored; otherwise (or when a
    file is asked for explicitly) it is a file."""

    def _db(self, enabled=True):
        return patch.object(lp, "_x_feed_store", return_value=type("S", (), {
            "is_enabled": staticmethod(lambda: enabled),
            "write_feed": AsyncMock(), "read_feed": AsyncMock(return_value={"topics": [{"external_id": "db", "name": "From DB",
                                                                                          "comments": []}], "collection": {"status": "SUCCESS"}}),
            "written_at": AsyncMock(return_value=None), "collection_name": staticmethod(lambda: "x_feed_data"),
        })())

    def test_a_stale_json_file_is_not_read_when_the_vector_database_is_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            stale = Path(tmp) / "x_listening_feed.json"
            stale.write_text(json.dumps({"topics": [{"external_id": "old", "name": "Old file topic", "comments": []}]}))
            with self._db(), patch.object(lp, "DEFAULT_X_JSON_PATH", stale), patch.dict(os.environ, {"X_LISTENING_JSON_PATH": ""}):
                topics = asyncio.run(lp.XJsonListeningProvider().list_topics())
        self.assertEqual([t["name"] for t in topics], ["From DB"])

    def test_an_empty_vector_database_says_nothing_has_been_collected_instead_of_using_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            stale = Path(tmp) / "f.json"
            stale.write_text(json.dumps({"topics": [{"external_id": "old", "name": "Old", "comments": []}]}))
            db = self._db()
            with db as store_patch, patch.object(lp, "DEFAULT_X_JSON_PATH", stale), patch.dict(os.environ, {"X_LISTENING_JSON_PATH": ""}):
                store_patch.return_value.read_feed = AsyncMock(return_value=None)
                with self.assertRaises(lp.ListeningUnavailable) as cm:
                    asyncio.run(lp.XJsonListeningProvider().list_topics())
        self.assertIn("Scrape X", str(cm.exception))

    def test_saving_goes_to_the_vector_database_and_says_so(self):
        with self._db() as store_patch, tempfile.TemporaryDirectory() as tmp, \
                patch.object(lp, "DEFAULT_X_JSON_PATH", Path(tmp) / "f.json"):
            where = asyncio.run(lp.save_feed({"topics": []}))
            self.assertIn("x_feed_data", where)
            store_patch.return_value.write_feed.assert_awaited_once()
            self.assertFalse((Path(tmp) / "f.json").exists())  # no file is written

    def test_an_explicit_file_is_still_honoured(self):
        with self._db() as store_patch, tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "mine.json"
            self.assertEqual(asyncio.run(lp.save_feed({"topics": [{"external_id": "x"}]}, target)), str(target))
            self.assertEqual(asyncio.run(lp.load_feed(target))["topics"][0]["external_id"], "x")
            store_patch.return_value.write_feed.assert_not_awaited()

    def test_without_a_vector_database_the_file_is_the_fallback(self):
        with self._db(enabled=False), tempfile.TemporaryDirectory() as tmp:
            default = Path(tmp) / "f.json"
            with patch.object(lp, "DEFAULT_X_JSON_PATH", default):
                self.assertIsNone(asyncio.run(lp.load_feed()))
                asyncio.run(lp.save_feed({"topics": [{"external_id": "f"}]}))
                self.assertEqual(asyncio.run(lp.load_feed())["topics"][0]["external_id"], "f")
                self.assertIsNotNone(asyncio.run(lp.feed_stamp()))

    def test_the_x_provider_is_chosen_whenever_the_vector_database_is_configured_never_made_up_data(self):
        lp.reset_provider_cache()
        try:
            with self._db(), patch.dict(os.environ, {"SOCIAL_LISTENING_PROVIDER": ""}), \
                    patch.object(lp, "DEFAULT_X_JSON_PATH", Path("does-not-exist.json")):
                self.assertEqual(lp.get_provider().name, "x_json")
            lp.reset_provider_cache()
            with self._db(enabled=False), patch.dict(os.environ, {"SOCIAL_LISTENING_PROVIDER": ""}), \
                    patch.object(lp, "DEFAULT_X_JSON_PATH", Path("does-not-exist.json")):
                self.assertTrue(lp.get_provider().is_mock)  # no database and no file: the labelled mock
        finally:
            lp.reset_provider_cache()

    def test_collection_info_and_stamp_come_from_the_same_place_as_the_feed(self):
        with self._db(), patch.dict(os.environ, {"X_LISTENING_JSON_PATH": ""}):
            provider = lp.XJsonListeningProvider()
            self.assertEqual(asyncio.run(provider.collection_info())["status"], "SUCCESS")
            self.assertIsNone(asyncio.run(provider.feed_stamp()))


if __name__ == "__main__":
    unittest.main()
