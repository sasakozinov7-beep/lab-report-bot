import os
import sqlite3
import json
from typing import Optional, Dict, Any, List
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "sessions", "bot.db")


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        
        # Таблица профилей пользователей
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_profiles (
                user_id INTEGER PRIMARY KEY,
                full_name TEXT DEFAULT '',
                group_name TEXT DEFAULT '',
                university TEXT DEFAULT '',
                teacher TEXT DEFAULT '',
                discipline TEXT DEFAULT '',
                antiplagiat_mode INTEGER DEFAULT 1,
                include_title INTEGER DEFAULT 1,
                include_questions INTEGER DEFAULT 1,
                omit_theory INTEGER DEFAULT 1,
                include_charts INTEGER DEFAULT 1,
                variant TEXT DEFAULT '',
                clean_chat_mode INTEGER DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Миграция: проверяем наличие колонки clean_chat_mode в существующих БД
        cursor.execute("PRAGMA table_info(user_profiles)")
        cols = [row[1] for row in cursor.fetchall()]
        if "clean_chat_mode" not in cols:
            cursor.execute("ALTER TABLE user_profiles ADD COLUMN clean_chat_mode INTEGER DEFAULT 0")
        
        # Таблица истории сгенерированных лабораторных работ
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS lab_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                title TEXT,
                discipline TEXT,
                docx_path TEXT,
                pdf_path TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()


def get_profile(user_id: int) -> Dict[str, Any]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM user_profiles WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if not row:
            cursor.execute("""
                INSERT INTO user_profiles (user_id) VALUES (?)
            """, (user_id,))
            conn.commit()
            cursor.execute("SELECT * FROM user_profiles WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
        return dict(row)


def update_profile(user_id: int, **kwargs):
    valid_fields = [
        "full_name", "group_name", "university", "teacher", 
        "discipline", "antiplagiat_mode", "include_title", 
        "include_questions", "omit_theory", "include_charts", "variant",
        "clean_chat_mode"
    ]
    updates = []
    values = []
    for k, v in kwargs.items():
        if k in valid_fields:
            updates.append(f"{k} = ?")
            values.append(v)
            
    if not updates:
        return
        
    values.append(user_id)
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        query = f"UPDATE user_profiles SET {', '.join(updates)}, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?"
        cursor.execute(query, values)
        conn.commit()


def add_lab_history(user_id: int, title: str, discipline: str, docx_path: str, pdf_path: Optional[str] = None):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO lab_history (user_id, title, discipline, docx_path, pdf_path)
            VALUES (?, ?, ?, ?, ?)
        """, (user_id, title, discipline, docx_path, pdf_path or ""))
        conn.commit()


def get_user_labs(user_id: int, limit: int = 10) -> List[Dict[str, Any]]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM lab_history 
            WHERE user_id = ? 
            ORDER BY created_at DESC 
            LIMIT ?
        """, (user_id, limit))
        rows = cursor.fetchall()
        return [dict(r) for r in rows]


init_db()
