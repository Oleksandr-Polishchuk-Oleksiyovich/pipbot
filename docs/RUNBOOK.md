# SportNerdBot — Operations Runbook

> Інструкція з експлуатації, моніторингу, деплою, відкату та відновлення після збоїв для бота SportNerdBot на GCP VM.

---

## 1. Загальний огляд сервісів

Бот працює під керуванням `systemd` на віртуальній машині GCP (`sportnerd-bot-vm`) під окремим системним користувачем **`sportnerd`** (non-root).

| Сервіс | Файл unit | Призначення |
|---|---|---|
| **`sportnerdbot`** | `/etc/systemd/system/sportnerdbot.service` | Основний aiogram long-polling процес бота |
| **`sportnerd-cv`** | `/etc/systemd/system/sportnerd-cv.service` | Асинхронний MediaPipe CV воркер відеозвітів |
| **`sportnerd-gateway`** | `/etc/systemd/system/sportnerd-gateway.service` | HTTP aiohttp API (`:8443`) для `/deploy`, `/callback`, `/healthz` |
| **`sportnerdbot-deploy.timer`** | `/etc/systemd/system/sportnerdbot-deploy.timer` | Періодичний safety-net таймер автоматичного оновлення (кожні 1–2 хв) |

### 1.1 Корисні команди для керування сервісами

```bash
# Перевірка статусу всіх сервісів
sudo systemctl status sportnerdbot sportnerd-cv sportnerd-gateway sportnerdbot-deploy.timer

# Перезапуск бота
sudo systemctl restart sportnerdbot

# Перегляд логів бота у реальному часі
sudo journalctl -u sportnerdbot -f

# Перегляд останніх 100 рядків логів CV-воркера
sudo journalctl -u sportnerd-cv -n 100 --no-pager

# Перегляд логів шлюзу gateway
sudo journalctl -u sportnerd-gateway -f
```

---

## 2. Процедура деплою

Деплой у системі є **pull-based** і виконується за принципом атомарного перемикання symlink на версіоновані каталоги релізів:
`/opt/sportnerdbot/current -> /opt/sportnerdbot/releases/<sha>`.

### 2.1 Спосіб 1: Через SportNerd Gateway (Рекомендований)

Виклик API через PowerShell або curl з передачею HMAC-SHA256 підпису:

```powershell
# З локальної машини за допомогою PowerShell helper:
.\deploy.ps1 -GatewayUrl https://35.254.7.29:8443 -HmacSecret "ВАШ_СЕКРЕТ"

# Або деплой конкретного коміту:
.\deploy.ps1 -Sha abc1234 -GatewayUrl https://35.254.7.29:8443 -HmacSecret "ВАШ_СЕКРЕТ"
```

Шлюз перевіряє HMAC, реєструє запит, повертає `202 Accepted` і запускає деплой у фоні.

### 2.2 Спосіб 2: Прямий запуск скрипту на VM (SSH)

```bash
# Підключення до VM
gcloud compute ssh sportnerd-bot-vm --project=gen-lang-client-0185250272 --zone=us-central1-a

# Запуск деплою від імені користувача sportnerd:
sudo -u sportnerd /usr/local/bin/sportnerd-deploy.sh origin/main

# Або вказати конкретний SHA коміту:
sudo -u sportnerd /usr/local/bin/sportnerd-deploy.sh <commit_sha>
```

### 2.3 Спосіб 3: Тестовий режим (Dry Run)

Для перевірки логіки оновлення без реального перезапуску системних служб та без ризику зупинки бота:

```bash
sudo -u sportnerd DRY_RUN=1 /usr/local/bin/sportnerd-deploy.sh origin/main
```

