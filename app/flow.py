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
import json
import logging
import re
import os
import random
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

import pytz

from agents.admin import AdminAgent
from agents.coach import CoachAgent
from agents.onboarding import OnboardingAgent
from agents.prompt_editor import CoachPromptEditor, PromptConflictAgent, PromptMergerAgent, PromptVerifierAgent
from agents.reps_claim import RepsClaim, RepsClaimAgent, last_claim_for
from agents.report_judge import JudgeVerdict, ReportJudgeAgent, VideoFact, VideoVerdict
from agents.router import RouteDecision, RouterAgent, has_passphrase
from app.offload import RoutineOffloadClient
from core import challenge, dayroll, dedup, progression
from core.bot_texts import BotTexts
from core.claims import (
    is_dispute,
    looks_like_count,
    parse_claimed_exercise,
    parse_claimed_reps,
    parse_dispute_reps,
    split_reply_context,
)
from core.config import (
    ADMIN_TASK_STALE_HOURS,
    CV_OFFLOAD,
    CV_STILL_COUNTING_MIN,
    DB_PATH,
    IS_PRODUCTION,
    LOG_RETENTION_DAYS,
    OBEY_MEDIA_DIR,
    TIMEZONE,
    TRUST_MODE,
    DAY_ROLLOVER_HOUR,
    DAY_ROLLOVER_MINUTE,
)
from core.guardrails import (
    check_active_request_limit,
    check_admin_rate_limit,
    is_admin_allowed,
    is_forbidden_path,
)
from core.llm import GeminiLLM, LLMUnavailable
from database.store import ChatMessage, Store, User, format_ts
from migrate_users import ensure_default_users, migrate_dynamic_prompt
from prompts import (
    DEFAULT_BOT_TEXTS,
    DEFAULT_PROMPTS,
    FALLBACK_ADMIN_MESSAGE,
    FALLBACK_GEMINI_MESSAGE,
    REDO_WORDS,
    build_video_reply_tone_prompt,
)

logger = logging.getLogger(__name__)

COACH_NAME = "Спорт Зануда"
VIDEO_REPLY_PARAPHRASE_SYSTEM = (
    "Ти — «Спорт Зануда», суворий і дотепний спортивний AI-тренер. "
    "Перефразуй наданий тон-зразок відповіді на відеозвіт за правилами нижче."
)


class Sender(Protocol):
    async def send_text(self, chat_id: int, text: str, **kwargs) -> None: ...
    async def send_video(self, chat_id: int, path: str) -> None: ...



def _short_task_text(text: Optional[str], limit: int = 300) -> str:
    """Для анонсів: лише суть задачі (до блоку «--- повний текст …»), обрізана."""
    head = (text or "").split("\n\n--- повний текст", 1)[0].strip()
    return head if len(head) <= limit else head[: limit - 1] + "…"


def load_release_commits(release_dir: str) -> Optional[set]:
    """SHA задеплоєного релізу + його історія (REVISION, REVISION_COMMITS від deploy.sh). None — файлів немає."""
    commits = set()
    found = False
    for name in ("REVISION", "REVISION_COMMITS"):
        path = os.path.join(release_dir, name)
        if os.path.isfile(path):
            found = True
            with open(path, encoding="utf-8") as f:
                commits.update(line.strip().lower() for line in f if line.strip())
    return commits if found else None


def commit_is_deployed(fix_commit: Optional[str], deployed: Optional[set]) -> bool:
    if deployed is None:
        return True  # fallback: невідомо, що задеплоєно — анонсуємо
    fc = (fix_commit or "").strip().lower()
    if len(fc) < 7:
        return False
    return any(c.startswith(fc) or fc.startswith(c) for c in deployed if len(c) >= 7)

_EX_UNSET = object()  # «вправу не передано — розібрати regex-ом»


DEDUP_WINDOW_H = 48.0  # вікно пошуку дублів відео того самого автора, год


