"""
Pose-estimation rep counter for pushups and squats using MediaPipe Pose Landmarker.
Packaging for SportNerdBot core/cv (Contract 8.3).
Includes front-camera vertical motion detection for pushups.
"""
import json
import logging
import os
import subprocess
import time
import uuid
from typing import Dict, List, Optional, Tuple, Any

logger = logging.getLogger(__name__)

# Optional/lazy-imported dependencies
try:
    import numpy as np
except ImportError:
    np = None

try:
    import cv2
except ImportError:
    cv2 = None

try:
    import mediapipe as mp
    from mediapipe.tasks.python import vision, BaseOptions
except ImportError:
    mp = None
    vision = None
    BaseOptions = None

try:
    from scipy.signal import find_peaks, savgol_filter
except ImportError:
    find_peaks = None
    savgol_filter = None

DEFAULT_CFG = dict(target_fps=8, max_side=256, model="lite")
# landmark ids: (shoulder, elbow, wrist, hip, knee, ankle)
SIDES = {"left": (11, 13, 15, 23, 25, 27), "right": (12, 14, 16, 24, 26, 28)}
DEFAULTS = {"pushup": (100, 150), "squat": (110, 160)}


def is_cv_available() -> bool:
    """Перевіряє чи встановлені всі необхідні CV/ML бібліотеки (cv2, mediapipe, numpy, scipy)."""
    return all(x is not None for x in (np, cv2, mp, vision, BaseOptions, find_peaks, savgol_filter))


def find_model_path(model_type: str = "lite") -> Optional[str]:
    """Шукає файл моделі pose_landmarker_{model_type}.task у відомих директоріях."""
    env_override = os.environ.get("POSE_MODEL_PATH")
    if env_override and os.path.isfile(env_override):
        return env_override

    filename = f"pose_landmarker_{model_type}.task"
    candidates = [
        os.path.join(os.path.dirname(__file__), "models", filename),
        os.path.join(os.path.dirname(__file__), "..", "..", "optionToConsider", "cv_test", filename),
        f"/opt/sportnerdbot/models/{filename}",
        f"/var/lib/sportnerdbot/models/{filename}",
        f"/workspace/pipbot/optionToConsider/cv_test/{filename}",
    ]
    for c in candidates:
        abs_c = os.path.abspath(c)
        if os.path.isfile(abs_c):
            return abs_c
    return None


def probe(path: str) -> dict:
    """Витягує метадані відео через ffprobe."""
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", path
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
        j = json.loads(out)
        v = next((s for s in j.get("streams", []) if s.get("codec_type") == "video"), {})
        rot = int(v.get("tags", {}).get("rotate", 0) or 0)
        for sd in v.get("side_data_list", []):
            if "rotation" in sd:
                rot = int(sd["rotation"])
        rate_parts = v.get("r_frame_rate", "25/1").split("/")
        fps = float(rate_parts[0]) / float(rate_parts[1]) if len(rate_parts) == 2 and float(rate_parts[1]) > 0 else 25.0
        dur_val = j.get("format", {}).get("duration", 0) or v.get("duration", 0) or 0
        duration = float(dur_val)
        return {
            "duration": duration,
            "fps": fps,
            "width": int(v.get("width", 640) or 640),
            "height": int(v.get("height", 480) or 480),
            "rotation": rot,
        }
    except Exception as e:
        logger.warning("ffprobe failed on %s: %s", path, e)
        return {"duration": 0.0, "fps": 25.0, "width": 640, "height": 480, "rotation": 0}