### 2.4 Етапи, які автоматично виконує `deploy.sh`:
1. `git fetch --all` і валідація коміту SHA.
2. Створення ізольованого каталогу `/opt/sportnerdbot/releases/<sha>` через `git archive`.
3. Перевірка кешу залежностей `requirements.txt` (якщо хеш змінився — `pip install`, інакше спільний кеш).
4. **Створення резервної копії БД** через `sqlite3 .backup` у `/var/lib/sportnerdbot/backups/<timestamp>.db`.
5. Запуск міграцій: `python -m database.migrate --db $DB_PATH`.
6. Smoke-тест імпорту (`import bot; bot.build_app()`) та швидкі офлайн-тести (`pytest -m offline`).
7. Атомарне перемикання symlink: `ln -sfn releases/<sha> current`.
8. Перезапуск служби `sportnerdbot` через systemd.
9. Health-check перевірка стану служби.
10. Очищення старих релізів (зберігаються останні 5 релізів; поточний та попередній релізи захищені від видалення).

### 2.5 Перевірка статусу деплою

```bash
# Перевірка через локальний статус-файл
cat /var/lib/sportnerdbot/deploy_status/<request_id_або_sha>.json

# Або через Gateway API
curl -k https://127.0.0.1:8443/deploy/<request_id>
```

---

## 3. Процедура відкату (Rollback)

### 3.1 Автоматичний відкат (Automated Rollback)
Якщо на етапах міграції, smoke-тесту або health-check трапляється помилка, `deploy/deploy.sh` автоматично:
1. Залишає або відновлює попередній symlink: `ln -sfn $PREV /opt/sportnerdbot/current`.
2. Перезапускає службу `sportnerdbot`.
3. Фіксує статус `rolled_back` у JSON-файлі статусу.
4. Повертає ненульовий код завершення.

### 3.2 Ручний відкат через Telegram-команду (`/rollback`)
Адміністратор може надіслати команду безпосередньо в чат Telegram:
```text
/rollback
```
Бот перевіряє права адміністратора за `telegram_id` та наявністю passphrase, викликає процедуру відкату на попередній релізний symlink та сповіщає про результат.

### 3.3 Ручний відкат через командний рядок (Emergency CLI Rollback)
Якщо бот не відповідає або сталася критична помилка після деплою:

```bash
# 1. Переглянути наявні релізи
ls -lt /opt/sportnerdbot/releases/

# 2. Перемкнути symlink на попередній стабільний реліз
sudo -u sportnerd ln -sfn /opt/sportnerdbot/releases/<previous_stable_sha> /opt/sportnerdbot/current

# 3. Перезапустити сервіс бота
sudo systemctl restart sportnerdbot

# 4. Переконатися, що сервіс активний
sudo systemctl status sportnerdbot
```

---

## 4. Робота з базою даних та бекапами

База даних розташована поза каталогом релізів за шляхом:
`/var/lib/sportnerdbot/challenge.db`.
Режим роботи: **SQLite WAL** (`challenge.db`, `challenge.db-wal`, `challenge.db-shm`).

### 4.1 Перевірка статусу міграцій
```bash
sudo -u sportnerd /opt/sportnerdbot/venv/bin/python -m database.migrate \
    --db /var/lib/sportnerdbot/challenge.db \
    --status
```

### 4.2 Ручне створення бекапу
```bash
BACKUP_FILE="/var/lib/sportnerdbot/backups/manual_$(date +%Y%m%d_%H%M%S).db"
sudo -u sportnerd sqlite3 /var/lib/sportnerdbot/challenge.db ".backup $BACKUP_FILE"
echo "Backup created at $BACKUP_FILE"
```

### 4.3 Повне відновлення БД з бекапу (Disaster Recovery Restore)

У разі пошкодження бази даних або помилкових змін виконайте наступні кроки:

1. **Зупинити сервіси бота та воркерів:**
   ```bash
   sudo systemctl stop sportnerdbot sportnerd-cv
   ```

2. **Створити аварійну копію поточного стану:**
   ```bash
   sudo -u sportnerd cp /var/lib/sportnerdbot/challenge.db /var/lib/sportnerdbot/challenge.db.corrupted_$(date +%s)
   ```

