# SportNerdBot — специфікація застосунку (поточний стан)

> **Статус:** описує систему такою, якою вона працює зараз (жовтень 2026), за реальним кодом у цьому репозиторії.
> Старіший документ [`ARCHITECTURE.md`](ARCHITECTURE.md) — це «цільова» архітектура фаз 0–5; частина її (локальний
> `sportnerd-cv.service` на VM, офлоад `admin_dev` через `offload_requests`) уже замінена на CV та admin-tasks на box
> (див. розділ 2). Покрокові послідовності — у [`FLOWS.md`](FLOWS.md). Експлуатація й бекапи — у [`RUNBOOK.md`](RUNBOOK.md).
>
> Секрети (`BOT_TOKEN`, `GEMINI_API_KEY`, `GATEWAY_HMAC_SECRET`, ключ вебхука) у git та в цих доках **не зберігаються**;
> нижче вказано лише назви змінних. Користувач на VM — `sportnerd` (non-root), база — `/var/lib/sportnerdbot/challenge.db`.

---

## 1. Що це

Telegram-бот «Спорт Зануда» (`@SportNerdBot`) для щоденного челенджу з відтискань/присідань у чаті «Котяки-Віджимаки».
Бот: веде норми, життя, XP та рівні; приймає відеозвіти й текстові звіти; нагадує за розкладом; має адмін-режим за кодовою
фразою (швидкі зміни даних у боті та «довгі» запити розробнику — на box).

## 2. Де що працює

| # | Місце | Що там | Примітка |
|---|---|---|---|
| a | **Telegram** (чат «Котяки-Віджимаки») | Користувачі, Bot API (`getUpdates`/`sendMessage`/`getFile`) | Єдиний polling-процес (інакше `409 Conflict`) |
| b | **GCP VM `sportnerd-bot-vm`** (IP `35.254.7.29`, gateway `https://35.254.7.29.sslip.io`) | Бот, gateway, autodeploy, SQLite, Caddy (TLS → `127.0.0.1:8443`) | Джерело правди для даних |
| c | **Box** — віддалена Linux-машина Grok Bot (персистентний хмарний комп'ютер агентів користувача, не ноутбук) | CV worker, admin-tasks worker, `agy`/`claude` CLI, робоча копія репо `/workspace/pipbot`, lock `heavy` | Немає вхідних з'єднань: усе робиться pull-ом (long-poll) |
| d | **GitHub** (`Oleksandr-Polishchuk-Oleksiyovich/pipbot`) | Код; `origin/main` — джерело деплою | VM тягне сама |
| e | **Grok Bot cloud** | Webhook-routine «SportNerdBot admin task», що будить агента на box | Дзвінок приходить від admin-tasks worker |
| f | **LLM-провайдер** | Лише **Google Gemini** (`google-genai`, `core/llm.py`): текст/JSON-відповіді: router, judge, coach, admin-тули, онбординг, перефраз відповідей (vision-класифікатор кадрів є в `cv_worker/vision.py`, але box-CV його не викликає) | Список моделей `CANDIDATE_MODELS` у `core/config.py`, перша, що відповіла, піднімається нагору; rate-limiter ~15 RPM |

Ключові процеси:

- **VM (systemd):** `sportnerdbot.service` (aiogram + APScheduler, `/opt/sportnerdbot/current`), `sportnerd-gateway.service`
  (aiohttp `:8443`), `sportnerd-autodeploy.timer` + `.service` + `.path` (автодеплой), опційно `sportnerdbot-deploy.timer`
  (старий safety-net) та `sportnerd-cv.service` (CV на VM — **вимкнений**, CV рахує box). Лог бота — `bot.log`
  (ротація `LOG_RETENTION_DAYS`), змінні — `/etc/sportnerdbot/env` (0600), код — `/opt/sportnerdbot/releases/<sha>` +
  symlink `/opt/sportnerdbot/current`, репо для автодеплою — `/opt/sportnerdbot/repo`.
- **Box:** `cv_worker/box_worker.py` (запуск `cv_worker/run_box_worker.sh`), `tools/admin_tasks_worker.py`
  (`tools/run_admin_tasks_worker.sh`), `agy` (Antigravity CLI, `/home/box/.local/bin/agy`, модель Gemini Flash), `claude` CLI як fallback.
  Supervisor-скрипти — звичайні `nohup`-цикли (на box немає systemd --user/cron), тому **після рестарту box їх треба
  запустити вручну**: `nohup cv_worker/run_box_worker.sh &` та `nohup tools/run_admin_tasks_worker.sh &`.
- **Відомі обмеження box:** у простої box може бути призупинений (suspended) — тоді воркери не поллять, відео й admin-задачі
  чекають у черзі на VM. Для відео це закрито **wake-механізмом** (`core/wake.py`): коли відео ставиться в чергу, а CV worker
  не поллить довше `WAKE_STALE_S` (45 с), VM сама б'є вебхуком Grok Bot routine «SportNerdBot box wake» (3 спроби, backoff 2/5/10 с,
  будь-який 2xx = прийнято), чекає до `WAKE_WAIT_S` (90 с) на свіжий `/cv/claim`, за потреби повторює; watchdog у боті повторює
  не частіше ніж раз на 5 хв, поки job висить. Job увесь час лишається `queued` і ніколи не губиться; якщо worker так і не
  прокинувся — чат отримує **одне** м'яке повідомлення (`box_waking`). Без секрету вебхука або з `WAKE_ENABLED=0` фіча — no-op.
  Для `admin_tasks` аналогічного wake немає. Повна нормативна специфікація взаємодії VM↔box (станові машини, lease,
  ідемпотентність, діаграми послідовності, таблиця відповідності вимог, прогалини) — у
  [`SPEC_VM_BOX_INTERACTION.md`](SPEC_VM_BOX_INTERACTION.md).

