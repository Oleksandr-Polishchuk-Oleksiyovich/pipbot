"""
Box CLI: одноразова ідемпотентна адмін-операція на VM (POST /admin/ops, HMAC).
  python tools/vm_ops.py <op> --key <unique-key> --params '<json>' [--params-file f.json] [--dry-run]
Ops: adjust_user, credit_attempt, send_message, admin_task_add, cv_cleanup. Повтор з тим самим key нічого не виконує вдруге.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cv_worker.box_worker import REPO_DIR, load_secret  # noqa: E402
from tools.admin_tasks import AdminTasksClient  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("op")
    ap.add_argument("--key", required=True)
    ap.add_argument("--params", default="{}")
    ap.add_argument("--params-file", help="UTF-8 JSON-файл (для кирилиці)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(REPO_DIR, ".env"), override=False)
    except ImportError:
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    params = json.load(open(args.params_file, encoding="utf-8")) if args.params_file else json.loads(args.params)
    url = os.getenv("CV_GATEWAY_URL", "")
    if not url:
        raise SystemExit("CV_GATEWAY_URL is not set")
    client = AdminTasksClient(url, load_secret())
    code, data = client._request("POST", "/admin/ops", payload={"op": args.op, "key": args.key, "params": params, "dry_run": args.dry_run}, timeout=60)
    try:
        print(json.dumps(json.loads(data), ensure_ascii=False, indent=1))
    except ValueError:
        print(json.dumps({"http": code, "body": data[:200].decode("utf-8", "replace")}))
    return 0 if code == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
