"""
Box CLI: передати VM секрет вебхука пробудження box («SportNerdBot box wake») — без ноутбука і без друку значень.

  python tools/set_wake_webhook.py            # читає WAKE_WEBHOOK_URL / WAKE_WEBHOOK_KEY з env box (або .env), POST /admin/secrets/wake
  python tools/set_wake_webhook.py --check    # лише чи налаштовано на VM (без значень)

Значення беруться ТІЛЬКИ з env (ніколи з аргументів), ніколи не друкуються і не логуються. Gateway кладе їх у
root-only файл на VM (/var/lib/sportnerdbot/wake_webhook.json, mode 0600, поза git). Автентифікація — HMAC+nonce
(GATEWAY_HMAC_SECRET, CV_GATEWAY_URL), як у tools/admin_tasks.py.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cv_worker.box_worker import REPO_DIR, load_secret  # noqa: E402
from tools.admin_tasks import AdminTasksClient  # noqa: E402


def _load_env() -> None:
    env_file = os.path.join(REPO_DIR, ".env")
    if os.path.isfile(env_file):
        try:
            from dotenv import load_dotenv
            load_dotenv(env_file, override=False)
        except ImportError:
            pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="лише перевірити, чи секрет уже є на VM")
    args = ap.parse_args(argv)
    _load_env()
    gateway = os.getenv("CV_GATEWAY_URL", "")
    if not gateway:
        raise SystemExit("CV_GATEWAY_URL is not set")
    client = AdminTasksClient(gateway, load_secret())

    if args.check:
        code, data = client._request("GET", "/admin/secrets/wake")
        print(json.dumps({"http": code, **(json.loads(data) if code == 200 else {})}))
        return 0 if code == 200 else 1

    url = os.getenv("WAKE_WEBHOOK_URL", "").strip()
    key = os.getenv("WAKE_WEBHOOK_KEY", "").strip()
    if not url or not key:
        print("WAKE_WEBHOOK_URL / WAKE_WEBHOOK_KEY are not set in the box env — nothing sent", file=sys.stderr)
        return 2
    code, data = client._request("POST", "/admin/secrets/wake", payload={"url": url, "key": key})
    try:
        status = json.loads(data).get("status") or json.loads(data).get("error")
    except Exception:
        status = None
    print(json.dumps({"http": code, "result": status}))  # без значень
    return 0 if code == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
