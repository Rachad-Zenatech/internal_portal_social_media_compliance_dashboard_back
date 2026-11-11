"""Validate the complete public-mood flow against the live services:

    collected feed -> embeddings -> Atlas search -> relevance checks -> recommendation -> reviewer screen

    python -m scripts.validate_mood_flow

It runs the scenarios below on synthetic drafts (real Atlas search, the real collected feed, the real relevance and
decision code) with the assessment storage held in memory, so test drafts are never written into your data; then it
puts the real drafts through the real path (which saves their assessments, as opening them does). It prints PASS, FAIL
or NOT RUN for each and exits non-zero on a FAIL. NOT RUN means the live data didn't contain the situation, not a pass.

Public mood stays advisory until these hold up on real drafts.
"""

import asyncio
import sys
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch
from uuid import UUID, uuid4

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT_DIR / ".env")
except ImportError:
    pass

from postgresql_db import database as db  # noqa: E402
from services.ai.core import vector_store_service as store  # noqa: E402
from services.compliance.analytics import conversation_evidence as ce  # noqa: E402
from services.compliance.analytics import mood_features, mood_records, public_mood_service as pms  # noqa: E402

RESULTS: List[Dict[str, Any]] = []


class QuotaExhausted(Exception):
    """Gemini's embedding quota stayed exhausted: the run can't continue, and that says nothing about the behaviour."""


def check(scenario: str, expected: str, ok: Optional[bool], observed: str) -> None:
    status = "NOT RUN" if ok is None else ("PASS" if ok else "FAIL")
    RESULTS.append({"scenario": scenario, "expected": expected, "status": status, "observed": observed})


def draft(title: str, caption: str, brand: str = "ZenaTech", audience: str = "GENERAL", platforms=("LINKEDIN",),
          script: str = "") -> Dict[str, Any]:
    return {"title": title, "caption_text": caption, "script_text": script, "transcript_text": "", "brand_name": brand,
            "brand_code": brand.upper().replace(" ", "_"), "audience": audience, "platforms": list(platforms),
            "current_version": 1, "text": "\n".join(p for p in [title, caption, script] if p)}


class Harness:
    """Real search, real feed, real checks; the assessment storage is in memory, so nothing synthetic is saved."""

    def __init__(self) -> None:
        self.saved: Dict[str, Dict[str, Any]] = {}
        self.latest: Dict[str, Dict[str, Any]] = {}
        self.records: List[Dict[str, Any]] = []
        self.searches = 0
        self.version: Optional[str] = None  # None = the real evidence version
        self.search_error: Optional[Exception] = None
        self.ctx: Optional[ce.EvidenceContext] = None
        self.store_on: Optional[bool] = None
        self._stack = ExitStack()

    def __enter__(self) -> "Harness":
        real_fetch_one, real_get_state = ce.fetch_one, ce.get_state
        real_search, real_context = store.search, ce.evidence_context
        real_enabled = store.is_enabled  # captured before it is patched, or the patch would call itself

        async def fetch_one(sql, *args):
            if "public_mood_assessments" in sql:
                # Like the real query: a saved assessment belongs to one submission (and one cache key).
                return self.saved.get((str(args[0]), args[1])) if len(args) == 2 else self.latest.get(str(args[0]))
            return await real_fetch_one(sql, *args)

        async def save(sid, ver, key, ctx_hash, evidence_version, emb, status, mood, rows, expires_at):
            mood.assessment_id, mood.assessed_at, mood.expires_at = uuid4(), ce.datetime.now(ce.timezone.utc), expires_at
            row = {"id": mood.assessment_id, "status": status, "result": mood.model_dump(mode="json")}
            self.saved[(str(sid), key)] = row
            self.latest[str(sid)] = row
            return mood

        async def get_state(key):
            return self.version if (key == "evidence_version" and self.version) else await real_get_state(key)

        async def search(*a, **k):
            self.searches += 1
            if self.search_error:
                raise self.search_error
            return await real_search(*a, **k)

        async def context():
            return self.ctx or await real_context()

        async def record(rec):
            self.records.append(rec)
            return True

        for p in (patch.object(ce, "fetch_one", fetch_one), patch.object(ce, "_save_assessment", save),
                  patch.object(ce, "get_state", get_state), patch.object(ce.store, "search", search),
                  patch.object(ce, "evidence_context", context), patch.object(mood_records, "record_assessment", record),
                  patch.object(ce.store, "is_enabled", lambda: real_enabled() if self.store_on is None else self.store_on)):
            self._stack.enter_context(p)
        return self

    def __exit__(self, *exc) -> bool:
        return self._stack.__exit__(*exc)

    async def assess(self, row: Dict[str, Any], submission: Optional[UUID] = None):
        """One assessment. If the search returned nothing because Gemini's embedding quota (100 a minute on the free tier)
        ran out, wait and try again: that is the quota, not the behaviour being validated. If it stays out, stop."""
        for attempt in range(4):
            m = await self._assess_once(row, submission)
            if not (m.status == "UNAVAILABLE" and "SEARCH_UNAVAILABLE" in m.reason_codes):
                await asyncio.sleep(1.5)  # a little air between assessments
                return m
            print(f"   (the embedding quota is exhausted; waiting {20 + 15 * attempt}s and trying again)")
            await asyncio.sleep(20 + 15 * attempt)
        raise QuotaExhausted()

    async def _assess_once(self, row: Dict[str, Any], submission: Optional[UUID] = None):
        # Matching the draft to topics is not what a simulated outage is about: it only affects the assessment itself.
        outage, self.search_error = self.search_error, None
        topics = await pms._load_topics()
        meaning = await pms._meaning_scores(row, len(topics))
        matches = pms._match(row["text"], topics, meaning)
        self.search_error = outage
        return await ce.conversation_mood(row, submission or self.sub, topics, {t["id"] for t, _, _, _ in matches})

    sub = uuid4()


