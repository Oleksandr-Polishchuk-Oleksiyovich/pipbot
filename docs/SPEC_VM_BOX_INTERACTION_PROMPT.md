# Prompt: специфікація взаємодії VM ↔ box (для ревʼю моделлю)

Цей файл — промпт, яким модель-ревʼюер (Claude Opus; fallback — agy `gemini-4-argon-high`) дописує й перевіряє
`docs/SPEC_VM_BOX_INTERACTION.md` проти реального коду. Мова документів — українська (код/ідентифікатори — як є).

## Роль і завдання
Ти — старший інженер-ревʼюер. Репозиторій: `/workspace/pipbot` (SportNerdBot: Telegram-бот на VM у Google Cloud + «box» —
ефемерна машина Grok Bot, що рахує відео й виконує admin-задачі). Заверши й перевір **нормативну специфікацію**
`docs/SPEC_VM_BOX_INTERACTION.md` (якщо файлу ще немає — створи його; якщо є — доповни/виправ):

1. Прочитай код через `rg`/читання файлів: `core/wake.py`, `gateway/cv_queue.py`, `gateway/admin_tasks_api.py`,
   `gateway/ops_api.py`, `gateway/app.py`, `database/store.py` (cv_jobs, admin_tasks, video_checks, bot_state),
   `app/flow.py` (process_cv_results, check_overdue/still_counting, box_wake_watchdog, announce_admin_tasks),
   `cv_worker/box_worker.py`, `tools/admin_tasks_worker.py`, `tools/admin_tasks.py`, `tools/ensure_workers.sh`,
   `cv_worker/run_box_worker.sh`, `tools/run_admin_tasks_worker.sh`, `deploy/` (autodeploy, defer-while-queued), `docs/SPEC.md`, `docs/FLOWS.md`.
2. Для КОЖНОЇ вимоги нижче перевір, чи реалізована, і заповни таблицю відповідності
   «вимога → реалізовано? (✅ / ⚠️ частково / ❌) → посилання на код (`файл:функція`) → примітка».
   Жодних «✅» без підтвердження в коді; кожне посилання перевір через `rg`.
3. Знайди **суперечності** (між специфікацією, SPEC.md/FLOWS.md і кодом, між вимогами) і випиши розділом «Суперечності»
   з рішенням для кожної (що вважати нормою).
4. Дорости відсутнє: стани й переходи, таймаути/числа (бери фактичні константи з коду: `CV_LEASE_S`, `CV_STILL_COUNTING_MIN`,
   `CV_MAX_ATTEMPTS`, інтервали watchdog/rate-limit у `core/wake.py` тощо), діаграми, формати payload, ідемпотентність.
5. Додай розділ «Прогалини та наступні кроки» — пронумерований, відсортований за пріоритетом, з оцінкою ризику.
6. **Не змінюй код** (ні Python, ні shell) — тільки документи: `docs/SPEC_VM_BOX_INTERACTION.md`, за потреби короткі
   посилання з `docs/SPEC.md`, `docs/FLOWS.md` і `README.md` (індекс документів). Секрети/.env не читати й не друкувати.
7. Наприкінці виведи короткий звіт: що додано/змінено, список суперечностей, список прогалин, чи специфікація повна.

## Нормативні вимоги (власник; MUST/SHOULD)
R1. **VM — ініціатор.** VM (Google Cloud) MUST першою «стукати» у box вебхуком (routine Grok Bot «SportNerdBot box wake»
    для відео; MUST існувати аналог для admin_tasks), щоб розбудити box/воркер, і MUST передавати в payload дані
    (job id / task id, kind, reference). Box MUST NOT мати вхідних зʼєднань.
R2. **VM опитує статус делегованих задач** (patch/admin_task, відео): MUST визначити інтервал опитування, таймаути, lease,
    набір статусів і поведінку VM при відсутності відповіді: повторне пробудження з ретраями (backoff), MUST не більше
    одного «мʼякого» повідомлення в чат (одне на задачу), повернення в чергу після спливу lease (з лімітом спроб).
R3. **Завершення**: box MUST повідомити результат hook-ом (POST результату на gateway) АБО VM MUST зловити готовий статус
    наступним опитуванням. Обидва шляхи MUST бути ідемпотентні (job id + claim token / нонс), результат приймається рівно
    один раз; повтор → `already_done`/409 без побічних ефектів; чужий/прострочений токен → відхилення.
R4. **Збої** (кожен — окремий підрозділ: симптом → поведінка VM → поведінка box → відновлення → статус ✅/❌):
    box спить/призупинений; крах воркера; дубльований результат; перезапуск VM посеред задачі; **деплой під час задачі**
    (video-first: autodeploy MUST відкладатись, поки є queued/claimed cv_jobs); 502 від gateway/nginx під час деплою;
    втрата/дублікат вебхука; вебхук без секрету/не налаштований; розсинхрон годинників (HMAC ±5 хв).
R5. **Станові машини** (mermaid `stateDiagram-v2`): `cv_jobs` (queued → claimed → done|failed, expired/re-queue, attempts),
    `admin_tasks` (pending → claimed → done_pending_deploy → announced | rejected, + що буде при простроченні claim),
    а також допоміжні `video_checks`, `pending_replies`. Для кожного переходу: хто ініціює (VM/box/час), умова, побічні ефекти.
R6. **Діаграми послідовності** (mermaid `sequenceDiagram`): (a) відео happy path з wake; (b) відео при сплячому box
    (wake → claim → result → reply); (c) admin_task (VM → box worker → routine webhook → agent → result → announce);
    (d) збій: crash воркера / lease expiry / re-queue; (e) деплой із відкладенням.
R7. **Таблиця відповідності** «вимога → реалізовано? → код» з ✅/⚠️/❌; прогалини мають бути видимі.
R8. Секрети/ключі/URL у документах MUST NOT наводитись (лише назви змінних).

## Що вже відомо (перевір, не приймай на віру)
- Box не має вхідних зʼєднань; box-воркер long-poll-ить `/cv/claim` (≈25 с) і `/admin_tasks/claim`, результати POST-ить на
  `/cv/jobs/{rid}/result`, `/admin_tasks/{id}/result` (HMAC, нонс одноразовий).
- VM будить box для CV: `core/wake.py` (BoxWaker; fire = 3 спроби backoff 2/5/10 с; watchdog 60 с; rate-limit 5 хв;
  job у `claimed` у межах lease вважається живим; `wake_test` через `/admin/ops`). E2E 2026-10-26 підтверджено:
  routine запустилась (≈12:39 Київ), supervisor воркера стартував.
- Для admin_tasks wake з VM НЕМАЄ; VM-періодичного опитування статусу box НЕМАЄ (VM бачить лише вік поллінгу `cv_last_poll_ts` і lease).
- Admin_tasks пересилає box-воркер → routine «SportNerdBot admin task» (webhook); задача, вставлена як `claimed`, не пересилається.
- Інцидент 2026-10-24/25: box «спав» 13 год; відео чекали 9–11 год; усі 17 результатів доставлено (`updated_at`/`replied_at`
  ≤ 34 с після done); 502 на claim збігаються з autodeploy-рестартами VM.
