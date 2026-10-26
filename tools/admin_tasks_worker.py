"""
Box-side admin_tasks worker: long-poll GET /admin_tasks/claim на gateway (HMAC) і пересилає задачу у вебхук.

Env (або .env у корені репо):
  CV_GATEWAY_URL, GATEWAY_HMAC_SECRET       — gateway і секрет (секрет ніколи не логується)
  ADMIN_TASKS_WEBHOOK_URL                   — куди POST-ити {task_id, text, requester, created_at}
  ADMIN_TASKS_WEBHOOK_KEY                   — ключ авторизації вебхука
  ADMIN_TASKS_WEBHOOK_HEADER                — назва заголовка (default 'Authorization' -> 'Bearer <key>';
                                              інша назва -> значенням іде сам ключ)
  ADMIN_TASKS_LOG                           — лог (default /tmp/sportnerd-admintasks.log, ротація щодня, 7 днів)

Поведінка: 204 -> одразу новий long-poll; помилки -> backoff 1,2,5,10 с. Якщо вебхук не налаштовано —
задачі НЕ claim-имо (лишаються pending, щоб не «загубились» у claimed без lease) і голосно логуємо.
Задачу, яку вже взяли, доставляємо з повторами, доки вебхук не прийме (2xx).
Політика: задачі з цієї черги обробляються лише agy з моделлю Gemini 3.8 Flash (не сильною моделлю).
"""
import json
import logging
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from logging.handlers import TimedRotatingFileHandler
from typing import Any, Callable, Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cv_worker.box_worker import EX_CONFIG, load_env, load_secret  # noqa: E402  (also loads .env)
from tools.admin_tasks import AdminTasksClient  # noqa: E402

logger = logging.getLogger("admin_tasks_worker")
BACKOFF_STEPS = (1, 2, 5, 10)
CONFIG_RECHECK_S = 60


def webhook_config() -> Optional[Dict[str, str]]:
    url = os.getenv("ADMIN_TASKS_WEBHOOK_URL", "").strip()
    key = os.getenv("ADMIN_TASKS_WEBHOOK_KEY", "").strip()
    if not url or not key:
        return None
    header = os.getenv("ADMIN_TASKS_WEBHOOK_HEADER", "").strip() or "Authorization"
    value = f"Bearer {key}" if header.lower() == "authorization" else key
    return {"url": url, "header": header, "value": value}


def post_webhook(cfg: Dict[str, str], payload: Dict, timeout: float = 30) -> int:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(cfg["url"], data=body, method="POST",
                                 headers={"Content-Type": "application/json", cfg["header"]: cfg["value"],
                                          "User-Agent": "SportNerdBot-AdminTasks/1.0"})
    from cv_worker.box_worker import urlopen_with_deadline
    return urlopen_with_deadline(req, timeout)[0]


def task_payload(task: Dict) -> Dict:
    return {"task_id": task["id"], "text": task.get("text"), "requester": task.get("requester_name"),
            "created_at": task.get("created_at")}


class AdminTasksWorker:
    def __init__(self, client, get_config: Callable = webhook_config, poster: Callable = post_webhook,
                 sleep: Callable[[float], None] = time.sleep,
                 get_workers_config: Optional[Callable[[], Dict[str, Any]]] = None):
        self.client, self.get_config, self.poster, self.sleep = client, get_config, poster, sleep
        self.errors = 0
        self.stopped = False
        if get_workers_config is not None:
            self.get_workers_config = get_workers_config
        else:
            from tools.worker_timers import read_workers_config
            self.get_workers_config = read_workers_config

    def _backoff(self) -> None:
        delay = BACKOFF_STEPS[min(self.errors, len(BACKOFF_STEPS) - 1)]
        self.errors += 1
        self.sleep(delay)

    def deliver(self, task: Dict) -> None:
        """Повторює доставку, доки вебхук не відповість 2xx (або воркер не зупинять)."""
        attempt = 0
        while not self.stopped:
            cfg = self.get_config()
            if cfg is None:
                logger.error("!!! admin_task #%s claimed but webhook config missing (ADMIN_TASKS_WEBHOOK_URL/KEY); "
                             "holding and retrying in %ss", task["id"], CONFIG_RECHECK_S)
                self.sleep(CONFIG_RECHECK_S)
                continue
            attempt += 1
            try:
                status = self.poster(cfg, task_payload(task))
            except Exception as e:
                status = None
                logger.warning("admin_task #%s webhook error (attempt %d): %s", task["id"], attempt, e)
            if status is not None and 200 <= status < 300:
                logger.info("admin_task #%s delivered to webhook (HTTP %s, attempt %d)", task["id"], status, attempt)
                return
            logger.warning("admin_task #%s webhook failed (HTTP %s, attempt %d)", task["id"], status, attempt)
            self.sleep(BACKOFF_STEPS[min(attempt - 1, len(BACKOFF_STEPS) - 1)])

    def step(self) -> bool:
        """Один цикл. True — оброблено задачу."""
        if self.get_config() is None:
            logger.error("!!! ADMIN_TASKS_WEBHOOK_URL/ADMIN_TASKS_WEBHOOK_KEY not set — not claiming tasks "
                         "(they stay pending). Recheck in %ss", CONFIG_RECHECK_S)
            self.sleep(CONFIG_RECHECK_S)
            return False
        cfg = self.get_workers_config()
        poll_wait = float(cfg.get("ADMIN_TASKS_POLL_WAIT_SEC", 25.0))
        check_interval = float(cfg.get("ADMIN_TASKS_CHECK_INTERVAL_SEC", 0.0))
        try:
            try:
                task = self.client.claim(wait_s=poll_wait)
            except TypeError:
                task = self.client.claim()
        except Exception as e:
            logger.warning("claim failed: %s", e)
            self._backoff()
            return False
        self.errors = 0
        if task is None:
            if check_interval > 0:
                self.sleep(check_interval)
            return False  # 204 -> одразу новий long-poll
        logger.info("admin_task #%s claimed: requester=%s text=%r", task["id"], task.get("requester_name"),
                    (task.get("text") or "")[:200])
        self.deliver(task)
        return True

    def run(self) -> None:
        last_beat = time.monotonic()
        while not self.stopped:
            cfg = self.get_workers_config()
            heartbeat_s = float(cfg.get("ADMIN_TASKS_HEARTBEAT_S", 300.0))
            if heartbeat_s > 0 and time.monotonic() - last_beat >= heartbeat_s:
                logger.info("heartbeat: alive, polling (errors=%d)", self.errors)
                last_beat = time.monotonic()
            self.step()


def setup_logging() -> None:
    path = os.getenv("ADMIN_TASKS_LOG", "/tmp/sportnerd-admintasks.log")
    handler = TimedRotatingFileHandler(path, when="midnight", backupCount=7, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                        handlers=[handler], force=True)


def main() -> None:
    setup_logging()
    load_env()
    secret = load_secret()
    url = os.getenv("CV_GATEWAY_URL", "")
    if not url:
        logger.error("CONFIG ERROR: CV_GATEWAY_URL is not set (neither in the environment nor in .env)")
        sys.exit(EX_CONFIG)
    worker = AdminTasksWorker(AdminTasksClient(url, secret))

    def _stop(*_):
        worker.stopped = True
    signal.signal(signal.SIGTERM, _stop)
    logger.info("admin_tasks worker started (gateway=%s, webhook configured=%s)", url, webhook_config() is not None)
    worker.run()


if __name__ == "__main__":
    main()
