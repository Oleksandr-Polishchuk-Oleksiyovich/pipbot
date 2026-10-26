"""Єдиний детермінований статус: усі характеристики в /status, у промпті коуча, ранковому повідомленні."""
from datetime import date

from agents.coach import CoachAgent
from core import challenge
from core.leave import set_leave
from database.store import Store

D = date(2026, 10, 26)


def _store(tmp_path):
    st = Store(str(tmp_path / "t.db"))
    challenge.ensure_default_users(st)
    return st


def _setup(tmp_path):
    st = _store(tmp_path)
    users = {u.slug: u for u in st.list_users(status="active")}
    assert users, "очікуються дефолтні активні учасники"
    return st, list(users)


def test_status_line_has_all_characteristics(tmp_path):
    st, slugs = _setup(tmp_path)
    a, b = slugs[0], slugs[1]
    st.set_xp(a, 42, 3)
    challenge.set_user_lives(st, a, 3, 4)
    st.set_attempt(D.isoformat(), a, "pushups", 7)
    set_leave(st, b, "sick", "2026-10-26", None, set_by="owner")
    board = challenge.status_board(st, D)
    la = [l for l in board.splitlines() if f"норма {challenge.day_targets(st, D)[a]}" in l][0]
    for part in ("❤️ 3/4", "⭐ 42/100 XP", "🆙 рів. 3", "🔥 серія", "найкраща спроба: 7"):
        assert part in la, (part, la)
    lb = [l for l in board.splitlines() if "🤒" in l]
    assert len(lb) == 1 and "на лікарняному з 26.10 (безстроково)" in lb[0]
    assert "⭐" in lb[0] and "🆙" in lb[0] and "🔥" in lb[0]
    assert "❌ ще не здав" not in lb[0]


def test_leave_range_and_vacation_text(tmp_path):
    st, slugs = _setup(tmp_path)
    set_leave(st, slugs[0], "vacation", "2026-10-26", "2026-11-02", set_by="x")
    board = challenge.status_board(st, D)
    assert "🏖 у відпустці з 26.10 до 02.11" in board


def test_streak_counts_days_and_skips_leave(tmp_path):
    st, slugs = _setup(tmp_path)
    s = slugs[0]
    user = st.get_user(s)
    for d in ("2026-10-23", "2026-10-25"):
        st.set_done(s, True, d)
    # 24-го — лікарняний (серію не рве), 26-го ще не закрито
    set_leave(st, s, "sick", "2026-10-24", "2026-10-24", set_by="x")
    assert challenge.streak_days(st, user, D) == 2
    st.set_done(s, True, "2026-10-26")
    assert challenge.streak_days(st, user, D) == 3
    # пропуск без лікарняного рве серію
    st.set_done(s, False, "2026-10-25")
    assert challenge.streak_days(st, user, D) == 1


def test_coach_prompt_contains_full_board_and_mandate(tmp_path):
    st, slugs = _setup(tmp_path)
    set_leave(st, slugs[0], "sick", "2026-10-26", None, set_by="owner")
    prompt = CoachAgent(None, st).build_system_prompt(D)
    board = challenge.status_board(st, D)
    assert board in prompt
    assert "ОБОВ'ЯЗКОВЕ ПРАВИЛО ДЛЯ СТАТУСУ" in prompt
    # мандат стоїть ПІСЛЯ динамічних правил (пріоритет над «рядком статусу: лише життя»)
    st.save_prompt("coach_dynamic", "Рядок статусу: лише життя", "test")
    prompt2 = CoachAgent(None, st).build_system_prompt(D)
    assert prompt2.index("ОБОВ'ЯЗКОВЕ ПРАВИЛО ДЛЯ СТАТУСУ") > prompt2.index("Рядок статусу: лише життя")


def test_status_text_uses_same_lines(tmp_path):
    st, slugs = _setup(tmp_path)
    assert challenge.status_board(st, D) in challenge.status_text(st, D)


def test_morning_message_includes_all_characteristics(tmp_path):
    import asyncio
    from tests.test_flow_offline import CHAT, make_flow
    flow, sender, clock = make_flow(tmp_path, llm=None)
    today = flow._today()
    slug = flow.store.list_users(status="active")[0].slug
    set_leave(flow.store, slug, "sick", today.isoformat(), None, set_by="owner")
    asyncio.run(flow.process(CHAT, trigger="timer", job="morning"))
    msg = sender.texts[-1]
    assert "🤒 на лікарняному" in msg and "⭐" in msg and "🆙" in msg and "🔥" in msg


def test_diag_snapshot_exposes_status_board(tmp_path):
    from gateway.ops_api import snapshot
    st, slugs = _setup(tmp_path)
    set_leave(st, slugs[0], "sick", "2026-10-26", None, set_by="owner")
    out = snapshot(st, 3, "2026-10-26", 0)
    assert "status_board_error" not in out, out.get("status_board_error")
    assert any("🤒" in l and "⭐" in l and "🆙" in l for l in out["status_board"])
    assert out["leaves"][0]["set_by"] == "owner"
