"""Retrieve specific company facts for each claim, excluding weak incidental matches."""
import os

from models.compliance.compliance_model import SourceCheck
from postgresql_db.database import fetch_all
from . import newsroom_facts, source_passages, source_identity

MIN_RELEVANCE = float(os.getenv("SOURCE_NEWSROOM_MIN_RELEVANCE", "0.25"))


def relevant(score, best):
    return score >= MIN_RELEVANCE and score >= best * .6


async def readings(inventory):
    if not inventory:
        return []
    rows = await fetch_all("SELECT title,body,COALESCE(source_url,source) AS url FROM messaging_context "
                          "WHERE kind='FACT' AND is_active AND deleted_at IS NULL AND auto_source='NEWSROOM'")
    passages = [source_passages.Passage(number=i + 1, text=f"{r['title']}\n{r['body']}", section=None)
                for i, r in enumerate(rows)]
    scores = await source_passages.rank([item["claim"] for item in inventory], passages)
    selected = {}
    for ranking in scores:
        best = max(ranking, default=0.)
        for index in sorted(range(len(rows)), key=lambda i: (-ranking[i], i))[:2]:
            if not relevant(ranking[index], best):
                continue
            row = rows[index]
            url = row["url"] or newsroom_facts.NEWSROOM_URL
            identity = source_identity.url_key(url) if row["url"] else row["title"]
            selected.setdefault(identity, (SourceCheck(reference=row["title"], title=row["title"], url=url,
                kind="URL", where="newsroom", cue="newsroom facts", status="READ", authority="COMPANY"),
                (f"Company newsroom article: {row['title']}", f"{row['title']}\n{row['body']}")))
    return list(selected.values())
