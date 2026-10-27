"""
Тул report_reps (LLM): число повторів зі слів/цифр/сленгу замість regex-парсера.
LLM замокано: він «читає» повідомлення й викликає report_reps(...) так, як це зробила б справжня модель.
Regex — лише фолбек, коли LLM недоступний.
"""
import json
import logging
from datetime import date

import pytest

from agents.coach import CoachAgent
from agents.reps_claim import RepsClaim, make_report_reps_tool, normalize_exercise
from core.llm import LLMUnavailable
from core.progression import settle_progression
from tests.flow_fixtures import CHAT, SON, make_flow

DAY = "2026-10-04"
DIMA = "Нє нє, 50 там і не пахне, я 23 зробив у знак солідарності із Сашком"


class ToolLLM:
    """Мок GeminiLLM: text() віддає тули сценарію; json() недоступний (роутер/суддя йдуть у фолбек)."""

    def __init__(self, script=None, unavailable=False):
        self.script = script
        self.unavailable = unavailable
        self.prompts = []
        self.tool_names = []

    def text(self, system, prompt, *, tools=None, temperature=0.7):
        if self.unavailable:
            raise LLMUnavailable("mock down")
        fns = {t.__name__: t for t in (tools or [])}
        self.tool_names.append(sorted(fns))
        self.prompts.append(prompt)
        if self.script and "report_reps" in fns:
            self.script(fns["report_reps"], prompt)
        return "ок"

    def json(self, *a, **k):
        raise LLMUnavailable("mock json down")


def _video(flow, rid="req-1", reps=50, ex="squat", slug="lyosha", flags=None):
    flow.store.create_video_check(
        request_id=rid, chat_id=CHAT, message_id=500, user_slug=slug, duration_s=60.0,
        exercise=ex, reps=reps, confidence=0.75, valid=True, status="done", flags=flags or [],
    )
    with flow.store._connect() as con:
        con.execute("UPDATE video_checks SET created_at = ? WHERE request_id = ?", (f"{DAY} 08:00:00", rid))


async def _dispute(flow, text):
    flow.record_user_message(CHAT, SON, text)
    await flow.process(CHAT, trigger="push")


def _credit(flow, rid):
    return json.loads(flow.store.get_state(f"video_credit:{rid}", ""))


# ---------- сама схема/валідація тулу ----------
def test_tool_schema_and_validation(tmp_path):
    flow, _, _ = make_flow(tmp_path, llm=None, trust_mode=True)
    sink = []
    tool = make_report_reps_tool(flow.store, sink, valid_request_ids=["r1"])
    assert tool.__name__ == "report_reps"
    assert list(tool.__code__.co_varnames[:tool.__code__.co_argcount]) == [
        "subject_user", "count", "exercise", "corrects_existing_video", "request_id", "confidence"]
    assert "Помилка" in tool("lyosha", 0)
    assert "Помилка" in tool("lyosha", 5000)
    assert "Помилка" in tool("lyosha", True)
    assert "Помилка" in tool("lyosha", "багато")
    assert "Помилка" in tool("lyosha", 10, exercise="планка")
    assert "Помилка" in tool("nobody-here", 10)
    assert sink == []
    assert tool("lyosha", 23, exercise="віджимання", request_id="r1", confidence=7).startswith("Прийнято")
    assert tool("lyosha", "23", exercise="", request_id="zzz").startswith("Прийнято")
    c1, c2 = sink
    assert (c1.user, c1.count, c1.exercise, c1.request_id, c1.confidence) == ("lyosha", 23, "pushups", "r1", 1.0)
    assert c2.exercise is None and c2.request_id is None and c2.corrects_existing_video is True
    assert normalize_exercise("Squats") == "squats" and normalize_exercise(None) is None


