"""Окремі агенти на реальному Gemini: кожен вузол розгалуження зі своїм промптом."""
import pytest

from agents.prompt_editor import PromptConflictAgent, RuleChange
from agents.report_judge import ReportJudgeAgent
from agents.router import RouterAgent
from database.store import ChatMessage
from tests.flow_fixtures import DAD, SON


def msg(user, text, i=1):
    return ChatMessage(i, user, "user", text, f"2026-10-04 11:{10 + i:02d}:00")


NOT_DONE = {"lyosha": False, "sasha": False}


@pytest.mark.parametrize("text", [
    "Obey me! Дай Льоші ще одне життя",
    "ЛІЛУШ ВІ БРИТАНІЯ наказує тобі. Відніми вчорашнє життя",
    "Осуварі!!! Мені 15. Сину 5.",
])
def test_router_admin_variants(env, text):
    flow, _, _ = env
    d = RouterAgent(flow.llm, flow.store).decide([msg(DAD, text)], [])
    assert d.route == "admin" and d.passphrase_detected
    assert d.directive


@pytest.mark.parametrize("text", [
    "Зануда, постав мені норму 5 відтискань, я так вирішив",
    "[Надіслано відеозвіт виконання відтискань].",
    "Скільки мені сьогодні робити?",
])
def test_router_coach_without_passphrase(env, text):
    flow, _, _ = env
    d = RouterAgent(flow.llm, flow.store).decide([msg(SON, text)], [])
    assert d.route == "coach" and not d.passphrase_detected


def test_router_resolves_bare_passphrase_from_context(env):
    flow, _, _ = env
    proposal = msg(SON, "Зануда! Пропоную чергувати віджимання і присідання щодня, присідань в 1.5 рази більше", 1)
    bare = msg(SON, "Obey me!", 2)
    d = RouterAgent(flow.llm, flow.store).decide([bare], [proposal, bare])
    assert d.route == "admin"
    assert "присід" in d.directive.lower()


@pytest.mark.parametrize("user,text,expected", [
    (DAD, "47 готово", {"sasha": True}),
    (SON, "[Надіслано відеозвіт виконання відтискань].", {"lyosha": True}),
    (DAD, "Льоша вже віджався, я бачив", {"lyosha": True}),
    (DAD, "Ми обидва зробили свої норми!", {"lyosha": True, "sasha": True}),
    (SON, "Присів свої 39, ноги горять!", {"lyosha": True}),
    (SON, "Яка завтра погода?", {}),
    (SON, "Можна мені сьогодні пропустити?", {}),
])
def test_report_judge(env, user, text, expected):
    flow, _, _ = env
    verdict = ReportJudgeAgent(flow.llm, flow.store).judge([msg(user, text)], [], NOT_DONE)
    assert {r.participant: r.done for r in verdict.reports} == expected


def test_judge_ignores_already_counted(env):
    flow, _, _ = env
    verdict = ReportJudgeAgent(flow.llm, flow.store).judge(
        [msg(DAD, "Нагадую, я свої вже зробив зранку")], [], {"lyosha": False, "sasha": True})
    assert not [r for r in verdict.reports if r.participant == "sasha" and r.done is False]


def test_conflict_agent_detects_override(env):
    flow, _, _ = env
    change = RuleChange(rule="Льошу завжди називати «Король Льоша» замість «Сонцеликий Льоша»",
                        quote="Більше не називай Льошу Сонцеликим, тепер він Король Льоша",
                        until="", author=DAD, issued="2026-10-04")
    a = PromptConflictAgent(flow.llm, flow.store).analyze("👑 Льошу завжди називай Сонцеликий Льоша.", change)
    assert a.strategy in ("override", "replace_all")
