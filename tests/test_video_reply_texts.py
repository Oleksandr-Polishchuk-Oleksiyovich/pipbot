import pytest

from core.challenge import exercise_word, plural_uk
from prompts import DEFAULT_BOT_TEXTS

CATS = ("success", "near_miss", "far_below", "bad_quality", "wrong_exercise")


@pytest.mark.parametrize("n,word", [(1, "присідання"), (3, "присідання"), (4, "присідання"), (5, "присідань"),
                                    (11, "присідань"), (12, "присідань"), (21, "присідання"), (22, "присідання"),
                                    (51, "присідання"), (53, "присідання"), (0, "присідань"), (100, "присідань")])
def test_plural_squats(n, word):
    assert exercise_word("squats", n) == word
    assert exercise_word("squat", n) == word


def test_plural_pushups():
    assert exercise_word("pushup", 1) == "віджимання"
    assert exercise_word("pushups", 7) == "віджимань"
    assert plural_uk(14, ("a", "b", "c")) == "c"


@pytest.mark.parametrize("key", CATS)
def test_texts_start_with_seen_prefix_and_never_accuse(key):
    text = DEFAULT_BOT_TEXTS[key]
    assert text.startswith("Побачив {reps} {exercise} з {target}")
    low = text.lower()
    for bad in ("надур", "халтур", "обман", "чітинг", "читинг", "перезн", "ще раз"):
        assert bad not in low


def test_credit_wording_matches_category():
    for key in ("success", "bad_quality"):
        assert "зараховано" in DEFAULT_BOT_TEXTS[key] and "не зараховано" not in DEFAULT_BOT_TEXTS[key]
    for key in ("near_miss", "far_below", "wrong_exercise"):
        assert "не зараховано" in DEFAULT_BOT_TEXTS[key]


async def test_video_reply_uses_counted_exercise_and_plural(tmp_path):
    from tests.flow_fixtures import CHAT, make_flow
    flow, sender, _ = make_flow(tmp_path, llm=None)
    flow.store.create_video_check(request_id="v1", chat_id=CHAT, message_id=5, user_slug="lyosha",
                                  file_id="f", status="offloaded", location="box")
    flow.store.update_video_check("v1", status="done", exercise="pushup", reps=4, confidence=0.9,
                                  valid=True, flags=[], finished=True)
    reply = await flow.handle_video_callback("v1")
    assert reply.startswith("Побачив 4 віджимання з ")
    assert "не зараховано" in reply
