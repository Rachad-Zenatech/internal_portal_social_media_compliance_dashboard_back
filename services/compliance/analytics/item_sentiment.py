"""Item-level sentiment models: general X/social RoBERTa, FinBERT for financial news, and VADER fallback."""

import logging
import os
import re
import threading
from functools import lru_cache
from typing import Any, Callable, Dict, Optional, Tuple

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

logger = logging.getLogger(__name__)

_VADER = SentimentIntensityAnalyzer()

# VADER's built-in lexicon rates words from -4.0 (most negative) to +4.0 (most positive).
# Words with a lexicon valence <= -2.0 represent severe negative/crisis intensity.
SEVERE_VALENCE_THRESHOLD = -2.0
SEVERE_COMPOUND_THRESHOLD = -0.60

LOCAL_MODEL_NAME = os.getenv("SENTIMENT_MODEL", "cardiffnlp/twitter-roberta-base-sentiment-latest").strip()
FINANCIAL_MODEL_NAME = os.getenv("FINANCIAL_SENTIMENT_MODEL", "ProsusAI/finbert").strip()

_financial_model: Any = None  # (tokenizer, model), or False once disabled/unavailable
_financial_lock = threading.Lock()


def _has_memory_for_hf() -> bool:
    """Avoid Linux OOM kills when loading ~500MB PyTorch models on memory-tight hosts."""
    min_mb = int(os.getenv("HF_MIN_AVAILABLE_MB", "1500"))
    if min_mb <= 0 or not os.path.exists("/proc/meminfo"):
        return True
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    avail_mb = int(line.split()[1]) // 1024
                    if avail_mb < min_mb:
                        logger.warning(
                            "Skipping HuggingFace sentiment model load (MemAvailable=%dMB < %dMB); using VADER",
                            avail_mb,
                            min_mb,
                        )
                        return False
                    return True
    except Exception:
        pass
    return True


def load_hf_model_from_hub(model_name: str) -> Any:
    """Load a HuggingFace sequence-classification model once; return False when disabled or unavailable."""
    name = (model_name or "").strip()
    if not name or name.lower() == "vader":
        return False
    if not _has_memory_for_hf():
        return False
    try:
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        try:
            torch.set_num_threads(max(1, int(os.getenv("TORCH_NUM_THREADS", "1"))))
        except Exception:
            pass

        try:
            tok = AutoTokenizer.from_pretrained(name, local_files_only=True)
            mdl = AutoModelForSequenceClassification.from_pretrained(name, local_files_only=True).eval()
        except Exception:
            tok = AutoTokenizer.from_pretrained(name)
            mdl = AutoModelForSequenceClassification.from_pretrained(name).eval()
        return (tok, mdl)
    except Exception as exc:
        logger.warning("Sentiment model %s unavailable, using VADER: %s", name, exc)
        return False


def load_financial_model() -> Any:
    """Load the financial sentiment model (FINANCIAL_SENTIMENT_MODEL, default ProsusAI/finbert) once."""
    global _financial_model
    if _financial_model is not None:
        return _financial_model
    with _financial_lock:
        if _financial_model is None:
            _financial_model = load_hf_model_from_hub(FINANCIAL_MODEL_NAME)
        return _financial_model


# The same headlines are scored on every public-mood view; keying by the loaded (tokenizer, model) tuple guarantees
# FinBERT and the general X model never share cached scores.
@lru_cache(maxsize=20000)
def run_hf_model(text: str, loaded: Tuple[Any, Any]) -> Optional[Dict[str, float]]:
    """Run an already-loaded HuggingFace classifier and return {'POSITIVE', 'NEUTRAL', 'NEGATIVE'} probs."""
    try:
        import torch

        tok, mdl = loaded
        with torch.no_grad():
            enc = tok(text, return_tensors="pt", truncation=True, max_length=256)
            probs = torch.softmax(mdl(**enc).logits[0], dim=-1).tolist()
        labels = [mdl.config.id2label[i].upper() for i in range(len(probs))]
        out = {lbl: prob for lbl, prob in zip(labels, probs)}
        return out if {"POSITIVE", "NEUTRAL", "NEGATIVE"} <= set(out) else None
    except Exception as exc:
        logger.warning("Local sentiment model failed on a text, using VADER: %s", exc)
        return None


def model_probs_for(text: str, loader: Callable[[], Any]) -> Optional[Dict[str, float]]:
    """Return class probabilities from the model returned by `loader`, or None to fall back to VADER."""
    loaded = loader()
    if not loaded:
        return None
    out = run_hf_model(text, loaded)
    return dict(out) if out is not None else None


def score_with_probs(text: str, probs: Optional[Dict[str, float]], *, model_name: str) -> Dict[str, Any]:
    """Build the final sentiment dict from HuggingFace probabilities or VADER when `probs` is None."""
    cleaned = (text or "").strip()
    if not cleaned:
        return {"sentiment": "NEUTRAL", "confidence": 50.0, "compound": 0.0, "crisis_flags": []}

    tokens = re.findall(r"[a-z0-9'-]+", cleaned.lower())
    crisis_flags = list(
        dict.fromkeys(w for w in tokens if _VADER.lexicon.get(w, 0.0) <= SEVERE_VALENCE_THRESHOLD)
    )
    if probs:
        label = max(("POSITIVE", "NEUTRAL", "NEGATIVE"), key=lambda k: probs[k])
        return {
            "sentiment": label,
            "confidence": round(min(99.0, probs[label] * 100.0), 1),
            "compound": round(probs["POSITIVE"] - probs["NEGATIVE"], 3),
            # Crisis words only count when the model reads the text negatively: e.g. military defense contracts
            # mentioning "kill enemy threats" are not brand crises.
            "crisis_flags": crisis_flags if label == "NEGATIVE" else [],
            "model": model_name,
        }

    scores = _VADER.polarity_scores(cleaned)
    compound = float(scores["compound"])
    if compound >= 0.05:
        label = "POSITIVE"
        conf = min(99.0, round(60.0 + abs(compound) * 39.0, 1))
    elif compound <= -0.05:
        label = "NEGATIVE"
        conf = min(99.0, round(60.0 + abs(compound) * 39.0, 1))
    else:
        label = "NEUTRAL"
        conf = min(98.0, round(70.0 + (1.0 - abs(compound)) * 25.0, 1))

    return {
        "sentiment": label,
        "confidence": conf,
        "compound": round(compound, 3),
        "crisis_flags": crisis_flags,
    }


def _financial_probs(text: str) -> Optional[Dict[str, float]]:
    return model_probs_for(text, load_financial_model)


def classify_financial_sentiment(text: str) -> Dict[str, Any]:
    """Classify financial/stock headlines with FinBERT (FINANCIAL_SENTIMENT_MODEL), falling back to VADER."""
    cleaned = (text or "").strip()
    if not cleaned:
        return {"sentiment": "NEUTRAL", "confidence": 50.0, "compound": 0.0, "crisis_flags": []}
    return score_with_probs(cleaned, _financial_probs(cleaned), model_name=FINANCIAL_MODEL_NAME)
