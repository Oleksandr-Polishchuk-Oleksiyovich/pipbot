"""
Пробудження box (Grok Bot) перед обробкою відео.

Box вимикається у простої, тоді CV-воркер не поллить /cv/claim і відео висять у cv_jobs. VM сама будить box вебхуком
(Grok Bot routine «SportNerdBot box wake»), переконується, що воркер знову поллить, і лише тоді «відпускає» відео
(job увесь час лишається в черзі — його ніколи не скасовуємо).

Секрети (URL і ключ вебхука) — ніколи в git/логах: або env WAKE_WEBHOOK_URL/WAKE_WEBHOOK_KEY, або root-only файл
(WAKE_SECRET_FILE, за замовчуванням /var/lib/sportnerdbot/wake_webhook.json, mode 0600), який кладе gateway
(POST /admin/secrets/wake) з box-CLI tools/set_wake_webhook.py. Якщо секрету немає — фіча no-op (одне попередження в лог).

Формат виклику вебхука такий самий, як у tools/admin_tasks_worker.py: POST JSON, заголовок Authorization: Bearer <key>
(або WAKE_WEBHOOK_HEADER зі значенням = ключ). Будь-який 2xx = «прийнято».
"""
import asyncio
import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Optional, Sequence

logger = logging.getLogger("wake")

POLL_TS_KEY = "cv_last_poll_ts"          # bot_state: epoch-секунди останнього /cv/claim (або /result) від box worker-а
LAST_FIRE_KEY = "wake_last_fire_ts"      # bot_state: коли востаннє стріляли вебхуком (rate-limit watchdog-а)
NOTICE_KEY = "wake_notice_ts"            # bot_state: коли казали в чат «помічник прокидається» (скидається, коли воркер ожив)
DEFAULT_SECRET_FILE = "/var/lib/sportnerdbot/wake_webhook.json"
DEFAULT_BACKOFF = (2.0, 5.0, 10.0)


def secret_file_path() -> str:
    return os.getenv("WAKE_SECRET_FILE", "").strip() or DEFAULT_SECRET_FILE


