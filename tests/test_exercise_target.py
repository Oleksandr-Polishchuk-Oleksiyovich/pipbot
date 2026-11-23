from datetime import timedelta

from core import challenge
from database.store import Store
from tests.flow_fixtures import NOW

D = NOW.date()


def _store(tmp_path):
    st = Store(str(tmp_path / "e.db"))
    st.init_schema()
    challenge.day_targets(st, D)
    return st


def _alt(st, today_ex="pushups"):
    challenge.enable_alternating(st, D, {"lyosha": 20, "sasha": 30}, 1.5, today_ex)


def test_squat_target_on_pushup_day_applies_to_next_squat_day_only_for_user(tmp_path):
    st = _store(tmp_path)
    _alt(st, "pushups")
    assert challenge.day_exercise(st, D) == "pushups"
    before_today = challenge.day_targets(st, D)
    before_tom = challenge.day_targets(st, D + timedelta(days=1))
    slug = challenge.set_exercise_target(st, "lyosha", "squats", 40, D)
    assert slug == "lyosha"
    assert challenge.day_targets(st, D) == before_today  # pushups untouched today
    tom = challenge.day_targets(st, D + timedelta(days=1))
    assert challenge.day_exercise(st, D + timedelta(days=1)) == "squats"
    assert tom["lyosha"] == 40
    assert tom["sasha"] == before_tom["sasha"]  # other participant untouched
    # next pushup day keeps normal formula
    assert challenge.day_targets(st, D + timedelta(days=2))["lyosha"] == 21
    assert challenge.exercise_target_preview(st, "lyosha", "squats", D) == ((D + timedelta(days=1)).isoformat(), 40)


def test_squat_target_on_squat_day_applies_today(tmp_path):
    st = _store(tmp_path)
    _alt(st, "squats")
    assert challenge.day_exercise(st, D) == "squats"
    challenge.set_exercise_target(st, "sasha", "squats", 55, D)
    assert challenge.day_targets(st, D)["sasha"] == 55


def test_squat_target_in_ladder_waits_for_alternate(tmp_path):
    st = _store(tmp_path)
    before = challenge.day_targets(st, D)
    challenge.set_exercise_target(st, "lyosha", "squats", 33, D)
    assert challenge.day_targets(st, D) == before
    assert challenge.exercise_target_preview(st, "lyosha", "squats", D) is None
    challenge.enable_alternating(st, D, challenge.pushups_equivalent(st, D), 1.5, "squats")
    assert challenge.day_targets(st, D)["lyosha"] == 33


def test_set_exercise_target_rejects_unknown(tmp_path):
    st = _store(tmp_path)
    import pytest
    with pytest.raises(ValueError):
        challenge.set_exercise_target(st, "lyosha", "burpees", 10, D)
