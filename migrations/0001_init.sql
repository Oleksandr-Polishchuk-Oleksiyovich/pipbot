-- 0001_init.sql: Baseline schema for SportNerdBot

CREATE TABLE IF NOT EXISTS progress (
    date TEXT PRIMARY KEY,
    lyosha_done INTEGER DEFAULT 0,
    sasha_done INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS users (
    slug TEXT PRIMARY KEY,
    telegram_id INTEGER UNIQUE,
    telegram_name TEXT DEFAULT '',
    username TEXT DEFAULT '',
    display_name TEXT NOT NULL,
    emoji TEXT NOT NULL DEFAULT '🏋️',
    description TEXT NOT NULL DEFAULT '',
    aliases_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'uninitialized',
    difficulty TEXT NOT NULL DEFAULT 'normal',
    lives INTEGER,
    max_lives INTEGER,
    base_pushups INTEGER NOT NULL DEFAULT 10,
    ladder_offset INTEGER NOT NULL DEFAULT 0,
    alt_pushups INTEGER,
    max_pushups_reported INTEGER,
    max_squats_reported INTEGER,
    joined_date TEXT NOT NULL DEFAULT '2026-08-31',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_progress (
    date TEXT NOT NULL,
    user_slug TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (date, user_slug),
    FOREIGN KEY (user_slug) REFERENCES users(slug)
);

CREATE TABLE IF NOT EXISTS chat_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    user_name TEXT,
    role TEXT,
    text TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS bot_state (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS prompts (
    key TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    updated_by TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS prompt_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    content TEXT NOT NULL,
    version INTEGER NOT NULL,
    updated_by TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
