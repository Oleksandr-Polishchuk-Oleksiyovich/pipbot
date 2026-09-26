# Інфраструктура та доступ до Google Cloud (GCP)

Цей документ містить повну технічну інформацію про розгортання Telegram-бота **«Спорт Зануда»** (`@SportNerdBot`) у хмарі Google Cloud Platform (GCP). Використовуйте ці дані для підключення, діагностики, перегляду логів та оновлення коду бота.

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
| **Веб-посилання на VM у консолі** | [Google Cloud Console VM Instances](https://console.cloud.google.com/compute/instances?project=gen-lang-client-0185250272) |

---

## 2. Розміщення файлів та конфігурація на VM

* **Робоча директорія бота**: `/opt/sportnerdbot/`
* **Файл коду**: `/opt/sportnerdbot/bot.py`
* **Файл змінних оточення**: `/opt/sportnerdbot/.env` (містить `GEMINI_API_KEY`)
* **База даних SQLite**: `/opt/sportnerdbot/challenge.db`
* **Лог-файл**: `/opt/sportnerdbot/bot.log`
* **Віртуальне оточення Python**: `/opt/sportnerdbot/venv/`
* **Системна служба**: `/etc/systemd/system/sportnerdbot.service`
* **Користувач служби**: `root`

---

## 3. Корисні команди для керування через `gcloud CLI`

Всі команди виконуються з локального терміналу Windows (де встановлено Google Cloud SDK):

### 3.1. Підключення через SSH
```powershell
& "C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd" compute ssh sportnerd-bot-vm --project=gen-lang-client-0185250272 --zone=us-central1-a
```

### 3.2. Перегляд статусу служби бота
```powershell
& "C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd" compute ssh sportnerd-bot-vm --project=gen-lang-client-0185250272 --zone=us-central1-a --command="sudo systemctl status sportnerdbot"
```

### 3.3. Перегляд логів у реальному часі (live stream)
```powershell
& "C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd" compute ssh sportnerd-bot-vm --project=gen-lang-client-0185250272 --zone=us-central1-a --command="sudo journalctl -u sportnerdbot -f"
```

### 3.4. Перегляд останніх 50 рядків логів
```powershell
& "C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd" compute ssh sportnerd-bot-vm --project=gen-lang-client-0185250272 --zone=us-central1-a --command="sudo journalctl -u sportnerdbot -n 50 --no-pager"
```

### 3.5. Перезапуск служби бота
```powershell
& "C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd" compute ssh sportnerd-bot-vm --project=gen-lang-client-0185250272 --zone=us-central1-a --command="sudo systemctl restart sportnerdbot"
```

---

## 4. Як оновити код бота на хмарі (Деплой)

Якщо ви змінили [bot.py](bot.py) локально і хочете оновити його на хмарній машині:

1. **Скопіювати оновлений `bot.py` на сервер:**
   ```powershell
   & "C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd" compute scp bot.py sportnerd-bot-vm:/home/OleksandrPolishchuk/bot.py --project=gen-lang-client-0185250272 --zone=us-central1-a --quiet
   ```
2. **Перемістити в робочу директорію та перезапустити службу:**
   ```powershell
   & "C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd" compute ssh sportnerd-bot-vm --project=gen-lang-client-0185250272 --zone=us-central1-a --command="sudo cp /home/OleksandrPolishchuk/bot.py /opt/sportnerdbot/bot.py && sudo systemctl restart sportnerdbot"
   ```

---

## 5. Запобігання конфліктам (Telegram Conflicts)

Telegram Bot API забороняє одночасне опитування (`getUpdates`) від кількох інстансів з одним токеном.
* **Локальний запуск на ноутбуці має бути ВИМКНЕНИЙ**:
  Завдання у Windows Task Scheduler `SportNerdBot` вимкнено командою:
  ```powershell
  Disable-ScheduledTask -TaskName "SportNerdBot"
  ```
* Якщо потрібно запустити бота локально для розробки — спочатку зупиніть службу на хмарі:
  ```powershell
  & "C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd" compute ssh sportnerd-bot-vm --project=gen-lang-client-0185250272 --zone=us-central1-a --command="sudo systemctl stop sportnerdbot"
  ```
