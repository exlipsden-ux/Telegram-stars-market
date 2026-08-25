import asyncio
import hashlib
import hmac
import json
import re
import sqlite3
import logging
import os
import datetime
from typing import Any, Callable, Dict, Awaitable

from aiogram import Bot, Dispatcher, types, F, BaseMiddleware
from aiogram.filters import Command, CommandObject
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, TelegramObject
from aiohttp import web
import aiohttp

# ⚙️ НАСТРОЙКИ БОТА — всё берётся из переменных окружения, см. .env.example
BOT_TOKEN = os.getenv("ALMAZ_BOT_TOKEN", "")
if not BOT_TOKEN:
    raise SystemExit("ALMAZ_BOT_TOKEN не задан. Токен бота выдаёт @BotFather.")

BOT_ID = int(BOT_TOKEN.split(":")[0])

# Владелец бота — только он управляет паролем и полноправными админами.
ROOT_OWNER_IDS = [int(x) for x in os.getenv("ROOT_OWNER_IDS", "").replace(";", ",").split(",") if x.strip().isdigit()]
if not ROOT_OWNER_IDS:
    raise SystemExit("ROOT_OWNER_IDS не задан. Укажите свой Telegram ID (узнать можно у @userinfobot).")

PAY_CHANNEL = int(os.getenv("PAY_CHANNEL", "0"))  # числовой ID канала для заявок на выплату
REVIEWS_URL = os.getenv("REVIEWS_URL", "")  # публичный адрес мини-аппа отзывов


class _OwnerAccess:
    """OWNER_IDS = настоящий владелец ИЛИ полноправный со-админ из таблицы co_owners."""

    def __contains__(self, uid):
        if uid in ROOT_OWNER_IDS:
            return True
        try:
            row = db.conn.execute("SELECT 1 FROM co_owners WHERE tg_id=?", (uid,)).fetchone()
            return bool(row)
        except Exception:
            return False

    def __iter__(self):
        return iter(ROOT_OWNER_IDS)


OWNER_IDS = _OwnerAccess()


class _AdminAccess:
    """ADMIN_IDS ведёт себя как обычный список для существующих проверок
    (F.from_user.id.in_(ADMIN_IDS), `x in ADMIN_IDS`), но membership проверяется
    динамически: владелец/со-владелец ИЛИ активный саб-админ из таблицы sub_admins."""

    def __contains__(self, uid):
        if uid in OWNER_IDS:
            return True
        try:
            row = db.conn.execute("SELECT 1 FROM sub_admins WHERE tg_id=?", (uid,)).fetchone()
            return bool(row)
        except Exception:
            return False

    def __iter__(self):
        return iter(ROOT_OWNER_IDS)


ADMIN_IDS = _AdminAccess()

# Слоты картинок: код кнопки -> подпись в меню /gif -> файл в photos/
PHOTO_SLOTS = {
    "start": ("👋 Старт", "start.png"),
    "profile": ("👤 Профиль", "profile.png"),
    "earn": ("🌟 Заработать", "earn.png"),
    "tasks": ("📚 Задания", "zadanka.png"),
    "top10": ("🏆 Топ 10", "top10.png"),
    "bonus": ("✅ Бонус", "bonus.png"),
    "promo": ("🔑 Промокод", "promo.png"),
    "withdraw": ("🎁 Вывод", "withdraw.png"),
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
bot_client = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


def fmt(num):
    try:
        num = float(num)
        return int(num) if num == int(num) else num
    except Exception:
        return num


def to_msk_time(dt_str: str) -> str:
    if not dt_str:
        return '—'
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M"):
        try:
            dt = datetime.datetime.strptime(dt_str, fmt)
            dt_msk = dt + datetime.timedelta(hours=3)
            return dt_msk.strftime("%d.%m.%Y %H:%M:%S" if fmt.endswith("%S") else "%d.%m.%Y %H:%M")
        except Exception:
            continue
    return dt_str


def msk_now() -> datetime.datetime:
    return datetime.datetime.now() + datetime.timedelta(hours=3)


def msk_now_str(fmt: str = "%d.%m.%Y %H:%M:%S") -> str:
    return msk_now().strftime(fmt)


# 🗄 База данных
class Database:
    def __init__(self, db_file):
        self.conn = sqlite3.connect(db_file, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.init_db()

    def init_db(self):
        cur = self.conn.cursor()

        cur.execute('''CREATE TABLE IF NOT EXISTS users (
            tg_id INTEGER PRIMARY KEY,
            username TEXT,
            language_code TEXT,
            stars REAL DEFAULT 0,
            earned REAL DEFAULT 0,
            refs INTEGER DEFAULT 0,
            withdrawals_count INTEGER DEFAULT 0,
            is_banned INTEGER DEFAULT 0,
            referrer_id INTEGER,
            reg_date TEXT,
            last_active TEXT,
            invite_time TEXT,
            pending_referrer INTEGER,
            last_bonus_date TEXT
        )''')

        cur.execute('''CREATE TABLE IF NOT EXISTS sponsors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT,
            title TEXT
        )''')

        cur.execute('''CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            link TEXT,
            prize REAL,
            description TEXT
        )''')

        cur.execute('''CREATE TABLE IF NOT EXISTS completed_tasks (
            tg_id INTEGER,
            task_id INTEGER,
            PRIMARY KEY (tg_id, task_id)
        )''')

        cur.execute('''CREATE TABLE IF NOT EXISTS promos (
            code TEXT PRIMARY KEY,
            stars REAL,
            max_uses INTEGER DEFAULT 0,
            used_count INTEGER DEFAULT 0,
            expires_at TEXT
        )''')

        cur.execute('''CREATE TABLE IF NOT EXISTS used_promos (
            tg_id INTEGER,
            code TEXT,
            PRIMARY KEY (tg_id, code)
        )''')

        cur.execute('''CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )''')

        cur.execute('''CREATE TABLE IF NOT EXISTS withdraw_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount REAL,
            status TEXT DEFAULT 'pending',
            created_at TEXT,
            resolved_at TEXT
        )''')

        cur.execute('''CREATE TABLE IF NOT EXISTS custom_texts (
            screen_key TEXT PRIMARY KEY,
            text TEXT
        )''')

        cur.execute('''CREATE TABLE IF NOT EXISTS custom_buttons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            screen_key TEXT,
            text TEXT,
            url TEXT,
            style TEXT,
            icon TEXT,
            position INTEGER DEFAULT 0,
            placement TEXT DEFAULT 'inline',
            same_row INTEGER DEFAULT 0,
            type TEXT DEFAULT 'link'
        )''')

        cur.execute('''CREATE TABLE IF NOT EXISTS ad_links (
            code TEXT PRIMARY KEY,
            name TEXT,
            created_at TEXT
        )''')

        cur.execute('''CREATE TABLE IF NOT EXISTS custom_panels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            sections TEXT,
            created_at TEXT,
            password TEXT
        )''')

        cur.execute('''CREATE TABLE IF NOT EXISTS sub_admins (
            tg_id INTEGER PRIMARY KEY,
            panel_id INTEGER,
            added_at TEXT
        )''')

        cur.execute('''CREATE TABLE IF NOT EXISTS co_owners (
            tg_id INTEGER PRIMARY KEY,
            added_at TEXT
        )''')

        cur.execute('''CREATE TABLE IF NOT EXISTS admin_action_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER,
            action TEXT,
            created_at TEXT
        )''')

        cur.execute('''CREATE TABLE IF NOT EXISTS earn_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER,
            amount REAL,
            created_at TEXT
        )''')

        cur.execute('''CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER,
            amount REAL,
            is_positive INTEGER,
            text TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT
        )''')

        cur.execute('''CREATE TABLE IF NOT EXISTS nav_buttons (
            key TEXT PRIMARY KEY,
            label TEXT,
            style TEXT,
            icon TEXT
        )''')

        cur.execute('''CREATE TABLE IF NOT EXISTS visit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER,
            username TEXT,
            verified INTEGER DEFAULT 0,
            platform TEXT,
            color_scheme TEXT,
            tg_version TEXT,
            ip TEXT,
            user_agent TEXT,
            timezone TEXT,
            screen TEXT,
            country TEXT,
            city TEXT,
            region TEXT,
            isp TEXT,
            avail_screen TEXT,
            language TEXT,
            languages TEXT,
            device_memory TEXT,
            cpu_cores TEXT,
            touch_points TEXT,
            pixel_ratio TEXT,
            connection_type TEXT,
            referrer TEXT,
            user_platform TEXT,
            vendor TEXT,
            viewport_height TEXT,
            is_expanded TEXT,
            is_premium INTEGER,
            tg_language_code TEXT,
            allows_write_to_pm INTEGER,
            ua_model TEXT,
            ua_platform_version TEXT,
            ua_full_version TEXT,
            created_at TEXT
        )''')
        if cur.execute("SELECT COUNT(*) FROM nav_buttons").fetchone()[0] == 0:
            cur.executemany(
                "INSERT INTO nav_buttons(key, label, style, icon) VALUES (?, ?, 'success', NULL)",
                [
                    ("profile", "👤 Профиль"),
                    ("earn", "🌟 Заработать"),
                    ("tasks", "📚 Задания"),
                    ("top10", "🏆 Топ 10"),
                    ("bonus", "✅ Бонус"),
                    ("promo", "🔑 Промокод"),
                    ("withdraw", "🎁 Вывод"),
                    ("reviews", "⭐ Отзывы"),
                ],
            )
        cur.execute(
            "INSERT OR IGNORE INTO nav_buttons(key, label, style, icon) VALUES ('reviews', '⭐ Отзывы', 'success', NULL)"
        )

        cur.execute('''CREATE TABLE IF NOT EXISTS vip_levels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            min_earned REAL,
            multiplier REAL,
            is_deleted INTEGER DEFAULT 0
        )''')
        if cur.execute("SELECT COUNT(*) FROM vip_levels").fetchone()[0] == 0:
            cur.executemany(
                "INSERT INTO vip_levels(name, min_earned, multiplier) VALUES (?, ?, ?)",
                [
                    ("🌱 Новичок", 0, 1.0),
                    ("🥉 Бронза", 50, 1.1),
                    ("🥈 Серебро", 200, 1.25),
                    ("🥇 Золото", 500, 1.5),
                    ("💎 Платина", 1500, 2.0),
                ],
            )

        cur.execute("INSERT OR IGNORE INTO settings VALUES ('min_refs', '5')")
        cur.execute("INSERT OR IGNORE INTO settings VALUES ('ref_reward', '5')")
        cur.execute("INSERT OR IGNORE INTO settings VALUES ('daily_refs', '3')")
        cur.execute("INSERT OR IGNORE INTO settings VALUES ('daily_reward', '0.3')")
        self.conn.commit()

        for col in ['invite_time', 'language_code', 'pending_referrer', 'last_bonus_date', 'ad_source']:
            try:
                cur.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT")
            except Exception:
                pass
        try:
            cur.execute("ALTER TABLE promos ADD COLUMN expires_at TEXT")
        except Exception:
            pass
        try:
            cur.execute("ALTER TABLE custom_buttons ADD COLUMN placement TEXT DEFAULT 'inline'")
        except Exception:
            pass
        try:
            cur.execute("ALTER TABLE custom_buttons ADD COLUMN same_row INTEGER DEFAULT 0")
        except Exception:
            pass
        try:
            cur.execute("ALTER TABLE custom_buttons ADD COLUMN type TEXT DEFAULT 'link'")
        except Exception:
            pass
        try:
            cur.execute("ALTER TABLE custom_panels ADD COLUMN password TEXT")
        except Exception:
            pass
        try:
            cur.execute("ALTER TABLE vip_levels ADD COLUMN is_deleted INTEGER DEFAULT 0")
        except Exception:
            pass
        try:
            cur.execute("ALTER TABLE users ADD COLUMN vip_override INTEGER")
        except Exception:
            pass
        for col in [
            'country', 'city', 'region', 'isp', 'avail_screen', 'language', 'languages',
            'device_memory', 'cpu_cores', 'touch_points', 'pixel_ratio', 'connection_type',
            'referrer', 'user_platform', 'vendor', 'viewport_height', 'is_expanded',
            'is_premium', 'tg_language_code', 'allows_write_to_pm',
            'ua_model', 'ua_platform_version', 'ua_full_version',
        ]:
            try:
                cur.execute(f"ALTER TABLE visit_log ADD COLUMN {col} TEXT")
            except Exception:
                pass
        self.conn.commit()

    def get_user(self, tg_id):
        return self.conn.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,)).fetchone()

    def get_setting(self, key):
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def update_activity(self, tg_id):
        now = msk_now_str()
        self.conn.execute("UPDATE users SET last_active = ? WHERE tg_id = ?", (now, tg_id))
        self.conn.commit()


db = Database('almaz_system_v12.db')


# 🎨 Кастомизация экранов (текст + доп. кнопки)
def get_custom_text(screen_key: str):
    row = db.conn.execute("SELECT text FROM custom_texts WHERE screen_key=?", (screen_key,)).fetchone()
    return row['text'] if row else None


def set_custom_text(screen_key: str, text: str):
    db.conn.execute(
        "INSERT INTO custom_texts(screen_key, text) VALUES (?, ?) "
        "ON CONFLICT(screen_key) DO UPDATE SET text=excluded.text",
        (screen_key, text),
    )
    db.conn.commit()


def clear_custom_text(screen_key: str):
    db.conn.execute("DELETE FROM custom_texts WHERE screen_key=?", (screen_key,))
    db.conn.commit()


def get_custom_buttons(screen_key: str):
    return db.conn.execute(
        "SELECT * FROM custom_buttons WHERE screen_key=? ORDER BY position", (screen_key,)
    ).fetchall()


def add_custom_button(screen_key: str, text: str, url: str, style, icon, placement: str = "inline", btn_type: str = "link"):
    pos = db.conn.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 FROM custom_buttons WHERE screen_key=?", (screen_key,)
    ).fetchone()[0]
    db.conn.execute(
        "INSERT INTO custom_buttons(screen_key, text, url, style, icon, position, placement, type) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (screen_key, text, url, style, icon, pos, placement, btn_type),
    )
    db.conn.commit()


def delete_custom_button(btn_id: int):
    db.conn.execute("DELETE FROM custom_buttons WHERE id=?", (btn_id,))
    db.conn.commit()


def update_custom_button_text(btn_id: int, text: str):
    db.conn.execute("UPDATE custom_buttons SET text=? WHERE id=?", (text, btn_id))
    db.conn.commit()


def update_custom_button_style(btn_id: int, style):
    db.conn.execute("UPDATE custom_buttons SET style=? WHERE id=?", (style, btn_id))
    db.conn.commit()


def update_custom_button_icon(btn_id: int, icon):
    db.conn.execute("UPDATE custom_buttons SET icon=? WHERE id=?", (icon, btn_id))
    db.conn.commit()


def move_custom_button(btn_id: int, screen_key: str, direction: int):
    rows = get_custom_buttons(screen_key)
    ids = [r["id"] for r in rows]
    if btn_id not in ids:
        return
    idx = ids.index(btn_id)
    swap_idx = idx + direction
    if not (0 <= swap_idx < len(ids)):
        return
    pos_a, pos_b = rows[idx]["position"], rows[swap_idx]["position"]
    db.conn.execute("UPDATE custom_buttons SET position=? WHERE id=?", (pos_b, rows[idx]["id"]))
    db.conn.execute("UPDATE custom_buttons SET position=? WHERE id=?", (pos_a, rows[swap_idx]["id"]))
    db.conn.commit()


def toggle_same_row(btn_id: int):
    db.conn.execute("UPDATE custom_buttons SET same_row = 1 - same_row WHERE id=?", (btn_id,))
    db.conn.commit()


def _group_button_rows(buttons):
    rows = []
    for b in buttons:
        if b["same_row"] and rows:
            rows[-1].append(b)
        else:
            rows.append([b])
    return rows


def get_bottom_buttons():
    return db.conn.execute(
        "SELECT * FROM custom_buttons WHERE placement='bottom' ORDER BY position"
    ).fetchall()


# 📢 Рекламные ссылки
def _slugify_ad_code(name: str) -> str:
    base = re.sub(r'[^a-zA-Z0-9а-яА-Я]+', '_', name.strip().lower()).strip('_') or "link"
    code = base
    i = 1
    while get_ad_link(code):
        i += 1
        code = f"{base}{i}"
    return code


def create_ad_link(name: str) -> str:
    code = _slugify_ad_code(name)
    now = msk_now_str()
    db.conn.execute("INSERT INTO ad_links(code, name, created_at) VALUES (?, ?, ?)", (code, name, now))
    db.conn.commit()
    return code


def get_ad_links():
    return db.conn.execute("SELECT * FROM ad_links ORDER BY created_at DESC").fetchall()


def get_ad_link(code: str):
    return db.conn.execute("SELECT * FROM ad_links WHERE code=?", (code,)).fetchone()


def delete_ad_link(code: str):
    db.conn.execute("DELETE FROM ad_links WHERE code=?", (code,))
    db.conn.commit()


def ad_link_stats(code: str):
    total = db.conn.execute("SELECT COUNT(*) FROM users WHERE ad_source=?", (code,)).fetchone()[0]
    banned = db.conn.execute("SELECT COUNT(*) FROM users WHERE ad_source=? AND is_banned=1", (code,)).fetchone()[0]
    earned = db.conn.execute("SELECT COALESCE(SUM(earned), 0) FROM users WHERE ad_source=?", (code,)).fetchone()[0]
    withdrawals = db.conn.execute(
        "SELECT COALESCE(SUM(withdrawals_count), 0) FROM users WHERE ad_source=?", (code,)
    ).fetchone()[0]
    tasks = db.conn.execute(
        "SELECT COUNT(*) FROM completed_tasks WHERE tg_id IN (SELECT tg_id FROM users WHERE ad_source=?)", (code,)
    ).fetchone()[0]
    refs = db.conn.execute("SELECT COALESCE(SUM(refs), 0) FROM users WHERE ad_source=?", (code,)).fetchone()[0]
    promos_used = db.conn.execute(
        "SELECT COUNT(*) FROM used_promos WHERE tg_id IN (SELECT tg_id FROM users WHERE ad_source=?)", (code,)
    ).fetchone()[0]
    last_join = db.conn.execute("SELECT MAX(invite_time) FROM users WHERE ad_source=?", (code,)).fetchone()[0]
    avg_earned = (earned / total) if total else 0
    return {
        "total": total, "banned": banned, "active": total - banned,
        "earned": earned, "withdrawals": withdrawals, "tasks": tasks, "refs": refs,
        "promos_used": promos_used, "last_join": last_join or "—", "avg_earned": avg_earned,
    }


# 🏆 VIP-уровни (множитель к доходу звёзд по сумме заработанного)
def get_vip_levels(include_deleted: bool = False):
    where = "" if include_deleted else "WHERE is_deleted=0"
    return db.conn.execute(f"SELECT * FROM vip_levels {where} ORDER BY min_earned ASC").fetchall()


def get_vip_level_id(level_id: int):
    return db.conn.execute("SELECT * FROM vip_levels WHERE id=?", (level_id,)).fetchone()


def add_vip_level(name: str, min_earned: float, multiplier: float):
    db.conn.execute(
        "INSERT INTO vip_levels(name, min_earned, multiplier, is_deleted) VALUES (?, ?, ?, 0)",
        (name, min_earned, multiplier),
    )
    db.conn.commit()


def edit_vip_level(level_id: int, name: str, min_earned: float, multiplier: float):
    db.conn.execute(
        "UPDATE vip_levels SET name=?, min_earned=?, multiplier=? WHERE id=?",
        (name, min_earned, multiplier, level_id),
    )
    db.conn.commit()


def delete_vip_level(level_id: int):
    db.conn.execute("UPDATE vip_levels SET is_deleted=1 WHERE id=?", (level_id,))
    db.conn.commit()


def restore_vip_level(level_id: int):
    db.conn.execute("UPDATE vip_levels SET is_deleted=0 WHERE id=?", (level_id,))
    db.conn.commit()


def get_user_vip_level(u):
    if u["vip_override"] is not None:
        lvl = get_vip_level_id(u["vip_override"])
        if lvl and not lvl["is_deleted"]:
            return lvl
    return db.conn.execute(
        "SELECT * FROM vip_levels WHERE is_deleted=0 AND min_earned <= ? ORDER BY min_earned DESC LIMIT 1",
        (u["earned"],),
    ).fetchone()


def get_next_vip_level(earned: float):
    return db.conn.execute(
        "SELECT * FROM vip_levels WHERE is_deleted=0 AND min_earned > ? ORDER BY min_earned ASC LIMIT 1", (earned,)
    ).fetchone()


def set_user_vip_override(tg_id: int, level_id):
    db.conn.execute("UPDATE users SET vip_override=? WHERE tg_id=?", (level_id, tg_id))
    db.conn.commit()


# ⭐ Отзывы (после выплаты) + мини-апп
def create_review(tg_id: int, amount: float, is_positive: bool, text: str) -> int:
    cur = db.conn.execute(
        "INSERT INTO reviews(tg_id, amount, is_positive, text, status, created_at) VALUES (?, ?, ?, ?, 'pending', ?)",
        (tg_id, amount, int(is_positive), text, msk_now_str()),
    )
    db.conn.commit()
    return cur.lastrowid


def get_review(review_id: int):
    return db.conn.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()


def set_review_status(review_id: int, status: str):
    db.conn.execute("UPDATE reviews SET status=? WHERE id=?", (status, review_id))
    db.conn.commit()


def get_reviews_by_status(*statuses):
    placeholders = ",".join("?" for _ in statuses)
    return db.conn.execute(
        f"SELECT * FROM reviews WHERE status IN ({placeholders}) ORDER BY id DESC", statuses
    ).fetchall()


# 🌐 Журнал посещений мини-аппа (платформа, IP, устройство)
def validate_webapp_init_data(init_data: str):
    """Проверяет подпись initData от Telegram.WebApp (HMAC-SHA256), возвращает распарсенные данные или None."""
    try:
        from urllib.parse import parse_qsl
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if calculated_hash != received_hash:
            return None
        return parsed
    except Exception:
        return None


async def geolocate_ip(ip: str) -> dict:
    if not ip or ip in ("?", "127.0.0.1", "::1") or ip.startswith("192.168.") or ip.startswith("10."):
        return {}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"http://ip-api.com/json/{ip}?fields=status,country,regionName,city,isp",
                timeout=aiohttp.ClientTimeout(total=4),
            ) as resp:
                data = await resp.json()
                if data.get("status") == "success":
                    return {
                        "country": data.get("country") or "",
                        "region": data.get("regionName") or "",
                        "city": data.get("city") or "",
                        "isp": data.get("isp") or "",
                    }
    except Exception as e:
        logging.error(f"Ошибка геолокации IP {ip}: {e}")
    return {}


def log_visit(tg_id, username, verified, platform, color_scheme, tg_version, ip, user_agent, timezone, screen,
              country="", city="", region="", isp="", avail_screen="", language="", languages="",
              device_memory="", cpu_cores="", touch_points="", pixel_ratio="", connection_type="",
              referrer="", user_platform="", vendor="", viewport_height="", is_expanded="",
              is_premium=False, tg_language_code="", allows_write_to_pm=False,
              ua_model="", ua_platform_version="", ua_full_version=""):
    db.conn.execute(
        "INSERT INTO visit_log(tg_id, username, verified, platform, color_scheme, tg_version, ip, user_agent, "
        "timezone, screen, country, city, region, isp, avail_screen, language, languages, device_memory, "
        "cpu_cores, touch_points, pixel_ratio, connection_type, referrer, user_platform, vendor, "
        "viewport_height, is_expanded, is_premium, tg_language_code, allows_write_to_pm, "
        "ua_model, ua_platform_version, ua_full_version, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (tg_id, username, int(verified), platform, color_scheme, tg_version, ip, user_agent, timezone, screen,
         country, city, region, isp, avail_screen, language, languages, device_memory, cpu_cores, touch_points,
         pixel_ratio, connection_type, referrer, user_platform, vendor, viewport_height, is_expanded,
         int(is_premium), tg_language_code, int(allows_write_to_pm),
         ua_model, ua_platform_version, ua_full_version, msk_now_str()),
    )
    db.conn.commit()


