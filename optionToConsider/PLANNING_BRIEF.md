# PLANNING BRIEF: SportNerdBot — деплой, БД, admin-flow через Grok Bot, video intake

> **Аудиторія:** Claude Opus у planning mode.
> **Мета:** на основі цього brief сформувати (a) **target architecture** і (b) **work plan** у вигляді
> самодостатніх задач, які далі виконуватиме Gemini 3.8 в Antigravity (`agy` CLI) як autonomous coding agent.
> **Статус:** нічого не закомічено, код не змінено. Brief базується на читанні репо станом на 2026-10-20.
> Формат вимог до результату — у кінці документа (розділ «Output requirements»).

---

## 0. Поточний стан (факти з репо)

### 0.1 Runtime
- `bot.py`: aiogram, **long-polling** (`getUpdates`), один процес. `describe_message()` перетворює
  повідомлення на текст; для `video` / `video_note` / `animation` (включно з forward) підставляє
  **placeholder**, тобто вміст відео не аналізується.
- APScheduler (`Europe/Kyiv`) у `main()`: cron-джоби викликають `flow.process(..., trigger="timer")`.
- `app/flow.py`: `ChatFlow.process` під **глобальним `asyncio.Lock`**; читає pending-повідомлення
  (`unprocessed_user_messages(after_ts, day_start_ts)`), далі Router → Onboarding / Admin / Coach.
  Admin-гілка відправляє «obey video» з `obey_media_dir`. `_handle_timer` + `_settle_lives`.
- `agents/`: `router.py`, `onboarding.py`, `admin.py`, `coach.py`, `report_judge.py` (structured JSON
  verdict), `prompt_editor.py` (Conflict → Merger → Verifier для `coach_dynamic`, ліміт 6000 символів),
  `base.py` (`PromptAgent`).
- `core/llm.py`: `GeminiLLM` — flash-lite, free tier, список моделей з promote-on-success,
  **ring-buffer rate limiter** (`threading.Lock`), `text()` з AFC tools (tools — closures),
  `json()` зі schema (pydantic).
- `core/challenge.py`: таргети, alternating schedule (pushups/squats), lives, `settle_missed_day`;
  ще є hardcoded `SASHA_ALIASES` / `LYOSHA_ALIASES`.
- `database/store.py`: SQLite `challenge.db`; таблиці `progress` (legacy, колонки
  `lyosha_done`/`sasha_done`), `users`, `user_progress`, `chat_history`, `bot_state`, `prompts`,
  `prompt_history`. Схема — `CREATE TABLE IF NOT EXISTS` в `init_schema`, версіонованих міграцій немає.
- `prompts.py`: дефолтні промпти, сідяться в БД (`seed_prompts`, `migrate_prompts.py`).
- Тести: `tests/test_flow_offline.py`, `tests/integration/`, `flow_fixtures.py`, `pytest.ini`.

### 0.2 Інфраструктура та деплой
- GCP **e2-micro** (1 GB RAM, shared core), `sportnerd-bot-vm`, `us-central1-a`,
  project `gen-lang-client-0185250272`.
- systemd `sportnerdbot.service`, **`User=root`**, `WorkingDirectory=/opt/sportnerdbot`,
  `Restart=always` (з `setup_remote.sh`).
- `deploy.ps1` (Windows): tar файлів + `.env` → `gcloud compute scp` → `gcloud compute ssh` → бекап
  `/opt/sportnerdbot_backup_<ts>.tgz` → stop → розпаковка поверх → `migrate_prompts.py`,
  `migrate_users.py`, `enable_alternating.py` → smoke `import bot; bot.build_app()` → start;
  rollback через `trap ERR`. БД живе **в тому ж каталозі, що й код** (`/opt/sportnerdbot/challenge.db`).
- **git-pull механізму на VM немає.** Outbound SSH з машини користувача заблоковано → деплой має бути
  **pull-based**.
- `update_bot.py` — небезпечний скрипт (прибрати/замінити). README застарілий.

### 0.3 CV-прототип (`optionToConsider/cv_test`, `HANDOFF.md`, `mediapipe_limits.md`)
- `cv_test/rep_counter.py::analyze_video(path) -> dict` (MediaPipe Pose Tasks API), `bench.py`.
- Lite model, 2 cores, послідовно: side-view squats `1247W` (75 s) стабільний до ~**15 fps / 480 px**
  long side; найшвидший коректний — **12.7 s** з ffmpeg pre-decode (`fps`+`scale` filter);
  10 fps / 400 px — межа. Відео `VX8JN6` (93 s) змінює count при будь-якому зниженні → потрібен native
  (~29–31 s).
