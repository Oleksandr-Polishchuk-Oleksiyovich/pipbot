# 🏋️‍♂️ SportNerdBot («Спорт Зануда»)

Telegram AI-бот для відстеження щоденного спортивного челенджу з відтискань та присідань між **Сашею (батьком)** та **Льошею (сином)**, побудований на базі моделі Google Gemini, автоматичного комп'ютерного зору (MediaPipe Pose + Gemini Vision) та дворівневої системи адміністрування.

---

## 📌 Основна інформація

* **Bot Username:** `@SportNerdBot` (ID: `8637170983`)
* **Цільовий чат:** `Котяки-Віджимаки💪🏿` (ID: `-1004481392380`)
* **AI Модель:** Google Gemini (`gemini-2.5-flash-lite`, `gemini-2.5-flash`) з Automatic Function Calling (AFC) та ring-buffer rate limiter.
* **Хостинг:** Google Cloud Platform (Compute Engine `e2-micro`, Ubuntu 24.04 LTS, 24/7).
* **Системний користувач:** `sportnerd` (non-root, systemd hardening).

---

## 👥 Учасники челенджу та правила

* **Саша (Oleksandr Polishchuk):** денна норма 14 відтискань (або альтернативна вправа / відеозвіт).
* **Льоша (Олексій):** денна норма 25 відтискань (або альтернативна вправа / відеозвіт).
* **Чергування вправ:** система підтримує чергування днів (відтискання / присідання).
* **Система життів:** учасники мають ліміт життів, які списуються у підсумку дня (`settle`, за замовчуванням о 02:30 за Києвом — `DAY_ROLLOVER_HOUR`/`DAY_ROLLOVER_MINUTE`; відео між 00:00 і 02:30 йдуть у попередній день) у разі невиконання норми.
* **Режим довіри (`TRUST_MODE=1`, за замовчуванням):** заявлена в підписі/повідомленні кількість повторів зараховується на слово; CV лише інформує і ніколи не відхиляє. Якщо учасник оспорює підрахунок («неправильно порахував», «не 4 а 40»), бот вірить людині: з числом — виправляє залік, без числа — один раз м'яко питає «скільки вийшло?» і зараховує відповідь. Це **м'який режим «до виправлення»** — тимчасовий, поки CV-лічильник ненадійний. `TRUST_MODE=0` повертає стару поведінку.

---

## ⚙️ Архітектура системи

Система складається з чотирьох ключових компонентів на VM та агента на Grok Bot Box:

```
                                +---------------------------+
                                |      Telegram Bot API     |
                                +-------------+-------------+
                                              |
                     +------------------------+------------------------+
                     | getUpdates / sendMessage                        | getFile
                     v                                                 v
+---------------------------------------+             +----------------------------------+
| sportnerdbot.service                  |             | sportnerd-cv.service             |
| - aiogram long-polling                |             | - MediaPipe Pose (reps counter)  |
| - Router (Coach / Admin / Onboarding) |             | - Gemini Vision (exercise filter)|
| - Ring-buffer rate limiter            |             | - Concurrency=1, MemoryMax=500M  |
+-------------------+-------------------+             +----------------+-----------------+
                    |                                                  |
                    | (enqueue / read result)                          | (update video_checks)
                    v                                                  v
+----------------------------------------------------------------------------------------+
| SQLite WAL Database: /var/lib/sportnerdbot/challenge.db                                |
| Tables: users, user_progress, chat_history, video_checks, pending_replies, admin_audit |
+----------------------------------------------------------------------------------------+
                    ^                                                  ^
                    | (trigger release deploy)                         | (callback)
+-------------------+-------------------+             +----------------+-----------------+
| sportnerd-gateway.service             |             | Grok Bot Box                     |
| - aiohttp API (:8443)                 | <---------  | - admin_dev offload agent        |
| - /deploy, /callback, /healthz        |  HMAC-SHA256| - Automated tests & git push     |
+---------------------------------------+             +----------------------------------+
```

### 1. Сервіси на віртуальній машині:
* **`sportnerdbot.service`:** Основний процес опитування Telegram (long-polling), запущений з атомарного symlink `/opt/sportnerdbot/current`.
* **`sportnerd-cv.service`:** Окремий асинхронний сервіс черги комп'ютерного зору (MediaPipe), що аналізує відеозвіти з обмеженням оперативної пам'яті (`MemoryMax=500M`).
* **`sportnerd-gateway.service`:** Легкий HTTP aiohttp-шлюз на порту `:8443` для прийому вебхуків деплою (`/deploy`) та результатів зовнішньої обробки (`/callback`).
* **`sportnerdbot-deploy.timer`:** Резервний periodic safety-net таймер, що перевіряє оновлення в `origin/main`.

### 2. Відокремлена база даних (`/var/lib/sportnerdbot/challenge.db`):
* База даних винесена за межі коду релізів.
* Працює в режимі **SQLite WAL** (`busy_timeout=5000`, `foreign_keys=ON`).
* Версіоновані міграції через `python -m database.migrate` з онлайн-бекапом перед кожною зміною.

---

## 📹 Конвеєр верифікації відеозвітів