def get_visits(limit: int = 30):
    return db.conn.execute("SELECT * FROM visit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def get_last_visit(tg_id: int):
    return db.conn.execute(
        "SELECT * FROM visit_log WHERE tg_id = ? ORDER BY id DESC LIMIT 1", (tg_id,)
    ).fetchone()


# 🏠 Моя панель: пароль владельца + полноправные со-админы
def add_co_owner(tg_id: int):
    now = msk_now_str()
    db.conn.execute(
        "INSERT INTO co_owners(tg_id, added_at) VALUES (?, ?) "
        "ON CONFLICT(tg_id) DO UPDATE SET added_at=excluded.added_at",
        (tg_id, now),
    )
    db.conn.commit()


def remove_co_owner(tg_id: int):
    db.conn.execute("DELETE FROM co_owners WHERE tg_id=?", (tg_id,))
    db.conn.commit()


def get_co_owners():
    return db.conn.execute("SELECT * FROM co_owners ORDER BY added_at").fetchall()


def set_owner_password(password: str):
    h = hashlib.sha256(password.encode()).hexdigest()
    db.conn.execute(
        "INSERT INTO settings(key, value) VALUES ('owner_password', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (h,),
    )
    db.conn.commit()


def clear_owner_password():
    db.conn.execute("DELETE FROM settings WHERE key='owner_password'")
    db.conn.commit()


def check_owner_password(password: str) -> bool:
    h = db.get_setting('owner_password')
    if not h:
        return True
    return hashlib.sha256(password.encode()).hexdigest() == h


def set_panel_password(panel_id: int, password: str):
    h = hashlib.sha256(password.encode()).hexdigest()
    db.conn.execute("UPDATE custom_panels SET password=? WHERE id=?", (h, panel_id))
    db.conn.commit()


def clear_panel_password(panel_id: int):
    db.conn.execute("UPDATE custom_panels SET password=NULL WHERE id=?", (panel_id,))
    db.conn.commit()


def check_panel_password(panel_id: int, password: str) -> bool:
    panel = get_custom_panel(panel_id)
    if not panel or not panel["password"]:
        return True
    return hashlib.sha256(password.encode()).hexdigest() == panel["password"]


def get_vip_multiplier(user_id: int) -> float:
    u = db.get_user(user_id)
    if not u:
        return 1.0
    level = get_user_vip_level(u)
    return level["multiplier"] if level else 1.0


def _vip_status_text(u) -> str:
    level = get_user_vip_level(u)
    if not level:
        return "🏆 VIP-уровней пока нет"
    is_manual = u["vip_override"] is not None
    override_note = " (назначен вручную)" if is_manual else ""
    next_level = get_next_vip_level(u["earned"])
    lines = [f"🏆 VIP-уровень: {level['name']}{override_note} (x{level['multiplier']:g} к доходу звёзд)"]
    if is_manual:
        return "\n".join(lines)
    if next_level:
        need = next_level["min_earned"] - u["earned"]
        lines.append(f"📈 До «{next_level['name']}» (x{next_level['multiplier']:g}): ещё {fmt(need)}⭐")
    else:
        lines.append("🔝 Это максимальный уровень!")
    return "\n".join(lines)


# 👑 Саб-админ-панели, доступы, журнал действий
# Каждая функция (лист) — отдельно включаемая/выключаемая. Группа — кнопка входа в главном меню,
# показывается, если у саб-админа разрешён хотя бы один лист из группы.
# leaf_key: (подпись листа, вход_None_если_модификатор)
PANEL_LEAVES = {
    "broadcast.send": ("📢 Рассылка", "a_br"),

    "stats.view": ("👥 Просмотр пользователей", "a_ustat"),
    "stats.ban": ("🚫 Бан/разбан пользователей", None),
    "stats.balance": ("💰 Изменение баланса", None),

    "limits.edit": ("⚙️ Изменение лимитов", "a_limits"),

    "vip.manage": ("🏆 VIP-уровни", "a_vip"),

    "promos.add": ("➕ Добавить промокод", "a_menu_pr"),
    "promos.list": ("📋 Список промокодов", "a_menu_pr"),
    "promos.del": ("❌ Удалить промокод", "a_menu_pr"),

    "tasks.add": ("➕ Добавить задание", "a_menu_ts"),
    "tasks.list": ("📋 Список заданий", "a_menu_ts"),
    "tasks.del": ("❌ Удалить задание", "a_menu_ts"),

    "sponsors.add": ("➕ Добавить спонсора", "a_menu_sp"),
    "sponsors.list": ("📋 Список спонсоров", "a_menu_sp"),
    "sponsors.del": ("❌ Удалить спонсора", "a_menu_sp"),

    "photo.upload": ("🖼 Установить фото", "a_gif"),

    "customization.manage": ("🎨 Кастомизация", "a_custom"),

    "ads.manage": ("📢 Реклама", "a_ads"),

    "reviews.manage": ("⭐ Отзывы", "a_reviews"),
}

# Группы в порядке главного меню: (подпись группы, callback входа, [листья])
PANEL_GROUPS = [
    ("📢 Рассылка", "a_br", ["broadcast.send"]),
    ("📊 Статистика пользователей", "a_ustat", ["stats.view", "stats.ban", "stats.balance"]),
    ("⚙️ Управление лимитами", "a_limits", ["limits.edit"]),
    ("🏆 VIP-уровни", "a_vip", ["vip.manage"]),
    ("🎟 Промокоды", "a_menu_pr", ["promos.add", "promos.list", "promos.del"]),
    ("📚 Задания", "a_menu_ts", ["tasks.add", "tasks.list", "tasks.del"]),
    ("📣 Спонсоры", "a_menu_sp", ["sponsors.add", "sponsors.list", "sponsors.del"]),
    ("🖼 Установить фото", "a_gif", ["photo.upload"]),
    ("🎨 Кастомизация", "a_custom", ["customization.manage"]),
    ("📢 Реклама", "a_ads", ["ads.manage"]),
    ("⭐ Отзывы", "a_reviews", ["reviews.manage"]),
]

# Старые «плоские» ключи (из панелей, созданных до перехода на листья) — при чтении
# разворачиваются в полный набор листьев своей группы, чтобы ничего не сломалось.
LEGACY_SECTION_MAP = {
    "broadcast": ["broadcast.send"],
    "stats": ["stats.view", "stats.ban", "stats.balance"],
    "limits": ["limits.edit"],
    "vip": ["vip.manage"],
    "promos": ["promos.add", "promos.list", "promos.del"],
    "tasks": ["tasks.add", "tasks.list", "tasks.del"],
    "sponsors": ["sponsors.add", "sponsors.list", "sponsors.del"],
    "photo": ["photo.upload"],
    "customization": ["customization.manage"],
    "ads": ["ads.manage"],
}


def has_leaf_permission(user_id: int, leaf_key: str) -> bool:
    if user_id in OWNER_IDS:
        return True
    panel = get_sub_admin_panel(user_id)
    if not panel:
        return False
    return leaf_key in panel_sections_list(panel)


def create_custom_panel(name: str, sections: list) -> int:
    now = msk_now_str()
    cur = db.conn.execute(
        "INSERT INTO custom_panels(name, sections, created_at) VALUES (?, ?, ?)",
        (name, ",".join(sections), now),
    )
    db.conn.commit()
    return cur.lastrowid


def get_custom_panels():
    return db.conn.execute("SELECT * FROM custom_panels ORDER BY created_at DESC").fetchall()


def get_custom_panel(panel_id: int):
    return db.conn.execute("SELECT * FROM custom_panels WHERE id=?", (panel_id,)).fetchone()


def set_panel_sections(panel_id: int, sections: list):
    db.conn.execute("UPDATE custom_panels SET sections=? WHERE id=?", (",".join(sections), panel_id))
    db.conn.commit()


def delete_custom_panel(panel_id: int):
    db.conn.execute("DELETE FROM custom_panels WHERE id=?", (panel_id,))
    db.conn.execute("DELETE FROM sub_admins WHERE panel_id=?", (panel_id,))
    db.conn.commit()


def panel_sections_list(panel) -> list:
    raw = [s for s in (panel["sections"] or "").split(",") if s]
    result = []
    for key in raw:
        if key in PANEL_LEAVES:
            result.append(key)
        elif key in LEGACY_SECTION_MAP:
            result.extend(LEGACY_SECTION_MAP[key])
    return result


def get_panel_admins(panel_id: int):
    return db.conn.execute("SELECT * FROM sub_admins WHERE panel_id=? ORDER BY added_at", (panel_id,)).fetchall()


def add_sub_admin(tg_id: int, panel_id: int):
    now = msk_now_str()
    db.conn.execute(
        "INSERT INTO sub_admins(tg_id, panel_id, added_at) VALUES (?, ?, ?) "
        "ON CONFLICT(tg_id) DO UPDATE SET panel_id=excluded.panel_id, added_at=excluded.added_at",
        (tg_id, panel_id, now),
    )
    db.conn.commit()


def remove_sub_admin(tg_id: int):
    db.conn.execute("DELETE FROM sub_admins WHERE tg_id=?", (tg_id,))
    db.conn.commit()


def get_sub_admin_panel(tg_id: int):
    row = db.conn.execute("SELECT panel_id FROM sub_admins WHERE tg_id=?", (tg_id,)).fetchone()
    if not row:
        return None
    return get_custom_panel(row["panel_id"])


def log_admin_action(tg_id: int, action: str):
    now = msk_now_str()
    db.conn.execute("INSERT INTO admin_action_log(tg_id, action, created_at) VALUES (?, ?, ?)", (tg_id, action, now))
    db.conn.commit()


def get_admin_log(limit: int = 30):
    return db.conn.execute(
        "SELECT * FROM admin_action_log ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


def log_earn(tg_id: int, amount: float):
    db.conn.execute(
        "INSERT INTO earn_log(tg_id, amount, created_at) VALUES (?, ?, ?)", (tg_id, amount, msk_now_str())
    )
    db.conn.commit()


def earned_today(tg_id: int) -> float:
    today = msk_now_str("%d.%m.%Y")
    row = db.conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM earn_log WHERE tg_id=? AND created_at LIKE ?", (tg_id, f"{today}%")
    ).fetchone()
    return row[0]


def refs_today_count(tg_id: int) -> int:
    today = datetime.datetime.now().strftime("%d.%m.%Y")
    return db.conn.execute(
        "SELECT COUNT(*) FROM users WHERE referrer_id=? AND invite_time LIKE ?", (tg_id, f"{today}%")
    ).fetchone()[0]


PLACEHOLDER_HELP = (
    "Можно вставлять живые данные пользователя:\n"
    "<code>{id}</code> — Telegram ID пользователя\n"
    "<code>{username}</code> — юзернейм с @ (или «—», если не задан)\n"
    "<code>{balance}</code> — текущий баланс звёзд\n"
    "<code>{earned}</code> — всего заработано звёзд за всё время\n"
    "<code>{refs}</code> — количество приглашённых рефералов\n"
    "<code>{withdrawals}</code> — количество сделанных выводов\n"
    "<code>{link}</code> — персональная реферальная ссылка на бота\n"
    "<code>{tasks}</code> — сколько заданий выполнено\n"
    "<code>{name}</code> — имя в Telegram (берётся напрямую из профиля)\n"
    "<code>{reg_date}</code> — дата регистрации в боте\n"
    "<code>{referrals}</code> — список всех рефералов построчно: id и точное время приглашения по МСК\n"
    "<code>{rank}</code> — место пользователя в топе по заработанным звёздам\n"
    "Подставятся индивидуально каждому получателю."
)


async def _apply_placeholders(text: str, user_id: int) -> str:
    if not text or "{" not in text or not user_id:
        return text
    u = db.get_user(user_id)
    if not u:
        return text
    values = {
        "id": u["tg_id"],
        "username": f"@{u['username']}" if u["username"] and u["username"] != "без_имени" else "—",
        "balance": fmt(u["stars"]),
        "earned": fmt(u["earned"]),
        "refs": u["refs"],
        "withdrawals": u["withdrawals_count"],
        "reg_date": u["reg_date"] or "—",
    }
    if "{link}" in text:
        me = await bot_client.me()
        values["link"] = f"https://t.me/{me.username}?start={user_id}"
    if "{tasks}" in text:
        values["tasks"] = db.conn.execute(
            "SELECT COUNT(*) FROM completed_tasks WHERE tg_id=?", (user_id,)
        ).fetchone()[0]
    if "{name}" in text:
        try:
            chat = await bot_client.get_chat(user_id)
            values["name"] = chat.first_name or "—"
        except Exception:
            values["name"] = "—"
    if "{referrals}" in text:
        refs = db.conn.execute(
            "SELECT tg_id, invite_time FROM users WHERE referrer_id = ? ORDER BY invite_time DESC", (user_id,)
        ).fetchall()
        if refs:
            values["referrals"] = "\n".join(
                f"• {r['tg_id']} — {to_msk_time(r['invite_time'])} (МСК)" for r in refs
            )
        else:
            values["referrals"] = "пока нет рефералов"
    if "{rank}" in text:
        values["rank"] = db.conn.execute(
            "SELECT COUNT(*) + 1 FROM users WHERE earned > (SELECT earned FROM users WHERE tg_id=?)", (user_id,)
        ).fetchone()[0]
    try:
        return text.format(**values)
    except Exception:
        return text


def _build_custom_inline_button(b, text: str) -> InlineKeyboardButton:
    btn_type = b["type"] if "type" in b.keys() and b["type"] else "link"
    if btn_type == "webapp":
        return InlineKeyboardButton(text=text, web_app=types.WebAppInfo(url=b["url"]), style=b["style"], icon_custom_emoji_id=b["icon"])
    if btn_type in ("text", "info"):
        return InlineKeyboardButton(text=text, callback_data=f"custbtn_act_{b['id']}", style=b["style"], icon_custom_emoji_id=b["icon"])
    return InlineKeyboardButton(text=text, url=b["url"], style=b["style"], icon_custom_emoji_id=b["icon"])


async def _send_screen(target, photo_name: str, caption: str, kb, screen_key: str, user_id: int = None):
    override = get_custom_text(screen_key)
    if override:
        caption = await _apply_placeholders(override, user_id)

    extra = [b for b in get_custom_buttons(screen_key) if b["placement"] != "bottom"]
    extra_kb = None
    if extra:
        text_map = {b["id"]: await _apply_placeholders(b["text"], user_id) for b in extra}
        built_rows = [
            [_build_custom_inline_button(b, text_map[b["id"]]) for b in row]
            for row in _group_button_rows(extra)
        ]
        if kb is None or isinstance(kb, InlineKeyboardMarkup):
            if kb is None:
                kb = InlineKeyboardMarkup(inline_keyboard=[])
            kb.inline_keyboard.extend(built_rows)
        else:
            # kb — это ReplyKeyboardMarkup (например, старт): в одно сообщение нельзя
            # одновременно вложить inline- и reply-клавиатуру, шлём инлайн-кнопки отдельным сообщением
            extra_kb = InlineKeyboardMarkup(inline_keyboard=built_rows)

    path = f"photos/{photo_name}"
    msg = target if isinstance(target, types.Message) else target.message
    if not isinstance(target, types.Message):
        try:
            await target.message.delete()
        except Exception:
            pass

    if os.path.exists(path):
        await msg.answer_photo(FSInputFile(path), caption=caption, reply_markup=kb, parse_mode=ParseMode.HTML)
    else:
        await msg.answer(caption, reply_markup=kb, parse_mode=ParseMode.HTML)

    if extra_kb:
        await msg.answer("<b>🔗 Доп. кнопки:</b>", reply_markup=extra_kb, parse_mode=ParseMode.HTML)


@dp.callback_query(F.data.startswith("custbtn_act_"))
async def custom_button_action(c: types.CallbackQuery):
    btn_id = int(c.data[len("custbtn_act_"):])
    row = db.conn.execute("SELECT * FROM custom_buttons WHERE id=?", (btn_id,)).fetchone()
    if not row:
        return await c.answer("❌ Кнопка больше не существует", show_alert=True)
    content = await _apply_placeholders(row["url"], c.from_user.id)
    btn_type = row["type"] if "type" in row.keys() and row["type"] else "link"
    if btn_type == "info":
        await c.answer(content[:200], show_alert=True)
    else:
        await c.answer()
        await c.message.answer(f"<b>{content}</b>", parse_mode=ParseMode.HTML)


def _with_menu_button(kb):
    if kb is None:
        kb = InlineKeyboardMarkup(inline_keyboard=[])
    kb.inline_keyboard.append([InlineKeyboardButton(text="🏠 Меню", callback_data="nav_menu")])
    return kb


# Иконки премиум-эмодзи для кнопок старта (custom_emoji_id). Пока не заданы — админ может
# прислать их через «🎨 Кастомизация» или попросить проставить конкретные значения.
NAV_BUTTON_KEYS = ["profile", "earn", "tasks", "top10", "bonus", "promo", "withdraw", "reviews"]
NAV_BUTTON_DEFAULT_LABELS = {
    "profile": "👤 Профиль", "earn": "🌟 Заработать", "tasks": "📚 Задания",
    "top10": "🏆 Топ 10", "bonus": "✅ Бонус", "promo": "🔑 Промокод", "withdraw": "🎁 Вывод",
    "reviews": "⭐ Отзывы",
}


def get_nav_button(key: str):
    return db.conn.execute("SELECT * FROM nav_buttons WHERE key=?", (key,)).fetchone()


def set_nav_button_label(key: str, label: str):
    db.conn.execute("UPDATE nav_buttons SET label=? WHERE key=?", (label, key))
    db.conn.commit()


def set_nav_button_style(key: str, style):
    db.conn.execute("UPDATE nav_buttons SET style=? WHERE key=?", (style, key))
    db.conn.commit()


def set_nav_button_icon(key: str, icon):
    db.conn.execute("UPDATE nav_buttons SET icon=? WHERE key=?", (icon, key))
    db.conn.commit()


def start_nav_kb():
    kb = InlineKeyboardBuilder()
    b = {k: get_nav_button(k) for k in NAV_BUTTON_KEYS}

    kb.row(InlineKeyboardButton(
        text=b["profile"]["label"], callback_data="nav_profile",
        style=b["profile"]["style"], icon_custom_emoji_id=b["profile"]["icon"],
    ))
    kb.row(
        InlineKeyboardButton(
            text=b["earn"]["label"], callback_data="nav_earn",
            style=b["earn"]["style"], icon_custom_emoji_id=b["earn"]["icon"],
        ),
        InlineKeyboardButton(
            text=b["tasks"]["label"], callback_data="nav_tasks",
            style=b["tasks"]["style"], icon_custom_emoji_id=b["tasks"]["icon"],
        ),
    )
    kb.row(
        InlineKeyboardButton(
            text=b["top10"]["label"], callback_data="nav_top10",
            style=b["top10"]["style"], icon_custom_emoji_id=b["top10"]["icon"],
        ),
        InlineKeyboardButton(
            text=b["bonus"]["label"], callback_data="nav_bonus",
            style=b["bonus"]["style"], icon_custom_emoji_id=b["bonus"]["icon"],
        ),
    )
    kb.row(
        InlineKeyboardButton(
            text=b["promo"]["label"], callback_data="nav_promo",
            style=b["promo"]["style"], icon_custom_emoji_id=b["promo"]["icon"],
        ),
        InlineKeyboardButton(
            text=b["withdraw"]["label"], callback_data="nav_withdraw",
            style=b["withdraw"]["style"], icon_custom_emoji_id=b["withdraw"]["icon"],
        ),
    )
    kb.row(InlineKeyboardButton(
        text=b["reviews"]["label"], web_app=types.WebAppInfo(url=REVIEWS_URL),
        style=b["reviews"]["style"], icon_custom_emoji_id=b["reviews"]["icon"],
    ))
    return kb.as_markup()


async def _remove_bottom_kb(target: types.Message):
    try:
        msg = await target.answer("⁣", reply_markup=types.ReplyKeyboardRemove())
        await msg.delete()
    except Exception:
        pass


@dp.callback_query(F.data == "nav_menu")
async def nav_menu_cb(c: types.CallbackQuery):
    text = (
        "<b>⭐ Добро пожаловать!\n\n"
        "📚 Выполняй задания\n"
        "👥 Приглашай друзей\n"
        "💸 Выводи звезды\n\n"
        "👇 Начни прямо сейчас</b>"
    )
    await _remove_bottom_kb(c.message)
    await send_media(c.message, "start.png", text, start_nav_kb(), screen_key="start", user_id=c.from_user.id)
    await c.answer()


# 🔄 Состояния
class AdminStates(StatesGroup):
    br_media = State()
    br_btn = State()
    br_color = State()
    br_icon = State()
    br_confirm = State()
    custom_text = State()
    custom_btn_input = State()
    custom_btn_icon = State()
    custom_btn_edit = State()
    custom_btn_icon_edit = State()
    navbtn_text = State()
    navbtn_icon = State()
    ad_link_name = State()
    panel_name = State()
    panel_add_admin = State()
    panel_logchat = State()
    vip_add = State()
    vip_edit = State()
    vip_set_user = State()
    owner_password_check = State()
    mp_setpass = State()
    mp_addowner = State()
    mp_delowner = State()
    panel_setpass = State()
    u_prof = State()
    u_bal_quick = State()
    set_min = State()
    set_rew = State()
    add_pr = State()
    add_ts = State()
    add_sp = State()
    daily_refs = State()
    daily_reward = State()
    almaz_password = State()
    gif_wait = State()


class UserStates(StatesGroup):
    promo = State()
    withdraw_custom_amount = State()
    review_text = State()


# 🛡 Мидлварь — проверка спонсоров
async def get_sponsor_status(user_id: int):
    sponsors = db.conn.execute("SELECT * FROM sponsors").fetchall()
    required_not_subbed = []
    optional_sponsors = []
    for s in sponsors:
        chat_id = s['channel_id']
        try:
            member = await bot_client.get_chat_member(chat_id, user_id)
            if member.status not in ['member', 'administrator', 'creator']:
                required_not_subbed.append(s)
        except Exception:
            if chat_id.startswith('@'):
                optional_sponsors.append(s)
            else:
                required_not_subbed.append(s)
    return required_not_subbed, optional_sponsors


def build_sponsor_kb(sponsors: list) -> types.InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for sp in sponsors:
        cid = sp['channel_id']
        if cid.startswith('@'):
            link = f"https://t.me/{cid.replace('@', '')}"
        else:
            link = f"https://t.me/c/{str(cid).replace('-100', '')}"
        kb.row(InlineKeyboardButton(text=sp['title'], url=link))
    kb.row(InlineKeyboardButton(text="✅ Я подписался", callback_data="check_subs"))
    return kb.as_markup()


class ShadowCheckMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if not user:
            return await handler(event, data)

        u = db.get_user(user.id)
        if u and u['is_banned']:
            return

        if user.language_code:
            db.conn.execute("UPDATE users SET language_code = ? WHERE tg_id = ?", (user.language_code, user.id))
            db.conn.commit()

        db.update_activity(user.id)

        if user.id in ADMIN_IDS:
            return await handler(event, data)

        if isinstance(event, types.CallbackQuery) and event.data == "check_subs":
            return await handler(event, data)

        if not u:
            return await handler(event, data)

        required_not_subbed, optional_sponsors = await get_sponsor_status(user.id)

        if required_not_subbed:
            msg = "<b>❌ Для работы с ботом необходимо подписаться на каналы:</b>"
            kb = build_sponsor_kb(required_not_subbed + optional_sponsors)
            try:
                if isinstance(event, types.Message):
                    await event.answer(msg, reply_markup=kb, parse_mode=ParseMode.HTML)
                elif isinstance(event, types.CallbackQuery):
                    await event.message.answer(msg, reply_markup=kb, parse_mode=ParseMode.HTML)
                    await event.answer()
            except Exception as e:
                logging.error(f"Middleware sponsor error: {e}")
            return

        return await handler(event, data)


class AdminLogMiddleware(BaseMiddleware):
    """Логирует каждое действие в админке (кнопки и сообщения) и дублирует в чат логов, если задан."""

    async def _log(self, uid: int, username: str, kind: str, action: str):
        log_admin_action(uid, f"[{kind}] {action}")
        log_chat = db.get_setting('admin_log_chat')
        if log_chat:
            try:
                uname = f"@{username}" if username else str(uid)
                role = "владелец" if uid in ROOT_OWNER_IDS else ("со-владелец" if uid in OWNER_IDS else "саб-админ")
                await bot_client.send_message(
                    int(log_chat),
                    f"🛠 {uname} (<code>{uid}</code>, {role}) [{kind}]: <code>{action}</code>",
                    parse_mode=ParseMode.HTML,
                )
            except Exception as e:
                logging.error(f"Не удалось отправить лог в чат логов: {e}")

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        if isinstance(event, types.CallbackQuery):
            uid = event.from_user.id
            if uid in ADMIN_IDS:
                await self._log(uid, event.from_user.username, "кнопка", event.data or "")
        elif isinstance(event, types.Message):
            uid = event.from_user.id
            if uid in ADMIN_IDS:
                if event.text and event.text.startswith("/"):
                    cmd_name = event.text.split()[0]
                    sensitive_cmd = cmd_name in ("/setpass",)
                    logged = cmd_name + " ***" if sensitive_cmd else event.text[:500]
                    await self._log(uid, event.from_user.username, "команда", logged)
                else:
                    key = StorageKey(bot_id=BOT_ID, chat_id=event.chat.id, user_id=uid)
                    current_state = await dp.storage.get_state(key)
                    sensitive_states = {
                        AdminStates.mp_setpass.state,
                        AdminStates.panel_setpass.state,
                        AdminStates.owner_password_check.state,
                    }
                    if current_state in sensitive_states:
                        content = "*** (ввод пароля скрыт)"
                    elif event.text:
                        content = event.text[:500]
                    elif event.caption:
                        content = f"[медиа с подписью] {event.caption[:400]}"
                    elif event.photo:
                        content = f"[фото] id={event.photo[-1].file_id}"
                    elif event.sticker:
                        emoji = event.sticker.emoji or ""
                        content = f"[стикер {emoji}] id={event.sticker.file_id}"
                    elif event.video:
                        content = f"[видео] id={event.video.file_id}"
                    elif event.video_note:
                        content = f"[видеосообщение] id={event.video_note.file_id}"
                    elif event.voice:
                        content = f"[голосовое] id={event.voice.file_id}"
                    elif event.audio:
                        content = f"[аудио] id={event.audio.file_id}"
                    elif event.animation:
                        content = f"[gif] id={event.animation.file_id}"
                    elif event.document:
                        fname = event.document.file_name or ""
                        content = f"[файл {fname}] id={event.document.file_id}"
                    elif event.contact:
                        content = f"[контакт] {event.contact.phone_number}"
                    elif event.location:
                        content = f"[геолокация] {event.location.latitude},{event.location.longitude}"
                    elif event.venue:
                        content = f"[место] {event.venue.title}"
                    elif event.poll:
                        content = f"[опрос] {event.poll.question}"
                    elif event.dice:
                        content = f"[кубик {event.dice.emoji}] {event.dice.value}"
                    else:
                        content = "[сообщение]"
                    if event.reply_to_message:
                        content += " (ответ на сообщение)"
                    await self._log(uid, event.from_user.username, "сообщение", content)
        return await handler(event, data)


class AdminEditLogMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        if isinstance(event, types.Message):
            uid = event.from_user.id
            if uid in ADMIN_IDS:
                text = event.text or event.caption or "[медиа]"
                log_admin_action(uid, f"[редактирование] {text[:500]}")
                log_chat = db.get_setting('admin_log_chat')
                if log_chat:
                    try:
                        uname = f"@{event.from_user.username}" if event.from_user.username else str(uid)
                        await bot_client.send_message(
                            int(log_chat),
                            f"✏️ {uname} (<code>{uid}</code>) отредактировал сообщение: <code>{text[:500]}</code>",
                            parse_mode=ParseMode.HTML,
                        )
                    except Exception as e:
                        logging.error(f"Не удалось отправить лог редактирования в чат логов: {e}")
        return await handler(event, data)


dp.message.outer_middleware(ShadowCheckMiddleware())
dp.message.outer_middleware(AdminLogMiddleware())
dp.callback_query.outer_middleware(AdminLogMiddleware())
dp.callback_query.outer_middleware(ShadowCheckMiddleware())
dp.edited_message.outer_middleware(AdminEditLogMiddleware())


# 🎛 Клавиатуры
RESERVED_BUTTON_TEXTS = {
    "👤 Профиль", "🌟 Заработать", "📚 Задания",
    "🏆 Топ 10", "✅ Бонус", "🔑 Промокод", "🎁 Вывод",
}


def main_kb():
    kb = ReplyKeyboardBuilder()
    kb.row(types.KeyboardButton(text="👤 Профиль"))
    kb.row(types.KeyboardButton(text="🌟 Заработать"), types.KeyboardButton(text="📚 Задания"))
    kb.row(types.KeyboardButton(text="🏆 Топ 10"), types.KeyboardButton(text="✅ Бонус"))
    kb.row(types.KeyboardButton(text="🔑 Промокод"), types.KeyboardButton(text="🎁 Вывод"))

    row_buf = []
    for b in get_bottom_buttons():
        btn_type = b["type"] if "type" in b.keys() and b["type"] else "link"
        if btn_type == "webapp":
            row_buf.append(types.KeyboardButton(text=b["text"], web_app=types.WebAppInfo(url=b["url"])))
        else:
            row_buf.append(types.KeyboardButton(text=b["text"]))
        if len(row_buf) == 2:
            kb.row(*row_buf)
            row_buf = []
    if row_buf:
        kb.row(*row_buf)

    return kb.as_markup(resize_keyboard=True)


async def _is_bottom_custom_button(m: types.Message) -> bool:
    if not m.text:
        return False
    row = db.conn.execute(
        "SELECT 1 FROM custom_buttons WHERE placement='bottom' AND text=? LIMIT 1", (m.text,)
    ).fetchone()
    return bool(row)


@dp.message(_is_bottom_custom_button)
async def bottom_custom_button_pressed(m: types.Message):
    row = db.conn.execute(
        "SELECT * FROM custom_buttons WHERE placement='bottom' AND text=? LIMIT 1", (m.text,)
    ).fetchone()
    if not row:
        return
    btn_type = row["type"] if "type" in row.keys() and row["type"] else "link"
    if btn_type in ("text", "info"):
        content = await _apply_placeholders(row["url"], m.from_user.id)
        await m.answer(f"<b>{content}</b>", parse_mode=ParseMode.HTML)
        return
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(
        text="🔗 Открыть", url=row["url"], style=row["style"], icon_custom_emoji_id=row["icon"],
    ))
    text = await _apply_placeholders(row["text"], m.from_user.id)
    await m.answer(f"<b>{text}</b>", reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)


def almaz_panel_kb(user_id: int = None):
    b = InlineKeyboardBuilder()

    if user_id is not None and user_id not in OWNER_IDS:
        panel = get_sub_admin_panel(user_id)
        allowed = set(panel_sections_list(panel)) if panel else set()
        for group_label, group_cb, leaves in PANEL_GROUPS:
            if any(leaf in allowed for leaf in leaves):
                b.row(InlineKeyboardButton(text=group_label, callback_data=group_cb))
        b.row(InlineKeyboardButton(text="⬅️ Закрыть панель", callback_data="a_close"))
        return b.as_markup()

    b.row(InlineKeyboardButton(text="📢 Рассылка", callback_data="a_br"))
    b.row(InlineKeyboardButton(text="📊 Статистика пользователей", callback_data="a_ustat"))
    b.row(InlineKeyboardButton(text="⚙️ Управление лимитами", callback_data="a_limits"))
    b.row(InlineKeyboardButton(text="🏆 VIP-уровни", callback_data="a_vip"))
    b.row(InlineKeyboardButton(text="🎟 Промокоды", callback_data="a_menu_pr"))
    b.row(InlineKeyboardButton(text="📚 Задания", callback_data="a_menu_ts"))
    b.row(InlineKeyboardButton(text="📣 Спонсоры", callback_data="a_menu_sp"))
    b.row(InlineKeyboardButton(text="🎨 Кастомизация", callback_data="a_custom"))
    b.row(InlineKeyboardButton(text="👑 Админ-панель", callback_data="a_adminpanels"))
    b.row(InlineKeyboardButton(text="📢 Реклама", callback_data="a_ads"))
    b.row(InlineKeyboardButton(text="⭐ Отзывы", callback_data="a_reviews"))
    b.row(InlineKeyboardButton(text="⬅️ Закрыть панель", callback_data="a_close"))
    return b.as_markup()


async def send_media(m: types.Message, photo_name: str, caption: str, kb=None, screen_key: str = None, user_id: int = None):
    if screen_key:
        await _send_screen(m, photo_name, caption, kb, screen_key, user_id=user_id)
        return
    path = f"photos/{photo_name}"
    if os.path.exists(path):
        await m.answer_photo(FSInputFile(path), caption=caption, reply_markup=kb, parse_mode=ParseMode.HTML)
    else:
        await m.answer(caption, reply_markup=kb, parse_mode=ParseMode.HTML)


