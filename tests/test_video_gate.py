"""
Офлайн-тести для T-30: Gate + ffprobe metadata + dedup у боті.
Перевіряє всі gate-правила та роботу Store з таблицею video_checks.
"""
from types import SimpleNamespace
import pytest

from database.store import Store, User
from tests.flow_fixtures import CHAT, DAD, SON, make_flow


@pytest.mark.asyncio
async def test_store_video_checks_crud(tmp_path):
    store = Store(str(tmp_path / "test.db"))
    store.init_schema()

    # 1. create
    vc = store.create_video_check(
        request_id="req-1",
        chat_id=CHAT,
        message_id=101,
        user_slug="lyosha",
        file_id="fid_123",
        file_unique_id="fuid_abc",
        duration_s=15.5,
    )
    assert vc.id is not None
    assert vc.request_id == "req-1"
    assert vc.status == "queued"
    assert vc.duration_s == 15.5
    assert vc.valid is False

    # 2. get by request_id, id, file_unique_id
    by_req = store.get_video_check("req-1")
    assert by_req is not None and by_req.id == vc.id

    by_id = store.get_video_check_by_id(vc.id)
    assert by_id is not None and by_id.request_id == "req-1"

    by_fuid = store.get_video_check_by_file_unique_id("fuid_abc")
    assert by_fuid is not None and by_fuid.request_id == "req-1"

    # 3. claim_next_video_check
    claimed = store.claim_next_video_check()
    assert claimed is not None
    assert claimed.id == vc.id
    assert claimed.status == "running"

    # Second claim returns None because queue is empty
    claimed_again = store.claim_next_video_check()
    assert claimed_again is None

    # 4. update_video_check
    store.update_video_check(
        "req-1",
        status="done",
        exercise="pushup",
        reps=32,
        confidence=0.88,
        flags=["front_camera"],
        valid=True,
        analyzer_version="cv-1.0",
        finished=True,
    )
    updated = store.get_video_check("req-1")
    assert updated.status == "done"
    assert updated.exercise == "pushup"
    assert updated.reps == 32
    assert updated.confidence == 0.88
    assert updated.flags == ["front_camera"]
    assert updated.valid is True
    assert updated.finished_at is not None

    # 5. list_video_checks
    checks = store.list_video_checks(status="done")
    assert len(checks) == 1
    assert checks[0].request_id == "req-1"


@pytest.mark.asyncio
async def test_gate_rejects_animation_gif(tmp_path):
    flow, sender, _ = make_flow(tmp_path, llm=None)
    user = flow.store.get_user("lyosha")

    passed, reply = await flow.handle_video(
        CHAT, user, message_id=1, is_animation=True, file_id="gif1", file_unique_id="uid_gif"
    )
    assert passed is False
    assert "гіфка" in reply.lower() or "анімація" in reply.lower()
    assert len(flow.store.list_video_checks()) == 0
    assert sender.texts[-1] == reply
    assert flow.store.get_day_progress("2026-10-04")["lyosha"] is False


@pytest.mark.asyncio
async def test_gate_rejects_short_video(tmp_path):
    flow, sender, _ = make_flow(tmp_path, llm=None)
    user = flow.store.get_user("lyosha")

    # duration 3.5s < 5s
    passed, reply = await flow.handle_video(
        CHAT, user, message_id=2, duration=3.5, file_id="vid_short", file_unique_id="uid_short"
    )
    assert passed is False
    assert "коротке" in reply.lower() or "5 секунд" in reply.lower()
    assert len(flow.store.list_video_checks()) == 0
    assert sender.texts[-1] == reply
    assert flow.store.get_day_progress("2026-10-04")["lyosha"] is False


@pytest.mark.asyncio
async def test_gate_rejects_large_video(tmp_path):
    flow, sender, _ = make_flow(tmp_path, llm=None)
    user = flow.store.get_user("lyosha")

    # file_size 25MB > 20MB
    passed, reply = await flow.handle_video(
        CHAT, user, message_id=3, duration=20.0, file_size=25 * 1024 * 1024,
        file_id="vid_big", file_unique_id="uid_big"
    )
    assert passed is False
    assert "20mb" in reply.lower() or "стисни" in reply.lower()
    assert len(flow.store.list_video_checks()) == 0
    assert sender.texts[-1] == reply
    assert flow.store.get_day_progress("2026-10-04")["lyosha"] is False


@pytest.mark.asyncio
async def test_gate_rejects_duplicate_video(tmp_path):
    flow, sender, _ = make_flow(tmp_path, llm=None)
    user = flow.store.get_user("lyosha")

    # Перше валідне відео — приймається
    passed1, reply1 = await flow.handle_video(
        CHAT, user, message_id=4, duration=25.0, file_size=5 * 1024 * 1024,
        file_id="vid_valid1", file_unique_id="uid_same"
    )
    assert passed1 is True
    assert "попкорн" in reply1.lower()
    assert len(flow.store.list_video_checks()) == 1

    # Повтор з тим самим file_unique_id — відхиляється як дублікат
    passed2, reply2 = await flow.handle_video(
        CHAT, user, message_id=5, duration=25.0, file_size=5 * 1024 * 1024,
        file_id="vid_valid2", file_unique_id="uid_same"
    )
    assert passed2 is False
    assert "вже бачив" in reply2.lower() or "дублікат" in reply2.lower()
    assert len(flow.store.list_video_checks()) == 1  # новий запис не створено


@pytest.mark.asyncio
async def test_gate_accepts_valid_video_and_does_not_prematurely_credit(tmp_path):
    flow, sender, _ = make_flow(tmp_path, llm=None)
    user = flow.store.get_user("lyosha")

    passed, reply = await flow.handle_video(
        CHAT, user, message_id=6, duration=35.0, file_size=8 * 1024 * 1024,
        file_id="vid_123", file_unique_id="uid_unique_1"
    )
    assert passed is True
    assert reply == "Беру попкорну і йду дивитись відео"
    # CV_OFFLOAD=1 (default): job іде в box-чергу cv_jobs, VM-ний worker його не бере
    checks = flow.store.list_video_checks(status="offloaded")
    assert len(checks) == 1
    assert flow.store.get_cv_job(checks[0].request_id)["status"] == "queued"
    assert flow.store.get_cv_job(checks[0].request_id)["processing_sent_at"] is not None
    assert checks[0].file_id == "vid_123"
    assert checks[0].user_slug == "lyosha"
    assert checks[0].chat_id == CHAT
    assert checks[0].message_id == 6

    # Прогрес користувача НЕ повинен бути зарахований завчасно!
    assert flow.store.get_day_progress("2026-10-04")["lyosha"] is False
