import unittest
from models.compliance.compliance_model import SourceCheck, SourceClaim
from services.compliance.evaluation import claim_checks, financial_evidence, source_check_service as sources
from services.compliance.evaluation.compliance_service import _charge_unsupported_claims

FULL = "ZenaTech's contract revenue on these projects totals approximately $503,000 and supports construction programs with a combined value of nearly $65 million, funded through federal and state transportation dollars."
WRAPPED = FULL.replace("on these projects", "\non these projects").replace("construction programs", "construction\nprograms")
OTHER = "The three new contracts support projects with a combined construction value of about $13.2 million."


def source(claim, *, where="caption", status="READ", verdict="SUPPORTED"):
    return SourceCheck(reference="Company announcement", kind="URL", where="newsroom", status=status, authority="COMPANY",
                       claims=[SourceClaim(claim=claim, where=where, verdict=verdict, evidence=claim, confidence=1)])


def check(post, checks, extra=()):
    violations = claim_checks.find_claims(post)
    violations.extend(extra)
    financial_evidence.expand(violations, checks, post)
    sources.clear_supported(violations, checks)
    _charge_unsupported_claims(violations)
    return [v for v in violations if v.rule_number == "Rule 18"]


class FinancialEvidenceTests(unittest.TestCase):
    def test_live_wrapped_passage_uses_the_supported_statement_and_loses_deduction(self):
        findings = check(WRAPPED, [source(FULL)])
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].passage, FULL)
        self.assertEqual(findings[0].status, "CLEARED")
        self.assertEqual(findings[0].penalty, 0)

    def test_support_for_503k_does_not_clear_a_separate_13million_claim(self):
        findings = check(WRAPPED + " " + OTHER, [source(FULL)])
        unresolved = [v for v in findings if v.status != "CLEARED"]
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(unresolved[0].flagged_terms, ["$13.2 million"])
        self.assertEqual(unresolved[0].passage, OTHER)
        self.assertEqual(sum(v.penalty for v in findings), 5)

    def test_equal_amounts_for_different_projects_are_not_interchangeable(self):
        first = "Alpha's contract revenue is approximately $503,000."
        second = "Beta's contract revenue is approximately $503,000."
        findings = check(first + " " + second, [source(first)])
        self.assertEqual([v.passage for v in findings if v.status == "CLEARED"], [first])
        self.assertEqual([v.passage for v in findings if v.status == "EVIDENCE_REQUIRED"], [second])

    def test_changed_number_or_missing_qualification_cannot_supply_support(self):
        for unsupported in [FULL.replace("$503,000", "$530,000"), FULL.replace("approximately ", "")]:
            with self.subTest(claim=unsupported):
                findings = check(WRAPPED, [source(unsupported)])
                self.assertTrue(all(v.status == "EVIDENCE_REQUIRED" for v in findings))

    def test_unreadable_or_inconclusive_source_does_not_clear_financial_evidence(self):
        for evidence in [source(FULL, status="UNREADABLE"), source(FULL, verdict="UNCLEAR")]:
            self.assertTrue(all(v.status == "EVIDENCE_REQUIRED" for v in check(WRAPPED, [evidence])))

    def test_caption_and_image_are_never_joined_into_one_assertion(self):
        post = "Contract revenue totals $503,000.\nText in image 1:\nConstruction programs total $65 million."
        fabricated = "Contract revenue totals $503,000. Construction programs total $65 million."
        self.assertTrue(all(v.status == "EVIDENCE_REQUIRED" for v in check(post, [source(fabricated)])))

    def test_supported_image_financial_claim_clears_independently_of_caption(self):
        image = "These projects generate over $500,000 in revenue."
        findings = check(OTHER + "\nText in image 1:\n" + image, [source(image, where="image")])
        self.assertEqual([v.passage for v in findings if v.status == "CLEARED"], [image])
        self.assertEqual([v.passage for v in findings if v.status == "EVIDENCE_REQUIRED"], [OTHER])

    def test_stable_ids_do_not_depend_on_wrapping_or_source_order(self):
        a = check(WRAPPED, [source(FULL)])
        b = check(FULL, [source("An unrelated assertion."), source(FULL)])
        self.assertEqual(a[0].rule_id, b[0].rule_id)

    def test_different_same_rule_claims_have_distinct_ids(self):
        findings = check(FULL + " " + OTHER, [])
        self.assertEqual(len({v.rule_id for v in findings}), 2)

    def test_abbreviations_and_decimal_figures_preserve_the_whole_assertion(self):
        text = "U.S. contracts total $13.2 million and are under development."
        self.assertEqual(check(text, [])[0].passage, text)

    def test_bare_supported_amount_cannot_clear_a_contextual_financial_claim(self):
        self.assertEqual(check(WRAPPED, [source("$503,000")])[0].status, "EVIDENCE_REQUIRED")

    def test_existing_source_concern_does_not_receive_a_second_financial_deduction(self):
        checks = [source(FULL, verdict="NOT_FOUND").model_copy(update={"where": "caption"})]
        findings = check(FULL, checks, sources.findings(checks))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].category, "Source Check")
        self.assertEqual(findings[0].penalty, 5)
