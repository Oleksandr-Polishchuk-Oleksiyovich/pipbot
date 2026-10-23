"""
Box CLI для черги admin_tasks через gateway (HMAC, як box CV worker).

  python tools/admin_tasks.py list [--status pending]
  python tools/admin_tasks.py claim [--wait]
  python tools/admin_tasks.py done <id> --commit <sha> [--note TEXT]
  python tools/admin_tasks.py reject <id> --note TEXT

Env (або .env у корені репо): CV_GATEWAY_URL, GATEWAY_HMAC_SECRET (значення секрету ніколи не друкується).
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.signing import sign_request  # noqa: E402
from cv_worker.box_worker import load_secret, urlopen_with_deadline  # noqa: E402
from core.textguard import looks_mangled  # noqa: E402


class AdminTasksClient:
    def __init__(self, base_url: str, secret: str):
        self.base = base_url.rstrip("/")
        self.secret = secret

    def _request(self, method: str, path: str, query: Optional[Dict[str, str]] = None,
                 payload: Optional[Dict[str, Any]] = None, timeout: float = 30) -> Tuple[int, bytes]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else b""
        headers = sign_request(method, path, body, self.secret)  # підпис — лише path, без query
        headers["User-Agent"] = "SportNerdBot-AdminTasks/1.0"
        if body:
            headers["Content-Type"] = "application/json"
        url = self.base + path + (("?" + urllib.parse.urlencode(query)) if query else "")
        req = urllib.request.Request(url, data=body or None, method=method, headers=headers)
        return urlopen_with_deadline(req, timeout)

    def list(self, status: Optional[str] = None) -> list:
        code, data = self._request("GET", "/admin_tasks", {"status": status} if status else None)
        if code != 200:
            raise RuntimeError(f"list failed: HTTP {code} {data[:200]!r}")
        return json.loads(data)["tasks"]

    def claim(self, wait_s: Optional[float] = None) -> Optional[dict]:
        query = {"wait": str(wait_s)} if wait_s is not None else None
        timeout = max(40.0, (float(wait_s) + 15.0) if wait_s is not None else 40.0)
        code, data = self._request("GET", "/admin_tasks/claim", query=query, timeout=timeout)
        if code == 204:
            return None
        if code != 200:
            raise RuntimeError(f"claim failed: HTTP {code} {data[:200]!r}")
        return json.loads(data)["task"]

    def result(self, task_id: int, status: str, fix_commit: Optional[str], note: Optional[str]) -> dict:
        code, data = self._request("POST", f"/admin_tasks/{int(task_id)}/result",
                                   payload={"status": status, "fix_commit": fix_commit, "result": note})
        if code != 200:
            raise RuntimeError(f"result failed: HTTP {code} {data[:200]!r}")
        return json.loads(data)["task"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list"); p.add_argument("--status")
    p = sub.add_parser("claim"); p.add_argument("--wait", type=float, default=None)
    p = sub.add_parser("done"); p.add_argument("id", type=int); p.add_argument("--commit", required=True); p.add_argument("--note", default=""); p.add_argument("--note-file", help="UTF-8 файл з нотаткою (замість --note)")
    p = sub.add_parser("reject"); p.add_argument("id", type=int); p.add_argument("--note", default=""); p.add_argument("--note-file", help="UTF-8 файл з нотаткою (замість --note)")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    note = getattr(args, "note", "")
    if getattr(args, "note_file", None):
        with open(args.note_file, encoding="utf-8") as f:
            note = f.read().strip()
    if note and looks_mangled(note):
        raise SystemExit("note looks encoding-mangled ('????'); pass Cyrillic via --note-file (UTF-8), not the Windows command line")
    if args.cmd == "reject" and not note:
        raise SystemExit("reject needs --note or --note-file")

    secret = load_secret()
    url = os.getenv("CV_GATEWAY_URL", "")
    if not url:
        raise SystemExit("CV_GATEWAY_URL is not set")
    client = AdminTasksClient(url, secret)
    if args.cmd == "list":
        out = client.list(args.status)
    elif args.cmd == "claim":
        out = client.claim(wait_s=args.wait)
    elif args.cmd == "done":
        out = client.result(args.id, "done_pending_deploy", args.commit, note)
    else:
        out = client.result(args.id, "rejected", None, note)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
