import os
import sqlite3
from config import DATABASE_PATH

def create_tables():
    """Создаёт таблицы, если их ещё нет."""
    # Создаем папку data, если её нет
    os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)
    
    # --- ОТЛАДКА ---
    print(f"DEBUG: Абсолютный путь к базе данных: {os.path.abspath(DATABASE_PATH)}")
    # --------------
    
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()

    # ... (твой код CREATE TABLE clients и CREATE TABLE cases) ...

    conn.commit()
    
    # --- ОТЛАДКА ---
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    print(f"DEBUG: Созданные таблицы в базе: {cursor.fetchall()}")
    # --------------
    
    conn.close()
    print("✅ Таблицы созданы (или уже существуют)")