"""
Тести для TRUST_MODE:
1. Заявлена кількість зараховується попри низький результат CV.
2. Заявка з сусіднього текстового повідомлення.
3. Без заявки використовується підрахунок CV (CV >= target -> done; CV < target -> attempt записано, без «не зараховано»).
4. Поганий ракурс / низький confidence зараховується з додаванням підказки trust_angle_hint.
5. Не та вправа (wrong exercise) визнається без звинувачень і без порівняння з таргетом іншої вправи.
6. TRUST_MODE вимкнено (trust_mode=False) — зберігає стару поведінку («не зараховано»).
7. Unit-тести для parse_claimed_reps та parse_claimed_exercise.
8. Перевірка дефолтного значення core.config.TRUST_MODE (True).
"""
import os
import pytest

from core import challenge
from core.claims import parse_claimed_exercise, parse_claimed_reps
from core.config import TRUST_MODE
from tests.flow_fixtures import CHAT, SON, make_flow


def test_config_trust_mode_default_on():
    """core.config.TRUST_MODE має бути за замовчуванням True (ON)."""
    assert TRUST_MODE is True


def test_claims_parse_claimed_reps_unit():
    # Безпосередньо поруч зі словом вправи/повтору
    assert parse_claimed_reps("56 віджимань") == 56
    assert parse_claimed_reps("зробив 40 присідань") == 40
    assert parse_claimed_reps("віджимань 56") == 56
    assert parse_claimed_reps("x56") == 56
    assert parse_claimed_reps("х56") == 56
    assert parse_claimed_reps("56x") == 56

    # Ігнорування одиниць часу/ваги/відсотків/підходів
    assert parse_claimed_reps("40 віджимань за 2 хвилини") == 40
    assert parse_claimed_reps("45 віджимань за 30 сек") == 45
    assert parse_claimed_reps("35 віджимань за 45 с") == 35
    assert parse_claimed_reps("вага 80 кг, 50 віджимань") == 50
    assert parse_claimed_reps("1 підхід: 50 віджимань") == 50
    assert parse_claimed_reps("підхід 2: 50 віджимань") == 50
    assert parse_claimed_reps("2026-10-04 15:00 45 віджимань") == 45

    # Ігнорування службових блоків
    assert parse_claimed_reps("[Надіслано відеозвіт виконання норми]. 56 віджимань") == 56
    assert parse_claimed_reps("[У відповідь на Oleksandr: «Зроби 30»]\n[Надіслано відеозвіт виконання норми]. 40") == 40
    assert parse_claimed_reps("[У відповідь на Користувач: «50»]\n35 віджимань") == 35

    # Одиночне число без слів вправи
    assert parse_claimed_reps("56") == 56
    assert parse_claimed_reps("зробив 10 і потім 20") == 20

    # Межі діапазону та порожній результат
    assert parse_claimed_reps("0 віджимань") is None
    assert parse_claimed_reps("1200 віджимань") is None
    assert parse_claimed_reps("привіт як справи") is None
    assert parse_claimed_reps(None) is None


def test_claims_parse_claimed_exercise_unit():
    assert parse_claimed_exercise("56 віджимань") == "pushups"
    assert parse_claimed_exercise("відтиснувся 40") == "pushups"
    assert parse_claimed_exercise("pushup done") == "pushups"
    assert parse_claimed_exercise("push-up: 30") == "pushups"
    assert parse_claimed_exercise("40 присідань") == "squats"
    assert parse_claimed_exercise("присяд 35") == "squats"
    assert parse_claimed_exercise("squat 50") == "squats"
    assert parse_claimed_exercise("віджимання та присідання") is None
    assert parse_claimed_exercise("зробив 40") is None
    assert parse_claimed_exercise(None) is None


