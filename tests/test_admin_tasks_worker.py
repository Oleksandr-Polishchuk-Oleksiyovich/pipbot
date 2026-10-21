from tools.admin_tasks_worker import AdminTasksWorker, task_payload

CFG = {"url": "http://hook", "header": "Authorization", "value": "Bearer k"}
TASK = {"id": 7, "text": "додай рейтинг", "requester_name": "Саша", "created_at": "2026-10-21 15:00:00"}


class Client:
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def claim(self):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_204_repolls_without_sleep_and_errors_backoff():
    sleeps = []
    w = AdminTasksWorker(Client([None, RuntimeError("x"), RuntimeError("y"), None]), get_config=lambda: CFG,
                         poster=lambda c, p: 200, sleep=sleeps.append)
    for _ in range(4):
        w.step()
    assert sleeps == [1, 2]
    assert w.errors == 0


def test_backoff_caps_at_10():
    sleeps = []
    w = AdminTasksWorker(Client([RuntimeError()] * 6), get_config=lambda: CFG, poster=None, sleep=sleeps.append)
    for _ in range(6):
        w.step()
    assert sleeps == [1, 2, 5, 10, 10, 10]


def test_delivers_with_retry():
    sent, sleeps = [], []
    codes = iter([500, None, 201])

    def poster(cfg, payload):
        sent.append(payload)
        c = next(codes)
        if c is None:
            raise OSError("conn reset")
        return c
    w = AdminTasksWorker(Client([TASK]), get_config=lambda: CFG, poster=poster, sleep=sleeps.append)
    assert w.step() is True
    assert len(sent) == 3 and sent[0] == task_payload(TASK)
    assert sent[0] == {"task_id": 7, "text": "додай рейтинг", "requester": "Саша", "created_at": "2026-10-21 15:00:00"}
    assert sleeps == [1, 2]


def test_no_webhook_config_does_not_claim():
    sleeps = []
    c = Client([TASK])
    w = AdminTasksWorker(c, get_config=lambda: None, poster=None, sleep=sleeps.append)
    assert w.step() is False
    assert c.calls == 0 and sleeps == [60]


def test_webhook_config_header(monkeypatch):
    from tools import admin_tasks_worker as m
    monkeypatch.setenv("ADMIN_TASKS_WEBHOOK_URL", "http://h")
    monkeypatch.setenv("ADMIN_TASKS_WEBHOOK_KEY", "abc")
    monkeypatch.delenv("ADMIN_TASKS_WEBHOOK_HEADER", raising=False)
    assert m.webhook_config()["value"] == "Bearer abc"
    monkeypatch.setenv("ADMIN_TASKS_WEBHOOK_HEADER", "X-Api-Key")
    assert m.webhook_config() == {"url": "http://h", "header": "X-Api-Key", "value": "abc"}
    monkeypatch.delenv("ADMIN_TASKS_WEBHOOK_KEY")
    assert m.webhook_config() is None