## 3. Компоненти

| Компонент | Де | Що робить | Ключові файли | Як перевірити здоров'я |
|---|---|---|---|---|
| Бот | VM | Telegram-хендлери, ChatFlow, таймери | `bot.py`, `app/flow.py` | `systemctl status sportnerdbot`, `bot.log` |
| Gateway | VM | HTTP API для box/деплою/статусу | `gateway/app.py`, `gateway/cv_queue.py`, `gateway/admin_tasks_api.py` | `GET /healthz` → `{"status":"ok"}` |
| Autodeploy | VM | Тягне `origin/main`, деплоїть, синхронізує `vm_config.env` | `deploy/autodeploy/sportnerd-autodeploy.sh`, `deploy/deploy.sh` | `GET /autodeploy/status` (`deployed_sha`, `failed_sha`), `journalctl -u sportnerd-autodeploy` |
| SQLite | VM | Усі дані | `database/store.py`, `migrations/` | `sqlite3 … 'select count(*) from cv_jobs'` |
| CV worker | box | Забирає відео з черги, рахує повтори (MediaPipe Pose) | `cv_worker/box_worker.py`, `cv_worker/cv_job.py`, `core/cv/` | `/tmp/sportnerd-boxcv.log`, `/tmp/sportnerd-boxcv.pid`, `heavy --status` |
| Admin-tasks worker | box | Забирає admin-задачі, шле у вебхук | `tools/admin_tasks_worker.py` | `/tmp/sportnerd-admintasks.log`, heartbeat кожні 300 с |
| Box wake | VM (бот + gateway) | Будить box вебхуком, коли відео в черзі, а CV worker мовчить; watchdog; одне повідомлення в чат | `core/wake.py`, `app/flow.py: _ensure_box_awake`, `bot.py: build_waker`, `gateway/cv_queue.py` | Лог бота: «wake webhook attempt N/3 accepted»; `GET /cv/worker_status` |
| Grok Bot routine «SportNerdBot box wake» | Grok Bot cloud | Будить агента на box: запускає воркери, якщо їх нема, і чекає, поки cv_worker почне поллити | `tools/ensure_workers.sh`, `tools/cv_worker_status.py`; секрет вебхука на VM кладе `tools/set_wake_webhook.py` | `python tools/cv_worker_status.py` |
| Grok Bot routine Будить агента на box із payload задачі | (налаштовується в Grok Bot; URL/ключ — `ADMIN_TASKS_WEBHOOK_URL/KEY`) | Лог admin-tasks worker: «delivered to webhook (HTTP 2xx)» |
| Агент розробки | box | Правки через `agy`, тести, commit/push, закриття задачі | `skills/admin_dev.md`, `tools/admin_tasks.py` | `python tools/admin_tasks.py list` |
| GitHub | GitHub | Репозиторій, `origin/main` | — | `git ls-remote origin main` |
| Gemini | Google | LLM / vision | `core/llm.py` | Помилки `LLMUnavailable` у `bot.log` |

## 4. Карта репозиторію

