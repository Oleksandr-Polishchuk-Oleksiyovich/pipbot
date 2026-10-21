"""Завантаження файлів через Telegram Bot API getFile (спільне для gateway та VM CV worker)."""
import json
import urllib.request

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
