import pytest
from agents import admin


@pytest.mark.parametrize("key", ["schedule_mode", "alt_start_date", "override_lyosha_2026-10-21", "offset_sasha", "lives_lyosha", "text:greeting"])
def test_set_counter_rejects_schedule_keys(key):
    assert key.startswith(admin.SCHEDULE_KEY_PREFIXES)


def test_set_counter_allows_plain_counter():
    assert not "bonus_points_lyosha".startswith(admin.SCHEDULE_KEY_PREFIXES)
