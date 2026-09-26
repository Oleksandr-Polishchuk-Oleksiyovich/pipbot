[ Telegram Message / Video / Text ]
               │
               ▼
      ┌──────────────────┐
      │   Jev Decision   │ <── Тільки класифікація (JSON)
      │      Engine      │     (без промпту тренера, блискавично)
      └────────┬─────────┘
               │
               ▼
     { intent, author, auth_valid, target_tool }
               │
       [ Python Router ] ──── (Пряме виконання безпечних операцій)
               │
               ▼
      ┌──────────────────┐
      │   Gemini Call    │ <── Тільки генерація персонажа (System 2)
      │  (Persona only)  │     (чистий гумор, розбивка рядків, стиль)
      └──────────────────┘