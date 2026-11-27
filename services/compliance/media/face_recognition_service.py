"""Local CPU face detection & 512d ArcFace recognition using InsightFace (buffalo_sc)."""
import asyncio
import json
import logging
import os
import threading
from typing import Dict, List, Optional, Tuple
from uuid import UUID

from postgresql_db.database import fetch_all, fetch_one, get_pool

logger = logging.getLogger(__name__)

_MODEL_NAME = os.getenv("INSIGHTFACE_MODEL", "buffalo_sc").strip() or "buffalo_sc"
_SCORE_THRESHOLD = float(os.getenv("FACE_DET_SCORE_THRESHOLD", "0.55"))
_MATCH_THRESHOLD = float(os.getenv("FACE_MATCH_THRESHOLD", "0.48"))
_IOU_DEDUP_THRESHOLD = 0.45
_MAX_FACES = 20
_MAX_SIDE = 1600
_MIN_BOX_PX = 24

_lock = threading.Lock()
_app = None


def _load_app():
    global _app
    if _app is not None:
        return _app
    with _lock:
        if _app is not None:
            return _app
        from insightface.app import FaceAnalysis

        app = FaceAnalysis(name=_MODEL_NAME, providers=["CPUExecutionProvider"])
        app.prepare(ctx_id=0, det_size=(640, 640))
        _app = app
        return _app


def _iou(a: List[int], b: List[int]) -> float:
    top = max(a[0], b[0])
    left = max(a[1], b[1])
    bottom = min(a[2], b[2])
    right = min(a[3], b[3])
    inter = max(0, bottom - top) * max(0, right - left)
    if inter <= 0:
        return 0.0
    area_a = max(1, (a[2] - a[0]) * (a[3] - a[1]))
    area_b = max(1, (b[2] - b[0]) * (b[3] - b[1]))
    return inter / (area_a + area_b - inter)