@pytest.mark.asyncio
async def test_claimed_number_credited_despite_low_cv(tmp_path):
    """
    CV нарахував лише 4 повтори, але у підписі користувача вказано 56 віджимань.
    Норма Льоші — 37. День має бути зараховано (done=True), спробу записано як 56,
    відповідь починається з 'Побачив 4 віджимання з 37', містить 'вірю' і не містить 'не зараховано'.
    """
    flow, sender, clock = make_flow(tmp_path, llm=None, trust_mode=True)
    req_id = "req-trust-high-claim"

    flow.store.set_state(f"video_caption:{req_id}", "56 віджимань")
    flow.store.create_video_check(
        request_id=req_id,
        chat_id=CHAT,
        message_id=501,
        user_slug="lyosha",
        duration_s=45.0,
        exercise="pushup",
        reps=4,
        confidence=0.9,
        valid=True,
        status="done",
    )

    reply = await flow.handle_video_callback(req_id)

    assert flow.store.get_day_progress("2026-10-04")["lyosha"] is True
    assert flow.store.get_best_attempt("2026-10-04", "lyosha") == 56
    assert reply.startswith("Побачив 4 віджимання з")
    assert "вірю" in reply
    assert "не зараховано" not in reply


@pytest.mark.asyncio
async def test_claim_from_nearby_text_message(tmp_path):
    """
    Підпису немає, але за кілька хвилин до цього той самий користувач написав повідомлення з кількістю.
    Бот повинен підтягнути заявку з історії чату.
    """
    flow, sender, clock = make_flow(tmp_path, llm=None, trust_mode=True)
    req_id = "req-trust-nearby-text"

    # Користувач надсилає повідомлення в чат
    flow.record_user_message(CHAT, SON, "зробив 45 віджимань")

    flow.store.create_video_check(
        request_id=req_id,
        chat_id=CHAT,
        message_id=502,
        user_slug="lyosha",
        duration_s=45.0,
        exercise="pushup",
        reps=5,
        confidence=0.9,
        valid=True,
        status="done",
    )

    reply = await flow.handle_video_callback(req_id)

    assert flow.store.get_day_progress("2026-10-04")["lyosha"] is True
    assert flow.store.get_best_attempt("2026-10-04", "lyosha") == 45
    assert "вірю" in reply
    assert "не зараховано" not in reply


@pytest.mark.asyncio
async def test_no_claim_uses_cv_count_success_and_missed(tmp_path):
    """
    Без заявки використовується підрахунок CV:
    - Якщо CV >= target -> норму зараховано.
    - Якщо CV < target -> не зараховано як день, але спробу записано, і НІКОЛИ немає фрази 'не зараховано'.
    """
    # 1. CV 40 >= target 37 -> done
    succ_dir = tmp_path / "succ"
    succ_dir.mkdir()
    flow1, _, _ = make_flow(succ_dir, llm=None, trust_mode=True)
    req1 = "req-noclaim-succ"
    flow1.store.create_video_check(
        request_id=req1,
        chat_id=CHAT,
        message_id=503,
        user_slug="lyosha",
        duration_s=45.0,
        exercise="pushup",
        reps=40,
        confidence=0.9,
        valid=True,
        status="done",
    )
    reply1 = await flow1.handle_video_callback(req1)
    assert flow1.store.get_day_progress("2026-10-04")["lyosha"] is True
    assert flow1.store.get_best_attempt("2026-10-04", "lyosha") == 40
    assert "норму зараховано" in reply1
    assert "не зараховано" not in reply1

    # 2. CV 4 < target 37 -> not done, attempt 4 recorded, no 'не зараховано'
    miss_dir = tmp_path / "miss"
    miss_dir.mkdir()
    flow2, _, _ = make_flow(miss_dir, llm=None, trust_mode=True)
    req2 = "req-noclaim-miss"
    flow2.store.create_video_check(
        request_id=req2,
        chat_id=CHAT,
        message_id=504,
        user_slug="lyosha",
        duration_s=45.0,
        exercise="pushup",
        reps=4,
        confidence=0.9,
        valid=True,
        status="done",
    )
    reply2 = await flow2.handle_video_callback(req2)
    assert flow2.store.get_day_progress("2026-10-04")["lyosha"] is False
    assert flow2.store.get_best_attempt("2026-10-04", "lyosha") == 4
    assert "не зараховано" not in reply2
    assert "у підписі до відео" in reply2


