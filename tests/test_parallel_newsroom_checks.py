"""Rule 18 compares cited sources and relevant newsroom facts without serially gating either."""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from models.compliance.compliance_model import SourceCheck, SourceClaim
from services.compliance.evaluation import source_claim_routing as routing


def readings():
    return [(SourceCheck(reference=name, kind="URL", where=where, status="READ"), (name, "The payload is 50 kg."))
            for name, where in [("official", "source_links"), ("newsroom", "newsroom")]]


def test_both_evidence_groups_start_without_waiting_for_the_other():
    async def scenario():
        started = {name: asyncio.Event() for name in ("official", "newsroom")}
        claim = {"claim": "The payload is 50 kg", "where": "caption"}
        async def compare(check, material, assigned):
            started[check.reference].set()
            await asyncio.gather(*(event.wait() for event in started.values()))
            verdict = "SUPPORTED" if check.reference == "official" else "CONTRADICTED"
            check.claims = [SourceClaim(**claim, verdict=verdict, confidence=.95)]
            return check.claims
        sources = readings()
        with patch.object(routing.source_passages, "rank", AsyncMock(return_value=[[.9, .8]])):
            assert await asyncio.wait_for(routing.verify([claim], sources, compare), timeout=2) == set()
        assert [source.claims[0].verdict for source, _ in sources] == ["SUPPORTED", "CONTRADICTED"]
    asyncio.run(scenario())


@pytest.mark.parametrize("failed_source", ["official", "newsroom"])
def test_one_failed_evidence_group_does_not_erase_verified_support(failed_source):
    claim = {"claim": "The payload is 50 kg", "where": "caption"}
    async def compare(check, material, assigned):
        if check.reference == failed_source:
            return None
        return [SourceClaim(**claim, verdict="SUPPORTED", confidence=.95)]
    with patch.object(routing.source_passages, "rank", AsyncMock(return_value=[[.9, .8]])):
        assert asyncio.run(routing.verify([claim], readings(), compare)) == set()
