"""
ДЕТЕРМІНОВАНИЙ шар тест-набору довіри (TRUST_MODE) — завжди в CI, без мережі.

1. Параметризація по реєстру `SCENARIOS`: кожен сценарій будує ChatFlow із моканим LLM (ScriptedToolLLM),
   прокручує повідомлення й асертить `expect` (стан БД + тексти бота).
2. Тести ШАРУ ПРОМПТІВ (без LLM): редаговані промпти/кодові вставки не містять строгих/підозрілих формулювань
   (STRICT_PHRASES), а trust_*-тексти бота — без докорів/REDO/«не зараховано». Частина цих тестів МАЄ падати на
   поточному коді (еталон «до тюнінгу»), поки формулювання справді суперечать м'якому режиму.
"""
import pytest

from agents.coach import CoachAgent
from core import trust_core
from prompts import DEFAULT_BOT_TEXTS, DEFAULT_PROMPTS, REDO_WORDS, build_video_reply_tone_prompt
from tests.flow_fixtures import make_flow
from tests.trust_scenarios import (
    DAY_DATE,
    SCENARIOS,
    run_scenario,
    scripted_llm_for,
    verify,
)


# ---------------------------------------------------------------- сценарії
@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.id for s in SCENARIOS])
async def test_scenario(tmp_path, scenario):
    flow, sender, _ = make_flow(tmp_path, llm=scripted_llm_for(scenario), trust_mode=True)
    await run_scenario(flow, sender, scenario)
    fails = verify(flow, sender, scenario)
    assert not fails, f"[{scenario.id}] {scenario.category}:\n  " + "\n  ".join(fails)


# ---------------------------------------------------------------- шар промптів: STRICT_PHRASES
# Фрази, що суперечать М'ЯКОМУ режиму довіри (підозра / вердикт CV / прохання перезняти / надмірна суворість
# ЩОДО ЗАЯВЛЕНОГО ЧИСЛА). Персона може лишатися гумористичною та навіть «строгою» в онбордингу, але НЕ у ставленні
# до числа повторів. Для кожної фрази — чому вона шкідлива. Тести мають падати, поки ці рядки живі в промптах.
STRICT_PHRASES = {
    # coach_base: заявлена «дуже строга й вимоглива» персона переноситься й на оцінку заявленого числа.
    "дуже строгий і вимогливий": "персона-«строгий» тисне на оцінку заявлених повторів — підозра замість довіри",
    "Відмовляй жорстко": "жорстка відмова суперечить «не повчати, приймати слово людини»",
    "не суперечи судді": "у контексті чисел штовхає ігнорувати слово людини на користь вердикту (CV/судді)",
    # coach_dynamic (дефолт): драматичні погрози дискваліфікацією й «вердикт шансів вижити» = тиск і повчання.
    "дискваліфікація, драматично оголоси": "драматична погроза дискваліфікацією — тиск, несумісний із м'яким тоном",
    "гострий вердикт щодо їхніх шансів вижити": "винесення «вердикту» = повчання/суворість, заборонені ядром",
    # app.flow / prompts: тон перефразування відповіді на відео — «суворий».
    "по-тренерськи сувору тональність": "суворість у відповіді на відеозвіт конфліктує з «вірю на слово, без докорів»",
    "суворий і дотепний": "«суворий» у system-промпті перефразування відповіді на відео = суворість щодо результату",
    # Загальні маркери вердикту/підозри як ВИМОГИ до результату (guard від регресій; зараз у редагованих шарах їх нема).
    # Навмисно НЕ включаємо «підозр»/«доказ»/«перезняти» окремими коренями — у промптах вони вживаються саме в
    # ЗАБОРОННОМУ контексті («не вимагай доказів», «ніколи не проси перезняти»), і скан дав би хибні спрацювання.
    "не зараховано": "вердикт CV «не зараховано» заборонений у м'якому режимі",
    "шахрай": "звинувачення в шахрайстві — груба підозра",
    "обман щодо": "підозра в обмані щодо результату",
}


