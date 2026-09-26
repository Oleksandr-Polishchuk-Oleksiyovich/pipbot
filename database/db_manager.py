import aiosqlite
from core.config import DB_PATH

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS prompt_store (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prompt_type TEXT UNIQUE,
                content TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS user_profiles (
                user_id TEXT PRIMARY KEY,
                name TEXT,
                likes TEXT DEFAULT '[]',
                dislikes TEXT DEFAULT '[]',
                interests TEXT DEFAULT '[]',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS dynamic_tools (
                key TEXT PRIMARY KEY,
                skill TEXT NOT NULL,
                python_script TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS system_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        ''')
        await db.commit()