3. **Скопіювати цільовий файл резервної копії:**
   ```bash
   TARGET_BACKUP="/var/lib/sportnerdbot/backups/20261020_200000.db"
   sudo -u sportnerd cp "$TARGET_BACKUP" /var/lib/sportnerdbot/challenge.db
   ```

4. **Видалити старі WAL/SHM файли:**
   ```bash
   sudo rm -f /var/lib/sportnerdbot/challenge.db-wal /var/lib/sportnerdbot/challenge.db-shm
   ```

5. **Перевірити права власності та цілісність:**
   ```bash
   sudo chown sportnerd:sportnerd /var/lib/sportnerdbot/challenge.db
   sudo -u sportnerd sqlite3 /var/lib/sportnerdbot/challenge.db "PRAGMA integrity_check;"
   ```

6. **Запустити сервіси:**
   ```bash
   sudo systemctl start sportnerdbot sportnerd-cv
   sudo systemctl status sportnerdbot sportnerd-cv
   ```

---

## 5. Адміністрування, Guardrails та команди чату

### 5.1 Рівні обробки адмін-запитів
- **`admin_quick` (In-Bot):** Зміна параметрів челенджу (життя, денні норми, розклад, ручні позначки виконання). Виконується миттєво через AFC-тули всередині бота.
- **`admin_dev` (Offloaded):** Зміна коду, структури бази даних або системних промптів. Бот повертає `request_id`, перенаправляє запит на Box, де агент генерує код/міграцію, тестує, комітить і запускає деплой.

### 5.2 Захищені зони та команда `/approve`
Якщо запит `admin_dev` зачіпає критичні зони:
- Файли середовища та конфігурації секретів (`.env`, `/etc/sportnerdbot/env`)
- Скрипти каталогу `deploy/`
- Файли `systemd` конфігурацій
- Деструктивні SQL-операції (`DROP`, `DELETE` з історії)

Бот блокує автоматичний деплой і очікує явного підтвердження в чаті:
```text
/approve <request_id>
```
Лише після перевірки та схвалення тим самим адміністратором деплой продовжується.

### 5.3 Журнал аудиту
Усі дії адмінів фіксуються в таблиці `admin_audit`:
```bash
sudo -u sportnerd sqlite3 /var/lib/sportnerdbot/challenge.db \
    "SELECT id, request_id, author_slug, request, status, created_at FROM admin_audit ORDER BY id DESC LIMIT 10;"
```

---

## 6. CV воркер та черга відеозвітів

### 6.1 Перевірка черги `video_checks`
```bash
sudo -u sportnerd sqlite3 /var/lib/sportnerdbot/challenge.db \
    "SELECT id, request_id, user_slug, exercise, reps, confidence, valid, status, created_at FROM video_checks ORDER BY id DESC LIMIT 5;"
```

### 6.2 Моніторинг пам'яті та swap
На VM типу `e2-micro` (1 GB RAM) критично контролювати використання оперативної пам'яті:
```bash
# Перевірка RAM та Swap
free -m
swapon --show

# Перевірка, чи не спрацьовував OOM killer
sudo dmesg -T | grep -i -E "oom|killed process"
```

Якщо воркер завис у статусі `running` через аварійне завершення:
```bash
sudo -u sportnerd sqlite3 /var/lib/sportnerdbot/challenge.db \
    "UPDATE video_checks SET status='failed' WHERE status='running' AND datetime(created_at) < datetime('now', '-15 minutes');"
```

---

## 7. Діагностика та усунення неполадок (Troubleshooting)

### 7.1 Помилка Telegram: 409 Conflict
**Симптом:** У журналі `journalctl -u sportnerdbot` з'являється повідомлення:
`TelegramConflictError: Conflict: terminated by other getUpdates request`.
**Причина:** Одночасно запущено два екземпляри бота з одним токеном (наприклад, локальний процес розробника та служба на VM).
**Вирішення:**
1. Зупинити будь-які локальні екземпляри бота на комп'ютері розробника.
2. Перевірити процеси на сервері:
   ```bash
   ps aux | grep -i "bot.py"
   ```
