"""
Финансовый AI-агент Telegram бот v5.2
"""
import os, logging, tempfile, json, re, asyncio
from datetime import datetime, timedelta, time as dtime
from collections import defaultdict
from dotenv import load_dotenv

load_dotenv()

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from groq import Groq
from zoneinfo import ZoneInfo
KYIV_TZ = ZoneInfo("Europe/Kiev")
import gspread
from google.oauth2.service_account import Credentials
import httpx

# ── ENV ─────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY      = os.getenv("GROQ_API_KEY")
GOOGLE_SHEET_ID   = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS")
CHAT_ID           = os.getenv("CHAT_ID")

groq_client = Groq(api_key=GROQ_API_KEY)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── КОНСТАНТЫ ────────────────────────────────────────────────────────────────
DEFAULT_CATEGORIES = ["Еда / продукты", "Транспорт", "Развлечения", "Здоровье / аптека", "Никотин", "Другое"]

EMOJI_MAP = {
    "Еда / продукты": "🍔", "Транспорт": "🚗", "Развлечения": "🎮",
    "Здоровье / аптека": "💊", "Никотин": "🚬", "Другое": "📦",
    "Одежда": "👕", "Коммунальные": "🏠", "Подписки": "📺",
    "Спорт": "💪", "Образование": "📚", "Путешествия": "✈️",
}

CURRENCY_SYMBOLS = {"UAH": "₴", "USD": "$", "EUR": "€"}

MONTH_NAMES     = ["Январь","Февраль","Март","Апрель","Май","Июнь",
                   "Июль","Август","Сентябрь","Октябрь","Ноябрь","Декабрь"]
MONTH_NAMES_GEN = ["января","февраля","марта","апреля","мая","июня",
                   "июля","августа","сентября","октября","ноября","декабря"]
DAY_NAMES = ["Понедельник","Вторник","Среда","Четверг","Пятница","Суббота","Воскресенье"]

EQUIVALENTS = [
    (2000,"🍕 100 пицц"),(3000,"🎮 3 игры в Steam"),(5000,"✈️ билет в Европу"),
    (8000,"📱 бюджетный смартфон"),(15000,"💻 ноутбук"),(25000,"📱 iPhone"),
    (40000,"🏖 неделя на море"),(60000,"🚗 взнос на авто"),(100000,"🌍 отпуск мечты"),
]

DAYS_LABELS = {1:"1 день",3:"3 дня",7:"1 неделю",14:"2 недели",21:"3 недели",30:"1 месяц"}

# ── GOOGLE SHEETS — единый клиент с кэшем ────────────────────────────────────
_gs_client = None
_spreadsheet = None
_records_cache: dict = {}
CACHE_TTL = 60

def _get_gs_client():
    global _gs_client
    if _gs_client is None:
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = (Credentials.from_service_account_info(json.loads(GOOGLE_CREDENTIALS), scopes=scopes)
                 if GOOGLE_CREDENTIALS else
                 Credentials.from_service_account_file("credentials.json", scopes=scopes))
        _gs_client = gspread.authorize(creds)
    return _gs_client

def _get_spreadsheet():
    global _spreadsheet
    if _spreadsheet is None:
        _spreadsheet = _get_gs_client().open_by_key(GOOGLE_SHEET_ID)
    return _spreadsheet

def _get_worksheet(name="sheet1"):
    sp = _get_spreadsheet()
    return sp.sheet1 if name == "sheet1" else _worksheet_get_or_create(sp, name)

# FIX: правильный отступ и инвалидация кэша внутри функции
def _worksheet_get_or_create(sp, name):
    ws = None
    try:
        ws = sp.worksheet(name)
    except:
        ws = sp.add_worksheet(title=name, rows=200, cols=10)
    _records_cache.pop(name, None)
    return ws

def _cached_records(name="sheet1") -> list:
    now = datetime.now(KYIV_TZ).timestamp()
    if name in _records_cache:
        ts, data = _records_cache[name]
        if now - ts < CACHE_TTL:
            return data
    try:
        data = _get_worksheet(name).get_all_records()
        _records_cache[name] = (now, data)
        return data
    except Exception as e:
        logger.error(f"Cache read error ({name}): {e}")
        return []

# FIX: функция _invalidate была вызвана, но не определена
def _invalidate(name="sheet1"):
    _records_cache.pop(name, None)

# ── НАСТРОЙКИ ────────────────────────────────────────────────────────────────
_settings: dict = {}

def _settings_sheet():
    sh = _get_worksheet("Настройки")
    if not sh.get_all_values():
        sh.insert_row(["Ключ", "Значение"], 1)
    return sh

def load_settings():
    try:
        data = {r["Ключ"]: str(r["Значение"]) for r in _settings_sheet().get_all_records()
                if r.get("Ключ") and str(r.get("Значение","")) != ""}
        _settings.update(data)
        logger.info(f"Настройки загружены: {list(data.keys())}")
    except Exception as e:
        logger.error(f"load_settings: {e}")

def save_setting(key: str, value: str):
    _settings[key] = value
    try:
        sh = _settings_sheet()
        for i, r in enumerate(sh.get_all_records(), start=2):
            if r.get("Ключ") == key:
                sh.update_cell(i, 2, value); return
        sh.append_row([key, value])
    except Exception as e:
        logger.error(f"save_setting: {e}")

def get_setting(key: str, default=None):
    return _settings.get(key, default)

# ── КАТЕГОРИИ (пользовательские) ──────────────────────────────────────────────
_user_categories: list = []

def get_all_categories() -> list:
    base = list(DEFAULT_CATEGORIES)
    for uc in _user_categories:
        if uc not in base:
            base.append(uc)
    return base

def load_user_categories():
    val = get_setting("user_categories")
    if val:
        try: _user_categories.extend(json.loads(val))
        except: pass

def save_user_category(cat: str, emoji: str = ""):
    if cat not in _user_categories:
        _user_categories.append(cat)
        save_setting("user_categories", json.dumps(_user_categories))
    if cat not in EMOJI_MAP:
        EMOJI_MAP[cat] = emoji if emoji else get_category_emoji(cat)

# ── РАСХОДЫ ──────────────────────────────────────────────────────────────────
def get_sheet():
    sh = _get_worksheet("sheet1")
    if not sh.get_all_values():
        sh.insert_row(["Дата","Сумма (₴)","Категория","Описание","Исходный текст"], 1)
    return sh

def get_all_records() -> list:
    return _cached_records("sheet1")

def get_sum_key(records: list) -> str:
    if not records: return "Сумма (₴)"
    for k in records[0]:
        if "умм" in k or "сум" in k.lower(): return k
    return list(records[0].keys())[1]

def sum_records(records: list) -> float:
    k = get_sum_key(records)
    return sum(float(r[k]) for r in records if r.get(k))

def fix_cat(cat: str, desc: str = "") -> str:
    cats = get_all_categories()
    if cat in cats: return cat
    cat_low = cat.lower().strip()
    for c in cats:
        if cat_low in c.lower() or c.lower() in cat_low:
            return c
    return "Другое"

def validate_category(cat: str, desc: str = "") -> str:
    return fix_cat(cat, desc)

def save_expense(date, amount, category, description, raw_text):
    category = validate_category(category, description)
    get_sheet().append_row([date, amount, category, description, raw_text])
    _invalidate("sheet1")

# FIX: добавил .replace(tzinfo=KYIV_TZ) везде где парсим дату для сравнения с aware datetime
def records_for_month(month: int, year: int, all_recs=None) -> list:
    recs = all_recs or get_all_records()
    result = []
    for r in recs:
        try:
            d = datetime.strptime(r.get("Дата","")[:10], "%d.%m.%Y").replace(tzinfo=KYIV_TZ)
            if d.month == month and d.year == year: result.append(r)
        except: pass
    return result

def get_current_month_records() -> list:
    now = datetime.now(KYIV_TZ)
    return records_for_month(now.month, now.year)

def get_week_records() -> list:
    week_ago = datetime.now(KYIV_TZ) - timedelta(days=7)
    result = []
    for r in get_all_records():
        try:
            # FIX: добавил .replace(tzinfo=KYIV_TZ)
            d = datetime.strptime(r.get("Дата","")[:10], "%d.%m.%Y").replace(tzinfo=KYIV_TZ)
            if d >= week_ago:
                result.append(r)
        except: pass
    return result

def get_today_records() -> list:
    today = datetime.now(KYIV_TZ).strftime("%d.%m.%Y")
    return [r for r in get_all_records() if r.get("Дата","").startswith(today)]

# ── АНАЛИТИКА ────────────────────────────────────────────────────────────────
def analyze_records(records: list) -> dict | None:
    if not records: return None
    sk = get_sum_key(records)
    total = sum(float(r[sk]) for r in records if r.get(sk))
    by_cat = defaultdict(float)
    by_day = defaultdict(float)
    by_desc = defaultdict(lambda: {"count":0,"total":0.0})
    for r in records:
        amt = float(r[sk]) if r.get(sk) else 0
        cat = fix_cat(r.get("Категория","Другое"), r.get("Описание",""))
        desc = r.get("Описание","").lower()
        by_cat[cat] += amt
        try:
            # FIX: добавил .replace(tzinfo=KYIV_TZ)
            d = datetime.strptime(r.get("Дата","")[:10], "%d.%m.%Y").replace(tzinfo=KYIV_TZ)
            by_day[DAY_NAMES[d.weekday()]] += amt
        except: pass
        if desc:
            by_desc[desc]["count"] += 1
            by_desc[desc]["total"] += amt
    return {
        "total": total, "count": len(records),
        "by_category": dict(by_cat), "by_day": dict(by_day),
        "leaks": {k:v for k,v in by_desc.items() if v["count"] >= 3},
    }

def get_category_emoji(cat: str) -> str:
    if cat in EMOJI_MAP: return EMOJI_MAP[cat]
    low = cat.lower()
    emoji_hints = [
        (["еда","продукт","кафе","ресторан","обед","пицца","суши","фаст","кухн","готовк"],"🍔"),
        (["транспорт","такси","бензин","авто","машин","метро","автобус","мойк","запчаст"],"🚗"),
        (["развлеч","игр","кино","steam","netflix","spotify","боулинг","клуб","концерт"],"🎮"),
        (["здоров","аптека","врач","медиц","стоматолог","фитнес","спортзал","массаж","парикм","маникюр"],"💊"),
        (["никотин","снюс","вейп","сигарет","кальян","zyn","velo"],"🚬"),
        (["одежда","обувь","шопинг","одягу"],"👕"),
        (["коммунальн","квартир","жкх","аренд","ипотек","кварплат"],"🏠"),
        (["подписк","netflix","spotify","apple","google","онлайн"],"📺"),
        (["образован","курс","книг","учеб","навчан","репетитор","школ","универ","институт"],"📚"),
        (["путешеств","отпуск","авиа","билет","гостиниц","туризм","відпочин"],"✈️"),
        (["спорт","трениров","зал","фитнес","йога","бассейн"],"💪"),
        (["инвест","акци","крипт","накоплен","сбереж"],"📈"),
        (["кафе","ресторан","бар","суши","пицца"],"🍽"),
        (["подарок","подарун","праздник","день рожден"],"🎁"),
        (["ремонт","стройк","инструмент","материал"],"🔧"),
        (["связь","телефон","интернет","мобільн"],"📱"),
        (["домашн","питомц","кот","собак","животн"],"🐾"),
        (["красот","косметик","уход","парфюм"],"💄"),
        (["алкогол","пиво","вино","бар"],"🍺"),
    ]
    for keywords, emoji in emoji_hints:
        if any(k in low for k in keywords):
            return emoji
    first = low[0] if low else ""
    misc = {"а":"🅰️","б":"💼","в":"💡","г":"🏪","д":"📋","е":"⚡","з":"🔑","и":"💎","к":"🛍",
            "л":"🌿","м":"🎯","н":"🔔","о":"⭕","п":"📦","р":"🔴","с":"⭐","т":"🏷","у":"🎓",
            "ф":"🔵","х":"🏠","ц":"💰","ч":"⏰","ш":"🛒","э":"⚙️","ю":"🌍","я":"🧩"}
    return misc.get(first, "📦")

def fmt(amt: float) -> str:
    return f"{amt:,.0f}"

def month_name(n: int, gen: bool = False) -> str:
    mn = ["Январь","Февраль","Март","Апрель","Май","Июнь",
          "Июль","Август","Сентябрь","Октябрь","Ноябрь","Декабрь"]
    mn_gen = ["января","февраля","марта","апреля","мая","июня",
              "июля","августа","сентября","октября","ноября","декабря"]
    return (mn_gen if gen else mn)[n - 1]

