# MediaPipe limits benchmark (2026-10-20)
Setup: `cv_test/rep_counter.py` via `bench.py`, lite model (full only as sanity column), `taskset` 2 cores, 4 videos in parallel on an 8-core box (timings ±30% noisy). fps = target sampling (step = round(src/target), so 30→20 gives effective 15 on 30fps sources). Resolution = `max_side` (LONG side). Scratch: `/workspace/cvbench` (logs `log_*.jsonl`, contact sheets `sheets/`). Exercise identified from keyframes.

| Video | Exercise (visual) | Dur | Native fps / res | Baseline lite (full) | Min stable fps | fps where changed | Min stable res (long side) | Res where changed | Fastest OK time / config | Notes |
|---|---|---|---|---|---|---|---|---|---|---|
| TCS52R .mp4 | pushups, front camera | 42.8 s | 25 / 592x1280 | none 0 (full: squat 29) | 10 | 8 → squat 16 | 720 | 640 → pushup 10 | 9.0–10 s, 10fps/720 | Baseline WRONG: front-facing pushups not recognised |
| VX8JN6 .mp4 | squats, side view | 93.3 s | 24.9 / 592x1280 | squat 46 (45) | 20 (eff. 25) | 15 (eff. 12.5) → 44 | 1280 (native) | 960 → 48 | 40.1 s, native | Unstable ±2 reps |
| WBYCSB .mp4 | squats, high/rear camera | 82.7 s | 30 / 1280x720 | squat 21 (full 8) | 20 (eff. 30) | 15 → 18 | 1280 (native) | 960 → 18 | 39.5 s, native | Baseline unreliable (lite≠full), bad camera angle |
| WZCRRZ .mov | pushups, front camera | 42.1 s | 30 / 1072x1920 | squat 13 (full 7) | 20 (eff. 30) | 15 → 19 | 1280 | 960 → 17 | 23.8 s, native | Baseline WRONG: classified as squat |
| 123TYQS .mov | pushups, front camera | 112.7 s | 30 / 1072x1920 | squat 57 (56) | 30 (native) | 20 (eff. 15) → 33 | 1920 (native) | 1280 → 52 | 89.9 s, native | Labelled squat; count may be plausible but untrusted |
| 1247W .mp4 | squats, side view | 75.4 s | 120 / 592x1280 | squat 50 (49) | 12 (eff. 12) | 10 → 49 | 400 | 320 → 51 | ~16.5 s, 12fps/640 | Only clean case; robust |
| Q80ZAQ .mp4 | junk (person sitting) | 1.9 s | 10 / 480x480 | squat 1 (1) | 3 | – | 96 | – | 0.4 s, 3fps/any | FALSE POSITIVE squat 1 → needs min-duration gate |
| VC2PWP .mp4 | junk (meme face) | 3.2 s | 10 / 640x640 | none 0 | 3 | – | 96 | – | 0.5 s, 3fps/≤720 | Correct none |
| RZE8 .mp4 | junk (game cutscene) | 10.0 s | 24 / 1280x720 | none 0 | 3 | – | 96 | – | ~1.7–2.6 s, 3fps/low | Correct none |

## Conclusions
- Only side-view squats (1247W, and old video 1) are stable down to ~10-12 fps / 256-400 px. Front-camera pushups are misclassified (squat/none) and change count at every step → classifier/angle logic for pushups must be fixed before any fps/res tuning.
- Junk: 3 fps / 128 px is enough to reject; add duration <5 s gate (Q80ZAQ false rep).
- Recommendation for e2-micro: lite, 12 fps, long side 480 (≈0.3–0.4× realtime on 2 cores); for gate pass 3 fps/160 px.

## Sequential re-run: 1247W, decode vs inference (2026-10-20 ~20:15 Europe/Kiev)
Strictly sequential, `taskset -c 0,1`, lite model, script `/workspace/cvbench/split.py`. Note: the file is VFR, container says 120 fps but real ≈29.6 fps (2235 frames). OpenCV reports ≈29.6, so step = round(29.6/target): 30 and 20 → every frame (29.6), 15 and 12 → 14.8, 10 → 9.9. The ffmpeg path uses `fps=<same effective fps>` + `scale=…:flags=area` → rawvideo rgb24 pipe (`-threads 2`).