```
bot.py                   точка входу: aiogram-хендлери, APScheduler (timer_schedule), /start /rules /status /approve /rollback
app/flow.py              ChatFlow — оркестратор (обробка повідомлень, відео, таймери, adm-задачі, trust-режим, диспути)
app/offload.py           старий клієнт підписаного webhook-offload (legacy, ROUTINE_*)
agents/                  router.py (admin/coach/ignore), coach.py (відповіді тренера + tools), admin.py (admin-тули),
                         onboarding.py, report_judge.py (детермінований суддя відео), prompt_editor.py, base.py
core/                    config.py, llm.py (Gemini), challenge.py (правила/норми), progression.py (XP/норма),
                         claims.py (заявлена кількість, диспути), dayroll.py (межа дня 02:30), guardrails.py (адмін-ліміти),
                         signing.py (HMAC), bot_texts.py, textguard.py, telegram_files.py, cv/ (MediaPipe-лічильник)
database/                store.py (SQLite-шар), migrate.py (раннер міграцій)
migrations/              NNNN_*.sql / *.py (expand-only)
gateway/                 app.py, cv_queue.py (/cv/*), admin_tasks_api.py (/admin_tasks, /autodeploy/*)
cv_worker/               box_worker.py, cv_job.py, vision.py, run_box_worker.sh
tools/                   admin_tasks.py (CLI), admin_tasks_worker.py, worker_timers.py, run_admin_tasks_worker.sh
deploy/                  deploy.sh, autodeploy/, systemd/, vm_config.env, README.md
config/workers.env       інтервали поллінгу воркерів box (читаються «наживо»)
prompts.py               DEFAULT_PROMPTS та DEFAULT_BOT_TEXTS (в т.ч. trust_*)
skills/admin_dev.md      інструкція для агента розробки
tests/                   офлайн pytest (CV-модулі пропускаються без numpy)
docs/                    ця документація
optionToConsider/        старі плани/дослідження (див. README → Документація)
```

## 5. Дані (SQLite)

| Таблиця | Призначення |
|---|---|
| `users` | Учасники (`slug`, імена, `telegram_id`, `status` active/uninitialized, життя `lives`/`max_lives`, режим) |
| `user_progress` | Виконання норми по днях (`date`, `user_slug`, `done`, `source`) |
| `progress` | Legacy `lyosha_done`/`sasha_done` (лише читання) |
| `daily_attempts` | Найкраща спроба дня (`date`, `user_slug`, `exercise`, `best_reps`) |
| `user_xp`, `xp_log` | XP/рівні та журнал нарахувань |
| `next_norms` | Наступна норма на вправу (`user_slug`, `exercise`, `target`, `effective_from`) |
| `chat_history` | Повідомлення чату (контекст для LLM, курсор обробки) |
| `bot_state` | Ключ-значення: курсори `last_processed_*`, `progression_settled_through`, `lives_settled_through`, `schedule_mode`, `video_caption:<rid>`, `video_credit:<rid>`, `dispute_wait:<slug>`, overrides норм тощо |
| `bot_texts` | Фіксовані тексти бота (правляться адміном) |
| `prompts`, `prompt_history` | Системні промпти та їхні версії |
| `video_checks` | Відеозвіти (`request_id`, `file_unique_id`, `exercise`, `reps`, `confidence`, `flags`, `valid`, `status`, `location`) |
| `cv_jobs` | Черга CV для box (`status` queued/claimed/done/failed, `claim_token`, `attempts`, `processing_sent_at`, `still_sent_at`, `replied_at`) |
| `admin_tasks` | Черга задач розробнику (`pending` → `claimed` → `done_pending_deploy` → `announced` / `rejected`, `fix_commit`, `result`) |
| `admin_audit` | Аудит адмін-запитів |
| `pending_replies` | Відповіді, що мають піти після рестарту/збою |
| `offload_requests` | Legacy-журнал webhook-офлоаду |
| `leaves` | Відпустки та лікарняні (`user_slug`, `kind`: sick/vacation, `start_date`, `end_date`, `set_by`, `reason`, `cancelled`) |
| `schema_version` | Застосовані міграції |

Міграції: `python -m database.migrate --db <path>`, policy expand-only, онлайн-бекап перед застосуванням.

## 6. Конфігурація

- **`.env` (локально на box; gitignored) / `/etc/sportnerdbot/env` (VM)** — лише назви: `BOT_TOKEN`, `CHAT_ID`, `GEMINI_API_KEY`,
  `GATEWAY_HMAC_SECRET`, `CV_GATEWAY_URL`, `ADMIN_TASKS_WEBHOOK_URL`, `ADMIN_TASKS_WEBHOOK_KEY`, `DB_PATH`, `ADMIN_TELEGRAM_IDS`,
  `ROUTINE_*` (legacy). Значення ніколи не коммітяться.
