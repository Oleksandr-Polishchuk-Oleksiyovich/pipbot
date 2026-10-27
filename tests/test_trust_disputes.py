"""
Тести оспорювання підрахунку (TRUST_MODE):
1. Unit-тести для core.claims.is_dispute та core.claims.parse_dispute_reps.
2. Оспорювання з числом, більшим за CV (норма виконана): оновлення спроби, set_done=True, м'яка відповідь.
3. Оспорювання зі зниженням числа (нижче CV / норми): спроба оновлюється, день не зараховано.
4. Оспорювання через реплай із цитатою («[У відповідь на Спорт Зануда: «Побачив 4 віджимання з 37»] 45»).
5. Оспорювання без числа: бот запитує один раз, друге повідомлення без числа не дублює запитання,
   наступне повідомлення з числом зараховується.
6. Оспорювання для відео, зарахованого раніше того ж дня (після відповіді бота).
7. Оспорювання від користувача без завершеного відео за сьогодні не перехоплюється.
8. TRUST_MODE вимкнено (trust_mode=False) — повідомлення не перехоплюються як оспорювання.
9. Прогресія: settle_progression після оспорювання (XP, наступна норма, ідемпотентність розрахунку).
"""
from datetime import date
import pytest

from core.claims import is_dispute, parse_dispute_reps, split_reply_context
from core.progression import settle_progression
from database.store import ChatMessage
from tests.flow_fixtures import CHAT, DAD, SON, make_flow


def _create_done_video_check(
    flow,
    req_id: str,
    reps: int = 4,
    user_slug: str = "lyosha",
    msg_id: int = 500,
    utc_time: str = "2026-10-04 08:00:00",
):
    flow.store.create_video_check(
        request_id=req_id,
        chat_id=CHAT,
        message_id=msg_id,
        user_slug=user_slug,
        duration_s=30.0,
        exercise="pushup",
        reps=reps,
        confidence=0.9,
        valid=True,
        status="done",
    )
    with flow.store._connect() as con:
        con.execute(
            "UPDATE video_checks SET created_at = ? WHERE request_id = ?",
            (utc_time, req_id),
        )


def test_claims_parse_dispute_reps_unit():
    """Перевірка парсингу виправленої кількості повторів із тексту оспорювання."""
    assert parse_dispute_reps("не 4 а 40") == 40
    assert parse_dispute_reps("40, а не 4") == 40
    assert parse_dispute_reps("ты ошибся, я сделал 30") == 30
    assert parse_dispute_reps("зарахуй 42") == 42
    assert parse_dispute_reps("я зробив більше") is None
    assert parse_dispute_reps("ти неправильно порахував") is None
    assert parse_dispute_reps("дякую") is None
    assert parse_dispute_reps(None) is None
    assert parse_dispute_reps("") is None


def test_claims_is_dispute_unit():
    """Перевірка детекції оспорювання підрахунку (укр, рос, англ, цитування)."""
    assert is_dispute("не 4 а 40") is True
    assert is_dispute("40, а не 4") is True
    assert is_dispute("ты ошибся, я сделал 30") is True
    assert is_dispute("дякую") is False
    assert is_dispute("bot is wrong") is True
    assert is_dispute("я зробив більше") is True
    assert is_dispute("ти неправильно порахував") is True
    assert is_dispute("бот помилився") is True
    assert is_dispute("зарахуй") is True

    reply_text = "[У відповідь на Спорт Зануда: «Побачив 4 віджимання з 37»] 45"
    assert is_dispute(reply_text, reply_to_bot_count=False) is False
    assert is_dispute(reply_text, reply_to_bot_count=True) is True
    assert is_dispute(None) is False
    assert is_dispute("") is False


async def test_dispute_with_number_higher_than_cv(tmp_path):
    """
    CV нарахував 4 з норми 37. Користувач оспорює: 'не 4 а 40'.
    Бот вірить користувачу: спроба = 40, день закрито, відповідь дружня без звинувачень.
    """
    flow, sender, clock = make_flow(tmp_path, llm=None, trust_mode=True)
    rid = "req-dispute-higher"
    _create_done_video_check(flow, rid, reps=4)

    initial_reply = await flow.handle_video_callback(rid)
    assert flow.store.get_day_progress("2026-10-04")["lyosha"] is False
    assert flow.store.get_best_attempt("2026-10-04", "lyosha") == 4
    sender.texts.clear()

    flow.record_user_message(CHAT, SON, "не 4 а 40")
    await flow.process(CHAT, trigger="push")

    assert flow.store.get_best_attempt("2026-10-04", "lyosha") == 40
    assert flow.store.get_day_progress("2026-10-04")["lyosha"] is True
    assert len(sender.texts) == 1

    reply = sender.texts[0]
    assert "Вибач, лічильник міг помилитись" in reply
    assert "40" in reply
    assert "віджимань" in reply
    assert "Сьогоднішню норму зараховано" in reply
    assert "не зараховано" not in reply


