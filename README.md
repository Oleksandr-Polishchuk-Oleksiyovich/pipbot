# 🏋️‍♂️ SportNerdBot («Спорт Зануда»)

Telegram AI-бот для відстеження щоденного спортивного челенджу з відтискань між **Сашею (батьком)** та **Льошею (сином)**.

---

## 📌 Основна інформація

* **Bot Username**: `@SportNerdBot` (ID: `8637170983`)
* **Цільовий чат**: `Котяки-Віджимаки💪🏿` (ID: `-1004481392380`)
* **AI Модель**: Google Gemini (`gemini-3.5-flash-lite`, `gemini-3.1-flash-lite`, `gemini-3.5-flash`, `gemini-3.6-flash`) з Automatic Function Calling (AFC).
* **Хостинг**: Google Cloud Platform (GCP Compute Engine `e2-micro`, 24/7).

---

## 👥 Учасники челенджу та нормативи

1. **Саша (Oleksandr Polishchuk)**:
   * Норматив: **14 відтискань** на день (або 1 відео / чітке підтвердження).
2. **Льоша (Олексій)**:
   * Норматив: **25 відтискань** на день (або 1 відео / чітке підтвердження).

---

## ⚙️ Архітектура: 3 тригери та повний контекст дня

1. **Тригер 1: Таймер (Планові нагадування)**:
   * **Розклад**: `08:30`, `15:00`, `19:00` (Kyiv Time).
   * **Логіка**: Перевіряє базу даних. Якщо обидва учасники вже закрили норматив на сьогодні — нагадування **мовчки скіпається**. Якщо хтось не зробив — бот генерує дотепне нагадування.
2. **Тригер 2: Push (Нове повідомлення в чаті)**:
   * **Логіка**: При отриманні нового повідомлення (текст, відео, кружечок, відповідь на повідомлення) бот бере **всі повідомлення за сьогодні** як контекст і відправляє в Gemini. AI-агент самостійно викликає функцію `mark_participant_done(participant, date, note)` та оновлює статус у базі даних.
3. **Тригер 3: Нічний фінал (23:30)**:
   * **Логіка**: За 30 хвилин до півночі. Якщо норматив виконано обома — мовчить. Якщо ні — жартівливо-драматично нагадує, що залишилось 30 хвилин.

---

## 🗄️ Структура бази даних (`challenge.db`)

* **`progress`**: Відмітки про виконання нормативу (`date`, `participant`, `done`, `source`, `updated_at`).
* **`chat_history`**: Повна історія повідомлень у групі (`id`, `chat_id`, `user_id`, `user_name`, `role`, `text`, `timestamp`).
* **`bot_state`**: Збереження часової мітки останнього обробленого повідомлення (`key`, `value`).

---

## ☁️ Хмарне розгортання (GCP Compute Engine)

Повна інструкція та параметри доступу описані у файлі [CLOUD_DEPLOYMENT.md](CLOUD_DEPLOYMENT.md).

* **GCP Project**: `gen-lang-client-0185250272` (`Gemini Voice PoC`)
* **VM Name**: `sportnerd-bot-vm` (Zone: `us-central1-a`, e2-micro Always Free)
* **IP**: `35.254.7.29`
* **Каталог на сервері**: `/opt/sportnerdbot/`
* **Служба**: `sportnerdbot.service` (`systemd`)

### Швидкі команди:
* **Логи в реальному часі:**
  ```powershell
  & "C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd" compute ssh sportnerd-bot-vm --project=gen-lang-client-0185250272 --zone=us-central1-a --command="sudo journalctl -u sportnerdbot -f"
  ```
* **Перезапуск бота:**
  ```powershell
  & "C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd" compute ssh sportnerd-bot-vm --project=gen-lang-client-0185250272 --zone=us-central1-a --command="sudo systemctl restart sportnerdbot"
  ```