- **`deploy/vm_config.env`** (у git, автодеплой синхронізує непротектовані ключі в `/etc/sportnerdbot/env` і перезапускає
  сервіси): `AUTODEPLOY_INTERVAL`, `CV_CLAIM_WAIT_S`, `CV_RESULTS_INTERVAL_SEC`, `ADMIN_TASKS_ANNOUNCE_INTERVAL_SEC`,
  `TRUST_MODE`, `DAY_ROLLOVER_HOUR`, `DAY_ROLLOVER_MINUTE`, `WAKE_ENABLED`, `WAKE_STALE_S`, `WAKE_WAIT_S`.
  Секрет вебхука пробудження (URL і ключ) **не** тут і не в git: він лежить лише на VM у `/var/lib/sportnerdbot/wake_webhook.json`
  (mode 0600), куди його кладе `tools/set_wake_webhook.py` через `POST /admin/secrets/wake` (значення з env box
  `WAKE_WEBHOOK_URL`/`WAKE_WEBHOOK_KEY`, ніколи не друкуються).
- **`config/workers.env`** (у git, читається box-воркерами щоразу): `CV_POLL_WAIT_SEC`, `CV_CHECK_INTERVAL_SEC`, `CV_HEARTBEAT_S`,
  `ADMIN_TASKS_POLL_WAIT_SEC`, `ADMIN_TASKS_CHECK_INTERVAL_SEC`, `ADMIN_TASKS_HEARTBEAT_S`. Керується `tools/worker_timers.py`
  (`status [--remote]`, `set`, `trigger-deploy`).
- Інші параметри з `core/config.py`: `CV_OFFLOAD`, `CV_LEASE_S`, `CV_STILL_COUNTING_MIN`, `CV_VIDEO_DIR`, `ADMIN_DEV_RATE_LIMIT_HOUR`,
  `ADMIN_TASK_STALE_HOURS`, `LOG_RETENTION_DAYS`, `WAKE_ATTEMPTS`, `WAKE_WATCHDOG_INTERVAL_S`, `WAKE_SECRET_FILE`.

## 7. Gateway (VM)

Усі, крім `/healthz`, підписані HMAC (`core.signing.sign_request`: `METHOD\nPATH\nTS\nNONCE\nsha256(body)`, вікно ±5 хв, одноразовий nonce).

| Endpoint | Хто викликає | Призначення |
|---|---|---|
| `GET /healthz` | моніторинг, `deploy.sh` | liveness |
| `POST /deploy`, `GET /deploy/{id}`, `GET /offload/{id}`, `POST /callback` | legacy webhook-flow | deploy за sha / статуси / callback |
| `POST /cv/claim` | box CV worker | long-poll (до `CV_CLAIM_WAIT_S`=25 с): 200 job або 204 |
| `GET /cv/jobs/{rid}/video` | box CV worker | байти відео (gateway сам качав із Telegram — box не знає `BOT_TOKEN`) |
| `POST /cv/jobs/{rid}/result` | box CV worker | `{claim_token, ok, result, error}` |
| `GET /admin/diag` | `tools/vm_diag.py` | read-only знімок VM: cv_jobs/video_checks/daily_attempts/users/XP + хвіст логів із редакцією секретів |
| `POST /admin/ops` | `tools/vm_ops.py` | одноразові ідемпотентні (за `key`, запис у `bot_state: ops:<key>`) операції: `adjust_user`, `credit_attempt`, `send_message`, `admin_task_add`, `cv_cleanup`, `wake_test`, `set_leave`, `end_leave`, `correct_settlement` (виправлення вже проведеного розрахунку дня; `dry_run` дає план before/after) |
| `GET /cv/worker_status` | box CLI, діагностика | `last_poll_age_s`, `queued`, `claimed`, `worker_polling`, `wake_configured` (будь-який `/cv/claim`, зокрема порожній long-poll, оновлює `bot_state: cv_last_poll_ts`) |
| `POST /admin/secrets/wake`, `GET /admin/secrets/wake` | `tools/set_wake_webhook.py` | зберегти URL/ключ вебхука пробудження (лише https, файл 0600; значення не логуються й не повертаються) / `{configured}` |
| `GET /admin_tasks`, `GET /admin_tasks/claim`, `POST /admin_tasks/{id}/result` | box (worker, CLI) | список / long-poll claim / результат (`done_pending_deploy` + `fix_commit` або `rejected`) |
| `GET /autodeploy/status` | `tools/worker_timers.py status --remote` | `{deployed_sha, failed_sha}` |
| `POST /autodeploy/trigger` | `tools/worker_timers.py trigger-deploy` | торкається `/var/lib/sportnerdbot/autodeploy_trigger` + `systemctl start` → 202 |

