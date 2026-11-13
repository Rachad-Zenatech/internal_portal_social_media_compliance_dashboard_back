"""Passages of a source: what the model reads of a long source, and where in the source each claim really is.

A source is split into numbered paragraphs, each with the section heading above it. Passages are ranked against a
piece of the post (or a claim's evidence) by a hybrid score: keyword overlap weighted by how rare each word is (so
exact figures like "8.1" and names like "POAM" count most), plus Gemini embedding similarity when embeddings are
available. Embeddings only help find the passage; they never decide a verdict ("$8.6M" and "$8.1 million" read alike
to them).

- excerpt(): a source longer than MODEL_SOURCE_CHARS is not cut off at the end; the model is given the opening and the
  paragraphs closest to each statement in the post, in reading order, each marked with its place.
- checked_claims(): the model's answers through the safeguards, then each claim's evidence is matched back to the
  source: the claim gets the source's own words (source_quote) and the exact place (source_location), so a person can
  read what the source says next to what the post says and confirm it.
"""

import hashlib
import logging
import math
import os
import re
from collections import Counter, OrderedDict
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from models.compliance.compliance_model import SourceClaim
from services.compliance.evaluation.source_claim_checks import _stems, _to_claims

logger = logging.getLogger(__name__)

# Sources up to this long are given to the model whole; longer ones as an excerpt of the passages that matter.
MODEL_SOURCE_CHARS = int(os.getenv("SOURCE_MODEL_CHARS", "30000"))
EMBED_ENABLED = os.getenv("SOURCE_PASSAGES_EMBED", "true").strip().lower() not in {"0", "false", "no", "off"}
MAX_EMBED_PASSAGES = int(os.getenv("SOURCE_PASSAGES_MAX_EMBED", "400"))
PASSAGE_CHARS = int(os.getenv("SOURCE_PASSAGE_CHARS", "900"))
TOP_PER_STATEMENT = int(os.getenv("SOURCE_PASSAGES_PER_STATEMENT", "4"))
# A passage scoring below this isn't taken as where a claim is.
LOCATE_MIN_SCORE = float(os.getenv("SOURCE_LOCATE_MIN_SCORE", "0.3"))
MAX_QUOTE_CHARS = int(os.getenv("SOURCE_QUOTE_MAX_CHARS", "500"))
# How much a passage's score rests on shared words; the rest on meaning (embeddings).
KEYWORD_WEIGHT = min(1.0, max(0.0, float(os.getenv("SOURCE_PASSAGES_KEYWORD_WEIGHT", "0.6"))))
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+(?=[\"“(]?[A-Z0-9])")
# A full stop after these isn't the end of a sentence ("Oct. 1, 2027", "Dr. Passley", "U.S. Army").
_ABBREVIATION = re.compile(
    r"(?:\b(?:Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec|Mr|Mrs|Ms|Dr|Prof|Gen|Col|Lt|Sgt|Capt|Adm|Rep|Sen|Gov|"
    r"St|No|Nos|Inc|Corp|Co|Ltd|vs|approx|est|Fig|Vol|Jr|Sr)|\b[A-Z](?:\.[A-Z])*)\.$"
)
_NOT_A_HEADING = re.compile(r"^[-–—•*·>]|^\d+\s*min(?:ute)?s?\s+read\b|^(?:by|photo|image|source|credit)\b", re.I)


class _Sentences:
    @staticmethod
    def split(text: str) -> List[str]:
        """Sentences, without breaking after an abbreviation or an initial."""
        out: List[str] = []
        for piece in _SENTENCE_BREAK.split(text or ""):
            if out and _ABBREVIATION.search(out[-1]):
                out[-1] = f"{out[-1]} {piece}"
            else:
                out.append(piece)
        return out


_SENTENCE = _Sentences
_vectors: "OrderedDict[str, List[float]]" = OrderedDict()
_VECTORS_MAX = 4000


@dataclass
class Passage:
    number: int
    text: str
    section: Optional[str] = None

    @property
    def label(self) -> str:
        return f"{self.section} · paragraph {self.number}" if self.section else f"Paragraph {self.number}"


# --- Splitting ---------------------------------------------------------------------------------------------------------

def _is_heading(line: str) -> bool:
    """A section heading: short, no closing punctuation, not a bullet or byline, and in title style once it is long."""
    words = line.split()
    if not (0 < len(words) <= 12 and len(line) <= 90) or re.search(r"[.!?:;,\"”)]$", line) or _NOT_A_HEADING.search(line):
        return False
    long_words = [w for w in words if len(w) > 3]
    capitalised = sum(1 for w in long_words if w[0].isupper())
    return len(words) <= 6 or capitalised * 2 >= len(long_words)


def _pieces(line: str) -> List[str]:
    """A long line in pieces of about PASSAGE_CHARS, cut between sentences."""
    if len(line) <= PASSAGE_CHARS * 1.4:
        return [line]
    out, cur = [], ""
    for s in _SENTENCE.split(line):
        if cur and len(cur) + len(s) > PASSAGE_CHARS:
            out.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    return out + ([cur] if cur else [])


def split(text: str) -> List[Passage]:
    """The source as numbered paragraphs, each with the heading it sits under."""
    passages: List[Passage] = []
    section: Optional[str] = None
    for raw in (text or "").split("\n"):
        line = " ".join(raw.split())
        if not line:
            continue
        if _is_heading(line):
            section = line
            continue
        if len(line) < 40 and passages and passages[-1].section == section:
            passages[-1].text += f" {line}"  # a stray fragment (a bullet's tail, a label) belongs with what's above
            continue
        for piece in _pieces(line):
            passages.append(Passage(number=len(passages) + 1, text=piece, section=section))
    return passages


# --- Ranking -----------------------------------------------------------------------------------------------------------

def _terms(text: str) -> List[str]:
    return _stems(text) + [n.replace(",", "") for n in _NUMBER.findall(text or "")]


def _keyword_scores(query: str, passages: Sequence[Passage], df: Counter) -> List[float]:
    wanted = set(_terms(query))
    if not wanted:
        return [0.0] * len(passages)
    n = len(passages)
    weight = {t: math.log(1 + n / (1 + df[t])) for t in wanted}
    total = sum(weight.values()) or 1.0
    return [sum(weight[t] for t in wanted & set(_terms(p.text))) / total for p in passages]


async def _embeddings(texts: List[str]) -> Optional[List[List[float]]]:
    """Embeddings for the texts (remembered by text), or None when they can't be had."""
    if not EMBED_ENABLED or not texts:
        return None
    from services.ai.core import vector_store_service

    keys = [hashlib.sha256(t.encode("utf-8")).hexdigest() for t in texts]
    missing = [t for t, k in zip(texts, keys) if k not in _vectors]
    try:
        if missing:
            for t, v in zip(missing, await vector_store_service.embed(missing)):
                _vectors[hashlib.sha256(t.encode("utf-8")).hexdigest()] = v
                while len(_vectors) > _VECTORS_MAX:
                    _vectors.popitem(last=False)
        return [_vectors[k] for k in keys]
    except Exception as e:  # quota, no key, network: keywords alone still work
        logger.info("Source passages: no embeddings (%s); ranking by keywords only", str(e)[:120])
        return None


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


async def rank(queries: Sequence[str], passages: Sequence[Passage]) -> List[List[float]]:
    """For each query, a score per passage (0 to 1): keywords, plus embedding similarity when available."""
    if not queries or not passages:
        return [[] for _ in queries]
    df = Counter(t for p in passages for t in set(_terms(p.text)))
    keyword = [_keyword_scores(q, passages, df) for q in queries]
    vectors = None
    if len(passages) <= MAX_EMBED_PASSAGES:
        vectors = await _embeddings(list(queries) + [p.text for p in passages])
    if not vectors:
        return keyword
    qv, pv = vectors[:len(queries)], vectors[len(queries):]
    # Unrelated texts still score about 0.5 by cosine; spread what is above that over 0 to 1.
    kw, mw = KEYWORD_WEIGHT, 1.0 - KEYWORD_WEIGHT
    return [[kw * k + mw * max(0.0, (_cosine(q, p) - 0.5) / 0.5) for k, p in zip(keyword[i], pv)]
            for i, q in enumerate(qv)]


# --- What the model reads ----------------------------------------------------------------------------------------------

def _statements(post_text: str) -> List[str]:
    """The post's lines and sentences that could hold a claim (a figure, or a few real words)."""
    parts = [s.strip() for line in (post_text or "").split("\n") for s in _SENTENCE.split(line)]
    keep = [p for p in parts if _NUMBER.search(p) or len(_stems(p)) >= 3]
    return list(dict.fromkeys(keep))[:40]


async def excerpt(post_text: str, source_text: str) -> str:
    """The source as the model should read it: whole when short, else the opening and the passages nearest the post."""
    if len(source_text or "") <= MODEL_SOURCE_CHARS:
        return source_text
    passages = split(source_text)
    statements = _statements(post_text)
    scores = await rank(statements, passages)
    chosen = {0, 1} & set(range(len(passages)))
    for row in scores:
        chosen |= {i for i in sorted(range(len(row)), key=lambda i: -row[i])[:TOP_PER_STATEMENT] if row[i] > 0}
    best = sorted(range(len(passages)), key=lambda i: -max((r[i] for r in scores), default=0.0))
    size = sum(len(passages[i].text) for i in chosen)
    for i in best:  # fill what room is left with the next best passages
        if size >= MODEL_SOURCE_CHARS:
            break
        if i not in chosen:
            chosen.add(i)
            size += len(passages[i].text)
    lines, last = [], -2
    for i in sorted(chosen):
        if i != last + 1:
            lines.append("[…]")
        lines.append(f"[{passages[i].label}] {passages[i].text}")
        last = i
    note = "(Excerpt: the source is long, so these are its opening and the passages nearest the post's statements.)"
    return note + "\n" + "\n".join(lines)


# --- Where each claim is -----------------------------------------------------------------------------------------------

def _best_sentences(passage: Passage, query: str) -> str:
    """The sentence of the passage closest to the query (with the next one when it is short), word for word."""
    sentences = [s for s in _SENTENCE.split(passage.text) if s.strip()] or [passage.text]
    df = Counter(t for s in sentences for t in set(_terms(s)))
    pseudo = [Passage(number=i, text=s) for i, s in enumerate(sentences)]
    scores = _keyword_scores(query, pseudo, df)
    i = max(range(len(sentences)), key=lambda k: scores[k])
    quote = sentences[i]
    if len(quote) < 80 and i + 1 < len(sentences):
        quote = f"{quote} {sentences[i + 1]}"
    return quote[:MAX_QUOTE_CHARS]


async def locate(claims: List[SourceClaim], source_text: str) -> List[SourceClaim]:
    """Each claim with evidence gets the source's own words and the exact place they are."""
    wanted = [c for c in claims if c.verdict != "NOT_FOUND" and (c.evidence or c.claim)]
    passages = split(source_text)
    if not wanted or not passages:
        return claims
    queries = [f"{c.evidence or ''} {c.claim}".strip() for c in wanted]
    scores = await rank(queries, passages)
    found: Dict[int, Tuple[str, str]] = {}
    for c, q, row in zip(wanted, queries, scores):
        i = max(range(len(row)), key=lambda k: row[k])
        if row[i] >= LOCATE_MIN_SCORE:
            found[id(c)] = (passages[i].label, _best_sentences(passages[i], q))
    out = []
    for c in claims:
        if id(c) in found:
            label, quote = found[id(c)]
            c = c.model_copy(update={"source_location": label, "source_quote": quote})
        out.append(c)
    return out


async def checked_claims(results: Sequence[Any], source_text: str) -> List[SourceClaim]:
    """The model's answers through the safeguards, each placed in the source with the source's own words."""
    return await locate(_to_claims(results, source_text), source_text)
