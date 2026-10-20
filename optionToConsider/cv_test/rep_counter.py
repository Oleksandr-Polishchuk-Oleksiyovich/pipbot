"""Pose-estimation rep counter (push-up vs squat) using MediaPipe Pose Landmarker (Tasks API)."""
import json, os, subprocess, sys, time
import cv2, numpy as np
import mediapipe as mp
from mediapipe.tasks.python import vision, BaseOptions
from scipy.signal import find_peaks, savgol_filter
import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
MODELS = {m: os.path.join(HERE, f"pose_landmarker_{m}.task") for m in ("lite", "full")}
DEFAULT_CFG = dict(target_fps=8, max_side=256, model="lite")  # chosen via bench.py on 2 cores
PLOTS = os.path.join(HERE, "plots")
# landmark ids: (shoulder, elbow, wrist, hip, knee, ankle)
SIDES = {"left": (11, 13, 15, 23, 25, 27), "right": (12, 14, 16, 24, 26, 28)}
DEFAULTS = {"pushup": (100, 150), "squat": (110, 160)}

def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
                         capture_output=True, text=True).stdout
    j = json.loads(out); v = next(s for s in j["streams"] if s["codec_type"] == "video")
    rot = int(v.get("tags", {}).get("rotate", 0) or 0)
    for sd in v.get("side_data_list", []):
        if "rotation" in sd: rot = int(sd["rotation"])
    n, d = v["r_frame_rate"].split("/")
    return dict(duration=float(j["format"]["duration"]), fps=float(n) / float(d),
                width=v["width"], height=v["height"], rotation=rot)

def angle(a, b, c):
    ba, bc = a - b, c - b
    cos = np.sum(ba * bc, -1) / (np.linalg.norm(ba, axis=-1) * np.linalg.norm(bc, axis=-1) + 1e-9)
    return np.degrees(np.arccos(np.clip(cos, -1, 1)))

def interp_nan(x):
    x = x.copy(); m = np.isnan(x)
    if m.all(): return x
    x[m] = np.interp(np.flatnonzero(m), np.flatnonzero(~m), x[~m]); return x

def count_hysteresis(sig, lo, hi):
    """Full rep = up(>hi) -> down(<lo) -> up(>hi). Partial = left up, went below mid, came back without reaching lo."""
    state, reps, partial, minseen, events = None, 0, 0, 999, []
    mid = (lo + hi) / 2
    for i, a in enumerate(sig):
        if state is None:
            state = "up" if a > hi else ("down" if a < lo else None)
        elif state == "up":
            minseen = min(minseen, a)
            if a < lo: state = "down"
            elif a > hi and minseen < mid and minseen >= lo: partial += 1; minseen = 999
            elif a > hi: minseen = 999
        elif state == "down" and a > hi:
            state = "up"; reps += 1; events.append(i); minseen = 999
    return reps, partial, events

def analyze_video(path, plot=True, target_fps=None, max_side=None, model=None, use_cache=False):
    cfg = dict(DEFAULT_CFG)
    for k, v in dict(target_fps=target_fps, max_side=max_side, model=model).items():
        if v is not None: cfg[k] = v
    t0 = time.time(); meta = probe(path)
    tag = f"{cfg['model']}_{cfg['target_fps']}_{cfg['max_side']}"
    cache = os.path.join(HERE, "cache", f"{os.path.basename(path)}.{tag}.npz")
    if use_cache and os.path.exists(cache):
        z = np.load(cache); P, V, fps = z["P"], z["V"], float(z["fps"])
    else:
        P, V, fps = _run_pose(path, meta, **cfg)
        if use_cache:
            os.makedirs(os.path.dirname(cache), exist_ok=True); np.savez(cache, P=P, V=V, fps=fps)
    r = _analyze(path, meta, P, V, fps, len(P), t0, plot)
    r["config"] = cfg; return r