| Config (target → eff. fps / long side) | cv2 decode s | cv2 infer s | cv2 total s | cv2 reps | ffmpeg decode s | ffmpeg infer s | ffmpeg total s | ffmpeg reps |
|---|---|---|---|---|---|---|---|---|
| native (29.6) / 1280 | 2.6 | 24.1 | 26.9 | 50 | 12.9* | 100.9* | 114.5* | 50 |
| 30 → 29.6 / 1280 | 2.6 | 23.8 | 26.5 | 50 | 4.1 | 27.5 | 31.9 | 51 |
| 20 → 29.6 / 1280 | 2.6 | 23.0 | 25.7 | 50 | 3.9 | 26.2 | 30.3 | 51 |
| 15 → 14.8 / 1280 | 3.1 | 11.8 | 14.9 | 50 | 2.0 | 12.7 | 14.8 | 50 |
| 12 → 14.8 / 1280 | 2.8 | 11.8 | 14.6 | 50 | 2.1 | 13.5 | 15.7 | 50 |
| 10 → 9.9 / 1280 | 2.5 | 7.8 | 10.3 | 49 | 1.4 | 9.0 | 10.5 | 50 |
| 12 → 14.8 / 640 | 2.6 | 10.8 | 13.5 | 50 | 0.4 | 12.6 | 13.2 | 50 |
| 12 → 14.8 / 480 | 4.0 | 11.2 | 15.2 | 50 | 0.3 | 12.0 | 12.5 | 50 |
| 12 → 14.8 / 400 | 5.1 | 11.3 | 16.4 | 51 | 0.2 | 12.8 | 13.3 | 51 |
\* ffmpeg without a filter emits CFR 120 fps (9046 duplicated frames), so the native ffmpeg row is an artifact.

Findings: the 60.7 s at 20 fps in the parallel run was CPU contention; sequentially it takes 25.7 s. Inference time is ~0.010–0.011 s/frame and barely depends on input resolution (the lite model rescales internally to 256), so the only real lever is the number of frames (fps). Decode is a small share (≤ 5 s). With ffmpeg the decode for scaled input drops to 0.2–0.4 s (in cv2 the resize costs more than it saves). Overall ffmpeg gains only ~1–2.5 s at 12 fps/≤640.

## Sequential (strict, taskset -c 0,1, lite, 3 runs per point, median and min–max)

### VX8JN6 (squats, side view, 93.3 s, VFR)
The container reports 24.9 fps, but OpenCV reads 2741 frames (≈29.4 fps real), while ffmpeg without a filter outputs CFR 24.9 (2325 frames, drops some). That is why the native baseline differs: cv2 46, ffmpeg 47. Each sweep stops at the first point where reps change.

| target fps / side | eff fps | side | method | decode s (med) | infer s (med) | total s med (min–max) | reps ×3 |
|---|---|---|---|---|---|---|---|
| native / native | 29.4 | native | cv2 | 3.1 | 27.6 | 30.9 (30.8–31.3) | 46/46/46 |
| 15 / native | 14.7 | native | cv2 | 3.3 | 14.1 | 17.4 (17.4–17.6) | 44/44/44 |
| native / 1280 | 29.4 | 1280 | cv2 | 2.9 | 27.3 | 30.4 (30.0–30.5) | 46/46/46 |
| native / 960 | 29.4 | 960 | cv2 | 9.2 | 26.4 | 35.7 (34.5–35.8) | 48/48/48 |
| native / native | 24.9 | native | ffmpeg | 3.6 | 25.1 | 28.9 (28.7–29.4) | 47/47/47 |
| native / 1280 | 24.9 | 1280 | ffmpeg | 3.7 | 25.2 | 29.2 (29.1–29.5) | 47/47/47 |
| 15 / native | 14.7 | native | ffmpeg | 2.5 | 16.1 | 18.7 (18.1–18.9) | 43/43/43 |
| native / 960 | 24.9 | 960 | ffmpeg | 2.1 | 27.8 | 30.5 (28.3–30.6) | 45/45/45 |

1247W:

| target fps / side | eff fps | side | method | decode s (med) | infer s (med) | total s med (min–max) | reps ×3 |
|---|---|---|---|---|---|---|---|
| 12 / 640 | 14.8 | 640 | cv2 | 2.5 | 10.6 | 13.1 (12.9–13.1) | 50/50/50 |
| 12 / 480 | 14.8 | 480 | cv2 | 4.2 | 11.4 | 15.6 (14.0–260.1) | 50/50/50 |
| 12 / 640 | 14.8 | 640 | ffmpeg | 0.4 | 12.9 | 13.5 (13.4–14.2) | 50/50/50 |
| 12 / 480 | 14.8 | 480 | ffmpeg | 0.3 | 12.2 | 12.7 (12.6–12.8) | 50/50/50 |

