"""
Дедуплікація повторно завантажених відео (інцидент 27.10: Саша надіслав те саме відео з телефона й з ноута —
новий file_unique_id, тривалість 115 → 116 с, бот зарахував двічі).
"""
import random

import pytest

from core import dedup
from core.cv.fingerprint import dhash_bytes, frame_hashes, video_fingerprint
from tests.flow_fixtures import CHAT, make_flow

DAY = "2026-10-04"


def _timeline(seed, n=67, period=1.7):
    rnd = random.Random(seed)
    t, out = 2.0, []
    for _ in range(n):
        t += period + rnd.uniform(-0.25, 0.25)
        out.append(round(t, 2))
    return out


def _fp(rep_t, dur, ph=None):
    return {"v": 1, "dur": dur, "ph": ph or [], "rep_t": rep_t}


def _rec(rid="a", dur=115.0, reps=67, ex="squat", fuid="u1", size=9_000_000, sha=None, fp=None, w=1080, h=1920):
    return dict(request_id=rid, file_unique_id=fuid, file_size=size, width=w, height=h, duration_s=dur,
                sha256=sha, exercise=ex, reps=reps, fingerprint=fp)


# ---------- чисті функції ----------
def test_reencoded_same_video_matches_by_rep_timeline():
    a = _timeline(1)
    rnd = random.Random(7)
    b = [round(t * (116.0 / 115.0) + 0.15 + rnd.uniform(-0.08, 0.08), 2) for t in a]  # масштаб часу + зсув + шум
    old = _rec("a", 115.0, fp=_fp(a, 115.2))
    new = _rec("b", 116.0, fuid="u2", size=7_400_000, fp=_fp(b, 116.2))
    dup, why = dedup.is_duplicate(new, old)
    assert dup and why == "rep_timeline"


def test_different_set_same_reps_and_duration_is_not_duplicate():
    old = _rec("a", 115.0, fp=_fp(_timeline(1), 115.2, ph=["ffff000011112222"] * 8))
    new = _rec("b", 115.0, fuid="u2", size=8_000_000, fp=_fp(_timeline(2), 115.1, ph=["ffff000011112222"] * 8))
    assert dedup.timeline_match(_timeline(1), _timeline(2)) < 0.6
    dup, why = dedup.is_duplicate(new, old)
    assert not dup, why


def test_sha256_and_unique_id_and_same_size():
    assert dedup.is_duplicate(_rec(sha="x"), _rec(sha="x", fuid="u9")) == (True, "sha256")
    assert dedup.is_duplicate(_rec(fuid="u1"), _rec(fuid="u1", dur=300)) == (True, "file_unique_id")
    assert dedup.is_duplicate(_rec(fuid="n1"), _rec(fuid="n2")) == (True, "same_size")
    assert not dedup.is_duplicate(_rec(fuid="n1", size=1), _rec(fuid="n2", size=2, reps=40))[0]


def test_duration_gate_and_exercise_gate():
    t = _timeline(1)
    base = _rec("a", 115.0, fp=_fp(t, 115.0))
    far = _rec("b", 120.0, fuid="u2", size=1, fp=_fp(t, 120.0))
    assert dedup.is_duplicate(far, base) == (False, "duration")
    other_ex = _rec("c", 115.0, ex="pushup", fuid="u3", size=2, fp=_fp(t, 115.0))
    assert not dedup.is_duplicate(other_ex, base)[0]