def _run_pose(path, meta, target_fps=10, max_side=480, model="lite"):
    """Returns landmarks (pixel coords of ORIGINAL frame size), visibility, effective fps."""
    cap = cv2.VideoCapture(path)  # OpenCV auto-applies rotation metadata (CAP_PROP_ORIENTATION_AUTO)
    src_fps = cap.get(cv2.CAP_PROP_FPS) or meta["fps"]
    step = max(1, int(round(src_fps / target_fps))) if target_fps else 1
    opts = vision.PoseLandmarkerOptions(base_options=BaseOptions(model_asset_path=MODELS[model]),
                                        running_mode=vision.RunningMode.VIDEO, num_poses=1)
    pts, vis, idx = [], [], 0
    with vision.PoseLandmarker.create_from_options(opts) as lm:
        while True:
            if idx % step:
                if not cap.grab(): break
                idx += 1; continue
            ok, fr = cap.read()
            if not ok: break
            h, w = fr.shape[:2]
            s = min(1.0, max_side / max(h, w)) if max_side else 1.0
            if s < 1: fr = cv2.resize(fr, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
            img = mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(fr, cv2.COLOR_BGR2RGB))
            r = lm.detect_for_video(img, int(idx * 1000 / src_fps)); idx += 1
            if r.pose_landmarks:
                L = r.pose_landmarks[0]
                pts.append([[p.x * w, p.y * h] for p in L]); vis.append([p.visibility for p in L])
            else:
                pts.append([[np.nan, np.nan]] * 33); vis.append([0.0] * 33)
    cap.release()
    return np.array(pts, float), np.array(vis, float), src_fps / step

