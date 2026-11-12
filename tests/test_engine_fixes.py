"""Fixes from the compliance engine review: a post must not slip through on a near-match or a stale or failed check."""

import json
import re
import unittest
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from models.compliance.compliance_model import ComplianceRule, ComplianceViolation
from services.compliance.evaluation import compliance_service as cs
from services.compliance.evaluation import context_evaluator as ce
from services.compliance.evaluation import source_check_service as sc
from services.compliance.submissions import flag_review_service as frs
from services.compliance.submissions import submission_service as subs


def padded(text: str) -> str:
    return f" {cs._normalize(text)} "


class DisclaimerNearMatch(unittest.TestCase):
    long_disclaimer = "The appearance of DoD visual information does not imply endorsement"

    def test_the_opposite_statement_is_not_the_disclaimer(self):
        text = "The appearance of DoD visual information does imply endorsement"
        self.assertFalse(cs._disclaimer_present(self.long_disclaimer, padded(text)))

    def test_a_dropped_not_is_not_a_small_drift(self):
        text = "The appearance of DoD visual information does imply an endorsement by the DoD"
        self.assertFalse(cs._disclaimer_present(self.long_disclaimer, padded(text)))

    def test_an_added_negator_turns_it_around(self):
        disclaimer = "The appearance of DoD visual information implies endorsement of this product"
        text = "The appearance of DoD visual information never implies endorsement of this product"
        self.assertFalse(cs._disclaimer_present(disclaimer, padded(text)))

    def test_or_and_and_are_not_dropped(self):
        disclaimer = "This content does not imply or constitute any official endorsement by the agency"
        text = "This content does not imply any official endorsement by the agency"
        self.assertFalse(cs._disclaimer_present(disclaimer, padded(text)))

    def test_a_short_disclaimer_must_be_word_for_word(self):
        self.assertTrue(cs._disclaimer_present("Not financial advice", padded("Not financial advice.")))
        self.assertFalse(cs._disclaimer_present("Not financial advice", padded("This is financial advice")))
        self.assertFalse(cs._disclaimer_present("Not financial advice", padded("financial advice, not for sale")))

    def test_a_contraction_still_counts_as_not(self):
        text = "The appearance of DOD visual information doesn't imply endorsement"
        self.assertTrue(cs._disclaimer_present(self.long_disclaimer, padded(text)))


class NoEndorsementWording(unittest.TestCase):
    def has(self, text):
        from services.compliance.analytics import post_signals
        return bool(post_signals._NO_ENDORSEMENT.search(text))

    def test_the_real_disclaimer_wording_counts(self):
        for text in (
            "The appearance of U.S. Department of Defense visual information does not imply or constitute DOD endorsement.",
            "The appearance of DoD visual information doesn't imply endorsement",
            "No endorsement is implied",
            "This is not an endorsement by the Army",
            "Not endorsed by the DoD",
        ):
            self.assertTrue(self.has(text), text)

    def test_an_endorsement_that_merely_follows_a_no_does_not_count(self):
        for text in (
            "No doubt, the @USArmy endorses ZenaDrone",
            "No doubt the USArmy endorses ZenaDrone",
            "Not only did the Army endorse us, it ordered more",
        ):
            self.assertFalse(self.has(text), text)


def make_rule(**fields):
    now = datetime.now(timezone.utc)
    data = {"id": uuid.uuid4(), "rule_number": "Rule 99", "category": "Test", "title": "Test rule", "summary": "x",
            "created_at": now, "updated_at": now}
    data.update(fields)
    return ComplianceRule(**data)


def make_violation(rule, **fields):
    data = {"rule_id": rule.id, "rule_number": rule.rule_number, "title": rule.title, "category": "Test",
            "severity": str(rule.severity), "traffic_light": "RED", "flagged_terms": ["x"], "explanation": "e",
            "penalty": 35}
    data.update(fields)
    return ComplianceViolation(**data)


class ReadsThatFailed(unittest.TestCase):
    def test_a_check_lists_what_it_could_not_read(self):
        check = {"reads_failed": [{"what": "image text", "name": "a.png", "error": "429"}]}
        self.assertEqual(len(subs.reads_failed(check)), 1)

    def test_a_check_with_nothing_failed_has_none(self):
        self.assertEqual(subs.reads_failed({"reads_failed": []}), [])
        self.assertEqual(subs.reads_failed(None), [])

    def test_an_older_check_says_it_through_its_counts(self):
        check = {"images_failed": 2, "documents_failed": [{"name": "script.pdf", "error": "bad file"}]}
        self.assertEqual(len(subs.reads_failed(check)), 3)

    def test_outage_and_quota_errors_are_worth_another_try_and_a_corrupt_file_is_not(self):
        retry = re.compile(subs._TRANSIENT_READ_ERROR, re.IGNORECASE)
        for error in ("429 RESOURCE_EXHAUSTED quota exceeded", "503 UNAVAILABLE", "Request timed out", "Connection reset"):
            self.assertTrue(retry.search(error), error)
        for error in ("The file is not a valid PDF", "Password-protected document"):
            self.assertFalse(retry.search(error), error)


