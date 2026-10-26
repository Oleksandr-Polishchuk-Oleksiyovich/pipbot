"""
Тести для системи «вакейшенів та ілнесів» (core/leave.py, database/store.py, core/challenge.py,
core/progression.py, agents/admin.py, app/flow.py).
"""
import sqlite3
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from agents.admin import AdminAgent
from app.flow import ChatFlow
from core import challenge, progression
from core.leave import describe, end_leave, leave_on, normalize_kind, set_leave
from database.migrate import run_migrations
from database.store import Store, User
from tests.flow_fixtures import Clock, FakeSender, NOW

D = date(2026, 10, 26)


def _store(tmp_path) -> Store:
    db = str(tmp_path / "test_leave.db")
    st = Store(db)
    st.init_schema()
    st.upsert_user(User(slug="lyosha", telegram_id=1, telegram_name="Lyosha", username="lyosha",
                        display_name="Льоша", emoji="🦁", status="active", lives=5, max_lives=5, joined_date="2026-08-31"))
    st.upsert_user(User(slug="sasha", telegram_id=2, telegram_name="Sasha", username="sasha",
                        display_name="Саша", emoji="🐻", status="active", lives=5, max_lives=5, joined_date="2026-08-31"))
    challenge.set_target(st, "lyosha", 30, D)
    challenge.set_target(st, "sasha", 30, D)
    return st


# 1. normalize_kind
def test_normalize_kind():
    for syn in ("sick", "illness", "ill", "хвороба", "лікарняний", "болен", "захворів", "захворіла", "хворіє"):
        assert normalize_kind(syn) == "sick"
        assert normalize_kind(syn.upper()) == "sick"

    for syn in ("vacation", "відпустка", "vacay", "відпустці", "отпуск", "отдых"):
        assert normalize_kind(syn) == "vacation"
        assert normalize_kind(syn.upper()) == "vacation"

    with pytest.raises(ValueError):
        normalize_kind("unknown_kind")


# 2. CRUD та ідемпотентність
def test_leave_crud_and_idempotency(tmp_path):
    st = _store(tmp_path)

    # Невідомий учасник
    with pytest.raises(ValueError, match="unknown participant"):
        set_leave(st, "unknown_user", "sick", "2026-10-26")

    # end_date < start_date
    with pytest.raises(ValueError, match="cannot be before start_date"):
        set_leave(st, "lyosha", "sick", "2026-10-26", "2026-10-20")

    # Створення
    l1 = set_leave(st, "lyosha", "sick", "2026-10-26", "2026-10-30", set_by="admin", reason="грип")
    assert l1["user_slug"] == "lyosha"
    assert l1["kind"] == "sick"
    assert l1["start_date"] == "2026-10-26"
    assert l1["end_date"] == "2026-10-30"
    assert l1["set_by"] == "admin"
    assert l1["reason"] == "грип"
    assert l1["cancelled"] == 0

    # Ідемпотентність: повторний виклик повертає той самий id без нового рядка
    l1_again = set_leave(st, "lyosha", "sick", "2026-10-26", "2026-10-30", set_by="other")
    assert l1_again["id"] == l1["id"]

    leaves = st.list_leaves(slug="lyosha")
    assert len(leaves) == 1


# 3. Активність за датами
def test_leave_active_by_dates(tmp_path):
    st = _store(tmp_path)
    set_leave(st, "lyosha", "sick", "2026-10-10", "2026-10-15")

    # До початку
    assert leave_on(st, "lyosha", "2026-10-09") is None
    # Межі включно
    assert leave_on(st, "lyosha", "2026-10-10") is not None
    assert leave_on(st, "lyosha", "2026-10-12") is not None
    assert leave_on(st, "lyosha", "2026-10-15") is not None
    # Після кінця
    assert leave_on(st, "lyosha", "2026-10-16") is None

    # Безстрокова (end_date=None)
    set_leave(st, "sasha", "vacation", "2026-10-20", None)
    assert leave_on(st, "sasha", "2026-10-19") is None
    assert leave_on(st, "sasha", "2026-10-20") is not None
    assert leave_on(st, "sasha", "2026-12-31") is not None


