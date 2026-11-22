# ZEX bot build v38 - moderation reports and timed restrictions
# -*- coding: utf-8 -*-
# ربات ZEX - نسخه یکپارچه و اصلاح‌شده
# قبل از اجرا، TOKEN و GROUP_LINK را تنظیم کنید.

import os
import re
import time
import sqlite3
import threading
import random
import uuid
import math
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from html import escape
from urllib.parse import quote

# نمایش تمام تاریخ‌ها با تقویم شمسی و منطقه زمانی ایران
try:
    IRAN_TZ = ZoneInfo("Asia/Tehran")
except Exception:
    # اگر دیتابیس منطقه‌های زمانی در ترموکس نصب نبود، از ساعت رسمی فعلی ایران استفاده کن.
    IRAN_TZ = timezone(timedelta(hours=3, minutes=30), name="IRST")

def _gregorian_to_jalali(gy, gm, gd):
    g_days_in_month = [31, 29 if (gy % 4 == 0 and (gy % 100 != 0 or gy % 400 == 0)) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    j_days_in_month = [31, 31, 31, 31, 31, 31, 30, 30, 30, 30, 30, 29]
    gy2 = gy - 1600
    gm2 = gm - 1
    gd2 = gd - 1
    g_day_no = 365 * gy2 + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400
    for i in range(gm2):
        g_day_no += g_days_in_month[i]
    g_day_no += gd2
    j_day_no = g_day_no - 79
    j_np = j_day_no // 12053
    j_day_no %= 12053
    jy = 979 + 33 * j_np + 4 * (j_day_no // 1461)
    j_day_no %= 1461
    if j_day_no >= 366:
        jy += (j_day_no - 1) // 365
        j_day_no = (j_day_no - 1) % 365
    jm = 0
    while jm < 11 and j_day_no >= j_days_in_month[jm]:
        j_day_no -= j_days_in_month[jm]
        jm += 1
    return jy, jm + 1, j_day_no + 1

def format_iran_datetime(value, include_seconds=False):
    """تبدیل timestamp یا تاریخ SQLite به تاریخ شمسی و ساعت ایران."""
    if value is None or value == "":
        return "نامشخص"
    try:
        if isinstance(value, (int, float)) or str(value).strip().isdigit():
            dt = datetime.fromtimestamp(float(value), tz=timezone.utc).astimezone(IRAN_TZ)
        else:
            raw = str(value).strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                # تاریخ‌های SQLite از datetime('now') به‌صورت UTC ذخیره شده‌اند.
                dt = dt.replace(tzinfo=timezone.utc)
            dt = dt.astimezone(IRAN_TZ)
        jy, jm, jd = _gregorian_to_jalali(dt.year, dt.month, dt.day)
        clock = dt.strftime("%H:%M:%S" if include_seconds else "%H:%M")
        return f"{jy:04d}/{jm:02d}/{jd:02d} {clock}"
    except (TypeError, ValueError, OverflowError, OSError):
        return str(value)


import telebot
from telebot import types
from flask import Flask

# ==============================
# تنظیمات
# ==============================

# توکن جدیدی را که از BotFather می‌گیرید اینجا قرار دهید.
TOKEN = "8732862876:AAGj735wPiK8PUYrvUZBvC3XYGwXYUcoxeg"
MANAGER_ID = 6961752872
GROUP_LINK = "https://t.me/sssssssssknj"
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ZEX.db")

# قیمت خرید و فروش هر ZEX به تومان.
BUY_PRICE_TOMAN = 20
SELL_PRICE_TOMAN = 17
# شماره کارت را خودت اینجا وارد کن؛ تا وقتی عوض نشده، ربات جای‌نگهدار نشان می‌دهد.
CARD_NUMBER = "6219861449588788"
CARD_OWNER = "جوهری"
# سازگاری با بخش‌های قدیمی کد
ZEX_PRICE = BUY_PRICE_TOMAN
TRANSFER_FEE_PERCENT = 10
MIN_SELL = 500
MAX_AMOUNT = 1_000_000_000

bot = telebot.TeleBot(TOKEN, parse_mode="HTML")
steps = {}
steps_lock = threading.Lock()
audit_alerts = set()


# ==============================
# دیتابیس
# ==============================

def connect_db():
    """اتصال به دیتابیس با پشتیبانی از استفاده هم‌زمان."""
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.execute("PRAGMA foreign_keys = ON")
    return db


def create_db():
    """ساخت جدول‌ها؛ داده‌های قبلی پاک نمی‌شوند."""
    with connect_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                balance INTEGER NOT NULL DEFAULT 0,
                role TEXT NOT NULL DEFAULT 'user',
                joined_at INTEGER NOT NULL,
                blocked INTEGER NOT NULL DEFAULT 0,
                debt INTEGER NOT NULL DEFAULT 0
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                reason TEXT,
                kind TEXT NOT NULL DEFAULT 'balance',
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                target_user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                total_price INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                handled_by INTEGER
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS debt_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS manager_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            )
        """)
        # سابقه هشدارها حتی پس از لغو یا اقدام مدیر باقی می‌ماند.
        db.execute("""
            CREATE TABLE IF NOT EXISTS warning_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                status TEXT NOT NULL DEFAULT 'open',
                handled_by INTEGER,
                handled_at TEXT
            )
        """)
        # گروه‌هایی که ربات در آن‌ها دیده شده تا مدیر بتواند حذف از گپ را امتحان کند.
        db.execute("""
            CREATE TABLE IF NOT EXISTS known_groups (
                chat_id INTEGER PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                seen_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
        """)
        # گزارش‌ها و اخطارهای مدیریتی گروه؛ مستقل از پیام تلگرام ذخیره می‌شوند.
        db.execute("""CREATE TABLE IF NOT EXISTS moderation_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, group_title TEXT NOT NULL DEFAULT '',
            message_id INTEGER NOT NULL, target_user_id INTEGER NOT NULL, target_name TEXT NOT NULL DEFAULT '',
            target_username TEXT NOT NULL DEFAULT '', reporter_id INTEGER NOT NULL, reporter_name TEXT NOT NULL DEFAULT '',
            message_text TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')) ,
            status TEXT NOT NULL DEFAULT 'pending', handled_by INTEGER, handled_action TEXT, handled_at TEXT
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS moderation_warnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL DEFAULT 0,
            report_id INTEGER, actor_id INTEGER NOT NULL, reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS moderation_restrictions (
            user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, kind TEXT NOT NULL,
            until_ts INTEGER NOT NULL, prior_blocked INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            PRIMARY KEY(user_id, chat_id, kind)
        )""")
        db.execute("""
            CREATE TABLE IF NOT EXISTS rocket_players (
                user_id INTEGER PRIMARY KEY,
                points INTEGER NOT NULL DEFAULT 0,
                auto_cashout REAL NOT NULL DEFAULT 0,
                total_games INTEGER NOT NULL DEFAULT 0,
                successful_cashouts INTEGER NOT NULL DEFAULT 0,
                group_wins INTEGER NOT NULL DEFAULT 0,
                best_multiplier REAL NOT NULL DEFAULT 0,
                total_score INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS rocket_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL DEFAULT '',
                result TEXT NOT NULL,
                stake INTEGER NOT NULL DEFAULT 0,
                multiplier REAL NOT NULL DEFAULT 1.0,
                score INTEGER NOT NULL DEFAULT 0,
                points_change INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS rocket_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        db.execute("INSERT OR IGNORE INTO rocket_settings(key,value) VALUES('immediate_crash_percent','50')")
        db.execute("""
            CREATE TABLE IF NOT EXISTS balance_audit_baseline (
                user_id INTEGER PRIMARY KEY,
                base_balance INTEGER NOT NULL,
                last_transaction_id INTEGER NOT NULL DEFAULT 0
            )
        """)
        # تاریخچه مستقل ZEXها؛ تراکنش‌های ZEX با تاریخچه ZEX قاطی نمی‌شوند.
        db.execute("""
            CREATE TABLE IF NOT EXISTS point_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        # کارت‌های مقصد پرداخت فروش؛ هر کاربر می‌تواند چند کارت ذخیره کند.
        db.execute("""
            CREATE TABLE IF NOT EXISTS payout_cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                card_number TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(user_id, card_number)
            )
        """)
        # کدهای تخفیف مدیر و مصرف هر کد توسط کاربران.
        db.execute("""
            CREATE TABLE IF NOT EXISTS discount_codes (
                code TEXT PRIMARY KEY,
                discount_amount INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS discount_uses (
                code TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                request_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(code, user_id)
            )
        """)
        # فیلدهای تکمیلی مدیریت کد تخفیف، سازگار با سه نوع LUX.
        _ensure_column(db, "discount_codes", "max_uses", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(db, "discount_codes", "used_count", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(db, "discount_codes", "expires_at", "INTEGER")
        db.execute("UPDATE discount_codes SET used_count=(SELECT COUNT(*) FROM discount_uses WHERE discount_uses.code=discount_codes.code) WHERE used_count=0")

        # آرشیو پیام‌هایی که مدیر اصلی در گفت‌وگوی خصوصی برای ربات می‌فرستد.
        db.execute("""
            CREATE TABLE IF NOT EXISTS admin_message_archive (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                content_type TEXT NOT NULL DEFAULT 'text',
                created_at INTEGER NOT NULL,
                UNIQUE(chat_id, message_id)
            )
        """)
        # طرح‌های پاداش عضویت زمان‌دار؛ تاریخ‌ها به‌صورت timestamp ذخیره می‌شوند.
        db.execute("""
            CREATE TABLE IF NOT EXISTS join_bonus_campaigns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                amount INTEGER NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                starts_at INTEGER NOT NULL,
                ends_at INTEGER NOT NULL,
                created_by INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'active'
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS join_bonus_rewards (
                campaign_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (campaign_id, user_id)
            )
        """)

        # افزودن ستون‌های موردنیاز به دیتابیس‌های قدیمی‌تر، بدون پاک‌کردن اطلاعات.
        _ensure_column(db, "users", "debt", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(db, "users", "game_points", "REAL NOT NULL DEFAULT 0")
        _ensure_column(db, "users", "zex_activity_at", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(db, "users", "bot_activity_at", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(db, "users", "game_activity_at", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(db, "users", "active_warning_at", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(db, "users", "blocked", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(db, "users", "card_number", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(db, "users", "referred_by", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(db, "users", "referral_rewarded", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(db, "users", "admin_since", "TEXT NOT NULL DEFAULT ''")
        db.execute("""CREATE TABLE IF NOT EXISTS balance_locks (
            user_id INTEGER PRIMARY KEY,
            request_id INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )""")
        # قفل سراسری دیتابیس: هیچ مسیر دیگری نتواند موجودی کاربر قفل‌شده را تغییر دهد.
        db.execute("""CREATE TRIGGER IF NOT EXISTS prevent_locked_balance_update
            BEFORE UPDATE OF balance ON users
            WHEN EXISTS (SELECT 1 FROM balance_locks WHERE user_id=OLD.user_id)
            BEGIN
                SELECT RAISE(ABORT, 'balance is locked for request review');
            END""")
        _ensure_column(db, "transactions", "reason", "TEXT")
        _ensure_column(db, "transactions", "kind", "TEXT NOT NULL DEFAULT 'balance'")
        _ensure_column(db, "debt_transactions", "reason", "TEXT")

        # سازگاری دیتابیس‌های قدیمی با جدول امتیاز موشکی؛ علت خطای تسویه در بعضی نسخه‌ها
        # می‌تواند نبودن ستون‌هایی باشد که در جدول قدیمی ساخته نشده‌اند.
        _ensure_column(db, "rocket_players", "points", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(db, "rocket_players", "auto_cashout", "REAL NOT NULL DEFAULT 0")
        _ensure_column(db, "rocket_players", "total_games", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(db, "rocket_players", "successful_cashouts", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(db, "rocket_players", "group_wins", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(db, "rocket_players", "best_multiplier", "REAL NOT NULL DEFAULT 0")
        _ensure_column(db, "rocket_players", "total_score", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(db, "rocket_players", "updated_at", "TEXT NOT NULL DEFAULT (datetime('now'))")
        _ensure_column(db, "rocket_history", "username", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(db, "rocket_history", "result", "TEXT NOT NULL DEFAULT 'unknown'")
        # بعضی دیتابیس‌های قدیمی ستون stake را اجباری دارند و INSERT بدون آن شکست می‌خورد.
        _ensure_column(db, "rocket_history", "stake", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(db, "rocket_history", "multiplier", "REAL NOT NULL DEFAULT 1.0")
        _ensure_column(db, "rocket_history", "score", "INTEGER NOT NULL DEFAULT 0")
        # بعضی دیتابیس‌های قدیمی ستون points_change را اجباری دارند.
        _ensure_column(db, "rocket_history", "points_change", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(db, "rocket_history", "created_at", "TEXT NOT NULL DEFAULT (datetime('now'))")

        # هماهنگ‌سازی جدول درخواست‌ها با دیتابیس‌های قدیمی؛ اطلاعات قبلی حذف نمی‌شوند.
        _ensure_column(db, "requests", "target_user_id", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(db, "requests", "total_price", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(db, "requests", "status", "TEXT NOT NULL DEFAULT 'pending'")
        _ensure_column(db, "requests", "created_at", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(db, "requests", "handled_by", "INTEGER")
        _ensure_column(db, "requests", "discount_code", "TEXT")
        _ensure_column(db, "requests", "discount_amount", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(db, "requests", "payout_card", "TEXT NOT NULL DEFAULT ''")

        # انتقال توضیحات قدیمی به ستون جدید و تبدیل زمان‌های قدیمی به متن قابل‌خواندن.
        transaction_columns = [row[1] for row in db.execute("PRAGMA table_info(transactions)").fetchall()]
        if "details" in transaction_columns:
            db.execute("UPDATE transactions SET reason=details WHERE reason IS NULL AND details IS NOT NULL")
        debt_columns = [row[1] for row in db.execute("PRAGMA table_info(debt_transactions)").fetchall()]
        if "details" in debt_columns:
            db.execute("UPDATE debt_transactions SET reason=details WHERE reason IS NULL AND details IS NOT NULL")
        db.execute("UPDATE transactions SET created_at=datetime(created_at, 'unixepoch') WHERE typeof(created_at)='integer' AND created_at > 1000000000")
        db.execute("UPDATE debt_transactions SET created_at=datetime(created_at, 'unixepoch') WHERE typeof(created_at)='integer' AND created_at > 1000000000")

        # ثبت مدیر اصلی و حفظ نقش او.
        db.execute("""
            INSERT OR IGNORE INTO users
            (user_id, username, first_name, balance, role, joined_at, blocked, debt)
            VALUES (?, NULL, 'مدیر اصلی', 0, 'manager', ?, 0, 0)
        """, (MANAGER_ID, int(time.time())))
        db.execute("UPDATE users SET role='manager', blocked=0 WHERE user_id=?", (MANAGER_ID,))

        # برای کاربران قدیمی، موجودی فعلی نقطه شروع ممیزی است.
        users = db.execute("SELECT user_id, balance FROM users").fetchall()
        for user_id, balance in users:
            last_id = db.execute(
                "SELECT COALESCE(MAX(id), 0) FROM transactions WHERE user_id=?",
                (user_id,)
            ).fetchone()[0]
            db.execute("""
                INSERT OR IGNORE INTO balance_audit_baseline
                (user_id, base_balance, last_transaction_id) VALUES (?, ?, ?)
            """, (user_id, balance, last_id))


def _ensure_column(db, table, column, definition):
    columns = [row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def get_user(user_id):
    with connect_db() as db:
        return db.execute("""
            SELECT user_id, username, first_name, balance, role, joined_at, blocked, debt
            FROM users WHERE user_id=?
        """, (user_id,)).fetchone()


def valid_card_number(card):
    # فقط قالب ۱۶ رقمی و رقم کنترلی کارت را بررسی می‌کند؛ وجود واقعی کارت را تأیید نمی‌کند.
    if not re.fullmatch(r"[0-9]{16}", card):
        return False
    total = 0
    for index, char in enumerate(reversed(card)):
        digit = int(char)
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def get_card_number(user_id):
    # شماره کارت احراز‌شدهٔ کاربر را می‌خوانیم.
    with connect_db() as db:
        row = db.execute("SELECT card_number FROM users WHERE user_id=?", (user_id,)).fetchone()
    return (row[0] or "") if row else ""


def get_payout_cards(user_id):
    # کارت‌های مقصد ذخیره‌شده را برای انتخاب در زمان فروش می‌خوانیم.
    with connect_db() as db:
        return db.execute(
            "SELECT id, card_number FROM payout_cards WHERE user_id=? ORDER BY id DESC",
            (user_id,)
        ).fetchall()


def ask_payout_card(chat_id, user_id):
    # هر بار فروش، اول کارت احراز هویت‌شده و سپس کارت‌های ذخیره‌شده را به شکل دکمه نشان می‌دهیم.
    verified_card = get_card_number(user_id)
    cards = get_payout_cards(user_id)
    keyboard = types.InlineKeyboardMarkup()

    # کارت احراز هویت همیشه اولین گزینه است.
    if verified_card:
        masked = verified_card[:4] + " **** **** " + verified_card[-4:]
        keyboard.add(types.InlineKeyboardButton(
            "💳 کارت احراز هویت: " + masked,
            callback_data="payout_use_verified"
        ))

    # کارت‌هایی که قبلاً اضافه شده‌اند، گزینه‌های بعدی هستند.
    for card_id, card_number in cards:
        if card_number == verified_card:
            continue
        masked = card_number[:4] + " **** **** " + card_number[-4:]
        keyboard.add(types.InlineKeyboardButton(
            "💳 " + masked,
            callback_data=f"payout_use_{card_id}"
        ))

    # افزودن کارت هم همیشه آخر فهرست نمایش داده می‌شود.
    keyboard.add(types.InlineKeyboardButton("➕ افزودن شماره کارت", callback_data="payout_add"))
    bot.send_message(chat_id, "💳 کارت مقصد این فروش را انتخاب کن:", reply_markup=keyboard)


def ask_sell_amount(chat_id, user_id, payout_card):
    # کارت انتخاب‌شده را نگه می‌داریم و صفحه‌کلید عددی را نشان می‌دهیم.
    user = get_user(user_id)
    steps[user_id] = {"action": "sell_amount", "payout_card": payout_card, "amount_text": ""}
    show_amount_keypad(chat_id, user_id)


def find_username(username):
    username = username.strip().lstrip("@").lower()
    with connect_db() as db:
        return db.execute("""
            SELECT user_id, username, first_name, balance, role, joined_at, blocked, debt
            FROM users WHERE LOWER(username)=?
        """, (username,)).fetchone()


def add_transaction(db, user_id, amount, reason, kind="balance"):
    """ثبت تراکنش سازگار با ساختار دیتابیس‌های قدیمی و جدید."""
    columns = [row[1] for row in db.execute("PRAGMA table_info(transactions)").fetchall()]
    fields = ["user_id", "amount"]
    values = [user_id, amount]
    placeholders = ["?", "?"]

    # در دیتابیس‌های مختلف ممکن است reason، details یا هر دو وجود داشته باشند.
    # هر دو را پر می‌کنیم تا با ستون اجباری دیتابیس قدیمی هم سازگار بماند.
    if "reason" in columns:
        fields.append("reason")
        values.append(reason)
        placeholders.append("?")
    if "details" in columns:
        fields.append("details")
        values.append(reason)
        placeholders.append("?")

    # در دیتابیس قدیمی، kind اجباری است؛ پس آن را هم صریح وارد می‌کنیم.
    if "kind" in columns:
        fields.append("kind")
        values.append(kind)
        placeholders.append("?")

    # بعضی نسخه‌های قدیمی دیتابیس ستون balance_after را اجباری دارند.
    # موجودی فعلی را ذخیره می‌کنیم؛ این تابع پس از تغییر موجودی فراخوانی می‌شود.
    if "balance_after" in columns:
        current = db.execute(
            "SELECT balance FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        fields.append("balance_after")
        values.append(current[0] if current else 0)
        placeholders.append("?")

    if "created_at" in columns:
        fields.append("created_at")
        placeholders.append("datetime('now')")

    sql = f"INSERT INTO transactions ({', '.join(fields)}) VALUES ({', '.join(placeholders)})"
    db.execute(sql, tuple(values))


def register_user(user):
    now = int(time.time())
    with connect_db() as db:
        old = db.execute("SELECT user_id FROM users WHERE user_id=?", (user.id,)).fetchone()
        if old:
            db.execute("""
                UPDATE users SET username=?, first_name=? WHERE user_id=?
            """, (user.username, user.first_name or "بدون نام", user.id))
        else:
            role = "manager" if user.id == MANAGER_ID else "user"
            db.execute("""
                INSERT INTO users
                (user_id, username, first_name, balance, role, joined_at, blocked, debt)
                VALUES (?, ?, ?, 0, ?, ?, 0, 0)
            """, (user.id, user.username, user.first_name or "بدون نام", role, now))
            add_transaction(db, user.id, 0, "ثبت‌نام اولیه", "register")

        if user.id == MANAGER_ID:
            db.execute("UPDATE users SET role='manager', blocked=0 WHERE user_id=?", (MANAGER_ID,))

        # نقطه شروع ممیزی کاربر تازه‌وارد
        last_id = db.execute(
            "SELECT COALESCE(MAX(id), 0) FROM transactions WHERE user_id=?", (user.id,)
        ).fetchone()[0]
        db.execute("""
            INSERT OR IGNORE INTO balance_audit_baseline
            (user_id, base_balance, last_transaction_id)
            SELECT user_id, balance, ? FROM users WHERE user_id=?
        """, (last_id, user.id))


def is_manager(user_id):
    return user_id == MANAGER_ID


def is_admin(user_id):
    user = get_user(user_id)
    return bool(user and user[4] in ("manager", "admin"))


def check_active(user_id):
    user = get_user(user_id)
    return bool(user and not user[6])


def is_balance_locked(user_id, db=None):
    """آیا موجودی ZEX کاربر به‌خاطر بررسی درخواست فروش قفل است؟"""
    own = db is None
    conn = db or connect_db()
    try:
        return conn.execute("SELECT 1 FROM balance_locks WHERE user_id=?", (user_id,)).fetchone() is not None
    finally:
        if own:
            conn.close()


def change_balance(user_id, amount, reason):
    """تغییر موجودی همراه با ثبت تاریخچه؛ موجودی منفی مجاز نیست."""
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        if is_balance_locked(user_id, db):
            db.rollback()
            return False
        row = db.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not row or row[0] + amount < 0:
            db.rollback()
            return False
        db.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amount, user_id))
        add_transaction(db, user_id, amount, reason, "balance")
        db.commit()
        return True


def change_debt(user_id, amount, reason):
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT debt FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not row or row[0] + amount < 0:
            db.rollback()
            return False
        db.execute("UPDATE users SET debt=debt+? WHERE user_id=?", (amount, user_id))
        db.execute(
            "INSERT INTO debt_transactions (user_id, amount, reason, created_at) VALUES (?, ?, ?, datetime('now'))",
            (user_id, amount, reason)
        )
        db.commit()
        return True


def create_request(user_id, kind, target_user_id, amount, total_price=0):
    """ثبت درخواست با پشتیبانی از نام ستون‌های دیتابیس‌های قدیمی."""
    with connect_db() as db:
        info = db.execute("PRAGMA table_info(requests)").fetchall()
        columns = {row[1]: row for row in info}

        # نام‌های قدیمی احتمالی را هم مقداردهی می‌کنیم تا ستون اجباری خالی نماند.
        available_values = {
            "user_id": user_id,
            "requester_id": user_id,
            "kind": kind,
            "type": kind,
            "target_user_id": target_user_id,
            "target_id": target_user_id,
            "amount": amount,
            "quantity": amount,
            "total_price": total_price,
            "price": total_price,
            "status": "pending",
            "created_at": datetime.now(IRAN_TZ).strftime("%Y-%m-%d %H:%M:%S"),
            "handled_by": None,
            "admin_id": None,
        }

        fields = []
        values = []
        for name, row in columns.items():
            # row: cid, name, type, notnull, default, pk
            if name in available_values and name != "id":
                fields.append(name)
                values.append(available_values[name])
            elif row[3] and row[4] is None and not row[5]:
                # اگر نسخه قدیمی ستون اجباری دیگری داشت، مقدار امن و خالی‌نبودن می‌دهیم.
                fields.append(name)
                type_name = (row[2] or "").upper()
                values.append(0 if any(t in type_name for t in ("INT", "REAL", "NUM", "DEC")) else "")

        placeholders = ", ".join("?" for _ in fields)
        sql = f"INSERT INTO requests ({', '.join(fields)}) VALUES ({placeholders})"
        cursor = db.execute(sql, tuple(values))
        return cursor.lastrowid


def send_to_admins(text, reply_markup=None):
    with connect_db() as db:
        admins = db.execute(
            "SELECT user_id FROM users WHERE role IN ('manager', 'admin') AND blocked=0"
        ).fetchall()
    for (admin_id,) in admins:
        try:
            bot.send_message(admin_id, text, reply_markup=reply_markup)
        except Exception as error:
            print(f"ارسال پیام به مدیر {admin_id} ناموفق بود: {error}")


def main_menu(user_id):
    # منوی اصلی مرتب و دوتایی چیده شده؛ گزینه‌های مستقل تمام‌عرض هستند.
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.row("موجودی", "👤 مشخصات")
    keyboard.row("👥 گپمون", "🏪 مرکز خرید")
    keyboard.row("📜 تراکنش‌ها", "🪪 احراز هویت")
    keyboard.row("👥 زیرمجموعه‌گیری", "📘 راهنمای شروع")
    keyboard.row("🆘 پشتیبانی")
    if is_admin(user_id):
        keyboard.row("⚙️ پنل مدیریت")
    keyboard.row("↩️ برگشت / لغو")
    return keyboard


def request_buttons(request_id):
    # مدیر می‌تواند درخواست را بررسی کند، تأیید کند یا لغو کند.
    keyboard = types.InlineKeyboardMarkup()
    keyboard.row(
        types.InlineKeyboardButton("🔎 بررسی", callback_data=f"req_review_{request_id}"),
        types.InlineKeyboardButton("✅ تأیید", callback_data=f"req_ok_{request_id}"),
        types.InlineKeyboardButton("❌ لغو", callback_data=f"req_no_{request_id}")
    )
    return keyboard


# ==============================
# پاداش عضویت زمان‌دار
# ==============================

def remaining_duration_text(seconds):
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if hours and minutes:
        return f"{hours} ساعت و {minutes} دقیقه"
    if hours:
        return f"{hours} ساعت"
    if minutes:
        return f"{minutes} دقیقه"
    return "کمتر از یک دقیقه"


def get_active_join_bonus(now=None):
    now = int(time.time()) if now is None else int(now)
    with connect_db() as db:
        row = db.execute(
            "SELECT id, amount, reason, starts_at, ends_at, status "
            "FROM join_bonus_campaigns WHERE status='active' "
            "AND starts_at<=? AND ends_at>? ORDER BY id DESC LIMIT 1",
            (now, now)
        ).fetchone()
        return row


def grant_active_join_bonus(user_id):
    """پاداش را فقط یک‌بار برای هر عضو تازه و هر طرح پرداخت می‌کند."""
    now = int(time.time())
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        campaign = db.execute(
            "SELECT id, amount, reason, starts_at, ends_at FROM join_bonus_campaigns "
            "WHERE status='active' AND starts_at<=? AND ends_at>? ORDER BY id DESC LIMIT 1",
            (now, now)
        ).fetchone()
        if not campaign:
            db.commit()
            return None
        campaign_id, amount, reason, starts_at, ends_at = campaign
        cur = db.execute(
            "INSERT OR IGNORE INTO join_bonus_rewards(campaign_id,user_id,amount,created_at) VALUES(?,?,?,?)",
            (campaign_id, user_id, amount, now)
        )
        if cur.rowcount != 1:
            db.commit()
            return None
        db.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amount, user_id))
        add_transaction(db, user_id, amount, reason or "پاداش عضویت زمان‌دار", "join_bonus")
        db.commit()
    return {
        "amount": amount,
        "reason": reason,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "remaining": max(0, ends_at - now),
    }


def activate_join_bonus(admin_id, chat_id, amount, hours, reason):
    now = int(time.time())
    end_at = now + int(hours) * 3600
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        # با شروع طرح جدید، طرح فعال قبلی دیگر پاداش نمی‌دهد؛ سوابقش باقی می‌ماند.
        db.execute("UPDATE join_bonus_campaigns SET status='replaced' WHERE status='active'")
        cur = db.execute(
            "INSERT INTO join_bonus_campaigns(amount,reason,starts_at,ends_at,created_by,status) "
            "VALUES(?,?,?,?,?,'active')",
            (amount, reason or '', now, end_at, admin_id)
        )
        campaign_id = cur.lastrowid
        db.commit()
    reason_line = escape(reason) if reason else "بدون دلیل مشخص"
    bot.send_message(
        chat_id,
        "✅ <b>طرح پاداش عضویت فعال شد</b>\n\n"
        f"🆔 شماره طرح: <code>#{campaign_id}</code>\n"
        f"🎁 پاداش هر عضو جدید: <b>{amount:,} ZEX</b>\n"
        f"⏳ مدت اعتبار: <b>{hours} ساعت</b>\n"
        f"📝 دلیل: {reason_line}\n"
        f"🟢 شروع: {format_iran_datetime(now)}\n"
        f"🔴 پایان: {format_iran_datetime(end_at)}\n\n"
        "از همین لحظه، هر عضو تازه‌ای که برای اولین بار وارد ربات شود این پاداش را می‌گیرد.",
        reply_markup=admin_menu(admin_id)
    )


def send_join_bonus_status(chat_id):
    now = int(time.time())
    with connect_db() as db:
        db.execute("UPDATE join_bonus_campaigns SET status='expired' WHERE status='active' AND ends_at<=?", (now,))
        row = db.execute(
            "SELECT id, amount, reason, starts_at, ends_at FROM join_bonus_campaigns "
            "WHERE status='active' AND starts_at<=? AND ends_at>? ORDER BY id DESC LIMIT 1",
            (now, now)
        ).fetchone()
        if row:
            campaign_id, amount, reason, starts_at, ends_at = row
            count = db.execute(
                "SELECT COUNT(*) FROM join_bonus_rewards WHERE campaign_id=?", (campaign_id,)
            ).fetchone()[0]
        else:
            latest = db.execute(
                "SELECT id, amount, reason, starts_at, ends_at, status FROM join_bonus_campaigns "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            count = 0
    keyboard = types.InlineKeyboardMarkup()
    keyboard.row(types.InlineKeyboardButton("➕ ساخت طرح جدید", callback_data="admin_join_bonus_new"))
    if row:
        keyboard.row(types.InlineKeyboardButton("⛔ لغو طرح فعال", callback_data="admin_join_bonus_cancel"))
        body = (
            "🎁 <b>طرح پاداش عضویت فعال</b>\n\n"
            f"🆔 طرح: <code>#{campaign_id}</code>\n"
            f"💎 پاداش هر نفر: <b>{amount:,} ZEX</b>\n"
            f"👥 تعداد دریافت‌کنندگان: <b>{count:,}</b>\n"
            f"📝 دلیل: {escape(reason or 'بدون دلیل مشخص')}\n"
            f"🟢 شروع: {format_iran_datetime(starts_at)}\n"
            f"🔴 پایان: {format_iran_datetime(ends_at)}\n"
            f"⏳ زمان باقی‌مانده: <b>{remaining_duration_text(ends_at-now)}</b>"
        )
    elif latest:
        lid, lamount, lreason, lstart, lend, lstatus = latest
        body = (
            "🎁 <b>در حال حاضر طرح فعالی وجود ندارد.</b>\n\n"
            f"آخرین طرح: <code>#{lid}</code>\n"
            f"💎 پاداش: {lamount:,} ZEX\n"
            f"📝 دلیل: {escape(lreason or 'بدون دلیل مشخص')}\n"
            f"📅 شروع: {format_iran_datetime(lstart)}\n"
            f"📅 پایان: {format_iran_datetime(lend)}\n"
            f"📌 وضعیت: {escape(lstatus)}"
        )
    else:
        body = "🎁 هنوز هیچ طرح پاداش عضویتی ساخته نشده است."
    bot.send_message(chat_id, body, reply_markup=keyboard)


# ==============================
# دستورات پایه و مشخصات
# ==============================


def get_referral_reward():
    """مبلغ فعلی پاداش هر زیرمجموعه موفق را از تنظیمات مدیر می‌خواند."""
    with connect_db() as db:
        row = db.execute("SELECT value FROM manager_settings WHERE key='referral_reward'").fetchone()
    try:
        value = int(row[0]) if row else 250
        return max(0, value)
    except (TypeError, ValueError):
        return 250

def attach_referral_after_membership(user_id, referral_id):
    """پاداش دعوت را فقط پس از ثبت عضویت در گپ و فقط یک‌بار پرداخت می‌کند."""
    if not referral_id or referral_id == user_id:
        return False
    rewarded = False
    reward_amount = get_referral_reward()
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        # اگر این کاربر قبلاً به دعوت‌کننده‌ای وصل شده باشد، تغییرش نمی‌دهیم.
        existing = db.execute(
            "SELECT referred_by, referral_rewarded FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        referrer = db.execute(
            "SELECT user_id, blocked FROM users WHERE user_id=?", (referral_id,)
        ).fetchone()
        if existing and existing[0] == 0 and existing[1] == 0 and referrer and not referrer[1]:
            cur = db.execute(
                "UPDATE users SET referred_by=?, referral_rewarded=1 "
                "WHERE user_id=? AND referred_by=0 AND referral_rewarded=0",
                (referral_id, user_id)
            )
            if cur.rowcount == 1:
                if reward_amount > 0:
                    db.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (reward_amount, referral_id))
                    add_transaction(db, referral_id, reward_amount,
                                    f"پاداش دعوت موفق کاربر {user_id}", "referral_reward")
                rewarded = True
        db.commit()
    if rewarded:
        try:
            bot.send_message(
                referral_id,
                f"🎉 دعوت موفق! {reward_amount:,} ZEX به موجودی تو اضافه شد.\n"
                f"👤 کاربر جدید: <code>{user_id}</code>\n"
                "✅ عضویت او در گپ تأیید و در آمار اعضای ربات ثبت شد."
            )
        except Exception:
            pass
    return rewarded


@bot.message_handler(commands=["start"])
def start(message):
    # با زدن /start هر گفت‌وگوی نیمه‌کاره لغو و منو از نو نمایش داده می‌شود.
    steps.pop(message.from_user.id, None)
    user_id = message.from_user.id
    referral_id = 0
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2 and parts[1].startswith("ref_"):
        try:
            referral_id = int(parts[1][4:])
        except ValueError:
            referral_id = 0
    # اگر هنوز عضو گپ نیست، شناسه دعوت‌کننده را تا زمان تأیید عضویت در دکمه نگه می‌داریم.
    if message.chat.type == "private" and not is_group_member(user_id):
        send_join_gate(message.chat.id, user_id, referral_id=referral_id)
        return
    existed = get_user(user_id) is not None
    register_user(message.from_user)
    join_bonus_notice = None
    if not existed:
        join_bonus_notice = grant_active_join_bonus(user_id)
    # اگر اعلان ورود روشن باشد، مدیر اصلی اطلاعات کاربر جدید را می‌گیرد؛ اگر خاموش باشد، کاربر در فهرست اعضا باقی می‌ماند.
    if not existed and manager_notifications_enabled():
        try:
            bot.send_message(MANAGER_ID,
                "🆕 <b>عضو جدید ربات</b>\n"
                f"👤 {escape(message.from_user.first_name or 'بدون نام')}\n"
                f"🔹 @{escape(message.from_user.username or 'ندارد')}\n"
                f"🆔 <code>{user_id}</code>\n"
                f"💎 {(join_bonus_notice['amount'] if join_bonus_notice else 0):,} ZEX\n"
                f"📅 {format_iran_datetime(time.time())}")
        except Exception:
            pass
    # پاداش دعوت فقط پس از تأیید عضویت در گپ و ثبت کاربر در آمار پرداخت می‌شود.
    if not existed:
        attach_referral_after_membership(user_id, referral_id)
    if message.chat.type == "private":
        with connect_db() as db:
            db.execute("UPDATE users SET bot_activity_at=? WHERE user_id=?", (int(time.time()), message.from_user.id))
    if not check_active(message.from_user.id):
        bot.send_message(message.chat.id, "❌ حساب شما مسدود است.")
        return
    welcome_text = "سلام 👋\nبه ربات ZEX خوش اومدی!"
    if join_bonus_notice and message.chat.type == "private":
        bonus = join_bonus_notice
        reason_text = escape(bonus["reason"] or "ثبت‌نام در بازه طرح پاداش")
        welcome_text += (
            "\n\n🎉 <b>پاداش عضویت زمان‌دار دریافت کردی!</b>\n"
            f"💎 مقدار پاداش: <b>{bonus['amount']:,} ZEX</b>\n"
            f"📝 دلیل: {reason_text}\n"
            f"⏳ زمان باقی‌مانده از طرح: <b>{remaining_duration_text(bonus['remaining'])}</b>\n"
            f"📅 پایان طرح: <b>{format_iran_datetime(bonus['ends_at'])}</b>"
        )
    bot.send_message(
        message.chat.id,
        welcome_text,
        reply_markup=main_menu(message.from_user.id) if message.chat.type == "private" else types.ReplyKeyboardRemove()
    )


@bot.message_handler(commands=["id"])
def show_id(message):
    bot.reply_to(message, f"🆔 آیدی شما:\n<code>{message.from_user.id}</code>")


def show_balance(chat_id, target_id, reply_to_message_id=None):
    user = get_user(target_id)
    if not user:
        bot.send_message(chat_id, "❌ این کاربر در ربات ثبت نشده.", reply_to_message_id=reply_to_message_id)
        return
    name = escape(user[2] or user[1] or "کاربر")
    toman = user[3] * SELL_PRICE_TOMAN

    # مقادیر مستقیماً روی دکمه‌ها نمایش داده می‌شوند؛ نیازی به لمس برای دیدن عدد نیست.
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.row(types.InlineKeyboardButton(
        f"💎 {user[3]:,} ZEX", callback_data=f"balance_info_zex_{target_id}"
    ))
    keyboard.row(types.InlineKeyboardButton(
        f"💳 {toman:,} تومان", callback_data=f"balance_info_toman_{target_id}"
    ))
    balance_text = f"💰 <b>موجودی شما | {name}</b>"

    bot.send_message(
        chat_id, balance_text,
        reply_to_message_id=reply_to_message_id,
        reply_markup=keyboard
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("balance_info_"))
def balance_info_callback(call):
    # اطلاعات از قبل روی خود دکمه‌ها دیده می‌شود؛ لمس دکمه فقط واکنش تلگرام را می‌بندد.
    bot.answer_callback_query(call.id)


@bot.message_handler(func=lambda m: m.text in ("موجودی", "💰 موجودی"))
def balance_button(message):
    # در گروه، اگر پیام کاربری ریپلای شده باشد، موجودی همان کاربر نمایش داده می‌شود.
    if message.chat.type in ("group", "supergroup") and message.reply_to_message and message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id
    else:
        target_id = message.from_user.id
    if message.chat.type in ("group", "supergroup"):
        show_balance(message.chat.id, target_id, reply_to_message_id=message.message_id)
    else:
        show_balance(message.chat.id, target_id)


@bot.message_handler(commands=["balance"])
def balance_command(message):
    target_id = message.reply_to_message.from_user.id if message.reply_to_message else message.from_user.id
    show_balance(message.chat.id, target_id)


def profile(chat_id, target_id):
    user = get_user(target_id)
    if not user:
        bot.send_message(chat_id, "❌ این کاربر در ربات ثبت نشده.")
        return
    user_id, username, first_name, balance, role, joined_at, blocked, debt = user
    name = escape(first_name or "بدون نام")
    username_text = f"@{escape(username)}" if username else "ندارد"
    try:
        joined_text = format_iran_datetime(joined_at)
    except (TypeError, ValueError, OSError):
        joined_text = str(joined_at)
    role_label = {"manager": "🔴 مدیر", "admin": "🟢 ادمین", "user": "🟤 کاربر"}.get(role, "🟤 کاربر")
    invited_count = 0
    invited_reward = 0
    with connect_db() as db:
        invited_count = db.execute("SELECT COUNT(*) FROM users WHERE referred_by=? AND referral_rewarded=1", (target_id,)).fetchone()[0]
        current_referral_reward = get_referral_reward()
    bot.send_message(chat_id,
        "👤 <b>مشخصات</b>\n\n"
        f"📝 {name} | {username_text}\n"
        f"🆔 <code>{user_id}</code>\n💎 {balance:,} ZEX\n"
        f"🎁 دعوت: {invited_count} نفر | پاداش فعلی هر دعوت: {current_referral_reward:,} ZEX\n"
        f"⭐ نقش: {role_label}\n📅 ورود: {escape(joined_text)}\n"
        f"🚦 وضعیت: {'مسدود' if blocked else 'فعال'}")


@bot.message_handler(func=lambda m: m.text == "👤 مشخصات")
def profile_button(message):
    profile(message.chat.id, message.from_user.id)


# فقط مدیر اصلی می‌تواند با دستور «مشخصات» اطلاعات کامل کاربر دیگری را جست‌وجو کند.
@bot.message_handler(func=lambda m: bool(m.text and (m.text.strip() == "مشخصات" or m.text.strip().startswith("مشخصات "))))
def manager_profile_lookup(message):
    if not message.from_user or not is_manager(message.from_user.id):
        bot.reply_to(message, "⛔ این دستور فقط برای مدیر اصلی است.")
        return
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) == 1:
        steps[message.from_user.id] = {"action": "manager_profile_target"}
        bot.reply_to(message, "🆔 آیدی عددی یا یوزرنیم کاربر را بفرست؛ مثال: 123456789 یا @username\nبرای لغو بنویس «لغو».")
        return
    manager_profile_show_target(message.chat.id, parts[1].strip().lstrip("@"))


def manager_profile_show_target(chat_id, target):
    user = None
    if target.isdigit():
        user = get_user(int(target))
    else:
        with connect_db() as db:
            row = db.execute("SELECT user_id FROM users WHERE lower(username)=lower(?) LIMIT 1", (target,)).fetchone()
        if row:
            user = get_user(row[0])
    if not user:
        bot.send_message(chat_id, "❌ کاربری با این آیدی یا یوزرنیم در دیتابیس ربات پیدا نشد.")
        return
    target_id = user[0]
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("👤 مشخصات کامل", callback_data=f"mgr_user_profile_{target_id}"),
        types.InlineKeyboardButton("🎮 تعداد بازی‌ها", callback_data=f"mgr_user_games_{target_id}"),
        types.InlineKeyboardButton("👥 چه کسانی را دعوت کرده؟", callback_data=f"mgr_user_invites_{target_id}"),
        types.InlineKeyboardButton("🗂 اطلاعات و کارت‌ها", callback_data=f"mgr_user_data_{target_id}"),
        types.InlineKeyboardButton("💳 آمار خرید و فروش", callback_data=f"mgr_user_trade_{target_id}")
    )
    bot.send_message(chat_id, f"🔎 کاربر پیدا شد: <b>{escape(user[2] or user[1] or 'بدون نام')}</b>\n🆔 <code>{target_id}</code>\nیکی از گزینه‌ها را انتخاب کن:", reply_markup=kb)


@bot.message_handler(func=lambda m: m.from_user is not None and steps.get(m.from_user.id, {}).get("action") == "manager_profile_target")
def manager_profile_target_process(message):
    user_id = message.from_user.id
    raw = (message.text or "").strip()
    if raw.lower() in ("لغو", "cancel", "برگشت"):
        steps.pop(user_id, None)
        bot.reply_to(message, "لغو شد.")
        return
    if not is_manager(user_id):
        steps.pop(user_id, None)
        bot.reply_to(message, "⛔ این دستور فقط برای مدیر اصلی است.")
        return
    steps.pop(user_id, None)
    manager_profile_show_target(message.chat.id, raw.lstrip("@"))


@bot.callback_query_handler(func=lambda c: c.data.startswith(("mgr_user_profile_", "mgr_user_tx_", "mgr_user_games_", "mgr_user_invites_", "mgr_user_data_", "mgr_user_trade_")))
def manager_user_detail_callback(call):
    if not is_manager(call.from_user.id):
        bot.answer_callback_query(call.id, "فقط مدیر اصلی دسترسی دارد.", show_alert=True)
        return
    try:
        target_id = int(call.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "شناسه نامعتبر است.", show_alert=True)
        return
    user = get_user(target_id)
    if not user:
        bot.answer_callback_query(call.id, "کاربر پیدا نشد.", show_alert=True)
        return
    if call.data.startswith("mgr_user_profile_"):
        profile(call.message.chat.id, target_id)
    elif call.data.startswith("mgr_user_invites_"):
        with connect_db() as db:
            rows = db.execute("""
                SELECT user_id, username, first_name, joined_at, referral_rewarded
                FROM users WHERE referred_by=? ORDER BY joined_at DESC
            """, (target_id,)).fetchall()
        target_name = escape(user[2] or user[1] or str(target_id))
        lines = [f"👥 <b>افرادی که {target_name} دعوت کرده</b>\n🆔 <code>{target_id}</code>\n"]
        if not rows:
            lines.append("هنوز کسی با لینک دعوت این کاربر ثبت نشده است.")
        else:
            lines.append(f"تعداد کل ثبت‌شده: <b>{len(rows)}</b>\n")
            for index, (invite_id, invite_username, invite_name, joined_at, rewarded) in enumerate(rows, 1):
                shown_name = escape(invite_name or "بدون نام")
                shown_username = f"@{escape(invite_username)}" if invite_username else "یوزرنیم ندارد"
                try:
                    joined_text = format_iran_datetime(joined_at)
                except (TypeError, ValueError, OSError):
                    joined_text = str(joined_at or "نامشخص")
                status = "✅ پاداش ثبت شده" if rewarded else "⏳ پاداش ثبت نشده"
                lines.append(f"{index}. {shown_name} | {shown_username}\n🆔 <code>{invite_id}</code> | 📅 {escape(joined_text)} | {status}")
        text = "\n".join(lines)
        # پیام‌های تلگرام محدودیت طول دارند؛ فهرست طولانی را در چند پیام می‌فرستیم.
        for start in range(0, len(text), 3500):
            bot.send_message(call.message.chat.id, text[start:start+3500])
    elif call.data.startswith("mgr_user_tx_"):
        with connect_db() as db:
            rows = db.execute("""
                SELECT 'ZEX' AS typ, amount, COALESCE(reason, kind, 'تراکنش'), created_at
                FROM transactions WHERE user_id=?
                UNION ALL
                SELECT 'ZEX', amount, COALESCE(reason, 'تراکنش ZEX'), created_at
                FROM point_transactions WHERE user_id=?
                ORDER BY created_at DESC LIMIT 20
            """, (target_id, target_id)).fetchall()
        name = escape(user[2] or user[1] or str(target_id))
        lines = [f"📜 <b>۲۰ تراکنش اخیر</b> — {name}\n🆔 <code>{target_id}</code>\n"]
        if not rows:
            lines.append("تراکنشی ثبت نشده است.")
        else:
            for typ, amount, reason, created in rows:
                date = str(created)
                if isinstance(created, (int, float)) or date.isdigit():
                    try:
                        date = format_iran_datetime(created)
                    except (TypeError, ValueError, OSError):
                        pass
                amount_text = f"{int(amount):+,}" if typ == "ZEX" else fmt_points(amount)
                lines.append(f"• {escape(date)} | {escape(str(typ))}: <b>{amount_text}</b>\n  {escape(str(reason or ''))}")
        bot.send_message(call.message.chat.id, "\n".join(lines))
    elif call.data.startswith("mgr_user_data_"):
        with connect_db() as db:
            columns = [r[1] for r in db.execute("PRAGMA table_info(users)").fetchall()]
            row = db.execute("SELECT * FROM users WHERE user_id=?", (target_id,)).fetchone()
            payout = db.execute("SELECT card_number FROM payout_cards WHERE user_id=? ORDER BY rowid DESC", (target_id,)).fetchall()
        data = dict(zip(columns, row or ()))
        lines = [f"🗂 <b>اطلاعات ثبت‌شده کاربر</b>\n👤 {escape(user[2] or user[1] or str(target_id))}\n🆔 <code>{target_id}</code>"]
        hidden = {"user_id", "username", "first_name", "card_number"}
        for key, value in data.items():
            if key in hidden or value in (None, ""):
                continue
            lines.append(f"• {escape(str(key))}: <code>{escape(str(value))}</code>")
        verified = data.get("card_number") or "ثبت نشده"
        lines.append(f"\n💳 <b>کارت احراز هویت:</b> <code>{escape(str(verified))}</code>")
        lines.append("💳 <b>کارت‌های واریز ذخیره‌شده:</b>")
        lines.extend([f"• <code>{escape(str(card[0]))}</code>" for card in payout] or ["• موردی ثبت نشده"])
        bot.send_message(call.message.chat.id, "\n".join(lines)[:4000])
    elif call.data.startswith("mgr_user_trade_"):
        with connect_db() as db:
            reqs = db.execute("SELECT kind,status,COUNT(*),COALESCE(SUM(amount),0),COALESCE(SUM(total_price),0) FROM requests WHERE user_id=? GROUP BY kind,status ORDER BY kind,status", (target_id,)).fetchall()
            txs = db.execute("SELECT kind,COUNT(*),COALESCE(SUM(amount),0) FROM transactions WHERE user_id=? AND kind IN ('buy','sell') GROUP BY kind", (target_id,)).fetchall()
        lines = [f"💳 <b>گزارش خرید و فروش</b>\n👤 {escape(user[2] or user[1] or str(target_id))}\n🆔 <code>{target_id}</code>", "\n<b>درخواست‌ها بر اساس نوع و وضعیت</b>"]
        if reqs:
            for kind, status, count, amount_sum, price_sum in reqs:
                lines.append(f"• {escape(str(kind))} / {escape(str(status))}: {count} درخواست | {amount_sum:,} ZEX | {price_sum:,} تومان")
        else:
            lines.append("درخواستی ثبت نشده است.")
        lines.append("\n<b>تراکنش‌های نهایی</b>")
        lines.extend([f"• {escape(str(kind))}: {count} تراکنش | خالص {amount_sum:+,} ZEX" for kind, count, amount_sum in txs] or ["• تراکنشی ثبت نشده است"])
        bot.send_message(call.message.chat.id, "\n".join(lines)[:4000])
    else:
        with connect_db() as db:
            tic = db.execute("SELECT COUNT(*) FROM point_transactions WHERE user_id=? AND reason LIKE '%دوز%'", (target_id,)).fetchone()[0]
            random_count = db.execute("SELECT COUNT(*) FROM point_transactions WHERE user_id=? AND reason LIKE '%بازی شانسی%'", (target_id,)).fetchone()[0]
        bot.send_message(call.message.chat.id,
            f"🎮 <b>آمار بازی کاربر</b>\n👤 {escape(user[2] or user[1] or str(target_id))}\n🆔 <code>{target_id}</code>\n\n"
            f"⭕ رویدادهای ثبت‌شده دوز: <b>{tic}</b>\n🎲 رویدادهای ثبت‌شده بازی شانسی: <b>{random_count}</b>\n\n"
            "ℹ️ این آمار از تاریخچه ZEXها محاسبه می‌شود؛ بازی‌های مساوی یا بازی‌هایی که تغییری در ZEX ایجاد نکرده‌اند ممکن است شمرده نشوند.")
    bot.answer_callback_query(call.id)


@bot.message_handler(commands=["profile"])
def profile_command(message):
    target_id = message.reply_to_message.from_user.id if message.reply_to_message else message.from_user.id
    profile(message.chat.id, target_id)


@bot.message_handler(func=lambda m: m.text == "👥 گپمون")
def group_button(message):
    if not GROUP_LINK or GROUP_LINK == "PASTE_GROUP_LINK_HERE":
        bot.send_message(message.chat.id, "لینک گروه هنوز در تنظیمات قرار داده نشده است.")
    else:
        bot.send_message(message.chat.id, f"👥 لینک گروه:\n{escape(GROUP_LINK)}")


@bot.message_handler(func=lambda m: m.text in ("↩️ برگشت / لغو", "لغو", "برگشت", "❌ لغو", "⬅️ برگشت"))
def cancel_or_back(message):
    # هر مرحله نیمه‌کاره، از خرید و فروش تا عملیات مدیریت، لغو می‌شود.
    user_id = message.from_user.id
    was_in_step = steps.pop(user_id, None) is not None
    if was_in_step:
        reply = "↩️ عملیات لغو شد و به منوی اصلی برگشتی."
    else:
        reply = "🏠 منوی اصلی"
    bot.send_message(
        message.chat.id,
        reply,
        reply_markup=main_menu(user_id)
    )
    # اگر مدیر هست، پنل مدیریت را هم دوباره در دسترس قرار می‌دهیم.
    if is_admin(user_id):
        bot.send_message(message.chat.id, "⚙️ پنل مدیریت", reply_markup=admin_menu(user_id))


# احراز هویت فعلاً فقط با ثبت شماره کارت کاربر انجام می‌شود.
@bot.message_handler(func=lambda m: m.text == "🪪 احراز هویت")
def card_verification_start(message):
    user_id = message.from_user.id
    if message.chat.type != "private":
        bot.send_message(message.chat.id, "لطفاً احراز هویت را در پیوی ربات انجام بده.")
        return
    card = get_card_number(user_id)
    if card:
        bot.send_message(message.chat.id, f"✅ احراز هویت انجام شده است.\n💳 شماره کارت ثبت‌شده: <code>{escape(card)}</code>")
        return
    steps[user_id] = {"action": "card_number"}
    bot.send_message(message.chat.id, "💳 برای تأیید کارت، شماره کارت ۱۶ رقمی خودت را بدون فاصله بفرست. این بررسی مالکیت واقعی کارت را تأیید نمی‌کند.")


@bot.message_handler(func=lambda m: m.text == "👥 زیرمجموعه‌گیری")
def referral_menu(message):
    # هر کاربر لینک اختصاصی خودش را می‌گیرد.
    if message.chat.type != "private":
        bot.reply_to(message, "برای دریافت لینک دعوت، وارد پیوی ربات شو.")
        return
    user_id = message.from_user.id
    try:
        bot_name = bot.get_me().username
    except Exception:
        bot.send_message(message.chat.id, "⚠️ الان نتونستم لینک دعوت بسازم؛ چند لحظه دیگه دوباره تلاش کن.")
        return
    link = f"https://t.me/{bot_name}?start=ref_{user_id}"
    with connect_db() as db:
        invited = db.execute("SELECT COUNT(*) FROM users WHERE referred_by=? AND referral_rewarded=1", (user_id,)).fetchone()[0]
        pending = db.execute("SELECT COUNT(*) FROM users WHERE referred_by=? AND referral_rewarded=0", (user_id,)).fetchone()[0]
    share_text = "به گپ ZEX بیا! از طریق لینک زیر ربات رو فعال کن و کنار بقیه عضوهای گپ باش."
    share_url = "https://t.me/share/url?url=" + quote(link, safe="") + "&text=" + quote(share_text, safe="")
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(types.InlineKeyboardButton("📤 ارسال لینک برای چند نفر", url=share_url))
    if GROUP_LINK and GROUP_LINK != "PASTE_GROUP_LINK_HERE":
        keyboard.add(types.InlineKeyboardButton("👥 ورود به گپ ZEX", url=GROUP_LINK))
    keyboard.add(types.InlineKeyboardButton("🏆 دعوت‌کنندگان برتر", callback_data="referral_leaderboard"))
    bot.send_message(
        message.chat.id,
        "👥 <b>دعوت دوستان به ZEX</b>\n\n"
        "لینک اختصاصی خودت رو می‌تونی برای چند نفر بفرستی. روی دکمهٔ «ارسال لینک برای چند نفر» بزن و در صفحهٔ اشتراک‌گذاری تلگرام مخاطب‌ها یا گروه‌های موردنظرت رو انتخاب کن؛ یا لینک زیر رو کپی و هرجا خواستی ارسال کن:\n"
        f"<code>{escape(link)}</code>\n\n"
        f"🎁 پاداش هر دعوت موفق: <b>{get_referral_reward():,} ZEX</b>\n"
        "پاداش فقط برای ثبت‌نام واقعی و اولین ورود هر کاربر محاسبه می‌شه.\n\n"
        f"✅ دعوت‌های موفق تو: <b>{invited}</b>",
        reply_markup=keyboard
    )


@bot.callback_query_handler(func=lambda c: c.data == "referral_leaderboard")
def referral_leaderboard(call):
    with connect_db() as db:
        rows = db.execute(
            "SELECT u.user_id, COALESCE(NULLIF(u.first_name,''), NULLIF(u.username,''), CAST(u.user_id AS TEXT)) AS display_name, COUNT(*) AS total "
            "FROM users u JOIN users invited ON invited.referred_by=u.user_id AND invited.referral_rewarded=1 "
            "WHERE u.blocked=0 GROUP BY u.user_id ORDER BY total DESC, u.joined_at ASC LIMIT 10"
        ).fetchall()
    if not rows:
        text = "🏆 هنوز دعوت موفقی ثبت نشده؛ اولین نفر باش!"
    else:
        medals = ("🥇", "🥈", "🥉")
        lines = ["🏆 <b>۱۰ دعوت‌کنندهٔ برتر ZEX</b>", ""]
        for index, row in enumerate(rows, 1):
            medal = medals[index-1] if index <= 3 else f"{index}️⃣"
            lines.append(f"{medal} {escape(str(row[1]))} — <b>{row[2]}</b> دعوت موفق")
        text = "\n".join(lines)
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, text)


@bot.message_handler(func=lambda m: m.text == "📘 راهنمای شروع")
def quick_start_guide(message):
    text = (
        "📘 <b>راهنمای سریع ZEX</b>\n\n"
        "1️⃣ برای شروع، عضو گپ ZEX شو.\n"
        "2️⃣ از «👤 مشخصات» اطلاعات حساب و تاریخ عضویتت رو ببین.\n"
        "3️⃣ از «👥 زیرمجموعه‌گیری» لینک دعوت اختصاصی بگیر و برای دوستات بفرست.\n"
        "4️⃣ برای پرسیدن سؤال یا گزارش مشکل، از «🆘 پشتیبانی» استفاده کن.\n\n"
        "💡 برای اینکه دوستات راحت‌تر وارد بشن، لینک دعوت رو همراه توضیح کوتاه براشون بفرست."
    )
    bot.send_message(message.chat.id, text)


@bot.message_handler(func=lambda m: m.text == "🆘 پشتیبانی")
def support_button(message):
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton("💬 ورود به پشتیبانی", url="https://t.me/Am_amir_89"))
    bot.send_message(message.chat.id, "🆘 برای پشتیبانی روی دکمه زیر بزن:", reply_markup=keyboard)


# صفحه‌کلید عددی خرید و فروش ZEX؛ عددها داخل همین پیام انتخاب می‌شوند.
def amount_keypad_markup(user_id):
    step = steps.get(user_id, {})
    action = step.get("action")
    amount_text = step.get("amount_text", "")
    price = BUY_PRICE_TOMAN if action == "buy_amount" else SELL_PRICE_TOMAN
    amount = int(amount_text) if amount_text.isdigit() else 0
    kb = types.InlineKeyboardMarkup(row_width=3)
    kb.row(*(types.InlineKeyboardButton(str(n), callback_data=f"amt_digit_{n}") for n in (1, 2, 3)))
    kb.row(*(types.InlineKeyboardButton(str(n), callback_data=f"amt_digit_{n}") for n in (4, 5, 6)))
    kb.row(*(types.InlineKeyboardButton(str(n), callback_data=f"amt_digit_{n}") for n in (7, 8, 9)))
    kb.row(types.InlineKeyboardButton("⌫ حذف", callback_data="amt_delete"),
           types.InlineKeyboardButton("0", callback_data="amt_digit_0"),
           types.InlineKeyboardButton("🗑 پاک‌کردن", callback_data="amt_clear"))
    kb.row(types.InlineKeyboardButton("✅ تأیید تعداد", callback_data="amt_confirm"))
    return kb


def amount_keypad_text(user_id):
    step = steps.get(user_id, {})
    action = step.get("action")
    amount_text = step.get("amount_text", "")
    amount = int(amount_text) if amount_text.isdigit() else 0
    if action == "buy_amount":
        price = BUY_PRICE_TOMAN
        title = "🛒 خرید ZEX"
        detail = f"💵 مبلغ خرید: <b>{amount * price:,} تومان</b>"
    else:
        price = SELL_PRICE_TOMAN
        title = "📤 فروش ZEX"
        detail = f"💵 مبلغ فروش: <b>{amount * price:,} تومان</b>"
        user = get_user(user_id)
        detail += f"\n💰 موجودی: <b>{user[3] if user else 0:,} ZEX</b>\n📉 حداقل فروش: <b>{MIN_SELL:,} ZEX</b>"
        if step.get("payout_card"):
            card = step["payout_card"]
            detail += f"\n💳 کارت مقصد: <code>{escape(card)}</code>"
    return f"{title}\n\n🔢 تعداد واردشده: <b>{amount:,}</b> ZEX\n{detail}\n\nبا دکمه‌های زیر تعداد را وارد کن."


def show_amount_keypad(chat_id, user_id, message_id=None):
    text = amount_keypad_text(user_id)
    keyboard = amount_keypad_markup(user_id)
    if message_id is None:
        bot.send_message(chat_id, text, reply_markup=keyboard)
    else:
        bot.edit_message_text(text, chat_id, message_id, reply_markup=keyboard)


@bot.callback_query_handler(func=lambda c: c.data.startswith("amt_digit_") or c.data in ("amt_delete", "amt_clear", "amt_confirm"))
def amount_keypad_callback(call):
    user_id = call.from_user.id
    step = steps.get(user_id)
    if not step or step.get("action") not in ("buy_amount", "sell_amount"):
        bot.answer_callback_query(call.id, "این صفحه‌کلید دیگر فعال نیست.", show_alert=True)
        return
    current = step.get("amount_text", "")
    if call.data.startswith("amt_digit_"):
        digit = call.data.rsplit("_", 1)[1]
        if len(current) < 10:
            current += digit
        step["amount_text"] = current
        show_amount_keypad(call.message.chat.id, user_id, call.message.message_id)
        bot.answer_callback_query(call.id)
        return
    if call.data == "amt_delete":
        step["amount_text"] = current[:-1]
        show_amount_keypad(call.message.chat.id, user_id, call.message.message_id)
        bot.answer_callback_query(call.id)
        return
    if call.data == "amt_clear":
        step["amount_text"] = ""
        show_amount_keypad(call.message.chat.id, user_id, call.message.message_id)
        bot.answer_callback_query(call.id, "عدد پاک شد.")
        return
    if not current.isdigit() or not (0 < int(current) <= MAX_AMOUNT):
        bot.answer_callback_query(call.id, "یک تعداد معتبر وارد کن.", show_alert=True)
        return
    amount = int(current)
    chat_id = call.message.chat.id
    if step["action"] == "buy_amount":
        step["amount"] = amount
        step["action"] = "buy_coupon_choice"
        base_price = amount * BUY_PRICE_TOMAN
        try:
            bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
        except Exception:
            pass
        bot.send_message(chat_id,
            f"💎 تعداد: <b>{amount:,} ZEX</b>\n💵 مبلغ قبل از تخفیف: <b>{base_price:,} تومان</b>\n\n"
            "آیا کد تخفیف دارید؟", reply_markup=coupon_choice_keyboard())
        bot.answer_callback_query(call.id, "تعداد ثبت شد.")
        return

    user = get_user(user_id)
    if amount < MIN_SELL:
        bot.answer_callback_query(call.id, f"حداقل فروش {MIN_SELL:,} ZEX است.", show_alert=True)
        return
    if not user or amount > user[3]:
        bot.answer_callback_query(call.id, "موجودی کافی نیست.", show_alert=True)
        return
    payout_card = step.get("payout_card", "")
    if not payout_card:
        bot.answer_callback_query(call.id, "کارت مقصد انتخاب نشده؛ فروش را دوباره شروع کن.", show_alert=True)
        steps.pop(user_id, None)
        return
    total_price = amount * SELL_PRICE_TOMAN
    request_id = create_request(user_id, "sell", user_id, amount, total_price)
    with connect_db() as db:
        db.execute("UPDATE requests SET payout_card=? WHERE id=?", (payout_card, request_id))
    steps.pop(user_id, None)
    try:
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    bot.send_message(chat_id,
        f"✅ <b>درخواست فروش ثبت شد</b>\n💎 تعداد: <b>{amount:,} ZEX</b>\n"
        f"💵 مبلغی که مدیر باید واریز کند: <b>{total_price:,} تومان</b>\n"
        f"💳 کارت مقصد: <code>{escape(payout_card)}</code>\n🔖 درخواست: <code>{request_id}</code>\n"
        "🟤 وضعیت: در انتظار بررسی مدیر.")
    seller = get_user(user_id)
    uname = f"@{seller[1]}" if seller and seller[1] else "ندارد"
    send_to_admins(
        "📤 <b>درخواست فروش ZEX</b>\n"
        f"👤 نام: {escape(str(seller[2] if seller else ''))}\n🔗 نام کاربری: {escape(uname)}\n"
        f"🆔 آیدی عددی: <code>{user_id}</code>\n💎 تعداد فروش: <b>{amount:,} ZEX</b>\n"
        f"💵 مبلغ قابل واریز: <b>{total_price:,} تومان</b>\n💳 کارت دریافت‌کننده: <code>{escape(payout_card)}</code>\n"
        f"💰 موجودی فعلی: <b>{seller[3] if seller else 0:,} ZEX</b>\n🔖 درخواست: <code>{request_id}</code>",
        request_buttons(request_id))
    bot.answer_callback_query(call.id, "درخواست فروش ثبت شد.")


def coupon_choice_keyboard():
    # دو گزینه قابل لمس برای سؤال کد تخفیف.
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("✅ کد تخفیف دارم", callback_data="buy_coupon_yes"),
        types.InlineKeyboardButton("❌ کد تخفیف ندارم", callback_data="buy_coupon_no")
    )
    return kb


@bot.callback_query_handler(func=lambda c: c.data in ("buy_coupon_yes", "buy_coupon_no"))
def buy_coupon_choice_callback(call):
    # پاسخ کاربر به سؤال «آیا کد تخفیف دارید؟».
    user_id = call.from_user.id
    step = steps.get(user_id)
    if call.message.chat.type != "private" or not step or step.get("action") != "buy_coupon_choice":
        bot.answer_callback_query(call.id, "این مرحله خرید فعال نیست.", show_alert=True)
        return
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    if call.data == "buy_coupon_yes":
        step["action"] = "buy_coupon"
        bot.send_message(call.message.chat.id, "🎟 کد تخفیف را وارد کن:")
        bot.answer_callback_query(call.id, "واردکردن کد تخفیف")
    else:
        bot.answer_callback_query(call.id, "بدون تخفیف ادامه می‌دهیم.")
        finish_buy_coupon(user_id, call.message.chat.id, "ندارم")


# ==============================
# خرید و فروش
# ==============================

@bot.message_handler(func=lambda m: m.text in ("➕ افزایش موجودی", "🏪 مرکز خرید", "🔴🛒 خرید ZEX"))
def shopping_center(message):
    # مرکز خرید و صفحه‌کلیدهای خرید فقط در گفت‌وگوی خصوصی نمایش داده می‌شوند.
    user_id = message.from_user.id
    if message.chat.type != "private":
        bot.reply_to(message, "برای استفاده از مرکز خرید، وارد گفت‌وگوی خصوصی ربات شو.")
        return
    if not check_active(user_id):
        bot.send_message(message.chat.id, "❌ حساب شما فعال نیست.")
        return
    keyboard = types.InlineKeyboardMarkup()
    # رنگ danger روی دکمه خرید در کلاینت‌های جدید تلگرام، دکمه را قرمز نمایش می‌دهد.
    keyboard.row(types.InlineKeyboardButton("🛒 خرید ZEX", callback_data="center_buy_zex", style="danger"))
    keyboard.row(types.InlineKeyboardButton("📤 فروش ZEX", callback_data="center_sell_zex"))
    bot.send_message(message.chat.id,
        "🏪 <b>مرکز خرید ZEX</b>\n\n"
        f"💎 خرید هر ZEX: <b>{BUY_PRICE_TOMAN} تومان</b>\n"
        f"💸 فروش هر ZEX: <b>{SELL_PRICE_TOMAN} تومان</b>\n\n"
        "از اینجا می‌توانی ZEX بخری یا بفروشی.",
        reply_markup=keyboard)


@bot.callback_query_handler(func=lambda c: c.data in ("center_buy_zex", "center_sell_zex"))
def center_zex_trade_start(call):
    # شروع خرید یا فروش ZEX از داخل مرکز خرید.
    user_id = call.from_user.id
    if call.message.chat.type != "private":
        bot.answer_callback_query(call.id, "این کار را در گفت‌وگوی خصوصی ربات انجام بده.", show_alert=True)
        return
    if not check_active(user_id):
        bot.answer_callback_query(call.id, "حساب شما فعال نیست.", show_alert=True)
        return
    if not get_card_number(user_id):
        steps[user_id] = {"action": "card_number"}
        bot.answer_callback_query(call.id, "اول باید احراز هویت کنی.", show_alert=True)
        bot.send_message(call.message.chat.id, "💳 برای خرید یا فروش، اول شماره کارت ۱۶ رقمی خودت را برای احراز هویت بفرست.")
        return
    if call.data == "center_buy_zex":
        # کاربر گیرنده خرید را با دو دکمه قابل لمس انتخاب می‌کند.
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(
            types.InlineKeyboardButton("🙋‍♂️ خودم", callback_data="buy_target_self"),
            types.InlineKeyboardButton("👤 شخص دیگر", callback_data="buy_target_other")
        )
        bot.send_message(call.message.chat.id,
            "🛒 <b>خرید ZEX</b>\n"
            "برای چه کسی می‌خواهی خرید کنی؟", reply_markup=kb)
    else:
        ask_payout_card(call.message.chat.id, user_id)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data in ("buy_target_self", "buy_target_other"))
def buy_target_choice(call):
    # انتخاب «خودم» مستقیم به صفحه‌کلید تعداد می‌رود؛ گزینه دیگر آیدی گیرنده را می‌پرسد.
    user_id = call.from_user.id
    if call.message.chat.type != "private":
        bot.answer_callback_query(call.id, "این کار را در گفت‌وگوی خصوصی انجام بده.", show_alert=True)
        return
    if call.data == "buy_target_self":
        target = get_user(user_id)
        if not target or target[6]:
            bot.answer_callback_query(call.id, "حساب شما فعال نیست.", show_alert=True)
            return
        with steps_lock:
            steps[user_id] = {
                "action": "buy_amount",
                "target_id": target[0],
                "target_name": target[2] or target[1] or str(target[0]),
                "amount_text": ""
            }
        bot.answer_callback_query(call.id, "خرید برای خودت انتخاب شد.")
        show_amount_keypad(call.message.chat.id, user_id)
    else:
        with steps_lock:
            steps[user_id] = {"action": "buy_username"}
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, "🆔 آیدی عددی یا نام کاربری شخص موردنظر را بفرست.")


@bot.message_handler(func=lambda m: m.text == "📤 فروش")
def sell_start(message):
    user_id = message.from_user.id
    if not get_card_number(user_id):
        steps[user_id] = {"action": "card_number"}
        bot.send_message(message.chat.id, "💳 برای فروش، اول شماره کارت ۱۶ رقمی خودت را برای احراز هویت بفرست.")
        return
    user = get_user(user_id)
    if not user or user[6]:
        bot.send_message(message.chat.id, "❌ حساب شما فعال نیست؛ ابتدا /start را بزن.")
        return
    ask_payout_card(message.chat.id, user_id)


# ==============================
# افزایش/کاهش موجودی توسط مدیر اصلی
# ==============================

@bot.message_handler(func=lambda m: bool(m.text and re.fullmatch(r"(?:افزایش|کاهش)(?:\s+ZEX)?\s+\d+(?:\s+کل)?", m.text.strip())))
def manager_balance_command(message):
    # دستورها: «کاهش 300» یا «کاهش ZEX 300» با ریپلای؛ برای همه «کاهش 300 کل» یا «کاهش ZEX 300 کل».
    if not message.from_user or message.from_user.id != MANAGER_ID:
        return
    match = re.fullmatch(r"(افزایش|کاهش)(?:\s+(ZEX))?\s+(\d+)(?:\s+(کل))?", message.text.strip())
    if not match:
        return
    action, points_flag, raw_amount, all_users = match.groups()
    asset = "ZEX" if points_flag else "ZEX"
    amount = int(raw_amount)
    all_users = bool(all_users)
    if not (0 < amount <= MAX_AMOUNT):
        bot.reply_to(message, "❌ مقدار باید بیشتر از صفر و در محدودهٔ مجاز باشد.")
        return

    # با ریپلای فقط همان کاربر انتخاب می‌شود؛ عبارت «کل» در حالت ریپلای همگانی می‌ماند.
    if message.reply_to_message and message.reply_to_message.from_user and not all_users:
        target = message.reply_to_message.from_user
        if target.is_bot or target.id == MANAGER_ID:
            bot.reply_to(message, "❌ این کاربر قابل انتخاب نیست.")
            return
        targets = [(target.id,)]
        audience = f"کاربر {target.id}"
    elif all_users:
        with connect_db() as db:
            if message.chat.type == "private":
                # در خصوصی «کل» یعنی تمام کاربران ثبت‌شده در ربات، فعال یا غیرفعال.
                targets = db.execute(
                    "SELECT user_id FROM users WHERE user_id<>? ORDER BY user_id", (MANAGER_ID,)
                ).fetchall()
                audience = "تمام کاربران ثبت‌شدهٔ ربات"
            elif message.chat.type in ("group", "supergroup"):
                # در گروه «کل» یعنی همهٔ کاربرانی که قبلاً در همین گروه دیده شده‌اند؛ بدون محدودیت زمانی فعالیت.
                targets = db.execute(
                    "SELECT user_id FROM users WHERE user_id<>? AND game_activity_at>0 ORDER BY user_id",
                    (MANAGER_ID,)
                ).fetchall()
                audience = "تمام کاربران شناخته‌شدهٔ گروه"
            else:
                bot.reply_to(message, "❌ این دستور را در گروه یا گفت‌وگوی خصوصی ربات اجرا کن.")
                return
    else:
        bot.reply_to(message, "برای همه، آخر دستور «کل» بنویس؛ مثلاً «کاهش 400 کل». برای یک نفر، روی پیامش ریپلای کن.")
        return

    changed = total = 0
    for row in targets:
        target_id = int(row[0])
        target = get_user(target_id)
        if not target:
            continue
        # همهٔ تغییرات ZEX باید از یک مسیر ثبت شوند تا سامانهٔ هشدار،
        # تاریخچه و موجودی با هم هماهنگ بمانند.
        current = float(target[3] or 0)
        actual = amount if action == "افزایش" else min(amount, int(current))
        if actual <= 0:
            continue
        delta = actual if action == "افزایش" else -actual
        if not change_balance(target_id, delta, f"{action} ZEX توسط مدیر {MANAGER_ID}"):
            continue
        new_value = get_user(target_id)[3]
        changed += 1
        total += actual
        if target_id != MANAGER_ID:
            try:
                bot.send_message(target_id, f"{'➕' if action == 'افزایش' else '➖'} مدیر {action} {actual:,} {asset}.\nموجودی جدید: <b>{fmt_points(new_value) if asset == 'ZEX' else f'{new_value:,}'}</b>")
            except Exception:
                pass
    bot.reply_to(message, f"✅ {action} {asset} انجام شد.\n🎯 هدف: {audience}\n👥 تغییرکرده: {changed}/{len(targets)}\n📊 مجموع تغییر: {total:,} {asset}")

# مدیریت کدهای تخفیف از دکمه‌های پنل مدیریت انجام می‌شود.




def finish_buy_coupon(user_id, chat_id, entered):
    # ثبت درخواست خرید با یا بدون کد تخفیف.
    step = steps.get(user_id)
    if not step or step.get("action") not in ("buy_coupon", "buy_coupon_choice"):
        bot.send_message(chat_id, "❌ مرحله خرید پیدا نشد؛ خرید را دوباره شروع کن.")
        return
    amount = int(step["amount"])
    target_id = step["target_id"]
    target_name = step["target_name"]
    base_price = amount * BUY_PRICE_TOMAN
    code = ""
    discount = 0
    entered = (entered or "").strip()
    if entered.lower() not in ("ندارم", "بدون", "نه", "-", "skip"):
        code = entered.upper()
        with connect_db() as db:
            coupon = db.execute(
                "SELECT discount_amount, max_uses, used_count, expires_at FROM discount_codes WHERE code=? AND active=1", (code,)
            ).fetchone()
            used = db.execute(
                "SELECT 1 FROM discount_uses WHERE code=? AND user_id=?", (code, user_id)
            ).fetchone()
        if not coupon:
            bot.send_message(chat_id, "❌ کد تخفیف معتبر نیست. دوباره کد را بفرست یا بنویس «ندارم».")
            return
        if used:
            bot.send_message(chat_id, "❌ قبلاً از این کد تخفیف استفاده کرده‌ای. کد دیگری بفرست یا بنویس «ندارم».")
            return
        if coupon[3] is not None and int(coupon[3]) < int(time.time()):
            bot.send_message(chat_id, "❌ مهلت استفاده از این کد تمام شده است.")
            return
        if int(coupon[1] or 0) > 0 and int(coupon[2] or 0) >= int(coupon[1]):
            bot.send_message(chat_id, "❌ ظرفیت استفاده از این کد تمام شده است.")
            return
        discount = int(coupon[0])
        # استفاده از تخفیف فقط برای خرید بالاتر از ۲۰٬۰۰۰ تومان مجاز است؛
        # مبلغ خرید باید دست‌کم ۵٬۰۰۰ تومان بیشتر از مقدار تخفیف باشد.
        minimum_purchase = max(20001, discount + 5000)
        if base_price < minimum_purchase:
            bot.send_message(chat_id,
                f"❌ برای استفاده از این کد، مبلغ خرید باید حداقل <b>{minimum_purchase:,} تومان</b> باشد.\n"
                f"مبلغ خرید فعلی: <b>{base_price:,} تومان</b>.\n"
                "مقدار خرید را اصلاح کن و دوباره درخواست خرید را شروع کن.")
            steps.pop(user_id, None)
            return
        if discount >= base_price:
            discount = max(0, base_price - 1)
    total_price = max(1, base_price - discount)
    request_id = create_request(user_id, "buy", target_id, amount, total_price)
    try:
        with connect_db() as db:
            db.execute("UPDATE requests SET discount_code=?, discount_amount=? WHERE id=?",
                       (code or None, discount, request_id))
            if code:
                db.execute("INSERT INTO discount_uses(code, user_id, request_id) VALUES (?, ?, ?)",
                           (code, user_id, request_id))
                db.execute("UPDATE discount_codes SET used_count=used_count+1 WHERE code=?", (code,))
    except sqlite3.IntegrityError:
        bot.send_message(chat_id, "❌ این کد تخفیف قبلاً برای حسابت استفاده شده؛ دوباره درخواست خرید را شروع کن.")
        steps.pop(user_id, None)
        return
    steps.pop(user_id, None)
    card_text = (f"🔵━━━━━━━━━━━━🔵\n💳 <b>واریز به کارت مدیر</b>\n🔵 <code>{escape(CARD_NUMBER)}</code> 🔵\n👤 صاحب کارت: {escape(CARD_OWNER)}\n🔵━━━━━━━━━━━━🔵"
                 if CARD_NUMBER != "CARD_NUMBER_HERE" else
                 "⚠️ شماره کارت هنوز توسط مدیر در تنظیمات کد وارد نشده است.")
    bot.send_message(chat_id,
        "✅ <b>درخواست خرید ثبت شد</b>\n"
        f"👤 گیرنده: {escape(str(target_name))}\n💎 تعداد: <b>{amount:,} ZEX</b>\n"
        f"💵 مبلغ اصلی: <b>{base_price:,} تومان</b>\n"
        f"🎟 تخفیف: <b>{discount:,} تومان</b>\n"
        f"💰 مبلغ قابل پرداخت: <b>{total_price:,} تومان</b>\n"
        f"🔖 شماره درخواست: <code>{request_id}</code>\n\n{card_text}\n"
        "بعد از واریز، مدیر پرداخت را بررسی می‌کند و سپس ZEX به موجودی گیرنده اضافه می‌شود.")
    requester = get_user(user_id)
    uname = f"@{requester[1]}" if requester and requester[1] else "ندارد"
    send_to_admins(
        "🛒 <b>درخواست خرید ZEX</b>\n"
        f"👤 نام: {escape(str(requester[2] if requester else ''))}\n🔗 نام کاربری: {escape(uname)}\n"
        f"🆔 درخواست‌دهنده: <code>{user_id}</code>\n🎯 گیرنده: <code>{target_id}</code>\n"
        f"💎 تعداد: <b>{amount:,} ZEX</b>\n💵 مبلغ اصلی: <b>{base_price:,} تومان</b>\n"
        f"🎟 کد تخفیف: <code>{escape(code or 'ندارد')}</code>\n➖ تخفیف: <b>{discount:,} تومان</b>\n"
        f"💰 مبلغ نهایی: <b>{total_price:,} تومان</b>\n🔖 درخواست: <code>{request_id}</code>",
        request_buttons(request_id))
    return



@bot.message_handler(func=lambda m: m.from_user is not None and m.from_user.id in steps and steps[m.from_user.id].get("action") in ("card_number", "buy_username", "buy_amount", "buy_coupon", "sell_amount", "sell_card_add", "transfer_amount", "transfer_receiver"))
def process_user_steps(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    text = (message.text or "").strip()
    step = steps.get(user_id)
    if not step:
        return
    if message.chat.type == "private":
        with connect_db() as db:
            db.execute("UPDATE users SET bot_activity_at=? WHERE user_id=?", (int(time.time()), user_id))

    if step["action"] == "transfer_receiver":
        # آیدی عددی گیرنده را بررسی می‌کنیم و سپس مقدار انتقال را می‌پرسیم.
        if message.chat.type not in ("group", "supergroup") or not text.isdigit() or int(text) <= 0:
            bot.reply_to(message, "❌ آیدی عددی معتبر بفرست.")
            return
        receiver_id = int(text)
        if not get_user(receiver_id):
            bot.reply_to(message, "❌ این کاربر هنوز ربات را با /start فعال نکرده است.")
            return
        steps[user_id] = {"action": "transfer_amount", "receiver_id": receiver_id, "chat_id": message.chat.id}
        bot.reply_to(message, f"🔢 گیرنده انتخاب شد (آیدی <code>{receiver_id}</code>). حالا مقدار ZEX را بفرست.")
        return

    if step["action"] == "transfer_amount":
        # مقدار انتقال را می‌گیریم و همان منطق انتقال را اجرا می‌کنیم.
        if message.chat.type not in ("group", "supergroup") or not text.isdigit() or int(text) <= 0:
            bot.reply_to(message, "❌ مقدار باید یک عدد مثبت باشد.")
            return
        receiver_id = int(step["receiver_id"])
        steps.pop(user_id, None)
        perform_transfer(message, receiver_id, int(text))
        return

    if step["action"] == "card_number":
        card = re.sub(r"\s+", "", text)
        # شماره کارت باید ۱۶ رقم انگلیسی باشد و رقم کنترلی آن معتبر باشد.
        if not valid_card_number(card):
            bot.send_message(chat_id, "❌ شماره کارت اشتباه یا نامعتبر است. ۱۶ رقم کارت را بررسی کن و دوباره بفرست.")
            return
        with connect_db() as db:
            db.execute("UPDATE users SET card_number=? WHERE user_id=?", (card, user_id))
        steps.pop(user_id, None)
        bot.send_message(chat_id, "✅ ثبت شماره کارت انجام شد. حالا می‌توانی از مرکز خرید استفاده کنی.", reply_markup=main_menu(user_id))
        return

    if step["action"] == "sell_card_add":
        # شماره کارت مقصد را پاک‌سازی می‌کنیم تا فاصله‌ها باعث تفاوت نشوند.
        card = re.sub(r"\s+", "", text)
        # شماره کارت مقصد نیز از نظر قالب و رقم کنترلی بررسی می‌شود.
        if not valid_card_number(card):
            bot.send_message(chat_id, "❌ شماره کارت اشتباه یا نامعتبر است. ۱۶ رقم کارت را بررسی کن و دوباره بفرست.")
            return

        # شماره کارت احراز هویت هم جزو کارت‌های ثبت‌شده حساب می‌شود.
        verified_card = get_card_number(user_id)
        if card == verified_card:
            bot.send_message(chat_id, "⚠️ این شماره کارت تکراری است؛ همین کارت از قبل به‌عنوان کارت احراز هویت ثبت شده. شماره دیگری وارد کن.")
            return

        # قبل از ذخیره بررسی می‌کنیم این کارت قبلاً برای همین کاربر ثبت نشده باشد.
        with connect_db() as db:
            exists = db.execute(
                "SELECT 1 FROM payout_cards WHERE user_id=? AND card_number=? LIMIT 1",
                (user_id, card)
            ).fetchone()
            if exists:
                bot.send_message(chat_id, "⚠️ این شماره کارت را قبلاً اضافه کرده‌ای. شماره دیگری وارد کن.")
                return
            db.execute("INSERT INTO payout_cards(user_id, card_number) VALUES (?, ?)", (user_id, card))

        steps.pop(user_id, None)
        bot.send_message(chat_id, "✅ شماره کارت ذخیره شد.")
        ask_payout_card(chat_id, user_id)
        return

    if step["action"] == "buy_username":
        if text.lower() == "خودم":
            target = get_user(user_id)
        elif text.isdigit():
            target = get_user(int(text))
        elif re.fullmatch(r"@?[A-Za-z0-9_]{5,32}", text):
            target = find_username(text)
        else:
            target = None
        if not target:
            bot.send_message(chat_id, "❌ کاربر پیدا نشد. باید قبلاً ربات را با /start فعال کرده باشد؛ دوباره بفرست.")
            return
        if target[6]:
            bot.send_message(chat_id, "❌ حساب این کاربر مسدود است.")
            return
        step["target_id"] = target[0]
        step["target_name"] = target[2] or target[1] or str(target[0])
        step["action"] = "buy_amount"
        step["amount_text"] = ""
        bot.send_message(chat_id, f"✅ گیرنده: {escape(str(step['target_name']))}")
        show_amount_keypad(chat_id, user_id)
        return

    if step["action"] == "buy_coupon":
        finish_buy_coupon(user_id, chat_id, text)
        return

    if step["action"] in ("buy_amount", "sell_amount"):
        if not text.isdigit() or not (0 < int(text) <= MAX_AMOUNT):
            bot.send_message(chat_id, "❌ تعداد باید عدد مثبت و در محدوده مجاز باشد.")
            return
        amount = int(text)

        if step["action"] == "buy_amount":
            # پس از دریافت تعداد، کد تخفیف را می‌پرسیم؛ کاربر می‌تواند «ندارم» بزند.
            step["amount"] = amount
            step["action"] = "buy_coupon_choice"
            base_price = amount * BUY_PRICE_TOMAN
            bot.send_message(chat_id,
                f"💎 تعداد: <b>{amount:,} ZEX</b>\n"
                f"💵 مبلغ قبل از تخفیف: <b>{base_price:,} تومان</b>\n\n"
                "آیا کد تخفیف دارید؟", reply_markup=coupon_choice_keyboard())
            return

        user = get_user(user_id)
        if amount < MIN_SELL:
            bot.send_message(chat_id, f"❌ حداقل فروش {MIN_SELL:,} ZEX است.")
            return
        if not user or amount > user[3]:
            bot.send_message(chat_id, "❌ موجودی کافی نیست.")
            steps.pop(user_id, None)
            return
        total_price = amount * SELL_PRICE_TOMAN
        payout_card = step.get("payout_card", "")
        if not payout_card:
            bot.send_message(chat_id, "❌ کارت مقصد انتخاب نشده؛ فروش را دوباره شروع کن.")
            steps.pop(user_id, None)
            return
        request_id = create_request(user_id, "sell", user_id, amount, total_price)
        with connect_db() as db:
            db.execute("UPDATE requests SET payout_card=? WHERE id=?", (payout_card, request_id))
        steps.pop(user_id, None)
        bot.send_message(chat_id,
            f"✅ <b>درخواست فروش ثبت شد</b>\n💎 تعداد: <b>{amount:,} ZEX</b>\n"
            f"💵 مبلغی که مدیر باید واریز کند: <b>{total_price:,} تومان</b>\n"
            f"💳 کارت مقصد: <code>{escape(payout_card)}</code>\n"
            f"🔖 درخواست: <code>{request_id}</code>\n🟤 وضعیت: ثبت شد؛ در انتظار بررسی مدیر.\n⏳ پس از بررسی، مدیر پرداخت را انجام می‌دهد و با تأیید نهایی موجودی کم می‌شود.")
        seller = get_user(user_id)
        uname = f"@{seller[1]}" if seller and seller[1] else "ندارد"
        send_to_admins(
            "📤 <b>درخواست فروش ZEX</b>\n"
            f"👤 نام: {escape(str(seller[2] if seller else ''))}\n🔗 نام کاربری: {escape(uname)}\n"
            f"🆔 آیدی عددی: <code>{user_id}</code>\n💎 تعداد فروش: <b>{amount:,} ZEX</b>\n"
            f"💵 مبلغ قابل واریز: <b>{total_price:,} تومان</b>\n"
            f"💳 شماره کارت دریافت‌کننده: <code>{escape(payout_card)}</code>\n"
            f"💰 موجودی فعلی: <b>{seller[3] if seller else 0:,} ZEX</b>\n🔖 درخواست: <code>{request_id}</code>\n🟤 وضعیت: در انتظار بررسی مدیر",
            request_buttons(request_id))


# ==============================
# تاریخچه تراکنش‌های شخصی کاربر فعال است؛ گزارش‌های مدیریتی تراکنش جداگانه حذف شده‌اند.

# ==============================
# تاریخچه تراکنش‌های شخصی کاربر
# ==============================

def show_history(chat_id, user_id, days=None, trade_filter="all"):
    with connect_db() as db:
        params = [user_id]
        if trade_filter in ("buy", "sell"):
            rows = []
        else:
            if days is None:
                rows = db.execute("""SELECT item_type, amount, reason, created_at FROM (
                    SELECT 'zex' AS item_type, amount, reason, created_at FROM transactions WHERE user_id=? AND kind NOT IN ('buy','sell')
                    UNION ALL SELECT 'points' AS item_type, amount, reason, created_at FROM point_transactions WHERE user_id=?
                ) ORDER BY created_at DESC LIMIT 30""", (user_id, user_id)).fetchall()
            else:
                modifier = f"-{int(days)} days"
                rows = db.execute("""SELECT item_type, amount, reason, created_at FROM (
                    SELECT 'zex' AS item_type, amount, reason, created_at FROM transactions WHERE user_id=? AND kind NOT IN ('buy','sell') AND datetime(created_at)>=datetime('now', ?)
                    UNION ALL SELECT 'points' AS item_type, amount, reason, created_at FROM point_transactions WHERE user_id=? AND datetime(created_at)>=datetime('now', ?)
                ) ORDER BY created_at DESC LIMIT 30""", (user_id, modifier, user_id, modifier)).fetchall()
        request_rows = []
        if trade_filter in ("all", "buy", "sell"):
            req_sql = "SELECT id, kind, amount, total_price, status, created_at FROM requests WHERE user_id=? AND kind IN ('buy','sell')"
            req_params = [user_id]
            if trade_filter == "buy": req_sql += " AND kind='buy'"
            if trade_filter == "sell": req_sql += " AND kind='sell'"
            if days is not None:
                req_sql += " AND datetime(created_at)>=datetime('now', ?)"
                req_params.append(f"-{int(days)} days")
            req_sql += " ORDER BY created_at DESC LIMIT 20"
            request_rows = db.execute(req_sql, tuple(req_params)).fetchall()
    if not rows and not request_rows:
        bot.send_message(chat_id, "📭 تراکنشی برای نمایش نداری.")
        return
    lines = ["📜 <b>تاریخچه تراکنش‌ها</b>", "━━━━━━━━━━━━━━"]
    for item_type, amount, reason, created_at in rows:
        value = float(amount or 0) if item_type == "points" else int(amount or 0)
        icon = "🟢" if value > 0 else "🔴" if value < 0 else "⚪"
        amount_text = f"{value:+g} ZEX" if item_type == "points" else f"{value:+,} ZEX"
        category = "🏅 ZEX بازی" if item_type == "points" else "💎 ZEX"
        lines.append(f"{icon} <b>{escape(amount_text)}</b> · {category}\n📝 {escape(str(reason or 'تراکنش'))}\n🕒 {escape(format_iran_datetime(created_at))}")
    for req_id, kind, amount, price, status, created_at in request_rows:
        if status == "approved": icon, state = "🟢", "تأیید شده / پرداخت انجام شده"
        elif status == "reviewing": icon, state = "🟡", "در حال بررسی"
        elif status == "rejected": icon, state = "🔴", "لغو شده"
        else: icon, state = "🟤", "در انتظار بررسی مدیر"
        label = "خرید" if kind == "buy" else "فروش"
        lines.append(f"{icon} <b>{label} {amount:,} ZEX</b> — {state}\n💵 {price:,} تومان · درخواست <code>{req_id}</code>\n🕒 {escape(format_iran_datetime(created_at))}")
    bot.send_message(chat_id, "\n\n".join(lines)[:4000])


@bot.message_handler(func=lambda m: m.text == "📜 تراکنش‌ها")
def history_button(message):
    keyboard = types.InlineKeyboardMarkup()
    keyboard.row(
        types.InlineKeyboardButton("یک روز اخیر", callback_data="history_1"),
        types.InlineKeyboardButton("همه تراکنش‌ها", callback_data="history_all")
    )
    keyboard.row(
        types.InlineKeyboardButton("🛒 خریدهای ZEX", callback_data="history_buy"),
        types.InlineKeyboardButton("📤 فروش‌های ZEX", callback_data="history_sell")
    )
    bot.send_message(message.chat.id, "📜 کدام تاریخچه را می‌خواهی؟", reply_markup=keyboard)


@bot.callback_query_handler(func=lambda call: call.data in ("history_1", "history_all", "history_buy", "history_sell"))
def history_callback(call):
    if call.data == "history_1": show_history(call.message.chat.id, call.from_user.id, 1)
    elif call.data == "history_buy": show_history(call.message.chat.id, call.from_user.id, None, "buy")
    elif call.data == "history_sell": show_history(call.message.chat.id, call.from_user.id, None, "sell")
    else: show_history(call.message.chat.id, call.from_user.id, None)
    bot.answer_callback_query(call.id)


@bot.message_handler(commands=["history"])
def history_command(message):
    show_history(message.chat.id, message.from_user.id)


@bot.callback_query_handler(func=lambda call: call.data == "payout_add" or call.data == "payout_use_verified" or call.data.startswith("payout_use_"))
def payout_card_callback(call):
    # انتخاب کارت احراز هویت‌شده، کارت ذخیره‌شده یا افزودن کارت تازه.
    user_id = call.from_user.id
    if call.data == "payout_add":
        steps[user_id] = {"action": "sell_card_add"}
        bot.send_message(call.message.chat.id, "💳 شماره کارت ۱۶ رقمی مقصد واریز این فروش را بفرست.")
        bot.answer_callback_query(call.id)
        return

    if call.data == "payout_use_verified":
        card = get_card_number(user_id)
        if not card:
            bot.answer_callback_query(call.id, "کارت احراز هویت‌شده پیدا نشد؛ دوباره تلاش کن.", show_alert=True)
            return
        ask_sell_amount(call.message.chat.id, user_id, card)
        bot.answer_callback_query(call.id, "کارت احراز هویت انتخاب شد.")
        return

    try:
        card_id = int(call.data.rsplit("_", 1)[1])
    except ValueError:
        bot.answer_callback_query(call.id, "انتخاب نامعتبر است.", show_alert=True)
        return
    with connect_db() as db:
        row = db.execute("SELECT card_number FROM payout_cards WHERE id=? AND user_id=?", (card_id, user_id)).fetchone()
    if not row:
        bot.answer_callback_query(call.id, "این کارت پیدا نشد؛ دوباره انتخاب کن.", show_alert=True)
        return
    ask_sell_amount(call.message.chat.id, user_id, row[0])
    bot.answer_callback_query(call.id, "کارت انتخاب شد.")


# انتقال ZEX با ریپلای یا آیدی عددی در گروه.
def perform_points_transfer(message, receiver_id, amount):
    sender_id = message.from_user.id
    if is_balance_locked(sender_id) or is_balance_locked(receiver_id):
        bot.reply_to(message, "🔒 موجودی یکی از حساب‌ها موقتاً برای بررسی درخواست فروش قفل است؛ بعداً دوباره تلاش کن.")
        return
    if amount <= 0 or amount > MAX_AMOUNT:
        bot.reply_to(message, "❌ مقدار ZEX معتبر نیست.")
        return
    if sender_id == receiver_id:
        bot.reply_to(message, "❌ نمی‌توانی به خودت انتقال بدهی.")
        return
    if not get_user(receiver_id):
        bot.reply_to(message, "❌ گیرنده هنوز ربات را با /start فعال نکرده است.")
        return
    try:
        with connect_db() as db:
            db.execute("BEGIN IMMEDIATE")
            sender = db.execute("SELECT balance, blocked FROM users WHERE user_id=?", (sender_id,)).fetchone()
            receiver = db.execute("SELECT blocked FROM users WHERE user_id=?", (receiver_id,)).fetchone()
            if not sender or sender[1] or not receiver or receiver[0]:
                db.rollback(); bot.reply_to(message, "❌ حساب فرستنده یا گیرنده فعال نیست."); return
            if float(sender[0] or 0) < amount:
                db.rollback(); bot.reply_to(message, f"❌ ZEX کافی نیست. موجودی ZEX: {fmt_points(sender[0])}"); return
            db.execute("UPDATE users SET balance=balance-? WHERE user_id=?", (amount, sender_id))
            db.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amount, receiver_id))
            add_point_transaction(db, sender_id, -amount, f"انتقال ZEX به {receiver_id}")
            add_point_transaction(db, receiver_id, amount, f"دریافت ZEX از {sender_id}")
            db.commit()
        bot.reply_to(message, f"✅ انتقال {fmt_points(amount)} ZEX انجام شد.\n🏅 ZEX باقی‌مانده: {fmt_points(get_game_points(sender_id))}")
    except Exception as e:
        print(f"خطای انتقال ZEX: {e}")
        bot.reply_to(message, "❌ انتقال انجام نشد؛ دوباره تلاش کن.")

@bot.message_handler(func=lambda m: bool(m.text and re.fullmatch(r"انتقال ZEX\s+\d+", m.text.strip())) and m.chat.type in ("group", "supergroup"))
def points_transfer_command(message):
    amount = int(message.text.strip().split()[-1])
    if message.reply_to_message and message.reply_to_message.from_user:
        if message.reply_to_message.from_user.is_bot:
            bot.reply_to(message, "❌ به ربات نمی‌توان ZEX انتقال داد."); return
        perform_points_transfer(message, message.reply_to_message.from_user.id, amount)
    else:
        bot.reply_to(message, "ℹ️ برای انتقال ZEX، روی پیام گیرنده ریپلای کن و بنویس: انتقال ZEX 300")

# ==============================
# انتقال ZEX با ریپلای در گروه
# ==============================

def perform_transfer(message, receiver_id, amount):
    # انتقال فقط در گروه انجام می‌شود و مبلغ/گیرنده پیش از تغییر دیتابیس بررسی می‌شوند.
    if message.chat.type not in ("group", "supergroup"):
        bot.reply_to(message, "❌ انتقال فقط داخل گروه امکان‌پذیر است.")
        return
    sender_id = message.from_user.id
    if not isinstance(amount, int) or amount <= 0 or amount > MAX_AMOUNT:
        bot.reply_to(message, "❌ تعداد معتبر نیست؛ یک عدد مثبت وارد کن.")
        return
    if sender_id == receiver_id:
        bot.reply_to(message, "❌ نمی‌توانی به خودت انتقال بدهی.")
        return
    receiver = get_user(receiver_id)
    if not receiver:
        bot.reply_to(message, "❌ گیرنده هنوز ربات را با /start فعال نکرده است.")
        return
    if receiver[6]:
        bot.reply_to(message, "❌ حساب گیرنده مسدود است.")
        return
    if receiver_id == MANAGER_ID:
        bot.reply_to(message, "❌ انتقال مستقیم به مدیر اصلی امکان‌پذیر نیست.")
        return
    if not check_active(sender_id):
        bot.reply_to(message, "❌ حساب شما فعال نیست.")
        return

    fee = amount * TRANSFER_FEE_PERCENT // 100
    total = amount + fee
    try:
        with connect_db() as db:
            db.execute("BEGIN IMMEDIATE")
            sender = db.execute("SELECT balance, blocked FROM users WHERE user_id=?", (sender_id,)).fetchone()
            target = db.execute("SELECT blocked FROM users WHERE user_id=?", (receiver_id,)).fetchone()
            if is_balance_locked(sender_id, db) or is_balance_locked(receiver_id, db):
                db.rollback()
                bot.reply_to(message, "🔒 موجودی یکی از حساب‌ها موقتاً برای بررسی درخواست فروش قفل است؛ بعد از پایان بررسی دوباره تلاش کن.")
                return
            if not sender or sender[1] or not target or target[0]:
                db.rollback()
                bot.reply_to(message, "❌ حساب فرستنده یا گیرنده غیرفعال است.")
                return
            balance = sender[0]
            # مطابق منطق فعلی ربات، در صورت کمبود موجودی، کسری به‌عنوان بدهی ثبت می‌شود.
            paid = min(balance, total)
            debt_amount = max(0, total - balance)
            db.execute("UPDATE users SET balance=balance-? WHERE user_id=?", (paid, sender_id))
            db.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amount, receiver_id))
            add_transaction(db, sender_id, -paid, f"انتقال به {receiver_id}", "transfer_out")
            add_transaction(db, receiver_id, amount, f"دریافت از {sender_id}", "transfer_in")
            if debt_amount:
                db.execute("UPDATE users SET debt=debt+? WHERE user_id=?", (debt_amount, sender_id))
                db.execute("INSERT INTO debt_transactions (user_id, amount, reason, created_at) VALUES (?, ?, ?, datetime('now'))",
                           (sender_id, debt_amount, f"بدهی انتقال به {receiver_id}"))
            db.commit()
    except Exception as error:
        print(f"خطای انتقال: {error}")
        bot.reply_to(message, "❌ انتقال انجام نشد؛ خطای دیتابیس رخ داد.")
        return
    receiver_name = escape(receiver[2] or receiver[1] or str(receiver_id))
    bot.reply_to(message,
        "✅ انتقال انجام شد.\n"
        f"👤 گیرنده: {receiver_name}\n💎 مبلغ: <b>{amount:,} ZEX</b>\n"
        f"💸 کارمزد: <b>{fee:,} ZEX</b>\n📌 بدهی اضافه‌شده: <b>{debt_amount:,} ZEX</b>")


@bot.message_handler(func=lambda m: bool(m.text and m.text.strip() == "انتقال") and m.chat.type in ("group", "supergroup"))
def transfer_without_amount(message):
    # با ریپلای، گیرنده از پیام انتخاب می‌شود؛ بدون ریپلای، از کاربر آیدی گیرنده خواسته می‌شود.
    if message.reply_to_message and message.reply_to_message.from_user:
        receiver_id = message.reply_to_message.from_user.id
        if message.reply_to_message.from_user.is_bot:
            bot.reply_to(message, "❌ انتقال به ربات امکان‌پذیر نیست.")
            return
        steps[message.from_user.id] = {"action": "transfer_amount", "receiver_id": receiver_id, "chat_id": message.chat.id}
        bot.reply_to(message, "🔢 چه مقدار ZEX می‌خواهی انتقال بدهی؟ فقط عدد را بفرست.")
    else:
        steps[message.from_user.id] = {"action": "transfer_receiver", "chat_id": message.chat.id}
        bot.reply_to(message, "🆔 آیدی عددی گیرنده را بفرست.")


@bot.message_handler(func=lambda m: bool(m.text and re.fullmatch(r"انتقال\s+\d+", m.text.strip())) and m.chat.type in ("group", "supergroup"))
def transfer_command(message):
    # «انتقال 500» روی پیام گیرنده، مبلغ است؛ بدون ریپلای، عدد به‌عنوان آیدی گیرنده در نظر گرفته می‌شود.
    parts = message.text.strip().split()
    if len(parts) != 2 or not parts[1].isdigit() or int(parts[1]) <= 0:
        bot.reply_to(message, "❌ فرمت درست نیست. روی پیام طرف ریپلای کن و بنویس «انتقال»، یا بنویس «انتقال آیدی».")
        return
    number = int(parts[1])
    if message.reply_to_message and message.reply_to_message.from_user:
        if message.reply_to_message.from_user.is_bot:
            bot.reply_to(message, "❌ انتقال به ربات امکان‌پذیر نیست.")
            return
        perform_transfer(message, message.reply_to_message.from_user.id, number)
    else:
        receiver = get_user(number)
        if not receiver:
            bot.reply_to(message, "❌ این آیدی در ربات ثبت نشده؛ گیرنده باید یک بار /start را بزند.")
            return
        steps[message.from_user.id] = {"action": "transfer_amount", "receiver_id": number, "chat_id": message.chat.id}
        bot.reply_to(message, f"🔢 گیرنده انتخاب شد (آیدی <code>{number}</code>). حالا مقدار ZEX را بفرست.")


# ==============================
# درخواست‌های خرید و فروش
# ==============================

def handle_request(call, request_id, action):
    admin_id = call.from_user.id
    if not is_admin(admin_id):
        bot.answer_callback_query(call.id, "اجازه انجام این کار را نداری.", show_alert=True)
        return
    try:
        with connect_db() as db:
            db.execute("BEGIN IMMEDIATE")
            request = db.execute("""
                SELECT id, user_id, kind, target_user_id, amount, total_price, status
                FROM requests WHERE id=?
            """, (request_id,)).fetchone()
            if not request:
                db.rollback()
                bot.answer_callback_query(call.id, "درخواست پیدا نشد.", show_alert=True)
                return
            req_id, user_id, kind, target_id, amount, total_price, status = request
            if status not in ("pending", "reviewing"):
                db.rollback()
                bot.answer_callback_query(call.id, "این درخواست قبلاً نهایی شده است.", show_alert=True)
                return
            if action == "review":
                if kind == "sell":
                    balance_row = db.execute("SELECT balance FROM users WHERE user_id=?", (target_id,)).fetchone()
                    if not balance_row or balance_row[0] < amount:
                        db.rollback()
                        bot.answer_callback_query(call.id, "❌ موجودی کاربر برای این مقدار فروش کافی نیست؛ درخواست وارد بررسی نشد.", show_alert=True)
                        try:
                            bot.send_message(call.message.chat.id, f"❌ درخواست فروش #{req_id}: موجودی کاربر کافی نیست. موجودی فعلی: {balance_row[0] if balance_row else 0:,} ZEX | مقدار درخواست: {amount:,} ZEX")
                        except Exception:
                            pass
                        return
                    existing_lock = db.execute("SELECT request_id FROM balance_locks WHERE user_id=?", (target_id,)).fetchone()
                    if existing_lock and existing_lock[0] != req_id:
                        db.rollback()
                        bot.answer_callback_query(call.id, "این کاربر درخواست فروش دیگری در حال بررسی دارد.", show_alert=True)
                        return
                    db.execute("INSERT INTO balance_locks(user_id, request_id) VALUES(?, ?) ON CONFLICT(user_id) DO UPDATE SET request_id=excluded.request_id, created_at=datetime('now')", (target_id, req_id))
                db.execute("UPDATE requests SET status='reviewing', handled_by=? WHERE id=? AND status IN ('pending','reviewing')", (admin_id, req_id))
                db.commit()
                try:
                    bot.send_message(user_id, f"🟡 درخواست شماره <code>{req_id}</code> توسط مدیر بررسی می‌شود. وضعیت: در حال بررسی")
                except Exception:
                    pass
                bot.answer_callback_query(call.id, "وضعیت به در حال بررسی تغییر کرد.")
                try:
                    bot.edit_message_text(call.message.text + "\n\n🟡 وضعیت: در حال بررسی", call.message.chat.id, call.message.message_id, reply_markup=request_buttons(req_id))
                except Exception:
                    pass
                return
            approve = action == "approve"
            if not approve:
                db.execute("UPDATE requests SET status='rejected', handled_by=? WHERE id=? AND status IN ('pending','reviewing')", (admin_id, req_id))
                db.execute("DELETE FROM balance_locks WHERE user_id=? AND request_id=?", (target_id, req_id))
                # اگر درخواست خرید رد شد، کد تخفیف برای استفاده بعدی آزاد می‌شود.
                db.execute("DELETE FROM discount_uses WHERE request_id=?", (req_id,))
            else:
                requester = db.execute("SELECT blocked FROM users WHERE user_id=?", (user_id,)).fetchone()
                target = db.execute("SELECT balance, blocked FROM users WHERE user_id=?", (target_id,)).fetchone()
                if not requester or requester[0] or not target or target[1]:
                    db.rollback()
                    bot.answer_callback_query(call.id, "حساب درخواست‌دهنده یا گیرنده فعال نیست.", show_alert=True)
                    return
                if kind == "buy":
                    db.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amount, target_id))
                    add_transaction(db, target_id, amount, f"خرید؛ درخواست {req_id}", "buy")
                elif kind == "sell":
                    if target[0] < amount:
                        db.rollback()
                        bot.answer_callback_query(call.id, "موجودی کاربر برای فروش کافی نیست.", show_alert=True)
                        return
                    db.execute("DELETE FROM balance_locks WHERE user_id=? AND request_id=?", (target_id, req_id))
                    db.execute("UPDATE users SET balance=balance-? WHERE user_id=?", (amount, target_id))
                    add_transaction(db, target_id, -amount, f"فروش؛ درخواست {req_id}", "sell")
                else:
                    db.rollback()
                    bot.answer_callback_query(call.id, "نوع درخواست نامعتبر است.", show_alert=True)
                    return
                db.execute("UPDATE requests SET status='approved', handled_by=? WHERE id=? AND status IN ('pending','reviewing')", (admin_id, req_id))
                db.execute("DELETE FROM balance_locks WHERE user_id=? AND request_id=?", (target_id, req_id))
                # فعالیت خرید/فروش ZEX برای کمپین ارسال ZEX تا ۴۸ ساعت معتبر است.
                db.execute("UPDATE users SET zex_activity_at=? WHERE user_id=?", (int(time.time()), user_id))
            db.commit()
    except Exception as error:
        print(f"خطای بررسی درخواست: {error}")
        bot.answer_callback_query(call.id, "خطا هنگام بررسی درخواست.", show_alert=True)
        return

    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    if approve and kind == "sell":
        result = (f"✅ درخواست فروش شماره {request_id} تأیید شد.\n"
                  f"💎 {amount:,} ZEX از موجودی کم شد.\n"
                  f"💵 مبلغ قابل واریز: {total_price:,} تومان")
    elif approve and kind == "buy":
        result = (f"✅ درخواست خرید شماره {request_id} تأیید شد.\n"
                  f"💎 {amount:,} ZEX به موجودی گیرنده اضافه شد.\n"
                  f"💵 مبلغ ثبت‌شده: {total_price:,} تومان")
    else:
        result = f"❌ درخواست شماره {request_id} رد شد."
    try:
        bot.send_message(user_id, result)
    except Exception:
        pass
    bot.answer_callback_query(call.id, "انجام شد.")
    try:
        bot.send_message(call.message.chat.id, result)
    except Exception:
        pass


@bot.callback_query_handler(func=lambda call: call.data.startswith("req_ok_") or call.data.startswith("req_no_") or call.data.startswith("req_review_"))
def request_callback(call):
    parts = call.data.split("_")
    if len(parts) != 3 or not parts[2].isdigit():
        bot.answer_callback_query(call.id, "درخواست نامعتبر است.")
        return
    action = {"ok": "approve", "no": "cancel", "review": "review"}.get(parts[1])
    if not action:
        bot.answer_callback_query(call.id, "عملیات نامعتبر است.")
        return
    handle_request(call, int(parts[2]), action)



# جست‌وجوی مستقیم درخواست توسط مدیر با شماره آن.
@bot.message_handler(func=lambda m: bool(m.text and re.fullmatch(r"(?:شماره\s+درخواست|درخواست)\s+\d+", m.text.strip())))
def admin_find_request(message):
    admin_id = message.from_user.id if message.from_user else 0
    if not is_admin(admin_id):
        return

    request_id = int(message.text.strip().split()[-1])
    with connect_db() as db:
        row = db.execute(
            """SELECT r.id, r.user_id, r.kind, r.target_user_id, r.amount,
                      r.total_price, r.status, r.created_at, r.handled_by,
                      r.payout_card, u.first_name, u.username
               FROM requests r LEFT JOIN users u ON u.user_id=r.user_id
               WHERE r.id=?""",
            (request_id,)
        ).fetchone()

    if not row:
        bot.reply_to(message, f"🔎 درخواست <code>{request_id}</code> پیدا نشد.")
        return

    rid, user_id, kind, target_id, amount, total_price, status, created_at, handled_by, payout_card, first_name, username = row
    kind_text = {"buy": "خرید ZEX", "sell": "فروش ZEX"}.get(kind, str(kind))
    status_text = {
        "pending": "🟡 در انتظار بررسی",
        "reviewing": "🔎 در حال بررسی",
        "approved": "✅ تأییدشده",
        "rejected": "❌ ردشده",
        "cancelled": "❌ لغوشده",
    }.get(status, str(status))
    user_label = escape(first_name or "کاربر")
    if username:
        user_label += " (@" + escape(username) + ")"

    body = (
        f"🔎 <b>درخواست #{rid}</b>\n"
        f"━━━━━━━━━━━━\n"
        f"📦 نوع: <b>{escape(kind_text)}</b>\n"
        f"👤 درخواست‌دهنده: {user_label}\n"
        f"🆔 آیدی: <code>{user_id}</code>\n"
        f"🎯 گیرنده: <code>{target_id}</code>\n"
        f"🔢 مقدار: <b>{amount:,} ZEX</b>\n"
        f"💵 مبلغ: <b>{total_price:,} تومان</b>\n"
        f"📌 وضعیت: {status_text}\n"
        f"🕒 زمان: {escape(format_iran_datetime(created_at))}"
    )
    if payout_card:
        body += f"\n💳 کارت پرداخت: <code>{escape(str(payout_card))}</code>"
    if handled_by:
        body += f"\n🛡 بررسی‌کننده: <code>{handled_by}</code>"

    markup = request_buttons(rid) if status in ("pending", "reviewing") else None
    bot.reply_to(message, body, reply_markup=markup)

# ==============================
# پنل مدیریت
# ==============================

def admin_menu(user_id=None):
    keyboard = types.InlineKeyboardMarkup()
    keyboard.row(types.InlineKeyboardButton("📊 آمار کاربران", callback_data="admin_stats"))
    keyboard.row(types.InlineKeyboardButton("👥 اعضای ربات", callback_data="admin_members"),
                 types.InlineKeyboardButton("🔔 اعلان‌ها", callback_data="admin_notifications"))
    keyboard.row(types.InlineKeyboardButton("✉️ پیام به اعضا", callback_data="admin_broadcast"))
    # فقط مدیر اصلی این دو دکمه را می‌بیند.
    keyboard.row(types.InlineKeyboardButton("💎 افزایش/کاهش ZEX خودم", callback_data="admin_self_zex")) if False else None
    keyboard.row(
        types.InlineKeyboardButton("➕ افزایش موجودی", callback_data="admin_add"),
        types.InlineKeyboardButton("➖ کاهش موجودی", callback_data="admin_remove")
    )
    keyboard.row(
        types.InlineKeyboardButton("🚫 مسدود کردن", callback_data="admin_block"),
        types.InlineKeyboardButton("✅ رفع مسدودی", callback_data="admin_unblock")
    )
    keyboard.row(types.InlineKeyboardButton("🚨 هشدارها", callback_data="admin_warnings"))
    if user_id == MANAGER_ID:
        keyboard.row(types.InlineKeyboardButton("💸 تغییر قیمت زیرمجموعه‌گیری", callback_data="admin_referral_reward"))
        keyboard.row(types.InlineKeyboardButton("⚖️ تغییر موجودی کاربران", callback_data="admin_balance_scale"))
        keyboard.row(types.InlineKeyboardButton("👥 ادمین‌ها", callback_data="admin_admin_list"))
        keyboard.row(types.InlineKeyboardButton("🎁 پاداش عضویت زمان‌دار", callback_data="admin_join_bonus"))
    keyboard.row(
        types.InlineKeyboardButton("🗑 حذف از ربات", callback_data="admin_remove_from_bot"),
        types.InlineKeyboardButton("👢 حذف از گپ", callback_data="admin_remove_from_group")
    )
    keyboard.row(
        types.InlineKeyboardButton("⭐ ادمین کردن", callback_data="admin_make_admin"),
        types.InlineKeyboardButton("👤 حذف ادمین", callback_data="admin_remove_admin")
    )
    # مدیریت کدهای تخفیف فقط برای مدیر اصلی فعال است.
    keyboard.row(types.InlineKeyboardButton("🏷 مدیریت کدهای تخفیف", callback_data="admin_discounts"))
    return keyboard



# ---------- مدیریت کدهای تخفیف به سبک LUX ----------
def discount_admin_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("1) ساخت کد تخفیف", callback_data="dsc_create"))
    kb.add(types.InlineKeyboardButton("2) کدهای تخفیف و فعال/غیرفعال", callback_data="dsc_list"))
    kb.add(types.InlineKeyboardButton("3) حذف کد تخفیف", callback_data="dsc_delete"))
    return kb


def parse_discount_duration(value):
    raw = (value or "").strip().lower()
    m = re.fullmatch(r"(\d+)\s*(دقیقه|دقیقه‌ای|min|mins|m|ساعت|ساعته|hour|hours|h|روز|روزه|day|days|d|هفته|هفته‌ای|week|weeks|w)?", raw)
    if not m: return None
    n = int(m.group(1)); unit = m.group(2) or "ساعت"
    factors = {"دقیقه":60,"دقیقه‌ای":60,"min":60,"mins":60,"m":60,"ساعت":3600,"ساعته":3600,"hour":3600,"hours":3600,"h":3600,"روز":86400,"روزه":86400,"day":86400,"days":86400,"d":86400,"هفته":604800,"هفته‌ای":604800,"week":604800,"weeks":604800,"w":604800}
    return n * factors[unit] if n > 0 else None


def discount_amount_keyboard(uid):
    st = steps[uid]; amount = int(st.get("discount_amount_input", "") or 0); kind = st.get("discount_type")
    kb = types.InlineKeyboardMarkup(row_width=3)
    if kind == "timed": kb.add(types.InlineKeyboardButton("تغییر زمان", callback_data="dsc_change_time"))
    elif kind == "count": kb.add(types.InlineKeyboardButton("تغییر تعداد", callback_data="dsc_change_count"))
    for row in (("1","2","3"),("4","5","6"),("7","8","9"),("0","حذف ۱ رقم","حذف کل")):
        buttons=[]
        for key in row:
            data = "dscpad_clear" if key == "حذف کل" else ("dscpad_back" if key == "حذف ۱ رقم" else "dscpad_"+key)
            buttons.append(types.InlineKeyboardButton(key, callback_data=data))
        kb.row(*buttons)
    kb.row(types.InlineKeyboardButton("✅ ادامه", callback_data="dscpad_confirm"), types.InlineKeyboardButton("❌ لغو", callback_data="dsc_cancel"))
    detail = (f"مدت اعتبار: <b>{escape(st.get('discount_duration_label',''))}</b>" if kind == "timed" else f"تعداد دفعات مجاز: <b>{st.get('discount_count',0):,}</b>" if kind == "count" else "اعتبار: <b>تمام‌وقت تا غیرفعال‌سازی مدیر</b>")
    return f"🏷 <b>ساخت کد تخفیف</b>\n{detail}\nمقدار کسر از مبلغ: <b>{amount:,} تومان</b>\n\nمقدار تخفیف را وارد کن:", kb


def discount_codes_text():
    with connect_db() as db:
        rows = db.execute("SELECT code,discount_amount,max_uses,used_count,expires_at,created_at,active FROM discount_codes ORDER BY rowid DESC").fetchall()
    if not rows: return "🏷 <b>کدهای تخفیف</b>\nهنوز کدی ساخته نشده است.", []
    lines = ["🏷 <b>کدهای تخفیف</b>", "برای تغییر وضعیت، دکمه زیر هر کد را بزن:"]
    for code, amount, limit, used, expires, created, active in rows:
        typ = "زمان‌دار" if expires is not None and not limit else "تعداددار" if limit else "تمام‌وقت"
        if expires is not None and limit: typ = "زمان‌دار + تعداددار"
        expired = expires is not None and expires < int(time.time())
        remain = max(0, int(limit or 0)-int(used or 0)) if limit else None
        state = "غیرفعال" if not active else "منقضی" if expired else f"باقی‌مانده {remain:,}" if remain is not None else "نامحدود"
        lines.append(f"\n• <code>{escape(code)}</code> | تخفیف {amount:,} تومان | {typ} | {state}")
    return "".join(lines), rows


def discount_codes_markup(rows):
    kb = types.InlineKeyboardMarkup(row_width=1)
    for code, amount, limit, used, expires, created, active in rows:
        label = ("⏸ غیرفعال‌کردن" if active else "▶️ فعال‌کردن") + " | " + code
        kb.add(types.InlineKeyboardButton(label, callback_data="dsc_toggle_"+code))
    return kb


def show_discount_delete_list(call, uid, edit=False):
    st = steps[uid]
    with connect_db() as db:
        rows = db.execute("SELECT code,discount_amount,max_uses,expires_at FROM discount_codes ORDER BY rowid DESC").fetchall()
    kb = types.InlineKeyboardMarkup(row_width=1)
    for code, amount, limit, expires in rows:
        mark = "☑️" if code in st.get("selected",[]) else "▫️"
        typ = "زمان‌دار" if expires is not None and not limit else "تعداددار" if limit else "تمام‌وقت"
        if expires is not None and limit: typ = "زمان‌دار + تعداددار"
        kb.add(types.InlineKeyboardButton(f"{mark} {code} | {amount:,} | {typ}", callback_data="dsc_del_toggle_"+code))
    kb.add(types.InlineKeyboardButton(f"🗑 حذف انتخاب‌شده ({len(st.get('selected',[]))})", callback_data="dsc_del_confirm"))
    txt = "برای حذف، کدها را انتخاب کن؛ امکان انتخاب چند کد وجود دارد:" if rows else "هیچ کد تخفیفی وجود ندارد."
    if edit: return bot.edit_message_text(txt, call.message.chat.id, call.message.message_id, reply_markup=kb if rows else None)
    return bot.send_message(call.message.chat.id, txt, reply_markup=kb if rows else None)


@bot.callback_query_handler(func=lambda c: c.data.startswith("dsc_") or c.data.startswith("dscpad_"))
def discount_admin_callbacks(c):
    uid = c.from_user.id
    if not is_manager(uid): return bot.answer_callback_query(c.id, "فقط مدیر اصلی دسترسی دارد.", show_alert=True)
    data = c.data
    if data == "dsc_create":
        kb=types.InlineKeyboardMarkup(row_width=1)
        kb.add(types.InlineKeyboardButton("1) زمان‌دار",callback_data="dsc_timed"))
        kb.add(types.InlineKeyboardButton("2) تعداددار (بدون محدودیت زمانی)",callback_data="dsc_count"))
        kb.add(types.InlineKeyboardButton("3) تمام‌وقت",callback_data="dsc_perm"))
        bot.answer_callback_query(c.id); return bot.send_message(c.message.chat.id,"نوع کد تخفیف را انتخاب کن:",reply_markup=kb)
    if data in ("dsc_timed","dsc_count","dsc_perm"):
        kind={"dsc_timed":"timed","dsc_count":"count","dsc_perm":"permanent"}[data]
        steps[uid]={"action":"discount_setup","discount_type":kind,"discount_count":0}
        bot.answer_callback_query(c.id)
        if kind == "timed": steps[uid]["action"]="discount_duration"; return bot.send_message(c.message.chat.id,"مدت اعتبار را وارد کن؛ مثال: 30 دقیقه، 3 ساعت، 1 روز یا 2 هفته:")
        if kind == "count": steps[uid]["action"]="discount_count"; return bot.send_message(c.message.chat.id,"حداکثر تعداد دفعات استفاده را وارد کن:")
        steps[uid]["action"]="discount_amount_pad"; steps[uid]["discount_amount_input"]=""
        txt,kb=discount_amount_keyboard(uid); return bot.send_message(c.message.chat.id,txt,reply_markup=kb)
    if data == "dsc_list":
        txt,rows=discount_codes_text(); kb=discount_codes_markup(rows) if rows else None
        bot.answer_callback_query(c.id); return bot.send_message(c.message.chat.id,txt,reply_markup=kb)
    if data.startswith("dsc_toggle_"):
        code=data[len("dsc_toggle_"):]
        with connect_db() as db:
            row=db.execute("SELECT active FROM discount_codes WHERE code=?",(code,)).fetchone()
            if not row: return bot.answer_callback_query(c.id,"کد پیدا نشد.",show_alert=True)
            db.execute("UPDATE discount_codes SET active=? WHERE code=?",(0 if row[0] else 1,code))
        txt,rows=discount_codes_text(); kb=discount_codes_markup(rows) if rows else None
        bot.answer_callback_query(c.id,"وضعیت کد تغییر کرد.")
        return bot.edit_message_text(txt,c.message.chat.id,c.message.message_id,reply_markup=kb)
    if data == "dsc_delete":
        steps[uid]={"action":"discount_delete_select","selected":[]}; bot.answer_callback_query(c.id)
        return show_discount_delete_list(c,uid)
    if data.startswith("dsc_del_toggle_"):
        st=steps.get(uid,{})
        if st.get("action")!="discount_delete_select": return bot.answer_callback_query(c.id,"صفحه منقضی شده است.",show_alert=True)
        code=data[len("dsc_del_toggle_"):]; selected=st.setdefault("selected",[])
        selected.remove(code) if code in selected else selected.append(code)
        bot.answer_callback_query(c.id); return show_discount_delete_list(c,uid,edit=True)
    if data == "dsc_del_confirm":
        st=steps.get(uid,{})
        if st.get("action")!="discount_delete_select" or not st.get("selected"): return bot.answer_callback_query(c.id,"حداقل یک کد را انتخاب کن.",show_alert=True)
        with connect_db() as db:
            for code in st["selected"]:
                db.execute("DELETE FROM discount_uses WHERE code=?",(code,)); db.execute("DELETE FROM discount_codes WHERE code=?",(code,))
        selected=st["selected"][:]; steps.pop(uid,None); bot.answer_callback_query(c.id,"حذف شد.")
        return bot.edit_message_text("✅ کدهای حذف‌شده:\n"+"\n".join(selected),c.message.chat.id,c.message.message_id,reply_markup=None)
    if data == "dsc_create_confirm":
        st=steps.get(uid,{})
        if st.get("action")!="discount_create_confirm": return bot.answer_callback_query(c.id,"مرحله منقضی شده است.",show_alert=True)
        code=st["discount_code"]; expiry=int(time.time())+st["discount_duration_seconds"] if st.get("discount_type")=="timed" else None
        limit=int(st.get("discount_count",0) or 0)
        try:
            with connect_db() as db: db.execute("INSERT INTO discount_codes(code,discount_amount,active,max_uses,used_count,expires_at) VALUES(?,?,1,?,0,?)",(code,st["discount_amount"],limit,expiry))
        except sqlite3.IntegrityError: return bot.answer_callback_query(c.id,"این کد قبلاً ساخته شده است.",show_alert=True)
        kind={"timed":"زمان‌دار","count":"تعداددار","permanent":"تمام‌وقت"}[st["discount_type"]]
        summary=f"✅ کد تخفیف ساخته شد.\nنوع: {kind}\nکد: <code>{escape(code)}</code>\nمقدار تخفیف: {st['discount_amount']:,} تومان\nتعداد مجاز: {limit:,}" if limit else f"✅ کد تخفیف ساخته شد.\nنوع: {kind}\nکد: <code>{escape(code)}</code>\nمقدار تخفیف: {st['discount_amount']:,} تومان\nتعداد مجاز: نامحدود"
        if expiry: summary += "\nمدت اعتبار: "+escape(st.get("discount_duration_label",""))
        elif not limit: summary += "\nاعتبار تا غیرفعال‌سازی مدیر"
        steps.pop(uid,None); bot.answer_callback_query(c.id,"کد ساخته شد.")
        return bot.edit_message_text(summary,c.message.chat.id,c.message.message_id,reply_markup=None)
    if data == "dsc_cancel":
        steps.pop(uid,None); bot.answer_callback_query(c.id,"لغو شد.")
        return bot.edit_message_text("❌ عملیات لغو شد.",c.message.chat.id,c.message.message_id,reply_markup=None)
    if data in ("dsc_change_count","dsc_change_time"):
        st=steps.get(uid,{})
        if data=="dsc_change_count": st["action"]="discount_count"; prompt="تعداد جدید دفعات استفاده را وارد کن:"
        else: st["action"]="discount_duration"; prompt="مدت اعتبار جدید را وارد کن:"
        bot.answer_callback_query(c.id); return bot.send_message(c.message.chat.id,prompt)
    if data.startswith("dscpad_"):
        st=steps.get(uid,{})
        if st.get("action")!="discount_amount_pad": return bot.answer_callback_query(c.id,"مرحله منقضی شده است.",show_alert=True)
        key=data[len("dscpad_"):]; raw=st.get("discount_amount_input","")
        if key=="clear": raw=""
        elif key=="back": raw=raw[:-1]
        elif key=="confirm":
            amount=int(raw or 0)
            if amount<1: return bot.answer_callback_query(c.id,"مبلغ تخفیف باید بیشتر از صفر باشد.",show_alert=True)
            st["discount_amount"]=amount; st["action"]="discount_code_input"; bot.answer_callback_query(c.id)
            return bot.send_message(c.message.chat.id,"کد تخفیف دلخواه را وارد کن (۲ تا ۳۲ نویسه انگلیسی/عدد):")
        elif key.isdigit() and len(raw)<10: raw+=key
        st["discount_amount_input"]=raw; txt,kb=discount_amount_keyboard(uid); bot.answer_callback_query(c.id)
        return bot.edit_message_text(txt,c.message.chat.id,c.message.message_id,reply_markup=kb)


@bot.message_handler(func=lambda m: m.from_user is not None and m.from_user.id in steps and steps[m.from_user.id].get("action") in ("discount_count","discount_duration","discount_code_input"))
def discount_setup_message(message):
    uid=message.from_user.id; st=steps.get(uid,{})
    if not is_manager(uid): steps.pop(uid,None); return
    text=(message.text or "").strip(); action=st.get("action")
    if action=="discount_count":
        if not text.isdigit() or not (1<=int(text)<=MAX_AMOUNT): return bot.reply_to(message,"تعداد معتبر و مثبت وارد کن.")
        st["discount_count"]=int(text); st["action"]="discount_amount_pad"; st["discount_amount_input"]=""
        txt,kb=discount_amount_keyboard(uid); return bot.send_message(message.chat.id,txt,reply_markup=kb)
    if action=="discount_duration":
        seconds=parse_discount_duration(text)
        if not seconds: return bot.reply_to(message,"زمان معتبر نیست؛ نمونه: 30 دقیقه، 3 ساعت، 1 روز یا 2 هفته.")
        st["discount_duration_seconds"]=seconds; st["discount_duration_label"]=text; st["action"]="discount_amount_pad"; st["discount_amount_input"]=""
        txt,kb=discount_amount_keyboard(uid); return bot.send_message(message.chat.id,txt,reply_markup=kb)
    code=text.upper()
    if not re.fullmatch(r"[A-Z0-9_-]{2,32}",code): return bot.reply_to(message,"کد باید ۲ تا ۳۲ نویسه و فقط شامل حروف انگلیسی، عدد، خط تیره یا زیرخط باشد.")
    with connect_db() as db: exists=db.execute("SELECT 1 FROM discount_codes WHERE code=?",(code,)).fetchone()
    if exists: return bot.reply_to(message,"این کد قبلاً ساخته شده؛ کد دیگری وارد کن.")
    st["discount_code"]=code; st["action"]="discount_create_confirm"
    kind={"timed":"زمان‌دار","count":"تعداددار","permanent":"تمام‌وقت"}[st["discount_type"]]
    detail=(f"مدت اعتبار: {escape(st['discount_duration_label'])}" if st["discount_type"]=="timed" else f"تعداد دفعات مجاز: {st['discount_count']:,} (بدون محدودیت زمانی)" if st["discount_type"]=="count" else "بدون محدودیت زمان و تعداد؛ تا غیرفعال‌سازی مدیر")
    kb=types.InlineKeyboardMarkup(); kb.row(types.InlineKeyboardButton("✅ تأیید و ساخت",callback_data="dsc_create_confirm"),types.InlineKeyboardButton("❌ لغو",callback_data="dsc_cancel"))
    return bot.reply_to(message,f"بررسی کد تخفیف:\nنوع: {kind}\n{detail}\nمقدار تخفیف: {st['discount_amount']:,} تومان\nکد: <code>{escape(code)}</code>\n\nبرای ساخت تأیید کن.",reply_markup=kb)


def get_stats():
    with connect_db() as db:
        total = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        active = db.execute("SELECT COUNT(*) FROM users WHERE blocked=0").fetchone()[0]
        balance = db.execute("SELECT COALESCE(SUM(balance),0) FROM users").fetchone()[0]
        debt = db.execute("SELECT COALESCE(SUM(debt),0) FROM users").fetchone()[0]
        pending = db.execute("SELECT COUNT(*) FROM requests WHERE status IN ('pending','reviewing')").fetchone()[0]
    return ("📊 <b>آمار ربات ZEX</b>\n\n"
            f"👥 کل کاربران: <b>{total:,}</b>\n✅ کاربران فعال: <b>{active:,}</b>\n"
            f"💰 مجموع موجودی: <b>{balance:,} ZEX</b>\n📌 مجموع بدهی: <b>{debt:,} ZEX</b>\n"
            f"⏳ درخواست‌های در انتظار: <b>{pending:,}</b>")


@bot.message_handler(func=lambda m: m.text == "⚙️ پنل مدیریت")
def admin_panel(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "❌ این بخش مخصوص مدیران است.")
        return
    markup = admin_menu(message.from_user.id)
    if is_manager(message.from_user.id):
        markup.row(types.InlineKeyboardButton("💎 افزایش/کاهش ZEX خودم", callback_data="admin_self_zex"))
    bot.send_message(message.chat.id, "⚙️ <b>پنل مدیریت ZEX</b>", reply_markup=markup)


def manager_notifications_enabled():
    with connect_db() as db:
        row = db.execute("SELECT value FROM manager_settings WHERE key='join_notifications'").fetchone()
    return bool(row and row[0] == '1')


def send_member_digest(chat_id, sort="latest", offset=0, message_id=None):
    sort_sql = {
        "alpha": "LOWER(COALESCE(NULLIF(username,''), first_name, '')) ASC, user_id ASC",
        "latest": "joined_at DESC, user_id DESC",
        "balance": "balance DESC, joined_at DESC"
    }.get(sort, "joined_at DESC, user_id DESC")
    with connect_db() as db:
        rows = db.execute(f"""
            SELECT user_id, username, first_name, balance, role, joined_at, blocked, game_points
            FROM users ORDER BY {sort_sql} LIMIT 25 OFFSET ?
        """, (offset,)).fetchall()
        total = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("🔤 بر اساس نام", callback_data="admin_members_sort_alpha"),
        types.InlineKeyboardButton("🕒 زمان عضویت", callback_data="admin_members_sort_latest")
    )
    kb.row(types.InlineKeyboardButton("💰 بیشترین موجودی", callback_data="admin_members_sort_balance"))
    if not rows:
        text = "📭 عضوی برای نمایش وجود ندارد."
        if message_id:
            try: bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)
            except Exception: pass
        else:
            bot.send_message(chat_id, text, reply_markup=kb)
        return
    sort_titles = {"alpha": "ترتیب الفبایی نام کاربری", "latest": "آخرین اعضای ثبت‌نام‌شده", "balance": "بیشترین موجودی"}
    lines = [f"👥 <b>اعضای ربات</b> — {sort_titles.get(sort, 'اعضا')}\n📄 {offset+1} تا {offset+len(rows)} از {total}"]
    for uid, username, first, bal, role, joined, blocked, points in rows:
        uname = '@' + username if username else 'بدون یوزرنیم'
        name = escape(first or 'بدون نام')
        date = format_iran_datetime(joined) if str(joined).isdigit() else str(joined)
        lines.append(f"👤 <b>{name}</b> | {escape(uname)}\n🆔 <code>{uid}</code>\n💰 موجودی: <b>{bal:,} ZEX</b>\n📅 عضویت: {escape(date)}")
    if offset > 0:
        kb.add(types.InlineKeyboardButton("⬅️ قبلی", callback_data=f"admin_members_{sort}_{max(0, offset-25)}"))
    if offset + len(rows) < total:
        kb.add(types.InlineKeyboardButton("بعدی ➡️", callback_data=f"admin_members_{sort}_{offset+25}"))
    text = "\n\n".join(lines)[:4000]
    if message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)
        except Exception as error:
            # اگر متن تغییری نکرده باشد تلگرام خطا می‌دهد؛ همان پیام حفظ می‌شود.
            if "message is not modified" not in str(error).lower():
                try: bot.send_message(chat_id, text, reply_markup=kb)
                except Exception: pass
    else:
        bot.send_message(chat_id, text, reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_members_"))
def admin_members_page(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "دسترسی نداری.", show_alert=True)
        return
    parts = call.data.split("_")
    if len(parts) == 4 and parts[2] == "sort":
        sort, offset = parts[3], 0
    elif len(parts) == 4:
        sort = parts[2] if parts[2] in ("alpha", "latest", "balance") else "latest"
        try: offset = max(0, int(parts[3]))
        except ValueError: offset = 0
    else:
        sort, offset = "latest", 0
    send_member_digest(call.message.chat.id, sort, offset, call.message.message_id)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_"))
def admin_callback(call):
    admin_id = call.from_user.id
    if not is_admin(admin_id):
        bot.answer_callback_query(call.id, "اجازه دسترسی نداری.", show_alert=True)
        return
    task = call.data.removeprefix("admin_")
    if task == "referral_reward":
        if not is_manager(admin_id):
            bot.answer_callback_query(call.id, "فقط مدیر اصلی.", show_alert=True); return
        steps[admin_id] = {"action": "manager_referral_reward"}
        bot.send_message(call.message.chat.id, f"مبلغ جدید پاداش هر زیرمجموعه موفق را به ZEX بفرست.\nمبلغ فعلی: {get_referral_reward():,} ZEX\nبرای لغو بنویس: لغو")
        bot.answer_callback_query(call.id)
        return
    if task == "balance_scale":
        if not is_manager(admin_id):
            bot.answer_callback_query(call.id, "فقط مدیر اصلی.", show_alert=True); return
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.row(types.InlineKeyboardButton("✖️ دو برابر کردن", callback_data="admin_scale_double"),
               types.InlineKeyboardButton("➗ نصف کردن", callback_data="admin_scale_half"))
        bot.send_message(call.message.chat.id, "نوع تغییر موجودی را انتخاب کن:", reply_markup=kb)
        bot.answer_callback_query(call.id)
        return
    if task in ("scale_double", "scale_half"):
        if not is_manager(admin_id):
            bot.answer_callback_query(call.id, "فقط مدیر اصلی.", show_alert=True); return
        factor = 2 if task == "scale_double" else 0.5
        steps[admin_id] = {"action": "manager_scale_audience", "factor": factor}
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.row(types.InlineKeyboardButton("👥 همه کاربران", callback_data="admin_scale_all"),
               types.InlineKeyboardButton("👤 یک نفر", callback_data="admin_scale_one"))
        bot.send_message(call.message.chat.id, ("دو برابر کردن" if factor == 2 else "نصف کردن") + " موجودی برای چه کسانی انجام شود؟", reply_markup=kb)
        bot.answer_callback_query(call.id)
        return
    if task in ("scale_all", "scale_one"):
        if not is_manager(admin_id):
            bot.answer_callback_query(call.id, "فقط مدیر اصلی.", show_alert=True); return
        state = steps.get(admin_id, {})
        if state.get("action") != "manager_scale_audience":
            bot.answer_callback_query(call.id, "این مرحله منقضی شده است.", show_alert=True); return
        if task == "scale_all":
            factor = state["factor"]
            with connect_db() as db:
                ids = [r[0] for r in db.execute("SELECT user_id FROM users WHERE user_id<>? ORDER BY user_id", (MANAGER_ID,)).fetchall()]
            changed = failed = 0
            for uid in ids:
                u = get_user(uid)
                if not u: continue
                old = int(u[3] or 0)
                new = int(old * factor)
                if new == old: continue
                if change_balance(uid, new-old, "دو برابر شدن موجودی توسط مدیر" if factor == 2 else "نصف شدن موجودی توسط مدیر"):
                    changed += 1
                else: failed += 1
            steps.pop(admin_id, None)
            bot.send_message(call.message.chat.id, f"✅ عملیات برای همه کاربران انجام شد.\n👥 موفق: {changed}\n⚠️ بدون تغییر (مثلاً موجودی قفل‌شده): {failed}", reply_markup=admin_menu(admin_id))
        else:
            state["action"] = "manager_scale_one_target"
            bot.send_message(call.message.chat.id, "آیدی عددی یا نام کاربری کاربر را بفرست؛ مثال: 123456789 یا @username\nبرای لغو بنویس: لغو")
        bot.answer_callback_query(call.id)
        return
    if task == "admin_list":
        if not is_manager(admin_id):
            bot.answer_callback_query(call.id, "فقط مدیر اصلی دسترسی دارد.", show_alert=True)
            return
        with connect_db() as db:
            rows = db.execute("SELECT user_id, username, first_name, admin_since FROM users WHERE role='admin' ORDER BY admin_since DESC, user_id ASC").fetchall()
        lines = ["👥 <b>فهرست ادمین‌ها</b>", ""]
        if not rows:
            lines.append("ادمینی ثبت نشده است.")
        for uid, username, first_name, since in rows:
            display = escape(first_name or "بدون نام")
            uname = "@" + escape(username) if username else "یوزرنیم ندارد"
            when = format_iran_datetime(since) if since else "زمان ثبت در نسخه‌های قبلی موجود نیست"
            lines.append(f"👤 <b>{display}</b> | {uname}\n🆔 <code>{uid}</code>\n📅 ادمین‌شدن: {escape(when)}")
        bot.send_message(call.message.chat.id, "\n\n".join(lines)[:4000])
        bot.answer_callback_query(call.id)
        return
    if task == "join_bonus":
        if not is_manager(admin_id):
            bot.answer_callback_query(call.id, "فقط مدیر اصلی.", show_alert=True)
            return
        send_join_bonus_status(call.message.chat.id)
        bot.answer_callback_query(call.id)
        return
    if task == "join_bonus_new":
        if not is_manager(admin_id):
            bot.answer_callback_query(call.id, "فقط مدیر اصلی.", show_alert=True)
            return
        keyboard = types.InlineKeyboardMarkup(row_width=3)
        for hours in (1, 2, 3, 4, 5, 6, 24):
            label = "۲۴ ساعت" if hours == 24 else f"{hours} ساعت"
            keyboard.add(types.InlineKeyboardButton(label, callback_data=f"admin_join_bonus_duration_{hours}"))
        keyboard.row(types.InlineKeyboardButton("❌ لغو", callback_data="admin_join_bonus_setup_cancel"))
        bot.send_message(call.message.chat.id, "⏳ مدت اعتبار پاداش عضویت را انتخاب کن:", reply_markup=keyboard)
        bot.answer_callback_query(call.id)
        return
    if task.startswith("join_bonus_duration_"):
        if not is_manager(admin_id):
            bot.answer_callback_query(call.id, "فقط مدیر اصلی.", show_alert=True)
            return
        try:
            hours = int(task.rsplit("_", 1)[1])
        except ValueError:
            bot.answer_callback_query(call.id, "مدت نامعتبر است.", show_alert=True)
            return
        if hours not in (1, 2, 3, 4, 5, 6, 24):
            bot.answer_callback_query(call.id, "مدت نامعتبر است.", show_alert=True)
            return
        steps[admin_id] = {"action": "join_bonus_amount", "hours": hours}
        bot.send_message(call.message.chat.id, f"💎 مقدار پاداش هر عضو جدید را به ZEX بفرست؛ مثلاً 200\n⏳ مدت انتخاب‌شده: {hours} ساعت\nبرای لغو بنویس: لغو")
        bot.answer_callback_query(call.id)
        return
    if task == "join_bonus_no_reason":
        if not is_manager(admin_id):
            bot.answer_callback_query(call.id, "فقط مدیر اصلی.", show_alert=True)
            return
        state = steps.get(admin_id, {})
        if state.get("action") != "join_bonus_reason":
            bot.answer_callback_query(call.id, "این مرحله منقضی شده است.", show_alert=True)
            return
        amount, hours = state["amount"], state["hours"]
        steps.pop(admin_id, None)
        activate_join_bonus(admin_id, call.message.chat.id, amount, hours, "")
        bot.answer_callback_query(call.id, "طرح فعال شد.")
        return
    if task in ("join_bonus_cancel", "join_bonus_setup_cancel"):
        if not is_manager(admin_id):
            bot.answer_callback_query(call.id, "فقط مدیر اصلی.", show_alert=True)
            return
        if task == "join_bonus_cancel":
            with connect_db() as db:
                cur = db.execute("UPDATE join_bonus_campaigns SET status='cancelled' WHERE status='active' AND ends_at>?", (int(time.time()),))
            msg = "⛔ طرح فعال لغو شد." if cur.rowcount else "طرح فعالی برای لغو وجود نداشت."
        else:
            steps.pop(admin_id, None)
            msg = "❌ ساخت طرح لغو شد."
        bot.answer_callback_query(call.id, msg, show_alert=True)
        bot.send_message(call.message.chat.id, msg, reply_markup=admin_menu(admin_id))
        return
    if task == "broadcast":
        keyboard = types.InlineKeyboardMarkup()
        keyboard.row(types.InlineKeyboardButton("👥 پیام به همه اعضا", callback_data="broadcast_all"))
        keyboard.row(types.InlineKeyboardButton("👤 پیام به یک نفر", callback_data="broadcast_one"))
        bot.send_message(call.message.chat.id, "✉️ پیام را برای چه کسانی می‌خواهی بفرستی؟", reply_markup=keyboard)
        bot.answer_callback_query(call.id)
        return
    if task == "warnings":
        with connect_db() as db:
            rows = db.execute("SELECT id,user_id,reason,details,created_at,status FROM warning_history ORDER BY id DESC LIMIT 20").fetchall()
        if not rows:
            bot.send_message(call.message.chat.id, "🚨 هنوز هشداری ثبت نشده است.")
        else:
            for wid, uid, reason, details, created, status in rows:
                kb = types.InlineKeyboardMarkup()
                if status == 'open':
                    kb.row(types.InlineKeyboardButton("🙋 من بودم", callback_data=f"warning_handle_mine_{wid}"))
                    kb.row(types.InlineKeyboardButton("❌ لغو هشدار", callback_data=f"warning_handle_cancel_{wid}"),
                           types.InlineKeyboardButton("➖ کاهش موجودی", callback_data=f"warning_handle_reduce_{wid}"))
                    kb.row(types.InlineKeyboardButton("🗑 حذف کاربر", callback_data=f"warning_handle_delete_{wid}"))
                body = (f"🚨 <b>هشدار #{wid}</b>\n👤 کاربر: <code>{uid}</code>\n"
                        f"🕒 زمان: {escape(format_iran_datetime(created))}\n🔢 هشدار: {escape(str(reason))}\n"
                        f"📋 جزئیات: {escape(str(details))}\n📌 وضعیت: {escape(str(status))}")
                bot.send_message(call.message.chat.id, body, reply_markup=kb)
        bot.answer_callback_query(call.id)
        return
    if task in ("remove_from_bot", "remove_from_group"):
        if not is_manager(admin_id):
            bot.answer_callback_query(call.id, "فقط مدیر اصلی.", show_alert=True); return
        steps[admin_id] = {"action": "manager_remove_user", "mode": task}
        prompt = "آیدی عددی کاربر را بفرست تا از ربات مسدود شود؛ موجودی و سابقه‌اش باقی می‌ماند." if task == "remove_from_bot" else "آیدی عددی کاربر را بفرست تا ربات تلاش کند او را از گروه‌های شناخته‌شده حذف کند؛ موجودی‌اش باقی می‌ماند."
        bot.send_message(call.message.chat.id, prompt)
        bot.answer_callback_query(call.id)
        return
    if task == "members":
        send_member_digest(call.message.chat.id)
        bot.answer_callback_query(call.id)
        return
    if task == "notifications":
        if not is_manager(admin_id):
            bot.answer_callback_query(call.id, "فقط مدیر اصلی.", show_alert=True); return
        enabled = manager_notifications_enabled()
        new_value = '0' if enabled else '1'
        with connect_db() as db:
            db.execute("INSERT INTO manager_settings(key,value) VALUES('join_notifications',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (new_value,))
        bot.answer_callback_query(call.id, "اعلان‌ها روشن شد" if new_value == '1' else "اعلان‌ها خاموش شد")
        bot.send_message(call.message.chat.id, "🔔 اعلان ورود کاربران: " + ("🟢 فعال" if new_value == '1' else "🔴 غیرفعال"))
        return
    if call.data.startswith("admin_self_action_"):
        if not is_manager(admin_id):
            bot.answer_callback_query(call.id, "فقط مدیر اصلی.", show_alert=True)
            return
        parts = call.data.split("_")
        action = "افزایش" if parts[3] == "add" else "کاهش"
        asset = parts[4]
        steps[admin_id] = {"action": "manager_self_amount", "asset": asset, "operation": action}
        bot.send_message(call.message.chat.id, f"مقدار {asset} برای {action} به خودت را بفرست:")
        bot.answer_callback_query(call.id)
        return
    if task == "stats":
        bot.send_message(call.message.chat.id, get_stats())
        bot.answer_callback_query(call.id)
        return
    if task in ("self_zex", "self_points"):
        if not is_manager(admin_id):
            bot.answer_callback_query(call.id, "فقط مدیر اصلی.", show_alert=True)
            return
        asset = "ZEX" if task == "self_zex" else "ZEX"
        steps[admin_id] = {"action": "manager_self_adjust", "asset": asset}
        keyboard = types.InlineKeyboardMarkup()
        keyboard.row(types.InlineKeyboardButton("➕ افزایش", callback_data=f"admin_self_action_add_{asset}"),
                     types.InlineKeyboardButton("➖ کاهش", callback_data=f"admin_self_action_remove_{asset}"))
        bot.send_message(call.message.chat.id, f"برای خودت تغییر {asset} را انتخاب کن:", reply_markup=keyboard)
        bot.answer_callback_query(call.id)
        return
    if not is_manager(admin_id):
        bot.answer_callback_query(call.id, "این عملیات فقط برای مدیر اصلی است.", show_alert=True)
        return
    if task == "discounts":
        bot.send_message(call.message.chat.id, "🏷 <b>مدیریت کدهای تخفیف</b>", reply_markup=discount_admin_keyboard())
        bot.answer_callback_query(call.id)
        return
    if task not in ("add", "remove", "block", "unblock", "make_admin", "remove_admin"):
        bot.answer_callback_query(call.id, "عملیات نامعتبر است.")
        return
    steps[admin_id] = {"action": "admin_target", "task": task}
    bot.send_message(call.message.chat.id, "🆔 آیدی عددی کاربر موردنظر را بفرست.")
    bot.answer_callback_query(call.id)


@bot.message_handler(func=lambda m: m.from_user is not None and m.from_user.id in steps and steps[m.from_user.id].get("action") in ("manager_referral_reward", "manager_scale_one_target"))
def manager_new_controls_step(message):
    admin_id = message.from_user.id
    if not is_manager(admin_id):
        steps.pop(admin_id, None)
        return
    state = steps.get(admin_id, {})
    raw = (message.text or "").strip()
    if raw == "لغو":
        steps.pop(admin_id, None)
        bot.send_message(message.chat.id, "عملیات لغو شد.", reply_markup=admin_menu(admin_id))
        return
    if state.get("action") == "manager_referral_reward":
        if not raw.isdigit() or not (0 <= int(raw) <= MAX_AMOUNT):
            bot.reply_to(message, "❌ مبلغ باید عدد صحیح بین صفر تا سقف مجاز باشد.")
            return
        amount = int(raw)
        with connect_db() as db:
            db.execute("INSERT INTO manager_settings(key,value) VALUES('referral_reward',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(amount),))
        steps.pop(admin_id, None)
        bot.send_message(message.chat.id, f"✅ پاداش هر زیرمجموعه موفق به {amount:,} ZEX تغییر کرد. از این پس برای دعوت‌های جدید اعمال می‌شود.", reply_markup=admin_menu(admin_id))
        return
    # انتخاب یک کاربر با شناسه عددی یا نام کاربری
    target = None
    if raw.lstrip("@").isdigit():
        target = get_user(int(raw.lstrip("@")))
    else:
        target = find_username(raw)
    if not target:
        bot.reply_to(message, "❌ کاربر در ربات پیدا نشد. آیدی عددی یا @نام‌کاربری را بررسی کن.")
        return
    uid = int(target[0])
    if uid == MANAGER_ID:
        bot.reply_to(message, "❌ برای جلوگیری از تغییر ناخواسته، موجودی مدیر اصلی با این ابزار تغییر نمی‌کند.")
        return
    old = int(target[3] or 0)
    new = int(old * state.get("factor", 1))
    delta = new - old
    if delta == 0:
        steps.pop(admin_id, None)
        bot.send_message(message.chat.id, f"موجودی کاربر <code>{uid}</code> تغییری نکرد؛ مقدار فعلی {old:,} ZEX است.", reply_markup=admin_menu(admin_id))
        return
    factor_label = "دو برابر" if state.get("factor") == 2 else "نصف"
    if not change_balance(uid, delta, f"{factor_label} شدن موجودی توسط مدیر"):
        bot.reply_to(message, "❌ تغییر انجام نشد؛ ممکن است موجودی کاربر در حال بررسی فروش قفل شده باشد.")
        return
    steps.pop(admin_id, None)
    bot.send_message(message.chat.id, f"✅ موجودی کاربر <code>{uid}</code> {factor_label} شد.\nموجودی قبلی: {old:,} ZEX\nموجودی جدید: {new:,} ZEX", reply_markup=admin_menu(admin_id))
    try:
        bot.send_message(uid, f"⚖️ موجودی ZEX شما توسط مدیر تغییر کرد.\nموجودی جدید: <b>{new:,} ZEX</b>")
    except Exception:
        pass


@bot.message_handler(func=lambda m: m.from_user is not None and m.from_user.id in steps and steps[m.from_user.id].get("action") in ("join_bonus_amount", "join_bonus_reason"))
def join_bonus_setup_step(message):
    admin_id = message.from_user.id
    if admin_id != MANAGER_ID:
        steps.pop(admin_id, None)
        bot.send_message(message.chat.id, "⛔ فقط مدیر اصلی اجازه ساخت طرح پاداش را دارد.")
        return
    state = steps.get(admin_id, {})
    text = (message.text or "").strip()
    if state.get("action") == "join_bonus_amount":
        if not text.isdigit() or not (1 <= int(text) <= MAX_AMOUNT):
            bot.reply_to(message, "❌ مقدار باید عددی مثبت و در محدوده مجاز باشد. دوباره بفرست یا «لغو» بنویس.")
            return
        state["amount"] = int(text)
        state["action"] = "join_bonus_reason"
        keyboard = types.InlineKeyboardMarkup()
        keyboard.row(types.InlineKeyboardButton("بدون دلیل", callback_data="admin_join_bonus_no_reason"),
                     types.InlineKeyboardButton("❌ لغو", callback_data="admin_join_bonus_setup_cancel"))
        bot.send_message(
            message.chat.id,
            f"📝 دلیل این پاداش را بفرست.\n\n💎 مقدار: <b>{state['amount']:,} ZEX</b>\n⏳ مدت: <b>{state['hours']} ساعت</b>\n\nاگر نمی‌خواهی دلیلی نمایش داده شود، «بدون دلیل» را بزن؛ برای لغو هم «لغو» را بزن.",
            reply_markup=keyboard
        )
        return
    if not text:
        bot.reply_to(message, "❌ دلیل را به‌صورت متن بفرست یا گزینه «بدون دلیل» را انتخاب کن.")
        return
    if len(text) > 500:
        bot.reply_to(message, "❌ دلیل حداکثر می‌تواند ۵۰۰ نویسه باشد.")
        return
    amount, hours = state["amount"], state["hours"]
    steps.pop(admin_id, None)
    activate_join_bonus(admin_id, message.chat.id, amount, hours, text)


@bot.message_handler(func=lambda m: m.from_user is not None and m.from_user.id in steps and steps[m.from_user.id].get("action") == "manager_self_amount")
def manager_self_amount_step(message):
    user_id = message.from_user.id
    if user_id != MANAGER_ID:
        steps.pop(user_id, None)
        return
    state = steps.get(user_id, {})
    try:
        amount = int((message.text or "").strip())
    except ValueError:
        bot.reply_to(message, "فقط عدد بفرست؛ برای خروج بنویس لغو.")
        return
    if amount <= 0 or amount > MAX_AMOUNT:
        bot.reply_to(message, "مقدار باید بیشتر از صفر و در محدودهٔ مجاز باشد.")
        return
    asset, action = state.get("asset", "ZEX"), state.get("operation", "افزایش")
    if asset == "ZEX":
        current = get_game_points(user_id)
        if action == "کاهش" and current < amount:
            bot.reply_to(message, f"❌ ZEX کافی نیست؛ موجودی فعلی: {fmt_points(current)}")
            return
        delta = amount if action == "افزایش" else -amount
        with connect_db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (delta, user_id))
            add_transaction(db, user_id, delta, f"{action} ZEX توسط مدیر از پنل", "admin")
            db.commit()
        current = get_game_points(user_id)
        shown = fmt_points(current)
    else:
        if not change_balance(user_id, amount if action == "افزایش" else -amount, f"{action} ZEX توسط مدیر از پنل"):
            bot.reply_to(message, f"❌ ZEX کافی نیست؛ موجودی فعلی: {get_user(user_id)[3]:,}")
            return
        shown = f"{get_user(user_id)[3]:,}"
    steps.pop(user_id, None)
    bot.send_message(message.chat.id, f"✅ {action} {amount:,} {asset} انجام شد.\nموجودی جدید: <b>{shown}</b>", reply_markup=admin_menu(user_id))


# ساخت کد تخفیف از داخل پنل: اول مبلغ، بعد خود کد.
@bot.message_handler(func=lambda m: m.from_user is not None and m.from_user.id in steps and steps[m.from_user.id].get("action") in ("discount_amount", "discount_code"))
def discount_creation_step(message):
    admin_id = message.from_user.id
    chat_id = message.chat.id
    if admin_id != MANAGER_ID:
        steps.pop(admin_id, None)
        bot.send_message(chat_id, "⛔ فقط مدیر اصلی اجازه ساخت کد تخفیف دارد.")
        return
    text = (message.text or "").strip()
    step = steps.get(admin_id, {})
    if step.get("action") == "discount_amount":
        if not text.isdigit() or not (1 <= int(text) <= 1_000_000_000):
            bot.send_message(chat_id, "❌ مبلغ باید عددی مثبت باشد. دوباره مقدار تخفیف را به تومان بفرست.")
            return
        step["discount_amount"] = int(text)
        step["action"] = "discount_code"
        bot.send_message(chat_id, "🔤 حالا کد تخفیف را بفرست؛ فقط حروف انگلیسی، عدد، خط تیره یا زیرخط (۲ تا ۳۲ نویسه).")
        return
    if not re.fullmatch(r"[A-Za-z0-9_-]{2,32}", text):
        bot.send_message(chat_id, "❌ قالب کد مناسب نیست. فقط حروف انگلیسی، عدد، خط تیره یا زیرخط بفرست.")
        return
    code = text.upper()
    discount = int(step["discount_amount"])
    with connect_db() as db:
        db.execute("INSERT INTO discount_codes(code, discount_amount, active) VALUES (?, ?, 1) ON CONFLICT(code) DO UPDATE SET discount_amount=excluded.discount_amount, active=1", (code, discount))
    steps.pop(admin_id, None)
    bot.send_message(chat_id, f"✅ کد تخفیف <code>{escape(code)}</code> فعال شد.\n💸 مقدار تخفیف: <b>{discount:,} تومان</b>")


@bot.callback_query_handler(func=lambda call: call.data.startswith("discount_del_"))
def discount_delete_callback(call):
    if call.from_user.id != MANAGER_ID:
        bot.answer_callback_query(call.id, "فقط مدیر اصلی می‌تواند کد حذف کند.", show_alert=True)
        return
    code = call.data[len("discount_del_"):].upper()
    with connect_db() as db:
        cur = db.execute("UPDATE discount_codes SET active=0 WHERE code=? AND active=1", (code,))
    if cur.rowcount:
        bot.answer_callback_query(call.id, "کد تخفیف حذف شد.", show_alert=True)
        try:
            bot.edit_message_text(f"✅ کد تخفیف <code>{escape(code)}</code> حذف شد.", call.message.chat.id, call.message.message_id)
        except Exception:
            bot.send_message(call.message.chat.id, f"✅ کد تخفیف <code>{escape(code)}</code> حذف شد.")
    else:
        bot.answer_callback_query(call.id, "این کد دیگر فعال نیست.", show_alert=True)


def _broadcast_profile_text(user_row):
    uid, username, first_name, balance, role, joined_at, blocked, debt = user_row
    uname = '@' + username if username else 'ندارد'
    joined = format_iran_datetime(joined_at) if str(joined_at).isdigit() else str(joined_at)
    return ("👤 <b>مشخصات گیرنده</b>\n"
            f"📝 نام: {escape(first_name or 'بدون نام')}\n"
            f"🔹 نام کاربری: {escape(uname)}\n"
            f"🆔 آیدی عددی: <code>{uid}</code>\n"
            f"💰 موجودی: <b>{balance:,} ZEX</b>\n"
            f"📅 زمان عضویت: {escape(joined)}\n"
            f"📌 وضعیت: {'مسدود' if blocked else 'فعال'}")


def _broadcast_user_rows():
    with connect_db() as db:
        return db.execute("SELECT user_id, username, first_name, balance, role, joined_at, blocked, debt FROM users ORDER BY LOWER(COALESCE(NULLIF(username,''), first_name, '')), user_id").fetchall()


def _saved_admin_message_rows(admin_id):
    with connect_db() as db:
        return db.execute(
            "SELECT id, chat_id, message_id, content_type, created_at "
            "FROM admin_message_archive WHERE chat_id=? ORDER BY id DESC",
            (admin_id,)
        ).fetchall()


def _show_saved_broadcast_preview(chat_id, admin_id):
    state = steps.get(admin_id, {})
    rows = _saved_admin_message_rows(admin_id)
    if not rows:
        bot.send_message(chat_id, "📭 هنوز پیامی از طرف مدیر ذخیره نشده است. اول پیام‌هایت را در گفت‌وگوی خصوصی ربات بفرست؛ سپس از این بخش انتخابشان کن.")
        steps.pop(admin_id, None)
        return

    index = max(0, min(int(state.get("saved_index", 0)), len(rows) - 1))
    state["saved_index"] = index
    row = rows[index]
    state["saved_message_id"] = row[2]
    state["saved_chat_id"] = row[1]
    steps[admin_id] = state

    old_preview = state.get("preview_message_id")
    if old_preview:
        try:
            bot.delete_message(chat_id, old_preview)
        except Exception:
            pass

    if state.get("broadcast_mode") == "all":
        recipient_label = "کل اعضا"
        send_button_text = "📤 ارسال به: کل اعضا"
    else:
        target_row = state.get("target_row")
        target_name = (target_row[2] if target_row and len(target_row) > 2 else "") or "کاربر انتخاب‌شده"
        target_name = str(target_name).strip()
        # متن دکمه را کوتاه و خوانا نگه می‌داریم تا از محدودیت تلگرام عبور نکند.
        if len(target_name) > 38:
            target_name = target_name[:37] + "…"
        recipient_label = target_name
        send_button_text = f"📤 ارسال به: {target_name}"
    mode_text = recipient_label
    kb = types.InlineKeyboardMarkup(row_width=2)
    if state.get("broadcast_mode") == "all":
        kb.row(types.InlineKeyboardButton(send_button_text, callback_data="broadcast_send_all"))
    else:
        kb.row(types.InlineKeyboardButton(send_button_text, callback_data="broadcast_send_one"))
    # دکمه‌های صفحه‌بندی مثل بخش «اعضای ربات» هستند، اما فقط پیام انتخابی را عوض می‌کنند.
    nav = []
    if index > 0:
        nav.append(types.InlineKeyboardButton("⬅️ قبلی", callback_data="savedmsg_prev"))
    if index < len(rows) - 1:
        nav.append(types.InlineKeyboardButton("بعدی ➡️", callback_data="savedmsg_next"))
    if nav:
        kb.row(*nav)
    kb.row(types.InlineKeyboardButton("❌ لغو", callback_data="broadcast_cancel"))

    try:
        copied = bot.copy_message(chat_id, row[1], row[2])
        preview_id = copied.message_id
        bot.edit_message_reply_markup(chat_id, preview_id, reply_markup=kb)
    except Exception as error:
        preview = bot.send_message(
            chat_id,
            f"📨 <b>انتخاب پیام برای ارسال</b>\n🗂 شماره پیام: {index + 1} از {len(rows)}\n🎯 گیرنده: {mode_text}\n⚠️ پیش‌نمایش این نوع پیام در دسترس نیست: {escape(str(error))[:180]}",
            reply_markup=kb
        )
        preview_id = preview.message_id
    state["preview_message_id"] = preview_id
    steps[admin_id] = state


@bot.callback_query_handler(func=lambda c: c.data in (
    "broadcast_all", "broadcast_one", "broadcast_send_all", "broadcast_send_one",
    "savedmsg_next", "savedmsg_prev", "broadcast_cancel"
))
def broadcast_callback(call):
    admin_id = call.from_user.id
    if not is_admin(admin_id):
        bot.answer_callback_query(call.id, "دسترسی نداری.", show_alert=True)
        return
    data = call.data

    if data == "broadcast_cancel":
        steps.pop(admin_id, None)
        bot.answer_callback_query(call.id, "لغو شد.")
        bot.send_message(call.message.chat.id, "❌ ارسال پیام لغو شد.")
        return

    if data == "broadcast_all":
        steps[admin_id] = {"action": "broadcast_choose_message", "broadcast_mode": "all", "saved_index": 0}
        bot.answer_callback_query(call.id)
        _show_saved_broadcast_preview(call.message.chat.id, admin_id)
        return

    if data == "broadcast_one":
        steps[admin_id] = {"action": "broadcast_target", "broadcast_mode": "one"}
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, "🆔 آیدی عددی یا نام کاربری فرد را بفرست. (مثلاً 123456789 یا @username)")
        return

    state = steps.get(admin_id, {})
    if data in ("savedmsg_next", "savedmsg_prev"):
        rows = _saved_admin_message_rows(admin_id)
        if not rows:
            bot.answer_callback_query(call.id, "پیامی ذخیره نشده است.", show_alert=True)
            return
        current = int(state.get("saved_index", 0))
        state["saved_index"] = max(0, min(len(rows) - 1, current + (1 if data == "savedmsg_next" else -1)))
        steps[admin_id] = state
        bot.answer_callback_query(call.id)
        _show_saved_broadcast_preview(call.message.chat.id, admin_id)
        return

    if data == "broadcast_send_all":
        if state.get("broadcast_mode") != "all" or not state.get("saved_message_id"):
            bot.answer_callback_query(call.id, "پیامی برای ارسال انتخاب نشده است.", show_alert=True)
            return
        with connect_db() as db:
            ids = [r[0] for r in db.execute("SELECT user_id FROM users WHERE blocked=0").fetchall()]
        sent = failed = 0
        for uid in ids:
            try:
                bot.copy_message(uid, state["saved_chat_id"], state["saved_message_id"])
                sent += 1
                time.sleep(0.04)
            except Exception:
                failed += 1
        steps.pop(admin_id, None)
        bot.answer_callback_query(call.id, "ارسال تمام شد.")
        bot.send_message(call.message.chat.id, f"📨 ارسال همگانی تمام شد.\n✅ موفق: {sent}\n❌ ناموفق: {failed}")
        return

    if data == "broadcast_send_one":
        uid = state.get("target_id")
        source_chat = state.get("saved_chat_id")
        source_message = state.get("saved_message_id")
        if not uid or not source_chat or not source_message:
            bot.answer_callback_query(call.id, "گیرنده یا پیام مشخص نیست.", show_alert=True)
            return
        try:
            bot.copy_message(uid, source_chat, source_message)
            steps.pop(admin_id, None)
            bot.answer_callback_query(call.id, "پیام ارسال شد.")
            bot.send_message(call.message.chat.id, "✅ پیام با موفقیت ارسال شد.")
        except Exception as error:
            bot.answer_callback_query(call.id, "ارسال ناموفق بود.", show_alert=True)
            bot.send_message(call.message.chat.id, f"❌ ارسال پیام انجام نشد: {escape(str(error))[:300]}")


@bot.message_handler(func=lambda m: m.from_user is not None and m.from_user.id in steps and steps[m.from_user.id].get("action") == "broadcast_target")
def broadcast_message_step(message):
    admin_id = message.from_user.id
    if not is_admin(admin_id):
        steps.pop(admin_id, None)
        return
    value = (message.text or "").strip()
    if not value:
        bot.reply_to(message, "❌ لطفاً آیدی عددی یا نام کاربری را به‌صورت متن بفرست.")
        return
    with connect_db() as db:
        if value.lstrip('@').isdigit():
            row = db.execute("SELECT user_id, username, first_name, balance, role, joined_at, blocked, debt FROM users WHERE user_id=?", (int(value),)).fetchone()
        else:
            row = db.execute("SELECT user_id, username, first_name, balance, role, joined_at, blocked, debt FROM users WHERE LOWER(username)=LOWER(?)", (value.lstrip('@'),)).fetchone()
    if not row:
        bot.reply_to(message, "❌ این کاربر در ربات ثبت نشده است. آیدی یا نام کاربری را بررسی کن.")
        return
    steps[admin_id] = {
        "action": "broadcast_choose_message", "broadcast_mode": "one",
        "target_id": row[0], "target_row": row, "saved_index": 0
    }
    _show_saved_broadcast_preview(message.chat.id, admin_id)


def save_admin_incoming_messages(messages):
    """ذخیرهٔ پیام‌های ورودی مدیران از چت خصوصی خودشان، بدون محدودیت نوع پیام."""
    for message in messages or []:
        try:
            if not message.from_user or not is_admin(message.from_user.id):
                continue
            if message.chat.type != "private" or message.chat.id != message.from_user.id:
                continue
            content_type = getattr(message, "content_type", "text") or "text"
            with connect_db() as db:
                db.execute(
                    "INSERT OR IGNORE INTO admin_message_archive(chat_id,message_id,content_type,created_at) VALUES(?,?,?,?)",
                    (message.chat.id, message.message_id, content_type, int(message.date or time.time()))
                )
        except Exception as error:
            print(f"ذخیره پیام ادمین ناموفق بود: {error}")


@bot.callback_query_handler(func=lambda c: c.data.startswith("warning_handle_"))
def warning_handle_callback(call):
    if not is_manager(call.from_user.id):
        bot.answer_callback_query(call.id, "فقط مدیر اصلی.", show_alert=True); return
    parts = call.data.split("_")
    action, warning_id = parts[2], int(parts[3])
    with connect_db() as db:
        row = db.execute("SELECT user_id,status FROM warning_history WHERE id=?", (warning_id,)).fetchone()
        if not row:
            bot.answer_callback_query(call.id, "هشدار پیدا نشد.", show_alert=True); return
        user_id, status = row
        if status != 'open':
            bot.answer_callback_query(call.id, "این هشدار قبلاً بررسی شده است.", show_alert=True); return
        if action == "mine":
            db.execute("UPDATE warning_history SET status='manager_confirmed',handled_by=?,handled_at=datetime('now','localtime') WHERE id=?", (call.from_user.id, warning_id))
            bot.answer_callback_query(call.id, "ثبت شد؛ هشدار در سابقه می‌ماند و تکرار نمی‌شود.")
            bot.send_message(call.message.chat.id, f"🙋 هشدار #{warning_id} به‌عنوان تغییر مدیر ثبت شد؛ سابقه حذف نمی‌شود.")
        elif action == "cancel":
            db.execute("UPDATE warning_history SET status='cancelled',handled_by=?,handled_at=datetime('now','localtime') WHERE id=?", (call.from_user.id, warning_id))
            bot.answer_callback_query(call.id, "هشدار لغو شد و در سابقه باقی ماند.")
            bot.send_message(call.message.chat.id, f"❌ هشدار #{warning_id} لغو شد؛ سابقه حذف نمی‌شود.")
        elif action == "reduce":
            steps[call.from_user.id] = {"action":"warning_reduce_amount", "warning_id":warning_id, "target_id":user_id}
            bot.send_message(call.message.chat.id, f"مقدار ZEX برای کم‌کردن از کاربر <code>{user_id}</code> را بفرست.")
            bot.answer_callback_query(call.id)
        elif action == "delete":
            db.execute("UPDATE users SET blocked=1 WHERE user_id=?", (user_id,))
            db.execute("UPDATE warning_history SET status='user_blocked',handled_by=?,handled_at=datetime('now','localtime') WHERE id=?", (call.from_user.id, warning_id))
            bot.answer_callback_query(call.id, "کاربر از ربات مسدود شد؛ موجودی باقی ماند.")
            bot.send_message(call.message.chat.id, f"🗑 کاربر <code>{user_id}</code> از ربات مسدود شد. موجودی و سابقه‌اش باقی مانده است.")

@bot.message_handler(func=lambda m: m.from_user is not None and m.from_user.id in steps and steps[m.from_user.id].get("action") == "manager_remove_user")
def manager_remove_user_step(message):
    admin_id = message.from_user.id
    if not is_manager(admin_id):
        steps.pop(admin_id, None); return
    text = (message.text or "").strip()
    if not text.isdigit():
        bot.send_message(message.chat.id, "آیدی عددی معتبر بفرست."); return
    target_id = int(text)
    mode = steps[admin_id].get("mode")
    target = get_user(target_id)
    if not target:
        bot.send_message(message.chat.id, "این کاربر در دیتابیس ربات پیدا نشد؛ برای گروه هم باید ربات قبلاً او را دیده باشد.")
        steps.pop(admin_id, None); return
    if target_id == MANAGER_ID:
        bot.send_message(message.chat.id, "حذف یا مسدودکردن مدیر اصلی مجاز نیست.")
        steps.pop(admin_id, None); return
    if mode == "remove_from_bot":
        with connect_db() as db:
            db.execute("UPDATE users SET blocked=1 WHERE user_id=?", (target_id,))
        bot.send_message(message.chat.id, f"✅ کاربر <code>{target_id}</code> از ربات مسدود شد. موجودی و سوابقش دست‌نخورده ماند.")
    else:
        with connect_db() as db:
            groups = db.execute("SELECT chat_id,title FROM known_groups").fetchall()
        succeeded, failed = 0, 0
        for group_id, title in groups:
            try:
                bot.ban_chat_member(group_id, target_id)
                # آنبَن می‌کنیم تا اخراج انجام شود ولی امکان برگشت با لینک گروه باقی بماند.
                bot.unban_chat_member(group_id, target_id, only_if_banned=True)
                succeeded += 1
            except Exception:
                failed += 1
        bot.send_message(message.chat.id, f"👢 تلاش برای حذف <code>{target_id}</code> از گروه‌ها تمام شد.\nموفق: {succeeded}\nناموفق/بدون دسترسی: {failed}\nموجودی کاربر تغییر نکرد. اگر گروهی در فهرست نبود، ربات هنوز آن گروه را ثبت نکرده است.")
    steps.pop(admin_id, None)

@bot.message_handler(func=lambda m: m.from_user is not None and m.from_user.id in steps and steps[m.from_user.id].get("action") == "warning_reduce_amount")
def warning_reduce_amount_step(message):
    admin_id = message.from_user.id
    step = steps.get(admin_id, {})
    raw = (message.text or "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        bot.send_message(message.chat.id, "مقدار باید عدد مثبت باشد."); return
    amount = int(raw); user_id = step["target_id"]; warning_id = step["warning_id"]
    with connect_db() as db:
        row = db.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            bot.send_message(message.chat.id, "کاربر پیدا نشد."); steps.pop(admin_id, None); return
        actual = min(amount, int(row[0] or 0))
        db.execute("UPDATE users SET balance=balance-? WHERE user_id=?", (actual, user_id))
        add_transaction(db, user_id, -actual, f"کاهش موجودی از طریق هشدار #{warning_id}", "admin")
        db.execute("UPDATE warning_history SET status='balance_reduced',handled_by=?,handled_at=datetime('now','localtime') WHERE id=?", (admin_id, warning_id))
    bot.send_message(message.chat.id, f"✅ از موجودی <code>{user_id}</code> مقدار {actual:,} ZEX کم شد. هشدار در سابقه باقی ماند.")
    steps.pop(admin_id, None)

@bot.message_handler(func=lambda m: m.from_user is not None and m.from_user.id in steps and steps[m.from_user.id].get("action") == "admin_target")
def admin_target_step(message):
    admin_id = message.from_user.id
    chat_id = message.chat.id
    text = (message.text or "").strip()
    if not text.isdigit():
        bot.send_message(chat_id, "❌ آیدی باید عددی باشد.")
        return
    target_id = int(text)
    target = get_user(target_id)
    if not target:
        bot.send_message(chat_id, "❌ کاربر پیدا نشد.")
        steps.pop(admin_id, None)
        return
    task = steps[admin_id]["task"]
    if target_id == MANAGER_ID:
        bot.send_message(chat_id, "❌ نمی‌توان روی مدیر اصلی این عملیات را انجام داد.")
        steps.pop(admin_id, None)
        return
    if task in ("block", "unblock"):
        with connect_db() as db:
            db.execute("UPDATE users SET blocked=? WHERE user_id=?", (1 if task == "block" else 0, target_id))
        bot.send_message(chat_id, "🚫 کاربر مسدود شد." if task == "block" else "✅ مسدودی کاربر برداشته شد.")
        steps.pop(admin_id, None)
        return
    if task == "make_admin":
        with connect_db() as db:
            db.execute("UPDATE users SET role='admin', admin_since=CASE WHEN role!='admin' OR admin_since='' THEN datetime('now') ELSE admin_since END WHERE user_id=?", (target_id,))
        bot.send_message(chat_id, "⭐ کاربر ادمین شد.")
        steps.pop(admin_id, None)
        return
    if task == "remove_admin":
        with connect_db() as db:
            db.execute("UPDATE users SET role='user', admin_since='' WHERE user_id=? AND role='admin'", (target_id,))
        bot.send_message(chat_id, "👤 دسترسی ادمین حذف شد.")
        steps.pop(admin_id, None)
        return
    steps[admin_id] = {"action": "admin_amount", "task": task, "target_id": target_id}
    bot.send_message(chat_id, "🔢 تعداد ZEX را وارد کن.")


@bot.message_handler(func=lambda m: m.from_user is not None and m.from_user.id in steps and steps[m.from_user.id].get("action") == "admin_amount")
def admin_amount_step(message):
    admin_id = message.from_user.id
    chat_id = message.chat.id
    text = (message.text or "").strip()
    if not text.isdigit() or not (0 < int(text) <= MAX_AMOUNT):
        bot.send_message(chat_id, "❌ تعداد باید عدد مثبت و در محدوده مجاز باشد.")
        return
    amount = int(text)
    step = steps.get(admin_id)
    if not step:
        return
    target_id = step["target_id"]
    task = step["task"]
    target = get_user(target_id)
    if not target:
        bot.send_message(chat_id, "❌ کاربر پیدا نشد.")
        steps.pop(admin_id, None)
        return
    if task == "add":
        ok = change_balance(target_id, amount, f"افزایش موجودی توسط مدیر {admin_id}")
        bot.send_message(chat_id, f"✅ {amount:,} ZEX به موجودی کاربر اضافه شد." if ok else "❌ تغییر موجودی انجام نشد.")
    elif task == "remove":
        ok = change_balance(target_id, -amount, f"کاهش موجودی توسط مدیر {admin_id}")
        bot.send_message(chat_id, f"✅ {amount:,} ZEX از موجودی کم شد." if ok else f"❌ موجودی کافی نیست. موجودی فعلی: {target[3]:,} ZEX")
    steps.pop(admin_id, None)


# ==============================
# بررسی مغایرت موجودی
# ==============================

def check_balance_audit():
    problems = []
    with connect_db() as db:
        rows = db.execute("""
            SELECT u.user_id, u.balance, b.base_balance, b.last_transaction_id
            FROM users u JOIN balance_audit_baseline b ON u.user_id=b.user_id
        """).fetchall()
        for user_id, balance, base_balance, last_id in rows:
            delta = db.execute(
                "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE user_id=? AND id>?",
                (user_id, last_id)
            ).fetchone()[0]
            expected = base_balance + delta
            if expected != balance:
                problems.append((user_id, balance, expected))
    return problems


def audit_loop():
    while True:
        try:
            problems = check_balance_audit()
            current = set()
            for user_id, balance, expected in problems:
                current.add(user_id)
                if user_id not in audit_alerts:
                    details = f"ثبت‌شده: {balance:,}؛ مورد انتظار: {expected:,}"
                    with connect_db() as db:
                        # اگر همین مغایرت قبلاً بررسی یا لغو شده باشد، پیام تکراری نفرست.
                        previous = db.execute("SELECT id,status FROM warning_history WHERE user_id=? AND reason=? AND details=? ORDER BY id DESC LIMIT 1", (user_id, "مغایرت موجودی", details)).fetchone()
                        if previous and previous[1] != 'open':
                            continue
                        if previous and previous[1] == 'open':
                            continue
                        db.execute("INSERT INTO warning_history(user_id,reason,details) VALUES(?,?,?)", (user_id, "مغایرت موجودی", details))
                        warning_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
                    kb = types.InlineKeyboardMarkup()
                    kb.row(types.InlineKeyboardButton("🙋 من بودم", callback_data=f"warning_handle_mine_{warning_id}"))
                    kb.row(types.InlineKeyboardButton("❌ لغو هشدار", callback_data=f"warning_handle_cancel_{warning_id}"),
                           types.InlineKeyboardButton("➖ کاهش موجودی", callback_data=f"warning_handle_reduce_{warning_id}"))
                    kb.row(types.InlineKeyboardButton("🗑 حذف کاربر", callback_data=f"warning_handle_delete_{warning_id}"))
                    bot.send_message(MANAGER_ID,
                        "⚠️ <b>هشدار بررسی موجودی</b>\n"
                        f"🆔 کاربر: <code>{user_id}</code>\n"
                        f"💰 موجودی ثبت‌شده: <b>{balance:,}</b>\n"
                        f"🧮 مقدار مورد انتظار: <b>{expected:,}</b>\n"
                        f"🕒 هشدار #{warning_id}", reply_markup=kb)
            audit_alerts.intersection_update(current)
            audit_alerts.update(current)
        except Exception as error:
            print(f"خطا در بررسی موجودی: {error}")
        time.sleep(600)



# ==============================
# ZEX و بازی دوز گروهی
# ==============================

# ZEX جدا از موجودی ZEX نگهداری می‌شود.
POINT_BUY_PRICE = 4.0       # هر ۱ ZEX، ۴ ZEX هزینه دارد
POINT_SELL_PRICE = 4.0      # هر ۱ ZEX، ۴ ZEX برمی‌گرداند
GAME_MIN_AMOUNT = 10
WIN_POINTS_RATE = 0.50      # برنده ۵۰٪ مبلغ بازی را ZEX می‌گیرد
LOSE_POINTS_RATE = 0.50     # بازنده ۵۰٪ مبلغ بازی را از ZEX می‌دهد

games = {}
pending_game_actions = {}
_game_counter = 0


def fmt_points(value):
    """نمایش ZEX با حداکثر دو رقم اعشار."""
    value = round(float(value or 0), 4)
    return f"{value:g}"


def get_game_points(user_id):
    """سازگاری با توابع قدیمی بازی؛ موجودی بازی همان موجودی واقعی ZEX است."""
    with connect_db() as db:
        row = db.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()
    return float(row[0] or 0) if row else 0.0


def add_point_transaction(db, user_id, amount, reason):
    # تمام تغییرات ZEX در تاریخچه جداگانه ذخیره می‌شوند.
    db.execute(
        "INSERT INTO point_transactions (user_id, amount, reason, created_at) VALUES (?, ?, ?, datetime('now'))",
        (user_id, round(float(amount), 4), reason)
    )


def convert_zex_to_points(user_id, zex_amount, reason="خرید ZEX بازی"):
    """کسر ZEX و اضافه‌کردن ZEX را در یک تراکنش انجام می‌دهد."""
    if zex_amount <= 0:
        return False
    points = zex_amount / POINT_BUY_PRICE
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not row or row[0] < zex_amount:
            db.rollback()
            return False
        db.execute("UPDATE users SET balance=balance-?, game_points=game_points+? WHERE user_id=?",
                   (zex_amount, points, user_id))
        add_transaction(db, user_id, -zex_amount, reason, "points_buy")
        add_point_transaction(db, user_id, points, reason)
        db.commit()
    return True


def convert_points_to_zex(user_id, points_amount):
    """فروش ZEX؛ مبلغ ZEX به عدد صحیح رو به بالا گرد می‌شود."""
    points_amount = round(float(points_amount), 4)
    zex_amount = int(__import__('math').ceil(points_amount * POINT_SELL_PRICE - 1e-9))
    if points_amount <= 0:
        return None
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not row or float(row[0] or 0) + 1e-9 < points_amount:
            db.rollback()
            return None
        db.execute("UPDATE users SET balance=balance-?, balance=balance+? WHERE user_id=?",
                   (points_amount, zex_amount, user_id))
        add_transaction(db, user_id, zex_amount, f"فروش {fmt_points(points_amount)} ZEX بازی", "points_sell")
        add_point_transaction(db, user_id, -points_amount, f"فروش ZEX و دریافت {zex_amount} ZEX")
        db.commit()
    return zex_amount


@bot.message_handler(func=lambda m: m.text == "🏅 ZEX بازی")
def old_points_button_handler(message):
    bot.send_message(message.chat.id, "🎮 از این به بعد همه بازی‌ها مستقیماً با موجودی ZEX انجام می‌شوند؛ تبدیل ZEX وجود ندارد.")


def player_has_open_game(user_id):
    # تا وقتی دعوت یا بازی فعال است، ZEX قابل فروش نیست.
    return any(g.get("status") in ("waiting", "playing") and user_id in (g.get("x_id"), g.get("o_id")) for g in games.values())


def game_required_points(amount):
    # برای اینکه بازنده بتواند جریمه ZEXی بازی را بپردازد، ۷۰٪ مبلغ لازم است.
    return round(amount * LOSE_POINTS_RATE, 4)


def game_board_keyboard(game):
    kb = types.InlineKeyboardMarkup(row_width=3)
    buttons = []
    for index, value in enumerate(game["board"]):
        label = value if value else str(index + 1)
        buttons.append(types.InlineKeyboardButton(label, callback_data=f"game_move_{game['id']}_{index}"))
    kb.add(*buttons)
    kb.row(types.InlineKeyboardButton("❌ لغو این بازی", callback_data=f"game_cancel_{game['id']}"))
    return kb


def game_board_text(game):
    x = game["x_id"]
    o = game.get("o_id")
    x_name = escape(game.get("x_name", str(x)))
    o_name = escape(game.get("o_name", "منتظر بازیکن دوم"))
    turn_name = x_name if game["turn"] == "X" else o_name
    joined = '۲/۲' if o else '۱/۲'
    return (f"🎮 <b>بازی دوز شروع شد</b>\n"
            f"👥 بازیکنان: <b>{joined}</b>\n"
            f"💠 مقدار بازی: <b>{game['amount']:,} ZEX</b>\n"
            f"❌ بازیکن X: {x_name}\n⭕ بازیکن O: {o_name}\n\n"
            f"🎯 نوبت: <b>{turn_name}</b>")


def game_invite(chat_id, command_message, creator_id, amount):
    global _game_counter
    _game_counter += 1
    game_id = str(_game_counter)
    creator = get_user(creator_id)
    creator_name = (creator[2] or creator[1] or str(creator_id)) if creator else str(creator_id)
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton("🎮 ورود به بازی", callback_data=f"game_join_{game_id}"),
           types.InlineKeyboardButton("❌ لغو بازی", callback_data=f"game_cancel_{game_id}"))
    creator_link = f"<a href='tg://user?id={creator_id}'>{escape(str(creator_name))}</a>"
    reply = bot.reply_to(command_message,
        f"🎮 <b>بازی جدید ایجاد شد.</b>\n"
        f"👤 سازنده: {creator_link}\n"
        f"👥 تعداد بازیکنان: <b>۱/۲</b>\n"
        f"💠 مقدار: <b>{amount:,} ZEX</b>\n"
        f"🏅 ZEX لازم برای پوشش باخت: <b>{fmt_points(game_required_points(amount))}</b>\n\n"
        "⏳ <b>در انتظار بازیکن دوم</b>", reply_markup=kb)
    games[game_id] = {
        "id": game_id, "chat_id": chat_id, "message_id": reply.message_id,
        "amount": amount, "x_id": creator_id, "x_name": creator_name,
        "o_id": None, "o_name": "منتظر بازیکن دوم", "board": [""] * 9,
        "turn": "X", "status": "waiting", "command_message_id": command_message.message_id
    }


def ask_for_game_points(user_id, chat_id, amount, role, game_id=None, command_message=None):
    user = get_user(user_id)
    if not user:
        bot.send_message(chat_id, "❌ اول در گفت‌وگوی خصوصی ربات /start را بزن.")
        return False
    required = game_required_points(amount)
    current = get_game_points(user_id)
    if user[3] < amount:
        bot.send_message(chat_id, f"❌ برای این بازی حداقل {amount} ZEX موجودی لازم داری. موجودی تو: {user[3]:,} ZEX")
        return False
    if current + 1e-9 < required:
        bot.send_message(chat_id, f"❌ برای پوشش بازی حداقل {required:g} ZEX لازم داری. موجودی فعلی: {current:g} ZEX")
        return False
    return True


def winner_line(board):
    lines = ((0,1,2),(3,4,5),(6,7,8),(0,3,6),(1,4,7),(2,5,8),(0,4,8),(2,4,6))
    for a,b,c in lines:
        if board[a] and board[a] == board[b] == board[c]:
            return board[a]
    if all(board):
        return "draw"
    return None


@bot.message_handler(func=lambda m: bool(m.text and re.fullmatch(r"دوز\s+\d+", m.text.strip())) and m.chat.type in ("group", "supergroup"))
def tic_tac_toe_start(message):
    user_id = message.from_user.id
    if is_balance_locked(user_id):
        bot.reply_to(message, "🔒 موجودی شما تا پایان بررسی درخواست فروش قفل است؛ فعلاً نمی‌توانی بازی کنی.")
        return
    if not check_active(user_id):
        bot.reply_to(message, "❌ حساب شما فعال نیست.")
        return
    amount = int(message.text.strip().split()[1])
    if amount < GAME_MIN_AMOUNT:
        bot.reply_to(message, f"❌ حداقل بازی {GAME_MIN_AMOUNT} ZEX است.")
        return
    if amount > MAX_AMOUNT:
        bot.reply_to(message, "❌ مبلغ بازی بیش از حد مجاز است.")
        return
    # فقط بازی فعالِ همین کاربر مانع ساخت بازی دوم اوست؛ بازی‌های دیگران مانعی ندارند.
    active_game = next((g for g in games.values()
                        if g.get("status") in ("waiting", "playing")
                        and user_id in (g.get("x_id"), g.get("o_id"))), None)
    if active_game:
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton(
            "❌ لغو بازی خودم", callback_data=f"game_cancel_{active_game['id']}"))
        bot.reply_to(message,
            "🎮 خودت یک بازی فعال داری. می‌تونی همون رو ادامه بدی یا با دکمه زیر لغوش کنی؛ بعد بازی جدید بساز.",
            reply_markup=kb)
        return
    if ask_for_game_points(user_id, message.chat.id, amount, "creator", command_message=message):
        game_invite(message.chat.id, message, user_id, amount)


@bot.callback_query_handler(func=lambda c: c.data.startswith("game_topup_yes_") or c.data.startswith("game_topup_no_"))
def obsolete_game_topup_callback(call):
    bot.answer_callback_query(call.id, "تبدیل ZEX حذف شده؛ بازی‌ها با ZEX انجام می‌شوند.", show_alert=True)


@bot.callback_query_handler(func=lambda c: c.data.startswith("game_cancel_") )
def game_cancel_callback(call):
    data = call.data
    user_id = call.from_user.id
    if data == "game_cancel_mine":
        active = next((g for g in games.values() if g.get("status") in ("waiting", "playing") and user_id in (g.get("x_id"), g.get("o_id"))), None)
        if not active:
            bot.answer_callback_query(call.id, "بازی فعالی پیدا نشد.", show_alert=True); return
        game = active
    else:
        game_id = data.removeprefix("game_cancel_")
        game = games.get(game_id)
        if not game or game.get("status") not in ("waiting", "playing"):
            bot.answer_callback_query(call.id, "این بازی دیگر فعال نیست.", show_alert=True); return
    if user_id not in (game.get("x_id"), game.get("o_id")):
        bot.answer_callback_query(call.id, "فقط بازیکنان همین بازی می‌توانند آن را لغو کنند.", show_alert=True); return
    game["status"] = "cancelled"
    try:
        bot.edit_message_text("❌ این بازی لغو شد.", game["chat_id"], game["message_id"], reply_markup=None)
    except Exception:
        bot.send_message(game["chat_id"], "❌ این بازی لغو شد.")
    bot.answer_callback_query(call.id, "بازی لغو شد")

@bot.callback_query_handler(func=lambda c: c.data.startswith("game_join_"))
def game_join_callback(call):
    game_id = call.data.removeprefix("game_join_")
    game = games.get(game_id)
    user_id = call.from_user.id
    if is_balance_locked(user_id):
        bot.answer_callback_query(call.id, "موجودی شما تا پایان بررسی درخواست فروش قفل است.", show_alert=True)
        return
    if not game or game["status"] != "waiting":
        bot.answer_callback_query(call.id, "این بازی دیگر قابل ورود نیست.", show_alert=True)
        return
    if user_id == game["x_id"]:
        bot.answer_callback_query(call.id, "خودت سازنده بازی هستی!", show_alert=True)
        return
    if not check_active(user_id):
        bot.answer_callback_query(call.id, "حساب شما فعال نیست.", show_alert=True)
        return
    if player_has_open_game(user_id):
        bot.answer_callback_query(call.id, "خودت یک بازی فعال داری؛ اول بازی خودت را تمام یا لغو کن.", show_alert=True)
        return
    if ask_for_game_points(user_id, call.message.chat.id, game["amount"], "joiner", game_id=game_id):
        join_game_after_check(call.message, game_id, user_id)
    bot.answer_callback_query(call.id)


def join_game_after_check(message, game_id, user_id):
    game = games.get(game_id)
    if not game or game["status"] != "waiting":
        bot.send_message(message.chat.id, "❌ این بازی دیگر در انتظار بازیکن نیست.")
        return
    if user_id == game["x_id"]:
        return
    user = get_user(user_id)
    creator = get_user(game["x_id"])
    required_points = game_required_points(game["amount"])
    if not creator or creator[3] < game["amount"] or get_game_points(game["x_id"]) + 1e-9 < required_points:
        game["status"] = "cancelled"
        try:
            bot.edit_message_text("❌ بازی لغو شد؛ موجودی یا ZEX سازنده دیگر کافی نیست.", game["chat_id"], game["message_id"], reply_markup=None)
        except Exception:
            bot.send_message(game["chat_id"], "❌ بازی لغو شد؛ موجودی یا ZEX سازنده دیگر کافی نیست.")
        return
    if not user or user[3] < game["amount"] or get_game_points(user_id) + 1e-9 < required_points:
        bot.send_message(message.chat.id, f"❌ برای ورود به این بازی باید {game['amount']} ZEX و {fmt_points(required_points)} ZEX داشته باشی.")
        return
    game["o_id"] = user_id
    game["o_name"] = user[2] or user[1] or str(user_id)
    game["status"] = "playing"
    try:
        bot.edit_message_text(game_board_text(game), game["chat_id"], game["message_id"], reply_markup=game_board_keyboard(game))
    except Exception as error:
        print(f"خطا در نمایش صفحه دوز: {error}")
        bot.send_message(game["chat_id"], game_board_text(game), reply_markup=game_board_keyboard(game))


def tic_result_keyboard(reward, winner_balance, loser_balance):
    """سه ردیف و شش گزینهٔ اطلاعاتی، هم‌شکل نتیجهٔ بازی شانسی."""
    winner_balance = math.floor(float(winner_balance))
    loser_balance = math.floor(float(loser_balance))
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.row(types.InlineKeyboardButton(f"{fmt_points(reward)} ZEX", callback_data="tic_result_info"),
           types.InlineKeyboardButton("جایزه برنده", callback_data="tic_result_info"))
    kb.row(types.InlineKeyboardButton(f"{fmt_points(winner_balance)} ZEX", callback_data="tic_result_info"),
           types.InlineKeyboardButton("موجودی برنده", callback_data="tic_result_info"))
    kb.row(types.InlineKeyboardButton(f"{fmt_points(loser_balance)} ZEX", callback_data="tic_result_info"),
           types.InlineKeyboardButton("موجودی بازنده", callback_data="tic_result_info"))
    return kb


@bot.callback_query_handler(func=lambda c: c.data == "tic_result_info")
def tic_result_info_callback(call):
    bot.answer_callback_query(call.id, "این گزینه فقط اطلاعات نتیجه بازی را نشان می‌دهد.")


@bot.callback_query_handler(func=lambda c: c.data.startswith("game_move_"))
def game_move_callback(call):
    parts = call.data.split("_")
    if len(parts) != 4:
        bot.answer_callback_query(call.id, "حرکت نامعتبر است.")
        return
    game_id = parts[2]
    try:
        index = int(parts[3])
    except ValueError:
        bot.answer_callback_query(call.id, "خانه نامعتبر است.")
        return
    game = games.get(game_id)
    if not game or game["status"] != "playing" or not 0 <= index < 9:
        bot.answer_callback_query(call.id, "این بازی فعال نیست.")
        return
    symbol = "X" if call.from_user.id == game["x_id"] else "O" if call.from_user.id == game["o_id"] else None
    if not symbol:
        bot.answer_callback_query(call.id, "تو بازیکن این بازی نیستی.", show_alert=True)
        return
    if symbol != game["turn"]:
        bot.answer_callback_query(call.id, "الان نوبت تو نیست.")
        return
    if game["board"][index]:
        bot.answer_callback_query(call.id, "این خانه پر است.")
        return
    game["board"][index] = "❌" if symbol == "X" else "⭕"
    # هر حرکت واقعی در دوز، فعالیت گروهی این کاربر را تا ۴۸ ساعت تازه می‌کند.
    with connect_db() as db:
        db.execute("UPDATE users SET game_activity_at=? WHERE user_id=?", (int(time.time()), call.from_user.id))
    # منطق برنده با نمادهای نمایشی کار می‌کند.
    result = winner_line(game["board"])
    if result:
        amount = game["amount"]
        if result == "draw":
            game["status"] = "finished"
            ending = "🤝 بازی مساوی شد؛ ZEXی کم یا زیاد نشد."
        else:
            winner_id = game["x_id"] if result == "❌" else game["o_id"]
            loser_id = game["o_id"] if result == "❌" else game["x_id"]
            # مانند بازی شانسی: ورودی هر دو بازیکن کسر و ۱۰٪ از مجموع ورودی‌ها مالیات می‌شود.
            total_entry = amount * 2
            tax = total_entry // 10
            reward = total_entry - tax
            winner_net = reward - amount
            try:
                with connect_db() as db:
                    db.execute("BEGIN IMMEDIATE")
                    winner_row = db.execute("SELECT balance FROM users WHERE user_id=?", (winner_id,)).fetchone()
                    loser_row = db.execute("SELECT balance FROM users WHERE user_id=?", (loser_id,)).fetchone()
                    if not winner_row or not loser_row:
                        raise ValueError("اطلاعات یکی از بازیکنان پیدا نشد")
                    if int(winner_row[0] or 0) < amount or int(loser_row[0] or 0) < amount:
                        raise ValueError("موجودی یکی از بازیکنان برای پرداخت ورودی کافی نیست")

                    # ابتدا ورودی از موجودی هر دو نفر کم می‌شود.
                    for uid in (winner_id, loser_id):
                        db.execute("UPDATE users SET balance=balance-? WHERE user_id=?", (amount, uid))
                        add_transaction(db, uid, -amount, f"ورودی بازی دوز {amount} ZEX", "game")

                    # سپس کل جایزه پس از کسر ۱۰٪ مالیات به برنده پرداخت می‌شود.
                    db.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (reward, winner_id))
                    add_transaction(db, winner_id, reward, f"جایزه برد دوز پس از کسر مالیات {tax} ZEX", "game")
                    db.commit()
            except Exception as error:
                game["board"][index] = ""
                game["turn"] = symbol
                print(f"خطا در تسویه بازی دوز {game_id}: {error}")
                bot.answer_callback_query(call.id, "ثبت نتیجه با خطا روبه‌رو شد؛ دوباره تلاش کن.", show_alert=True)
                return

            game["status"] = "finished"
            winner_name = game["x_name"] if winner_id == game["x_id"] else game["o_name"]
            loser_name = game["o_name"] if loser_id == game["o_id"] else game["x_name"]
            winner_balance = get_game_points(winner_id)
            loser_balance = get_game_points(loser_id)
            ending = (f"🏁 <b>پایان بازی دوز</b>\\n"
                      f"🏆 برنده: <a href='tg://user?id={winner_id}'>{escape(str(winner_name))}</a>\\n"
                      f"❌ بازنده: <a href='tg://user?id={loser_id}'>{escape(str(loser_name))}</a>\\n\\n"
                      f"➖ کسر ورودی از هر بازیکن: <b>{amount:,} ZEX</b>\\n"
                      f"🎁 مبلغ واریزی به برنده: <b>+{reward:,} ZEX</b>\\n"
                      f"📈 سود خالص برنده: <b>+{winner_net:,} ZEX</b>\\n"
                      f"🧾 مالیات ۱۰٪: <b>{tax:,} ZEX</b>")
            result_kb = tic_result_keyboard(reward, winner_balance, loser_balance)
        if result == "draw":
            result_kb = None
        text = game_board_text(game).split("\n\n🎯 نوبت:")[0] + "\n\n" + ending
        try:
            bot.edit_message_text(text, game["chat_id"], game["message_id"], reply_markup=result_kb)
        except Exception:
            bot.send_message(game["chat_id"], text, reply_markup=result_kb)
    else:
        game["turn"] = "O" if symbol == "X" else "X"
        try:
            bot.edit_message_text(game_board_text(game), game["chat_id"], game["message_id"], reply_markup=game_board_keyboard(game))
        except Exception as error:
            print(f"خطا در به‌روزرسانی دوز: {error}")
    bot.answer_callback_query(call.id)


# ==============================
# ==============================
# بازی شانسی چندنفره با موجودی واقعی ZEX
# ==============================

_random_games = {}
_random_game_counter = 0
_random_games_lock = threading.Lock()


def _change_game_points(user_id, delta, reason):
    """تغییر موجودی واقعی ZEX و ثبت آن در تاریخچهٔ اصلی تراکنش‌ها."""
    delta = int(round(float(delta)))
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            db.rollback()
            return False
        current = int(row[0] or 0)
        if delta < 0 and current < abs(delta):
            db.rollback()
            return False
        db.execute("UPDATE users SET balance=MAX(0, balance+?) WHERE user_id=?", (delta, user_id))
        add_transaction(db, user_id, delta, reason, "game")
        db.commit()
    return True


def _random_game_info(game):
    count = int(game.get("player_count", 2))
    amount = int(game["amount"])
    total = count * amount
    tax = total // 10
    prize = total - tax
    creator = escape(str(game.get("first_name", "بازیکن")))
    creator_id = int(game.get("first_id", 0))
    creator_link = f"<a href='tg://user?id={creator_id}'>{creator}</a>" if creator_id else creator
    return (f"🎲 <b>بازی جدید ایجاد شد.</b>\n"
            f"👤 سازنده: {creator_link}\n"
            f"👥 تعداد بازیکنان: <b>{len(game['players'])}/{count}</b>\n"
            f"💠 مقدار: <b>{amount:,} ZEX</b>\n"
            f"🏆 جایزه: <b>{prize:,} ZEX</b>\n"
            f"🧾 مالیات: <b>{tax:,} ZEX</b>\n\n"
            f"⏳ <b>در انتظار بازیکنان</b>")


@bot.message_handler(func=lambda m: bool(m.text and re.fullmatch(r"بازی\s+\d+", m.text.strip())) and m.chat.type in ("group", "supergroup"))
def random_game_start(message):
    global _random_game_counter
    try:
        with connect_db() as db:
            db.execute("INSERT INTO known_groups(chat_id,title) VALUES(?,?) ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title,seen_at=datetime('now','localtime')", (message.chat.id, message.chat.title or ""))
    except Exception:
        pass
    if not message.from_user or message.from_user.is_bot:
        return
    register_user(message.from_user)
    if not check_active(message.from_user.id):
        bot.reply_to(message, "❌ اول در گفت‌وگوی خصوصی ربات /start را بزن.")
        return
    if is_balance_locked(message.from_user.id):
        bot.reply_to(message, "🔒 موجودی شما تا پایان بررسی درخواست فروش قفل است؛ فعلاً نمی‌توانی بازی کنی.")
        return
    amount = int(message.text.strip().split()[1])
    if amount < GAME_MIN_AMOUNT:
        bot.reply_to(message, f"❌ حداقل ورودی بازی {GAME_MIN_AMOUNT} ZEX است.")
        return
    if amount > MAX_AMOUNT:
        bot.reply_to(message, "❌ مبلغ بازی بیش از حد مجاز است.")
        return
    if get_game_points(message.from_user.id) < amount:
        bot.reply_to(message, f"❌ برای ساخت بازی حداقل {amount:,} ZEX لازم داری. موجودی فعلی: {fmt_points(get_game_points(message.from_user.id))}")
        return
    own_waiting = next((g for g in _random_games.values() if g["status"] in ("selecting", "waiting") and any(x["id"] == message.from_user.id for x in g["players"])), None)
    if own_waiting:
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("❌ لغو بازی منتظر خودم", callback_data=f"random_cancel_{own_waiting['id']}"))
        bot.reply_to(message, "⏳ خودت هنوز یک بازی منتظر داری؛ اول آن را لغو کن یا منتظر بمان.", reply_markup=kb)
        return
    # شناسه یکتا؛ با ری‌استارت یا ساخت هم‌زمان بازی، دکمه‌های بازی‌های قبلی
    # به بازی تازه‌ای وصل نمی‌شوند.
    with _random_games_lock:
        _random_game_counter += 1
        game_id = uuid.uuid4().hex[:12]
    player = message.from_user
    name = player.first_name or player.username or str(player.id)
    game = {"id": game_id, "chat_id": message.chat.id, "message_id": 0,
            "first_id": player.id, "first_name": name, "amount": amount,
            "status": "selecting", "player_count": 0,
            "players": [{"id": player.id, "name": name}]}
    kb = types.InlineKeyboardMarkup()
    # سه گزینهٔ تعداد نفرات به شکل دکمه‌های اینلاین استاندارد تلگرام نمایش داده می‌شوند.
    kb.row(types.InlineKeyboardButton("۲ نفر", callback_data=f"random_count_{game_id}_2"))
    kb.row(types.InlineKeyboardButton("۳ نفر", callback_data=f"random_count_{game_id}_3"))
    kb.row(types.InlineKeyboardButton("۴ نفر", callback_data=f"random_count_{game_id}_4"))
    kb.add(types.InlineKeyboardButton("❌ لغو بازی", callback_data=f"random_cancel_{game_id}"))
    sent = bot.reply_to(message,
        f"🎲 <b>ساخت بازی شانسی ZEX</b>\n👤 سازنده: {escape(str(name))}\n🎟 ورودی هر نفر: <b>{amount:,} ZEX</b>\n"
        f"👥 تعداد بازیکنان را انتخاب کن:", reply_markup=kb)
    game["message_id"] = sent.message_id
    _random_games[game_id] = game


@bot.callback_query_handler(func=lambda c: c.data.startswith("random_count_"))
def random_game_choose_count(call):
    parts = call.data.split("_")
    try:
        game_id, count = parts[2], int(parts[3])
    except (IndexError, ValueError):
        bot.answer_callback_query(call.id, "درخواست نامعتبر است.", show_alert=True)
        return
    with _random_games_lock:
        game = _random_games.get(game_id)
        if not game or game["status"] != "selecting":
            bot.answer_callback_query(call.id, "این بازی دیگر قابل تنظیم نیست.", show_alert=True); return
        if call.from_user.id != game["first_id"]:
            bot.answer_callback_query(call.id, "فقط سازنده می‌تواند تعداد بازیکنان را انتخاب کند.", show_alert=True); return
        if count not in (2, 3, 4):
            bot.answer_callback_query(call.id, "تعداد نامعتبر است.", show_alert=True); return
        if get_game_points(game["first_id"]) < game["amount"]:
            game["status"] = "cancelled"
            bot.answer_callback_query(call.id, "موجودی کافی نیست.", show_alert=True); return
        game["player_count"] = count
        game["status"] = "waiting"
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🎲 پیوستن به بازی", callback_data=f"random_join_{game_id}"))
    kb.add(types.InlineKeyboardButton("❌ لغو بازی منتظر", callback_data=f"random_cancel_{game_id}"))
    try:
        bot.edit_message_text(_random_game_info(game), game["chat_id"], game["message_id"], reply_markup=kb)
    except Exception as error:
        print(f"خطا در نمایش بازی شانسی: {error}")
        bot.answer_callback_query(call.id, "نمایش بازی با خطا روبه‌رو شد؛ دوباره تلاش کن.", show_alert=True)
        return
    bot.answer_callback_query(call.id, f"بازی {count} نفره ساخته شد.")


@bot.callback_query_handler(func=lambda c: c.data.startswith("random_cancel_"))
def random_game_cancel(call):
    game_id = call.data[len("random_cancel_"):]
    with _random_games_lock:
        game = _random_games.get(game_id)
        if not game or game.get("status") not in ("selecting", "waiting"):
            bot.answer_callback_query(call.id, "این بازی شروع شده یا دیگر فعال نیست.", show_alert=True); return
        if call.from_user.id != game["first_id"]:
            bot.answer_callback_query(call.id, "فقط سازنده می‌تواند بازی خودش را لغو کند.", show_alert=True); return
        # وضعیت همان لحظه بسته می‌شود تا کلیک هم‌زمان برای پیوستن پذیرفته نشود.
        game["status"] = "cancelled"
    try:
        bot.edit_message_text("❌ بازی لغو شد؛ موجودی هیچ‌کس تغییر نکرد.", game["chat_id"], game["message_id"], reply_markup=None)
    except Exception as error:
        print(f"خطا در لغو بازی شانسی: {error}")
    bot.answer_callback_query(call.id, "بازی لغو شد.")


def _finish_random_game(game, call):
    """تسویهٔ بازی شانسی فقط یک‌بار و در یک تراکنش اتمیک انجام می‌شود."""
    with _random_games_lock:
        if game.get("status") != "processing":
            return
        game["status"] = "settling"
        players = list(game["players"])

    amount = int(game["amount"])
    winner = random.choice(players)
    losers = [p for p in players if p["id"] != winner["id"]]
    total = amount * len(players)
    tax = total // 10
    prize = total - tax
    try:
        with connect_db() as db:
            db.execute("BEGIN IMMEDIATE")
            # ابتدا موجودی همه بررسی می‌شود؛ قبل از کامل‌شدن بررسی هیچ تغییری ثبت نمی‌شود.
            balances = {}
            for player in players:
                row = db.execute("SELECT balance FROM users WHERE user_id=?", (int(player["id"]),)).fetchone()
                if not row or int(row[0] or 0) < amount:
                    raise ValueError(f"موجودی {player['name']} برای ورودی کافی نیست")
                balances[int(player["id"])] = int(row[0] or 0)

            # کسر ورودی همهٔ بازیکنان و ثبت تاریخچه در همان تراکنش.
            for player in players:
                uid = int(player["id"])
                db.execute("UPDATE users SET balance=balance-? WHERE user_id=?", (amount, uid))
                add_transaction(db, uid, -amount, f"ورودی بازی شانسی {game['id']} ({amount} ZEX)", "game")

            # جایزهٔ برنده طبق قوانین بازی ثبت می‌شود.
            winner_id = int(winner["id"])
            db.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (prize, winner_id))
            add_transaction(db, winner_id, prize, f"جایزه برد بازی شانسی {game['id']} ({prize} ZEX)", "game")
            db.commit()
    except Exception as error:
        try:
            with connect_db() as db:
                db.rollback()
        except Exception:
            pass
        with _random_games_lock:
            game["status"] = "cancelled"
        print(f"خطا در تسویهٔ بازی شانسی {game.get('id')}: {type(error).__name__}: {error}")
        bot.send_message(game["chat_id"],
            "❌ بازی شانسی انجام نشد؛ خطای ثبت نتیجه رخ داد. هیچ ورودی‌ای از بازیکنان کم نشد. مدیر می‌تواند گزارش خطا را بررسی کند.")
        try:
            bot.answer_callback_query(call.id, "تسویه ناموفق بود؛ مبلغی کسر نشد.", show_alert=True)
        except Exception:
            pass
        return

    with _random_games_lock:
        game["status"] = "finished"

    winner_balance = math.floor(float(get_game_points(winner["id"])))
    # در نتیجه فقط مقدار کل موجودی بازنده‌ها نمایش داده می‌شود، نه نام و موجودی تک‌تک آن‌ها.
    loser_balance_total = sum(math.floor(float(get_game_points(p["id"]))) for p in losers)
    loser_names = "، ".join(escape(str(p["name"])) for p in losers)
    result = (
        f"🏁 <b>نتیجه بازی مشخص شد</b>\n"
        f"🏆 بازیکن برنده: <a href='tg://user?id={winner['id']}'>{escape(str(winner['name']))}</a>\n"
        f"❌ بازیکنان بازنده: {loser_names}\n\n"
        f"🧾 مالیات: <b>{tax:,} ZEX</b>"
    )
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.row(types.InlineKeyboardButton(f"{prize:,} ZEX", callback_data="random_info_noop"),
           types.InlineKeyboardButton("جایزه برنده", callback_data="random_info_noop"))
    kb.row(types.InlineKeyboardButton(f"{winner_balance:,} ZEX", callback_data="random_info_noop"),
           types.InlineKeyboardButton("موجودی برنده", callback_data="random_info_noop"))
    kb.row(types.InlineKeyboardButton(f"{loser_balance_total:,} ZEX", callback_data="random_info_noop"),
           types.InlineKeyboardButton("موجودی بازنده‌ها", callback_data="random_info_noop"))
    try:
        bot.edit_message_text(result, game["chat_id"], game["message_id"], reply_markup=kb)
    except Exception:
        bot.send_message(game["chat_id"], result, reply_markup=kb)
    try:
        bot.answer_callback_query(call.id, "نتیجه ثبت شد!")
    except Exception:
        pass


@bot.callback_query_handler(func=lambda c: c.data.startswith("random_join_"))
def random_game_join(call):
    game_id = call.data[len("random_join_"):]
    uid = call.from_user.id
    if is_balance_locked(uid):
        bot.answer_callback_query(call.id, "موجودی شما تا پایان بررسی درخواست فروش قفل است.", show_alert=True)
        return
    if call.from_user.is_bot:
        bot.answer_callback_query(call.id, "ربات‌ها نمی‌توانند وارد بازی شوند.", show_alert=True); return
    if not check_active(uid):
        bot.answer_callback_query(call.id, "اول در گفت‌وگوی خصوصی ربات /start را بزن.", show_alert=True); return

    # بررسی ظرفیت و افزودن بازیکن در یک قفل واحد؛ دو کلیک هم‌زمان نمی‌توانند
    # ظرفیت را بیشتر کنند یا بازی را دوبار به مرحله پایان ببرند.
    with _random_games_lock:
        game = _random_games.get(game_id)
        if not game or game.get("status") != "waiting":
            bot.answer_callback_query(call.id, "این بازی دیگر فعال نیست؛ احتمالاً لغو یا تمام شده است.", show_alert=True); return
        if call.message.chat.id != game["chat_id"]:
            bot.answer_callback_query(call.id, "این بازی مربوط به گروه دیگری است.", show_alert=True); return
        if any(p["id"] == uid for p in game["players"]):
            bot.answer_callback_query(call.id, "تو قبلاً به این بازی پیوسته‌ای.", show_alert=True); return
        if len(game["players"]) >= game["player_count"]:
            bot.answer_callback_query(call.id, "ظرفیت این بازی تکمیل شده است.", show_alert=True); return
        if get_game_points(uid) < game["amount"]:
            bot.answer_callback_query(call.id, f"برای ورود حداقل {game['amount']:,} ZEX لازم داری.", show_alert=True); return
        name = call.from_user.first_name or call.from_user.username or str(uid)
        game["players"].append({"id": uid, "name": name})
        is_full = len(game["players"]) >= game["player_count"]
        if is_full:
            game["status"] = "processing"
        current_count = len(game["players"])

    if not is_full:
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("🎲 پیوستن به بازی", callback_data=f"random_join_{game_id}"))
        kb.add(types.InlineKeyboardButton("❌ لغو بازی منتظر", callback_data=f"random_cancel_{game_id}"))
        try:
            bot.edit_message_text(_random_game_info(game), game["chat_id"], game["message_id"], reply_markup=kb)
        except Exception as error:
            print(f"خطا در به‌روزرسانی بازیکنان بازی شانسی: {error}")
        bot.answer_callback_query(call.id, f"پیوستی؛ {current_count}/{game['player_count']} نفر")
        return

    _finish_random_game(game, call)


@bot.callback_query_handler(func=lambda c: c.data == "random_info_noop")
def random_result_info_noop(call):
    # دکمهٔ اطلاعاتی هیچ تغییری ایجاد نمی‌کند.
    bot.answer_callback_query(call.id, "این فقط اطلاعات نتیجه است.")


@bot.message_handler(func=lambda m: bool(m.text and m.text.strip() in ("رتبه بندی", "رتبه‌بندی")) and m.chat.type in ("group", "supergroup", "private"))
def game_points_leaderboard(message):
    # فقط مدیر اصلی می‌تواند یکی از دو رتبه‌بندی را انتخاب کند.
    if not message.from_user or message.from_user.id != MANAGER_ID:
        return
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton("🏆 بیشترین برد در بازی", callback_data="rank_wins"))
    kb.row(types.InlineKeyboardButton("💎 بیشترین موجودی ZEX", callback_data="rank_zex"))
    bot.reply_to(message, "📊 کدام رتبه‌بندی را می‌خواهی ببینی؟", reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data in ("rank_wins", "rank_zex"))
def ranking_choice_callback(call):
    if call.from_user.id != MANAGER_ID:
        bot.answer_callback_query(call.id, "این بخش فقط برای مدیر اصلی است.", show_alert=True)
        return
    with connect_db() as db:
        if call.data == "rank_zex":
            rows = db.execute(
                "SELECT user_id, first_name, username, balance FROM users "
                "WHERE user_id<>? AND blocked=0 ORDER BY balance DESC, user_id ASC LIMIT 10",
                (MANAGER_ID,)
            ).fetchall()
            title = "💎 <b>۱۰ نفر برتر از نظر موجودی ZEX</b>\n"
            suffix = " ZEX"
            values = rows
        else:
            # شمارش بردها از تراکنش‌های جایزه برد در بازی‌های ثبت‌شده انجام می‌شود.
            rows = db.execute(
                "SELECT u.user_id, u.first_name, u.username, COUNT(t.id) AS wins "
                "FROM users u LEFT JOIN transactions t ON t.user_id=u.user_id "
                "AND (t.reason LIKE '%جایزه برد بازی شانسی%' OR t.reason LIKE '%برد دوز%') "
                "WHERE u.user_id<>? AND u.blocked=0 GROUP BY u.user_id "
                "ORDER BY wins DESC, u.user_id ASC LIMIT 10",
                (MANAGER_ID,)
            ).fetchall()
            title = "🏆 <b>۱۰ نفر برتر از نظر تعداد برد</b>\n"
            suffix = " برد"
            values = rows
    if not values or (call.data == "rank_wins" and all(int(r[3] or 0) == 0 for r in values)):
        bot.answer_callback_query(call.id, "هنوز آمار کافی برای این رتبه‌بندی وجود ندارد.", show_alert=True)
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = [title]
    for index, (user_id, first_name, username, value) in enumerate(values, start=1):
        medal = medals[index - 1] if index <= 3 else f"{index}."
        name = first_name or (f"@{username}" if username else str(user_id))
        lines.append(f"{medal} <a href='tg://user?id={user_id}'>{escape(str(name))}</a> — <b>{int(value or 0):,}{suffix}</b>")
    bot.send_message(call.message.chat.id, "\n".join(lines)
                     , reply_to_message_id=call.message.message_id)
    bot.answer_callback_query(call.id)


# ==============================
# مدیریت گروه: ضداسپم، کیک و تگ کاربران شناخته‌شده
# ==============================

_recent_group_text = {}
_group_admin_cache = {}

def is_group_admin(user_id, chat_id=None):
    # مدیرهای ربات همیشه مجازند؛ ادمین‌های خود تلگرام نیز در همان گروه مجازند.
    if user_id == MANAGER_ID or is_admin(user_id):
        return True
    if chat_id is None:
        return False
    cache_key = (int(chat_id), int(user_id))
    cached = _group_admin_cache.get(cache_key)
    if cached and time.time() - cached[0] < 60:
        return cached[1]
    try:
        member = bot.get_chat_member(chat_id, user_id)
        result = member.status in ("administrator", "creator")
    except Exception:
        result = False
    _group_admin_cache[cache_key] = (time.time(), result)
    return result

@bot.message_handler(func=lambda m: m.chat.type in ("group", "supergroup") and bool(m.text)
    and not m.text.startswith("/") and m.text.strip() not in ("کیک", "تگ")
    and m.text.strip() not in ("گزارش", "اخطار", "اخطارها", "دستورات", "دستور ها", "دستورها")
    and not re.fullmatch(r"حذف\s+اخطار(?:\s*ها)?(?:\s+\d+)?", m.text.strip())
    and not re.fullmatch(r"سکوت به مدت\s+\d+\s+روز", m.text.strip())
    and not (m.from_user and is_group_admin(m.from_user.id, m.chat.id)))
def group_spam_guard(message):
    # فعالیت گروهی کاربران در ۴۸ ساعت اخیر ثبت می‌شود؛ تکرار متن هم کنترل می‌شود.
    if not message.from_user or message.from_user.is_bot:
        return
    # ثبت نام کاربر در دیتابیس باعث می‌شود مدیر بتواند کاربران فعال گروه را شناسایی کند.
    register_user(message.from_user)
    with connect_db() as db:
        db.execute("UPDATE users SET game_activity_at=? WHERE user_id=?", (int(time.time()), message.from_user.id))
    text = message.text.strip()
    if not text or len(text) > 1000:
        return
    key = (message.chat.id, message.from_user.id)
    previous = _recent_group_text.get(key)
    if previous and previous[0] == text and message.date - previous[1] <= 90:
        count = previous[2] + 1
    else:
        count = 1
    _recent_group_text[key] = (text, message.date, count)
    if count >= 3:
        try:
            until_date = int(time.time()) + 24 * 60 * 60
            bot.restrict_chat_member(message.chat.id, message.from_user.id, until_date=until_date,
                permissions=types.ChatPermissions(can_send_messages=False, can_send_audios=False, can_send_documents=False,
                can_send_photos=False, can_send_videos=False, can_send_video_notes=False, can_send_voice_notes=False,
                can_send_polls=False, can_send_other_messages=False, can_add_web_page_previews=False))
            bot.reply_to(message, f"🔇 {escape(message.from_user.first_name or 'کاربر')} به‌دلیل تکرار یک پیام ۳ بار، تا ۲۴ ساعت محدود شد.")
        except Exception as error:
            bot.reply_to(message, "⚠️ نتوانستم کاربر را محدود کنم؛ به ربات دسترسی مدیر و مجوز محدودکردن اعضا بده.")
            print(f"خطای محدودکردن اسپم: {error}")
        _recent_group_text.pop(key, None)

@bot.message_handler(func=lambda m: bool(m.text and m.text.strip() == "کیک") and m.chat.type in ("group", "supergroup"))
def manager_kick_reply(message):
    # دستور «کیک» باید روی پیام فرد موردنظر ریپلای شود.
    if not message.from_user or not is_group_admin(message.from_user.id, message.chat.id):
        return
    if not message.reply_to_message or not message.reply_to_message.from_user:
        bot.reply_to(message, "↩️ برای کیک‌کردن، روی پیام کاربر ریپلای کن و بنویس: کیک")
        return
    target = message.reply_to_message.from_user
    if target.is_bot or target.id in (MANAGER_ID, message.from_user.id):
        bot.reply_to(message, "❌ این کاربر قابل کیک‌کردن نیست.")
        return
    try:
        bot.ban_chat_member(message.chat.id, target.id)
        bot.unban_chat_member(message.chat.id, target.id, only_if_banned=True)
        notice = bot.reply_to(message, f"🚪 کاربر <b>{escape(target.first_name or 'کاربر')}</b> از گروه کیک شد.")
        threading.Thread(target=lambda: (time.sleep(5), bot.delete_message(message.chat.id, notice.message_id)), daemon=True).start()
    except Exception as error:
        bot.reply_to(message, "⚠️ کیک انجام نشد؛ ربات باید مدیر گروه با مجوز مسدودکردن اعضا باشد.")
        print(f"خطای کیک: {error}")

@bot.message_handler(func=lambda m: bool(m.text and m.text.strip() == "تگ") and m.chat.type in ("group", "supergroup"))
def manager_tag_known_users(message):
    # Bot API فهرست کامل اعضای گروه را نمی‌دهد؛ این دستور کاربران شناخته‌شده و فعال ۴۸ ساعت اخیر را تگ می‌کند.
    if not message.from_user or message.from_user.id != MANAGER_ID:
        return
    if not message.reply_to_message:
        bot.reply_to(message, "↩️ برای اجرای تگ، روی یک پیام ریپلای کن و بنویس: تگ")
        return
    cutoff = int(time.time()) - 48 * 60 * 60
    with connect_db() as db:
        rows = db.execute("SELECT user_id, first_name, username FROM users WHERE blocked=0 AND user_id<>? AND game_activity_at>=? ORDER BY user_id", (MANAGER_ID, cutoff)).fetchall()
    mentions = []
    for uid, first_name, username in rows:
        label = (first_name or ("@" + username if username else str(uid))).strip()
        mentions.append(f'<a href="tg://user?id={uid}">{escape(label)}</a>')
    if not mentions:
        bot.reply_to(message, "کاربر شناخته‌شده و فعالی برای تگ‌کردن پیدا نشد.")
        return
    # تکه‌تکه ارسال می‌شود تا محدودیت طول پیام تلگرام رعایت شود.
    for start_idx in range(0, len(mentions), 35):
        bot.send_message(message.chat.id, "📣 " + " ".join(mentions[start_idx:start_idx + 35]), disable_web_page_preview=True)


# ==============================
# اجرای ربات
# ==============================

def active_warning_loop():
    # اگر کاربر 43 ساعت از آخرین فعالیت رباتش گذشته باشد، 5 ساعت مانده به پایان بازهٔ 48 ساعته هشدار می‌گیرد.
    while True:
        try:
            now = int(time.time())
            start, end = now - 48 * 3600, now - 43 * 3600
            with connect_db() as db:
                rows = db.execute("SELECT user_id, bot_activity_at, active_warning_at FROM users WHERE blocked=0 AND bot_activity_at>? AND bot_activity_at<=? AND active_warning_at<bot_activity_at", (start, end)).fetchall()
                for user_id, last_active, warned_at in rows:
                    try:
                        bot.send_message(user_id, "⏳ <b>هشدار فعالیت</b>\nحدود ۵ ساعت تا غیرفعال‌شدن وضعیت فعالیتت باقی مانده. برای حفظ فعالیتت از مرکز خرید استفاده کن؛ مثل دریافت ZEX، خرید ZEX یا تبدیل ZEX به ZEX.", reply_markup=main_menu(user_id))
                    except Exception:
                        pass
                    db.execute("UPDATE users SET active_warning_at=? WHERE user_id=?", (last_active, user_id))
        except Exception as error:
            print(f"خطای هشدار فعالیت: {error}")
        time.sleep(600)




# ==============================
# بررسی عضویت اجباری در گپ ZEX
# ==============================

def group_chat_id():
    # آیدی گپ عمومی از لینک فعلی استخراج می‌شود.
    from urllib.parse import urlparse
    username = urlparse(GROUP_LINK).path.strip('/').split('/')[0]
    if username and not username.startswith('+'):
        return '@' + username
    return None

def is_group_member(user_id):
    # وضعیت عضویت را از تلگرام می‌پرسیم؛ ربات باید در گپ دسترسی لازم داشته باشد.
    chat_id = group_chat_id()
    if not chat_id:
        return False
    try:
        member = bot.get_chat_member(chat_id, user_id)
        return member.status in ('creator', 'administrator', 'member', 'restricted') and (member.status != 'restricted' or getattr(member, 'is_member', False))
    except Exception as error:
        print(f"بررسی عضویت گپ ناموفق بود: {error}")
        return False

def send_join_gate(chat_id, user_id, failed=False, referral_id=0):
    # شناسه دعوت‌کننده در callback حفظ می‌شود تا پس از تأیید عضویت از بین نرود.
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(types.InlineKeyboardButton('🔗 ورود به گپ ZEX', url=GROUP_LINK))
    callback_data = f'check_group_membership_{int(referral_id)}' if referral_id else 'check_group_membership'
    keyboard.add(types.InlineKeyboardButton('✅ عضو شدم', callback_data=callback_data))
    message_text = '❌ شما هنوز عضو گپ نشدید.\n\n' if failed else ''
    message_text += 'برای استفاده از ربات، ابتدا وارد گپ زیر شوید:'
    bot.send_message(chat_id, message_text, reply_markup=keyboard)

def check_membership_callback(call):
    # ابتدا عضویت واقعی از تلگرام بررسی می‌شود؛ سپس ثبت، پاداش زمان‌دار و پاداش دعوت انجام می‌شود.
    if is_group_member(call.from_user.id):
        data = call.data or ''
        referral_id = 0
        if data.startswith('check_group_membership_'):
            try:
                referral_id = int(data.rsplit('_', 1)[1])
            except (ValueError, IndexError):
                referral_id = 0
        existed = get_user(call.from_user.id) is not None
        register_user(call.from_user)
        bonus = grant_active_join_bonus(call.from_user.id) if not existed else None
        if not existed:
            attach_referral_after_membership(call.from_user.id, referral_id)
        bot.answer_callback_query(call.id, 'عضویت تأیید و حساب ثبت شد ✅')
        text = '✅ عضویتت تأیید شد و حسابت ثبت شد. حالا می‌تونی از ربات استفاده کنی.'
        if bonus:
            text += (
                f"\n\n🎉 پاداش عضویت زمان‌دار دریافت کردی!\n"
                f"💎 مقدار پاداش: {bonus['amount']:,} ZEX\n"
                f"📝 دلیل: {escape(bonus['reason'] or 'ثبت‌نام در بازه طرح پاداش')}\n"
                f"⏳ زمان باقی‌مانده: {remaining_duration_text(bonus['remaining'])}\n"
                f"📅 پایان طرح: {format_iran_datetime(bonus['ends_at'])}"
            )
        bot.send_message(call.message.chat.id, text, reply_markup=main_menu(call.from_user.id) if call.message.chat.type == "private" else types.ReplyKeyboardRemove())
    else:
        bot.answer_callback_query(call.id, 'هنوز عضویتت تأیید نشده است.', show_alert=True)
        referral_id = 0
        if (call.data or '').startswith('check_group_membership_'):
            try:
                referral_id = int(call.data.rsplit('_', 1)[1])
            except (ValueError, IndexError):
                referral_id = 0
        send_join_gate(call.message.chat.id, call.from_user.id, failed=True, referral_id=referral_id)

bot.callback_query_handler(func=lambda c: c.data == 'check_group_membership' or c.data.startswith('check_group_membership_'))(check_membership_callback)

def install_membership_guards():
    # همهٔ قابلیت‌های دیگر ربات تا عضویت در گپ قفل می‌شوند؛ /start و دکمهٔ بررسی آزادند.
    for handler in bot.message_handlers:
        original = handler.get('function')
        if not original or getattr(original, '_membership_guarded', False):
            continue
        if getattr(original, '__name__', '') == 'start':
            continue
        def make_wrapper(func):
            def wrapped(message):
                if message.from_user:
                    user_row = get_user(message.from_user.id)
                    allowed_while_blocked = bool(message.text and message.text.strip() in ("اخطارها", "/warnings"))
                    if user_row and user_row[6] and not allowed_while_blocked:
                        if message.chat.type == 'private':
                            bot.send_message(message.chat.id, "⛔ دسترسی شما به ربات به‌صورت موقت مسدود است.")
                        return
                if message.chat.type == 'private' and message.from_user:
                    allowed_without_group = bool(message.text and message.text.strip() in ("اخطارها", "/warnings"))
                    if not allowed_without_group and not is_group_member(message.from_user.id):
                        send_join_gate(message.chat.id, message.from_user.id)
                        return
                return func(message)
            wrapped.__name__ = getattr(func, '__name__', 'wrapped')
            wrapped._membership_guarded = True
            return wrapped
        handler['function'] = make_wrapper(original)

    for handler in bot.callback_query_handlers:
        original = handler.get('function')
        if not original or getattr(original, '_membership_guarded', False):
            continue
        if getattr(original, '__name__', '') == 'check_membership_callback':
            continue
        def make_callback_wrapper(func):
            def wrapped(call):
                if (call.data or '').startswith('mod_report_'):
                    return func(call)
                if not is_group_member(call.from_user.id):
                    bot.answer_callback_query(call.id, 'ابتدا باید عضو گپ ZEX شوی.', show_alert=True)
                    if call.message:
                        send_join_gate(call.message.chat.id, call.from_user.id)
                    return
                return func(call)
            wrapped.__name__ = getattr(func, '__name__', 'wrapped_callback')
            wrapped._membership_guarded = True
            return wrapped
        handler['function'] = make_callback_wrapper(original)


# بازی موشکی از نسخه ۳۸ حذف شده است؛ جدول‌های قدیمی دیتابیس برای حفظ داده‌ها دست‌نخورده می‌مانند.


_TIC_GAME_LOCK = threading.RLock()

def install_tic_game_callback_locks():
    """قفل مشترک برای جلوگیری از ورود هم‌زمان و ثبت دوبارهٔ حرکت/نتیجهٔ دوز."""
    protected = {"game_join_callback", "game_move_callback", "game_cancel_callback"}
    for handler in bot.callback_query_handlers:
        original = handler.get("function")
        if not original or getattr(original, "_tic_locked", False):
            continue
        if getattr(original, "__name__", "") not in protected:
            continue
        def make_wrapper(func):
            def wrapped(call):
                with _TIC_GAME_LOCK:
                    return func(call)
            wrapped.__name__ = getattr(func, "__name__", "tic_locked_callback")
            wrapped._tic_locked = True
            return wrapped
        handler["function"] = make_wrapper(original)



# ==============================
# گزارش، اخطار و محدودیت زمان‌دار گروه
# ==============================

def _moderation_name(user):
    if not user:
        return "کاربر نامشخص"
    return (user.first_name or "کاربر").strip()

def _moderation_admin_label(user_id):
    try:
        u = bot.get_chat(user_id)
        name = getattr(u, "first_name", None) or str(user_id)
        username = getattr(u, "username", None)
        return f"{escape(name)}" + (f" (@{escape(username)})" if username else "") + f" | <code>{user_id}</code>"
    except Exception:
        return f"<code>{user_id}</code>"

def _moderation_notify_admins(text, markup=None):
    # ارسال خصوصی به ادمین‌های ثبت‌شده؛ اگر کاربر قبلاً ربات را استارت نکرده باشد تلگرام اجازه DM نمی‌دهد.
    send_to_admins(text, reply_markup=markup)


def _moderation_issue_warning(user_id, chat_id, actor_id, reason, report_id=None):
    with connect_db() as db:
        db.execute("INSERT INTO moderation_warnings(user_id,chat_id,report_id,actor_id,reason) VALUES(?,?,?,?,?)",
                   (user_id, chat_id, report_id, actor_id, reason[:500]))
        count = db.execute("SELECT COUNT(*) FROM moderation_warnings WHERE user_id=?", (user_id,)).fetchone()[0]
        prior = db.execute("SELECT blocked FROM users WHERE user_id=?", (user_id,)).fetchone()
        prior_blocked = int(prior[0]) if prior else 0
    duration_days = 3 if count == 3 else (7 if count > 3 else 0)
    restriction_text = ""
    if duration_days:
        until_ts = int(time.time()) + duration_days * 86400
        with connect_db() as db:
            db.execute("UPDATE users SET blocked=1 WHERE user_id=?", (user_id,))
            db.execute("""INSERT INTO moderation_restrictions(user_id,chat_id,kind,until_ts,prior_blocked)
                VALUES(?,?,'ban',?,?) ON CONFLICT(user_id,chat_id,kind) DO UPDATE SET
                until_ts=MAX(until_ts,excluded.until_ts), prior_blocked=MAX(prior_blocked,excluded.prior_blocked)""",
                (user_id, chat_id, until_ts, prior_blocked))
        try:
            bot.ban_chat_member(chat_id, user_id, until_date=until_ts)
            restriction_text = f"\n⛔ به‌دلیل رسیدن به {count} اخطار، {duration_days} روز از گروه و ربات مسدود شد."
        except Exception as error:
            restriction_text = f"\n⚠️ دسترسی ربات برای {duration_days} روز مسدود شد، اما مسدودسازی گروه انجام نشد؛ مجوز بن‌کردن ربات را بررسی کنید."
            print(f"خطای مسدودسازی گروه پس از اخطار: {error}")
    return count, duration_days, restriction_text


def _moderation_report_keyboard(report_id):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.row(types.InlineKeyboardButton("✅ بررسی شد", callback_data=f"mod_report_review_{report_id}"),
           types.InlineKeyboardButton("⚠️ اضافه کردن اخطار", callback_data=f"mod_report_warn_{report_id}"))
    return kb


@bot.message_handler(func=lambda m: m.chat.type in ("group", "supergroup") and bool(m.text) and m.text.strip() == "گزارش")
def moderation_report_message(message):
    if not message.from_user or message.from_user.is_bot:
        return
    if not message.reply_to_message or not message.reply_to_message.from_user:
        bot.reply_to(message, "↩️ برای گزارش، روی پیام موردنظر ریپلای کن و بنویس: گزارش")
        return
    target = message.reply_to_message.from_user
    if target.is_bot or target.id == message.from_user.id:
        bot.reply_to(message, "❌ این پیام قابل گزارش نیست.")
        return
    register_user(message.from_user)
    register_user(target)
    target_name = _moderation_name(target)
    reporter_name = _moderation_name(message.from_user)
    message_text = (message.reply_to_message.text or message.reply_to_message.caption or "[پیام بدون متن]")[:1500]
    with connect_db() as db:
        db.execute("INSERT OR REPLACE INTO known_groups(chat_id,title) VALUES(?,?)", (message.chat.id, message.chat.title or "گروه"))
        cur = db.execute("""INSERT INTO moderation_reports(chat_id,group_title,message_id,target_user_id,target_name,target_username,
            reporter_id,reporter_name,message_text) VALUES(?,?,?,?,?,?,?,?,?)""",
            (message.chat.id, message.chat.title or "گروه", message.reply_to_message.message_id, target.id, target_name,
             target.username or "", message.from_user.id, reporter_name, message_text))
        report_id = cur.lastrowid
    mention = f'<a href="tg://user?id={target.id}">{escape(target_name)}</a>'
    with connect_db() as db:
        admin_rows = db.execute("SELECT user_id,username,first_name FROM users WHERE role IN ('manager','admin') AND blocked=0").fetchall()
    admin_mentions = " ".join(f'<a href="tg://user?id={uid}">{escape(first or username or str(uid))}</a>' for uid, username, first in admin_rows)
    report_text = (f"🚨 <b>گزارش جدید گروه</b> | شماره {report_id}\n"
        f"🔔 مدیران ربات: {admin_mentions or 'ارسال خصوصی انجام می‌شود'}\n"
        f"👥 گروه: {escape(message.chat.title or 'گروه')}\n"
        f"👤 کاربر گزارش‌شده: {mention} | <code>{target.id}</code>\n"
        f"🗣 گزارش‌دهنده: {escape(reporter_name)} | <code>{message.from_user.id}</code>\n"
        f"💬 متن پیام: {escape(message_text)}")
    _moderation_notify_admins(report_text, _moderation_report_keyboard(report_id))
    if admin_mentions:
        try:
            bot.send_message(message.chat.id, f"🔔 مدیران ربات {admin_mentions} — یک گزارش جدید با شماره <b>{report_id}</b> برای بررسی ارسال شد.", disable_web_page_preview=True)
        except Exception as error:
            print(f"خطای تگ مدیران در گروه: {error}")
    bot.reply_to(message, f"✅ گزارش شما با شماره {report_id} برای مدیران ارسال شد.")


@bot.callback_query_handler(func=lambda c: (c.data or "").startswith("mod_report_review_") or (c.data or "").startswith("mod_report_warn_"))
def moderation_report_action(call):
    try:
        action, report_id = call.data.rsplit("_", 1)
        report_id = int(report_id)
    except Exception:
        bot.answer_callback_query(call.id, "گزارش نامعتبر است.", show_alert=True)
        return
    with connect_db() as db:
        row = db.execute("SELECT chat_id,target_user_id,target_name,target_username,reporter_id,reporter_name,message_text,status FROM moderation_reports WHERE id=?", (report_id,)).fetchone()
    if not row:
        bot.answer_callback_query(call.id, "گزارش پیدا نشد.", show_alert=True)
        return
    chat_id, target_id, target_name, target_username, reporter_id, reporter_name, message_text, status = row
    if not is_group_admin(call.from_user.id, chat_id):
        bot.answer_callback_query(call.id, "فقط مدیران ربات یا مدیران همین گروه اجازه بررسی دارند.", show_alert=True)
        return
    if status != "pending":
        bot.answer_callback_query(call.id, "این گزارش قبلاً بررسی شده است.", show_alert=True)
        return
    handled_action = "reviewed" if action == "mod_report_review" else "warning"
    with connect_db() as db:
        changed = db.execute("UPDATE moderation_reports SET status=?,handled_by=?,handled_action=?,handled_at=datetime('now','localtime') WHERE id=? AND status='pending'",
            (handled_action, call.from_user.id, handled_action, report_id)).rowcount
    if not changed:
        bot.answer_callback_query(call.id, "این گزارش هم‌زمان توسط فرد دیگری بررسی شد.", show_alert=True)
        return
    if handled_action == "warning":
        count, days, restriction_text = _moderation_issue_warning(target_id, chat_id, call.from_user.id, "اخطار از طریق گزارش", report_id)
    else:
        count = None
        restriction_text = ""
    actor = _moderation_admin_label(call.from_user.id)
    detail = (f"\n⚠️ تعداد اخطارهای کاربر: {count}{restriction_text}" if count is not None else "\n✅ گزارش فقط بررسی و بسته شد.")
    _moderation_notify_admins(f"📋 <b>گزارش {report_id} بررسی شد</b>\n👤 کاربر: {escape(target_name)} (<code>{target_id}</code>)\n🛠 اقدام‌کننده: {actor}\n📨 گزارش‌دهنده: {escape(reporter_name)} (<code>{reporter_id}</code>){detail}")
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    bot.answer_callback_query(call.id, "اقدام ثبت شد.")


@bot.message_handler(func=lambda m: m.chat.type in ("group", "supergroup") and bool(m.text) and m.text.strip() == "اخطار")
def moderation_direct_warning(message):
    if not message.from_user or not is_group_admin(message.from_user.id, message.chat.id):
        return
    if not message.reply_to_message or not message.reply_to_message.from_user:
        bot.reply_to(message, "↩️ روی پیام کاربر ریپلای کن و بنویس: اخطار")
        return
    target = message.reply_to_message.from_user
    if target.is_bot or target.id in (message.from_user.id, MANAGER_ID):
        bot.reply_to(message, "❌ برای این کاربر اخطار ثبت نمی‌شود.")
        return
    register_user(target)
    count, days, extra = _moderation_issue_warning(target.id, message.chat.id, message.from_user.id, "اخطار مستقیم مدیر")
    bot.reply_to(message, f"⚠️ اخطار برای {escape(_moderation_name(target))} ثبت شد.\n📊 مجموع اخطارها: {count}{extra}")
    _moderation_notify_admins(f"⚠️ <b>اخطار جدید</b>\n👤 کاربر: {escape(_moderation_name(target))} (<code>{target.id}</code>)\n🛠 ثبت‌کننده: {_moderation_admin_label(message.from_user.id)}\n📊 مجموع اخطارها: {count}{extra}")


@bot.message_handler(func=lambda m: m.chat.type in ("group", "supergroup") and bool(m.text)
    and re.fullmatch(r"حذف\s+اخطار(?:\s*ها)?(?:\s+\d+)?", m.text.strip()))
def moderation_remove_warnings(message):
    # فقط مدیران ربات و مدیران همان گروه اجازه حذف اخطار دارند.
    if not message.from_user or not is_group_admin(message.from_user.id, message.chat.id):
        return
    if not message.reply_to_message or not message.reply_to_message.from_user:
        bot.reply_to(message, "↩️ روی پیام کاربر ریپلای کن و بنویس: حذف اخطارها یا حذف اخطار 2")
        return

    target = message.reply_to_message.from_user
    if target.is_bot:
        bot.reply_to(message, "❌ اخطارهای ربات قابل حذف نیست.")
        return

    match = re.fullmatch(r"حذف\s+اخطار(?:\s*ها)?(?:\s+(\d+))?", message.text.strip())
    amount_text = match.group(1) if match else None
    requested = int(amount_text) if amount_text else None

    with connect_db() as db:
        total = db.execute(
            "SELECT COUNT(*) FROM moderation_warnings WHERE user_id=?",
            (target.id,)
        ).fetchone()[0]

        if total == 0:
            bot.reply_to(message, f"ℹ️ {escape(_moderation_name(target))} هیچ اخطاری ندارد.")
            return

        remove_count = total if requested is None else min(requested, total)
        if remove_count <= 0:
            bot.reply_to(message, "❌ تعداد اخطار باید بیشتر از صفر باشد.")
            return

        db.execute("""
            DELETE FROM moderation_warnings
            WHERE id IN (
                SELECT id FROM moderation_warnings
                WHERE user_id=?
                ORDER BY id DESC
                LIMIT ?
            )
        """, (target.id, remove_count))
        remaining = db.execute(
            "SELECT COUNT(*) FROM moderation_warnings WHERE user_id=?",
            (target.id,)
        ).fetchone()[0]

    actor = _moderation_admin_label(message.from_user.id)
    target_label = f"{escape(_moderation_name(target))} (<code>{target.id}</code>)"
    if requested is None:
        result_text = f"🧹 تمام اخطارهای {target_label} حذف شد."
    else:
        result_text = f"🧹 {remove_count} اخطار از {target_label} حذف شد."
    result_text += f"\n📊 اخطارهای باقی‌مانده: <b>{remaining}</b>"
    result_text += "\nℹ️ اگر کاربر از قبل مسدودیت زمان‌دار داشته باشد، آن مسدودیت تا پایان زمان تعیین‌شده باقی می‌ماند."
    bot.reply_to(message, result_text)

    _moderation_notify_admins(
        f"🧹 <b>حذف اخطار انجام شد</b>\n"
        f"👤 کاربر: {target_label}\n"
        f"➖ تعداد حذف‌شده: <b>{remove_count}</b>\n"
        f"📊 باقی‌مانده: <b>{remaining}</b>\n"
        f"🛠 انجام‌دهنده: {actor}"
    )


@bot.message_handler(func=lambda m: m.chat.type in ("private", "group", "supergroup") and bool(m.text) and m.text.strip() in ("اخطارها", "/warnings"))
def moderation_show_warnings(message):
    target_id = message.from_user.id
    with connect_db() as db:
        count = db.execute("SELECT COUNT(*) FROM moderation_warnings WHERE user_id=?", (target_id,)).fetchone()[0]
        rows = db.execute("SELECT reason,created_at FROM moderation_warnings WHERE user_id=? ORDER BY id DESC LIMIT 5", (target_id,)).fetchall()
    text = f"⚠️ <b>سابقه اخطارهای شما</b>\n📊 تعداد کل: <b>{count}</b>"
    if rows:
        text += "\n\n<b>آخرین اخطارها:</b>"
        for reason, created in rows:
            text += f"\n• {escape(reason)} — {escape(format_iran_datetime(created))}"
    bot.reply_to(message, text)


@bot.message_handler(func=lambda m: m.chat.type in ("group", "supergroup") and bool(m.text) and re.fullmatch(r"سکوت به مدت\s+\d+\s+روز", m.text.strip()))
def moderation_mute_reply(message):
    if not message.from_user or not is_group_admin(message.from_user.id, message.chat.id):
        return
    if not message.reply_to_message or not message.reply_to_message.from_user:
        bot.reply_to(message, "↩️ روی پیام کاربر ریپلای کن و بنویس: سکوت به مدت 3 روز")
        return
    days = int(re.search(r"(\d+)", message.text).group(1))
    if not 1 <= days <= 30:
        bot.reply_to(message, "مدت سکوت باید بین ۱ تا ۳۰ روز باشد.")
        return
    target = message.reply_to_message.from_user
    if target.is_bot or target.id in (message.from_user.id, MANAGER_ID):
        bot.reply_to(message, "❌ این کاربر قابل محدودکردن نیست.")
        return
    until_ts = int(time.time()) + days * 86400
    try:
        bot.restrict_chat_member(message.chat.id, target.id, until_date=until_ts,
            permissions=types.ChatPermissions(can_send_messages=False, can_send_audios=False, can_send_documents=False,
                can_send_photos=False, can_send_videos=False, can_send_video_notes=False, can_send_voice_notes=False,
                can_send_polls=False, can_send_other_messages=False, can_add_web_page_previews=False))
        with connect_db() as db:
            db.execute("INSERT INTO moderation_restrictions(user_id,chat_id,kind,until_ts,prior_blocked) VALUES(?,?,'mute',?,0) "
                "ON CONFLICT(user_id,chat_id,kind) DO UPDATE SET until_ts=MAX(until_ts,excluded.until_ts)",
                (target.id, message.chat.id, until_ts))
        bot.reply_to(message, f"🔇 {escape(_moderation_name(target))} به مدت {days} روز سکوت شد.")
    except Exception as error:
        bot.reply_to(message, "⚠️ سکوت انجام نشد؛ ربات باید مدیر گروه و دارای مجوز محدودکردن اعضا باشد.")
        print(f"خطای سکوت زمان‌دار: {error}")


def moderation_expiry_loop():
    while True:
        try:
            now = int(time.time())
            with connect_db() as db:
                expired = db.execute("SELECT user_id,chat_id,kind,prior_blocked FROM moderation_restrictions WHERE until_ts<=?", (now,)).fetchall()
                for uid, chat_id, kind, prior_blocked in expired:
                    try:
                        if kind == "ban":
                            try:
                                bot.unban_chat_member(chat_id, uid, only_if_banned=True)
                            except Exception as unban_error:
                                # خود تلگرام نیز بنِ زمان‌دار را در زمان پایان منقضی می‌کند؛
                                # دسترسی ربات را در هر حال در موعد مقرر برمی‌گردانیم.
                                print(f"خطای رفع بن تلگرام برای {uid}: {unban_error}")
                            db.execute("DELETE FROM moderation_restrictions WHERE user_id=? AND chat_id=? AND kind=?", (uid, chat_id, kind))
                            other_bans = db.execute("SELECT 1 FROM moderation_restrictions WHERE user_id=? AND kind='ban' LIMIT 1", (uid,)).fetchone()
                            if not other_bans:
                                db.execute("UPDATE users SET blocked=? WHERE user_id=?", (prior_blocked, uid))
                            continue
                        elif kind == "mute":
                            chat_info = bot.get_chat(chat_id)
                            default_permissions = getattr(chat_info, "permissions", None)
                            if default_permissions is None:
                                default_permissions = types.ChatPermissions(can_send_messages=True, can_send_audios=True,
                                    can_send_documents=True, can_send_photos=True, can_send_videos=True,
                                    can_send_video_notes=True, can_send_voice_notes=True, can_send_polls=True,
                                    can_send_other_messages=True, can_add_web_page_previews=True)
                            bot.restrict_chat_member(chat_id, uid, permissions=default_permissions)
                        db.execute("DELETE FROM moderation_restrictions WHERE user_id=? AND chat_id=? AND kind=?", (uid, chat_id, kind))
                    except Exception as error:
                        print(f"خطای پایان محدودیت {uid}: {error}")
        except Exception as error:
            print(f"خطای حلقه پایان محدودیت‌ها: {error}")
        time.sleep(30)

# ==============================
# Render health server (Flask)
# ==============================
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!", 200

def run_flask():
    # Render مقدار PORT را به‌صورت متغیر محیطی در اختیار سرویس می‌گذارد.
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)


def run_bot():
    if TOKEN == "PASTE_NEW_BOT_TOKEN_HERE" or ":" not in TOKEN:
        raise RuntimeError("ابتدا TOKEN را با توکن جدید ربات در فایل تنظیم کنید.")
    create_db()
    bot.set_update_listener(save_admin_incoming_messages)
    install_membership_guards()
    install_tic_game_callback_locks()
    threading.Thread(target=audit_loop, daemon=True).start()
    threading.Thread(target=active_warning_loop, daemon=True).start()
    threading.Thread(target=moderation_expiry_loop, daemon=True).start()
    print("ربات ZEX نسخه ۳۸ آماده اجراست.")
    while True:
        try:
            print("در حال اتصال به تلگرام...")
            bot.polling(non_stop=False, timeout=30, long_polling_timeout=30, skip_pending=False)
        except Exception as error:
            print(f"اتصال قطع شد: {error}")
            print("۱۰ ثانیه دیگر دوباره تلاش می‌کنم...")
            time.sleep(10)




@bot.message_handler(func=lambda m: bool(m.text and m.text.strip().lower() in ("دستورات", "دستور ها", "دستورها", "دستورها")) and m.chat.type in ("group", "supergroup"))
def group_admin_commands_help(message):
    uid = message.from_user.id if message.from_user else 0
    if not is_admin(uid):
        return
    body = (
        "📚 <b>دستورهای مدیریتی قابل استفاده در گپ</b>\n\n"
        "• <code>افزایش 100</code> — افزایش موجودی با ریپلای روی کاربر (مدیر اصلی)\n"
        "• <code>کاهش 100</code> — کاهش موجودی با ریپلای روی کاربر (مدیر اصلی)\n"
        "• <code>افزایش 100 کل</code> — افزایش موجودی کاربران شناخته‌شده گروه (مدیر اصلی)\n"
        "• <code>کاهش 100 کل</code> — کاهش موجودی کاربران شناخته‌شده گروه (مدیر اصلی)\n"
        "• <code>اخطار</code> — ثبت اخطار با ریپلای\n"
        "• <code>اخطارها</code> — مشاهده تعداد اخطارهای خودت\n"
        "• <code>حذف اخطارها</code> — حذف همه اخطارهای کاربر با ریپلای\n"
        "• <code>حذف اخطار 2</code> — حذف دو اخطار آخر با ریپلای\n"
        "• <code>کیک</code> — حذف کاربر از گروه با ریپلای\n"
        "• <code>سکوت به مدت 3 روز</code> — محدودکردن ارسال پیام با ریپلای\n"
        "• <code>گزارش</code> — ثبت گزارش روی پیام با ریپلای\n\n"
        "ℹ️ بعضی دستورها فقط برای مدیر اصلی فعال‌اند؛ دستورهای گروهی به مجوزهای لازم ربات در گروه نیاز دارند."
    )
    bot.reply_to(message, body)


if __name__ == "__main__":
    # Flask در یک Thread جدا اجرا می‌شود و polling ربات در Thread اصلی می‌ماند.
    flask_thread = threading.Thread(target=run_flask, daemon=True, name="render-flask")
    flask_thread.start()
    run_bot()