def _analyze(path, meta, P, V, fps, n, t0, plot):
    detected = float(np.mean(V.max(1) > 0))
    # torso orientation: angle of shoulder-mid -> hip-mid line vs horizontal
    sh, hp = P[:, [11, 12]].mean(1), P[:, [23, 24]].mean(1)
    d = hp - sh
    torso = np.degrees(np.arctan2(np.abs(d[:, 1]), np.abs(d[:, 0])))  # 0=horizontal, 90=vertical
    torso_med = float(np.nanmedian(torso))
    exercise = "pushup" if torso_med < 45 else "squat"
    j = (0, 1, 2) if exercise == "pushup" else (3, 4, 5)
    side_vis = {s: float(np.nanmean(V[:, [ids[k] for k in j]])) for s, ids in SIDES.items()}
    side = max(side_vis, key=side_vis.get); ids = SIDES[side]
    a, b, c = (P[:, ids[k]] for k in j)
    raw = angle(a, b, c)
    gated = raw.copy(); gated[V[:, [ids[k] for k in j]].min(1) < 0.3] = np.nan
    notes_pre = []
    if np.mean(~np.isnan(gated)) >= 0.2: raw = gated
    else: notes_pre.append("joint visibility <0.3 in most frames; using ungated landmarks (occluded/out-of-frame joints)")
    valid = float(np.mean(~np.isnan(gated)))
    if np.isnan(raw).all():
        return dict(file=os.path.basename(path), duration_s=meta["duration"], exercise="none", reps=0, partial_reps=0,
                    confidence="none", notes=notes_pre + ["no usable pose"], plot=None, processing_s=round(time.time()-t0, 1))
    sig = interp_nan(raw)
    win = max(5, int(round(fps * 0.3)) | 1)
    sm = savgol_filter(sig, win, 2) if n > win else sig
    lo, hi = DEFAULTS[exercise]; notes = notes_pre
    smin, smax = np.percentile(sm, 3), np.percentile(sm, 97); rng = smax - smin
    adapted = False
    if smin > lo - 5 or smax < hi + 3:
        lo2, hi2 = smin + 0.35 * rng, smax - 0.25 * rng
        notes.append(f"signal range {smin:.0f}-{smax:.0f} deg does not span default thresholds {lo}/{hi}; "
                     f"adapted to {lo2:.0f}/{hi2:.0f}")
        lo, hi, adapted = lo2, hi2, True
    lower_vis = float(np.mean(V[:, [23, 24, 25, 26, 27, 28]].max(1) > 0.5))
    reasons = []
    if detected < 0.6: reasons.append(f"low detection rate {detected:.2f}")
    if exercise == "squat" and lower_vis < 0.5: reasons.append(f"low lower-body visibility {lower_vis:.2f}")
    if rng < 40: reasons.append(f"angle range {rng:.0f} deg < 40")
    if reasons:
        return dict(file=os.path.basename(path), duration_s=round(meta["duration"], 2), fps=round(fps, 2),
                    frames=n, exercise="none", reps=0, partial_reps=0, confidence="high",
                    notes=["not_exercise: " + "; ".join(reasons)], pose_detected_frac=round(detected, 3),
                    lower_body_vis_frac=round(lower_vis, 3), angle_range=(round(smin, 1), round(smax, 1)),
                    plot=None, processing_s=round(time.time() - t0, 1))
    reps, partial, events = count_hysteresis(sm, lo, hi)
    pk, _ = find_peaks(-sm, prominence=max(20, 0.4 * rng), distance=max(1, int(fps * 0.6)))
    peaks = len(pk)
    if peaks != reps: notes.append(f"peak count ({peaks}) differs from hysteresis count ({reps})")
    conf = "high"
    if adapted or peaks != reps or valid < 0.8 or rng < 40: conf = "medium"
    if valid < 0.5 or rng < 25 or detected < 0.5: conf = "low"
    plot_path = None
    if plot:
        os.makedirs(PLOTS, exist_ok=True)
        t = np.arange(n) / fps
        plot_path = os.path.join(PLOTS, os.path.splitext(os.path.basename(path))[0] + "_angle.png")
        fig, ax = plt.subplots(2, 1, figsize=(12, 6), sharex=True)
        ax[0].plot(t, raw, alpha=.35, label="raw"); ax[0].plot(t, sm, label="smoothed")
        ax[0].axhline(lo, c="r", ls="--", label=f"down {lo:.0f}"); ax[0].axhline(hi, c="g", ls="--", label=f"up {hi:.0f}")
        ax[0].plot(t[pk], sm[pk], "kx", label="peaks (minima)")
        for e in events: ax[0].axvline(t[e], c="purple", alpha=.3)
        ax[0].set_ylabel(f"{side} {'elbow' if exercise=='pushup' else 'knee'} angle"); ax[0].legend(fontsize=7)
        ax[0].set_title(f"{os.path.basename(path)}: {exercise}, reps={reps}, partial={partial}, peaks={peaks}")
        ax[1].plot(t, torso); ax[1].set_ylabel("torso angle (0=horiz)"); ax[1].set_xlabel("s")
        fig.tight_layout(); fig.savefig(plot_path, dpi=90); plt.close(fig)
    return dict(file=os.path.basename(path), duration_s=round(meta["duration"], 2), fps=round(fps, 2),
                resolution=f"{meta['width']}x{meta['height']}", rotation=meta["rotation"], frames=n,
                exercise=exercise, torso_angle_median=round(torso_med, 1), side=side,
                reps=reps, partial_reps=partial, peak_count=peaks,
                thresholds=(round(lo, 1), round(hi, 1)), angle_range=(round(smin, 1), round(smax, 1)),
                pose_detected_frac=round(detected, 3), angle_valid_frac=round(valid, 3),
                confidence=conf, notes=notes, plot=plot_path, processing_s=round(time.time() - t0, 1))

if __name__ == "__main__":
    vids = sys.argv[1:] or sorted(os.path.join(HERE, "videos", f) for f in os.listdir(os.path.join(HERE, "videos")))
    res = [analyze_video(v) for v in vids]
    for r in res: print(json.dumps(r, default=float))
    json.dump(res, open(os.path.join(HERE, "results.json"), "w"), indent=2, default=float)
