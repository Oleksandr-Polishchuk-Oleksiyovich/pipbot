"""
Відбиток відео для дедуплікації (рахується на box worker-і поруч із CV, дешево):
  dur   — точна тривалість (ffprobe), с;
  ph    — dHash (64 біти, hex) для 8 кадрів на фіксованих частках тривалості (ffmpeg, один кадр на виклик);
  rep_t — часи піків повторів (с) з rep_counter; переживають перекодування.
Див. core/dedup.py: рішення «дубль» приймається там.
"""
import subprocess
from typing import Any, Dict, List, Optional

FRACTIONS = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8)


def dhash_bytes(gray9x8: bytes) -> Optional[str]:
    """dHash із 72 байтів (9 стовпців × 8 рядків, 8-бітний gray) -> 16 hex."""
    if len(gray9x8) != 72:
        return None
    bits = 0
    for row in range(8):
        base = row * 9
        for col in range(8):
            bits = (bits << 1) | (1 if gray9x8[base + col] > gray9x8[base + col + 1] else 0)
    return f"{bits:016x}"


def frame_hashes(path: str, duration_s: float, fractions=FRACTIONS, timeout_s: float = 20.0) -> List[str]:
    out: List[str] = []
    if not duration_s or duration_s <= 0:
        return out
    for fr in fractions:
        try:
            proc = subprocess.run(
                ["ffmpeg", "-v", "error", "-ss", f"{duration_s * fr:.2f}", "-i", path, "-frames:v", "1",
                 "-vf", "scale=9:8:flags=area,format=gray", "-f", "rawvideo", "-"],
                capture_output=True, timeout=timeout_s,
            )
            h = dhash_bytes(proc.stdout) if proc.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            h = None
        if h is None:
            return []  # неповний набір — краще без хешів, ніж зсунуті позиції
        out.append(h)
    return out


def video_fingerprint(path: str, duration_s: Optional[float], rep_times: Optional[List[float]] = None) -> Dict[str, Any]:
    dur = duration_s
    if not dur:
        try:
            from cv_worker.vision import probe
            dur = float(probe(path).get("duration", 0.0) or 0.0)
        except Exception:  # noqa: BLE001
            dur = 0.0
    fp: Dict[str, Any] = {"v": 1, "dur": round(float(dur or 0.0), 2)}
    fp["ph"] = frame_hashes(path, float(dur or 0.0))
    fp["rep_t"] = [round(float(t), 2) for t in (rep_times or [])][:400]
    return fp
