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
import warnings
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

DEFAULT_CFG = dict(target_fps=15, max_side=640, model="lite")
# landmark ids: (shoulder, elbow, wrist, hip, knee, ankle)
SIDES = {"left": (11, 13, 15, 23, 25, 27), "right": (12, 14, 16, 24, 26, 28)}
DEFAULTS = {"pushup": (100, 150), "squat": (110, 160)}


def is_cv_available() -> bool:
    """Перевіряє чи встановлені всі необхідні CV/ML бібліотеки (cv2, mediapipe, numpy, scipy)
    і чи завантажується нативна бібліотека mediapipe (потребує системних libEGL/libGLES)."""
    global _NATIVE_OK
    if not all(x is not None for x in (np, cv2, mp, vision, BaseOptions, find_peaks, savgol_filter)):
        return False
    if _NATIVE_OK is None:
        try:
            from mediapipe.tasks.python.core import mediapipe_c_bindings
            mediapipe_c_bindings.load_raw_library()
            _NATIVE_OK = True
        except Exception as e:  # OSError: libEGL.so.1 missing тощо
            logger.warning("MediaPipe native library unavailable: %s", e)
            _NATIVE_OK = False
    return _NATIVE_OK


_NATIVE_OK: Optional[bool] = None


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


# --- Класифікація вправи за вікнами (а не за всім відео) -------------------------------------------------
N_WINDOWS = 8
VOTE_CONFIDENT_SHARE = 0.65   # частка переможця у зважених голосах, нижче — «неоднозначно»
VOTE_MIXED_SHARE = 0.25       # частка «програвшої» вправи, вище якої ставимо mixed_exercises


def _classify_window(torso_med: float, vert_rng: float, knee_rng: float, knee_vis: float, lower_vis: float) -> Tuple[str, float]:
    """(клас вікна 'pushup'|'squat'|'idle'|'unknown', вага руху 0..1). Ті самі критерії, що й глобальні, але на вікні."""
    moving_v = vert_rng >= 0.15
    if torso_med < 45:
        # горизонтальний торс: віджимання лише якщо є вертикальний рух; інакше — планка/лежачи без повторів
        return ("pushup", min(vert_rng, 1.0)) if moving_v else ("idle", 0.0)
    if moving_v and (knee_vis < 0.40 or knee_rng < 35 or lower_vis < 0.60):
        return "pushup", min(vert_rng, 1.0)          # фронтальне віджимання
    if knee_rng >= 35 and knee_vis >= 0.40:
        return "squat", min(knee_rng / 90.0, 1.0)
    if vert_rng < 0.15 and (knee_rng < 25 or knee_vis < 0.40):
        return "idle", 0.0
    return "unknown", 0.0


