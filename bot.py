"""
Финансовый AI-агент Telegram бот v4.1
Умный парсер: ИИ понимает любой контекст, сумму и категорию без шаблонов
"""
import os, logging, tempfile, json, re, asyncio
from datetime import datetime, timedelta, time as dtime
from collections import defaultdict
from dotenv import load_dotenv

load_dotenv()

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from groq import Groq
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

# Шаблоны убраны намеренно — ИИ сам определяет категории по смыслу

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
    return sp.sheet1 if name == "sheet1" else (_worksheet_get_or_create(sp, name))

def _worksheet_get_or_create(sp, name):
    try: return sp.worksheet(name)
    except: return sp.add_worksheet(title=name, rows=200, cols=10)

def _worksheet_exists(sp, name):
    try: sp.worksheet(name); return True
    except: return False

def _invalidate(name="sheet1"):
    _records_cache.pop(name, None)

def _cached_records(name="sheet1") -> list:
    now = datetime.now().timestamp()
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

def save_user_category(cat: str):
    if cat not in _user_categories:
        _user_categories.append(cat)
        save_setting("user_categories", json.dumps(_user_categories))

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
    """Принимает категорию от ИИ — без шаблонов, только мягкое сопоставление"""
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

def records_for_month(month: int, year: int, all_recs=None) -> list:
    recs = all_recs or get_all_records()
    result = []
    for r in recs:
        try:
            d = datetime.strptime(r.get("Дата","")[:10], "%d.%m.%Y")
            if d.month == month and d.year == year: result.append(r)
        except: pass
    return result

def get_current_month_records() -> list:
    now = datetime.now()
    return records_for_month(now.month, now.year)

def get_week_records() -> list:
    week_ago = datetime.now() - timedelta(days=7)
    result = []
    for r in get_all_records():
        try:
            if datetime.strptime(r.get("Дата","")[:10], "%d.%m.%Y") >= week_ago:
                result.append(r)
        except: pass
    return result

def get_today_records() -> list:
    today = datetime.now().strftime("%d.%m.%Y")
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
            d = datetime.strptime(r.get("Дата","")[:10], "%d.%m.%Y")
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
    # Мягкий поиск по части слова
    low = cat.lower()
    emoji_hints = [
        (["еда","продукт","кафе","ресторан","обед","пицца","суши","фаст"],"🍔"),
        (["транспорт","такси","бензин","авто","машин","метро","автобус"],"🚗"),
        (["развлеч","игр","кино","steam","netflix","spotify","боулинг"],"🎮"),
        (["здоров","аптека","врач","медиц","стоматолог","фитнес","спортзал"],"💊"),
        (["никотин","снюс","вейп","сигарет","кальян"],"🚬"),
        (["одежда","обувь","шопинг"],"👕"),
        (["коммунальн","квартир","жкх","аренд"],"🏠"),
        (["подписк","netflix","spotify","apple"],"📺"),
        (["образован","курс","книг","учеб"],"📚"),
        (["путешеств","отпуск","авиа","билет","гостиниц"],"✈️"),
        (["спорт","трениров","зал","фитнес"],"💪"),
    ]
    for keywords, emoji in emoji_hints:
        if any(k in low for k in keywords):
            return emoji
    return "📦"

def add_emoji_to_desc(desc: str, emoji_hint: str = "") -> str:
    """Возвращает описание с эмодзи. Приоритет — эмодзи от ИИ, затем авто-поиск"""
    if emoji_hint:
        return f"{emoji_hint} {desc}"
    return desc

def month_name(n: int, gen=False) -> str:
    return (MONTH_NAMES_GEN if gen else MONTH_NAMES)[n-1]

def fmt(amt: float) -> str:
    return f"{amt:,.0f}"

# ── ПАМЯТЬ ───────────────────────────────────────────────────────────────────
memory: dict = {}

def load_memory():
    val = get_setting("user_memory")
    if val:
        try: memory.update(json.loads(val))
        except: pass

# ── ПАМЯТЬ (устарела — ИИ сам определяет категории) ──────────────────────────
memory: dict = {}  # оставляем для совместимости с load_settings

def load_memory():
    pass  # больше не нужна — ИИ понимает контекст без словаря

def update_memory(keyword: str, category: str):
    pass  # ИИ не нуждается в обучении по ключевым словам

# ── БЮДЖЕТ ───────────────────────────────────────────────────────────────────
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
    now = datetime.now()
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

# ── ФИНАНСОВЫЕ ЦЕЛИ (НОВОЕ) ───────────────────────────────────────────────────
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

async def fetch_nbu_rates() -> dict:
    global _rates_cache, _rates_ts
    now = datetime.now().timestamp()
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

async def build_rates_msg() -> str:
    rates = await fetch_nbu_rates()
    usd = rates.get("USD", 0)
    eur = rates.get("EUR", 0)
    gbp = rates.get("GBP", 0)
    lines = [
        "💱 *Курс НБУ (актуальный):*\n",
        f"🇺🇸 USD: *{usd:.2f} ₴*",
        f"🇪🇺 EUR: *{eur:.2f} ₴*",
        f"🇬🇧 GBP: *{gbp:.2f} ₴*",
    ]
    return "\n".join(lines)

# ── КОНТЕКСТ РАЗГОВОРА ───────────────────────────────────────────────────────
# Хранит последний контекст: имя человека, последнее действие
_conv_context: dict = {}  # chat_id -> {"last_name": str, "last_action": str}

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

def build_debts_msg() -> str:
    if not debts: return "✅ Активных долгов нет!"
    lines = ["💸 *Активные долги:*\n"]
    totals: dict = defaultdict(float)
    for d in debts.values():
        days_ago = (datetime.now() - datetime.strptime(d["date"], "%d.%m.%Y")).days
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

# ── GROQ ─────────────────────────────────────────────────────────────────────
def transcribe(path: str) -> str:
    with open(path, "rb") as f:
        return groq_client.audio.transcriptions.create(
            model="whisper-large-v3", file=f, language="ru").text

def _llm(messages: list, max_tokens=600, temperature=0.0) -> str:
    """Базовый вызов LLM. temperature=0 для парсинга, выше — для чата."""
    r = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    return r.choices[0].message.content.strip()

