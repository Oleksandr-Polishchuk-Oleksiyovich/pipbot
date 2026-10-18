"""
Оркестратор флоу бота. Не залежить від aiogram: усе спілкування з Telegram — через Sender,
тому той самий код ганяється в інтеграційних тестах з фейковим Sender.

push / startup:  нові повідомлення ─┬─ програмна перевірка: автор відсутній у БД або має status='uninitialized'
                                    │   (і без кодової фрази адміна) → OnboardingAgent (промпт 'onboarding' із БД)
                                    └─ інакше RouterAgent ─┬─ admin  → AdminAgent (тули + редактор промпту тренера)
                                                           ├─ coach  → ReportJudgeAgent (запис у БД) → CoachAgent
                                                           └─ ignore → лише зсув мітки
timer:           morning / afternoon / evening — шаблони; night — CoachAgent; settle — списання життів за вчора
"""
import asyncio
import glob
import logging
import os
import random
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Protocol

from agents.admin import AdminAgent
from agents.coach import CoachAgent
from agents.onboarding import OnboardingAgent
from agents.prompt_editor import CoachPromptEditor, PromptConflictAgent, PromptMergerAgent, PromptVerifierAgent
from agents.report_judge import JudgeVerdict, ReportJudgeAgent
from agents.router import RouteDecision, RouterAgent, has_passphrase
from core import challenge
from core.config import OBEY_MEDIA_DIR, TIMEZONE
from core.llm import GeminiLLM, LLMUnavailable
from database.store import ChatMessage, Store, User, format_ts
from migrate_users import ensure_default_users, migrate_dynamic_prompt
from prompts import DEFAULT_PROMPTS, FALLBACK_ADMIN_MESSAGE, FALLBACK_GEMINI_MESSAGE

logger = logging.getLogger(__name__)

COACH_NAME = "Спорт Зануда"


class Sender(Protocol):
    async def send_text(self, chat_id: int, text: str) -> None: ...
    async def send_video(self, chat_id: int, path: str) -> None: ...