def _window_votes(P: Any, V: Any, fps: float, torso: Any, knee_sm: Any, n_windows: int = N_WINDOWS) -> Dict[str, Any]:
    """
    Ділить відео на рівні вікна, класифікує кожне і зважено голосує за вправу. Дешево: numpy на ~8 вікнах.
    Повертає {'winner', 'share', 'weights': {pushup, squat}, 'labels': [...], 'bounds': [(a, b), ...]}.
    """
    n = len(P)
    n_win = max(1, min(n_windows, int(n // max(1.0, fps * 1.5))))
    edges = [int(round(i * n / n_win)) for i in range(n_win + 1)]
    labels: List[str] = []
    weights = {"pushup": 0.0, "squat": 0.0}
    bounds: List[Tuple[int, int]] = []
    for i in range(n_win):
        a, b = edges[i], edges[i + 1]
        bounds.append((a, b))
        if b - a < 3:
            labels.append("idle")
            continue
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)
            t_med = float(np.nanmedian(torso[a:b])) if not np.isnan(torso[a:b]).all() else 90.0
            _, _, v_rng = count_pushups_vy(P[a:b], fps)
            seg_knee = knee_sm[a:b]
            k_rng = float(np.percentile(seg_knee, 97) - np.percentile(seg_knee, 3))
            k_vis = float(np.nanmean(V[a:b, [25, 26]]))
            l_vis = float(np.mean(V[a:b][:, [23, 24, 25, 26, 27, 28]].max(1) > 0.4))
        lab, w = _classify_window(t_med, v_rng, k_rng, k_vis, l_vis)
        labels.append(lab)
        if lab in weights:
            weights[lab] += w
    total = weights["pushup"] + weights["squat"]
    if total <= 0:
        return {"winner": None, "share": 0.0, "weights": weights, "labels": labels, "bounds": bounds}
    winner = "pushup" if weights["pushup"] >= weights["squat"] else "squat"
    return {"winner": winner, "share": weights[winner] / total, "weights": weights, "labels": labels, "bounds": bounds}


def _winner_span(votes: Dict[str, Any]) -> Tuple[int, int]:
    """Кадровий проміжок вправи-переможця: від першого до останнього її вікна + прилеглі «idle»/unknown вікна без руху."""
    labels, bounds, win = votes["labels"], votes["bounds"], votes["winner"]
    idx = [i for i, l in enumerate(labels) if l == win]
    lo, hi = idx[0], idx[-1]
    while lo > 0 and labels[lo - 1] in ("idle",):
        lo -= 1
    while hi < len(labels) - 1 and labels[hi + 1] in ("idle",):
        hi += 1
    return bounds[lo][0], bounds[hi][1]


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
        if target_fps < 10:
            logger.warning("Target fps %s is below 10 fps: high risk of undercounting reps", target_fps)
    if max_side is not None:
        cfg["max_side"] = max_side
    # Якість важливіша за швидкість: fps не нижче 12, довга сторона 480-720
    if cfg["target_fps"] < 12:
        logger.warning("Target fps %s raised to minimum 12", cfg["target_fps"])
        cfg["target_fps"] = 12
    cfg["max_side"] = min(720, max(480, int(cfg["max_side"])))
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


def count_pushups_vy(P: Any, fps: float) -> Tuple[int, List[int], float]:
    """
    Computes pushup reps using vertical-motion signal vy
    (peaks of mean nose + shoulders y normalized by shoulder width),
    per mediapipe_limits.md and cvbench/split.py.
    Returns (reps, peak_indices, range).
    """
    if np is None or len(P) == 0:
        return 0, [], 0.0

    # Nose (0) + mid-shoulder (11, 12) vertical position (y in frame)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        y_raw = np.nanmean(P[:, [0, 11, 12], 1], axis=1)
        sw = float(np.nanmedian(np.abs(P[:, 11, 0] - P[:, 12, 0])))
    if np.isnan(y_raw).all() or not sw or np.isnan(sw) or sw < 1.0:
        return 0, [], 0.0

    y_norm = interp_nan(y_raw) / sw
    win = max(5, int(round(fps * 0.3)) | 1)
    if savgol_filter is not None and len(y_norm) > win:
        y_sm = savgol_filter(y_norm, win, 2)
    else:
        y_sm = y_norm

    rng = float(np.percentile(y_sm, 97) - np.percentile(y_sm, 3))
    min_dist = max(1, int(round(fps * 0.6)))
    prominence = max(0.15, 0.4 * rng)
    if find_peaks is not None:
        pk, _ = find_peaks(y_sm, prominence=prominence, distance=min_dist)
        return int(len(pk)), list(pk), rng
    return 0, [], rng


def is_unreliable_camera_angle(
    P: Any,
    V: Any,
    meta: dict,
    sh_y_frac: Optional[float] = None,
    lower_vis: Optional[float] = None,
    knee_vis: Optional[float] = None,
    sw_rel_rng: Optional[float] = None,
) -> bool:
    """
    Detects floor-level / low camera angles (camera from below, e.g. hoodie case in QZJQY9W)
    where lower body is cut off and perspective motion toward lens makes rep counting unreliable.
    """
    if np is None or len(P) == 0:
        return False

    h = meta.get("height", 0) or 480
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        if sh_y_frac is None:
            sh_y = np.nanmean(P[:, [11, 12], 1], axis=1)
            sh_y_frac = float(np.nanmedian(sh_y) / h) if h > 0 else 0.0

        if lower_vis is None:
            lower_vis = float(np.mean(V[:, [23, 24, 25, 26, 27, 28]].max(1) > 0.4))

        if knee_vis is None:
            knee_vis = float(np.nanmean(V[:, [25, 26]]))

        if sw_rel_rng is None:
            sw_series = np.abs(P[:, 11, 0] - P[:, 12, 0])
            sw_med = float(np.nanmedian(sw_series))
            sw_rel_rng = float((np.nanpercentile(sw_series, 95) - np.nanpercentile(sw_series, 5)) / sw_med) if sw_med > 5.0 else 0.0

    return bool(
        (sh_y_frac > 0.52 and (knee_vis < 0.25 or lower_vis < 0.48))
        or (sw_rel_rng > 1.8 and lower_vis < 0.50)
    )


def _run_pose(
    path: str,
    meta: dict,
    model_path: str,
    target_fps: int = 15,
    max_side: int = 640,
    model: str = "lite",
) -> Tuple[Any, Any, float]:
    """Runs pose landmarker: tries ffmpeg pre-decode first; falls back to cv2.VideoCapture."""
    try:
        res = _run_pose_ffmpeg(path, meta, model_path, target_fps=target_fps, max_side=max_side)
        if res is not None and len(res[0]) > 0:
            return res
    except Exception as e:
        logger.debug("ffmpeg pre-decode fallback to cv2 due to: %s", e)

    return _run_pose_cv2(path, meta, model_path, target_fps=target_fps, max_side=max_side)


def _run_pose_ffmpeg(
    path: str,
    meta: dict,
    model_path: str,
    target_fps: int = 15,
    max_side: int = 640,
) -> Optional[Tuple[Any, Any, float]]:
    orig_w = meta.get("width", 640)
    orig_h = meta.get("height", 480)
    rot = meta.get("rotation", 0)
    w, h = (orig_h, orig_w) if rot in (90, 270, -90, -270) else (orig_w, orig_h)
    src_fps = meta.get("fps", 25.0)
    step = max(1, int(round(src_fps / target_fps))) if target_fps else 1
    efps = src_fps / step
    s = min(1.0, max_side / max(w, h)) if max_side else 1.0
    w2, h2 = (int(w * s) // 2 * 2, int(h * s) // 2 * 2) if s < 1.0 else (w // 2 * 2, h // 2 * 2)

    vf = [f"fps={efps}", f"scale={w2}:{h2}:flags=area"]
    cmd = [
        "ffmpeg", "-nostdin", "-v", "error", "-threads", "2",
        "-i", path, "-vf", ",".join(vf),
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-"
    ]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=10**7)
    frame_bytes = w2 * h2 * 3

    opts = vision.PoseLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=model_path),
        running_mode=vision.RunningMode.VIDEO,
        num_poses=1,
    )
    pts, vis, idx = [], [], 0
    try:
        with vision.PoseLandmarker.create_from_options(opts) as lm:
            while True:
                b = p.stdout.read(frame_bytes)
                if len(b) < frame_bytes:
                    break
                rgb = np.frombuffer(b, np.uint8).reshape(h2, w2, 3)
                img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                r = lm.detect_for_video(img, int(idx * 1000 / efps))
                idx += 1
                if r.pose_landmarks:
                    L = r.pose_landmarks[0]
                    pts.append([[pt.x * w, pt.y * h] for pt in L])
                    vis.append([pt.visibility for pt in L])
                else:
                    pts.append([[np.nan, np.nan]] * 33)
                    vis.append([0.0] * 33)
    finally:
        p.stdout.close()
        p.wait()

    if idx == 0:
        return None
    return np.array(pts, float), np.array(vis, float), efps


