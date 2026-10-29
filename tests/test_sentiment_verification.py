import unittest
from datetime import datetime, timezone

from models.sentiment_model import SentimentFinding, SentimentResult
from services.compliance import sentiment_service as s


def inp(caption="", title="Post", transcript=None, images=0, guidelines=()):
    return {
        "title": title,
        "caption": caption,
        "transcript": transcript,
        "images": [{"file_id": f"f{i}", "name": f"img{i}.png"} for i in range(images)],
        "guidelines": [{"rule_number": n} for n in guidelines],
    }


def finding(quote, basis="OBSERVED_WORDING", severity="MODERATE", **extra):
    row = {"category": "TONE", "severity": severity, "title": "t", "quote": quote, "why_it_matters": "w", "basis": basis}
    row.update(extra)
    return row


def result(*findings, evidence=(), controversy=None):
    return SentimentResult.model_validate(
        {
            "summary": "s",
            "overall_assessment": "REVIEW_SUGGESTED",
            "reputational_concern": "LOW",
            "tone": {"primary": "Informative", "intensity": "LOW", "evidence": list(evidence)},
            "brand_alignment": {"rating": "ALIGNED", "explanation": "e"},
            "controversy": controversy or {"applies": False},
            "findings": list(findings),
        }
    )


class QuoteVerificationTests(unittest.TestCase):
    def test_a_quote_thats_not_in_the_content_is_dropped(self):
        r = s._verify(result(finding("we are the best in the world")), inp(caption="Meet the team at the expo."))
        self.assertEqual(r.findings, [])
        self.assertEqual(r.unverified_findings_removed, 1)

    def test_a_quote_is_found_despite_case_curly_quotes_and_punctuation(self):
        r = s._verify(result(finding("we’re the ONLY drone company")), inp(caption="We're the only drone company, period."))
        self.assertEqual(len(r.findings), 1)
        self.assertEqual(r.findings[0].source, "caption")

    def test_the_same_wording_is_flagged_once_keeping_the_most_severe(self):
        r = s._verify(
            result(finding("never fails", severity="LOW"), finding("never fails", severity="HIGH")),
            inp(caption="It never fails."),
        )
        self.assertEqual([f.severity for f in r.findings], ["HIGH"])

    def test_tone_evidence_not_in_the_content_is_removed(self):
        r = s._verify(result(evidence=["at the expo", "invented words"]), inp(caption="Meet the team at the expo."))
        self.assertEqual(r.tone.evidence, ["at the expo"])


class TranscriptTimingTests(unittest.TestCase):
    TRANSCRIPT = [
        {"start_sec": 0.0, "end_sec": 4.0, "text": "Welcome to the field trials."},
        {"start_sec": 4.0, "end_sec": 9.5, "text": "This drone never fails."},
        {"start_sec": 9.5, "end_sec": 12.0, "text": "Ever. Really."},
    ]

    def locate(self, quote):
        return s._locate(quote, inp(transcript=self.TRANSCRIPT))

    def test_a_quote_starting_a_segment_takes_only_that_segments_time(self):
        self.assertEqual(self.locate("this drone never fails"), ("transcript", 4.0, 9.5))

    def test_a_quote_ending_a_segment_doesnt_take_the_next_one(self):
        self.assertEqual(self.locate("the field trials"), ("transcript", 0.0, 4.0))

    def test_a_quote_across_segments_spans_both(self):
        self.assertEqual(self.locate("field trials this drone"), ("transcript", 0.0, 9.5))
        self.assertEqual(self.locate("never fails ... ever"), ("transcript", 4.0, 12.0))


