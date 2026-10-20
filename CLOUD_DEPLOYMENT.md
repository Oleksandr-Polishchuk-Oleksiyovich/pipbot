# Інфраструктура та доступ до Google Cloud (GCP)

Цей документ містить технічну інформацію про розгортання Telegram-бота **«Спорт Зануда»** (`@SportNerdBot`) у хмарі Google Cloud Platform (GCP).

---

## 1. Параметри хмари та віртуальної машини

| Параметр | Значення |
| :--- | :--- |
| **GCP Project ID** | `gen-lang-client-0185250272` |
| **GCP Project Name** | `Gemini Voice PoC` |
| **GCP Project Number** | `92294967951` |
| **Google Account** | `polilex789@gmail.com` |
| **VM Instance Name** | `sportnerd-bot-vm` |
| **Зона (Zone)** | `us-central1-a` (Iowa, Always Free Tier) |
| **Тип машини (Machine Type)** | `e2-micro` (2 vCPU, 1 GB RAM — $0/місяць) |
| **Диск (Boot Disk)** | 20 GB `pd-standard` (Standard Persistent Disk) |
| **Операційна система** | Ubuntu 24.04 LTS |
| **Внутрішній IP (Internal IP)** | `10.128.0.2` |
| **Зовнішній IP (External IP)** | `35.254.7.29` |
| **Системний користувач** | `sportnerd` (non-root з обмеженими привілеями) |
| **Консоль GCP** | [Google Cloud Console VM Instances](https://console.cloud.google.com/compute/instances?project=gen-lang-client-0185250272) |

---

## 2. Розміщення файлів та архітектура на VM

Відповідно до цільової архітектури, код застосунку, база даних та секрети суворо розділені:

* **Каталог релізів коду:** `/opt/sportnerdbot/releases/<sha>/`
* **Атомарний symlink активного коду:** `/opt/sportnerdbot/current -> /opt/sportnerdbot/releases/<sha>`
* **Спільне віртуальне оточення Python:** `/opt/sportnerdbot/venv/`
* **База даних SQLite (WAL):** `/var/lib/sportnerdbot/challenge.db` (винесена за межі коду)
* **Резервні копії БД:** `/var/lib/sportnerdbot/backups/<timestamp>.db`
* **Статуси деплоїв:** `/var/lib/sportnerdbot/deploy_status/<request_id>.json`
* **Змінні оточення та секрети:** `/etc/sportnerdbot/env` (права `0600`, власник `sportnerd:sportnerd`)
* **Скрипт релізного деплою:** `/usr/local/bin/sportnerd-deploy.sh -> /opt/sportnerdbot/current/deploy/deploy.sh`

---

## 3. Системні служби Systemd

Усі служби працюють під системним non-root обліковим записом **`sportnerd`** із увімкненим hardening (`ProtectSystem=strict`, `NoNewPrivileges=yes`, `MemoryMax`):

1. **`sportnerdbot.service`** — Основний процес бота (aiogram long-polling):
   - Файл: `/etc/systemd/system/sportnerdbot.service`
   - Каталог: `WorkingDirectory=/opt/sportnerdbot/current`
   - Ліміт пам'яті: `MemoryMax=550M`
2. **`sportnerd-cv.service`** — Фоновий воркер комп'ютерного зору (MediaPipe Pose + Gemini Vision):
   - Файл: `/etc/systemd/system/sportnerd-cv.service`
   - Ліміт пам'яті: `MemoryMax=500M`, `Nice=10`, `CPUWeight=50`, concurrency=1
3. **`sportnerd-gateway.service`** — HTTP aiohttp мікросервіс для вебхуків і тригерів:
   - Файл: `/etc/systemd/system/sportnerd-gateway.service`
   - Порт: `:8443` (loopback + firewall-захищений зовнішній інтерфейс)
   - Ендпойнти: `/deploy`, `/callback`, `/healthz`
   - Ліміт пам'яті: `MemoryMax=80M`
4. **`sportnerdbot-deploy.timer` + `.service`** — Резервний safety-net таймер:
   - Періодично (кожні 1–2 хв) виконує перевірку оновлень у `origin/main`.

---

## 4. Керування та діагностика через gcloud CLI

Усі команди виконуються з локального терміналу (PowerShell або Bash) з Google Cloud SDK:

### 4.1 Підключення через SSH
```powershell
& "C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd" compute ssh sportnerd-bot-vm --project=gen-lang-client-0185250272 --zone=us-central1-a
```

### 4.2 Перегляд статусу служб
```powershell
& "C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd" compute ssh sportnerd-bot-vm --project=gen-lang-client-0185250272 --zone=us-central1-a --command="sudo systemctl status sportnerdbot sportnerd-cv sportnerd-gateway"
```

### 4.3 Перегляд логів бота у реальному часі
```powershell
& "C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd" compute ssh sportnerd-bot-vm --project=gen-lang-client-0185250272 --zone=us-central1-a --command="sudo journalctl -u sportnerdbot -f"
```

### 4.4 Перезапуск служби бота
```powershell
& "C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd" compute ssh sportnerd-bot-vm --project=gen-lang-client-0185250272 --zone=us-central1-a --command="sudo systemctl restart sportnerdbot"
```

---

## 5. Процедура деплою коду (Pull-based release deployment)

Оновлення коду на сервері реалізовано через pull-based архітектуру з версіонованими каталогами релізів, автоматичними міграціями, онлайн-бекапом та automatic rollback у разі помилок:

### 5.1 Спосіб 1: Через шлюз Gateway (з локальної машини)
```powershell
# Запуск через PowerShell-помічник з підписом HMAC
.\deploy.ps1 -GatewayUrl https://35.254.7.29:8443 -HmacSecret "СЕКРЕТ_ШЛЮЗУ"
```

### 5.2 Спосіб 2: Через gcloud SSH
```powershell
& "C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd" compute ssh sportnerd-bot-vm --project=gen-lang-client-0185250272 --zone=us-central1-a --command="sudo -u sportnerd /usr/local/bin/sportnerd-deploy.sh"
```

### 5.3 Що відбувається під час релізу:
1. `git fetch --all` завантажує свіжі коміти з GitHub.
2. Створюється ізольована директорія релізу `/opt/sportnerdbot/releases/<sha>`.
3. Оновлюються пакети у venv (лише у разі зміни хешу `requirements.txt`).
4. Робиться онлайн-бекап SQLite бази (`sqlite3 .backup`) у `/var/lib/sportnerdbot/backups/`.
5. Виконуються нові версіоновані міграції (`python -m database.migrate`).
6. Запускається smoke-тест (`import bot; bot.build_app()`) та офлайн тести.
7. Атомарно перемикається symlink `/opt/sportnerdbot/current`.
8. Перезапускається служба `sportnerdbot` та проводиться health check.
9. У разі будь-якого збою symlink автоматично повертається на попередній реліз (automatic rollback).

Детальні інструкції щодо відкату та відновлення бази даних наведено у файлі [docs/RUNBOOK.md](docs/RUNBOOK.md).

---

## 6. Запобігання конфліктам (Telegram 409 Conflict)

Telegram Bot API забороняє одночасне опитування (`getUpdates`) від кількох процесів з одним токеном:
* **Локальний запуск має бути ВИМКНЕНИЙ**, коли служба активна на хмарі.
* Якщо потрібно запустити бота локально для розробки — спочатку зупиніть службу на VM:
  ```powershell
  & "C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd" compute ssh sportnerd-bot-vm --project=gen-lang-client-0185250272 --zone=us-central1-a --command="sudo systemctl stop sportnerdbot"
  ```
