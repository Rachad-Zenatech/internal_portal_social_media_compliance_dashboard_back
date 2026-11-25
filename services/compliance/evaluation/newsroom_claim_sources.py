"""Retrieve specific company facts for each claim, excluding weak incidental matches."""
import os

from models.compliance.compliance_model import SourceCheck
from postgresql_db.database import fetch_all
from . import newsroom_facts, source_passages, source_identity, newsroom_article_sources, newsroom_source_selection

MIN_RELEVANCE = float(os.getenv("SOURCE_NEWSROOM_MIN_RELEVANCE", "0.25"))


def relevant(score, best):
    return score >= MIN_RELEVANCE and score >= best * .6


async def readings(inventory, *, exclude=(), recall=False):
    if not inventory:
        return []
    rows = await fetch_all("SELECT title,body,as_of,COALESCE(source_url,source) AS url FROM messaging_context "
                          "WHERE kind='FACT' AND is_active AND deleted_at IS NULL AND auto_source='NEWSROOM'")
    passages = [source_passages.Passage(number=i + 1, text=f"{r['title']}\n{r['body']}", section=None)
                for i, r in enumerate(rows)]
    scores = await source_passages.rank([item["claim"] for item in inventory], passages)
    excluded = {source_identity.url_key(c.url or c.reference) for c in exclude}
    selected = {}
    for item, ranking in zip(inventory, scores):
        candidates = newsroom_source_selection.candidates(item["claim"], rows, ranking, MIN_RELEVANCE)
        if recall:
            candidates = sorted((i for i, score in enumerate(ranking) if score > 0 and (score >= MIN_RELEVANCE
                or newsroom_source_selection.product_match(item["claim"], rows[i]["title"]))
                and source_identity.url_key(rows[i]["url"] or "") not in excluded),
                key=lambda i: (-ranking[i], str(rows[i]["url"] or rows[i]["title"])))[:2]
        for index in candidates:
            row = rows[index]
            url = row["url"] or newsroom_facts.NEWSROOM_URL
            identity = source_identity.url_key(url) if row["url"] else row["title"]
            if identity in excluded:
                continue
            selected.setdefault(identity, (SourceCheck(reference=row["title"], title=row["title"], url=url,
                kind="URL", where="newsroom", cue="newsroom facts", status="READ", authority="COMPANY",
                published=str(row["as_of"]) if row.get("as_of") else None),
                (f"Company newsroom article: {row['title']}", f"{row['title']}\n{row['body']}")))
    return await newsroom_article_sources.hydrate(list(selected.values()))


async def recall(inventory, readings_so_far):
    """Try up to two unseen relevant articles per unresolved claim, irrespective of publication recency."""
    return await readings(inventory, exclude=[check for check, _ in readings_so_far], recall=True)