- Inference ~10–11 ms/frame незалежно від resolution (модель ресайзить сама) → економить тільки
  зниження fps. VFR-відео: кількість кадрів cv2 vs ffmpeg відрізняється.
- Front-camera pushups класифікуються як squat/none → **логіку кутів для pushup треба переписати**.
- Junk clips < 5 s → gate. Peak RAM ~380 MB. Очікувано e2-micro у **2–3× повільніший** за dev box.
- Узгоджена hybrid-схема: **Gemini vision класифікує exercise vs flood, pose estimation рахує reps**;
  розбіжність → попросити перезаписати.

---

## 1. Constraints

| # | Обмеження | Наслідок для плану |
|---|---|---|
| C1 | Gemini free tier (flash-lite), низькі RPM/RPD, ring-buffer limiter | Video vision — кілька кадрів, не повне відео; admin-flow не має «з'їдати» квоту чату |
| C2 | e2-micro: 1 GB RAM, shared core | CV локально лише з swap, lite model, черга з concurrency=1; ризик OOM для бота |
| C3 | Long-polling: **лише один інстанс** може викликати `getUpdates` на токен (інакше 409 Conflict) | Справжній паралельний blue/green неможливий без переходу на webhook |
| C4 | Outbound SSH з машини користувача заблоковано | Деплой — pull-based (VM тягне з GitHub) |
| C5 | Grok Bot box не має public inbound endpoint | GCP → box лише через webhook routine; box → світ outbound є |
| C6 | Routine run не може інтерактивно вводити secrets | Токени (Telegram, Gemini, GitHub) мають заздалегідь лежати на box (`/workspace/pipbot/.env`, gitignored) |
| C7 | Telegram Bot API: `getFile` віддає файли **≤ 20 MB** | Більші відео — відмова/прохання стиснути або обрізати |
| C8 | Правило користувача: **спочатку план, локальні тести, push тільки після OK** | Конфлікт з admin-flow, який за дизайном пушить напряму (див. 7.Q1) |

---

## 1A. ⭐ Намір користувача (user intention) + варіанти для зважування

> Цей розділ фіксує **явні побажання користувача** (2026-10-20). Claude має зважити варіанти, але
> рішення, що суперечать позначкам **USER INTENTION**, потрібно явно обґрунтувати.

### 1A.1 Admin request tiers (рівні обробки admin-запитів)

Користувач хоче **responsiveness**: прості речі — миттєво, складні — якісно.

| Варіант | Суть | Latency | Free-tier quota (Gemini у боті) | Quota box (wake routine) | Safety |
|---|---|---|---|---|---|
| **A** | In-bot Gemini (AFC) робить **лише прості зміни значень** у БД: lives, targets, schedule, progress, counters. Без schema/metadata і без prompt changes. Усе інше → coding agent на box | секунди для простого; хвилини для решти | низька | лише складні запити | висока: AFC tools мають вузький whitelist операцій |
| **B** | Gemini сам вирішує: пробує виконати; якщо власні acceptance criteria не виконано або запит зачіпає code/schema/prompts → escalates як coding task | секунди або хвилини | середня (спроба + перевірка + ескалація) | менше, ніж C | середня: недетермінована межа, ризик «напів-виконаних» змін |
| **C** | **Усі** admin-запити → box agent | завжди хвилини | мінімальна | максимальна | висока (тести, audit, git), але повільно навіть для «+1 життя» |

**USER INTENTION:**
- **Prompt changes** (включно з `coach_dynamic`, який зараз редагує `prompt_editor.py`) — виконує
  **повноцінний coding tool на box**, навіть ціною витрати лімітів.
- **Quick DB value tweaks** (lives, targets, schedule, progress, counters) — лишаються **in-bot** заради швидкості.
- Тобто фактично варіант **A** (з prompt changes, перенесеними на box). Claude може запропонувати
  гібрид A+B (наприклад, in-bot Gemini класифікує й ескалює, але сам ніколи не змінює prompts/schema).

Що вирішити в плані: точний whitelist in-bot операцій; що робити з `agents/prompt_editor.py`
(видалити / залишити як fallback, коли box недоступний); як Router розпізнає tier (`admin_quick` vs `admin_dev`).