# 👤 Старт
@dp.message(Command("start"))
async def start_cmd(m: types.Message, command: CommandObject, state: FSMContext):
    await state.clear()
    u = db.get_user(m.from_user.id)
    ref_id_str = command.args
    pending_ref = None
    ad_code = None

    if ref_id_str and ref_id_str.strip().isdigit():
        ref_candidate = int(ref_id_str.strip())
        if ref_candidate != m.from_user.id:
            pending_ref = ref_candidate
    elif ref_id_str and ref_id_str.strip().startswith("ad_"):
        candidate_code = ref_id_str.strip()[3:]
        if get_ad_link(candidate_code):
            ad_code = candidate_code

    if u:
        required_not_subbed, optional_sponsors = await get_sponsor_status(m.from_user.id)
        if required_not_subbed:
            msg = "<b>❌ Для работы с ботом необходимо подписаться на каналы:</b>"
            kb = build_sponsor_kb(required_not_subbed + optional_sponsors)
            await m.answer(msg, reply_markup=kb, parse_mode=ParseMode.HTML)
        else:
            text = (
                "<b>⭐ Добро пожаловать!\n\n"
                "📚 Выполняй задания\n"
                "👥 Приглашай друзей\n"
                "💸 Выводи звезды\n\n"
                "👇 Начни прямо сейчас</b>"
            )
            await _remove_bottom_kb(m)
            await send_media(m, "start.png", text, start_nav_kb(), screen_key="start", user_id=m.from_user.id)
        return

    required_not_subbed, optional_sponsors = await get_sponsor_status(m.from_user.id)
    if required_not_subbed:
        date = datetime.datetime.now().strftime("%d.%m.%Y")
        now_time = datetime.datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        db.conn.execute(
            "INSERT INTO users (tg_id, username, referrer_id, reg_date, last_active, language_code, invite_time, pending_referrer, ad_source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (m.from_user.id, m.from_user.username or "без_имени", None, date, now_time, m.from_user.language_code, now_time, pending_ref, ad_code),
        )
        db.conn.commit()
        msg = "<b>❌ Для работы с ботом необходимо подписаться на каналы:</b>"
        kb = build_sponsor_kb(required_not_subbed + optional_sponsors)
        await m.answer(msg, reply_markup=kb, parse_mode=ParseMode.HTML)
    else:
        date = datetime.datetime.now().strftime("%d.%m.%Y")
        now_time = datetime.datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        db.conn.execute(
            "INSERT INTO users (tg_id, username, referrer_id, reg_date, last_active, language_code, invite_time, ad_source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (m.from_user.id, m.from_user.username or "без_имени", pending_ref, date, now_time, m.from_user.language_code, now_time, ad_code),
        )
        if pending_ref:
            if m.from_user.id > 8000000000:
                try:
                    await bot_client.send_message(
                        pending_ref,
                        "<b>❌📏 Реферал отклонён</b>\n\n"
                        "У приглашённого пользователя ID больше 8 миллиардов.\n"
                        "По правилам проекта такие рефералы не засчитываются.\n\n"
                        "📌 <a href='https://t.me/+oL_j20X0QlkzYjRi'>Правила</a>",
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    )
                except Exception:
                    pass
            else:
                reward = float(db.get_setting('ref_reward')) * get_vip_multiplier(pending_ref)
                db.conn.execute(
                    "UPDATE users SET stars = stars + ?, earned = earned + ?, refs = refs + 1 WHERE tg_id = ?",
                    (reward, reward, pending_ref),
                )
                log_earn(pending_ref, reward)
                try:
                    await bot_client.send_message(
                        pending_ref,
                        f"<b>🎉 У вас новый реферал!\nВам начислено {fmt(reward)}⭐</b>",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass
        db.conn.commit()
        text = (
            "<b>⭐ Добро пожаловать!\n\n"
            "📚 Выполняй задания\n"
            "👥 Приглашай друзей\n"
            "💸 Выводи звезды\n\n"
            "👇 Начни прямо сейчас</b>"
        )
        await _remove_bottom_kb(m)
        await send_media(m, "start.png", text, start_nav_kb(), screen_key="start", user_id=m.from_user.id)


# ✅ Проверка подписки
@dp.callback_query(F.data == "check_subs")
async def check_subs_callback(c: types.CallbackQuery):
    required_not_subbed, _ = await get_sponsor_status(c.from_user.id)

    if required_not_subbed:
        await c.answer("❌ Вы ещё не подписались на все каналы!", show_alert=True)
        return

    u = db.get_user(c.from_user.id)
    if u and u['pending_referrer'] is not None:
        pending_ref = u['pending_referrer']
        db.conn.execute(
            "UPDATE users SET pending_referrer = NULL, referrer_id = ? WHERE tg_id = ?",
            (pending_ref, c.from_user.id),
        )
        if pending_ref:
            if c.from_user.id > 8000000000:
                try:
                    await bot_client.send_message(
                        pending_ref,
                        "<b>❌📏 Реферал отклонён</b>\n\n"
                        "У приглашённого пользователя ID больше 8 миллиардов.\n"
                        "По правилам проекта такие рефералы не засчитываются.\n\n"
                        "📌 <a href='https://t.me/+oL_j20X0QlkzYjRi'>Правила</a>",
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    )
                except Exception:
                    pass
            else:
                reward = float(db.get_setting('ref_reward')) * get_vip_multiplier(pending_ref)
                db.conn.execute(
                    "UPDATE users SET stars = stars + ?, earned = earned + ?, refs = refs + 1 WHERE tg_id = ?",
                    (reward, reward, pending_ref),
                )
                log_earn(pending_ref, reward)
                try:
                    await bot_client.send_message(
                        pending_ref,
                        f"<b>🎉 У вас новый реферал!\nВам начислено {fmt(reward)}⭐</b>",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass
        db.conn.commit()

    try:
        await c.message.delete()
    except Exception:
        pass
    await _remove_bottom_kb(c.message)
    await c.message.answer(
        "<b>✅ Отлично! Доступ к боту открыт.</b>",
        reply_markup=start_nav_kb(),
        parse_mode=ParseMode.HTML,
    )
    await c.answer()


# 👤 Профиль пользователя
async def show_profile(user_id: int, responder):
    u = db.get_user(user_id)
    mr = db.get_setting('min_refs')
    tasks_done = db.conn.execute(
        "SELECT COUNT(*) FROM completed_tasks WHERE tg_id=?", (user_id,)
    ).fetchone()[0]

    me = await bot_client.me()
    text = (
        f"<b>👤 Профиль\n\n"
        f"⭐ Баланс: {fmt(u['stars'])}\n"
        f"💰 Всего заработано: {fmt(u['earned'])}\n"
        f"👥 Рефералы: {u['refs']}/{mr}\n"
        f"📤 Выводов: {u['withdrawals_count']}\n"
        f"📚 Выполнено заданий: {tasks_done}\n\n"
        f"{_vip_status_text(u)}\n\n"
        f"🔗 Твоя ссылка:\n"
        f"https://t.me/{me.username}?start={u['tg_id']}</b>"
    )
    await send_media(responder, "profile.png", text, _with_menu_button(None), screen_key="profile", user_id=user_id)


@dp.message(F.text == "👤 Профиль")
async def profile_user(m: types.Message):
    await show_profile(m.from_user.id, m)


@dp.callback_query(F.data == "nav_profile")
async def nav_profile_cb(c: types.CallbackQuery):
    await show_profile(c.from_user.id, c.message)
    await c.answer()


# 🌟 Заработать
async def show_earn(user_id: int, responder):
    u = db.get_user(user_id)
    reward = float(db.get_setting('ref_reward')) * get_vip_multiplier(user_id)
    me = await bot_client.me()
    vip_block = _vip_status_text(u) if u else ""
    text = (
        f"<b>🌟 Заработать звезды\n\n"
        f"👥 Приглашай друзей и получай за каждого: {fmt(reward)}⭐\n\n"
        f"{vip_block}\n\n"
        f"🔗 Твоя ссылка:\n"
        f"https://t.me/{me.username}?start={user_id}</b>"
    )
    await send_media(responder, "earn.png", text, _with_menu_button(None), screen_key="earn", user_id=user_id)


@dp.message(F.text == "🌟 Заработать")
async def earn_user(m: types.Message):
    await show_earn(m.from_user.id, m)


@dp.callback_query(F.data == "nav_earn")
async def nav_earn_cb(c: types.CallbackQuery):
    await show_earn(c.from_user.id, c.message)
    await c.answer()


# 📚 Задания
@dp.message(F.text == "📚 Задания")
async def tasks_user(m: types.Message):
    await show_next_task(m.from_user.id, m)


@dp.callback_query(F.data == "nav_tasks")
async def nav_tasks_cb(c: types.CallbackQuery):
    await show_next_task(c.from_user.id, c)
    await c.answer()


async def show_next_task(user_id: int, message_or_callback):
    total_tasks = db.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    if total_tasks == 0:
        await _send_screen(message_or_callback, "zadanka.png", "<b>📚 Заданий пока нет. Загляни позже!</b>", _with_menu_button(None), "tasks", user_id=user_id)
        return

    all_ts = db.conn.execute(
        "SELECT * FROM tasks WHERE id NOT IN (SELECT task_id FROM completed_tasks WHERE tg_id=?)",
        (user_id,),
    ).fetchall()

    if not all_ts:
        await _send_screen(message_or_callback, "zadanka.png", "<b>🎉 Вы выполнили все задания! 🎉</b>", _with_menu_button(None), "tasks", user_id=user_id)
        return

    t = all_ts[0]
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🔗 Выполнить задание", url=t['link']))
    kb.row(InlineKeyboardButton(text="✅ Проверить выполнение", callback_data=f"chk_ts_{t['id']}"))
    kb.row(InlineKeyboardButton(text="⏭ Пропустить", callback_data=f"skip_ts_{t['id']}"))

    text = f"<b>📚 Задания\n\n{t['description']}\n\n💰 Награда: {fmt(t['prize'])}⭐</b>"
    await _send_screen(message_or_callback, "zadanka.png", text, _with_menu_button(kb.as_markup()), "tasks", user_id=user_id)


async def verify_subscription(chat_id: str, user_id: int) -> bool:
    try:
        member = await bot_client.get_chat_member(chat_id, user_id)
        return member.status in ['member', 'administrator', 'creator']
    except Exception:
        return False


@dp.callback_query(F.data.startswith("skip_ts_"))
async def skip_task_callback(c: types.CallbackQuery):
    tid = int(c.data.split("_")[2])
    already_done = db.conn.execute(
        "SELECT 1 FROM completed_tasks WHERE tg_id=? AND task_id=?",
        (c.from_user.id, tid),
    ).fetchone()
    if not already_done:
        db.conn.execute("INSERT INTO completed_tasks VALUES (?, ?)", (c.from_user.id, tid))
        db.conn.commit()
    await c.answer("Задание пропущено")
    await show_next_task(c.from_user.id, c)


@dp.callback_query(F.data.startswith("chk_ts_"))
async def check_task_callback(c: types.CallbackQuery):
    tid = int(c.data.split("_")[2])

    already_done = db.conn.execute(
        "SELECT 1 FROM completed_tasks WHERE tg_id=? AND task_id=?",
        (c.from_user.id, tid),
    ).fetchone()

    if already_done:
        await c.answer("❌ Вы уже выполнили это задание!", show_alert=True)
        await show_next_task(c.from_user.id, c)
        return

    t = db.conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    if not t:
        await c.answer("Задание больше не доступно.", show_alert=True)
        await show_next_task(c.from_user.id, c)
        return

    link = t['link']
    checked = False

    chat_id = None
    if "t.me/" in link:
        if "/+" in link or "joinchat" in link:
            pass
        else:
            parts = link.split("t.me/")[1].split("/")[0]
            chat_id = parts if parts.startswith('@') else '@' + parts
    elif link.startswith('@') or link.startswith('-100'):
        chat_id = link
    else:
        checked = True

    if chat_id:
        checked = await verify_subscription(chat_id, c.from_user.id)
    else:
        checked = True

    if checked:
        already_done = db.conn.execute(
            "SELECT 1 FROM completed_tasks WHERE tg_id=? AND task_id=?",
            (c.from_user.id, tid),
        ).fetchone()
        if already_done:
            await c.answer("❌ Вы уже выполнили это задание!", show_alert=True)
            await show_next_task(c.from_user.id, c)
            return

        prize = t['prize'] * get_vip_multiplier(c.from_user.id)
        try:
            db.conn.execute("INSERT INTO completed_tasks VALUES (?, ?)", (c.from_user.id, tid))
            db.conn.execute(
                "UPDATE users SET stars=stars+?, earned=earned+? WHERE tg_id=?",
                (prize, prize, c.from_user.id),
            )
            log_earn(c.from_user.id, prize)
            db.conn.commit()
        except Exception as e:
            logging.error(f"Ошибка при выполнении задания: {e}")
            await c.answer("❌ Произошла ошибка, попробуйте позже", show_alert=True)
            return

        await c.answer(f"✅ Награда {fmt(prize)}⭐ успешно получена!", show_alert=True)
        await show_next_task(c.from_user.id, c)
    else:
        await c.answer("❌ Вы не выполнили условие задания!", show_alert=True)


# 🏆 Топ 10
async def show_top10(user_id: int, responder):
    top = db.conn.execute(
        "SELECT tg_id, username, earned, refs, withdrawals_count FROM users ORDER BY earned DESC LIMIT 10"
    ).fetchall()
    text = "<b>🏆 Топ 10 по звёздам</b>\n\n"
    for i, r in enumerate(top, 1):
        tasks = db.conn.execute("SELECT COUNT(*) FROM completed_tasks WHERE tg_id = ?", (r['tg_id'],)).fetchone()[0]
        uname = f"@{r['username']}" if r['username'] else "—"
        text += (
            f"<b>{i}. <a href='tg://user?id={r['tg_id']}'>{r['tg_id']}</a> ({uname})</b>\n"
            f"⭐ {fmt(r['earned'])}  👥 {r['refs']}  📚 {tasks}  📤 {r['withdrawals_count']}\n\n"
        )
    await send_media(responder, "top10.png", text, _with_menu_button(None), screen_key="top10", user_id=user_id)


@dp.message(F.text == "🏆 Топ 10")
async def top10_user(m: types.Message):
    await show_top10(m.from_user.id, m)


@dp.callback_query(F.data == "nav_top10")
async def nav_top10_cb(c: types.CallbackQuery):
    await show_top10(c.from_user.id, c.message)
    await c.answer()


# ✅ Ежедневный бонус
async def show_daily_bonus(user_id: int, responder):
    u = db.get_user(user_id)
    today = datetime.datetime.now().strftime("%d.%m.%Y")
    me = await bot_client.me()
    ref_link = f"https://t.me/{me.username}?start={user_id}"
    daily_needed = int(db.get_setting('daily_refs'))
    daily_reward = float(db.get_setting('daily_reward')) * get_vip_multiplier(user_id)

    if u['last_bonus_date'] == today:
        text = "✅ <b>Ты уже получил бонус сегодня!</b>\nПриходи завтра за новым бонусом."
        await send_media(responder, "bonus.png", text, _with_menu_button(None), screen_key="bonus", user_id=user_id)
        return

    refs_today = db.conn.execute(
        "SELECT COUNT(*) FROM users WHERE referrer_id = ? AND invite_time LIKE ?",
        (user_id, f"{today}%"),
    ).fetchone()[0]

    if refs_today >= daily_needed:
        text = (
            f"🎉 <b>Бонус доступен!</b>\n\n"
            f"📊 Приглашено сегодня: {refs_today} из {daily_needed}\n"
            f"💰 Награда: {fmt(daily_reward)}⭐\n\n"
            f"Нажми кнопку ниже, чтобы получить бонус!"
        )
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="🎁 Получить бонус", callback_data="get_daily_bonus"))
        await send_media(responder, "bonus.png", text, _with_menu_button(kb.as_markup()), screen_key="bonus", user_id=user_id)
    else:
        need = daily_needed - refs_today
        text = (
            f"📢 <b>Для получения бонуса нужно пригласить {daily_needed} человек в день.</b>\n\n"
            f"🔹 Приглашено сегодня: {refs_today}\n"
            f"🔹 Осталось пригласить: {need}\n"
            f"🔹 Награда: {fmt(daily_reward)}⭐\n\n"
            f"🔗 Твоя реферальная ссылка:\n{ref_link}\n\n"
            f"Делись ссылкой с друзьями и получай бонусы!"
        )
        await send_media(responder, "bonus.png", text, _with_menu_button(None), screen_key="bonus", user_id=user_id)


@dp.message(F.text == "✅ Бонус")
async def daily_bonus_info(m: types.Message):
    await show_daily_bonus(m.from_user.id, m)


@dp.callback_query(F.data == "nav_bonus")
async def nav_bonus_cb(c: types.CallbackQuery):
    await show_daily_bonus(c.from_user.id, c.message)
    await c.answer()


@dp.callback_query(F.data == "get_daily_bonus")
async def get_daily_bonus_callback(c: types.CallbackQuery):
    u = db.get_user(c.from_user.id)
    today = datetime.datetime.now().strftime("%d.%m.%Y")
    daily_needed = int(db.get_setting('daily_refs'))
    daily_reward = float(db.get_setting('daily_reward'))

    if u['last_bonus_date'] == today:
        await c.answer("❌ Ты уже получил бонус сегодня!", show_alert=True)
        return

    refs_today = db.conn.execute(
        "SELECT COUNT(*) FROM users WHERE referrer_id = ? AND invite_time LIKE ?",
        (c.from_user.id, f"{today}%"),
    ).fetchone()[0]

    if refs_today >= daily_needed:
        vip_reward = daily_reward * get_vip_multiplier(c.from_user.id)
        db.conn.execute(
            "UPDATE users SET stars = stars + ?, earned = earned + ?, last_bonus_date = ? WHERE tg_id = ?",
            (vip_reward, vip_reward, today, c.from_user.id),
        )
        log_earn(c.from_user.id, vip_reward)
        db.conn.commit()
        await c.message.edit_text(
            f"🎉 <b>Бонус получен!</b>\n\n"
            f"➕ Начислено: {fmt(vip_reward)}⭐\n"
            f"📊 Приглашено сегодня: {refs_today}\n"
            f"Приходи завтра за новым бонусом!",
            parse_mode=ParseMode.HTML,
        )
    else:
        need = daily_needed - refs_today
        await c.answer(f"❌ Пригласи ещё {need} друзей сегодня!", show_alert=True)


# 🔑 Промокод
async def show_promo(user_id: int, responder, state: FSMContext):
    await send_media(responder, "promo.png", "<b>🔑 Введите промокод:</b>", _with_menu_button(None), screen_key="promo", user_id=user_id)
    await state.set_state(UserStates.promo)


@dp.message(F.text == "🔑 Промокод")
async def promo_user(m: types.Message, state: FSMContext):
    await show_promo(m.from_user.id, m, state)


@dp.callback_query(F.data == "nav_promo")
async def nav_promo_cb(c: types.CallbackQuery, state: FSMContext):
    await show_promo(c.from_user.id, c.message, state)
    await c.answer()


@dp.message(UserStates.promo)
async def promo_activate(m: types.Message, state: FSMContext):
    code = m.text.strip().upper()
    p = db.conn.execute("SELECT * FROM promos WHERE code = ?", (code,)).fetchone()
    if not p:
        await m.answer("<b>❌ Промокод не найден.</b>", parse_mode=ParseMode.HTML)
        await state.clear()
        return

    if p['expires_at']:
        try:
            expire_dt = datetime.datetime.strptime(p['expires_at'], "%d.%m.%Y %H:%M")
            now_msk = msk_now()
            if now_msk > expire_dt:
                await m.answer("<b>❌ Срок действия промокода истёк.</b>", parse_mode=ParseMode.HTML)
                await state.clear()
                return
        except Exception:
            pass

    if p['max_uses'] > 0 and p['used_count'] >= p['max_uses']:
        await m.answer("<b>❌ Промокод больше не действует (исчерпан лимит активаций).</b>", parse_mode=ParseMode.HTML)
        await state.clear()
        return

    used = db.conn.execute(
        "SELECT 1 FROM used_promos WHERE tg_id = ? AND code = ?", (m.from_user.id, code)
    ).fetchone()
    if used:
        await m.answer("<b>❌ Вы уже использовали этот промокод.</b>", parse_mode=ParseMode.HTML)
        await state.clear()
        return

    db.conn.execute(
        "UPDATE users SET stars = stars + ?, earned = earned + ? WHERE tg_id = ?",
        (p['stars'], p['stars'], m.from_user.id),
    )
    log_earn(m.from_user.id, p['stars'])
    db.conn.execute("INSERT INTO used_promos VALUES (?, ?)", (m.from_user.id, code))
    db.conn.execute("UPDATE promos SET used_count = used_count + 1 WHERE code = ?", (code,))
    db.conn.commit()

    await m.answer(
        f"<b>✅ Промокод активирован!\nЗачислено: {fmt(p['stars'])}⭐</b>",
        parse_mode=ParseMode.HTML,
    )
    await state.clear()


# 💸 Вывод
async def show_withdraw(user_id: int, responder):
    u = db.get_user(user_id)
    if not u:
        return await responder.answer("<b>❌ Пользователь не найден!</b>", parse_mode=ParseMode.HTML)

    mr_setting = db.get_setting('min_refs')
    mr = int(mr_setting) if mr_setting else 5

    if u['refs'] < mr:
        return await responder.answer(
            f"<b>❌ Для вывода нужно пригласить минимум {mr} друзей.\nУ вас: {u['refs']}</b>",
            parse_mode=ParseMode.HTML,
        )

    if u['stars'] <= 0:
        return await responder.answer("<b>❌ У вас нет звёзд для вывода!</b>", parse_mode=ParseMode.HTML)

    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="15⭐", callback_data="req_wd_15"),
        InlineKeyboardButton(text="25⭐", callback_data="req_wd_25"),
        InlineKeyboardButton(text="50⭐", callback_data="req_wd_50"),
        InlineKeyboardButton(text="100⭐", callback_data="req_wd_100"),
    )
    await send_media(responder, "withdraw.png", "<b>🎁 Вывод\n\nВыберите сумму:</b>", _with_menu_button(kb.as_markup()), screen_key="withdraw", user_id=user_id)


@dp.message(F.text == "🎁 Вывод")
async def withdraw_user(m: types.Message):
    await show_withdraw(m.from_user.id, m)


@dp.callback_query(F.data == "nav_withdraw")
async def nav_withdraw_cb(c: types.CallbackQuery):
    await show_withdraw(c.from_user.id, c.message)
    await c.answer()


@dp.callback_query(F.data.startswith("req_wd_"))
async def process_withdraw_request(c: types.CallbackQuery):
    try:
        amt = float(c.data.split("_")[2])
    except (IndexError, ValueError):
        return await c.answer("❌ Ошибка в данных запроса!", show_alert=True)

    u = db.get_user(c.from_user.id)
    if not u:
        return await c.answer("❌ Пользователь не найден!", show_alert=True)

    if amt > u['stars']:
        return await c.answer(
            f"❌ Недостаточно средств! Ваш баланс: {fmt(u['stars'])}⭐",
            show_alert=True,
        )

    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(
        text=f"✅ Подтвердить вывод {fmt(amt)}⭐",
        callback_data=f"confirm_wd_{amt}",
    ))
    kb.row(InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_wd"))

    try:
        await c.message.edit_reply_markup(reply_markup=kb.as_markup())
    except Exception as e:
        logging.error(f"Ошибка редактирования клавиатуры: {e}")
        await c.answer("❌ Ошибка! Попробуйте снова.", show_alert=True)
    else:
        await c.answer()


@dp.callback_query(F.data == "cancel_wd")
async def cancel_withdraw(c: types.CallbackQuery):
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="15⭐", callback_data="req_wd_15"),
        InlineKeyboardButton(text="25⭐", callback_data="req_wd_25"),
        InlineKeyboardButton(text="50⭐", callback_data="req_wd_50"),
        InlineKeyboardButton(text="100⭐", callback_data="req_wd_100"),
    )
    try:
        await c.message.edit_reply_markup(reply_markup=kb.as_markup())
    except Exception:
        pass
    await c.answer("Отменено.")


processed_confirmations = set()


@dp.callback_query(F.data.startswith("confirm_wd_"))
async def confirm_withdraw_final(c: types.CallbackQuery):
    if c.id in processed_confirmations:
        await c.answer("⏳ Заявка уже обрабатывается...", show_alert=True)
        return
    processed_confirmations.add(c.id)

    try:
        amt = float(c.data.split("_")[2])
    except (IndexError, ValueError):
        processed_confirmations.discard(c.id)
        return await c.answer("❌ Ошибка в данных запроса!", show_alert=True)

    u = db.get_user(c.from_user.id)
    if not u:
        processed_confirmations.discard(c.id)
        return await c.answer("❌ Пользователь не найден!", show_alert=True)

    if amt > u['stars']:
        processed_confirmations.discard(c.id)
        return await c.answer("❌ Недостаточно средств!", show_alert=True)

    if not PAY_CHANNEL:
        processed_confirmations.discard(c.id)
        logging.error("PAY_CHANNEL не указан!")
        return await c.answer("❌ Ошибка конфигурации!", show_alert=True)

    db.conn.execute(
        "UPDATE users SET stars=stars-?, withdrawals_count=withdrawals_count+1 WHERE tg_id=?",
        (amt, c.from_user.id),
    )
    now = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")
    cur = db.conn.execute(
        "INSERT INTO withdraw_requests (user_id, amount, created_at) VALUES (?, ?, ?)",
        (c.from_user.id, amt, now),
    )
    db.conn.commit()
    request_id = cur.lastrowid

    try:
        await c.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    u_upd = db.get_user(c.from_user.id)
    all_refs = db.conn.execute(
        "SELECT tg_id FROM users WHERE referrer_id=?", (c.from_user.id,)
    ).fetchall()

    ref_links = "\n".join(
        [f"• <a href='tg://user?id={r[0]}'>{r[0]}</a>" for r in all_refs]
    ) if all_refs else "• Нет рефералов"

    uname = f"@{u_upd['username']}" if u_upd['username'] and u_upd['username'] != "без_имени" else "без юзернейма"

    pay_msg = (
        f"<b>💎 ЗАЯВКА НА ВЫВОД #{request_id}\n"
        f"{'━' * 25}\n\n"
        f"👤 Пользователь: <a href='tg://user?id={c.from_user.id}'>{c.from_user.id}</a>\n"
        f"📛 Юзернейм: {uname}\n\n"
        f"💰 Сумма вывода: {fmt(amt)}⭐\n"
        f"💳 Остаток на балансе: {fmt(u_upd['stars'])}⭐\n"
        f"📩 Номер вывода: #{u_upd['withdrawals_count']}\n"
        f"👥 Рефералов всего: {u_upd['refs']}\n"
        f"📋 Список рефералов:\n{ref_links}\n"
        f"{'━' * 25}</b>"
    )

    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="✅ Выплатить", callback_data=f"wd_acc_{c.from_user.id}_{amt}_{request_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"wd_rej_{c.from_user.id}_{amt}_{request_id}"),
    )

    try:
        await bot_client.send_message(
            PAY_CHANNEL,
            pay_msg,
            reply_markup=kb.as_markup(),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as e:
        logging.error(f"Ошибка отправки в PAY_CHANNEL: {e}")
        await c.message.answer(
            "<b>❌ Произошла ошибка при создании заявки. Попробуйте позже.</b>",
            parse_mode=ParseMode.HTML,
        )
        processed_confirmations.discard(c.id)
        return await c.answer("❌ Ошибка сервера", show_alert=True)

    try:
        await c.message.delete()
    except Exception:
        pass

    await c.message.answer(
        "<b>✅ Заявка отправлена! Ожидайте зачисления.</b>",
        parse_mode=ParseMode.HTML,
    )
    await c.answer("✅ Заявка создана!", show_alert=True)
    processed_confirmations.discard(c.id)


# 👑 ADMIN PANEL
@dp.message(Command("admin"), F.from_user.id.in_(ADMIN_IDS))
async def almaz_entry(m: types.Message, state: FSMContext):
    await state.clear()
    uid = m.from_user.id

    if uid in OWNER_IDS and uid not in ROOT_OWNER_IDS and db.get_setting('owner_password'):
        await state.set_state(AdminStates.owner_password_check)
        await state.update_data(pw_kind="owner")
        await m.answer("<b>🔒 Введите пароль для входа в панель:</b>", parse_mode=ParseMode.HTML)
        return

    if uid not in OWNER_IDS:
        panel = get_sub_admin_panel(uid)
        if panel and panel["password"]:
            await state.set_state(AdminStates.owner_password_check)
            await state.update_data(pw_kind="panel", pw_panel_id=panel["id"])
            await m.answer("<b>🔒 Введите пароль для входа в панель:</b>", parse_mode=ParseMode.HTML)
            return

    await m.answer("<b>🛠 В админ-панели</b>", reply_markup=almaz_panel_kb(uid), parse_mode=ParseMode.HTML)


@dp.message(AdminStates.owner_password_check, F.from_user.id.in_(ADMIN_IDS))
async def almaz_password_check(m: types.Message, state: FSMContext):
    data = await state.get_data()
    kind = data.get("pw_kind")
    panel_id = data.get("pw_panel_id")
    await state.clear()
    entered = (m.text or "").strip()

    ok = check_panel_password(panel_id, entered) if kind == "panel" else check_owner_password(entered)
    if ok:
        await m.answer("<b>🛠 В админ-панели</b>", reply_markup=almaz_panel_kb(m.from_user.id), parse_mode=ParseMode.HTML)
    else:
        await m.answer("<b>❌ Неверный пароль.</b>", parse_mode=ParseMode.HTML)


@dp.message(Command("setpass"), F.from_user.id.in_(ROOT_OWNER_IDS))
async def cmd_setpass(m: types.Message, command: CommandObject):
    if not command.args:
        return await m.answer("<b>Использование: /setpass новый_пароль</b>", parse_mode=ParseMode.HTML)
    set_owner_password(command.args.strip())
    await m.answer("<b>✅ Пароль на панель установлен/изменён.</b>\n\nЕго будут спрашивать у добавленных со-владельцев при входе в /admin (у вас — никогда).", parse_mode=ParseMode.HTML)


@dp.message(Command("delpass"), F.from_user.id.in_(ROOT_OWNER_IDS))
async def cmd_delpass(m: types.Message):
    clear_owner_password()
    await m.answer("<b>✅ Пароль удалён.</b>", parse_mode=ParseMode.HTML)


@dp.message(Command("addowner"), F.from_user.id.in_(ROOT_OWNER_IDS))
async def cmd_addowner(m: types.Message, command: CommandObject):
    ids = (command.args or "").split()
    if not ids or not all(i.isdigit() for i in ids):
        return await m.answer(
            "<b>Использование: /addowner id1 id2 id3 ...</b>\nМожно указать сразу несколько ID через пробел.",
            parse_mode=ParseMode.HTML,
        )
    added = []
    for id_str in ids:
        uid = int(id_str)
        add_co_owner(uid)
        added.append(uid)
        try:
            await bot_client.send_message(
                uid, "<b>👑 Вам выдан полный доступ к админ-панели бота. Откройте /admin</b>", parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
    names = "\n".join(f"• <code>{uid}</code>" for uid in added)
    await m.answer(f"<b>✅ Добавлены как полноправные со-владельцы:</b>\n{names}", parse_mode=ParseMode.HTML)


@dp.message(Command("delowner"), F.from_user.id.in_(ROOT_OWNER_IDS))
async def cmd_delowner(m: types.Message, command: CommandObject):
    ids = (command.args or "").split()
    if not ids or not all(i.isdigit() for i in ids):
        return await m.answer(
            "<b>Использование: /delowner id1 id2 ...</b>\nМожно указать сразу несколько ID через пробел.",
            parse_mode=ParseMode.HTML,
        )
    for id_str in ids:
        remove_co_owner(int(id_str))
    names = "\n".join(f"• <code>{i}</code>" for i in ids)
    await m.answer(f"<b>✅ Убраны из полноправных со-владельцев:</b>\n{names}", parse_mode=ParseMode.HTML)


@dp.callback_query(F.data.startswith("wd_"), F.from_user.id.in_(OWNER_IDS))
async def process_withdrawal_buttons(c: types.CallbackQuery):
    data = c.data
    parts = data.split('_')
    if len(parts) < 3:
        return await c.answer("❌ Некорректные данные", show_alert=True)

    if parts[1] == "acc":
        action = "acc"
        uid = int(parts[2])
        amt = float(parts[3])
        req_id = int(parts[4]) if len(parts) > 4 else None
    elif parts[1] == "rej":
        action = "rej"
        uid = int(parts[2])
        amt = float(parts[3])
        req_id = int(parts[4]) if len(parts) > 4 else None
    elif parts[1] == "confirm":
        if parts[2] == "acc":
            action = "confirm_acc"
            uid = int(parts[3])
            amt = float(parts[4])
            req_id = int(parts[5]) if len(parts) > 5 else None
        elif parts[2] == "rej":
            action = "confirm_rej"
            uid = int(parts[3])
            amt = float(parts[4])
            req_id = int(parts[5]) if len(parts) > 5 else None
        else:
            return await c.answer("❌ Неизвестное подтверждение", show_alert=True)
    elif parts[1] == "back":
        action = "back"
        uid = int(parts[2])
        amt = float(parts[3])
        req_id = int(parts[4]) if len(parts) > 4 else None
    else:
        return await c.answer("❌ Неизвестное действие", show_alert=True)

    if action == "acc":
        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(text="✅ Да, выплатить", callback_data=f"wd_confirm_acc_{uid}_{amt}_{req_id}"),
            InlineKeyboardButton(text="↩️ Назад", callback_data=f"wd_back_{uid}_{amt}_{req_id}"),
        )
        await c.message.edit_reply_markup(reply_markup=kb.as_markup())
        await c.answer()

    elif action == "rej":
        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(text="✅ Да, отклонить", callback_data=f"wd_confirm_rej_{uid}_{amt}_{req_id}"),
            InlineKeyboardButton(text="↩️ Назад", callback_data=f"wd_back_{uid}_{amt}_{req_id}"),
        )
        await c.message.edit_reply_markup(reply_markup=kb.as_markup())
        await c.answer()

    elif action == "confirm_acc":
        now = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")
        db.conn.execute("UPDATE withdraw_requests SET status='paid', resolved_at=? WHERE id=?", (now, req_id))
        db.conn.commit()
        try:
            await bot_client.send_message(
                uid,
                "<b>✅ Выплата была отправлена на ваш юзернейм✅</b>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        try:
            rev_kb = InlineKeyboardBuilder()
            rev_kb.row(
                InlineKeyboardButton(text="👍 Хорошо", callback_data=f"rev_pos_{amt}"),
                InlineKeyboardButton(text="👎 Плохо", callback_data=f"rev_neg_{amt}"),
            )
            await bot_client.send_message(
                uid,
                f"<b>🌟 Оцените вывод {fmt(amt)}⭐!</b>\n\nВаш отзыв поможет другим пользователям.",
                reply_markup=rev_kb.as_markup(),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        try:
            await c.message.edit_text(
                c.message.html_text + "\n\n<b>✅ СТАТУС: ВЫПЛАЧЕНО</b>",
                reply_markup=None,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception:
            pass
        await c.answer("Выплата подтверждена.")

    elif action == "confirm_rej":
        now = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")
        db.conn.execute("UPDATE withdraw_requests SET status='rejected', resolved_at=? WHERE id=?", (now, req_id))
        db.conn.commit()
        try:
            await bot_client.send_message(
                uid,
                "<b>❌ Выплата была отклонена, средства не возвращаются на баланс❌</b>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        try:
            await c.message.edit_text(
                c.message.html_text + "\n\n<b>❌ СТАТУС: ОТКЛОНЕНО</b>",
                reply_markup=None,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception:
            pass
        await c.answer("Отклонено.")

    elif action == "back":
        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(text="✅ Выплатить", callback_data=f"wd_acc_{uid}_{amt}_{req_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"wd_rej_{uid}_{amt}_{req_id}"),
        )
        await c.message.edit_reply_markup(reply_markup=kb.as_markup())
        await c.answer()


# ⭐ ОТЗЫВЫ
@dp.callback_query(F.data.startswith("rev_pos_"))
async def review_start_positive(c: types.CallbackQuery, state: FSMContext):
    amt = float(c.data[len("rev_pos_"):])
    await state.update_data(rev_amt=amt, rev_positive=True)
    await state.set_state(UserStates.review_text)
    await c.message.edit_text(
        "<b>👍 Спасибо! Напишите короткий отзыв, или отправьте «-», чтобы пропустить:</b>", parse_mode=ParseMode.HTML,
    )
    await c.answer()


@dp.callback_query(F.data.startswith("rev_neg_"))
async def review_start_negative(c: types.CallbackQuery, state: FSMContext):
    amt = float(c.data[len("rev_neg_"):])
    await state.update_data(rev_amt=amt, rev_positive=False)
    await state.set_state(UserStates.review_text)
    await c.message.edit_text(
        "<b>👎 Жаль! Расскажите, что пошло не так, или отправьте «-», чтобы пропустить:</b>", parse_mode=ParseMode.HTML,
    )
    await c.answer()


@dp.message(UserStates.review_text)
async def review_text_received(m: types.Message, state: FSMContext):
    data = await state.get_data()
    amt = data.get("rev_amt")
    is_positive = data.get("rev_positive")
    await state.clear()
    if amt is None:
        return
    text = (m.text or "").strip()
    if text == "-":
        text = ""
    review_id = create_review(m.from_user.id, amt, bool(is_positive), text)
    await m.answer("<b>✅ Спасибо за отзыв! Он появится после проверки.</b>", parse_mode=ParseMode.HTML)

    badge = "👍 Хорошо" if is_positive else "👎 Плохо"
    uname = f"@{m.from_user.username}" if m.from_user.username else str(m.from_user.id)
    mod_text = (
        f"<b>⭐ Новый отзыв на модерацию</b>\n\n"
        f"От: {uname} (<code>{m.from_user.id}</code>)\n"
        f"Сумма вывода: {fmt(amt)}⭐\n"
        f"Оценка: {badge}\n"
        f"Текст: {text or '—'}"
    )
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="✅ Принять", callback_data=f"revmod_ask_acc_{review_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"revmod_ask_rej_{review_id}"),
    )
    for owner_id in ROOT_OWNER_IDS:
        try:
            await bot_client.send_message(owner_id, mod_text, reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)
        except Exception:
            pass


def _revmod_pending_kb(review_id: int):
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="✅ Принять", callback_data=f"revmod_ask_acc_{review_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"revmod_ask_rej_{review_id}"),
    )
    return kb.as_markup()


@dp.callback_query(F.data.startswith("revmod_ask_acc_"), F.from_user.id.in_(ROOT_OWNER_IDS))
async def revmod_ask_accept(c: types.CallbackQuery):
    review_id = int(c.data[len("revmod_ask_acc_"):])
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="✅ Да, опубликовать", callback_data=f"revmod_acc_{review_id}"),
        InlineKeyboardButton(text="↩️ Назад", callback_data=f"revmod_cancel_{review_id}"),
    )
    await c.message.edit_reply_markup(reply_markup=kb.as_markup())
    await c.answer()


@dp.callback_query(F.data.startswith("revmod_ask_rej_"), F.from_user.id.in_(ROOT_OWNER_IDS))
async def revmod_ask_reject(c: types.CallbackQuery):
    review_id = int(c.data[len("revmod_ask_rej_"):])
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="✅ Да, отклонить", callback_data=f"revmod_rej_{review_id}"),
        InlineKeyboardButton(text="↩️ Назад", callback_data=f"revmod_cancel_{review_id}"),
    )
    await c.message.edit_reply_markup(reply_markup=kb.as_markup())
    await c.answer()


