"""
Офлайн-тести для cv_worker/vision.py (T-33).
Перевіряє reconcile_classes без звернень до мережі або реального Gemini API.
"""
from cv_worker.vision import VisionClassification, reconcile_classes


def test_reconcile_matching_pushup():
    pose_result = {
        "exercise": "pushup",
        "reps": 30,
        "confidence": 0.88,
        "flags": ["front_camera"],
        "valid": True,
    }
    vision_result = VisionClassification(exercise="pushup", confidence=0.95, reasoning="Man doing pushups")
    reconciled = reconcile_classes(pose_result, vision_result)

    assert reconciled["exercise"] == "pushup"
    assert reconciled["pose_class"] == "pushup"
    assert reconciled["vision_class"] == "pushup"
    assert reconciled["valid"] is True
    assert "class_mismatch" not in reconciled["flags"]


def test_reconcile_matching_squat():
    pose_result = {
        "exercise": "squat",
        "reps": 50,
        "confidence": 0.90,
        "flags": [],
        "valid": True,
    }
    vision_result = VisionClassification(exercise="squat", confidence=0.92, reasoning="Person squatting")
    reconciled = reconcile_classes(pose_result, vision_result)

    assert reconciled["exercise"] == "squat"
    assert reconciled["pose_class"] == "squat"
    assert reconciled["vision_class"] == "squat"
    assert reconciled["valid"] is True
    assert "class_mismatch" not in reconciled["flags"]


def test_reconcile_mismatch_flags_and_invalidates():
    # Наприклад: pose_class='squat' (помилка ракурсу), але vision='pushup'
    pose_result = {
        "exercise": "squat",
        "reps": 25,
        "confidence": 0.70,
        "flags": [],
        "valid": True,
    }
    vision_result = VisionClassification(exercise="pushup", confidence=0.90, reasoning="Pushups on floor")
    reconciled = reconcile_classes(pose_result, vision_result)

    assert reconciled["exercise"] == "none"
    assert reconciled["pose_class"] == "squat"
    assert reconciled["vision_class"] == "pushup"
    assert reconciled["valid"] is False
    assert "class_mismatch" in reconciled["flags"]


def test_reconcile_vision_none_junk_meme_rejected():
    # Pose нарахував якісь хибні повтори на мемі, але Vision визначив 'none'
    pose_result = {
        "exercise": "squat",
        "reps": 2,
        "confidence": 0.60,
        "flags": [],
        "valid": True,
    }
    vision_result = VisionClassification(exercise="none", confidence=0.99, reasoning="Funny meme face")
    reconciled = reconcile_classes(pose_result, vision_result)

    assert reconciled["exercise"] == "none"
    assert reconciled["pose_class"] == "squat"
    assert reconciled["vision_class"] == "none"
    assert reconciled["valid"] is False
    assert "not_exercise" in reconciled["flags"]


def test_reconcile_when_vision_is_none():
    pose_result = {
        "exercise": "pushup",
        "reps": 20,
        "confidence": 0.85,
        "flags": [],
        "valid": True,
    }
    reconciled = reconcile_classes(pose_result, None)
    assert reconciled["exercise"] == "pushup"
    assert reconciled["vision_class"] is None
    assert reconciled["pose_class"] == "pushup"
    assert reconciled["valid"] is True
