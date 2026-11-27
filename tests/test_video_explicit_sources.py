"""Videos opt into source checks through saved links, documents or script citations."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from models.compliance.compliance_model import ComplianceViolation, ProvidedSource, SourceCheck, SourceClaim
from services.compliance.evaluation import rule18_policy, source_check_service

RELEASE = "https://www.zenatech.com/zenatechs-drone-as-a-service-wins-three-new-mid-atlantic-dot-survey-contracts-bringing-department-of-transportation-work-to-over-500000-in-revenue-over-eight-projects/?utm_medium=social&utm_source=facebook"
CLAIM = "ZenaTech has secured three new transportation survey contracts."


def request(**changes):
    fields = dict(content_type="VIDEO_FACEBOOK", content_parts=["video"], text=CLAIM,
                  sources=[], source_script=None)
    return SimpleNamespace(**{**fields, **changes})


@pytest.mark.parametrize("changes", [
    {"text": CLAIM + "\n\nSources:\n" + RELEASE},
    {"text": CLAIM + " Read the full article: " + RELEASE},
    {"sources": [ProvidedSource(name="Press release", text=CLAIM)]},
    {"source_script": "Sources:\n" + RELEASE},
])
def test_explicit_sources_enable_video_checks(changes):
    assert rule18_policy.applies(request(**changes))


@pytest.mark.parametrize("changes", [
    {},
    {"text": CLAIM + " Learn more: https://www.zenatech.com/"},
    {"sources": [ProvidedSource(name="Empty document", text=" ")]},
    {"text": CLAIM + "\nText in image 1:\nSource: " + RELEASE},
])
def test_videos_without_explicit_usable_sources_keep_the_exemption(changes):
    assert not rule18_policy.applies(request(**changes))


def test_non_video_posts_keep_source_checking():
    assert rule18_policy.applies(request(content_type="TEXT_POST", content_parts=["caption"]))


def test_saved_link_reaches_the_full_source_and_newsroom_pipeline():
    req = request(text=CLAIM + "\n\nSources:\n" + RELEASE)
    result = source_check_service.SourceCheckResult()
    run = AsyncMock(return_value=result)
    with patch.object(source_check_service, "run", run):
        assert asyncio.run(rule18_policy.check_sources(req)) is result
    run.assert_awaited_once_with(req.text, req.sources, source_script=None, include_image_citations=False)


def test_no_source_does_not_start_source_or_newsroom_calls():
    run = AsyncMock()
    with patch.object(source_check_service, "run", run):
        result = asyncio.run(rule18_policy.check_sources(request()))
    run.assert_not_awaited()
    assert result.checks == []


@pytest.mark.parametrize("status, verdict, authority, expected", [
    ("READ", "SUPPORTED", "COMPANY", "CLEARED"),
    ("READ", "SUPPORTED", "OTHER", "VIOLATION"),
    ("UNREADABLE", "SUPPORTED", "COMPANY", "VIOLATION"),
    ("READ", "CONTRADICTED", "COMPANY", "VIOLATION"),
    ("READ", "UNCLEAR", "COMPANY", "VIOLATION"),
])
def test_rule6_requires_matching_verified_public_evidence(status, verdict, authority, expected):
    finding = ComplianceViolation(rule_id="00000000-0000-4000-8000-000000000006", rule_number="Rule 6", title="Contract announcement",
        category="Financial Disclosure", severity="HIGH", traffic_light="YELLOW", status="VIOLATION",
        flagged_terms=["secured three new transportation survey contracts"], passage=CLAIM,
        explanation="Must already be public", penalty=20)
    source = SourceCheck(reference=RELEASE, kind="URL", where="source_links", status=status,
        authority=authority, claims=[SourceClaim(claim=CLAIM, where="caption", verdict=verdict,
        evidence=CLAIM, confidence=0.95)])
    source_check_service.clear_supported([finding], [source])
    assert finding.status == expected
    assert finding.penalty == (0 if expected == "CLEARED" else 20)
