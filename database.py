import os
import sqlite3
import json
from typing import Optional, Dict, Any, List
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "sessions", "bot.db")


DEFAULT_TITLE_TEMPLATE = """Образовательная автономная некоммерческая организация высшего образования
«МОСКОВСКИЙ ТЕХНОЛОГИЧЕСКИЙ ИНСТИТУТ»

по дисциплине
«{discipline}»
на тему
«{topic}»

Выполнил:
студент группы {group_name} {full_name}

Проверил:
{teacher}

Москва 2026"""


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
                custom_title_template TEXT DEFAULT '',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Миграция: проверяем наличие колонок в существующих БД
        cursor.execute("PRAGMA table_info(user_profiles)")
        cols = [row[1] for row in cursor.fetchall()]
        if "clean_chat_mode" not in cols:
            cursor.execute("ALTER TABLE user_profiles ADD COLUMN clean_chat_mode INTEGER DEFAULT 0")
        if "custom_title_template" not in cols:
            cursor.execute("ALTER TABLE user_profiles ADD COLUMN custom_title_template TEXT DEFAULT ''")
        
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
                INSERT INTO user_profiles (
                    user_id, full_name, group_name, university, teacher, discipline, custom_title_template
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                user_id, 
                "Плотникова Таисия", 
                "ОККИПд-305", 
                "ОАНО ВО «МОСКОВСКИЙ ТЕХНОЛОГИЧЕСКИЙ ИНСТИТУТ»", 
                "преподаватель Радонежская Нина Владиславовна", 
                "4.01 Внедрение информационных систем",
                DEFAULT_TITLE_TEMPLATE
            ))
            conn.commit()
            cursor.execute("SELECT * FROM user_profiles WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()

        data = dict(row)
        if not data.get("custom_title_template"):
            data["custom_title_template"] = DEFAULT_TITLE_TEMPLATE
        if not data.get("full_name"):
            data["full_name"] = "Плотникова Таисия"
        if not data.get("group_name"):
            data["group_name"] = "ОККИПд-305"
        if not data.get("university"):
            data["university"] = "ОАНО ВО «МОСКОВСКИЙ ТЕХНОЛОГИЧЕСКИЙ ИНСТИТУТ»"
        if not data.get("teacher"):
            data["teacher"] = "преподаватель Радонежская Нина Владиславовна"
        if not data.get("discipline"):
            data["discipline"] = "4.01 Внедрение информационных систем"
        return data


def update_profile(user_id: int, **kwargs):
    valid_fields = [
        "full_name", "group_name", "university", "teacher", 
        "discipline", "antiplagiat_mode", "include_title", 
        "include_questions", "omit_theory", "include_charts", "variant",
        "clean_chat_mode", "custom_title_template"
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
