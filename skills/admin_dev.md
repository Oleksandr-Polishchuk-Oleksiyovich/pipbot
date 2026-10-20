# Skill: Admin Dev Execution Pipeline (Grok Box / agy)

Цей документ визначає інструкцію та жорсткий пайплайн для headless-виконавця (`agy` або Gemini CLI) на машині **Grok Box** для виконання розробницьких завдань (`admin_dev`), ініційованих адміністратором у Telegram-боті **SportNerdBot** (@SportNerdBot).

> [!IMPORTANT]
> **[MANUAL CONFIGURATION]:**
> Виконавець на box повинен бути попередньо авторизований у GitHub (через SSH deploy key або personal access token із правами push у репозиторій) та мати неінтерактивну авторизацію у Google Gemini API (`GEMINI_API_KEY`). Налаштування самого середовища box та встановлення `agy` виконується вручну адміністратором.

---

## 1. Вхідний контракт (Контракт 8.1)

Виконавець отримує JSON-пейлоад через routine webhook:
```json
{
  "contract_version": "1.0",
  "request_id": "<uuid-v4>",
  "type": "admin",
  "created_at": "2026-10-20T20:47:00+03:00",
  "chat_id": -100123,
  "message_id": 4567,
  "reply_to_message_id": 4560,
  "user": {
    "telegram_id": 111,
    "username": "admin_username",
    "slug": "lyosha"
  },
  "payload": {
    "directive": "додай нову вправу планка або зміни стиль тренера",
    "text": "Obey me! зміни стиль тренера на більш суворий",
    "referenced_text": "попереднє повідомлення з контекстом",
    "recent_messages": [
      "[2026-10-20 20:40:00] lyosha: здали підхід",
      "[2026-10-20 20:41:00] Тренер: Красунчик!"
    ]
  },
  "reply_mode": "callback",
  "ts": 1760982420,
  "signature": "hmac-sha256(hex, body || ts, ROUTINE_SHARED_SECRET)"
}
```

---

## 2. Жорсткий 8-кроковий пайплайн виконання

Виконавець зобов'язаний суворо дотримуватися послідовності кроків 1–8:

### Крок 1. Отримати referenced message та перевірити ідемпотентність
- Перевірити `request_id`: якщо запит із цим `request_id` уже був опрацьований (коміт уже існує в історії git або статус у `admin_audit`), повернути збережений результат (no-op).
- Витягти `directive`, `text`, `referenced_text` та метадані автора (`slug`, `telegram_id`).

### Крок 2. Формалізувати запит
- Сформулювати чітку ціль інженерної зміни.
- Визначити рамки: що змінюється, а що залишається незмінним.

### Крок 3. Підтягнути контекст (останні N повідомлень)
- Проаналізувати `recent_messages` з пейлоаду для розуміння передісторії та точних формулювань.

### Крок 4. Переглянути метадані БД та spec-файли
- Ознайомитися з поточною схемою та специфікаціями:
  - `optionToConsider/ARCHITECTURE.md`
  - `optionToConsider/TASKS.md`
  - Поточні таблиці у `database/store.py` та каталозі `migrations/`.

### Крок 5. Спланувати зміни за категоріями
Розподілити завдання на відповідні категорії:
1. **DB data changes:** зміни числових даних або стану, які повинні бути персистентними — оформити як нову numbered data-migration у `migrations/`.
2. **Нові DB entities:** нові таблиці або колонки — створити розширювану (expand-only) SQL-міграцію у `migrations/` та оновити `database/store.py`.
3. **Код:** логіка агентів (`agents/`), пайплайни (`app/`), допоміжні модулі (`core/`).
4. **Prompts:** зміни системних промптів або `coach_dynamic` оформлюються **виключно через версіоновані міграції** `migrations/00NN_prompt_*.py` з викликом `apply_prompt_migration(conn, key, content, author)`.

### Крок 6. Виконати зміни та запустити тести
- Внести необхідні зміни у робочій копії репозиторію.
- Запустити обов'язкові офлайн unit-тести:
  ```bash
  .venv/bin/python -m pytest -q --ignore=tests/integration
  ```