def count_hysteresis(sig: Any, lo: float, hi: float) -> Tuple[int, int, List[int]]:
    """
    Чистий Python гістерезис для підрахунку повторень.
    Повний rep = up(>hi) -> down(<lo) -> up(>hi).
    Частковий = пішов нижче середини, але не дійшов до lo.
    """
    state, reps, partial, minseen, events = None, 0, 0, float("inf"), []
    mid = (lo + hi) / 2.0
    for i, a in enumerate(sig):
        if a is None or (isinstance(a, float) and a != a):  # NaN check
            continue
        if state is None:
            state = "up" if a > hi else ("down" if a < lo else None)
        elif state == "up":
            minseen = min(minseen, a)
            if a < lo:
                state = "down"
            elif a > hi and minseen < mid and minseen >= lo:
                partial += 1
                minseen = float("inf")
            elif a > hi:
                minseen = float("inf")
        elif state == "down" and a > hi:
            state = "up"
            reps += 1
            events.append(i)
            minseen = float("inf")
    return reps, partial, events


def interp_nan(x: Any) -> Any:
    if np is None:
        return x
    x = x.copy()
    m = np.isnan(x)
    if m.all():
        return x
    if not m.any():
        return x
    x[m] = np.interp(np.flatnonzero(m), np.flatnonzero(~m), x[~m])
    return x


def angle(a: Any, b: Any, c: Any) -> Any:
    if np is None:
        raise RuntimeError("numpy required")
    ba, bc = a - b, c - b
    cos = np.sum(ba * bc, -1) / (np.linalg.norm(ba, axis=-1) * np.linalg.norm(bc, axis=-1) + 1e-9)
    return np.degrees(np.arccos(np.clip(cos, -1, 1)))


def analyze_video(
    path: str,
    *,
    request_id: Optional[str] = None,
    file_unique_id: Optional[str] = None,
    target_fps: Optional[int] = None,
    max_side: Optional[int] = None,
    model: Optional[str] = None,
    model_path: Optional[str] = None,
    plot: bool = False,
) -> Dict[str, Any]:
    """
    Аналізує відео через MediaPipe Pose Landmarker з підтримкою фронтальних віджимань.
    Повертає словник за форматом контракту 8.3 (ARCHITECTURE).
    """
    t0 = time.time()
    req_id = request_id or str(uuid.uuid4())
    fuid = file_unique_id or ""

    if not os.path.isfile(path):
        return _make_result(
            req_id, fuid, 0.0, "none", 0, 0.0, ["file_not_found"], False, time.time() - t0
        )

    meta = probe(path)
    duration_s = meta["duration"]

    if not is_cv_available():
        logger.warning("CV dependencies (mediapipe/cv2/numpy/scipy) not installed")
        return _make_result(
            req_id, fuid, duration_s, "none", 0, 0.0, ["missing_cv_deps"], False, time.time() - t0
        )

    cfg = dict(DEFAULT_CFG)
    if target_fps is not None:
        cfg["target_fps"] = target_fps
    if max_side is not None:
        cfg["max_side"] = max_side
    if model is not None:
        cfg["model"] = model

    m_path = model_path or find_model_path(cfg["model"])
    if not m_path or not os.path.isfile(m_path):
        logger.error("Pose landmarker model not found: %s", m_path)
        return _make_result(
            req_id, fuid, duration_s, "none", 0, 0.0, ["model_not_found"], False, time.time() - t0
        )

    try:
        P, V, eff_fps = _run_pose(path, meta, m_path, **cfg)
    except Exception as e:
        logger.exception("Pose detection failed: %s", e)
        return _make_result(
            req_id, fuid, duration_s, "none", 0, 0.0, ["pose_failed", str(e)[:60]], False, time.time() - t0
        )

    return _analyze_landmarks(P, V, eff_fps, meta, req_id, fuid, t0)