def _editable_prompt_texts(store) -> dict:
    """Редаговані шари та кодові вставки, які адмін/код реально показують моделі ПЕРЕД блоком ядра."""
    from app.flow import VIDEO_REPLY_PARAPHRASE_SYSTEM

    coach_assembled = trust_core.strip_core(CoachAgent(None, store).build_system_prompt(DAY_DATE))
    return {
        "DEFAULT_PROMPTS[coach_base]": DEFAULT_PROMPTS["coach_base"],
        "DEFAULT_PROMPTS[coach_dynamic]": DEFAULT_PROMPTS["coach_dynamic"],
        "DEFAULT_PROMPTS[reps_claim]": DEFAULT_PROMPTS["reps_claim"],
        "DEFAULT_PROMPTS[admin]": DEFAULT_PROMPTS["admin"],
        "VIDEO_REPLY_PARAPHRASE_SYSTEM": VIDEO_REPLY_PARAPHRASE_SYSTEM,
        "build_video_reply_tone_prompt": build_video_reply_tone_prompt("Побачив 4 віджимання з 37."),
        "coach_prompt_before_core": coach_assembled,
    }


def test_no_strict_phrases_in_editable_prompts(tmp_path):
    """Еталон «до тюнінгу»: МАЄ падати, доки в редагованих промптах лишаються суворі/підозрілі формулювання."""
    flow, _, _ = make_flow(tmp_path, llm=None, trust_mode=True)
    texts = _editable_prompt_texts(flow.store)
    violations = []
    for where, text in texts.items():
        low = text.lower()
        for phrase in STRICT_PHRASES:
            if phrase.lower() in low:
                violations.append(f"{where}: «{phrase}» — {STRICT_PHRASES[phrase]}")
    assert not violations, "Знайдено строгі/підозрілі формулювання (прибрати при тюнінгу):\n  " + "\n  ".join(violations)


# ---------------------------------------------------------------- шар промптів: приклади few-shot
def test_reps_claim_prompt_has_examples_via_core(tmp_path):
    """
    У новій архітектурі few-shot «ПРИКЛАДИ» живуть у ЗАХИЩЕНОМУ ЯДРІ (prompts_core), а не в редагованому
    reps_claim. Перевіряємо, що ЗІБРАНИЙ системний промпт reps_claim містить блок ПРИКЛАДИ (≥6) і ключові кейси
    (Діма-заперечення, третя особа, відкрите питання, слова, приблизність), а coach спирається на ті самі правила.
    """
    from agents.reps_claim import RepsClaimAgent

    flow, _, _ = make_flow(tmp_path, llm=None, trust_mode=True)
    assembled = RepsClaimAgent(None, flow.store).system_prompt
    assert "ПРИКЛАДИ" in assembled
    assert len(trust_core.load_examples()) >= 6
    assert assembled.count("report_reps(") >= 6
    for needle in ("23 зробив", "записуй Льоші 36", "36 вийшло", "двадцять три", "десь 20"):
        assert needle in assembled, f"у прикладах ядра немає «{needle}»"
    # coach спирається на ті самі ПРАВИЛА ДОВІРИ / приклади (той самий блок ядра в кінці)
    coach_prompt = CoachAgent(None, flow.store).build_system_prompt(DAY_DATE)
    assert trust_core.has_core(coach_prompt) and "report_reps" in coach_prompt


# ---------------------------------------------------------------- шар промптів: trust_* тексти бота
def test_trust_bot_texts_are_soft():
    """Фіксовані trust_*-відповіді бота не містять REDO/докорів/«не зараховано»."""
    bad = ("не зараховано", "перезняти", "ще раз", "шахрай", "обман", "підозр") + tuple(REDO_WORDS)
    offenders = []
    for key, text in DEFAULT_BOT_TEXTS.items():
        if not key.startswith("trust_"):
            continue
        low = text.lower()
        for b in bad:
            if b and b.lower() in low:
                offenders.append(f"{key}: «{b}»")
    assert not offenders, "trust_*-тексти містять заборонені формулювання:\n  " + "\n  ".join(offenders)