### 1A.2 Response orchestration end-to-end (що відбувається на VM і що бачить користувач)

Користувач питав: «що відбувається на VM і що я бачу в чаті?». **Цільовий flow (USER PROPOSAL):**

1. Адмін пише admin-фразу в групі (можливо reply на інше повідомлення).
2. Бот (Gemini, Router) класифікує як `admin_dev` і **одразу** відповідає в чаті в admin-стилі:
   «Прийняв, виконую. `request_id=…`» (reply на оригінал). Створює запис у `offload_requests`.
3. Бот шле webhook на Grok Bot routine (контракт — Тема 5).
4. Box agent: формалізує → планує → змінює код/prompts/міграції → запускає тести → commit → **push
   напряму в `main`**.
5. *(Optional progress messages)* агент шле в чат коротке «План: …» та/або «Деплою `<sha>`…» (через
   Bot API напряму або через endpoint VM).
6. Агент викликає **deploy endpoint на VM** з `{request_id, sha, reply_text, chat_id, reply_to_message_id, signature}`.
7. VM: `git fetch` → checkout саме цього `sha` у `releases/<sha>` → migrations (з backup) → smoke test →
   switch symlink → restart.
8. **Після успішного restart** бот публікує `reply_text` (у стилі admin prompt) у чат як reply на
   оригінальне повідомлення; позначає запит `done`.
9. **On failure:** automatic rollback → бот (стара версія) пише користувачу про помилку → звіт
   агенту (callback-відповідь endpoint-а або статус у `offload_requests`, який агент може перевірити),
   агент може спробувати виправити (обмежена кількість спроб).

**Важливо — restart і втрата відповіді:** під час restart polling зупиняється на кілька секунд;
процес, який прийняв deploy-запит, буде вбито. Тому `reply_text` треба **спершу зберегти** в БД
(таблиця `pending_replies`: request_id, chat_id, reply_to_message_id, text, status, attempts) або у
файл у `/var/lib/sportnerdbot/`, а **нова версія** на startup читає pending і надсилає (idempotent за
`request_id`). Endpoint відповідає агенту `202 Accepted` одразу, фінальний статус — окремо
(агент поллить `GET /deploy/<request_id>` або отримує його іншим шляхом).

**Fallback:** якщо endpoint VM недоступний / не відповів за N хв — box agent надсилає повідомлення в
Telegram **напряму через Bot API** (токен є на box) з поясненням, а деплой підхоплює механізм (ii).

### 1A.3 Deploy trigger: VM тягне за розкладом чи за commit?

| Варіант | Як працює | Latency деплою | Security | Complexity | Доставка reply |
|---|---|---|---|---|---|
| **(i) VM deploy endpoint** | мінімальний `aiohttp` на окремому порту (окремий systemd unit, не в процесі бота) або через Cloud-side IAP/proxy; агент робить POST з HMAC | секунди | потрібен **відкритий HTTPS-порт** (firewall, TLS, HMAC/shared secret, timestamp window, allowlist по IP де можливо) — поверхня атаки | середня | `pending_replies` → нова версія надсилає після старту |
| **(ii) systemd timer polling git** | кожні 1–2 хв `git fetch`, деплой якщо `main` змінився | 1–2 хв + час деплою | найкраща: **немає відкритого порту** | низька | `pending_replies` у коміті (файл, наприклад `deploy/pending_replies/<request_id>.json`) або агент шле в Telegram напряму (але тоді не знає, чи деплой успішний) |
| **(iii) GitHub Actions on push to main** | workflow робить `gcloud compute ssh` з service account (GitHub runners не під блокуванням SSH користувача) | ~1–3 хв (старт runner) | GCP key у GitHub secrets — ризик витоку; потрібен мінімальний IAM role | середня | як (ii); workflow може сам надіслати reply через Bot API по результату |

**USER INTENTION:** схиляється до **(i)** заради швидкості. Claude має оцінити всі три й
запропонувати рішення; розумна комбінація — **(i) як основний + (ii) як safety net** (timer ловить
коміти, які endpoint пропустив, і гарантує сходження до `main`). Врахувати 1 GB RAM: окремий endpoint
процес має бути дуже легким.

