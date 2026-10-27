# Тест-набір довіри (TRUST_MODE): суперечки про кількість повторів

М'який режим довіри означає: **слова людей важливіші за підрахунок CV**; число від будь-якого учасника про чуже
відео зараховується **власнику відео**; без підозр, без «не зараховано» від CV, без лекцій; усі тексти — українською
(див. `docs/SPEC.md`, `docs/FLOWS.md`, `prompts_core/trust_core.md`).

Цей набір захищає поведінку від регресій і служить еталоном **до тюнінгу** промптів. Єдине джерело сценаріїв —
`tests/trust_scenarios.py` (`SCENARIOS` + приклади ядра `prompts_core/trust_examples.json`). Воно живить **два шари**.

## Два шари

| Шар | Файл | LLM | Коли | Що перевіряє |
|-----|------|-----|------|--------------|
| **Детермінований** | `tests/test_trust_scenarios.py`, `tests/test_trust_core.py` | мок (`ScriptedToolLLM`) | завжди в CI | що ІДЕАЛЬНИЙ виклик `report_reps` + код flow дають правильний стан БД і тон; інваріанти захищеного ядра; промпти без строгих формулювань |
| **Live-eval** | `tests/eval/run_prompt_eval.py` | реальний `core.llm.GeminiLLM` | вручну | чи реальна модель за чинними промптами робить правильний виклик і чи стан БД збігається з `expect` |

Обидва шари будують **один і той самий** `ChatFlow` (`tests.flow_fixtures.make_flow`), ставлять однаковий контекст
(`setup_scenario`) і прокручують однакові повідомлення (`run_action`), а потім асертять однаковий `expect` (`verify`).
Відмінність лише в тому, який `llm` підкладено: мок зі сценарним `tool_call` чи справжня модель.

День у фікстурах: **2026-10-04**, вправа дня — **віджимання**, норми: Льоша **37**, Саша **47**; Діма (`kovdimay`)
створюється за потреби з нормою 36.

## Як запускати

**Детермінований (швидко, без мережі):**
```bash
.venv/bin/python -m pytest -q tests/test_trust_scenarios.py tests/test_trust_core.py tests/eval
```
Очікувано **падає лише** `test_no_strict_phrases_in_editable_prompts` — це еталон «до тюнінгу»: він перелічує строгі/
підозрілі формулювання в редагованих промптах (`дуже строгий і вимогливий`, `Відмовляй жорстко`, `не суперечи судді`,
`0 життів — дискваліфікація, драматично оголоси`, `гострий вердикт щодо їхніх шансів вижити`, `по-тренерськи сувору
тональність`, `суворий і дотепний`). Коли промпти пом'якшать — тест позеленіє (жодного `xfail`).

**Live-eval (реальні виклики Gemini, гроші/час):**
```bash
python tests/eval/run_prompt_eval.py                         # без --live: «skipped: need --live»
python tests/eval/run_prompt_eval.py --live                  # усі сценарії
python tests/eval/run_prompt_eval.py --live --only words_twenty_three,third_party_reply_zarahuj
python tests/eval/run_prompt_eval.py --live --repeat 3        # pass-rate при недетермінізмі
python tests/eval/run_prompt_eval.py --live --json out.json   # ще й JSON
```
Ключ береться з `.env` через `core.config` (`GEMINI_API_KEY`, `CANDIDATE_MODELS`) — **ключ ніколи не друкується**.
Виклики послідовні з паузою (без паралелі). Код виходу 0 лише якщо всі сценарії PASS. Сам eval мережі не вантажить CPU,
тож `/workspace/bin/heavy` для нього не потрібен; але якщо хочете запустити разом із важким CV-прогоном, обгортайте важку
частину: `/workspace/bin/heavy --who Pipebot-GodFather .venv/bin/python -m pytest …`.

## Сценарії (детермінований реєстр `SCENARIOS`, 34 шт.)

За категоріями: digits_vs_words 6; negation 2; correction_lower 1 / correction_higher 2; own_count 2;
third_party_reply_to 2; third_party_by_name 2; third_party_pending_question 2; caption_or_text_before_video 2;
exercise_mismatch 2; no_number 1; duplicate_reupload 1; user_on_leave 1; day_already_settled 1; joking_sarcasm 2;
two_people_disagree 1; flood_idempotent 1; not_a_count 2; tone_no_suspicion 1. Плюс **9 сценаріїв із прикладів ядра**
(`examples_as_scenarios()`, інваріант (f) у `tests/test_trust_core.py`).

