import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import telebot
from bs4 import BeautifulSoup
from curl_cffi import requests
from telebot import types

# ==========================================
# НАЛАШТУВАННЯ
# ==========================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")


def load_local_env(path=ENV_PATH):
    """Load KEY=VALUE lines from a local .env file for PyCharm/local runs."""
    if not os.path.exists(path):
        return

    with open(path, "r", encoding="utf-8") as env_file:
        for line in env_file:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_local_env()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError(
        f"BOT_TOKEN is not set. Add BOT_TOKEN=your_token to {ENV_PATH} "
        "or set it in PyCharm Run Configuration -> Environment variables."
    )
if BOT_TOKEN == "put_your_new_bot_token_here" or ":" not in BOT_TOKEN:
    raise RuntimeError(
        f"BOT_TOKEN in {ENV_PATH} is still a placeholder or invalid. "
        "Paste your real BotFather token there, for example 123456:ABC..."
    )

bot = telebot.TeleBot(BOT_TOKEN)

DB_PATH = os.getenv("DB_PATH", "bot.db")
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "3600"))
PAYMENT_CURRENCY = os.getenv("PAYMENT_CURRENCY", "XTR")
PAYMENT_PROVIDER_TOKEN = os.getenv("PAYMENT_PROVIDER_TOKEN")
REFERRAL_REWARD_SLOTS = int(os.getenv("REFERRAL_REWARD_SLOTS", "1"))
REFERRAL_MAX_BONUS_SLOTS = int(os.getenv("REFERRAL_MAX_BONUS_SLOTS", "10"))
ADMIN_IDS = {
    int(admin_id.strip())
    for admin_id in os.getenv("ADMIN_IDS", "").split(",")
    if admin_id.strip().isdigit()
}

DB_LOCK = threading.Lock()


def parse_promo_codes(raw_codes):
    """Parse CODE:plan:days,CODE2:plan:days into a promo config dict."""
    promo_codes = {}
    for item in raw_codes.split(","):
        parts = [part.strip() for part in item.split(":")]
        if len(parts) != 3:
            continue
        code, plan_id, days = parts
        if not code or not days.isdigit():
            continue
        promo_codes[code.upper()] = {"plan": plan_id, "days": int(days)}
    return promo_codes


PROMO_CODES = parse_promo_codes(
    os.getenv("PROMO_CODES", "LAUNCH7:premium:7,STUDENT7:premium:7,PARTNER7:premium:7")
)

PLAN_CONFIG = {
    "free": {
        "name": "Free",
        "limit": 3,
        "days": None,
        "price": 0,
        "description": "3 товари, перевірка раз на годину",
    },
    "premium": {
        "name": "Premium",
        "limit": 15,
        "days": 30,
        "price": int(os.getenv("PREMIUM_PRICE_STARS", "199")),
        "description": "15 товарів на 30 днів",
    },
    "business": {
        "name": "Business",
        "limit": 30,
        "days": 30,
        "price": int(os.getenv("BUSINESS_PRICE_STARS", "499")),
        "description": "30 товарів на 30 днів",
    },
}

# Тимчасові стани користувачів для меню
user_states = {}

# Доступні магазини за категоріями (для ручного вибору)
SHOPS_DATA = {
    "electronics": {
        "name": "💻 Електроніка",
        "items": {
            "rozetka": "Rozetka",
            "ekatalog": "e-Katalog",
            "moyo": "Moyo.ua",
        },
    },
    "beauty": {
        "name": "💄 Косметика та краса",
        "items": {
            "makeup": "Makeup.com.ua",
            "eva": "Eva.ua",
        },
    },
}


# ==========================================
# БАЗА ДАНИХ
# ==========================================

def utc_now():
    return datetime.now(timezone.utc)


def to_iso(dt):
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat()


def from_iso(value):
    if not value:
        return None
    return datetime.fromisoformat(value)


def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_column(conn, table, column, definition):
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db():
    with DB_LOCK, db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER PRIMARY KEY,
                plan TEXT NOT NULL DEFAULT 'free',
                premium_until TEXT,
                referral_bonus_slots INTEGER NOT NULL DEFAULT 0,
                referred_by INTEGER,
                created_at TEXT NOT NULL
            )
            """
        )
        ensure_column(conn, "users", "referral_bonus_slots", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "users", "referred_by", "INTEGER")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS monitors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                site TEXT NOT NULL,
                url TEXT NOT NULL,
                last_price REAL NOT NULL,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(chat_id, url)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                plan TEXT NOT NULL,
                payload TEXT NOT NULL UNIQUE,
                amount INTEGER NOT NULL,
                currency TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_chat_id INTEGER NOT NULL,
                referred_chat_id INTEGER NOT NULL UNIQUE,
                reward_slots INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS promo_redemptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                code TEXT NOT NULL,
                plan TEXT NOT NULL,
                days INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(chat_id, code)
            )
            """
        )


