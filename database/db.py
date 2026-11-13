"""
Функции для работы с базой данных.
Все операции привязаны к user_id (Telegram ID пользователя).
"""
import sqlite3
from contextlib import closing
from typing import List, Dict, Optional
from config import DATABASE_PATH


def _connect():
    # timeout: не ждать блокировку файла дольше 30 секунд
    return sqlite3.connect(DATABASE_PATH, timeout=30)


# ============ РАБОТА С КЛИЕНТАМИ ============

def add_client(user_id: int, inn: str, name: str, days_back: int = 30) -> bool:
    conn = _connect()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO clients (user_id, inn, name, days_back) VALUES (?, ?, ?, ?)",
            (user_id, inn.strip(), name.strip(), days_back)
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def get_all_clients(user_id: int) -> List[Dict]:
    with closing(_connect()) as conn, closing(conn.cursor()) as cur:
        cur.execute(
            "SELECT id, inn, name, days_back, added_at FROM clients WHERE user_id = ? ORDER BY name",
            (user_id,)
        )
        rows = cur.fetchall()
    return [
        {"id": r[0], "inn": r[1], "name": r[2], "days_back": r[3], "added_at": r[4]}
        for r in rows
    ]


def get_client_by_inn(user_id: int, inn: str) -> Optional[Dict]:
    with closing(_connect()) as conn, closing(conn.cursor()) as cur:
        cur.execute(
            "SELECT id, inn, name, days_back FROM clients WHERE user_id = ? AND inn = ?",
            (user_id, inn)
        )
        row = cur.fetchone()
    if row:
        return {"id": row[0], "inn": row[1], "name": row[2], "days_back": row[3]}
    return None


def delete_client(user_id: int, inn: str) -> bool:
    with closing(_connect()) as conn, closing(conn.cursor()) as cur:
        cur.execute("DELETE FROM clients WHERE user_id = ? AND inn = ?", (user_id, inn))
        conn.commit()
        deleted = cur.rowcount > 0
    return deleted


def delete_client_cases(user_id: int, inn: str) -> int:
    with closing(_connect()) as conn, closing(conn.cursor()) as cur:
        cur.execute("DELETE FROM cases WHERE user_id = ? AND client_inn = ?", (user_id, inn))
        conn.commit()
        deleted = cur.rowcount
    return deleted


# ============ РАБОТА С ДЕЛАМИ ============

def add_case(user_id: int, case_data: Dict) -> bool:
    conn = _connect()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO cases 
            (user_id, case_number, client_inn, court, plaintiff, defendant, 
             claim_amount, receipt_date, url, ai_summary)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            user_id,
            case_data.get("case_number"),
            case_data.get("client_inn"),
            case_data.get("court"),
            case_data.get("plaintiff"),
            case_data.get("defendant"),
            case_data.get("claim_amount"),
            case_data.get("receipt_date"),
            case_data.get("url"),
            case_data.get("ai_summary"),
        ))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def case_exists(user_id: int, case_number: str) -> bool:
    with closing(_connect()) as conn, closing(conn.cursor()) as cur:
        cur.execute(
            "SELECT 1 FROM cases WHERE user_id = ? AND case_number = ?",
            (user_id, case_number)
        )
        exists = cur.fetchone() is not None
    return exists


def get_cases_by_client(user_id: int, inn: str) -> List[Dict]:
    with closing(_connect()) as conn, closing(conn.cursor()) as cur:
        cur.execute("""
            SELECT case_number, court, plaintiff, defendant, claim_amount, 
                   receipt_date, url, ai_summary, found_at
            FROM cases WHERE user_id = ? AND client_inn = ? ORDER BY found_at DESC
        """, (user_id, inn))
        rows = cur.fetchall()
    return [
        {
            "case_number": r[0], "court": r[1], "plaintiff": r[2],
            "defendant": r[3], "claim_amount": r[4], "receipt_date": r[5],
            "url": r[6], "ai_summary": r[7], "found_at": r[8]
        }
        for r in rows
    ]