async def test_dispute_with_number_lower_not_met(tmp_path):
    """
    Зниження результату користувачем: заявлено було 50, але користувач виправляє на 20.
    Норма 37 -> день не зараховано, спроба стає 20, без фрази 'не зараховано'.
    """
    flow, sender, clock = make_flow(tmp_path, llm=None, trust_mode=True)
    rid = "req-dispute-lower"
    flow.store.set_state(f"video_caption:{rid}", "50 віджимань")
    _create_done_video_check(flow, rid, reps=50)

    await flow.handle_video_callback(rid)
    assert flow.store.get_day_progress("2026-10-04")["lyosha"] is True
    assert flow.store.get_best_attempt("2026-10-04", "lyosha") == 50
    sender.texts.clear()

    flow.record_user_message(CHAT, SON, "не 50 а 20")
    await flow.process(CHAT, trigger="push")

    assert flow.store.get_best_attempt("2026-10-04", "lyosha") == 20
    assert flow.store.get_day_progress("2026-10-04")["lyosha"] is False
    assert len(sender.texts) == 1

    reply = sender.texts[0]
    assert "Вибач, лічильник міг помилитись" in reply
    assert "20" in reply
    assert "не зараховано" not in reply


async def test_dispute_via_reply_quote(tmp_path):
    """
    Оспорювання у форматі Telegram-відповіді на повідомлення бота «Побачив 4 віджимання з 37».
    Користувач відповідає '45'.
    """
    flow, sender, clock = make_flow(tmp_path, llm=None, trust_mode=True)
    rid = "req-dispute-quote"
    _create_done_video_check(flow, rid, reps=4)

    await flow.handle_video_callback(rid)
    sender.texts.clear()

    reply_text = "[У відповідь на Спорт Зануда: «Побачив 4 віджимання з 37»] 45"
    flow.record_user_message(CHAT, SON, reply_text)
    await flow.process(CHAT, trigger="push")

    assert flow.store.get_best_attempt("2026-10-04", "lyosha") == 45
    assert flow.store.get_day_progress("2026-10-04")["lyosha"] is True
    assert len(sender.texts) == 1

    reply = sender.texts[0]
    assert "Вибач, лічильник міг помилитись" in reply
    assert "45" in reply
    assert "не зараховано" not in reply


async def test_dispute_without_number_asks_once_and_credits_followup(tmp_path):
    """
    Оспорювання без числа:
    1. 'ти неправильно порахував' -> бот запитує один раз «Скільки в тебе вийшло? Повірю твоєму слову.»
    2. 'бот помилився' -> повторного запитання немає.
    3. '45' -> зараховується виправлене число.
    """
    flow, sender, clock = make_flow(tmp_path, llm=None, trust_mode=True)
    rid = "req-dispute-no-num"
    _create_done_video_check(flow, rid, reps=4)

    await flow.handle_video_callback(rid)
    sender.texts.clear()

    # 1. Перше повідомлення без числа
    flow.record_user_message(CHAT, SON, "ти неправильно порахував")
    await flow.process(CHAT, trigger="push")

    assert len(sender.texts) == 1
    assert "Скільки в тебе вийшло? Повірю твоєму слову." in sender.texts[0]

    # 2. Друге повідомлення без числа не дублює запитання
    flow.record_user_message(CHAT, SON, "бот помилився")
    await flow.process(CHAT, trigger="push")

    assert len(sender.texts) == 1

    # 3. Наступне повідомлення з числом зараховується
    flow.record_user_message(CHAT, SON, "45")
    await flow.process(CHAT, trigger="push")

    assert len(sender.texts) == 2
    assert "Вибач, лічильник міг помилитись" in sender.texts[1]
    assert "45" in sender.texts[1]
    assert flow.store.get_best_attempt("2026-10-04", "lyosha") == 45
    assert flow.store.get_day_progress("2026-10-04")["lyosha"] is True


