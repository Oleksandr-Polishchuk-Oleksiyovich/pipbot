"""
Число про ЧУЖЕ відео: власник (або будь-який учасник) пише «Льоша зробив 38» / відповідає на повідомлення бота про відео Льоші /
відповідає на питання «скільки вийшло?» — зараховується власнику відео, а не тому, хто писав.
Інцидент 27.10: Саша тричі писав «у Льоші 36», бот трактував це як спробу самого Саші й не виправив Льошу.
"""
import json

import pytest

from core.llm import LLMUnavailable
from tests.flow_fixtures import CHAT, DAD, make_flow
from tests.test_report_reps_tool import DAY, ToolLLM, _credit, _video

QUOTE = "[У відповідь на Спорт Зануда: «Побачив 34 віджимань з 37. Записав 34»] "


async def _say_as(flow, name, text):
    flow.record_user_message(CHAT, name, text)
    await flow.process(CHAT, trigger="push")


def _setup_two_videos(flow):
    _video(flow, rid="req-ly", reps=34, ex="pushup", slug="lyosha")
    _video(flow, rid="req-sa", reps=50, ex="pushup", slug="sasha")


@pytest.mark.asyncio
async def test_reply_to_bot_message_about_lyosha_credits_lyosha_not_speaker(tmp_path):
    seen = {}

    def script(tool, prompt):
        seen["prompt"] = prompt
        tool(subject_user="lyosha", count=38, exercise="", corrects_existing_video=True, request_id="req-ly")

    flow, sender, _ = make_flow(tmp_path, llm=ToolLLM(script), trust_mode=True)
    _setup_two_videos(flow)
    await flow.handle_video_callback("req-ly")
    await flow.handle_video_callback("req-sa")
    sa_before = flow.store.get_best_attempt(DAY, "sasha")
    sender.texts.clear()
    await _say_as(flow, DAD, QUOTE + "Я нарахував 38, ботя зараховуй 38")
    assert flow.store.get_best_attempt(DAY, "lyosha") == 38
    assert flow.store.get_day_progress(DAY)["lyosha"] is True       # норма 37 виконана
    assert flow.store.get_best_attempt(DAY, "sasha") == sa_before    # автор повідомлення не чіпається
    assert _credit(flow, "req-ly")["reps"] == 38 and _credit(flow, "req-sa")["reps"] == 50
    assert "owner=lyosha" in seen["prompt"] and "owner=sasha" in seen["prompt"] and "Побачив 34" in seen["prompt"]
    assert len(sender.texts) == 1 and "38" in sender.texts[0] and "Льош" in sender.texts[0]


@pytest.mark.asyncio
async def test_by_name_without_quote(tmp_path):
    def script(tool, prompt):
        tool(subject_user="lyosha", count=38, request_id="")

    flow, sender, _ = make_flow(tmp_path, llm=ToolLLM(script), trust_mode=True)
    _setup_two_videos(flow)
    await flow.handle_video_callback("req-ly")
    await flow.handle_video_callback("req-sa")
    await _say_as(flow, DAD, "Лічильник недорахував у Льоші — записуй Льоші 38")
    assert flow.store.get_best_attempt(DAY, "lyosha") == 38
    assert flow.store.get_best_attempt(DAY, "sasha") == 50


@pytest.mark.asyncio
async def test_empty_subject_defaults_to_author(tmp_path):
    def script(tool, prompt):
        tool(subject_user="", count=45, request_id="")

    flow, sender, _ = make_flow(tmp_path, llm=ToolLLM(script), trust_mode=True)
    _setup_two_videos(flow)
    await flow.handle_video_callback("req-ly")
    await flow.handle_video_callback("req-sa")
    await _say_as(flow, DAD, "та там було 45 а не 50")
    assert flow.store.get_best_attempt(DAY, "sasha") == 45
    assert flow.store.get_best_attempt(DAY, "lyosha") == 34