@dp.callback_query(F.data.startswith("revmod_cancel_"), F.from_user.id.in_(ROOT_OWNER_IDS))
async def revmod_cancel(c: types.CallbackQuery):
    review_id = int(c.data[len("revmod_cancel_"):])
    await c.message.edit_reply_markup(reply_markup=_revmod_pending_kb(review_id))
    await c.answer()


@dp.callback_query(F.data.startswith("revmod_acc_"), F.from_user.id.in_(ROOT_OWNER_IDS))
async def revmod_accept(c: types.CallbackQuery):
    review_id = int(c.data[len("revmod_acc_"):])
    set_review_status(review_id, "published")
    await c.message.edit_text(
        c.message.html_text + "\n\n<b>✅ ОПУБЛИКОВАН</b>", reply_markup=None, parse_mode=ParseMode.HTML,
    )
    await c.answer("Опубликован")


@dp.callback_query(F.data.startswith("revmod_rej_"), F.from_user.id.in_(ROOT_OWNER_IDS))
async def revmod_reject(c: types.CallbackQuery):
    review_id = int(c.data[len("revmod_rej_"):])
    set_review_status(review_id, "rejected")
    await c.message.edit_text(
        c.message.html_text + "\n\n<b>❌ ОТКЛОНЁН</b>", reply_markup=None, parse_mode=ParseMode.HTML,
    )
    await c.answer("Отклонён")


# 📢 РАССЫЛКА
@dp.callback_query(F.data == "a_br", F.from_user.id.in_(ADMIN_IDS))
async def adm_broadcast_start(c: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text = (
        "<b>📢 Рассылка\n\n"
        "Шаг 1/2: Отправьте сообщение для рассылки.\n\n"
        "Можно отправить:\n"
        "• Текст (HTML поддерживается)\n"
        "• Фото с подписью\n"
        "• Видео с подписью\n"
        "• GIF с подписью\n\n"
        "💎 Премиум-эмодзи можно вставлять прямо в текст/подпись — просто скопируйте сообщение с ними, они сохранятся.\n"
        f"{PLACEHOLDER_HELP}\n\n"
        "Просто отправьте сообщение сюда 👇</b>"
    )
    await c.message.answer(text, parse_mode=ParseMode.HTML)
    await state.set_state(AdminStates.br_media)
    await c.answer()


@dp.message(AdminStates.br_media, F.from_user.id.in_(ADMIN_IDS))
async def adm_broadcast_got_media(m: types.Message, state: FSMContext):
    media_data = {"text": None, "photo_id": None, "video_id": None, "animation_id": None, "caption": None}

    if m.photo:
        media_data["photo_id"] = m.photo[-1].file_id
        media_data["caption"] = m.html_text if m.caption else ""
    elif m.video:
        media_data["video_id"] = m.video.file_id
        media_data["caption"] = m.html_text if m.caption else ""
    elif m.animation:
        media_data["animation_id"] = m.animation.file_id
        media_data["caption"] = m.html_text if m.caption else ""
    elif m.text:
        media_data["text"] = m.html_text
    else:
        await m.answer("<b>❌ Неподдерживаемый тип медиа. Используйте текст, фото, видео или GIF.</b>", parse_mode=ParseMode.HTML)
        return

    await state.update_data(media=media_data)

    text = (
        "<b>Шаг 2/2: Добавьте кнопки (до 3 штук).\n\n"
        "Формат - каждая кнопка с новой строки:\n"
        "<code>Текст кнопки - https://ссылка.com</code>\n\n"
        "Например:\n"
        "<code>Наш канал - https://t.me/mychannel\n"
        "Сайт - https://example.com</code>\n\n"
        "После этого бот спросит цвет кнопки (кнопка станет реально цветной) и предложит добавить премиум-эмодзи-иконку.\n\n"
        "Если кнопки не нужны - напишите <code>нет</code></b>"
    )
    await m.answer(text, parse_mode=ParseMode.HTML)
    await state.set_state(AdminStates.br_btn)


BUTTON_STYLES = {"blue": "primary", "red": "danger", "green": "success", "classic": None}


def _color_pick_kb(prefix: str = "br_color_"):
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="🔵 Синий", callback_data=f"{prefix}blue"),
        InlineKeyboardButton(text="🔴 Красный", callback_data=f"{prefix}red"),
    )
    kb.row(
        InlineKeyboardButton(text="🟢 Зелёный", callback_data=f"{prefix}green"),
        InlineKeyboardButton(text="⚪ Классик", callback_data=f"{prefix}classic"),
    )
    return kb.as_markup()


@dp.message(AdminStates.br_btn, F.from_user.id.in_(ADMIN_IDS))
async def adm_broadcast_got_buttons(m: types.Message, state: FSMContext):
    buttons = []

    if m.text.strip().lower() != "нет":
        lines = m.text.strip().split('\n')
        for line in lines[:3]:
            sep = '-' if '-' in line else ('—' if '—' in line else None)
            if not sep:
                continue
            btn_text, btn_url = line.split(sep, 1)
            btn_text, btn_url = btn_text.strip(), btn_url.strip()
            if btn_text and btn_url.startswith("http"):
                buttons.append({"text": btn_text, "url": btn_url, "style": None, "icon": None})

    await state.update_data(buttons=buttons, color_idx=0)

    if not buttons:
        await _broadcast_show_preview(m, state)
        return

    await m.answer(
        f"<b>🎨 Кнопка 1/{len(buttons)}: «{buttons[0]['text']}»\nВыберите цвет:</b>",
        reply_markup=_color_pick_kb(),
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(AdminStates.br_color)


@dp.callback_query(F.data.startswith("br_color_"), F.from_user.id.in_(ADMIN_IDS))
async def adm_broadcast_pick_color(c: types.CallbackQuery, state: FSMContext):
    color_key = c.data[len("br_color_"):]

    data = await state.get_data()
    buttons = data.get("buttons", [])
    idx = data.get("color_idx", 0)

    if idx >= len(buttons):
        await c.answer()
        return

    buttons[idx]["style"] = BUTTON_STYLES.get(color_key)
    await state.update_data(buttons=buttons)
    await c.answer()

    await c.message.edit_text(
        f"<b>💎 Кнопка {idx + 1}/{len(buttons)}: «{buttons[idx]['text']}»\n"
        f"Пришлите премиум-эмодзи для иконки кнопки, или напишите <code>нет</code>:</b>",
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(AdminStates.br_icon)


@dp.message(AdminStates.br_icon, F.from_user.id.in_(ADMIN_IDS))
async def adm_broadcast_got_icon(m: types.Message, state: FSMContext):
    data = await state.get_data()
    buttons = data.get("buttons", [])
    idx = data.get("color_idx", 0)

    if idx >= len(buttons):
        await state.clear()
        return

    icon_id = None
    skip = bool(m.text) and m.text.strip().lower() == "нет"
    if not skip:
        for ent in (m.entities or []):
            if ent.type == "custom_emoji":
                icon_id = ent.custom_emoji_id
                break
        if icon_id is None:
            await m.answer(
                "<b>❌ Не нашла премиум-эмодзи в сообщении. Пришлите эмодзи ещё раз или напишите нет.</b>",
                parse_mode=ParseMode.HTML,
            )
            return

    buttons[idx]["icon"] = icon_id
    idx += 1
    await state.update_data(buttons=buttons, color_idx=idx)

    if idx < len(buttons):
        await m.answer(
            f"<b>🎨 Кнопка {idx + 1}/{len(buttons)}: «{buttons[idx]['text']}»\nВыберите цвет:</b>",
            reply_markup=_color_pick_kb(),
            parse_mode=ParseMode.HTML,
        )
        await state.set_state(AdminStates.br_color)
    else:
        await _broadcast_show_preview(m, state)


async def _broadcast_show_preview(m: types.Message, state: FSMContext):
    data = await state.get_data()
    media = data["media"]
    buttons = data.get("buttons", [])

    preview_kb = InlineKeyboardBuilder()
    for btn in buttons:
        preview_kb.row(InlineKeyboardButton(
            text=btn["text"], url=btn["url"], style=btn.get("style"), icon_custom_emoji_id=btn.get("icon"),
        ))

    preview_markup = preview_kb.as_markup() if buttons else None

    await m.answer("<b>👁 Предпросмотр рассылки:</b>", parse_mode=ParseMode.HTML)

    try:
        if media["photo_id"]:
            await m.answer_photo(media["photo_id"], caption=media["caption"] or None, reply_markup=preview_markup, parse_mode=ParseMode.HTML)
        elif media["video_id"]:
            await m.answer_video(media["video_id"], caption=media["caption"] or None, reply_markup=preview_markup, parse_mode=ParseMode.HTML)
        elif media["animation_id"]:
            await m.answer_animation(media["animation_id"], caption=media["caption"] or None, reply_markup=preview_markup, parse_mode=ParseMode.HTML)
        else:
            await m.answer(media["text"], reply_markup=preview_markup, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception as e:
        await m.answer(f"<b>❌ Ошибка предпросмотра: {e}</b>", parse_mode=ParseMode.HTML)
        await state.clear()
        return

    users_count = db.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    confirm_kb = InlineKeyboardBuilder()
    confirm_kb.row(
        InlineKeyboardButton(text=f"✅ Отправить ({users_count} чел.)", callback_data="br_confirm_yes"),
        InlineKeyboardButton(text="❌ Отменить", callback_data="br_confirm_no"),
    )

    await m.answer(
        f"<b>Отправить рассылку {users_count} пользователям?</b>",
        reply_markup=confirm_kb.as_markup(),
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(AdminStates.br_confirm)


@dp.callback_query(F.data == "br_confirm_no", F.from_user.id.in_(ADMIN_IDS))
async def adm_broadcast_cancel(c: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text("<b>❌ Рассылка отменена.</b>", reply_markup=None, parse_mode=ParseMode.HTML)
    await c.answer()


@dp.callback_query(F.data == "br_confirm_yes", F.from_user.id.in_(ADMIN_IDS))
async def adm_broadcast_send(c: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()

    media = data.get("media", {})
    buttons = data.get("buttons", [])

    has_placeholders = "{" in (media.get("caption") or "") or "{" in (media.get("text") or "")
    has_btn_placeholders = any("{" in btn["text"] for btn in buttons)

    static_markup = None
    if buttons and not has_btn_placeholders:
        kb = InlineKeyboardBuilder()
        for btn in buttons:
            kb.row(InlineKeyboardButton(
                text=btn["text"], url=btn["url"], style=btn.get("style"), icon_custom_emoji_id=btn.get("icon"),
            ))
        static_markup = kb.as_markup()

    users = db.conn.execute("SELECT tg_id FROM users").fetchall()
    total = len(users)

    await c.message.edit_text(f"<b>🚀 Рассылка запущена... (0/{total})</b>", reply_markup=None, parse_mode=ParseMode.HTML)
    await c.answer()

    count_ok = 0
    count_fail = 0

    for i, u in enumerate(users):
        uid = u[0]
        try:
            caption = (await _apply_placeholders(media.get("caption") or "", uid)) or None if has_placeholders else (media.get("caption") or None)
            text = (await _apply_placeholders(media.get("text") or "", uid)) if has_placeholders else media.get("text")

            if has_btn_placeholders:
                kb = InlineKeyboardBuilder()
                for btn in buttons:
                    btn_text = await _apply_placeholders(btn["text"], uid)
                    kb.row(InlineKeyboardButton(
                        text=btn_text, url=btn["url"], style=btn.get("style"), icon_custom_emoji_id=btn.get("icon"),
                    ))
                markup = kb.as_markup()
            else:
                markup = static_markup

            if media.get("photo_id"):
                await bot_client.send_photo(uid, media["photo_id"], caption=caption, reply_markup=markup, parse_mode=ParseMode.HTML)
            elif media.get("video_id"):
                await bot_client.send_video(uid, media["video_id"], caption=caption, reply_markup=markup, parse_mode=ParseMode.HTML)
            elif media.get("animation_id"):
                await bot_client.send_animation(uid, media["animation_id"], caption=caption, reply_markup=markup, parse_mode=ParseMode.HTML)
            else:
                await bot_client.send_message(uid, text, reply_markup=markup, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            count_ok += 1
        except Exception:
            count_fail += 1

        await asyncio.sleep(0.05)

        if (i + 1) % 50 == 0:
            try:
                await c.message.edit_text(f"<b>🚀 Рассылка... ({i + 1}/{total})</b>", parse_mode=ParseMode.HTML)
            except Exception:
                pass

    await c.message.edit_text(
        f"<b>✅ Рассылка завершена!\n\n📤 Отправлено: {count_ok}\n❌ Не доставлено: {count_fail}</b>",
        reply_markup=None,
        parse_mode=ParseMode.HTML,
    )


# 👤 Управление пользователями (список, поиск, фильтр по бану)
USERS_PER_PAGE = 10


def _format_visit_detail(visit, title: str = "🌐 Данные захода в мини-апп") -> str:
    loc_parts = [p for p in (visit['city'], visit['region'], visit['country']) if p]
    loc = ", ".join(loc_parts) if loc_parts else "—"
    mark = "✅" if visit['verified'] else "⚠️ не подтв."
    premium_mark = "⭐ Premium" if visit['is_premium'] else "обычный"
    pm_mark = "да" if visit['allows_write_to_pm'] else "нет"
    return (
        f"<b>{title}</b>\n"
        f"📍 Локация: {loc}\n"
        f"🏢 Провайдер: {visit['isp'] or '—'}\n"
        f"🖥 IP: <code>{visit['ip'] or '—'}</code>\n"
        f"📱 Платформа: {visit['platform'] or '—'} | {visit['color_scheme'] or '—'} | Tg {visit['tg_version'] or '—'}\n"
        f"📟 Точная модель: {visit['ua_model'] or '— (недоступно на этой ОС/браузере)'}\n"
        f"💻 ОС/браузер: {visit['user_platform'] or '—'} | {visit['vendor'] or '—'} | ОС-версия: {visit['ua_platform_version'] or '—'}\n"
        f"🧠 Память: {visit['device_memory'] or '—'} ГБ | Ядер CPU: {visit['cpu_cores'] or '—'}\n"
        f"👆 Тачпоинты: {visit['touch_points'] or '0'} | Пиксель-ратио: {visit['pixel_ratio'] or '—'}\n"
        f"🖼 Экран: {visit['screen'] or '—'} (доступно: {visit['avail_screen'] or '—'})\n"
        f"📶 Соединение: {visit['connection_type'] or '—'}\n"
        f"🌍 Часовой пояс: {visit['timezone'] or '—'}\n"
        f"🗣 Язык браузера: {visit['language'] or '—'} ({visit['languages'] or '—'})\n"
        f"💬 Язык Telegram: {visit['tg_language_code'] or '—'}\n"
        f"👑 Telegram Premium: {premium_mark}\n"
        f"✉️ Разрешил ЛС: {pm_mark}\n"
        f"↩️ Реферер: {visit['referrer'] or '—'}\n"
        f"🔒 Подлинность: {mark}\n"
        f"🕐 Время захода (МСК): {visit['created_at']}"
    )


def get_visits_by_tgid(tg_id: int, limit: int = 15):
    return db.conn.execute(
        "SELECT * FROM visit_log WHERE tg_id = ? ORDER BY id DESC LIMIT ?", (tg_id, limit)
    ).fetchall()


def count_visits_by_tgid(tg_id: int) -> int:
    return db.conn.execute("SELECT COUNT(*) FROM visit_log WHERE tg_id = ?", (tg_id,)).fetchone()[0]


def get_visit_by_id(visit_id: int):
    return db.conn.execute("SELECT * FROM visit_log WHERE id = ?", (visit_id,)).fetchone()


def _unique_devices_by_tgid(tg_id: int, scan_limit: int = 300):
    rows = get_visits_by_tgid(tg_id, limit=scan_limit)
    groups = {}
    order = []
    for v in rows:
        key = (v['platform'] or '', v['user_platform'] or '', v['vendor'] or '', v['ua_model'] or '')
        if key not in groups:
            groups[key] = {"latest": v, "count": 1}
            order.append(key)
        else:
            groups[key]["count"] += 1
    return [groups[k] for k in order]


def _devices_list_render(tgid: int, make_open_cb, back_cb: str):
    groups = _unique_devices_by_tgid(tgid)
    kb = InlineKeyboardBuilder()
    for i, g in enumerate(groups, 1):
        v = g["latest"]
        model = v['ua_model'] or v['user_platform'] or v['platform'] or '—'
        loc_parts = [p for p in (v['city'], v['country']) if p]
        loc = ", ".join(loc_parts) if loc_parts else '—'
        label = f"{i}. {model} | {loc} | заходов: {g['count']} | посл.: {v['created_at']}"
        kb.row(InlineKeyboardButton(text=label[:64], callback_data=make_open_cb(v['id'])))
    kb.row(InlineKeyboardButton(text="⬅️ Назад к профилю", callback_data=back_cb))
    text = f"<b>📱 Устройства пользователя {tgid}</b>\n\nВсего уникальных устройств: {len(groups)}"
    return text, kb.as_markup()


def _recent_visits_render(tgid: int, make_open_cb, back_cb: str):
    visits = get_visits_by_tgid(tgid, limit=15)
    total = count_visits_by_tgid(tgid)
    kb = InlineKeyboardBuilder()
    for i, v in enumerate(visits, 1):
        model = v['ua_model'] or v['user_platform'] or v['platform'] or '—'
        loc_parts = [p for p in (v['city'], v['country']) if p]
        loc = ", ".join(loc_parts) if loc_parts else '—'
        label = f"{i}. {model} | {loc} | {v['created_at']}"
        kb.row(InlineKeyboardButton(text=label[:60], callback_data=make_open_cb(v['id'])))
    kb.row(InlineKeyboardButton(text="⬅️ Назад к профилю", callback_data=back_cb))
    text = f"<b>🕐 Последние заходы пользователя {tgid}</b>\n\nВсего заходов: {total}"
    if total > len(visits):
        text += f"\nПоказаны последние {len(visits)}."
    return text, kb.as_markup()


def _admin_profile_text(u, tsks) -> str:
    ad_label = "— (обычный переход)"
    if u['ad_source']:
        link = get_ad_link(u['ad_source'])
        ad_label = link['name'] if link else u['ad_source']
    role_label = ""
    if u['tg_id'] in ROOT_OWNER_IDS:
        role_label = "\n👑 Роль: настоящий владелец"
    elif u['tg_id'] in OWNER_IDS:
        role_label = "\n👑 Роль: со-владелец (полный доступ)"
    elif get_sub_admin_panel(u['tg_id']):
        panel = get_sub_admin_panel(u['tg_id'])
        role_label = f"\n🛠 Роль: саб-админ панели «{panel['name']}»"

    visit = get_last_visit(u['tg_id'])
    if visit:
        visit_block = f"\n{'━' * 20}\n" + _format_visit_detail(visit, "🌐 Данные последнего захода в мини-апп")
    else:
        visit_block = f"\n{'━' * 20}\n<b>🌐 Мини-апп:</b> ещё не заходил"

    return (
        f"<b>👤 Профиль пользователя</b>\n"
        f"{'━' * 20}\n"
        f"🆔 ID: <code>{u['tg_id']}</code>\n"
        f"📛 Юзернейм: @{u['username'] or '—'}\n"
        f"🌐 Язык: {u['language_code'] or '—'}\n"
        f"⭐ Баланс: {fmt(u['stars'])}\n"
        f"💰 Заработано всего: {fmt(u['earned'])}\n"
        f"📈 Заработано сегодня: {fmt(earned_today(u['tg_id']))}\n"
        f"👥 Рефералы всего: {u['refs']}\n"
        f"👥 Рефералов сегодня: {refs_today_count(u['tg_id'])}\n"
        f"📤 Выводов: {u['withdrawals_count']}\n"
        f"📚 Заданий: {tsks}\n"
        f"⛔ Бан: {'Да' if u['is_banned'] else 'Нет'}\n"
        f"📢 Пришёл по рекламе: {ad_label}"
        f"{role_label}\n"
        f"📅 Регистрация: {u['reg_date']}\n"
        f"⏱ Последняя активность: {u['last_active']}"
        f"{visit_block}"
    )


def _user_row_label(u) -> str:
    name = f"@{u['username']}" if u['username'] and u['username'] != "без_имени" else f"id{u['tg_id']}"
    return f"{name} | {u['tg_id']}"


ADS_USERS_PER_PAGE = 10


def render_ad_users_list(page: int, ad_code: str):
    is_all = ad_code == "__all__"
    where = "WHERE ad_source IS NOT NULL" if is_all else "WHERE ad_source = ?"
    params = () if is_all else (ad_code,)

    total = db.conn.execute(f"SELECT COUNT(*) FROM users {where}", params).fetchone()[0]
    total_pages = max(1, (total + ADS_USERS_PER_PAGE - 1) // ADS_USERS_PER_PAGE)
    page = min(max(1, page), total_pages)
    offset = (page - 1) * ADS_USERS_PER_PAGE

    rows = db.conn.execute(
        f"SELECT * FROM users {where} ORDER BY tg_id DESC LIMIT ? OFFSET ?",
        params + (ADS_USERS_PER_PAGE, offset),
    ).fetchall()

    kb = InlineKeyboardBuilder()
    for u in rows:
        kb.row(InlineKeyboardButton(text=_user_row_label(u), callback_data=f"aduser_open_{u['tg_id']}_{page}_{ad_code}"))
    kb.row(
        InlineKeyboardButton(text="«", callback_data=f"aduser_page_{page - 1}_{ad_code}"),
        InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="noop"),
        InlineKeyboardButton(text="»", callback_data=f"aduser_page_{page + 1}_{ad_code}"),
    )
    back_cb = "a_ads" if is_all else f"ads_open_{ad_code}"
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb))

    if is_all:
        title = "по всем рекламным ссылкам"
    else:
        link = get_ad_link(ad_code)
        title = f"по ссылке «{link['name']}»" if link else f"по ссылке «{ad_code}»"
    text = f"<b>👥 Пользователи {title}</b>\n\nВсего: <b>{total}</b>"
    if not rows:
        text += "\n\nСписок пуст."
    return text, kb.as_markup()


def _is_removable_admin(tgid: int) -> bool:
    return tgid not in ROOT_OWNER_IDS and (bool(get_sub_admin_panel(tgid)) or tgid in OWNER_IDS)


def _ad_profile_kb(tgid: int, page: int, ad_code: str, viewer_id: int = None):
    u = db.get_user(tgid)
    is_banned = bool(u["is_banned"]) if u else False
    ban_label = "✅ Разбанить" if is_banned else "🚫 Забанить"
    new_ban = 0 if is_banned else 1
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text=ban_label, callback_data=f"aduact_ban_{tgid}_{page}_{new_ban}_{ad_code}"),
        InlineKeyboardButton(text="💰 Баланс", callback_data=f"aduact_bal_{tgid}_{page}_{ad_code}"),
    )
    kb.row(InlineKeyboardButton(text="🏆 VIP", callback_data=f"vipsetlistad_{tgid}_{page}_{ad_code}"))
    visits_count = count_visits_by_tgid(tgid)
    if visits_count > 1:
        devices_count = len(_unique_devices_by_tgid(tgid))
        kb.row(
            InlineKeyboardButton(text=f"📱 Устройства ({devices_count})", callback_data=f"devlistad_{tgid}_{page}_{ad_code}"),
            InlineKeyboardButton(text=f"🕐 Заходы ({visits_count})", callback_data=f"visitlistad_{tgid}_{page}_{ad_code}"),
        )
    if viewer_id in ROOT_OWNER_IDS and _is_removable_admin(tgid):
        kb.row(InlineKeyboardButton(text="🚫 Снять администратора", callback_data=f"rmadminad_{tgid}_{page}_{ad_code}"))
    kb.row(InlineKeyboardButton(text="⬅️ К списку", callback_data=f"aduser_page_{page}_{ad_code}"))
    return kb.as_markup()


@dp.callback_query(F.data.startswith("aduser_page_"), F.from_user.id.in_(ADMIN_IDS))
async def aduser_page_cb(c: types.CallbackQuery):
    rest = c.data[len("aduser_page_"):]
    page_str, ad_code = rest.split("_", 1)
    text, kb = render_ad_users_list(int(page_str), ad_code)
    try:
        await c.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception:
        pass
    await c.answer()


@dp.callback_query(F.data.startswith("aduser_open_"), F.from_user.id.in_(ADMIN_IDS))
async def aduser_open_cb(c: types.CallbackQuery):
    rest = c.data[len("aduser_open_"):]
    tgid_str, page_str, ad_code = rest.split("_", 2)
    tgid, page = int(tgid_str), int(page_str)
    u = db.get_user(tgid)
    if not u:
        await c.answer("Пользователь не найден", show_alert=True)
        return
    tsks = db.conn.execute("SELECT COUNT(*) FROM completed_tasks WHERE tg_id=?", (tgid,)).fetchone()[0]
    await c.message.edit_text(
        _admin_profile_text(u, tsks), reply_markup=_ad_profile_kb(tgid, page, ad_code, viewer_id=c.from_user.id), parse_mode=ParseMode.HTML
    )
    await c.answer()


@dp.callback_query(F.data.startswith("aduact_ban_"), F.from_user.id.in_(ADMIN_IDS))
async def aduact_ban_cb(c: types.CallbackQuery):
    if not has_leaf_permission(c.from_user.id, "stats.ban"):
        return await c.answer("❌ Нет доступа к бану/разбану", show_alert=True)
    rest = c.data[len("aduact_ban_"):]
    tgid_str, page_str, newban_str, ad_code = rest.split("_", 3)
    tgid, page, new_ban = int(tgid_str), int(page_str), int(newban_str)
    if new_ban and tgid in ADMIN_IDS:
        await c.answer("❌ Нельзя забанить администратора", show_alert=True)
        return
    db.conn.execute("UPDATE users SET is_banned=? WHERE tg_id=?", (new_ban, tgid))
    db.conn.commit()
    try:
        msg = "<b>🚫 Ваш аккаунт заблокирован!</b>" if new_ban else "<b>✅ Ваш аккаунт разблокирован!</b>"
        await bot_client.send_message(tgid, msg, parse_mode=ParseMode.HTML)
    except Exception:
        pass
    u = db.get_user(tgid)
    tsks = db.conn.execute("SELECT COUNT(*) FROM completed_tasks WHERE tg_id=?", (tgid,)).fetchone()[0]
    await c.message.edit_text(
        _admin_profile_text(u, tsks), reply_markup=_ad_profile_kb(tgid, page, ad_code, viewer_id=c.from_user.id), parse_mode=ParseMode.HTML
    )
    await c.answer("✅ Забанен" if new_ban else "✅ Разбанен")


@dp.callback_query(F.data.startswith("aduact_bal_"), F.from_user.id.in_(ADMIN_IDS))
async def aduact_bal_cb(c: types.CallbackQuery, state: FSMContext):
    if not has_leaf_permission(c.from_user.id, "stats.balance"):
        return await c.answer("❌ Нет доступа к изменению баланса", show_alert=True)
    rest = c.data[len("aduact_bal_"):]
    tgid_str, page_str, ad_code = rest.split("_", 2)
    await state.update_data(bal_uid=int(tgid_str), bal_page=int(page_str), bal_context="ad", bal_code=ad_code)
    await state.set_state(AdminStates.u_bal_quick)
    await c.message.answer(
        f"<b>💰 Введите сумму для {tgid_str} (с минусом для списания), например: 50 или -50</b>",
        parse_mode=ParseMode.HTML,
    )
    await c.answer()


def render_user_list(page: int, only_banned: bool):
    where = "WHERE is_banned=1" if only_banned else ""
    total = db.conn.execute(f"SELECT COUNT(*) FROM users {where}").fetchone()[0]
    total_pages = max(1, (total + USERS_PER_PAGE - 1) // USERS_PER_PAGE)
    page = min(max(1, page), total_pages)
    offset = (page - 1) * USERS_PER_PAGE

    rows = db.conn.execute(
        f"SELECT * FROM users {where} ORDER BY tg_id DESC LIMIT ? OFFSET ?",
        (USERS_PER_PAGE, offset),
    ).fetchall()

    flag = int(only_banned)
    kb = InlineKeyboardBuilder()
    for u in rows:
        kb.row(InlineKeyboardButton(text=_user_row_label(u), callback_data=f"ulist_open_{u['tg_id']}_{page}_{flag}"))

    kb.row(
        InlineKeyboardButton(text="«", callback_data=f"ulist_page_{page - 1}_{flag}"),
        InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="noop"),
        InlineKeyboardButton(text="»", callback_data=f"ulist_page_{page + 1}_{flag}"),
    )
    kb.row(InlineKeyboardButton(text="🔍 Поиск по username/ID", callback_data="ulist_search"))
    ban_label = "✅ Только заблокированные" if only_banned else "🚫 Только заблокированные"
    kb.row(InlineKeyboardButton(text=ban_label, callback_data=f"ulist_filt_{int(not only_banned)}"))
    kb.row(InlineKeyboardButton(text="‹ Назад", callback_data="a_menu"))

    scope = "заблокированных" if only_banned else "перешедших по боту"
    text = (
        f"<b>👥 Управление пользователями вашего бота</b>\n\n"
        f"Всего {scope}: <b>{total}</b>\n"
        f"Выберите пользователя для просмотра."
    )
    if not rows:
        text = f"<b>👥 Управление пользователями</b>\n\nСписок пуст."
    return text, kb.as_markup()


def _profile_kb(tgid: int, page: int, only_banned: bool, is_banned: bool, viewer_id: int = None):
    flag = int(only_banned)
    ban_label = "✅ Разбанить" if is_banned else "🚫 Забанить"
    new_ban = 0 if is_banned else 1
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text=ban_label, callback_data=f"uact_ban_{tgid}_{page}_{flag}_{new_ban}"),
        InlineKeyboardButton(text="💰 Баланс", callback_data=f"uact_bal_{tgid}_{page}_{flag}"),
    )
    kb.row(InlineKeyboardButton(text="👥 Все рефералы", callback_data=f"admin_show_refs_{tgid}_{page}_{flag}"))
    kb.row(InlineKeyboardButton(text="🏆 VIP", callback_data=f"vipsetlist_{tgid}_{page}_{flag}"))
    visits_count = count_visits_by_tgid(tgid)
    if visits_count > 1:
        devices_count = len(_unique_devices_by_tgid(tgid))
        kb.row(
            InlineKeyboardButton(text=f"📱 Устройства ({devices_count})", callback_data=f"devlist_{tgid}_{page}_{flag}"),
            InlineKeyboardButton(text=f"🕐 Заходы ({visits_count})", callback_data=f"visitlist_{tgid}_{page}_{flag}"),
        )
    if viewer_id in ROOT_OWNER_IDS and _is_removable_admin(tgid):
        kb.row(InlineKeyboardButton(text="🚫 Снять администратора", callback_data=f"rmadmin_{tgid}_{page}_{flag}"))
    kb.row(InlineKeyboardButton(text="⬅️ К списку", callback_data=f"ulist_page_{page}_{flag}"))
    return kb.as_markup()


def _vip_pick_kb(prefix: str, tgid: int, rest: str):
    kb = InlineKeyboardBuilder()
    for lvl in get_vip_levels():
        kb.row(InlineKeyboardButton(text=f"{lvl['name']} (x{lvl['multiplier']:g})", callback_data=f"{prefix}_{tgid}_{lvl['id']}_{rest}"))
    kb.row(InlineKeyboardButton(text="🔄 Автоматически (по заработку)", callback_data=f"{prefix}_{tgid}_0_{rest}"))
    return kb.as_markup()


@dp.callback_query(F.data.startswith("vipsetlist_"), F.from_user.id.in_(ADMIN_IDS))
async def adm_vipsetlist(c: types.CallbackQuery):
    rest = c.data[len("vipsetlist_"):]
    tgid_str, page_str, flag_str = rest.split("_")
    await c.message.answer(
        "<b>🏆 Выберите VIP-уровень для пользователя (или сброс на автоматический):</b>",
        reply_markup=_vip_pick_kb("vipset", int(tgid_str), f"{page_str}_{flag_str}"),
        parse_mode=ParseMode.HTML,
    )
    await c.answer()


@dp.callback_query(F.data.startswith("vipset_"), F.from_user.id.in_(ADMIN_IDS))
async def adm_vipset(c: types.CallbackQuery):
    rest = c.data[len("vipset_"):]
    tgid_str, level_id_str, page_str, flag_str = rest.split("_")
    tgid, level_id, page, flag = int(tgid_str), int(level_id_str), int(page_str), int(flag_str)
    set_user_vip_override(tgid, level_id if level_id else None)
    u = db.get_user(tgid)
    tsks = db.conn.execute("SELECT COUNT(*) FROM completed_tasks WHERE tg_id=?", (tgid,)).fetchone()[0]
    await c.message.edit_text(
        _admin_profile_text(u, tsks),
        reply_markup=_profile_kb(tgid, page, bool(flag), bool(u['is_banned']), viewer_id=c.from_user.id),
        parse_mode=ParseMode.HTML,
    )
    await c.answer("✅ Уровень назначен" if level_id else "✅ Сброшено на автоматический")


@dp.callback_query(F.data.startswith("vipsetlistad_"), F.from_user.id.in_(ADMIN_IDS))
async def adm_vipsetlistad(c: types.CallbackQuery):
    rest = c.data[len("vipsetlistad_"):]
    tgid_str, page_str, ad_code = rest.split("_", 2)
    await c.message.answer(
        "<b>🏆 Выберите VIP-уровень для пользователя (или сброс на автоматический):</b>",
        reply_markup=_vip_pick_kb("vipsetad", int(tgid_str), f"{page_str}_{ad_code}"),
        parse_mode=ParseMode.HTML,
    )
    await c.answer()


@dp.callback_query(F.data.startswith("vipsetad_"), F.from_user.id.in_(ADMIN_IDS))
async def adm_vipsetad(c: types.CallbackQuery):
    rest = c.data[len("vipsetad_"):]
    tgid_str, level_id_str, page_str, ad_code = rest.split("_", 3)
    tgid, level_id, page = int(tgid_str), int(level_id_str), int(page_str)
    set_user_vip_override(tgid, level_id if level_id else None)
    u = db.get_user(tgid)
    tsks = db.conn.execute("SELECT COUNT(*) FROM completed_tasks WHERE tg_id=?", (tgid,)).fetchone()[0]
    await c.message.edit_text(
        _admin_profile_text(u, tsks),
        reply_markup=_ad_profile_kb(tgid, page, ad_code, viewer_id=c.from_user.id),
        parse_mode=ParseMode.HTML,
    )
    await c.answer("✅ Уровень назначен" if level_id else "✅ Сброшено на автоматический")


def ustat_kb():
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="👤 Профиль (список/поиск)", callback_data="a_uprof"))
    kb.row(InlineKeyboardButton(text="📊 Общая статистика", callback_data="a_stat"))
    kb.row(InlineKeyboardButton(text="🏆 Топ 10", callback_data="a_top"))
    kb.row(InlineKeyboardButton(text="‹ Назад", callback_data="a_menu"))
    return kb.as_markup()