# ── ПАМЯТЬ ───────────────────────────────────────────────────────────────────
memory: dict = {}

def get_budget_status(chat_id):
    val = get_setting(f"budget_{chat_id}")
    if not val: return None
    try: budget = float(val)
    except: return None
    recs = get_current_month_records()
    spent = sum_records(recs)
    left = budget - spent
    return {"budget":budget,"spent":spent,"left":left,"percent":min(int(spent/budget*100),100)}

# ── ЗАРПЛАТА ─────────────────────────────────────────────────────────────────
def get_salary_info(chat_id):
    val = get_setting(f"salary_{chat_id}")
    if not val: return None
    try: return json.loads(val)
    except: return None

def set_salary_info(chat_id, day, amount=None):
    save_setting(f"salary_{chat_id}", json.dumps({"day":day,"amount":amount}))

def build_salary_status(chat_id) -> str | None:
    info = get_salary_info(chat_id)
    if not info: return None
    now = datetime.now(KYIV_TZ)
    day = info["day"]
    amount = info.get("amount")
    if now.day < day:
        days_left = day - now.day
        next_sal = now.replace(day=day)
    else:
        nm = (now.replace(day=1) + timedelta(days=32)).replace(day=1)
        next_sal = nm.replace(day=min(day, 28))
        days_left = (next_sal - now).days
    spent = sum_records(get_current_month_records())
    lines = [f"💵 *День зарплаты — {day}-е число*\n"]
    if days_left == 0: lines.append("🎉 *Сегодня зарплата!*")
    elif days_left == 1: lines.append("⏰ *Завтра зарплата!*")
    else: lines.append(f"📅 До зарплаты: *{days_left} дней* ({next_sal.strftime('%d')} {month_name(next_sal.month, True)})")
    lines.append(f"\n💸 Потрачено: *{fmt(spent)} ₴*")
    if amount:
        left = amount - spent
        lines.append(f"💰 Зарплата: *{fmt(amount)} ₴*")
        lines.append(f"{'🟢' if left>0 else '🔴'} Осталось: *{fmt(left)} ₴*")
        if days_left > 0 and left > 0:
            lines.append(f"📊 Можно тратить: *{fmt(left/days_left)} ₴/день*")
    return "\n".join(lines)

# ── НАПОМИНАНИЯ ──────────────────────────────────────────────────────────────
def get_reminder_interval(chat_id) -> timedelta:
    val = get_setting(f"reminder_interval_{chat_id}")
    return timedelta(days=int(val)) if val else timedelta(weeks=2)

def set_reminder_interval(chat_id, days: int):
    save_setting(f"reminder_interval_{chat_id}", str(days))

def reminder_label(chat_id) -> str:
    val = get_setting(f"reminder_interval_{chat_id}")
    days = int(val) if val else 14
    return DAYS_LABELS.get(days, f"{days} дней")

# ── ФИНАНСОВЫЕ ЦЕЛИ ───────────────────────────────────────────────────────────
goals: dict = {}
goal_counter = [0]

def _goals_sheet():
    sh = _get_worksheet("Цели")
    if not sh.get_all_values():
        sh.insert_row(["ID","Название","Целевая сумма","Накоплено","Дата создания","Статус","Emoji"], 1)
    return sh

def load_goals():
    try:
        for r in _goals_sheet().get_all_records():
            if r.get("Статус") != "активна": continue
            gid = str(r["ID"])
            goals[gid] = {
                "name": r["Название"],
                "target": float(r.get("Целевая сумма", 0)),
                "saved": float(r.get("Накоплено", 0)),
                "date": r["Дата создания"],
                "emoji": r.get("Emoji","🎯"),
            }
            try: goal_counter[0] = max(goal_counter[0], int(r["ID"]))
            except: pass
    except Exception as e:
        logger.error(f"load_goals: {e}")

def save_goal_to_sheet(gid, name, target, saved, date, emoji="🎯"):
    try: _goals_sheet().append_row([gid, name, target, saved, date, "активна", emoji])
    except Exception as e: logger.error(f"save_goal: {e}")

def update_goal_saved(gid, saved):
    try:
        sh = _goals_sheet()
        for i, r in enumerate(sh.get_all_records(), start=2):
            if str(r.get("ID")) == str(gid):
                sh.update_cell(i, 4, saved); return
    except Exception as e: logger.error(f"update_goal_saved: {e}")

def close_goal(gid):
    try:
        sh = _goals_sheet()
        for i, r in enumerate(sh.get_all_records(), start=2):
            if str(r.get("ID")) == str(gid):
                sh.update_cell(i, 6, "выполнена"); return
    except Exception as e: logger.error(f"close_goal: {e}")

def build_goal_bar(saved, target) -> str:
    if target <= 0: return "░░░░░░░░░░"
    pct = min(saved / target, 1.0)
    filled = int(pct * 10)
    return "█" * filled + "░" * (10 - filled)

def build_goals_msg() -> str:
    if not goals: return "🎯 Целей нет.\n\nДобавь: «Цель iPhone 25000» или «Цель Отпуск 50000»"
    lines = ["🎯 *Мои финансовые цели:*\n"]
    for gid, g in goals.items():
        saved = g["saved"]
        target = g["target"]
        pct = min(int(saved / target * 100), 100) if target > 0 else 0
        bar = build_goal_bar(saved, target)
        left = max(target - saved, 0)
        lines.append(f"{g['emoji']} *{g['name']}*")
        lines.append(f"   [{bar}] {pct}%")
        lines.append(f"   {fmt(saved)} / {fmt(target)} ₴ (ещё {fmt(left)} ₴)")
        lines.append("")
    return "\n".join(lines)

# ── КУРС ВАЛЮТ НБУ (НОВОЕ) ────────────────────────────────────────────────────
_rates_cache: dict = {}
_rates_ts: float = 0
_mono_cache: dict = {}
_mono_ts: float = 0
_obmen_cache: dict = {}
_obmen_ts: float = 0

async def fetch_nbu_rates() -> dict:
    global _rates_cache, _rates_ts
    now = datetime.now(KYIV_TZ).timestamp()
    if _rates_cache and now - _rates_ts < 3600:
        return _rates_cache
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get("https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?json")
            data = resp.json()
            rates = {item["cc"]: item["rate"] for item in data}
            _rates_cache = rates
            _rates_ts = now
            return rates
    except Exception as e:
        logger.error(f"NBU rates: {e}")
        return {"USD": 41.5, "EUR": 44.0}

async def convert_to_uah(amount: float, currency: str) -> float:
    if currency == "UAH": return amount
    rates = await fetch_nbu_rates()
    rate = rates.get(currency, 1.0)
    return amount * rate

async def fetch_monobank_rates() -> dict:
    global _mono_cache, _mono_ts
    now = datetime.now(KYIV_TZ).timestamp()
    if _mono_cache and now - _mono_ts < 600:
        return _mono_cache
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get("https://api.monobank.ua/bank/currency")
            data = resp.json()
            result = {}
            for item in data:
                if item.get("currencyCodeB") == 980:
                    code_a = item.get("currencyCodeA")
                    if code_a == 840: result["USD"] = item
                    elif code_a == 978: result["EUR"] = item
            _mono_cache = result
            _mono_ts = now
            return result
    except Exception as e:
        logger.error(f"monobank rates: {e}")
        return {}


import httpx
from datetime import datetime

_obmen_cache = {}
_obmen_ts = 0


async def fetch_obmen_rates():
    global _obmen_cache, _obmen_ts

    now_ts = datetime.now(KYIV_TZ).timestamp()

    # Кэш на 10 минут
    if _obmen_cache and now_ts - _obmen_ts < 600:
        return _obmen_cache

    # --- 1. Пытаемся получить с obmen24 ---
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://obmen24.com.ua/api/quotes?city=lviv",
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "application/json"
                }
            )

            # ВАЖНО: проверка HTTP-статуса
            resp.raise_for_status()

            # Можно дополнительно проверить content-type
            content_type = resp.headers.get("content-type", "")
            if "application/json" not in content_type.lower():
                raise ValueError(f"Unexpected content-type: {content_type}")

            data = resp.json()

        result = {}

        for item in data:
            cur = item.get("currency")
            if cur in ("USD", "EUR"):
                result[cur] = {
                    "buy": float(item.get("buy", 0) or 0),
                    "sale": float(item.get("sale", 0) or 0),
                }

        if result:
            _obmen_cache = result
            _obmen_ts = now_ts
            logger.info(f"obmen24 API parsed: {result}")
            return result

        raise ValueError("obmen24 returned empty or invalid data")

    except Exception as e:
        logger.error(f"obmen24 API error: {e}")

    # --- 2. fallback на Приват ---
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(
                "https://api.privatbank.ua/p24api/pubinfo?json&exchange&coursid=5"
            )
            resp.raise_for_status()

            data = resp.json()

        result = {}
        for item in data:
            ccy = item.get("ccy", "")
            if ccy in ("USD", "EUR"):
                result[ccy] = {
                    "buy": float(item.get("buy", 0) or 0),
                    "sale": float(item.get("sale", 0) or 0),
                }

        if result:
            logger.info(f"PrivatBank fallback parsed: {result}")
            return result

        raise ValueError("PrivatBank returned empty data")

    except Exception as e:
        logger.error(f"PrivatBank fallback error: {e}")
        return {}
async def build_rates_msg() -> str:
    nbu, mono, obmen = await asyncio.gather(
        fetch_nbu_rates(), fetch_monobank_rates(), fetch_obmen_rates()
    )
    now = datetime.now(KYIV_TZ).strftime("%H:%M")
    lines = [f"💱 *Курс валют* _{now}_\n"]

    def v(d, key, fallback="  — "):
        val = d.get(key)
        return f"{float(val):.2f}" if val else fallback

    for cur, flag in [("USD", "🇺🇸"), ("EUR", "🇪🇺")]:
        mono_d = mono.get(cur, {})
        ob_d   = obmen.get(cur, {})
        mb  = v(mono_d, "rateBuy");  ms  = v(mono_d, "rateSell")
        ob  = v(ob_d,   "buy");      os_ = v(ob_d,   "sale")
        lines.append(
            f"{flag} *{cur}*\n"
            f"`{'':1}{'':7}{'Моно':>6}  {'obmen24':>9}`\n"
            f"`{'':1}{'Купить':<7}{mb:>7}  {ob:>7}`\n"
            f"`{'':1}{'Продать':<7}{ms:>7}  {os_:>7}`"
        )

    return "\n\n".join(lines)

# ── КОНТЕКСТ РАЗГОВОРА ───────────────────────────────────────────────────────
_conv_context: dict = {}

def get_ctx(chat_id) -> dict:
    return _conv_context.get(str(chat_id), {})

def set_ctx(chat_id, **kwargs):
    cid = str(chat_id)
    if cid not in _conv_context:
        _conv_context[cid] = {}
    _conv_context[cid].update(kwargs)

# ── ДОЛГИ ────────────────────────────────────────────────────────────────────
debts: dict = {}
debt_counter = [0]

def _debts_sheet():
    sh = _get_worksheet("Долги")
    if not sh.get_all_values():
        sh.insert_row(["ID","Кому","Сумма","Дата","Статус","Примечание"], 1)
    return sh

def load_debts():
    try:
        sym_map = {"₴":"UAH","$":"USD","€":"EUR"}
        for r in _debts_sheet().get_all_records():
            if r.get("Статус") != "активен": continue
            did = str(r["ID"])
            raw = r["Сумма"]
            try: amounts = [{"amount":float(raw),"currency":"UAH"}]
            except:
                amounts = []
                for part in str(raw).split("+"):
                    part = part.strip()
                    for sym, cur in sym_map.items():
                        if sym in part:
                            nums = re.findall(r'[\d,.]+', part)
                            if nums: amounts.append({"amount":float(nums[0].replace(",","")), "currency":cur})
                            break
                if not amounts: amounts = [{"amount":0,"currency":"UAH"}]
            debts[did] = {"name":r["Кому"],"amounts":amounts,"date":r["Дата"],"note":r.get("Примечание","")}
            try: debt_counter[0] = max(debt_counter[0], int(r["ID"]))
            except: pass
    except Exception as e:
        logger.error(f"load_debts: {e}")

def save_debt(did, name, amounts, date, note=""):
    amt_str = amounts_str(amounts)
    try: _debts_sheet().append_row([did, name, amt_str, date, "активен", note])
    except Exception as e: logger.error(f"save_debt: {e}")

def mark_paid(did):
    try:
        sh = _debts_sheet()
        for i, r in enumerate(sh.get_all_records(), start=2):
            if str(r.get("ID")) == str(did):
                sh.update_cell(i, 5, "погашен"); return
    except Exception as e: logger.error(f"mark_paid: {e}")

