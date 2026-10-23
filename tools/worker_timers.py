#!/usr/bin/env python3
"""
CLI and helper functions for live worker timers and VM configuration:
  - status: prints JSON with workers_env, vm_config_env, and autodeploy_status
  - set: updates config/workers.env and/or deploy/vm_config.env
  - trigger-deploy: sends HMAC-signed POST /autodeploy/trigger to CV_GATEWAY_URL
"""
import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)

from core.signing import sign_request
from cv_worker.box_worker import load_secret, urlopen_with_deadline

DEFAULT_WORKERS_ENV_FILE = os.path.join(REPO_DIR, "config", "workers.env")
DEFAULT_VM_CONFIG_FILE = os.path.join(REPO_DIR, "deploy", "vm_config.env")

DEFAULT_WORKERS_CONFIG: Dict[str, float] = {
    "CV_POLL_WAIT_SEC": 25.0,
    "CV_CHECK_INTERVAL_SEC": 0.0,
    "CV_HEARTBEAT_S": 300.0,
    "ADMIN_TASKS_POLL_WAIT_SEC": 25.0,
    "ADMIN_TASKS_CHECK_INTERVAL_SEC": 0.0,
    "ADMIN_TASKS_HEARTBEAT_S": 300.0,
}

DEFAULT_VM_CONFIG: Dict[str, str] = {
    "AUTODEPLOY_INTERVAL": "1min",
    "CV_CLAIM_WAIT_S": "25",
    "CV_RESULTS_INTERVAL_SEC": "10",
    "ADMIN_TASKS_ANNOUNCE_INTERVAL_SEC": "60",
}


def read_env_file(path: str) -> Dict[str, str]:
    if not os.path.isfile(path):
        return {}
    res: Dict[str, str] = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            res[k.strip()] = v.strip().strip("\"'")
    return res


def write_env_file(path: str, updates: Dict[str, Any]) -> None:
    lines = []
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    seen = set()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _ = stripped.split("=", 1)
            k = k.strip()
            if k in updates:
                new_lines.append(f"{k}={updates[k]}\n")
                seen.add(k)
                continue
        new_lines.append(line)
    for k, v in updates.items():
        if k not in seen:
            if new_lines and not new_lines[-1].endswith("\n"):
                new_lines[-1] += "\n"
            new_lines.append(f"{k}={v}\n")
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)


def read_workers_config(path: Optional[str] = None) -> Dict[str, Any]:
    file_path = path or os.getenv("WORKERS_ENV_FILE", DEFAULT_WORKERS_ENV_FILE)
    loaded = read_env_file(file_path)
    res: Dict[str, Any] = dict(DEFAULT_WORKERS_CONFIG)
    for k, default_val in DEFAULT_WORKERS_CONFIG.items():
        if k in loaded:
            try:
                res[k] = float(loaded[k])
            except (ValueError, TypeError):
                pass
        elif k in os.environ:
            try:
                res[k] = float(os.environ[k])
            except (ValueError, TypeError):
                pass
    return res


def read_vm_config(path: Optional[str] = None) -> Dict[str, str]:
    file_path = path or os.getenv("VM_CONFIG_FILE", DEFAULT_VM_CONFIG_FILE)
    loaded = read_env_file(file_path)
    res: Dict[str, str] = dict(DEFAULT_VM_CONFIG)
    res.update(loaded)
    return res


def fetch_autodeploy_status(base_url: str, secret: str, timeout: float = 10.0) -> Dict[str, Any]:
    base = base_url.rstrip("/")
    path = "/autodeploy/status"
    headers = sign_request("GET", path, b"", secret)
    headers["User-Agent"] = "SportNerdBot-WorkerTimers/1.0"
    req = urllib.request.Request(base + path, method="GET", headers=headers)
    status_code, data = urlopen_with_deadline(req, timeout)
    if status_code != 200:
        raise RuntimeError(f"GET /autodeploy/status failed HTTP {status_code}: {data.decode('utf-8', errors='replace')}")
    return json.loads(data.decode("utf-8"))


def trigger_deploy(base_url: str, secret: str, timeout: float = 10.0) -> Dict[str, Any]:
    base = base_url.rstrip("/")
    path = "/autodeploy/trigger"
    headers = sign_request("POST", path, b"{}", secret)
    headers["User-Agent"] = "SportNerdBot-WorkerTimers/1.0"
    headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=b"{}", method="POST", headers=headers)
    status_code, data = urlopen_with_deadline(req, timeout)
    if status_code != 202:
        raise RuntimeError(f"POST /autodeploy/trigger failed HTTP {status_code}: {data.decode('utf-8', errors='replace')}")
    return json.loads(data.decode("utf-8"))