def detect_faces_and_embeddings(image_bytes: bytes) -> Optional[Tuple[List[List[int]], List[List[float]]]]:
    """Detect deduplicated faces and 512-dim normalized ArcFace embeddings, or None if unavailable."""
    if not image_bytes:
        return None
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    try:
        arr = np.frombuffer(image_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None or img.size == 0:
            return None
        h, w = img.shape[:2]
        scale = min(1.0, _MAX_SIDE / max(h, w))
        if scale < 1.0:
            img = cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
            h, w = img.shape[:2]
        raw = _load_app().get(img)
    except Exception as exc:
        logger.warning("InsightFace detection failed: %s", exc)
        return None

    candidates = []
    for face in raw or []:
        if float(getattr(face, "det_score", 0.0)) < _SCORE_THRESHOLD:
            continue
        x1, y1, x2, y2 = [float(v) for v in face.bbox]
        bw, bh = x2 - x1, y2 - y1
        if bw < _MIN_BOX_PX or bh < _MIN_BOX_PX or max(bw / bh, bh / bw) > 2.2:
            continue
        box = [
            max(0, min(1000, int(round(y1 / h * 1000)))),
            max(0, min(1000, int(round(x1 / w * 1000)))),
            max(0, min(1000, int(round(y2 / h * 1000)))),
            max(0, min(1000, int(round(x2 / w * 1000)))),
        ]
        emb = getattr(face, "normed_embedding", None)
        if box[2] <= box[0] or box[3] <= box[1] or emb is None:
            continue
        candidates.append((float(face.det_score), box, [float(v) for v in emb]))

    # Keep highest-confidence face when two detections overlap heavily.
    candidates.sort(key=lambda item: item[0], reverse=True)
    kept = []
    for item in candidates:
        if any(_iou(item[1], other[1]) > _IOU_DEDUP_THRESHOLD for other in kept):
            continue
        kept.append(item)
        if len(kept) == _MAX_FACES:
            break
    kept.sort(key=lambda item: (item[1][1], item[1][0]))
    return [k[1] for k in kept], [k[2] for k in kept]


def match_embeddings(
    embeddings: List[List[float]],
    saved: List[List[float]],
    threshold: float = _MATCH_THRESHOLD,
) -> Dict[int, float]:
    """Return {face_index: best_cosine_similarity} for faces matching any saved employee."""
    if not embeddings or not saved:
        return {}
    import numpy as np

    mat = np.asarray(saved, dtype=np.float32)
    matches: Dict[int, float] = {}
    for idx, emb in enumerate(embeddings):
        vec = np.asarray(emb, dtype=np.float32)
        sims = mat @ vec
        best = float(np.max(sims))
        if best >= threshold:
            matches[idx] = round(best, 3)
    return matches


def find_same_person_across_files(
    file_faces: List[Tuple[UUID, str, List[List[float]]]],
    threshold: float = _MATCH_THRESHOLD,
) -> Dict[str, List[str]]:
    """Return {face_key: [other_file_names]} when the same person appears in multiple photos of one post."""
    if len(file_faces) < 2:
        return {}
    import numpy as np

    out: Dict[str, List[str]] = {}
    for i, (fid_a, _name_a, embs_a) in enumerate(file_faces):
        if not embs_a:
            continue
        for idx_a, emb_a in enumerate(embs_a):
            vec_a = np.asarray(emb_a, dtype=np.float32)
            hits: List[str] = []
            for j, (fid_b, name_b, embs_b) in enumerate(file_faces):
                if i == j or fid_a == fid_b or not embs_b:
                    continue
                sims = np.asarray(embs_b, dtype=np.float32) @ vec_a
                if float(np.max(sims)) >= threshold and name_b not in hits:
                    hits.append(name_b)
            if hits:
                out[f"{fid_a}-{idx_a}"] = hits
    return out


async def _load_file_embeddings(file_id: UUID) -> Optional[Tuple[List[List[int]], List[List[float]]]]:
    from services.compliance.analytics.sentiment_service import _image_bytes
    from services.compliance.media.transcription_service import STORAGE_ROOT
    from services.shared.storage.s3_storage_service import ensure_local_file

    row = await fetch_one(
        "SELECT storage_path, mime_type FROM content_submission_files WHERE id = $1 AND deleted_at IS NULL",
        file_id,
    )
    if not row:
        return None
    path = (STORAGE_ROOT / row["storage_path"]).resolve()
    if STORAGE_ROOT not in path.parents:
        return None
    await asyncio.to_thread(ensure_local_file, path, "submissions", row["storage_path"])
    if not path.is_file():
        return None
    data, _ = await asyncio.to_thread(_image_bytes, path, row["mime_type"])
    return await asyncio.to_thread(detect_faces_and_embeddings, data)


async def sync_employee_faces(submission_id: UUID, face_flags: Dict[str, bool], user_id: UUID) -> None:
    """Save 512d embeddings for faces marked Employee=Yes, and remove any marked No."""
    if not isinstance(face_flags, dict) or not face_flags:
        return
    by_file: Dict[UUID, Dict[int, bool]] = {}
    for key, is_emp in face_flags.items():
        if "-" not in str(key):
            continue
        file_str, idx_str = str(key).rsplit("-", 1)
        try:
            fid, idx = UUID(file_str), int(idx_str)
        except ValueError:
            continue
        by_file.setdefault(fid, {})[idx] = bool(is_emp)

    pool = get_pool()
    for fid, idx_map in by_file.items():
        to_remove = [idx for idx, val in idx_map.items() if not val]
        if to_remove:
            await pool.execute(
                "DELETE FROM employee_face_embeddings WHERE file_id = $1 AND face_index = ANY($2::int[])",
                fid, to_remove,
            )
        to_save = [idx for idx, val in idx_map.items() if val]
        if not to_save:
            continue
        detected = await _load_file_embeddings(fid)
        if not detected:
            continue
        boxes, embeddings = detected
        for idx in to_save:
            if 0 <= idx < len(embeddings):
                await pool.execute(
                    """
                    INSERT INTO employee_face_embeddings (submission_id, file_id, face_index, embedding, source_box, created_by)
                    VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6)
                    ON CONFLICT (file_id, face_index) DO UPDATE
                    SET embedding = EXCLUDED.embedding, source_box = EXCLUDED.source_box, submission_id = EXCLUDED.submission_id
                    """,
                    submission_id, fid, idx, json.dumps(embeddings[idx]), json.dumps(boxes[idx]), user_id,
                )


async def auto_match_file_faces(file_id: UUID, embeddings: List[List[float]]) -> Dict[str, float]:
    """Compare newly detected faces against saved employee embeddings and auto-flag matches."""
    if not embeddings:
        return {}
    saved_rows = await fetch_all(
        "SELECT embedding FROM employee_face_embeddings WHERE file_id <> $1",
        file_id,
    )
    saved = [
        json.loads(r["embedding"]) if isinstance(r["embedding"], str) else r["embedding"]
        for r in saved_rows if r.get("embedding")
    ]
    matches = match_embeddings(embeddings, saved)
    if not matches:
        return {}
    sub = await fetch_one(
        """
        SELECT s.id, s.face_employee_flags, s.face_auto_matched
        FROM content_submissions s
        JOIN content_submission_files f ON f.submission_id = s.id
        WHERE f.id = $1 AND s.deleted_at IS NULL
        """,
        file_id,
    )
    if not sub:
        return {}
    flags = json.loads(sub["face_employee_flags"]) if isinstance(sub["face_employee_flags"], str) else dict(sub["face_employee_flags"] or {})
    auto_map = json.loads(sub["face_auto_matched"]) if isinstance(sub.get("face_auto_matched"), str) else dict(sub.get("face_auto_matched") or {})
    matched_keys: Dict[str, float] = {}
    for idx, sim in matches.items():
        key = f"{file_id}-{idx}"
        matched_keys[key] = sim
        auto_map[key] = sim
        if key not in flags:
            flags[key] = True
    any_emp = any(flags.values())
    await get_pool().execute(
        """
        UPDATE content_submissions
        SET face_employee_flags = $2::jsonb,
            face_auto_matched = $3::jsonb,
            employee_in_images = CASE WHEN $4 THEN TRUE ELSE employee_in_images END,
            employee_in_images_confirmed = CASE WHEN $4 THEN TRUE ELSE employee_in_images_confirmed END
        WHERE id = $1
        """,
        sub["id"], json.dumps(flags), json.dumps(auto_map), any_emp,
    )
    return matched_keys


async def refresh_submission_face_links(file_id: UUID) -> Dict[str, List[str]]:
    """Detect when the same person appears across multiple images in the same submission."""
    sub = await fetch_one(
        "SELECT submission_id FROM content_submission_files WHERE id = $1 AND deleted_at IS NULL",
        file_id,
    )
    if not sub:
        return {}
    rows = await fetch_all(
        """
        SELECT id, original_name FROM content_submission_files
        WHERE submission_id = $1 AND deleted_at IS NULL
          AND kind IN ('MAIN', 'IMAGE') AND mime_type LIKE 'image/%'
        ORDER BY created_at ASC
        """,
        sub["submission_id"],
    )
    if len(rows) < 2:
        return {}
    file_faces: List[Tuple[UUID, str, List[List[float]]]] = []
    for r in rows:
        detected = await _load_file_embeddings(r["id"])
        if detected and detected[1]:
            file_faces.append((r["id"], r["original_name"], detected[1]))
    links = find_same_person_across_files(file_faces)
    await get_pool().execute(
        "UPDATE content_submissions SET face_same_person = $2::jsonb WHERE id = $1",
        sub["submission_id"], json.dumps(links),
    )
    return links