# 4. end_leave семантика
def test_end_leave_semantics(tmp_path):
    st = _store(tmp_path)
    # Відкрита відпустка з 2026-10-20
    set_leave(st, "lyosha", "vacation", "2026-10-20", None)
    assert leave_on(st, "lyosha", "2026-10-25") is not None

    # Завершуємо з 2026-10-25 (тобто 25-го вже активний, відпустка до 24-го)
    res = end_leave(st, "lyosha", from_day="2026-10-25", ended_by="coach")
    assert res["end_date"] == "2026-10-24"
    assert res["cancelled"] == 0
    assert leave_on(st, "lyosha", "2026-10-24") is not None
    assert leave_on(st, "lyosha", "2026-10-25") is None

    # Скасування, якщо from_day <= start_date (end_date < start_date)
    set_leave(st, "sasha", "sick", "2026-10-26", "2026-10-30")
    res_sasha = end_leave(st, "sasha", from_day="2026-10-26", ended_by="admin")
    assert res_sasha["cancelled"] == 1
    assert leave_on(st, "sasha", "2026-10-26") is None


# 5. Перетин відпусток/лікарняних
def test_overlapping_leaves(tmp_path):
    st = _store(tmp_path)
    # Стара відпустка: 10.10 - 20.10
    l_old = set_leave(st, "lyosha", "vacation", "2026-10-10", "2026-10-20")

    # Новий лікарняний: з 15.10
    l_new = set_leave(st, "lyosha", "sick", "2026-10-15", "2026-10-25", set_by="doctor")
    old_updated = st.get_leave(l_old["id"])
    assert old_updated["end_date"] == "2026-10-14"
    assert old_updated["cancelled"] == 0

    assert leave_on(st, "lyosha", "2026-10-14")["kind"] == "vacation"
    assert leave_on(st, "lyosha", "2026-10-15")["kind"] == "sick"

    # Якщо нова починається раніше або одночасно зі старою — стара скасовується
    l_future = set_leave(st, "sasha", "vacation", "2026-11-01", "2026-11-10")
    set_leave(st, "sasha", "sick", "2026-10-28", "2026-11-15")
    fut_updated = st.get_leave(l_future["id"])
    assert fut_updated["cancelled"] == 1


# 6. settle_missed_day (життя не знімаються)
def test_settle_missed_day_preserves_lives(tmp_path):
    st = _store(tmp_path)
    set_leave(st, "lyosha", "sick", D.isoformat(), D.isoformat())

    # Ніхто не робив вправу
    changed = challenge.settle_missed_day(st, D)

    # Льоша на лікарняному — життя збережено (5)
    assert "lyosha" not in changed
    assert st.get_user("lyosha").lives == 5

    # Саша без відпустки — втрачає життя (4)
    assert changed.get("sasha") == 4
    assert st.get_user("sasha").lives == 4


# 7. settle_progression: пропуск, якщо не зараховано; обробка, якщо виконав сам
def test_settle_progression_with_leave(tmp_path):
    st = _store(tmp_path)
    set_leave(st, "lyosha", "vacation", D.isoformat(), D.isoformat())
    st.set_xp("lyosha", 50, 1)

    # 1. Льоша не виконав норму (credited False)
    res = progression.settle_progression(st, D)
    assert "lyosha" not in res
    assert st.get_xp("lyosha") == (50, 1)
    # Норма не спадає
    assert st.get_next_norm("lyosha", challenge.day_exercise(st, D)) is None

    # 2. Якщо учасник виконав норму під час відпустки — звичайна обробка
    D2 = D + timedelta(days=1)
    challenge.set_target(st, "lyosha", 30, D2)
    challenge.set_target(st, "sasha", 30, D2)
    set_leave(st, "lyosha", "vacation", D2.isoformat(), D2.isoformat())
    st.set_done("lyosha", True, D2.isoformat())
    st.record_attempt(D2.isoformat(), "lyosha", challenge.day_exercise(st, D2), 35)

    res2 = progression.settle_progression(st, D2)
    assert "lyosha" in res2
    assert res2["lyosha"]["credited"] is True
    # XP нараховано
    xp, _ = st.get_xp("lyosha")
    assert xp > 50