@dp.callback_query(F.data == "a_ustat", F.from_user.id.in_(ADMIN_IDS))
async def adm_ustat(c: types.CallbackQuery):
    text = "<b>📊 Статистика пользователей</b>\n\nВыберите раздел:"
    try:
        await c.message.edit_text(text, reply_markup=ustat_kb(), parse_mode=ParseMode.HTML)
    except Exception:
        await c.message.answer(text, reply_markup=ustat_kb(), parse_mode=ParseMode.HTML)
    await c.answer()


@dp.callback_query(F.data == "a_uprof", F.from_user.id.in_(ADMIN_IDS))
async def adm_uprof(c: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text, kb = render_user_list(1, False)
    await c.message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    await c.answer()


@dp.callback_query(F.data == "a_menu", F.from_user.id.in_(ADMIN_IDS))
async def adm_menu_back(c: types.CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await c.message.edit_text("<b>🛠 В админ-панели</b>", reply_markup=almaz_panel_kb(c.from_user.id), parse_mode=ParseMode.HTML)
    except Exception:
        await c.message.answer("<b>🛠 В админ-панели</b>", reply_markup=almaz_panel_kb(c.from_user.id), parse_mode=ParseMode.HTML)
    await c.answer()


@dp.callback_query(F.data == "noop")
async def noop_cb(c: types.CallbackQuery):
    await c.answer()


@dp.callback_query(F.data.startswith("ulist_page_"), F.from_user.id.in_(ADMIN_IDS))
async def ulist_page_cb(c: types.CallbackQuery):
    parts = c.data.split("_")
    page, flag = int(parts[2]), bool(int(parts[3]))
    text, kb = render_user_list(page, flag)
    try:
        await c.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception:
        pass
    await c.answer()


@dp.callback_query(F.data.startswith("ulist_filt_"), F.from_user.id.in_(ADMIN_IDS))
async def ulist_filt_cb(c: types.CallbackQuery):
    flag = bool(int(c.data.split("_")[2]))
    text, kb = render_user_list(1, flag)
    try:
        await c.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception:
        pass
    await c.answer()


@dp.callback_query(F.data.startswith("ulist_open_"), F.from_user.id.in_(ADMIN_IDS))
async def ulist_open_cb(c: types.CallbackQuery):
    parts = c.data.split("_")
    tgid, page, flag = int(parts[2]), int(parts[3]), bool(int(parts[4]))
    u = db.get_user(tgid)
    if not u:
        await c.answer("Пользователь не найден", show_alert=True)
        return
    tsks = db.conn.execute("SELECT COUNT(*) FROM completed_tasks WHERE tg_id=?", (tgid,)).fetchone()[0]
    try:
        await c.message.edit_text(
            _admin_profile_text(u, tsks), reply_markup=_profile_kb(tgid, page, flag, bool(u['is_banned']), viewer_id=c.from_user.id), parse_mode=ParseMode.HTML
        )
    except Exception:
        await c.message.answer(
            _admin_profile_text(u, tsks), reply_markup=_profile_kb(tgid, page, flag, bool(u['is_banned']), viewer_id=c.from_user.id), parse_mode=ParseMode.HTML
        )
    await c.answer()


@dp.callback_query(F.data.startswith("uact_ban_"), F.from_user.id.in_(ADMIN_IDS))
async def uact_ban_cb(c: types.CallbackQuery):
    if not has_leaf_permission(c.from_user.id, "stats.ban"):
        return await c.answer("❌ Нет доступа к бану/разбану", show_alert=True)
    parts = c.data.split("_")
    tgid, page, flag, new_ban = int(parts[2]), int(parts[3]), int(parts[4]), int(parts[5])
    if new_ban and tgid in ADMIN_IDS:
        await c.answer("❌ Нельзя забанить администратора", show_alert=True)
        return
    db.conn.execute("UPDATE users SET is_banned=? WHERE tg_id=?", (new_ban, tgid))
    db.conn.commit()
    try:
        msg = "<b>🚫 Ваш аккаунт заблокирован!</b>" if new_ban else "<b>✅ Ваш аккаунт разблокирован!</b>"
        await bot_client.send_message(tgid, msg, parse_mode=ParseMode.HTML)
    except Exception:
        pass
    u = db.get_user(tgid)
    tsks = db.conn.execute("SELECT COUNT(*) FROM completed_tasks WHERE tg_id=?", (tgid,)).fetchone()[0]
    await c.message.edit_text(
        _admin_profile_text(u, tsks), reply_markup=_profile_kb(tgid, page, bool(flag), bool(u['is_banned']), viewer_id=c.from_user.id), parse_mode=ParseMode.HTML
    )
    await c.answer("✅ Забанен" if new_ban else "✅ Разбанен")


@dp.callback_query(F.data.startswith("uact_bal_"), F.from_user.id.in_(ADMIN_IDS))
async def uact_bal_cb(c: types.CallbackQuery, state: FSMContext):
    if not has_leaf_permission(c.from_user.id, "stats.balance"):
        return await c.answer("❌ Нет доступа к изменению баланса", show_alert=True)
    parts = c.data.split("_")
    tgid, page, flag = int(parts[2]), int(parts[3]), int(parts[4])
    await state.update_data(bal_uid=tgid, bal_page=page, bal_flag=flag)
    await state.set_state(AdminStates.u_bal_quick)
    await c.message.answer(
        f"<b>💰 Введите сумму для {tgid} (с минусом для списания), например: 50 или -50</b>",
        parse_mode=ParseMode.HTML,
    )
    await c.answer()


@dp.message(AdminStates.u_bal_quick, F.from_user.id.in_(ADMIN_IDS))
async def uact_bal_process(m: types.Message, state: FSMContext):
    data = await state.get_data()
    uid, page = data.get("bal_uid"), data.get("bal_page", 1)
    context = data.get("bal_context", "normal")
    try:
        amt = float(m.text.replace(",", ".").strip())
    except ValueError:
        return await m.answer("<b>❌ Введите число, например: 50 или -50</b>", parse_mode=ParseMode.HTML)
    db.conn.execute("UPDATE users SET stars = stars + ? WHERE tg_id = ?", (amt, uid))
    db.conn.commit()
    try:
        await bot_client.send_message(uid, f"💰 Ваш баланс изменён на {fmt(amt)}⭐", parse_mode=ParseMode.HTML)
    except Exception:
        pass
    await state.clear()
    u = db.get_user(uid)
    tsks = db.conn.execute("SELECT COUNT(*) FROM completed_tasks WHERE tg_id=?", (uid,)).fetchone()[0]
    if context == "ad":
        reply_markup = _ad_profile_kb(uid, page, data.get("bal_code", "__all__"), viewer_id=m.from_user.id)
    else:
        flag = data.get("bal_flag", 0)
        reply_markup = _profile_kb(uid, page, bool(flag), bool(u['is_banned']), viewer_id=m.from_user.id)
    await m.answer(
        f"<b>✅ Баланс {uid} изменён на {fmt(amt)}⭐</b>\n\n" + _admin_profile_text(u, tsks),
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML,
    )


@dp.callback_query(F.data == "ulist_search", F.from_user.id.in_(ADMIN_IDS))
async def ulist_search_start(c: types.CallbackQuery, state: FSMContext):
    await c.message.answer("<b>🔍 Введите username (можно без @) или ID:</b>", parse_mode=ParseMode.HTML)
    await state.set_state(AdminStates.u_prof)
    await c.answer()


@dp.message(AdminStates.u_prof, F.from_user.id.in_(ADMIN_IDS))
async def ulist_search_process(m: types.Message, state: FSMContext):
    await state.clear()
    q = m.text.strip().lstrip('@')

    if q.isdigit():
        rows = db.conn.execute(
            "SELECT * FROM users WHERE tg_id = ? OR username LIKE ? ORDER BY tg_id DESC LIMIT 15",
            (int(q), f"%{q}%"),
        ).fetchall()
    else:
        rows = db.conn.execute(
            "SELECT * FROM users WHERE username LIKE ? ORDER BY tg_id DESC LIMIT 15",
            (f"%{q}%",),
        ).fetchall()

    if not rows:
        return await m.answer("<b>❌ Ничего не найдено.</b>", parse_mode=ParseMode.HTML)

    kb = InlineKeyboardBuilder()
    for u in rows:
        kb.row(InlineKeyboardButton(text=_user_row_label(u), callback_data=f"ulist_open_{u['tg_id']}_1_0"))
    kb.row(InlineKeyboardButton(text="‹ Назад", callback_data="ulist_page_1_0"))
    await m.answer(
        f"<b>🔍 Найдено: {len(rows)}</b>", reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML
    )


@dp.callback_query(F.data.startswith("admin_show_refs_"), F.from_user.id.in_(ADMIN_IDS))
async def admin_show_refs_cb(c: types.CallbackQuery):
    parts = c.data.split("_")
    uid, page, flag = int(parts[3]), int(parts[4]), int(parts[5])
    refs = db.conn.execute(
        "SELECT tg_id, invite_time FROM users WHERE referrer_id = ? ORDER BY invite_time DESC",
        (uid,),
    ).fetchall()
    if not refs:
        await c.answer("У этого пользователя нет рефералов.", show_alert=True)
        return
    lines = []
    for r in refs:
        time_msk = to_msk_time(r['invite_time'])
        lines.append(f"• <code>{r['tg_id']}</code> — {time_msk} (МСК)")
    text = f"<b>👥 Рефералы пользователя {uid}:</b>\n\n" + "\n".join(lines)
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin_back_to_profile_{uid}_{page}_{flag}"))
    await c.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    await c.answer()


@dp.callback_query(F.data.startswith("admin_back_to_profile_"), F.from_user.id.in_(ADMIN_IDS))
async def admin_back_to_profile_cb(c: types.CallbackQuery):
    parts = c.data.split("_")
    uid, page, flag = int(parts[4]), int(parts[5]), bool(int(parts[6]))
    u = db.get_user(uid)
    if not u:
        await c.answer("Пользователь не найден", show_alert=True)
        return
    tsks = db.conn.execute("SELECT COUNT(*) FROM completed_tasks WHERE tg_id=?", (uid,)).fetchone()[0]
    await c.message.edit_text(
        _admin_profile_text(u, tsks), reply_markup=_profile_kb(uid, page, flag, bool(u['is_banned']), viewer_id=c.from_user.id), parse_mode=ParseMode.HTML
    )
    await c.answer()


@dp.callback_query(F.data.startswith("devlist_"), F.from_user.id.in_(ADMIN_IDS))
async def adm_devlist(c: types.CallbackQuery):
    rest = c.data[len("devlist_"):]
    tgid_str, page_str, flag_str = rest.split("_")
    tgid, page, flag = int(tgid_str), int(page_str), int(flag_str)
    text, kb = _devices_list_render(
        tgid,
        make_open_cb=lambda vid: f"devopen_{tgid}_{page}_{flag}_{vid}",
        back_cb=f"admin_back_to_profile_{tgid}_{page}_{flag}",
    )
    await c.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    await c.answer()


@dp.callback_query(F.data.startswith("devlistad_"), F.from_user.id.in_(ADMIN_IDS))
async def adm_devlistad(c: types.CallbackQuery):
    rest = c.data[len("devlistad_"):]
    tgid_str, page_str, ad_code = rest.split("_", 2)
    tgid, page = int(tgid_str), int(page_str)
    text, kb = _devices_list_render(
        tgid,
        make_open_cb=lambda vid: f"devopenad_{vid}_{tgid}_{page}_{ad_code}",
        back_cb=f"aduser_open_{tgid}_{page}_{ad_code}",
    )
    await c.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    await c.answer()


@dp.callback_query(F.data.startswith("devopen_"), F.from_user.id.in_(ADMIN_IDS))
async def adm_devopen(c: types.CallbackQuery):
    rest = c.data[len("devopen_"):]
    tgid_str, page_str, flag_str, vid_str = rest.split("_")
    tgid, page, flag, vid = int(tgid_str), int(page_str), int(flag_str), int(vid_str)
    visit = get_visit_by_id(vid)
    if not visit:
        return await c.answer("Запись не найдена", show_alert=True)
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="⬅️ Назад к списку", callback_data=f"devlist_{tgid}_{page}_{flag}"))
    await c.message.edit_text(_format_visit_detail(visit, "📱 Устройство"), reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)
    await c.answer()


@dp.callback_query(F.data.startswith("devopenad_"), F.from_user.id.in_(ADMIN_IDS))
async def adm_devopenad(c: types.CallbackQuery):
    rest = c.data[len("devopenad_"):]
    vid_str, tgid_str, page_str, ad_code = rest.split("_", 3)
    vid, tgid, page = int(vid_str), int(tgid_str), int(page_str)
    visit = get_visit_by_id(vid)
    if not visit:
        return await c.answer("Запись не найдена", show_alert=True)
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="⬅️ Назад к списку", callback_data=f"devlistad_{tgid}_{page}_{ad_code}"))
    await c.message.edit_text(_format_visit_detail(visit, "📱 Устройство"), reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)
    await c.answer()


@dp.callback_query(F.data.startswith("visitlist_"), F.from_user.id.in_(ADMIN_IDS))
async def adm_visitlist(c: types.CallbackQuery):
    rest = c.data[len("visitlist_"):]
    tgid_str, page_str, flag_str = rest.split("_")
    tgid, page, flag = int(tgid_str), int(page_str), int(flag_str)
    text, kb = _recent_visits_render(
        tgid,
        make_open_cb=lambda vid: f"visitopen_{tgid}_{page}_{flag}_{vid}",
        back_cb=f"admin_back_to_profile_{tgid}_{page}_{flag}",
    )
    await c.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    await c.answer()


@dp.callback_query(F.data.startswith("visitlistad_"), F.from_user.id.in_(ADMIN_IDS))
async def adm_visitlistad(c: types.CallbackQuery):
    rest = c.data[len("visitlistad_"):]
    tgid_str, page_str, ad_code = rest.split("_", 2)
    tgid, page = int(tgid_str), int(page_str)
    text, kb = _recent_visits_render(
        tgid,
        make_open_cb=lambda vid: f"visitopenad_{vid}_{tgid}_{page}_{ad_code}",
        back_cb=f"aduser_open_{tgid}_{page}_{ad_code}",
    )
    await c.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    await c.answer()


@dp.callback_query(F.data.startswith("visitopen_"), F.from_user.id.in_(ADMIN_IDS))
async def adm_visitopen(c: types.CallbackQuery):
    rest = c.data[len("visitopen_"):]
    tgid_str, page_str, flag_str, vid_str = rest.split("_")
    tgid, page, flag, vid = int(tgid_str), int(page_str), int(flag_str), int(vid_str)
    visit = get_visit_by_id(vid)
    if not visit:
        return await c.answer("Запись не найдена", show_alert=True)
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="⬅️ Назад к списку", callback_data=f"visitlist_{tgid}_{page}_{flag}"))
    await c.message.edit_text(_format_visit_detail(visit, "🕐 Заход в мини-апп"), reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)
    await c.answer()


@dp.callback_query(F.data.startswith("visitopenad_"), F.from_user.id.in_(ADMIN_IDS))
async def adm_visitopenad(c: types.CallbackQuery):
    rest = c.data[len("visitopenad_"):]
    vid_str, tgid_str, page_str, ad_code = rest.split("_", 3)
    vid, tgid, page = int(vid_str), int(tgid_str), int(page_str)
    visit = get_visit_by_id(vid)
    if not visit:
        return await c.answer("Запись не найдена", show_alert=True)
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="⬅️ Назад к списку", callback_data=f"visitlistad_{tgid}_{page}_{ad_code}"))
    await c.message.edit_text(_format_visit_detail(visit, "🕐 Заход в мини-апп"), reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)
    await c.answer()


@dp.callback_query(F.data.startswith("rmadmin_"), F.from_user.id.in_(ROOT_OWNER_IDS))
async def adm_remove_admin(c: types.CallbackQuery):
    rest = c.data[len("rmadmin_"):]
    tgid_str, page_str, flag_str = rest.split("_", 2)
    tgid, page, flag = int(tgid_str), int(page_str), int(flag_str)
    remove_sub_admin(tgid)
    remove_co_owner(tgid)
    u = db.get_user(tgid)
    tsks = db.conn.execute("SELECT COUNT(*) FROM completed_tasks WHERE tg_id=?", (tgid,)).fetchone()[0]
    await c.message.edit_text(
        f"<b>✅ Права администратора сняты.</b>\n\n" + _admin_profile_text(u, tsks),
        reply_markup=_profile_kb(tgid, page, bool(flag), bool(u['is_banned']), viewer_id=c.from_user.id),
        parse_mode=ParseMode.HTML,
    )
    await c.answer("Снято")


@dp.callback_query(F.data.startswith("rmadminad_"), F.from_user.id.in_(ROOT_OWNER_IDS))
async def adm_remove_admin_ad(c: types.CallbackQuery):
    rest = c.data[len("rmadminad_"):]
    tgid_str, page_str, ad_code = rest.split("_", 2)
    tgid, page = int(tgid_str), int(page_str)
    remove_sub_admin(tgid)
    remove_co_owner(tgid)
    u = db.get_user(tgid)
    tsks = db.conn.execute("SELECT COUNT(*) FROM completed_tasks WHERE tg_id=?", (tgid,)).fetchone()[0]
    await c.message.edit_text(
        f"<b>✅ Права администратора сняты.</b>\n\n" + _admin_profile_text(u, tsks),
        reply_markup=_ad_profile_kb(tgid, page, ad_code, viewer_id=c.from_user.id),
        parse_mode=ParseMode.HTML,
    )
    await c.answer("Снято")


# 📊 СТАТИСТИКА
@dp.callback_query(F.data == "a_stat", F.from_user.id.in_(ADMIN_IDS))
async def adm_stat(c: types.CallbackQuery):
    usrs = db.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    banned = db.conn.execute("SELECT COUNT(*) FROM users WHERE is_banned=1").fetchone()[0]
    wds = db.conn.execute("SELECT SUM(withdrawals_count) FROM users").fetchone()[0] or 0
    ernd = db.conn.execute("SELECT SUM(earned) FROM users").fetchone()[0] or 0
    tsks_done = db.conn.execute("SELECT COUNT(*) FROM completed_tasks").fetchone()[0]
    prm_used = db.conn.execute("SELECT COUNT(*) FROM used_promos").fetchone()[0]

    today = datetime.datetime.now().strftime("%d.%m.%Y")
    today_reg = db.conn.execute("SELECT COUNT(*) FROM users WHERE reg_date=?", (today,)).fetchone()[0]

    text = (
        f"<b>📊 Статистика бота\n"
        f"{'━' * 20}\n"
        f"👥 Всего пользователей: {usrs}\n"
        f"📈 Новых сегодня: {today_reg}\n"
        f"⛔ Заблокированных: {banned}\n\n"
        f"💰 Заработано юзерами: {fmt(ernd)}⭐\n"
        f"📤 Выводов всего: {wds}\n"
        f"📚 Заданий выполнено: {tsks_done}\n"
        f"🔑 Промокодов активировано: {prm_used}</b>"
    )
    await c.message.answer(text, parse_mode=ParseMode.HTML)
    await c.answer()


# 🏆 ТОП 10 (АДМИН)
@dp.callback_query(F.data == "a_top", F.from_user.id.in_(ADMIN_IDS))
async def adm_top(c: types.CallbackQuery):
    top = db.conn.execute(
        "SELECT tg_id, username, earned, refs, withdrawals_count FROM users ORDER BY earned DESC LIMIT 10"
    ).fetchall()
    text = "<b>🏆 Топ 10 по звёздам</b>\n\n"
    for i, r in enumerate(top, 1):
        tasks = db.conn.execute("SELECT COUNT(*) FROM completed_tasks WHERE tg_id = ?", (r['tg_id'],)).fetchone()[0]
        uname = f"@{r['username']}" if r['username'] else "—"
        text += (
            f"<b>{i}. <a href='tg://user?id={r['tg_id']}'>{r['tg_id']}</a> ({uname})</b>\n"
            f"⭐ {fmt(r['earned'])}  👥 {r['refs']}  📚 {tasks}  📤 {r['withdrawals_count']}\n\n"
        )
    await c.message.answer(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    await c.answer()


# ⚙️ УПРАВЛЕНИЕ ЛИМИТАМИ (объединённое подменю)
def limits_kb():
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="⚙️ Мин. рефералов для вывода", callback_data="a_mref"))
    kb.row(InlineKeyboardButton(text="🎁 Награда за реферала", callback_data="a_rew"))
    kb.row(InlineKeyboardButton(text="👥 Рефералов для ежедневки", callback_data="a_daily_refs"))
    kb.row(InlineKeyboardButton(text="🎁 Награда за ежедневку", callback_data="a_daily_reward"))
    kb.row(InlineKeyboardButton(text="‹ Назад", callback_data="a_menu"))
    return kb.as_markup()


@dp.callback_query(F.data == "a_limits", F.from_user.id.in_(ADMIN_IDS))
async def adm_limits(c: types.CallbackQuery):
    text = (
        "<b>⚙️ Управление лимитами</b>\n\n"
        f"👥 Мин. рефералов для вывода: <b>{db.get_setting('min_refs')}</b>\n"
        f"🎁 Награда за реферала: <b>{fmt(db.get_setting('ref_reward'))}⭐</b>\n"
        f"📅 Рефералов для ежедневки: <b>{db.get_setting('daily_refs')}</b>\n"
        f"🎁 Награда за ежедневку: <b>{fmt(db.get_setting('daily_reward'))}⭐</b>\n\n"
        "Выберите, что изменить:"
    )
    try:
        await c.message.edit_text(text, reply_markup=limits_kb(), parse_mode=ParseMode.HTML)
    except Exception:
        await c.message.answer(text, reply_markup=limits_kb(), parse_mode=ParseMode.HTML)
    await c.answer()


# ⚙️ МИН. РЕФЕРАЛОВ
@dp.callback_query(F.data == "a_mref", F.from_user.id.in_(ADMIN_IDS))
async def adm_mref(c: types.CallbackQuery, state: FSMContext):
    current = db.get_setting('min_refs')
    await c.message.answer(f"<b>Текущий минимум рефералов: {current}\nВведите новое значение:</b>", parse_mode=ParseMode.HTML)
    await state.set_state(AdminStates.set_min)
    await c.answer()


@dp.message(AdminStates.set_min, F.from_user.id.in_(ADMIN_IDS))
async def adm_mref_process(m: types.Message, state: FSMContext):
    if not m.text.isdigit():
        return await m.answer("<b>❌ Введите только цифры!</b>", parse_mode=ParseMode.HTML)
    db.conn.execute("UPDATE settings SET value=? WHERE key='min_refs'", (m.text,))
    db.conn.commit()
    await m.answer(f"<b>✅ Минимум рефералов: {m.text}</b>", parse_mode=ParseMode.HTML)
    await state.clear()


# 🎁 НАГРАДА ЗА РЕФЕРАЛА
@dp.callback_query(F.data == "a_rew", F.from_user.id.in_(ADMIN_IDS))
async def adm_rew(c: types.CallbackQuery, state: FSMContext):
    current = db.get_setting('ref_reward')
    await c.message.answer(f"<b>Текущая награда за реферала: {current}⭐\nВведите новую сумму:</b>", parse_mode=ParseMode.HTML)
    await state.set_state(AdminStates.set_rew)
    await c.answer()


@dp.message(AdminStates.set_rew, F.from_user.id.in_(ADMIN_IDS))
async def adm_rew_process(m: types.Message, state: FSMContext):
    try:
        val = float(m.text.replace(',', '.'))
        db.conn.execute("UPDATE settings SET value=? WHERE key='ref_reward'", (str(val),))
        db.conn.commit()
        await m.answer(f"<b>✅ Награда за реферала: {fmt(val)}⭐</b>", parse_mode=ParseMode.HTML)
    except Exception:
        await m.answer("<b>❌ Введите корректное число!</b>", parse_mode=ParseMode.HTML)
    await state.clear()


# 👥 Рефералы для ежедневки
@dp.callback_query(F.data == "a_daily_refs", F.from_user.id.in_(ADMIN_IDS))
async def adm_daily_refs(c: types.CallbackQuery, state: FSMContext):
    current = db.get_setting('daily_refs')
    await c.message.answer(f"<b>Текущее количество рефералов для бонуса: {current}\nВведите новое значение:</b>", parse_mode=ParseMode.HTML)
    await state.set_state(AdminStates.daily_refs)
    await c.answer()


@dp.message(AdminStates.daily_refs, F.from_user.id.in_(ADMIN_IDS))
async def adm_daily_refs_process(m: types.Message, state: FSMContext):
    if not m.text.isdigit():
        return await m.answer("<b>❌ Введите только цифры!</b>", parse_mode=ParseMode.HTML)
    val = int(m.text)
    if val < 1:
        return await m.answer("<b>❌ Минимум 1 реферал!</b>", parse_mode=ParseMode.HTML)
    db.conn.execute("UPDATE settings SET value=? WHERE key='daily_refs'", (str(val),))
    db.conn.commit()
    await m.answer(f"<b>✅ Теперь нужно {val} рефералов в день для бонуса</b>", parse_mode=ParseMode.HTML)
    await state.clear()


# 🎁 Награда за бонус
@dp.callback_query(F.data == "a_daily_reward", F.from_user.id.in_(ADMIN_IDS))
async def adm_daily_reward(c: types.CallbackQuery, state: FSMContext):
    current = db.get_setting('daily_reward')
    await c.message.answer(f"<b>Текущая награда за бонус: {current}⭐\nВведите новую сумму (мин. 0.1):</b>", parse_mode=ParseMode.HTML)
    await state.set_state(AdminStates.daily_reward)
    await c.answer()


@dp.message(AdminStates.daily_reward, F.from_user.id.in_(ADMIN_IDS))
async def adm_daily_reward_process(m: types.Message, state: FSMContext):
    try:
        val = float(m.text.replace(',', '.'))
        if val < 0.1:
            return await m.answer("<b>❌ Минимальная награда — 0.1⭐</b>", parse_mode=ParseMode.HTML)
        db.conn.execute("UPDATE settings SET value=? WHERE key='daily_reward'", (str(val),))
        db.conn.commit()
        await m.answer(f"<b>✅ Награда за бонус: {fmt(val)}⭐</b>", parse_mode=ParseMode.HTML)
    except Exception:
        await m.answer("<b>❌ Введите корректное число!</b>", parse_mode=ParseMode.HTML)
    await state.clear()


# 📁 Подменю: Промокоды / Задания / Спонсоры
def _pr_kb():
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🎟 Добавить промокод", callback_data="a_pr_add"))
    kb.row(InlineKeyboardButton(text="📋 Список промокодов", callback_data="a_pr_list"))
    kb.row(InlineKeyboardButton(text="❌ Удалить промокод", callback_data="a_pr_del"))
    kb.row(InlineKeyboardButton(text="‹ Назад", callback_data="a_menu"))
    return kb.as_markup()


