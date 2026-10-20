"""
Gemini vision classifier for video frames and reconciliation with pose estimation (T-33).
Extracts 3-4 frames from the stable middle segment of video and classifies exercise
as 'pushup', 'squat', or 'none'.
"""
import logging
import os
import subprocess
from typing import Dict, List, Literal, Optional, Any
from pydantic import BaseModel, Field

from core.cv.rep_counter import probe
from core.llm import GeminiLLM, LLMUnavailable

logger = logging.getLogger(__name__)

VISION_SYSTEM_PROMPT = """Ти — суворий спортивний AI-суддя SportNerdBot.
Твоє завдання — переглянути 3–4 кадри з середини відео та визначити, яку спортивну вправу виконує людина.

Класифікуй вправу ВИКЛЮЧНО як одне з наступних значень:
- 'pushup': віджимання від підлоги (людина в упорі лежачи або віджимається).
- 'squat': присідання (людина стоїть і присідає/згинає коліна).
- 'none': не є спортивною вправою (меми, обличчя, людина просто сидить/стоїть/йде, тварини, комп'ютерні ігри, випадковий флуд).

Повертай JSON зі структурою:
{"exercise": "pushup"|"squat"|"none", "confidence": float, "reasoning": "коротке пояснення"}
"""


class VisionClassification(BaseModel):
    exercise: str = Field(description="'pushup', 'squat', or 'none'")
    confidence: float = Field(default=0.8, description="0.0 to 1.0 confidence")
    reasoning: str = Field(default="", description="Short reasoning")


def extract_middle_frames(video_path: str, count: int = 3) -> List[bytes]:
    """
    Витягує 3-4 кадри зі стабільного середнього сегмента відео (від 20% до 80% тривалості).
    Використовує cv2 якщо доступно, інакше ffmpeg pipe.
    """
    if not os.path.isfile(video_path):
        return []

    meta = probe(video_path)
    duration = meta.get("duration", 0.0)
    if duration <= 0:
        return []

    # Часові мітки: наприклад для count=3 при duration=30 -> 7.5s, 15.0s, 22.5s
    timestamps = [
        duration * (0.25 + 0.5 * i / max(1, count - 1))
        for i in range(count)
    ]

    frames: List[bytes] = []

    # 1. Спробуємо через cv2 якщо встановлено
    try:
        import cv2
        cap = cv2.VideoCapture(video_path)
        if cap.isOpened():
            for ts in timestamps:
                cap.set(cv2.CAP_PROP_POS_MSEC, ts * 1000.0)
                ok, fr = cap.read()
                if ok and fr is not None:
                    # Resize до помірного розміру для економії квоти Gemini
                    h, w = fr.shape[:2]
                    s = min(1.0, 512.0 / max(h, w))
                    if s < 1.0:
                        fr = cv2.resize(fr, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
                    ok_enc, buf = cv2.imencode(".jpg", fr, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                    if ok_enc:
                        frames.append(buf.tobytes())
            cap.release()
            if len(frames) == count:
                return frames
    except Exception as e:
        logger.debug("cv2 frame extraction failed, trying ffmpeg: %s", e)

    # 2. Фолбек на ffmpeg subprocess
    frames = []
    for ts in timestamps:
        cmd = [
            "ffmpeg", "-v", "error",
            "-ss", f"{ts:.2f}",
            "-i", video_path,
            "-vf", "scale='min(512,iw)':'min(512,ih)':force_original_aspect_ratio=decrease",
            "-frames:v", "1",
            "-f", "image2pipe",
            "-c:v", "mjpeg",
            "-q:v", "4",
            "-",
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, check=True)
            if res.stdout:
                frames.append(res.stdout)
        except Exception as e:
            logger.warning("ffmpeg frame extract at %.1fs failed: %s", ts, e)

    return frames


def classify_exercise_vision(llm: GeminiLLM, frames: List[bytes]) -> VisionClassification:
    """Викликає Gemini vision для класифікації вправи на кадрах."""
    if not frames:
        return VisionClassification(exercise="none", confidence=0.0, reasoning="No frames extracted")

    prompt = "Оціни ці кадри з відео звіту. Яку вправу виконує спортсмен? 'pushup', 'squat', чи це не вправа ('none')?"
    try:
        result = llm.vision_json(VISION_SYSTEM_PROMPT, prompt, frames, VisionClassification)
        # Нормалізуємо клас
        ex = (result.exercise or "none").lower().strip()
        if ex not in ("pushup", "squat", "none"):
            ex = "none"
        result.exercise = ex
        return result
    except LLMUnavailable as e:
        logger.warning("Gemini vision unavailable: %s", e)
        return VisionClassification(exercise="none", confidence=0.0, reasoning=f"LLM unavailable: {e}")


def reconcile_classes(pose_result: Dict[str, Any], vision_result: Optional[VisionClassification]) -> Dict[str, Any]:
    """
    Звіряє результати класифікатора пози та Gemini-vision.
    Якщо класи сходяться — valid=True.
    Якщо не сходяться або vision='none' (flood/meme) — valid=False, flag 'class_mismatch'.
    """
    res = dict(pose_result)
    flags = list(res.get("flags", []))
    pose_class = res.get("exercise", "none")

    if vision_result is None:
        res["vision_class"] = None
        res["pose_class"] = pose_class
        return res

    vision_class = vision_result.exercise
    res["vision_class"] = vision_class
    res["pose_class"] = pose_class

    if vision_class == "none":
        # Мем, флуд або людина не займається спортом
        res["exercise"] = "none"
        res["valid"] = False
        if "not_exercise" not in flags and "junk_video" not in flags:
            flags.append("not_exercise")
    elif pose_class == vision_class:
        # Збіг! Вправа підтверджена двома незалежними методами
        res["exercise"] = pose_class
        # valid залишається таким, як визначив pose (залежно від reps/confidence)
        if res.get("reps", 0) > 0 and res.get("confidence", 0) >= 0.5:
            res["valid"] = True
    else:
        # Розбіжність (наприклад, pose подумав присяд, а vision бачить віджимання або навпаки)
        res["exercise"] = "none"
        res["valid"] = False
        if "class_mismatch" not in flags:
            flags.append("class_mismatch")

    res["flags"] = flags
    return res