def update_debt_amounts(did, new_amounts):
    try:
        sh = _debts_sheet()
        for i, r in enumerate(sh.get_all_records(), start=2):
            if str(r.get("ID")) == str(did):
                sh.update_cell(i, 3, amounts_str(new_amounts)); return
    except Exception as e: logger.error(f"update_debt_amounts: {e}")

def amounts_str(amounts: list) -> str:
    return " + ".join(f"{a['amount']} {CURRENCY_SYMBOLS.get(a.get('currency','UAH'),'₴')}" for a in amounts)

def format_amounts(amounts: list) -> str:
    parts = [f"*{a['amount']:,.0f} {CURRENCY_SYMBOLS.get(a.get('currency','UAH'),'₴')}*" for a in amounts]
    return " + ".join(parts)

# FIX: добавил .replace(tzinfo=KYIV_TZ) для корректного вычитания aware datetime
def build_debts_msg() -> str:
    if not debts: return "✅ Активных долгов нет!"
    lines = ["💸 *Активные долги:*\n"]
    totals: dict = defaultdict(float)
    for d in debts.values():
        try:
            days_ago = (datetime.now(KYIV_TZ) - datetime.strptime(d["date"], "%d.%m.%Y").replace(tzinfo=KYIV_TZ)).days
        except:
            days_ago = 0
        note = f" — _{d['note']}_" if d.get("note") else ""
        ams = d.get("amounts",[{"amount":d.get("amount",0),"currency":"UAH"}])
        lines.append(f"👤 *{d['name']}* — {format_amounts(ams)}{note}")
        lines.append(f"   📅 {d['date']} ({days_ago} дн. назад)")
        for a in ams: totals[a.get("currency","UAH")] += float(a["amount"])
    if totals:
        lines.append("")
        for cur in ["USD","EUR","UAH"]:
            if cur in totals:
                sym = CURRENCY_SYMBOLS[cur]
                lines.append(f"💰 Итого в {sym}: *{fmt(totals[cur])} {sym}*")
    return "\n".join(lines)

# ── GROQ / LLM ───────────────────────────────────────────────────────────────
def transcribe(path: str) -> str:
    with open(path, "rb") as f:
        return groq_client.audio.transcriptions.create(
            model="whisper-large-v3", file=f, language="ru").text

def _llm(messages: list, max_tokens=600, temperature=0.0) -> str:
    r = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    return r.choices[0].message.content.strip()

def groq_chat(messages: list, max_tokens=800) -> str:
    return _llm(messages, max_tokens=max_tokens, temperature=0.7)

def _extract_json(raw: str, bracket="[") -> str:
    close = "]" if bracket == "[" else "}"
    raw = raw.replace("```json","").replace("```","").strip()
    s = raw.find(bracket)
    if s == -1: return raw
    depth = 0
    for i, ch in enumerate(raw[s:], start=s):
        if ch == bracket: depth += 1
        elif ch == close:
            depth -= 1
            if depth == 0:
                return raw[s:i+1]
    return raw[s:]

# ── УМНЫЙ ПАРСЕР ТРАТ ────────────────────────────────────────────────────────
PARSE_SYSTEM = """Ты — парсер финансовых записей. Извлеки ВСЕ траты из сообщения.

КАТЕГОРИИ:
- "Еда / продукты" — еда, напитки, рестораны, кафе, доставка, магазины, алкоголь
- "Транспорт" — бензин, заправки, такси, парковка, мойка, СТО, метро, автобус
- "Развлечения" — игры, кино, стриминг, подписки, ставки, боулинг, концерты
- "Здоровье / аптека" — лекарства, аптека, врачи, массаж, парикмахер, маникюр, спортзал
- "Никотин" — сигареты, снюс, вейп, кальян, ZYN, VELO
- "Другое" — одежда, техника, коммунальные, интернет, телефон, подарки, ремонт

ПРАВИЛА:
1. Ищи ВСЕ суммы — "потратил", "заплатил", "купил", "вышло"
2. Понимай сленг: "шаурма 120", "синька 200", "ашан 3к"
3. "к"/"тыс" = тысячи: "3к"=3000, "1.5к"=1500
4. Несколько трат через запятую/и — отдельный объект для каждой
5. "закинул на карту 500" — НЕ трата, пропусти
6. emoji — подходящий эмодзи для описания

ТОЛЬКО JSON массив:
[{"amount":<число>,"category":"<категория>","description":"<2-4 слова>","emoji":"<эмодзи>"}]"""

PARSE_EXAMPLES = [
    {"role":"user","content":"снюс 800"},
    {"role":"assistant","content":'[{"amount":800,"category":"Никотин","description":"снюс","emoji":"🚬"}]'},
    {"role":"user","content":"зашёл в атб, взял еды, вышло 340"},
    {"role":"assistant","content":'[{"amount":340,"category":"Еда / продукты","description":"ATБ продукты","emoji":"🛒"}]'},
    {"role":"user","content":"мойка 350, залил бензин 1200"},
    {"role":"assistant","content":'[{"amount":350,"category":"Транспорт","description":"мойка машины","emoji":"🚿"},{"amount":1200,"category":"Транспорт","description":"бензин","emoji":"⛽"}]'},
    {"role":"user","content":"купил кроссовки 2к и носки 150"},
    {"role":"assistant","content":'[{"amount":2000,"category":"Другое","description":"кроссовки","emoji":"👟"},{"amount":150,"category":"Другое","description":"носки","emoji":"🧦"}]'},
    {"role":"user","content":"netflix 199, spotify 99"},
    {"role":"assistant","content":'[{"amount":199,"category":"Развлечения","description":"Netflix","emoji":"🎬"},{"amount":99,"category":"Развлечения","description":"Spotify","emoji":"🎵"}]'},
    {"role":"user","content":"сколько я потратил?"},
    {"role":"assistant","content":'[]'},
    {"role":"user","content":"пополнил счёт 500"},
    {"role":"assistant","content":'[]'},
]

def parse_expenses(text: str) -> list:
    user_cats = _user_categories
    system = PARSE_SYSTEM
    if user_cats:
        system += f"\n\nДОПОЛНИТЕЛЬНЫЕ КАТЕГОРИИ: {', '.join(user_cats)}"
    messages = [{"role":"system","content":system}]
    messages.extend(PARSE_EXAMPLES)
    messages.append({"role":"user","content":text})
    try:
        raw = _llm(messages, max_tokens=500, temperature=0.0)
        raw = _extract_json(raw, "[")
        result = json.loads(raw)
        if isinstance(result, dict): result = [result]
        validated = []
        for item in result:
            try:
                amt = float(str(item.get("amount","0")).replace(",","."))
                if amt <= 0: continue
                item["amount"] = amt
                item["category"] = fix_cat(item.get("category","Другое"))
                validated.append(item)
            except: continue
        return validated
    except Exception as e:
        logger.error(f"parse_expenses error: {e}")
        nums = re.findall(r'\b\d+(?:[.,]\d+)?\b', text)
        if nums:
            try:
                return [{"amount": float(nums[0].replace(",",".")), "category": "Другое",
                         "description": text[:40], "emoji": "📦"}]
            except: pass
        return []

# ── ФИНАНСОВЫЙ КОНТЕКСТ ДЛЯ ИИ ───────────────────────────────────────────────
def get_financial_context(chat_id) -> str:
    recs = get_current_month_records()
    s = analyze_records(recs)
    bs = get_budget_status(chat_id)
    sal = get_salary_info(chat_id)
    parts = [f"Сегодня: {datetime.now(KYIV_TZ).strftime('%d.%m.%Y')}"]
    if s:
        parts.append(f"Траты за {month_name(datetime.now(KYIV_TZ).month)}: {fmt(s['total'])} ₴")
        cats = "; ".join(f"{c}: {fmt(a)}₴" for c,a in sorted(s["by_category"].items(), key=lambda x:-x[1])[:4])
        parts.append(f"Категории: {cats}")
    if bs:
        parts.append(f"Бюджет: {fmt(bs['budget'])}₴, использовано {bs['percent']}%, осталось {fmt(bs['left'])}₴")
    if sal:
        parts.append(f"Зарплата: {sal.get('amount','?')}₴, {sal['day']}-е число")
    if debts:
        dl = "; ".join(f"{d['name']}: {format_amounts(d['amounts']).replace('*','')}" for d in list(debts.values())[:3])
        parts.append(f"Долги: {dl}")
    if goals:
        gl = "; ".join(f"{g['name']}: {fmt(g['saved'])}/{fmt(g['target'])}₴" for g in list(goals.values())[:3])
        parts.append(f"Цели: {gl}")
    return "\n".join(parts)

# ── ИИ ЧАТ С ПАМЯТЬЮ ─────────────────────────────────────────────────────────
_ai_chat_history: dict = {}

async def ai_chat_response(chat_id, user_message: str) -> str:
    if chat_id not in _ai_chat_history:
        _ai_chat_history[chat_id] = []
    history = _ai_chat_history[chat_id]
    financial_ctx = get_financial_context(chat_id)
    system = f"""Ты умный финансовый ИИ-ассистент. Отвечай кратко и по делу (3-5 предложений).
Используй данные пользователя. Будь дружелюбным, с эмодзи. Отвечай на русском/украинском.

ДАННЫЕ ПОЛЬЗОВАТЕЛЯ:
{financial_ctx}"""
    messages = [{"role":"system","content":system}]
    messages.extend(history[-10:])
    messages.append({"role":"user","content":user_message})
    try:
        response = groq_chat(messages, max_tokens=600)
        history.append({"role":"user","content":user_message})
        history.append({"role":"assistant","content":response})
        if len(history) > 20:
            _ai_chat_history[chat_id] = history[-20:]
        return response
    except Exception as e:
        logger.error(f"ai_chat: {e}")
        return "🤔 Не могу ответить сейчас. Попробуй чуть позже!"

# ── ЗАПАСНЫЕ ФУНКЦИИ АНАЛИТИКИ ────────────────────────────────────────────────
def build_advice_fallback(records: list) -> str:
    if not records: return ""
    s = analyze_records(records)
    if not s: return ""
    total = s["total"]
    bc = s["by_category"]
    tips = []
    food = bc.get("Еда / продукты", 0)
    if food > total * 0.35:
        tips.append(f"🍔 На еду {int(food/total*100)}%. −25% = *+{fmt(food*0.25)} ₴/мес*")
    nic = bc.get("Никотин", 0)
    if nic > 500:
        tips.append(f"🚬 Никотин: *{fmt(nic)} ₴/мес* = *{fmt(nic*12)} ₴/год*")
    if not tips: return "💡 Трать осознанно!"
    return "💡 *Советы:*\n" + "\n".join(f"{i}. {t}" for i,t in enumerate(tips,1))

def build_insight() -> str:
    recs = get_week_records()
    month_recs = get_current_month_records()
    if not recs: return "📭 За эту неделю данных нет."
    s = analyze_records(recs)
    lines = ["🧠 *Инсайт недели*\n"]
    if s["by_day"]:
        td = max(s["by_day"], key=s["by_day"].get)
        avg = s["total"] / 7
        if s["by_day"][td] > avg * 1.5:
            lines.append(f"📅 Дорогой день: *{td}* (+{int(s['by_day'][td]/avg*100-100)}% от среднего)")
    if s["by_category"]:
        tc = max(s["by_category"], key=s["by_category"].get)
        pct = int(s["by_category"][tc] / s["total"] * 100)
        lines.append(f"{get_category_emoji(tc)} Топ: *{tc}* — {pct}%")
    if month_recs:
        ms_data = analyze_records(month_recs)
        avg_day = ms_data["total"] / datetime.now(KYIV_TZ).day
        lines.append(f"📈 Прогноз месяца: *~{fmt(avg_day*30)} ₴*")
    lines.append(f"\n💰 За неделю: *{fmt(s['total'])} ₴* ({s['count']} записей)")
    return "\n".join(lines)

def build_weekly_report() -> str:
    recs = get_week_records()
    if not recs: return "📭 За прошлую неделю трат нет."
    s = analyze_records(recs)
    lines = ["📅 *Отчёт за неделю*\n",
             f"💰 Потрачено: *{fmt(s['total'])} ₴* ({s['count']} записей)\n",
             "*По категориям:*"] + _cat_lines(s)
    if s["by_day"]:
        td = max(s["by_day"], key=s["by_day"].get)
        lines.append(f"\n📈 Самый дорогой день: *{td}* — {fmt(s['by_day'][td])} ₴")
    lines += _leak_lines(s)
    return "\n".join(lines)