Boundary: fps is unstable already at 15 (eff. 14.7): cv2 46→44, ffmpeg 47→43. Resolution: 1280 OK, 960 changes (cv2 →48, ffmpeg →45). Fastest correct: ffmpeg native/native 28.9 s (count 47); for cv2 (=46) native/1280 30.4 s. This video does not tolerate downsampling: unstable within ±2–4 reps; ground truth is unknown.
1247W 1 of 3 cv2 12/480 runs took an outlier of 260.1 s (a stall on the box); the median is 15.6 s. Medians: ffmpeg 12/480 = 12.7 s is the fastest; spreads are ≤1 s, so a 0.5–1 s difference between the configs is at the noise level.

### WBYCSB (squats, camera top/back, 82.7 s, CFR 30 fps, 1280x720)
Full model native ×1: cv2 8 reps (35.2 s: decode 2.9 / infer 32.2), ffmpeg 8 reps (40.2 s: decode 4.7 / infer 35.3).

| target fps | eff fps | side | method | decode s | infer s | total s med (min–max) | reps ×3 |
|---|---|---|---|---|---|---|---|
| native | 30.0 | native | cv2 | 2.9 | 26.1 | 29.1 (28.4–29.7) | 21/21/21 |
| 15 | 15.0 | native | cv2 | 3.6 | 12.9 | 16.6 (16.3–16.8) | 18/18/18 |
| native | 30.0 | 1280 | cv2 | 2.9 | 24.9 | 27.9 (27.9–28.5) | 21/21/21 |
| native | 30.0 | 960 | cv2 | 10.6 | 26.0 | 36.7 (36.1–37.2) | 18/18/18 |
| native | 30.0 | native | ffmpeg | 5.0 | 29.3 | 34.6 (32.9–34.6) | 21/21/21 |
| 15 | 15.0 | native | ffmpeg | 2.5 | 14.7 | 17.3 (16.9–17.5) | 19/19/19 |
| native | 30.0 | 1280 | ffmpeg | 4.7 | 28.1 | 33.0 (32.3–34.0) | 21/21/21 |
| native | 30.0 | 960 | ffmpeg | 1.6 | 27.6 | 30.1 (29.6–31.6) | 19/19/19 |

Boundary: fps 15 → 18 (cv2) / 19 (ffmpeg); side 960 → 18/19. Formally the fastest correct (=lite baseline 21) is cv2 native/1280 at 27.9 s. BUT baseline is unreliable: lite 21 vs full 8, a factor of ~2.6; the true count is unknown. The top/back camera angle is unsuitable for counting, so treat it as "ask to re-record" rather than a tuning case.

### TCS52R (pushups, front camera, 42.8 s, 25 fps, 592x1280)
rep_counter has no exercise override → a copy `/workspace/cvbench/rc_force.py` with env `FORCE_EX=pushup` (the bot code is untouched). Each run computes both modes on the same landmarks. Sweep criterion = forced-pushup count. Times are median of 3 (spread ≤1.5 s); counts were identical across all 3 runs.

| config | eff fps | side | method | total s med | auto (ex / reps) | forced pushup reps (peaks) |
|---|---|---|---|---|---|---|
| full, native | 25.0 | native | cv2 | 14.9 (×1) | squat 29 | 18 (29) |
| full, native | 25.0 | native | ffmpeg | 16.2 (×1) | squat 29 | 18 (29) |
| native | 25.0 | native | cv2 | 12.3 | none 0 | 5 (10) |
| 15 | 12.5 | native | cv2 | 6.8 | none 0 | **7** (10) |
| native | 25.0 | 1280 | cv2 | 11.5 | none 0 | 5 (10) |
| native | 25.0 | 960 | cv2 | 14.3 | squat 22 | **10** (15) |
| native | 25.0 | native | ffmpeg | 13.3 | none 0 | 5 (10) |
| 15 | 12.5 | native | ffmpeg | 7.2 | none 0 | **6** (9) |
| native | 25.0 | 1280 | ffmpeg | 13.7 | none 0 | 5 (10) |
| native | 25.0 | 960 | ffmpeg | 13.4 | squat 28 | **6** (10) |