async def test_dispute_for_earlier_video_same_day(tmp_path):
    """
    Відео зараховано о 11:00 ранку. О 16:30 того ж дня користувач оспорює підрахунок.
    """
    flow, sender, clock = make_flow(tmp_path, llm=None, trust_mode=True)
    rid = "req-dispute-earlier"
    _create_done_video_check(flow, rid, reps=4, utc_time="2026-10-04 08:00:00")

    await flow.handle_video_callback(rid)
    assert flow.store.get_best_attempt("2026-10-04", "lyosha") == 4

    # Переводимо годинник вперед на 16:30
    clock.at(16, 30, 0)
    sender.texts.clear()

    flow.record_user_message(CHAT, SON, "зарахуй 42")
    await flow.process(CHAT, trigger="push")

    assert flow.store.get_best_attempt("2026-10-04", "lyosha") == 42
    assert flow.store.get_day_progress("2026-10-04")["lyosha"] is True
    assert len(sender.texts) == 1
    assert "42" in sender.texts[0]


async def test_dispute_without_video_not_intercepted(tmp_path):
    """
    Повідомлення оспорювання від учасника, у якого немає завершеного відео за сьогодні:
    - Не перехоплюється _handle_disputes (залишається у списку remaining).
    - При flow.process не викликає помилок і не надсилає відповіді вибачення.
    """
    flow, sender, clock = make_flow(tmp_path, llm=None, trust_mode=True)

    msg = ChatMessage(
        id=1,
        user_name=DAD,
        role="user",
        text="ти неправильно порахував",
        created_at="2026-10-04 11:15:00+03:00",
    )
    remaining = await flow._handle_disputes(CHAT, [msg])
    assert len(remaining) == 1
    assert remaining[0] is msg

    flow.record_user_message(CHAT, DAD, "ти неправильно порахував")
    await flow.process(CHAT, trigger="push")
    assert not any("Вибач, лічильник" in t for t in sender.texts)


async def test_trust_mode_off_does_not_intercept(tmp_path):
    """
    Якщо trust_mode=False, _handle_pending не запускає _handle_disputes:
    повідомлення оспорювання не перехоплюється і текст вибачення не надсилається.
    """
    flow, sender, clock = make_flow(tmp_path, llm=None, trust_mode=False)
    rid = "req-dispute-trust-off"
    _create_done_video_check(flow, rid, reps=4)

    await flow.handle_video_callback(rid)
    sender.texts.clear()

    flow.record_user_message(CHAT, SON, "ти неправильно порахував")
    await flow.process(CHAT, trigger="push")

    assert not any("Вибач, лічильник" in t for t in sender.texts)


async def test_progression_settle_after_dispute(tmp_path):
    """
    Прогресія:
    - CV зарахував 4, таргет 37.
    - Оспорювання 'не 4 а 40' -> best=40.
    - settle_progression розраховує xp_gain = 1 + 2*(40 - 37) = 7.
    - next target = (40 + 37)//2 + 1 = 39.
    - Повторне оспорювання з тим самим числом та повторний settle ідемпотентні (progression_settled_through).
    """
    flow, sender, clock = make_flow(tmp_path, llm=None, trust_mode=True)
    rid = "req-dispute-progression"
    _create_done_video_check(flow, rid, reps=4)

    await flow.handle_video_callback(rid)

    flow.record_user_message(CHAT, SON, "не 4 а 40")
    await flow.process(CHAT, trigger="push")

    res = settle_progression(flow.store, date(2026, 10, 4))
    assert res["lyosha"]["xp_gain"] == 7
    assert res["lyosha"]["next"] == 39
    assert res["lyosha"]["best"] == 40
    assert res["lyosha"]["credited"] is True

    xp, lvl = flow.store.get_xp("lyosha")
    assert xp == 7

    # Повторне оспорювання з тим самим числом
    flow.record_user_message(CHAT, SON, "не 4 а 40")
    await flow.process(CHAT, trigger="push")

    # Повторний settle повертає порожній dict, XP не подвоюються
    res2 = settle_progression(flow.store, date(2026, 10, 4))
    assert res2 == {}
    xp2, lvl2 = flow.store.get_xp("lyosha")
    assert xp2 == 7
    assert lvl2 == lvl


# --- Діма 26.10: «Нє нє, 50 там і не пахне, я 23 зробив» зараховано як 50 (парсер брав максимум) ---

DIMA_DISPUTE = "Нє нє, 50 там і не пахне, я 23 зробив"