@pytest.mark.asyncio
async def test_pending_question_answered_by_other_participant(tmp_path):
    state = {"n": 0, "prompts": []}

    def script(tool, prompt):
        state["prompts"].append(prompt)
        if "ВІДКРИТЕ ПИТАННЯ" in prompt:
            tool(subject_user="lyosha", count=38, request_id="req-ly")

    flow, sender, _ = make_flow(tmp_path, llm=ToolLLM(script), trust_mode=True)
    _setup_two_videos(flow)
    await flow.handle_video_callback("req-ly")
    await flow.handle_video_callback("req-sa")
    sender.texts.clear()
    # 1) без числа: бот питає один раз і запам'ятовує, ПРО КОГО питання
    await _say_as(flow, DAD, QUOTE + "лічильник неправильно порахував")
    assert len(sender.texts) == 1 and "Скільки вийшло в Льош" in sender.texts[0]
    wait = json.loads(flow.store.get_state("dispute_wait:lyosha", ""))
    assert wait["asked_by"] == "sasha" and wait["rid"] == "req-ly"
    assert flow.store.get_state("dispute_wait:sasha", "") == ""
    # 2) відповідь від того, хто питав, — зараховується Льоші
    sender.texts.clear()
    await _say_as(flow, DAD, "38 вийшло")
    assert flow.store.get_best_attempt(DAY, "lyosha") == 38
    assert flow.store.get_best_attempt(DAY, "sasha") == 50
    assert any("ВІДКРИТЕ ПИТАННЯ" in p for p in state["prompts"])
    assert flow.store.get_state("dispute_wait:lyosha", "") == ""


@pytest.mark.asyncio
async def test_regex_fallback_reply_to_quote(tmp_path):
    flow, sender, _ = make_flow(tmp_path, llm=ToolLLM(unavailable=True), trust_mode=True)
    _setup_two_videos(flow)
    await flow.handle_video_callback("req-ly")
    await flow.handle_video_callback("req-sa")
    await _say_as(flow, DAD, QUOTE + "Я нарахував 38, ботя зараховуй 38")
    assert flow.store.get_best_attempt(DAY, "lyosha") == 38
    assert flow.store.get_best_attempt(DAY, "sasha") == 50


@pytest.mark.asyncio
async def test_regex_fallback_by_name_and_pending(tmp_path):
    flow, sender, _ = make_flow(tmp_path, llm=ToolLLM(unavailable=True), trust_mode=True)
    _setup_two_videos(flow)
    await flow.handle_video_callback("req-ly")
    await flow.handle_video_callback("req-sa")
    await _say_as(flow, DAD, "Лічильник недорахував у Льоші, записуй Льоші 38")
    assert flow.store.get_best_attempt(DAY, "lyosha") == 38

    # питання без числа -> відповідь «39 вийшло» від того самого учасника
    sender.texts.clear()
    await _say_as(flow, DAD, QUOTE + "ти неправильно порахував")
    assert flow.store.get_state("dispute_wait:lyosha", "")
    await _say_as(flow, DAD, "39 вийшло")
    assert flow.store.get_best_attempt(DAY, "lyosha") == 39


@pytest.mark.asyncio
async def test_regex_subject_self_claim_beats_mentioned_name(tmp_path):
    flow, _, _ = make_flow(tmp_path, llm=None, trust_mode=True)
    from tests.flow_fixtures import SON
    today = flow._today()
    # Діма-приклад: «я 23 зробив у знак солідарності із Сашком» — це його власне число
    flow.store.resolve_or_create_user("Діма", DAY)
    subj = flow._regex_subject("Нє нє, 50 там і не пахне, я 23 зробив у знак солідарності із Сашком", "lyosha", None, {}, today)
    assert subj == "lyosha"
    assert flow._regex_subject("Льоша зробив 36", "sasha", None, {}, today) == "lyosha"
    assert flow._regex_subject("36", "sasha", None, {"lyosha": {"asked_by": "sasha"}}, today) == "lyosha"
    assert flow._regex_subject("36", "sasha", None, {}, today) == "sasha"