### 1A.4 Confirm-point: «push тільки після мого OK» vs direct push
Користувач **сам описав** admin-flow з прямим push у `main`, тобто для admin-flow direct push
прийнятний. Але загальне правило «план → локальні тести → push після OK» лишається для звичайної
розробки. Claude має **залишити це як явний confirm-point** в архітектурі (див. 7.Q1): які категорії
змін у admin-flow пушаться без підтвердження, а які (destructive, schema drop, deploy-скрипти,
secrets) вимагають «так» від адміна.

---

## 2. Тема 1 — Blue-green deployment на GCP

**Позиція користувача.** Чи потрібен blue-green? Застосунок маленький, можливо зайве.

**Факти.** Одна e2-micro; long-polling (C3); зараз деплой — розпаковка поверх робочого каталогу з
tar-бекапом; немає git на VM у робочому процесі; outbound SSH заблоковано (C4).

**Припущення.** Downtime у 10–30 s під час рестарту прийнятний (чат-бот, повідомлення не губляться —
Telegram тримає апдейти до 24 год). Друга VM небажана (вартість, free tier).

**Пропозиція.**
- **Не робити повний blue-green.** Замість цього — *release directories + atomic symlink switch*:
  - `/opt/sportnerdbot/releases/<git_sha>/` — чистий checkout; `/opt/sportnerdbot/current -> releases/<sha>`.
  - Спільний venv на реліз (або `venv` всередині релізу, створюється тільки якщо змінився
    `requirements.txt` — hash-кеш).
  - Кроки: fetch → checkout у новий каталог → `pip install` → **migrations** (див. Тема 2) →
    smoke test (`python -c "import bot; bot.build_app()"` + offline pytest subset) → `ln -sfn` →
    `systemctl restart` → health check (процес active N секунд + heartbeat-файл/лог «polling started»)
    → при фейлі **automatic rollback** на попередній symlink. Тримати останні 5 релізів.
- **Тригер pull-based:** варіант A — systemd timer (`sportnerdbot-deploy.timer`, кожні 1–2 хв
  `git fetch`, деплой якщо `origin/main` змінився і коміт має зелений CI-статус); варіант B — GitHub
  Actions **self-hosted runner** на VM (подієвий, але runner їсть ~100–200 MB RAM — критично на 1 GB).
  Рекомендація: **A** (timer) + перевірка CI-статусу через GitHub API або тег/branch `deploy`.
- Нотатка: «справжній» blue/green з двома паралельними інстансами конфліктує з `getUpdates`. Якщо колись
  потрібно — перехід на Telegram webhook + reverse proxy, тоді switch = зміна upstream.

**Open questions.**
- Q1.1: Прийнятний downtime при рестарті? Чи потрібен drain (дочекатися завершення `ChatFlow.process`)?
- Q1.2: Timer vs self-hosted runner — чи ок RAM-overhead runner'а?
- Q1.3: Деплоїти кожен push у `main` чи лише коміти з тегом / після зеленого CI?

---

## 3. Тема 2 — Винести БД, не передеплоювати її

**Позиція користувача.** БД окремо від коду; та сама VM, але окремі «застосунки».

**Факти.** `challenge.db` у `/opt/sportnerdbot`, бекап разом із кодом; сервіс під root; схема через
`CREATE TABLE IF NOT EXISTS` + ад-хок скрипти `migrate_*.py`, `enable_alternating.py` запускаються
при кожному деплої.

**Припущення.** Навантаження мізерне (одна група, кілька користувачів) → SQLite достатньо.
Cloud SQL (~$7–10+/міс мінімум) невиправданий.

**Пропозиція.**
- Шлях `/var/lib/sportnerdbot/challenge.db` (поза release dir), власник — **non-root** user
  `sportnerd`; `.env` → `/etc/sportnerdbot/env` (`0600`), systemd `EnvironmentFile=`.
- `PRAGMA journal_mode=WAL`, `busy_timeout`, `foreign_keys=ON` у `_connect`.
- **Versioned migrations**: `migrations/NNNN_name.sql|py` + таблиця `schema_version`; runner
  ідемпотентний, запускається **до** switch; перед міграцією — `sqlite3 .backup` (online backup API).
  Ад-хок скрипти (`migrate_prompts.py`, `migrate_users.py`, `enable_alternating.py`) або перетворити
  на міграції, або прибрати з деплою.
- Міграції лише **expand-only** (додавати колонки/таблиці), щоб rollback коду не ламав БД; destructive
  зміни — окремим кроком після стабілізації.