Boundary: forced-pushup count changes already at 15 fps and at 960. Lite gives 5, full gives 18, peaks range 9–29 → no stable baseline; the "fastest correct" config is undefined. From a front camera the elbow angle is foreshortened and lite barely tracks it. Pushups need a side camera, or a different signal (e.g. vertical motion of shoulders/nose) — that is logic work, not fps/res tuning. Ground truth needs a manual count.

### WZCRRZ, RAS32FN, QZJQY9W: auto + forced pushup (`rc_force.py`) + experimental vertical signal `vy`
`vy` = find_peaks on the mean y of nose+shoulders, normalised by shoulder width, savgol smoothed, prominence max(0.15, 0.4·range), distance 0.6 s (in `split.py`). Sweep criterion: WZCRRZ = forced-pu baseline; RAS and QZJ = vy within ground truth (RAS 30–34, QZJ ≈29, accepted 27–31), with the FULL fps/res sweep (no early stop). All counts were identical across the 3 runs. The QZJ tail ran via `heavy`. Times in s, median of 3.
Full model native ×1: WZCRRZ auto squat 7/4, pu 14, vy 13 | RAS auto squat 30/29, pu 14/19, vy 33 | QZJ auto none 0, pu 0, vy 21.

#### WZCRRZ
| target fps | eff fps | side | method | decode | infer | total med (min–max) | auto | forced pu (peaks) | vy |
|---|---|---|---|---|---|---|---|---|---|
| native | 29.9 | native | cv2 | 4.1 | 16.1 | 20.2 (18.5–20.5) | squat 13 | 11 (14) | 13 |
| 15 | 15.0 | native | cv2 | 6.2 | 8.3 | 14.6 (14.4–14.6) | squat 19 | 13 (15) | 13 |
| native | 29.9 | 1280 | cv2 | 13.6 | 15.8 | 29.5 (28.0–29.7) | squat 13 | 12 (14) | 13 |
| native | 30.0 | native | ffmpeg | 6.4 | 21.1 | 27.7 (26.9–28.1) | squat 12 | 10 (14) | 13 |
| 15 | 15.0 | native | ffmpeg | 3.3 | 10.0 | 13.3 (13.1–13.4) | squat 16 | 14 (14) | 13 |
| native | 30.0 | 1280 | ffmpeg | 2.8 | 19.3 | 22.6 (21.5–23.1) | squat 15 | 13 (14) | 13 |

