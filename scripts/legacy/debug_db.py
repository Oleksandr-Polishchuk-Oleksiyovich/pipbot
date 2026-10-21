import sqlite3, os, sys
sys.stdout.reconfigure(encoding="utf-8")

conn = sqlite3.connect("/opt/sportnerdbot/challenge.db")
c = conn.cursor()
c.execute("SELECT id, user_name, role, text, timestamp FROM chat_history ORDER BY id DESC LIMIT 20")
rows = c.fetchall()
print("=== RECENT DB ROWS ===")
for r in reversed(rows):
    print(r)

c.execute("SELECT * FROM progress")
print("=== PROGRESS ROWS ===")
for r in c.fetchall():
    print(r)

c.execute("SELECT * FROM bot_state")
print("=== BOT STATE ===")
for r in c.fetchall():
    print(r)