def test_parse_dispute_reps_dima_case():
    assert parse_dispute_reps(DIMA_DISPUTE) == 23
    assert parse_dispute_reps(DIMA_DISPUTE, exclude=50) == 23
    # число, що збігається з CV, відкидається, якщо є інше; нижче число так само валідне
    assert parse_dispute_reps("ні, там 50 і 23 віджимання", exclude=50) == 23
    assert parse_dispute_reps("я зробив 23", exclude=50) == 23
    # єдине число навіть рівне CV лишається (людина підтверджує)
    assert parse_dispute_reps("я зробив 50", exclude=50) == 50
    # старі поведінки
    assert parse_dispute_reps("не 4 а 40") == 40
    assert parse_dispute_reps("насправді 60 а не 50", exclude=50) == 60
    assert is_dispute(DIMA_DISPUTE) is True


async def _dima_flow(tmp_path, cv_ex="squat", cv_reps=50, flags=None):
    flow, sender, clock = make_flow(tmp_path, llm=None, trust_mode=True)
    rid = "req-dima"
    flow.store.create_video_check(
        request_id=rid, chat_id=CHAT, message_id=501, user_slug="lyosha", duration_s=60.0,
        exercise=cv_ex, reps=cv_reps, confidence=0.75, valid=True, status="done", flags=flags or [],
    )
    with flow.store._connect() as con:
        con.execute("UPDATE video_checks SET created_at = ? WHERE request_id = ?", ("2026-10-04 08:00:00", rid))
    return flow, sender, rid


async def test_dispute_sets_stored_attempt_to_exact_lower_number(tmp_path):
    flow, sender, rid = await _dima_flow(tmp_path)
    await flow.handle_video_callback(rid)
    # без підпису: бот зарахував CV-шні 50 (вправа дня — віджимання)
    assert flow.store.get_best_attempt("2026-10-04", "lyosha") == 50
    assert flow.store.get_day_progress("2026-10-04")["lyosha"] is True
    sender.texts.clear()

    flow.record_user_message(CHAT, SON, DIMA_DISPUTE)
    await flow.process(CHAT, trigger="push")

    assert flow.store.get_best_attempt("2026-10-04", "lyosha") == 23     # а не 50
    import json as _json
    credit = _json.loads(flow.store.get_state(f"video_credit:{rid}", ""))
    assert credit["reps"] == 23 and credit["disputed"] is True and credit["ex"] == "pushups"
    assert len(sender.texts) == 1 and "23" in sender.texts[0] and "50" not in sender.texts[0]

    # settle/XP беруть саме 23
    res = settle_progression(flow.store, date(2026, 10, 4))
    best = res["lyosha"]["best"]
    assert best == 23


async def test_dispute_number_credited_to_day_exercise_not_cv_exercise(tmp_path):
    """CV побачив присідання, людина назвала лише число -> число йде на вправу дня."""
    flow, sender, rid = await _dima_flow(tmp_path, cv_ex="squat", cv_reps=50)
    # відео-кредит записано «чужою» вправою (старий запис без ex дня) — виправлення має піти на вправу дня
    flow.store.set_state(f"video_credit:{rid}", "")
    await flow.handle_video_callback(rid)
    flow.store.set_state(f"video_credit:{rid}", "")  # як у старих записах: без довіри
    flow.record_user_message(CHAT, SON, "я 23 зробив")
    await flow.process(CHAT, trigger="push")
    assert flow.store.get_best_attempt("2026-10-04", "lyosha") == 23


async def test_trust_soft_hint_when_cv_sees_other_exercise(tmp_path):
    flow, sender, rid = await _dima_flow(tmp_path, cv_ex="squat", cv_reps=30)
    reply = await flow.handle_video_callback(rid)
    assert "не зовсім" in reply            # м'яка підказка, не відмова
    assert flow.store.get_best_attempt("2026-10-04", "lyosha") == 30   # зараховано на слово/CV


async def test_trust_soft_hint_on_ambiguous_flag_but_not_when_claimed(tmp_path):
    flow, sender, rid = await _dima_flow(tmp_path, cv_ex="pushup", cv_reps=20, flags=["ambiguous_exercise"])
    reply = await flow.handle_video_callback(rid)
    assert "не зовсім" in reply

    (tmp_path / "b").mkdir()
    flow2, sender2, rid2 = await _dima_flow(tmp_path / "b", cv_ex="pushup", cv_reps=20, flags=["ambiguous_exercise"])
    flow2.store.set_state(f"video_caption:{rid2}", "20 віджимань")
    reply2 = await flow2.handle_video_callback(rid2)
    assert "не зовсім" not in reply2       # вправа названа людиною — підказка не потрібна
