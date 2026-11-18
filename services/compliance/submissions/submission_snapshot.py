"""The immutable content snapshot stored when a post is submitted for review."""
from typing import Any, Dict, List, Optional


def build_snapshot(row: Dict[str, Any], accounts: List[Any], files: List[Dict[str, Any]],
                   script_text: Optional[str]) -> Dict[str, Any]:
    from services.compliance.submissions import submission_service as subs

    account = accounts[0] if accounts else None
    needed_by = row.get("approval_needed_by")
    return {
        "title": row["title"],
        "content_type": row["content_type"],
        "brand_code": row["brand_code"],
        "brand_codes": subs.brands_of(row),
        "audience": row["audience"],
        "audiences": subs.audiences_of(row),
        "region": row["region"],
        "platforms": list(row["platforms"] or []),
        "account": account.model_dump(mode="json") if account else None,
        "accounts": [a.model_dump(mode="json") for a in accounts],
        "publish_at": row["publish_at"].isoformat() if row["publish_at"] else None,
        "approval_needed_by": needed_by.isoformat() if needed_by else None,
        "caption_html": row["caption_html"],
        "caption_text": row["caption_text"],
        "platform_captions": subs._platform_captions(row),
        "script_text": script_text,
        "rule_sets": list(row["rule_sets"] or []),
        "reviewer_route": list(row["reviewer_route"] or []),
        "reviewer_ids": [str(r) for r in row["reviewer_ids"]],
        "files": [
            {"id": str(f["id"]), "kind": f["kind"], "original_name": f["original_name"],
             "mime_type": f["mime_type"], "size_bytes": f["size_bytes"],
             **({"source": "CANVA", "canva_design_id": f["canva_design_id"], "canva_revision": f["canva_revision"]}
                if f["source"] == "CANVA" else {})}
            for f in files
        ],
    }