def ensure_user(chat_id):
    with DB_LOCK, db_connect() as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO users (chat_id, plan, premium_until, referral_bonus_slots, referred_by, created_at)
            VALUES (?, 'free', NULL, 0, NULL, ?)
            """,
            (chat_id, to_iso(utc_now())),
        )
    return cursor.rowcount == 1


def get_user(chat_id):
    ensure_user(chat_id)
    with DB_LOCK, db_connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,)).fetchone()
    return row


def get_effective_plan(chat_id):
    user = get_user(chat_id)
    plan = user["plan"]
    premium_until = from_iso(user["premium_until"])
    if plan != "free" and premium_until and premium_until > utc_now():
        return plan

    if plan != "free":
        with DB_LOCK, db_connect() as conn:
            conn.execute(
                "UPDATE users SET plan = 'free', premium_until = NULL WHERE chat_id = ?",
                (chat_id,),
            )
    return "free"


def get_plan_limit(chat_id):
    user = get_user(chat_id)
    base_limit = PLAN_CONFIG[get_effective_plan(chat_id)]["limit"]
    return base_limit + user["referral_bonus_slots"]


def register_referral(new_chat_id, referrer_chat_id):
    if new_chat_id == referrer_chat_id:
        return False

    ensure_user(referrer_chat_id)
    with DB_LOCK, db_connect() as conn:
        existing = conn.execute(
            "SELECT referred_by FROM users WHERE chat_id = ?",
            (new_chat_id,),
        ).fetchone()
        if not existing or existing["referred_by"] is not None:
            return False

        current_bonus = conn.execute(
            "SELECT referral_bonus_slots FROM users WHERE chat_id = ?",
            (referrer_chat_id,),
        ).fetchone()["referral_bonus_slots"]
        if current_bonus >= REFERRAL_MAX_BONUS_SLOTS:
            reward_slots = 0
        else:
            reward_slots = min(REFERRAL_REWARD_SLOTS, REFERRAL_MAX_BONUS_SLOTS - current_bonus)

        conn.execute(
            "UPDATE users SET referred_by = ? WHERE chat_id = ?",
            (referrer_chat_id, new_chat_id),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO referrals (referrer_chat_id, referred_chat_id, reward_slots, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (referrer_chat_id, new_chat_id, reward_slots, to_iso(utc_now())),
        )
        if reward_slots:
            conn.execute(
                """
                UPDATE users
                SET referral_bonus_slots = referral_bonus_slots + ?
                WHERE chat_id = ?
                """,
                (reward_slots, referrer_chat_id),
            )
    return reward_slots > 0


def get_referral_stats(chat_id):
    user = get_user(chat_id)
    with DB_LOCK, db_connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS total FROM referrals WHERE referrer_chat_id = ?",
            (chat_id,),
        ).fetchone()
    return row["total"], user["referral_bonus_slots"]


def count_monitors(chat_id):
    with DB_LOCK, db_connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS total FROM monitors WHERE chat_id = ?", (chat_id,)).fetchone()
    return row["total"]


def get_monitors(chat_id):
    with DB_LOCK, db_connect() as conn:
        rows = conn.execute(
            "SELECT * FROM monitors WHERE chat_id = ? ORDER BY id DESC",
            (chat_id,),
        ).fetchall()
    return rows


def insert_monitor(chat_id, site, url, price, title):
    with DB_LOCK, db_connect() as conn:
        conn.execute(
            """
            INSERT INTO monitors (chat_id, site, url, last_price, title, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (chat_id, site, url, price, title, to_iso(utc_now())),
        )


def delete_monitor(chat_id, monitor_id):
    with DB_LOCK, db_connect() as conn:
        row = conn.execute(
            "SELECT * FROM monitors WHERE chat_id = ? AND id = ?",
            (chat_id, monitor_id),
        ).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM monitors WHERE chat_id = ? AND id = ?", (chat_id, monitor_id))
    return row


def get_all_monitors():
    with DB_LOCK, db_connect() as conn:
        rows = conn.execute("SELECT * FROM monitors ORDER BY chat_id, id").fetchall()
    return rows


def update_monitor_price(monitor_id, new_price):
    with DB_LOCK, db_connect() as conn:
        conn.execute("UPDATE monitors SET last_price = ? WHERE id = ?", (new_price, monitor_id))