| id | категорія | що перевіряє |
|----|-----------|--------------|
| words_twenty_three | digits_vs_words | «двадцять три» словами → 23 |
| words_half_hundred | digits_vs_words | «півсотні» → 50 |
| words_forty_five | digits_vs_words | «сорок п'ять» → 45 |
| words_sotka | digits_vs_words | сленг «сотка» → 100 |
| approx_around_20 | digits_vs_words | «десь 20» → 20 (нижча впевненість) |
| approx_about_30 | digits_vs_words | «≈30» → 30 |
| negation_dima_solidarity | negation | «50 там і не пахне, я 23 зробив» → 23, не 50 |
| negation_not_50_but_23 | negation | «не 50, а 23» → 23 |
| correction_lower_than_cv | correction_lower | CV 50 → людина 23, день не закрито |
| correction_higher_than_cv | correction_higher | CV 34 → людина 36 (<норми 37) |
| correction_higher_meets_norm | correction_higher | CV 4 → людина 40, день закрито |
| own_count_default_subject | own_count | власне число автора про своє відео |
| own_count_no_video_today | own_count | число без відео сьогодні → claim ігнорується |
| third_party_reply_zarahuj | third_party_reply_to | Саша reply-to про відео Льоші «зараховуй 36» → Льоші |
| third_party_reply_36_vyyshlo | third_party_reply_to | Саша reply-to «36 вийшло» → Льоші |
| third_party_by_name_zapysuy | third_party_by_name | «записуй Льоші 36» → Льоші, Сашу не чіпати |
| third_party_by_name_cases | third_party_by_name | «Льоша зробив 36» → Льоші |
| pending_q_answered_by_other | third_party_pending_question | бот спитав про Льошу, відповів Саша → Льоші |
| pending_q_answered_self | third_party_pending_question | бот спитав Льошу, відповів Льоша |
| caption_words | caption_or_text_before_video | число словами в підписі → 23 |
| caption_not_a_count | caption_or_text_before_video | число в підписі про час → CV, тул не викликано |
| mismatch_cv_squat_person_pushups | exercise_mismatch | CV присідання, людина «23 віджимання» → на вправу дня |
| mismatch_cv_pushup_person_squats | exercise_mismatch | CV віджимання, людина «23 присідання» → кредит на присідання, день не закрито |
| no_number_ask_once | no_number | «порахуй ще раз» без числа → одне уточнення, повторне — без дублю |
| duplicate_reupload_no_double | duplicate_reupload | повторне завантаження того самого відео не дає подвійного заліку |
| leave_sick_correction_no_penalty | user_on_leave | на лікарняному: число правиться, життя/XP/норма без змін, без докорів |
| settled_day_dispute | day_already_settled | день пораховано — **поточна** поведінка: диспут усе одно застосовується (GAP) |
| joke_thousand | joking_sarcasm | явний жарт «1000 😂» — політика ядра: береться заявлене число (ВІДКРИТЕ ПИТАННЯ) |
| joke_norm_vs_done | joking_sarcasm | «норма 50? та я 23 зробив» → 23 (50 — норма) |
| two_people_disagree_last_wins | two_people_disagree | Саша 36, Льоша 34 про відео Льоші → **останнє слово** 34 (ВІДКРИТЕ ПИТАННЯ) |
| flood_same_correction | flood_idempotent | 5 однакових «40» → одна спроба, XP не подвоюється |
| not_a_count_norm | not_a_count | «норма 36» → тул не викликати |
| not_a_count_sets | not_a_count | «3 підходи» → тул не викликати |
| tone_after_accept_no_doubt | tone_no_suspicion | після прийнятого числа — без сумніву/докору/вердикту CV |

У `verify()` до **всіх** відповідей бота застосовується універсальна заборона фраз (`FORBIDDEN_IN_REPLY`:
«не зараховано», REDO-слова, «шахрай/обман/підозр/брехн») — це закриває категорію tone_no_suspicion скрізь.

## Інваріанти захищеного ядра (`tests/test_trust_core.py`)

- (a) зібрані промпти coach/reps_claim закінчуються блоком ядра і містять усі приклади; ядро — після coach_dynamic і STATUS_MANDATE;
- (b) «брудний» coach_base/coach_dynamic (суворість + підмінений/незакритий блок ядра) не прибирає ядро; `save_prompt` зберігає текст без блоку;
- (c) `CoachPromptEditor.apply(replace_all)` і `.reset()` не зачіпають ядро (навіть коли merger намагається вшити підміну);
- (d) `AdminAgent` має read-only `get_trust_examples` (повертає блок ядра), `ADMIN_PROMPT` його згадує, тулу-запису в ядро немає;
- (e) валідація `trust_examples.json`: унікальні id, обов'язкові поля, slug-и лише `sasha/lyosha/kovdimay`, `count` 1..1000, ≥6 прикладів, набір кейсів (reply-to, by name, pending, слова, приблизність, заперечення, без числа);
- (f) кожен приклад ядра → детермінований сценарій (той самий runner/verify).

## Як додати сценарій

1. У `tests/trust_scenarios.py` додайте `Scenario(...)` у список `SCENARIOS`:
   - `videos=[VideoSpec(owner, rid, cv_exercise, cv_reps, flags=…, caption=…, claim_tool=…)]` — контекст відео;
   - `turns=[Turn(author, text, reply_to=…, tool=ToolCall(...) | None)]` — повідомлення (по одному `tool` на повідомлення; `None` = тул не викликати). Для сценаріїв «з підпису» лишіть `turns` порожнім і задайте `caption`/`claim_tool` у `VideoSpec`;
   - `pending_question=PendingQuestion(subject, rid, asked_by)` — якщо бот уже спитав «скільки вийшло?»;
   - `leaves`, `settle_before`, `extra_users` — за потреби;
   - `expect=Expect(best_attempt=…, done=…, credit=…, bot_msg_count=…, must_contain=…, must_not_contain=…, dispute_wait_set/cleared=…, xp_zero=…)`.
2. Детермінований шар підхопить сценарій автоматично (parametrize). Запустіть `pytest tests/test_trust_scenarios.py -k <id>`.
3. Live-eval теж підхопить його автоматично (`--only <id>`).
4. Якщо сценарій вимагає зміни **коду** (а не промптів) — додайте рядок у «GAPS» у звіті/PR і лишіть `open_question` у сценарії.

> `ScriptedToolLLM` викликає `report_reps` лише коли в наборі тулів **рівно** цей тул (тобто `RepsClaimAgent`), і бере
> черговий `tool`/`claim_tool` з черги FIFO. Coach (кілька тулів) не чіпається — щоб claim не застосовувався двічі.
