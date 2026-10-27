"""
Єдиний джерельний реєстр сценаріїв суперечок про кількість повторів / довіри (TRUST_MODE).

Один табличний реєстр `SCENARIOS` живить ДВА шари:
  • детермінований (tests/test_trust_scenarios.py) — моканий LLM (ScriptedToolLLM), жодної мережі, швидко;
  • живий eval (tests/eval/run_prompt_eval.py) — реальний core.llm.GeminiLLM, порівняння фактичного стану БД із `expect`.

Обидва шари будують ОДИН І ТОЙ САМИЙ ChatFlow через `tests.flow_fixtures.make_flow`, ставлять однаковий контекст
(`build_context`) і прокручують однакові повідомлення (`run_turns`), а потім асертять однаковий `expect` (`verify`).
Відмінність лише в тому, ЯКИЙ llm підкладений у flow: мок зі сценарним tool_call чи справжня модель.

Інцидент 27.10: відео Льоші полічило 34/36; Саша (власник) писав «зараховуй 36», «36 вийшло», «записуй Льоші 36»,
а бот сприймав числа як слова Саші про СЕБЕ й не виправляв Льошу. Звідси категорії third_party_*.

Приклади ядра (`prompts_core/trust_examples.json`) окремо перетворюються на сценарії у `examples_as_scenarios()`
(інваріант (f) у tests/test_trust_core.py) — вони теж тест-кейси.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional

from core import challenge, trust_core
from core.llm import LLMUnavailable
from database.store import User
from prompts import REDO_WORDS
from tests.flow_fixtures import CHAT

# День челенджу у фікстурах: 2026-10-04, вправа дня — ВІДЖИМАННЯ, норми: Льоша 37, Саша 47.
DAY = "2026-10-04"
DAY_DATE = date(2026, 10, 4)
DAY_TS = f"{DAY} 08:00:00"

# slug -> ім'я автора в чаті (так, як воно резолвиться у БД через resolve_or_create_user).
AUTHOR_NAME = {"lyosha": "Алексей", "sasha": "Oleksandr Polishchuk", "kovdimay": "Діма"}

# Фрази, які НЕ мають з'являтися у жодній відповіді бота в м'якому режимі (підозра / вердикт CV / прохання перезняти).
# Застосовується універсально до всіх відповідей бота у сценаріях (категорія tone_no_suspicion — для всіх).
FORBIDDEN_IN_REPLY = ("не зараховано", "шахрай", "обман", "підозр", "брехн", *REDO_WORDS)


# ------------------------------------------------------------------ dataclasses
@dataclass
class VideoSpec:
    owner: str                      # slug власника відео
    rid: str
    cv_exercise: str = "pushup"     # що «побачив» CV (raw: 'pushup' | 'squat')
    cv_reps: int = 34
    flags: List[str] = field(default_factory=list)
    caption: Optional[str] = None   # підпис до відео (вмикає LLM-екстракцію на етапі callback)
    claim_tool: Optional["ToolCall"] = None  # що ідеальний LLM має викликати під час екстракції з підпису (None — нічого)
    confidence: float = 0.75
    # метадані дедуплікації (лише для duplicate_reupload): той самий file_size+тривалість => дубль
    file_unique_id: Optional[str] = None
    file_size: Optional[int] = None
    duration_s: float = 60.0


@dataclass
class ToolCall:
    """Ідеальний виклик report_reps, який має зробити LLM (None у Turn означає «тул не викликати»)."""
    subject_user: str
    count: int
    exercise: str = ""
    corrects_existing_video: bool = True
    request_id: str = ""
    confidence: float = 1.0

    def as_kwargs(self) -> dict:
        return dict(subject_user=self.subject_user, count=self.count, exercise=self.exercise,
                    corrects_existing_video=self.corrects_existing_video,
                    request_id=self.request_id, confidence=self.confidence)


@dataclass
class Turn:
    author: str                      # slug автора повідомлення
    text: str
    reply_to: Optional[str] = None   # цитований текст повідомлення бота (reply-to), без обгортки
    tool: Optional[ToolCall] = None  # що має викликати ІДЕАЛЬНИЙ LLM на цьому повідомленні

    def rendered_text(self) -> str:
        if self.reply_to:
            return f"[У відповідь на Спорт Зануда: «{self.reply_to}»] {self.text}"
        return self.text


@dataclass
class PendingQuestion:
    subject: str     # про чиє відео бот спитав «скільки вийшло?»
    rid: str
    asked_by: str    # через чиє повідомлення поставлено питання


@dataclass
class Expect:
    best_attempt: Dict[str, int] = field(default_factory=dict)        # get_best_attempt(DAY, slug)
    done: Dict[str, bool] = field(default_factory=dict)               # get_day_progress(DAY)[slug]
    credit: Dict[str, dict] = field(default_factory=dict)             # video_credit:<rid> (підмножина ключів)
    bot_msg_count: Optional[int] = None                               # len(sender.texts) за час дії
    must_contain: List[str] = field(default_factory=list)            # підрядки, що мають бути у відповідях бота
    must_not_contain: List[str] = field(default_factory=list)        # підрядки, яких НЕ має бути
    dispute_wait_set: List[str] = field(default_factory=list)        # slugs із НЕпорожнім dispute_wait
    dispute_wait_cleared: List[str] = field(default_factory=list)    # slugs із порожнім dispute_wait
    xp_zero: List[str] = field(default_factory=list)                 # slugs, чий XP лишився 0 (settle не запускали)
    tool_called: Optional[bool] = None                               # чи має LLM викликати report_reps (для eval)


@dataclass
class Scenario:
    id: str
    category: str
    description: str
    videos: List[VideoSpec] = field(default_factory=list)
    extra_users: List[str] = field(default_factory=list)             # slugs, яких треба створити (крім lyosha/sasha)
    leaves: List[tuple] = field(default_factory=list)                # [(slug, 'sick'|'vacation')]
    pending_question: Optional[PendingQuestion] = None
    settle_before: bool = False                                      # провести settle_progression до дії (день пораховано)
    turns: List[Turn] = field(default_factory=list)                  # None => дія = обробка callback-ів відео (підпис/дубль)
    expect: Expect = field(default_factory=Expect)
    open_question: str = ""                                          # нотатка про відкрите питання політики / GAP


# ------------------------------------------------------------------ мок LLM
class ScriptedToolLLM:
    """
    Мок GeminiLLM: json() недоступний (роутер/суддя йдуть у keyword-фолбек). text() повертає 'ок'.
    Коли у виклику text() присутній РІВНО тул report_reps (це RepsClaimAgent, не coach) — бере черговий ToolCall
    зі сценарію (FIFO) і викликає тул рівно так, як це зробив би ідеальний LLM. Coach (кілька тулів) НЕ чіпаємо,
    щоб не застосовувати claim двічі й не «з'їдати» чергу.
    """

    def __init__(self, tool_queue: List[Optional[ToolCall]]):
        self._queue = list(tool_queue)
        self.calls: List[Optional[ToolCall]] = []

    def text(self, system, prompt, *, tools=None, temperature=0.7):
        names = {t.__name__ for t in (tools or [])}
        if names == {"report_reps"}:
            tc = self._queue.pop(0) if self._queue else None
            self.calls.append(tc)
            if tc is not None:
                fn = next(t for t in tools if t.__name__ == "report_reps")
                fn(**tc.as_kwargs())
        return "ок"

    def json(self, *a, **k):
        raise LLMUnavailable("mock json down")


def scripted_llm_for(scenario: Scenario) -> ScriptedToolLLM:
    """Черга tool_call-ів для детермінованого шару: по одному на кожне повідомлення/підпис, що очікує екстракції."""
    queue: List[Optional[ToolCall]] = []
    if scenario.turns:
        queue = [t.tool for t in scenario.turns]
    else:
        # дія без повідомлень: екстракція з підпису (один виклик на відео з caption)
        for v in scenario.videos:
            if v.caption is not None:
                queue.append(v.claim_tool)
    return ScriptedToolLLM(queue)


# ------------------------------------------------------------------ побудова контексту
def _ensure_user(flow, slug: str) -> None:
    if flow.store.find_user(slug) is not None:
        return
    name = AUTHOR_NAME.get(slug, slug)
    flow.store.upsert_user(User(
        slug=slug, telegram_id=abs(hash(slug)) % 100000, telegram_name=name, username=slug,
        display_name=name, emoji="💪", status="active", difficulty="normal", lives=5, max_lives=5,
        joined_date="2026-08-31", aliases=[name.lower(), slug],
    ))
    # норма на день для нових учасників (щоб day_targets не падав у дефолт 10)
    challenge.set_target(flow.store, slug, 36, DAY_DATE)


def _create_video(flow, v: VideoSpec) -> None:
    flow.store.create_video_check(
        request_id=v.rid, chat_id=CHAT, message_id=500 + abs(hash(v.rid)) % 400, user_slug=v.owner,
        duration_s=v.duration_s, exercise=v.cv_exercise, reps=v.cv_reps, confidence=v.confidence,
        valid=True, status="done", flags=list(v.flags),
    )
    with flow.store._connect() as con:
        con.execute("UPDATE video_checks SET created_at = ? WHERE request_id = ?", (DAY_TS, v.rid))
    if v.file_size is not None:
        flow.store.save_video_meta(v.rid, v.owner, file_unique_id=v.file_unique_id, file_size=v.file_size,
                                   width=1080, height=1920, duration_s=v.duration_s, file_name=None)


async def build_context(flow) -> None:
    """Ставить УСІ учасники/норми перед будь-яким сценарієм (lyosha/sasha завжди є з make_flow)."""
    # make_flow уже сідить lyosha/sasha. Тут нічого глобального не треба.


async def setup_scenario(flow, scenario: Scenario) -> None:
    """Готує стан БД: учасники, лікарняні, відео (+ попередній залік через callback), відкрите питання, settle."""
    for slug in scenario.extra_users:
        _ensure_user(flow, slug)
    for slug, _ in scenario.leaves:
        _ensure_user(flow, slug)
    from core.leave import set_leave as core_set_leave
    for slug, kind in scenario.leaves:
        core_set_leave(flow.store, slug, kind, start=DAY, end=None, set_by="test")

    for v in scenario.videos:
        if v.caption is not None:
            flow.store.set_state(f"video_caption:{v.rid}", v.caption)
        _create_video(flow, v)

    if scenario.turns:
        # Диспут-сценарій: відео вже «пораховано» ботом (callback без підпису => без LLM), потім летить повідомлення.
        for v in scenario.videos:
            await flow.handle_video_callback(v.rid)

    if scenario.pending_question:
        pq = scenario.pending_question
        flow.store.set_state(f"dispute_wait:{pq.subject}",
                             json.dumps({"rid": pq.rid, "day": DAY, "asked_by": pq.asked_by}))

    if scenario.settle_before:
        from core.progression import settle_progression
        settle_progression(flow.store, DAY_DATE)


async def run_action(flow, sender, scenario: Scenario) -> None:
    """Виконує дію сценарію й ЛИШАЄ у sender.texts лише відповіді на цю дію (попередні очищено)."""
    sender.texts.clear()
    if scenario.turns:
        for turn in scenario.turns:
            flow.record_user_message(CHAT, AUTHOR_NAME.get(turn.author, turn.author), turn.rendered_text())
            await flow.process(CHAT, trigger="push")
    else:
        # дія без повідомлень: обробити callback-и відео (підпис/дубль) по порядку
        for v in scenario.videos:
            await flow.handle_video_callback(v.rid)


async def run_scenario(flow, sender, scenario: Scenario) -> None:
    await build_context(flow)
    await setup_scenario(flow, scenario)
    await run_action(flow, sender, scenario)


# ------------------------------------------------------------------ перевірка
def _credit_state(flow, rid: str) -> dict:
    raw = flow.store.get_state(f"video_credit:{rid}", "")
    return json.loads(raw) if raw else {}


def verify(flow, sender, scenario: Scenario) -> List[str]:
    """Повертає список розбіжностей (порожній => PASS)."""
    exp = scenario.expect
    fails: List[str] = []
    joined = "\n".join(sender.texts)

    for slug, want in exp.best_attempt.items():
        got = flow.store.get_best_attempt(DAY, slug)
        if got != want:
            fails.append(f"best_attempt[{slug}]={got}, очікували {want}")

    if exp.done:
        prog = flow.store.get_day_progress(DAY)
        for slug, want in exp.done.items():
            if bool(prog.get(slug)) != want:
                fails.append(f"done[{slug}]={bool(prog.get(slug))}, очікували {want}")

    for rid, want in exp.credit.items():
        cr = _credit_state(flow, rid)
        for k, v in want.items():
            if cr.get(k) != v:
                fails.append(f"credit[{rid}].{k}={cr.get(k)}, очікували {v}")

    if exp.bot_msg_count is not None and len(sender.texts) != exp.bot_msg_count:
        fails.append(f"bot_msg_count={len(sender.texts)}, очікували {exp.bot_msg_count}")

    for sub in exp.must_contain:
        if sub not in joined:
            fails.append(f"у відповідях бота немає «{sub}»")
    low = joined.lower()
    for sub in list(exp.must_not_contain) + list(FORBIDDEN_IN_REPLY):
        if sub and sub.lower() in low:
            fails.append(f"у відповідях бота є заборонене «{sub}»")

    for slug in exp.dispute_wait_set:
        if not flow.store.get_state(f"dispute_wait:{slug}", ""):
            fails.append(f"dispute_wait[{slug}] порожній, мав бути встановлений")
    for slug in exp.dispute_wait_cleared:
        if flow.store.get_state(f"dispute_wait:{slug}", ""):
            fails.append(f"dispute_wait[{slug}] не очищено")

    for slug in exp.xp_zero:
        xp, _lvl = flow.store.get_xp(slug)
        if xp != 0:
            fails.append(f"xp[{slug}]={xp}, очікували 0 (settle не запускали)")

    return fails


# ================================================================== РЕЄСТР СЦЕНАРІЇВ
def _v(owner="lyosha", rid="req-ly", cv_reps=34, cv_exercise="pushup", **kw) -> VideoSpec:
    return VideoSpec(owner=owner, rid=rid, cv_reps=cv_reps, cv_exercise=cv_exercise, **kw)


SCENARIOS: List[Scenario] = [
    # ---------------- digits_vs_words ----------------
    Scenario(
        id="words_twenty_three", category="digits_vs_words",
        description="«двадцять три» словами замість цифр — зараховуємо 23.",
        videos=[_v(cv_reps=4)],
        turns=[Turn("lyosha", "двадцять три", tool=ToolCall("lyosha", 23, request_id="req-ly"))],
        expect=Expect(best_attempt={"lyosha": 23}, done={"lyosha": False},
                      credit={"req-ly": {"reps": 23, "disputed": True}},
                      must_contain=["23"], dispute_wait_cleared=["lyosha"], xp_zero=["lyosha"]),
    ),
    Scenario(
        id="words_half_hundred", category="digits_vs_words",
        description="«півсотні» => 50.",
        videos=[_v(cv_reps=4)],
        turns=[Turn("lyosha", "та там півсотні було, бот помилився", tool=ToolCall("lyosha", 50, request_id="req-ly"))],
        expect=Expect(best_attempt={"lyosha": 50}, done={"lyosha": True}, must_contain=["50"]),
    ),
    Scenario(
        id="words_forty_five", category="digits_vs_words",
        description="«сорок п'ять» => 45.",
        videos=[_v(cv_reps=4)],
        turns=[Turn("lyosha", "сорок п'ять зробив", tool=ToolCall("lyosha", 45, request_id="req-ly"))],
        expect=Expect(best_attempt={"lyosha": 45}, done={"lyosha": True}, must_contain=["45"]),
    ),
    Scenario(
        id="words_sotka", category="digits_vs_words",
        description="сленг «сотка» => 100.",
        videos=[_v(cv_reps=4)],
        turns=[Turn("lyosha", "та я сотку зробив", tool=ToolCall("lyosha", 100, request_id="req-ly"))],
        expect=Expect(best_attempt={"lyosha": 100}, done={"lyosha": True}, must_contain=["100"]),
    ),
    Scenario(
        id="approx_around_20", category="digits_vs_words",
        description="приблизність «десь 20» => 20 (нижча впевненість).",
        videos=[_v(cv_reps=4)],
        turns=[Turn("lyosha", "десь 20, не рахував точно", tool=ToolCall("lyosha", 20, request_id="req-ly", confidence=0.6))],
        expect=Expect(best_attempt={"lyosha": 20}, done={"lyosha": False}, must_contain=["20"]),
    ),
    Scenario(
        id="approx_about_30", category="digits_vs_words",
        description="приблизність «≈30» => 30.",
        videos=[_v(cv_reps=4)],
        turns=[Turn("lyosha", "ну ≈30 було", tool=ToolCall("lyosha", 30, request_id="req-ly", confidence=0.6))],
        expect=Expect(best_attempt={"lyosha": 30}, done={"lyosha": False}, must_contain=["30"]),
    ),

    # ---------------- negation ----------------
    Scenario(
        id="negation_dima_solidarity", category="negation",
        description="Діма: «50 там і не пахне, я 23 зробив у знак солідарності» => 23, не 50.",
        extra_users=["kovdimay"],
        videos=[_v(owner="kovdimay", rid="req-dima", cv_reps=50, cv_exercise="squat")],
        turns=[Turn("kovdimay", "Нє нє, 50 там і не пахне, я 23 зробив у знак солідарності із Сашком",
                    tool=ToolCall("kovdimay", 23, request_id="req-dima"))],
        expect=Expect(best_attempt={"kovdimay": 23}, credit={"req-dima": {"reps": 23, "ex": "pushups", "disputed": True}},
                      must_contain=["23"], must_not_contain=["50"]),
    ),
    Scenario(
        id="negation_not_50_but_23", category="negation",
        description="«не 50, а 23» => 23.",
        videos=[_v(cv_reps=50)],
        turns=[Turn("lyosha", "не 50, а 23", tool=ToolCall("lyosha", 23, request_id="req-ly"))],
        expect=Expect(best_attempt={"lyosha": 23}, must_contain=["23"], must_not_contain=["50"]),
    ),

    # ---------------- correction_lower / correction_higher ----------------
    Scenario(
        id="correction_lower_than_cv", category="correction_lower",
        description="CV 50 → людина 23 (нижче CV): спроба 23, день не закрито.",
        videos=[_v(cv_reps=50)],
        turns=[Turn("lyosha", "та ні, я 23 зробив", tool=ToolCall("lyosha", 23, request_id="req-ly"))],
        expect=Expect(best_attempt={"lyosha": 23}, done={"lyosha": False},
                      credit={"req-ly": {"reps": 23, "disputed": True}}, must_contain=["23"]),
    ),
    Scenario(
        id="correction_higher_than_cv", category="correction_higher",
        description="CV 34 → людина 36 (вище CV): норма 37 все одно не закрита, спроба 36.",
        videos=[_v(cv_reps=34)],
        turns=[Turn("lyosha", "я 36 зробив, а не 34", tool=ToolCall("lyosha", 36, request_id="req-ly"))],
        expect=Expect(best_attempt={"lyosha": 36}, done={"lyosha": False},
                      credit={"req-ly": {"reps": 36, "disputed": True}}, must_contain=["36"]),
    ),
    Scenario(
        id="correction_higher_meets_norm", category="correction_higher",
        description="CV 4 → людина 40 (вище норми 37): день закрито.",
        videos=[_v(cv_reps=4)],
        turns=[Turn("lyosha", "не 4 а 40", tool=ToolCall("lyosha", 40, request_id="req-ly"))],
        expect=Expect(best_attempt={"lyosha": 40}, done={"lyosha": True},
                      must_contain=["40", "зараховано"]),
    ),

    # ---------------- own_count ----------------
    Scenario(
        id="own_count_default_subject", category="own_count",
        description="Автор називає своє число про своє відео (subject за замовчуванням = автор).",
        videos=[_v(owner="sasha", rid="req-sa", cv_reps=40)],
        turns=[Turn("sasha", "та там 48 було", tool=ToolCall("sasha", 48, request_id="req-sa"))],
        expect=Expect(best_attempt={"sasha": 48}, done={"sasha": True}, must_contain=["48"]),
    ),
    Scenario(
        id="own_count_no_video_today", category="own_count",
        description="Число від учасника, у якого немає пораховано відео сьогодні — claim про повтори ігнорується.",
        videos=[_v(owner="lyosha", rid="req-ly", cv_reps=34)],
        turns=[Turn("sasha", "та я б і 60 витягнув", tool=ToolCall("sasha", 60, request_id=""))],
        expect=Expect(best_attempt={"lyosha": 34}, done={"sasha": False}),
        open_question="Саша без відео: claim ігнорується (_pick_target_check повертає None — немає відео).",
    ),

    # ---------------- third_party_reply_to ----------------
    Scenario(
        id="third_party_reply_zarahuj", category="third_party_reply_to",
        description="Саша відповідає на повідомлення бота про відео Льоші: «Ботя зараховуй 36» → Льоші 36.",
        videos=[_v(owner="lyosha", rid="req-ly", cv_reps=34), _v(owner="sasha", rid="req-sa", cv_reps=50)],
        turns=[Turn("sasha", "Я нарахував 36, ботя зараховуй 36",
                    reply_to="Побачив 34 віджимань з 37. Записав 34",
                    tool=ToolCall("lyosha", 36, request_id="req-ly"))],
        expect=Expect(best_attempt={"lyosha": 36, "sasha": 50}, done={"lyosha": False, "sasha": True},
                      credit={"req-ly": {"reps": 36, "disputed": True}},
                      must_contain=["36", "Льош"], dispute_wait_cleared=["lyosha"]),
    ),
    Scenario(
        id="third_party_reply_36_vyyshlo", category="third_party_reply_to",
        description="Саша відповідає на повідомлення бота: «36 вийшло» → Льоші 36 (власник відео).",
        videos=[_v(owner="lyosha", rid="req-ly", cv_reps=34), _v(owner="sasha", rid="req-sa", cv_reps=50)],
        turns=[Turn("sasha", "36 вийшло", reply_to="Побачив 34 віджимань з 37. Записав 34",
                    tool=ToolCall("lyosha", 36, request_id="req-ly"))],
        expect=Expect(best_attempt={"lyosha": 36, "sasha": 50}, must_contain=["36"]),
    ),

    # ---------------- third_party_by_name ----------------
    Scenario(
        id="third_party_by_name_zapysuy", category="third_party_by_name",
        description="«записуй Льоші 36» (без цитати) → Льоші 36, Сашу не чіпаємо.",
        videos=[_v(owner="lyosha", rid="req-ly", cv_reps=34), _v(owner="sasha", rid="req-sa", cv_reps=50)],
        turns=[Turn("sasha", "Лічильник недорахував у Льоші — записуй Льоші 36",
                    tool=ToolCall("lyosha", 36, request_id="req-ly"))],
        expect=Expect(best_attempt={"lyosha": 36, "sasha": 50}, must_contain=["36", "Льош"]),
    ),
    Scenario(
        id="third_party_by_name_cases", category="third_party_by_name",
        description="«Льоша зробив 36» (називний відмінок) → Льоші 36.",
        videos=[_v(owner="lyosha", rid="req-ly", cv_reps=34), _v(owner="sasha", rid="req-sa", cv_reps=50)],
        turns=[Turn("sasha", "Льоша зробив 36, зараховуй йому", tool=ToolCall("lyosha", 36, request_id="req-ly"))],
        expect=Expect(best_attempt={"lyosha": 36, "sasha": 50}, must_contain=["36"]),
    ),

    # ---------------- third_party_pending_question ----------------
    Scenario(
        id="pending_q_answered_by_other", category="third_party_pending_question",
        description="Бот спитав «Скільки в Льоші вийшло?», Саша відповідає «36 вийшло» → Льоші 36.",
        videos=[_v(owner="lyosha", rid="req-ly", cv_reps=34), _v(owner="sasha", rid="req-sa", cv_reps=50)],
        pending_question=PendingQuestion(subject="lyosha", rid="req-ly", asked_by="sasha"),
        turns=[Turn("sasha", "36 вийшло", tool=ToolCall("lyosha", 36, request_id="req-ly"))],
        expect=Expect(best_attempt={"lyosha": 36, "sasha": 50}, must_contain=["36"],
                      dispute_wait_cleared=["lyosha"]),
    ),
    Scenario(
        id="pending_q_answered_self", category="third_party_pending_question",
        description="Бот спитав Льошу «скільки вийшло?», Льоша відповідає «36» → Льоші 36.",
        videos=[_v(owner="lyosha", rid="req-ly", cv_reps=34)],
        pending_question=PendingQuestion(subject="lyosha", rid="req-ly", asked_by="lyosha"),
        turns=[Turn("lyosha", "36", tool=ToolCall("lyosha", 36, request_id="req-ly"))],
        expect=Expect(best_attempt={"lyosha": 36}, must_contain=["36"], dispute_wait_cleared=["lyosha"]),
    ),

    # ---------------- caption_or_text_before_video ----------------
    Scenario(
        id="caption_words", category="caption_or_text_before_video",
        description="Число словами у підписі до відео: «двадцять три віджимання» → 23.",
        videos=[_v(owner="lyosha", rid="req-c", cv_reps=5, caption="двадцять три віджимання",
                   claim_tool=ToolCall("lyosha", 23, exercise="pushups", corrects_existing_video=False, request_id="req-c"))],
        expect=Expect(best_attempt={"lyosha": 23}, credit={"req-c": {"reps": 23, "ex": "pushups"}},
                      must_contain=["23"]),
    ),
    Scenario(
        id="caption_not_a_count", category="caption_or_text_before_video",
        description="Число в підписі не про повтори (час/пробіжка) → зараховано CV, тул не викликаний.",
        videos=[_v(owner="lyosha", rid="req-n", cv_reps=12, caption="гарна погода, 5 хв пробіжки")],
        expect=Expect(best_attempt={"lyosha": 12}, credit={"req-n": {"reps": 12}}),
    ),

    # ---------------- exercise_mismatch ----------------
    Scenario(
        id="mismatch_cv_squat_person_pushups", category="exercise_mismatch",
        description="CV бачить присідання, людина каже «23 віджимання» (вправа дня) → 23 на віджимання.",
        videos=[_v(cv_reps=40, cv_exercise="squat")],
        turns=[Turn("lyosha", "то були віджимання, 23 їх", tool=ToolCall("lyosha", 23, exercise="pushups", request_id="req-ly"))],
        expect=Expect(best_attempt={"lyosha": 23}, done={"lyosha": False},
                      credit={"req-ly": {"reps": 23, "ex": "pushups", "disputed": True}}, must_contain=["23"]),
    ),
    Scenario(
        id="mismatch_cv_pushup_person_squats", category="exercise_mismatch",
        description="CV бачить віджимання (вправа дня), людина каже «23 присідання» → кредит іде на присідання, норму дня не закрито.",
        videos=[_v(cv_reps=40, cv_exercise="pushup")],
        turns=[Turn("lyosha", "то присідання були, 23", tool=ToolCall("lyosha", 23, exercise="squats", request_id="req-ly"))],
        expect=Expect(best_attempt={"lyosha": 0}, done={"lyosha": False},
                      credit={"req-ly": {"reps": 23, "ex": "squats"}}),
    ),

    # ---------------- no_number ----------------
    Scenario(
        id="no_number_ask_once", category="no_number",
        description="«бот помилився, порахуй ще раз» без числа → одне уточнення; друге таке саме — без повтору.",
        videos=[_v(cv_reps=30)],
        turns=[Turn("lyosha", "бот помилився, порахуй ще раз", tool=None),
               Turn("lyosha", "ну неправильно ж", tool=None)],
        expect=Expect(best_attempt={"lyosha": 30}, bot_msg_count=1,
                      must_contain=["Скільки в тебе вийшло"], dispute_wait_set=["lyosha"]),
    ),

    # ---------------- duplicate_reupload ----------------
    Scenario(
        id="duplicate_reupload_no_double", category="duplicate_reupload",
        description="Повторне завантаження того самого відео (той самий розмір/тривалість) не дає подвійного заліку.",
        videos=[
            _v(owner="sasha", rid="dup-1", cv_reps=48, file_unique_id="u1", file_size=9_000_000, duration_s=115.0),
            _v(owner="sasha", rid="dup-2", cv_reps=48, file_unique_id="u2", file_size=9_000_000, duration_s=115.0),
        ],
        expect=Expect(best_attempt={"sasha": 48}, credit={"dup-2": {}}, must_contain=["вже бачив"]),
        open_question="Дубль ловиться _reject_duplicate_video (same_size) на callback другого відео.",
    ),

    # ---------------- user_on_leave ----------------
    Scenario(
        id="leave_sick_correction_no_penalty", category="user_on_leave",
        description="Льоша на лікарняному: число виправляється, життя/XP/норма (settle) не змінюються, без докорів.",
        leaves=[("lyosha", "sick")],
        videos=[_v(cv_reps=30)],
        turns=[Turn("lyosha", "та я 36 зробив", tool=ToolCall("lyosha", 36, request_id="req-ly"))],
        expect=Expect(best_attempt={"lyosha": 36}, must_contain=["36"], xp_zero=["lyosha"]),
    ),

    # ---------------- day_already_settled ----------------
    Scenario(
        id="settled_day_dispute", category="day_already_settled",
        description="День уже пораховано (settle): автозміни немає, бот відповідає, що це шлях адміна (correct_settlement).",
        videos=[_v(cv_reps=34)],
        settle_before=True,
        turns=[Turn("lyosha", "я 40 зробив", tool=ToolCall("lyosha", 40, request_id="req-ly"))],
        expect=Expect(best_attempt={"lyosha": 34}, must_contain=["адмін"]),
    ),

    # ---------------- joking_sarcasm ----------------
    Scenario(
        id="joke_thousand", category="joking_sarcasm",
        description="Явний жарт «та я сьогодні 1000 віджимань зробив 😂» — політика з коду: LLM однаково зафіксує 1000 "
                    "(ідеальний LLM за правилами ядра бере заявлене число; гумор — не привід підозрювати).",
        videos=[_v(cv_reps=34)],
        turns=[Turn("lyosha", "та я сьогодні 1000 віджимань зробив 😂", tool=ToolCall("lyosha", 1000, exercise="pushups", request_id="req-ly"))],
        expect=Expect(best_attempt={"lyosha": 1000}, done={"lyosha": True}, must_contain=["1000"]),
        open_question="Відкрите питання політики жартів: ядро не має спец-правила на сарказм; закріплено «береться заявлене число».",
    ),
    Scenario(
        id="joke_norm_vs_done", category="joking_sarcasm",
        description="«норма 50? та я 23 зробив» — 50 це норма (не повтори), береться 23.",
        videos=[_v(cv_reps=4)],
        turns=[Turn("lyosha", "норма 50? та я 23 зробив", tool=ToolCall("lyosha", 23, request_id="req-ly"))],
        expect=Expect(best_attempt={"lyosha": 23}, must_contain=["23"], must_not_contain=["50"]),
    ),

    # ---------------- two_people_disagree ----------------
    Scenario(
        id="two_people_disagree_last_wins", category="two_people_disagree",
        description="Саша каже 36, Льоша каже 34 про відео Льоші. Політика коду: приймається ОСТАННЄ слово (34).",
        videos=[_v(owner="lyosha", rid="req-ly", cv_reps=34), _v(owner="sasha", rid="req-sa", cv_reps=50)],
        turns=[Turn("sasha", "записуй Льоші 36", tool=ToolCall("lyosha", 36, request_id="req-ly")),
               Turn("lyosha", "та ні, 34 було насправді", tool=ToolCall("lyosha", 34, request_id="req-ly"))],
        expect=Expect(best_attempt={"lyosha": 34}, credit={"req-ly": {"reps": 34, "disputed": True}}),
        open_question="Політики пріоритету власника немає — закріплено «останнє слово приймається» (ВІДКРИТЕ ПИТАННЯ).",
    ),

    # ---------------- flood_idempotent ----------------
    Scenario(
        id="flood_same_correction", category="flood_idempotent",
        description="П'ять однакових виправлень «40» підряд → одна й та сама спроба 40, без подвоєння XP (settle не чіпали).",
        videos=[_v(cv_reps=4)],
        turns=[Turn("lyosha", "не 4 а 40", tool=ToolCall("lyosha", 40, request_id="req-ly")) for _ in range(5)],
        expect=Expect(best_attempt={"lyosha": 40}, done={"lyosha": True}, xp_zero=["lyosha"]),
        open_question="Ідемпотентність стану: best стабільний, XP рахує лише settle від best_reps (без подвоєння).",
    ),

    # ---------------- not_a_count ----------------
    Scenario(
        id="not_a_count_norm", category="not_a_count",
        description="«норма 36» — це норма, не повтори → тул не викликати, нічого не змінюється.",
        videos=[_v(cv_reps=34)],
        turns=[Turn("lyosha", "а яка в мене норма? 36?", tool=None)],
        expect=Expect(best_attempt={"lyosha": 34}, credit={"req-ly": {}}),
    ),
    Scenario(
        id="not_a_count_sets", category="not_a_count",
        description="«це 3 підходи» — підходи, не повтори → тул не викликати.",
        videos=[_v(cv_reps=34)],
        turns=[Turn("lyosha", "та то я за 3 підходи робив", tool=None)],
        expect=Expect(best_attempt={"lyosha": 34}),
    ),

    # ---------------- tone_no_suspicion ----------------
    Scenario(
        id="tone_after_accept_no_doubt", category="tone_no_suspicion",
        description="Після прийнятого числа відповідь бота не містить сумніву/докору/вердикту CV.",
        videos=[_v(cv_reps=4)],
        turns=[Turn("lyosha", "не 4 а 40", tool=ToolCall("lyosha", 40, request_id="req-ly"))],
        expect=Expect(best_attempt={"lyosha": 40},
                      must_contain=["вірю"], must_not_contain=["лічильник правий", "точно?", "доказ"]),
    ),
]


# ================================================================== приклади ядра -> сценарії (інваріант f)
_SLUG_RE = re.compile(r"slug\s+([a-z_]+)")


def _example_speaker_slug(ex: dict) -> str:
    m = _SLUG_RE.search(ex.get("speaker", ""))
    return m.group(1) if m else "sasha"


def example_to_scenario(ex: dict) -> Scenario:
    """Перетворює один приклад trust_examples.json на детермінований сценарій (той самий runner/verify)."""
    speaker = _example_speaker_slug(ex)
    tc = ex.get("tool_call")
    rid = f"ex-{ex['id']}"
    subject = (tc or {}).get("subject_user") or speaker
    subject = subject or speaker
    count = (tc or {}).get("count")
    ex_name = (tc or {}).get("exercise") or ""

    # CV-число, відмінне від заявленого, щоб корекція була видимою; вправа CV — віджимання (вправа дня).
    cv_reps = (count + 7) if (count and count + 7 <= 300) else 55
    if count and cv_reps == count:
        cv_reps = count + 3

    extra = [s for s in {speaker, subject} if s not in ("lyosha", "sasha")]
    video = VideoSpec(owner=subject, rid=rid, cv_reps=cv_reps, cv_exercise="pushup")

    pending = None
    if ex.get("pending_question"):
        pending = PendingQuestion(subject=subject, rid=rid, asked_by=speaker)

    tool = None
    if tc is not None:
        tool = ToolCall(subject_user=subject, count=int(count), exercise=ex_name, request_id=rid)
    turn = Turn(author=speaker, text=ex["message"], reply_to=ex.get("replied_to"), tool=tool)

    exp = Expect()
    if tc is None:
        exp.best_attempt = {subject: cv_reps}
        exp.credit = {rid: {}}            # не позначено disputed
        exp.tool_called = False
    elif ex_name == "squats":
        exp.credit = {rid: {"reps": int(count), "ex": "squats"}}
        exp.tool_called = True
    else:
        exp.best_attempt = {subject: int(count)}
        exp.credit = {rid: {"reps": int(count), "disputed": True}}
        exp.tool_called = True

    return Scenario(
        id=f"example_{ex['id']}", category="trust_example", description=ex.get("comment", ex["id"]),
        extra_users=extra, videos=[video], pending_question=pending, turns=[turn], expect=exp,
    )


def examples_as_scenarios() -> List[Scenario]:
    return [example_to_scenario(ex) for ex in trust_core.load_examples()]