def activate_plan(chat_id, plan_id):
    plan = PLAN_CONFIG[plan_id]
    premium_until = None
    if plan["days"]:
        current_user = get_user(chat_id)
        current_until = from_iso(current_user["premium_until"])
        start_at = current_until if current_until and current_until > utc_now() else utc_now()
        premium_until = start_at + timedelta(days=plan["days"])

    with DB_LOCK, db_connect() as conn:
        conn.execute(
            "UPDATE users SET plan = ?, premium_until = ? WHERE chat_id = ?",
            (plan_id, to_iso(premium_until), chat_id),
        )
    return premium_until


def activate_custom_plan(chat_id, plan_id, days):
    current_user = get_user(chat_id)
    current_until = from_iso(current_user["premium_until"])
    start_at = current_until if current_until and current_until > utc_now() else utc_now()
    premium_until = start_at + timedelta(days=days)

    with DB_LOCK, db_connect() as conn:
        conn.execute(
            "UPDATE users SET plan = ?, premium_until = ? WHERE chat_id = ?",
            (plan_id, to_iso(premium_until), chat_id),
        )
    return premium_until


def redeem_promo_code(chat_id, code):
    code = code.strip().upper()
    promo = PROMO_CODES.get(code)
    if not promo:
        return None, "unknown"
    if promo["plan"] not in PLAN_CONFIG or promo["plan"] == "free":
        return None, "invalid"

    ensure_user(chat_id)
    with DB_LOCK, db_connect() as conn:
        already_used = conn.execute(
            "SELECT id FROM promo_redemptions WHERE chat_id = ? AND code = ?",
            (chat_id, code),
        ).fetchone()
        if already_used:
            return None, "used"
        conn.execute(
            """
            INSERT INTO promo_redemptions (chat_id, code, plan, days, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (chat_id, code, promo["plan"], promo["days"], to_iso(utc_now())),
        )

    premium_until = activate_custom_plan(chat_id, promo["plan"], promo["days"])
    return premium_until, promo


def create_payment(chat_id, plan_id, payload):
    plan = PLAN_CONFIG[plan_id]
    with DB_LOCK, db_connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO payments (chat_id, plan, payload, amount, currency, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?)
            """,
            (chat_id, plan_id, payload, plan["price"], PAYMENT_CURRENCY, to_iso(utc_now())),
        )


def complete_payment(payload):
    with DB_LOCK, db_connect() as conn:
        payment = conn.execute("SELECT * FROM payments WHERE payload = ?", (payload,)).fetchone()
        if not payment:
            return None
        conn.execute("UPDATE payments SET status = 'paid' WHERE payload = ?", (payload,))
    premium_until = activate_plan(payment["chat_id"], payment["plan"])
    return payment, premium_until


# ==========================================
# ФУНКЦІЇ ПАРСИНГУ (SCRAPING)
# ==========================================

def parse_rozetka(url):
    """Парсер для Rozetka."""
    try:
        response = requests.get(url, impersonate="chrome120", timeout=15)
        if response.status_code != 200:
            return None
        soup = BeautifulSoup(response.text, "html.parser")

        title_elem = soup.find("h1")
        title = title_elem.text.strip() if title_elem else "Товар з Rozetka"

        scripts = soup.find_all("script", type="application/ld+json")
        for script in scripts:
            try:
                data = json.loads(script.string)
                if data.get("@type") == "Product" and "offers" in data and "price" in data["offers"]:
                    return float(data["offers"]["price"]), title
            except Exception:
                continue

        price_element = soup.find("p", class_="product-price__big")
        if price_element:
            clean_price = "".join(filter(str.isdigit, price_element.text))
            return float(clean_price), title

        return None
    except Exception as e:
        print(f"Помилка парсингу Rozetka: {e}")
        return None


def parse_ekatalog(url):
    """Парсер для e-Katalog."""
    try:
        response = requests.get(url, impersonate="chrome120", timeout=15)
        if response.status_code != 200:
            return None
        soup = BeautifulSoup(response.text, "html.parser")

        title_elem = soup.find("h1")
        title = title_elem.text.strip() if title_elem else "Товар з e-Katalog"

        price_container = soup.find("div", class_="desc-big-price")
        if price_container:
            min_price_element = price_container.find("span")
            if min_price_element:
                clean_price = "".join(filter(str.isdigit, min_price_element.text))
                return float(clean_price), title

        return None
    except Exception as e:
        print(f"Помилка парсингу e-Katalog: {e}")
        return None


def parse_moyo(url):
    """Парсер для e-Katalog."""
    try:
        response = requests.get(url, impersonate="chrome120", timeout=15)
        if response.status_code != 200:
            return None
        soup = BeautifulSoup(response.text, "html.parser")

        title_elem = soup.find("h1")
        title = title_elem.text.strip() if title_elem else "Товар з moyo"

        price_container = soup.find("div", class_="product_price_current")
        if price_container:
            min_price_element = price_container.find("span")
            if min_price_element:
                clean_price = "".join(filter(str.isdigit, min_price_element.text))
                return float(clean_price), title

        return None
    except Exception as e:
        print(f"Помилка парсингу moyo: {e}")
        return None