#### RAS32FN
| target fps | eff fps | side | method | decode | infer | total med (min–max) | auto | forced pu (peaks) | vy |
|---|---|---|---|---|---|---|---|---|---|
| native | 25.0 | native | cv2 | 1.3 | 12.6 | 13.9 (13.8–14.2) | squat 36 | 6 (17) | 31 |
| 15 | 12.5 | native | cv2 | 1.4 | 6.1 | 7.5 (7.4–7.9) | squat 29 | 33 (34) | 31 |
| 12 | 12.5 | native | cv2 | 1.4 | 6.3 | 7.8 (7.7–7.9) | squat 29 | 33 (34) | 31 |
| 10 | 12.5 | native | cv2 | 1.4 | 6.2 | 7.6 (7.4–8.0) | squat 29 | 33 (34) | 31 |
| 8 | 8.3 | native | cv2 | 1.3 | 4.4 | 5.7 (5.5–5.7) | squat 18 | 7 (10) | 29 |
| 10 | 12.5 | 1280 | cv2 | 1.5 | 6.7 | 8.2 (7.5–8.5) | squat 29 | 33 (34) | 31 |
| 10 | 12.5 | 960 | cv2 | 2.5 | 6.2 | 8.6 (8.6–9.2) | squat 27 | 10 (14) | 31 |
| 10 | 12.5 | 720 | cv2 | 2.5 | 6.4 | 8.8 (8.6–9.2) | squat 23 | 31 (33) | 30 |
| 10 | 12.5 | 640 | cv2 | 1.5 | 6.6 | 8.1 (8.0–8.5) | squat 25 | 8 (11) | 30 |
| 10 | 12.5 | 480 | cv2 | 2.3 | 6.9 | 9.2 (8.8–9.2) | squat 28 | 31 (35) | 31 |
| 10 | 12.5 | 400 | cv2 | 2.6 | 6.2 | 8.8 (8.7–9.2) | squat 26 | 10 (15) | 31 |
| 10 | 12.5 | 320 | cv2 | 1.8 | 6.4 | 8.2 (8.0–8.3) | squat 27 | 8 (17) | 31 |
| 10 | 12.5 | 256 | cv2 | 2.3 | 6.4 | 8.7 (8.4–9.0) | squat 27 | 12 (13) | 30 |
| 10 | 12.5 | 192 | cv2 | 2.2 | 6.0 | 8.2 (8.0–8.8) | squat 26 | 14 (16) | 30 |
| 6 | 6.3 | native | cv2 | 1.2 | 3.3 | 4.5 (4.1–4.6) | squat 16 | 25 (25) | 29 |
| 5 | 5.0 | native | cv2 | 1.1 | 2.5 | 3.6 (3.4–3.8) | squat 8 | 18 (25) | 26 |
| 4 | 4.2 | native | cv2 | 1.1 | 2.2 | 3.3 (3.2–3.5) | squat 7 | 11 (12) | 22 |
| 3 | 3.1 | native | cv2 | 1.0 | 1.5 | 2.6 (2.5–2.7) | squat 4 | 1 (4) | 12 |
| 10 | 12.5 | 160 | cv2 | 1.6 | 5.9 | 7.5 (7.4–7.6) | squat 25 | 15 (17) | 31 |
| 10 | 12.5 | 128 | cv2 | 2.0 | 5.7 | 7.7 (7.6–8.1) | squat 25 | 13 (16) | 31 |
| native | 25.0 | native | ffmpeg | 1.8 | 12.5 | 14.4 (14.2–15.0) | squat 36 | 6 (17) | 31 |
| 15 | 12.5 | native | ffmpeg | 1.1 | 6.8 | 8.0 (7.6–8.3) | squat 30 | 32 (34) | 31 |
| 12 | 12.5 | native | ffmpeg | 1.1 | 6.4 | 7.7 (7.3–8.0) | squat 30 | 32 (34) | 31 |
| 10 | 12.5 | native | ffmpeg | 1.0 | 6.2 | 7.3 (7.3–7.3) | squat 30 | 32 (34) | 31 |
| 8 | 8.3 | native | ffmpeg | 0.7 | 4.2 | 4.9 (4.8–5.0) | squat 17 | 27 (31) | 29 |
| 6 | 6.3 | native | ffmpeg | 0.6 | 3.4 | 4.0 (3.7–4.1) | squat 12 | 7 (9) | 29 |
| 5 | 5.0 | native | ffmpeg | 0.4 | 2.3 | 2.8 (2.8–2.9) | squat 11 | 1 (5) | 30 |
| 4 | 4.2 | native | ffmpeg | 0.4 | 2.2 | 2.6 (2.5–2.8) | squat 6 | 3 (6) | 27 |
| 3 | 3.1 | native | ffmpeg | 0.3 | 1.6 | 1.9 (1.8–2.1) | squat 3 | 5 (4) | 11 |
| 5 | 5.0 | 1280 | ffmpeg | 0.5 | 2.4 | 2.9 (2.8–3.4) | squat 11 | 1 (5) | 30 |
| 5 | 5.0 | 960 | ffmpeg | 0.2 | 2.2 | 2.4 (2.4–2.5) | squat 14 | 3 (7) | 30 |
| 5 | 5.0 | 720 | ffmpeg | 0.2 | 2.1 | 2.3 (2.3–2.4) | squat 8 | 6 (8) | 30 |
| 5 | 5.0 | 640 | ffmpeg | 0.1 | 2.2 | 2.4 (2.4–2.5) | squat 11 | 4 (8) | 30 |
| 5 | 5.0 | 480 | ffmpeg | 0.1 | 2.2 | 2.4 (2.2–2.4) | squat 7 | 3 (7) | 30 |
| 5 | 5.0 | 400 | ffmpeg | 0.1 | 2.2 | 2.3 (2.2–2.3) | squat 8 | 17 (22) | 26 |
| 5 | 5.0 | 320 | ffmpeg | 0.1 | 2.1 | 2.2 (2.2–2.3) | squat 9 | 6 (8) | 28 |
| 5 | 5.0 | 256 | ffmpeg | 0.1 | 2.2 | 2.3 (2.3–2.5) | squat 5 | 5 (8) | 28 |
| 5 | 5.0 | 192 | ffmpeg | 0.1 | 2.2 | 2.3 (2.2–2.4) | squat 6 | 12 (13) | 24 |
| 5 | 5.0 | 160 | ffmpeg | 0.1 | 2.0 | 2.1 (2.1–2.4) | squat 14 | 11 (16) | 27 |
| 5 | 5.0 | 128 | ffmpeg | 0.1 | 2.2 | 2.4 (2.3–2.4) | squat 7 | 2 (4) | 32 |