def _run_pose(path: str, meta: dict, model_path: str, target_fps: int = 8, max_side: int = 256, model: str = "lite") -> Tuple[Any, Any, float]:
    cap = cv2.VideoCapture(path)
    src_fps = cap.get(cv2.CAP_PROP_FPS) or meta.get("fps", 25.0)
    step = max(1, int(round(src_fps / target_fps))) if target_fps else 1
    opts = vision.PoseLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=model_path),
        running_mode=vision.RunningMode.VIDEO,
        num_poses=1,
    )
    pts, vis, idx = [], [], 0
    with vision.PoseLandmarker.create_from_options(opts) as lm:
        while True:
            if idx % step:
                if not cap.grab():
                    break
                idx += 1
                continue
            ok, fr = cap.read()
            if not ok:
                break
            h, w = fr.shape[:2]
            s = min(1.0, max_side / max(h, w)) if max_side else 1.0
            if s < 1:
                fr = cv2.resize(fr, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
            img = mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(fr, cv2.COLOR_BGR2RGB))
            r = lm.detect_for_video(img, int(idx * 1000 / src_fps))
            idx += 1
            if r.pose_landmarks:
                L = r.pose_landmarks[0]
                pts.append([[p.x * w, p.y * h] for p in L])
                vis.append([p.visibility for p in L])
            else:
                pts.append([[np.nan, np.nan]] * 33)
                vis.append([0.0] * 33)
    cap.release()
    return np.array(pts, float), np.array(vis, float), src_fps / step