- **Вимога GREEN PYTEST:** Усі тести повинні успішно проходити (exit code 0). Якщо тести впали:
  - Дозволено до 2 автоматичних спроб виправлення.
  - Якщо тести не проходять — зупинити процес, зафіксувати статус `failed` у callback і повідомити причину помилки без push.

### Крок 7. Сформувати persona-відповідь для адміна
- Відповідь формується у стилі `admin` prompt тренера «Спорт Зануда»:
  - Покірний та шанобливий тон перед адміном: «🦾 **Слухаюся і підкоряюся, <автор>!**».
  - Чіткий перелік внесених змін (номер версії міграції, змінені файли, нові правила або норми).
  - Telegram Markdown-форматування.

### Крок 8. Commit та Push (з урахуванням guardrails T-52)
- Сформувати git commit із обов'язковим зазначенням `request_id`:
  ```bash
  git commit -m "feat(admin): <опис зміни> [request_id=<request_id>]"
  ```
- **Перевірка заборонених зон (Forbidden Zones):**
  - **Заборонені зони:** `.env*`, `deploy/*`, `systemd/*`, `*.service`, `*.timer`, `.github/*`, `workflows/*`, деструктивні міграції (`DROP`, `DELETE`).
  - **Категорія A (Дозволені зміни):** DB data migration, prompt-migration, serving-код агентів, нові сутності, які НЕ зачіпають заборонені зони:
    - Виконати: `git push origin main`
    - Викликати gateway deploy (Контракт 8.4):
      `POST /deploy` із `{ "request_id": "<id>", "sha": "<commit_sha>", "chat_id": ..., "reply_text": "..." }`
  - **Категорія B (Заборонені зони):** зміни у файлах заборонених зон:
    - **ЗАБОРОНЕНО пушити напряму в `main`!**
    - Створити окрему гілку: `git push origin HEAD:refs/heads/auto/<request_id>`
    - Надіслати callback на gateway із повідомленням адміну про необхідність підтвердження:
      «Зміни зачіпають захищені файли конфігурації/деплою. Для застосування надішліть команду `/approve <request_id>`.»
    - Деплой виконується лише після отримання команди `/approve <request_id>` від того самого адміністратора.

---

## 3. Вихідні контракти (Callback 8.2 та Deploy 8.4)

### Callback на Gateway (`POST /callback`)
```json
{
  "contract_version": "1.0",
  "request_id": "<uuid-v4>",
  "type": "admin",
  "status": "done",
  "commit_sha": "<sha>",
  "reply_text": "🦾 **Слухаюся і підкоряюся, Льоша!**\n\nПравила тренера оновлено до версії v3...",
  "ts": 1760982500,
  "signature": "hmac-sha256(hex, body || ts, GATEWAY_HMAC_SECRET)"
}
```

### Deploy запит на Gateway (`POST /deploy`)
```json
{
  "contract_version": "1.0",
  "request_id": "<uuid-v4>",
  "sha": "<sha>",
  "chat_id": -100123,
  "reply_to_message_id": 4560,
  "reply_text": "🦾 Зміни успішно протестовано і розгорнуто!",
  "ts": 1760982600,
  "signature": "hmac-sha256(hex, body || ts, GATEWAY_HMAC_SECRET)"
}
```

---

## 4. Механізм відкату (Rollback Hook)

Якщо після деплою бота виявлено проблему або адміністратор надіслав команду `/rollback`:
1. Бот або gateway перемикає симлінк `current` на попередній реліз у `/opt/sportnerdbot/releases/`.
2. Запускається `systemctl restart sportnerdbot`.
3. Виконується healthcheck (`/healthz`).
4. Подія відкату фіксується у таблиці `admin_audit` зі статусом `deployed` (або `failed`).

---

## 5. Реальні шляхи кодової бази
- Агенти: `agents/` (`router.py`, `admin.py`, `prompt_editor.py`, `coach.py`)
- Оркестрація флоу: `app/flow.py`, `app/offload.py`
- Конфігурація та guardrails: `core/config.py`, `core/guardrails.py`, `core/signing.py`
- База даних та міграції: `database/store.py`, `database/migrate.py`, `migrations/`
- Деплой та шлюз: `gateway/app.py`, `deploy/deploy.sh`