def build_monthly_report() -> str:
    recs = get_current_month_records()
    if not recs: return "📭 В этом месяце трат нет."
    s = analyze_records(recs)
    now = datetime.now(KYIV_TZ)
    avg = s["total"] / now.day if now.day else 0
    lines = [f"📆 *Отчёт за {month_name(now.month)} {now.year}*\n",
             f"💰 Потрачено: *{fmt(s['total'])} ₴* за {now.day} дней",
             f"📊 В среднем: *{fmt(avg)} ₴/день*",
             f"📈 Прогноз: *~{fmt(avg*30)} ₴*\n",
             "*Топ категории:*"] + _cat_lines(s, 5) + _leak_lines(s)
    return "\n".join(lines)

def build_comparison() -> str:
    all_recs = get_all_records()
    months: dict = {}
    for r in all_recs:
        try:
            # FIX: добавил .replace(tzinfo=KYIV_TZ) для консистентности
            d = datetime.strptime(r.get("Дата","")[:10], "%d.%m.%Y").replace(tzinfo=KYIV_TZ)
            months.setdefault((d.year, d.month), []).append(r)
        except: pass
    if len(months) < 2: return "📭 Нужно минимум 2 месяца данных."
    lines = ["📊 *Сравнение месяцев*\n"]
    prev_total = None
    for ym in sorted(months, reverse=True)[:3]:
        s = analyze_records(months[ym])
        name = f"{month_name(ym[1])} {ym[0]}"
        if prev_total:
            diff = int((s["total"] - prev_total) / prev_total * 100)
            arrow = "📈" if diff > 0 else "📉"
            lines.append(f"*{name}*: {fmt(s['total'])} ₴ {arrow} {'+' if diff>0 else ''}{diff}%")
        else:
            lines.append(f"*{name}*: {fmt(s['total'])} ₴")
        if s["by_category"]:
            tc = max(s["by_category"], key=s["by_category"].get)
            lines.append(f"  └ Топ: {get_category_emoji(tc)} {tc} — {fmt(s['by_category'][tc])} ₴")
        prev_total = s["total"]
    return "\n".join(lines)

async def build_past_self_ai(chat_id) -> str:
    all_recs = get_all_records()
    now = datetime.now(KYIV_TZ)
    def ms(ago):
        t = now.replace(day=1)
        for _ in range(ago): t = (t - timedelta(days=1)).replace(day=1)
        return analyze_records(records_for_month(t.month, t.year, all_recs)), t
    cur = analyze_records(get_current_month_records())
    if not cur: return "📭 Недостаточно данных — запиши хотя бы несколько трат."
    data_lines = [f"Текущий месяц ({month_name(now.month)}): {fmt(cur['total'])} ₴"]
    cats_cur = ", ".join(f"{c}: {fmt(a)}₴" for c,a in sorted(cur["by_category"].items(), key=lambda x:-x[1])[:5])
    data_lines.append(f"Категории: {cats_cur}")
    history_parts = []
    for ago, label in [(1,"1 месяц назад"),(2,"2 месяца назад"),(3,"3 месяца назад")]:
        s, t = ms(ago)
        if not s: continue
        diff = int((cur["total"] - s["total"]) / s["total"] * 100) if s["total"] else 0
        history_parts.append(f"{month_name(t.month)}: {fmt(s['total'])} ₴ ({'+' if diff>0 else ''}{diff}% vs сейчас)")
        cats_h = ", ".join(f"{c}: {fmt(a)}₴" for c,a in sorted(s["by_category"].items(), key=lambda x:-x[1])[:3])
        history_parts.append(f"  категории: {cats_h}")
    if not history_parts: return "📭 Нужно минимум 2 месяца данных для сравнения."
    data_lines += ["", "История:"] + history_parts
    prompt = f"""Ты финансовый аналитик. Проанализируй динамику трат пользователя.

ДАННЫЕ:
{chr(10).join(data_lines)}

Напиши анализ:
- Заголовок 🪞 *Сравнение с прошлым «я»*
- 2-3 конкретных наблюдения с цифрами
- 1 короткий вывод или совет
- Тон: дружелюбный, без воды, с эмодзи
- Длина: 5-8 строк"""
    messages = [{"role":"system","content":"Отвечай только на русском. Markdown через *. Без лишних слов."},
                {"role":"user","content":prompt}]
    try:
        return groq_chat(messages, max_tokens=400)
    except:
        lines = ["🪞 *Сравнение с прошлым «я»*\n"]
        for ago, label in [(1,"1 мес"),(2,"2 мес"),(3,"3 мес")]:
            s, t = ms(ago)
            if not s: continue
            diff = int((cur["total"] - s["total"]) / s["total"] * 100)
            sign = "+" if diff > 0 else ""
            lines.append(f"{'📈' if diff>0 else '📉'} *{label} назад* ({month_name(t.month)}): {sign}{diff}%")
        return "\n".join(lines)

async def build_habits_ai() -> str:
    all_recs = get_all_records()
    months: dict = {}
    for r in all_recs:
        try:
            # FIX: добавил .replace(tzinfo=KYIV_TZ)
            d = datetime.strptime(r.get("Дата","")[:10], "%d.%m.%Y").replace(tzinfo=KYIV_TZ)
            months.setdefault((d.year, d.month), []).append(r)
        except: pass
    if not months: return "📭 Недостаточно данных."
    n = max(len(months), 1)
    desc_data: dict = defaultdict(lambda: {"total":0.0,"count":0,"months":set()})
    for ym, recs in months.items():
        sk = get_sum_key(recs)
        for r in recs:
            desc = r.get("Описание","").lower().strip()
            amt = float(r[sk]) if r.get(sk) else 0
            if desc and amt:
                desc_data[desc]["total"] += amt
                desc_data[desc]["count"] += 1
                desc_data[desc]["months"].add(ym)
    habits = {k:v for k,v in desc_data.items() if len(v["months"])>=2 and v["total"]/n>=200}
    if not habits: return "📭 Пока мало данных. Записывай траты несколько недель — и я найду паттерны!"
    habits_text = []
    for desc, d in sorted(habits.items(), key=lambda x:-x[1]["total"])[:8]:
        monthly = d["total"] / n
        annual = monthly * 12
        habits_text.append(f"- {desc}: {fmt(monthly)}₴/мес, {fmt(annual)}₴/год ({d['count']} раз)")
    prompt = f"""Ты финансовый аналитик. Проанализируй регулярные траты.

ПРИВЫЧКИ:
{chr(10).join(habits_text)}

Напиши анализ:
- Заголовок 💸 *Стоимость привычек*
- Для каждой: сумма/мес и /год, сравнение с чем-то ("это = 3 поездки в кино")
- Выдели 1-2 где можно сэкономить
- Тон: без осуждения, с юмором, с эмодзи
- Компактно, без воды"""
    messages = [{"role":"system","content":"Отвечай только на русском. Markdown через *. Кратко."},
                {"role":"user","content":prompt}]
    try:
        return groq_chat(messages, max_tokens=500)
    except:
        lines = ["💸 *Стоимость привычек*\n"]
        for desc, d in sorted(habits.items(), key=lambda x:-x[1]["total"])[:6]:
            monthly = d["total"] / n
            annual = monthly * 12
            equiv = next((label for thr,label in EQUIVALENTS if annual>=thr*0.7), None)
            lines += [f"*{desc.capitalize()}*", f"  📅 {fmt(monthly)} ₴/мес · 📆 {fmt(annual)} ₴/год"]
            if equiv: lines.append(f"  💡 = {equiv}")
            lines.append("")
        return "\n".join(lines)

async def build_advice_ai(chat_id) -> str:
    recs = get_current_month_records()
    if not recs or len(recs) < 3:
        return "📭 Маловато данных — запиши хотя бы 5-10 трат."
    s = analyze_records(recs)
    now = datetime.now(KYIV_TZ)
    avg = s["total"] / now.day if now.day else 0
    bs = get_budget_status(chat_id)
    sal = get_salary_info(chat_id)
    cats = ", ".join(f"{c}: {fmt(a)}₴ ({int(a/s['total']*100)}%)"
                     for c,a in sorted(s["by_category"].items(), key=lambda x:-x[1]))
    budget_info = f"Бюджет: {fmt(bs['budget'])}₴, использовано {bs['percent']}%, осталось {fmt(bs['left'])}₴" if bs else "бюджет не установлен"
    salary_info = f"Зарплата: {sal.get('amount','?')}₴, {sal['day']}-е число" if sal else "зарплата не указана"
    leaks = ", ".join(f"{k} ({v['count']}×={fmt(v['total'])}₴)"
                      for k,v in list(s.get("leaks",{}).items())[:3])
    goals_info = ", ".join(f"{g['name']}: {fmt(g['saved'])}/{fmt(g['target'])}₴"
                           for g in goals.values()) or "нет"
    prompt = f"""Ты личный финансовый советник. Дай конкретные советы.

ДАННЫЕ за {month_name(now.month)}:
- Потрачено: {fmt(s['total'])}₴ за {now.day} дней, среднее {fmt(avg)}₴/день
- Прогноз на месяц: {fmt(avg*30)}₴
- Категории: {cats}
- {budget_info}
- {salary_info}
- Частые траты: {leaks or 'нет'}
- Цели: {goals_info}

Напиши 3-4 совета:
- Заголовок 💡 *Персональные советы*
- Каждый совет с конкретной цифрой
- Если есть цели — как быстрее накопить
- Тон: дружелюбный, практичный, без банальщины"""
    messages = [{"role":"system","content":"Отвечай только на русском. Markdown через *. Конкретные цифры важны."},
                {"role":"user","content":prompt}]
    try:
        return groq_chat(messages, max_tokens=500)
    except:
        return build_advice_fallback(recs)

async def send_debt_reminder(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data or {}
    did = data.get("debt_id")
    cid = data.get("chat_id") or CHAT_ID
    if not did or did not in debts or not cid: return
    d = debts[did]
    ams = d.get("amounts",[])
    kb = inline_kb([
        [("✅ Вернули","paid_"+did),("⏰ Напомнить ещё","remind_"+did)],
    ])
    await context.bot.send_message(
        chat_id=cid,
        text=f"⏰ *Напоминание о долге*\n\n👤 *{d['name']}* должен {format_amounts(ams)}",
        parse_mode="Markdown", reply_markup=kb)

async def send_weekly_insight(context: ContextTypes.DEFAULT_TYPE):
    """Еженедельный инсайт по пятницам в 19:00"""
    cid = (context.job.data or {}).get("chat_id") or CHAT_ID
    if not cid: return
    recs = get_week_records()
    month_recs = get_current_month_records()
    if not recs:
        await context.bot.send_message(chat_id=cid, text="📭 За эту неделю данных нет.")
        return
    s = analyze_records(recs)
    lines = ["🧠 *Инсайт недели*\n"]
    if s["by_day"]:
        td = max(s["by_day"], key=s["by_day"].get)
        avg = s["total"] / 7
        if s["by_day"][td] > avg * 1.5:
            lines.append(f"📅 Дорогой день: *{td}* (+{int(s['by_day'][td]/avg*100-100)}% от среднего)")
    if s["by_category"]:
        tc = max(s["by_category"], key=s["by_category"].get)
        pct = int(s["by_category"][tc] / s["total"] * 100)
        lines.append(f"{get_category_emoji(tc)} Топ: *{tc}* — {pct}%")
    if month_recs:
        ms = analyze_records(month_recs)
        avg_day = ms["total"] / datetime.now(KYIV_TZ).day
        lines.append(f"📈 Прогноз месяца: *~{fmt(avg_day*30)} ₴*")
    lines.append(f"\n💰 За неделю: *{fmt(s['total'])} ₴* ({s['count']} записей)")
    await context.bot.send_message(chat_id=cid, text="\n".join(lines), parse_mode="Markdown")

async def send_morning_briefing(context: ContextTypes.DEFAULT_TYPE):
    """Утренняя сводка каждый день в 9:00"""
    cid = (context.job.data or {}).get("chat_id") or CHAT_ID
    if not cid: return
    bs = get_budget_status(cid)
    sal = get_salary_info(cid)
    now = datetime.now(KYIV_TZ)
    lines = [f"☀️ *Доброе утро, {now.strftime('%d.%m')}!*\n"]
    today_spent = sum_records(get_today_records())
    if today_spent > 0:
        lines.append(f"📌 Вчера потрачено: *{fmt(today_spent)} ₴*")
    if bs:
        days_left = 30 - now.day + 1
        daily_limit = bs["left"] / max(days_left, 1)
        lines.append(f"💰 Бюджет: *{bs['percent']}%* использован")
        lines.append(f"📊 Лимит сегодня: *{fmt(daily_limit)} ₴*")
    elif sal and sal.get("amount"):
        spent = sum_records(get_current_month_records())
        left = float(sal["amount"]) - spent
        sal_day = sal["day"]
        days_left = (sal_day - now.day) if now.day < sal_day else (
            (datetime.now(KYIV_TZ).replace(day=1) + timedelta(days=32)).replace(day=sal_day) - now
        ).days
        if days_left > 0:
            lines.append(f"💵 До зарплаты *{fmt(left/max(days_left,1))} ₴/день*")
    if len(lines) > 1:
        await context.bot.send_message(chat_id=cid, text="\n".join(lines), parse_mode="Markdown")

# ── КЛАВИАТУРА ───────────────────────────────────────────────────────────────
# FIX: добавил кнопку "💱 Курс" на главный экран
MAIN_KB = ReplyKeyboardMarkup([
    [KeyboardButton("💰 Финансы"), KeyboardButton("📊 Аналитика")],
    [KeyboardButton("💸 Долги"),   KeyboardButton("🎯 Цели")],
    [KeyboardButton("💱 Курс валют"), KeyboardButton("⚙️ Прочее")],
], resize_keyboard=True)

def inline_kb(buttons: list[list]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d) for t,d in row] for row in buttons])

