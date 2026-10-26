"""
Box CLI: read-only діагностика VM (GET /admin/diag, HMAC). Друкує JSON (логи з редакцією секретів).
  python tools/vm_diag.py [--since-days 3] [--log-lines 120] [--section cv_jobs_recent --section users ...] [--logs]
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
    ap.add_argument("--since-days", type=int, default=3)
    ap.add_argument("--log-lines", type=int, default=0)
    ap.add_argument("--section", action="append", help="показати лише ці ключі верхнього рівня")
    args = ap.parse_args(argv)
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(REPO_DIR, ".env"), override=False)
    except ImportError:
        pass
    url = os.getenv("CV_GATEWAY_URL", "")
    if not url:
        raise SystemExit("CV_GATEWAY_URL is not set")
    client = AdminTasksClient(url, load_secret())
    code, data = client._request("GET", "/admin/diag", {"since_days": str(args.since_days), "log_lines": str(args.log_lines)})
    if code != 200:
        print(json.dumps({"http": code}))
        return 1
    obj = json.loads(data)
    if args.section:
        obj = {k: obj.get(k) for k in args.section}
    print(json.dumps(obj, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
