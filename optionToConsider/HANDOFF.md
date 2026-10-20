# Handoff: CV rep counting work (from Братюня попиздіть, 2026-10-20)

## Done so far
- Option docs: `computer-vision-rep-counting.md`, `grokbot-webhook-offload.md`.
- Prototype: `cv_test/rep_counter.py` with `analyze_video(path) -> dict` (MediaPipe Pose Tasks API,
  `pose_landmarker_full.task`, venv in `cv_test/.venv`, needs `libgles2 libegl1`).
- Test videos in `cv_test/videos/`, results in `cv_test/results.json`, angle plots in `cv_test/plots/`.

## Test results
| # | Video | AI vision | MediaPipe | Truth |
|---|---|---|---|---|
| 1 | 01M5CZNMND14ESWWE462ZV1FJB.mov (154 s) | squats, 50 | squats, 50 (high conf.) | 50 squats |
| 2 | 01M5CZR0CBCTX2C2HSQVV33CMB.mp4 (3 s) | meme face clip | "squat", 0 (low conf.) | not exercise |
| 3 | 01M5CZR0CJ9PP1SJ747JEDED7Q.mp4 (10 s) | game cutscene | "squat", 3 (false reps) | not exercise |

## Conclusions
- Pose estimation counts accurately but cannot tell exercise vs junk -> hybrid design agreed with user:
  1. Cheap gate: `ffprobe` (duration/fps/GIF) + MediaPipe full-body detection.
  2. AI (Gemini) on 3-4 frames taken from the stable middle segment (skip start/end where the
     phone is placed/picked up) -> classify pushup / squat / junk.
  3. Pose estimation counts reps only within the stable segment.
  4. If AI and pose classification disagree -> ask to re-record.
- Speed: first run took 78.8 s for 154 s video (full model, every frame, 720p). Optimization in progress
  (lite model, ~8-10 fps sampling, downscaling, 2-thread benchmark to approximate e2-micro, plus a
  `not_exercise` outcome). Results will be appended here.

## Open
- Nothing committed or pushed. Outbound SSH from this machine is blocked; GitHub push needs `gh` HTTPS login.

## Optimization results (appended)
Default now: lite model, 8 fps sampling, long side 256 px, cache off. Benchmark on video 1 with `taskset -c 0,1`:
| Model | fps | Long side | Time (s) | Reps |
|---|---|---|---|---|
| full | 30 | orig | 77.7 | 50 |
| full | 10 | 480 | 33.3 | 50 |
| lite | 10 | 256 | 26.1 | 50 |
| **lite** | **8** | **256** | **23.5 (default)** | 50 |
| lite | 5 | 256 | 18.3 | 50 |
- Decoding all 4622 frames alone ~7.5 s on 2 cores (floor). Peak RAM ~380 MB.
- New `exercise='none'` outcome: body detected <60% frames, or (squat) lower body visible <50%, or angle range <40°. Videos 2 and 3 now -> none, 0 reps.
- Caveat: e2-micro shared vCPUs likely slower than taskset approximation. Lower-body check applies to squats only.
- Benchmark runner: `cv_test/bench.py`.