def parse_generic_product(url, fallback_title):
    """Fallback parser for stores that expose price in common meta/schema fields."""
    try:
        response = requests.get(url, impersonate="chrome120", timeout=15)
        if response.status_code != 200:
            return None
        soup = BeautifulSoup(response.text, "html.parser")

        title_elem = soup.find("h1")
        title = title_elem.text.strip() if title_elem else fallback_title

        scripts = soup.find_all("script", type="application/ld+json")
        for script in scripts:
            try:
                data = json.loads(script.string)
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if item.get("@type") == "Product" and "offers" in item:
                        offers = item["offers"]
                        if isinstance(offers, list):
                            offers = offers[0]
                        if "price" in offers:
                            return float(str(offers["price"]).replace(",", ".")), title
            except Exception:
                continue

        for attrs in (
            {"property": "product:price:amount"},
            {"property": "og:price:amount"},
            {"itemprop": "price"},
        ):
            meta_price = soup.find("meta", attrs=attrs)
            if meta_price and meta_price.get("content"):
                return float(meta_price["content"].replace(",", ".")), title

        return None
    except Exception as e:
        print(f"Помилка generic-парсингу: {e}")
        return None


def get_price_and_title(site, url):
    """Диспетчер для викликання потрібного парсера."""
    if site == "rozetka":
        return parse_rozetka(url)
    if site == "ekatalog":
        return parse_ekatalog(url)
    if site == "moyo":
        return parse_moyo(url)
    if site == "makeup":
        return parse_generic_product(url, "Товар з Makeup")
    if site == "eva":
        return parse_generic_product(url, "Товар з Eva")
    return None


# ==========================================
# АВТОВИЗНАЧЕННЯ САЙТУ ПО ПОСИЛАННЮ
# ==========================================

def detect_site(url):
    """Аналізує посилання та повертає унікальний ID сайту."""
    url = url.lower()
    if "rozetka.com.ua" in url or "rozetka.ua" in url:
        return "rozetka"
    if "ek.ua" in url or "e-katalog" in url:
        return "ekatalog"
    if "moyo.ua" in url:
        return "moyo"
    if "makeup.com.ua" in url:
        return "makeup"
    if "eva.ua" in url:
        return "eva"
    return None


# ==========================================
# МЕНЮ ТА ПЛАНИ
# ==========================================

def build_main_menu():
    main_markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    main_markup.add(
        types.KeyboardButton("📋 Мої товари"),
        types.KeyboardButton("🗂 Вибрати магазин вручну"),
        types.KeyboardButton("⭐ Плани"),
        types.KeyboardButton("🎁 Запросити друга"),
    )
    return main_markup


def build_plans_markup():
    markup = types.InlineKeyboardMarkup(row_width=1)
    for plan_id, plan in PLAN_CONFIG.items():
        if plan_id == "free":
            continue
        markup.add(
            types.InlineKeyboardButton(
                f"Купити {plan['name']} - {plan['price']} {PAYMENT_CURRENCY}",
                callback_data=f"buy_{plan_id}",
            )
        )
    return markup


def plan_status_text(chat_id):
    user = get_user(chat_id)
    plan_id = get_effective_plan(chat_id)
    plan = PLAN_CONFIG[plan_id]
    premium_until = from_iso(user["premium_until"])
    expires = ""
    if plan_id != "free" and premium_until:
        expires = f"\nДіє до: {premium_until.strftime('%Y-%m-%d %H:%M UTC')}"

    return (
        f"Ваш план: **{plan['name']}**\n"
        f"Базовий ліміт: **{plan['limit']}**\n"
        f"Бонус за друзів: **+{user['referral_bonus_slots']}**\n"
        f"Загальний ліміт: **{get_plan_limit(chat_id)}**\n"
        f"Зараз додано: **{count_monitors(chat_id)}**{expires}"
    )


def send_plans(chat_id):
    lines = ["⭐ **Плани монетизації**\n"]
    for plan_id, plan in PLAN_CONFIG.items():
        price_text = "безкоштовно" if plan["price"] == 0 else f"{plan['price']} {PAYMENT_CURRENCY}"
        lines.append(f"**{plan['name']}** — {price_text}\n{plan['description']}")
    lines.append("\nОплата відкриває більший ліміт товарів для моніторингу.")

    bot.send_message(chat_id, "\n\n".join(lines), parse_mode="Markdown", reply_markup=build_plans_markup())


def send_upgrade_hint(chat_id):
    limit = get_plan_limit(chat_id)
    bot.send_message(
        chat_id,
        f"🔒 Ви досягли ліміту плану: {limit} товарів.\n"
        "Оберіть Premium або Business, щоб додати більше товарів.",
        reply_markup=build_plans_markup(),
    )


