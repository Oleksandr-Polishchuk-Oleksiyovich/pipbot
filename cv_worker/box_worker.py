"""
Box CV worker: long-polls the VM gateway (/cv/claim ~25s), downloads the video, runs CV under the
box-wide `heavy` lock, posts the result back. Every request is HMAC-signed (core.signing.sign_request).

Env:
  CV_GATEWAY_URL        https://<vm-host>:<port>  (обов'язково)
  CV_HMAC_SECRET_ENV    назва змінної з секретом (default GATEWAY_HMAC_SECRET); значення ніколи не логується
  CV_ENV_FILE           .env, з якого підвантажити секрет (default: <repo>/.env, якщо існує)
  CV_HEAVY_BIN          default "heavy"; CV_HEAVY=0 — без heavy (лише для тестів/діагностики)
  CV_HEAVY_MAX_S        default 600
  CV_TARGET_FPS=15 (min 12), CV_MAX_SIDE=640, CV_MODEL=lite — quality defaults
Backoff лише на помилках: 1, 2, 5, 10, 20, 30 с. 204 (таймаут long-poll) -> одразу новий claim.
"""
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.signing import sign_request

logger = logging.getLogger("box_cv_worker")

BACKOFF_STEPS = (1, 2, 5, 10, 20, 30)
CLAIM_HTTP_TIMEOUT_S = 40  # > серверних ~25 с long-poll
REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_secret() -> str:
    name = os.getenv("CV_HMAC_SECRET_ENV", "GATEWAY_HMAC_SECRET")
    env_file = os.getenv("CV_ENV_FILE", os.path.join(REPO_DIR, ".env"))
    if not os.getenv(name) and os.path.isfile(env_file):
        try:
            from dotenv import load_dotenv
            load_dotenv(env_file, override=False)
        except ImportError:
            pass
    secret = os.getenv(name, "")
    if not secret:
        raise RuntimeError(f"HMAC secret env var {name} is not set")
    return secret


class GatewayClient:
    def __init__(self, base_url: str, secret: str):
        self.base = base_url.rstrip("/")
        self.secret = secret

    def request(self, method: str, path: str, body: bytes = b"", timeout: float = 30) -> Tuple[int, bytes]:
        headers = sign_request(method, path, body, self.secret)
        headers["User-Agent"] = "SportNerdBot-BoxCV/1.0"
        if body:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=body if method != "GET" else None,
                                     method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def claim(self) -> Optional[Dict[str, Any]]:
        status, data = self.request("POST", "/cv/claim", b"{}", timeout=CLAIM_HTTP_TIMEOUT_S)
        if status == 204:
            return None
        if status != 200:
            raise RuntimeError(f"claim failed: HTTP {status}")
        return json.loads(data.decode("utf-8"))

    def download(self, path: str, dest: str) -> None:
        status, data = self.request("GET", path, timeout=120)
        if status != 200:
            raise RuntimeError(f"video download failed: HTTP {status}")
        with open(dest, "wb") as f:
            f.write(data)

    def post_result(self, rid: str, payload: Dict[str, Any]) -> int:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        status, _ = self.request("POST", f"/cv/jobs/{rid}/result", body, timeout=30)
        return status


def heavy_cv_command(video: str, out: str) -> List[str]:
    cmd = [sys.executable, "-m", "cv_worker.cv_job", video, out]
    if os.getenv("CV_HEAVY", "1") == "0":
        return cmd
    heavy = os.getenv("CV_HEAVY_BIN", "heavy")
    return [heavy, "--who", "SportNerdBot-CV", "--max", os.getenv("CV_HEAVY_MAX_S", "600")] + cmd


def run_cv_subprocess(video: str) -> Dict[str, Any]:
    fd, out = tempfile.mkstemp(suffix=".json", prefix="cvres_")
    os.close(fd)
    try:
        env = dict(os.environ)
        env.setdefault("CV_TARGET_FPS", "15")
        env.setdefault("CV_MAX_SIDE", "640")
        env["CV_TARGET_FPS"] = str(max(12, int(env["CV_TARGET_FPS"])))
        proc = subprocess.run(heavy_cv_command(video, out), cwd=REPO_DIR, env=env,
                              capture_output=True, text=True, timeout=900)
        if proc.returncode != 0:
            raise RuntimeError(f"cv_job exit {proc.returncode}: {proc.stderr[-300:]}")
        with open(out, encoding="utf-8") as f:
            return json.load(f)
    finally:
        if os.path.exists(out):
            os.unlink(out)


class BoxCVWorker:
    def __init__(self, client: GatewayClient, analyzer: Optional[Callable[[str], Dict[str, Any]]] = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.client = client
        self.analyzer = analyzer or run_cv_subprocess
        self.sleep = sleep
        self.running = False
        self.errors = 0

    def process(self, job: Dict[str, Any]) -> int:
        rid = job["request_id"]
        t0 = time.time()
        fd, video = tempfile.mkstemp(suffix=".mp4", prefix=f"boxcv_{rid[:8]}_")
        os.close(fd)
        try:
            self.client.download(job["video_url"], video)
            t_dl = time.time()
            try:
                result = self.analyzer(video)
                payload = {"claim_token": job["claim_token"], "ok": True, "result": result}
            except Exception as e:
                logger.warning("CV failed for %s: %s", rid, e)
                payload = {"claim_token": job["claim_token"], "ok": False, "error": str(e)[:200]}
            payload["timings"] = {"download_s": round(t_dl - t0, 2), "total_s": round(time.time() - t0, 2)}
            return self.client.post_result(rid, payload)
        finally:
            if os.path.exists(video):
                os.unlink(video)

    def step(self) -> bool:
        """Один цикл long-poll. True — job оброблено, False — таймаут 204."""
        job = self.client.claim()
        if job is None:
            return False
        status = self.process(job)
        logger.info("job %s result posted: HTTP %s", job["request_id"], status)
        return True

    def run(self) -> None:
        self.running = True
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "running", False))
        signal.signal(signal.SIGINT, lambda *_: setattr(self, "running", False))
        while self.running:
            try:
                self.step()
                self.errors = 0
            except Exception as e:
                delay = BACKOFF_STEPS[min(self.errors, len(BACKOFF_STEPS) - 1)]
                self.errors += 1
                logger.warning("worker error (%s); retry in %ss", e, delay)
                self.sleep(delay)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    url = os.getenv("CV_GATEWAY_URL", "")
    if not url:
        raise SystemExit("CV_GATEWAY_URL is not set")
    if os.getenv("CV_HEAVY", "1") != "0" and not shutil.which(os.getenv("CV_HEAVY_BIN", "heavy")):
        raise SystemExit("`heavy` not found on PATH: CV jobs must run through the box heavy lock")
    BoxCVWorker(GatewayClient(url, load_secret())).run()


if __name__ == "__main__":
    main()