def _ts_kb():
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="📚 Добавить задание", callback_data="a_ts_add"))
    kb.row(InlineKeyboardButton(text="📋 Список заданий", callback_data="a_ts_list"))
    kb.row(InlineKeyboardButton(text="❌ Удалить задание", callback_data="a_ts_del"))
    kb.row(InlineKeyboardButton(text="‹ Назад", callback_data="a_menu"))
    return kb.as_markup()


def _sp_kb():
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="📣 Добавить спонсора", callback_data="a_sp_add"))
    kb.row(InlineKeyboardButton(text="📋 Список спонсоров", callback_data="a_sp_list"))
    kb.row(InlineKeyboardButton(text="❌ Удалить спонсора", callback_data="a_sp_del"))
    kb.row(InlineKeyboardButton(text="‹ Назад", callback_data="a_menu"))
    return kb.as_markup()


@dp.callback_query(F.data == "a_menu_pr", F.from_user.id.in_(ADMIN_IDS))
async def adm_menu_pr(c: types.CallbackQuery):
    text = "<b>🎟 Промокоды</b>\n\nВыберите действие:"
    try:
        await c.message.edit_text(text, reply_markup=_pr_kb(), parse_mode=ParseMode.HTML)
    except Exception:
        await c.message.answer(text, reply_markup=_pr_kb(), parse_mode=ParseMode.HTML)
    await c.answer()


@dp.callback_query(F.data == "a_menu_ts", F.from_user.id.in_(ADMIN_IDS))
async def adm_menu_ts(c: types.CallbackQuery):
    text = "<b>📚 Задания</b>\n\nВыберите действие:"
    try:
        await c.message.edit_text(text, reply_markup=_ts_kb(), parse_mode=ParseMode.HTML)
    except Exception:
        await c.message.answer(text, reply_markup=_ts_kb(), parse_mode=ParseMode.HTML)
    await c.answer()


@dp.callback_query(F.data == "a_menu_sp", F.from_user.id.in_(ADMIN_IDS))
async def adm_menu_sp(c: types.CallbackQuery):
    text = "<b>📣 Спонсоры</b>\n\nВыберите действие:"
    try:
        await c.message.edit_text(text, reply_markup=_sp_kb(), parse_mode=ParseMode.HTML)
    except Exception:
        await c.message.answer(text, reply_markup=_sp_kb(), parse_mode=ParseMode.HTML)
    await c.answer()


# 🎟 ПРОМОКОДЫ (админ)
@dp.callback_query(F.data == "a_pr_add", F.from_user.id.in_(ADMIN_IDS))
async def adm_pr_add(c: types.CallbackQuery, state: FSMContext):
    if not has_leaf_permission(c.from_user.id, "promos.add"):
        return await c.answer("❌ Нет доступа", show_alert=True)
    await c.message.answer(
        "<b>Введите данные промокода:</b>\n"
        "<code>КОД НАГРАДА [LIMIT число] [DATE дд.мм.гггг чч:мм]</code>\n\n"
        "Примеры:\n"
        "<code>PROMO 50</code> — без ограничений\n"
        "<code>PROMO 50 LIMIT 100</code> — лимит 100 активаций\n"
        "<code>PROMO 50 DATE 31.12.2025</code> — до 31.12.2025 23:59\n"
        "<code>PROMO 50 LIMIT 100 DATE 31.12.2025 18:00</code> — лимит и дата",
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(AdminStates.add_pr)
    await c.answer()


@dp.message(AdminStates.add_pr, F.from_user.id.in_(ADMIN_IDS))
async def adm_pr_add_process(m: types.Message, state: FSMContext):
    parts = m.text.strip().split()
    if len(parts) < 2:
        await m.answer("<b>❌ Укажите как минимум КОД и НАГРАДУ.</b>", parse_mode=ParseMode.HTML)
        await state.clear()
        return

    code = parts[0].upper()
    try:
        stars = float(parts[1])
    except Exception:
        await m.answer("<b>❌ Награда должна быть числом.</b>", parse_mode=ParseMode.HTML)
        await state.clear()
        return

    max_uses = 0
    expires_at = None

    i = 2
    while i < len(parts):
        if parts[i].upper() == 'LIMIT' and i + 1 < len(parts):
            try:
                max_uses = int(parts[i + 1])
                i += 2
            except Exception:
                await m.answer("<b>❌ Неверный формат LIMIT.</b>", parse_mode=ParseMode.HTML)
                await state.clear()
                return
        elif parts[i].upper() == 'DATE' and i + 1 < len(parts):
            date_str = parts[i + 1]
            time_str = "23:59"
            if i + 2 < len(parts) and ':' in parts[i + 2]:
                time_str = parts[i + 2]
                i += 3
            else:
                i += 2
            try:
                datetime.datetime.strptime(f"{date_str} {time_str}", "%d.%m.%Y %H:%M")
                expires_at = f"{date_str} {time_str}"
            except Exception:
                await m.answer("<b>❌ Неверный формат даты/времени.</b>", parse_mode=ParseMode.HTML)
                await state.clear()
                return
        else:
            await m.answer("<b>❌ Неизвестный параметр.</b>", parse_mode=ParseMode.HTML)
            await state.clear()
            return

    try:
        db.conn.execute(
            "INSERT INTO promos (code, stars, max_uses, expires_at) VALUES (?, ?, ?, ?)",
            (code, stars, max_uses, expires_at),
        )
        db.conn.commit()
        msg = f"<b>✅ Промокод <code>{code}</code> добавлен!</b>\n⭐ {fmt(stars)}"
        if max_uses > 0:
            msg += f"\n🔢 Лимит: {max_uses}"
        if expires_at:
            msg += f"\n📅 Действует до: {expires_at} МСК"
        await m.answer(msg, parse_mode=ParseMode.HTML)
    except sqlite3.IntegrityError:
        await m.answer("<b>❌ Такой промокод уже существует!</b>", parse_mode=ParseMode.HTML)
    except Exception:
        await m.answer("<b>❌ Ошибка при добавлении.</b>", parse_mode=ParseMode.HTML)
    await state.clear()


@dp.callback_query(F.data == "a_pr_list", F.from_user.id.in_(ADMIN_IDS))
async def adm_pr_list(c: types.CallbackQuery):
    if not has_leaf_permission(c.from_user.id, "promos.list"):
        return await c.answer("❌ Нет доступа", show_alert=True)
    prs = db.conn.execute("SELECT * FROM promos").fetchall()
    if not prs:
        await c.message.answer("<b>Список промокодов пуст.</b>", parse_mode=ParseMode.HTML)
        await c.answer()
        return
    now_msk = msk_now()
    text = "<b>🎟 Промокоды:</b>\n\n"
    for p in prs:
        status = ""
        if p['expires_at']:
            try:
                exp = datetime.datetime.strptime(p['expires_at'], "%d.%m.%Y %H:%M")
                if now_msk > exp:
                    status = "⌛ Истёк"
            except Exception:
                pass
        if p['max_uses'] > 0 and p['used_count'] >= p['max_uses']:
            status = "🚫 Исчерпан"
        text += f"<b><code>{p['code']}</code></b> — {fmt(p['stars'])}⭐"
        if p['max_uses'] > 0:
            text += f" | {p['used_count']}/{p['max_uses']}"
        if p['expires_at']:
            text += f" | до {p['expires_at']}"
        if status:
            text += f" {status}"
        text += "\n"
    await c.message.answer(text, parse_mode=ParseMode.HTML)
    await c.answer()


@dp.callback_query(F.data == "a_pr_del", F.from_user.id.in_(ADMIN_IDS))
async def adm_pr_del(c: types.CallbackQuery):
    if not has_leaf_permission(c.from_user.id, "promos.del"):
        return await c.answer("❌ Нет доступа", show_alert=True)
    prs = db.conn.execute("SELECT * FROM promos").fetchall()
    if not prs:
        await c.message.answer("<b>Список промокодов пуст.</b>", parse_mode=ParseMode.HTML)
        await c.answer()
        return
    kb = InlineKeyboardBuilder()
    for p in prs:
        kb.row(InlineKeyboardButton(text=f"❌ {p['code']} ({fmt(p['stars'])}⭐)", callback_data=f"adm_del_pr_{p['code']}"))
    await c.message.answer("<b>Выберите промокод для удаления:</b>", reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)
    await c.answer()


@dp.callback_query(F.data.startswith("adm_del_pr_"), F.from_user.id.in_(ADMIN_IDS))
async def adm_pr_del_process(c: types.CallbackQuery):
    code = c.data.replace("adm_del_pr_", "")
    db.conn.execute("DELETE FROM promos WHERE code=?", (code,))
    db.conn.commit()
    try:
        await c.message.delete()
    except Exception:
        pass
    await c.answer(f"✅ Промокод {code} удалён.")


# 📚 ЗАДАНИЯ (админ)
@dp.callback_query(F.data == "a_ts_add", F.from_user.id.in_(ADMIN_IDS))
async def adm_ts_add(c: types.CallbackQuery, state: FSMContext):
    if not has_leaf_permission(c.from_user.id, "tasks.add"):
        return await c.answer("❌ Нет доступа", show_alert=True)
    await c.message.answer(
        "<b>Введите данные задания:\nСсылка Награда Описание\n"
        "Для телеграм-каналов/чатов/ботов можно указать @username или ID (-100...)\n"
        "Пример: https://t.me/durov 10 Подпишись на канал\n"
        "Пример: -1001234567890 5 Вступи в приватный чат</b>",
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(AdminStates.add_ts)
    await c.answer()


@dp.message(AdminStates.add_ts, F.from_user.id.in_(ADMIN_IDS))
async def adm_ts_add_process(m: types.Message, state: FSMContext):
    try:
        parts = m.text.split(maxsplit=2)
        if len(parts) != 3:
            raise ValueError
        link, prize, desc = parts[0], float(parts[1]), parts[2]
        db.conn.execute("INSERT INTO tasks (link, prize, description) VALUES (?, ?, ?)", (link, prize, desc))
        db.conn.commit()
        await m.answer("<b>✅ Задание добавлено.</b>", parse_mode=ParseMode.HTML)
    except Exception:
        await m.answer("<b>❌ Неверный формат. Пример:\nhttps://t.me/durov 10 Подпишись</b>", parse_mode=ParseMode.HTML)
    await state.clear()


@dp.callback_query(F.data == "a_ts_list", F.from_user.id.in_(ADMIN_IDS))
async def adm_ts_list(c: types.CallbackQuery):
    if not has_leaf_permission(c.from_user.id, "tasks.list"):
        return await c.answer("❌ Нет доступа", show_alert=True)
    ts = db.conn.execute("SELECT * FROM tasks").fetchall()
    if not ts:
        await c.message.answer("<b>Список заданий пуст.</b>", parse_mode=ParseMode.HTML)
        await c.answer()
        return
    text = "<b>📚 Задания:\n\n</b>"
    for t in ts:
        text += f"<b>ID: {t['id']} | {fmt(t['prize'])}⭐\n{t['description']}\n{t['link']}\n\n</b>"
    await c.message.answer(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    await c.answer()


@dp.callback_query(F.data == "a_ts_del", F.from_user.id.in_(ADMIN_IDS))
async def adm_ts_del(c: types.CallbackQuery):
    if not has_leaf_permission(c.from_user.id, "tasks.del"):
        return await c.answer("❌ Нет доступа", show_alert=True)
    ts = db.conn.execute("SELECT * FROM tasks").fetchall()
    if not ts:
        await c.message.answer("<b>Список заданий пуст.</b>", parse_mode=ParseMode.HTML)
        await c.answer()
        return
    kb = InlineKeyboardBuilder()
    for t in ts:
        short = (t['description'][:18] + '..') if len(t['description']) > 18 else t['description']
        kb.row(InlineKeyboardButton(text=f"❌ ID{t['id']} | {short}", callback_data=f"adm_del_ts_{t['id']}"))
    await c.message.answer("<b>Выберите задание для удаления:</b>", reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)
    await c.answer()


@dp.callback_query(F.data.startswith("adm_del_ts_"), F.from_user.id.in_(ADMIN_IDS))
async def adm_ts_del_process(c: types.CallbackQuery):
    tid = int(c.data.replace("adm_del_ts_", ""))
    db.conn.execute("DELETE FROM tasks WHERE id=?", (tid,))
    db.conn.commit()
    try:
        await c.message.delete()
    except Exception:
        pass
    await c.answer("✅ Задание удалено.")


# 📣 СПОНСОРЫ
@dp.callback_query(F.data == "a_sp_add", F.from_user.id.in_(ADMIN_IDS))
async def adm_sp_add(c: types.CallbackQuery, state: FSMContext):
    if not has_leaf_permission(c.from_user.id, "sponsors.add"):
        return await c.answer("❌ Нет доступа", show_alert=True)
    await c.message.answer(
        "<b>Введите данные спонсора:\n@username или ID (например -1001234567890) и Текст кнопки\n\n"
        "Пример: @mychannel Наш канал\n"
        "Пример: -1001234567890 Приватный чат\n"
        "Можно также указать бота: @my_bot (подписка не проверяется, бот будет показан)</b>",
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(AdminStates.add_sp)
    await c.answer()


@dp.message(AdminStates.add_sp, F.from_user.id.in_(ADMIN_IDS))
async def adm_sp_add_process(m: types.Message, state: FSMContext):
    try:
        parts = m.text.split(maxsplit=1)
        if len(parts) != 2:
            raise ValueError
        cid, btn_text = parts[0], parts[1]
        if not cid.startswith('@') and not cid.startswith('-100'):
            cid = '@' + cid
        db.conn.execute("INSERT INTO sponsors (channel_id, title) VALUES (?, ?)", (cid, btn_text))
        db.conn.commit()
        await m.answer(f"<b>✅ Спонсор {cid} добавлен.</b>", parse_mode=ParseMode.HTML)
    except Exception:
        await m.answer("<b>❌ Неверный формат. Пример: @mychannel Наш канал</b>", parse_mode=ParseMode.HTML)
    await state.clear()


@dp.callback_query(F.data == "a_sp_list", F.from_user.id.in_(ADMIN_IDS))
async def adm_sp_list(c: types.CallbackQuery):
    if not has_leaf_permission(c.from_user.id, "sponsors.list"):
        return await c.answer("❌ Нет доступа", show_alert=True)
    sps = db.conn.execute("SELECT * FROM sponsors").fetchall()
    if not sps:
        await c.message.answer("<b>Список спонсоров пуст.</b>", parse_mode=ParseMode.HTML)
        await c.answer()
        return
    text = "<b>📣 Спонсоры:\n\n</b>"
    for s in sps:
        text += f"<b>ID: {s['id']} | {s['channel_id']} - {s['title']}\n</b>"
    await c.message.answer(text, parse_mode=ParseMode.HTML)
    await c.answer()


@dp.callback_query(F.data == "a_sp_del", F.from_user.id.in_(ADMIN_IDS))
async def adm_sp_del(c: types.CallbackQuery):
    if not has_leaf_permission(c.from_user.id, "sponsors.del"):
        return await c.answer("❌ Нет доступа", show_alert=True)
    sps = db.conn.execute("SELECT * FROM sponsors").fetchall()
    if not sps:
        await c.message.answer("<b>Список спонсоров пуст.</b>", parse_mode=ParseMode.HTML)
        await c.answer()
        return
    kb = InlineKeyboardBuilder()
    for s in sps:
        kb.row(InlineKeyboardButton(text=f"❌ {s['channel_id']}", callback_data=f"adm_del_sp_{s['id']}"))
    await c.message.answer("<b>Выберите спонсора для удаления:</b>", reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)
    await c.answer()


@dp.callback_query(F.data.startswith("adm_del_sp_"), F.from_user.id.in_(ADMIN_IDS))
async def adm_sp_del_process(c: types.CallbackQuery):
    sid = int(c.data.replace("adm_del_sp_", ""))
    db.conn.execute("DELETE FROM sponsors WHERE id=?", (sid,))
    db.conn.commit()
    try:
        await c.message.delete()
    except Exception:
        pass
    await c.answer("✅ Спонсор удалён.")


# ❌ ЗАКРЫТЬ ПАНЕЛЬ
@dp.callback_query(F.data == "a_close", F.from_user.id.in_(ADMIN_IDS))
async def adm_close(c: types.CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await c.message.delete()
    except Exception:
        pass
    await c.message.answer("<b>Панель управления закрыта.</b>", reply_markup=main_kb(), parse_mode=ParseMode.HTML)
    await c.answer()


# 🖼 ЗАМЕНА КАРТИНОК (кнопка «Установить фото» в ALMAZ PANEL)
@dp.callback_query(F.data == "a_gif", F.from_user.id.in_(ADMIN_IDS))
async def adm_gif_entry(c: types.CallbackQuery, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardBuilder()
    for code, (label, filename) in PHOTO_SLOTS.items():
        mark = "✅ " if os.path.exists(f"photos/{filename}") else "▫️ "
        kb.row(InlineKeyboardButton(text=f"{mark}{label}", callback_data=f"gif_pick_{code}"))
    await c.message.answer(
        "<b>🖼 Установить фото</b>\n\nВыберите, на какую кнопку загрузить картинку:",
        reply_markup=kb.as_markup(),
        parse_mode=ParseMode.HTML,
    )
    await c.answer()


@dp.callback_query(F.data.startswith("gif_pick_"), F.from_user.id.in_(ADMIN_IDS))
async def gif_pick(c: types.CallbackQuery, state: FSMContext):
    code = c.data.replace("gif_pick_", "")
    if code not in PHOTO_SLOTS:
        return await c.answer("❌ Неизвестная кнопка", show_alert=True)
    label, filename = PHOTO_SLOTS[code]
    await state.update_data(gif_filename=filename, gif_label=label)
    await state.set_state(AdminStates.gif_wait)
    try:
        await c.message.delete()
    except Exception:
        pass
    await c.message.answer(
        f"<b>📤 Пришлите фото или стикер для «{label}» одним сообщением.</b>\n"
        f"<i>Анимированные и видео-стикеры не подходят — нужна статичная картинка.</i>",
        parse_mode=ParseMode.HTML,
    )
    await c.answer()


@dp.message(AdminStates.gif_wait, F.from_user.id.in_(ADMIN_IDS))
async def gif_receive(m: types.Message, state: FSMContext):
    is_static_sticker = m.sticker and not m.sticker.is_animated and not m.sticker.is_video
    if not m.photo and not is_static_sticker:
        return await m.answer(
            "<b>❌ Нужно фото или статичный стикер (не анимированный).</b>",
            parse_mode=ParseMode.HTML,
        )

    data = await state.get_data()
    filename = data.get("gif_filename")
    label = data.get("gif_label", filename)
    if not filename:
        await state.clear()
        return await m.answer("<b>❌ Сессия истекла, откройте «Установить фото» заново.</b>", parse_mode=ParseMode.HTML)

    os.makedirs("photos", exist_ok=True)
    dest = f"photos/{filename}"

    if m.photo:
        await bot_client.download(m.photo[-1].file_id, destination=dest)
    else:
        tmp = dest + ".src"
        await bot_client.download(m.sticker.file_id, destination=tmp)
        try:
            from PIL import Image
            with Image.open(tmp) as img:
                img.convert("RGBA").save(dest, "PNG")
        except Exception as e:
            os.remove(tmp) if os.path.exists(tmp) else None
            await state.clear()
            logging.error(f"Ошибка конвертации стикера: {e}")
            return await m.answer("<b>❌ Не удалось обработать стикер.</b>", parse_mode=ParseMode.HTML)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    await state.clear()
    await m.answer_photo(
        FSInputFile(dest),
        caption=f"<b>✅ Фото для «{label}» обновлено.</b>",
        parse_mode=ParseMode.HTML,
    )


# 🎨 КАСТОМИЗАЦИЯ (тексты + доп. кнопки на любом экране)
@dp.callback_query(F.data == "a_custom", F.from_user.id.in_(ADMIN_IDS))
async def adm_custom_entry(c: types.CallbackQuery, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🖼 Установить фото", callback_data="a_gif"))
    kb.row(InlineKeyboardButton(text="🔘 Изменение кнопок", callback_data="cust_mode_btns"))
    kb.row(InlineKeyboardButton(text="✏️ Изменение текстов", callback_data="cust_mode_txt"))
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="a_menu"))
    text = "<b>🎨 Кастомизация</b>\n\nЧто настраиваем?"
    try:
        await c.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)
    except Exception:
        await c.message.answer(text, reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)
    await c.answer()


def _screen_picker_kb(target_prefix: str, mark_fn):
    kb = InlineKeyboardBuilder()
    for code, (label, _filename) in PHOTO_SLOTS.items():
        kb.row(InlineKeyboardButton(text=f"{mark_fn(code)}{label}", callback_data=f"{target_prefix}{code}"))
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="a_custom"))
    return kb.as_markup()


@dp.callback_query(F.data == "cust_mode_btns", F.from_user.id.in_(ADMIN_IDS))
async def adm_custom_mode_btns(c: types.CallbackQuery):
    kb = _screen_picker_kb("cust_btns_", lambda code: f"({len(get_custom_buttons(code))}) ")
    text = "<b>🔘 Изменение кнопок</b>\n\nВыберите экран."
    try:
        await c.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception:
        await c.message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    await c.answer()


@dp.callback_query(F.data == "cust_mode_txt", F.from_user.id.in_(ADMIN_IDS))
async def adm_custom_mode_txt(c: types.CallbackQuery):
    kb = _screen_picker_kb("cust_txt_", lambda code: "✏️ " if get_custom_text(code) else "▫️ ")
    text = "<b>✏️ Изменение текстов</b>\n\nВыберите экран. ✏️ — уже изменён, ▫️ — стандартный."
    try:
        await c.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception:
        await c.message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    await c.answer()


@dp.callback_query(F.data.startswith("cust_txtreset_"), F.from_user.id.in_(ADMIN_IDS))
async def adm_custom_text_reset(c: types.CallbackQuery):
    code = c.data[len("cust_txtreset_"):]
    if code not in PHOTO_SLOTS:
        return await c.answer("❌ Неизвестный экран", show_alert=True)
    clear_custom_text(code)
    kb = _screen_picker_kb("cust_txt_", lambda code_: "✏️ " if get_custom_text(code_) else "▫️ ")
    await c.message.edit_text("<b>♻️ Текст сброшен на стандартный.</b>", reply_markup=kb, parse_mode=ParseMode.HTML)
    await c.answer()


@dp.callback_query(F.data.startswith("cust_txt_"), F.from_user.id.in_(ADMIN_IDS))
async def adm_custom_text_start(c: types.CallbackQuery, state: FSMContext):
    code = c.data[len("cust_txt_"):]
    if code not in PHOTO_SLOTS:
        return await c.answer("❌ Неизвестный экран", show_alert=True)
    label = PHOTO_SLOTS[code][0]
    await state.update_data(cust_screen=code)
    await state.set_state(AdminStates.custom_text)
    current = get_custom_text(code)
    current_block = f"\n\nТекущий текст:\n{current}" if current else ""

    kb = InlineKeyboardBuilder()
    if current:
        kb.row(InlineKeyboardButton(text="♻️ Сбросить на стандартный", callback_data=f"cust_txtreset_{code}"))
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="cust_mode_txt"))

    await c.message.answer(
        f"<b>✏️ Новый текст для «{label}»</b>\n\n"
        f"Пришлите сообщение с нужным текстом — поддерживается жирный/курсив/спойлер и премиум-эмодзи "
        f"(форматируйте прямо в Telegram и просто пришлите).\n"
        f"{PLACEHOLDER_HELP}\n"
        f"⚠️ Если у этого экрана бывает несколько вариантов сообщения (например, разные статусы), "
        f"новый текст заменит их все.{current_block}",
        reply_markup=kb.as_markup(),
        parse_mode=ParseMode.HTML,
    )
    await c.answer()


@dp.message(AdminStates.custom_text, F.from_user.id.in_(ADMIN_IDS))
async def adm_custom_text_save(m: types.Message, state: FSMContext):
    data = await state.get_data()
    code = data.get("cust_screen")
    await state.clear()
    if not code or code not in PHOTO_SLOTS or not m.text:
        return await m.answer("<b>❌ Сессия истекла или это не текст.</b>", parse_mode=ParseMode.HTML)
    set_custom_text(code, m.html_text)
    label = PHOTO_SLOTS[code][0]
    kb = _screen_picker_kb("cust_txt_", lambda code_: "✏️ " if get_custom_text(code_) else "▫️ ")
    await m.answer(f"<b>✅ Текст для «{label}» обновлён.</b>", reply_markup=kb, parse_mode=ParseMode.HTML)


def _custom_buttons_kb(code: str):
    kb = InlineKeyboardBuilder()
    if code == "start":
        for key in NAV_BUTTON_KEYS:
            nb = get_nav_button(key)
            kb.row(InlineKeyboardButton(text=f"🏠 {nb['label']}", callback_data=f"navbtn_open_{key}"))
    type_marks = {"link": "🔗", "webapp": "📱", "text": "💬", "info": "ℹ️"}
    for b in get_custom_buttons(code):
        place_mark = "⬇️" if b["placement"] == "bottom" else "📩"
        row_mark = "↔️" if b["same_row"] else "▫️"
        btn_type = b["type"] if "type" in b.keys() and b["type"] else "link"
        type_mark = type_marks.get(btn_type, "🔗")
        kb.row(InlineKeyboardButton(text=f"{row_mark}{place_mark}{type_mark} {b['text']}", callback_data=f"cust_btnopen_{b['id']}_{code}"))
    kb.row(InlineKeyboardButton(text="➕ Добавить кнопку", callback_data=f"cust_btnadd_{code}"))
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="cust_mode_btns"))
    return kb.as_markup()


def _custom_button_edit_kb(btn_id: int, code: str):
    b = db.conn.execute("SELECT * FROM custom_buttons WHERE id=?", (btn_id,)).fetchone()
    row_label = "↔️ Отклеить от предыдущей" if b and b["same_row"] else "↔️ Прилепить к предыдущей (в один ряд)"
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="✏️ Изменить текст", callback_data=f"cust_btnedit_{btn_id}_{code}"))
    kb.row(
        InlineKeyboardButton(text="⬆️ Выше", callback_data=f"cust_btnmove_{btn_id}_{code}_up"),
        InlineKeyboardButton(text="⬇️ Ниже", callback_data=f"cust_btnmove_{btn_id}_{code}_down"),
    )
    kb.row(InlineKeyboardButton(text=row_label, callback_data=f"cust_btnrow_{btn_id}_{code}"))
    kb.row(
        InlineKeyboardButton(text="🎨 Изменить цвет", callback_data=f"cust_btncolor_{btn_id}_{code}"),
        InlineKeyboardButton(text="💎 Изменить иконку", callback_data=f"cust_btnicon_{btn_id}_{code}"),
    )
    kb.row(InlineKeyboardButton(text="❌ Удалить", callback_data=f"cust_btndel_{btn_id}_{code}"))
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"cust_btns_{code}"))
    return kb.as_markup()


@dp.callback_query(F.data.startswith("cust_btns_"), F.from_user.id.in_(ADMIN_IDS))
async def adm_custom_buttons_list(c: types.CallbackQuery):
    code = c.data[len("cust_btns_"):]
    if code not in PHOTO_SLOTS:
        return await c.answer("❌ Неизвестный экран", show_alert=True)
    label = PHOTO_SLOTS[code][0]
    text = (
        f"<b>🔘 Кнопки на «{label}»</b>\n\n"
        f"Порядок сверху вниз — порядок на экране. ↔️ — приклеена к предыдущей (один ряд), ▫️ — свой ряд.\n"
    )
    if code == "start":
        text += "🏠 — встроенные кнопки меню (можно менять текст/цвет/иконку).\n"
    text += "Выберите кнопку, чтобы изменить."
    try:
        await c.message.edit_text(text, reply_markup=_custom_buttons_kb(code), parse_mode=ParseMode.HTML)
    except Exception:
        await c.message.answer(text, reply_markup=_custom_buttons_kb(code), parse_mode=ParseMode.HTML)
    await c.answer()


@dp.callback_query(F.data.startswith("cust_btnopen_"), F.from_user.id.in_(ADMIN_IDS))
async def adm_custom_button_open(c: types.CallbackQuery):
    rest = c.data[len("cust_btnopen_"):]
    btn_id_str, code = rest.rsplit("_", 1)
    await c.message.edit_text(
        "<b>Что сделать с кнопкой?</b>",
        reply_markup=_custom_button_edit_kb(int(btn_id_str), code),
        parse_mode=ParseMode.HTML,
    )
    await c.answer()


@dp.callback_query(F.data.startswith("cust_btnmove_"), F.from_user.id.in_(ADMIN_IDS))
async def adm_custom_button_move(c: types.CallbackQuery):
    rest = c.data[len("cust_btnmove_"):]
    btn_id_str, code, direction = rest.rsplit("_", 2)
    move_custom_button(int(btn_id_str), code, -1 if direction == "up" else 1)
    await c.message.edit_text(
        "<b>Что сделать с кнопкой?</b>",
        reply_markup=_custom_button_edit_kb(int(btn_id_str), code),
        parse_mode=ParseMode.HTML,
    )
    await c.answer("Готово")


@dp.callback_query(F.data.startswith("cust_btnrow_"), F.from_user.id.in_(ADMIN_IDS))
async def adm_custom_button_row_toggle(c: types.CallbackQuery):
    rest = c.data[len("cust_btnrow_"):]
    btn_id_str, code = rest.rsplit("_", 1)
    toggle_same_row(int(btn_id_str))
    await c.message.edit_text(
        "<b>Что сделать с кнопкой?</b>",
        reply_markup=_custom_button_edit_kb(int(btn_id_str), code),
        parse_mode=ParseMode.HTML,
    )
    await c.answer("Готово")


@dp.callback_query(F.data.startswith("cust_btnedit_"), F.from_user.id.in_(ADMIN_IDS))
async def adm_custom_button_edit_start(c: types.CallbackQuery, state: FSMContext):
    rest = c.data[len("cust_btnedit_"):]
    btn_id_str, code = rest.rsplit("_", 1)
    await state.update_data(cust_btn_edit_id=int(btn_id_str), cust_btn_edit_code=code)
    await state.set_state(AdminStates.custom_btn_edit)
    await c.message.answer(f"<b>✏️ Пришлите новый текст кнопки:</b>\n{PLACEHOLDER_HELP}", parse_mode=ParseMode.HTML)
    await c.answer()


@dp.message(AdminStates.custom_btn_edit, F.from_user.id.in_(ADMIN_IDS))
async def adm_custom_button_edit_save(m: types.Message, state: FSMContext):
    data = await state.get_data()
    btn_id = data.get("cust_btn_edit_id")
    code = data.get("cust_btn_edit_code")
    await state.clear()
    new_text = (m.text or "").strip()
    if not btn_id or not new_text:
        return await m.answer("<b>❌ Сессия истекла или пустой текст.</b>", parse_mode=ParseMode.HTML)
    update_custom_button_text(btn_id, new_text)
    await m.answer(
        "<b>✅ Текст кнопки обновлён.</b>",
        reply_markup=_custom_buttons_kb(code),
        parse_mode=ParseMode.HTML,
    )


@dp.callback_query(F.data.startswith("cust_btncolor_"), F.from_user.id.in_(ADMIN_IDS))
async def adm_custom_button_color_start(c: types.CallbackQuery, state: FSMContext):
    rest = c.data[len("cust_btncolor_"):]
    btn_id_str, code = rest.rsplit("_", 1)
    await state.update_data(cbcolor_btn_id=int(btn_id_str), cbcolor_code=code)
    await c.message.answer(
        "<b>🎨 Выберите новый цвет кнопки:</b>", reply_markup=_color_pick_kb("ccoledit_"), parse_mode=ParseMode.HTML,
    )
    await c.answer()