def _extract_json(raw: str, bracket="[") -> str:
    """Вырезает первый валидный JSON из ответа модели."""
    close = "]" if bracket == "[" else "}"
    raw = raw.replace("```json","").replace("```","").strip()
    s = raw.find(bracket)
    e = raw.rfind(close)
    if s != -1 and e != -1:
        return raw[s:e+1]
    return raw

def groq_chat(messages: list, max_tokens=800) -> str:
    return _llm(messages, max_tokens=max_tokens, temperature=0.7)

# ── УМНЫЙ ПАРСЕР РАСХОДОВ ────────────────────────────────────────────────────
PARSE_SYSTEM = """Ты — парсер финансовых записей. Твоя задача: извлечь ВСЕ траты из сообщения пользователя.

КАТЕГОРИИ (выбирай наиболее подходящую по смыслу):
- "Еда / продукты" — любая еда, напитки, рестораны, кафе, доставка, продуктовые магазины, алкоголь
- "Транспорт" — бензин, заправки, такси, каршеринг, парковка, мойка, СТО, запчасти, метро, автобус
- "Развлечения" — игры, кино, стриминг, подписки на развлечения, ставки, боулинг, клубы, концерты
- "Здоровье / аптека" — лекарства, аптека, врачи, стоматолог, анализы, массаж, парикмахер, маникюр, педикюр, спортзал, фитнес
- "Никотин" — сигареты, снюс, вейп, кальян, ZYN, VELO и любые никотиновые продукты
- "Другое" — всё остальное: одежда, техника, коммунальные, интернет, телефон, подарки, ремонт

ПРАВИЛА ПАРСИНГА:
1. Ищи ВСЕ суммы в тексте — даже если написано "потратил", "заплатил", "купил", "вышло"
2. Понимай контекст: "взял кофе 85" = Еда, "закинул на карту 500" = НЕ трата (пропусти)
3. Понимай сокращения и разговорный язык: "шаурма 120", "синька 200", "ашан 3к", "комуналка 1500"
4. "к" или "тыс" = тысячи: "3к" = 3000, "1.5к" = 1500
5. Если несколько трат через запятую/и/плюс — создай отдельный объект для каждой
6. description — короткое название (2-4 слова), что именно куплено
7. emoji — один подходящий эмодзи для описания
8. Если текст НЕ про расходы (вопрос, команда, разговор) — верни пустой массив []

ФОРМАТ ОТВЕТА — ТОЛЬКО JSON, никакого текста вокруг:
[{"amount": <число>, "category": "<категория>", "description": "<что купил>", "emoji": "<эмодзи>"}]"""

PARSE_EXAMPLES = [
    {"role":"user","content":"снюс 800"},
    {"role":"assistant","content":'[{"amount":800,"category":"Никотин","description":"снюс","emoji":"🚬"}]'},
    {"role":"user","content":"зашёл в атб, взял еды и воды, вышло 340"},
    {"role":"assistant","content":'[{"amount":340,"category":"Еда / продукты","description":"ATБ продукты","emoji":"🛒"}]'},
    {"role":"user","content":"мойка 350, залил бензин 1200"},
    {"role":"assistant","content":'[{"amount":350,"category":"Транспорт","description":"мойка машины","emoji":"🚿"},{"amount":1200,"category":"Транспорт","description":"бензин","emoji":"⛽"}]'},
    {"role":"user","content":"купил кроссовки за 2к и носки за 150"},
    {"role":"assistant","content":'[{"amount":2000,"category":"Другое","description":"кроссовки","emoji":"👟"},{"amount":150,"category":"Другое","description":"носки","emoji":"🧦"}]'},
    {"role":"user","content":"сколько я потратил на еду?"},
    {"role":"assistant","content":"[]"},
    {"role":"user","content":"пополнил счёт 500"},
    {"role":"assistant","content":"[]"},
    {"role":"user","content":"стоматолог 1800"},
    {"role":"assistant","content":'[{"amount":1800,"category":"Здоровье / аптека","description":"стоматолог","emoji":"🦷"}]'},
    {"role":"user","content":"netflix 199, spotify 99, купил донат в игре 300"},
    {"role":"assistant","content":'[{"amount":199,"category":"Развлечения","description":"Netflix подписка","emoji":"🎬"},{"amount":99,"category":"Развлечения","description":"Spotify подписка","emoji":"🎵"},{"amount":300,"category":"Развлечения","description":"донат в игре","emoji":"🎮"}]'},
    {"role":"user","content":"коммуналка 2400"},
    {"role":"assistant","content":'[{"amount":2400,"category":"Другое","description":"коммунальные услуги","emoji":"🏠"}]'},
    {"role":"user","content":"обед с клиентом 850, такси туда-обратно 280"},
    {"role":"assistant","content":'[{"amount":850,"category":"Еда / продукты","description":"бизнес-обед","emoji":"🍽"},{"amount":280,"category":"Транспорт","description":"такси","emoji":"🚕"}]'},
]

def parse_expenses(text: str) -> list:
    """
    Умный ИИ-парсер трат. Понимает контекст, сленг, сокращения.
    Без единого hardcoded ключевого слова.
    """
    user_cats = _user_categories
    system = PARSE_SYSTEM
    if user_cats:
        system += f"\n\nДОПОЛНИТЕЛЬНЫЕ КАТЕГОРИИ ПОЛЬЗОВАТЕЛЯ: {', '.join(user_cats)}"

    messages = [{"role":"system","content":system}]
    messages.extend(PARSE_EXAMPLES)
    messages.append({"role":"user","content":text})

    try:
        raw = _llm(messages, max_tokens=500, temperature=0.0)
        raw = _extract_json(raw, "[")
        result = json.loads(raw)
        if isinstance(result, dict): result = [result]
        # Валидация: убираем записи без amount или с нулём
        validated = []
        for item in result:
            try:
                amt = float(str(item.get("amount","0")).replace(",",".").replace("к","000").replace("k","000"))
                if amt <= 0: continue
                item["amount"] = amt
                item["category"] = fix_cat(item.get("category","Другое"))
                validated.append(item)
            except: continue
        return validated
    except Exception as e:
        logger.error(f"parse_expenses error: {e} | raw: {raw if 'raw' in dir() else '?'}")
        # Fallback: хотя бы вытащи числа
        nums = re.findall(r'\b(\d[\d\s]*(?:[.,]\d+)?)\s*(?:к|тыс|грн|₴|uah)?\b', text.lower())
        if nums:
            amount_str = nums[0].replace(" ","").replace(",",".")
            try:
                amount = float(amount_str) * (1000 if "к" in text.lower() or "тыс" in text.lower() else 1)
                return [{"amount": amount, "category": "Другое", "description": text[:40], "emoji": "📦"}]
            except: pass
        return []

