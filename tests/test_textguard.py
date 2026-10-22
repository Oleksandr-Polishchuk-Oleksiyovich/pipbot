import pytest

from core.textguard import looks_mangled
from database.store import Store


def test_looks_mangled():
    assert looks_mangled("???????? ?????? (??????)")
    assert looks_mangled("abc \ufffd def")
    assert not looks_mangled("Що??? Серйозно?")
    assert not looks_mangled("Лілуш ві Британія наказує")


def test_store_refuses_mangled(tmp_path):
    st = Store(str(tmp_path / "g.db"))
    st.init_schema()
    with pytest.raises(ValueError):
        st.create_admin_task(1, 2, "Саша", "???????? ?????????????")
    with pytest.raises(ValueError):
        st.set_bot_text("success", "?????? ????")
    assert st.create_admin_task(1, 2, "Саша", "Нова норма") == 1