def send_referral_info(chat_id):
    referrals_count, bonus_slots = get_referral_stats(chat_id)
    bot_info = bot.get_me()
    referral_link = f"https://t.me/{bot_info.username}?start=ref_{chat_id}"
    text = (
        "🎁 **Запросіть друзів і отримайте більше місць**\n\n"
        f"За кожного друга: **+{REFERRAL_REWARD_SLOTS} товар**\n"
        f"Максимальний бонус: **+{REFERRAL_MAX_BONUS_SLOTS} товарів**\n\n"
        f"Запрошено друзів: **{referrals_count}**\n"
        f"Ваш бонус зараз: **+{bonus_slots}**\n\n"
        f"Ваше посилання:\n`{referral_link}`"
    )
    bot.send_message(chat_id, text, parse_mode="Markdown", disable_web_page_preview=True)


def send_promo_help(chat_id):
    bot.send_message(
        chat_id,
        "🎟 Маєте промокод від каналу або партнера?\n\n"
        "Введіть його так:\n"
        "`/promo STUDENT7`\n\n"
        "Або відкрийте партнерське посилання виду:\n"
        "`https://t.me/your_bot?start=promo_STUDENT7`",
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


# ==========================================
# ОБРОБНИКИ КОМАНД ТА КЛАВІАТУРИ TELEGRAM
# ==========================================

@bot.message_handler(commands=["start", "help"])
def send_welcome(message):
    """Команда /start або /help — створює головне меню кнопок."""
    is_new_user = ensure_user(message.chat.id)
    parts = message.text.split(maxsplit=1)
    start_payload = parts[1].strip() if len(parts) > 1 else ""

    if is_new_user and start_payload.startswith("ref_"):
        referrer_value = start_payload.replace("ref_", "", 1)
        if referrer_value.isdigit() and register_referral(message.chat.id, int(referrer_value)):
            try:
                bot.send_message(
                    int(referrer_value),
                    f"🎁 Новий користувач приєднався за вашим посиланням. Ви отримали +{REFERRAL_REWARD_SLOTS} слот!",
                )
            except Exception as e:
                print(f"Не вдалося повідомити реферера {referrer_value}: {e}")

    if start_payload.startswith("promo_"):
        code = start_payload.replace("promo_", "", 1)
        premium_until, promo = redeem_promo_code(message.chat.id, code)
        if promo not in ["unknown", "used", "invalid"]:
            bot.send_message(
                message.chat.id,
                f"🎟 Промокод активовано! План **{PLAN_CONFIG[promo['plan']]['name']}** діє до "
                f"{premium_until.strftime('%Y-%m-%d')}.",
                parse_mode="Markdown",
            )

    text = (
        "👋 **Привіт! Я бот для відстежування цін на товари.**\n\n"
        "🚀 **Як користуватися:**\n"
        "Надішліть мені пряме посилання на товар, а я збережу його та повідомлю, "
        "коли ціна зміниться.\n\n"
        "Безкоштовний план дозволяє відстежувати 3 товари. Більше можливостей: /plans"
    )
    bot.send_message(message.chat.id, text, parse_mode="Markdown", reply_markup=build_main_menu())


@bot.message_handler(func=lambda message: message.text in ["📋 Мої товари", "🗂 Вибрати магазин вручну", "⭐ Плани", "🎁 Запросити друга"])
def handle_reply_keyboard(message):
    """Обробка великих кнопок нижнього меню."""
    if message.text == "📋 Мої товари":
        list_products(message)
    elif message.text == "🗂 Вибрати магазин вручну":
        show_categories(message)
    elif message.text == "⭐ Плани":
        send_plans(message.chat.id)
    elif message.text == "🎁 Запросити друга":
        send_referral_info(message.chat.id)


@bot.message_handler(commands=["plans", "upgrade"])
def plans_command(message):
    ensure_user(message.chat.id)
    send_plans(message.chat.id)


@bot.message_handler(commands=["referral", "invite"])
def referral_command(message):
    ensure_user(message.chat.id)
    send_referral_info(message.chat.id)


@bot.message_handler(commands=["promo"])
def promo_command(message):
    ensure_user(message.chat.id)
    parts = message.text.split(maxsplit=1)
    if len(parts) == 1:
        send_promo_help(message.chat.id)
        return

    code = parts[1].strip()
    premium_until, result = redeem_promo_code(message.chat.id, code)
    if result == "unknown":
        bot.send_message(message.chat.id, "❌ Такого промокоду немає або він уже неактивний.")
        return
    if result == "used":
        bot.send_message(message.chat.id, "ℹ️ Ви вже використали цей промокод.")
        return
    if result == "invalid":
        bot.send_message(message.chat.id, "❌ Цей промокод налаштований некоректно.")
        return

    bot.send_message(
        message.chat.id,
        f"🎟 Промокод активовано! План **{PLAN_CONFIG[result['plan']]['name']}** діє до "
        f"{premium_until.strftime('%Y-%m-%d')}.",
        parse_mode="Markdown",
    )


@bot.message_handler(commands=["subscription"])
def subscription_command(message):
    ensure_user(message.chat.id)
    bot.send_message(message.chat.id, plan_status_text(message.chat.id), parse_mode="Markdown")


@bot.message_handler(commands=["grant"])
def grant_command(message):
    """Admin-only: /grant CHAT_ID PLAN_ID. Useful while payment setup is being approved/tested."""
    if message.chat.id not in ADMIN_IDS:
        bot.send_message(message.chat.id, "Ця команда доступна лише адміністратору.")
        return

    parts = message.text.split()
    if len(parts) != 3 or parts[2] not in PLAN_CONFIG:
        bot.send_message(message.chat.id, "Формат: /grant CHAT_ID premium або /grant CHAT_ID business")
        return

    target_chat_id = int(parts[1])
    ensure_user(target_chat_id)
    premium_until = activate_plan(target_chat_id, parts[2])
    until_text = premium_until.strftime("%Y-%m-%d") if premium_until else "безстроково"
    bot.send_message(message.chat.id, f"План {parts[2]} активовано для {target_chat_id} до {until_text}.")
    bot.send_message(target_chat_id, f"✅ Ваш план оновлено до {PLAN_CONFIG[parts[2]]['name']}!")


@bot.message_handler(commands=["list"])
def list_products(message):
    """Виводить список усіх активних моніторингів користувача."""
    chat_id = message.chat.id
    ensure_user(chat_id)
    products = get_monitors(chat_id)

    if not products:
        bot.send_message(chat_id, "📭 Ваш список моніторингу порожній. Просто надішліть мені посилання на товар!")
        return

    bot.send_message(chat_id, "📋 **Ваші товари на моніторингу:**", parse_mode="Markdown")

    for item in products:
        markup = types.InlineKeyboardMarkup()
        btn_delete = types.InlineKeyboardButton("❌ Видалити цей товар", callback_data=f"del_{item['id']}")
        markup.add(btn_delete)

        text = (
            f"📦 **{item['title']}**\n"
            f"🏪 Магазин: {item['site'].upper()}\n"
            f"💰 Поточна ціна: **{item['last_price']} грн**\n"
            f"🔗 [Посилання на товар]({item['url']})"
        )
        bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=markup, disable_web_page_preview=True)


