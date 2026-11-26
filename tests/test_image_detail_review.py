"""Uncertain observed text requests review; it never creates a confirmed violation."""
import unittest
from types import SimpleNamespace
from services.compliance.evaluation import image_detail_review as guard
from services.compliance.evaluation.visual_review import Assessment


def assessment(status="CLEARED"):
    return Assessment(rule_number="Rule 28",status=status,confidence=.92,evidence_basis="NO_CONCERN",
                      evidence="",reasoning="No crime references.",box=[],start_sec=0.,end_sec=0.)


def region(text="MAFIA",confidence=.6):
    return {"text":text,"confidence":confidence,"box":[400.,100.,430.,170.]}


class DetailReviewTests(unittest.TestCase):
    def test_specific_uncertain_plate_word_cannot_be_silently_cleared(self):
        original=assessment()
        result=guard.apply([original],{"image_text_details":[region()]},{"Rule 28":SimpleNamespace(prohibited_terms=["mafia"])})
        self.assertEqual(result[0].status,"NEEDS_REVIEW")
        self.assertEqual(result[0].evidence,"MAFIA")
        self.assertLess(result[0].confidence,.85)
        self.assertEqual(original.status,"CLEARED")

    def test_unrelated_uncertain_words_do_not_create_a_rule28_finding(self):
        result=guard.apply([assessment()],{"image_text_details":[region("SMALL LABEL")]},{"Rule 28":SimpleNamespace(prohibited_terms=["mafia"])})
        self.assertEqual(result[0].status,"CLEARED")

    def test_unselected_rule_and_missing_details_remain_unchanged(self):
        self.assertEqual(guard.apply([assessment()],{"image_text_details":[region()]},{})[0].status,"CLEARED")
        self.assertEqual(guard.apply([assessment()],{}, {"Rule 28":SimpleNamespace(prohibited_terms=["mafia"])})[0].status,"CLEARED")

    def test_existing_confirmed_finding_is_not_downgraded(self):
        result=guard.apply([assessment("VIOLATION")],{"image_text_details":[region()]},{"Rule 28":SimpleNamespace(prohibited_terms=["mafia"])})
        self.assertEqual(result[0].status,"VIOLATION")

    def test_invalid_coordinates_or_very_uncertain_words_do_not_raise(self):
        invalid=region(); invalid["box"]=[-1,2,3,4]
        for item in (invalid,region(confidence=.1)):
            result=guard.apply([assessment()],{"image_text_details":[item]},{"Rule 28":SimpleNamespace(prohibited_terms=["mafia"])})
            self.assertEqual(result[0].status,"CLEARED")

    def test_explicit_wording_exception_is_preserved(self):
        result=guard.apply([assessment()],{"image_text_details":[region("anti mafia")]},
            {"Rule 28":SimpleNamespace(prohibited_terms=["mafia"],exception_terms=["anti mafia"])})
        self.assertEqual(result[0].status,"CLEARED")