@dp.callback_query(F.data.startswith("ccoledit_"), F.from_user.id.in_(ADMIN_IDS))
async def adm_custom_button_color_apply(c: types.CallbackQuery, state: FSMContext):
    color_key = c.data[len("ccoledit_"):]
    data = await state.get_data()
    btn_id = data.get("cbcolor_btn_id")
    code = data.get("cbcolor_code")
    if not btn_id:
        return await c.answer("❌ Сессия истекла", show_alert=True)
    update_custom_button_style(btn_id, BUTTON_STYLES.get(color_key))
    await c.message.edit_text(
        "<b>✅ Цвет обновлён.</b>", reply_markup=_custom_button_edit_kb(btn_id, code), parse_mode=ParseMode.HTML,
    )
    await c.answer()


@dp.callback_query(F.data.startswith("cust_btnicon_"), F.from_user.id.in_(ADMIN_IDS))
async def adm_custom_button_icon_start(c: types.CallbackQuery, state: FSMContext):
    rest = c.data[len("cust_btnicon_"):]
    btn_id_str, code = rest.rsplit("_", 1)
    await state.update_data(cbicon_btn_id=int(btn_id_str), cbicon_code=code)
    await state.set_state(AdminStates.custom_btn_icon_edit)
    await c.message.answer(
        "<b>💎 Пришлите премиум-эмодзи для новой иконки, или напишите нет, чтобы убрать иконку:</b>",
        parse_mode=ParseMode.HTML,
    )
    await c.answer()


@dp.message(AdminStates.custom_btn_icon_edit, F.from_user.id.in_(ADMIN_IDS))
async def adm_custom_button_icon_apply(m: types.Message, state: FSMContext):
    data = await state.get_data()
    btn_id = data.get("cbicon_btn_id")
    code = data.get("cbicon_code")
    await state.clear()
    if not btn_id:
        return await m.answer("<b>❌ Сессия истекла.</b>", parse_mode=ParseMode.HTML)

    icon_id = None
    skip = bool(m.text) and m.text.strip().lower() == "нет"
    if not skip:
        for ent in (m.entities or []):
            if ent.type == "custom_emoji":
                icon_id = ent.custom_emoji_id
                break
        if icon_id is None:
            await m.answer(
                "<b>❌ Не нашла премиум-эмодзи в сообщении. Пришлите эмодзи ещё раз или напишите нет.</b>",
                parse_mode=ParseMode.HTML,
            )
            return

    update_custom_button_icon(btn_id, icon_id)
    await m.answer(
        "<b>✅ Иконка обновлена.</b>", reply_markup=_custom_button_edit_kb(btn_id, code), parse_mode=ParseMode.HTML,
    )


# 🏠 Встроенные кнопки старта (Профиль/Заработать/... — редактирование текста/цвета/иконки)
def _navbtn_edit_kb(key: str):
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="✏️ Изменить текст", callback_data=f"navbtn_text_{key}"))
    kb.row(
        InlineKeyboardButton(text="🎨 Изменить цвет", callback_data=f"navbtn_color_{key}"),
        InlineKeyboardButton(text="💎 Изменить иконку", callback_data=f"navbtn_icon_{key}"),
    )
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="cust_btns_start"))
    return kb.as_markup()


@dp.callback_query(F.data.startswith("navbtn_open_"), F.from_user.id.in_(ADMIN_IDS))
async def adm_navbtn_open(c: types.CallbackQuery):
    key = c.data[len("navbtn_open_"):]
    nb = get_nav_button(key)
    if not nb:
        return await c.answer("❌ Не найдено", show_alert=True)
    text = (
        f"<b>🏠 Встроенная кнопка «{nb['label']}»</b>\n\n"
        f"Цвет: {nb['style'] or 'классик'}\n"
        f"Иконка: {'установлена' if nb['icon'] else 'нет'}"
    )
    try:
        await c.message.edit_text(text, reply_markup=_navbtn_edit_kb(key), parse_mode=ParseMode.HTML)
    except Exception:
        await c.message.answer(text, reply_markup=_navbtn_edit_kb(key), parse_mode=ParseMode.HTML)
    await c.answer()


@dp.callback_query(F.data.startswith("navbtn_text_"), F.from_user.id.in_(ADMIN_IDS))
async def adm_navbtn_text_start(c: types.CallbackQuery, state: FSMContext):
    key = c.data[len("navbtn_text_"):]
    await state.update_data(navbtn_key=key)
    await state.set_state(AdminStates.navbtn_text)
    await c.message.answer("<b>✏️ Пришлите новый текст кнопки:</b>", parse_mode=ParseMode.HTML)
    await c.answer()


@dp.message(AdminStates.navbtn_text, F.from_user.id.in_(ADMIN_IDS))
async def adm_navbtn_text_save(m: types.Message, state: FSMContext):
    data = await state.get_data()
    key = data.get("navbtn_key")
    await state.clear()
    new_text = (m.text or "").strip()
    if not key or not new_text:
        return await m.answer("<b>❌ Сессия истекла или пустой текст.</b>", parse_mode=ParseMode.HTML)
    set_nav_button_label(key, new_text)
    await m.answer("<b>✅ Текст обновлён.</b>", reply_markup=_navbtn_edit_kb(key), parse_mode=ParseMode.HTML)


@dp.callback_query(F.data.startswith("navbtn_color_"), F.from_user.id.in_(ADMIN_IDS))
async def adm_navbtn_color_start(c: types.CallbackQuery, state: FSMContext):
    key = c.data[len("navbtn_color_"):]
    await state.update_data(navbtn_color_key=key)
    await c.message.answer(
        "<b>🎨 Выберите новый цвет:</b>", reply_markup=_color_pick_kb("ncoledit_"), parse_mode=ParseMode.HTML,
    )
    await c.answer()


@dp.callback_query(F.data.startswith("ncoledit_"), F.from_user.id.in_(ADMIN_IDS))
async def adm_navbtn_color_apply(c: types.CallbackQuery, state: FSMContext):
    color_key = c.data[len("ncoledit_"):]
    data = await state.get_data()
    key = data.get("navbtn_color_key")
    if not key:
        return await c.answer("❌ Сессия истекла", show_alert=True)
    set_nav_button_style(key, BUTTON_STYLES.get(color_key))
    await c.message.edit_text("<b>✅ Цвет обновлён.</b>", reply_markup=_navbtn_edit_kb(key), parse_mode=ParseMode.HTML)
    await c.answer()


@dp.callback_query(F.data.startswith("navbtn_icon_"), F.from_user.id.in_(ADMIN_IDS))
async def adm_navbtn_icon_start(c: types.CallbackQuery, state: FSMContext):
    key = c.data[len("navbtn_icon_"):]
    await state.update_data(navbtn_icon_key=key)
    await state.set_state(AdminStates.navbtn_icon)
    await c.message.answer(
        "<b>💎 Пришлите премиум-эмодзи для иконки, или напишите нет, чтобы убрать:</b>", parse_mode=ParseMode.HTML,
    )
    await c.answer()


@dp.message(AdminStates.navbtn_icon, F.from_user.id.in_(ADMIN_IDS))
async def adm_navbtn_icon_save(m: types.Message, state: FSMContext):
    data = await state.get_data()
    key = data.get("navbtn_icon_key")
    await state.clear()
    if not key:
        return await m.answer("<b>❌ Сессия истекла.</b>", parse_mode=ParseMode.HTML)
    icon_id = None
    skip = bool(m.text) and m.text.strip().lower() == "нет"
    if not skip:
        for ent in (m.entities or []):
            if ent.type == "custom_emoji":
                icon_id = ent.custom_emoji_id
                break
        if icon_id is None:
            await m.answer(
                "<b>❌ Не нашла премиум-эмодзи. Пришлите ещё раз или напишите нет.</b>", parse_mode=ParseMode.HTML,
            )
            return
    set_nav_button_icon(key, icon_id)
    await m.answer("<b>✅ Иконка обновлена.</b>", reply_markup=_navbtn_edit_kb(key), parse_mode=ParseMode.HTML)


@dp.callback_query(F.data.startswith("cust_btndel_"), F.from_user.id.in_(ADMIN_IDS))
async def adm_custom_button_delete(c: types.CallbackQuery):
    rest = c.data[len("cust_btndel_"):]
    btn_id_str, code = rest.rsplit("_", 1)
    delete_custom_button(int(btn_id_str))
    label = PHOTO_SLOTS.get(code, ("?",))[0]
    await c.message.edit_text(
        f"<b>🔘 Кнопки на «{label}»</b>\n\nВыберите кнопку, чтобы изменить текст или удалить.",
        reply_markup=_custom_buttons_kb(code),
        parse_mode=ParseMode.HTML,
    )
    await c.answer("Удалено")


BUTTON_TYPE_LABELS = {
    "link": "🔗 Кнопка со ссылкой",
    "webapp": "📱 Кнопка с мини-аппом",
    "text": "💬 Обычная кнопка",
    "info": "ℹ️ Информационная кнопка",
}


def _button_type_kb(code: str):
    kb = InlineKeyboardBuilder()
    for t, label in BUTTON_TYPE_LABELS.items():
        kb.row(InlineKeyboardButton(text=label, callback_data=f"cust_btntype_{t}_{code}"))
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"cust_btns_{code}"))
    return kb.as_markup()


@dp.callback_query(F.data.startswith("cust_btnadd_"), F.from_user.id.in_(ADMIN_IDS))
async def adm_custom_button_add_start(c: types.CallbackQuery, state: FSMContext):
    code = c.data[len("cust_btnadd_"):]
    if code not in PHOTO_SLOTS:
        return await c.answer("❌ Неизвестный экран", show_alert=True)
    await state.clear()
    await c.message.edit_text(
        "<b>➕ Новая кнопка</b>\n\nВыберите тип кнопки:\n\n"
        "🔗 <b>Со ссылкой</b> — открывает внешний сайт\n"
        "📱 <b>С мини-аппом</b> — открывает мини-приложение внутри Telegram\n"
        "💬 <b>Обычная</b> — присылает текст сообщением при нажатии\n"
        "ℹ️ <b>Информационная</b> — показывает всплывающую подсказку, не засоряя чат",
        reply_markup=_button_type_kb(code),
        parse_mode=ParseMode.HTML,
    )
    await c.answer()


@dp.callback_query(F.data.startswith("cust_btntype_"), F.from_user.id.in_(ADMIN_IDS))
async def adm_custom_button_type_picked(c: types.CallbackQuery, state: FSMContext):
    rest = c.data[len("cust_btntype_"):]
    btn_type, code = rest.split("_", 1)
    if code not in PHOTO_SLOTS or btn_type not in BUTTON_TYPE_LABELS:
        return await c.answer("❌ Ошибка", show_alert=True)
    await state.update_data(cust_screen=code, cust_btn_type=btn_type)
    await state.set_state(AdminStates.custom_btn_input)
    if btn_type in ("link", "webapp"):
        prompt = "<b>➕ Новая кнопка</b>\n\nПришлите в формате:\n<code>Текст кнопки - https://ссылка.com</code>"
    else:
        kind = "всплывающей подсказки (до 200 символов)" if btn_type == "info" else "сообщения"
        prompt = (
            f"<b>➕ Новая кнопка</b>\n\nПришлите в формате:\n<code>Текст кнопки - Текст {kind}</code>\n{PLACEHOLDER_HELP}"
        )
    await c.message.edit_text(prompt, parse_mode=ParseMode.HTML)
    await c.answer()


@dp.message(AdminStates.custom_btn_input, F.from_user.id.in_(ADMIN_IDS))
async def adm_custom_button_add_got_text(m: types.Message, state: FSMContext):
    data = await state.get_data()
    btn_type = data.get("cust_btn_type", "link")
    line = (m.text or "").strip()
    sep = '-' if '-' in line else ('—' if '—' in line else None)
    if not sep:
        return await m.answer(f"<b>❌ Формат: Текст кнопки - {'https://ссылка.com' if btn_type in ('link', 'webapp') else 'Значение'}</b>", parse_mode=ParseMode.HTML)
    btn_text, btn_value = line.split(sep, 1)
    btn_text, btn_value = btn_text.strip(), btn_value.strip()

    if btn_type in ("link", "webapp"):
        if not btn_text or not btn_value.startswith("http"):
            return await m.answer("<b>❌ Формат: Текст кнопки - https://ссылка.com</b>", parse_mode=ParseMode.HTML)
    else:
        if not btn_text or not btn_value:
            return await m.answer("<b>❌ Пришлите текст кнопки и значение через «-»</b>", parse_mode=ParseMode.HTML)
        if btn_type == "info":
            btn_value = btn_value[:200]

    await state.update_data(cust_btn_text=btn_text, cust_btn_url=btn_value)
    await m.answer(
        f"<b>🎨 Кнопка: «{btn_text}»\nВыберите цвет:</b>",
        reply_markup=_color_pick_kb("ccol_"),
        parse_mode=ParseMode.HTML,
    )


@dp.callback_query(F.data.startswith("ccol_"), F.from_user.id.in_(ADMIN_IDS))
async def adm_custom_button_pick_color(c: types.CallbackQuery, state: FSMContext):
    color_key = c.data[len("ccol_"):]
    await state.update_data(cust_btn_style=BUTTON_STYLES.get(color_key))
    await state.set_state(AdminStates.custom_btn_icon)
    await c.answer()
    await c.message.edit_text(
        "<b>💎 Пришлите премиум-эмодзи для иконки кнопки, или напишите <code>нет</code>:</b>",
        parse_mode=ParseMode.HTML,
    )


@dp.message(AdminStates.custom_btn_icon, F.from_user.id.in_(ADMIN_IDS))
async def adm_custom_button_add_finish(m: types.Message, state: FSMContext):
    data = await state.get_data()
    code = data.get("cust_screen")
    btn_text = data.get("cust_btn_text")
    btn_url = data.get("cust_btn_url")
    style = data.get("cust_btn_style")

    if not code or code not in PHOTO_SLOTS or not btn_text or not btn_url:
        await state.clear()
        return await m.answer("<b>❌ Сессия истекла, начните заново через «➕ Добавить кнопку».</b>", parse_mode=ParseMode.HTML)

    icon_id = None
    skip = bool(m.text) and m.text.strip().lower() == "нет"
    if not skip:
        for ent in (m.entities or []):
            if ent.type == "custom_emoji":
                icon_id = ent.custom_emoji_id
                break
        if icon_id is None:
            await m.answer(
                "<b>❌ Не нашла премиум-эмодзи в сообщении. Пришлите эмодзи ещё раз или напишите нет.</b>",
                parse_mode=ParseMode.HTML,
            )
            return

    await state.update_data(cust_btn_icon=icon_id)
    btn_type = data.get("cust_btn_type", "link")

    if btn_type == "info":
        add_custom_button(code, btn_text, btn_url, style, icon_id, "inline", btn_type)
        await state.clear()
        await m.answer(
            f"<b>✅ Кнопка «{btn_text}» добавлена (под сообщением).</b>",
            reply_markup=_custom_buttons_kb(code),
            parse_mode=ParseMode.HTML,
        )
        return

    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="📩 Под сообщением", callback_data="cplace_inline"))
    kb.row(InlineKeyboardButton(text="⬇️ Снизу, в меню бота", callback_data="cplace_bottom"))
    await m.answer(
        "<b>📍 Где показывать кнопку?</b>\n\n"
        "📩 <b>Под сообщением</b> — прикрепится прямо к этому экрану.\n"
        "⬇️ <b>Снизу</b> — станет постоянной кнопкой в меню бота (видна всегда, на всех экранах); "
        "при нажатии бот пришлёт ссылку отдельным сообщением.",
        reply_markup=kb.as_markup(),
        parse_mode=ParseMode.HTML,
    )


@dp.callback_query(F.data.startswith("cplace_"), F.from_user.id.in_(ADMIN_IDS))
async def adm_custom_button_placement(c: types.CallbackQuery, state: FSMContext):
    placement = c.data[len("cplace_"):]
    data = await state.get_data()
    code = data.get("cust_screen")
    btn_text = data.get("cust_btn_text")
    btn_url = data.get("cust_btn_url")
    style = data.get("cust_btn_style")
    icon_id = data.get("cust_btn_icon")
    btn_type = data.get("cust_btn_type", "link")

    if not code or code not in PHOTO_SLOTS or not btn_text or not btn_url:
        await state.clear()
        return await c.answer("❌ Сессия истекла, начните заново", show_alert=True)

    if placement == "bottom" and btn_text in RESERVED_BUTTON_TEXTS:
        return await c.answer("❌ Такой текст уже занят стандартной кнопкой, выберите другой", show_alert=True)

    add_custom_button(code, btn_text, btn_url, style, icon_id, placement, btn_type)
    await state.clear()
    label = PHOTO_SLOTS[code][0]
    place_label = "под сообщением на «" + label + "»" if placement == "inline" else "снизу в меню бота"
    await c.message.edit_text(
        f"<b>✅ Кнопка «{btn_text}» добавлена ({place_label}).</b>",
        reply_markup=_custom_buttons_kb(code),
        parse_mode=ParseMode.HTML,
    )
    await c.answer()


# 📢 РЕКЛАМНЫЕ ССЫЛКИ
@dp.callback_query(F.data == "a_ads", F.from_user.id.in_(ADMIN_IDS))
async def adm_ads_entry(c: types.CallbackQuery, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="➕ Создать ссылку", callback_data="ads_add"))
    kb.row(InlineKeyboardButton(text="📊 Список ссылок", callback_data="ads_list"))
    kb.row(InlineKeyboardButton(text="👥 Все пользователи (по рекламе)", callback_data="aduser_page_1___all__"))
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="a_menu"))
    text = (
        "<b>📢 Реклама</b>\n\n"
        "Создавайте отслеживаемые ссылки для рекламы и смотрите полную статистику по каждой: "
        "сколько человек пришло, сколько активны, сколько заработали и вывели."
    )
    try:
        await c.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)
    except Exception:
        await c.message.answer(text, reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)
    await c.answer()


@dp.callback_query(F.data == "ads_add", F.from_user.id.in_(ADMIN_IDS))
async def adm_ads_add_start(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.ad_link_name)
    await c.message.answer(
        "<b>➕ Название рекламной ссылки</b>\n\nПришлите короткое название (например: vk_reklama, youtube_1):",
        parse_mode=ParseMode.HTML,
    )
    await c.answer()


@dp.message(AdminStates.ad_link_name, F.from_user.id.in_(ADMIN_IDS))
async def adm_ads_add_save(m: types.Message, state: FSMContext):
    await state.clear()
    name = (m.text or "").strip()
    if not name:
        return await m.answer("<b>❌ Пустое название.</b>", parse_mode=ParseMode.HTML)
    code = create_ad_link(name)
    me = await bot_client.me()
    link = f"https://t.me/{me.username}?start=ad_{code}"
    await m.answer(
        f"<b>✅ Ссылка создана!</b>\n\n"
        f"📛 Название: {name}\n"
        f"🔗 <code>{link}</code>\n\n"
        f"Все, кто перейдёт по ней, попадут в статистику этой ссылки.",
        parse_mode=ParseMode.HTML,
    )


def _ads_list_kb():
    kb = InlineKeyboardBuilder()
    for link in get_ad_links():
        stats = ad_link_stats(link["code"])
        kb.row(InlineKeyboardButton(
            text=f"{link['name']} — {stats['total']} чел.", callback_data=f"ads_open_{link['code']}",
        ))
    kb.row(InlineKeyboardButton(text="➕ Создать ссылку", callback_data="ads_add"))
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="a_ads"))
    return kb.as_markup()


@dp.callback_query(F.data == "ads_list", F.from_user.id.in_(ADMIN_IDS))
async def adm_ads_list(c: types.CallbackQuery):
    links = get_ad_links()
    text = "<b>📊 Рекламные ссылки</b>\n\nВыберите ссылку для подробной статистики." if links else "<b>📊 Рекламные ссылки</b>\n\nПока ни одной не создано."
    try:
        await c.message.edit_text(text, reply_markup=_ads_list_kb(), parse_mode=ParseMode.HTML)
    except Exception:
        await c.message.answer(text, reply_markup=_ads_list_kb(), parse_mode=ParseMode.HTML)
    await c.answer()


@dp.callback_query(F.data.startswith("ads_open_"), F.from_user.id.in_(ADMIN_IDS))
async def adm_ads_open(c: types.CallbackQuery):
    code = c.data[len("ads_open_"):]
    link = get_ad_link(code)
    if not link:
        return await c.answer("❌ Ссылка не найдена", show_alert=True)
    stats = ad_link_stats(code)
    me = await bot_client.me()
    full_link = f"https://t.me/{me.username}?start=ad_{code}"
    text = (
        f"<b>📢 Ссылка «{link['name']}»</b>\n"
        f"🔗 <code>{full_link}</code>\n"
        f"📅 Создана: {link['created_at']}\n\n"
        f"👥 Перешло по ссылке: <b>{stats['total']}</b>\n"
        f"✅ Активных: <b>{stats['active']}</b>\n"
        f"⛔ Забанено: <b>{stats['banned']}</b>\n"
        f"🕐 Последний переход: {stats['last_join']}\n\n"
        f"⭐ Заработано когортой: <b>{fmt(stats['earned'])}</b>\n"
        f"📊 В среднем на человека: <b>{fmt(stats['avg_earned'])}</b>⭐\n"
        f"📤 Выводов сделано: <b>{stats['withdrawals']}</b>\n"
        f"📚 Заданий выполнено: <b>{stats['tasks']}</b>\n"
        f"🔑 Промокодов активировано: <b>{stats['promos_used']}</b>\n"
        f"👥 Своих рефералов привели: <b>{stats['refs']}</b>"
    )
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="👥 Пользователи по этой ссылке", callback_data=f"aduser_page_1_{code}"))
    kb.row(InlineKeyboardButton(text="❌ Удалить ссылку", callback_data=f"ads_del_{code}"))
    kb.row(InlineKeyboardButton(text="⬅️ К списку", callback_data="ads_list"))
    try:
        await c.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)
    except Exception:
        await c.message.answer(text, reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)
    await c.answer()


@dp.callback_query(F.data.startswith("ads_del_"), F.from_user.id.in_(ADMIN_IDS))
async def adm_ads_delete(c: types.CallbackQuery):
    code = c.data[len("ads_del_"):]
    delete_ad_link(code)
    await c.message.edit_text(
        "<b>✅ Ссылка удалена.</b>\n\n<i>Статистика пользователей, пришедших по ней, сохраняется — удаляется только сама ссылка из списка.</i>",
        reply_markup=_ads_list_kb(),
        parse_mode=ParseMode.HTML,
    )
    await c.answer("Удалено")


# 🏆 VIP-УРОВНИ (админ)
def _vip_list_kb():
    kb = InlineKeyboardBuilder()
    for lvl in get_vip_levels(include_deleted=True):
        mark = "❌ " if lvl["is_deleted"] else ""
        kb.row(InlineKeyboardButton(
            text=f"{mark}{lvl['name']} — от {fmt(lvl['min_earned'])}⭐, x{lvl['multiplier']:g}",
            callback_data=f"vip_open_{lvl['id']}",
        ))
    kb.row(InlineKeyboardButton(text="➕ Добавить уровень", callback_data="vip_add"))
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="a_menu"))
    return kb.as_markup()


@dp.callback_query(F.data == "a_vip", F.from_user.id.in_(ADMIN_IDS))
async def adm_vip_entry(c: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text = (
        "<b>🏆 VIP-уровни</b>\n\n"
        "Чем больше пользователь суммарно заработал звёзд — тем выше его уровень и множитель к доходу "
        "(рефералка, ежедневный бонус, награда за задания). Нажмите на уровень, чтобы изменить/удалить/восстановить его.\n\n"
    )
    for lvl in get_vip_levels(include_deleted=True):
        mark = "❌ " if lvl["is_deleted"] else "• "
        text += f"{mark}<b>{lvl['name']}</b> — от {fmt(lvl['min_earned'])}⭐ — x{lvl['multiplier']:g}\n"
    try:
        await c.message.edit_text(text, reply_markup=_vip_list_kb(), parse_mode=ParseMode.HTML)
    except Exception:
        await c.message.answer(text, reply_markup=_vip_list_kb(), parse_mode=ParseMode.HTML)
    await c.answer()


@dp.callback_query(F.data == "vip_add", F.from_user.id.in_(ADMIN_IDS))
async def adm_vip_add_start(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.vip_add)
    await c.message.answer(
        "<b>➕ Новый уровень</b>\n\nФормат:\n<code>Название Порог Множитель</code>\n\n"
        "Пример: <code>Алмаз 3000 3</code> — с 3000⭐ заработанного множитель x3\n"
        "(если в названии несколько слов — оно должно идти первым, а порог и множитель последними двумя числами)",
        parse_mode=ParseMode.HTML,
    )
    await c.answer()


@dp.message(AdminStates.vip_add, F.from_user.id.in_(ADMIN_IDS))
async def adm_vip_add_process(m: types.Message, state: FSMContext):
    await state.clear()
    parts = (m.text or "").strip().split()
    if len(parts) < 3:
        return await m.answer("<b>❌ Формат: Название Порог Множитель</b>", parse_mode=ParseMode.HTML)
    try:
        multiplier = float(parts[-1])
        min_earned = float(parts[-2])
    except ValueError:
        return await m.answer("<b>❌ Порог и множитель должны быть числами.</b>", parse_mode=ParseMode.HTML)
    name = " ".join(parts[:-2]).strip()
    if not name:
        return await m.answer("<b>❌ Укажите название уровня.</b>", parse_mode=ParseMode.HTML)
    add_vip_level(name, min_earned, multiplier)
    text = f"<b>✅ Уровень «{name}» добавлен: от {fmt(min_earned)}⭐, множитель x{multiplier:g}</b>"
    await m.answer(text, reply_markup=_vip_list_kb(), parse_mode=ParseMode.HTML)


def _vip_detail_kb(level_id: int, is_deleted: bool):
    kb = InlineKeyboardBuilder()
    if is_deleted:
        kb.row(InlineKeyboardButton(text="♻️ Восстановить", callback_data=f"vip_restore_{level_id}"))
    else:
        kb.row(InlineKeyboardButton(text="✏️ Изменить", callback_data=f"vip_edit_{level_id}"))
        kb.row(InlineKeyboardButton(text="❌ Удалить", callback_data=f"vip_ask_{level_id}"))
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="a_vip"))
    return kb.as_markup()


@dp.callback_query(F.data.startswith("vip_open_"), F.from_user.id.in_(ADMIN_IDS))
async def adm_vip_open(c: types.CallbackQuery):
    level_id = int(c.data[len("vip_open_"):])
    lvl = get_vip_level_id(level_id)
    if not lvl:
        return await c.answer("❌ Уровень не найден", show_alert=True)
    status = "❌ Удалён (можно восстановить)" if lvl["is_deleted"] else "✅ Активен"
    text = (
        f"<b>🏆 Уровень «{lvl['name']}»</b>\n\n"
        f"Порог: от {fmt(lvl['min_earned'])}⭐\n"
        f"Множитель: x{lvl['multiplier']:g}\n"
        f"Статус: {status}"
    )
    await c.message.edit_text(text, reply_markup=_vip_detail_kb(level_id, bool(lvl["is_deleted"])), parse_mode=ParseMode.HTML)
    await c.answer()


@dp.callback_query(F.data.startswith("vip_edit_"), F.from_user.id.in_(ADMIN_IDS))
async def adm_vip_edit_start(c: types.CallbackQuery, state: FSMContext):
    level_id = int(c.data[len("vip_edit_"):])
    lvl = get_vip_level_id(level_id)
    if not lvl:
        return await c.answer("❌ Уровень не найден", show_alert=True)
    await state.update_data(vip_edit_id=level_id)
    await state.set_state(AdminStates.vip_edit)
    await c.message.answer(
        f"<b>✏️ Изменение уровня «{lvl['name']}»</b>\n\n"
        f"Текущее: {lvl['name']} {fmt(lvl['min_earned'])} {lvl['multiplier']:g}\n\n"
        f"Пришлите новые данные в формате:\n<code>Название Порог Множитель</code>",
        parse_mode=ParseMode.HTML,
    )
    await c.answer()


@dp.message(AdminStates.vip_edit, F.from_user.id.in_(ADMIN_IDS))
async def adm_vip_edit_process(m: types.Message, state: FSMContext):
    data = await state.get_data()
    level_id = data.get("vip_edit_id")
    await state.clear()
    parts = (m.text or "").strip().split()
    if not level_id or len(parts) < 3:
        return await m.answer("<b>❌ Формат: Название Порог Множитель</b>", parse_mode=ParseMode.HTML)
    try:
        multiplier = float(parts[-1])
        min_earned = float(parts[-2])
    except ValueError:
        return await m.answer("<b>❌ Порог и множитель должны быть числами.</b>", parse_mode=ParseMode.HTML)
    name = " ".join(parts[:-2]).strip()
    if not name:
        return await m.answer("<b>❌ Укажите название уровня.</b>", parse_mode=ParseMode.HTML)
    edit_vip_level(level_id, name, min_earned, multiplier)
    text = f"<b>✅ Уровень обновлён: «{name}», от {fmt(min_earned)}⭐, x{multiplier:g}</b>"
    await m.answer(text, reply_markup=_vip_list_kb(), parse_mode=ParseMode.HTML)


@dp.callback_query(F.data.startswith("vip_restore_"), F.from_user.id.in_(ADMIN_IDS))
async def adm_vip_restore(c: types.CallbackQuery):
    level_id = int(c.data[len("vip_restore_"):])
    restore_vip_level(level_id)
    await c.message.edit_text(
        "<b>✅ Уровень восстановлен.</b>", reply_markup=_vip_list_kb(), parse_mode=ParseMode.HTML,
    )
    await c.answer("Восстановлено")


@dp.callback_query(F.data.startswith("vip_ask_"), F.from_user.id.in_(ADMIN_IDS))
async def adm_vip_delete_ask(c: types.CallbackQuery):
    level_id = int(c.data[len("vip_ask_"):])
    lvl = get_vip_level_id(level_id)
    if not lvl:
        return await c.answer("❌ Уровень не найден", show_alert=True)
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"vip_del_{level_id}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"vip_open_{level_id}"),
    )
    await c.message.edit_text(
        f"<b>⚠️ Удалить уровень «{lvl['name']}» (от {fmt(lvl['min_earned'])}⭐, x{lvl['multiplier']:g})?</b>\n\n"
        f"Можно будет восстановить позже.",
        reply_markup=kb.as_markup(),
        parse_mode=ParseMode.HTML,
    )
    await c.answer()


@dp.callback_query(F.data.startswith("vip_del_"), F.from_user.id.in_(ADMIN_IDS))
async def adm_vip_delete(c: types.CallbackQuery):
    level_id = int(c.data[len("vip_del_"):])
    delete_vip_level(level_id)
    await c.message.edit_text(
        "<b>✅ Уровень удалён (❌ в списке). Можно восстановить в любой момент.</b>",
        reply_markup=_vip_list_kb(),
        parse_mode=ParseMode.HTML,
    )
    await c.answer("Удалено")


# ⭐ ОТЗЫВЫ (админ)
def _reviews_list_kb():
    kb = InlineKeyboardBuilder()
    for r in get_reviews_by_status("published", "deleted"):
        mark = "❌ " if r["status"] == "deleted" else ""
        badge = "👍" if r["is_positive"] else "👎"
        kb.row(InlineKeyboardButton(
            text=f"{mark}{badge} {fmt(r['amount'])}⭐ — #{r['id']}", callback_data=f"rvopen_{r['id']}",
        ))
    pend = get_reviews_by_status("pending")
    if pend:
        kb.row(InlineKeyboardButton(text=f"⏳ На модерации: {len(pend)}", callback_data="noop"))
    kb.row(InlineKeyboardButton(text="🌐 Посетители мини-аппа", callback_data="a_visits"))
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="a_menu"))
    return kb.as_markup()