class ChatFlow:
    def __init__(self, store: Store, llm: Optional[GeminiLLM], sender: Sender,
                 now: Callable[[], datetime] = lambda: datetime.now(TIMEZONE),
                 obey_media_dir: str = OBEY_MEDIA_DIR):
        self.store = store
        self.llm = llm
        self.sender = sender
        self.now = now
        self.obey_media_dir = obey_media_dir
        self._lock = asyncio.Lock()
        self._chat_members_cache: Dict = {}

        self.router = RouterAgent(llm, store)
        self.onboarding = OnboardingAgent(llm, store)
        self.judge = ReportJudgeAgent(llm, store)
        self.coach = CoachAgent(llm, store, chat_members_fetcher=lambda: self._chat_members_cache)
        self.editor = CoachPromptEditor(PromptConflictAgent(llm, store), PromptMergerAgent(llm, store),
                                        PromptVerifierAgent(llm, store))
        self.admin = AdminAgent(llm, store, self.editor, chat_members_fetcher=lambda: self._chat_members_cache)

    def init_storage(self) -> None:
        self.store.init_schema()
        ensure_default_users(self.store)
        seeded = self.store.seed_prompts(DEFAULT_PROMPTS)
        if seeded:
            logger.info("Seeded prompts: %s", seeded)
        migrate_dynamic_prompt(self.store)
        if not self.store.get_state("last_processed_timestamp"):
            self.store.set_state("last_processed_timestamp", self._yesterday_cutoff())

    # --- допоміжне ---
    def _yesterday_cutoff(self) -> str:
        return (self.now() - timedelta(days=1)).strftime("%Y-%m-%d 23:55:00")

    def _day_start(self) -> str:
        return self.now().strftime("%Y-%m-%d 00:00:00")

    async def _call(self, fn, *args):
        if self.llm is None:
            raise LLMUnavailable("Gemini client is not configured")
        return await asyncio.to_thread(fn, *args)

    async def _say(self, chat_id: int, text: str) -> None:
        self.store.save_message(chat_id, COACH_NAME, "coach", text, format_ts(self.now()))
        await self.sender.send_text(chat_id, text)

    async def _refresh_chat_members(self, chat_id: int) -> None:
        fetch_fn = getattr(self.sender, "fetch_chat_members", None)
        if callable(fetch_fn):
            try:
                self._chat_members_cache = await fetch_fn(chat_id)
            except Exception as e:
                logger.debug("Could not fetch chat members from Telegram: %s", e)

    def record_user_message(
        self,
        chat_id: int,
        user_name: str,
        text: str,
        created_at: Optional[datetime] = None,
        telegram_id: Optional[int] = None,
        username: Optional[str] = None,
    ) -> User:
        if not self.store.list_users():
            ensure_default_users(self.store)
        dt = created_at or self.now()
        user = self.store.resolve_or_create_user(
            user_name,
            dt.date().isoformat(),
            telegram_id=telegram_id,
            username=username,
        )
        self.store.save_message(chat_id, user_name, "user", text, format_ts(dt))
        return user

    # --- головна точка входу ---
    async def process(self, chat_id: int, trigger: str = "push", job: str = "") -> None:
        async with self._lock:
            last_ts = self.store.get_state("last_processed_timestamp") or self._yesterday_cutoff()
            pending = self.store.unprocessed_user_messages(chat_id, last_ts, self._day_start())
            if pending:
                logger.info("[%s] %d unprocessed message(s) since %s", trigger, len(pending), last_ts)
                await self._refresh_chat_members(chat_id)
                await self._handle_pending(chat_id, pending)
                self.store.set_state("last_processed_timestamp", pending[-1].created_at)
            elif trigger == "timer":
                await self._handle_timer(chat_id, job)

    async def _handle_pending(self, chat_id: int, pending: List[ChatMessage]) -> None:
        today_iso = self.now().date().isoformat()
        history = self.store.messages_since(chat_id, self._day_start())

        # Програмна перевірка авторів: якщо автора немає в БД — він створюється як 'uninitialized'.
        uninit_by_slug: Dict[str, tuple[User, List[ChatMessage]]] = {}
        active_pending: List[ChatMessage] = []
        for m in pending:
            u = self.store.resolve_or_create_user(m.user_name, today_iso)
            if u.status == "uninitialized" and not has_passphrase(m.text):
                if u.slug not in uninit_by_slug:
                    uninit_by_slug[u.slug] = (u, [])
                uninit_by_slug[u.slug][1].append(m)
            else:
                active_pending.append(m)

        # Якщо є повідомлення від неактуалізованих юзерів — програмно ведемо їх через OnboardingAgent.
        for slug, (user, user_msgs) in uninit_by_slug.items():
            await self._handle_onboarding(chat_id, user, user_msgs, history)

        if not active_pending:
            return

        try:
            decision = await self._call(self.router.decide, active_pending, history)
        except LLMUnavailable as e:
            logger.warning("Router unavailable (%s) — keyword fallback", e)
            decision = RouterAgent.fallback(active_pending)
        logger.info("Route decision: %s", decision.model_dump())

        if decision.route == "admin":
            await self._handle_admin(chat_id, decision, active_pending, history)
        elif decision.route == "coach":
            await self._handle_coach(chat_id, active_pending, history)

    async def _handle_onboarding(
        self,
        chat_id: int,
        user: User,
        pending: List[ChatMessage],
        history: List[ChatMessage],
    ) -> None:
        today = self.now().date()
        try:
            reply, _ = await self._call(self.onboarding.interview, today, user, pending, history)
        except LLMUnavailable as e:
            logger.warning("Onboarding agent unavailable (%s) — deterministic fallback", e)
            reply = OnboardingAgent.fallback(self.store, user, pending, today)
        await self._say(chat_id, reply)

    async def _handle_admin(self, chat_id: int, decision: RouteDecision, pending: List[ChatMessage],
                            history: List[ChatMessage]) -> None:
        await self._send_obey_video(chat_id)
        try:
            reply, _ = await self._call(self.admin.execute, self.now().date(), decision.directive,
                                        decision.sender, pending, history)
        except LLMUnavailable as e:
            logger.error("Admin agent unavailable: %s", e)
            reply = FALLBACK_ADMIN_MESSAGE
        await self._say(chat_id, reply)

    async def _handle_coach(self, chat_id: int, pending: List[ChatMessage], history: List[ChatMessage]) -> None:
        today = self.now().date()
        done_before = self.store.get_day_progress(today.isoformat())
        llm_ok = True
        try:
            verdict = await self._call(self.judge.judge, pending, history, done_before)
        except LLMUnavailable as e:
            logger.warning("Judge unavailable (%s) — heuristic fallback", e)
            verdict, llm_ok = ReportJudgeAgent.fallback(pending, done_before, self.store), False

        notes = self._apply_verdict(verdict, done_before, today)

        reply = None
        if llm_ok:
            try:
                reply = await self._call(self.coach.reply, today, self.store.messages_since(chat_id, self._day_start()), notes)
            except LLMUnavailable as e:
                logger.warning("Coach unavailable: %s", e)
        await self._say(chat_id, reply or self._template_reply(notes, today))

    def _apply_verdict(self, verdict: JudgeVerdict, done_before: dict, today) -> List[str]:
        notes = []
        targets = challenge.day_targets(self.store, today)
        pmap = challenge.participants_map(self.store)
        exercise = challenge.EXERCISES[challenge.day_exercise(self.store, today)]["genitive"]
        for r in verdict.reports:
            if r.participant not in pmap:
                continue
            if done_before.get(r.participant) == r.done:
                continue
            self.store.set_done(r.participant, r.done, today.isoformat())
            name = pmap[r.participant]["name"]
            if r.done:
                notes.append(f"{name}: ЗАРАХОВАНО норму {targets[r.participant]} {exercise} (доказ: {r.evidence})")
            else:
                notes.append(f"{name}: зарахування СКАСОВАНО (причина: {r.evidence})")
            logger.info("Progress updated: %s done=%s (%s)", r.participant, r.done, r.evidence)
        return notes

    @staticmethod
    def _template_reply(notes: List[str], today) -> str:
        if notes:
            return "🦾 **Зануда на зв'язку!**\n\n" + "\n".join(f"✅ {n.split(' (')[0]}" for n in notes) + "\n\nТак тримати! 👊"
        return FALLBACK_GEMINI_MESSAGE

    async def _send_obey_video(self, chat_id: int) -> None:
        if not os.path.isdir(self.obey_media_dir):
            return
        videos = [p for ext in ("mp4", "mov", "gif") for p in glob.glob(os.path.join(self.obey_media_dir, f"*.{ext}"))]
        if videos:
            try:
                await self.sender.send_video(chat_id, random.choice(videos))
            except Exception as e:
                logger.error("Failed to send obey video: %s", e)

    # --- таймери ---
    async def _handle_timer(self, chat_id: int, job: str) -> None:
        today = self.now().date()
        if job == "settle":
            await self._settle_lives(chat_id, challenge.yesterday(today))
            return

        missing = challenge.missing_participants(self.store, today)
        if not missing:
            logger.info("Timer %s: all active participants are done — skipping reminder", job)
            return
        targets = challenge.day_targets(self.store, today)
        ex = challenge.EXERCISES[challenge.day_exercise(self.store, today)]
        p = challenge.participants_map(self.store)
        missing_str = [f"{p[m]['emoji']} {p[m]['name']} (`{targets[m]}` {ex['genitive']})" for m in missing if m in p]

        if job == "morning":
            norms_lines = "\n".join(f"{meta['emoji']} {meta['name']}: `{targets[slug]}`" for slug, meta in p.items())
            text = (f"☀️ **День {challenge.day_index(today) + 1} ({today.strftime('%d.%m.%Y')})**\n\n"
                    f"{ex['emoji']} **Сьогодні {ex['name'].upper()}!**\n\n"
                    f"🎯 **Норма на сьогодні:**\n{norms_lines}\n\n"
                    "Чекаю на звіти! 💪")
        elif job == "afternoon":
            text = ("🔔 **Денний чек-поінт (15:00):**\nЩе винні:\n" + "\n".join(missing_str)
                    + "\n\nЗробіть підхід зараз, не відкладайте! 🏃‍♂️")
        elif job == "evening":
            text = ("🚨 **Вечірнє нагадування (19:00):**\nЧекаю звіт від: " + ", ".join(missing_str)
                    + ".\nДотискаємо до сну! 🧘‍♂️✅")
        elif job == "night":
            try:
                text = await self._call(self.coach.night_reminder, today, missing)
            except LLMUnavailable:
                text = ("😭 **Караул, до півночі лишилось 30 хвилин!**\n\n"
                        f"Ще не закрили день: {', '.join(missing_str)}.\n"
                        "Невже челендж зірветься? Чи ще встигнете закрити норму до 00:00? 🥺⏱️")
        else:
            logger.warning("Unknown timer job: %s", job)
            return
        await self._say(chat_id, text)
        self.store.set_state("last_processed_timestamp", format_ts(self.now()))

    async def _settle_lives(self, chat_id: int, day) -> None:
        changed = challenge.settle_missed_day(self.store, day)
        if not changed:
            return
        pmap = challenge.participants_map(self.store)
        lines = [
            f"💔 **{pmap.get(p, {}).get('name', p)}** втрачає життя → залишилось **{v}** ❤️"
            for p, v in changed.items()
        ]
        dead = [pmap.get(p, {}).get("name", p) for p, v in changed.items() if v == 0]
        text = f"⚰️ **Розрахунок за {day.strftime('%d.%m.%Y')}:**\n\n" + "\n".join(lines)
        if dead:
            text += f"\n\n💀 **{', '.join(dead)} — нуль життів. Дискваліфікація!**"
        await self._say(chat_id, text)