Lease CV: claimed без результату довше `CV_LEASE_S` (300 с) повертається в чергу, після 3 спроб — `failed`.

## 8. Правила челенджу

- **Розклад:** режим `ladder` («+1 щодня») або `alternate` (дні віджимань ↔ присідань, коефіцієнт `alt_squat_factor`); старт 31.08.2026.
  Норму можна перекрити `override_<slug>_<date>` та `next_norms`.
- **Прогресія** (`core/progression.py`, при підсумку дня): залік → `next = (done + target)//2 + 1`, `done = max(best, target)`;
  не залік, але була спроба → `max(1, best − 5)`; не робив → `max(1, target − 10)`. Наступна норма діє для наступного дня тієї ж вправи.
- **XP:** залік = +1, плюс +2 за кожне повторення понад норму; кожні 100 XP — рівень (+1 `max_lives`, життя відновлюються).
- **Життя:** незакритий день → −1 життя; 0 життів → дискваліфікація. Режими новачків: Хардкор 2, Норма 5, Вітальний 10.
- **Межа дня 02:30 (Київ):** `core/dayroll.py` — «день челенджу» = календарна дата Києва мінус зміщення
  `DAY_ROLLOVER_HOUR:DAY_ROLLOVER_MINUTE` (за замовчуванням 02:30), DST-safe. Підсумок дня (`settle`: життя + прогресія) і так
  запускається о 02:30, відео між 00:00 і 02:30 належать попередньому дню. Settle ідемпотентний (`progression_settled_through`,
  `lives_settled_through`). Нагадування: 08:30, 15:00, 19:00, 23:30 (нічне досі каже «до півночі»).
- **TRUST_MODE (за замовчуванням ввімкнено) — м'який режим «до виправлення»** (тимчасово, поки CV-лічильник ненадійний):
  - якщо в підписі до відео або в тексті автора поруч (10 хв до відео) є кількість — бот зараховує **саме її**
    (`core/claims.py`: `parse_claimed_reps`, `parse_claimed_exercise`); інакше береться число CV, CV ніколи не відхиляє;
  - день закрито, якщо зарахована кількість ≥ норми; менше — лише записується спроба;
  - відповіді — з `trust_*` текстів (`prompts.py`), без перефразу Gemini;
  - **диспути:** «неправильно порахував», «не 4 а 40», «зарахуй» тощо (`is_dispute`, `parse_dispute_reps`) — бот вірить людині.
    З числом: виправляє залік за цей день (може бути вищим чи нижчим за CV), без числа: один раз просить («Скільки в тебе вийшло?
    Повірю твоєму слову») і зараховує відповідь. XP/життя/норма рахуються лише в `settle_progression` від `best_reps`, тож
    подвійного нарахування немає. Диспути працюють після відповіді бота і для відео, зарахованого раніше того ж дня челенджу;
    для вже підсумованого дня — ні (потрібен відкат XP);
  - **Число повторів розбирає LLM-тул `report_reps`, а не regex** (`agents/reps_claim.py`): `report_reps(user, count, exercise,
    corrects_existing_video, request_id, confidence)`. `count` — ціле 1..1000; `exercise` — `pushups|squats` або порожньо
    (лише якщо людина сама назвала вправу); `request_id` — відео з контексту; `user` — slug автора (claim-и про інших людей
    відкидаються). LLM розуміє цифри, слова й сленг («двадцять три», «півсотні», «десь 20»), жарти і заперечення
    («50 там і не пахне, я 23 зробив»). Тул валідує аргументи й повертає claim; застосовує його `ChatFlow`
    (`_apply_video_correction`: спроба дня = рівно це число, вище чи нижче; done; `video_credit`; вправа = названа людиною →
    під яку вже зараховано відео → вправа дня; XP/норму потім рахує settle). Шляхи:
    1. **Підпис/текст до відео** (`_extract_video_claim`): LLM бачить підпис, повідомлення автора за 10 хв до відео та відео-контекст;
       немає виклику тулу (`llm_none`) = числа немає (regex НЕ підміняє рішення LLM);
    2. **Повідомлення після відео** (`_handle_disputes`): префільтр (`is_dispute`/цифра/слово-число/відповідь на бота/очікування
       числа) -> LLM-тул (`_llm_dispute_claim`); без числа — одне запитання «Скільки вийшло?»;
    3. **Coach**: `report_reps` є в списку тулів coach (`agents/coach.py`, правила `REPS_CLAIM_MANDATE` у промпті — claim-и
       людини перемагають, не перепитувати, якщо число ясне, одне запитання якщо числа немає); claim-и застосовуються тихо.
    Regex (`core/claims.py`) — лише дешевий фолбек, якщо LLM недоступний/не налаштований. Шлях логується
    (`Claim extraction path=…`, `Dispute path=…`: `llm`/`llm_none`/`regex`/`regex_fallback`), для відео пишеться
    `bot_state: video_claim_path:<request_id>`. Промпт модуля — `prompts.py: REPS_CLAIM_PROMPT` (ключ `reps_claim`).
  - `TRUST_MODE=0` повертає стару CV-поведінку (`ReportJudgeAgent.judge_video_fact`).

