"""
Парсинг заявлених користувачем повторів та вправ із підписів до відео або повідомлень чату.
Pure functions, unit-testable.
"""
import re
from typing import List, Optional, Tuple

_RE_REPLY_CONTEXT = re.compile(r"\[У відповідь на [^\]]*?: «.*?»\]\s*", re.DOTALL)
_RE_VIDEO_MARKER = re.compile(r"\[Надіслано відеозвіт виконання норми\]\.?\s*")

_RE_UNIT_AFTER = re.compile(
    r"^(?:-?(?:й|ий|ій|го|му)\s+)?(?:хв\b|хвилин\w*|сек\b|секунд\w*|с\b|год\b|годин\w*|кг\b|кілограм\w*|%|відсот\w*|процент\w*|підхід\w*|підход\w*)",
    re.IGNORECASE,
)
_RE_SET_BEFORE = re.compile(
    r"(?:підхід\w*|підход\w*)\s*(?:№|#)?\s*$",
    re.IGNORECASE,
)

_EXERCISE_REP_ROOTS = (  # tuple для str.startswith
    "віджим",
    "відтис",
    "pushup",
    "push-up",
    "присід",
    "присяд",
    "squat",
    "раз",
    "повтор",
)


def _clean_text(text: str) -> str:
    text = _RE_REPLY_CONTEXT.sub("", text)
    text = _RE_VIDEO_MARKER.sub("", text)
    return text.strip()


def parse_claimed_exercise(text: Optional[str]) -> Optional[str]:
    """
    Визначає заявлену вправу з тексту підпису або повідомлення.
    'pushups' якщо є (case-insens) "віджим" / "відтис" / "pushup" / "push-up";
    'squats' якщо "присід" / "присяд" / "squat";
    None якщо жодної або обидві вправи згадані.
    """
    if not text or not isinstance(text, str):
        return None
    cleaned = _clean_text(text).lower()
    has_pushups = any(k in cleaned for k in ("віджим", "відтис", "pushup", "push-up"))
    has_squats = any(k in cleaned for k in ("присід", "присяд", "squat"))
    if has_pushups and not has_squats:
        return "pushups"
    if has_squats and not has_pushups:
        return "squats"
    return None


def parse_claimed_reps(text: Optional[str]) -> Optional[int]:
    """
    Витягує заявлену кількість повторів із тексту підпису або повідомлення.
    - Ігнорує маркер [Надіслано відеозвіт виконання норми]. та блоки [У відповідь на ...: «...»].
    - Ігнорує числа з одиницями часу/ваги/відсотків/підходів (хв, сек, с, кг, %, год, підходи).
    - Ігнорує числа < 1 або > 1000.
    - Надає перевагу числу, безпосередньо суміжному зі словом вправи/повтору (напр. "56 віджимань", "x56").
    - Інакше, якщо лишилось рівно одне число — повертає його; якщо кілька — найбільше.
    - None якщо чисел немає.
    """
    if not text or not isinstance(text, str):
        return None
    cleaned = _clean_text(text)
    if not cleaned:
        return None

    # Прибираємо дати та часові позначки, щоб вони не розпізнавались як повтори
    t = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", " ", cleaned)
    t = re.sub(r"\b\d{1,2}\.\d{1,2}(?:\.\d{2,4})?\b", " ", t)
    t = re.sub(r"\b\d{1,2}:\d{2}(?::\d{2})?\b", " ", t)

    matches = list(re.finditer(r"(?i)\b(?:[xх]\s*)?(\d+)(?:\s*[xх])?\b", t))
    if not matches:
        return None

    candidates: List[Tuple[int, bool]] = []

    for m in matches:
        full_match = m.group(0).strip()
        num_str = m.group(1)
        val = int(num_str)
        if val < 1 or val > 1000:
            continue

        start, end = m.start(), m.end()

        # Що йде безпосередньо після числа
        after = t[end:].lstrip()
        if _RE_UNIT_AFTER.match(after):
            continue

        # Що йде безпосередньо перед числом (наприклад, номер підходу)
        before = t[:start].rstrip()
        if _RE_SET_BEFORE.search(before):
            continue

        # Суміжність зі словом вправи/повтору:
        # 1. Префікс або суфікс x/х (наприклад "x56" або "56x")
        has_x = full_match.lower().startswith(("x", "х")) or full_match.lower().endswith(("x", "х"))

        # 2. Попереднє слово
        prev_match = re.search(r"(\w[\w-]*)\W*$", before)
        prev_word = prev_match.group(1).lower() if prev_match else ""

        # 3. Наступне слово
        next_match = re.search(r"^\W*(\w[\w-]*)", after)
        next_word = next_match.group(1).lower() if next_match else ""

        is_adjacent = (
            has_x
            or prev_word.startswith(_EXERCISE_REP_ROOTS)
            or next_word.startswith(_EXERCISE_REP_ROOTS)
        )

        candidates.append((val, is_adjacent))

    if not candidates:
        return None

    adjacent_candidates = [val for val, is_adj in candidates if is_adj]
    if adjacent_candidates:
        return max(adjacent_candidates)

    if len(candidates) == 1:
        return candidates[0][0]

    return max(val for val, _ in candidates)
