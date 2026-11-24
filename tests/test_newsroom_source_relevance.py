"""Avoid sequential AI calls to unrelated company articles for unresolved external claims."""
import asyncio
from unittest.mock import AsyncMock, patch

from models.compliance.compliance_model import SourceCheck, SourceClaim
from services.compliance.evaluation import newsroom_claim_sources as news, source_claim_routing as routing


def test_weak_newsroom_matches_do_not_trigger_comparisons():
    claim = {"claim": "Four Army ranges are open to private industry", "where": "caption"}
    sources = [(SourceCheck(reference=str(i), kind="URL", where="newsroom", status="READ"), (str(i), "Newsroom article body."))
               for i in range(12)]
    compare = AsyncMock()
    with patch.object(routing.source_passages, "rank", AsyncMock(return_value=[[.08] * 12])):
        assert asyncio.run(routing.verify([claim], sources, compare)) == set()
    compare.assert_not_called()


def test_supported_cited_source_does_not_skip_relevant_newsroom_matches():
    claim = {"claim": "Drone payload is 50 kg", "where": "caption"}
    sources = [(SourceCheck(reference="official", kind="URL", where="source_links", status="READ"), ("official", "Payload is 50 kg.")),
               (SourceCheck(reference="news", kind="URL", where="newsroom", status="READ"), ("news", "Payload is 50 kg."))]
    compare = AsyncMock(return_value=[SourceClaim(**claim, verdict="SUPPORTED", confidence=.95)])
    with patch.object(routing.source_passages, "rank", AsyncMock(return_value=[[.6, .9]])):
        assert asyncio.run(routing.verify([claim], sources, compare)) == set()
    assert compare.await_count == 2
    assert {call.args[0].reference for call in compare.await_args_list} == {"official", "news"}


def test_article_union_does_not_expand_each_claims_fallbacks():
    claim = {"claim": "IQ Nano supports warehouse inventory management", "where": "caption"}
    sources = [(SourceCheck(reference=str(i), kind="URL", where="newsroom", status="READ"), (str(i), "Product capability details."))
               for i in range(8)]
    with patch.object(routing.source_passages, "rank", AsyncMock(return_value=[[.9, .8, .7, .6, .5, .4, .3, .2]])):
        assert asyncio.run(routing.plans([claim], sources)) == [[0, 1]]


def test_weak_retrieval_is_not_promoted_just_because_it_is_the_best_match():
    rows = [{"title": "Investor event", "body": "A company event announcement.", "url": "https://example.com/event"}]
    with patch.object(news, "fetch_all", AsyncMock(return_value=rows)), \
            patch.object(news.source_passages, "rank", AsyncMock(return_value=[[.08]])):
        assert asyncio.run(news.readings([{"claim": "Army test ranges are open", "where": "caption"}])) == []