- Nightly backup: `.backup` → gzip → `gsutil cp` у GCS bucket (lifecycle 30 днів), VM service account
  з мінімальною роллю на bucket.
- Опційно пізніше: окремий «DB API» сервіс (локальний HTTP/unix socket) — потрібен лише якщо admin-flow
  на box має читати/писати БД віддалено (див. Тема 5). Зараз — не робити.

**Open questions.**
- Q2.1: Чи є GCS bucket / дозвіл створити (free tier 5 GB)?
- Q2.2: Видаляти legacy `progress` (з міграцією даних у `user_progress`) чи залишити read-only?
- Q2.3: Чи потрібен box-у прямий доступ до prod БД, чи достатньо snapshot + змін через міграції/admin-команди?

---

## 4. Тема 3 — Переписати admin: запит із Telegram → агент на Grok Bot box

**Позиція користувача.** Admin-запит із Telegram іде на Grok Bot box. Там агент запускає Gemini CLI
(або `agy`) з preset prompt/skill і доступом до БД. Flow:
1. отримати referenced message (reply / forward);
2. формалізувати запит;
3. якщо контекст неявний — підтягнути останні кілька повідомлень;
4. переглянути метадані БД і актуальні spec-файли;
5. спланувати зміни за категоріями: **DB data changes**, **нові DB entities**, **код serving agents**,
   **prompts у БД**;
6. виконати план і задеплоїти;
7. відповісти користувачу у стилі admin-prompt;
8. commit message і **push напряму в main** (не PR).

**Факти.** Зараз admin обробляється в процесі бота: `agents/admin.py` (Gemini AFC з closures — mark
done, targets, lives тощо), `prompt_editor.py` для `coach_dynamic`; відповідь + «obey video». Box
досяжний лише через webhook routine (Тема 5); асинхронно, секунди–хвилини.

**Припущення.** Дрібні data-фікси (mark done, змінити норму) мають лишатися миттєвими в боті; на box
ідуть лише зміни, що потребують коду/схеми/складних промптів. Router має розрізняти `admin_quick` vs
`admin_dev`.

**Пропозиція.**
- Router: новий клас `admin_dev` → бот відповідає «прийнято, request_id=…», шле webhook (контракт —
  Тема 5) з `referenced_message`, останніми N повідомленнями, автором, `telegram_id`.
- На box: skill-файл (`optionToConsider/skills/admin_dev.md` → згодом у репо) з жорстким пайплайном
  кроків 1–8; виконавець — `agy`/Gemini CLI у headless режимі з робочою копією репо.
- Доступ до БД: **не пряма prod-БД**. Варіанти: (a) snapshot з GCS для аналізу + зміни даних як
  **data migration** у коміті; (b) admin-команди, які бот виконує після деплою. Рекомендація: (a).
- Prompt-зміни в БД — також через міграцію (`prompts` + `prompt_history`), щоб усе було в git.
- **Guardrails:**
  - allowlist `telegram_id` адмінів + passphrase (або session-токен на N хв) для `admin_dev`;
  - **dry-run / plan echo**: для destructive змін (DROP, DELETE, зміна lives/progress заднім числом,
    видалення промптів) — бот показує план, чекає «так» від того ж адміна;
  - DB backup перед будь-якою зміною (автоматично в міграційному runner);
  - **tests must pass** (`pytest` offline) перед push; lint/compile check;
  - automatic rollback (Тема 1) + команда `/rollback` для адміна;
  - audit log: таблиця `admin_audit` (request_id, author, request, plan, commit_sha, status) +
    коміт містить `request_id`;
  - rate limit: ≤ N `admin_dev` запитів/год/адмін, один активний запит одночасно;
  - заборонені зони: `.env`, secrets, deploy-скрипти, workflow-файли — лише з окремим підтвердженням.

**Open questions.**
- Q3.1: Конфлікт «push напряму» vs правило «push після OK» (див. 7.Q1).
- Q3.2: `agy` чи Gemini CLI на box — який реально встановлений/авторизований без інтерактиву?
- Q3.3: Хто власник commit-ів (бот-акаунт / deploy key / PAT з мінімальними правами)?
- Q3.4: Що робити, якщо тести падають — відповісти адміну з поясненням чи робити кілька спроб?

---

## 5. Тема 4 — Video intake

