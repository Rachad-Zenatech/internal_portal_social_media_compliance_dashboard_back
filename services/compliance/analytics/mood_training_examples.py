"""What the decision model was trained on: the examples of the dataset behind the current model, for the Training Data page."""

from typing import Any, Dict, Optional

from services.ai.core import mood_store
from services.compliance.analytics import mood_dataset, mood_model

MAX_EXAMPLES = 200


async def _current_version() -> Optional[str]:
    """The dataset the registered model learned from, else the newest finished dataset."""
    status = await mood_model.model_status()
    version = (status.get("training_summary") or {}).get("dataset_version")
    if version:
        return str(version)
    datasets = await mood_store.find(mood_store.DATASETS, {"status": "READY"}, sort=[("created_at", -1)], limit=1)
    return str(datasets[0]["_id"]) if datasets else None


async def training_examples(version: Optional[str] = None) -> Dict[str, Any]:
    """{dataset_version, total, splits, examples[]}: each example is one reviewed assessment (features, the system's
    recommendation, the reviewer's label and reason, split) and the draft it came from (submission_id)."""
    version = version or await _current_version()
    if not version or not mood_store.is_enabled():
        return {"dataset_version": None, "total": 0, "splits": {}, "examples": []}
    try:
        _, examples = await mood_dataset.load_dataset(version)
    except mood_dataset.DatasetError:
        return {"dataset_version": version, "total": 0, "splits": {}, "examples": []}
    splits: Dict[str, int] = {}
    for e in examples:
        splits[e.get("split") or "unknown"] = splits.get(e.get("split") or "unknown", 0) + 1
    rows = [mood_dataset.training_row(e) for e in examples[-MAX_EXAMPLES:]]
    return {"dataset_version": version, "total": len(examples), "splits": splits, "examples": rows}
