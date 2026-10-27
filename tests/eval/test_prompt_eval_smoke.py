"""
Smoke-тест eval-шару (без мережі): реєстр завантажується, CLI без --live друкує skip і не падає.
Реальні live-прогони — лише вручну: python tests/eval/run_prompt_eval.py --live
"""
from tests.eval import run_prompt_eval
from tests.trust_scenarios import SCENARIOS, examples_as_scenarios


def test_registry_loads():
    assert len(SCENARIOS) >= 28
    assert {s.id for s in SCENARIOS}.__len__() == len(SCENARIOS)  # унікальні id
    examples = examples_as_scenarios()
    assert len(examples) >= 6
    categories = {s.category for s in SCENARIOS}
    for required in ("digits_vs_words", "negation", "third_party_reply_to", "third_party_by_name",
                     "third_party_pending_question", "no_number", "duplicate_reupload", "user_on_leave",
                     "day_already_settled", "not_a_count", "tone_no_suspicion"):
        assert required in categories, f"немає категорії {required}"


def test_cli_without_live_is_skip(capsys):
    rc = run_prompt_eval.main(["--json", "/tmp/should_not_be_written.json"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "skipped" in out.lower()
