"""
One-shot CV job (runs under `heavy` on the box): python -m cv_worker.cv_job <video> <out.json>
Пише результат core.cv.rep_counter.analyze_video у JSON. Окремий процес -> пам'ять звільняється після job-а.
"""
import json
import os
import sys
import time


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    video, out = argv[0], argv[1]
    from core.cv import rep_counter

    t0 = time.time()
    res = rep_counter.analyze_video(
        video,
        target_fps=int(os.getenv("CV_TARGET_FPS", "15")),
        max_side=int(os.getenv("CV_MAX_SIDE", "640")),
        model=os.getenv("CV_MODEL", "lite"),
    )
    res["cv_elapsed_s"] = round(time.time() - t0, 2)
    # Відбиток для дедуплікації повторно завантажених відео (хеші кадрів + таймінги повторів); збій не фатальний.
    try:
        from core.cv.fingerprint import video_fingerprint
        res["fingerprint"] = video_fingerprint(video, res.get("duration_s"), res.get("rep_times"))
    except Exception as e:  # noqa: BLE001
        res["fingerprint"] = {"v": 1, "error": str(e)[:80]}
    with open(out, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