#### QZJQY9W
| target fps | eff fps | side | method | decode | infer | total med (min–max) | auto | forced pu (peaks) | vy |
|---|---|---|---|---|---|---|---|---|---|
| native | 30.0 | native | cv2 | 2.8 | 25.8 | 28.7 (28.6–29.1) | none 0 | 0 (None) | 21 |
| 15 | 15.0 | native | cv2 | 3.3 | 12.9 | 16.2 (16.1–16.4) | squat 15 | 29 (43) | 22 |
| 12 | 15.0 | native | cv2 | 3.4 | 13.3 | 16.8 (16.6–17.0) | squat 15 | 29 (43) | 22 |
| 10 | 10.0 | native | cv2 | 3.2 | 9.1 | 12.3 (12.2–13.2) | none 0 | 18 (31) | 13 |
| 8 | 7.5 | native | cv2 | 2.8 | 6.9 | 9.8 (9.7–9.9) | none 0 | 0 (None) | 14 |
| 6 | 6.0 | native | cv2 | 2.6 | 5.5 | 8.1 (8.0–8.4) | none 0 | 0 (None) | 10 |
| 5 | 5.0 | native | cv2 | 2.7 | 5.0 | 7.7 (7.0–8.0) | none 0 | 0 (None) | 13 |
| 4 | 4.3 | native | cv2 | 2.6 | 4.3 | 6.9 (6.8–7.5) | none 0 | 0 (None) | 14 |
| 3 | 3.0 | native | cv2 | 2.5 | 2.9 | 5.4 (5.2–5.6) | none 0 | 8 (20) | 15 |
| native | 30.0 | 1280 | cv2 | 2.9 | 26.4 | 29.3 (28.3–30.3) | none 0 | 0 (None) | 21 |
| native | 30.0 | 960 | cv2 | 9.8 | 25.1 | 35.0 (34.8–36.8) | squat 19 | 25 (37) | 21 |
| native | 30.0 | 720 | cv2 | 10.5 | 24.9 | 35.3 (35.1–36.5) | none 0 | 26 (30) | 20 |
| native | 30.0 | 640 | cv2 | 2.4 | 24.7 | 27.2 (24.2–27.7) | squat 41 | 28 (41) | 17 |
| native | 30.0 | 480 | cv2 | 7.7 | 22.2 | 30.0 (29.1–30.4) | squat 11 | 31 (34) | 20 |
| native | 30.0 | 400 | cv2 | 8.4 | 23.0 | 31.5 (31.2–31.6) | squat 19 | 25 (33) | 20 |
| native | 30.0 | 320 | cv2 | 4.1 | 22.7 | 26.8 (26.1–28.1) | none 0 | 20 (37) | 18 |
| native | 30.0 | 256 | cv2 | 4.0 | 22.7 | 26.8 (26.5–28.2) | none 0 | 26 (35) | 17 |
| native | 30.0 | 192 | cv2 | 6.8 | 23.2 | 30.0 (28.6–30.2) | squat 10 | 28 (32) | 15 |
| native | 30.0 | 160 | cv2 | 3.6 | 23.6 | 27.3 (26.0–28.7) | none 0 | 0 (None) | 16 |
| native | 30.0 | 128 | cv2 | 3.4 | 22.6 | 26.0 (25.6–26.3) | none 0 | 0 (None) | 15 |
| native | 30.0 | native | ffmpeg | 4.9 | 29.0 | 34.2 (33.9–34.5) | none 0 | 0 (None) | 21 |
| 15 | 15.0 | native | ffmpeg | 2.6 | 14.7 | 17.4 (16.6–17.5) | squat 18 | 27 (39) | 19 |
| 12 | 15.0 | native | ffmpeg | 2.6 | 14.6 | 17.3 (17.2–18.0) | squat 18 | 27 (39) | 19 |
| 10 | 10.0 | native | ffmpeg | 1.7 | 10.6 | 12.5 (12.1–12.5) | none 0 | 20 (31) | 16 |
| 8 | 7.5 | native | ffmpeg | 1.5 | 8.5 | 10.0 (9.8–10.5) | none 0 | 17 (28) | 15 |
| 6 | 6.0 | native | ffmpeg | 1.1 | 6.3 | 7.4 (7.4–7.9) | none 0 | 13 (20) | 12 |
| 5 | 5.0 | native | ffmpeg | 0.9 | 5.2 | 6.2 (5.7–6.5) | none 0 | 0 (None) | 9 |
| 4 | 4.3 | native | ffmpeg | 0.7 | 4.0 | 4.7 (4.6–5.0) | none 0 | 0 (None) | 8 |
| 3 | 3.0 | native | ffmpeg | 1.0 | 3.0 | 4.0 (3.9–4.1) | none 0 | 9 (11) | 6 |
| native | 30.0 | 1280 | ffmpeg | 5.2 | 30.0 | 35.4 (34.7–35.7) | none 0 | 0 (None) | 21 |
| native | 30.0 | 960 | ffmpeg | 4.0 | 29.7 | 33.8 (33.0–33.9) | none 0 | 27 (38) | 23 |
| native | 30.0 | 720 | ffmpeg | 1.2 | 30.1 | 32.1 (29.9–32.5) | squat 12 | 27 (36) | 22 |
| native | 30.0 | 640 | ffmpeg | 0.9 | 27.0 | 28.4 (28.3–29.1) | squat 18 | 26 (35) | 18 |
| native | 30.0 | 480 | ffmpeg | 0.8 | 26.1 | 27.2 (27.2–29.5) | squat 15 | 23 (28) | 19 |
| native | 30.0 | 400 | ffmpeg | 0.5 | 26.0 | 27.0 (25.2–27.0) | none 0 | 0 (None) | 19 |
| native | 30.0 | 320 | ffmpeg | 0.4 | 25.1 | 25.9 (24.7–25.9) | squat 30 | 25 (36) | 21 |
| native | 30.0 | 256 | ffmpeg | 0.4 | 24.3 | 25.1 (24.1–25.5) | none 0 | 37 (41) | 21 |
| native | 30.0 | 192 | ffmpeg | 0.1 | 24.3 | 24.6 (24.1–24.7) | squat 34 | 27 (38) | 20 |
| native | 30.0 | 160 | ffmpeg | 0.1 | 24.7 | 24.9 (23.7–25.1) | none 0 | 35 (40) | 19 |
| native | 30.0 | 128 | ffmpeg | 0.1 | 24.4 | 24.6 (24.2–25.2) | none 0 | 0 (None) | 15 |