def _analyze_landmarks(
    P: Any,
    V: Any,
    fps: float,
    meta: dict,
    request_id: str,
    file_unique_id: str,
    t0: float,
) -> Dict[str, Any]:
    n = len(P)
    duration_s = meta.get("duration", 0.0)
    flags: List[str] = []

    if n == 0:
        return _make_result(request_id, file_unique_id, duration_s, "none", 0, 0.0, ["empty_video"], False, time.time() - t0)

    detected = float(np.mean(V.max(1) > 0))
    if detected < 0.4:
        flags.append("low_body_visibility")
        return _make_result(request_id, file_unique_id, duration_s, "none", 0, round(detected, 2), flags, False, time.time() - t0)

    # 1. Torso orientation & geometry
    sh = P[:, [11, 12]].mean(1)
    hp = P[:, [23, 24]].mean(1)
    d = hp - sh
    torso = np.degrees(np.arctan2(np.abs(d[:, 1]), np.abs(d[:, 0])))
    torso_med = float(np.nanmedian(torso))

    # 2. Vertical shoulder/nose motion signal (for front-camera & side-camera pushups)
    # Nose (0) + mid-shoulder (11, 12) vertical position (y in frame)
    y_raw = np.nanmean(P[:, [0, 11, 12], 1], axis=1)
    sw = float(np.nanmedian(np.abs(P[:, 11, 0] - P[:, 12, 0])))
    if not sw or np.isnan(sw) or sw < 5.0:
        sw = 100.0  # fallback normalization

    # Inverted vertical signal: higher value = arms extended (top), lower value = chest to floor (bottom)
    vert_sig = - (y_raw / sw)
    vert_sig_clean = interp_nan(vert_sig)
    win = max(5, int(round(fps * 0.3)) | 1)
    vert_sm = savgol_filter(vert_sig_clean, win, 2) if len(vert_sig_clean) > win else vert_sig_clean
    v_smin, v_smax = float(np.percentile(vert_sm, 3)), float(np.percentile(vert_sm, 97))
    vert_rng = v_smax - v_smin

    # Lower body visibility (hips, knees, ankles)
    lower_vis = float(np.mean(V[:, [23, 24, 25, 26, 27, 28]].max(1) > 0.4))

    # Knee angle range for squat check
    side_vis_leg = {s: float(np.nanmean(V[:, [ids[k] for k in (3, 4, 5)]])) for s, ids in SIDES.items()}
    side_leg = max(side_vis_leg, key=side_vis_leg.get)
    knee_raw = angle(P[:, SIDES[side_leg][3]], P[:, SIDES[side_leg][4]], P[:, SIDES[side_leg][5]])
    knee_sig = interp_nan(knee_raw)
    knee_sm = savgol_filter(knee_sig, win, 2) if len(knee_sig) > win else knee_sig
    knee_rng = float(np.percentile(knee_sm, 97) - np.percentile(knee_sm, 3))

    # 3. Exercise classification
    # Pushup criteria:
    # a) Horizontal torso (side-view pushup, torso < 45 deg)
    # b) OR vertical shoulder/nose amplitude >= 0.20 with lower body behind shoulders (front-camera pushup)
    # Squat criteria:
    # a) Upright torso (torso >= 45 deg) AND knee range >= 35 deg AND good lower body visibility
    is_front_pushup = (vert_rng >= 0.20 and (knee_rng < 35 or lower_vis < 0.60))
    is_side_pushup = (torso_med < 45)

    if is_side_pushup or is_front_pushup:
        exercise = "pushup"
    elif knee_rng >= 35 and torso_med >= 45:
        exercise = "squat"
    else:
        # Check if any significant motion exists
        if vert_rng < 0.15 and knee_rng < 25:
            flags.append("low_amplitude_motion")
            return _make_result(request_id, file_unique_id, duration_s, "none", 0, 0.9, flags, False, time.time() - t0)
        exercise = "squat" if torso_med >= 55 else "pushup"

    # 4. Counting reps with hysteresis
    if exercise == "pushup":
        # Always use vertical shoulder/nose signal for pushup counting (robust for both front and side camera)
        if vert_rng < 0.18:
            flags.append("small_pushup_range")
            return _make_result(request_id, file_unique_id, duration_s, "none", 0, 0.2, flags, False, time.time() - t0)

        lo = v_smin + 0.35 * vert_rng
        hi = v_smax - 0.25 * vert_rng
        reps, partial, events = count_hysteresis(vert_sm, lo, hi)
        pk, _ = find_peaks(-vert_sm, prominence=max(0.10, 0.25 * vert_rng), distance=max(1, int(fps * 0.5)))
        peaks = len(pk)
        primary_rng = vert_rng
    else:
        # Squat: count via knee angle
        if knee_rng < 30:
            flags.append("small_squat_range")
            return _make_result(request_id, file_unique_id, duration_s, "none", 0, 0.2, flags, False, time.time() - t0)

        lo_d, hi_d = DEFAULTS["squat"]
        k_min, k_max = float(np.percentile(knee_sm, 3)), float(np.percentile(knee_sm, 97))
        lo = k_min + 0.35 * knee_rng
        hi = k_max - 0.25 * knee_rng
        reps, partial, events = count_hysteresis(knee_sm, lo, hi)
        pk, _ = find_peaks(-knee_sm, prominence=max(15, 0.35 * knee_rng), distance=max(1, int(fps * 0.5)))
        peaks = len(pk)
        primary_rng = knee_rng

    # Confidence score calculation (0.0 to 1.0)
    confidence = 0.85
    if detected > 0.8:
        confidence += 0.05
    if abs(peaks - reps) > 3:
        confidence -= 0.15
        flags.append("peak_hysteresis_divergence")
    if reps == 0:
        confidence = 0.3
        valid = False
    else:
        valid = True

    confidence = max(0.1, min(0.98, confidence))

    return _make_result(
        request_id=request_id,
        file_unique_id=file_unique_id,
        duration_s=duration_s,
        exercise=exercise,
        reps=reps,
        confidence=confidence,
        flags=flags,
        valid=valid,
        elapsed_s=time.time() - t0,
    )


def _make_result(
    request_id: str,
    file_unique_id: str,
    duration_s: float,
    exercise: str,
    reps: int,
    confidence: float,
    flags: List[str],
    valid: bool,
    elapsed_s: float,
) -> Dict[str, Any]:
    """Форматує словник згідно з контрактом 8.3 (ARCHITECTURE)."""
    return {
        "contract_version": "1.0",
        "request_id": request_id,
        "file_unique_id": file_unique_id,
        "duration_s": round(float(duration_s), 2),
        "exercise": exercise,
        "reps": int(reps),
        "confidence": round(float(confidence), 2),
        "vision_class": None,
        "pose_class": exercise,
        "flags": flags,
        "valid": bool(valid),
        "analyzer_version": "cv-1.0",
        "elapsed_s": round(float(elapsed_s), 2),
    }