@bot.callback_query_handler(func=lambda call: call.data.startswith("del_"))
def delete_product(call):
    """Обробка видалення товару зі списку користувача."""
    monitor_id = int(call.data.split("_")[1])
    chat_id = call.message.chat.id
    removed_item = delete_monitor(chat_id, monitor_id)

    if removed_item:
        bot.answer_callback_query(call.id, "Товар видалено!")
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=call.message.message_id,
            text=f"🗑 **Товар видалено з моніторингу:**\n_{removed_item['title']}_",
            parse_mode="Markdown",
        )
    else:
        bot.answer_callback_query(call.id, "Помилка видалення або товар вже видалено.")


@bot.callback_query_handler(func=lambda call: call.data.startswith("buy_"))
def buy_plan(call):
    plan_id = call.data.split("_")[1]
    chat_id = call.message.chat.id
    ensure_user(chat_id)

    if plan_id not in PLAN_CONFIG or plan_id == "free":
        bot.answer_callback_query(call.id, "Невідомий план.")
        return

    plan = PLAN_CONFIG[plan_id]
    payload = f"plan:{chat_id}:{plan_id}:{int(time.time())}"
    create_payment(chat_id, plan_id, payload)

    try:
        bot.send_invoice(
            chat_id=chat_id,
            title=f"{plan['name']} на {plan['days']} днів",
            description=plan["description"],
            invoice_payload=payload,
            provider_token=PAYMENT_PROVIDER_TOKEN,
            currency=PAYMENT_CURRENCY,
            prices=[types.LabeledPrice(label=plan["name"], amount=plan["price"])],
        )
        bot.answer_callback_query(call.id)
    except Exception as e:
        print(f"Помилка створення інвойсу: {e}")
        bot.answer_callback_query(call.id, "Не вдалося відкрити оплату.")
        bot.send_message(
            chat_id,
            "Оплата ще не налаштована. Адміністратор може тимчасово активувати план командою /grant.",
        )


@bot.pre_checkout_query_handler(func=lambda query: True)
def checkout(pre_checkout_query):
    if not pre_checkout_query.invoice_payload.startswith("plan:"):
        bot.answer_pre_checkout_query(pre_checkout_query.id, ok=False, error_message="Некоректний платіж.")
        return
    bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