Бот реалізує повний захист від фальсифікацій (anti-cheat) для відео та кружечків:
1. **Вхідний фільтр (Gate):** Відхилення GIF/animation, перевірка тривалості (`duration >= 5s`), ліміту розміру Telegram (`<= 20MB`) та дедуплікація за `file_unique_id`.
2. **Асинхронна черга (`video_checks`):** Бот негайно відповідає учаснику в чаті у стилі тренера («Беру попкорну і йду дивитись відео») та передає завдання CV-воркеру.
3. **Класифікація вправи (Gemini Vision):** Аналіз 3–4 ключових кадрів стабільного сегмента відео для виявлення спаму, мемів або невідповідної вправи.
4. **Підрахунок повторень (MediaPipe Pose):** Розрахунок амплітуди повторень. Для відтискань з фронтальної камери відстежується вертикальне зміщення плечей та носа (замість кутів ліктів).
5. **Узгодження та вердикт (`ReportJudgeAgent`):** Якщо класи збігаються, кількість повторень відповідає нормативу і впевненість `confidence >= 0.6` — норма автоматично зараховується з персоналізованою відповіддю тренера.

---

## 🛡️ Адміністрування та Guardrails

* **`admin_quick` (In-Bot):** Миттєва зміна параметрів челенджу (життя, норми, розклад) через виклики функцій всередині бота.
* **`admin_dev` (Offload на Box):** Складні запити (зміна коду, структури БД чи системних промптів) автоматично офлоадяться на Grok Bot Box через підписаний HMAC вебхук. Агент готує зміни, проганяє тести, робить коміт у git та ініціює деплой.
* **Захищені зони (Forbidden Zones):** Будь-які зміни конфігурації `.env`, deploy-скриптів, systemd-юнітів або деструктивні міграції вимагають підтвердження від адміністратора через Telegram-команду:
  ```text
  /approve <request_id>
  ```
* **Аварійний відкат:** Адміністратор може ініціювати відкат на попередній реліз безпосередньо з чату:
  ```text
  /rollback
  ```

---

## 🚀 Деплой та експлуатація

Деплой виконується за принципом **pull-based release directories with atomic symlink**:

```powershell
# Деплой через локальний PowerShell-скрипт (викликає Gateway API)
.\deploy.ps1 -GatewayUrl https://35.254.7.29:8443 -HmacSecret "СЕКРЕТ"

# Або запуск на сервері через SSH:
sudo -u sportnerd /usr/local/bin/sportnerd-deploy.sh origin/main
```

Детальні інструкції щодо розгортання, моніторингу та бекапів:
* [Інфраструктура та доступ до GCP](CLOUD_DEPLOYMENT.md)
* [Runbook з експлуатації та відновлення](docs/RUNBOOK.md)
* [Цільова архітектура системи](docs/ARCHITECTURE.md)

---

## 📚 Документація

**Поточний стан (актуально):**
* [docs/SPEC.md](docs/SPEC.md) — специфікація застосунку: що де працює (Telegram / VM / box / GitHub / Grok Bot), компоненти, карта репозиторію, таблиці БД, конфіги, gateway, правила челенджу (прогресія, XP, trust-режим, межа дня 02:30).
* [docs/FLOWS.md](docs/FLOWS.md) — потоки з mermaid-діаграмами й таблицями кроків: звичайне повідомлення, адмін-режим, відеозвіт (з диспутами), патч і деплой.
* [docs/RUNBOOK.md](docs/RUNBOOK.md) — експлуатація: деплой, відкат, бекап і відновлення.
* [deploy/README.md](deploy/README.md) — розгортання VM: gateway, Caddy, autodeploy, скрипти.
* [skills/admin_dev.md](skills/admin_dev.md) — інструкція агента розробки на box (admin_dev).

**Частково застаріле / історичне:**
* [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — «цільова» архітектура фаз 0–5; загальні принципи актуальні, але CV на VM і webhook-офлоад `admin_dev` замінені (див. SPEC/FLOWS).
* [CLOUD_DEPLOYMENT.md](CLOUD_DEPLOYMENT.md) — довідка з GCP-інфраструктури (VM, доступи); деплой-кроки в ній частково старі.

**Старі плани та дослідження (не описують поточний стан):**
* [optionToConsider/ARCHITECTURE.md](optionToConsider/ARCHITECTURE.md) — початковий проєкт архітектури.
* [optionToConsider/PLANNING_BRIEF.md](optionToConsider/PLANNING_BRIEF.md) — планувальний бриф.
* [optionToConsider/TASKS.md](optionToConsider/TASKS.md) — початковий беклог задач T-xx.
* [optionToConsider/grokbot-webhook-offload.md](optionToConsider/grokbot-webhook-offload.md) — ідея офлоаду на Grok Bot через webhook.
* [optionToConsider/computer-vision-rep-counting.md](optionToConsider/computer-vision-rep-counting.md), [optionToConsider/mediapipe_limits.md](optionToConsider/mediapipe_limits.md), [optionToConsider/HANDOFF.md](optionToConsider/HANDOFF.md) — дослідження CV-лічильника.

---

## 🧪 Локальне тестування

Проєкт покритий повним набором офлайн юніт-тестів, які не потребують підключення до Telegram або зовнішніх мереж:

```bash
# Запуск офлайн-тестів
.venv/bin/python -m pytest -q --ignore=tests/integration
```