def test_phash_route_for_few_reps_and_no_fingerprint_fallback():
    ph = ["0f0f0f0f0f0f0f0f"] * 8
    near = ["0f0f0f0f0f0f0f0e"] * 8
    a = _rec("a", 30.0, reps=3, fp=_fp([5.0, 9.0, 13.0], 30.0, ph))
    b = _rec("b", 31.0, reps=3, fuid="u2", size=5, fp=_fp([5.0, 9.0, 13.0], 31.0, near))
    assert dedup.is_duplicate(b, a) == (True, "phash_reps")
    far = _rec("c", 31.0, reps=3, fuid="u3", size=6, fp=_fp([5.0, 9.0, 13.0], 31.0, ["f0f0f0f0f0f0f0f0"] * 8))
    assert not dedup.is_duplicate(far, a)[0]
    # без відбитків (старий worker): однакові вправа+повтори+тривалість, ≥15 повторів
    assert dedup.is_duplicate(_rec("n", 116.0, fuid="u5", size=3), _rec("o", 115.0)) == (True, "duration_reps")
    assert not dedup.is_duplicate(_rec("n", 116.0, reps=10, fuid="u5", size=3), _rec("o", 115.0, reps=10))[0]


def test_dhash_bytes_and_fingerprint_without_ffmpeg(tmp_path):
    flat = bytes([100] * 72)
    assert dhash_bytes(flat) == "0" * 16
    inc = bytes([r * 9 + c for r in range(8) for c in range(9)][:72])
    assert dhash_bytes(inc) == "0" * 16          # зростання зліва направо -> усі біти 0
    dec = bytes([255 - (c * 10) for r in range(8) for c in range(9)])
    assert dhash_bytes(dec) == "f" * 16
    assert dhash_bytes(b"short") is None
    missing = str(tmp_path / "none.mp4")
    assert frame_hashes(missing, 60.0) == []
    fp = video_fingerprint(missing, 60.0, [1.234, 2.5])
    assert fp["rep_t"] == [1.23, 2.5] and fp["ph"] == [] and fp["dur"] == 60.0


# ---------- store ----------
def test_store_video_meta_roundtrip_and_retention(tmp_path):
    flow, _, _ = make_flow(tmp_path, llm=None)
    st = flow.store
    st.save_video_meta("r1", "sasha", file_unique_id="u1", file_size=100, width=1, height=2, duration_s=9.0, file_name="a.mp4")
    st.set_video_sha("r1", "abc", 100)
    st.set_video_fingerprint("r1", {"ph": ["00"], "rep_t": [1.0]})
    m = st.get_video_meta("r1")
    assert m["sha256"] == "abc" and m["fingerprint"]["rep_t"] == [1.0] and m["file_name"] == "a.mp4"
    assert [x["request_id"] for x in st.recent_video_metas("sasha")] == ["r1"]
    assert st.recent_video_metas("sasha", exclude_request_id="r1") == []
    assert st.recent_video_metas("other") == []
    with st._connect() as con:
        con.execute("UPDATE video_fingerprints SET created_at = datetime('now','-5 days')")
    st.save_video_meta("r2", "sasha")
    assert st.get_video_meta("r1") is None  # старше 3 діб прибрано


# ---------- флоу ----------
async def _send(flow, user, mid, fuid, dur, size, rid_box=None):
    ok, reply = await flow.handle_video(CHAT, user, mid, file_id=f"f{fuid}", file_unique_id=fuid, duration=dur,
                                        file_size=size, width=1080, height=1920, file_name=f"{fuid}.mp4")
    return ok, reply


def _finish(flow, rid, reps, ex, fp=None, sha=None):
    flow.store.update_video_check(rid, status="done", exercise=ex, reps=reps, confidence=0.9, valid=True, finished=True)
    if fp is not None:
        flow.store.set_video_fingerprint(rid, fp)
    if sha:
        flow.store.set_video_sha(rid, sha, 1)