def parse_debt(text: str, context_name: str = "") -> dict:
    """
    Парсит долг из текста. context_name — имя из предыдущего сообщения (если есть).
    Если несколько сумм одному человеку — суммирует в один долг.
    """
    ctx = f'\nКонтекст: предыдущее сообщение было про "{context_name}", используй это имя если в тексте нет другого.' if context_name else ""
    messages = [
        {"role":"system","content":f"""Извлеки информацию о долге из текста.{ctx}

Верни ТОЛЬКО JSON без markdown:
{{"name":"<имя человека>","amounts":[{{"amount":<число>,"currency":"<UAH|USD|EUR>"}}],"note":"<за что или пусто>"}}

Правила:
- name: только имя (Папа, Саша, Вася) — без слов "долг", "дал" и т.д.
- Если несколько сумм одному человеку ("папа 200 и ещё 800") — СЛОЖИ их в одну: amount=1000
- Валюта: гривна/грн/₴=UAH, доллар/бакс/$=USD, евро/€=EUR. Без валюты=UAH
- amounts всегда массив

Примеры:
"дал папе 200" → {{"name":"Папа","amounts":[{{"amount":200,"currency":"UAH"}}],"note":""}}
"папа 200 и ещё 800" → {{"name":"Папа","amounts":[{{"amount":1000,"currency":"UAH"}}],"note":""}}
"одолжил Саше 50 баксов на еду" → {{"name":"Саша","amounts":[{{"amount":50,"currency":"USD"}}],"note":"на еду"}}"""},
        {"role":"user","content":text},
    ]
    try:
        raw = _llm(messages, max_tokens=250, temperature=0.0)
        raw = _extract_json(raw, "{")
        result = json.loads(raw)
        # Нормализация имени
        if result.get("name"):
            result["name"] = result["name"].strip().capitalize()
        return result
    except Exception as e:
        logger.error(f"parse_debt: {e} | text: {text}")
        return {}

def parse_goal(text: str) -> dict | None:
    messages = [
        {"role":"system","content":"""Извлеки финансовую цель из текста.
Верни ТОЛЬКО JSON без markdown:
{"name":"<название цели>","amount":<целевая сумма>,"emoji":"<подходящий эмодзи>"}
Примеры: "Цель iPhone 25000" → {"name":"iPhone","amount":25000,"emoji":"📱"}
"хочу накопить на машину 200к" → {"name":"Машина","amount":200000,"emoji":"🚗"}"""},
        {"role":"user","content":text},
    ]
    try:
        raw = _llm(messages, max_tokens=150, temperature=0.0)
        raw = _extract_json(raw, "{")
        result = json.loads(raw)
        if result.get("amount"): return result
    except Exception as e:
        logger.error(f"parse_goal: {e}")
    return None

# ── ИИ ФИНАНСОВЫЙ СОВЕТНИК (НОВОЕ) ────────────────────────────────────────────
# История чата per chat_id
_ai_chat_history: dict = {}

def get_financial_context(chat_id) -> str:
    """Собирает финансовый контекст для ИИ"""
    recs = get_current_month_records()
    s = analyze_records(recs)
    bs = get_budget_status(chat_id)
    sal = get_salary_info(chat_id)
    
    parts = [f"Сегодня: {datetime.now().strftime('%d.%m.%Y')}"]
    
    if s:
        parts.append(f"Траты за {month_name(datetime.now().month)}: {fmt(s['total'])} ₴")
        cats = "; ".join(f"{c}: {fmt(a)}₴" for c,a in sorted(s["by_category"].items(), key=lambda x:-x[1])[:4])
        parts.append(f"По категориям: {cats}")
    
    if bs:
        parts.append(f"Бюджет: {fmt(bs['budget'])}₴, потрачено {bs['percent']}%, осталось {fmt(bs['left'])}₴")
    
    if sal:
        parts.append(f"Зарплата: {sal.get('amount','?')}₴, {sal['day']}-е число")
    
    if debts:
        debt_list = "; ".join(f"{d['name']}: {format_amounts(d['amounts']).replace('*','')}" for d in list(debts.values())[:3])
        parts.append(f"Активные долги: {debt_list}")
    
    if goals:
        goal_list = "; ".join(f"{g['name']}: {fmt(g['saved'])}/{fmt(g['target'])}₴" for g in list(goals.values())[:3])
        parts.append(f"Финансовые цели: {goal_list}")
    
    return "\n".join(parts)

async def ai_chat_response(chat_id, user_message: str) -> str:
    """ИИ-агент с памятью финансового контекста"""
    if chat_id not in _ai_chat_history:
        _ai_chat_history[chat_id] = []
    
    history = _ai_chat_history[chat_id]
    financial_ctx = get_financial_context(chat_id)
    
    system = f"""Ты умный финансовый ИИ-ассистент в Telegram боте. Помогаешь анализировать расходы, давать советы, отвечать на вопросы о финансах.
    
ТЕКУЩИЕ ДАННЫЕ ПОЛЬЗОВАТЕЛЯ:
{financial_ctx}

Правила:
- Отвечай кратко и по делу (до 5 предложений)
- Используй данные пользователя в ответах
- Будь дружелюбным, используй эмодзи
- Отвечай на русском языке
- Если спрашивают о конкретных цифрах — используй данные выше
- Давай практичные советы"""

    messages = [{"role":"system","content":system}]
    # Последние 6 сообщений истории
    messages.extend(history[-6:])
    messages.append({"role":"user","content":user_message})
    
    try:
        response = groq_chat(messages)
        history.append({"role":"user","content":user_message})
        history.append({"role":"assistant","content":response})
        if len(history) > 20:
            _ai_chat_history[chat_id] = history[-20:]
        return response
    except Exception as e:
        logger.error(f"ai_chat: {e}")
        return "🤔 Не могу ответить сейчас. Попробуй чуть позже!"

