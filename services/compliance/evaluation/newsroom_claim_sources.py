"""Retrieve specific company facts for each claim, without a post-wide eight-article cutoff."""
from models.compliance.compliance_model import SourceCheck
from postgresql_db.database import fetch_all
from . import newsroom_facts, source_passages, source_identity


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
        minimum = max(ranking, default=0.) * .6
        for index in sorted(range(len(rows)), key=lambda i: (-ranking[i], i))[:2]:
            if ranking[index] <= 0 or ranking[index] < minimum:
                continue
            row = rows[index]
            url = row["url"] or newsroom_facts.NEWSROOM_URL
            identity = source_identity.url_key(url) if row["url"] else row["title"]
            selected.setdefault(identity, (SourceCheck(reference=row["title"], title=row["title"], url=url,
                kind="URL", where="newsroom", cue="newsroom facts", status="READ", authority="COMPANY"),
                (f"Company newsroom article: {row['title']}", f"{row['title']}\n{row['body']}")))
    return list(selected.values())