FINANCE_KB = inline_kb([
    [("📊 Статистика","menu_stats"),("💰 Бюджет","menu_budget")],
    [("💵 Зарплата","menu_salary"),("📊 Сравнение","menu_compare")],
])
ANALYTICS_KB = inline_kb([
    [("📅 Неделя","menu_week"),("📆 Месяц","menu_month")],
    [("🪞 Прошлое я","menu_past"),("💸 Привычки","menu_habits")],
    [("💡 Советы","menu_advice")],
])
OTHER_KB = inline_kb([
    [("💡 Советы","menu_advice"),("💵 Зарплата","menu_salary")],
    [("🪞 Прошлое я","menu_past"),("💸 Привычки","menu_habits")],
    [("⏰ Напоминания","menu_reminder"),("🏷 Категории","menu_categories")],
])
REMINDER_KB = inline_kb([
    [("1 день","reminder_1"),("3 дня","reminder_3")],
    [("1 неделю","reminder_7"),("2 недели","reminder_14")],
    [("3 недели","reminder_21"),("1 месяц","reminder_30")],
])

# ── INLINE helpers ───────────────────────────────────────────────────────────
async def cmd_stats_inline(chat_id, context):
    recs = get_current_month_records()
    if not recs:
        await context.bot.send_message(chat_id=chat_id, text="📭 В этом месяце ещё нет записей."); return
    s = analyze_records(recs)
    now = datetime.now(KYIV_TZ)
    avg = s["total"]/now.day if now.day else 0
    lines = [f"📊 *Статистика за {month_name(now.month)}* ({s['count']} записей)\n"]
    lines += _cat_lines(s)
    lines += [f"\n💰 *Итого: {fmt(s['total'])} ₴*", f"📈 Прогноз: *~{fmt(avg*30)} ₴*"]
    bs = get_budget_status(chat_id)
    if bs:
        bar = "█"*(bs["percent"]//10) + "░"*(10-bs["percent"]//10)
        lines += [f"\n💰 Бюджет: [{bar}] {bs['percent']}%", f"Осталось: *{fmt(bs['left'])} ₴*"]
    lines += _leak_lines(s)
    await context.bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode="Markdown")

async def cmd_budget_inline(chat_id, context):
    bs = get_budget_status(chat_id)
    if not bs:
        await context.bot.send_message(chat_id=chat_id, text="💰 Бюджет не установлен.\n\nНапиши: «Бюджет 20000»"); return
    pct = bs["percent"]
    bar = "█"*(pct//10) + "░"*(10-pct//10)
    status = "🟢" if pct<70 else "🟡" if pct<90 else "🔴"
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"💰 *Бюджет на месяц*\n\n{status} [{bar}] *{pct}%*\n\n"
             f"Бюджет: *{fmt(bs['budget'])} ₴*\nПотрачено: *{fmt(bs['spent'])} ₴*\nОсталось: *{fmt(bs['left'])} ₴*",
        parse_mode="Markdown")

# ── ВСПОМОГАТЕЛЬНЫЕ ДЛЯ ОТЧЁТОВ ──────────────────────────────────────────────
def _cat_lines(stats: dict, limit: int = None) -> list:
    items = sorted(stats["by_category"].items(), key=lambda x: -x[1])
    if limit: items = items[:limit]
    lines = []
    for cat, amt in items:
        pct = int(amt / stats["total"] * 100)
        lines.append(f"{get_category_emoji(cat)} {cat}: *{fmt(amt)} ₴* ({pct}%)")
    return lines

def _leak_lines(stats: dict) -> list:
    if not stats.get("leaks"): return []
    lines = ["\n💸 *Частые траты:*"]
    for desc, d in list(stats["leaks"].items())[:3]:
        lines.append(f"• {desc}: {d['count']}× = *{fmt(d['total'])} ₴*")
    return lines

# ── HANDLERS ─────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _records_cache.clear()
    _settings.clear()
    load_settings()
    load_user_categories()
    debts.clear()
    debt_counter[0] = 0
    load_debts()
    goals.clear()
    goal_counter[0] = 0
    load_goals()
    await update.message.reply_text(
        "👋 Привет! Я твой финансовый *AI-агент*.\n\n"
        "Просто пиши как обычно:\n"
        "🛒 «Снюс 800» — запишу трату\n"
        "💸 «Дал папе 500» — запишу долг\n"
        "💰 «Бюджет 25000» — установлю бюджет\n"
        "💵 «Зарплата 6го 30000» — запомню\n"
        "🎯 «Накопить на iPhone 25000» — создам цель\n"
        "❓ «Сколько потратил на еду?» — отвечу\n\n"
        "_Понимаю русский, украинский и несколько команд сразу_ 🧠",
        parse_mode="Markdown", reply_markup=MAIN_KB)

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Анализирую...")
    await cmd_stats_inline(update.effective_chat.id, context)

async def cmd_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_budget_inline(update.effective_chat.id, context)

async def cmd_salary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = build_salary_status(update.effective_chat.id)
    if not status:
        await update.message.reply_text("💵 Зарплата не установлена.\n\nНапиши: «Зарплата 25» или «Зарплата 25 числа 35000»")
    else:
        await update.message.reply_text(status, parse_mode="Markdown")

async def cmd_debts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = build_debts_msg()
    if debts:
        kb = inline_kb([[("✅ Отметить погашенным","show_debts")]])
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=kb)
    else:
        await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_goals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = build_goals_msg()
    kb = inline_kb([[("➕ Пополнить цель","goal_deposit"),("🗑 Закрыть цель","goal_close")]])
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=kb if goals else None)

async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Формирую отчёт...")
    await update.message.reply_text(build_weekly_report(), parse_mode="Markdown")

async def cmd_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Формирую отчёт...")
    await update.message.reply_text(build_monthly_report(), parse_mode="Markdown")

async def cmd_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cur = reminder_label(update.effective_chat.id)
    await update.message.reply_text(
        f"⏰ *Напоминания о долгах*\n\nТекущий интервал: *{cur}*\n\nВыбери:",
        parse_mode="Markdown", reply_markup=REMINDER_KB)

async def cmd_rates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Запрашиваю курс...")
    msg = await build_rates_msg()
    await update.message.reply_text(msg, parse_mode="Markdown")

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎙 Распознаю...")
    try:
        file = await context.bot.get_file(update.message.voice.file_id)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            await file.download_to_drive(tmp.name)
            path = tmp.name
        text = transcribe(path)
        os.unlink(path)
        await update.message.reply_text(f"📝 Распознал: _{text}_", parse_mode="Markdown")
        await process(update, context, text)
    except Exception as e:
        logger.error(f"voice: {e}")
        await update.message.reply_text("❌ Не удалось распознать. Попробуй ещё раз.")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    chat_id = update.effective_chat.id

    # ── Кнопки меню — всегда приоритет ──
    routes = {
        "📊 Статистика": cmd_stats, "📅 Отчёт за неделю": cmd_week,
        "📆 Отчёт за месяц": cmd_month, "💰 Бюджет": cmd_budget,
        "💸 Долги": cmd_debts, "💵 Зарплата": cmd_salary,
        "🎯 Цели": cmd_goals, "💱 Курс валют": cmd_rates,
    }
    if text in routes:
        await routes[text](update, context); return
    if text == "💰 Финансы":
        await update.message.reply_text("💰 *Финансы*:", parse_mode="Markdown", reply_markup=FINANCE_KB); return
    if text == "📊 Аналитика":
        await update.message.reply_text("📊 *Аналитика*:", parse_mode="Markdown", reply_markup=ANALYTICS_KB); return
    if text == "⚙️ Прочее":
        await update.message.reply_text("⚙️ *Прочее*:", parse_mode="Markdown", reply_markup=OTHER_KB); return
    if text == "🎯 Цели":
        await cmd_goals(update, context); return

    # Быстрый fallback: удалить последнюю трату (без LLM)
    if re.search(r"(удал|убер|скасу|відмін|cancel|undo).*(останн|последн|last|запис|трат|витрат)", text, re.IGNORECASE) or \
       re.match(r"(удал|убер|скасу|відмін)[иьує]?\s*$", text.strip(), re.IGNORECASE):
        try:
            all_recs = get_all_records()
            sh = get_sheet()
            if all_recs:
                last = all_recs[-1]
                row_idx = len(all_recs) + 1
                sk = get_sum_key(all_recs)
                sh.delete_rows(row_idx)
                _invalidate("sheet1")
                cat = last.get("Категория","?")
                await update.message.reply_text(
                    f"🗑 *Удалено*\n{get_category_emoji(cat)} *{last.get('Описание','?')}* — *{fmt(float(last.get(sk,0)))} ₴*",
                    parse_mode="Markdown"); return
        except Exception as e:
            logger.error(f"undo fallback: {e}")

    # Быстрый fallback: добавить категорию (без LLM)
    _cat_m = re.match(
        r"(?:добав[ьи]|add|нова\s+категор\w*|новая\s+категор\w*)\s+(?:категор\w*\s+)?(\w[\w\s-]{1,29})",
        text.strip(), re.IGNORECASE
    )
    if _cat_m:
        cat_name = _cat_m.group(1).strip().capitalize()
        if cat_name and cat_name.lower() not in [c.lower() for c in get_all_categories()]:
            save_user_category(cat_name)
            em = get_category_emoji(cat_name)
            await update.message.reply_text(
                f"✅ Категория {em} *{cat_name}* добавлена!\nТеперь пиши: «{cat_name} 1500»",
                parse_mode="Markdown"); return

    await process(update, context, text)