**Позиція користувача.** Інтегрувати аналіз відео: повертати тривалість, вправу, кількість повторень
(+ confidence, flags, valid).

**Факти.** Див. 0.3. Зараз `describe_message` → placeholder, judge зараховує → **anti-cheat hole**
(GIF теж проходить). Telegram `getFile` ≤ 20 MB (C7). e2-micro 2–3× повільніший; 75 s відео на
dev box 12.7 s → на VM ~25–40 s; native-fps випадки ~60–90 s, RAM ~380 MB.

**Припущення.** Звіти приходять кілька разів на день, не одночасно; затримка вердикту до 1–2 хв ок.

**Пропозиція.**
- **Pipeline:**
  1. Gate (бот): тип (`animation`/GIF → reject), `duration < 5 s` → reject, `file_size > 20 MB` →
     попросити стиснути; dedupe за `file_unique_id` (повтор → flag `duplicate`).
  2. `ffprobe`: duration, fps, VFR, rotation.
  3. Gemini vision на 3–4 кадрах зі стабільного середнього сегмента → `pushup | squat | none`.
  4. Pose (MediaPipe lite): ffmpeg pre-decode `fps=15,scale=480:-2`; якщо confidence низька —
     повтор на native fps. Рахує reps лише в stable segment.
  5. Розбіжність класифікацій → `valid=false`, flag `class_mismatch`, просимо перезаписати.
  6. Результат → `ReportJudge` як структурований факт (не placeholder); judge вирішує done/not.
- **Де виконувати:** 
  - Варіант A — **на VM**: окремий systemd-сервіс `sportnerd-cv` (worker, concurrency=1, `nice`,
    `MemoryMax=`), swap 1–2 GB, черга через таблицю `video_checks` (status=queued). Плюс — автономно,
    без квоти Grok Bot. Мінус — ризик OOM/CPU starvation бота.
  - Варіант B — **offload на box** через webhook (Тема 5): 8 vCPU, швидко, але латентність wake і
    квота.
  - Рекомендація: A як основний, B як fallback для довгих/native-fps відео (або навпаки — рішення
    планувальника з обґрунтуванням).
- **JSON contract (`analyze_video` result):**
```json
{
  "request_id": "uuid", "file_unique_id": "AgAD...", "duration_s": 75.2,
  "exercise": "squat|pushup|none", "reps": 50, "confidence": 0.87,
  "vision_class": "squat", "pose_class": "squat",
  "flags": ["short", "gif", "duplicate", "class_mismatch", "low_body_visibility", "too_large", "vfr"],
  "valid": true, "analyzer_version": "cv-1.0", "elapsed_s": 31.4
}
```
- **Таблиця `video_checks`:** id, chat_id, message_id, user_slug, file_id, file_unique_id (UNIQUE
  на день/юзера або глобально), duration_s, exercise, reps, confidence, flags (JSON), valid, status
  (`queued|running|done|failed`), analyzer_version, created_at, finished_at.
- **Fix pushups:** переписати angle logic для front-camera (elbow angle + shoulder/wrist vertical
  displacement, перевірка горизонтальності тулуба); тест-набір відео з ground truth.

**Open questions.**
- Q4.1: Строгість: reps < target → not done, чи partial? Чи довіряти AI-підрахунку при low confidence?
- Q4.2: Video notes (кружечки, front-camera) — обов'язково підтримувати з першої версії?
- Q4.3: Зберігати відео після аналізу? (рекомендація — ні, лише метадані).
- Q4.4: Swap на e2-micro прийнятний (диск pd-standard повільний)?

---

## 6. Тема 5 — Комунікація GCP ↔ Grok Bot box (MCP?)

**Позиція користувача.** MCP server? Чи досяжне середовище Grok Bot із Google Cloud?

**Факти.**
- Box **не має public inbound endpoint**; з GCP напряму недосяжний.
- Санкціонований inbound — **Grok Bot routine з webhook trigger**: бот на GCP робить `POST` JSON на
  webhook URL routine з sender key (користувач копіює URL+key з панелі routine) → агент прокидається.
- Агент може: запускати CLI на box, `git push` у GitHub, відповідати через Telegram Bot API напряму
  або через callback endpoint бота.
- Box має outbound internet (GitHub, Telegram API, GCP APIs).
- Webhook runs асинхронні (секунди–хвилини); routine не може інтерактивно заповнювати secrets → токени
  вже мають бути на box (`/workspace/pipbot/.env` існує, gitignored).