def _run_pose_cv2(
    path: str,
    meta: dict,
    model_path: str,
    target_fps: int = 15,
    max_side: int = 640,
) -> Tuple[Any, Any, float]:
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

    if fps < 10.0:
        logger.warning("Sampling fps %.1f is below 10 fps: high risk of undercounting reps", fps)
        flags.append("low_fps_undercount_risk")

    if n == 0:
        return _make_result(request_id, file_unique_id, duration_s, "none", 0, 0.0, flags + ["empty_video"], False, time.time() - t0)

    detected = float(np.mean(V.max(1) > 0))
    if detected < 0.4:
        flags.append("low_body_visibility")
        return _make_result(request_id, file_unique_id, duration_s, "none", 0, round(detected, 2), flags, False, time.time() - t0)

    # 1. Torso orientation & geometry
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        sh = P[:, [11, 12]].mean(1)
        hp = P[:, [23, 24]].mean(1)
        d = hp - sh
        torso = np.degrees(np.arctan2(np.abs(d[:, 1]), np.abs(d[:, 0])))
        torso_med = float(np.nanmedian(torso))

        # Lower body visibility (hips, knees, ankles)
        lower_vis = float(np.mean(V[:, [23, 24, 25, 26, 27, 28]].max(1) > 0.4))
        knee_vis = float(np.nanmean(V[:, [25, 26]]))
        sh_y_frac = float(np.nanmedian(sh[:, 1]) / meta.get("height", 1.0)) if meta.get("height") else 0.0

        sw_series = np.abs(P[:, 11, 0] - P[:, 12, 0])
        sw_med = float(np.nanmedian(sw_series))
        sw_rel_rng = float((np.nanpercentile(sw_series, 95) - np.nanpercentile(sw_series, 5)) / sw_med) if sw_med > 5.0 else 0.0

    # Camera angle check: floor-level / low camera angles (from below, e.g. hoodie).
    # NEVER reject for angle: flag it, keep counting, lower confidence; the bot credits
    # the reps and adds a camera-placement hint for the next video.
    low_camera_angle = is_unreliable_camera_angle(
        P, V, meta, sh_y_frac=sh_y_frac, lower_vis=lower_vis, knee_vis=knee_vis, sw_rel_rng=sw_rel_rng
    )
    if low_camera_angle:
        flags.append("low_camera_angle")

    # 2. Vertical shoulder/nose motion signal (vy)
    reps_vy, pk_vy, vert_rng = count_pushups_vy(P, fps)

    # Knee angle range for squat check
    side_vis_leg = {s: float(np.nanmean(V[:, [ids[k] for k in (3, 4, 5)]])) for s, ids in SIDES.items()}
    side_leg = max(side_vis_leg, key=side_vis_leg.get)
    knee_raw = angle(P[:, SIDES[side_leg][3]], P[:, SIDES[side_leg][4]], P[:, SIDES[side_leg][5]])
    knee_sig = interp_nan(knee_raw)
    win = max(5, int(round(fps * 0.3)) | 1)
    knee_sm = savgol_filter(knee_sig, win, 2) if len(knee_sig) > win else knee_sig
    knee_rng = float(np.percentile(knee_sm, 97) - np.percentile(knee_sm, 3))

    # 3. Exercise classification — за вікнами по всьому відео (зважене голосування), а не за медіаною всього відео.
    # Критерії вікна (_classify_window) ті самі, що раніше були глобальними. Лише сегмент вправи-переможця
    # йде в підрахунок; якщо голоси розійшлись — прапорці ambiguous_exercise / mixed_exercises (м'яка підказка
    # для бота: «орієнтир — вправа дня»), відео не відхиляється.
    votes = _window_votes(P, V, fps, torso, knee_sm)
    seg_a, seg_b = 0, n
    if votes["winner"] is not None:
        exercise = votes["winner"]
        seg_a, seg_b = _winner_span(votes)
        loser = "squat" if exercise == "pushup" else "pushup"
        tot = votes["weights"]["pushup"] + votes["weights"]["squat"]
        if votes["share"] < VOTE_CONFIDENT_SHARE:
            flags.append("ambiguous_exercise")
        if tot > 0 and votes["weights"][loser] / tot >= VOTE_MIXED_SHARE:
            flags.append("mixed_exercises")
        if (seg_a, seg_b) != (0, n) and seg_b - seg_a >= 3:
            P_seg, V_seg = P[seg_a:seg_b], V[seg_a:seg_b]
            reps_vy, pk_vy, vert_rng = count_pushups_vy(P_seg, fps)
            knee_sm_seg = knee_sm[seg_a:seg_b]
            knee_rng = float(np.percentile(knee_sm_seg, 97) - np.percentile(knee_sm_seg, 3))
            knee_sm = knee_sm_seg
    else:
        # жодного активного вікна — стара глобальна логіка (й відсів низької амплітуди)
        is_front_pushup = (vert_rng >= 0.15 and (knee_vis < 0.40 or knee_rng < 35 or lower_vis < 0.60))
        is_side_pushup = (torso_med < 45)
        if is_side_pushup or is_front_pushup:
            exercise = "pushup"
        elif knee_rng >= 35 and torso_med >= 45 and knee_vis >= 0.40:
            exercise = "squat"
        else:
            if vert_rng < 0.15 and (knee_rng < 25 or knee_vis < 0.40):
                flags.append("low_amplitude_motion")
                return _make_result(request_id, file_unique_id, duration_s, "none", 0, 0.9, flags, False, time.time() - t0)
            exercise = "squat" if (torso_med >= 55 and knee_vis >= 0.40) else "pushup"

    # 4. Counting reps
    if exercise == "pushup":
        # Always use vertical-motion signal vy (nose+shoulders y peaks normalized by shoulder width)
        if vert_rng < 0.15:
            flags.append("small_pushup_range")
            return _make_result(request_id, file_unique_id, duration_s, "none", 0, 0.2, flags, False, time.time() - t0)

        reps = reps_vy
        peaks = reps_vy
        events = pk_vy
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

    # Confidence score calculation (0.0 to 1.0)
    confidence = 0.85
    if detected > 0.8:
        confidence += 0.05
    if "ambiguous_exercise" in flags:
        confidence -= 0.15
    if exercise == "squat" and abs(peaks - reps) > 3:
        confidence -= 0.15
        flags.append("peak_hysteresis_divergence")
    if reps == 0:
        confidence = 0.3
        valid = False
    else:
        valid = True

    if low_camera_angle and valid:
        confidence = min(confidence, 0.55)

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