def _regex_route(text: str) -> list | None:
    """Быстрый роутер без LLM для ~80% простых команд"""
    t = text.strip()

    # Зарплата
    m = re.search(
        r"зарплат[аы]?\s+(\d{1,2})[-\s]?(?:го|числа|ого)?\s+(\d[\d\s.,]*(?:\s*к)?)"
        r"|зарплат[аы]?\s+(\d[\d\s.,]*(?:\s*к)?)\s+(\d{1,2})[-\s]?(?:го|числа)",
        t, re.IGNORECASE
    )
    if m:
        g = [x for x in m.groups() if x]
        try:
            candidates = []
            for x in g:
                x_clean = x.strip().replace(" ", "").replace(",", ".")
                mult = 1000 if x_clean.lower().endswith("к") else 1
                x_clean = re.sub(r"[кК]$", "", x_clean)
                candidates.append(float(x_clean) * mult)
            day = int(candidates[0]) if candidates[0] <= 31 else int(candidates[1])
            amt = candidates[1] if candidates[0] <= 31 else candidates[0]
            if 1 <= day <= 31 and amt > 0:
                return [{"action": "salary_set", "day": day, "amount": amt}]
        except: pass

    # Бюджет
    m = re.search(r"бюджет[^\d]*(\d[\d\s]*(?:[.,]\d+)?(?:\s*к)?)", t, re.IGNORECASE)
    if m:
        s = m.group(1).strip().replace(" ", "").replace(",", ".")
        mult = 1000 if s.lower().endswith("к") else 1
        s = re.sub(r"[кК]$", "", s)
        try:
            return [{"action": "budget_set", "amount": float(s) * mult}]
        except: pass

    # Трата: "кофе 85", "обед 650 грн", "3к бензин"
    m = re.match(
        r"^([а-яёіїєa-zA-Zа-яёіїє][а-яёіїєa-zA-Zа-яёіїє\w\s/-]{0,40}?)\s+"
        r"(\d[\d\s]*(?:[.,]\d+)?(?:\s*к(?:р|ривень|уб)?|\s*тис(?:яч)?)?)\s*(?:₴|грн|грн\.?|uah)?$",
        t, re.IGNORECASE
    ) or re.match(
        r"^(\d[\d\s]*(?:[.,]\d+)?(?:\s*к(?:р|ривень|уб)?|\s*тис(?:яч)?)?)\s*(?:₴|грн|uah)?\s+"
        r"([а-яёіїєa-zA-Zа-яёіїє][а-яёіїєa-zA-Zа-яёіїє\w\s/-]{1,40})$",
        t, re.IGNORECASE
    )
    if m:
        g = m.groups()
        def parse_amount(s):
            s = s.strip().replace(" ", "").replace(",", ".")
            mult = 1000 if re.search(r"к(?:р|ривень|уб)?$|тис", s, re.IGNORECASE) else 1
            s = re.sub(r"[кКтТис]+.*$", "", s, flags=re.IGNORECASE)
            try: return float(s) * mult
            except: return None

        amt = parse_amount(g[1]) or parse_amount(g[0])
        desc = (g[0] if parse_amount(g[1]) else g[1]).strip().lower()

        if amt and amt > 0 and desc and not re.match(r"^\d", desc):
            cat_map = [
                (["еда","продукт","обед","ужин","завтрак","кафе","ресторан","пицца","суши",
                  "шаурма","бургер","кофе","чай","сок","доставка","магазин","ашан","сільпо",
                  "атб","новус","перекус","снек","фрукт","хлеб","молоко","alco","алко"], "Еда / продукты", "🍔"),
                (["такси","бензин","заправк","автобус","метро","маршрутк","парковк","мойк",
                  "сто ","ремонт авто","запчаст","uber","bolt","поїзд","автовокзал"], "Транспорт", "🚗"),
                (["кино","игр","steam","netflix","spotify","боулинг","концерт","клуб",
                  "розваг","підписк","subscription","iptv"], "Развлечения", "🎮"),
                (["аптек","лікарств","лекарств","врач","лікар","медиц","стоматолог","масаж","массаж",
                  "парикмах","манікюр","маникюр","спортзал","фитнес","косметолог"], "Здоровье / аптека", "💊"),
                (["снюс","сигарет","вейп","кальян","никотин","zyn","velo","табак","тютюн"], "Никотин", "🚬"),
                (["гимнастик","йога","плаван","бокс","тренировк","тренуван","зал ","спорт ",
                  "пробежк","бег","велосипед","воркаут","качалк","фізкультур","гімнастик"], "Спорт", "💪"),
                (["курс","учеб","навчан","репетитор","школ","универ","книг","образован",
                  "english","урок","лекци","семинар","навчання"], "Образование", "📚"),
                (["одяг","одежд","обувь","шопинг","брюки","футболк","платт","куртк","сумк","взуття"], "Одежда", "👕"),
                (["комунальн","комунальні","квартир","аренд","оренд","кварплат","жкх","свет","газ",
                  "вода","інтернет","інтернет","мобільн","мобильн","телефон","зв'язок"], "Коммунальные", "🏠"),
            ]
            category, emoji = "Другое", "📦"
            for keywords, cat, em in cat_map:
                if any(k in desc for k in keywords):
                    category, emoji = cat, em
                    break
            for uc in _user_categories:
                if uc.lower() in desc or desc in uc.lower():
                    category = uc
                    emoji = get_category_emoji(uc)
                    break
            return [{"action": "expense", "expenses": [{"amount": amt, "category": category, "description": desc.capitalize(), "emoji": emoji}]}]

    return None

# ── ИИ-РОУТЕР ──────────────────────────────────────────────────────────────
ROUTER_SYSTEM = """Финансовый бот. Верни ТОЛЬКО JSON {{"actions":[...]}}.

ДЕЙСТВИЯ:
expense: {{"action":"expense","expenses":[{{"amount":N,"category":"C","description":"D","emoji":"E"}}]}}
debt_new: {{"action":"debt_new","name":"N","amounts":[{{"amount":N,"currency":"UAH"}}],"note":""}}
debt_add: {{"action":"debt_add","name":"N","amount":N,"currency":"UAH"}}
debt_return: {{"action":"debt_return","name":"N","amount":N,"currency":"UAH"}}
debt_remind: {{"action":"debt_remind","name":"N","minutes":N}}
budget_set: {{"action":"budget_set","amount":N}}
salary_set: {{"action":"salary_set","day":N,"amount":N}}
goal_new: {{"action":"goal_new","name":"N","amount":N,"emoji":"E"}}
goal_deposit: {{"action":"goal_deposit","amount":N,"goal_name":"N"}}
convert: {{"action":"convert","amount":N,"from_currency":"USD","to_currency":"UAH"}}
category_add: {{"action":"category_add","name":"N","emoji":"E"}}
expense_delete: {{"action":"expense_delete","description":null,"amount":null,"category":null}}
expense_edit: {{"action":"expense_edit","old_category":"C","new_category":"C","amount":null,"description":""}}
question: {{"action":"question","text":"T"}}

КАТЕГОРИИ: "Еда / продукты","Транспорт","Развлечения","Здоровье / аптека","Никотин","Другое"
Пользовательские: {user_cats}
ДОЛГИ: {debts} | ЦЕЛИ: {goals} | КОНТЕКСТ: {context}

Правила: "3к"=3000 | рус/укр одинаково | спорт/йога/тренировки→"Спорт" | несколько команд→все в массиве
{{"actions": [...]}}"""

async def route_message(text: str, chat_id, conv_ctx: dict) -> list:
    debts_info = ", ".join(f"{d['name']}: {format_amounts(d['amounts']).replace('*','')}" for d in list(debts.values())[:5]) or "нет"
    goals_info = ", ".join(f"{g['name']}: {fmt(g['saved'])}/{fmt(g['target'])}₴" for g in list(goals.values())[:3]) or "нет"
    last_msgs = conv_ctx.get("last_messages", [])
    ctx_info = f"имя: {conv_ctx.get('last_name','нет')}, действие: {conv_ctx.get('last_action','нет')}, последние сообщения: {'; '.join(last_msgs[-3:]) if last_msgs else 'нет'}"
    user_cats_info = ", ".join(_user_categories) if _user_categories else "нет"

    system = ROUTER_SYSTEM.format(context=ctx_info, debts=debts_info, goals=goals_info, user_cats=user_cats_info)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": "снюс 800"},
        {"role": "assistant", "content": '{"actions":[{"action":"expense","expenses":[{"amount":800,"category":"Никотин","description":"снюс","emoji":"🚬"}]}]}'},
        {"role": "user", "content": "дал Саше 500 и бюджет 25к"},
        {"role": "assistant", "content": '{"actions":[{"action":"debt_new","name":"Саша","amounts":[{"amount":500,"currency":"UAH"}],"note":""},{"action":"budget_set","amount":25000}]}'},
        {"role": "user", "content": "гимнастика 300"},
        {"role": "assistant", "content": '{"actions":[{"action":"expense","expenses":[{"amount":300,"category":"Спорт","description":"гимнастика","emoji":"🤸"}]}]}'},
        {"role": "user", "content": "зарплата 6-го 25 000"},
        {"role": "assistant", "content": '{"actions":[{"action":"salary_set","day":6,"amount":25000}]}'},
        {"role": "user", "content": "удали последнюю"},
        {"role": "assistant", "content": '{"actions":[{"action":"expense_delete","description":null,"amount":null,"category":null}]}'},
        {"role": "user", "content": "добавь категорию Инвестиции"},
        {"role": "assistant", "content": '{"actions":[{"action":"category_add","name":"Инвестиции","emoji":"📈"}]}'},
        {"role": "user", "content": "100 баксів в гривні"},
        {"role": "assistant", "content": '{"actions":[{"action":"convert","amount":100,"from_currency":"USD","to_currency":"UAH"}]}'},
        {"role": "user", "content": "сколько потратил на еду?"},
        {"role": "assistant", "content": '{"actions":[{"action":"question","text":"сколько потратил на еду?"}]}'},
        {"role": "user", "content": text},
    ]
    try:
        raw = _llm(messages, max_tokens=500, temperature=0.0)
        raw = _extract_json(raw, "{")
        result = json.loads(raw)
        actions = result.get("actions", [result])
        return actions if isinstance(actions, list) else [actions]
    except Exception as e:
        logger.error(f"route_message error: {e} | raw: {raw if 'raw' in dir() else '?'}")
        return [{"action": "unknown"}]