3. Перезапустити системну службу:
   ```bash
   sudo systemctl restart sportnerdbot
   ```

### 7.2 Шлюз повертає 401 Unauthorized
**Симптом:** При виклику `deploy.ps1` шлюз повертає помилку 401.
**Причина:**
1. Невірний `GATEWAY_HMAC_SECRET`.
2. Розсинхронізація системного годинника між машинами (вікно допустимого skew перевищує ±5 хвилин).
**Вирішення:**
1. Перевірити секрет у `/etc/sportnerdbot/env`.
2. Перевірити системний час: `date -u`.

### 7.3 Відновлення невідправлених повідомлень (`pending_replies`)
Якщо під час деплою було перезавантажено процес бота, збережені відповіді відправляються автоматично при старті (`startup drain`). Для перевірки статусу:
```bash
sudo -u sportnerd sqlite3 /var/lib/sportnerdbot/challenge.db \
    "SELECT request_id, chat_id, status, attempts, created_at FROM pending_replies WHERE status='pending';"
```

## Box CV offload (long-poll)

CV-підрахунок відео винесено з e2-micro VM на Grok Bot box (у box немає вхідних з'єднань, тому box сам тягне задачі).

- **VM (bot):** відео -> `video_checks(status='offloaded')` + `cv_jobs(status='queued')`, одна відповідь `processing` (з `bot_texts`). Scheduler кожні 10 с (`ChatFlow.process_cv_results`) відповідає на `done/failed` (judge + Gemini-тон як раніше) і один раз шле `still_counting`, якщо результату немає довше `CV_STILL_COUNTING_MIN` (5 хв). Ніколи не просить переробити підхід.
- **VM (gateway, `gateway/cv_queue.py`):** `POST /cv/claim` — long-poll до `CV_CLAIM_WAIT_S` (25 с), 200 з job або 204; `GET /cv/jobs/{id}/video` — відео (gateway качає його з Telegram на claim у `CV_VIDEO_DIR`, видаляє після done/failed); `POST /cv/jobs/{id}/result` — `{claim_token, ok, result, timings, error}`. Lease `CV_LEASE_S` (300 с): прострочений claim -> requeue, після 3 спроб -> failed.
- **Auth:** кожен запит підписаний HMAC (`core.signing.sign_request`): `METHOD\nPATH\nTS\nNONCE\nsha256(body)`, заголовки `X-SN-Ts/X-SN-Nonce/X-SN-Signature`, вікно ±5 хв + одноразовий nonce (replay -> 401). Секрет — існуючий `GATEWAY_HMAC_SECRET`.
- **Box worker (`cv_worker/box_worker.py`):** env `CV_GATEWAY_URL`, `CV_HMAC_SECRET_ENV` (назва змінної, default `GATEWAY_HMAC_SECRET`), `CV_ENV_FILE`. Після 204 одразу новий claim; backoff лише на помилках 1/2/5/10/20/30 с. Кожен job: `heavy --who SportNerdBot-CV --max 600 python -m cv_worker.cv_job <video> <out.json>` (15 fps, мін. 12; 640px). Запуск: `nohup cv_worker/run_box_worker.sh >/dev/null 2>&1 &` (supervisor-цикл, pidfile `/tmp/sportnerd-boxcv.pid`; у box немає systemd --user/cron — після рестарту box запустити знову). Залежності на box: `pip install -r requirements-cv.txt` + apt `libegl1 libgles2`.
- **VM deps:** `deploy.sh` за замовчуванням `INSTALL_CV=0` (без mediapipe). `sportnerd-cv.service` на VM вимкнути: `systemctl disable --now sportnerd-cv`.
- **Мережа:** gateway має бути доступний з box: публічний HTTPS (reverse proxy з TLS -> 127.0.0.1:8443, `proxy_read_timeout >= 60s`, `proxy_buffering off` для /cv/claim) і firewall-правило лише на цей порт. aiohttp `keepalive_timeout=75`.
