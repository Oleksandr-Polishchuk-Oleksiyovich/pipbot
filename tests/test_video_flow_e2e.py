"""
End-to-end офлайн тести для T-34:
Judge-інтеграція + async callback + coach persona-відповіді.
Перевіряє кожен сценарій тону, коректне встановлення set_done,
доставку реплаєм на повідомлення та збереження в pending_replies при збоях.
"""
import pytest

from agents.report_judge import ReportJudgeAgent, VideoFact
from database.store import Store
from tests.flow_fixtures import CHAT, DAD, SON, make_flow


def test_judge_video_fact_scenarios():
    """Перевірка правил судді: done якщо valid, reps >= target, confidence >= 0.6."""
    target = 30

    # 1. Успіх
    succ_fact = VideoFact(exercise="pushup", reps=32, target=target, confidence=0.85, valid=True)
    v_succ = ReportJudgeAgent.judge_video_fact(succ_fact)
    assert v_succ.done is True
    assert v_succ.category == "success"

    # 2. Не вистачає одного (29 з 30)
    near_fact = VideoFact(exercise="pushup", reps=29, target=target, confidence=0.85, valid=True)
    v_near = ReportJudgeAgent.judge_video_fact(near_fact)
    assert v_near.done is False
    assert v_near.category == "near_miss"

    # 3. Сильно менше половини (10 з 30)
    far_fact = VideoFact(exercise="pushup", reps=10, target=target, confidence=0.85, valid=True)
    v_far = ReportJudgeAgent.judge_video_fact(far_fact)
    assert v_far.done is False
    assert v_far.category == "far_below"

    # 4. Низька якість (confidence 0.4 < 0.6)
    low_conf_fact = VideoFact(exercise="pushup", reps=35, target=target, confidence=0.45, valid=True)
    v_low = ReportJudgeAgent.judge_video_fact(low_conf_fact)
    assert v_low.done is False
    assert v_low.category == "bad_quality"

    # 5. Прапорець low_body_visibility
    vis_fact = VideoFact(exercise="pushup", reps=35, target=target, confidence=0.85, valid=True, flags=["low_body_visibility"])
    v_vis = ReportJudgeAgent.judge_video_fact(vis_fact)
    assert v_vis.done is False
    assert v_vis.category == "bad_quality"

    # 6. Не та вправа / флуд / розбіжність класів
    mismatch_fact = VideoFact(exercise="none", reps=0, target=target, confidence=0.9, valid=False, flags=["class_mismatch"])
    v_mismatch = ReportJudgeAgent.judge_video_fact(mismatch_fact)
    assert v_mismatch.done is False
    assert v_mismatch.category == "wrong_exercise"

    # 7. Ненадійний ракурс камери (камера знизу/з підлоги)
    # -> ніколи не відхиляємо за ракурс: зараховуємо як пораховано + camera_hint
    low_cam_fact = VideoFact(exercise="pushup", reps=30, target=target, confidence=0.55, valid=True, flags=["low_camera_angle"])
    v_low_cam = ReportJudgeAgent.judge_video_fact(low_cam_fact)
    assert v_low_cam.done is True
    assert v_low_cam.category == "success"
    assert v_low_cam.hint_keys == ["angle_hint"]
    # Навіть дуже низька впевненість через ракурс не призводить до відмови
    low_cam_fact2 = VideoFact(exercise="pushup", reps=30, target=target, confidence=0.35, valid=True, flags=["low_camera_angle", "low_body_visibility"])
    assert ReportJudgeAgent.judge_video_fact(low_cam_fact2).done is True
    # Нормальний ракурс — без підказки
    assert ReportJudgeAgent.judge_video_fact(
        VideoFact(exercise="pushup", reps=30, target=target, confidence=0.9, valid=True, flags=[])
    ).hint_keys == []