def _format_num(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else str(v)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    # status subcommand
    p_status = sub.add_parser("status", help="Print workers_env, vm_config_env, and autodeploy_status")
    p_status.add_argument("--remote", action="store_true", help="Fetch remote autodeploy_status from gateway")
    p_status.add_argument("--gateway-url", help="Gateway URL override")
    p_status.add_argument("--secret", help="HMAC secret override")
    p_status.add_argument("--workers-file", help="Path to workers.env")
    p_status.add_argument("--vm-config-file", help="Path to vm_config.env")

    # set subcommand
    p_set = sub.add_parser("set", help="Update workers.env and/or vm_config.env")
    p_set.add_argument("--workers-file", help="Path to workers.env")
    p_set.add_argument("--vm-config-file", help="Path to vm_config.env")
    p_set.add_argument("--cv-wait", type=float, help="CV_POLL_WAIT_SEC")
    p_set.add_argument("--cv-interval", type=float, help="CV_CHECK_INTERVAL_SEC")
    p_set.add_argument("--cv-heartbeat", type=float, help="CV_HEARTBEAT_S")
    p_set.add_argument("--admin-wait", type=float, help="ADMIN_TASKS_POLL_WAIT_SEC")
    p_set.add_argument("--admin-interval", type=float, help="ADMIN_TASKS_CHECK_INTERVAL_SEC")
    p_set.add_argument("--admin-heartbeat", type=float, help="ADMIN_TASKS_HEARTBEAT_S")
    p_set.add_argument("--autodeploy-interval", help="AUTODEPLOY_INTERVAL (e.g. 1min, 30s)")
    p_set.add_argument("--cv-claim-wait", type=float, help="CV_CLAIM_WAIT_S")
    p_set.add_argument("--cv-results-interval", type=int, help="CV_RESULTS_INTERVAL_SEC")
    p_set.add_argument("--admin-announce-interval", type=int, help="ADMIN_TASKS_ANNOUNCE_INTERVAL_SEC")

    # trigger-deploy subcommand
    p_trig = sub.add_parser("trigger-deploy", help="Send HMAC POST /autodeploy/trigger to gateway")
    p_trig.add_argument("--gateway-url", help="Gateway URL override")
    p_trig.add_argument("--secret", help="HMAC secret override")

    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    if args.cmd == "status":
        workers_path = args.workers_file or os.getenv("WORKERS_ENV_FILE", DEFAULT_WORKERS_ENV_FILE)
        vm_path = args.vm_config_file or os.getenv("VM_CONFIG_FILE", DEFAULT_VM_CONFIG_FILE)
        out: Dict[str, Any] = {
            "workers_env": read_env_file(workers_path),
            "vm_config_env": read_env_file(vm_path),
        }
        gw_url = args.gateway_url or os.getenv("CV_GATEWAY_URL", "").strip()
        if args.remote or gw_url:
            if not gw_url:
                out["autodeploy_status"] = {"error": "CV_GATEWAY_URL not set"}
            else:
                try:
                    secret = args.secret or load_secret()
                    out["autodeploy_status"] = fetch_autodeploy_status(gw_url, secret)
                except Exception as e:
                    out["autodeploy_status"] = {"error": str(e)}
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    elif args.cmd == "set":
        workers_path = args.workers_file or os.getenv("WORKERS_ENV_FILE", DEFAULT_WORKERS_ENV_FILE)
        vm_path = args.vm_config_file or os.getenv("VM_CONFIG_FILE", DEFAULT_VM_CONFIG_FILE)
        worker_updates: Dict[str, str] = {}
        vm_updates: Dict[str, str] = {}

        if args.cv_wait is not None:
            worker_updates["CV_POLL_WAIT_SEC"] = _format_num(args.cv_wait)
        if args.cv_interval is not None:
            worker_updates["CV_CHECK_INTERVAL_SEC"] = _format_num(args.cv_interval)
        if args.cv_heartbeat is not None:
            worker_updates["CV_HEARTBEAT_S"] = _format_num(args.cv_heartbeat)
        if args.admin_wait is not None:
            worker_updates["ADMIN_TASKS_POLL_WAIT_SEC"] = _format_num(args.admin_wait)
        if args.admin_interval is not None:
            worker_updates["ADMIN_TASKS_CHECK_INTERVAL_SEC"] = _format_num(args.admin_interval)
        if args.admin_heartbeat is not None:
            worker_updates["ADMIN_TASKS_HEARTBEAT_S"] = _format_num(args.admin_heartbeat)

        if args.autodeploy_interval is not None:
            interval = args.autodeploy_interval.strip()
            if not re.match(r"^[0-9]+(s|sec|seconds|m|min|minutes|h|hr|hours)$", interval):
                raise SystemExit(f"Invalid --autodeploy-interval: {interval!r}")
            vm_updates["AUTODEPLOY_INTERVAL"] = interval
        if args.cv_claim_wait is not None:
            vm_updates["CV_CLAIM_WAIT_S"] = _format_num(args.cv_claim_wait)
        if args.cv_results_interval is not None:
            vm_updates["CV_RESULTS_INTERVAL_SEC"] = str(args.cv_results_interval)
        if args.admin_announce_interval is not None:
            vm_updates["ADMIN_TASKS_ANNOUNCE_INTERVAL_SEC"] = str(args.admin_announce_interval)

        if worker_updates:
            write_env_file(workers_path, worker_updates)
        if vm_updates:
            write_env_file(vm_path, vm_updates)

        out = {
            "updated_workers": worker_updates,
            "updated_vm_config": vm_updates,
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    elif args.cmd == "trigger-deploy":
        gw_url = args.gateway_url or os.getenv("CV_GATEWAY_URL", "").strip()
        if not gw_url:
            raise SystemExit("CV_GATEWAY_URL is not set")
        secret = args.secret or load_secret()
        res = trigger_deploy(gw_url, secret)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
