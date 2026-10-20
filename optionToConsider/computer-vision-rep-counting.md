# Option: Local Computer Vision for Rep Counting

## Problem
Currently, workout videos are sent to Gemini (free tier) for verification. This is
rate-limited, costly if moved to paid tier, and LLMs count fast repetitive motion
unreliably because frames between samples are lost. Cheating (e.g. sending a GIF
instead of a real video) is also hard to catch.

## Proposal
Count reps locally with pose estimation and pass only a short text result to Gemini.
Gemini never sees the video; it only writes the witty verdict and calls
`mark_participant_done` via AFC, as today.

## Pipeline
1. **Download** the video / video note from Telegram.
2. **Validate** with `ffprobe`: duration, fps, codec, audio track presence, reject GIF/animation.
3. **Preprocess** with `ffmpeg`: downscale to ~256px, resample to 8-10 fps.
4. **Pose estimation** on each frame -> body keypoints (shoulders, elbows, wrists, hips, knees, ankles).
5. **Rep counting** via joint-angle state machine:
   - Push-ups: elbow angle < ~90° = "down", > ~160° = "up"; one down->up cycle = 1 rep.
   - Squats: knee angle (or hip height relative to knee) with the same down/up logic.
   - Hysteresis + smoothing (moving average over keypoints) to avoid jitter double-counts.
6. **Exercise classification**: torso (shoulder-hip line) near horizontal -> push-up;
   torso vertical with knee flexion -> squat.
7. **Form flags**: partial range of motion, sagging hips (shoulder-hip-ankle line not straight),
   low keypoint confidence / person out of frame.
8. **Output** to the LLM context, e.g.:
   `Video from Sasha: push-ups, 34 reps (3 partial), duration 41s, valid video`.

## Model options
| Model | Pros | Cons |
|---|---|---|
| **MoveNet Lightning (TFLite)** | Tiny, fast, low RAM; fits GCP `e2-micro` (1 GB RAM) | 17 keypoints, some jitter |
| **MoveNet Thunder (TFLite)** | More accurate | Slower on CPU |
| **MediaPipe Pose** | 33 keypoints (incl. heels/toes), better on side views on the floor | Heavier, slower on e2-micro |
| **YOLO pose (Ultralytics)** | Most robust on hard angles | Too heavy without GPU |

**Recommendation:** start with MoveNet Lightning via `tflite-runtime` (no full TensorFlow)
on the existing `e2-micro` VM. Switch to Thunder or MediaPipe if accuracy on bad camera
angles is insufficient.

## Suggested module
`rep_counter.py` exposing:
```python
def analyze_video(path: str) -> dict:
    # returns {"exercise": "pushup" | "squat" | "unknown",
    #          "reps": int, "partial_reps": int,
    #          "duration_s": float, "is_valid_video": bool,
    #          "flags": [str]}
```

## Benefits
- No video tokens sent to Gemini -> free tier is enough.
- Deterministic, explainable counts.
- Built-in anti-cheat (GIF / too short / no person detected).