- **MCP не потрібен для GCP → box.** Опційно корисний у зворотний бік: MCP server, що експонує
  DB/admin tools агентам (read-only schema, snapshot, безпечні admin-операції).

**Припущення.** Callback на бот потребує inbound на VM (зараз бот лише polling, HTTP-порту немає);
простіше відповідати напряму в Telegram + писати результат у GitHub/GCS або через окремий
легкий HTTP endpoint на VM за firewall rule + secret.

**Пропозиція.**
- **Request contract (bot → routine webhook):**
```json
{
  "request_id": "uuid-v4", "type": "admin|video", "created_at": "2026-10-20T20:47:00+03:00",
  "chat_id": -100123, "message_id": 4567, "reply_to_message_id": 4560,
  "user": {"telegram_id": 111, "username": "x", "slug": "sasha"},
  "payload": {"text": "...", "referenced_text": "...", "recent_messages": [...]},
  "file_id": "BAAC...", "file_unique_id": "AgAD...",
  "reply_mode": "telegram_direct|callback|both",
  "signature": "hmac-sha256(body, shared_secret)"
}
```
- **Idempotency:** `request_id` — ключ; box веде журнал оброблених id; бот зберігає статус у
  `offload_requests` (sent/acked/done/failed, retries, expires_at). Повторна доставка — no-op.
- **Auth:** sender key routine + HMAC власного shared secret (захист, якщо URL витече) + timestamp
  window (±5 хв).
- **Result callback:** `{request_id, status, result (JSON contract Теми 4 або admin summary),
  commit_sha?, reply_text}`. Варіанти доставки: (a) box шле в Telegram напряму (простий варіант для admin), але
  polling-бот не отримує власні повідомлення, тож для video (результат має дійти до judge) краще (b) HTTP callback
  на VM (`/callback`, HMAC, firewall лише HTTPS) або (c) box пише результат у GCS/GitHub, VM поллить.
  Планувальник має обрати і обґрунтувати.
- **Deploy loop:** box → push у GitHub `main` → VM pull (timer або self-hosted runner) → migrations →
  smoke → switch → (опційно) бот повідомляє в чат «деплой <sha> ок / rollback».
- Timeout: якщо результату немає за N хв — бот повідомляє користувача і позначає `failed`.

**Open questions.**
- Q5.1: Чи готовий користувач відкрити HTTPS-порт на VM для callback (і TLS — Caddy/self-signed)?
- Q5.2: Ліміти/квота wake-ів routine — скільки відео/день можна offload-ити?
- Q5.3: MCP server для DB tools — зараз чи пізніше?

---

## 7. Наскрізні open questions
- **Q1 (ключове, confirm-point):** Користувач приймає direct push для admin-flow (див. 1A.4), але загальне правило «план → локальні тести → push після OK» лишається; треба визначити межу між ними.
  Варіанти: (a) push у `main` дозволено лише для категорій «prompts у БД» і «DB data» через міграції,
  код — через branch + підтвердження адміна в Telegram («deploy?»); (b) повний прямий push з
  guardrails; (c) push у branch `auto/*`, VM деплоїть його лише після команди `/approve <request_id>`.
- Q2: Одне джерело правди для промптів — git (міграції) чи БД (редагування в рантаймі через
  `prompt_editor`)? Як уникнути розходження?
- Q3: Чи переходимо колись на Telegram webhook замість polling (відкриває blue/green і callback)?

---

## 8. Known bugs to fold into plan
1. `last_processed_timestamp` з точністю до секунди + `created_at` з `message.date` → повідомлення в ту
   ж секунду можуть загубитися. Fix: курсор за монотонним `chat_history.id` (або `(ts, id)`).
2. `_handle_timer` пропускає нагадування, якщо є pending-повідомлення. Fix: обробити pending, потім
   нагадування (або окремі шляхи).
3. `set_done` / `get_user_done` / `get_day_progress` hardcode `lyosha`/`sasha` + legacy `progress`;
   `core/challenge.py` — `SASHA_ALIASES`/`LYOSHA_ALIASES`. Fix: міграція даних у `user_progress`,
   прибрати хардкод.
4. Video/GIF placeholder зараховується як виконання (anti-cheat hole) — закривається Темою 4; до того
   — тимчасово не зараховувати GIF/`animation`.