class SourceClaimsAreSeparate(unittest.TestCase):
    def test_each_claim_has_its_own_id_so_dismissing_one_leaves_the_others(self):
        a = sc.claim_id(sc.CONTRADICTED_ID, "pilots are underway")
        b = sc.claim_id(sc.CONTRADICTED_ID, "used by 500 technicians")
        self.assertNotEqual(a, b)

    def test_the_same_claim_keeps_the_same_id(self):
        self.assertEqual(sc.claim_id(sc.NOT_FOUND_ID, "Used by 500  technicians"),
                         sc.claim_id(sc.NOT_FOUND_ID, "used by 500 technicians"))

    def test_the_kind_is_part_of_the_id(self):
        self.assertNotEqual(sc.claim_id(sc.NOT_FOUND_ID, "x"), sc.claim_id(sc.CONTRADICTED_ID, "x"))


class PassageIsWhereTheWordingCounts(unittest.TestCase):
    def test_a_denied_sentence_is_not_the_one_the_model_reads(self):
        text = "We never offer guaranteed returns. Our fund delivers guaranteed returns of 10% a year."
        passage, _ = cs._passage(text, ["guaranteed returns"])
        self.assertIn("delivers", passage)

    def test_a_sentence_inside_an_exception_is_skipped(self):
        text = "This is not combat proven yet. After the trials it is combat proven."
        passage, _ = cs._passage(text, ["combat proven"], ["not combat proven"])
        self.assertIn("After the trials", passage)

    def test_when_every_match_is_denied_the_first_one_is_still_used(self):
        passage, _ = cs._passage("No guaranteed returns here.", ["guaranteed returns"])
        self.assertIn("No guaranteed returns", passage)


class DisclaimerNeedsToBePublished(unittest.TestCase):
    rule = make_rule(
        disclaimer_triggers=["partnered with the dod"],
        required_disclaimers=["The appearance of DoD visual information does not imply endorsement"],
    )

    def test_a_disclaimer_only_in_the_unpublished_parts_does_not_count(self):
        everything = padded("Title: the appearance of DoD visual information does not imply endorsement. We partnered with the DoD.")
        published = padded("We partnered with the DoD.")
        self.assertIsNone(cs._evaluate_rule(self.rule, everything))
        self.assertIsNotNone(cs._evaluate_rule(self.rule, everything, (), published))

    def test_a_disclaimer_in_the_published_parts_counts(self):
        published = padded("We partnered with the DoD. The appearance of DoD visual information does not imply endorsement")
        self.assertIsNone(cs._evaluate_rule(self.rule, published, (), published))


class CriticalRulesAreNotLetGoOnUncertainAnswers(unittest.TestCase):
    def judge(self, status, confidence, severity="CRITICAL", traffic_light="RED", matched="guaranteed returns"):
        rule = make_rule(severity=severity, traffic_light=traffic_light)
        finding = make_violation(rule, traffic_light=traffic_light)
        candidate = ce.Candidate(finding, rule, "Our fund has guaranteed returns.", "Our fund has guaranteed returns.")
        result = ce._Result(id="0", status=status, matched_text=matched, reasoning="r", confidence=confidence)
        ce._apply(candidate, result, "model", injected=False)
        return finding

    def test_an_unsure_downgrade_of_a_critical_rule_stays_a_violation(self):
        f = self.judge("NEEDS_REVIEW", 0.4)
        self.assertEqual((f.status, f.penalty), ("VIOLATION", 35))

    def test_an_unsure_all_clear_of_a_critical_rule_stays_a_violation(self):
        f = self.judge("NO_VIOLATION", 0.5)
        self.assertEqual(f.status, "VIOLATION")

    def test_a_sure_all_clear_of_a_critical_rule_is_still_cleared(self):
        f = self.judge("NO_VIOLATION", 0.95)
        self.assertEqual((f.status, f.penalty), ("CLEARED", 0))

    def test_a_downgrade_that_is_sure_of_itself_is_kept(self):
        f = self.judge("NEEDS_REVIEW", 0.9)
        self.assertEqual(f.status, "NEEDS_REVIEW")

    def test_a_less_serious_rule_may_still_be_downgraded(self):
        f = self.judge("NEEDS_REVIEW", 0.4, severity="MEDIUM", traffic_light="YELLOW")
        self.assertEqual((f.status, f.penalty), ("NEEDS_REVIEW", 0))


class OpenYellowFlagsNeedAnAnswer(unittest.IsolatedAsyncioTestCase):
    async def standing(self, violations, dismissed=(), confirmed=()):
        sid = uuid.uuid4()
        row = {"id": sid, "latest_check": json.dumps({"score": 90, "violations": violations})}
        answers = [[{"submission_id": sid, "rule_id": r} for r in dismissed],
                   [{"submission_id": sid, "rule_id": r} for r in confirmed]]
        with patch.object(frs, "fetch_all", new=AsyncMock(side_effect=answers)):
            return (await frs.approval_standing([row]))[sid]

    def v(self, rule_id, light="YELLOW"):
        return {"rule_id": str(rule_id), "traffic_light": light, "penalty": 5}

    async def test_an_open_yellow_flag_counts(self):
        a = uuid.uuid4()
        self.assertEqual((await self.standing([self.v(a)])).open_yellow, 1)

    async def test_a_dismissed_or_confirmed_flag_does_not(self):
        a, b = uuid.uuid4(), uuid.uuid4()
        s = await self.standing([self.v(a), self.v(b)], dismissed=[a], confirmed=[b])
        self.assertEqual(s.open_yellow, 0)

    async def test_green_and_red_flags_are_not_yellow(self):
        a, b = uuid.uuid4(), uuid.uuid4()
        s = await self.standing([self.v(a, "GREEN"), self.v(b, "RED")])
        self.assertEqual((s.open_yellow, s.open_critical), (0, 1))


if __name__ == "__main__":
    unittest.main()
