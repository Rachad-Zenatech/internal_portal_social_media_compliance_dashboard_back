"""Public mood from the conversations found for a post: relevance checks, stories, limits, saved assessments."""

import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from services.compliance.analytics import conversation_evidence as ce

DRONES = uuid4()
TOPICS = {
    "x-delivery": {"id": DRONES, "external_id": "x-delivery", "name": "Drone Delivery",
                   "terms": ["Drone Delivery", "last-mile", "crop"], "excluded": ["toy drone"]},
}
ROW = {"title": "ZenaDrone cargo launch", "caption_text": "Drone delivery for farms", "brand_name": "ZenaTech",
       "audience": "INVESTORS", "platforms": ["LINKEDIN"], "script_text": "", "transcript_text": "",
       "text": "ZenaDrone cargo launch", "current_version": 1}


def _item(text, sentiment="POSITIVE", topic="x-delivery", n=[0], **kw):
    n[0] += 1
    item_id = uuid4()
    return {"id": item_id, "item_key": f"k{n[0]}", "topic_external_id": topic, "topic_name": "Drone Delivery",
            "network": "X", "author": "@a", "url": f"https://n/{n[0]}", "text": text,
            "content_hash": ce.content_hash(text), "version": 1, "story_key": ce.story_key(text),
            "sentiment": sentiment, "compound": 0, "published_at": datetime.now(timezone.utc),
            "is_eligible": True, **kw}


def _hit(item, score):
    return {"score": score, "meta": {"item_id": str(item["id"]), "content_hash": item["content_hash"]}}


def _run(items, scores, matched=(DRONES,)):
    hits = [_hit(i, s) for i, s in zip(items, scores)]
    return ce.assess(hits, {str(i["id"]): i for i in items}, TOPICS, ce.post_context_of(ROW), set(matched))


class RelevanceTests(unittest.TestCase):
    def test_similar_scores_do_not_exclude_equally_relevant_items(self):
        items = [_item(f"Drone delivery pilot {w} expands to new region") for w in ("Alpha", "Bravo", "Charlie", "Delta")]
        status, mood, rows = _run(items, [0.901, 0.900, 0.900, 0.899])
        self.assertEqual(status, "READY")
        self.assertEqual(mood.count, 4)  # a median-margin rule would have thrown most of these out

    def test_single_everyday_word_is_uncertain_not_relevant(self):
        # "crop" is a topic word, but one everyday word in common isn't a connection.
        row = dict(ROW, title="Crop the photo", caption_text="crop tips", text="Crop the photo crop tips")
        item = _item("Wheat crop prices rise after dry summer")
        status, mood, rows = ce.assess([_hit(item, 0.7)], {str(item["id"]): item}, TOPICS, ce.post_context_of(row), set())
        self.assertEqual(rows[0].status, "UNCERTAIN")
        self.assertEqual(status, "NO_RELEVANT_MATCHES")
        self.assertEqual((mood.status, mood.recommendation), ("NO_RELEVANT_MATCHES", "WAIT"))

    def test_excluded_keyword_marks_item_unrelated(self):
        item = _item("Toy drone delivery to your door this holiday")
        _, _, rows = _run([item], [0.95])
        self.assertEqual(rows[0].status, "UNRELATED")
        self.assertIn("toy drone", rows[0].reason)

    def test_named_company_in_both_is_relevant_with_supporting_text(self):
        item = _item("ZenaTech wins contract for aerial survey work - Reuters", topic="other")
        _, mood, rows = _run([item], [0.8])
        self.assertEqual(rows[0].status, "RELEVANT")
        self.assertIn("zenatech", rows[0].reason.lower())
        self.assertIn("ZenaTech", rows[0].supporting)

    def test_no_evidence_means_no_mood_and_so_no_automatic_post(self):
        status, mood, _ = _run([], [])
        self.assertEqual(status, "NO_RELEVANT_MATCHES")
        self.assertEqual(mood.recommendation, "WAIT")  # never POST just because nothing was found
        self.assertIn("NO_RELEVANT_EVIDENCE", mood.reason_codes)

    def test_outdated_or_aged_out_items_are_removed(self):
        fresh, stale, changed = _item("Drone delivery trial one"), _item("Drone delivery trial two"), _item("Drone delivery trial three")
        stale["is_eligible"] = False
        hits = [_hit(fresh, .9), _hit(stale, .9), {"score": .9, "meta": {"item_id": str(changed["id"]), "content_hash": "old"}}]
        _, mood, _ = ce.assess(hits, {str(i["id"]): i for i in (fresh, stale, changed)}, TOPICS,
                               ce.post_context_of(ROW), {DRONES})
        self.assertEqual(mood.outdated_count, 2)
        self.assertEqual(mood.count, 1)