# ---------- слова, сленг, Діма (диспут після відео) ----------
@pytest.mark.parametrize("text,count", [
    ("двадцять три", 23),
    ("та там півсотні було, а не 4", 50),
    ("десь 20", 20),
    ("я 23 зробив, а не 50", 23),
])
async def test_dispute_words_via_llm_tool(tmp_path, text, count):
    def script(tool, prompt):
        tool(subject_user="lyosha", count=count, exercise="", corrects_existing_video=True, request_id="req-1", confidence=0.9)

    flow, sender, _ = make_flow(tmp_path, llm=ToolLLM(script), trust_mode=True)
    _video(flow, reps=4, ex="pushup")
    await flow.handle_video_callback("req-1")
    sender.texts.clear()
    await _dispute(flow, text)
    assert flow.store.get_best_attempt(DAY, "lyosha") == count
    assert _credit(flow, "req-1") == {"reps": count, "ex": "pushups", "disputed": True}
    assert flow.store.get_state("last_processed_id")  # повідомлення оброблено
    assert len(sender.texts) == 1 and str(count) in sender.texts[0]


async def test_dima_example_llm_lower_number_exact_and_exercise_from_video(tmp_path):
    seen = {}

    def script(tool, prompt):
        seen["prompt"] = prompt
        tool(subject_user="lyosha", count=23, exercise="", corrects_existing_video=True, request_id="req-1", confidence=0.95)

    llm = ToolLLM(script)
    flow, sender, _ = make_flow(tmp_path, llm=llm, trust_mode=True)
    _video(flow, reps=50, ex="squat")
    await flow.handle_video_callback("req-1")
    assert flow.store.get_best_attempt(DAY, "lyosha") == 50          # як було у проді
    await _dispute(flow, DIMA)
    assert flow.store.get_best_attempt(DAY, "lyosha") == 23          # рівно назване, нижче за CV
    assert _credit(flow, "req-1")["ex"] == "pushups"                 # вправа відео/дня, бо Діма її не називав
    assert "я 23 зробив" in seen["prompt"] and "req-1" in seen["prompt"]   # LLM бачить текст і відео-контекст
    res = settle_progression(flow.store, date(2026, 10, 4))
    assert res["lyosha"]["best"] == 23


async def test_dima_example_names_other_exercise_moves_credit(tmp_path):
    def script(tool, prompt):
        tool(subject_user="lyosha", count=23, exercise="squats", request_id="req-1")

    flow, sender, _ = make_flow(tmp_path, llm=ToolLLM(script), trust_mode=True)
    _video(flow, reps=50, ex="squat")
    await flow.handle_video_callback("req-1")
    await _dispute(flow, "то були присідання, 23 їх")
    assert _credit(flow, "req-1")["ex"] == "squats"
    # відео більше не рахується на віджимання дня: спроба дня перераховано (немає кредитів вправи дня)
    assert flow.store.get_best_attempt(DAY, "lyosha") == 0
    assert flow.store.get_day_progress(DAY)["lyosha"] is False


async def test_llm_no_tool_call_and_no_dispute_marker_not_intercepted(tmp_path):
    flow, sender, _ = make_flow(tmp_path, llm=ToolLLM(lambda tool, prompt: None), trust_mode=True)
    _video(flow, reps=30, ex="pushup")
    await flow.handle_video_callback("req-1")
    before = flow.store.get_best_attempt(DAY, "lyosha")
    await _dispute(flow, "через 5 хвилин буду")      # є цифра, але LLM не бачить повторів
    assert flow.store.get_best_attempt(DAY, "lyosha") == before
    assert _credit(flow, "req-1").get("disputed") is None


async def test_llm_no_number_in_dispute_asks_once(tmp_path):
    flow, sender, _ = make_flow(tmp_path, llm=ToolLLM(lambda tool, prompt: None), trust_mode=True)
    _video(flow, reps=30, ex="pushup")
    await flow.handle_video_callback("req-1")
    sender.texts.clear()
    await _dispute(flow, "ти неправильно порахував")
    await _dispute(flow, "ну неправильно ж")
    assert len([t for t in sender.texts if "Скільки в тебе вийшло" in t]) == 1


async def test_claim_for_user_without_video_is_ignored(tmp_path):
    def script(tool, prompt):
        tool(subject_user="sasha", count=99, exercise="")

    flow, sender, _ = make_flow(tmp_path, llm=ToolLLM(script), trust_mode=True)
    _video(flow, reps=30, ex="pushup")
    await flow.handle_video_callback("req-1")
    await _dispute(flow, "Сашко 99 зробив")
    assert flow.store.get_best_attempt(DAY, "lyosha") == 30