@dp.callback_query(F.data == "a_visits", F.from_user.id.in_(ADMIN_IDS))
async def adm_visits(c: types.CallbackQuery):
    if not has_leaf_permission(c.from_user.id, "reviews.manage"):
        return await c.answer("❌ Нет доступа", show_alert=True)
    visits = get_visits(20)
    if not visits:
        text = "<b>🌐 Посетители мини-аппа</b>\n\nПока никто не заходил."
    else:
        lines = []
        for v in visits:
            who = f"@{v['username']}" if v["username"] else (f"<code>{v['tg_id']}</code>" if v["tg_id"] else "неизвестно")
            mark = "✅" if v["verified"] else "⚠️ не подтв."
            loc_parts = [p for p in (v['city'], v['region'], v['country']) if p]
            loc = ", ".join(loc_parts) if loc_parts else "—"
            premium_mark = " ⭐" if v['is_premium'] else ""
            lines.append(
                f"• {who}{premium_mark} {mark}\n"
                f"  📍 {loc} | 🏢 {v['isp'] or '—'}\n"
                f"  📱 {v['platform'] or '—'} | 🎨 {v['color_scheme'] or '—'} | 🌍 {v['timezone'] or '—'}\n"
                f"  📟 Модель: {v['ua_model'] or '—'}\n"
                f"  💻 {v['user_platform'] or '—'} | {v['vendor'] or '—'} | 🧠 {v['device_memory'] or '—'}ГБ/{v['cpu_cores'] or '—'}яд\n"
                f"  🖥 {v['screen'] or '—'} | IP: <code>{v['ip'] or '—'}</code>\n"
                f"  🗣 {v['language'] or '—'} | 💬 tg:{v['tg_language_code'] or '—'}\n"
                f"  🕐 {v['created_at']}"
            )
        text = f"<b>🌐 Посетители мини-аппа (последние {len(visits)})</b>\n\n" + "\n\n".join(lines)
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="a_reviews"))
    try:
        await c.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)
    except Exception:
        await c.message.answer(text, reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)
    await c.answer()


@dp.callback_query(F.data == "a_reviews", F.from_user.id.in_(ADMIN_IDS))
async def adm_reviews_entry(c: types.CallbackQuery):
    if not has_leaf_permission(c.from_user.id, "reviews.manage"):
        return await c.answer("❌ Нет доступа", show_alert=True)
    published = get_reviews_by_status("published")
    pos = sum(1 for r in published if r["is_positive"])
    text = (
        f"<b>⭐ Отзывы</b>\n\n"
        f"Опубликовано: <b>{len(published)}</b> (👍 {pos} / 👎 {len(published) - pos})\n\n"
        f"Нажмите на отзыв, чтобы удалить его или восстановить."
    )
    try:
        await c.message.edit_text(text, reply_markup=_reviews_list_kb(), parse_mode=ParseMode.HTML)
    except Exception:
        await c.message.answer(text, reply_markup=_reviews_list_kb(), parse_mode=ParseMode.HTML)
    await c.answer()


@dp.callback_query(F.data.startswith("rvopen_"), F.from_user.id.in_(ADMIN_IDS))
async def adm_review_open(c: types.CallbackQuery):
    if not has_leaf_permission(c.from_user.id, "reviews.manage"):
        return await c.answer("❌ Нет доступа", show_alert=True)
    review_id = int(c.data[len("rvopen_"):])
    r = get_review(review_id)
    if not r:
        return await c.answer("❌ Отзыв не найден", show_alert=True)
    badge = "👍 Хорошо" if r["is_positive"] else "👎 Плохо"
    text = (
        f"<b>⭐ Отзыв #{r['id']}</b>\n\n"
        f"От: <code>{r['tg_id']}</code>\n"
        f"Сумма: {fmt(r['amount'])}⭐\n"
        f"Оценка: {badge}\n"
        f"Текст: {r['text'] or '—'}\n"
        f"Дата: {r['created_at']}\n"
        f"Статус: {'❌ Удалён' if r['status'] == 'deleted' else '✅ Опубликован'}"
    )
    kb = InlineKeyboardBuilder()
    if r["status"] == "deleted":
        kb.row(InlineKeyboardButton(text="♻️ Восстановить", callback_data=f"rvrestore_{review_id}"))
    else:
        kb.row(InlineKeyboardButton(text="❌ Удалить", callback_data=f"rvask_{review_id}"))
    kb.row(InlineKeyboardButton(text="⬅️ К списку", callback_data="a_reviews"))
    await c.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)
    await c.answer()


@dp.callback_query(F.data.startswith("rvask_"), F.from_user.id.in_(ADMIN_IDS))
async def adm_review_delete_ask(c: types.CallbackQuery):
    review_id = int(c.data[len("rvask_"):])
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"rvdel_{review_id}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"rvopen_{review_id}"),
    )
    await c.message.edit_text(
        f"<b>⚠️ Удалить отзыв #{review_id} с публичной страницы?</b>\n\nМожно будет восстановить.",
        reply_markup=kb.as_markup(),
        parse_mode=ParseMode.HTML,
    )
    await c.answer()


@dp.callback_query(F.data.startswith("rvdel_"), F.from_user.id.in_(ADMIN_IDS))
async def adm_review_delete(c: types.CallbackQuery):
    review_id = int(c.data[len("rvdel_"):])
    set_review_status(review_id, "deleted")
    await c.message.edit_text(
        "<b>✅ Отзыв удалён с публичной страницы.</b>", reply_markup=_reviews_list_kb(), parse_mode=ParseMode.HTML,
    )
    await c.answer("Удалено")


@dp.callback_query(F.data.startswith("rvrestore_"), F.from_user.id.in_(ADMIN_IDS))
async def adm_review_restore(c: types.CallbackQuery):
    review_id = int(c.data[len("rvrestore_"):])
    set_review_status(review_id, "published")
    await c.message.edit_text(
        "<b>✅ Отзыв восстановлен.</b>", reply_markup=_reviews_list_kb(), parse_mode=ParseMode.HTML,
    )
    await c.answer("Восстановлено")


# 👑 АДМИН-ПАНЕЛЬ (создание саб-панелей, доступы, журнал действий) — только для владельца
@dp.callback_query(F.data == "a_adminpanels", F.from_user.id.in_(OWNER_IDS))
async def adm_panels_entry(c: types.CallbackQuery, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🏠 Моя панель", callback_data="mypanel"))
    kb.row(InlineKeyboardButton(text="➕ Создать панель", callback_data="panel_add"))
    kb.row(InlineKeyboardButton(text="📋 Список панелей", callback_data="panel_list"))
    kb.row(InlineKeyboardButton(text="📜 Журнал действий", callback_data="panel_log"))
    kb.row(InlineKeyboardButton(text="🔧 Чат для логов", callback_data="panel_logchat"))
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="a_menu"))
    text = (
        "<b>👑 Админ-панель</b>\n\n"
        "Создавайте отдельные панели с ограниченным набором функций для других людей и назначайте им доступ. "
        "Все их нажатия попадают в журнал (и в чат логов, если он задан)."
    )
    try:
        await c.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)
    except Exception:
        await c.message.answer(text, reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)
    await c.answer()


@dp.callback_query(F.data == "mypanel", F.from_user.id.in_(OWNER_IDS))
async def adm_mypanel(c: types.CallbackQuery):
    has_pass = bool(db.get_setting('owner_password'))
    owners = get_co_owners()
    owners_text = "\n".join(f"• <code>{o['tg_id']}</code>" for o in owners) or "— пока никого"
    is_root = c.from_user.id in ROOT_OWNER_IDS
    text = (
        "<b>🏠 Моя панель</b>\n\n"
        f"🔑 Пароль на панель: {'установлен' if has_pass else 'не задан'}\n\n"
        f"👑 Полноправные со-владельцы:\n{owners_text}"
    )
    kb = InlineKeyboardBuilder()
    if is_root:
        text += (
            "\n\n⚠️ Пароль спрашивается у со-владельцев при входе в /admin. У вас, настоящего владельца, — никогда.\n\n"
            "Нажмите кнопку — бот попросит прислать нужные данные. Либо используйте команды напрямую:\n"
            "<code>/setpass пароль</code>, <code>/delpass</code>, "
            "<code>/addowner id1 id2 ...</code>, <code>/delowner id1 id2 ...</code>"
        )
        kb.row(InlineKeyboardButton(text="🔑 Установить/изменить пароль", callback_data="mp_setpass"))
        if has_pass:
            kb.row(InlineKeyboardButton(text="🗑 Удалить пароль", callback_data="mp_delpass"))
        kb.row(InlineKeyboardButton(text="➕ Добавить со-владельцев", callback_data="mp_addowner"))
        if owners:
            kb.row(InlineKeyboardButton(text="➖ Убрать со-владельцев", callback_data="mp_delowner"))
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="a_adminpanels"))
    try:
        await c.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)
    except Exception:
        await c.message.answer(text, reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)
    await c.answer()


@dp.callback_query(F.data == "mp_setpass", F.from_user.id.in_(ROOT_OWNER_IDS))
async def adm_mp_setpass_start(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.mp_setpass)
    await c.message.answer("<b>🔑 Пришлите новый пароль для панели (просто текстом):</b>", parse_mode=ParseMode.HTML)
    await c.answer()


@dp.message(AdminStates.mp_setpass, F.from_user.id.in_(ROOT_OWNER_IDS))
async def adm_mp_setpass_save(m: types.Message, state: FSMContext):
    await state.clear()
    pwd = (m.text or "").strip()
    if not pwd:
        return await m.answer("<b>❌ Пустой пароль.</b>", parse_mode=ParseMode.HTML)
    set_owner_password(pwd)
    await m.answer("<b>✅ Пароль установлен/изменён.</b>", parse_mode=ParseMode.HTML)


@dp.callback_query(F.data == "mp_delpass", F.from_user.id.in_(ROOT_OWNER_IDS))
async def adm_mp_delpass(c: types.CallbackQuery):
    clear_owner_password()
    await c.message.answer("<b>✅ Пароль удалён.</b>", parse_mode=ParseMode.HTML)
    await c.answer()


@dp.callback_query(F.data == "mp_addowner", F.from_user.id.in_(ROOT_OWNER_IDS))
async def adm_mp_addowner_start(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.mp_addowner)
    await c.message.answer(
        "<b>➕ Пришлите Telegram ID (можно сразу несколько через пробел):</b>", parse_mode=ParseMode.HTML,
    )
    await c.answer()


@dp.message(AdminStates.mp_addowner, F.from_user.id.in_(ROOT_OWNER_IDS))
async def adm_mp_addowner_save(m: types.Message, state: FSMContext):
    await state.clear()
    ids = (m.text or "").split()
    if not ids or not all(i.isdigit() for i in ids):
        return await m.answer("<b>❌ Нужны числовые ID через пробел.</b>", parse_mode=ParseMode.HTML)
    added = []
    for id_str in ids:
        uid = int(id_str)
        add_co_owner(uid)
        added.append(uid)
        try:
            await bot_client.send_message(
                uid, "<b>👑 Вам выдан полный доступ к админ-панели бота. Откройте /admin</b>", parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
    names = "\n".join(f"• <code>{uid}</code>" for uid in added)
    await m.answer(f"<b>✅ Добавлены как полноправные со-владельцы:</b>\n{names}", parse_mode=ParseMode.HTML)


@dp.callback_query(F.data == "mp_delowner", F.from_user.id.in_(ROOT_OWNER_IDS))
async def adm_mp_delowner_start(c: types.CallbackQuery, state: FSMContext):
    owners = get_co_owners()
    kb = InlineKeyboardBuilder()
    for o in owners:
        kb.row(InlineKeyboardButton(text=f"➖ {o['tg_id']}", callback_data=f"mp_delowner1_{o['tg_id']}"))
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="mypanel"))
    await c.message.answer("<b>➖ Кого убрать из со-владельцев?</b>", reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)
    await c.answer()


@dp.callback_query(F.data.startswith("mp_delowner1_"), F.from_user.id.in_(ROOT_OWNER_IDS))
async def adm_mp_delowner_one(c: types.CallbackQuery):
    uid = int(c.data[len("mp_delowner1_"):])
    remove_co_owner(uid)
    await c.message.edit_text(f"<b>✅ {uid} убран из полноправных со-владельцев.</b>", parse_mode=ParseMode.HTML)
    await c.answer("Убрано")


def _section_picker_kb(selected):
    kb = InlineKeyboardBuilder()
    for group_label, _cb, leaves in PANEL_GROUPS:
        kb.row(InlineKeyboardButton(text=f"— {group_label} —", callback_data="noop"))
        for leaf_key in leaves:
            leaf_label = PANEL_LEAVES[leaf_key][0]
            mark = "✅" if leaf_key in selected else "▫️"
            kb.row(InlineKeyboardButton(text=f"{mark} {leaf_label}", callback_data=f"psect_{leaf_key}"))
    kb.row(InlineKeyboardButton(text="💾 Готово", callback_data="psecdone"))
    kb.row(InlineKeyboardButton(text="❌ Отмена", callback_data="pseccancel"))
    return kb.as_markup()


@dp.callback_query(F.data == "panel_add", F.from_user.id.in_(OWNER_IDS))
async def adm_panel_add_start(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.panel_name)
    await c.message.answer("<b>➕ Название новой панели:</b>", parse_mode=ParseMode.HTML)
    await c.answer()


@dp.message(AdminStates.panel_name, F.from_user.id.in_(OWNER_IDS))
async def adm_panel_add_name(m: types.Message, state: FSMContext):
    name = (m.text or "").strip()
    if not name:
        return await m.answer("<b>❌ Пустое название.</b>", parse_mode=ParseMode.HTML)
    await state.set_state(None)
    await state.update_data(panel_edit_id=None, panel_name=name, panel_sections=[])
    await m.answer(
        f"<b>➕ Панель «{name}»</b>\n\nВыберите функции для этой панели:",
        reply_markup=_section_picker_kb([]),
        parse_mode=ParseMode.HTML,
    )


@dp.callback_query(F.data.startswith("psect_"), F.from_user.id.in_(OWNER_IDS))
async def adm_panel_sec_toggle(c: types.CallbackQuery, state: FSMContext):
    key = c.data[len("psect_"):]
    if key not in PANEL_LEAVES:
        return await c.answer()
    data = await state.get_data()
    sections = set(data.get("panel_sections", []))
    if key in sections:
        sections.discard(key)
    else:
        sections.add(key)
    await state.update_data(panel_sections=list(sections))
    name = data.get("panel_name", "")
    try:
        await c.message.edit_text(
            f"<b>➕ Панель «{name}»</b>\n\nВыберите функции для этой панели:",
            reply_markup=_section_picker_kb(sections),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass
    await c.answer()


async def _render_panel_detail(panel_id: int):
    panel = get_custom_panel(panel_id)
    if not panel:
        return None, None
    sections = panel_sections_list(panel)
    labels = [PANEL_LEAVES[s][0] for s in sections if s in PANEL_LEAVES]
    admins = get_panel_admins(panel_id)
    admins_text = "\n".join(f"• <code>{a['tg_id']}</code>" for a in admins) or "— пока никого"
    text = (
        f"<b>👑 Панель «{panel['name']}»</b>\n\n"
        f"🔘 Функции:\n" + ("\n".join(f"• {l}" for l in labels) or "— ничего не выбрано") + "\n\n"
        f"👥 Админы:\n{admins_text}\n\n"
        f"🔑 Пароль: {'установлен' if panel['password'] else 'не задан'}\n"
        f"📅 Создана: {panel['created_at']}"
    )
    return text, _panel_detail_kb(panel_id)


def _panel_detail_kb(panel_id: int):
    panel = get_custom_panel(panel_id)
    has_pass = bool(panel and panel["password"])
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="✏️ Изменить функции", callback_data=f"panel_edit_{panel_id}"))
    kb.row(InlineKeyboardButton(
        text="🔑 Изменить пароль" if has_pass else "🔑 Установить пароль", callback_data=f"panel_setpass_{panel_id}",
    ))
    if has_pass:
        kb.row(InlineKeyboardButton(text="🗑 Удалить пароль", callback_data=f"panel_delpass_{panel_id}"))
    kb.row(InlineKeyboardButton(text="➕ Добавить админа", callback_data=f"panel_addadmin_{panel_id}"))
    for a in get_panel_admins(panel_id):
        kb.row(InlineKeyboardButton(text=f"➖ Убрать {a['tg_id']}", callback_data=f"panel_deladmin_{a['tg_id']}_{panel_id}"))
    kb.row(InlineKeyboardButton(text="🚪 Войти в эту панель", callback_data=f"panel_enter_{panel_id}"))
    kb.row(InlineKeyboardButton(text="❌ Удалить панель", callback_data=f"panel_del_{panel_id}"))
    kb.row(InlineKeyboardButton(text="⬅️ К списку", callback_data="panel_list"))
    return kb.as_markup()


@dp.callback_query(F.data == "psecdone", F.from_user.id.in_(OWNER_IDS))
async def adm_panel_sec_done(c: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    sections = data.get("panel_sections", [])
    edit_id = data.get("panel_edit_id")
    name = data.get("panel_name", "")
    await state.clear()
    if not sections:
        return await c.answer("❌ Выберите хотя бы одну функцию", show_alert=True)
    if edit_id:
        set_panel_sections(edit_id, sections)
        panel_id = edit_id
    else:
        panel_id = create_custom_panel(name, sections)
    text, kb = await _render_panel_detail(panel_id)
    await c.message.edit_text(f"<b>✅ Сохранено.</b>\n\n{text}", reply_markup=kb, parse_mode=ParseMode.HTML)
    await c.answer()


@dp.callback_query(F.data == "pseccancel", F.from_user.id.in_(OWNER_IDS))
async def adm_panel_sec_cancel(c: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await adm_panels_entry(c, state)


def _panel_list_kb():
    kb = InlineKeyboardBuilder()
    for p in get_custom_panels():
        admins_count = len(get_panel_admins(p["id"]))
        kb.row(InlineKeyboardButton(text=f"{p['name']} ({admins_count} чел.)", callback_data=f"panel_open_{p['id']}"))
    kb.row(InlineKeyboardButton(text="➕ Создать панель", callback_data="panel_add"))
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="a_adminpanels"))
    return kb.as_markup()


@dp.callback_query(F.data == "panel_list", F.from_user.id.in_(OWNER_IDS))
async def adm_panel_list(c: types.CallbackQuery):
    text = "<b>📋 Панели</b>\n\nВыберите панель для управления." if get_custom_panels() else "<b>📋 Панели</b>\n\nПока ни одной не создано."
    try:
        await c.message.edit_text(text, reply_markup=_panel_list_kb(), parse_mode=ParseMode.HTML)
    except Exception:
        await c.message.answer(text, reply_markup=_panel_list_kb(), parse_mode=ParseMode.HTML)
    await c.answer()


@dp.callback_query(F.data.startswith("panel_open_"), F.from_user.id.in_(OWNER_IDS))
async def adm_panel_open(c: types.CallbackQuery):
    panel_id = int(c.data[len("panel_open_"):])
    text, kb = await _render_panel_detail(panel_id)
    if not text:
        return await c.answer("❌ Панель не найдена", show_alert=True)
    try:
        await c.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception:
        await c.message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    await c.answer()


@dp.callback_query(F.data.startswith("panel_edit_"), F.from_user.id.in_(OWNER_IDS))
async def adm_panel_edit_start(c: types.CallbackQuery, state: FSMContext):
    panel_id = int(c.data[len("panel_edit_"):])
    panel = get_custom_panel(panel_id)
    if not panel:
        return await c.answer("❌ Панель не найдена", show_alert=True)
    sections = panel_sections_list(panel)
    await state.update_data(panel_edit_id=panel_id, panel_name=panel["name"], panel_sections=sections)
    await c.message.edit_text(
        f"<b>✏️ Панель «{panel['name']}»</b>\n\nВыберите функции:",
        reply_markup=_section_picker_kb(sections),
        parse_mode=ParseMode.HTML,
    )
    await c.answer()


@dp.callback_query(F.data.startswith("panel_addadmin_"), F.from_user.id.in_(OWNER_IDS))
async def adm_panel_addadmin_start(c: types.CallbackQuery, state: FSMContext):
    panel_id = int(c.data[len("panel_addadmin_"):])
    await state.update_data(panel_addadmin_id=panel_id)
    await state.set_state(AdminStates.panel_add_admin)
    await c.message.answer(
        "<b>➕ Пришлите Telegram ID (можно сразу несколько через пробел) — их назначить админами этой панели:</b>",
        parse_mode=ParseMode.HTML,
    )
    await c.answer()


@dp.message(AdminStates.panel_add_admin, F.from_user.id.in_(OWNER_IDS))
async def adm_panel_addadmin_save(m: types.Message, state: FSMContext):
    data = await state.get_data()
    panel_id = data.get("panel_addadmin_id")
    await state.clear()
    ids = (m.text or "").split()
    if not panel_id or not ids or not all(i.isdigit() for i in ids):
        return await m.answer("<b>❌ Пришлите один или несколько числовых Telegram ID через пробел.</b>", parse_mode=ParseMode.HTML)

    added = []
    skipped_owners = []
    for id_str in ids:
        uid = int(id_str)
        if uid in OWNER_IDS:
            skipped_owners.append(uid)
            continue
        add_sub_admin(uid, panel_id)
        added.append(uid)
        try:
            await bot_client.send_message(
                uid, "<b>🛠 Вам выдан доступ к админ-панели бота. Откройте /admin</b>", parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    text, kb = await _render_panel_detail(panel_id)
    summary = "\n".join(f"• <code>{uid}</code>" for uid in added) or "— никого (все указанные уже владельцы)"
    await m.answer(f"<b>✅ Добавлены админы:</b>\n{summary}\n\n{text}", reply_markup=kb, parse_mode=ParseMode.HTML)


@dp.callback_query(F.data.startswith("panel_setpass_"), F.from_user.id.in_(OWNER_IDS))
async def adm_panel_setpass_start(c: types.CallbackQuery, state: FSMContext):
    panel_id = int(c.data[len("panel_setpass_"):])
    await state.update_data(panel_setpass_id=panel_id)
    await state.set_state(AdminStates.panel_setpass)
    await c.message.answer("<b>🔑 Пришлите пароль для этой панели:</b>", parse_mode=ParseMode.HTML)
    await c.answer()


@dp.message(AdminStates.panel_setpass, F.from_user.id.in_(OWNER_IDS))
async def adm_panel_setpass_save(m: types.Message, state: FSMContext):
    data = await state.get_data()
    panel_id = data.get("panel_setpass_id")
    await state.clear()
    pwd = (m.text or "").strip()
    if not panel_id or not pwd:
        return await m.answer("<b>❌ Пустой пароль.</b>", parse_mode=ParseMode.HTML)
    set_panel_password(panel_id, pwd)
    text, kb = await _render_panel_detail(panel_id)
    await m.answer(f"<b>✅ Пароль для панели установлен.</b>\n\n{text}", reply_markup=kb, parse_mode=ParseMode.HTML)


@dp.callback_query(F.data.startswith("panel_delpass_"), F.from_user.id.in_(OWNER_IDS))
async def adm_panel_delpass(c: types.CallbackQuery):
    panel_id = int(c.data[len("panel_delpass_"):])
    clear_panel_password(panel_id)
    text, kb = await _render_panel_detail(panel_id)
    await c.message.edit_text(f"<b>✅ Пароль удалён.</b>\n\n{text}", reply_markup=kb, parse_mode=ParseMode.HTML)
    await c.answer("Удалено")


@dp.callback_query(F.data.startswith("panel_deladmin_"), F.from_user.id.in_(OWNER_IDS))
async def adm_panel_deladmin(c: types.CallbackQuery):
    rest = c.data[len("panel_deladmin_"):]
    uid_str, panel_id_str = rest.rsplit("_", 1)
    remove_sub_admin(int(uid_str))
    text, kb = await _render_panel_detail(int(panel_id_str))
    await c.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    await c.answer("Удалено")


@dp.callback_query(F.data.startswith("panel_enter_"), F.from_user.id.in_(OWNER_IDS))
async def adm_panel_enter(c: types.CallbackQuery):
    panel_id = int(c.data[len("panel_enter_"):])
    panel = get_custom_panel(panel_id)
    if not panel:
        return await c.answer("❌ Панель не найдена", show_alert=True)
    allowed = set(panel_sections_list(panel))
    kb = InlineKeyboardBuilder()
    for group_label, group_cb, leaves in PANEL_GROUPS:
        if any(leaf in allowed for leaf in leaves):
            kb.row(InlineKeyboardButton(text=group_label, callback_data=group_cb))
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"panel_open_{panel_id}"))
    await c.message.edit_text(
        f"<b>🚪 Вы вошли в панель «{panel['name']}» (просмотр глазами саб-админа)</b>",
        reply_markup=kb.as_markup(),
        parse_mode=ParseMode.HTML,
    )
    await c.answer()


@dp.callback_query(F.data.startswith("panel_del_"), F.from_user.id.in_(OWNER_IDS))
async def adm_panel_delete(c: types.CallbackQuery):
    panel_id = int(c.data[len("panel_del_"):])
    delete_custom_panel(panel_id)
    await c.message.edit_text(
        "<b>✅ Панель удалена (её админы потеряли доступ).</b>",
        reply_markup=_panel_list_kb(),
        parse_mode=ParseMode.HTML,
    )
    await c.answer("Удалено")


@dp.callback_query(F.data == "panel_log", F.from_user.id.in_(OWNER_IDS))
async def adm_panel_log(c: types.CallbackQuery):
    rows = get_admin_log(30)
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="a_adminpanels"))
    if not rows:
        text = "<b>📜 Журнал действий</b>\n\nПока пусто."
    else:
        lines = [f"• <code>{r['tg_id']}</code> — <code>{r['action']}</code> — {r['created_at']}" for r in rows]
        text = "<b>📜 Журнал действий (последние 30)</b>\n\n" + "\n".join(lines)
    try:
        await c.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)
    except Exception:
        await c.message.answer(text, reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)
    await c.answer()


@dp.callback_query(F.data == "panel_logchat", F.from_user.id.in_(OWNER_IDS))
async def adm_panel_logchat_start(c: types.CallbackQuery, state: FSMContext):
    current = db.get_setting('admin_log_chat')
    await state.set_state(AdminStates.panel_logchat)
    await c.message.answer(
        f"<b>🔧 Текущий чат логов: {current or 'не задан'}</b>\n\n"
        f"Пришлите числовой ID чата/канала (бот должен быть там участником/админом), или <code>0</code>, чтобы отключить:",
        parse_mode=ParseMode.HTML,
    )
    await c.answer()


@dp.message(AdminStates.panel_logchat, F.from_user.id.in_(OWNER_IDS))
async def adm_panel_logchat_save(m: types.Message, state: FSMContext):
    await state.clear()
    text = (m.text or "").strip()
    if not text.lstrip("-").isdigit():
        return await m.answer("<b>❌ Нужен числовой ID (например -1001234567890), или 0.</b>", parse_mode=ParseMode.HTML)
    val = "" if text == "0" else text

    if val:
        try:
            await bot_client.send_message(int(val), "<b>✅ Этот чат подключён как чат логов админ-панели.</b>", parse_mode=ParseMode.HTML)
        except Exception as e:
            return await m.answer(
                f"<b>❌ Не получилось отправить сообщение в этот чат.</b>\n\n"
                f"Ошибка: <code>{e}</code>\n\n"
                f"Проверьте, что бот добавлен в этот чат/канал и, если это канал — назначен там админом.",
                parse_mode=ParseMode.HTML,
            )

    db.conn.execute(
        "INSERT INTO settings(key, value) VALUES ('admin_log_chat', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (val,),
    )
    db.conn.commit()
    msg = "<b>✅ Чат логов отключён.</b>" if not val else f"<b>✅ Чат логов установлен и проверен: {val}</b>"
    await m.answer(msg, parse_mode=ParseMode.HTML)


# 🌐 МИНИ-АПП ОТЗЫВОВ
WEB_PORT = 8082


async def api_reviews(request: web.Request) -> web.Response:
    published = get_reviews_by_status("published")
    pos = sum(1 for r in published if r["is_positive"])
    neg = len(published) - pos

    filt = request.query.get("filter", "all")
    if filt == "positive":
        rows = [r for r in published if r["is_positive"]]
    elif filt == "negative":
        rows = [r for r in published if not r["is_positive"]]
    else:
        rows = published

    data = {
        "counts": {"all": len(published), "positive": pos, "negative": neg},
        "reviews": [
            {
                "id": r["id"],
                "order": r["id"],
                "amount": r["amount"],
                "is_positive": bool(r["is_positive"]),
                "text": r["text"] or "",
                "date": (r["created_at"] or "").split(" ")[0],
            }
            for r in rows
        ],
    }
    return web.json_response(data)


async def index_page(request: web.Request) -> web.Response:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp", "index.html")
    return web.FileResponse(path)


def _client_ip(request: web.Request) -> str:
    fwd = request.headers.get("X-Forwarded-For")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote or "?"


async def api_visit(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        body = {}

    init_data = body.get("init_data") or ""
    parsed = validate_webapp_init_data(init_data)
    tg_id = None
    username = None
    verified = False
    is_premium = False
    tg_language_code = ""
    allows_write_to_pm = False
    if parsed:
        verified = True
        try:
            user = json.loads(parsed.get("user", "{}"))
            tg_id = user.get("id")
            username = user.get("username")
            is_premium = bool(user.get("is_premium"))
            tg_language_code = user.get("language_code") or ""
            allows_write_to_pm = bool(user.get("allows_write_to_pm"))
        except Exception:
            pass

    ip = _client_ip(request)
    geo = await geolocate_ip(ip)

    def s(key, limit):
        return str(body.get(key) if body.get(key) is not None else "")[:limit]

    log_visit(
        tg_id=tg_id,
        username=username,
        verified=verified,
        platform=str(body.get("platform"))[:50],
        color_scheme=str(body.get("color_scheme"))[:20],
        tg_version=str(body.get("version"))[:20],
        ip=ip,
        user_agent=request.headers.get("User-Agent", "")[:300],
        timezone=str(body.get("timezone"))[:50],
        screen=str(body.get("screen"))[:30],
        country=geo.get("country", ""),
        city=geo.get("city", ""),
        region=geo.get("region", ""),
        isp=geo.get("isp", ""),
        avail_screen=s("avail_screen", 30),
        language=s("language", 20),
        languages=s("languages", 100),
        device_memory=s("device_memory", 10),
        cpu_cores=s("cpu_cores", 10),
        touch_points=s("touch_points", 10),
        pixel_ratio=s("pixel_ratio", 10),
        connection_type=s("connection_type", 20),
        referrer=s("referrer", 300),
        user_platform=s("user_platform", 50),
        vendor=s("vendor", 50),
        viewport_height=s("viewport_height", 10),
        is_expanded=s("is_expanded", 10),
        is_premium=is_premium,
        tg_language_code=tg_language_code[:10],
        allows_write_to_pm=allows_write_to_pm,
        ua_model=s("ua_model", 60),
        ua_platform_version=s("ua_platform_version", 30),
        ua_full_version=s("ua_full_version", 300),
    )
    return web.json_response({"ok": True})


async def start_web_app():
    app = web.Application()
    app.router.add_get("/", index_page)
    app.router.add_get("/api/reviews", api_reviews)
    app.router.add_post("/api/visit", api_visit)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEB_PORT)
    await site.start()
    logging.info(f"Веб-сервер отзывов запущен на порту {WEB_PORT}")


# 🚀 ЗАПУСК
async def main():
    if not os.path.exists("photos"):
        os.makedirs("photos")
    logging.info("Бот запущен.")
    await bot_client.delete_webhook(drop_pending_updates=True)
    await start_web_app()
    await dp.start_polling(bot_client)


if __name__ == "__main__":
    asyncio.run(main())