## 9. Відпустка/лікарняний (leaves)

- **Таблиця:** `leaves` (`id`, `user_slug`, `kind`: `'sick'`|`'vacation'`, `start_date`, `end_date` [включно, NULL = безстроково], `set_by`, `reason`, `created_at`, `ended_at`, `ended_by`, `cancelled`).
- **Тули:**
  - Адмін-тули: `set_leave(participant, kind, start_date='', end_date='', reason='')`, `end_leave(participant, from_date='')`, `list_leaves()`.
  - Gateway ops: `set_leave`, `end_leave` (ідемпотентні за `key`).
- **Правила активності:**
  - Запис активний у день `D`, якщо `start_date <= D` і (`end_date IS NULL` або `D <= end_date`) і `cancelled = 0`.
  - Оголосити може сам учасник або будь-хто інший (вказується у `set_by`).
  - При перетині з новою `set_leave` стара активна автоматично обрізається (`end_date = start_new - 1`) або скасовується, якщо ще не почалась.
- **Вплив на розрахунок і статус:**
  - `missing_participants` не включає учасника на лікарняному/у відпустці; нагадування (timers) його не згадують серед тих, хто заборгував;
  - `settle_missed_day` **не знімає** життя за дні активного лікарняного чи відпустки;
  - `settle_progression` для такого дня, якщо день не зараховано (`credited=False`), повністю пропускає учасника: норма не спадає, XP/рівень/життя не змінюються; якщо учасник виконав норму під час відпустки сам — нараховується звичайний прогрес;
  - `status_snapshot` додає для учасника поле `leave = {"kind", "until", "set_by"}` або `None`;
  - `status_text` виводить `🤒 на лікарняному (до DD.MM)` або `🏖 у відпустці (до DD.MM)` замість `НЕ виконано ❌`;
  - підсумок дня (`flow._settle_lives`) додає рядки `🤒 {name} на лікарняному — життя й норма без змін` / `🏖 {name} у відпустці — життя й норма без змін` для тих, кого пропущено;
  - завершення: автоматично за `end_date` або викликом `end_leave` (встановлює `end_date = from_day - 1 день`).

## 10. Єдиний статус учасників (детермінований)

- Джерело правди — `core/challenge.py`: `status_snapshot` → `participant_status_line` → `status_board` (рядки) / `status_text` (з заголовком).
- Рядок учасника (завжди повний): імʼя, норма дня, ✅ виконано (спроба) / ❌ ще не здав (найкраща спроба) / 🤒 на лікарняному або 🏖 у відпустці (з дати, до дати або «безстроково»),
  ❤️ життя x/max, ⭐ XP x/100, 🆙 рівень, 🔥 серія (дні відпустки серію не рвуть; `streak_days`).
- Споживачі одного й того ж форматування: команда `/status` (bot.py), ранкове повідомлення (`flow._handle_timer`), системний промпт коуча
  (`CoachAgent.build_system_prompt`: `participants_block` + `STATUS_MANDATE` ПІСЛЯ динамічних правил — вимагає передавати блок дослівно),
  тул коуча `get_status_board`, `GET /admin/diag` (поля `status_board`, `leaves` — read-only перевірка на VM).
- Причина інциденту 2026-10-26: відповідь «Бойове зведення» складав LLM-коуч сам (не `/status`), у його промпті не було статусу відпустки,
  а динамічне правило «Рядок статусу» вимагало лише життя; `status_text` з leave у проді ніхто не викликав.