@pytest.mark.asyncio
async def test_flow_reupload_not_credited_twice(tmp_path):
    flow, sender, _ = make_flow(tmp_path, llm=None, trust_mode=True)
    user = flow.store.get_user("lyosha")
    t = _timeline(3)
    ok1, _ = await _send(flow, user, 10, "uidA", 115, 9_000_000)
    ok2, _ = await _send(flow, user, 11, "uidB", 116, 7_400_000)  # інший id/розмір/тривалість — приймається до рахунку
    assert ok1 and ok2
    c1, c2 = flow.store.list_video_checks()
    _finish(flow, c1.request_id, 67, "squat", fp=_fp(t, 115.2))
    _finish(flow, c2.request_id, 67, "squat", fp=_fp([x + 0.1 for x in t], 116.1))
    day_ex_before = flow.store.get_state(f"video_credit:{c1.request_id}", "")
    await flow.handle_video_callback(c1.request_id)
    assert flow.store.get_state(f"video_credit:{c1.request_id}", "") != day_ex_before  # перше зараховане
    n_texts = len(sender.texts)
    out = await flow.handle_video_callback(c2.request_id)
    assert out is None
    assert flow.store.get_state(f"video_credit:{c2.request_id}", "") == ""  # вдруге не зараховано
    assert "вже бачив" in sender.texts[-1] and len(sender.texts) == n_texts + 1
    flags = flow.store.get_video_check(c2.request_id).flags
    assert any(f.startswith("duplicate_of:") and f.endswith(":rep_timeline") for f in flags)


@pytest.mark.asyncio
async def test_flow_same_sha_other_everything_is_duplicate(tmp_path):
    flow, sender, _ = make_flow(tmp_path, llm=None, trust_mode=True)
    user = flow.store.get_user("lyosha")
    await _send(flow, user, 10, "uidA", 60, 5_000_000)
    await _send(flow, user, 11, "uidB", 61, 4_000_000)
    c1, c2 = flow.store.list_video_checks()
    _finish(flow, c1.request_id, 20, "pushup", sha="same")
    _finish(flow, c2.request_id, 20, "pushup", sha="same")
    await flow.handle_video_callback(c1.request_id)
    await flow.handle_video_callback(c2.request_id)
    assert flow.store.get_state(f"video_credit:{c2.request_id}", "") == ""
    assert "вже бачив" in sender.texts[-1]


@pytest.mark.asyncio
async def test_flow_genuinely_new_video_is_credited(tmp_path):
    flow, sender, _ = make_flow(tmp_path, llm=None, trust_mode=True)
    user = flow.store.get_user("lyosha")
    await _send(flow, user, 10, "uidA", 115, 9_000_000)
    await _send(flow, user, 11, "uidB", 115, 8_900_000)
    c1, c2 = flow.store.list_video_checks()
    _finish(flow, c1.request_id, 67, "squat", fp=_fp(_timeline(11), 115.1))
    _finish(flow, c2.request_id, 67, "squat", fp=_fp(_timeline(12), 115.0))
    await flow.handle_video_callback(c1.request_id)
    await flow.handle_video_callback(c2.request_id)
    assert flow.store.get_state(f"video_credit:{c2.request_id}", "") != ""
    assert not any(f.startswith("duplicate_of") for f in flow.store.get_video_check(c2.request_id).flags)


@pytest.mark.asyncio
async def test_flow_same_file_new_id_rejected_at_receipt(tmp_path):
    flow, sender, _ = make_flow(tmp_path, llm=None, trust_mode=True)
    user = flow.store.get_user("lyosha")
    ok1, _ = await _send(flow, user, 10, "uidA", 115, 9_000_000)
    ok2, reply = await _send(flow, user, 11, "uidB", 115, 9_000_000)
    assert ok1 and not ok2 and "вже бачив" in reply
    assert len(flow.store.list_video_checks()) == 1


@pytest.mark.asyncio
async def test_other_user_same_video_not_duplicate(tmp_path):
    flow, sender, _ = make_flow(tmp_path, llm=None, trust_mode=True)
    ok1, _ = await _send(flow, flow.store.get_user("lyosha"), 10, "uidA", 115, 9_000_000)
    ok2, _ = await _send(flow, flow.store.get_user("kovdimay") or flow.store.get_user("lyosha"), 11, "uidB", 115, 9_000_000)
    assert ok1
    # інший автор: дедуп за розміром не спрацьовує (за 48 год шукаємо лише відео того самого автора)
    if flow.store.get_user("kovdimay"):
        assert ok2
