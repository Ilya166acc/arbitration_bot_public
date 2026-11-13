import os
import sqlite3
from config import DATABASE_PATH


def create_tables():
    """Создаёт таблицы, если их ещё нет."""
    # Создаем папку data, если её нет
    os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)

    conn = sqlite3.connect(DATABASE_PATH, timeout=30)
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")

    # === Таблица клиентов ===
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS clients (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            inn         TEXT NOT NULL,
            name        TEXT NOT NULL,
            days_back   INTEGER DEFAULT 30,
            added_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, inn)
        )
    """)

    # === Таблица дел ===
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS cases (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL,
            case_number     TEXT NOT NULL,
            client_inn      TEXT NOT NULL,
            court           TEXT,
            plaintiff       TEXT,
            defendant       TEXT,
            claim_amount    TEXT,
            receipt_date    TEXT,
            url             TEXT,
            ai_summary      TEXT,
            found_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, case_number)
        )
    """)

    conn.commit()
    conn.close()
    print("✅ Таблицы созданы (или уже существуют)")