def mentions(item: Any, *words: str) -> bool:
    text = (item.text or "").lower()
    return any(w in text for w in words)


async def scenarios(h: Harness) -> None:
    # --- 1. a relevant post about our product -------------------------------------------------------------------
    m = await h.assess(draft("ZenaTech Drone as a Service signs solar construction contracts",
                             "ZenaTech's Drone as a Service reports growth and faster survey delivery for solar projects."))
    relevant = [x for x in m.matches if x.relevance_status == "RELEVANT"]
    named = [x for x in relevant if mentions(x, "zenatech", "zenadrone", "drone as a service")]
    check("1 Relevant post about our product", "included as evidence, with the reason it matches",
          m.status == "READY" and len(named) >= 1 and all(x.relevance_reason and x.supporting_text for x in named),
          f"status={m.status}; {len(named)} of {len(relevant)} displayed matches name the company or product; "
          f"e.g. {named[0].text[:60]!r} because {named[0].relevance_reason!r}" if named else f"status={m.status}; no match named the company")

    # --- 2. similar wording about an unrelated product ----------------------------------------------------------
    m = await h.assess(draft("ZooOffice: a new office suite for modern teams",
                             "Office collaboration, documents and calendars in one workplace suite."))
    leaked = [x for x in m.matches if x.relevance_status == "RELEVANT"
              and not mentions(x, "zenatech", "zenadrone", "zooffice", "zoo office")]
    check("2 Similar wording, unrelated product", "excluded or uncertain, never counted as evidence",
          not leaked,
          f"status={m.status}; verified relevant stories={m.distinct_story_count}; uncertain={m.uncertain_count}; "
          + (f"WRONGLY relevant: {[x.text[:50] for x in leaked]}" if leaked else "nothing unrelated was counted"))

    # --- 3. several copies of one article -----------------------------------------------------------------------
    dup = await db.fetch_one(
        "SELECT story_key, (array_agg(text))[1] AS text, count(*) n FROM listening_items WHERE is_eligible "
        "GROUP BY story_key HAVING count(*) > 1 ORDER BY n DESC LIMIT 1")
    if not dup:
        check("3 Multiple copies of one article", "grouped as one underlying story", None,
              "no story is carried by two items in the collected data right now (the grouping itself is unit-tested)")
    else:
        title = ce._PUBLISHER.sub("", dup["text"]).strip()
        m = await h.assess(draft(title, title))
        group = [x for x in m.matches if ce.story_key(x.text) == dup["story_key"]]
        grouped = m.verified_relevant_count > m.distinct_story_count or any(x.also_reported_by for x in group)
        check("3 Multiple copies of one article", "grouped as one underlying story",
              len(group) <= 1 and grouped,
              f"{dup['n']} items carry the story; shown {len(group)} time(s); relevant items={m.verified_relevant_count} "
              f"but distinct stories={m.distinct_story_count}")

    # --- 4. no relevant evidence --------------------------------------------------------------------------------
    m = await h.assess(draft("Cafeteria menu and parking changes at the Toronto office",
                             "Next week's lunch menu, bike racks and visitor parking.", brand="Facilities"))
    explains = bool(m.recommendation_reason) and "nothing here supports posting" in m.recommendation_reason
    check("4 No relevant evidence", "clearly says the evidence is insufficient; never POST",
          m.status == "NO_RELEVANT_MATCHES" and m.recommendation == "WAIT" and "NO_RELEVANT_EVIDENCE" in m.reason_codes and explains,
          f"status={m.status} rec={m.recommendation} codes={m.reason_codes}; retrieved={m.retrieved_count}, uncertain={m.uncertain_count}")

    # --- 5/6. new evidence refreshes; a changed draft is assessed again -----------------------------------------
    row = draft("Drone delivery regulations and FAA approvals", "How BVLOS waivers change delivery operations.")
    sub = uuid4()
    first = await h.assess(row, sub)
    before = h.searches
    again = await h.assess(row, sub)
    cached = h.searches == before and again.assessment_id == first.assessment_id
    h.version = "simulated-newer-evidence"
    refreshed = await h.assess(row, sub)
    check("5 New relevant posts arrive", "an unchanged draft gets a new assessment",
          cached and refreshed.assessment_id != first.assessment_id and h.searches == before + 1,
          f"unchanged draft + same evidence reused the saved assessment: {cached}; after the evidence version changed "
          f"(simulated here; a real scrape does the same) a new assessment was made: {refreshed.assessment_id != first.assessment_id}")
    h.version = None
    edited = await h.assess({**row, "title": row["title"] + " (revised)", "text": row["text"] + " (revised)"}, sub)
    check("6 Draft changes", "a new assessment is created, the old one kept",
          edited.assessment_id not in (first.assessment_id, refreshed.assessment_id) and len(h.saved) >= 3,
          f"{len(h.saved)} assessments now exist for this draft's history (none overwritten)")

    # --- 7. collection or Atlas fails ---------------------------------------------------------------------------
    row = draft("Drone inspection services for solar farms", "Faster inspections with autonomous drones.")
    h.search_error = RuntimeError("Atlas unreachable")
    m = await h.assess(row, uuid4())
    h.search_error = None
    check("7a Atlas search fails", "explicit unavailable state, no recommendation",
          m.status == "UNAVAILABLE" and m.recommendation is None and "ASSESSMENT_ERROR" in m.reason_codes,
          f"status={m.status} rec={m.recommendation} codes={m.reason_codes}: {m.recommendation_reason}")

    h.store_on = False
    m = await h.assess(row, uuid4())
    h.store_on = None
    check("7b Vector store not configured", "explicit unavailable state",
          m.status == "UNAVAILABLE" and m.recommendation is None and "STORE_NOT_CONFIGURED" in m.reason_codes,
          f"status={m.status} codes={m.reason_codes}")

    real = await ce.evidence_context()
    h.ctx = ce.EvidenceContext(real.collection_status, real.collected_at, "INCOMPLETE", eligible=real.eligible + 40, indexed=real.indexed)
    m = await h.assess(row, uuid4())
    check("7c Indexing unfinished", "pending state, no recommendation made on part of the evidence",
          m.status == "INDEXING_PENDING" and m.recommendation is None and m.pending_index_count >= 40,
          f"status={m.status} pending={m.pending_index_count}: {m.recommendation_reason}")

    for status in ("PARTIAL", "FAILED"):
        h.ctx = ce.EvidenceContext(status, real.collected_at, real.indexing_status, real.eligible, real.indexed)
        m = await h.assess(row, uuid4())
        check(f"7d Collection {status.lower()}", "assessment still shown, with an explicit collection warning",
              m.status == "READY" and f"COLLECTION_{status}" in m.reason_codes and any("collection" in w.lower() for w in m.warnings),
              f"status={m.status} codes={m.reason_codes}; warning: {m.warnings[:1]}")
    h.ctx = None

    # an earlier assessment survives a later failure, labelled as earlier
    sub = uuid4()
    good = await h.assess(row, sub)
    h.search_error = RuntimeError("Atlas unreachable")
    h.version = "newer-evidence-so-it-must-refresh"
    stale = await h.assess(row, sub)
    h.search_error, h.version = None, None
    check("7e Failure after an earlier assessment", "the earlier assessment is shown, plainly marked stale",
          stale.status == "STALE" and "STALE_ASSESSMENT" in stale.reason_codes and stale.recommendation == good.recommendation,
          f"first assessment {good.status}/{good.recommendation}; after the failure: status={stale.status} "
          f"rec={stale.recommendation} codes={stale.reason_codes[:3]}")

    # --- the reviewer screen and the learning record ------------------------------------------------------------
    row = draft("ZenaTech Drone as a Service solar construction", "ZenaTech DaaS growth.")
    h.records.clear()
    m = await h.assess(row, uuid4())
    need = ("status", "recommendation", "recommendation_reason", "reason_codes", "warnings", "matched_items", "count",
            "positive_pct", "negative_pct", "retrieved_count", "distinct_story_count", "assessment_id", "assessed_at",
            "expires_at", "collection_status", "collected_at", "evidence_version")
    shown = m.model_dump(mode="json")
    item_need = ("url", "text", "relevance_reason", "supporting_text", "sentiment", "published_at", "similarity_score")
    screen_ok = all(k in shown for k in need) and bool(shown["matched_items"]) and all(
        all(k in it for k in item_need) for it in shown["matched_items"]) and any(it["url"] for it in shown["matched_items"])
    check("8 Reviewer screen", "everything a reviewer needs to judge the recommendation is in the response", screen_ok,
          f"{len(shown['matched_items'])} matched items with links and reasons; reason_codes={shown['reason_codes']}")
    rec = h.records[0] if h.records else None
    learn_ok = bool(rec) and set(rec["features"]) == set(mood_features.FEATURE_NAMES) and rec["evidence"] and \
        rec["feature_schema_version"] == mood_features.FEATURE_SCHEMA_VERSION and rec["decision"]["source"] == "RULES"
    check("9 Assessment saved for learning", "features, evidence snapshots and the decision are recorded", learn_ok,
          f"features={ {k: rec['features'][k] for k in ('distinct_story_count', 'negative_share', 'unique_author_count')} }, "
          f"{len(rec['evidence'])} evidence snapshots" if rec else "nothing was recorded")


