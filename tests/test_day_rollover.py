"""Межа дня челенджу / підсумок дня о DAY_ROLLOVER (за замовчуванням 02:30 Europe/Kyiv): атрибуція днів, таймер, DST."""
from datetime import date, datetime

import pytz

from bot import timer_schedule
from core import config, dayroll
from core.config import TIMEZONE
from database.store import Store
from tests.flow_fixtures import CHAT, make_flow

KYIV = TIMEZONE


def loc(y, mo, d, h, mi, s=0):
    return KYIV.localize(datetime(y, mo, d, h, mi, s))


def test_config_defaults_0230():
    assert (config.DAY_ROLLOVER_HOUR, config.DAY_ROLLOVER_MINUTE) == (2, 30)


def test_env_values_validated(monkeypatch):
    monkeypatch.setenv("DAY_ROLLOVER_HOUR", "99")
    monkeypatch.setenv("DAY_ROLLOVER_MINUTE", "abc")
    assert config._env_int("DAY_ROLLOVER_HOUR", 2, 0, 23) == 2
    assert config._env_int("DAY_ROLLOVER_MINUTE", 30, 0, 59) == 30
    monkeypatch.setenv("DAY_ROLLOVER_HOUR", "3")
    assert config._env_int("DAY_ROLLOVER_HOUR", 2, 0, 23) == 3


def test_vm_config_documents_rollover():
    import os
    from tools.worker_timers import read_env_file
    cfg = read_env_file(os.path.join(os.path.dirname(os.path.dirname(__file__)), "deploy", "vm_config.env"))
    assert cfg["DAY_ROLLOVER_HOUR"] == "2" and cfg["DAY_ROLLOVER_MINUTE"] == "30"
    assert cfg["TRUST_MODE"] == "1"


def test_day_attribution_before_and_after_0230():
    assert dayroll.challenge_date(loc(2026, 10, 5, 0, 0)) == date(2026, 10, 4)
    assert dayroll.challenge_date(loc(2026, 10, 5, 2, 29, 59)) == date(2026, 10, 4)
    assert dayroll.challenge_date(loc(2026, 10, 5, 2, 30, 0)) == date(2026, 10, 5)
    assert dayroll.challenge_date(loc(2026, 10, 5, 2, 31)) == date(2026, 10, 5)
    assert dayroll.challenge_date(loc(2026, 10, 5, 23, 59)) == date(2026, 10, 5)


def test_custom_rollover_and_midnight_legacy():
    assert dayroll.challenge_date(loc(2026, 10, 5, 3, 0), 4, 0) == date(2026, 10, 4)
    assert dayroll.challenge_date(loc(2026, 10, 5, 0, 0), 0, 0) == date(2026, 10, 5)
    assert dayroll.day_start_ts(date(2026, 10, 5)) == "2026-10-05 02:30:00"


def test_dst_autumn_change_2026_10_25():
    # Київ: 25.10.2026 о 04:00 EEST -> 03:00 EET. Межа 02:30 не двозначна.
    assert dayroll.challenge_date(loc(2026, 10, 25, 2, 29)) == date(2026, 10, 24)
    assert dayroll.challenge_date(loc(2026, 10, 25, 2, 31)) == date(2026, 10, 25)
    # UTC-вхід: 00:29 UTC = 03:29 EEST 25.10 -> день 25.10; 23:59 UTC 24.10 = 02:59 EEST 25.10 -> 25.10
    utc = pytz.UTC
    assert dayroll.challenge_date(utc.localize(datetime(2026, 10, 24, 23, 59))) == date(2026, 10, 25)
    assert dayroll.challenge_date(utc.localize(datetime(2026, 10, 24, 23, 20))) == date(2026, 10, 24)  # 02:20 EEST


def test_dst_spring_change_2026_03_29():
    utc = pytz.UTC
    # 28.03 23:59 UTC = 01:59 EET 29.03 -> ще день 28.03
    assert dayroll.challenge_date(utc.localize(datetime(2026, 3, 28, 23, 59))) == date(2026, 3, 28)
    # 29.03 00:31 UTC = 02:31 EET -> день 29.03
    assert dayroll.challenge_date(utc.localize(datetime(2026, 3, 29, 0, 31))) == date(2026, 3, 29)