class ChatFlow:
    def __init__(self, store: Store, llm: Optional[GeminiLLM], sender: Sender,
                 now: Callable[[], datetime] = lambda: datetime.now(TIMEZONE),
                 obey_media_dir: str = OBEY_MEDIA_DIR,
                 offload_client: Optional[RoutineOffloadClient] = None,
                 trust_mode: Optional[bool] = None,
                 rollover: Optional[Tuple[int, int]] = None,
                 waker: Optional[Any] = None):
        self.store = store
        self.llm = llm
        self.sender = sender
        self.now = now
        self.obey_media_dir = obey_media_dir
        self.offload_client = offload_client or RoutineOffloadClient(store, now_fn=self.now)
        self.bot_texts = BotTexts(store)
        self.cv_offload = CV_OFFLOAD
        self.trust_mode = TRUST_MODE if trust_mode is None else bool(trust_mode)
        # межа дня челенджу (год, хв, Europe/Kyiv): відео до неї належать попередньому дню
        self.rollover = tuple(rollover) if rollover is not None else (DAY_ROLLOVER_HOUR, DAY_ROLLOVER_MINUTE)
        self._lock = asyncio.Lock()
        self._chat_members_cache: Dict = {}
        # пробудження box перед відео (core.wake.BoxWaker); None — вимкнено
        self.waker = waker
        self._wake_tasks: set = set()

        self.router = RouterAgent(llm, store)
        self.onboarding = OnboardingAgent(llm, store)
        self.judge = ReportJudgeAgent(llm, store)
        self.claims_agent = RepsClaimAgent(llm, store)
        self.coach = CoachAgent(llm, store, chat_members_fetcher=lambda: self._chat_members_cache)
        self.editor = CoachPromptEditor(PromptConflictAgent(llm, store), PromptMergerAgent(llm, store),
                                        PromptVerifierAgent(llm, store))
        self.admin = AdminAgent(
            llm,
            store,
            self.editor,
            chat_members_fetcher=lambda: self._chat_members_cache,
            offload_client=self.offload_client,
            bot_texts=self.bot_texts,
        )

    def init_storage(self) -> None:
        self.store.init_schema()
        if IS_PRODUCTION:
            if not self.store.list_users():
                logger.error(
                    "Users table empty in production DB (%s) — refusing to auto-seed default "
                    "participants at startup. Run migrate_users.py explicitly or restore from backup.",
                    DB_PATH,
                )
        else:
            ensure_default_users(self.store)
        seeded = self.store.seed_prompts(DEFAULT_PROMPTS)
        if seeded:
            logger.info("Seeded prompts: %s", seeded)
        seeded_texts = self.store.seed_bot_texts(DEFAULT_BOT_TEXTS)
        if seeded_texts:
            logger.info("Seeded bot_texts: %s", seeded_texts)
            self.bot_texts.invalidate()
        migrate_dynamic_prompt(self.store)
        if not self.store.get_state("last_processed_timestamp"):
            self.store.set_state("last_processed_timestamp", self._yesterday_cutoff())
        if not self.store.get_state("last_processed_id"):
            self.store.set_state("last_processed_id", "0")

        deleted = self.store.prune_old_logs(self._log_retention_cutoff())
        if any(deleted.values()):
            logger.info("Startup log pruning (older than %dd): %s", LOG_RETENTION_DAYS, deleted)

        today = self._today()
        logger.info(
            "Startup: db_path=%s participants=%d schedule_mode=%s today_exercise=%s",
            os.path.abspath(DB_PATH),
            len(self.store.list_users(status="active")),
            self.store.get_state("schedule_mode", "ladder"),
            challenge.day_exercise(self.store, today),
        )

    async def announce_admin_tasks(self, chat_id: int, startup: bool = False,
                                   release_dir: Optional[str] = None) -> int:
        """
        Анонси по admin_tasks. Лише на старті (після деплою): done_pending_deploy, чий fix_commit входить у
        задеплоєний реліз (REVISION / REVISION_COMMITS у каталозі релізу; якщо файлів немає — анонсуємо)
        -> «Пропатчився…», status=announced. Rejected (ще не повідомлені) — і на старті, і на кожному тіку.
        """
        posted = 0
        if startup:
            deployed = load_release_commits(release_dir or os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            for task in self.store.list_admin_tasks(status="done_pending_deploy"):
                if not commit_is_deployed(task.get("fix_commit"), deployed):
                    logger.info("admin_task #%s: fix_commit %s not in deployed release yet", task["id"], task.get("fix_commit"))
                    continue
                text = self.bot_texts.get("admin_task_patched", requester=task.get("requester_name") or "",
                                          text=_short_task_text(task.get("text")), id=task["id"])
                try:
                    await self._say(task.get("chat_id") or chat_id, text)
                except Exception as e:
                    logger.warning("admin_task #%s announce failed: %s", task["id"], e)
                    continue
                self.store.mark_admin_task_announced(task["id"])
                logger.info("admin_task #%s announced (fix_commit=%s)", task["id"], task.get("fix_commit"))
                posted += 1
        for task in self.store.list_admin_tasks(status="rejected"):
            if task.get("notified"):
                continue
            text = self.bot_texts.get("admin_task_rejected", requester=task.get("requester_name") or "",
                                      text=_short_task_text(task.get("text")), note=task.get("result") or "", id=task["id"])
            try:
                await self._say(task.get("chat_id") or chat_id, text)
            except Exception as e:
                logger.warning("admin_task #%s reject notice failed: %s", task["id"], e)
                continue
            self.store.mark_admin_task_notified(task["id"])
            logger.info("admin_task #%s rejection notified", task["id"])
            posted += 1
        # Довгі задачі НЕ скасовуються: раз на ADMIN_TASK_STALE_HOURS — «ще в роботі».
        for task in self.store.list_stale_admin_tasks(ADMIN_TASK_STALE_HOURS):
            key = f"admin_task_stale_{task['id']}"
            last = self.store.get_state(key, "")
            now = self.now()
            if last:
                try:
                    if (now - datetime.fromisoformat(last)).total_seconds() < ADMIN_TASK_STALE_HOURS * 3600:
                        continue
                except ValueError:
                    pass
            text = self.bot_texts.get("admin_task_still_working", id=task["id"],
                                      requester=task.get("requester_name") or "", text=_short_task_text(task.get("text")))
            try:
                await self._say(task.get("chat_id") or chat_id, text)
            except Exception as e:
                logger.warning("admin_task #%s stale notice failed: %s", task["id"], e)
                continue
            self.store.set_state(key, now.isoformat())
            logger.info("admin_task #%s still in progress notice", task["id"])
            posted += 1
        return posted

    async def prune_old_logs(self) -> Dict[str, int]:
        """Щоденне прибирання рядків старших за LOG_RETENTION_DAYS днів (планується через scheduler)."""
        deleted = self.store.prune_old_logs(self._log_retention_cutoff())
        if any(deleted.values()):
            logger.info("Daily log pruning (older than %dd): %s", LOG_RETENTION_DAYS, deleted)
        return deleted

    # --- допоміжне ---
    def _today(self):
        """Поточний ДЕНЬ ЧЕЛЕНДЖУ (не календарна дата): до межі rollover (02:30) ще триває попередній день."""
        return dayroll.challenge_date(self.now(), *self.rollover)

    def _video_day(self, check):
        """
        ДЕНЬ ЧЕЛЕНДЖУ, до якого належить відео (за часом його надходження, а не обробки): якщо результат прийшов
        після rollover (чергу затримало), спроба все одно зараховується дню, коли відео надіслали.
        """
        try:
            utc_dt = datetime.strptime((check.created_at or "").split(".")[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=pytz.UTC)
            vday = dayroll.challenge_date(utc_dt.astimezone(TIMEZONE), *self.rollover)
        except Exception:
            return self._today()
        return min(vday, self._today())  # відео не може належати майбутньому дню

    def _yesterday_cutoff(self) -> str:
        return (self.now() - timedelta(days=1)).strftime("%Y-%m-%d 23:55:00")

    def _log_retention_cutoff(self) -> str:
        return (self.now() - timedelta(days=LOG_RETENTION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")

    def _day_start(self) -> str:
        return dayroll.day_start_ts(self._today(), *self.rollover)

    async def _call(self, fn, *args):
        if self.llm is None:
            raise LLMUnavailable("Gemini client is not configured")
        return await asyncio.to_thread(fn, *args)

    async def _say(self, chat_id: int, text: str) -> None:
        self.store.save_message(chat_id, COACH_NAME, "coach", text, format_ts(self.now()))
        await self.sender.send_text(chat_id, text)

    async def _say_reply(self, chat_id: int, text: str, reply_to_message_id: Optional[int] = None) -> None:
        self.store.save_message(chat_id, COACH_NAME, "coach", text, format_ts(self.now()))
        try:
            if reply_to_message_id is not None:
                await self.sender.send_text(chat_id, text, reply_to_message_id=reply_to_message_id)
            else:
                await self.sender.send_text(chat_id, text)
        except TypeError:
            await self.sender.send_text(chat_id, text)

    async def handle_video(
        self,
        chat_id: int,
        user: User,
        message_id: int,
        *,
        is_animation: bool = False,
        file_id: Optional[str] = None,
        file_unique_id: Optional[str] = None,
        duration: Optional[float] = None,
        file_size: Optional[int] = None,
        caption: Optional[str] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        file_name: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """
        Gate для вхідного відео/GIF (T-30):
        1. reject animation/GIF
        2. reject duration < 5s
        3. reject file_size > 20MB
        4. dedup: file_unique_id; метадані (той самий розмір+кадр+тривалість під новим id); далі, після
           підрахунку, — відбиток box CV (core/dedup.py; повторне завантаження/перекодування)
        5. passed -> INSERT video_checks(status='queued'), ack «Беру попкорну і йду дивитись відео»
        """
        if is_animation:
            reply = "Це гіфка або анімація, а не відеозвіт — таке не рахую."
            await self._say_reply(chat_id, reply, reply_to_message_id=message_id)
            return False, reply

        if duration is not None and duration < 5:
            reply = "Відео коротше 5 секунд — з такого підхід не порахувати."
            await self._say_reply(chat_id, reply, reply_to_message_id=message_id)
            return False, reply

        if file_size is not None and file_size > 20 * 1024 * 1024:
            reply = "Відео більше 20MB — Telegram не дасть його завантажити. Стисни це саме відео до меншого розміру — тоді гляну."
            await self._say_reply(chat_id, reply, reply_to_message_id=message_id)
            return False, reply

        if file_unique_id:
            existing = self.store.get_video_check_by_file_unique_id(file_unique_id)
            if existing is not None:
                reply = self._duplicate_text()
                await self._say_reply(chat_id, reply, reply_to_message_id=message_id)
                return False, reply

        request_id = str(uuid.uuid4())
        meta = dict(file_unique_id=file_unique_id, file_size=file_size, width=width, height=height,
                    duration_s=float(duration) if duration is not None else None, file_name=file_name)
        try:
            # Той самий файл під новим id: збігаються розмір у байтах, кадр і тривалість (за 48 год, той самий автор).
            for old in self.store.recent_video_metas(user.slug, DEDUP_WINDOW_H):
                dup, why = dedup.is_duplicate(meta, old)
                if dup and why in ("same_size", "file_unique_id"):
                    logger.info("Duplicate video at receipt (%s) of %s for %s", why, old["request_id"][:8], user.slug)
                    reply = self._duplicate_text()
                    await self._say_reply(chat_id, reply, reply_to_message_id=message_id)
                    return False, reply
            self.store.save_video_meta(request_id, user.slug, **meta)
        except Exception as e:  # noqa: BLE001  — дедуплікація не має ламати прийом відео
            logger.warning("Video meta/dedup step failed: %s", e)
        if caption and caption.strip():
            self.store.set_state(f"video_caption:{request_id}", caption.strip())
        self.store.create_video_check(
            request_id=request_id,
            chat_id=chat_id,
            message_id=message_id,
            user_slug=user.slug,
            file_id=file_id,
            file_unique_id=file_unique_id,
            duration_s=float(duration) if duration is not None else None,
            # offloaded: рахує box worker через gateway /cv/* (VM-ний sportnerd-cv таких не бере)
            status="offloaded" if self.cv_offload else "queued",
            location="box" if self.cv_offload else "vm",
        )
        if self.cv_offload:
            self.store.enqueue_cv_job(request_id)
        reply = self.bot_texts.get("processing")
        await self._say_reply(chat_id, reply, reply_to_message_id=message_id)
        if self.cv_offload:
            self.store.mark_cv_job(request_id, "processing_sent_at")
            self._spawn_box_wake(chat_id, message_id)
        return True, reply

    def _spawn_box_wake(self, chat_id: int, message_id: int) -> None:
        """Фоново: переконатись, що box worker поллить (вебхук з ретраями). Job лишається в черзі й нікуди не зникає."""
        if self.waker is None:
            return
        task = asyncio.ensure_future(self._ensure_box_awake(chat_id, message_id))
        self._wake_tasks.add(task)
        task.add_done_callback(self._wake_tasks.discard)

    async def _ensure_box_awake(self, chat_id: int, message_id: int) -> str:
        try:
            result = await self.waker.ensure_awake()
            if result == "timeout" and self.waker.should_notify():
                await self._say_reply(chat_id, self.bot_texts.get("box_waking"), reply_to_message_id=message_id)
            return result
        except Exception as e:
            logger.warning("box wake failed: %s", e)
            return "error"

    async def wait_box_wake(self) -> None:
        """Для тестів/graceful shutdown: дочекатись фонових пробуджень."""
        if self._wake_tasks:
            await asyncio.gather(*list(self._wake_tasks), return_exceptions=True)

    async def box_wake_watchdog(self) -> str:
        return await self.waker.watchdog() if self.waker is not None else "disabled"

    async def process_cv_results(self, still_counting_min: Optional[int] = None) -> int:
        """
        Періодично (scheduler, ~10 с): відповідає на завершені box CV jobs (judge + reply як раніше)
        і один раз надсилає «ще рахую», якщо результату немає довше N хв. Ніколи не просить переробити підхід.
        """
        handled = 0
        for job in self.store.list_cv_jobs_to_reply():
            rid = job["request_id"]
            try:
                await self.handle_video_callback(rid)
            except Exception as e:
                logger.warning("CV result reply failed for %s: %s", rid, e)
                continue
            self.store.mark_cv_job(rid, "replied_at")
            handled += 1
        minutes = CV_STILL_COUNTING_MIN if still_counting_min is None else still_counting_min
        for job in self.store.list_cv_jobs_overdue(minutes):
            check = self.store.get_video_check(job["request_id"])
            if check is not None:
                await self._say_reply(check.chat_id, self.bot_texts.get("still_counting"),
                                      reply_to_message_id=check.message_id)
            self.store.mark_cv_job(job["request_id"], "still_sent_at")
        return handled

    async def handle_video_callback(self, request_id: str) -> Optional[str]:
        """
        T-34: Обробка результату video_checks:
        1. Читає video_checks з БД.
        2. Отримує норму (target) для користувача на сьогодні.
        3. ReportJudgeAgent.judge_video_fact(fact).
        4. Якщо verdict.done -> store.set_done(user_slug, True, today).
        5. Формує persona-відповідь тренера.
        6. Надсилає реплаєм на оригінальне відео (check.message_id) або кладе в pending_replies.
        """
        check = self.store.get_video_check(request_id)
        if check is None:
            logger.warning("Video check %s not found in database", request_id)
            return None

        if await self._reject_duplicate_video(check, request_id):
            return None

        if self.trust_mode:
            return await self._handle_video_callback_trust(check, request_id)

        today = self._video_day(check)
        user_slug = check.user_slug or "lyosha"
        targets = challenge.day_targets(self.store, today)
        target = targets.get(user_slug, 10)

        fact = VideoFact(
            exercise=check.exercise or "none",
            reps=check.reps or 0,
            target=target,
            confidence=check.confidence or 0.0,
            valid=check.valid,
            flags=check.flags or [],
        )

        verdict = ReportJudgeAgent.judge_video_fact(fact)
        logger.info(
            "Video fact judged for %s: done=%s category=%s (%s)",
            user_slug, verdict.done, verdict.category, verdict.reason
        )

        if verdict.done:
            self.store.set_done(user_slug, True, today.isoformat())

        # Persona reply
        day_ex = challenge.day_exercise(self.store, today)
        reps = check.reps if check.reps is not None else 0
        counted_key = challenge.CV_EXERCISE_KEYS.get(check.exercise or "", day_ex)
        # Що побачив CV (у правильній формі для числа): «Побачив 4 присідання з 51»
        exercise_gen = challenge.exercise_word(counted_key, reps)
        if counted_key == day_ex and reps > 0:
            self.store.record_attempt(today.isoformat(), user_slug, day_ex, reps)

        reply_text = self.bot_texts.get(
            verdict.category, reps=reps, target=target, exercise=exercise_gen
        ) or "Відео переглянуто, але зарахувати не можу."

        seen_prefix = f"Побачив {reps} {exercise_gen} з {target}"
        paraphrased = await self._maybe_paraphrase_video_reply(reply_text)
        # Відповідь завжди починається з того, що побачив CV; перефраз без цього префікса відкидаємо.
        if reply_text.startswith(seen_prefix) and not paraphrased.startswith(seen_prefix):
            logger.info("Video reply paraphrase dropped the 'seen' prefix — using fixed text")
            paraphrased = reply_text
        reply_text = paraphrased

        for hint_key in verdict.hint_keys:
            hint_text = self.resolve_hint_text(hint_key)
            if hint_text:
                reply_text = f"{reply_text}\n{hint_text}"

        chat_id = check.chat_id
        reply_to_msg_id = check.message_id

        # Async доставка: реплаєм на відео; при недоступності — через pending_replies
        try:
            if reply_to_msg_id is not None:
                await self.sender.send_text(chat_id, reply_text, reply_to_message_id=reply_to_msg_id)
            else:
                await self.sender.send_text(chat_id, reply_text)
            self.store.save_message(chat_id, COACH_NAME, "coach", reply_text, format_ts(self.now()))
        except Exception as e:
            logger.warning("Failed to send video reply directly (%s) — queuing in pending_replies", e)
            self.store.add_pending_reply(
                request_id=f"vid_rep_{request_id}",
                chat_id=chat_id,
                text=reply_text,
                reply_to_message_id=reply_to_msg_id,
            )

        return reply_text

    def _duplicate_text(self) -> str:
        return self.bot_texts.get("video_duplicate") or "Це кіно я вже бачив 🍿 — вдруге не зараховую."

    def _find_duplicate_video(self, check) -> Optional[Tuple[Any, str]]:
        """Чи це відео — повтор раніше пораховано(го) відео цього автора за 48 год. (check, причина) або None."""
        if (check.status or "") != "done" or not check.user_slug:
            return None
        meta = self.store.get_video_meta(check.request_id)
        if meta is None:
            return None
        new = dict(meta)
        new.update(exercise=challenge.CV_EXERCISE_KEYS.get(check.exercise or "", check.exercise or ""),
                   reps=check.reps or 0)
        for old_meta in self.store.recent_video_metas(check.user_slug, DEDUP_WINDOW_H, exclude_request_id=check.request_id):
            old_check = self.store.get_video_check(old_meta["request_id"])
            # еталон — раніше пораховане відео (не пізніше за це) і саме не позначене дублем
            if old_check is None or (old_check.status or "") != "done" or old_check.id >= check.id:
                continue
            if any(str(f).startswith("duplicate_of:") for f in (old_check.flags or [])):
                continue
            old = dict(old_meta)
            old.update(exercise=challenge.CV_EXERCISE_KEYS.get(old_check.exercise or "", old_check.exercise or ""),
                       reps=old_check.reps or 0)
            dup, why = dedup.is_duplicate(new, old)
            if dup:
                return old_check, why
        return None

    async def _reject_duplicate_video(self, check, request_id: str) -> bool:
        """Повтор відео (повторне завантаження, перекодування): не зараховуємо вдруге, відповідаємо «вже бачив»."""
        try:
            found = self._find_duplicate_video(check)
        except Exception as e:  # noqa: BLE001
            logger.warning("Duplicate check failed for %s: %s", request_id, e)
            return False
        if found is None:
            return False
        original, why = found
        logger.info("Duplicate video %s of %s (%s) — not credited", request_id[:8], original.request_id[:8], why)
        flags = list(check.flags or [])
        flags.append(f"duplicate_of:{original.request_id[:8]}:{why}")
        self.store.update_video_check(request_id, flags=flags)
        reply_text = self._duplicate_text()
        try:
            await self._say_reply(check.chat_id, reply_text, reply_to_message_id=check.message_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to send duplicate reply (%s) — queuing in pending_replies", e)
            self.store.add_pending_reply(request_id=f"vid_rep_{request_id}", chat_id=check.chat_id,
                                         text=reply_text, reply_to_message_id=check.message_id)
        return True

    async def _handle_video_callback_trust(self, check, request_id: str) -> Optional[str]:
        today = self._video_day(check)
        today_iso = today.isoformat()
        user_slug = check.user_slug or "lyosha"

        # Заявлене число/вправа: LLM-тул report_reps (цифри, слова, сленг, заперечення); regex — лише фолбек без LLM.
        caption = self.store.get_state(f"video_caption:{request_id}", "") or ""
        claimed_reps, claimed_ex, claim_path = await self._extract_video_claim(check, request_id, caption, user_slug)

        # Trust logic
        day_ex = challenge.day_exercise(self.store, today)
        counted_ex = claimed_ex or day_ex

        if counted_ex == day_ex:
            targets = challenge.day_targets(self.store, today)
            target = targets.get(user_slug, 10)
        else:
            nn = self.store.get_next_norm(user_slug, counted_ex)
            if nn is not None:
                target = nn[0]
            else:
                targets = challenge.day_targets(self.store, today)
                target = targets.get(user_slug, 10)

        cv_reps = check.reps or 0
        cv_ex = challenge.CV_EXERCISE_KEYS.get(check.exercise or "")

        credited_reps = claimed_reps if claimed_reps is not None else cv_reps
        # Запам'ятовуємо, скільки зараховано саме за це відео (для оспорювання/перерахунку дня).
        self.store.set_state(f"video_credit:{request_id}", json.dumps({"reps": credited_reps, "ex": counted_ex}))

        if counted_ex == day_ex:
            if credited_reps > 0:
                self.store.record_attempt(today_iso, user_slug, day_ex, credited_reps)
            if credited_reps >= target:
                self.store.set_done(user_slug, True, today_iso)

        # Reply composition (always in this order: a, b, c, d, e)
        parts: List[str] = []

        # (a) "seen" part, friendly: if cv_reps>0 and cv_ex matches counted_ex: "Побачив {cv_reps} {exercise_word} з {target}";
        # if the CV saw a different exercise or 0: "Побачив {cv_reps} {cv exercise word} — це інша вправа / рахувати складно" style without a target comparison.
        cv_word = challenge.exercise_word(cv_ex or counted_ex, cv_reps)
        if cv_reps > 0 and cv_ex == counted_ex and counted_ex == day_ex:
            seen_text = self.bot_texts.get(
                "trust_seen",
                cv_reps=cv_reps,
                reps=cv_reps,
                target=target,
                exercise=cv_word,
            )
        else:
            seen_text = self.bot_texts.get(
                "trust_seen_other",
                cv_reps=cv_reps,
                reps=cv_reps,
                cv_exercise=cv_word,
                exercise=cv_word,
            )
        if seen_text:
            parts.append(seen_text)

        # (b) if claimed and claimed != cv_reps: add light phrase "я побачив {cv_reps}, але вірю твоєму слову" and credit text "Зарахував {claimed} {word}".
        if claimed_reps is not None and claimed_reps != cv_reps:
            claimed_word = challenge.exercise_word(counted_ex, claimed_reps)
            diff_text = self.bot_texts.get(
                "trust_claim_diff",
                cv_reps=cv_reps,
                reps=cv_reps,
                claimed=claimed_reps,
                exercise=claimed_word,
                word=claimed_word,
            )
            if diff_text:
                parts.append(diff_text)

        # (c) praise/credit: target met -> "норму зараховано" + praise;
        # target missed -> "записав {credited_reps} {word} у спробу дня, до норми {target} трохи не вистачило — але я в тебе вірю" (NEVER "не зараховано", never suspicion of cheating, never REDO words from prompts.REDO_WORDS).
        credited_word = challenge.exercise_word(counted_ex, credited_reps)
        if counted_ex == day_ex:
            if credited_reps >= target:
                credit_text = self.bot_texts.get(
                    "trust_credit_met",
                    target=target,
                    credited=credited_reps,
                    credited_reps=credited_reps,
                    reps=credited_reps,
                    exercise=credited_word,
                    word=credited_word,
                )
            else:
                credit_text = self.bot_texts.get(
                    "trust_credit_missed",
                    target=target,
                    credited=credited_reps,
                    credited_reps=credited_reps,
                    reps=credited_reps,
                    exercise=credited_word,
                    word=credited_word,
                )
        else:
            counted_name = challenge.EXERCISES.get(counted_ex, {}).get("name", counted_ex)
            day_name = challenge.EXERCISES.get(day_ex, {}).get("name", day_ex)
            credit_text = self.bot_texts.get(
                "trust_other_exercise",
                counted_exercise=counted_name,
                day_exercise=day_name,
                exercise=counted_name,
            )
        if credit_text:
            parts.append(credit_text)

        # (d) if no claim and CV reps < target add light tip "наступного разу напиши кількість у підписі до відео — зарахую її на слово".
        if claimed_reps is None and cv_reps < target and counted_ex == day_ex:
            tip_text = self.bot_texts.get("trust_no_claim_tip")
            if tip_text:
                parts.append(tip_text)

        # (e) if flags contain low_camera_angle / unreliable_camera_angle / low_body_visibility or confidence < 0.6, append the angle hint line "зарахую, але наступного разу зніми так, аби тебе було видно цілком" (new bot_text key trust_angle_hint).
        flags = check.flags or []
        has_angle_flag = any(f in flags for f in ("low_camera_angle", "unreliable_camera_angle", "low_body_visibility"))
        low_conf = (check.confidence is not None and check.confidence < 0.6)
        if has_angle_flag or low_conf:
            angle_text = self.bot_texts.get("trust_angle_hint")
            if angle_text:
                parts.append(angle_text)

        # (f) м'яка підказка про вправу: CV сумнівається / бачить іншу вправу, а людина вправу не називала -> не відхиляємо,
        # зараховуємо на слово й просимо уточнити (вправа дня — пріоритет).
        if claimed_ex is None and self._exercise_doubt(check, cv_ex, day_ex):
            doubt_text = self.bot_texts.get(
                "trust_exercise_hint",
                day_exercise=challenge.EXERCISES.get(day_ex, {}).get("name", day_ex),
                exercise=challenge.EXERCISES.get(day_ex, {}).get("name", day_ex),
            )
            if doubt_text:
                parts.append(doubt_text)

        reply_text = "\n".join(parts)

        chat_id = check.chat_id
        reply_to_msg_id = check.message_id

        try:
            if reply_to_msg_id is not None:
                await self.sender.send_text(chat_id, reply_text, reply_to_message_id=reply_to_msg_id)
            else:
                await self.sender.send_text(chat_id, reply_text)
            self.store.save_message(chat_id, COACH_NAME, "coach", reply_text, format_ts(self.now()))
        except Exception as e:
            logger.warning("Failed to send trust video reply directly (%s) — queuing in pending_replies", e)
            self.store.add_pending_reply(
                request_id=f"vid_rep_{request_id}",
                chat_id=chat_id,
                text=reply_text,
                reply_to_message_id=reply_to_msg_id,
            )

        return reply_text

    # --- Заявлене число: LLM-тул report_reps (основний шлях) + regex-фолбек ---
    def _nearby_author_messages(self, check, user_slug: str) -> List[ChatMessage]:
        """Текстові повідомлення автора до 10 хв до відео (хронологічно)."""
        try:
            utc_str = (check.created_at or "").split(".")[0]
            utc_dt = datetime.strptime(utc_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=pytz.UTC)
            check_local = utc_dt.astimezone(TIMEZONE)
        except Exception:
            check_local = self.now()
        ref_dt = self.now() if check_local.date() != self.now().date() else check_local
        window_start = ref_dt - timedelta(minutes=10)
        window_end = self.now()
        if window_end < ref_dt:
            window_end = ref_dt
        window_end_str = format_ts(window_end)
        out = []
        for m in self.store.messages_since(check.chat_id, format_ts(window_start)):
            if m.role != "user" or m.text.startswith("[Надіслано") or m.created_at > window_end_str:
                continue
            if challenge.normalize_participant(m.user_name, self.store) != user_slug:
                continue
            out.append(m)
        return out

    def _videos_block(self, slug: str, day) -> Tuple[str, List[str]]:
        """Відео автора за день: request_id, час (Kyiv), що порахував CV, що зараховано (для контексту LLM-тула)."""
        lines, ids = [], []
        for c in self._day_video_checks(slug, day):
            if c.status != "done":
                continue
            credit_reps, credit_ex = self._video_credit(c)
            lines.append(
                f"- owner={slug}, request_id={c.request_id}, час {self._check_local_ts(c)[11:16]}, бот порахував: {c.exercise or '?'} {int(c.reps or 0)}, "
                f"зараховано: {credit_reps} {credit_ex or '?'}"
            )
            ids.append(c.request_id)
        return "\n".join(lines), ids

    def _recent_chat_text(self, chat_id: int, minutes: int = 60, limit: int = 12) -> str:
        msgs = self.store.messages_since(chat_id, format_ts(self.now() - timedelta(minutes=minutes)))
        return "\n".join(m.render() for m in msgs[-limit:])

    def _regex_video_claim(self, caption: str, matching: List[ChatMessage]) -> Tuple[Optional[int], Optional[str]]:
        """Старий regex-шлях (фолбек): підпис, потім повідомлення автора поруч."""
        claimed_reps = parse_claimed_reps(caption) if caption else None
        claimed_ex = parse_claimed_exercise(caption) if caption else None
        if claimed_reps is None:
            for m in reversed(matching):
                val = parse_claimed_reps(m.text)
                if val is not None:
                    claimed_reps = val
                    if claimed_ex is None:
                        claimed_ex = parse_claimed_exercise(m.text)
                    break
        if claimed_ex is None:
            for m in reversed(matching):
                ex = parse_claimed_exercise(m.text)
                if ex is not None:
                    claimed_ex = ex
                    break
        return claimed_reps, claimed_ex

    async def _extract_video_claim(self, check, request_id: str, caption: str, user_slug: str
                                   ) -> Tuple[Optional[int], Optional[str], str]:
        """(кількість, вправа, шлях): шлях 'llm' | 'llm_none' | 'regex' (LLM не налаштований) | 'regex_fallback' (LLM впав)."""
        matching = self._nearby_author_messages(check, user_slug)
        path = "regex"
        reps: Optional[int] = None
        ex: Optional[str] = None
        if self.llm is not None and (caption or matching):
            try:
                day = self._video_day(check)
                videos_block, ids = self._videos_block(user_slug, day)
                if request_id not in ids:
                    videos_block += f"\n- request_id={request_id} (це відео, до якого належать підпис і повідомлення)"
                    ids.append(request_id)
                user = self.store.find_user(user_slug)
                target = "ПІДПИС ДО ВІДЕО: " + (caption or "(немає)") + "\nТЕКСТИ АВТОРА ПОРУЧ:\n" + (
                    "\n".join(f"[{m.created_at}] {m.text}" for m in matching) or "(немає)")
                claims = await self._call(
                    self.claims_agent.extract, day, user_slug, (user.display_name if user else user_slug),
                    "video", target, self._recent_chat_text(check.chat_id, 15), videos_block, ids,
                )
                claim = last_claim_for(claims, user_slug)
                if claim:
                    reps, ex, path = claim.count, claim.exercise, "llm"
                else:
                    path = "llm_none"
            except Exception as e:  # LLMUnavailable та будь-яка інша збійна відповідь -> дешевий regex
                logger.warning("Claim extraction via LLM failed (%s) — regex fallback (request %s)", e, request_id)
                path = "regex_fallback"
        if path in ("regex", "regex_fallback"):
            reps, ex = self._regex_video_claim(caption, matching)
        logger.info("Claim extraction path=%s request=%s user=%s reps=%s ex=%s", path, request_id, user_slug, reps, ex)
        try:
            self.store.set_state(f"video_claim_path:{request_id}", path)
        except Exception:
            pass
        return reps, ex, path

    @staticmethod
    def _exercise_doubt(check, cv_ex: Optional[str], day_ex: str) -> bool:
        """CV не впевнений у вправі (ambiguous/mixed) або бачить іншу, ніж вправа дня, при реальних повторах."""
        flags = check.flags or []
        if any(f in ("ambiguous_exercise", "mixed_exercises") for f in flags):
            return True
        return bool(cv_ex and cv_ex != day_ex and int(check.reps or 0) > 0)

    # --- TRUST_MODE (м'який режим «до виправлення»): людина оспорює підрахунок CV -> вірить людині ---
    def _check_local_ts(self, check) -> str:
        """Час відео в локальному форматі chat_history ('YYYY-MM-DD HH:MM:SS', Europe/Kyiv)."""
        try:
            utc_dt = datetime.strptime((check.created_at or "").split(".")[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=pytz.UTC)
            return format_ts(utc_dt.astimezone(TIMEZONE))
        except Exception:
            return ""

    def _day_video_checks(self, slug: str, day) -> list:
        lo, hi = dayroll.day_utc_bounds(day, *self.rollover)
        return self.store.video_checks_between(slug, lo, hi)

    def _video_credit(self, check) -> Tuple[int, Optional[str]]:
        """(зараховані повтори, вправа) для відео: запис довіри або (CV reps, CV вправа)."""
        raw = self.store.get_state(f"video_credit:{check.request_id}", "")
        if raw:
            try:
                d = json.loads(raw)
                return int(d.get("reps") or 0), d.get("ex")
            except (ValueError, TypeError):
                pass
        return int(check.reps or 0), challenge.CV_EXERCISE_KEYS.get(check.exercise or "")

    # --- учасники, їхні імена й відкриті питання «скільки вийшло?» ---
    def _participant_users(self) -> list:
        return list(self.store.list_users())

    @staticmethod
    def _name_stems(user) -> List[str]:
        """Основи імен для впізнавання відмінків: Льоша/Льоші/Льошу -> 'льош'."""
        stems = []
        for raw in [user.display_name, user.slug, user.telegram_name, user.username, *(user.aliases or [])]:
            w = re.sub(r"[^\w]", "", (raw or "").lower())
            if len(w) >= 3:
                stems.append(w[:-1] if len(w) >= 4 else w)
        return stems

    def _mentioned_users(self, text: str, exclude: Optional[str] = None) -> List[str]:
        words = re.findall(r"\w+", (text or "").lower())
        out = []
        for u in self._participant_users():
            if u.slug == exclude:
                continue
            if any(w.startswith(st) for st in self._name_stems(u) for w in words):
                out.append(u.slug)
        return out

    def _open_dispute_waits(self, today_iso: str) -> Dict[str, dict]:
        """{subject_slug: {rid, day, asked_by}} — відкриті питання «скільки вийшло?» за сьогодні."""
        waits: Dict[str, dict] = {}
        for u in self._participant_users():
            raw = self.store.get_state(f"dispute_wait:{u.slug}", "")
            if not raw:
                continue
            try:
                w = json.loads(raw)
            except ValueError:
                continue
            if isinstance(w, dict) and w.get("day") == today_iso:
                waits[u.slug] = w
        return waits

    def _participants_block(self) -> str:
        return "\n".join(f"- slug='{u.slug}': {u.display_name or u.slug}" for u in self._participant_users())

    def _all_videos_block(self, day) -> Tuple[str, List[str]]:
        lines, ids = [], []
        for u in self._participant_users():
            block, uids = self._videos_block(u.slug, day)
            if block:
                lines.append(block)
                ids.extend(uids)
        return "\n".join(lines), ids

    def _owner_of_quoted_count(self, quoted_text: str, today) -> Optional[str]:
        """Цитата відповіді бота «Побачив N <вправа>…» -> автор відео за сьогодні з таким N (останнє такс). Для regex-фолбеку."""
        m = re.search(r"Побачив\s+(\d+)", quoted_text or "")
        if not m:
            return None
        n = int(m.group(1))
        best = None
        for u in self._participant_users():
            for c in self._day_video_checks(u.slug, today):
                if c.status == "done" and int(c.reps or 0) == n:
                    if best is None or c.id > best[0]:
                        best = (c.id, u.slug)
        return best[1] if best else None

    def _regex_subject(self, text: str, speaker: str, quoted_text: Optional[str], waits: Dict[str, dict], today) -> str:
        """Чий це підрахунок, коли LLM недоступний: цитата бота > названа інша людина (без «я N») > питання, поставлене цій людині > автор."""
        owner = self._owner_of_quoted_count(quoted_text or "", today)
        if owner and owner != speaker:
            return owner
        self_claim = re.search(r"\bя\s+\d|\bя\s+(?:зробив|зробила|присів|присіла|відтис)", (text or "").lower())
        if not self_claim:
            others = self._mentioned_users(text, exclude=speaker)
            if others:
                return others[0]
        for subj, w in waits.items():
            if subj != speaker and w.get("asked_by") == speaker:
                return subj
        return speaker

    async def _llm_dispute_claim(self, chat_id: int, m: ChatMessage, speaker: str, today,
                                 quoted_text: Optional[str] = None, waits: Optional[Dict[str, dict]] = None
                                 ) -> Tuple[Optional[RepsClaim], str]:
        """
        Тул report_reps для повідомлення після відео. (claim|None, шлях): 'llm' | 'llm_none' | 'regex_fallback' | 'regex'
        ('regex*' — claim None, рішення приймає regex-шлях). Claim може стосуватись ІНШОЇ людини (subject_user):
        слово будь-якого учасника про чуже відео зараховується власнику відео.
        """
        if self.llm is None:
            return None, "regex"
        try:
            videos_block, ids = self._all_videos_block(today)
            user = self.store.find_user(speaker)
            extra = ""
            if quoted_text:
                extra += f"ЦИТОВАНЕ ПОВІДОМЛЕННЯ (на нього відповідає автор): «{quoted_text}»\n"
            for subj, w in (waits or {}).items():
                u2 = self.store.find_user(subj)
                extra += (f"ВІДКРИТЕ ПИТАННЯ БОТА: «скільки вийшло?» щодо відео {u2.display_name if u2 else subj} "
                          f"(subject_user='{subj}', request_id={w.get('rid')}), спитано через повідомлення "
                          f"учасника '{w.get('asked_by') or subj}'.\n")
            claims = await self._call(
                self.claims_agent.extract, today, speaker, (user.display_name if user else speaker), "dispute",
                m.text or "", self._recent_chat_text(chat_id, 60), videos_block, ids,
                self._participants_block(), extra,
            )
        except Exception as e:
            logger.warning("Dispute claim via LLM failed (%s) — regex fallback", e)
            return None, "regex_fallback"
        claim = claims[-1] if claims else None
        if len({c.user for c in claims}) > 1:
            logger.info("report_reps: several subjects, using the last: %s", [(c.user, c.count) for c in claims])
        return claim, ("llm" if claim else "llm_none")

    def _pick_target_check(self, slug: str, today, before_ts: str, wait: Optional[dict], request_id: Optional[str] = None):
        checks = [c for c in self._day_video_checks(slug, today)
                  if c.status == "done" and self._check_local_ts(c) <= (before_ts or "9999")]
        target = None
        if request_id:
            target = next((c for c in checks if c.request_id == request_id), None)
        if target is None and wait:
            target = next((c for c in checks if c.request_id == wait.get("rid")), None)
        if target is None and checks:
            target = checks[-1]
        return target

    async def _handle_disputes(self, chat_id: int, pending: List[ChatMessage]) -> List[ChatMessage]:
        """
        Людина називає/оспорює кількість на відео -> її слова важливіші за CV. Число розбирає LLM-тул report_reps (цифри, слова,
        сленг, заперечення; subject_user — чиї повтори: автора або ІНШОГО учасника, напр. власник пише про відео Льоші);
        regex — лише фолбек, якщо LLM недоступний. З числом — виправляємо залік власника відео; без числа — один раз м'яко
        питаємо «скільки вийшло?» (відповідь на це питання від того, кого спитали, закриває питання для потрібної людини).
        Повертає повідомлення, що лишились для звичайної обробки. Працює аж до кінця дня.
        """
        today = self._today()
        today_iso = today.isoformat()
        remaining: List[ChatMessage] = []
        for m in pending:
            speaker = challenge.normalize_participant(m.user_name, self.store)
            if not speaker or (m.text or "").startswith("[Надіслано"):
                remaining.append(m)
                continue
            quoted_sender, quoted_text, _rest = split_reply_context(m.text)
            reply_to_bot = bool(quoted_text) and (
                "Побачив" in quoted_text or "Зануда" in (quoted_sender or "") or "зарахов" in quoted_text.lower()
            )
            all_waits = self._open_dispute_waits(today_iso)
            # питання, що стосується цієї людини або було задане через її повідомлення; чужі — лише якщо є число
            waits = {subj: w for subj, w in all_waits.items()
                     if subj == speaker or w.get("asked_by") == speaker or looks_like_count(m.text)}
            regex_disputed = is_dispute(m.text, reply_to_bot_count=reply_to_bot)
            if not (regex_disputed or waits or reply_to_bot or looks_like_count(m.text)):
                remaining.append(m)
                continue
            if not any(self._pick_target_check(u.slug, today, m.created_at, None) for u in self._participant_users()):
                remaining.append(m)  # жодного пораховано відео сьогодні -> звичайна обробка
                continue

            claim, path = await self._llm_dispute_claim(chat_id, m, speaker, today, quoted_text, waits)
            ex_hint = _EX_UNSET
            if path == "llm":
                subject, number, disputed = claim.user, claim.count, True
                ex_hint = claim.exercise
            else:
                subject = self._regex_subject(m.text, speaker, quoted_text, waits, today)
                if path == "llm_none":
                    number, disputed = None, regex_disputed
                else:
                    number, disputed = parse_dispute_reps(m.text), regex_disputed
            wait = waits.get(subject)
            logger.info("Dispute path=%s speaker=%s subject=%s number=%s ex=%s disputed=%s", path, speaker, subject, number,
                        None if ex_hint is _EX_UNSET else ex_hint, disputed)
            if not disputed and not (wait and number is not None):
                remaining.append(m)
                continue

            target_check = self._pick_target_check(subject, today, m.created_at, wait,
                                                   claim.request_id if claim else None)
            if target_check is None:
                remaining.append(m)  # у названої людини сьогодні нема пораховано відео
                continue
            if number is not None and path.startswith("regex"):
                # людина не погоджується з підрахунком CV: число, що збігається з CV, відкидаємо, якщо є інше
                number = parse_dispute_reps(m.text, exclude=int(target_check.reps or 0) or None)

            if number is None:
                if path == "llm_none" and not regex_disputed and not wait:
                    remaining.append(m)
                    continue
                if wait:
                    logger.info("Dispute from %s about %s without number — already asked, waiting", speaker, subject)
                else:
                    self.store.set_state(f"dispute_wait:{subject}", json.dumps(
                        {"rid": target_check.request_id, "day": today_iso, "asked_by": speaker}))
                    if subject == speaker:
                        await self._say(chat_id, self.bot_texts.get("trust_dispute_ask"))
                    else:
                        await self._say(chat_id, self.bot_texts.get("trust_dispute_ask_other",
                                                                    name=self._display_name(subject)))
                continue

            await self._apply_video_correction(chat_id, target_check, subject, number, m.text, explicit_ex=ex_hint,
                                               reporter=speaker)
            for key in {subject, speaker}:
                if self.store.get_state(f"dispute_wait:{key}", ""):
                    self.store.set_state(f"dispute_wait:{key}", "")
        return remaining

    def _display_name(self, slug: str) -> str:
        u = self.store.find_user(slug)
        return (u.display_name if u and u.display_name else slug)

    async def _apply_reported_claims(self, chat_id: int, claims: List[RepsClaim]) -> None:
        """Claim-и, які coach-LLM передав тулом report_reps під час відповіді: застосовуємо тихо (відповідь coach уже є)."""
        today = self._today()
        seen = set()
        for c in claims:
            key = (c.user, c.request_id)
            if key in seen:
                continue
            seen.add(key)
            check = self._pick_target_check(c.user, today, "9999", None, c.request_id)
            if check is None:
                logger.info("report_reps from coach for %s ignored: no video today", c.user)
                continue
            await self._apply_video_correction(chat_id, check, c.user, c.count, "", explicit_ex=c.exercise, say=False,
                                               reporter=c.reported_by)

    async def _apply_video_correction(self, chat_id: int, check, slug: str, reps: int, text: str,
                                      explicit_ex=_EX_UNSET, say: bool = True, reporter: Optional[str] = None) -> str:
        """
        Виправляє залік до числа людини (воно ЗАВЖДИ перемагає CV, і вище, і нижче). Змінює лише денну спробу/статус —
        XP, життя й наступну норму рахує звичайний settle за best_reps дня, тож подвійного нарахування немає.
        explicit_ex: вправа, названа людиною ('pushups'|'squats'|None); _EX_UNSET -> розібрати regex-ом із text.
        """
        today = self._today()
        today_iso = today.isoformat()
        day_ex = challenge.day_exercise(self.store, today)
        # Вправа: названа людиною > вправа, під яку вже зараховано це відео (довіра) > вправа дня.
        # Вправу, яку «побачив» CV, ігноруємо: назване число зараховується на вправу дня (CV міг переплутати присідання/віджимання).
        if explicit_ex is _EX_UNSET:
            explicit_ex = parse_claimed_exercise(text)
        stored = self.store.get_state(f"video_credit:{check.request_id}", "")
        prev_ex = self._video_credit(check)[1] if stored else None
        ex = explicit_ex or prev_ex or day_ex
        self.store.set_state(f"video_credit:{check.request_id}",
                             json.dumps({"reps": int(reps), "ex": ex, "disputed": True}))
        target = challenge.day_targets(self.store, today).get(slug, 10)
        ex_word = challenge.exercise_word(ex, reps)
        if reporter and reporter != slug:
            # число про чуже відео назвала інша людина (напр. власник) — зараховуємо власнику відео, без повчань
            parts = [self.bot_texts.get("trust_dispute_fix_other", name=self._display_name(slug),
                                        reporter=self._display_name(reporter), claimed=reps, exercise=ex_word, reps=reps)]
        else:
            parts = [self.bot_texts.get("trust_dispute_fix", claimed=reps, exercise=ex_word, reps=reps)]
        if ex == day_ex or prev_ex == day_ex:
            best = 0
            for c in self._day_video_checks(slug, today):
                if c.status != "done":
                    continue
                c_reps, c_ex = self._video_credit(c)
                if c_ex == day_ex:
                    best = max(best, c_reps)
            self.store.set_attempt(today_iso, slug, day_ex, best)
            met = best >= target
            self.store.set_done(slug, met, today_iso)
        if ex == day_ex:
            key = "trust_credit_met" if met else "trust_credit_missed"
            parts.append(self.bot_texts.get(key, target=target, credited=best, credited_reps=best, reps=best,
                                            exercise=challenge.exercise_word(day_ex, best),
                                            word=challenge.exercise_word(day_ex, best)))
        else:
            parts.append(self.bot_texts.get(
                "trust_other_exercise",
                counted_exercise=challenge.EXERCISES.get(ex, {}).get("name", ex),
                day_exercise=challenge.EXERCISES.get(day_ex, {}).get("name", day_ex),
                exercise=challenge.EXERCISES.get(ex, {}).get("name", ex),
            ))
        reply = "\n".join(p for p in parts if p)
        if say:
            await self._say(chat_id, reply)
        logger.info("Dispute applied: %s %s reps=%s ex=%s (request %s)", slug, today_iso, reps, ex, check.request_id)
        return reply

    def resolve_hint_text(self, key: str) -> str:
        """Текст підказки за ключем: BotTexts (БД bot_texts -> дефолт із prompts.DEFAULT_BOT_TEXTS)."""
        return self.bot_texts.get(key)

    async def _maybe_paraphrase_video_reply(self, reply_text: str) -> str:
        """
        Якщо LLM доступний — перефразовує тон-зразок reply_text через Gemini (prompts.build_video_reply_tone_prompt),
        зберігаючи факти/числа й без прохань переробити підхід. При будь-якій помилці або порушенні правил —
        лишає фіксований reply_text як фолбек (не блокує тести/офлайн-режим, бо llm=None просто повертає reply_text).
        """
        if self.llm is None:
            return reply_text
        try:
            paraphrased = await self._call(
                self.llm.text, VIDEO_REPLY_PARAPHRASE_SYSTEM, build_video_reply_tone_prompt(reply_text)
            )
        except LLMUnavailable as e:
            logger.debug("Video reply paraphrase unavailable (%s) — using fixed tone sample", e)
            return reply_text
        except Exception as e:
            logger.warning("Video reply paraphrase failed (%s) — using fixed tone sample", e)
            return reply_text
        paraphrased = (paraphrased or "").strip()
        if not paraphrased or any(w in paraphrased.lower() for w in REDO_WORDS):
            return reply_text
        # Факти/числа мають зберегтися дослівно (жодних вигаданих чи змінених чисел)
        if sorted(re.findall(r"\d+", paraphrased)) != sorted(re.findall(r"\d+", reply_text)):
            return reply_text
        return paraphrased

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

    async def drain_pending_replies(self, max_attempts: int = 3) -> int:
        """Надсилає накопичені відповіді з pending_replies (наприклад після перезапуску бота)."""
        pending = self.store.list_pending_replies(status="pending")
        sent_count = 0
        for item in pending:
            if item.attempts >= max_attempts:
                continue
            try:
                if item.reply_to_message_id is not None:
                    try:
                        await self.sender.send_text(item.chat_id, item.text, reply_to_message_id=item.reply_to_message_id)
                    except TypeError:
                        await self.sender.send_text(item.chat_id, item.text)
                else:
                    await self.sender.send_text(item.chat_id, item.text)

                self.store.save_message(item.chat_id, COACH_NAME, "coach", item.text, format_ts(self.now()))
                self.store.mark_reply_sent(item.request_id)
                sent_count += 1
                logger.info("Sent pending reply %s to chat %s", item.request_id, item.chat_id)
            except Exception as e:
                logger.warning("Failed to send pending reply %s: %s", item.request_id, e)
                self.store.increment_reply_attempts(item.request_id, max_attempts=max_attempts)
        return sent_count

    async def check_expired_offload_requests(self, default_chat_id: Optional[int] = None) -> List[str]:
        """
        T-42: Періодична перевірка прострочених offload_requests.
        Знаходить записи зі статусом 'sent' або 'acked', у яких expires_at < now.
        Позначає їх як 'expired' та повідомляє користувача у persona-стилі.
        """
        expired = self.store.get_expired_offload_requests(now_ts=self.now())
        if not expired:
            return []

        expired_ids: List[str] = []
        for req in expired:
            self.store.update_offload_request(req.request_id, status="expired")
            expired_ids.append(req.request_id)
            logger.info("Marked offload request %s as expired", req.request_id)
            if req.type != "video":
                # admin_dev іде через admin_tasks (без таймера); старі admin-offload'и гасимо мовчки.
                continue

            if req.type == "video":
                notice = (
                    "⏱️ **Час очікування аналізу відео вичерпано.**\n"
                    "Сервер обробки не відповів вчасно. Будь ласка, надішліть відео повторно "
                    "або повідомте тренера!"
                )
            else:
                notice = (
                    f"⚠️ **Запит на зміну конфігурації (ID: `{req.request_id}`) прострочено.**\n"
                    "Grok Box не відповів за виділений ліміт часу. Спробуйте повторити запит пізніше."
                )

            target_chat = req.chat_id or default_chat_id
            if target_chat is not None:
                reply_to_id = req.reply_to_message_id or req.message_id
                await self._say_reply(target_chat, notice, reply_to_message_id=reply_to_id)

        return expired_ids

    # --- головна точка входу ---
    async def process(self, chat_id: int, trigger: str = "push", job: str = "") -> None:
        async with self._lock:
            if trigger == "startup":
                await self.drain_pending_replies()

            last_ts = self.store.get_state("last_processed_timestamp") or self._yesterday_cutoff()
            last_id_raw = self.store.get_state("last_processed_id")
            last_id = int(last_id_raw) if (last_id_raw is not None and last_id_raw != "") else None
            pending = self.store.unprocessed_user_messages(chat_id, last_ts, self._day_start(), after_id=last_id)
            if pending:
                logger.info(
                    "[%s] %d unprocessed message(s) since %s (after_id=%s)",
                    trigger, len(pending), last_ts, last_id,
                )
                await self._refresh_chat_members(chat_id)
                await self._handle_pending(chat_id, pending, trigger)
                self.store.set_state("last_processed_timestamp", pending[-1].created_at)
                self.store.set_state("last_processed_id", str(pending[-1].id))

            if trigger == "timer":
                if job == "offload_expiry":
                    await self.check_expired_offload_requests(chat_id)
                else:
                    await self._handle_timer(chat_id, job)

    async def _handle_pending(self, chat_id: int, pending: List[ChatMessage], trigger: str = "push") -> None:
        today_iso = self._today().isoformat()
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

        # Перевірка на команди адміна /approve та /rollback
        for m in list(active_pending):
            text_strip = (m.text or "").strip()
            # Команда /approve <request_id>
            if text_strip.startswith("/approve") or "/approve" in text_strip.lower():
                parts = text_strip.split()
                approve_idx = -1
                for idx, p in enumerate(parts):
                    if p.lower() == "/approve" and idx + 1 < len(parts):
                        approve_idx = idx + 1
                        break
                if approve_idx != -1:
                    target_req_id = parts[approve_idx].strip()
                    user = self.store.find_user(challenge.normalize_participant(m.user_name, self.store) or m.user_name)
                    if user is None:
                        user = self.store.resolve_or_create_user(m.user_name, today_iso)
                    await self.handle_approve(chat_id, target_req_id, user, reply_to_message_id=m.id)
                    active_pending.remove(m)
                    continue

            # Команда /rollback
            if text_strip.startswith("/rollback") or "/rollback" in text_strip.lower():
                user = self.store.find_user(challenge.normalize_participant(m.user_name, self.store) or m.user_name)
                if user is None:
                    user = self.store.resolve_or_create_user(m.user_name, today_iso)
                await self.handle_rollback(chat_id, user, reply_to_message_id=m.id)
                active_pending.remove(m)
                continue

        if self.trust_mode and active_pending:
            active_pending = await self._handle_disputes(chat_id, active_pending)

        if not active_pending:
            return

        try:
            decision = await self._call(self.router.decide, active_pending, history)
        except LLMUnavailable as e:
            logger.warning("Router unavailable (%s) — keyword fallback", e)
            decision = RouterAgent.fallback(active_pending)
        logger.info("Route decision: %s", decision.model_dump())

        if decision.is_admin_dev:
            await self._handle_admin_dev(chat_id, decision, active_pending, history, trigger)
        elif decision.is_admin_quick or decision.is_admin:
            await self._handle_admin_quick(chat_id, decision, active_pending, history, trigger)
        elif decision.route == "coach":
            await self._handle_coach(chat_id, active_pending, history)

    async def _handle_onboarding(
        self,
        chat_id: int,
        user: User,
        pending: List[ChatMessage],
        history: List[ChatMessage],
    ) -> None:
        today = self._today()
        try:
            reply, _ = await self._call(self.onboarding.interview, today, user, pending, history)
        except LLMUnavailable as e:
            logger.warning("Onboarding agent unavailable (%s) — deterministic fallback", e)
            reply = OnboardingAgent.fallback(self.store, user, pending, today)
        await self._say(chat_id, reply)

    async def _handle_admin_dev(
        self,
        chat_id: int,
        decision: RouteDecision,
        pending: List[ChatMessage],
        history: List[ChatMessage],
        trigger: str = "push",
    ) -> None:
        # Відповідаємо на повідомлення, що реально містить кодову фразу/наказ (а не на останнє в черзі).
        directive_msg = next((m for m in reversed(pending) if has_passphrase(m.text)), pending[-1] if pending else None)
        msg_id = directive_msg.id if directive_msg else None
        author_slug = challenge.normalize_participant(decision.sender, self.store) or decision.sender
        user = self.store.find_user(author_slug)
        telegram_id = user.telegram_id if user else None
        requester_name = (user.display_name if user else author_slug) or "невідомо"

        # Guardrail: Allowlist check (T-52)
        allowed = is_admin_allowed(telegram_id)
        logger.info(
            "Admin entry: mode=admin_dev trigger=%s user_id=%s user_name=%s sender_slug=%s allowed=%s msg_id=%s",
            trigger, telegram_id, decision.sender, author_slug, allowed, msg_id,
        )
        if not allowed:
            reply = (
                "❌ **Доступ заборонено.**\n"
                "Ваш Telegram ID не входить до списку дозволених адміністраторів для змін системи."
            )
            await self._say_reply(chat_id, reply, reply_to_message_id=msg_id)
            return

        # Guardrail: Rate limit check (T-52)
        allowed, rate_msg = check_admin_rate_limit(
            self.store,
            author_slug=author_slug,
            author_telegram_id=telegram_id,
            now=self.now(),
        )
        if not allowed:
            reply = f"⚠️ **Запит відхилено:** {rate_msg}"
            await self._say_reply(chat_id, reply, reply_to_message_id=msg_id)
            return

        # Guardrail: один активний запит одночасно (admin_audit + незавершена admin_task автора)
        allowed_active, active_id = check_active_request_limit(
            self.store,
            author_slug=author_slug,
            author_telegram_id=telegram_id,
        )
        active_task = self.store.active_admin_task_for(telegram_id, requester_name)
        if (not allowed_active and active_id) or active_task:
            ref = f"#{active_task}" if active_task else active_id
            reply = (
                f"⚠️ **Вже є активний запит в обробці** (`{ref}`). "
                "Будь ласка, зачекайте завершення попереднього запиту перед створенням нового."
            )
            await self._say_reply(chat_id, reply, reply_to_message_id=msg_id)
            return

        request_id = str(uuid.uuid4())
        full_text = directive_msg.text if directive_msg else ""
        self.store.create_admin_audit(
            request_id=request_id,
            author_slug=author_slug,
            author_telegram_id=telegram_id,
            request=decision.directive or full_text,
            plan="admin_dev -> admin_tasks queue",
            status="planned",
        )
        task_text = (
            f"{decision.directive or ''}\n\n--- повний текст (msg_id={msg_id}, автор={requester_name}/{author_slug}, "
            f"tg={telegram_id}, request_id={request_id}) ---\n{full_text}"
        )[:4000]
        task_id = self.store.create_admin_task(chat_id, telegram_id, requester_name, task_text)
        # admin_audit — лише журнал; активність далі контролює admin_tasks (pending/claimed), без таймерів.
        self.store.update_admin_audit(request_id, status="delegated")
        logger.info("admin_dev %s -> admin_task #%s (msg_id=%s)", request_id, task_id, msg_id)

        ack = self.bot_texts.get("admin_dev_accepted", id=task_id, request_id=request_id) \
            or f"Прийняв, задачу #{task_id} передав розробнику — чекайте."
        await self._say_reply(chat_id, ack, reply_to_message_id=msg_id)

    async def _handle_admin_quick(
        self,
        chat_id: int,
        decision: RouteDecision,
        pending: List[ChatMessage],
        history: List[ChatMessage],
        trigger: str = "push",
    ) -> None:
        last_msg = pending[-1] if pending else None
        msg_id = last_msg.id if last_msg else None
        author_slug = challenge.normalize_participant(decision.sender, self.store) or decision.sender
        user = self.store.find_user(author_slug)
        telegram_id = user.telegram_id if user else None

        # Guardrail: Allowlist check (T-52) — той самий, що й у _handle_admin_dev.
        allowed = is_admin_allowed(telegram_id)
        logger.info(
            "Admin entry: mode=admin_quick trigger=%s user_id=%s user_name=%s sender_slug=%s allowed=%s",
            trigger, telegram_id, decision.sender, author_slug, allowed,
        )
        if not allowed:
            reply = (
                "❌ **Доступ заборонено.**\n"
                "Ваш Telegram ID не входить до списку дозволених адміністраторів для змін системи."
            )
            await self._say_reply(chat_id, reply, reply_to_message_id=msg_id)
            return

        await self._send_obey_video(chat_id)
        try:
            reply, actions = await self._call(
                self.admin.execute,
                self._today(),
                decision.directive,
                decision.sender,
                pending,
                history,
            )
        except LLMUnavailable as e:
            logger.error("Admin agent unavailable: %s", e)
            reply, actions = FALLBACK_ADMIN_MESSAGE, []

        logger.info("Admin quick actions executed (%d): %s", len(actions), actions)
        for action in actions:
            m = re.match(r"create_admin_task\(#(\d+)\)", action)
            if m:
                fixed = self.bot_texts.get("admin_task_created", id=int(m.group(1)))
                if fixed not in (reply or ""):
                    reply = f"{reply}\n\n{fixed}" if reply else fixed
        for action in actions:
            self.store.create_admin_audit(
                request_id=str(uuid.uuid4()),
                author_slug=author_slug,
                author_telegram_id=telegram_id,
                request=action,
                plan=decision.directive or "",
                status="executed",
            )

        await self._say(chat_id, reply)

    async def _handle_admin(
        self,
        chat_id: int,
        decision: RouteDecision,
        pending: List[ChatMessage],
        history: List[ChatMessage],
    ) -> None:
        if decision.is_admin_dev:
            await self._handle_admin_dev(chat_id, decision, pending, history)
        else:
            await self._handle_admin_quick(chat_id, decision, pending, history)

    async def handle_approve(
        self,
        chat_id: int,
        request_id: str,
        user: User,
        reply_to_message_id: Optional[int] = None,
    ) -> Tuple[bool, str]:
        """
        T-52: Команда /approve <request_id>.
        Схвалює запит на деплой змін у заборонених зонах (branch auto/<request_id>).
        """
        if not is_admin_allowed(user.telegram_id):
            reply = "❌ **Доступ заборонено.** Тільки дозволені адміністратори можуть схвалювати деплой заборонених зон."
            await self._say_reply(chat_id, reply, reply_to_message_id=reply_to_message_id)
            return False, reply

        audit = self.store.get_admin_audit(request_id)
        if audit is None:
            reply = f"❌ Запит із `request_id={request_id}` не знайдено в журналі `admin_audit`."
            await self._say_reply(chat_id, reply, reply_to_message_id=reply_to_message_id)
            return False, reply

        if audit.status not in ("waiting_approval", "planned"):
            reply = f"⚠️ Запит `{request_id}` уже має статус `{audit.status}` і не очікує підтвердження."
            await self._say_reply(chat_id, reply, reply_to_message_id=reply_to_message_id)
            return False, reply

        self.store.update_admin_audit(request_id, status="approved")
        self.store.update_offload_request(request_id, status="approved")

        reply = f"✅ **Запит `{request_id}` схвалено!** Дозволено деплой гілки `auto/{request_id}`."
        await self._say_reply(chat_id, reply, reply_to_message_id=reply_to_message_id)
        logger.info("Admin %s approved request_id=%s", user.slug, request_id)
        return True, reply

    async def handle_rollback(
        self,
        chat_id: int,
        user: User,
        reply_to_message_id: Optional[int] = None,
    ) -> Tuple[bool, str]:
        """
        T-52: Команда /rollback.
        Відкочує реліз на попередній symlink через скрипт деплою або gateway.
        """
        if not is_admin_allowed(user.telegram_id):
            reply = "❌ **Доступ заборонено.** Тільки дозволені адміністратори можуть запускати rollback."
            await self._say_reply(chat_id, reply, reply_to_message_id=reply_to_message_id)
            return False, reply

        rollback_id = f"rollback_{uuid.uuid4().hex[:8]}"
        self.store.create_admin_audit(
            request_id=rollback_id,
            author_slug=user.slug,
            author_telegram_id=user.telegram_id,
            request="/rollback",
            plan="Manual rollback to previous release symlink",
            status="approved",
        )

        deploy_script = os.getenv("DEPLOY_SCRIPT_PATH", "deploy/deploy.sh")
        success = True
        detail = ""
        if os.path.isfile(deploy_script) and os.access(deploy_script, os.X_OK):
            try:
                proc = await asyncio.create_subprocess_exec(
                    deploy_script, "rollback",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                if proc.returncode != 0:
                    success = False
                    detail = stderr.decode().strip()
            except Exception as e:
                logger.warning("Deploy script rollback execution error: %s", e)
                detail = str(e)

        if success:
            self.store.update_admin_audit(rollback_id, status="deployed")
            reply = "⏮️ **Відкат виконано!** Повернуто попередню стабільну версію бота."
        else:
            self.store.update_admin_audit(rollback_id, status="failed")
            reply = f"⚠️ Помилка під час виконання відкату: {detail}"

        await self._say_reply(chat_id, reply, reply_to_message_id=reply_to_message_id)
        return success, reply

    async def _handle_coach(self, chat_id: int, pending: List[ChatMessage], history: List[ChatMessage]) -> None:
        today = self._today()
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
                claim_sink: List[RepsClaim] = []
                reply = await self._call(
                    self.coach.reply, today, self.store.messages_since(chat_id, self._day_start()), notes, claim_sink,
                )
                if claim_sink:
                    await self._apply_reported_claims(chat_id, claim_sink)
            except LLMUnavailable as e:
                logger.warning("Coach unavailable: %s", e)
        await self._say(chat_id, reply or self._template_reply(notes, today))

    def _apply_verdict(self, verdict: JudgeVerdict, done_before: dict, today) -> List[str]:
        notes = []
        targets = challenge.day_targets(self.store, today)
        pmap = challenge.participants_map(self.store)
        exercise = challenge.EXERCISES[challenge.day_exercise(self.store, today)]["genitive"]
        date_iso = today.strftime("%Y-%m-%d") if hasattr(today, "strftime") else (today.isoformat()[:10] if hasattr(today, "isoformat") else str(today)[:10])
        for r in verdict.reports:
            if r.participant not in pmap:
                continue
            if done_before.get(r.participant) == r.done:
                continue
            if r.done:
                ev = (r.evidence or "").lower()
                is_video_evidence = "відеозвіт" in ev or "[надіслано відеозвіт" in ev
                has_video_today = False
                has_check_fn = getattr(self.store, "has_video_check_today", None)
                if callable(has_check_fn):
                    has_video_today = bool(has_check_fn(r.participant, date_iso))
                if has_video_today or is_video_evidence:
                    logger.info("Skipping LLM credit for %s: video reports are judged by CV only", r.participant)
                    continue
            self.store.set_done(r.participant, r.done, date_iso)
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
        today = self._today()
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
            norms_lines = challenge.status_board(self.store, today, bold=True)
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

    async def _settle_lives(self, chat_id: int, day) -> None:
        # 1) мінус життя за незакритий день; 2) прогресія (норма/XP/рівні) — левел-ап відновлює життя.
        from core.leave import leave_on
        changed = challenge.settle_missed_day(self.store, day)
        try:
            prog = progression.settle_progression(self.store, day)
        except Exception as e:
            logger.exception("Progression settlement failed for %s: %s", day, e)
            prog = {}
        pmap = challenge.participants_map(self.store)
        skipped_leaves = []
        for slug in pmap:
            lv = leave_on(self.store, slug, day)
            if lv and slug not in prog:
                meta = pmap.get(slug, {})
                name = meta.get("name", slug)
                if lv["kind"] == "sick":
                    skipped_leaves.append(f"🤒 {name} на лікарняному — життя й норма без змін")
                else:
                    skipped_leaves.append(f"🏖 {name} у відпустці — життя й норма без змін")
        if not changed and not prog and not skipped_leaves:
            return
        text = f"📊 **Розрахунок за {day.strftime('%d.%m.%Y')}:**\n"
        if changed:
            lines = [
                f"💔 **{pmap.get(p, {}).get('name', p)}** втрачає життя → залишилось **{v}** ❤️"
                for p, v in changed.items()
            ]
            text += "\n" + "\n".join(lines)
        if skipped_leaves:
            text += "\n" + "\n".join(skipped_leaves)
        if prog:
            plines, ups = [], []
            for slug, r in prog.items():
                meta = pmap.get(slug, {})
                name = meta.get("name", slug)
                ex_name = challenge.EXERCISES.get(r["exercise"], {}).get("name", r["exercise"])
                plines.append(self.bot_texts.get("progress_settled", emoji=meta.get("emoji", ""), name=name,
                                                 xp_gain=r["xp_gain"], xp=r["xp"], level=r["level"],
                                                 exercise=ex_name, next=r["next"]))
                if r["level_ups"]:
                    ups.append(self.bot_texts.get("level_up", name=name, level=r["level"],
                                                  max_lives=r["max_lives"] if r["max_lives"] is not None else "—"))
            text += "\n\n" + "\n".join(plines)
            if ups:
                text += "\n\n" + "\n".join(ups)
        dead = [pmap.get(p, {}).get("name", p) for p, v in changed.items()
                if v == 0 and not (prog.get(p) or {}).get("level_ups")]
        if dead:
            text += f"\n\n💀 **{', '.join(dead)} — нуль життів. Дискваліфікація!**"
        await self._say(chat_id, text)
