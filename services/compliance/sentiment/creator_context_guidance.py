"""Actionable messaging-context feedback for a creator scanning the post beside four cards."""
CREATOR_CONTEXT_GUIDANCE = """
CREATOR FEEDBACK: SPECIFIC OBSERVATION -> RELEVANT GUIDANCE -> PRACTICAL CHANGE
The creator reads four messaging-context cards beside the post. Make each card useful without opening another page.
- Do not substitute a broad summary (e.g. 'may alienate audiences' or 'needs a stronger hook') for specific evidence.
  Identify the exact words, the particular image/slide, or a timestamped visual/audio detail causing the judgement.
- A concern needs both a concrete observation and a relevant provided context entry. Never invent a rule, an audience
  profile, a fact, a platform restriction, a reaction, or a violation. Unknown guidance is a gap, not a failed check.
- Brand voice drifts: quote the offending wording, identify the specific Avoid/Works well guidance in rule, explain
  the mismatch, and give a ready-to-use rewrite. A visual brand issue belongs in findings with OBSERVED_VISUAL and
  a concrete suggested_revision describing what to replace or change; do not fabricate a spoken quote for it.
- Facts: include capability and product-stage claims as well as numbers, dates and named relationships. For each
  CONFLICTS claim, improvement must give the corrected wording supported by the cited fact. For NO_FACT, request
  the particular evidence needed for that exact claim; do not invent corrected facts. MATCHES needs no improvement.
- Platform norms checks: evidence is an exact short quote from the post for WORDING, or a precise observable image,
  slide, video moment or audio description for VISUAL/AUDIO. Set evidence_type accordingly. observation explains
  how that evidence meets or misses norm. Every ADJUST needs improvement: an actual rewrite, target length, hook,
  CTA, visual substitution or other concrete change grounded in that platform entry. Never say just 'improve tone'.
- Audience readings: evidence identifies the exact wording or visual driving this audience's reading, with the
  appropriate evidence_type. Explain why that particular audience profile matters. Every CAUTIOUS or NEGATIVE
  reading needs improvement: a specific rewrite, context to add, or visual change that addresses that concern.
  Distinguish these likely interpretations from measured audience reactions. Do not repeat generic summaries.
- Positive checks also name what specifically works. Say no issue was found only when the evidence and supplied
  guidance support it. Do not manufacture an improvement or a concern to fill a card.
- Keep each observation and improvement to one or two short sentences. Prioritise the most consequential issues.
"""
