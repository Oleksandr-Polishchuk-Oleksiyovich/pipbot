"""
Code auto-update for the box CV worker and admin tasks worker.
Checks origin/main for new commits between jobs, fast-forwards cleanly, updates dependencies if
requirements changed, and triggers a clean worker restart.
"""
import glob
import hashlib
import logging
import os
import subprocess
import sys
import time
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger("cv_worker.autoupdate")
REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _hash_requirements(repo_dir: str) -> Dict[str, str]:
    hashes: Dict[str, str] = {}
    pattern = os.path.join(repo_dir, "requirements*.txt")
    for file_path in sorted(glob.glob(pattern)):
        try:
            with open(file_path, "rb") as f:
                hashes[os.path.basename(file_path)] = hashlib.sha256(f.read()).hexdigest()
        except OSError:
            pass
    return hashes


def _run_cmd(run_fn: Callable, cmd: list, cwd: str, timeout: float = 30) -> Any:
    try:
        return run_fn(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except TypeError:
        return run_fn(cmd, cwd=cwd, capture_output=True, text=True)


def check_and_update(repo_dir: Any, logger: Any, run: Callable = subprocess.run) -> bool:
    """
    Check origin/main for fast-forwardable updates and apply them cleanly.

    1. 'git fetch origin main'
    2. Compares HEAD with origin/main; returns False if equal.
    3. If working tree dirty ('git status --porcelain --untracked-files=no' non-empty), warn and return False.
    4. If HEAD is not an ancestor of origin/main (git merge-base --is-ancestor), warn and return False.
    5. Records hashes of requirements*.txt, runs 'git pull --ff-only origin main' (on failure warn, return False).
    6. If any requirements*.txt changed, runs 'uv pip install --python <sys.executable> -r requirements-cv.txt' (warn on failure, continue).
    7. Logs 'autoupdate: updated OLD..NEW' and returns True.
    All git errors / timeouts are caught, logged as warnings, and return False.
    """
    repo_dir_str = str(repo_dir)
    try:
        # 1. git fetch origin main
        res_fetch = _run_cmd(run, ["git", "fetch", "origin", "main"], cwd=repo_dir_str, timeout=30)
        if getattr(res_fetch, "returncode", 0) != 0:
            err = getattr(res_fetch, "stderr", "") or ""
            logger.warning("autoupdate: git fetch origin main failed (rc=%s): %s", getattr(res_fetch, "returncode", None), err.strip())
            return False

        # 2. compare HEAD with origin/main
        res_head = _run_cmd(run, ["git", "rev-parse", "HEAD"], cwd=repo_dir_str, timeout=30)
        if getattr(res_head, "returncode", 0) != 0:
            err = getattr(res_head, "stderr", "") or ""
            logger.warning("autoupdate: git rev-parse HEAD failed: %s", err.strip())
            return False
        old_head = (getattr(res_head, "stdout", "") or "").strip()

        res_origin = _run_cmd(run, ["git", "rev-parse", "origin/main"], cwd=repo_dir_str, timeout=30)
        if getattr(res_origin, "returncode", 0) != 0:
            err = getattr(res_origin, "stderr", "") or ""
            logger.warning("autoupdate: git rev-parse origin/main failed: %s", err.strip())
            return False
        new_head = (getattr(res_origin, "stdout", "") or "").strip()

        if old_head == new_head:
            return False

        # 3. dirty tree check
        res_status = _run_cmd(run, ["git", "status", "--porcelain", "--untracked-files=no"], cwd=repo_dir_str, timeout=30)
        if getattr(res_status, "returncode", 0) != 0:
            err = getattr(res_status, "stderr", "") or ""
            logger.warning("autoupdate: git status failed: %s", err.strip())
            return False
        status_out = (getattr(res_status, "stdout", "") or "").strip()
        if status_out:
            logger.warning("autoupdate: dirty working tree, skipping update: %s", status_out)
            return False

        # 4. ancestor check
        res_anc = _run_cmd(run, ["git", "merge-base", "--is-ancestor", "HEAD", "origin/main"], cwd=repo_dir_str, timeout=30)
        if getattr(res_anc, "returncode", 0) != 0:
            logger.warning("autoupdate: HEAD is not ancestor of origin/main (diverged or error)")
            return False

        # 5. record requirements*.txt hashes and ff-only pull
        before_req_hashes = _hash_requirements(repo_dir_str)

        res_pull = _run_cmd(run, ["git", "pull", "--ff-only", "origin", "main"], cwd=repo_dir_str, timeout=30)
        if getattr(res_pull, "returncode", 0) != 0:
            err = getattr(res_pull, "stderr", "") or ""
            logger.warning("autoupdate: git pull --ff-only origin main failed (rc=%s): %s", getattr(res_pull, "returncode", None), err.strip())
            return False

        # 6. check requirements change
        after_req_hashes = _hash_requirements(repo_dir_str)
        if before_req_hashes != after_req_hashes:
            try:
                res_uv = _run_cmd(
                    run,
                    ["uv", "pip", "install", "--python", sys.executable, "-r", "requirements-cv.txt"],
                    cwd=repo_dir_str,
                    timeout=300,
                )
                if getattr(res_uv, "returncode", 0) != 0:
                    err = getattr(res_uv, "stderr", "") or ""
                    logger.warning("autoupdate: uv pip install failed (rc=%s): %s", getattr(res_uv, "returncode", None), err.strip())
            except Exception as e:
                logger.warning("autoupdate: uv pip install error: %s", e)

        # 7. log updated and return True
        logger.info("autoupdate: updated %s..%s", old_head, new_head)
        return True

    except Exception as e:
        logger.warning("autoupdate: git error or timeout: %s", e)
        return False


class Autoupdater:
    def __init__(
        self,
        repo_dir: Optional[str] = None,
        logger: Optional[logging.Logger] = None,
        get_config: Optional[Callable[[], Dict[str, Any]]] = None,
        check_fn: Optional[Callable[..., bool]] = None,
    ):
        self.repo_dir = str(repo_dir or REPO_DIR)
        self.logger = logger or logging.getLogger("cv_worker.autoupdate")
        self.get_config = get_config
        self.check_fn = check_fn or check_and_update
        self.last_check: Optional[float] = None

    def _get_config(self) -> Tuple[bool, float]:
        cfg: Dict[str, Any] = {}
        if self.get_config is not None:
            try:
                cfg = self.get_config()
            except Exception:
                pass
        else:
            try:
                from tools.worker_timers import read_workers_config
                cfg = read_workers_config()
            except Exception:
                pass

        enabled = True
        interval = 60.0

        if "BOX_AUTOUPDATE" in cfg:
            try:
                enabled = bool(int(float(cfg["BOX_AUTOUPDATE"])))
            except (ValueError, TypeError):
                pass
        if "BOX_AUTOUPDATE_INTERVAL" in cfg:
            try:
                interval = float(cfg["BOX_AUTOUPDATE_INTERVAL"])
            except (ValueError, TypeError):
                pass

        # Environment variable override if explicitly set
        if "BOX_AUTOUPDATE" in os.environ:
            v = os.environ["BOX_AUTOUPDATE"].strip().lower()
            enabled = v not in ("0", "false", "no", "off")
        if "BOX_AUTOUPDATE_INTERVAL" in os.environ:
            try:
                interval = float(os.environ["BOX_AUTOUPDATE_INTERVAL"])
            except (ValueError, TypeError):
                pass

        return enabled, interval

    def maybe_update(self, now: Optional[float] = None) -> bool:
        """
        Check and update if the interval has elapsed and autoupdate is enabled.
        Honors BOX_AUTOUPDATE and BOX_AUTOUPDATE_INTERVAL.
        """
        if now is None:
            now = time.monotonic()

        enabled, interval = self._get_config()
        if not enabled:
            return False

        if self.last_check is not None and (now - self.last_check) < interval:
            return False

        self.last_check = now
        try:
            return bool(self.check_fn(self.repo_dir, self.logger))
        except Exception as e:
            self.logger.warning("autoupdate: check_fn error: %s", e)
            return False