Findings:
- **RAS32FN (pushups, front, elbows NOT visible, edge case)**: elbow-angle (forced pu) is chaotic (1–33). `vy` is in range 30–34 at: cv2 native..12.5 fps (31), every res 1280→128 at 12.5 fps (30–31); ffmpeg 12.5 fps (31), 5 fps at 1280–640/480 (30). Lower boundary: 8.3 fps → 29 (just out of range), 5 fps (cv2) → 26, 3 fps → 11–12; ffmpeg 5 fps / 400 → 26. **Fastest in range: ffmpeg 5 fps / 480 → 2.4 s (30)**; a robust choice is 12.5 fps / any res ≈ 7.5–8 s (31), safe down to 128 px.
- **QZJQY9W (son, camera on the floor below, hoodie, lower body cut off, pauses, edge case)**: nothing is reliably in range. `vy` 21 at native and falls with fps (pauses + the face moving toward the camera rather than vertically). Forced pu sometimes hits 27–31 (cv2 15 fps = 29, ffmpeg 15 fps = 27, 960/720/640/480 ≈ 23–31), but with no monotonic behaviour; it is chance. Auto classification = none/squat. Verdict: this camera angle is NOT countable with the current logic → "ask to re-record"; the son needs a side camera.
- **WZCRRZ (pushups, front)**: no ground truth; `vy` = 13 at every point measured (native, 15 fps, 1280; both methods; full=13), pu 10–14. `vy` is the most stable signal.
- Inference ~10 ms/frame regardless of res → time ≈ linear in eff fps; ffmpeg pre-decode saves 1–2 s.
