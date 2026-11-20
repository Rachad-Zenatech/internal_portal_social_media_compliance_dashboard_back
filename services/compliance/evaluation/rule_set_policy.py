"""Required rule sets by selected brand and target audience, shared by draft saving and evaluation."""
PUBLIC = "Public Company Disclosure"
GENERAL = "ZenaTech General"
DEFENSE = "ZenaDrone Defense"
CEO = "Shaun CEO Thought Leadership"
MANAGED = (PUBLIC, GENERAL, DEFENSE, CEO)
BRAND_SETS = {"ZENATECH": GENERAL, "SOFTWARE": GENERAL, "ZENADRONE": DEFENSE,
              "DAAS": DEFENSE, "CEO_THOUGHT_LEADERSHIP": CEO}
VERSION = "brand-audience-1"


def required(brands=(), audiences=()):
    selected = {PUBLIC}
    selected.update(BRAND_SETS[b] for b in brands if b in BRAND_SETS)
    if "DEFENSE" in audiences:
        selected.add(DEFENSE)
    return [name for name in MANAGED if name in selected]


def resolve(brands=(), audiences=(), selected=()):
    """Replace stale managed sets; retain explicitly selected additional sets such as Paid Social Ads."""
    return list(dict.fromkeys([*required(brands, audiences), *(name for name in selected if name not in MANAGED)]))


def update_fields(row, fields):
    """Persist the same policy for modern list fields and older single-brand/audience drafts."""
    brands = fields.get("brand_codes", row.get("brand_codes") or ([row["brand_code"]] if row.get("brand_code") else []))
    audiences = fields.get("audiences", row.get("audiences") or ([row["audience"]] if row.get("audience") else []))
    fields["rule_sets"] = resolve(brands, audiences, fields.get("rule_sets", row.get("rule_sets") or []))


def reasons(brands, audiences):
    out = {PUBLIC: ["required for every brand"]}
    for brand in brands:
        if brand in BRAND_SETS:
            out.setdefault(BRAND_SETS[brand], []).append("selected brand: " + brand)
    if "DEFENSE" in audiences:
        out.setdefault(DEFENSE, []).append("selected audience: Defense & Government")
    return out
