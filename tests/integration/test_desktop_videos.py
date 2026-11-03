import json
import os
import tempfile
import pytest
from core.cv.rep_counter import is_cv_available, analyze_video

MANIFEST_PATH = os.path.join(os.path.dirname(__file__), "fixtures_manifest.json")
CACHE_DIR = os.path.join(tempfile.gettempdir(), "pipbot_cv_fixtures")
DESKTOP = os.path.expanduser(r"~\Desktop")


def get_video_path(filename: str) -> str:
    desktop_file = os.path.join(DESKTOP, filename)
    if os.path.isfile(desktop_file):
        return desktop_file

    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_file = os.path.join(CACHE_DIR, filename)
    if os.path.isfile(cache_file):
        return cache_file

    if os.path.isfile(MANIFEST_PATH):
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        video_meta = manifest.get("videos", {}).get(filename)
        if video_meta and "id" in video_meta:
            try:
                import gdown
                gdown.download(id=video_meta["id"], output=cache_file, quiet=False)
                if os.path.isfile(cache_file):
                    return cache_file
            except Exception as e:
                pytest.skip(f"Could not download {filename} from Google Drive: {e}")

    pytest.skip(f"Video file {filename} not found locally or remotely")


@pytest.fixture(scope="session")
def check_cv_env():
    assert is_cv_available(), "MediaPipe / OpenCV must be installed"


# ======================== ТЕСТИ КЛАСИФІКАЦІЇ ТИПУ ВПРАВИ (EXERCISE TYPE DETECTION) ========================

@pytest.mark.parametrize("video_filename,expected_exercise,expected_reps_min", [
    ("IMG_5840.MOV", "pushup", 24),
    ("video_2026-11-03_21-26-23.mp4", "pushup", 8),
    ("IMG_5785.MOV", "pushup", 20),
    ("video_2026-11-03_22-12-28.mp4", "pushup", 20),
    ("video_2026-11-03_22-18-30.mp4", "pushup", 20),
    ("video_2026-11-03_22-33-09.mp4", "pushup", 30),
    ("video_2026-11-03_22-08-58.mp4", "squat", 28),
    ("IMG_5812.MOV", "squat", 64),
    ("video_2026-11-03_22-13-02.mp4", "squat", 24),
    ("video_2026-11-03_22-16-49.mp4", "squat", 35),
])
def test_exercise_type_detection(check_cv_env, video_filename, expected_exercise, expected_reps_min):
    """
    Головний тест на правильну детекцію типу вправи:
    - Віджимання (pushup) мають детектуватись як pushup навіть при фронтальному ракурсі з підлоги.
    - Присідання (squat) мають детектуватись як squat.
    """
    p = get_video_path(video_filename)
    res = analyze_video(p, request_id=f"test_cls_{video_filename}")
    assert res["exercise"] == expected_exercise, (
        f"Exercise mismatch for {video_filename}: expected {expected_exercise}, got {res['exercise']} "
        f"(flags: {res.get('flags')})"
    )
    assert res["valid"] is True
    assert res["reps"] >= expected_reps_min