async def test_llm_unavailable_falls_back_to_regex_and_logs_path(tmp_path, caplog):
    flow, sender, _ = make_flow(tmp_path, llm=ToolLLM(unavailable=True), trust_mode=True)
    _video(flow, reps=50, ex="squat")
    await flow.handle_video_callback("req-1")
    caplog.set_level(logging.INFO)
    await _dispute(flow, DIMA)
    assert flow.store.get_best_attempt(DAY, "lyosha") == 23
    assert any("Dispute path=regex_fallback" in r.getMessage() for r in caplog.records)


# ---------- підпис/сусідні тексти до відео ----------
async def test_caption_words_extracted_by_llm(tmp_path):
    def script(tool, prompt):
        assert "двадцять три віджимання" in prompt
        tool(subject_user="lyosha", count=23, exercise="pushups", corrects_existing_video=False, request_id="req-c")

    flow, sender, _ = make_flow(tmp_path, llm=ToolLLM(script), trust_mode=True)
    flow.store.set_state("video_caption:req-c", "двадцять три віджимання")
    _video(flow, rid="req-c", reps=5, ex="pushup")
    await flow.handle_video_callback("req-c")
    assert flow.store.get_best_attempt(DAY, "lyosha") == 23
    assert flow.store.get_state("video_claim_path:req-c") == "llm"
    assert _credit(flow, "req-c") == {"reps": 23, "ex": "pushups"}


async def test_caption_llm_none_does_not_use_regex(tmp_path):
    flow, sender, _ = make_flow(tmp_path, llm=ToolLLM(lambda tool, prompt: None), trust_mode=True)
    flow.store.set_state("video_caption:req-n", "гарна погода, 5 хв пробіжки")
    _video(flow, rid="req-n", reps=12, ex="pushup")
    await flow.handle_video_callback("req-n")
    assert flow.store.get_state("video_claim_path:req-n") == "llm_none"
    assert _credit(flow, "req-n")["reps"] == 12     # зараховано CV, а не «5»


async def test_caption_fallback_to_regex_when_llm_down(tmp_path):
    flow, sender, _ = make_flow(tmp_path, llm=ToolLLM(unavailable=True), trust_mode=True)
    flow.store.set_state("video_caption:req-f", "40 віджимань")
    _video(flow, rid="req-f", reps=12, ex="pushup")
    await flow.handle_video_callback("req-f")
    assert flow.store.get_state("video_claim_path:req-f") == "regex_fallback"
    assert flow.store.get_best_attempt(DAY, "lyosha") == 40


async def test_caption_without_llm_configured_uses_regex(tmp_path):
    flow, sender, _ = make_flow(tmp_path, llm=None, trust_mode=True)
    flow.store.set_state("video_caption:req-r", "40 віджимань")
    _video(flow, rid="req-r", reps=12, ex="pushup")
    await flow.handle_video_callback("req-r")
    assert flow.store.get_state("video_claim_path:req-r") == "regex"
    assert flow.store.get_best_attempt(DAY, "lyosha") == 40


# ---------- coach: тул у списку, правила в промпті, claim застосовується тихо ----------
def test_coach_has_report_reps_tool_and_prompt_rules(tmp_path):
    flow, _, _ = make_flow(tmp_path, llm=None, trust_mode=True)
    prompt = CoachAgent(None, flow.store).build_system_prompt(date(2026, 10, 4))
    assert "report_reps" in prompt and "Слова людей важливіші" in prompt

    sink = []

    def script(tool, p):
        tool(subject_user="lyosha", count=31, exercise="", request_id="")

    llm = ToolLLM(script)
    CoachAgent(llm, flow.store).reply(date(2026, 10, 4), [], [], sink)
    assert "report_reps" in llm.tool_names[-1]
    assert [(c.user, c.count) for c in sink] == [("lyosha", 31)]


async def test_coach_claims_applied_silently(tmp_path):
    flow, sender, _ = make_flow(tmp_path, llm=None, trust_mode=True)
    _video(flow, reps=50, ex="squat")
    await flow.handle_video_callback("req-1")
    sender.texts.clear()
    await flow._apply_reported_claims(CHAT, [RepsClaim("lyosha", 23, None, True, None)])
    assert flow.store.get_best_attempt(DAY, "lyosha") == 23
    assert sender.texts == []
