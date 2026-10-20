"""
Офлайн-тести для cv_worker/worker.py (T-32).
Тестує атомарний claim, обробку черги з concurrency=1, очистку тимчасових файлів та обробку помилок.
"""
import os
from types import SimpleNamespace
import pytest

from cv_worker.worker import CVWorker
from database.store import Store


def test_cv_worker_claim_when_empty(tmp_path):
    store = Store(str(tmp_path / "cv.db"))
    store.init_schema()
    worker = CVWorker(store=store)

    assert worker.claim_one() is None


def test_cv_worker_claim_and_concurrency(tmp_path):
    store = Store(str(tmp_path / "cv.db"))
    store.init_schema()

    vc1 = store.create_video_check(
        request_id="req-1",
        chat_id=-100,
        message_id=10,
        user_slug="lyosha",
        file_id="fid_1",
        duration_s=20.0,
    )
    vc2 = store.create_video_check(
        request_id="req-2",
        chat_id=-100,
        message_id=11,
        user_slug="sasha",
        file_id="fid_2",
        duration_s=25.0,
    )

    worker = CVWorker(store=store)

    # Перший claim бере найстаріший (req-1)
    claimed1 = worker.claim_one()
    assert claimed1 is not None
    assert claimed1.request_id == "req-1"
    assert claimed1.status == "running"

    # Другий claim бере req-2
    claimed2 = worker.claim_one()
    assert claimed2 is not None
    assert claimed2.request_id == "req-2"
    assert claimed2.status == "running"

    # Третій claim — черга порожня
    assert worker.claim_one() is None


def test_cv_worker_process_one_success_and_cleanup(tmp_path):
    store = Store(str(tmp_path / "cv.db"))
    store.init_schema()

    vc = store.create_video_check(
        request_id="req-succ",
        chat_id=-100,
        message_id=20,
        user_slug="lyosha",
        file_id="fid_succ",
        duration_s=30.0,
    )

    observed_files = []
    callbacks_received = []

    def fake_downloader(token, fid, dest_path):
        observed_files.append(dest_path)
        with open(dest_path, "wb") as f:
            f.write(b"dummy_mp4_bytes")

    def fake_analyzer(path, **kwargs):
        assert os.path.exists(path)
        return {
            "exercise": "pushup",
            "reps": 33,
            "confidence": 0.89,
            "flags": ["front_camera"],
            "valid": True,
            "analyzer_version": "cv-1.0",
        }

    def fake_callback(req_id, res):
        callbacks_received.append((req_id, res))

    worker = CVWorker(
        store=store,
        bot_token="fake_token",
        downloader=fake_downloader,
        analyzer=fake_analyzer,
        callback_handler=fake_callback,
    )

    claimed = worker.claim_one()
    assert claimed is not None
    res = worker.process_one(claimed)

    # 1. Результат успішний
    assert res["exercise"] == "pushup"
    assert res["reps"] == 33
    assert res["valid"] is True

    # 2. Статус у БД став 'done'
    updated = store.get_video_check("req-succ")
    assert updated.status == "done"
    assert updated.reps == 33
    assert updated.exercise == "pushup"
    assert updated.confidence == 0.89
    assert updated.valid is True
    assert updated.finished_at is not None

    # 3. Тимчасовий файл видалено
    assert len(observed_files) == 1
    assert not os.path.exists(observed_files[0])

    # 4. Callback викликано
    assert len(callbacks_received) == 1
    assert callbacks_received[0][0] == "req-succ"
    assert callbacks_received[0][1]["reps"] == 33


def test_cv_worker_process_one_error_handling(tmp_path):
    store = Store(str(tmp_path / "cv.db"))
    store.init_schema()

    vc = store.create_video_check(
        request_id="req-err",
        chat_id=-100,
        message_id=30,
        user_slug="sasha",
        file_id="fid_err",
    )

    observed_files = []

    def failing_analyzer(path, **kwargs):
        observed_files.append(path)
        raise RuntimeError("Corrupted video file")

    worker = CVWorker(
        store=store,
        bot_token="fake_token",
        downloader=lambda t, f, dest: open(dest, "wb").write(b"corrupt"),
        analyzer=failing_analyzer,
    )

    claimed = worker.claim_one()
    assert claimed is not None
    res = worker.process_one(claimed)

    # 1. Повертає failed результат
    assert res["status"] == "failed"
    assert "Corrupted video file" in res["error"]

    # 2. Статус у БД став 'failed'
    updated = store.get_video_check("req-err")
    assert updated.status == "failed"
    assert any("worker_error" in flag for flag in updated.flags)

    # 3. Тимчасовий файл все одно прибрано
    assert len(observed_files) == 1
    assert not os.path.exists(observed_files[0])
