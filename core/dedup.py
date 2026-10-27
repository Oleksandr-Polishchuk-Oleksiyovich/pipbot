"""
Дедуплікація повторно надісланих відео (чисті функції, без I/O).

Telegram дає перезавантаженому відео НОВИЙ file_unique_id (і часто 1 с іншу тривалість / інший розмір після
перекодування), тож id-критерій його не ловить. Тут порівнюємо те, що переживає перекодування:
  1) sha256 байтів (VM рахує під час завантаження для box worker-а) — точний збіг;
  2) метадані отримання: однакові розмір у байтах + розміри кадру + тривалість (той самий файл під іншим id);
  3) відбиток від box CV: таймінги повторів (rep_t) та dHash кількох кадрів — переживають перекодування.
Хеші кадрів самі по собі НЕ доказ (одна кімната/камера щодня дає схожі кадри) — лише разом із тими ж
вправою, кількістю повторів і тривалістю. Збіг таймінгів 5+ повторів — сильний доказ.
"""
from typing import Any, Dict, List, Optional, Tuple

DUR_TOL_S = 2.0          # тривалість з Telegram цілочисельна; перекодування зсуває на ~1 с
FP_DUR_TOL_S = 1.5       # точна тривалість з ffprobe (у відбитку)
TIMELINE_TOL_S = 0.2     # допуск збігу піків повторів (після масштабу за тривалістю і зсуву); період повтору ~1.5–2 с,
                         # тож ширший допуск «ловить» будь-який рівномірний підхід
TIMELINE_MIN_REPS = 5
TIMELINE_MATCH = 0.85
PHASH_MAX_MEAN_BITS = 10.0
NO_FP_MIN_REPS = 15      # без відбитка: однакові вправа+повтори+тривалість ±1.5 с, не менше 15 повторів
NO_FP_DUR_TOL_S = 1.5


def hamming_hex(a: str, b: str) -> Optional[int]:
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except (TypeError, ValueError):
        return None


def phash_mean_distance(pa: Optional[List[str]], pb: Optional[List[str]]) -> Optional[float]:
    """Середня відстань Хемінга між хешами кадрів на однакових позиціях (None — нема що порівнювати)."""
    if not pa or not pb or len(pa) != len(pb):
        return None
    ds = [hamming_hex(x, y) for x, y in zip(pa, pb)]
    ds = [d for d in ds if d is not None]
    return (sum(ds) / len(ds)) if ds else None


def timeline_match(ta: Optional[List[float]], tb: Optional[List[float]], tol: float = TIMELINE_TOL_S,
                   dur_a: Optional[float] = None, dur_b: Optional[float] = None) -> Optional[float]:
    """
    Частка піків A, що мають пік B у межах tol після масштабу за тривалістю (перекодування може розтягнути час
    на ~1%) і вирівнювання на медіанний зсув; None — замало повторів.
    Різні підходи з однаковою кількістю повторів дають низьку частку; те саме відео — ~1.0.
    """
    if not ta or not tb or len(ta) < TIMELINE_MIN_REPS or len(tb) < TIMELINE_MIN_REPS:
        return None
    tb = sorted(float(x) for x in tb)
    scales = [1.0]
    if dur_a and dur_b and dur_a > 0 and dur_b > 0 and abs(dur_b / dur_a - 1.0) > 1e-3:
        scales.append(min(1.03, max(0.97, dur_b / dur_a)))  # перекодування могло розтягнути час
    best = 0.0
    for sc in scales:
        xs = sorted(float(x) * sc for x in ta)
        nearest = [min(tb, key=lambda y, x=x: abs(y - x)) - x for x in xs]
        shift = sorted(nearest)[len(nearest) // 2]
        ok = sum(1 for x in xs if min(abs(y - (x + shift)) for y in tb) <= tol)
        best = max(best, ok / len(xs))
    return best


def _num(d: Dict[str, Any], key: str) -> Optional[float]:
    v = d.get(key)
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def is_duplicate(new: Dict[str, Any], old: Dict[str, Any]) -> Tuple[bool, str]:
    """
    new/old: {file_unique_id, file_size, width, height, duration_s, sha256, exercise, reps, fingerprint}
    (fingerprint — dict від box CV або None). Повертає (чи дубль, причина).
    """
    sa, sb = new.get("sha256"), old.get("sha256")
    if sa and sb and sa == sb:
        return True, "sha256"
    fa, fb = new.get("file_unique_id"), old.get("file_unique_id")
    if fa and fb and fa == fb:
        return True, "file_unique_id"

    da, db = _num(new, "duration_s"), _num(old, "duration_s")
    if da is not None and db is not None and abs(da - db) > DUR_TOL_S:
        return False, "duration"

    # Той самий файл під іншим id: збігаються байти за розміром, кадр і тривалість.
    sza, szb = _num(new, "file_size"), _num(old, "file_size")
    if (sza and szb and sza == szb and da is not None and da == db
            and new.get("width") and new.get("width") == old.get("width")
            and new.get("height") and new.get("height") == old.get("height")):
        return True, "same_size"

    ex_a, ex_b = new.get("exercise"), old.get("exercise")
    ra, rb = int(new.get("reps") or 0), int(old.get("reps") or 0)
    if not ex_a or ex_a == "none" or ex_a != ex_b or ra <= 0 or rb <= 0:
        return False, "no_cv_match"

    fpa, fpb = new.get("fingerprint") or {}, old.get("fingerprint") or {}
    dur_a = _num(fpa, "dur") if fpa else None
    dur_b = _num(fpb, "dur") if fpb else None
    if dur_a is not None and dur_b is not None and abs(dur_a - dur_b) > FP_DUR_TOL_S:
        return False, "duration_fp"

    if abs(ra - rb) <= 1:
        tm = timeline_match(fpa.get("rep_t"), fpb.get("rep_t"), dur_a=dur_a, dur_b=dur_b)
        if tm is not None and tm >= TIMELINE_MATCH:
            return True, "rep_timeline"
    if ra == rb:
        pd = phash_mean_distance(fpa.get("ph"), fpb.get("ph"))
        if pd is not None and pd <= PHASH_MAX_MEAN_BITS:
            # хеші кадрів + ті самі повтори; лише коли таймінги неінформативні (<5 повторів), інакше вирішує rep_timeline
            if timeline_match(fpa.get("rep_t"), fpb.get("rep_t"), dur_a=dur_a, dur_b=dur_b) is None:
                return True, "phash_reps"
        if not fpa or not fpb:
            if ra >= NO_FP_MIN_REPS and da is not None and db is not None and abs(da - db) <= NO_FP_DUR_TOL_S:
                return True, "duration_reps"
    return False, "no_match"