# ── ОТЧЁТЫ ───────────────────────────────────────────────────────────────────
def _cat_lines(stats, limit=None) -> list:
    items = sorted(stats["by_category"].items(), key=lambda x:-x[1])
    if limit: items = items[:limit]
    lines = []
    for cat, amt in items:
        pct = int(amt / stats["total"] * 100)
        lines.append(f"{get_category_emoji(cat)} {cat}: *{fmt(amt)} ₴* ({pct}%)")
    return lines

def _leak_lines(stats) -> list:
    if not stats.get("leaks"): return []
    lines = ["\n💸 *Частые траты:*"]
    for desc, d in list(stats["leaks"].items())[:3]:
        lines.append(f"• {desc}: {d['count']}× = *{fmt(d['total'])} ₴*")
    return lines

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
    now = datetime.now()
    avg = s["total"] / now.day if now.day else 0
    lines = [f"📆 *Отчёт за {month_name(now.month)} {now.year}*\n",
             f"💰 Потрачено: *{fmt(s['total'])} ₴* за {now.day} дней",
             f"📊 В среднем: *{fmt(avg)} ₴/день*",
             f"📈 Прогноз: *~{fmt(avg*30)} ₴*\n",
             "*Топ категории:*"] + _cat_lines(s, 5) + _leak_lines(s)
    return "\n".join(lines)

def build_comparison() -> str:
    all_recs = get_all_records()
    now = datetime.now()
    months = {}
    for r in all_recs:
        try:
            d = datetime.strptime(r.get("Дата","")[:10], "%d.%m.%Y")
            months.setdefault((d.year,d.month),[]).append(r)
        except: pass
    if len(months) < 2: return "📭 Нужно минимум 2 месяца данных."
    lines = ["📊 *Сравнение месяцев*\n"]
    prev_total = None
    for ym in sorted(months, reverse=True)[:3]:
        s = analyze_records(months[ym])
        name = f"{month_name(ym[1])} {ym[0]}"
        if prev_total:
            diff = int((s["total"]-prev_total)/prev_total*100)
            arrow = "📈" if diff>0 else "📉"
            lines.append(f"*{name}*: {fmt(s['total'])} ₴ {arrow} {'+' if diff>0 else ''}{diff}%")
        else:
            lines.append(f"*{name}*: {fmt(s['total'])} ₴")
        if s["by_category"]:
            tc = max(s["by_category"], key=s["by_category"].get)
            lines.append(f"  └ Топ: {get_category_emoji(tc)} {tc} — {fmt(s['by_category'][tc])} ₴")
        prev_total = s["total"]
    return "\n".join(lines)

def build_past_self() -> str:
    all_recs = get_all_records()
    now = datetime.now()
    def ms(ago):
        t = now.replace(day=1)
        for _ in range(ago): t = (t-timedelta(days=1)).replace(day=1)
        return analyze_records(records_for_month(t.month, t.year, all_recs)), t
    cur = analyze_records(get_current_month_records())
    if not cur: return "📭 Недостаточно данных."
    lines = ["🪞 *Сравнение с прошлым «я»*\n"]
    oldest = None
    for ago, label in [(1,"1 месяц назад"),(2,"2 месяца назад"),(3,"3 месяца назад")]:
        s, t = ms(ago)
        if not s: continue
        diff = int((cur["total"]-s["total"])/s["total"]*100)
        arrow = "📈" if diff>0 else "📉"
        sign = "+" if diff>0 else ""
        verb = "больше" if diff>0 else "меньше"
        lines.append(f"{arrow} *{label}* ({month_name(t.month)}):\n   {sign}{diff}% — тратишь на *{fmt(abs(cur['total']-s['total']))} ₴ {verb}*")
        oldest = (s, label)
    if oldest:
        s, label = oldest
        diffs = [(cat, cur["by_category"].get(cat,0)-s["by_category"].get(cat,0))
                 for cat in set(list(cur["by_category"])+list(s["by_category"]))]
        cat_lines = [f"{get_category_emoji(c)} {c}: {'📈 +' if d>0 else '📉 '}{fmt(d)} ₴"
                     for c,d in diffs if abs(d)>100]
        if cat_lines:
            lines.append(f"\n📊 *Изменения vs {label}:*")
            lines += cat_lines
    return "\n".join(lines)

def build_habits() -> str:
    all_recs = get_all_records()
    months: dict = {}
    for r in all_recs:
        try:
            d = datetime.strptime(r.get("Дата","")[:10], "%d.%m.%Y")
            months.setdefault((d.year,d.month),[]).append(r)
        except: pass
    if not months: return "📭 Недостаточно данных."
    n = max(len(months),1)
    desc_data: dict = defaultdict(lambda:{"total":0.0,"count":0,"months":set()})
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
    if not habits: return "📭 Пока мало данных. Записывай траты ещё несколько недель!"
    lines = ["💸 *Стоимость привычек*\n"]
    for desc, d in sorted(habits.items(), key=lambda x:-x[1]["total"])[:6]:
        monthly = d["total"]/n
        annual = monthly*12
        equiv = next((label for thr,label in EQUIVALENTS if annual>=thr*0.7), None)
        lines += [f"*{desc.capitalize()}*",
                  f"  📅 В месяц: *{fmt(monthly)} ₴*",
                  f"  📆 В год: *{fmt(annual)} ₴*"]
        if equiv: lines.append(f"  💡 Это = {equiv}")
        lines.append("")
    return "\n".join(lines)

def build_advice(records: list) -> str:
    if not records or len(records) < 5: return ""
    s = analyze_records(records)
    total = s["total"]
    bc = s["by_category"]
    tips = []
    food = bc.get("Еда / продукты",0)
    if food > total*0.35:
        tips.append(f"🍔 На еду {int(food/total*100)}%. Сократить на 25% = *+{fmt(food*0.25)} ₴/мес*")
    ent = bc.get("Развлечения",0)
    if ent > total*0.20:
        tips.append(f"🎮 Развлечения {int(ent/total*100)}%. Сократить на 30% = *+{fmt(ent*0.30)} ₴*")
    nic = bc.get("Никотин",0)
    if nic > 500:
        tips.append(f"🚬 Никотин: *{fmt(nic)} ₴/мес* = *{fmt(nic*12)} ₴/год* 💭")
    if s.get("leaks"):
        top = max(s["leaks"].items(), key=lambda x:x[1]["total"])
        if top[1]["total"] > 300:
            tips.append(f"💸 «{top[0]}» — {top[1]['count']} раз = *{fmt(top[1]['total'])} ₴*")
    if not tips: return ""
    lines = ["💡 *Персональные советы:*\n"]
    lines += [f"{i}. {t}" for i,t in enumerate(tips[:4],1)]
    return "\n".join(lines)