class ImageAndVideoFindingTests(unittest.TestCase):
    def test_a_finding_about_an_attached_image_is_kept(self):
        r = s._verify(result(finding("drone over a crowd", basis="OBSERVED_VISUAL", image_index=2)), inp(images=3), images_attached=3)
        self.assertEqual((r.findings[0].source, r.findings[0].image_index), ("image", 2))

    def test_a_finding_about_an_image_that_wasnt_attached_is_dropped(self):
        r = s._verify(result(finding("logo", basis="OBSERVED_VISUAL", image_index=9)), inp(images=3), images_attached=3)
        self.assertEqual(r.findings, [])
        self.assertEqual(r.unverified_findings_removed, 1)

    def test_wording_found_only_in_the_single_image_is_kept_against_it(self):
        # A headline inside a graphic: not in the (empty) caption, but the post's one image is where it is.
        r = s._verify(result(finding("Estimates Q3 revenue of C$14.8 million")), inp(images=1), images_attached=1)
        self.assertEqual((r.findings[0].source, r.findings[0].image_index), ("image", 1))

    def test_a_video_finding_needs_the_video_and_a_time(self):
        sound = finding("explosion effect", basis="OBSERVED_AUDIO", start_sec=3.0)
        self.assertTrue(s._verify(result(sound), inp(), video_attached=True).findings)
        self.assertFalse(s._verify(result(sound), inp(), video_attached=False).findings)
        no_time = finding("explosion effect", basis="OBSERVED_AUDIO")
        self.assertFalse(s._verify(result(no_time), inp(), video_attached=True).findings)


class FindingBookkeepingTests(unittest.TestCase):
    def test_cited_rules_are_matched_to_the_applicable_rule_numbers(self):
        rules = {"Rule 6", "Rule 25"}
        self.assertEqual(s._match_rule("Rule 25 (Forward-looking statements)", rules), "Rule 25")
        self.assertEqual(s._match_rule("6", rules), "Rule 6")
        self.assertIsNone(s._match_rule("Rule 99", rules))

    def test_finding_ids_are_stable_and_distinguish_images(self):
        first = SentimentFinding.model_validate(finding("logo", basis="OBSERVED_VISUAL", image_index=1))
        same = SentimentFinding.model_validate(finding("logo", basis="OBSERVED_VISUAL", image_index=1))
        other_image = SentimentFinding.model_validate(finding("logo", basis="OBSERVED_VISUAL", image_index=2))
        self.assertEqual(s.finding_id(first), s.finding_id(same))
        self.assertNotEqual(s.finding_id(first), s.finding_id(other_image))

    def test_findings_get_ids_and_controversy_is_cleared_when_it_doesnt_apply(self):
        r = s._verify(
            result(finding("never fails"), controversy={"applies": False, "level": 5, "target": "PEOPLE_OR_INSTITUTIONS"}),
            inp(caption="It never fails.", guidelines=["Rule 1"]),
        )
        self.assertTrue(r.findings[0].id)
        self.assertEqual((r.controversy.level, r.controversy.target), (None, None))
        self.assertEqual(r.guidelines_used, ["Rule 1"])

    def test_precedent_labels_become_past_review_names(self):
        precedents = [
            {"score": 0.93, "title": "Old post", "text": "",
             "meta": {"reference_code": "SUB-1012", "version": 2, "decision": "REQUEST_CHANGES"}}
        ]
        r = s._with_precedents(result(finding("x", precedent="P1"), finding("y", precedent="P7")), precedents)
        self.assertEqual([f.precedent for f in r.findings], ["SUB-1012 v2", None])
        self.assertEqual(r.precedents[0].label, "SUB-1012 v2")


class QuotaResetTests(unittest.TestCase):
    def test_the_daily_quota_resets_at_midnight_pacific(self):
        # 10:00 UTC on Oct 22 is 03:00 in Los Angeles (PDT), so the next reset is Oct 23 00:00 PDT = 07:00 UTC.
        reset = s.next_quota_reset(datetime(2026, 10, 22, 10, 0, tzinfo=timezone.utc))
        self.assertEqual(reset, datetime(2026, 10, 23, 7, 0, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()
