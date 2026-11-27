"""Local CPU face detection using OpenCV YuNet, returning anonymous [top, left, bottom, right] 0..1000 boxes."""

import logging
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

_MODEL_PATH = Path(__file__).with_name("face_detection_yunet_2023mar.onnx")
_SCORE_THRESHOLD = 0.78
_NMS_THRESHOLD = 0.3
_MAX_FACES = 20
_MAX_SIDE = 1600
_MIN_BOX_PX = 24


def detect_faces(image_bytes: bytes) -> Optional[List[List[int]]]:
    """Detect faces locally with InsightFace first, falling back to OpenCV YuNet if unavailable."""
    from services.compliance.media import face_recognition_service

    recognized = face_recognition_service.detect_faces_and_embeddings(image_bytes)
    if recognized is not None:
        return recognized[0]
    if not image_bytes or not _MODEL_PATH.is_file():
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
        detector = cv2.FaceDetectorYN.create(str(_MODEL_PATH), "", (w, h), _SCORE_THRESHOLD, _NMS_THRESHOLD, 5000)
        _, raw = detector.detect(img)
    except Exception as exc:
        logger.warning("Local YuNet face detection failed, falling back to Gemini: %s", exc)
        return None

    if raw is None or len(raw) == 0:
        return []
    boxes: List[List[int]] = []
    for row in raw:
        x, y, bw, bh = float(row[0]), float(row[1]), float(row[2]), float(row[3])
        if bw < _MIN_BOX_PX or bh < _MIN_BOX_PX or max(bw / bh, bh / bw) > 2.2:
            continue
        top = max(0, min(1000, int(round(y / h * 1000))))
        left = max(0, min(1000, int(round(x / w * 1000))))
        bottom = max(0, min(1000, int(round((y + bh) / h * 1000))))
        right = max(0, min(1000, int(round((x + bw) / w * 1000))))
        if bottom > top and right > left:
            boxes.append([top, left, bottom, right])
        if len(boxes) == _MAX_FACES:
            break
    boxes.sort(key=lambda b: (b[1], b[0]))
    return boxes