class StoryTests(unittest.TestCase):
    def test_same_story_from_several_outlets_counts_once(self):
        a = _item("Drone delivery expands to rural towns - The Guardian")
        b = _item("Drone delivery expands to rural towns - Yahoo News")
        c = _item("Drone delivery expands to rural towns", sentiment="NEGATIVE")
        _, mood, rows = _run([a, b, c], [0.95, 0.94, 0.93])
        self.assertEqual(mood.verified_relevant_count, 3)
        self.assertEqual(mood.distinct_story_count, 1)
        self.assertEqual(mood.matches[0].also_reported_by, 2)
        self.assertEqual(mood.positive_pct, 100.0)  # the best-scoring copy represents the story


class LimitTests(unittest.TestCase):
    def test_display_limit_does_not_shrink_the_assessment(self):
        # 8 different relevant stories, all negative; only 3 are displayed but the mood is calculated from all 8.
        words = ["Apple", "Berry", "Cedar", "Delta", "Echo", "Fox", "Grape", "Hazel"]
        items = [_item(f"Drone delivery {w} outage angers customers nationwide {i * 7}", "NEGATIVE") for i, w in enumerate(words)]
        with patch.object(ce, "DISPLAY_LIMIT", 3):
            _, mood, _ = _run(items, [0.9 - i * 0.01 for i in range(8)])
        self.assertEqual((mood.displayed_count, len(mood.matches)), (3, 3))
        self.assertEqual(mood.distinct_story_count, 8)
        self.assertEqual(mood.negative_pct, 100.0)
        self.assertEqual(mood.assessment_scope, "matched_conversations")

    def test_reaching_the_retrieval_limit_is_recorded(self):
        items = [_item(f"Drone delivery update number {i} {w}") for i, w in enumerate(["aa", "bb", "cc"])]
        with patch.object(ce, "RETRIEVAL_LIMIT", 3):
            _, mood, _ = _run(items, [0.9, 0.89, 0.88])
        self.assertTrue(mood.retrieval_capped)
        self.assertEqual(mood.retrieved_count, 3)


class CacheKeyTests(unittest.TestCase):
    def _key(self, row=ROW, ev="v1", emb="m1", matched=(DRONES,), fp="t"):
        return ce.cache_key(ce.context_hash(row), ev, emb, matched, fp)

    def test_every_part_of_the_draft_changes_the_key_even_past_the_search_text_limit(self):
        base = self._key()
        for field, value in [("title", "x"), ("caption_text", "x"), ("audience", "PUBLIC"), ("brand_name", "Other"),
                             ("platforms", ["X"]), ("script_text", "new script"), ("transcript_text", "new words")]:
            self.assertNotEqual(base, self._key(dict(ROW, **{field: value})), field)
        long = dict(ROW, script_text="a" * 5000)
        self.assertNotEqual(self._key(long), self._key(dict(long, script_text="a" * 4999 + "b")))

    def test_evidence_policy_embedding_and_topic_tuning_change_the_key(self):
        base = self._key()
        self.assertNotEqual(base, self._key(ev="v2"))
        self.assertNotEqual(base, self._key(emb="m2"))
        self.assertNotEqual(base, self._key(fp="t2"))
        self.assertNotEqual(base, self._key(matched=()))
        with patch.object(ce, "POLICY_VERSION", "conversation-v3"):
            self.assertNotEqual(base, self._key())