def build_insight() -> str:
    recs = get_week_records()
    month_recs = get_current_month_records()
    if not recs: return "📭 За эту неделю данных нет."
    s = analyze_records(recs)
    lines = ["🧠 *Инсайт недели*\n"]
    if s["by_day"]:
        td = max(s["by_day"], key=s["by_day"].get)
        avg = s["total"]/7
        if s["by_day"][td] > avg*1.5:
            lines.append(f"📅 Дорогой день: *{td}* (+{int(s['by_day'][td]/avg*100-100)}% от среднего)")
    if s["by_category"]:
        tc = max(s["by_category"], key=s["by_category"].get)
        pct = int(s["by_category"][tc]/s["total"]*100)
        lines.append(f"{get_category_emoji(tc)} Топ категория: *{tc}* — {pct}%")
    if month_recs:
        ms = analyze_records(month_recs)
        avg_day = ms["total"]/datetime.now().day
        lines.append(f"📈 Прогноз месяца: *~{fmt(avg_day*30)} ₴*")
    advice = build_advice(month_recs)
    if advice: lines.append(f"\n{advice}")
    return "\n".join(lines)

# ── УМНЫЕ УВЕДОМЛЕНИЯ (НОВОЕ) ─────────────────────────────────────────────────
async def send_morning_briefing(context: ContextTypes.DEFAULT_TYPE):
    """Утреннее сводка: сколько можно потратить сегодня"""
    cid = (context.job.data or {}).get("chat_id") or CHAT_ID
    if not cid: return
    
    bs = get_budget_status(cid)
    sal = get_salary_info(cid)
    now = datetime.now()
    lines = [f"☀️ *Доброе утро! {now.strftime('%d.%m.%Y')}*\n"]
    
    today_spent = sum_records(get_today_records())
    if today_spent > 0:
        lines.append(f"📌 Вчера потрачено: *{fmt(today_spent)} ₴*")
    
    if bs:
        days_in_month = 30
        days_left = days_in_month - now.day + 1
        daily_limit = bs["left"] / max(days_left, 1)
        lines.append(f"💰 Бюджет использован на *{bs['percent']}%*")
        lines.append(f"📊 Лимит на сегодня: *{fmt(daily_limit)} ₴*")
    elif sal and sal.get("amount"):
        spent = sum_records(get_current_month_records())
        left = float(sal["amount"]) - spent
        sal_day = sal["day"]
        if now.day <= sal_day:
            days_left = sal_day - now.day + 1
        else:
            next_m = (now.replace(day=1) + timedelta(days=32)).replace(day=1)
            days_left = (next_m.replace(day=sal_day) - now).days + 1
        daily = left / max(days_left, 1)
        lines.append(f"💵 До зарплаты: *{fmt(daily)} ₴/день*")
    
    if len(lines) > 1:
        await context.bot.send_message(chat_id=cid, text="\n".join(lines), parse_mode="Markdown")

async def send_weekly_insight(context: ContextTypes.DEFAULT_TYPE):
    cid = (context.job.data or {}).get("chat_id") or CHAT_ID
    if cid: await context.bot.send_message(chat_id=cid, text=build_insight(), parse_mode="Markdown")

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

# ── КЛАВИАТУРА ───────────────────────────────────────────────────────────────
MAIN_KB = ReplyKeyboardMarkup([
    [KeyboardButton("💰 Финансы"), KeyboardButton("📊 Аналитика")],
    [KeyboardButton("💸 Долги"),   KeyboardButton("🎯 Цели")],
    [KeyboardButton("🤖 Спросить ИИ"), KeyboardButton("⚙️ Прочее")],
], resize_keyboard=True)

def inline_kb(buttons: list[list]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d) for t,d in row] for row in buttons])