async def real_drafts() -> None:
    rows = await db.fetch_all(
        "SELECT id, title, brand_code, status FROM content_submissions WHERE deleted_at IS NULL ORDER BY created_at DESC LIMIT 12")
    print("\nReal drafts through the real path (this saves their assessments, as opening them does):")
    print(f"  {'draft':26} {'status':20} {'rec':5} {'stories':>7}  reason codes")
    for r in rows:
        try:
            cm = (await pms.get_public_mood(r["id"])).conversation_mood
        except Exception as e:
            print(f"  {(r['title'] or '')[:26]:26} ERROR {type(e).__name__}: {str(e)[:60]}")
            continue
        if cm is None:
            print(f"  {(r['title'] or '')[:26]:26} (no text to assess)")
            continue
        print(f"  {(r['title'] or '')[:26]:26} {cm.status:20} {str(cm.recommendation):5} {cm.count:>7}  {cm.reason_codes}")


async def embedding_quota_left() -> Optional[str]:
    """None if embeddings work; otherwise why not. The free tier allows 100 a minute and 1,000 a day: a minute's wait
    fixes the first, only the clock fixes the second, so the second is reported at once instead of waited for."""
    for attempt in range(3):
        try:
            await store.embed(["quota check"])
            return None
        except Exception as e:
            text = str(e)
            if "PerDay" in text or "per day" in text:
                import re
                wait = re.search(r"retry in ([0-9hms.]+)", text)
                return f"the DAILY embedding quota (1,000 a day on the free tier) is used up; it resets in {wait.group(1) if wait else 'many hours'}"
            if "429" in text or "RESOURCE_EXHAUSTED" in text:
                await asyncio.sleep(20)
                continue
            return f"embeddings are failing: {type(e).__name__}: {text[:120]}"
    return "the per-minute embedding quota stayed exhausted"