async def execute_action(route: dict, update, context, chat_id: int, text: str, conv_ctx: dict) -> str | None:
    action = route.get("action", "unknown")

    # ── ТРАТА ──
    if action == "expense":
        expenses = route.get("expenses", [])
        if not expenses: return "🤔 Не понял сумму. Например: «Кофе 85»"
        date = datetime.now(KYIV_TZ).strftime("%d.%m.%Y %H:%M")
        month_recs = get_current_month_records()
        lines = ["✅ *Записано!*\n"]
        for exp in expenses:
            amount = float(str(exp.get("amount",0)).replace(",","."))
            if amount <= 0: continue
            cat = fix_cat(exp.get("category","Другое"))
            desc = exp.get("description","—")
            emoji = exp.get("emoji","")
            save_expense(date, amount, cat, desc, text)
            lines.append(f"{emoji} *{desc}* — *{fmt(amount)} ₴*\n   _{get_category_emoji(cat)} {cat}_")
        if len(expenses) > 1:
            total = sum(float(str(e.get("amount",0)).replace(",",".")) for e in expenses)
            lines.append(f"\n💰 *Итого: {fmt(total)} ₴*")
        cat0 = fix_cat(expenses[0].get("category","Другое"))
        sk = get_sum_key(month_recs)
        cat_total = sum(float(r[sk]) for r in month_recs if fix_cat(r.get("Категория",""))==cat0 and r.get(sk))
        if cat_total > 0:
            lines.append(f"\n_{get_category_emoji(cat0)} {cat0} за месяц: *{fmt(cat_total)} ₴*_")
        bs = get_budget_status(chat_id)
        if bs:
            pct = bs["percent"]
            if pct >= 90: lines.append(f"\n🔴 *Бюджет на {pct}%!*")
            elif pct >= 70: lines.append(f"\n🟡 Бюджет на {pct}%")
        set_ctx(chat_id, last_action="expense")
        return "\n".join(lines)

    # ── УДАЛИТЬ ТРАТУ ──
    elif action == "expense_delete":
        desc_hint = route.get("description","").lower() if route.get("description") else ""
        amount_hint = route.get("amount")
        cat_hint = route.get("category","").lower() if route.get("category") else ""
        try:
            all_recs = get_all_records()
            sh = get_sheet()
            sk = get_sum_key(all_recs)
            for i, r in enumerate(reversed(all_recs), 1):
                row_idx = len(all_recs) - i + 2
                r_desc = r.get("Описание","").lower()
                r_amt = str(r.get(sk,""))
                r_cat = r.get("Категория","").lower()
                desc_ok = desc_hint in r_desc if desc_hint else True
                amt_ok = str(int(float(amount_hint))) in r_amt if amount_hint else True
                cat_ok = cat_hint in r_cat if cat_hint else True
                if desc_ok and amt_ok and cat_ok:
                    sh.delete_rows(row_idx)
                    _invalidate("sheet1")
                    return f"🗑 Запись *{r.get('Описание','?')}* — *{r.get(sk,'?')} ₴* удалена."
            return "🤔 Не нашёл такую запись."
        except Exception as e:
            logger.error(f"expense_delete: {e}")
            return "❌ Не удалось удалить запись."

    # ── НОВЫЙ ДОЛГ ──
    elif action == "debt_new":
        name = str(route.get("name","")).strip().capitalize()
        ams = route.get("amounts", [])
        if not ams:
            amt = route.get("amount")
            cur = route.get("currency","UAH")
            if amt: ams = [{"amount":float(amt),"currency":cur}]
        if not name or not ams: return "🤔 Не понял кому и сколько."
        existing = next((k for k,d in debts.items() if name.lower() in d["name"].lower()), None)
        if existing:
            ex_ams = debts[existing].get("amounts",[])
            for na in ams:
                cur = na.get("currency","UAH")
                found = next((a for a in ex_ams if a.get("currency","UAH")==cur), None)
                if found: found["amount"] = float(found["amount"]) + float(na["amount"])
                else: ex_ams.append(na)
            debts[existing]["amounts"] = ex_ams
            update_debt_amounts(existing, ex_ams)
            set_ctx(chat_id, last_name=name, last_action="debt_add")
            return f"➕ *{name}* — долг обновлён\n💰 Итого: {format_amounts(ex_ams)}"
        note = route.get("note","")
        debt_counter[0] += 1
        did = str(debt_counter[0])
        date_str = datetime.now(KYIV_TZ).strftime("%d.%m.%Y")
        debts[did] = {"name":name,"amounts":ams,"date":date_str,"note":note}
        save_debt(did, name, ams, date_str, note)
        context.job_queue.run_once(send_debt_reminder, when=get_reminder_interval(chat_id),
            data={"debt_id":did,"chat_id":chat_id}, name=f"debt_{did}")
        set_ctx(chat_id, last_name=name, last_action="debt_new")
        note_str = f"\n📝 {note}" if note else ""
        return (f"💸 *Долг записан!*\n\n👤 *{name}*\n💰 {format_amounts(ams)}{note_str}\n\n"
                f"_«ещё 500» — добавит к долгу_\n⏰ Напомню через {reminder_label(chat_id)}.")

    # ── ДОБАВИТЬ К ДОЛГУ ──
    elif action == "debt_add":
        name = str(route.get("name", conv_ctx.get("last_name",""))).strip().capitalize()
        amount = float(str(route.get("amount",0)).replace(",","."))
        currency = route.get("currency","UAH")
        did = next((k for k,d in debts.items() if name.lower() in d["name"].lower()), None)
        if not did: return f"🤔 Не нашёл долга для *{name}*."
        ex_ams = debts[did].get("amounts",[])
        found = next((a for a in ex_ams if a.get("currency","UAH")==currency), None)
        if found: found["amount"] = float(found["amount"]) + amount
        else: ex_ams.append({"amount":amount,"currency":currency})
        debts[did]["amounts"] = ex_ams
        update_debt_amounts(did, ex_ams)
        set_ctx(chat_id, last_name=debts[did]["name"], last_action="debt_add")
        return f"➕ Добавил *{fmt(amount)} ₴* к *{debts[did]['name']}*\n💰 Итого: {format_amounts(ex_ams)}"

    # ── ВОЗВРАТ ДОЛГА ──
    elif action == "debt_return":
        name = str(route.get("name", conv_ctx.get("last_name",""))).strip().capitalize()
        did = next((k for k,d in debts.items() if name.lower() in d["name"].lower()), None)
        if not did: return f"🤔 Не нашёл долга для *{name}*."
        d = debts[did]
        ret_amount = route.get("amount")
        currency = route.get("currency","UAH")
        ex_ams = list(d.get("amounts",[]))
        lines = [f"💰 *{d['name']}* вернул:\n"]
        if ret_amount:
            for ea in ex_ams:
                if ea.get("currency","UAH") == currency:
                    new = float(ea["amount"]) - float(ret_amount)
                    sym = CURRENCY_SYMBOLS.get(currency,"₴")
                    if new <= 0:
                        ex_ams = [a for a in ex_ams if a.get("currency","UAH") != currency]
                        lines.append(f"✅ {sym}: долг закрыт")
                    else:
                        ea["amount"] = new
                        lines.append(f"💸 {sym}: вернул {fmt(ret_amount)} → остаток *{fmt(new)} {sym}*")
                    break
        else:
            ex_ams = []
            lines.append("✅ Всё вернул!")
        if not ex_ams:
            debts.pop(did); mark_paid(did); lines.append("\n🎉 *Долг полностью закрыт!*")
        else:
            debts[did]["amounts"] = ex_ams; update_debt_amounts(did, ex_ams)
            lines.append(f"\n📊 Остаток: {format_amounts(ex_ams)}")
        set_ctx(chat_id, last_name=d["name"], last_action="debt_return")
        return "\n".join(lines)

    # ── БЮДЖЕТ ──
    elif action == "budget_set":
        budget = float(str(route.get("amount",0)).replace(",","."))
        if budget <= 0: return "🤔 Не понял сумму бюджета."
        save_setting(f"budget_{chat_id}", str(budget))
        bs = get_budget_status(chat_id)
        bar = "█"*(bs["percent"]//10) + "░"*(10-bs["percent"]//10) if bs else "░░░░░░░░░░"
        pct = bs["percent"] if bs else 0
        return f"💰 *Бюджет: {fmt(budget)} ₴*\n[{bar}] {pct}% использовано"

    # ── ЗАРПЛАТА ──
    elif action == "salary_set":
        day = int(route.get("day", 1))
        amount = route.get("amount")
        if not 1 <= day <= 31: return "🤔 Не понял день зарплаты."
        set_salary_info(chat_id, day, float(amount) if amount else None)
        amt_str = f" — *{fmt(float(amount))} ₴*" if amount else ""
        return f"💵 *Зарплата: {day}-е число*{amt_str}"

    # ── НОВАЯ ЦЕЛЬ ──
    elif action == "goal_new":
        name = route.get("name","Цель")
        target = float(str(route.get("amount",0)).replace(",","."))
        emoji_g = route.get("emoji","🎯")
        if target <= 0: return "🤔 Не понял сумму цели."
        goal_counter[0] += 1
        gid = str(goal_counter[0])
        date_str = datetime.now(KYIV_TZ).strftime("%d.%m.%Y")
        goals[gid] = {"name":name,"target":target,"saved":0.0,"date":date_str,"emoji":emoji_g}
        save_goal_to_sheet(gid, name, target, 0, date_str, emoji_g)
        bar = build_goal_bar(0, target)
        return (f"{emoji_g} *Цель: {name}*\n[{bar}] 0%\n0 / {fmt(target)} ₴\n\n"
                f"_«Отложил 500» — буду отслеживать!_")

    # ── ПОПОЛНЕНИЕ ЦЕЛИ ──
    elif action == "goal_deposit":
        amount = float(str(route.get("amount",0)).replace(",","."))
        goal_name = route.get("goal_name","")
        if not goals: return "🎯 Целей нет. Создай: «Накопить на отпуск 60000»"
        if len(goals) == 1:
            gid = list(goals.keys())[0]
        elif goal_name:
            gid = next((k for k,g in goals.items() if goal_name.lower() in g["name"].lower()), list(goals.keys())[0])
        else:
            kb = [[(f"{g['emoji']} {g['name']}", f"goal_add_{gid}_{amount}")] for gid, g in goals.items()]
            await update.message.reply_text(f"💰 *{fmt(amount)} ₴* — к какой цели?",
                parse_mode="Markdown", reply_markup=inline_kb(kb))
            return None
        g = goals[gid]
        g["saved"] = min(g["saved"] + amount, g["target"])
        update_goal_saved(gid, g["saved"])
        pct = min(int(g["saved"]/g["target"]*100), 100)
        bar = build_goal_bar(g["saved"], g["target"])
        msg = f"🎯 *{g['name']}*\n[{bar}] {pct}%\n{fmt(g['saved'])} / {fmt(g['target'])} ₴"
        if g["saved"] >= g["target"]:
            msg += "\n\n🎉 *Цель достигнута!* Поздравляю!"
            goals.pop(gid); close_goal(gid)
        return msg

    # ── КОНВЕРТАЦИЯ ──
    elif action == "convert":
        amount = float(str(route.get("amount",0)).replace(",","."))
        from_cur = route.get("from_currency","USD").upper()
        to_cur = route.get("to_currency","UAH").upper()
        uah = await convert_to_uah(amount, from_cur)
        sym = CURRENCY_SYMBOLS.get(from_cur, from_cur)
        return f"💱 *{fmt(amount)} {sym}* = *{fmt(uah)} ₴*\n_(по курсу НБУ)_"

    # ── ДОБАВИТЬ КАТЕГОРИЮ ──
    elif action == "category_add":
        name = route.get("name","").strip().capitalize()
        emoji_c = route.get("emoji","")
        if not name: return "🤔 Не понял название категории."
        save_user_category(name, emoji_c)
        em = EMOJI_MAP.get(name, get_category_emoji(name))
        return f"✅ Категория {em} *{name}* добавлена!\nТеперь пиши: «{name} 1500»"

    # ── РЕДАКТИРОВАТЬ ЗАПИСЬ ──
    elif action == "expense_edit":
        old_cat = route.get("old_category","")
        new_cat = route.get("new_category","").strip().capitalize()
        amount = route.get("amount")
        desc_hint = route.get("description","").lower() if route.get("description") else ""
        if new_cat and new_cat not in get_all_categories():
            save_user_category(new_cat)
        try:
            all_recs = get_all_records()
            sh = get_sheet()
            sk = get_sum_key(all_recs)
            for i, r in enumerate(reversed(all_recs), 1):
                row_idx = len(all_recs) - i + 2
                cat_ok = old_cat.lower() in r.get("Категория","").lower() if old_cat else True
                amt_ok = str(int(float(amount))) in str(r.get(sk,"")) if amount else True
                desc_ok = desc_hint in r.get("Описание","").lower() if desc_hint else True
                if cat_ok and amt_ok and desc_ok:
                    headers = sh.row_values(1)
                    cat_col = headers.index("Категория") + 1 if "Категория" in headers else 3
                    sh.update_cell(row_idx, cat_col, new_cat)
                    _invalidate("sheet1")
                    amt_str = f" {fmt(float(amount))} ₴" if amount else ""
                    return f"✏️ Запись{amt_str}: *{old_cat or '?'}* → {get_category_emoji(new_cat)} *{new_cat}*"
            return f"🤔 Не нашёл запись в категории *{old_cat}*."
        except Exception as e:
            logger.error(f"expense_edit: {e}")
            return "❌ Не удалось обновить запись."

    # ── НАПОМИНАНИЕ О ДОЛГЕ ──
    elif action == "debt_remind":
        name = str(route.get("name", conv_ctx.get("last_name",""))).strip().capitalize()
        minutes = float(route.get("minutes", route.get("hours", 24) * 60))
        did = next((k for k,d in debts.items() if name.lower() in d["name"].lower()), None) if name else None
        if not did and debts: did = list(debts.keys())[-1]
        if not did: return "🤔 Нет активных долгов."
        d = debts[did]
        for job in context.job_queue.get_jobs_by_name(f"debt_{did}"):
            job.schedule_removal()
        context.job_queue.run_once(send_debt_reminder,
            when=timedelta(minutes=minutes),
            data={"debt_id":did,"chat_id":chat_id}, name=f"debt_{did}")
        if minutes < 60: label = f"{int(minutes)} минут"
        elif minutes == 60: label = "1 час"
        elif minutes < 1440: label = f"{int(minutes//60)} часов"
        elif minutes == 1440: label = "1 день"
        else: label = f"{int(minutes//1440)} дней"
        set_ctx(chat_id, last_name=d["name"], last_action="debt_remind")
        return f"⏰ Напомню о долге *{d['name']}* через *{label}*."

    # ── ВОПРОС ──
    elif action == "question":
        q = route.get("text", text)
        return await ai_chat_response(chat_id, q)

    return None


async def process(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    chat_id = update.effective_chat.id
    conv_ctx = get_ctx(chat_id)

    await update.message.reply_chat_action("typing")

    # Сначала пробуем быстрый regex-роутер (без LLM)
    regex_actions = _regex_route(text)
    if regex_actions:
        logger.info(f"RegexRouter: '{text[:50]}' → {[a.get('action') for a in regex_actions]}")
        responses = []
        for route in regex_actions:
            try:
                result = await execute_action(route, update, context, chat_id, text, conv_ctx)
                if result: responses.append(result)
            except Exception as e:
                logger.error(f"regex execute_action {route.get('action')}: {e}")
        if responses:
            combined = "\n\n".join(responses)
            await update.message.reply_text(combined if len(combined) <= 4096 else responses[0], parse_mode="Markdown")
            return

    actions = await route_message(text, chat_id, conv_ctx)
    logger.info(f"Router: '{text[:50]}' → {[a.get('action') for a in actions]}")

    last_msgs = conv_ctx.get("last_messages", [])
    last_msgs.append(text[:100])
    set_ctx(chat_id, last_messages=last_msgs[-10:])

    responses = []
    for route in actions:
        try:
            result = await execute_action(route, update, context, chat_id, text, conv_ctx)
            if result:
                responses.append(result)
        except Exception as e:
            logger.error(f"execute_action {route.get('action')}: {e}")

    if not responses:
        expenses = parse_expenses(text)
        if expenses:
            date = datetime.now(KYIV_TZ).strftime("%d.%m.%Y %H:%M")
            lines = ["✅ *Записано!*\n"]
            for exp in expenses:
                amount = float(str(exp.get("amount",0)))
                if amount <= 0: continue
                cat = fix_cat(exp.get("category","Другое"))
                desc = exp.get("description","—")
                emoji = exp.get("emoji","")
                save_expense(date, amount, cat, desc, text)
                lines.append(f"{emoji} *{desc}* — *{fmt(amount)} ₴*\n   _{get_category_emoji(cat)} {cat}_")
            responses.append("\n".join(lines))
        else:
            responses.append(
                "🤔 Не понял. Попробуй:\n"
                "• *Трата:* «Кофе 85» или «Бензин 1200»\n"
                "• *Долг:* «Дал Саше 500»\n"
                "• *Вопрос:* «Сколько потратил на еду?»"
            )

    combined = "\n\n".join(responses)
    if len(combined) <= 4096:
        await update.message.reply_text(combined, parse_mode="Markdown")
    else:
        for r in responses:
            await update.message.reply_text(r, parse_mode="Markdown")

# ── CALLBACK ─────────────────────────────────────────────────────────────────
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id

    async def send(text, **kw):
        await context.bot.send_message(chat_id=chat_id, text=text, **kw)

    # ── МЕНЮ ──
    if data.startswith("menu_"):
        action = data[5:]
        if action == "stats": await cmd_stats_inline(chat_id, context)
        elif action == "budget": await cmd_budget_inline(chat_id, context)
        elif action == "salary":
            s = build_salary_status(chat_id)
            await send(s or "💵 Зарплата не установлена.", parse_mode="Markdown")
        elif action == "compare": await send("⏳ Сравниваю..."); await send(build_comparison(), parse_mode="Markdown")
        elif action == "week": await send("⏳ Формирую..."); await send(build_weekly_report(), parse_mode="Markdown")
        elif action == "month": await send("⏳ Формирую..."); await send(build_monthly_report(), parse_mode="Markdown")
        elif action == "past":
            await send("⏳ ИИ анализирует твою динамику...")
            await send(await build_past_self_ai(chat_id), parse_mode="Markdown")
        elif action == "habits":
            await send("⏳ ИИ ищет паттерны...")
            await send(await build_habits_ai(), parse_mode="Markdown")
        elif action == "rates":
            await send("⏳ Запрашиваю курс...")
            await send(await build_rates_msg(), parse_mode="Markdown")
        elif action == "advice":
            await send("⏳ ИИ готовит персональные советы...")
            await send(await build_advice_ai(chat_id), parse_mode="Markdown")
        elif action == "reminder":
            cur = reminder_label(chat_id)
            await context.bot.send_message(chat_id=chat_id,
                text=f"⏰ *Напоминания*\n\nТекущий: *{cur}*\n\nВыбери:", parse_mode="Markdown", reply_markup=REMINDER_KB)
        elif action == "categories":
            cats = get_all_categories()
            lines = ["🏷 *Категории:*\n"]
            lines += [f"{get_category_emoji(c)} {c}" for c in cats]
            if not _user_categories:
                lines.append("\n_Добавь свою: напиши «Добавь категорию Инвестиции»_")
            await send("\n".join(lines), parse_mode="Markdown")
        return

    # ── ЦЕЛИ — ПОПОЛНЕНИЕ ──
    if data == "goal_deposit":
        if not goals:
            await query.edit_message_text("🎯 Целей нет. Добавь: «Цель iPhone 25000»"); return
        kb = [[(f"{g['emoji']} {g['name']}", f"goal_pick_{gid}")] for gid, g in goals.items()]
        await query.edit_message_text("🎯 Выбери цель для пополнения:", reply_markup=inline_kb(kb)); return

    if data.startswith("goal_pick_"):
        gid = data[10:]
        if gid not in goals: await query.edit_message_text("Цель не найдена."); return
        context.user_data["goal_deposit_id"] = gid
        g = goals[gid]
        await query.edit_message_text(f"💰 Пополнение цели *{g['name']}*\nНапиши сумму в чат:", parse_mode="Markdown"); return

    if data.startswith("goal_add_"):
        parts = data.split("_")
        gid = parts[2]
        amount = float(parts[3])
        if gid not in goals: return
        g = goals[gid]
        g["saved"] = min(g["saved"] + amount, g["target"])
        update_goal_saved(gid, g["saved"])
        pct = min(int(g["saved"]/g["target"]*100), 100)
        bar = build_goal_bar(g["saved"], g["target"])
        msg = f"🎯 *{g['name']}*\n[{bar}] {pct}%\n{fmt(g['saved'])} / {fmt(g['target'])} ₴"
        if g["saved"] >= g["target"]:
            msg += "\n\n🎉 *Цель достигнута!*"
            goals.pop(gid); close_goal(gid)
        await query.edit_message_text(msg, parse_mode="Markdown"); return

    if data == "goal_close":
        if not goals: await query.edit_message_text("🎯 Целей нет."); return
        kb = [[(f"🗑 {g['emoji']} {g['name']}", f"goal_del_{gid}")] for gid, g in goals.items()]
        kb.append([("← Назад","back_goals")])
        await query.edit_message_text("Какую цель закрыть?", reply_markup=inline_kb(kb)); return

    if data.startswith("goal_del_"):
        gid = data[9:]
        if gid in goals:
            g = goals.pop(gid)
            close_goal(gid)
            await query.edit_message_text(f"🗑 Цель *{g['name']}* удалена.", parse_mode="Markdown")
        return

    if data == "back_goals":
        await query.edit_message_text(build_goals_msg(), parse_mode="Markdown",
            reply_markup=inline_kb([[("➕ Пополнить цель","goal_deposit"),("🗑 Закрыть цель","goal_close")]])); return

    # ── ИНТЕРВАЛ НАПОМИНАНИЙ ──
    if data.startswith("reminder_") and not data.startswith("reminder_date"):
        days = int(data[9:])
        set_reminder_interval(chat_id, days)
        label = DAYS_LABELS.get(days, f"{days} дней")
        await query.edit_message_text(f"✅ Интервал установлен: *{label}*", parse_mode="Markdown")
        return

    # ── БЫСТРЫЙ РЕЖИМ ──
    if data.startswith("quick_"):
        _, cat, amt_str = data.split("_", 2)
        amount = float(amt_str)
        date = datetime.now(KYIV_TZ).strftime("%d.%m.%Y %H:%M")
        save_expense(date, amount, cat, "быстрая запись", str(amount))
        await query.edit_message_text(
            f"⚡ *{fmt(amount)} ₴* → {get_category_emoji(cat)} {cat}\n✅ Записано!", parse_mode="Markdown")
        return

    # ── ДОЛГИ — СПИСОК ──
    if data == "show_debts":
        if not debts: await query.edit_message_text("✅ Активных долгов нет!"); return
        kb = []
        for did, d in debts.items():
            ams = d.get("amounts",[{"amount":d.get("amount",0),"currency":"UAH"}])
            amt = format_amounts(ams).replace("*","")
            kb.append([(f"👤 {d['name']} — {amt}", f"debt_menu_{did}")])
        kb.append([("← Назад","back")])
        await query.edit_message_text("Выбери долг:", reply_markup=inline_kb(kb))
        return

    if data.startswith("debt_menu_"):
        did = data[10:]
        if did not in debts: await query.edit_message_text("Долг уже закрыт."); return
        d = debts[did]
        ams = d.get("amounts",[{"amount":d.get("amount",0),"currency":"UAH"}])
        cur_remind = get_setting(f"debt_reminder_{did}") or reminder_label(chat_id)
        note = f"\n📝 {d['note']}" if d.get("note") else ""
        kb = inline_kb([
            [("✅ Вернули полностью", f"paid_{did}")],
            [("💰 Частичное погашение", f"partial_{did}")],
            [(f"⏰ {cur_remind}", f"debt_remind_settings_{did}")],
            [("← Назад","show_debts")],
        ])
        await query.edit_message_text(
            f"👤 *{d['name']}* — {format_amounts(ams)}{note}\n📅 {d['date']}\n\nЧто сделать?",
            parse_mode="Markdown", reply_markup=kb)
        return

    if data.startswith("debt_remind_settings_"):
        did = data[21:]
        if did not in debts: await query.edit_message_text("Долг уже закрыт."); return
        d = debts[did]
        cur = get_setting(f"debt_reminder_{did}") or reminder_label(chat_id)
        kb = inline_kb([
            [("1 день",f"dremind_{did}_1"),("3 дня",f"dremind_{did}_3")],
            [("1 неделю",f"dremind_{did}_7"),("2 недели",f"dremind_{did}_14")],
            [("3 недели",f"dremind_{did}_21"),("1 месяц",f"dremind_{did}_30")],
            [("← Назад",f"debt_menu_{did}")],
        ])
        await query.edit_message_text(
            f"⏰ *Напоминание для {d['name']}*\n\nТекущее: *{cur}*\n\nВыбери:",
            parse_mode="Markdown", reply_markup=kb)
        return

    if data.startswith("dremind_"):
        parts = data.split("_")
        did, days = parts[1], int(parts[2])
        if did not in debts: await query.edit_message_text("Долг уже закрыт."); return
        d = debts[did]
        for job in context.job_queue.get_jobs_by_name(f"debt_{did}"): job.schedule_removal()
        context.job_queue.run_once(send_debt_reminder, when=timedelta(days=days),
            data={"debt_id":did,"chat_id":chat_id}, name=f"debt_{did}")
        label = DAYS_LABELS.get(days, f"{days} дней")
        save_setting(f"debt_reminder_{did}", label)
        await query.edit_message_text(f"✅ Напомню о *{d['name']}* через *{label}*.", parse_mode="Markdown")
        return

    if data.startswith("paid_"):
        did = data[5:]
        if did in debts:
            d = debts.pop(did)
            mark_paid(did)
            ams = d.get("amounts",[{"amount":d.get("amount",0),"currency":"UAH"}])
            await query.edit_message_text(
                f"✅ *{d['name']}* вернул {format_amounts(ams)}\n\n🎉 Долг закрыт!",
                parse_mode="Markdown")
        else:
            await query.edit_message_text("Долг уже закрыт.")
        return

    if data.startswith("partial_"):
        did = data[8:]
        if did not in debts: await query.edit_message_text("Долг уже закрыт."); return
        d = debts[did]
        ams = d.get("amounts",[{"amount":d.get("amount",0),"currency":"UAH"}])
        kb = []
        for i, a in enumerate(ams):
            sym = CURRENCY_SYMBOLS.get(a.get("currency","UAH"),"₴")
            kb.append([(f"💰 В {sym} (осталось {fmt(a['amount'])} {sym})", f"partialcur_{did}_{i}")])
        kb.append([(f"← Назад", f"debt_menu_{did}")])
        await query.edit_message_text("💰 Частичное погашение — выбери валюту:", reply_markup=inline_kb(kb))
        return

    if data.startswith("partialcur_"):
        parts = data.split("_")
        did, idx = parts[1], int(parts[2])
        if did not in debts: await query.edit_message_text("Долг уже закрыт."); return
        ams = debts[did].get("amounts",[])
        if idx >= len(ams): return
        sym = CURRENCY_SYMBOLS.get(ams[idx].get("currency","UAH"),"₴")
        context.user_data["partial_debt_id"] = did
        context.user_data["partial_amt_idx"] = idx
        await query.edit_message_text(f"💰 Сколько вернули в {sym}?\n\nНапиши сумму в чат:", parse_mode="Markdown")
        return

    if data.startswith("remind_"):
        did = data[7:]
        if did in debts:
            d = debts[did]
            interval = get_reminder_interval(chat_id)
            for job in context.job_queue.get_jobs_by_name(f"debt_{did}"): job.schedule_removal()
            context.job_queue.run_once(send_debt_reminder, when=interval,
                data={"debt_id":did,"chat_id":chat_id}, name=f"debt_{did}")
            await query.edit_message_text(f"⏰ Напомню о *{d['name']}* через {reminder_label(chat_id)}.", parse_mode="Markdown")
        return

    if data == "back":
        await query.edit_message_text("Выбери раздел:", reply_markup=inline_kb([
            [("💰 Финансы","menu_stats"),("📊 Аналитика","menu_week")],
            [("💸 Долги","show_debts"),("🎯 Цели","back_goals")],
        ])); return

# ── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    for cmd, handler in [
        ("start",cmd_start), ("stats",cmd_stats), ("week",cmd_week),
        ("month",cmd_month), ("budget",cmd_budget), ("salary",cmd_salary),
        ("debts",cmd_debts), ("reminder",cmd_reminder), ("goals",cmd_goals),
        ("rates",cmd_rates),
    ]:
        app.add_handler(CommandHandler(cmd, handler))

    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    import time
    load_settings()
    time.sleep(1)
    load_user_categories()
    time.sleep(1)
    load_debts()
    time.sleep(1)
    load_goals()

    if CHAT_ID and app.job_queue:
        app.job_queue.run_daily(
            send_weekly_insight,
            time=dtime(19, 0),
            days=(4,), data={"chat_id": CHAT_ID})
        app.job_queue.run_daily(
            send_morning_briefing,
            time=dtime(9, 0),
            data={"chat_id": CHAT_ID})

    logger.info("AI-агент запущен! v5.2 🤖")
    app.run_polling()

if __name__ == "__main__":
    main()