# 8. missing_participants та status_text / status_snapshot
def test_missing_participants_and_status_text(tmp_path):
    st = _store(tmp_path)
    set_leave(st, "lyosha", "sick", D.isoformat(), "2026-10-30", set_by="doctor")

    # missing_participants не включає Льошу
    missing = challenge.missing_participants(st, D)
    assert "lyosha" not in missing
    assert "sasha" in missing

    # status_snapshot
    snap = challenge.status_snapshot(st, D)
    assert snap["participants"]["lyosha"]["leave"] == {
        "kind": "sick",
        "until": "2026-10-30",
        "set_by": "doctor",
    }
    assert snap["participants"]["sasha"]["leave"] is None

    # status_text
    txt = challenge.status_text(st, D)
    assert "🤒 на лікарняному (до 30.10)" in txt
    assert "НЕ виконано ❌" in txt  # для Саші

    # describe
    assert describe(snap["participants"]["lyosha"]["leave"]) == "на лікарняному до 30.10"
    assert describe({"kind": "vacation", "end_date": None}) == "у відпустці"


# 9. app/flow.py: _settle_lives рядки та morning norms_lines
@pytest.mark.asyncio
async def test_flow_settle_lives_and_morning_with_leave(tmp_path):
    st = _store(tmp_path)
    sender = FakeSender()
    today_dt = datetime.combine(D, datetime.min.time()).replace(hour=8, minute=0)
    flow = ChatFlow(st, None, sender, now=Clock(today_dt))

    set_leave(st, "lyosha", "sick", D.isoformat(), D.isoformat())

    # _settle_lives
    await flow._settle_lives(chat_id=123, day=D)
    assert len(sender.texts) == 1
    settle_msg = sender.texts[-1]
    assert "🤒 Льоша на лікарняному — життя й норма без змін" in settle_msg

    # morning timer
    sender.texts.clear()
    await flow._handle_timer(123, "morning")
    assert len(sender.texts) == 1
    morning_msg = sender.texts[-1]
    assert "🤒 на лікарняному" in morning_msg


# 10. AdminAgent tools: set_leave, end_leave, list_leaves
def test_admin_tools_leave(tmp_path):
    st = _store(tmp_path)
    admin = AdminAgent(None, st, editor=None)
    actions = []
    tools = {fn.__name__: fn for fn in admin._tools(D, "Адмін", actions)}

    # set_leave
    msg = tools["set_leave"]("Льоша", "відпустка", "2026-10-26", "2026-10-29", reason="відпочинок")
    assert "Встановлено статус для lyosha" in msg
    assert "у відпустці до 29.10" in msg
    assert any("set_leave(lyosha, vacation" in a for a in actions)

    # list_leaves
    l_json = tools["list_leaves"]()
    assert "lyosha" in l_json
    assert "vacation" in l_json

    # end_leave
    msg_end = tools["end_leave"]("lyosha", "2026-10-28")
    assert "Відпустку/лікарняний для lyosha завершено" in msg_end
    assert any("end_leave(lyosha, 2026-10-28)" in a for a in actions)


# 11. Міграція на чистій БД
def test_migration_0012_creates_leaves_table(tmp_path):
    db_file = str(tmp_path / "clean_mig.db")
    run_migrations(db_file)
    con = sqlite3.connect(db_file)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "leaves" in tables

    # Перевірка колонок
    cols = {r[1] for r in con.execute("PRAGMA table_info(leaves)")}
    assert {"id", "user_slug", "kind", "start_date", "end_date", "set_by", "reason", "created_at", "ended_at", "ended_by", "cancelled"} <= cols
    con.close()