5. README / CLOUD_DEPLOYMENT.md застарілі — переписати під новий деплой.
6. `update_bot.py` небезпечний — видалити або замінити на документований deploy-скрипт.
7. Сервіс під root — non-root user, hardening (`ProtectSystem=strict`, `ReadWritePaths=/var/lib/sportnerdbot`,
   `NoNewPrivileges`).
8. `deploy.ps1` копіює `.env` з локальної машини та запускає ад-хок скрипти на кожен деплой — замінити.
9. Глобальний `asyncio.Lock` + синхронні LLM-виклики: довгі операції (video) не мають блокувати чат →
   async черга / окремий worker.

---

## 9. Output requirements (для Claude Opus)

Видай **два артефакти** у Markdown:

### 9.1 `ARCHITECTURE.md`
- Компонентна діаграма (Mermaid): Telegram, bot (polling), CV worker, SQLite, GCS backup, GitHub,
  deploy timer/runner, Grok Bot routine/box.
- Sequence diagrams: (1) video report, (2) admin_dev request → push → deploy → reply, (3) deploy з rollback.
- Розміщення файлів на VM, systemd units, users/permissions, secrets.
- Схема БД (нові таблиці: `schema_version`, `video_checks`, `offload_requests`, `admin_audit`) і
  стратегія міграцій.
- Контракти JSON (request/result/callback) з версіонуванням.
- Рішення по кожній open question: обраний варіант + обґрунтування, або явно «потрібне рішення
  користувача» з дефолтом.
- Ризики та mitigations (RAM, квоти, latency, безпека).

### 9.2 `TASKS.md` — work plan для Gemini 3.8 / `agy`
Кожна задача **самодостатня** (агент не бачить цього brief і чату), формат:
```
### T-NN: <назва>
- Goal: що і навіщо (2–4 речення, включно з потрібним контекстом)
- Files: створити / змінити / не чіпати
- Steps: конкретні кроки
- Acceptance criteria: перевірювані пункти
- Tests: які тести додати/запустити (команди), локально, без мережі де можливо
- Dependencies: T-xx
- Order / phase: номер фази
- Out of scope / guardrails: що агенту заборонено (push, secrets, prod DB)
```
Вимоги до розбиття:
- Розмір: одна задача ≈ 1 PR, ≤ ~300 рядків diff, ≤ ~1–2 год роботи агента.
- Фази: (0) bugfix + тести, (1) DB relocation + migrations (включно з `pending_replies`), (2) pull-based deploy + rollback,
  (3) video intake (gate → CV worker → judge integration → pushup fix), (4) webhook offload contract,
  (5) admin_dev flow + guardrails, (6) docs.
- Задачі, що вимагають дій на VM/GCP/GitHub settings або secrets, позначити **[MANUAL]** з
  інструкцією для людини — агент їх не виконує.
- Жодна задача не робить `git push` / деплой без явного кроку підтвердження користувача (крім
  частини admin-flow, якщо Q1 вирішено на користь прямого push).
- Наприкінці — dependency graph (Mermaid) і критичний шлях.

## Додаток: асинхронний video flow і тон відповідей (user intention, додано пізніше)

- **Намір користувача.** Відео обробляється асинхронно через той самий механізм, що й admin-flow: endpoint/API на VM.
  1. Бот отримує відео.
  2. Одразу коротко відповідає й ставить async задачу на CV worker (локально або на box).
  3. Коли рахунок готовий, worker викликає callback, і бот відповідає в чаті реплаєм на відео.
- **Тон.** Відповіді генерує Gemini в стилі coach persona. Приклади від користувача (few-shot samples):
  - Ack при отриманні: «Беру попкорну і йду дивитись відео»
  - Майже норма: «Підрахунок завершено — не вистачає одного, докинеш?»
  - Сильно менше норми: «Там і половини немає — за кого ти мене маєш»
  - Погана якість або не видно: «Сорі, нічорта не видно»
  - Не та вправа або флуд: «Я думав, там присяд/віджимання, а відео цікаве, проте не по темі»
- **CV facts (останні).** Віджимання з фронтальної камери не рахуються за кутом ліктя: лікті часто не видно, плюс foreshortening. Потрібен сигнал вертикального руху плечей і носа. Тестові відео з ground truth: `7XRAS` — 30–34 reps, `7XQZJ` — ≈29 reps з паузами.
