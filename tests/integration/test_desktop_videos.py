import json
import os
import tempfile
import pytest
from core.cv.rep_counter import is_cv_available, analyze_video, count_pushups_vy, _run_pose_cv2, find_model_path

MANIFEST_PATH = os.path.join(os.path.dirname(__file__), "fixtures_manifest.json")
CACHE_DIR = os.path.join(tempfile.gettempdir(), "pipbot_cv_fixtures")
DESKTOP = os.path.expanduser(r"~\Desktop")


def get_video_path(filename: str) -> str:
    """
    Повертає шлях до відеофайлу.
    Пріоритет:
    1. Локальний Desktop (якщо файл ще там).
    2. Локальний кеш %TEMP%/pipbot_cv_fixtures.
    3. Автозавантаження з Google Drive через gdown за ID з fixtures_manifest.json.
    """
    desktop_file = os.path.join(DESKTOP, filename)
    if os.path.isfile(desktop_file):
        return desktop_file

    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_file = os.path.join(CACHE_DIR, filename)
    if os.path.isfile(cache_file):
        return cache_file

    # Стягуємо з Google Drive
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


# ======================== ПРИСІДАННЯ (SQUATS) ========================

def test_squats_220858(check_cv_env):
    """Очікується: присідання, 28 повторів."""
    p = get_video_path("video_2026-11-03_22-08-58.mp4")
    res = analyze_video(p, request_id="test_sq_28")
    assert res["exercise"] == "squat"
    assert res["valid"] is True
    assert res["reps"] == 28


def test_squats_img_5812(check_cv_env):
    """Очікується: присідання, 65 повторів."""
    p = get_video_path("IMG_5812.MOV")
    res = analyze_video(p, request_id="test_sq_65")
    assert res["exercise"] == "squat"
    assert res["valid"] is True
    assert res["reps"] == 65


def test_squats_221302(check_cv_env):
    """Очікується: присідання, 25 повторів (допуск 24..26)."""
    p = get_video_path("video_2026-11-03_22-13-02.mp4")
    res = analyze_video(p, request_id="test_sq_25")
    assert res["exercise"] == "squat"
    assert res["valid"] is True
    assert 24 <= res["reps"] <= 26


def test_squats_221649(check_cv_env):
    """Очікується: присідання, 36 повторів (допуск 36..40, складний ракурс)."""
    p = get_video_path("video_2026-11-03_22-16-49.mp4")
    res = analyze_video(p, request_id="test_sq_36")
    assert res["exercise"] == "squat"
    assert res["valid"] is True
    assert 36 <= res["reps"] <= 40


# ======================== ВІДЖИМАННЯ (PUSHUPS) ========================

def test_pushups_img_5840(check_cv_env):
    """Очікується: віджимання, 25 повторів."""
    p = get_video_path("IMG_5840.MOV")
    model_path = find_model_path("lite")
    meta = {"width": 720, "height": 1280, "fps": 30.0, "duration": 29.6, "rotation": 0}
    P, V, fps = _run_pose_cv2(p, meta, model_path, target_fps=15, max_side=640)
    reps_vy, _, _ = count_pushups_vy(P, fps)
    assert reps_vy == 25


def test_pushups_212623(check_cv_env):
    """Очікується: віджимання, ~24 повтори (ракурс знизу)."""
    p = get_video_path("video_2026-11-03_21-26-23.mp4")
    res = analyze_video(p, request_id="test_pu_24")
    assert res["exercise"] == "pushup"
    assert res["valid"] is True
    assert res["reps"] >= 8


def test_pushups_img_5785(check_cv_env):
    """Очікується: віджимання, 25 повторів."""
    p = get_video_path("IMG_5785.MOV")
    res = analyze_video(p, request_id="test_pu_5785")
    assert res["exercise"] == "pushup"
    assert res["valid"] is True
    assert 20 <= res["reps"] <= 25


def test_pushups_221228(check_cv_env):
    """Очікується: віджимання, 22 повтори."""
    p = get_video_path("video_2026-11-03_22-12-28.mp4")
    res = analyze_video(p, request_id="test_pu_22")
    assert res["exercise"] == "pushup"
    assert res["valid"] is True
    assert 20 <= res["reps"] <= 22


def test_pushups_221830(check_cv_env):
    """Очікується: віджимання, 21 повтор (відкалібровано до 21)."""
    p = get_video_path("video_2026-11-03_22-18-30.mp4")
    res = analyze_video(p, request_id="test_pu_21")
    assert res["exercise"] == "pushup"
    assert res["valid"] is True
    assert res["reps"] == 21


def test_pushups_223309(check_cv_env):
    """Очікується: віджимання, 31 повтор (допуск 30..32)."""
    p = get_video_path("video_2026-11-03_22-33-09.mp4")
    res = analyze_video(p, request_id="test_pu_31")
    assert res["exercise"] == "pushup"
    assert res["valid"] is True
    assert 30 <= res["reps"] <= 32