def load_wake_config(path: Optional[str] = None) -> Optional[Dict[str, str]]:
    """{'url','header','value'} або None, якщо секрету немає. Значення ніколи не логуються."""
    url = os.getenv("WAKE_WEBHOOK_URL", "").strip()
    key = os.getenv("WAKE_WEBHOOK_KEY", "").strip()
    if not (url and key):
        url = key = ""
        try:
            with open(path or secret_file_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
            url = str(data.get("url") or "").strip()
            key = str(data.get("key") or "").strip()
        except (OSError, ValueError):
            pass
    if not (url and key):
        return None
    header = os.getenv("WAKE_WEBHOOK_HEADER", "").strip() or "Authorization"
    value = f"Bearer {key}" if header.lower() == "authorization" else key
    return {"url": url, "header": header, "value": value}


def store_wake_secret(url: str, key: str, path: Optional[str] = None) -> None:
    """Атомарно пише секрет у файл mode 0600. ValueError — некоректні значення."""
    url, key = (url or "").strip(), (key or "").strip()
    if not url.startswith("https://") or not key or len(url) > 2000 or len(key) > 2000:
        raise ValueError("invalid wake webhook url/key")
    path = path or secret_file_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"url": url, "key": key}, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def post_webhook(cfg: Dict[str, str], payload: Dict[str, Any], timeout: float = 20) -> int:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(cfg["url"], data=body, method="POST",
                                 headers={"Content-Type": "application/json", cfg["header"]: cfg["value"],
                                          "User-Agent": "SportNerdBot-Wake/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


class BoxWaker:
    """Логіка пробудження. Стан — у bot_state (бот і gateway — різні процеси з однією SQLite)."""

    def __init__(self, store, *, enabled: bool = True, stale_s: float = 45.0, wait_s: float = 90.0,
                 attempts: int = 3, backoff: Sequence[float] = DEFAULT_BACKOFF, rounds: int = 2,
                 watchdog_interval_s: float = 300.0, lease_s: float = 300.0,
                 get_config: Callable[[], Optional[Dict[str, str]]] = load_wake_config,
                 poster: Callable[..., int] = post_webhook,
                 sleep: Callable[[float], Any] = asyncio.sleep,
                 clock: Callable[[], float] = time.time,
                 poll_interval_s: float = 2.0):
        self.store = store
        self.enabled = enabled
        self.stale_s, self.wait_s = float(stale_s), float(wait_s)
        self.attempts, self.backoff, self.rounds = max(1, int(attempts)), tuple(backoff), max(1, int(rounds))
        self.watchdog_interval_s = float(watchdog_interval_s)
        self.lease_s = float(lease_s)
        self.get_config, self.poster, self.sleep, self.clock = get_config, poster, sleep, clock
        self.poll_interval_s = float(poll_interval_s)
        self._warned = False
        self._busy = False

    # --- стан воркера ---
    def _state_float(self, key: str) -> Optional[float]:
        try:
            raw = self.store.get_state(key, "")
            return float(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            return None

    def poll_age_s(self) -> Optional[float]:
        ts = self._state_float(POLL_TS_KEY)
        return None if ts is None else max(0.0, self.clock() - ts)

    def worker_alive(self) -> bool:
        """Воркер «живий», якщо свіжий останній claim/result АБО є claimed-job у межах lease (під час CV воркер не поллить)."""
        age = self.poll_age_s()
        if age is not None and age <= self.stale_s:
            return True
        return self.store.has_active_cv_claim(self.lease_s)

    def configured(self) -> bool:
        return self.get_config() is not None

    def _active(self) -> bool:
        if not self.enabled:
            return False
        if self.get_config() is None:
            if not self._warned:
                logger.warning("Wake webhook not configured (no WAKE_WEBHOOK_URL/KEY or secret file) — box wake is a no-op")
                self._warned = True
            return False
        return True

    # --- вебхук ---
    async def fire(self, reason: str, queued: int = 0) -> bool:
        """Вебхук з ретраями (attempts, backoff). Будь-який 2xx = прийнято."""
        cfg = self.get_config()
        if cfg is None:
            return False
        payload = {"event": "box_wake", "reason": reason, "queued": queued, "ts": int(self.clock()),
                   "text": f"SportNerdBot: розбуди box і перевір, що cv_worker поллить ({reason}, у черзі: {queued})"}
        self.store.set_state(LAST_FIRE_KEY, f"{self.clock():.3f}")
        for attempt in range(1, self.attempts + 1):
            try:
                status = await asyncio.to_thread(self.poster, cfg, payload)
            except Exception as e:  # мережа/таймаут — теж ретрай
                status = None
                logger.warning("wake webhook attempt %d/%d error: %s", attempt, self.attempts, type(e).__name__)
            if status is not None and 200 <= status < 300:
                logger.info("wake webhook attempt %d/%d accepted (HTTP %s)", attempt, self.attempts, status)
                return True
            if status is not None:
                logger.warning("wake webhook attempt %d/%d failed (HTTP %s)", attempt, self.attempts, status)
            if attempt < self.attempts:
                await self.sleep(self.backoff[min(attempt - 1, len(self.backoff) - 1)] if self.backoff else 0)
        return False

    async def wait_for_worker(self) -> bool:
        waited = 0.0
        while waited < self.wait_s:
            if self.worker_alive():
                return True
            await self.sleep(self.poll_interval_s)
            waited += self.poll_interval_s
        return self.worker_alive()

    async def ensure_awake(self, reason: str = "cv_job_enqueued") -> str:
        """
        Викликається, коли відео щойно поставили в чергу. Повертає:
        'disabled' (фіча вимкнена/без секрету), 'alive' (воркер уже поллить), 'woken' (ожив після вебхука),
        'timeout' (не дочекались — треба повідомити чат; watchdog продовжить), 'busy' (інший виклик уже будить).
        """
        if not self._active():
            return "disabled"
        if self.worker_alive():
            self.store.set_state(NOTICE_KEY, "")
            return "alive"
        if self._busy:
            return "busy"
        self._busy = True
        try:
            for rnd in range(1, self.rounds + 1):
                queued = self.store.count_cv_jobs("queued")
                accepted = await self.fire(reason if rnd == 1 else f"{reason}_retry{rnd}", queued)
                if not accepted:
                    logger.warning("wake round %d/%d: webhook not accepted after %d attempts", rnd, self.rounds, self.attempts)
                if await self.wait_for_worker():
                    self.store.set_state(NOTICE_KEY, "")
                    logger.info("box worker is polling again (round %d)", rnd)
                    return "woken"
            logger.warning("box worker still not polling after %d round(s); job stays queued", self.rounds)
            return "timeout"
        finally:
            self._busy = False

    def should_notify(self) -> bool:
        """Раз на «епізод» (скидається, коли воркер ожив)."""
        if self.store.get_state(NOTICE_KEY, ""):
            return False
        self.store.set_state(NOTICE_KEY, f"{self.clock():.3f}")
        return True

    async def watchdog(self) -> str:
        """Періодично (scheduler): job довше WAKE_STALE_S у черзі + воркер мовчить → стріляємо вебхуком (раз на interval)."""
        if not self._active() or self._busy:
            return "disabled"
        if self.worker_alive():
            self.store.set_state(NOTICE_KEY, "")
            return "alive"
        age = self.store.oldest_queued_cv_age_s()
        if age is None or age < self.stale_s:
            return "idle"
        last = self._state_float(LAST_FIRE_KEY)
        if last is not None and self.clock() - last < self.watchdog_interval_s:
            return "rate_limited"
        accepted = await self.fire("watchdog", self.store.count_cv_jobs("queued"))
        return "fired" if accepted else "fire_failed"