@pytest.mark.asyncio
async def test_flow_video_callback_success(tmp_path):
    """Сценарій 1: reps >= target -> зарахування норми + persona-похвала реплаєм на відео."""
    flow, sender, _ = make_flow(tmp_path, llm=None)
    user = flow.store.get_user("lyosha")

    # Створюємо перевірку відео (норма Льоші на 2026-10-04 — 37)
    check = flow.store.create_video_check(
        request_id="req-e2e-succ",
        chat_id=CHAT,
        message_id=555,
        user_slug="lyosha",
        duration_s=45.0,
        exercise="pushup",
        reps=38,
        confidence=0.92,
        valid=True,
        status="done",
    )

    reply = await flow.handle_video_callback("req-e2e-succ")

    # Норму зараховано
    assert flow.store.get_day_progress("2026-10-04")["lyosha"] is True
    # Persona текст
    assert "зараховано" in reply.lower() or "красунчик" in reply.lower()
    # Реплай саме на message_id відео
    assert sender.replies[-1][0] == 555
    assert sender.replies[-1][1] == reply


@pytest.mark.asyncio
async def test_flow_video_callback_near_miss(tmp_path):
    """Сценарій 2: майже норма -> не зараховано + «не вистачає одного, докинеш?»."""
    flow, sender, _ = make_flow(tmp_path, llm=None)

    # Норма 37, зробив 36
    check = flow.store.create_video_check(
        request_id="req-e2e-near",
        chat_id=CHAT,
        message_id=556,
        user_slug="lyosha",
        duration_s=40.0,
        exercise="pushup",
        reps=36,
        confidence=0.88,
        valid=True,
        status="done",
    )

    reply = await flow.handle_video_callback("req-e2e-near")

    assert flow.store.get_day_progress("2026-10-04")["lyosha"] is False
    assert reply.startswith("Побачив 36 віджимань з 37") and "не зараховано" in reply
    assert sender.replies[-1][0] == 556


@pytest.mark.asyncio
async def test_flow_video_callback_far_below(tmp_path):
    """Сценарій 3: сильно менше -> «Там і половини немає — за кого ти мене маєш»."""
    flow, sender, _ = make_flow(tmp_path, llm=None)

    # Норма 37, зробив 12
    check = flow.store.create_video_check(
        request_id="req-e2e-far",
        chat_id=CHAT,
        message_id=557,
        user_slug="lyosha",
        duration_s=25.0,
        exercise="pushup",
        reps=12,
        confidence=0.85,
        valid=True,
        status="done",
    )

    reply = await flow.handle_video_callback("req-e2e-far")

    assert flow.store.get_day_progress("2026-10-04")["lyosha"] is False
    assert reply.startswith("Побачив 12 віджимань з 37") and "не зараховано" in reply
    assert sender.replies[-1][0] == 557


@pytest.mark.asyncio
async def test_flow_video_callback_bad_quality(tmp_path):
    """Сценарій 4: погана якість -> «Сорі, нічорта не видно»."""
    flow, sender, _ = make_flow(tmp_path, llm=None)

    check = flow.store.create_video_check(
        request_id="req-e2e-bad",
        chat_id=CHAT,
        message_id=558,
        user_slug="lyosha",
        duration_s=20.0,
        exercise="pushup",
        reps=20,
        confidence=0.45,  # низька впевненість
        valid=False,
        status="done",
    )

    reply = await flow.handle_video_callback("req-e2e-bad")

    assert flow.store.get_day_progress("2026-10-04")["lyosha"] is False
    assert reply.startswith("Побачив 20 віджимань з 37") and "зараховано" in reply
    assert sender.replies[-1][0] == 558


@pytest.mark.asyncio
async def test_flow_video_callback_wrong_exercise(tmp_path):
    """Сценарій 5: флуд/не та вправа -> «Я думав, там присяд/віджимання, а відео цікаве, проте не по темі»."""
    flow, sender, _ = make_flow(tmp_path, llm=None)

    check = flow.store.create_video_check(
        request_id="req-e2e-wrong",
        chat_id=CHAT,
        message_id=559,
        user_slug="lyosha",
        duration_s=15.0,
        exercise="none",
        reps=0,
        confidence=0.9,
        valid=False,
        flags=["class_mismatch"],
        status="done",
    )

    reply = await flow.handle_video_callback("req-e2e-wrong")

    assert flow.store.get_day_progress("2026-10-04")["lyosha"] is False
    assert reply.startswith("Побачив ") and "інша вправа" in reply
    assert sender.replies[-1][0] == 559