FINANCE_KB = inline_kb([
    [("📊 Статистика","menu_stats"),("💰 Бюджет","menu_budget")],
    [("💵 Зарплата","menu_salary"),("📊 Сравнение","menu_compare")],
    [("💱 Курс валют","menu_rates")],
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
    [("💱 Курс валют","menu_rates")],
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
    now = datetime.now()
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

# ── HANDLERS ─────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    load_debts()
    load_goals()
    await update.message.reply_text(
        "👋 Привет! Я твой финансовый *AI-агент*.\n\n"
        "🎙 *Запись трат:* «Снюс 800» или «Мойка 350, бензин 1200»\n"
        "💸 *Долг:* «Дал в долг Саше 500 долларов»\n"
        "💰 *Бюджет:* «Бюджет 20000»\n"
        "💵 *Зарплата:* «Зарплата 25 числа 35000»\n"
        "🎯 *Цель:* «Цель iPhone 25000»\n"
        "🤖 *Спросить:* нажми кнопку или задай вопрос свободно\n\n"
        "_Powered by Groq LLaMA 3.3 70B_ 🧠",
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
    await update.message.reply_text("⏳ Запрашиваю курс НБУ...")
    msg = await build_rates_msg()
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_ai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Включает режим ИИ-чата"""
    chat_id = update.effective_chat.id
    context.user_data["ai_mode"] = True
    _ai_chat_history[chat_id] = []  # сброс истории диалога
    await update.message.reply_text(
        "🤖 *Режим AI-советника активирован!*\n\n"
        "Задавай любые вопросы о своих финансах:\n"
        "• «Сколько я потратил на еду в этом месяце?»\n"
        "• «Как накопить на iPhone за 3 месяца?»\n"
        "• «Проанализируй мои траты»\n"
        "• «Дай совет по экономии»\n\n"
        "_Для выхода нажми любую кнопку меню._",
        parse_mode="Markdown")

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

    # ── Кнопки меню — всегда приоритет, всегда сбрасывают режим ИИ ──
    routes = {
        "📊 Статистика": cmd_stats, "📅 Отчёт за неделю": cmd_week,
        "📆 Отчёт за месяц": cmd_month, "💰 Бюджет": cmd_budget,
        "💸 Долги": cmd_debts, "💵 Зарплата": cmd_salary,
        "🎯 Цели": cmd_goals, "💱 Курс валют": cmd_rates,
    }
    if text in routes:
        context.user_data.pop("ai_mode", None)
        await routes[text](update, context); return
    if text == "💰 Финансы":
        context.user_data.pop("ai_mode", None)
        await update.message.reply_text("💰 *Финансы*:", parse_mode="Markdown", reply_markup=FINANCE_KB); return
    if text == "📊 Аналитика":
        context.user_data.pop("ai_mode", None)
        await update.message.reply_text("📊 *Аналитика*:", parse_mode="Markdown", reply_markup=ANALYTICS_KB); return
    if text == "⚙️ Прочее":
        context.user_data.pop("ai_mode", None)
        await update.message.reply_text("⚙️ *Прочее*:", parse_mode="Markdown", reply_markup=OTHER_KB); return
    if text == "🎯 Цели":
        context.user_data.pop("ai_mode", None)
        await cmd_goals(update, context); return
    if text == "🤖 Спросить ИИ":
        await cmd_ai(update, context); return

    # ── Режим ИИ-чата — активен только после явного нажатия кнопки/команды ──
    if context.user_data.get("ai_mode"):
        await update.message.reply_chat_action("typing")
        response = await ai_chat_response(chat_id, text)
        await update.message.reply_text(response, parse_mode="Markdown")
        return

    await process(update, context, text)

# ── ОСНОВНОЙ ОБРАБОТЧИК ───────────────────────────────────────────────────────
async def process(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    chat_id = update.effective_chat.id
    lower = text.lower().strip()

    # ── Пополнение цели ("в цель 500", "накопил 1000 на iPhone") ──
    goal_kw = any(kw in lower for kw in ["в цель","к цели","на цель","накопил","отложил","в копилку"])
    if goal_kw and goals:
        nums = re.findall(r'\d+(?:[.,]\d+)?', text)
        if nums:
            amount = float(nums[0].replace(",","."))
            if len(goals) == 1:
                gid = list(goals.keys())[0]
                g = goals[gid]
                g["saved"] = min(g["saved"] + amount, g["target"])
                update_goal_saved(gid, g["saved"])
                pct = min(int(g["saved"]/g["target"]*100), 100)
                bar = build_goal_bar(g["saved"], g["target"])
                msg = f"🎯 *{g['name']}*\n[{bar}] {pct}%\n{fmt(g['saved'])} / {fmt(g['target'])} ₴"
                if g["saved"] >= g["target"]:
                    msg += "\n\n🎉 *Цель достигнута!* Поздравляю!"
                    goals.pop(gid); close_goal(gid)
                await update.message.reply_text(msg, parse_mode="Markdown"); return
            else:
                # Несколько целей — выбор
                kb = [[( f"{g['emoji']} {g['name']}", f"goal_add_{gid}_{amount}")] for gid, g in goals.items()]
                await update.message.reply_text(f"💰 *{fmt(amount)} ₴* — к какой цели?",
                    parse_mode="Markdown", reply_markup=inline_kb(kb)); return

    # ── Новая цель ("цель iPhone 25000") ──
    if any(kw in lower for kw in ["цель","копить на","накопить на"]):
        parsed = parse_goal(text)
        if parsed and parsed.get("amount"):
            goal_counter[0] += 1
            gid = str(goal_counter[0])
            date_str = datetime.now().strftime("%d.%m.%Y")
            goals[gid] = {
                "name": parsed["name"], "target": float(parsed["amount"]),
                "saved": 0.0, "date": date_str, "emoji": parsed.get("emoji","🎯")
            }
            save_goal_to_sheet(gid, parsed["name"], float(parsed["amount"]), 0, date_str, parsed.get("emoji","🎯"))
            bar = build_goal_bar(0, float(parsed["amount"]))
            await update.message.reply_text(
                f"{parsed.get('emoji','🎯')} *Цель создана!*\n\n*{parsed['name']}*\n[{bar}] 0%\n0 / {fmt(parsed['amount'])} ₴\n\n"
                f"_Говори «Отложил 500» или «В цель 1000» — буду отслеживать!_",
                parse_mode="Markdown"); return

    # ── Вопрос к ИИ (свободный вопрос) ──
    ai_triggers = ["?","сколько","почему","как","посоветуй","проанализируй","помоги","скажи","можешь","расскажи"]
    if any(t in lower for t in ai_triggers) and len(text) > 10:
        await update.message.reply_chat_action("typing")
        response = await ai_chat_response(chat_id, text)
        await update.message.reply_text(response, parse_mode="Markdown")
        return

    # ── Конвертация валют ("100 долларов в гривны") ──
    conv_match = re.search(r'(\d+(?:[.,]\d+)?)\s*(долл?ар[ыаов]*|usd|\$|евро|eur|€|грив[еньни]*|uah|₴)', lower)
    if conv_match and any(kw in lower for kw in ["в гривн","в uah","в долл","в евро","конверт","курс"]):
        amount = float(conv_match.group(1).replace(",","."))
        cur_raw = conv_match.group(2)
        cur_map = {"доллар":"USD","долар":"USD","usd":"USD","$":"USD","евро":"EUR","eur":"EUR","€":"EUR"}
        currency = next((v for k,v in cur_map.items() if k in cur_raw), "UAH")
        uah = await convert_to_uah(amount, currency)
        sym = CURRENCY_SYMBOLS.get(currency,"₴")
        await update.message.reply_text(
            f"💱 *{fmt(amount)} {sym}* = *{fmt(uah)} ₴*\n_(по курсу НБУ)_",
            parse_mode="Markdown"); return

    # ── Пользовательская категория ("добавь категорию Инвестиции") ──
    if any(kw in lower for kw in ["добавь категорию","новая категория","создай категорию"]):
        words = text.split()
        for i, w in enumerate(words):
            if w.lower() in ["категорию","категория"] and i+1 < len(words):
                new_cat = " ".join(words[i+1:]).strip().capitalize()
                save_user_category(new_cat)
                await update.message.reply_text(
                    f"✅ Категория *{new_cat}* добавлена!\nТеперь её можно использовать в записях.",
                    parse_mode="Markdown"); return

    # ── Бюджет ──
    if any(kw in lower for kw in ["бюджет","лимит"]):
        nums = re.findall(r'\d+(?:[.,]\d+)?', text)
        if nums:
            budget = float(nums[0].replace(",","."))
            save_setting(f"budget_{chat_id}", str(budget))
            bs = get_budget_status(chat_id)
            if bs:
                bar = "█"*(bs["percent"]//10) + "░"*(10-bs["percent"]//10)
                await update.message.reply_text(
                    f"💰 *Бюджет установлен: {fmt(budget)} ₴*\n\n[{bar}] {bs['percent']}% использовано",
                    parse_mode="Markdown")
            else:
                await update.message.reply_text(f"💰 *Бюджет установлен: {fmt(budget)} ₴*", parse_mode="Markdown")
            return

    # ── Зарплата ──
    if any(kw in lower for kw in ["зарплата","получка","salary"]):
        nums = re.findall(r'\d+', text)
        if nums:
            day = int(nums[0])
            amount = float(nums[1]) if len(nums) > 1 else None
            if 1 <= day <= 31:
                set_salary_info(chat_id, day, amount)
                amt_str = f" — *{fmt(amount)} ₴*" if amount else ""
                await update.message.reply_text(
                    f"💵 *Зарплата установлена!*\n📅 День: *{day}-е число*{amt_str}",
                    parse_mode="Markdown"); return

    # ── Возврат долга ──
    if any(kw in lower for kw in ["отдал","вернул","отдала","вернула","погасил","расплатился"]):
        try:
            ctx = get_ctx(chat_id)
            parsed = parse_debt(text, ctx.get("last_name",""))
            ams = parsed.get("amounts",[])
            if not ams and parsed.get("amount"):
                ams = [{"amount":float(parsed["amount"]),"currency":"UAH"}]
            if ams and parsed.get("name"):
                name = parsed["name"].lower()
                did = next((k for k,d in debts.items() if name in d["name"].lower()), None)
                if not did:
                    await update.message.reply_text(f"🤔 Не нашёл долга для *{parsed['name']}*.", parse_mode="Markdown"); return
                d = debts[did]
                ex_ams = d.get("amounts",[])
                lines = [f"💰 *{d['name']}* вернул:\n"]
                closed_curs = []
                for ra in ams:
                    cur = ra.get("currency","UAH")
                    sym = CURRENCY_SYMBOLS.get(cur,"₴")
                    for ea in ex_ams:
                        if ea.get("currency","UAH") == cur:
                            new = float(ea["amount"]) - float(ra["amount"])
                            if new <= 0: closed_curs.append(cur); lines.append(f"✅ {sym}: закрыто")
                            else: ea["amount"] = new; lines.append(f"💸 {sym}: {fmt(ra['amount'])} → остаток *{fmt(new)} {sym}*")
                            break
                ex_ams = [a for a in ex_ams if a.get("currency","UAH") not in closed_curs]
                if not ex_ams:
                    debts.pop(did); mark_paid(did); lines.append("\n🎉 *Долг полностью закрыт!*")
                else:
                    debts[did]["amounts"] = ex_ams; update_debt_amounts(did, ex_ams)
                    lines.append(f"\n📊 Остаток: {format_amounts(ex_ams)}")
                set_ctx(chat_id, last_name=d["name"], last_action="debt_return")
                await update.message.reply_text("\n".join(lines), parse_mode="Markdown"); return
        except Exception as e: logger.error(f"debt_return: {e}")

    # ── Добавление к существующему долгу ("ещё 500", "плюс 300 папе") ──
    add_kw = any(kw in lower for kw in ["ещё","еще","плюс","добав","доплат","и ещё","и еще"])
    if add_kw:
        ctx = get_ctx(chat_id)
        last_name = ctx.get("last_name","")
        nums = re.findall(r'\d+(?:[.,]\d+)?', text)
        if nums and last_name:
            # Ищем существующий долг по имени из контекста
            did = next((k for k,d in debts.items() if last_name.lower() in d["name"].lower()), None)
            if did:
                amount = float(nums[0].replace(",","."))
                ex_ams = debts[did].get("amounts",[])
                for ea in ex_ams:
                    if ea.get("currency","UAH") == "UAH":
                        ea["amount"] = float(ea["amount"]) + amount
                        break
                else:
                    ex_ams.append({"amount": amount, "currency": "UAH"})
                debts[did]["amounts"] = ex_ams
                update_debt_amounts(did, ex_ams)
                set_ctx(chat_id, last_name=debts[did]["name"], last_action="debt_add")
                await update.message.reply_text(
                    f"➕ Добавил *{fmt(amount)} ₴* к долгу *{debts[did]['name']}*\n"
                    f"💰 Итого: {format_amounts(ex_ams)}",
                    parse_mode="Markdown"); return

    # ── Новый долг ──
    debt_triggers = ["дал в долг","одолжил","дала в долг","долг","занял","дал папе","дал маме","дал другу"]
    if any(kw in lower for kw in debt_triggers) or (
        any(kw in lower for kw in ["дал","дала","дали"]) and re.search(r'\d', text)
    ):
        try:
            ctx = get_ctx(chat_id)
            parsed = parse_debt(text, ctx.get("last_name",""))
            ams = parsed.get("amounts",[])
            if not ams and parsed.get("amount"):
                ams = [{"amount":float(parsed["amount"]),"currency":"UAH"}]
            if ams and parsed.get("name"):
                # Проверяем — может такой долг уже есть, тогда добавляем
                name_low = parsed["name"].lower()
                existing_did = next((k for k,d in debts.items() if name_low in d["name"].lower()), None)
                if existing_did:
                    # Добавляем к существующему
                    ex_ams = debts[existing_did].get("amounts",[])
                    for na in ams:
                        cur = na.get("currency","UAH")
                        found = next((a for a in ex_ams if a.get("currency","UAH")==cur), None)
                        if found: found["amount"] = float(found["amount"]) + float(na["amount"])
                        else: ex_ams.append(na)
                    debts[existing_did]["amounts"] = ex_ams
                    update_debt_amounts(existing_did, ex_ams)
                    set_ctx(chat_id, last_name=parsed["name"], last_action="debt_add")
                    await update.message.reply_text(
                        f"➕ *{parsed['name']}* — долг обновлён\n💰 Итого: {format_amounts(ex_ams)}",
                        parse_mode="Markdown"); return
                # Новый долг
                debt_counter[0] += 1
                did = str(debt_counter[0])
                date_str = datetime.now().strftime("%d.%m.%Y")
                debts[did] = {"name":parsed["name"],"amounts":ams,"date":date_str,"note":parsed.get("note","")}
                save_debt(did, parsed["name"], ams, date_str, parsed.get("note",""))
                interval = get_reminder_interval(chat_id)
                context.job_queue.run_once(send_debt_reminder, when=interval,
                    data={"debt_id":did,"chat_id":chat_id}, name=f"debt_{did}")
                note = f"\n📝 {parsed['note']}" if parsed.get("note") else ""
                set_ctx(chat_id, last_name=parsed["name"], last_action="debt_new")
                await update.message.reply_text(
                    f"💸 *Долг записан!*\n\n👤 *{parsed['name']}*\n💰 {format_amounts(ams)}{note}\n\n"
                    f"_Если нужно добавить ещё — просто напиши «ещё 500»_\n\n"
                    f"⏰ Напомню через {reminder_label(chat_id)}.",
                    parse_mode="Markdown"); return
        except Exception as e: logger.error(f"debt_new: {e}")

    # ── Быстрый режим (просто число) ──
    stripped = text.strip().replace(",",".").replace(" ","")
    if re.fullmatch(r'\d+(\.\d+)?', stripped):
        amount = float(stripped)
        # Просто число — спрашиваем ИИ что это может быть или показываем категории
        cats = get_all_categories()
        kb = []
        row = []
        for cat in cats:
            row.append((f"{get_category_emoji(cat)} {cat}", f"quick_{cat}_{amount}"))
            if len(row) == 2: kb.append(row); row = []
        if row: kb.append(row)
        await update.message.reply_text(f"⚡ *{fmt(amount)} ₴* — что это?",
            parse_mode="Markdown", reply_markup=inline_kb(kb))
        return

    # ── Обычные расходы ──
    try:
        expenses = parse_expenses(text)
        if not expenses:
            await update.message.reply_text(
                "🤔 Не понял что записать.\n\n"
                "Попробуй написать как обычно:\n"
                "• «Снюс 800»\n• «Заправился на 1200, мойка 300»\n• «Взял кофе и круасан 185»\n\n"
                "Или задай вопрос: «Сколько я потратил на еду?»"
            ); return
        date = datetime.now().strftime("%d.%m.%Y %H:%M")
        month_recs = get_current_month_records()
        lines = ["✅ *Записано!*\n"]
        for exp in expenses:
            amount = float(exp["amount"])
            cat = fix_cat(exp.get("category","Другое"))
            desc = exp.get("description","—")
            emoji = exp.get("emoji","")
            save_expense(date, amount, cat, desc, text)
            lines.append(f"{emoji} *{desc}* — *{fmt(amount)} ₴*\n   _{get_category_emoji(cat)} {cat}_")
        if len(expenses) > 1:
            lines.append(f"\n💰 *Итого: {fmt(sum(float(e['amount']) for e in expenses))} ₴*")
        # Умный комментарий по категории
        cat0 = fix_cat(expenses[0].get("category","Другое"))
        sk = get_sum_key(month_recs)
        cat_total = sum(float(r[sk]) for r in month_recs if fix_cat(r.get("Категория",""))==cat0 and r.get(sk))
        if cat_total > 0:
            lines.append(f"\n_{get_category_emoji(cat0)} {cat0} в этом месяце: *{fmt(cat_total)} ₴*_")
        # Предупреждения бюджета
        bs = get_budget_status(chat_id)
        if bs:
            pct = bs["percent"]
            if pct >= 90: lines.append(f"\n🔴 *Бюджет использован на {pct}%!*")
            elif pct >= 70: lines.append(f"\n🟡 Бюджет использован на {pct}%")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        logger.error(f"process expenses: {e}")
        await update.message.reply_text("❌ Ошибка при сохранении. Попробуй ещё раз.")

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
        elif action == "past": await send("⏳ Анализирую..."); await send(build_past_self(), parse_mode="Markdown")
        elif action == "habits": await send("⏳ Считаю..."); await send(build_habits(), parse_mode="Markdown")
        elif action == "rates": await send("⏳ Запрашиваю курс НБУ..."); await send(await build_rates_msg(), parse_mode="Markdown")
        elif action == "advice":
            await send("⏳ Анализирую...")
            recs = get_current_month_records()
            msg = build_advice(recs) or "📭 Пока недостаточно данных."
            insight = build_insight()
            await send(f"{msg}\n\n{insight}" if msg and insight else msg or insight, parse_mode="Markdown")
        elif action == "reminder":
            cur = reminder_label(chat_id)
            await context.bot.send_message(chat_id=chat_id,
                text=f"⏰ *Напоминания*\n\nТекущий: *{cur}*\n\nВыбери:", parse_mode="Markdown", reply_markup=REMINDER_KB)
        elif action == "categories":
            cats = get_all_categories()
            user_cats = _user_categories
            lines = ["🏷 *Категории:*\n"]
            lines += [f"{get_category_emoji(c)} {c}" for c in cats]
            if not user_cats:
                lines.append("\n_Добавь свою: «Добавь категорию Инвестиции»_")
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
        date = datetime.now().strftime("%d.%m.%Y %H:%M")
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
        ("rates",cmd_rates), ("ai",cmd_ai),
    ]:
        app.add_handler(CommandHandler(cmd, handler))

    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    load_settings()
    load_memory()
    load_debts()
    load_goals()
    load_user_categories()

    if CHAT_ID and app.job_queue:
        # Еженедельный инсайт (пятница 19:00)
        app.job_queue.run_daily(
            send_weekly_insight,
            time=dtime(19, 0),
            days=(4,), data={"chat_id": CHAT_ID})
        # Утреннее сводка (каждый день 9:00)
        app.job_queue.run_daily(
            send_morning_briefing,
            time=dtime(9, 0),
            data={"chat_id": CHAT_ID})

    logger.info("AI-агент запущен! v4.0 🤖")
    app.run_polling()

if __name__ == "__main__":
    main()
