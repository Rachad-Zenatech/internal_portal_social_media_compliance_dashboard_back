"""Identity matching between findings from successive sentiment analyses."""

from models.compliance.sentiment_model import SentimentFinding
from services.compliance.sentiment.evidence import _norm


def _same_finding(a: SentimentFinding, b: SentimentFinding) -> bool:
    """Whether two runs flagged the same thing: the same id, or (wording rewritten slightly, or a visual described
    differently) the same kind of finding where one's wording contains the other's."""
    if a.id and a.id == b.id:
        return True
    if a.basis != b.basis or a.image_index != b.image_index:
        return False
    x, y = _norm(a.quote), _norm(b.quote)
    if not (x and y and (x in y or y in x)):
        return False
    # Video moments must also roughly line up.
    return a.start_sec is None or b.start_sec is None or abs(a.start_sec - b.start_sec) <= 3