REDO_WORDS = ("перезн", "зніми ще", "надішли знову", "надішли ще", "переробі", "переделай", "ще раз")


@pytest.mark.asyncio
async def test_flow_video_callback_low_camera_angle(tmp_path):
    """Ракурс знизу/з підлоги -> повтори зараховано одразу + порада на НАСТУПНЕ відео (без прохання переробити)."""
    flow, sender, _ = make_flow(tmp_path, llm=None)

    flow.store.create_video_check(
        request_id="req-e2e-low-cam",
        chat_id=CHAT,
        message_id=560,
        user_slug="lyosha",
        duration_s=42.0,
        exercise="pushup",
        reps=40,
        confidence=0.55,
        valid=True,
        status="done",
        flags=["low_camera_angle"],
    )

    reply = await flow.handle_video_callback("req-e2e-low-cam")

    # Норму зараховано
    assert flow.store.get_day_progress("2026-10-04")["lyosha"] is True
    low = reply.lower()
    assert "зараховано" in low
    # Порада на наступний раз
    assert "наступного разу" in low
    assert "цілком" in low
    # Жодного прохання переробити/перезняти
    assert not any(w in low for w in REDO_WORDS)
    assert sender.replies[-1][0] == 560


@pytest.mark.asyncio
async def test_angle_hint_text_overridden_from_db(tmp_path):
    """angle_hint резолвиться з БД (bot_texts), якщо там є значення, і діє негайно (без рестарту)."""
    flow, sender, _ = make_flow(tmp_path, llm=None)
    flow.bot_texts.set("angle_hint", "Наступного разу постав камеру трохи вище 📹")
    flow.store.create_video_check(
        request_id="req-e2e-low-cam-db", chat_id=CHAT, message_id=561, user_slug="lyosha",
        duration_s=42.0, exercise="pushup", reps=40, confidence=0.55, valid=True,
        status="done", flags=["low_camera_angle"],
    )
    reply = await flow.handle_video_callback("req-e2e-low-cam-db")
    assert flow.store.get_day_progress("2026-10-04")["lyosha"] is True
    assert "постав камеру трохи вище" in reply


def test_video_replies_never_ask_to_redo():
    """Жодна відповідь на відео (fallbacks + camera hint) не просить перезняти/переробити підхід."""
    from prompts import COACH_VIDEO_PERSONA_FALLBACKS, DEFAULT_BOT_TEXTS, DEFAULT_HINT_TEXTS

    for text in list(COACH_VIDEO_PERSONA_FALLBACKS.values()) + list(DEFAULT_HINT_TEXTS.values()) + list(DEFAULT_BOT_TEXTS.values()):
        assert not any(w in text.lower() for w in REDO_WORDS), text


@pytest.mark.asyncio
async def test_flow_video_callback_error_queues_in_pending_replies(tmp_path):
    """Якщо відправка впала (мережева помилка), відповідь зберігається в pending_replies."""
    class FailingSender:
        async def send_text(self, chat_id, text, **kwargs):
            raise ConnectionError("Telegram network timeout")
        async def send_video(self, chat_id, path):
            pass

    from tests.flow_fixtures import Clock, NOW
    clock = Clock(NOW)
    store = Store(str(tmp_path / "fail.db"))
    store.init_schema()
    from app.flow import ChatFlow
    flow = ChatFlow(store, None, FailingSender(), now=clock)
    flow.init_storage()

    check = store.create_video_check(
        request_id="req-fail-dlv",
        chat_id=CHAT,
        message_id=777,
        user_slug="lyosha",
        exercise="pushup",
        reps=40,
        confidence=0.9,
        valid=True,
        status="done",
    )

    reply = await flow.handle_video_callback("req-fail-dlv")
    assert reply is not None

    # Повинна бути збережена в pending_replies
    pending = store.list_pending_replies()
    assert len(pending) == 1
    assert pending[0].chat_id == CHAT
    assert pending[0].reply_to_message_id == 777
    assert pending[0].text == reply
