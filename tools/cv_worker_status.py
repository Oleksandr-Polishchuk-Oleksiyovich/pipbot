"""
Box CLI: стан CV worker-а на VM (GET /cv/worker_status, HMAC). Для routine «SportNerdBot box wake»:

  python tools/cv_worker_status.py            # JSON: last_poll_age_s, queued, claimed, worker_polling, wake_configured
  python tools/cv_worker_status.py --wait 60  # чекати до 60 с, поки worker не почне поллити (exit 0), інакше exit 1
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cv_worker.box_worker import load_secret  # noqa: E402
from tools.admin_tasks import AdminTasksClient  # noqa: E402


def fetch(client: AdminTasksClient) -> dict:
    code, data = client._request("GET", "/cv/worker_status")
    if code != 200:
        raise RuntimeError(f"worker_status failed: HTTP {code} {data[:200]!r}")
    return json.loads(data)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wait", type=float, default=0.0)
    args = ap.parse_args(argv)
    url = os.getenv("CV_GATEWAY_URL", "")
    if not url:
        env_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
        try:
            from dotenv import load_dotenv
            load_dotenv(env_file, override=False)
        except ImportError:
            pass
        url = os.getenv("CV_GATEWAY_URL", "")
    if not url:
        raise SystemExit("CV_GATEWAY_URL is not set")
    client = AdminTasksClient(url, load_secret())
    deadline = time.monotonic() + max(0.0, args.wait)
    while True:
        st = fetch(client)
        if st.get("worker_polling") or time.monotonic() >= deadline:
            print(json.dumps(st))
            return 0 if st.get("worker_polling") else 1
        time.sleep(3)


if __name__ == "__main__":
    sys.exit(main())