def test_utc_bounds_follow_kyiv_offset():
    lo, hi = dayroll.day_utc_bounds(date(2026, 10, 24))  # EEST (UTC+3): 02:30 Kyiv = 23:30 UTC
    assert lo == "2026-10-23 23:30:00"
    assert hi == "2026-10-24 23:30:00"
    lo, hi = dayroll.day_utc_bounds(date(2026, 10, 25))  # перехід: 25.10 02:30 EEST = 23:30 UTC, 26.10 02:30 EET = 00:30 UTC
    assert lo == "2026-10-24 23:30:00"
    assert hi == "2026-10-26 00:30:00"


def test_timer_schedule_settle_at_rollover():
    sched = {job: (h, m) for job, h, m in timer_schedule((2, 30))}
    assert sched["settle"] == (2, 30)
    assert sched["night"] == (23, 30)
    assert {job: (h, m) for job, h, m in timer_schedule((4, 5))}["settle"] == (4, 5)


async def test_flow_default_rollover_and_today(tmp_path):
    flow, sender, clock = make_flow(tmp_path, llm=None)
    assert flow.rollover == (2, 30)
    clock.value = loc(2026, 10, 5, 1, 0)
    assert flow._today() == date(2026, 10, 4)
    clock.value = loc(2026, 10, 5, 2, 31)
    assert flow._today() == date(2026, 10, 5)


async def _video_with_claim(flow, rid, caption="200 віджимань"):
    flow.store.set_state(f"video_caption:{rid}", caption)
    flow.store.create_video_check(request_id=rid, chat_id=CHAT, message_id=1, user_slug="lyosha",
                                  duration_s=30.0, exercise="pushup", reps=4, confidence=0.9,
                                  valid=True, status="done")
    return await flow.handle_video_callback(rid)


async def test_video_at_0100_counts_for_previous_day(tmp_path):
    flow, sender, clock = make_flow(tmp_path, llm=None, trust_mode=True)
    clock.value = loc(2026, 10, 5, 1, 0)
    await _video_with_claim(flow, "r-night")
    assert flow.store.get_day_progress("2026-10-04")["lyosha"] is True
    assert flow.store.get_day_progress("2026-10-05")["lyosha"] is False
    assert flow.store.get_best_attempt("2026-10-04", "lyosha") == 200


async def test_video_at_0231_counts_for_new_day(tmp_path):
    flow, sender, clock = make_flow(tmp_path, llm=None, trust_mode=True)
    clock.value = loc(2026, 10, 5, 2, 31)
    await _video_with_claim(flow, "r-morning")
    assert flow.store.get_day_progress("2026-10-05")["lyosha"] is True
    assert flow.store.get_day_progress("2026-10-04")["lyosha"] is False


async def test_settle_at_0230_closes_previous_day_and_not_at_midnight(tmp_path):
    flow, sender, clock = make_flow(tmp_path, llm=None, trust_mode=True)
    # о 00:05 «сьогодні» ще 04.10 -> settle розраховує 03.10, а не щойно завершений 04.10
    clock.value = loc(2026, 10, 5, 0, 5)
    await flow.process(CHAT, trigger="timer", job="settle")
    assert flow.store.get_state("lives_settled_through", "") == "2026-10-03"
    # о 02:30 «сьогодні» вже 05.10 -> settle розраховує 04.10
    clock.value = loc(2026, 10, 5, 2, 30)
    await flow.process(CHAT, trigger="timer", job="settle")
    assert flow.store.get_state("lives_settled_through", "") == "2026-10-04"
    assert flow.store.get_state("progression_settled_through", "") == "2026-10-04"


def test_has_video_check_today_uses_challenge_day(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    store.init_schema()
    store.create_video_check(request_id="a", chat_id=CHAT, message_id=1, user_slug="lyosha")
    with store._connect() as con:
        # 01:00 Kyiv (EEST) 05.10 = 22:00 UTC 04.10 -> день челенджу 04.10
        con.execute("UPDATE video_checks SET created_at = '2026-10-04 22:00:00' WHERE request_id = 'a'")
    assert store.has_video_check_today("lyosha", "2026-10-04") is True
    assert store.has_video_check_today("lyosha", "2026-10-05") is False
