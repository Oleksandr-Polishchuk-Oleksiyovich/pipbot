from datetime import date, timedelta

import pytest

from app.flow import ChatFlow
from core import challenge
from core.progression import apply_xp, next_norm, settle_progression, xp_gain
from database.migrate import run_migrations
from database.store import Store
from tests.flow_fixtures import Clock, FakeSender, NOW

D = NOW.date()  # ladder: Льоша 37, Саша 47


@pytest.mark.parametrize("target,credited,best,expected", [
    (20, True, 30, 26),     # (30+20)//2+1
    (20, True, 20, 21),
    (21, True, 22, 22),     # (43)//2+1 = 22 (floor)
    (20, True, None, 21),   # зараховано без числа -> done = target
    (20, True, 5, 21),      # done не менше за target
    (20, False, 12, 7),
    (20, False, 3, 1),      # обмеження знизу 1
    (25, False, None, 15),
    (25, False, 0, 15),
    (5, False, None, 1),
])
def test_next_norm(target, credited, best, expected):
    assert next_norm(target, credited, best) == expected


def test_xp_gain():
    assert xp_gain(20, True, 20) == 1
    assert xp_gain(20, True, 23) == 7
    assert xp_gain(20, True, None) == 1
    assert xp_gain(20, False, 30) == 0


def test_apply_xp_carries_remainder():
    assert apply_xp(95, 0, 7) == (2, 1, 1)
    assert apply_xp(0, 3, 250) == (50, 5, 2)
    assert apply_xp(10, 0, 5) == (15, 0, 0)


def _store(tmp_path):
    st = Store(str(tmp_path / "p.db"))
    st.init_schema()
    challenge.day_targets(st, D)  # seed default users (non-prod)
    return st


def test_record_attempt_keeps_max(tmp_path):
    st = _store(tmp_path)
    st.record_attempt(D.isoformat(), "lyosha", "pushups", 10)
    st.record_attempt(D.isoformat(), "lyosha", "pushups", 30)
    st.record_attempt(D.isoformat(), "lyosha", "pushups", 12)
    assert st.get_best_attempt(D.isoformat(), "lyosha") == 30
    assert st.get_best_attempt(D.isoformat(), "sasha") is None


def test_settle_progression_end_to_end(tmp_path):
    st = _store(tmp_path)
    st.set_done("lyosha", True, D.isoformat())
    st.record_attempt(D.isoformat(), "lyosha", "pushups", 40)   # 37 + 3
    st.record_attempt(D.isoformat(), "sasha", "pushups", 30)    # не залік
    res = settle_progression(st, D)
    assert res["lyosha"]["next"] == (40 + 37) // 2 + 1 == 39
    assert res["lyosha"]["xp_gain"] == 7
    assert res["sasha"]["next"] == 25 and res["sasha"]["xp_gain"] == 0
    assert settle_progression(st, D) == {}  # ідемпотентно
    nxt = D + timedelta(days=1)
    assert challenge.day_targets(st, nxt) == {"lyosha": 39, "sasha": 25}
    assert challenge.day_targets(st, D)["lyosha"] == 37  # минулий день не змінюється
    # override усе ще має пріоритет
    st.set_state(f"override_lyosha_{nxt.isoformat()}", "50")
    assert challenge.day_targets(st, nxt)["lyosha"] == 50


def test_no_attempt_minus_ten(tmp_path):
    st = _store(tmp_path)
    res = settle_progression(st, D)
    assert res["lyosha"]["next"] == 27 and res["sasha"]["next"] == 37


def test_level_up_restores_lives(tmp_path):
    st = _store(tmp_path)
    challenge.set_user_lives(st, "lyosha", 2, 5)
    st.set_xp("lyosha", 98, 0)
    st.set_done("lyosha", True, D.isoformat())
    st.record_attempt(D.isoformat(), "lyosha", "pushups", 38)  # +1 +2 = 3
    res = settle_progression(st, D)["lyosha"]
    assert (res["xp"], res["level"], res["level_ups"]) == (1, 1, 1)
    u = st.get_user("lyosha")
    assert (u.lives, u.max_lives) == (6, 6)
    assert st.get_xp("lyosha") == (1, 1)


def test_other_exercise_unaffected(tmp_path):
    st = _store(tmp_path)
    challenge.enable_alternating(st, D, challenge.pushups_equivalent(st, D))  # D = віджимання, D+1 = присідання
    sq_before = challenge.day_targets(st, D + timedelta(days=1))
    st.set_done("lyosha", True, D.isoformat())
    settle_progression(st, D)
    assert challenge.day_targets(st, D + timedelta(days=1)) == sq_before
    assert challenge.day_targets(st, D + timedelta(days=2))["lyosha"] == next_norm(37, True, None)


def test_admin_set_target_clears_adaptive(tmp_path):
    st = _store(tmp_path)
    settle_progression(st, D)
    nxt = D + timedelta(days=1)
    challenge.set_target(st, "lyosha", 33, nxt)
    assert challenge.day_targets(st, nxt)["lyosha"] == 33


def test_migration_0011_creates_tables(tmp_path):
    db = str(tmp_path / "m.db")
    run_migrations(db)
    import sqlite3
    con = sqlite3.connect(db)
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"daily_attempts", "user_xp", "next_norms", "xp_log"} <= names
    keys = {r[0] for r in con.execute("SELECT key FROM bot_texts")}
    assert {"level_up", "progress_settled", "status_xp", "admin_dev_accepted", "admin_task_still_working"} <= keys


@pytest.mark.asyncio
async def test_flow_settle_announces_progress(tmp_path):
    st = _store(tmp_path)
    sender = FakeSender()
    flow = ChatFlow(st, None, sender, now=Clock(NOW + timedelta(days=1)))
    st.set_done("lyosha", True, D.isoformat())
    await flow._settle_lives(-1, D)
    text = sender.texts[-1]
    assert "XP" in text and "наступна норма" in text


@pytest.mark.asyncio
async def test_stale_admin_task_reminder_not_cancel(tmp_path):
    st = _store(tmp_path)
    sender = FakeSender()
    flow = ChatFlow(st, None, sender, now=Clock(NOW))
    tid = st.create_admin_task(-1, 5, "Саша", "задача")
    with st._connect() as con:
        con.execute("UPDATE admin_tasks SET created_at = datetime('now', '-25 hours'), status='claimed' WHERE id=?", (tid,))
    await flow.announce_admin_tasks(-1)
    assert any("ще в роботі" in t for t in sender.texts)
    n = len(sender.texts)
    await flow.announce_admin_tasks(-1)  # не частіше ніж раз на 24 год
    assert len(sender.texts) == n
    assert st.get_admin_task(tid)["status"] == "claimed"
