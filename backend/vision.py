
from __future__ import annotations

import base64
import io
from typing import Dict, Any

import cv2
import numpy as np
from PIL import Image


# Pre-load the Haar cascade once
_CASCADE = cv2.CascadeClassifier(
    cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
)


def _decode_data_url(data_url: str) -> np.ndarray:
    """Convert a base64 data URL (image/jpeg|png) into a BGR numpy array."""
    if "," in data_url:
        _, b64 = data_url.split(",", 1)
    else:
        b64 = data_url
    raw = base64.b64decode(b64)
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    arr = np.array(img)
    return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)


def _estimate_age_from_face(face_gray: np.ndarray) -> int:
    """
    Heuristic age estimation. Uses image statistics that empirically
    correlate with age (skin texture variance, edge density).
    Returns an integer in [18, 70].
    """
    face_gray = cv2.resize(face_gray, (128, 128))

    # Edge density: more wrinkles -> higher edge density -> older
    edges = cv2.Canny(face_gray, 60, 120)
    edge_ratio = float(np.count_nonzero(edges)) / edges.size  # 0..1

    # Texture variance via Laplacian
    lap = cv2.Laplacian(face_gray, cv2.CV_64F)
    tex_var = float(lap.var())

    # Map features -> age
    # baseline 22, +up to ~35 from edges, +up to ~15 from texture
    age = 22 + (edge_ratio * 200) + min(tex_var / 80.0, 15)
    age = int(round(max(18, min(70, age))))
    return age


def estimate_age(image_data_url: str) -> Dict[str, Any]:
    """
    Public entry point. Accepts a data URL (from a browser canvas snapshot)
    and returns:
        {
          face_detected: bool,
          estimated_age: int | None,
          confidence: float (0..1),
          face_box: [x, y, w, h] | None
        }
    """
    try:
        bgr = _decode_data_url(image_data_url)
    except Exception as e:
        return {
            "face_detected": False,
            "estimated_age": None,
            "confidence": 0.0,
            "face_box": None,
            "error": f"decode_error: {e}",
        }

    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    faces = _CASCADE.detectMultiScale(gray, scaleFactor=1.2, minNeighbors=5,
                                      minSize=(60, 60))

    if len(faces) == 0:
        return {
            "face_detected": False,
            "estimated_age": None,
            "confidence": 0.0,
            "face_box": None,
        }

    # Use the largest face
    x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
    face_crop = gray[y:y + h, x:x + w]
    age = _estimate_age_from_face(face_crop)

    # Confidence: larger faces and centered faces score higher
    img_h, img_w = gray.shape
    size_score = min(1.0, (w * h) / (img_w * img_h * 0.05))
    cx, cy = x + w / 2, y + h / 2
    center_dist = np.sqrt(((cx - img_w / 2) / img_w) ** 2 +
                          ((cy - img_h / 2) / img_h) ** 2)
    center_score = max(0.0, 1.0 - center_dist * 2)
    confidence = round(0.6 * size_score + 0.4 * center_score, 2)

    return {
        "face_detected": True,
        "estimated_age": age,
        "confidence": float(confidence),
        "face_box": [int(x), int(y), int(w), int(h)],
    }


def liveness_check(image_data_url: str) -> Dict[str, Any]:
    """
    Lightweight liveness signal — checks that the frame is not a uniform image
    (a printed photo would have low color variance / no skin tone histogram).
    Production systems use blink detection, depth, or challenge-response.
    """
    try:
        bgr = _decode_data_url(image_data_url)
    except Exception:
        return {"is_live": False, "score": 0.0}

    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    # Skin range in HSV
    lower = np.array([0, 30, 60], dtype=np.uint8)
    upper = np.array([25, 200, 255], dtype=np.uint8)
    skin_mask = cv2.inRange(hsv, lower, upper)
    skin_ratio = float(np.count_nonzero(skin_mask)) / skin_mask.size

    color_var = float(bgr.std())
    score = round(min(1.0, skin_ratio * 4 + color_var / 200), 2)
    return {"is_live": score > 0.4, "score": score, "skin_ratio": round(skin_ratio, 3)}