@bot.message_handler(content_types=["successful_payment"])
def got_payment(message):
    payload = message.successful_payment.invoice_payload
    result = complete_payment(payload)
    if not result:
        bot.send_message(message.chat.id, "Платіж отримано, але план не знайдено. Напишіть адміністратору.")
        return

    payment, premium_until = result
    until_text = premium_until.strftime("%Y-%m-%d") if premium_until else "безстроково"
    bot.send_message(
        message.chat.id,
        f"✅ Дякую за оплату! План **{PLAN_CONFIG[payment['plan']]['name']}** активовано до {until_text}.",
        parse_mode="Markdown",
    )


# ==========================================
# МЕНЮ ДЛЯ РУЧНОГО ВИБОРУ
# ==========================================

@bot.message_handler(commands=["track"])
def show_categories(message):
    """Команда /track — крок 1 ручного вибору."""
    markup = types.InlineKeyboardMarkup(row_width=2)
    buttons = [types.InlineKeyboardButton(info["name"], callback_data=f"cat_{id}") for id, info in SHOPS_DATA.items()]
    markup.add(*buttons)
    bot.send_message(message.chat.id, "📁 Оберіть категорію товарів:", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("cat_"))
def show_shops_by_category(call):
    """Ручний вибір — крок 2 (вибір магазину)."""
    category_id = call.data.split("_")[1]
    chat_id = call.message.chat.id

    markup = types.InlineKeyboardMarkup(row_width=2)
    buttons = [
        types.InlineKeyboardButton(name, callback_data=f"site_{id}")
        for id, name in SHOPS_DATA[category_id]["items"].items()
    ]
    markup.add(*buttons)
    markup.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="back_to_cats"))

    bot.edit_message_text(
        chat_id=chat_id,
        message_id=call.message.message_id,
        text="🛒 Оберіть магазин з категорії:",
        reply_markup=markup,
    )


@bot.callback_query_handler(func=lambda call: call.data == "back_to_cats")
def back_to_cats(call):
    """Повернення назад до категорій."""
    markup = types.InlineKeyboardMarkup(row_width=2)
    buttons = [types.InlineKeyboardButton(info["name"], callback_data=f"cat_{id}") for id, info in SHOPS_DATA.items()]
    markup.add(*buttons)
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text="📁 Оберіть категорію товарів:",
        reply_markup=markup,
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("site_"))
def handle_manual_site(call):
    """Користувач обрав сайт вручну, просимо лінк."""
    selected_site = call.data.split("_")[1]
    chat_id = call.message.chat.id
    user_states[chat_id] = {"site": selected_site}

    bot.edit_message_text(
        chat_id=chat_id,
        message_id=call.message.message_id,
        text="Записав! Тепер надішліть мені пряме посилання на товар:",
    )
    bot.register_next_step_handler(call.message, process_url_step)


def process_url_step(message):
    """Обробка посилання після ручного вибору сайту."""
    chat_id = message.chat.id
    url = message.text.strip()
    if chat_id in user_states and "site" in user_states[chat_id]:
        site = user_states[chat_id]["site"]
        add_product_to_monitor(chat_id, site, url)
        user_states.pop(chat_id, None)


# ==========================================
# ОБРОБКА ПРЯМОГО НАДСИЛАННЯ ПОСИЛАННЯ
# ==========================================

@bot.message_handler(func=lambda message: message.text and message.text.startswith("http"))
def handle_direct_link(message):
    """Головна фіча: користувач просто кинув посилання в чат."""
    url = message.text.strip()
    chat_id = message.chat.id

    site = detect_site(url)

    if site:
        add_product_to_monitor(chat_id, site, url)
    else:
        user_states[chat_id] = {"url": url}

        markup = types.InlineKeyboardMarkup(row_width=1)
        btn_select = types.InlineKeyboardButton("🗂 Вибрати магазин вручну", callback_data="manual_select_after_fail")
        markup.add(btn_select)

        bot.send_message(
            chat_id,
            "🤷‍♂️ Я не зміг автоматично розпізнати цей сайт.\n"
            "Можливо, цей магазин є у нашому списку, але посилання нестандартне? Ви можете вказати сайт вручну.",
            reply_markup=markup,
        )


