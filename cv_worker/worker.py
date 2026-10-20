"""
CV Worker Service for SportNerdBot (T-32).
Processes queued video checks (concurrency=1), downloads via Telegram getFile,
runs pose estimation and Gemini vision reconcile, updates database, and triggers callback.
"""
import asyncio
import json
import logging
import os
import signal
import sys
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional
import urllib.request
import urllib.error

from core.config import (
    BOT_TOKEN,
    DB_PATH,
    GEMINI_API_KEY,
    CANDIDATE_MODELS,
    GATEWAY_PORT,
    GATEWAY_HMAC_SECRET,
    CV_TARGET_FPS,
    CV_MAX_SIDE,
    CV_MODEL,
)
from core.cv import rep_counter
from core.llm import GeminiLLM
from core.signing import sign_payload
from cv_worker.vision import classify_exercise_vision, extract_middle_frames, reconcile_classes
from database.store import Store, VideoCheck

logger = logging.getLogger("cv_worker")

MAX_VIDEO_BYTES = 20 * 1024 * 1024  # 20 MB Telegram getFile limit


def download_telegram_file(bot_token: str, file_id: str, dest_path: str, max_bytes: int = MAX_VIDEO_BYTES) -> None:
    """Завантажує файл з Telegram Bot API через getFile."""
    get_file_url = f"https://api.telegram.org/bot{bot_token}/getFile?file_id={file_id}"
    req = urllib.request.Request(get_file_url, headers={"User-Agent": "SportNerdBot-CV/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        meta = json.loads(resp.read().decode("utf-8"))
        if not meta.get("ok"):
            raise RuntimeError(f"Telegram getFile returned error: {meta}")
        file_path = meta["result"]["file_path"]

    download_url = f"https://api.telegram.org/file/bot{bot_token}/{file_path}"
    req_dl = urllib.request.Request(download_url, headers={"User-Agent": "SportNerdBot-CV/1.0"})
    with urllib.request.urlopen(req_dl, timeout=60) as resp, open(dest_path, "wb") as f_out:
        total = 0
        while True:
            chunk = resp.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"Downloaded file exceeded maximum size of {max_bytes} bytes")
            f_out.write(chunk)


class CVWorker:
    def __init__(
        self,
        store: Store,
        bot_token: Optional[str] = None,
        llm: Optional[GeminiLLM] = None,
        callback_url: Optional[str] = None,
        downloader: Optional[Callable[[str, str, str], None]] = None,
        analyzer: Optional[Callable[..., Dict[str, Any]]] = None,
        callback_handler: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
        secret: Optional[str] = None,
        target_fps: Optional[int] = None,
        max_side: Optional[int] = None,
        model: Optional[str] = None,
    ):
        self.store = store
        self.bot_token = bot_token
        self.llm = llm
        self.callback_url = callback_url
        self.downloader = downloader or download_telegram_file
        self.analyzer = analyzer or rep_counter.analyze_video
        self.callback_handler = callback_handler
        self.secret = secret or GATEWAY_HMAC_SECRET
        self.target_fps = target_fps or CV_TARGET_FPS
        self.max_side = max_side or CV_MAX_SIDE
        self.model = model or CV_MODEL
        self.running = False

    def claim_one(self) -> Optional[VideoCheck]:
        """Атомарно бере наступну задачу з черги (concurrency=1)."""
        return self.store.claim_next_video_check()

    def process_one(self, check: VideoCheck) -> Dict[str, Any]:
        """
        Обробляє одну задачу:
        1. Створює тимчасовий файл
        2. Завантажує відео (якщо є file_id)
        3. Запускає analyze_video
        4. Запускає vision reconcile (якщо налаштовано llm)
        5. Оновлює БД (status='done' або 'failed')
        6. Видаляє тимчасовий файл (відео не зберігається)
        7. Викликає callback
        """
        logger.info("Processing video check id=%d request_id=%s file_id=%s", check.id, check.request_id, check.file_id)
        t0 = time.time()
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".mp4", prefix=f"cv_{check.request_id[:8]}_")
        os.close(tmp_fd)

        try:
            # 1. Download
            if check.file_id:
                if not self.bot_token and self.downloader == download_telegram_file:
                    raise RuntimeError("Cannot download video: BOT_TOKEN is not configured")
                self.downloader(self.bot_token or "", check.file_id, tmp_path)

            # 2. Analyze video via MediaPipe Pose Landmarker
            pose_res = self.analyzer(
                tmp_path,
                request_id=check.request_id,
                file_unique_id=check.file_unique_id or "",
                target_fps=self.target_fps,
                max_side=self.max_side,
                model=self.model,
            )

            # 3. Gemini vision classification & reconcile (T-33)
            final_res = pose_res
            if self.llm is not None:
                try:
                    frames = extract_middle_frames(tmp_path, count=3)
                    if frames:
                        vision_res = classify_exercise_vision(self.llm, frames)
                        final_res = reconcile_classes(pose_res, vision_res)
                except Exception as e:
                    logger.warning("Vision reconcile step skipped due to error: %s", e)

            # 4. Save result to database (status='done')
            self.store.update_video_check(
                check.request_id,
                status="done",
                exercise=final_res.get("exercise", "none"),
                reps=int(final_res.get("reps", 0)),
                confidence=float(final_res.get("confidence", 0.0)),
                flags=final_res.get("flags", []),
                valid=bool(final_res.get("valid", False)),
                analyzer_version=final_res.get("analyzer_version", "cv-1.0"),
                finished=True,
            )

            # 5. Trigger callback
            self._dispatch_callback(check, final_res)
            return final_res

        except Exception as e:
            logger.exception("Error processing video check %s: %s", check.request_id, e)
            self.store.update_video_check(
                check.request_id,
                status="failed",
                flags=["worker_error", str(e)[:100]],
                finished=True,
            )
            failed_res = {
                "contract_version": "1.0",
                "request_id": check.request_id,
                "status": "failed",
                "error": str(e),
                "elapsed_s": round(time.time() - t0, 2),
            }
            self._dispatch_callback(check, failed_res)
            return failed_res

        finally:
            # Always clean up temporary video file (privacy & disk space)
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except Exception as e:
                    logger.warning("Failed to unlink tmp file %s: %s", tmp_path, e)

    def _dispatch_callback(self, check: VideoCheck, result: Dict[str, Any]) -> None:
        """Доставляє результат через прямий handler або HTTP POST на gateway /callback."""
        # Прямий handler (in-process або unit tests)
        if self.callback_handler is not None:
            try:
                self.callback_handler(check.request_id, result)
            except Exception as e:
                logger.error("Error in callback_handler for %s: %s", check.request_id, e)

        # HTTP callback на gateway
        if self.callback_url:
            try:
                ts = int(time.time())
                payload = {
                    "contract_version": "1.0",
                    "request_id": check.request_id,
                    "type": "video",
                    "status": "done" if result.get("valid", False) or result.get("exercise") else "failed",
                    "result": result,
                    "ts": ts,
                }
                if self.secret:
                    payload["signature"] = sign_payload(payload, self.secret, ts=ts)
                data_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                req = urllib.request.Request(
                    self.callback_url,
                    data=data_bytes,
                    headers={"Content-Type": "application/json", "User-Agent": "SportNerdBot-CV/1.0"},
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    logger.info("Callback sent to %s status=%d", self.callback_url, resp.status)
            except Exception as e:
                logger.warning("Failed to POST callback to %s: %s", self.callback_url, e)

    def run_once(self) -> bool:
        """Перевіряє наявність однієї задачі та обробляє її. Повертає True якщо була задача."""
        check = self.claim_one()
        if not check:
            return False
        self.process_one(check)
        return True

    def run_loop(self, poll_interval: float = 2.0) -> None:
        """Основний робочий цикл сервісу sportnerd-cv (concurrency=1)."""
        self.running = True
        logger.info("CV Worker started polling video_checks queue (poll_interval=%.1fs)", poll_interval)

        def _handle_signal(signum, frame):
            logger.info("Signal %d received, stopping worker gracefully...", signum)
            self.running = False

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)

        while self.running:
            try:
                had_task = self.run_once()
                if not had_task and self.running:
                    time.sleep(poll_interval)
            except Exception as e:
                logger.exception("Unexpected error in worker loop: %s", e)
                time.sleep(poll_interval)

        logger.info("CV Worker stopped.")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    store = Store(DB_PATH)
    store.init_schema()
    llm = GeminiLLM.from_api_key(GEMINI_API_KEY, CANDIDATE_MODELS) if GEMINI_API_KEY else None
    callback_url = f"http://127.0.0.1:{GATEWAY_PORT}/callback"
    worker = CVWorker(
        store=store,
        bot_token=BOT_TOKEN,
        llm=llm,
        callback_url=callback_url,
    )
    worker.run_loop()


if __name__ == "__main__":
    main()
