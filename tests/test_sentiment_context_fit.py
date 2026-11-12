"""The messaging context report (brand voice, facts, platform norms, audiences) and graphic vs captions: every quote
is the post's own words and every judgement names an entry the analysis was given; and the transparency table."""

import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from models.compliance.sentiment_model import (
    AudienceReading, AudiencesReport, BrandVoiceReport, ClaimCheck, ContextReport, FactsReport, NormCheck, NormsReport,
    VisualAlignment, VoiceLine,
)
from services.compliance.sentiment import evidence, jobs
from tests.test_sentiment_verification import inp, result

CAPTION = "Twenty five years in software didn't prepare me for drones. Pilots are flying our drones every day."
ENTRIES = [
    {"id": "v1", "kind": "BRAND_VOICE", "title": "Plain-spoken"},
    {"id": "f1", "kind": "FACT", "title": "Pilot programme"},
    {"id": "n1", "kind": "PLATFORM_NORM", "title": "LinkedIn"},
    {"id": "a1", "kind": "AUDIENCE_PROFILE", "title": "Investors"},
]


def with_context(**kw):
    i = inp(caption=CAPTION, **kw)
    i["context"] = ENTRIES
    return i


def report(**over):
    base = dict(
        brand_voice=BrandVoiceReport(verdict="MOSTLY_ON_VOICE", headline="Sounds like ZenaTech", summary="Mostly.", follows=[
            VoiceLine(quote="Pilots are flying our drones", why="Concrete.", ref="C1"),
            VoiceLine(quote="invented line", why="x", ref="C1"),
        ], drifts=[VoiceLine(quote="Twenty five years in software", why="Boasting.", ref="[C1]", rule="Don't: credentials",
                             rewrite="Software taught me to ship fast.")]),
        facts=FactsReport(verdict="NEEDS_FIX", summary="One claim goes too far.", claims=[
            ClaimCheck(claim="Pilots are flying our drones every day", result="CONFLICTS", note="Pilots begin in 2027.", ref="C2"),
            ClaimCheck(claim="our new factory is open", result="MATCHES", note="", ref="C2"),
            ClaimCheck(claim="drones every day", result="MATCHES", note="", ref="C1"),  # a brand voice entry isn't a fact
        ], fix="Say pilots begin in 2027."),
        platform_norms=NormsReport(verdict="MOSTLY_FITS", summary="Fits.", checks=[
            NormCheck(aspect="Tone", norm="Professional", observation="Fine", status="FITS", ref="C3"),
            NormCheck(aspect="Length", norm="Short", observation="Fine", status="FITS", ref="C9"),
        ]),
        audiences=AudiencesReport(verdict="CAUTIOUS", summary="Investors may read a promise.", readings=[
            AudienceReading(audience="Investors", reading="A delivery promise.", reaction="CAUTIOUS", ref="C4"),
        ]),
        gaps=["No profile for operators.", " "],
    )
    base.update(over)
    return ContextReport(**base)


class ReportTests(unittest.TestCase):
    def check(self, rep, **kw):
        r = result()
        r.context_report = rep
        return evidence._verify(r, with_context(**kw)).context_report

    def test_voice_lines_must_be_the_posts_words_and_name_their_entry(self):
        out = self.check(report())
        self.assertEqual([f.quote for f in out.brand_voice.follows], ["Pilots are flying our drones"])
        self.assertEqual(out.brand_voice.follows[0].title, "Plain-spoken")
        self.assertEqual((out.brand_voice.drifts[0].ref, out.brand_voice.drifts[0].entry_id), ("C1", "v1"))

    def test_claims_are_checked_against_facts_only(self):
        claims = self.check(report()).facts.claims
        self.assertEqual([(c.claim, c.result, c.title) for c in claims], [
            ("Pilots are flying our drones every day", "CONFLICTS", "Pilot programme"),
            ("drones every day", "NO_FACT", None),  # pointed at a voice entry, so it has no fact behind it
        ])

    def test_no_fix_without_a_conflict(self):
        rep = report(facts=FactsReport(verdict="CHECKS_OUT", summary="Fine.", claims=[], fix="Something"))
        self.assertIsNone(self.check(rep).facts.fix)

    def test_norms_and_audiences_need_an_entry_they_were_given(self):
        out = self.check(report())
        self.assertEqual([n.aspect for n in out.platform_norms.checks], ["Tone"])
        self.assertEqual(out.audiences.readings[0].title, "Investors")
        self.assertEqual(out.gaps, ["No profile for operators."])


class VisualAlignmentTests(unittest.TestCase):
    def test_kept_when_there_is_a_graphic(self):
        r = result()
        r.visual_alignment = VisualAlignment(verdict="MISMATCH", explanation="Different drone.", mismatches=["quadcopter"])
        self.assertEqual(evidence._verify(r, with_context(images=1), images_attached=1).visual_alignment.verdict, "MISMATCH")

    def test_dropped_when_nothing_visual_was_attached(self):
        r = result()
        r.visual_alignment = VisualAlignment(verdict="ALIGNED", explanation="Looks fine.")
        self.assertIsNone(evidence._verify(r, with_context()).visual_alignment)


class TransparencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_used_old_and_not_applied(self):
        ran = datetime(2026, 11, 1, tzinfo=timezone.utc)
        voice, norm, old_fact, other_norm, other_voice = (uuid4() for _ in range(5))
        rows = [
            {"id": voice, "kind": "BRAND_VOICE", "title": "Plain-spoken", "brands": ["DAAS"], "audiences": [], "platforms": [],
             "as_of": None, "source": "Brand guide", "source_url": None, "updated_at": ran - timedelta(days=3),
             "deleted_at": None, "is_active": True},
            {"id": norm, "kind": "PLATFORM_NORM", "title": "LinkedIn", "brands": [], "audiences": [], "platforms": ["LINKEDIN"],
             "as_of": None, "source": None, "source_url": None, "updated_at": ran + timedelta(days=1),
             "deleted_at": None, "is_active": True},
            {"id": old_fact, "kind": "FACT", "title": "Reel length", "brands": [], "audiences": [], "platforms": [],
             "as_of": date(2024, 6, 1), "source": "Social team", "source_url": None, "updated_at": ran - timedelta(days=500),
             "deleted_at": None, "is_active": True},
            {"id": other_norm, "kind": "PLATFORM_NORM", "title": "TikTok", "brands": [], "audiences": [], "platforms": ["TIKTOK"],
             "as_of": None, "source": None, "source_url": None, "updated_at": ran, "deleted_at": None, "is_active": True},
            {"id": other_voice, "kind": "BRAND_VOICE", "title": "ZenaTech voice", "brands": ["ZENATECH"], "audiences": [],
             "platforms": [], "as_of": None, "source": None, "source_url": None, "updated_at": ran,
             "deleted_at": None, "is_active": True},
        ]
        given = {"context": [{"id": str(voice), "kind": "BRAND_VOICE", "title": "Plain-spoken"},
                             {"id": str(norm), "kind": "PLATFORM_NORM", "title": "LinkedIn"},
                             {"id": str(old_fact), "kind": "FACT", "title": "Reel length", "as_of": "2024-06-01"}]}
        post = {"brand_codes": ["DAAS"], "brand_code": "DAAS", "audiences": ["GENERAL"], "audience": "GENERAL",
                "platforms": ["LINKEDIN"]}
        with (patch.object(jobs, "fetch_all", new=AsyncMock(return_value=rows)),
              patch.object(jobs, "fetch_one", new=AsyncMock(return_value=post))):
            out = await jobs.context_entries(given, ran, uuid4())
        got = {e.title: (e.status, e.note) for e in out}
        self.assertEqual(got["Plain-spoken"], ("USED", None))
        self.assertEqual(got["LinkedIn"], ("USED_OLD", "Changed since this ran"))
        self.assertEqual(got["Reel length"], ("USED_OLD", "Over a year old"))
        self.assertEqual(got["TikTok"], ("NOT_APPLIED", "Other platform"))
        self.assertEqual(got["ZenaTech voice"], ("NOT_APPLIED", "Other brand"))


if __name__ == "__main__":
    unittest.main()


class VerdictTests(unittest.TestCase):
    def settle(self, rep, entries=ENTRIES):
        evidence._settle_verdicts(rep, entries)
        return rep

    def test_tiles_follow_their_cards(self):
        rep = self.settle(report(facts=FactsReport(verdict="NO_CLAIMS", summary="", claims=[
            ClaimCheck(claim="x", result="NO_FACT", note="")])))
        self.assertEqual(rep.facts.verdict, "UNVERIFIED")
        self.assertEqual(rep.platform_norms.verdict, "FITS")
        self.assertEqual(rep.audiences.verdict, "CAUTIOUS")

    def test_a_conflict_needs_a_fix_and_drifts_arent_on_voice(self):
        rep = report()
        rep.brand_voice.verdict = "ON_VOICE"
        rep = self.settle(rep)
        self.assertEqual(rep.facts.verdict, "NEEDS_FIX")
        self.assertEqual(rep.brand_voice.verdict, "MOSTLY_ON_VOICE")

    def test_no_voice_entry_means_no_guide(self):
        self.assertEqual(self.settle(report(), entries=ENTRIES[1:]).brand_voice.verdict, "NO_GUIDE")

    def test_mostly_adjust_needs_work(self):
        rep = report(platform_norms=NormsReport(verdict="FITS", summary="", checks=[
            NormCheck(aspect="a", norm="", observation="", status="ADJUST", ref="C3"),
            NormCheck(aspect="b", norm="", observation="", status="ADJUST", ref="C3"),
            NormCheck(aspect="c", norm="", observation="", status="FITS", ref="C3")]))
        self.assertEqual(self.settle(rep).platform_norms.verdict, "NEEDS_WORK")
