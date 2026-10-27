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


# --- Оспорювання підрахунку (TRUST_MODE, м'який режим «до виправлення»): слова людини важливіші за CV ---
_RE_REPLY_PREFIX = re.compile(r"^\s*\[У відповідь на ([^\]]*?): «(.*?)»\]\s*", re.DOTALL)

# Маркери оспорювання (нижній регістр; укр. + рос. + англ.)
_DISPUTE_PATTERNS = [re.compile(p) for p in (
    r"неправильн", r"невірн", r"не\s+вірн", r"неточн", r"помил", r"недораху", r"недосчита", r"не\s+так\s+(?:порах|рахув)",
    r"це\s+не\s+так", r"не\s+так\b", r"не\s+те\s+порах", r"не\s+порахував", r"не\s+порахувало", r"не\s+порахувала",
    r"зарахуй", r"перерахуй", r"перепорахуй", r"порахуй\s+(?:ще|знову|ще\s+раз)", r"перевір\s+(?:ще|знову)",
    r"(?:зробив|зробила|було|вийшло)\s+більше", r"насправді", r"а\s+не\s+\d", r"не\s+\d+\s*,?\s*а\s+\d",
    r"ошиб", r"неправильно\s+посчит", r"не\s+посчита", r"засчитай", r"пересчитай", r"не\s+так\s+посчит",
    r"\bwrong\b", r"miscount", r"mistake", r"recount", r"you\s+counted",
)]
# Слова «я зробив N» — оспорювання лише якщо в тексті є число
_NUMBERED_CLAIM_PATTERNS = [re.compile(p) for p in (
    r"(?:я\s+)?(?:зробив|зробила|вийшло|вийшов|було|набралось|набралося|відтиснувся|відтиснулась)", r"(?:сделал|сделала|получилось|было)",
)]


def split_reply_context(text: Optional[str]) -> Tuple[Optional[str], Optional[str], str]:
    """(автор цитованого, цитований текст, решта) з describe_message; (None, None, text) якщо це не reply."""
    if not text:
        return None, None, ""
    m = _RE_REPLY_PREFIX.match(text)
    if not m:
        return None, None, text.strip()
    return m.group(1).strip(), m.group(2).strip(), text[m.end():].strip()


# Клаузи-заперечення: «50 там і не пахне», «50 і близько не було», «нема там 50» — число в них НЕ є виправленням
_RE_NEGATED_CLAUSE = re.compile(
    r"не\s+пахне|і\s+близько|близько\s+не|не\s+було|не\s+вийшло|не\s+стільки|не\s+набагато|нема\w*|немає|ні\s+разу|"
    r"не\s+так\s+багато|не\s+вистачає|не\s+дотягу|не\s+зарахов",
    re.IGNORECASE,
)
# Клауза, де людина називає свій результат: «я 23 зробив», «зробив 23», «вийшло 23»
_RE_CLAIM_VERB = re.compile(
    r"\b(?:я|мені|в\s+мене|у\s+мене)\b|зробив|зробила|вийшл|набрал|відтисн|присів|присіл|було|сделал|получил",
    re.IGNORECASE,
)
_RE_CLAUSE_SPLIT = re.compile(r"[,;.!?\n]+|\s[—–-]\s")


def _strip_number(text: str, number: int) -> str:
    return re.sub(rf"(?<!\d){number}(?!\d)", " ", text)


def parse_dispute_reps(text: Optional[str], exclude: Optional[int] = None) -> Optional[int]:
    """
    Число, яким людина виправляє підрахунок: «не 4 а 40» -> 40, «40, а не 4» -> 40, «я зробив 50» -> 50,
    «Нє нє, 50 там і не пахне, я 23 зробив» -> 23 (число в клаузі-запереченні відкидається; пріоритет — клауза «я … зробив»).
    exclude — число, яке порахував бот (CV): якщо в тексті є інше число, то воно перемагає (людина НЕ погоджується з CV).
    Нижче число так само валідне, як і вище. None — числа немає.
    """
    if not text:
        return None
    _, _, rest = split_reply_context(text)
    cleaned = re.sub(r"(?i)\bне\s+\d+", " ", rest)
    clauses = [c.strip() for c in _RE_CLAUSE_SPLIT.split(cleaned) if c and c.strip()]
    with_num = [c for c in clauses if parse_claimed_reps(c) is not None]
    if len(with_num) > 1:
        positive = [c for c in with_num if not _RE_NEGATED_CLAUSE.search(c)]
        if positive:
            with_num = positive
        claim = [c for c in with_num if _RE_CLAIM_VERB.search(c)]
        if claim:
            with_num = claim
    pool = " , ".join(with_num) if with_num else cleaned
    value = parse_claimed_reps(pool)
    if exclude is not None and value == exclude:
        other = parse_claimed_reps(_strip_number(pool, exclude))
        if other is not None:
            return other
        other = parse_claimed_reps(_strip_number(cleaned, exclude))
        if other is not None:
            return other
    return value


def is_dispute(text: Optional[str], reply_to_bot_count: bool = False) -> bool:
    """
    Чи оспорює повідомлення підрахунок на відео. reply_to_bot_count — це reply на повідомлення бота «Побачив N…»
    (тоді достатньо числа). Без маркера й без числа — не оспорювання.
    """
    if not text:
        return False
    _, _, rest = split_reply_context(text)
    low = rest.lower()
    if not low:
        return False
    has_number = parse_dispute_reps(text) is not None
    if any(p.search(low) for p in _DISPUTE_PATTERNS):
        return True
    if has_number and any(p.search(low) for p in _NUMBERED_CLAIM_PATTERNS):
        return True
    if reply_to_bot_count and has_number:
        return True
    return False


# Слова-числа (укр./рос. корені) — дешевий префільтр: чи МОЖЕ повідомлення називати кількість (щоб не кликати LLM на кожну репліку)
_NUMBER_WORD_ROOTS = (
    "один", "одна", "одне", "два", "дві", "три", "чотири", "п'ят", "пʼят", "п’ят", "шіст", "сім", "вісім", "дев'ят", "дев’ят",
    "десят", "десь", "двадцят", "тридцят", "сорок", "п'ятдесят", "пʼятдесят", "п’ятдесят", "шістдесят", "сімдесят", "вісімдесят",
    "дев'яност", "дев’яност", "сто", "сотн", "сотк", "сотня", "півсот", "пів сот", "півтор", "тисяч",
    "одиннадцат", "двенадцат", "двадцать", "тридцать", "пятьдесят", "полсотни", "сорок",
    "twenty", "thirty", "forty", "fifty", "hundred",
)


def looks_like_count(text: Optional[str]) -> bool:
    """Чи може текст містити кількість повторів (цифра або слово-число). Лише префільтр перед LLM-тулом report_reps."""
    if not text:
        return False
    _, _, rest = split_reply_context(text)
    low = rest.lower()
    if re.search(r"\d", low):
        return True
    return any(root in low for root in _NUMBER_WORD_ROOTS)
