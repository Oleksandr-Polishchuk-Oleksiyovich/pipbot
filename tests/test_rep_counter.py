"""
Офлайн та ground-truth тести для core/cv/rep_counter.py (T-31).

System dependencies for full MediaPipe execution:
  - Linux: libgles2, libegl1, ffmpeg
  - Python: mediapipe, opencv-python, numpy, scipy
Якщо бібліотеки або тестові відео відсутні, тести автоматично пропускаються (skip).
"""
import math
import os
import pytest

from core.cv.rep_counter import (
    count_hysteresis,
    is_cv_available,
    probe,
    analyze_video,
    _make_result,
)

# Шляхи до ground-truth відео (якщо змонтовані/доступні локально)
CVBENCH_VIDEOS = "/workspace/cvbench/videos"
VIDEO_7XRAS = os.path.join(CVBENCH_VIDEOS, "01M5D7XRAS32FN3TBV63Z5A7YV.mp4")
VIDEO_7XQZJ = os.path.join(CVBENCH_VIDEOS, "01M5D7XQZJQY9W2JSP164NHZ9M.mp4")
VIDEO_SQUAT_50 = os.path.join(CVBENCH_VIDEOS, "01M5D1247W9YRM3S37YWG0G0DD.mp4")
VIDEO_MEME = os.path.join(CVBENCH_VIDEOS, "01M5D121VC2PWP91XG7J7W45Y0.mp4")
VIDEO_CUTSCENE = os.path.join(CVBENCH_VIDEOS, "01M5D124RZE8ZY07XHX8BGMKZ0.mp4")


def test_count_hysteresis_basic_and_partials():
    # Сигнал: пік (up > 8), спуск (down < 2), підйом (up > 8)
    # Rep 1: 10 -> 1 -> 10
    # Rep 2: 10 -> 1 -> 10
    # Partial: 10 -> 4 (< 5, але >= 2) -> 10
    sig = [10, 8, 5, 1, 3, 9, 10, 5, 0, 4, 10, 7, 4, 8, 10]
    lo, hi = 2.0, 8.0
    reps, partial, events = count_hysteresis(sig, lo, hi)
    assert reps == 2
    assert partial == 1
    assert len(events) == 2


def test_count_hysteresis_handles_nans_and_noise():
    # Сигнал з пропущеними кадрами (NaN) та шумом
    sig = [10.0, float("nan"), 1.0, float("nan"), 9.5]
    reps, partial, events = count_hysteresis(sig, 2.0, 8.0)
    assert reps == 1
    assert partial == 0


def test_contract_8_3_schema_structure():
    res = _make_result(
        request_id="test-req-123",
        file_unique_id="fuid-abc",
        duration_s=42.5,
        exercise="pushup",
        reps=30,
        confidence=0.87,
        flags=["front_camera"],
        valid=True,
        elapsed_s=1.23,
    )
    assert res["contract_version"] == "1.0"
    assert res["request_id"] == "test-req-123"
    assert res["file_unique_id"] == "fuid-abc"
    assert res["duration_s"] == 42.5
    assert res["exercise"] == "pushup"
    assert res["reps"] == 30
    assert res["confidence"] == 0.87
    assert res["valid"] is True
    assert res["analyzer_version"] == "cv-1.0"
    assert res["flags"] == ["front_camera"]
    assert "elapsed_s" in res


def test_analyze_video_handles_missing_file():
    res = analyze_video("non_existent_file.mp4")
    assert res["exercise"] == "none"
    assert res["reps"] == 0
    assert res["valid"] is False
    assert "file_not_found" in res["flags"]


def test_analyze_video_handles_missing_deps(monkeypatch):
    # Тестуємо поведінку при відсутності залежностей
    import core.cv.rep_counter as rc
    monkeypatch.setattr(rc, "is_cv_available", lambda: False)
    res = rc.analyze_video(__file__)  # існуючий файл
    assert res["exercise"] == "none"
    assert res["reps"] == 0
    assert res["valid"] is False
    assert "missing_cv_deps" in res["flags"]


@pytest.mark.skipif(not is_cv_available(), reason="MediaPipe/CV dependencies not installed in this environment")
@pytest.mark.skipif(not os.path.isfile(VIDEO_7XRAS), reason="Ground truth video 7XRAS not present")
def test_ground_truth_pushup_7xras():
    """T-31: pushup front-camera відео 7XRAS → 30–34 reps."""
    res = analyze_video(VIDEO_7XRAS, target_fps=8, max_side=256)
    assert res["exercise"] == "pushup"
    assert 30 <= res["reps"] <= 34


@pytest.mark.skipif(not is_cv_available(), reason="MediaPipe/CV dependencies not installed in this environment")
@pytest.mark.skipif(not os.path.isfile(VIDEO_7XQZJ), reason="Ground truth video 7XQZJ not present")
def test_ground_truth_pushup_7xqzj():
    """T-31: 7XQZJ → ~29 reps (з паузами) у межах ±2 (27-31)."""
    res = analyze_video(VIDEO_7XQZJ, target_fps=8, max_side=256)
    assert res["exercise"] == "pushup"
    assert 27 <= res["reps"] <= 31


@pytest.mark.skipif(not is_cv_available(), reason="MediaPipe/CV dependencies not installed in this environment")
@pytest.mark.skipif(not os.path.isfile(VIDEO_SQUAT_50), reason="Ground truth squat video not present")
def test_ground_truth_squat_50():
    """T-31: squat side-view (154s/75s) → 50 reps."""
    res = analyze_video(VIDEO_SQUAT_50, target_fps=8, max_side=256)
    assert res["exercise"] == "squat"
    assert 48 <= res["reps"] <= 52


@pytest.mark.skipif(not is_cv_available(), reason="MediaPipe/CV dependencies not installed in this environment")
@pytest.mark.skipif(not os.path.isfile(VIDEO_MEME), reason="Meme video not present")
def test_ground_truth_meme_rejected():
    """T-31: meme/cutscene кліпи → none, 0."""
    res = analyze_video(VIDEO_MEME, target_fps=8, max_side=256)
    assert res["exercise"] == "none"
    assert res["reps"] == 0
    assert res["valid"] is False
