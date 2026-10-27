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
import numpy as np

from core.cv.rep_counter import (
    count_hysteresis,
    count_pushups_vy,
    is_unreliable_camera_angle,
    _analyze_landmarks,
    is_cv_available,
    probe,
    analyze_video,
    _make_result,
    DEFAULT_CFG,
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
    """T-31/Sequential benchmark: pushup front-camera video 7XRAS → 30–34 reps via vy signal."""
    res = analyze_video(VIDEO_7XRAS, target_fps=12, max_side=480)
    assert res["exercise"] == "pushup"
    assert 30 <= res["reps"] <= 34
    assert res["valid"] is True


@pytest.mark.skipif(not is_cv_available(), reason="MediaPipe/CV dependencies not installed in this environment")
@pytest.mark.skipif(not os.path.isfile(VIDEO_7XQZJ), reason="Ground truth video 7XQZJ not present")
def test_ground_truth_pushup_7xqzj():
    """T-31/Sequential benchmark: 7XQZJ (floor camera from below, hoodie) is flagged low_camera_angle but still counted."""
    res = analyze_video(VIDEO_7XQZJ, target_fps=12, max_side=480)
    assert "low_camera_angle" in res["flags"]
    assert res["reps"] > 0
    assert res["valid"] is True


@pytest.mark.skipif(not is_cv_available(), reason="MediaPipe/CV dependencies not installed in this environment")
@pytest.mark.skipif(not os.path.isfile(VIDEO_SQUAT_50), reason="Ground truth squat video not present")
def test_ground_truth_squat_50():
    """T-31: squat side-view (154s/75s) → 50 reps."""
    res = analyze_video(VIDEO_SQUAT_50, target_fps=12, max_side=480)
    assert res["exercise"] == "squat"
    assert 48 <= res["reps"] <= 52


@pytest.mark.skipif(not is_cv_available(), reason="MediaPipe/CV dependencies not installed in this environment")
@pytest.mark.skipif(not os.path.isfile(VIDEO_MEME), reason="Meme video not present")
def test_ground_truth_meme_rejected():
    """T-31: meme/cutscene кліпи → none, 0."""
    res = analyze_video(VIDEO_MEME, target_fps=12, max_side=480)
    assert res["exercise"] == "none"
    assert res["reps"] == 0
    assert res["valid"] is False


# =========================================================================
# Офлайн unit-тести із синтетичними рядами landmark-точок (без mediapipe)
# =========================================================================

def make_synthetic_landmarks(
    num_reps: int = 10,
    fps: float = 12.0,
    duration_s: float = 20.0,
    amplitude: float = 60.0,
    sw: float = 100.0,
    base_y: float = 300.0,
    low_camera: bool = False,
    nan_ratio: float = 0.0,
    noise_std: float = 0.0,
    torso_angle: float = 20.0,
):
    """
    Генерує синтетичні масиви P (координати [N, 33, 2]) та V (видимість [N, 33]).
    Nose (0) та shoulders (11, 12) здійснюють коливання по осі y.
    У системі координат зображення більший y = ближче до підлоги (нижня точка віджимання).
    """
    n_frames = int(round(fps * duration_s))
    t = np.linspace(0, duration_s, n_frames, endpoint=False)

    # Вертикальні коливання (y збільшується на опусканні до підлоги)
    if num_reps > 0:
        freq = num_reps / duration_s
        y_motion = base_y + amplitude * np.sin(2 * np.pi * freq * t - np.pi / 2)
    else:
        y_motion = np.full(n_frames, base_y)

    if noise_std > 0:
        y_motion += np.random.normal(0, noise_std, size=n_frames)

    P = np.zeros((n_frames, 33, 2), dtype=float)
    V = np.ones((n_frames, 33), dtype=float)

    # Shoulders: 11 (left, x=200), 12 (right, x=300) -> shoulder width sw = 100
    P[:, 11, 0] = 200.0
    P[:, 12, 0] = 200.0 + sw
    P[:, 11, 1] = y_motion
    P[:, 12, 1] = y_motion

    # Nose: 0
    P[:, 0, 0] = 200.0 + sw / 2.0
    P[:, 0, 1] = y_motion - 20.0  # трохи вище плечей

    # Hips (23, 24): відстань визначається кутом torso_angle
    # d = hp - sh; tan(torso) = dy / dx
    rad = np.radians(torso_angle)
    torso_len = 200.0
    dx = torso_len * np.cos(rad)
    dy = torso_len * np.sin(rad)
    P[:, 23, 0] = P[:, 11, 0] + dx
    P[:, 24, 0] = P[:, 12, 0] + dx
    P[:, 23, 1] = P[:, 11, 1] + dy
    P[:, 24, 1] = P[:, 12, 1] + dy

    # Knees (25, 26) and Ankles (27, 28)
    P[:, 25, 0] = P[:, 23, 0] + 150.0
    P[:, 26, 0] = P[:, 24, 0] + 150.0
    P[:, 25, 1] = P[:, 23, 1]
    P[:, 26, 1] = P[:, 24, 1]

    if low_camera:
        # Камера знизу з підлоги: плечі в нижній частині кадру, нижня частина тіла обрізана
        P[:, :, 1] += 150.0  # зсув у нижню половину кадру
        V[:, [23, 24, 25, 26, 27, 28]] = 0.1  # ноги та таз не видно
    else:
        V[:, [23, 24, 25, 26, 27, 28]] = 0.9

    if nan_ratio > 0:
        mask = np.random.rand(n_frames) < nan_ratio
        P[mask, :, :] = np.nan

    return P, V


def test_count_pushups_vy_synthetic_perfect_series():
    """Тест підрахунку повторень vy на синтетичному ідеальному сигналі (10 віджимань)."""
    P, _ = make_synthetic_landmarks(num_reps=10, fps=12.0, duration_s=20.0, amplitude=60.0, sw=100.0)
    reps, pk, rng = count_pushups_vy(P, fps=12.0)
    assert reps == 10
    assert len(pk) == 10
    assert rng > 0.5


def test_count_pushups_vy_synthetic_various_cadences():
    """Тест різної кількості віджимань (5, 12, 18 повторень)."""
    for expected in [5, 12, 18]:
        P, _ = make_synthetic_landmarks(num_reps=expected, fps=12.0, duration_s=30.0, amplitude=50.0, sw=100.0)
        reps, pk, _ = count_pushups_vy(P, fps=12.0)
        assert reps == expected
        assert len(pk) == expected


def test_count_pushups_vy_synthetic_robust_to_nans_and_noise():
    """Тест стійкості vy до шумів та випадіння 10% кадрів (NaN)."""
    np.random.seed(42)
    P, _ = make_synthetic_landmarks(
        num_reps=8,
        fps=12.0,
        duration_s=20.0,
        amplitude=60.0,
        sw=100.0,
        nan_ratio=0.10,
        noise_std=3.0,
    )
    reps, pk, rng = count_pushups_vy(P, fps=12.0)
    assert reps == 8
    assert len(pk) == 8


def test_count_pushups_vy_synthetic_low_amplitude_rejected():
    """Тест: якщо амплітуда надто мала (людина майже не рухається), vy дає 0 повторень."""
    P, _ = make_synthetic_landmarks(num_reps=10, fps=12.0, duration_s=20.0, amplitude=4.0, sw=100.0)
    reps, pk, rng = count_pushups_vy(P, fps=12.0)
    assert reps == 0
    assert len(pk) == 0
    assert rng < 0.15


def test_is_unreliable_camera_angle_synthetic():
    """Тест детекції ненадійного ракурсу (камера знизу/з підлоги)."""
    meta = {"width": 640, "height": 480}

    # 1. Нормальний ракурс: плечі в середині, ноги видно
    P_norm, V_norm = make_synthetic_landmarks(num_reps=5, fps=12.0, low_camera=False, base_y=200.0)
    assert is_unreliable_camera_angle(P_norm, V_norm, meta) is False

    # 2. Камера на підлозі знизу: плечі низько (>0.52 кадру), ноги обрізані
    P_low, V_low = make_synthetic_landmarks(num_reps=5, fps=12.0, low_camera=True, base_y=300.0)
    assert is_unreliable_camera_angle(P_low, V_low, meta) is True


def test_analyze_landmarks_synthetic_pushup_valid():
    """Тест _analyze_landmarks на синтетичному валідному віджиманні."""
    P, V = make_synthetic_landmarks(num_reps=7, fps=12.0, duration_s=15.0, low_camera=False, base_y=180.0)
    meta = {"duration": 15.0, "width": 640, "height": 480, "fps": 12.0}

    res = _analyze_landmarks(P, V, fps=12.0, meta=meta, request_id="syn-1", file_unique_id="fuid-1", t0=0.0)
    assert res["exercise"] == "pushup"
    assert res["reps"] == 7
    assert res["valid"] is True
    assert "low_camera_angle" not in res["flags"]
    assert "low_fps_undercount_risk" not in res["flags"]


def test_analyze_landmarks_synthetic_low_camera_angle_still_counted():
    """Тест _analyze_landmarks: ракурс з підлоги знизу НЕ блокується — повтори пораховано, прапорець low_camera_angle, знижена впевненість."""
    P, V = make_synthetic_landmarks(num_reps=8, fps=12.0, duration_s=16.0, low_camera=True, base_y=300.0)
    meta = {"duration": 16.0, "width": 640, "height": 480, "fps": 12.0}

    res = _analyze_landmarks(P, V, fps=12.0, meta=meta, request_id="syn-low", file_unique_id="fuid-low", t0=0.0)
    assert "low_camera_angle" in res["flags"]
    assert res["exercise"] == "pushup"
    assert res["reps"] == 8
    assert res["valid"] is True
    assert res["confidence"] <= 0.55


def test_sampling_fps_below_10_warns_and_flags(caplog):
    """Тест: sampling fps < 10 викликає попередження про undercount risk та прапорець low_fps_undercount_risk."""
    P, V = make_synthetic_landmarks(num_reps=6, fps=8.0, duration_s=15.0, low_camera=False, base_y=180.0)
    meta = {"duration": 15.0, "width": 640, "height": 480, "fps": 8.0}

    with caplog.at_level("WARNING"):
        res = _analyze_landmarks(P, V, fps=8.0, meta=meta, request_id="syn-fps8", file_unique_id="fuid-fps8", t0=0.0)

    assert "low_fps_undercount_risk" in res["flags"]
    assert any("below 10 fps" in r.message for r in caplog.records)


# --- Класифікація вправи за вікнами (Діма 26.10: стояв, потім віджимання; все відео вважали присіданнями) ---

def _standing_segment(fps=12.0, duration_s=10.0):
    """Людина просто стоїть: торс майже вертикальний, рух мінімальний."""
    n = int(fps * duration_s)
    P = np.zeros((n, 33, 2), dtype=float)
    V = np.full((n, 33), 0.9)
    P[:, 11] = [200.0, 150.0]
    P[:, 12] = [300.0, 150.0]
    P[:, 0] = [250.0, 110.0]
    P[:, 23] = [210.0, 300.0]
    P[:, 24] = [290.0, 300.0]
    P[:, 25] = [210.0, 400.0]
    P[:, 26] = [290.0, 400.0]
    P[:, 27] = [210.0, 500.0]
    P[:, 28] = [290.0, 500.0]
    return P, V


def _mixed_video(stand_s, push_reps, push_s, fps=12.0):
    S, SV = _standing_segment(fps, stand_s)
    Pp, Vp = make_synthetic_landmarks(num_reps=push_reps, fps=fps, duration_s=push_s, base_y=180.0)
    return np.concatenate([S, Pp]), np.concatenate([SV, Vp])


def test_window_votes_pushup_after_long_standing_intro():
    P, V = _mixed_video(stand_s=14.0, push_reps=10, push_s=12.0)
    meta = {"duration": 26.0, "width": 640, "height": 480, "fps": 12.0}
    # передумова: медіана торса по всьому відео «стоїть» (>=45°) — стара глобальна логіка тут помилялась
    sh = P[:, [11, 12]].mean(1)
    hp = P[:, [23, 24]].mean(1)
    d = hp - sh
    assert np.nanmedian(np.degrees(np.arctan2(np.abs(d[:, 1]), np.abs(d[:, 0])))) >= 45
    res = _analyze_landmarks(P, V, fps=12.0, meta=meta, request_id="mix", file_unique_id="f", t0=0.0)
    assert res["exercise"] == "pushup"
    assert res["reps"] == 10
    assert res["valid"] is True


def test_window_votes_squat_video_still_squat():
    from core.cv.rep_counter import _window_votes, _classify_window
    # окремо перевіряємо класифікацію вікон: вертикальний торс + великий діапазон коліна = присідання
    assert _classify_window(80.0, 0.05, 90.0, 0.9, 0.9)[0] == "squat"
    assert _classify_window(20.0, 0.6, 5.0, 0.9, 0.9)[0] == "pushup"
    assert _classify_window(20.0, 0.02, 5.0, 0.9, 0.9)[0] == "idle"   # лежить у планці без руху
    assert _classify_window(85.0, 0.02, 5.0, 0.9, 0.9)[0] == "idle"   # стоїть


def test_winner_span_trims_loser_windows_and_keeps_idle_neighbours():
    from core.cv.rep_counter import _winner_span
    votes = {"winner": "pushup", "labels": ["squat", "idle", "pushup", "pushup", "idle", "squat"],
             "bounds": [(0, 10), (10, 20), (20, 30), (30, 40), (40, 50), (50, 60)]}
    assert _winner_span(votes) == (10, 50)