@pytest.mark.asyncio
async def test_bad_angle_or_low_confidence_still_credited_with_hint(tmp_path):
    """
    Поганий ракурс (low_camera_angle) чи низька впевненість (< 0.6) не блокують зарахування,
    але додають пораду trust_angle_hint.
    """
    flow, sender, clock = make_flow(tmp_path, llm=None, trust_mode=True)
    req_id = "req-trust-angle"

    flow.store.create_video_check(
        request_id=req_id,
        chat_id=CHAT,
        message_id=505,
        user_slug="lyosha",
        duration_s=45.0,
        exercise="pushup",
        reps=40,
        confidence=0.45,
        flags=["low_camera_angle"],
        valid=True,
        status="done",
    )

    reply = await flow.handle_video_callback(req_id)

    assert flow.store.get_day_progress("2026-10-04")["lyosha"] is True
    assert "норму зараховано" in reply
    assert "зарахую, але наступного разу зніми так, аби тебе було видно цілком" in reply.lower()


@pytest.mark.asyncio
async def test_wrong_exercise_handled_friendly_and_no_target_comparison(tmp_path):
    """
    Сьогодні віджимання, але підпис говорить про присідання.
    Бот фіксує це чесно й по-дружньому, не порівнює з таргетом віджимань, не звинувачує.
    """
    flow, sender, clock = make_flow(tmp_path, llm=None, trust_mode=True)
    req_id = "req-trust-wrong-ex"

    flow.store.set_state(f"video_caption:{req_id}", "зробив 40 присідань")
    flow.store.create_video_check(
        request_id=req_id,
        chat_id=CHAT,
        message_id=506,
        user_slug="lyosha",
        duration_s=45.0,
        exercise="pushup",
        reps=4,
        confidence=0.9,
        valid=True,
        status="done",
    )

    reply = await flow.handle_video_callback(req_id)

    # День віджимань не закрито, спробу для сьогоднішньої вправи не записано
    assert flow.store.get_day_progress("2026-10-04")["lyosha"] is False
    assert flow.store.get_best_attempt("2026-10-04", "lyosha") is None

    # Повідомлення визнає присідання та сьогоднішній розклад (віджимання)
    assert "присідання" in reply
    assert "віджимання" in reply
    # Не порівнює з нормою віджимань (37)
    assert "з 37" not in reply
    assert "не зараховано" not in reply


@pytest.mark.asyncio
async def test_trust_mode_off_preserves_old_behaviour(tmp_path):
    """
    Якщо trust_mode=False (режим суворого CV), повертається стара поведінка із 'не зараховано'.
    """
    flow, sender, clock = make_flow(tmp_path, llm=None, trust_mode=False)
    req_id = "req-old-behaviour"

    flow.store.create_video_check(
        request_id=req_id,
        chat_id=CHAT,
        message_id=507,
        user_slug="lyosha",
        duration_s=25.0,
        exercise="pushup",
        reps=4,
        confidence=0.9,
        valid=True,
        status="done",
    )

    reply = await flow.handle_video_callback(req_id)

    assert flow.store.get_day_progress("2026-10-04")["lyosha"] is False
    assert "не зараховано" in reply
    assert reply.startswith("Побачив 4 віджимання з 37")


@pytest.mark.asyncio
async def test_handle_video_stores_caption(tmp_path):
    """
    ChatFlow.handle_video зберігає caption у bot_state video_caption:<rid>.
    """
    flow, sender, clock = make_flow(tmp_path, llm=None, trust_mode=True)
    user = flow.store.get_user("lyosha")

    ok, reply = await flow.handle_video(
        chat_id=CHAT,
        user=user,
        message_id=601,
        file_id="vid_123",
        file_unique_id="uniq_123",
        duration=15.0,
        file_size=1024 * 1024,
        caption="56 віджимань",
    )

    assert ok is True
    # Знаходимо створений check
    check = flow.store.get_video_check_by_file_unique_id("uniq_123")
    assert check is not None
    caption_stored = flow.store.get_state(f"video_caption:{check.request_id}")
    assert caption_stored == "56 віджимань"