@bot.callback_query_handler(func=lambda call: call.data == "manual_select_after_fail")
def manual_select_after_fail(call):
    chat_id = call.message.chat.id
    markup = types.InlineKeyboardMarkup(row_width=2)
    buttons = [types.InlineKeyboardButton(info["name"], callback_data=f"failcat_{id}") for id, info in SHOPS_DATA.items()]
    markup.add(*buttons)
    bot.edit_message_text(
        chat_id=chat_id,
        message_id=call.message.message_id,
        text="Оберіть категорію для вашого посилання:",
        reply_markup=markup,
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("failcat_"))
def manual_shop_after_fail(call):
    category_id = call.data.split("_")[1]
    chat_id = call.message.chat.id
    markup = types.InlineKeyboardMarkup(row_width=2)
    buttons = [
        types.InlineKeyboardButton(name, callback_data=f"failsite_{id}")
        for id, name in SHOPS_DATA[category_id]["items"].items()
    ]
    markup.add(*buttons)
    bot.edit_message_text(
        chat_id=chat_id,
        message_id=call.message.message_id,
        text="Оберіть магазин:",
        reply_markup=markup,
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("failsite_"))
def process_fail_site(call):
    site = call.data.split("_")[1]
    chat_id = call.message.chat.id

    if chat_id in user_states and "url" in user_states[chat_id]:
        url = user_states[chat_id]["url"]
        bot.delete_message(chat_id, call.message.message_id)
        add_product_to_monitor(chat_id, site, url)
        user_states.pop(chat_id, None)
    else:
        bot.send_message(chat_id, "Сталася помилка сесії. Надішліть посилання знову.")


# ==========================================
# ГОЛОВНА ФУНКЦІЯ ДОДАВАННЯ В БАЗУ ДАНИХ
# ==========================================

def add_product_to_monitor(chat_id, site, url):
    ensure_user(chat_id)

    if count_monitors(chat_id) >= get_plan_limit(chat_id):
        send_upgrade_hint(chat_id)
        return

    bot.send_message(chat_id, "⏳ Зчитую актуальну ціну товару... Зачекайте.")
    res = get_price_and_title(site, url)

    if res is None:
        bot.send_message(chat_id, "❌ Не вдалося отримати ціну. Перевірте, чи це дійсно пряме посилання на товар.")
        return

    price, title = res

    try:
        insert_monitor(chat_id, site, url, price, title)
    except sqlite3.IntegrityError:
        bot.send_message(chat_id, "ℹ️ Цей товар уже є у вашому списку моніторингу.")
        return

    bot.send_message(
        chat_id,
        f"✅ **Товар успішно додано до моніторингу!**\n\n"
        f"📦 {title}\n"
        f"💰 Поточна ціна: **{price} грн**\n\n"
        f"Я надішлю сповіщення, щойно ціна зміниться! Переглянути список: /list",
        parse_mode="Markdown",
    )


# ==========================================
# ФОНОВИЙ ЦИКЛ ПЕРЕВІРКИ ЦІН
# ==========================================

def price_checker_loop():
    while True:
        print("Фонова перевірка всіх збережених товарів розпочата...")
        for item in get_all_monitors():
            site = item["site"]
            url = item["url"]
            old_price = item["last_price"]

            res = get_price_and_title(site, url)
            if res is not None:
                new_price, _ = res
                if new_price != old_price:
                    update_monitor_price(item["id"], new_price)

                    alert_text = (
                        f"🚨 **Ціна змінилася на {site.upper()}!**\n"
                        f"📦 *{item['title']}*\n\n"
                        f"📉 Стара ціна: {old_price} грн\n"
                        f"📈 Нова ціна: **{new_price} грн**\n\n"
                        f"🔗 [Посилання на товар]({url})"
                    )
                    try:
                        bot.send_message(item["chat_id"], alert_text, parse_mode="Markdown")
                    except Exception as e:
                        print(f"Не вдалося надіслати повідомлення користувачу {item['chat_id']}: {e}")

        time.sleep(CHECK_INTERVAL_SECONDS)


# ==========================================
# ЗАПУСК БОТА ТА РЕЄСТРАЦІЯ МЕНЮ КОМАНД
# ==========================================

if __name__ == "__main__":
    print("Бот ініціалізується...")
    init_db()

    checker_thread = threading.Thread(target=price_checker_loop, daemon=True)
    checker_thread.start()

    bot.set_my_commands(
        [
            telebot.types.BotCommand("/start", "🚀 Перезапустити бота та відкрити кнопки"),
            telebot.types.BotCommand("/list", "📋 Мої товари на моніторингу"),
            telebot.types.BotCommand("/track", "🗂 Вибрати магазин вручну"),
            telebot.types.BotCommand("/plans", "⭐ Плани та оплата"),
            telebot.types.BotCommand("/subscription", "👤 Мій тариф"),
            telebot.types.BotCommand("/referral", "🎁 Моє реферальне посилання"),
            telebot.types.BotCommand("/promo", "🎟 Активувати промокод"),
            telebot.types.BotCommand("/help", "❓ Як користуватися ботом"),
        ]
    )

    print("Бот успішно запущений та готовий до роботи!")
    bot.infinity_polling()