async def main() -> int:
    await db.create_pool()
    try:
        problem = await embedding_quota_left()
        if problem:
            print(f"Not run: {problem}. Every scenario needs embeddings, so none can be checked now.")
            return 3
        stopped = False
        try:
            with Harness() as h:
                await scenarios(h)
        except QuotaExhausted:
            stopped = True
            check("(run stopped)", "all scenarios checked", None,
                  "Gemini's embedding quota (100 a minute) stayed exhausted, so the rest were not run: wait a few minutes "
                  "(and stop anything else embedding, such as a running server) and rerun")
        width = max(len(r["scenario"]) for r in RESULTS)
        print(f"\n{'scenario':{width}}  result   expected behaviour -> what happened")
        for r in RESULTS:
            print(f"{r['scenario']:{width}}  {r['status']:7}  {r['expected']} -> {r['observed']}")
        if "--real" in sys.argv:
            await real_drafts()
        failed = [r for r in RESULTS if r["status"] == "FAIL"]
        print(f"\n{sum(r['status'] == 'PASS' for r in RESULTS)} passed, {len(failed)} failed, "
              f"{sum(r['status'] == 'NOT RUN' for r in RESULTS)} not run")
        return 1 if failed else (3 if stopped else 0)
    finally:
        await db.close_pool()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