class AssessmentFlowTests(unittest.TestCase):
    """conversation_mood with the database and Atlas faked: saved assessments are reused until the evidence moves on."""

    def setUp(self):
        self.saved = {}
        self.version = "v1"
        self.searches = 0
        self.item = _item("Drone delivery expands to rural towns")

        async def search(*a, **k):
            self.searches += 1
            return [_hit(self.item, 0.9)]

        async def fetch_one(sql, *args):
            # (submission, cache key) is the saved-assessment lookup; one argument is the "earlier assessment" lookup.
            return self.saved.get(args[1]) if len(args) == 2 else None

        async def fetch_all(sql, ids):
            return [self.item]

        async def save(sid, ver, key, ctx, ev, emb, status, mood, rows, exp):
            self.saved[key] = {"id": uuid4(), "status": status, "result": mood.model_dump(mode="json")}
            return mood

        async def evidence_context():
            return ce.EvidenceContext("SUCCESS", datetime.now(timezone.utc), "COMPLETE", eligible=1, indexed=1)

        async def no_ids(*a, **k):
            return set()

        async def nothing(*a, **k):
            return []

        async def state(key):
            return self.version

        self.patches = [
            patch.object(ce.store, "is_enabled", return_value=True),
            patch.object(ce.store, "search", search),
            patch.object(ce, "fetch_one", fetch_one),
            patch.object(ce, "fetch_all", fetch_all),
            patch.object(ce, "_save_assessment", save),
            patch.object(ce, "get_state", state),
            patch.object(ce, "evidence_context", evidence_context),
            patch.object(ce.feedback, "rejected_item_ids", no_ids),
            patch.object(ce.feedback, "excluded_for", nothing),
            patch.object(ce.feedback, "corrections_for", nothing),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def _mood(self, row=ROW):
        topics = [{"id": DRONES, "external_id": "x-delivery", "name": "Drone Delivery", "keywords": ["last-mile"],
                   "excluded_keywords": []}]
        return asyncio.run(ce.conversation_mood(row, uuid4(), topics, {DRONES}))

    def test_unchanged_draft_and_evidence_reuse_the_saved_assessment(self):
        first = self._mood()
        self.assertIsNotNone(first)
        self._mood()
        self.assertEqual(self.searches, 1)

    def test_new_evidence_gives_an_unchanged_draft_a_new_assessment(self):
        self._mood()
        self.version = "v2"  # a successful indexing batch changed what is searchable
        self._mood()
        self.assertEqual(self.searches, 2)

    def test_edited_draft_gets_a_new_assessment(self):
        self._mood()
        self._mood(dict(ROW, script_text="a new script"))
        self.assertEqual(self.searches, 2)

    def test_empty_search_is_not_saved(self):
        async def none(*a, **k):
            return []
        with patch.object(ce.store, "search", none):
            mood = self._mood()
        self.assertEqual((mood.status, mood.recommendation), ("UNAVAILABLE", None))
        self.assertEqual(self.saved, {})  # a failed or empty search is never saved as if it were an answer


class FakeConn:
    def __init__(self, log):
        self.log = log

    async def execute(self, sql, *a):
        self.log.append(sql)

    async def fetchrow(self, sql, *a):
        self.log.append(sql)
        return {"id": uuid4(), "version": 1}

    def transaction(self):
        class T:
            async def __aenter__(s): return None
            async def __aexit__(s, *e): return False
        return T()


class FakePool:
    def __init__(self):
        self.log = []

    async def fetchval(self, sql, *a):
        self.log.append(sql)
        return uuid4()

    async def execute(self, sql, *a):
        self.log.append(sql)

    def acquire(self):
        conn = FakeConn(self.log)

        class A:
            async def __aenter__(s): return conn
            async def __aexit__(s, *e): return False
        return A()


class CollectionTests(unittest.TestCase):
    def test_items_missing_from_a_later_feed_are_never_deleted(self):
        pool = FakePool()
        feed = [{"external_id": "x-delivery", "name": "Drone Delivery", "comments": [
            {"text": "Only one article today", "url": "https://n/1", "published_at": "2026-11-10T10:00:00+00:00"}]}]
        with patch.object(ce, "get_pool", return_value=pool), patch.object(ce, "fetch_all", AsyncMock(return_value=[])), \
                patch.object(ce, "refresh_eligibility", AsyncMock()), \
                patch.object(ce, "sync_index", AsyncMock(return_value={})):
            out = asyncio.run(ce.record_feed(feed, "x_json"))
        self.assertEqual(out["new"], 1)
        self.assertFalse([s for s in pool.log if "DELETE" in s.upper()])

    def test_an_empty_run_is_recorded_as_empty_and_removes_nothing(self):
        pool = FakePool()
        with patch.object(ce, "get_pool", return_value=pool), patch.object(ce, "fetch_all", AsyncMock(return_value=[])), \
                patch.object(ce, "refresh_eligibility", AsyncMock()), \
                patch.object(ce, "sync_index", AsyncMock(return_value={})):
            out = asyncio.run(ce.record_feed([], "x_json"))
        self.assertEqual(out["status"], "EMPTY")
        self.assertFalse([s for s in pool.log if "DELETE" in s.upper()])


class IdentityTests(unittest.TestCase):
    def test_item_key_is_stable_when_a_headline_is_edited_but_its_link_is_not(self):
        a = ce.item_key("t", "https://n/1", "Old headline")
        self.assertEqual(a, ce.item_key("t", "https://n/1", "Edited headline"))
        self.assertNotEqual(ce.content_hash("Old headline"), ce.content_hash("Edited headline"))

    def test_published_date_is_parsed_but_a_bare_date_is_not_trusted(self):
        self.assertEqual(ce.parse_published("Tue, 10 Nov 2026 14:03:00 GMT").hour, 14)
        self.assertEqual(ce.parse_published("2026-11-10T10:00:00Z").day, 10)
        self.assertIsNone(ce.parse_published("2026-11-10"))
        self.assertIsNone(ce.parse_published(None))


if __name__ == "__main__":
    unittest.main()


class IndexReconcileTests(unittest.TestCase):
    """sync_index keeps the vector index equal to the eligible items (stray and aged-out vectors go)."""

    def test_vectors_not_belonging_to_an_eligible_item_are_removed_and_evidence_version_changes(self):
        eligible, aged = uuid4(), uuid4()
        rows = [
            {"id": eligible, "topic_name": "T", "network": "X", "author": "@a", "text": "t", "content_hash": "h1",
             "version": 1, "is_eligible": True, "indexed_hash": "m:h1"},
            {"id": aged, "topic_name": "T", "network": "X", "author": "@a", "text": "old", "content_hash": "h2",
             "version": 1, "is_eligible": False, "indexed_hash": "m:h2"},
        ]
        pool = FakePool()
        pool.executemany = AsyncMock()
        kept = {}

        async def delete_except(kind, keep):
            kept["keep"] = keep
            return 75  # legacy vectors with no matching item

        state = AsyncMock()
        with patch.object(ce.store, "is_enabled", return_value=True), patch.object(ce.store, "embedding_model", return_value="m"), \
                patch.object(ce, "fetch_all", AsyncMock(return_value=rows)), patch.object(ce, "get_pool", return_value=pool), \
                patch.object(ce.store, "upsert", AsyncMock()), patch.object(ce.store, "delete", AsyncMock()), \
                patch.object(ce.store, "delete_kind_except", delete_except), patch.object(ce, "_set_state", state):
            out = asyncio.run(ce.sync_index())
        self.assertEqual(kept["keep"], [f"{ce.KIND}:{eligible}"])
        self.assertEqual((out["dropped"], out["stray_removed"], out["index_status"]), (1, 75, "COMPLETE"))
        self.assertIn("evidence_version", [c.args[0] for c in state.call_args_list])


class IndexPacingTests(unittest.TestCase):
    """Embedding is quota-limited: sync_index works in chunks, waits out a rate limit, and never half-reports."""

    def _rows(self, n):
        return [{"id": uuid4(), "topic_name": "T", "network": "X", "author": "@a", "text": f"t{i}", "content_hash": f"h{i}",
                 "version": 1, "is_eligible": True, "indexed_hash": None} for i in range(n)]

    def _run(self, rows, upsert):
        pool = FakePool()
        pool.executemany = AsyncMock()
        stray = AsyncMock(return_value=0)
        state = AsyncMock()
        sleeps = []

        async def sleep(s):
            sleeps.append(s)

        with patch.object(ce.store, "is_enabled", return_value=True), patch.object(ce.store, "embedding_model", return_value="m"), \
                patch.object(ce, "fetch_all", AsyncMock(return_value=rows)), patch.object(ce, "get_pool", return_value=pool), \
                patch.object(ce.store, "upsert", upsert), patch.object(ce.store, "delete_kind_except", stray), \
                patch.object(ce, "_set_state", state), patch.object(ce.asyncio, "sleep", sleep), patch.object(ce, "INDEX_CHUNK", 2):
            out = asyncio.run(ce.sync_index())
        return out, stray, state, sleeps

    def test_items_are_embedded_in_chunks_with_a_pause_between(self):
        calls = []

        async def upsert(docs):
            calls.append(len(docs))
        out, stray, state, sleeps = self._run(self._rows(5), upsert)
        self.assertEqual(calls, [2, 2, 1])
        self.assertEqual(len(sleeps), 2)  # between chunks, not after the last
        self.assertEqual((out["indexed"], out["pending"], out["index_status"]), (5, 0, "COMPLETE"))
        stray.assert_awaited()

    def test_a_rate_limited_chunk_waits_for_the_stated_delay_and_retries(self):
        attempts = {"n": 0}

        async def upsert(docs):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("429 RESOURCE_EXHAUSTED ... Please retry in 42.3s.")
        out, _, _, sleeps = self._run(self._rows(2), upsert)
        self.assertEqual(out["index_status"], "COMPLETE")
        self.assertEqual(attempts["n"], 2)
        self.assertAlmostEqual(sleeps[0], 44.3, places=1)

    def test_a_persistent_failure_is_incomplete_keeps_progress_and_removes_nothing(self):
        calls = {"n": 0}

        async def upsert(docs):
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("embedding service down")
        out, stray, state, _ = self._run(self._rows(4), upsert)
        self.assertEqual((out["indexed"], out["pending"], out["index_status"]), (2, 2, "INCOMPLETE"))
        stray.assert_not_awaited()  # nothing is removed before its replacement is in
        keys = [c.args for c in state.call_args_list]
        self.assertIn(("indexing_status", "INCOMPLETE"), keys)
        self.assertIn("evidence_version", [k[0] for k in keys])  # the 2 that did get in changed what is searchable


class EntityTests(unittest.TestCase):
    """What counts as a named company, product or place, and when sharing one is a real connection."""

    def test_a_capital_that_only_starts_a_sentence_or_line_is_not_a_name(self):
        text = "Cafeteria menu changes at the Toronto office\nNext week's lunch menu. Parking is full. Today we begin."
        self.assertEqual(ce.entities(text), {"toronto"})  # not "cafeteria", "next", "parking", "today"

    def test_names_with_a_capital_inside_or_in_capitals_are_kept_wherever_they_are(self):
        self.assertEqual(ce.entities("ZenaTech wins. The FAA approves it."), {"zenatech", "faa"})
        self.assertEqual(ce.entities("Sources say ZenaTech met the FAA."), {"zenatech", "faa"})

    def test_a_name_does_not_run_across_a_line_break(self):
        self.assertEqual(ce.entities("Red Cat and Joby partner in Orlando\nDetails to follow."), {"red cat", "joby", "orlando"})

    def test_a_lone_word_that_starts_a_sentence_is_kept_apart_until_the_other_side_names_it(self):
        firm, initial = ce.entities_split("Joby tests aircraft. Orlando hosts the event.")
        self.assertEqual((firm, initial), (set(), {"joby", "orlando"}))
        self.assertEqual(ce.entities_split("Next week we fly."), (set(), set()))  # a stopword is never a name

    def test_one_shared_place_name_is_not_enough_to_call_an_item_relevant(self):
        post = ce.post_context_of({"title": "Facilities update for the Toronto office", "caption_text": "Parking changes.",
                                   "brand_name": "Facilities"})
        status, reason, _ = ce.verify_relevance("Toronto drone maker raises funds for expansion", None, post, set(), 0.5)
        self.assertEqual(status, "UNCERTAIN")
        self.assertIn("toronto", reason)

    def test_two_shared_names_or_a_multi_word_name_or_the_posts_own_brand_is_a_connection(self):
        post = ce.post_context_of({"title": "Red Cat and Joby partner in Orlando", "caption_text": "Details to follow.",
                                   "brand_name": "ZenaTech"})
        self.assertEqual(ce.verify_relevance("Joby tests aircraft near Orlando", None, post, set(), 0.5)[0], "RELEVANT")   # two names
        self.assertEqual(ce.verify_relevance("Red Cat unveils a new drone", None, post, set(), 0.5)[0], "RELEVANT")        # a multi-word name
        self.assertEqual(ce.verify_relevance("ZenaTech wins a contract", None, post, set(), 0.5)[0], "RELEVANT")           # the brand
        self.assertEqual(ce.verify_relevance("Orlando airport reopens", None, post, set(), 0.5)[0], "UNCERTAIN")           # one place
