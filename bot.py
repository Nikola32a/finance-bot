"""
Финансовый AI-агент Telegram бот v5.7
Семантика долгов: "дал Саше 500" = Саша должен МНЕ (я дал в долг)
                  "Саша вернул 500" = Саша отдал мне обратно
                  "отдал Саше 500" = НЕ долг, это я заплатил/потратил

ИЗМЕНЕНИЯ v5.7:
- Просто число без описания (напр. "1650") → бот спрашивает категорию через инлайн-кнопки
- quick_ callback использует название категории как описание
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
DEFAULT_CATEGORIES = ["Еда / продукты", "Транспорт", "Развлечения", "Здоровье / аптека", "Никотин"]

EMOJI_MAP = {
    "Еда / продукты": "🍔", "Транспорт": "🚗", "Развлечения": "🎮",
    "Здоровье / аптека": "💊", "Никотин": "🚬",
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

# ── НЕ-ТРАТЫ: пополнение счёта, переводы ────────────────────────────────────
NON_EXPENSE_PATTERNS = re.compile(
    r"(пополн|поповн|закинул|закинув|перевод|переказ|transfer|зарядил|зачислен|поступил"
    r"|пришло|получил|отримав|дохід|доход|зарплат|salary"
    r"|пополнение\s+счет|поповнення\s+рахун)",
    re.IGNORECASE
)

# ── ДОЛГОВЫЕ ПАТТЕРНЫ ────────────────────────────────────────────────────────
DEBT_PATTERNS = re.compile(
    r"\b("
    r"дал|дав|позичив|позычил|одолжил|одолжив|в\s+долг"
    r"|взял\s+у|взяв\s+у"
    r"|должен|винен|должна|должны|борг|долг"
    r"|вернул|вернула|повернув|повернула|віддав|віддала|отдал|отдала"
    r"|напомни\s+о\s+долг|нагадай\s+про\s+борг"
    r")\b",
    re.IGNORECASE
)

# ── ПАТТЕРН "ПРОСТО ЧИСЛО" ───────────────────────────────────────────────────
JUST_NUMBER_PATTERN = re.compile(
    r"^\s*(\d[\d\s]*(?:[.,]\d+)?(?:\s*к(?:р|ривень|уб)?|\s*тис(?:яч)?)?)\s*(?:₴|грн|грн\.?|uah)?\s*$",
    re.IGNORECASE
)
def _parse_amount_str(s: str) -> float | None:
    """Универсальный парсер суммы: '2262,33' → 2262.33, '3к' → 3000, '1 500' → 1500."""
    s = s.strip()
    # Убираем валютные символы
    s = re.sub(r"\s*(?:₴|грн\.?|uah)\s*$", "", s, flags=re.IGNORECASE).strip()
    # Тысячный разделитель пробел: "1 500" → "1500"
    # Если запятая разделитель тысяч (1,500) или дробная (2262,33) — нужно различить
    # Правило: если после запятой ровно 3 цифры и нет точки → тысячный разделитель
    # Если после запятой 1-2 цифры → дробная часть
    s_nospace = s.replace(" ", "")
    mult = 1
    if re.search(r"к(?:р|ривень|уб)?$|тис", s_nospace, re.IGNORECASE):
        mult = 1000
        s_nospace = re.sub(r"[кКтТис]+.*$", "", s_nospace, flags=re.IGNORECASE)
    # Нормализуем разделители
    if "," in s_nospace and "." not in s_nospace:
        # Проверяем: если после запятой ровно 3 цифры — тысячный разделитель
        m = re.match(r"^(\d+),(\d+)$", s_nospace)
        if m and len(m.group(2)) == 3:
            s_nospace = s_nospace.replace(",", "")  # убираем разделитель тысяч
        else:
            s_nospace = s_nospace.replace(",", ".")  # дробная часть
    elif "." in s_nospace and "," in s_nospace:
        # "1.500,50" европейский формат
        s_nospace = s_nospace.replace(".", "").replace(",", ".")
    try:
        return float(s_nospace) * mult
    except:
        return None
# ── ВАЛЮТНЫЕ ПАТТЕРНЫ ─────────────────────────────────────────────────────────
CURRENCY_AMOUNT_PATTERNS = re.compile(
    r"(\d[\d\s,\.]*)\s*"
    r"(долар\w*|доллар\w*|бакс\w*|usd|\$|євро\w*|евро\w*|eur|€|фунт\w*|gbp|злот\w*|pln)"
    r"|\bв\s+(долар\w*|доллар\w*|бакс\w*|usd|євро\w*|евро\w*|eur|фунт\w*|gbp|злот\w*|pln)\b",
    re.IGNORECASE
)

def _detect_currency(text: str) -> str:
    t = text.lower()
    if re.search(r"гривн\w*|гривень|грн\b|₴|uah\b", t):
        return "UAH"
    if re.search(r"долар\w*|доллар\w*|бакс\w*|usd|\$", t):
        return "USD"
    if re.search(r"євро\w*|евро\w*|eur\b|€", t):
        return "EUR"
    return "UAH"

# ── GOOGLE SHEETS ─────────────────────────────────────────────────────────────
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

def _worksheet_get_or_create(sp, name):
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

# ── КАТЕГОРИИ ─────────────────────────────────────────────────────────────────
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

def fix_cat(cat: str, desc: str = "", keep_new: bool = False) -> str:

    """Нормализует категорию. keep_new=True — не сбрасывать в 'Другое' если категория новая от LLM."""
    cats = get_all_categories()

    if not cat or cat.strip() == "": return "Другое"
    if cat in cats: return cat
    cat_low = cat.lower().strip()
    if cat_low == "другое": return "Другое"
    for c in cats:
        if cat_low in c.lower() or c.lower() in cat_low:
            return c
    if keep_new:
        return cat.strip().capitalize()
    return "Другое"

def validate_category(cat: str, desc: str = "") -> str:
    # При сохранении используем keep_new=True — не ломаем новые категории
    return fix_cat(cat, desc, keep_new=True)

def save_expense(date, amount, category, description, raw_text):
    category = validate_category(category, description)
    get_sheet().append_row([date, amount, category, description, raw_text])
    _invalidate("sheet1")

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
            d = datetime.strptime(r.get("Дата","")[:10], "%d.%m.%Y").replace(tzinfo=KYIV_TZ)
            if d >= week_ago: result.append(r)
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

# ── БЮДЖЕТ / ЗАРПЛАТА ────────────────────────────────────────────────────────
memory: dict = {}
def get_salary_day(chat_id) -> int:
    """Возвращает день зарплаты (1-31), по умолчанию 1."""
    info = get_salary_info(chat_id)
    if info and info.get("day"): return int(info["day"])
    return 1

def get_period_start(chat_id) -> datetime:
    """Начало текущего зарплатного периода (день зарплаты текущего или прошлого месяца)."""
    now = datetime.now(KYIV_TZ)
    sal_day = get_salary_day(chat_id)
    if now.day >= sal_day:
        # Зарплата уже была в этом месяце
        try:
            return now.replace(day=sal_day, hour=0, minute=0, second=0, microsecond=0)
        except ValueError:
            return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        # Зарплата ещё не была — берём прошлый месяц
        first_of_month = now.replace(day=1)
        prev_month_last = first_of_month - timedelta(days=1)
        sal_day_clamped = min(sal_day, prev_month_last.day)
        return prev_month_last.replace(day=sal_day_clamped, hour=0, minute=0, second=0, microsecond=0)

def get_period_records(chat_id) -> list:
    """Записи за текущий зарплатный период (от дня зарплаты до сегодня)."""
    start = get_period_start(chat_id)
    result = []
    for r in get_all_records():
        try:
            d = datetime.strptime(r.get("Дата","")[:10], "%d.%m.%Y").replace(tzinfo=KYIV_TZ)
            if d >= start: result.append(r)
        except: pass
    return result

def get_yesterday_records() -> list:
    """Записи за вчерашний день."""
    yesterday = (datetime.now(KYIV_TZ) - timedelta(days=1)).strftime("%d.%m.%Y")
    return [r for r in get_all_records() if r.get("Дата","").startswith(yesterday)]

def get_budget_status(chat_id):
    val = get_setting(f"budget_{chat_id}")
    if not val: return None
    try: budget = float(val)
    except: return None
# Считаем от дня зарплаты
    recs = get_period_records(chat_id)
    spent = sum_records(recs)
    left = budget - spent
    period_start = get_period_start(chat_id)
    now = datetime.now(KYIV_TZ)
    days_in_period = (now - period_start).days + 1
    return {
        "budget": budget, "spent": spent, "left": left,
        "percent": min(int(spent / budget * 100), 100),
        "period_start": period_start,
        "days_in_period": days_in_period,
    }

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
   # Траты считаем от дня зарплаты
    spent = sum_records(get_period_records(chat_id))
    period_start = get_period_start(chat_id)
    lines = [f"💵 *День зарплаты — {day}-е число*\n"]
    lines.append(f"📅 Период: с *{period_start.strftime('%d.%m')}*")
    if days_left == 0: lines.append("🎉 *Сегодня зарплата!*")
    elif days_left == 1: lines.append("⏰ *Завтра зарплата!*")
    else: lines.append(f"📅 До зарплаты: *{days_left} дней* ({next_sal.strftime('%d')} {month_name(next_sal.month, True)})")
    lines.append(f"\n💸 Потрачено за период: *{fmt(spent)} ₴*")
    if amount:
        left = amount - spent
        lines.append(f"💰 Зарплата: *{fmt(amount)} ₴*")
        lines.append(f"{'🟢' if left>0 else '🔴'} Осталось: *{fmt(left)} ₴*")
        if days_left > 0 and left > 0:
             lines.append(f"📊 Можно тратить: *{fmt(left/max(days_left,1))} ₴/день*")
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

# ── КУРС ВАЛЮТ ────────────────────────────────────────────────────────────────
_rates_cache: dict = {}
_rates_ts: float = 0
_mono_cache: dict = {}
_mono_ts: float = 0
_obmen_cache: dict = {}
_obmen_ts: float = 0
_privat_cache: dict = {}
_privat_ts: float = 0

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
            _rates_cache = rates; _rates_ts = now
            return rates
    except Exception as e:
        logger.error(f"NBU rates: {e}")
        return {"USD": 41.5, "EUR": 44.0}

async def convert_to_uah(amount: float, currency: str) -> float:
    if currency == "UAH": return amount
    rates = await fetch_nbu_rates()
    return amount * rates.get(currency, 1.0)

async def fetch_monobank_rates() -> dict:
    global _mono_cache, _mono_ts
    now = datetime.now(KYIV_TZ).timestamp()
    if _mono_cache and now - _mono_ts < 600: return _mono_cache
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get("https://api.monobank.ua/bank/currency")
            data = resp.json()
            result = {}
            for item in data:
                if item.get("currencyCodeB") == 980:
                    if item.get("currencyCodeA") == 840: result["USD"] = item
                    elif item.get("currencyCodeA") == 978: result["EUR"] = item
            _mono_cache = result; _mono_ts = now
            return result
    except Exception as e:
        logger.error(f"monobank rates: {e}"); return {}

async def fetch_obmen_rates() -> dict:
    global _obmen_cache, _obmen_ts
    now_ts = datetime.now(KYIV_TZ).timestamp()
    if _obmen_cache and now_ts - _obmen_ts < 600: return _obmen_cache
    import re as _re
    SEARCH_KEYS = {"USD": ["USD", "Долар", "Dollar", "$"]}
    def _parse_rates_from_html(html: str) -> dict:
        result = {}
        num_pat = _re.compile(r"(\d{2,3}[.,]\d{1,4})")
        for cur, keys in SEARCH_KEYS.items():
            for key in keys:
                idx = html.find(key)
                if idx < 0: continue
                chunk = html[idx:idx + 600]
                nums = [float(n.replace(",",".")) for n in num_pat.findall(chunk) if 30 < float(n.replace(",",".")) < 200]
                if len(nums) >= 2:
                    result[cur] = {"buy": nums[1], "sale": nums[0]}; break
        return result
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            resp = await client.get("https://obmen24.com.ua/uk/lviv", headers={"User-Agent":"Mozilla/5.0"})
            if resp.status_code != 200: raise ValueError(f"obmen24 status {resp.status_code}")
            result = _parse_rates_from_html(resp.text)
        if result and "USD" in result:
            _obmen_cache = result; _obmen_ts = now_ts
            logger.info(f"obmen24 parsed USD: {result}")
            return result
        raise ValueError("obmen24 USD parse empty")
    except Exception as e:
        logger.error(f"obmen24 USD rates: {e}"); return {}

async def fetch_privat_rates() -> dict:
    global _privat_cache, _privat_ts
    now = datetime.now(KYIV_TZ).timestamp()
    if _privat_cache and now - _privat_ts < 600: return _privat_cache
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get("https://api.privatbank.ua/p24api/pubinfo?json&exchange&coursid=5")
            data = resp.json()
            result = {}
            for item in data:
                ccy = item.get("ccy","")
                if ccy in ("USD","EUR"):
                    result[ccy] = {"buy": float(item.get("buy",0)), "sale": float(item.get("sale",0))}
            _privat_cache = result; _privat_ts = now
            return result
    except Exception as e:
        logger.error(f"privat rates: {e}"); return {}

async def build_rates_msg() -> str:
    nbu, mono, obmen, privat = await asyncio.gather(
        fetch_nbu_rates(), fetch_monobank_rates(), fetch_obmen_rates(), fetch_privat_rates())
    now = datetime.now(KYIV_TZ).strftime("%H:%M")
    lines = [f"💱 *Курс валют* _{now}_\n"]
    def v(d, key, fallback="  — "):
        val = d.get(key)
        return f"{float(val):.2f}" if val else fallback
    for cur, flag in [("USD","🇺🇸"),("EUR","🇪🇺")]:
        mono_d = mono.get(cur, {})
        if cur == "USD":
            src_d = obmen.get("USD",{}) if "USD" in obmen else privat.get("USD",{})
            src_name = "obmen24" if "USD" in obmen else "Приват"
        else:
            src_d = privat.get("EUR",{}); src_name = "Приват"
        lines.append(
            f"{flag} *{cur}*\n"
            f"`{'':1}{'':7}{'Моно':>6}  {src_name:>9}`\n"
            f"`{'':1}{'Купить':<7}{v(mono_d,'rateSell'):>7}  {v(src_d,'sale'):>7}`\n"
            f"`{'':1}{'Продать':<7}{v(mono_d,'rateBuy'):>7}  {v(src_d,'buy'):>7}`"
        )
    return "\n\n".join(lines)

# ── КОНТЕКСТ РАЗГОВОРА ───────────────────────────────────────────────────────
_conv_context: dict = {}

def get_ctx(chat_id) -> dict:
    return _conv_context.get(str(chat_id), {})

def set_ctx(chat_id, **kwargs):
    cid = str(chat_id)
    if cid not in _conv_context: _conv_context[cid] = {}
    _conv_context[cid].update(kwargs)

# ── ДОЛГИ ────────────────────────────────────────────────────────────────────
debts: dict = {}
debt_counter = [0]

def _debts_sheet():
    sh = _get_worksheet("Долги")
    if not sh.get_all_values():
        sh.insert_row(["ID","Кому","Сумма","Дата","Статус","Примечание","Процент"], 1)
    return sh

def load_debts():
    try:
        sym_map = {"₴":"UAH","$":"USD","€":"EUR"}
        rows = _debts_sheet().get_all_records()
        logger.info(f"load_debts: найдено {len(rows)} записей")
        for r in rows:
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
            interest = 0.0
            try: interest = float(r.get("Процент", 0) or 0)
            except: pass
            debts[did] = {"name":r["Кому"],"amounts":amounts,"date":r["Дата"],"note":r.get("Примечание",""),"interest":interest}
            try: debt_counter[0] = max(debt_counter[0], int(r["ID"]))
            except: pass
        logger.info(f"load_debts: загружено активных долгов: {len(debts)}")
    except Exception as e:
        logger.error(f"load_debts: {e}")

def save_debt(did, name, amounts, date, note="", interest=0.0):
    amt_str = amounts_str(amounts)
    try: _debts_sheet().append_row([did, name, amt_str, date, "активен", note, interest])
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
    lines = ["💸 *Мне должны:*\n"]
    totals: dict = defaultdict(float)
    for d in debts.values():
        try:
            days_ago = (datetime.now(KYIV_TZ) - datetime.strptime(d["date"], "%d.%m.%Y").replace(tzinfo=KYIV_TZ)).days
        except:
            days_ago = 0
        note = f" — _{d['note']}_" if d.get("note") else ""
        ams = d.get("amounts",[{"amount":d.get("amount",0),"currency":"UAH"}])
        interest = d.get("interest", 0)
        interest_str = f" · 📈 {interest}%/мес" if interest else ""
        # Считаем начисленные проценты
        accrued_str = ""
        if interest and days_ago > 0:
            months_passed = days_ago / 30
            for a in ams:
                accrued = float(a["amount"]) * (interest / 100) * months_passed
                sym = CURRENCY_SYMBOLS.get(a.get("currency","UAH"),"₴")
                accrued_str += f"\n   📈 Начислено процентов: *{fmt(accrued)} {sym}*"
        lines.append(f"👤 *{d['name']}* — {format_amounts(ams)}{note}{interest_str}")
        lines.append(f"   📅 {d['date']} ({days_ago} дн. назад){accrued_str}")
        for a in ams: totals[a.get("currency","UAH")] += float(a["amount"])
    if totals:
        lines.append("")
        for cur in ["USD","EUR","UAH"]:
            if cur in totals:
                sym = CURRENCY_SYMBOLS[cur]
                lines.append(f"💰 Итого в {sym}: *{fmt(totals[cur])} {sym}*")
    return "\n".join(lines)

# ── РАССРОЧКА ─────────────────────────────────────────────────────────────────
recurring: dict = {}
memory: dict = {}
def _installments_sheet():
    sh = _get_worksheet("Рассрочка")
    if not sh.get_all_values():
        sh.insert_row(["ID","Название","Общая сумма","Ежемесячный платёж","Выплачено","Осталось платежей","Дата начала","Статус"], 1)
    return sh

def load_installments():
    try:
        for r in _installments_sheet().get_all_records():
            if r.get("Статус") != "активна": continue
            iid = str(r["ID"])
            installments[iid] = {
                "name": r["Название"],
                "total": float(r.get("Общая сумма", 0)),
                "monthly": float(r.get("Ежемесячный платёж", 0)),
                "paid": float(r.get("Выплачено", 0)),
                "payments_left": int(r.get("Осталось платежей", 0)),
                "date": r["Дата начала"],
            }
            try: installment_counter[0] = max(installment_counter[0], int(r["ID"]))
            except: pass
    except Exception as e:
        logger.error(f"load_installments: {e}")

def save_installment_to_sheet(iid, name, total, monthly, paid, payments_left, date):
    try: _installments_sheet().append_row([iid, name, total, monthly, paid, payments_left, date, "активна"])
    except Exception as e: logger.error(f"save_installment: {e}")

def update_installment_in_sheet(iid, paid, payments_left, status="активна"):
    try:
        sh = _installments_sheet()
        for i, r in enumerate(sh.get_all_records(), start=2):
            if str(r.get("ID")) == str(iid):
                sh.update_cell(i, 5, paid)
                sh.update_cell(i, 6, payments_left)
                sh.update_cell(i, 8, status)
                return
    except Exception as e: logger.error(f"update_installment: {e}")

def build_installments_msg() -> str:
    if not installments:
        return "💳 Активных рассрочек нет.\n\nДобавь: «Рассрочка Колёса 12000 на 12 месяцев 1000»"
    lines = ["💳 *Мои рассрочки:*\n"]
    for iid, inst in installments.items():
        paid, total, monthly, pl = inst["paid"], inst["total"], inst["monthly"], inst["payments_left"]
        pct = min(int(paid / total * 100), 100) if total > 0 else 0
        bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
        lines.append(f"💳 *{inst['name']}*")
        lines.append(f"   [{bar}] {pct}%")
        lines.append(f"   Выплачено: {fmt(paid)} / {fmt(total)} ₴")
        lines.append(f"   Ежемесячно: *{fmt(monthly)} ₴* · осталось платежей: {pl}")
        lines.append("")
    return "\n".join(lines)

# ── РЕГУЛЯРНЫЕ ПЛАТЕЖИ ────────────────────────────────────────────────────────
recurring: dict = {}
memory: dict = {}
def _recurring_sheet():
    sh = _get_worksheet("Регулярные")
    if not sh.get_all_values():
        sh.insert_row(["ID","Название","Сумма","День","Категория","Emoji","Статус"], 1)
    return sh

def load_recurring():
    try:
        for r in _recurring_sheet().get_all_records():
            if r.get("Статус") != "активен": continue
            rid = str(r["ID"])
            recurring[rid] = {
                "name": r["Название"],
                "amount": float(r.get("Сумма", 0)),
                "day": int(r.get("День", 1)),
                "category": r.get("Категория", "Другое"),
                "emoji": r.get("Emoji", "🔄"),
            }
    except Exception as e:
        logger.error(f"load_recurring: {e}")

def save_recurring_to_sheet(rid, name, amount, day, category, emoji):
    try: _recurring_sheet().append_row([rid, name, amount, day, category, emoji, "активен"])
    except Exception as e: logger.error(f"save_recurring: {e}")

def delete_recurring_from_sheet(rid):
    try:
        sh = _recurring_sheet()
        for i, r in enumerate(sh.get_all_records(), start=2):
            if str(r.get("ID")) == str(rid):
                sh.update_cell(i, 7, "удалён"); return
    except Exception as e: logger.error(f"delete_recurring: {e}")

_recurring_counter = [0]

def build_recurring_msg() -> str:
    if not recurring:
        return "🔄 Регулярных платежей нет.\n\nДобавь: «Учёба каждый месяц 24го 3000»"
    lines = ["🔄 *Регулярные платежи:*\n"]
    for r in recurring.values():
        lines.append(f"{r['emoji']} *{r['name']}* — {fmt(r['amount'])} ₴ каждое *{r['day']}-е* число")
    return "\n".join(lines)

async def fire_recurring_payments(context: ContextTypes.DEFAULT_TYPE):
    """Проверяем регулярные платежи — запускается ежедневно."""
    if not CHAT_ID: return
    now = datetime.now(KYIV_TZ)
    today = now.day
    fired = []
    for rid, r in recurring.items():
        if r["day"] == today:
            date_str = now.strftime("%d.%m.%Y %H:%M")
            save_expense(date_str, r["amount"], r["category"], r["name"], f"авто: {r['name']}")
            fired.append(r)
    if fired:
        lines = ["🔄 *Автоплатежи сегодня:*\n"]
        for r in fired:
            lines.append(f"{r['emoji']} *{r['name']}* — {fmt(r['amount'])} ₴")
        await context.bot.send_message(chat_id=CHAT_ID, text="\n".join(lines), parse_mode="Markdown")

# ── GROQ / LLM ───────────────────────────────────────────────────────────────
def transcribe(path: str) -> str:
    with open(path, "rb") as f:
        return groq_client.audio.transcriptions.create(
            model="whisper-large-v3", file=f, language="ru").text

def _llm(messages: list, max_tokens=600, temperature=0.0) -> str:
    r = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=messages, max_tokens=max_tokens, temperature=temperature,
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
            if depth == 0: return raw[s:i+1]
    return raw[s:]

# ── ПАРСЕР ТРАТ ──────────────────────────────────────────────────────────────
PARSE_SYSTEM = """Ты — парсер финансовых записей. Извлеки ВСЕ траты из сообщения.

КАТЕГОРИИ (используй одну из них ИЛИ придумай новую):
- "Еда / продукты" — еда, напитки, рестораны, кафе, доставка, магазины, пиво, алкоголь
- "Транспорт" — бензин, заправки, такси, парковка, мойка, СТО, метро, автобус
- "Развлечения" — игры, кино, стриминг, подписки, боулинг, концерты, бары
- "Здоровье / аптека" — лекарства, аптека, врачи, массаж, парикмахер, маникюр, спортзал
- "Никотин" — сигареты, снюс, вейп, кальян, ZYN, VELO
- Если не подходит ни одна — придумай КОНКРЕТНУЮ категорию 1-2 слова (например: "Одежда", "Подарки", "Ремонт", "Техника", "Питомец", "Коммунальные", "Музыка", "Спорт")
- ЗАПРЕЩЕНО использовать "Другое" — всегда придумывай конкретную категорию!

ПРАВИЛА:
1. Ищи ВСЕ суммы — "потратил", "заплатил", "купил", "вышло"
2. "к"/"тыс" = тысячи: "3к"=3000, "1.5к"=1500
3. Несколько трат через запятую/и — отдельный объект для каждой
4. "закинул на карту 500", "пополнение счета", "перевод" — НЕ трата, пропусти
5. "дал/дав Имени сумма" — НЕ трата (это долг), пропусти
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
    {"role":"user","content":"дал Саше 500"},
    {"role":"assistant","content":'[]'},
    {"role":"user","content":"пополнение счета 10"},
    {"role":"assistant","content":'[]'},
    {"role":"user","content":"сколько я потратил?"},
    {"role":"assistant","content":'[]'},
]

def parse_expenses(text: str) -> list:
    system = PARSE_SYSTEM
    if _user_categories:
        system += f"\n\nДОПОЛНИТЕЛЬНЫЕ КАТЕГОРИИ: {', '.join(_user_categories)}"
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
                item["category"] = fix_cat(item.get("category",""), keep_new=True)
                validated.append(item)
            except: continue
        return validated
    except Exception as e:
        logger.error(f"parse_expenses error: {e}")
        return []

# ── ФИНАНСОВЫЙ КОНТЕКСТ ───────────────────────────────────────────────────────
def get_financial_context(chat_id) -> str:
    now = datetime.now(KYIV_TZ)
    # Используем записи за зарплатный период (если есть зарплата) иначе за месяц
    period_recs = get_period_records(chat_id)
    month_recs = get_current_month_records()
    recs = period_recs if period_recs else month_recs
    s = analyze_records(recs)
    bs = get_budget_status(chat_id)
    sal = get_salary_info(chat_id)
  # Вчерашние траты
    yesterday_recs = get_yesterday_records()
    yesterday_spent = sum_records(yesterday_recs)
    period_start = get_period_start(chat_id)
    parts = [f"Сегодня: {now.strftime('%d.%m.%Y')}"]
    parts.append(f"Вчера ({(now-timedelta(days=1)).strftime('%d.%m')}): {fmt(yesterday_spent)} ₴")
    if s:
        parts.append(f"Траты за период (с {period_start.strftime('%d.%m')}): {fmt(s['total'])} ₴")
        cats = "; ".join(f"{c}: {fmt(a)}₴" for c,a in sorted(s["by_category"].items(), key=lambda x:-x[1])[:5])
        parts.append(f"По категориям: {cats}")
    if bs:
        parts.append(f"Бюджет: {fmt(bs['budget'])}₴, использовано {bs['percent']}%, осталось {fmt(bs['left'])}₴")
    if sal:
        sal_day = sal['day']
        parts.append(f"День зарплаты: {sal_day}-е число, сумма: {sal.get('amount','?')}₴")
    if debts:
        dl = "; ".join(f"{d['name']}: {format_amounts(d['amounts']).replace('*','')}" for d in list(debts.values())[:3])
        parts.append(f"Мне должны: {dl}")
    if goals:
        gl = "; ".join(f"{g['name']}: {fmt(g['saved'])}/{fmt(g['target'])}₴" for g in list(goals.values())[:3])
        parts.append(f"Цели: {gl}")
    return "\n".join(parts)

# ── ИИ ЧАТ ───────────────────────────────────────────────────────────────────
_ai_chat_history: dict = {}

async def ai_chat_response(chat_id, user_message: str) -> str:
    if chat_id not in _ai_chat_history: _ai_chat_history[chat_id] = []
    history = _ai_chat_history[chat_id]
    system = f"""Ты умный финансовый ИИ-ассистент. Отвечай кратко и по делу (3-5 предложений).
Долги — это деньги которые ТЕБЕ должны другие люди. Будь дружелюбным, с эмодзи.

ДАННЫЕ ПОЛЬЗОВАТЕЛЯ:
{get_financial_context(chat_id)}"""
    messages = [{"role":"system","content":system}]
    messages.extend(history[-10:])
    messages.append({"role":"user","content":user_message})
    try:
        response = groq_chat(messages, max_tokens=600)
        history.append({"role":"user","content":user_message})
        history.append({"role":"assistant","content":response})
        if len(history) > 20: _ai_chat_history[chat_id] = history[-20:]
        return response
    except Exception as e:
        logger.error(f"ai_chat: {e}")
        return "🤔 Не могу ответить сейчас. Попробуй чуть позже!"

# ── АНАЛИТИКА / ОТЧЁТЫ ───────────────────────────────────────────────────────
def build_advice_fallback(records: list) -> str:
    if not records: return ""
    s = analyze_records(records)
    if not s: return ""
    total = s["total"]; bc = s["by_category"]; tips = []
    food = bc.get("Еда / продукты", 0)
    if food > total * 0.35:
        tips.append(f"🍔 На еду {int(food/total*100)}%. −25% = *+{fmt(food*0.25)} ₴/мес*")
    nic = bc.get("Никотин", 0)
    if nic > 500:
        tips.append(f"🚬 Никотин: *{fmt(nic)} ₴/мес* = *{fmt(nic*12)} ₴/год*")
    if not tips: return "💡 Трать осознанно!"
    return "💡 *Советы:*\n" + "\n".join(f"{i}. {t}" for i,t in enumerate(tips,1))

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
    prompt = f"""Ты финансовый аналитик. Проанализируй динамику трат.

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
    habits_text = [f"- {desc}: {fmt(d['total']/n)}₴/мес, {fmt(d['total']/n*12)}₴/год ({d['count']} раз)"
                   for desc, d in sorted(habits.items(), key=lambda x:-x[1]["total"])[:8]]
    prompt = f"""Ты финансовый аналитик. Проанализируй регулярные траты.

ПРИВЫЧКИ:
{chr(10).join(habits_text)}

Напиши анализ:
- Заголовок 💸 *Стоимость привычек*
- Для каждой: сумма/мес и /год
- Выдели 1-2 где можно сэкономить
- Тон: без осуждения, с юмором, с эмодзи"""
    messages = [{"role":"system","content":"Отвечай только на русском. Markdown через *. Кратко."},
                {"role":"user","content":prompt}]
    try:
        return groq_chat(messages, max_tokens=500)
    except:
        lines = ["💸 *Стоимость привычек*\n"]
        for desc, d in sorted(habits.items(), key=lambda x:-x[1]["total"])[:6]:
            monthly = d["total"] / n
            lines += [f"*{desc.capitalize()}*", f"  📅 {fmt(monthly)} ₴/мес · 📆 {fmt(monthly*12)} ₴/год", ""]
        return "\n".join(lines)

async def build_advice_ai(chat_id) -> str:
    recs = get_current_month_records()
    if not recs or len(recs) < 3: return "📭 Маловато данных — запиши хотя бы 5-10 трат."
    s = analyze_records(recs)
    now = datetime.now(KYIV_TZ)
    avg = s["total"] / now.day if now.day else 0
    bs = get_budget_status(chat_id)
    sal = get_salary_info(chat_id)
    cats = ", ".join(f"{c}: {fmt(a)}₴ ({int(a/s['total']*100)}%)"
                     for c,a in sorted(s["by_category"].items(), key=lambda x:-x[1]))
    budget_info = f"Бюджет: {fmt(bs['budget'])}₴, использовано {bs['percent']}%, осталось {fmt(bs['left'])}₴" if bs else "бюджет не установлен"
    salary_info = f"Зарплата: {sal.get('amount','?')}₴, {sal['day']}-е число" if sal else "зарплата не указана"
    leaks = ", ".join(f"{k} ({v['count']}×={fmt(v['total'])}₴)" for k,v in list(s.get("leaks",{}).items())[:3])
    goals_info = ", ".join(f"{g['name']}: {fmt(g['saved'])}/{fmt(g['target'])}₴" for g in goals.values()) or "нет"
    prompt = f"""Ты личный финансовый советник. Дай конкретные советы.

ДАННЫЕ за {month_name(now.month)}:
- Потрачено: {fmt(s['total'])}₴ за {now.day} дней, среднее {fmt(avg)}₴/день
- Прогноз: {fmt(avg*30)}₴
- Категории: {cats}
- {budget_info}
- {salary_info}
- Частые траты: {leaks or 'нет'}
- Цели: {goals_info}

Напиши 3-4 совета:
- Заголовок 💡 *Персональные советы*
- Каждый совет с конкретной цифрой
- Тон: дружелюбный, практичный"""
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
    kb = inline_kb([[("✅ Вернули","paid_"+did),("⏰ Напомнить ещё","remind_"+did)]])
    await context.bot.send_message(
        chat_id=cid,
        text=f"⏰ *Напоминание о долге*\n\n👤 *{d['name']}* должен тебе {format_amounts(ams)}",
        parse_mode="Markdown", reply_markup=kb)

async def send_weekly_insight(context: ContextTypes.DEFAULT_TYPE):
    cid = (context.job.data or {}).get("chat_id") or CHAT_ID
    if not cid: return
    recs = get_week_records()
    month_recs = get_current_month_records()
    if not recs:
        await context.bot.send_message(chat_id=cid, text="📭 За эту неделю данных нет."); return
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
    await context.bot.send_message(chat_id=cid, text="\n".join(lines), parse_mode="Markdown")

async def send_morning_briefing(context: ContextTypes.DEFAULT_TYPE):
    cid = (context.job.data or {}).get("chat_id") or CHAT_ID
    if not cid: return
    bs = get_budget_status(cid)
    sal = get_salary_info(cid)
    now = datetime.now(KYIV_TZ)
    day_names_short = ["Пн","Вт","Ср","Чт","Пт","Сб","Вс"]
    day_name = day_names_short[now.weekday()]
    lines = [f"☀️ *Доброе утро! {now.strftime('%d.%m')} ({day_name})*\n"]

    # Вчерашние траты (корректно — берём записи за вчера)
    yesterday_recs = get_yesterday_records()
    yesterday_spent = sum_records(yesterday_recs)
    if yesterday_spent > 0:
        lines.append(f"📌 Вчера потрачено: *{fmt(yesterday_spent)} ₴*")
        # Топ категория вчера
        if yesterday_recs:
            by_cat: dict = {}
            sk = get_sum_key(yesterday_recs)
            for r in yesterday_recs:
                cat = r.get("Категория","?")
                by_cat[cat] = by_cat.get(cat, 0) + float(r.get(sk, 0) or 0)
            if by_cat:
                top_cat = max(by_cat, key=by_cat.get)
                lines.append(f"   └ Топ: {get_category_emoji(top_cat)} {top_cat} — {fmt(by_cat[top_cat])} ₴")
    else:
        lines.append("📌 Вчера трат не было 👌")

    lines.append("")

    # Бюджет (с прогресс-баром)
    if bs:
        pct = bs["percent"]
        filled = pct // 10
        bar = "█" * filled + "░" * (10 - filled)
        status_icon = "🟢" if pct < 70 else "🟡" if pct < 90 else "🔴"
        lines.append(f"{status_icon} Бюджет: [{bar}] *{pct}%*")
        lines.append(f"   Потрачено: {fmt(bs['spent'])} / {fmt(bs['budget'])} ₴")
        # Лимит на сегодня = остаток / дни до зарплаты
        sal_day = get_salary_day(cid)
        if now.day < sal_day:
            days_to_sal = sal_day - now.day
        else:
            nm = (now.replace(day=1) + timedelta(days=32)).replace(day=1)
            days_to_sal = (nm.replace(day=min(sal_day, 28)) - now).days
        if days_to_sal > 0 and bs["left"] > 0:
            daily_limit = bs["left"] / days_to_sal
            lines.append(f"   💡 Лимит сегодня: *{fmt(daily_limit)} ₴* (до зарплаты {days_to_sal} дн.)")
        elif bs["left"] <= 0:
            lines.append(f"   ❗ Бюджет превышен на *{fmt(abs(bs['left']))} ₴*")
    elif sal and sal.get("amount"):
          # Нет бюджета, но есть зарплата
        spent = sum_records(get_period_records(cid))
        left = float(sal["amount"]) - spent
        sal_day = sal["day"]
        if now.day < sal_day:
            days_to_sal = sal_day - now.day
        else:
            nm = (now.replace(day=1) + timedelta(days=32)).replace(day=1)
            days_to_sal = (nm.replace(day=min(sal_day, 28)) - now).days
        pct = min(int(spent / float(sal["amount"]) * 100), 100)
        filled = pct // 10
        bar = "█" * filled + "░" * (10 - filled)
        status_icon = "🟢" if pct < 70 else "🟡" if pct < 90 else "🔴"
        lines.append(f"{status_icon} Зарплата: [{bar}] *{pct}%*")
        lines.append(f"   Потрачено: {fmt(spent)} / {fmt(float(sal['amount']))} ₴")
        if days_to_sal > 0 and left > 0:
            lines.append(f"   💡 Можно тратить: *{fmt(left/days_to_sal)} ₴/день*")

    # Траты за текущий период
    period_recs = get_period_records(cid) if (bs or sal) else get_current_month_records()
    period_spent = sum_records(period_recs)
    if period_spent > 0 and not (bs or sal):
        lines.append(f"\n📊 За месяц: *{fmt(period_spent)} ₴*")

    if len(lines) > 2:
        await context.bot.send_message(chat_id=cid, text="\n".join(lines), parse_mode="Markdown")

# ── КЛАВИАТУРА ───────────────────────────────────────────────────────────────
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
    [("💳 Рассрочки","menu_installments"),("🔄 Автоплатежи","menu_recurring")],
])
REMINDER_KB = inline_kb([
    [("1 день","reminder_1"),("3 дня","reminder_3")],
    [("1 неделю","reminder_7"),("2 недели","reminder_14")],
    [("3 недели","reminder_21"),("1 месяц","reminder_30")],
])

# ── ВСПОМОГАТЕЛЬНАЯ: клавиатура выбора категории для суммы ──────────────────
def build_category_kb(amount: float) -> InlineKeyboardMarkup:
    """Создаёт инлайн-клавиатуру выбора категории для суммы без описания."""
    cats = get_all_categories()
    rows = []
    row = []
    for i, cat in enumerate(cats):
        emoji = get_category_emoji(cat)
        # callback_data: quick_{cat}_{amount}  — cat может содержать пробелы и /
        # используем индекс категории чтобы не ломать split
        btn_data = f"qcat_{i}_{amount}"
        row.append((f"{emoji} {cat}", btn_data))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return inline_kb(rows)

async def cmd_stats_inline(chat_id, context):
    now = datetime.now(KYIV_TZ)
    period_start = get_period_start(chat_id)
    recs = get_period_records(chat_id)
    if not recs:
        await context.bot.send_message(chat_id=chat_id, text="📭 В этом месяце ещё нет записей."); return
    s = analyze_records(recs)
    days_in_period = max((now - period_start).days + 1, 1)
    avg = s["total"] / days_in_period
    period_label = f"с {period_start.strftime('%d.%m')}"
    lines = [f"📊 *Статистика {period_label}* ({s['count']} записей)\n"]
    lines += _cat_lines(s)
    lines += [f"\n💰 *Итого: {fmt(s['total'])} ₴*", f"📈 Среднее: *{fmt(avg)} ₴/день*"]
    bs = get_budget_status(chat_id)
    if bs:
        bar = "█"*(bs["percent"]//10) + "░"*(10-bs["percent"]//10)
        lines += [f"\n{'🟢' if bs['percent']<70 else '🟡' if bs['percent']<90 else '🔴'} Бюджет: [{bar}] {bs['percent']}%",
                  f"Потрачено: *{fmt(bs['spent'])} ₴* / {fmt(bs['budget'])} ₴",
                  f"Осталось: *{fmt(bs['left'])} ₴*"]
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
    _records_cache.clear(); _settings.clear()
    load_settings(); load_user_categories()
    debts.clear(); debt_counter[0] = 0; load_debts()
    goals.clear(); goal_counter[0] = 0; load_goals()
    await update.message.reply_text(
        "👋 Привет! Я твой финансовый *AI-агент*.\n\n"
        "Просто пиши как обычно:\n"
        "🛒 «Снюс 800» — запишу трату\n"
        "💸 «Дал Саше 500» — запишу что Саша должен тебе\n"
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

async def cmd_installments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = build_installments_msg()
    if installments:
        kb = inline_kb([[("💳 Внести платёж","inst_pay_menu"),("🗑 Закрыть","inst_close_menu")]])
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=kb)
    else:
        await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_recurring(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = build_recurring_msg()
    if recurring:
        kb = inline_kb([[("🗑 Удалить платёж","recur_del_menu")]])
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=kb)
    else:
        await update.message.reply_text(msg, parse_mode="Markdown")

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎙 Распознаю...")
    try:
        file = await context.bot.get_file(update.message.voice.file_id)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            await file.download_to_drive(tmp.name); path = tmp.name
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

    routes = {
        "📊 Статистика": cmd_stats, "📅 Отчёт за неделю": cmd_week,
        "📆 Отчёт за месяц": cmd_month, "💰 Бюджет": cmd_budget,
        "💸 Долги": cmd_debts, "💵 Зарплата": cmd_salary,
        "🎯 Цели": cmd_goals, "💱 Курс валют": cmd_rates,
    }
    if text in routes: await routes[text](update, context); return
    if text == "💰 Финансы":
        await update.message.reply_text("💰 *Финансы*:", parse_mode="Markdown", reply_markup=FINANCE_KB); return
    if text == "📊 Аналитика":
        await update.message.reply_text("📊 *Аналитика*:", parse_mode="Markdown", reply_markup=ANALYTICS_KB); return
    if text == "⚙️ Прочее":
        await update.message.reply_text("⚙️ *Прочее*:", parse_mode="Markdown", reply_markup=OTHER_KB); return
    if text == "🎯 Цели": await cmd_goals(update, context); return

    # ── ПРИОРИТЕТ 1: частичное погашение долга ──────────────────────────────
    if "partial_debt_id" in context.user_data:
        did = context.user_data.get("partial_debt_id")
        idx = context.user_data.get("partial_amt_idx", 0)
        try:
            amount = float(text.strip().replace(",",".").replace(" ",""))
            if amount > 0 and did in debts:
                ex_ams = list(debts[did].get("amounts",[]))
                if idx < len(ex_ams):
                    ea = ex_ams[idx]
                    sym = CURRENCY_SYMBOLS.get(ea.get("currency","UAH"),"₴")
                    new_val = float(ea["amount"]) - amount
                    d = debts[did]
                    if new_val <= 0:
                        ex_ams = [a for i,a in enumerate(ex_ams) if i != idx]
                        msg = f"✅ *{d['name']}* вернул *{fmt(amount)} {sym}*\n💰 Этот долг закрыт"
                    else:
                        ea["amount"] = new_val
                        msg = f"✅ *{d['name']}* вернул *{fmt(amount)} {sym}*\n📊 Остаток: *{fmt(new_val)} {sym}*"
                    if not ex_ams:
                        debts.pop(did); mark_paid(did)
                        msg += "\n\n🎉 *Долг полностью закрыт!*"
                    else:
                        debts[did]["amounts"] = ex_ams
                        update_debt_amounts(did, ex_ams)
                    del context.user_data["partial_debt_id"]
                    context.user_data.pop("partial_amt_idx", None)
                    await update.message.reply_text(msg, parse_mode="Markdown")
                    return
        except (ValueError, TypeError):
            pass
        # Ввели не число — отмена
        del context.user_data["partial_debt_id"]
        context.user_data.pop("partial_amt_idx", None)

    # ── ПРИОРИТЕТ 2: пополнение цели ────────────────────────────────────────
    if "goal_deposit_id" in context.user_data:
        gid = context.user_data.get("goal_deposit_id")
        try:
            amount = float(text.strip().replace(",",".").replace(" ",""))
            if amount > 0 and gid in goals:
                g = goals[gid]
                g["saved"] = min(g["saved"] + amount, g["target"])
                update_goal_saved(gid, g["saved"])
                pct = min(int(g["saved"] / g["target"] * 100), 100)
                bar = build_goal_bar(g["saved"], g["target"])
                msg = f"🎯 *{g['name']}*\n[{bar}] {pct}%\n{fmt(g['saved'])} / {fmt(g['target'])} ₴"
                if g["saved"] >= g["target"]:
                    msg += "\n\n🎉 *Цель достигнута!*"
                    goals.pop(gid); close_goal(gid)
                del context.user_data["goal_deposit_id"]
                await update.message.reply_text(msg, parse_mode="Markdown")
                return
        except (ValueError, TypeError):
            pass
        del context.user_data["goal_deposit_id"]

    # ── ПРОСТО ЧИСЛО: спрашиваем категорию ──────────────────────────────────
    # Срабатывает когда пользователь пишет только сумму без описания
    # Примеры: "1650", "500", "3к", "1.5к", "200 грн"
    just_number_match = JUST_NUMBER_PATTERN.match(text.strip())
    if just_number_match:
        amount_raw = just_number_match.group(1).strip().replace(" ", "").replace(",", ".")
        mult = 1000 if re.search(r"[кК](?:р|ривень|уб)?$|тис", amount_raw, re.IGNORECASE) else 1
        amount_clean_str = re.sub(r"[кКтТис]+.*$", "", amount_raw, flags=re.IGNORECASE)
        try:
            amount_val = float(amount_clean_str) * mult
            if amount_val > 0:
                set_ctx(chat_id, pending_amount=amount_val)
                kb = build_category_kb(amount_val)
                await update.message.reply_text(
                    f"💰 *{fmt(amount_val)} ₴* — что это?\n\nВыбери категорию:",
                    parse_mode="Markdown",
                    reply_markup=kb
                )
                return
        except (ValueError, TypeError):
            pass  # не смогли распарсить — идём дальше обычным путём

    # Быстрый undo
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

    # Быстрый fallback: добавить категорию
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


# ── REGEX-РОУТЕР ──────────────────────────────────────────────────────────────
def _regex_route(text: str) -> list | None:
    t = text.strip()

    # Рассрочка — оплата (до проверки не-трат чтобы не пропустить)
    if re.search(r"оплатил|заплатил|внёс|вніс", t, re.IGNORECASE) and \
       re.search(r"рассрочк|розстрочк|платіж|платеж", t, re.IGNORECASE):
        # Ищем имя рассрочки
        m = re.search(r"(?:рассрочк[уи]?|розстрочк[уи]?|платіж|платеж)\s+([А-ЯЁа-яёіїєa-zA-Z][^\d\n]{1,30}?)(?:\s+(\d[\d.,\s]*))?$", t, re.IGNORECASE)
        name_hint = m.group(1).strip() if m else ""
        amount_raw = m.group(2).strip() if (m and m.group(2)) else "0"
        try: amount = float(amount_raw.replace(",",".").replace(" ",""))
        except: amount = 0
        return [{"action":"installment_pay","name":name_hint,"amount":amount}]

    # Регулярный платёж — "учёба каждый месяц 24го 3000"
    m = re.search(
        r"(.+?)\s+каждый?\s+мес\w*\s+(\d{1,2})-?(?:го|числа)?\s+(\d[\d.,\s]*(?:к)?)"
        r"|(.+?)\s+(\d{1,2})-?(?:го|числа)\s+каждый?\s+мес\w*\s+(\d[\d.,\s]*(?:к)?)",
        t, re.IGNORECASE
    )
    if m:
        try:
            g = m.groups()
            if g[0]:  # первый вариант
                name, day_s, amt_s = g[0].strip(), g[1], g[2]
            else:     # второй вариант
                name, day_s, amt_s = g[3].strip(), g[4], g[5]
            name = name.capitalize()
            day = int(day_s)
            amt_s = amt_s.strip().replace(" ","").replace(",",".")
            mult = 1000 if amt_s.lower().endswith("к") else 1
            amt_s = re.sub(r"[кК]$","",amt_s)
            amount = float(amt_s) * mult
            if 1 <= day <= 31 and amount > 0:
              cat, em = infer_category_from_name(name) 
            return [{"action":"recurring_new","name":name,"amount":amount,"day":day,"category":cat,"emoji":em}]
        except: pass

    # Не-траты → LLM
    if NON_EXPENSE_PATTERNS.search(t):
        logger.info(f"RegexRouter: пропуск (не-трата) '{t[:50]}'")
        return None

    # Долги → LLM
    if DEBT_PATTERNS.search(t):
        logger.info(f"RegexRouter: пропуск (долг) '{t[:50]}'")
        return None

    # Валютные суммы → LLM
    if CURRENCY_AMOUNT_PATTERNS.search(t):
        logger.info(f"RegexRouter: пропуск (валюта) '{t[:50]}'")
        return None

   
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
                x_clean = x.strip().replace(" ","").replace(",",".")
                mult = 1000 if x_clean.lower().endswith("к") else 1
                x_clean = re.sub(r"[кК]$","",x_clean)
                candidates.append(float(x_clean) * mult)
            day = int(candidates[0]) if candidates[0] <= 31 else int(candidates[1])
            amt = candidates[1] if candidates[0] <= 31 else candidates[0]
            if 1 <= day <= 31 and amt > 0:
                return [{"action":"salary_set","day":day,"amount":amt}]
        except: pass

    # Бюджет
    m = re.search(r"бюджет[^\d]*(\d[\d\s]*(?:[.,]\d+)?(?:\s*к)?)", t, re.IGNORECASE)
    if m:
        s = m.group(1).strip().replace(" ","").replace(",",".")
        mult = 1000 if s.lower().endswith("к") else 1
        s = re.sub(r"[кК]$","",s)
        try: return [{"action":"budget_set","amount":float(s)*mult}]
        except: pass

    # Трата: "кофе 85" или "85 кофе"
    m = re.match(
        r"^([а-яёіїєa-zA-Z][а-яёіїєa-zA-Z\w\s/-]{0,40}?)\s+"
        r"(\d[\d\s]*(?:[.,]\d+)?(?:\s*к(?:р|ривень|уб)?|\s*тис(?:яч)?)?)\s*(?:₴|грн|грн\.?|uah)?$",
        t, re.IGNORECASE
    ) or re.match(
        r"^(\d[\d\s]*(?:[.,]\d+)?(?:\s*к(?:р|ривень|уб)?|\s*тис(?:яч)?)?)\s*(?:₴|грн|uah)?\s+"
        r"([а-яёіїєa-zA-Z][а-яёіїєa-zA-Z\w\s/-]{1,40})$",
        t, re.IGNORECASE
    )
    if m:
        g = m.groups()
        def parse_amount(s):
            s = s.strip().replace(" ","").replace(",",".")
            mult = 1000 if re.search(r"к(?:р|ривень|уб)?$|тис", s, re.IGNORECASE) else 1
            s = re.sub(r"[кКтТис]+.*$","",s,flags=re.IGNORECASE)
            try: return float(s) * mult
            except: return None
        amt = parse_amount(g[1]) or parse_amount(g[0])
        desc = (g[0] if parse_amount(g[1]) else g[1]).strip().lower()
        if amt and amt > 0 and desc and not re.match(r"^\d", desc):
            if DEBT_PATTERNS.search(desc): return None
            cat_map = [
                (["еда","продукт","обед","ужин","завтрак","кафе","ресторан","пицца","суши",
                  "шаурма","бургер","кофе","чай","сок","доставка","магазин","ашан","сільпо",
                  "атб","новус","перекус","снек","фрукт","хлеб","молоко","алко"], "Еда / продукты","🍔"),
                (["такси","бензин","заправк","автобус","метро","маршрутк","парковк","мойк",
                  "ремонт авто","запчаст","uber","bolt","поїзд","автовокзал"], "Транспорт","🚗"),
                (["кино","игр","steam","netflix","spotify","боулинг","концерт","клуб",
                  "розваг","підписк","subscription","iptv"], "Развлечения","🎮"),
                (["аптек","лікарств","лекарств","врач","лікар","медиц","стоматолог","масаж","массаж",
                  "парикмах","манікюр","маникюр","спортзал","фитнес","косметолог"], "Здоровье / аптека","💊"),
                (["снюс","сигарет","вейп","кальян","никотин","zyn","velo","табак","тютюн"], "Никотин","🚬"),
                (["гимнастик","йога","плаван","бокс","тренировк","тренуван","зал ","спорт ",
                  "пробежк","бег","велосипед","воркаут","качалк"], "Спорт","💪"),
                (["курс","учеб","навчан","репетитор","школ","универ","книг","образован",
                  "english","урок","лекци","семинар"], "Образование","📚"),
                (["одяг","одежд","обувь","шопинг","брюки","футболк","платт","куртк","сумк","взуття"], "Одежда","👕"),
                (["комунальн","квартир","аренд","оренд","кварплат","жкх","свет","газ",
                  "вода","інтернет","мобільн","мобильн","телефон"], "Коммунальные","🏠"),
            ]
            category, emoji = "Другое","📦"
            for keywords, cat, em in cat_map:
                if any(k in desc for k in keywords):
                    category, emoji = cat, em; break
            for uc in _user_categories:
                if uc.lower() in desc or desc in uc.lower():
                    category = uc; emoji = get_category_emoji(uc); break
            return [{"action":"expense","expenses":[{"amount":amt,"category":category,"description":desc.capitalize(),"emoji":emoji}]}]
    return None


# ── LLM-РОУТЕР ───────────────────────────────────────────────────────────────
ROUTER_SYSTEM = """Финансовый бот. Верни ТОЛЬКО JSON {{"actions":[...]}}.

ДЕЙСТВИЯ:
expense: {{"action":"expense","expenses":[{{"amount":N,"category":"C","description":"D","emoji":"E"}}]}}
debt_new: {{"action":"debt_new","name":"N","amounts":[{{"amount":N,"currency":"UAH"}}],"note":"","interest":0}}
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
installment_new: {{"action":"installment_new","name":"N","total":N,"monthly":N,"months":N}}
installment_pay: {{"action":"installment_pay","name":"N","amount":0}}
recurring_new: {{"action":"recurring_new","name":"N","amount":N,"day":N,"category":"C","emoji":"E"}}
question: {{"action":"question","text":"T"}}

КАТЕГОРИИ: "Еда / продукты","Транспорт","Развлечения","Здоровье / аптека","Никотин"
ЗАПРЕЩЕНО использовать "Другое"! Придумай конкретную категорию (Одежда, Подарки, Ремонт, Музыка и т.д.)
Пользовательские: {user_cats}
МНЕ ДОЛЖНЫ: {debts} | ЦЕЛИ: {goals} | КОНТЕКСТ: {context}

ПРАВИЛА ДОЛГОВ (КРИТИЧНО!):
- "дал/дав/позичив Имя сумму" → debt_new
- "дал в долг Имя сумму под X%" → debt_new с interest=X
- "Имя борг/долг сумма" → debt_new
- "Имя вернул/повернув/отдал мне" → debt_return
- НЕ путать: "я отдал Имя" ≠ debt_return

ПРАВИЛА РАССРОЧКИ:
- "рассрочка/в рассрочку Название СУММА на N мес ПЛАТЁЖ" → installment_new
- "оплатил рассрочку/платёж Название" → installment_pay
- "оплатил рассрочку колёс/колёса/колёсо" — name="Колёса" (ищи ближайшее совпадение)

ПРАВИЛА РЕГУЛЯРНЫХ ПЛАТЕЖЕЙ:
- "учёба/курс/абонемент каждый месяц Nго СУММА" → recurring_new
- "добавь регулярный платёж Название СУММА Nго числа" → recurring_new

ДРУГИЕ ПРАВИЛА:
- "3к"=3000 | рус/укр одинаково
- несколько команд→все в массиве
- "закинул/пополнил счет/карту" → question (не трата)
- суммы с USD/EUR → currency="USD"/"EUR"
{{"actions": [...]}}"""

async def route_message(text: str, chat_id, conv_ctx: dict) -> list:
    debts_info = ", ".join(f"{d['name']}: {format_amounts(d['amounts']).replace('*','')}" for d in list(debts.values())[:5]) or "нет"
    goals_info = ", ".join(f"{g['name']}: {fmt(g['saved'])}/{fmt(g['target'])}₴" for g in list(goals.values())[:3]) or "нет"
    last_msgs = conv_ctx.get("last_messages",[])
    ctx_info = (f"имя: {conv_ctx.get('last_name','нет')}, "
                f"действие: {conv_ctx.get('last_action','нет')}, "
                f"последние: {'; '.join(last_msgs[-3:]) if last_msgs else 'нет'}")
    user_cats_info = ", ".join(_user_categories) if _user_categories else "нет"

    system = ROUTER_SYSTEM.format(context=ctx_info, debts=debts_info, goals=goals_info, user_cats=user_cats_info)

    messages = [
        {"role":"system","content":system},
        {"role":"user","content":"снюс 800"},
        {"role":"assistant","content":'{"actions":[{"action":"expense","expenses":[{"amount":800,"category":"Никотин","description":"снюс","emoji":"🚬"}]}]}'},
        {"role":"user","content":"дал Саше 500"},
        {"role":"assistant","content":'{"actions":[{"action":"debt_new","name":"Саша","amounts":[{"amount":500,"currency":"UAH"}],"note":""}]}'},
        {"role":"user","content":"дал папе 2000"},
        {"role":"assistant","content":'{"actions":[{"action":"debt_new","name":"Папа","amounts":[{"amount":2000,"currency":"UAH"}],"note":""}]}'},
        {"role":"user","content":"дал папе 55000 долларов"},
        {"role":"assistant","content":'{"actions":[{"action":"debt_new","name":"Папа","amounts":[{"amount":55000,"currency":"USD"}],"note":""}]}'},
        {"role":"user","content":"папа борг 2000 доларів"},
        {"role":"assistant","content":'{"actions":[{"action":"debt_new","name":"Папа","amounts":[{"amount":2000,"currency":"USD"}],"note":""}]}'},
        {"role":"user","content":"папа долг + 55 000 доларів"},
        {"role":"assistant","content":'{"actions":[{"action":"debt_add","name":"Папа","amount":55000,"currency":"USD"}]}'},
        {"role":"user","content":"Саша вернул 300"},
        {"role":"assistant","content":'{"actions":[{"action":"debt_return","name":"Саша","amount":300,"currency":"UAH"}]}'},
        {"role":"user","content":"папа отдал 55000"},
        {"role":"assistant","content":'{"actions":[{"action":"debt_return","name":"Папа","amount":55000,"currency":"UAH"}]}'},
        {"role":"user","content":"папа вернул 1000 долларов"},
        {"role":"assistant","content":'{"actions":[{"action":"debt_return","name":"Папа","amount":1000,"currency":"USD"}]}'},
        {"role":"user","content":"пополнение счета 500"},
        {"role":"assistant","content":'{"actions":[{"action":"question","text":"Пополнение счёта — не трата, ничего не записал."}]}'},
        {"role":"user","content":"дал Саше 500 и бюджет 25к"},
        {"role":"assistant","content":'{"actions":[{"action":"debt_new","name":"Саша","amounts":[{"amount":500,"currency":"UAH"}],"note":""},{"action":"budget_set","amount":25000}]}'},
        {"role":"user","content":"зарплата 6-го 25 000"},
        {"role":"assistant","content":'{"actions":[{"action":"salary_set","day":6,"amount":25000}]}'},
        {"role":"user","content":"100 баксів в гривні"},
        {"role":"assistant","content":'{"actions":[{"action":"convert","amount":100,"from_currency":"USD","to_currency":"UAH"}]}'},
        {"role":"user","content":"сколько потратил на еду?"},
        {"role":"assistant","content":'{"actions":[{"action":"question","text":"сколько потратил на еду?"}]}'},
        {"role":"user","content":text},
    ]
    try:
        raw = _llm(messages, max_tokens=500, temperature=0.0)
        raw = _extract_json(raw, "{")
        result = json.loads(raw)
        actions = result.get("actions",[result])
        return actions if isinstance(actions, list) else [actions]
    except Exception as e:
        logger.error(f"route_message error: {e} | raw: {raw if 'raw' in dir() else '?'}")
        return [{"action":"unknown"}]


async def execute_action(route: dict, update, context, chat_id: int, text: str, conv_ctx: dict) -> str | None:
    action = route.get("action","unknown")

    if action == "expense":
        expenses = route.get("expenses",[])
        if not expenses: return "🤔 Не понял сумму. Например: «Кофе 85»"
        date = datetime.now(KYIV_TZ).strftime("%d.%m.%Y %H:%M")
        month_recs = get_current_month_records()
        lines = ["✅ *Записано!*\n"]
        new_cats_to_suggest = []
        for exp in expenses:
            amount = float(str(exp.get("amount",0)).replace(",","."))
            if amount <= 0: continue
            raw_cat = (exp.get("category") or "").strip()
            # Определяем итоговую категорию — keep_new=True сохраняет то что придумала LLM
            cat = fix_cat(raw_cat, keep_new=True) if raw_cat else "Другое"
            is_new = cat not in get_all_categories() and cat != "Другое"
            desc = exp.get("description","—")
            emoji = exp.get("emoji","") or get_category_emoji(cat)
            save_expense(date, amount, cat, desc, text)
            lines.append(f"{emoji} *{desc}* — *{fmt(amount)} ₴*\n   _{get_category_emoji(cat)} {cat}_")
            if is_new and cat not in [c for c,_ in new_cats_to_suggest]:
                new_cats_to_suggest.append((cat, emoji))
        if len(expenses) > 1:
            total = sum(float(str(e.get("amount",0)).replace(",",".")) for e in expenses)
            lines.append(f"\n💰 *Итого: {fmt(total)} ₴*")
        cat0 = fix_cat((expenses[0].get("category") or "Другое"), keep_new=True)
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
        # Предлагаем сохранить новые категории
        if new_cats_to_suggest and update is not None:
            await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
            for new_cat, new_emoji in new_cats_to_suggest:
                em = new_emoji or get_category_emoji(new_cat)
                kb = inline_kb([
                    [(f"✅ Добавить «{new_cat}»", f"savecat_{new_cat}")],
                    [("Нет, не нужно", "savecat_skip")],
                ])
                await update.message.reply_text(
                    f"💡 Новая категория {em} *{new_cat}* — добавить в список?",
                    parse_mode="Markdown", reply_markup=kb)
            return None
        return "\n".join(lines)

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
                desc_ok = desc_hint in r.get("Описание","").lower() if desc_hint else True
                amt_ok = str(int(float(amount_hint))) in str(r.get(sk,"")) if amount_hint else True
                cat_ok = cat_hint in r.get("Категория","").lower() if cat_hint else True
                if desc_ok and amt_ok and cat_ok:
                    sh.delete_rows(row_idx); _invalidate("sheet1")
                    return f"🗑 Запись *{r.get('Описание','?')}* — *{r.get(sk,'?')} ₴* удалена."
            return "🤔 Не нашёл такую запись."
        except Exception as e:
            logger.error(f"expense_delete: {e}"); return "❌ Не удалось удалить запись."

    elif action == "debt_new":
        name = str(route.get("name","")).strip().capitalize()
        ams = route.get("amounts",[])
        if not ams:
            amt = route.get("amount")
            cur = route.get("currency","UAH")
            if amt: ams = [{"amount":float(amt),"currency":cur}]
        if not name or not ams: return "🤔 Не понял кому и сколько."
        detected_cur = _detect_currency(text)
        if detected_cur != "UAH":
            for a in ams:
                if a.get("currency","UAH") == "UAH":
                    a["currency"] = detected_cur
        interest = float(route.get("interest", 0) or 0)
        # Проверяем процент из текста если LLM не распознал
        if not interest:
            m_interest = re.search(r"под\s+(\d+(?:[.,]\d+)?)\s*%", text, re.IGNORECASE)
            if m_interest:
                interest = float(m_interest.group(1).replace(",","."))
        existing = next((k for k,d in debts.items() if name.lower() in d["name"].lower()), None)
        if existing:
            ex_ams = debts[existing].get("amounts",[])
            for na in ams:
                cur = na.get("currency","UAH")
                found = next((a for a in ex_ams if a.get("currency","UAH")==cur), None)
                if found: found["amount"] = float(found["amount"]) + float(na["amount"])
                else: ex_ams.append(na)
            debts[existing]["amounts"] = ex_ams
            if interest: debts[existing]["interest"] = interest
            update_debt_amounts(existing, ex_ams)
            set_ctx(chat_id, last_name=name, last_action="debt_add")
            return f"➕ *{name}* — долг обновлён\n💰 Итого: {format_amounts(ex_ams)}"
        note = route.get("note","")
        debt_counter[0] += 1
        did = str(debt_counter[0])
        date_str = datetime.now(KYIV_TZ).strftime("%d.%m.%Y")
        debts[did] = {"name":name,"amounts":ams,"date":date_str,"note":note,"interest":interest}
        save_debt(did, name, ams, date_str, note, interest)
        context.job_queue.run_once(send_debt_reminder, when=get_reminder_interval(chat_id),
            data={"debt_id":did,"chat_id":chat_id}, name=f"debt_{did}")
        set_ctx(chat_id, last_name=name, last_action="debt_new")
        note_str = f"\n📝 {note}" if note else ""
        interest_str = f"\n📈 Процент: *{interest}% в месяц*" if interest else ""
        return (f"💸 *Записал долг!*\n\n👤 *{name}* должен тебе:\n💰 {format_amounts(ams)}{note_str}{interest_str}\n\n"
                f"_«ещё 500» — добавит к долгу_\n⏰ Напомню через {reminder_label(chat_id)}.")

    elif action == "debt_add":
        name = str(route.get("name", conv_ctx.get("last_name",""))).strip().capitalize()
        amount = float(str(route.get("amount",0)).replace(",","."))
        currency = route.get("currency","UAH")
        detected_cur = _detect_currency(text)
        if detected_cur != "UAH" and currency == "UAH":
            currency = detected_cur
        did = next((k for k,d in debts.items() if name.lower() in d["name"].lower()), None)
        if not did: return f"🤔 Не нашёл долга для *{name}*."
        ex_ams = debts[did].get("amounts",[])
        found = next((a for a in ex_ams if a.get("currency","UAH")==currency), None)
        if found: found["amount"] = float(found["amount"]) + amount
        else: ex_ams.append({"amount":amount,"currency":currency})
        debts[did]["amounts"] = ex_ams
        update_debt_amounts(did, ex_ams)
        sym = CURRENCY_SYMBOLS.get(currency,"₴")
        set_ctx(chat_id, last_name=debts[did]["name"], last_action="debt_add")
        return f"➕ Добавил *{fmt(amount)} {sym}* к *{debts[did]['name']}*\n💰 Итого: {format_amounts(ex_ams)}"

    elif action == "debt_return":
        name = str(route.get("name", conv_ctx.get("last_name",""))).strip().capitalize()
        did = next((k for k,d in debts.items() if name.lower() in d["name"].lower()), None)
        if not did: return f"🤔 Не нашёл долга для *{name}*."
        d = debts[did]
        ret_amount = route.get("amount")
        currency = route.get("currency","UAH")
        detected_cur = _detect_currency(text)
        if detected_cur != "UAH" and currency == "UAH":
            currency = detected_cur
        ex_ams = list(d.get("amounts",[]))
        lines = [f"✅ *{d['name']}* вернул тебе:\n"]
        if ret_amount:
            for ea in ex_ams:
                if ea.get("currency","UAH") == currency:
                    new = float(ea["amount"]) - float(ret_amount)
                    sym = CURRENCY_SYMBOLS.get(currency,"₴")
                    if new <= 0:
                        ex_ams = [a for a in ex_ams if a.get("currency","UAH") != currency]
                        lines.append(f"💰 {sym}: долг закрыт")
                    else:
                        ea["amount"] = new
                        lines.append(f"💸 {sym}: вернул {fmt(ret_amount)} → остаток *{fmt(new)} {sym}*")
                    break
            else:
                if ex_ams:
                    ea = ex_ams[0]
                    new = float(ea["amount"]) - float(ret_amount)
                    sym = CURRENCY_SYMBOLS.get(ea.get("currency","UAH"),"₴")
                    if new <= 0:
                        ex_ams = []
                        lines.append(f"💰 {sym}: долг закрыт")
                    else:
                        ea["amount"] = new
                        lines.append(f"💸 {sym}: вернул {fmt(ret_amount)} → остаток *{fmt(new)} {sym}*")
        else:
            ex_ams = []
            lines.append("💰 Вернул всё!")
        if not ex_ams:
            debts.pop(did); mark_paid(did); lines.append("\n🎉 *Долг полностью закрыт!*")
        else:
            debts[did]["amounts"] = ex_ams; update_debt_amounts(did, ex_ams)
            lines.append(f"\n📊 Остаток: {format_amounts(ex_ams)}")
        set_ctx(chat_id, last_name=d["name"], last_action="debt_return")
        return "\n".join(lines)

    elif action == "budget_set":
        budget = float(str(route.get("amount",0)).replace(",","."))
        if budget <= 0: return "🤔 Не понял сумму бюджета."
        save_setting(f"budget_{chat_id}", str(budget))
        bs = get_budget_status(chat_id)
        bar = "█"*(bs["percent"]//10) + "░"*(10-bs["percent"]//10) if bs else "░░░░░░░░░░"
        pct = bs["percent"] if bs else 0
        return f"💰 *Бюджет: {fmt(budget)} ₴*\n[{bar}] {pct}% использовано"

    elif action == "salary_set":
        day = int(route.get("day",1))
        amount = route.get("amount")
        if not 1 <= day <= 31: return "🤔 Не понял день зарплаты."
        set_salary_info(chat_id, day, float(amount) if amount else None)
        amt_str = f" — *{fmt(float(amount))} ₴*" if amount else ""
        return f"💵 *Зарплата: {day}-е число*{amt_str}"

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

    elif action == "goal_deposit":
        amount = float(str(route.get("amount",0)).replace(",","."))
        goal_name = route.get("goal_name","")
        if not goals: return "🎯 Целей нет. Создай: «Накопить на отпуск 60000»"
        if len(goals) == 1: gid = list(goals.keys())[0]
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
        pct = min(int(g["saved"]/g["target"]*100),100)
        bar = build_goal_bar(g["saved"], g["target"])
        msg = f"🎯 *{g['name']}*\n[{bar}] {pct}%\n{fmt(g['saved'])} / {fmt(g['target'])} ₴"
        if g["saved"] >= g["target"]:
            msg += "\n\n🎉 *Цель достигнута!* Поздравляю!"
            goals.pop(gid); close_goal(gid)
        return msg

    elif action == "convert":
        amount = float(str(route.get("amount", 0)).replace(",", "."))
        from_cur = route.get("from_currency", "USD").upper()
        to_cur = route.get("to_currency", "UAH").upper()

        t = text.lower()
        if re.search(r"\bв\s+(долар\w*|доллар\w*|бакс\w*|usd|\$)", t) and \
           not re.search(r"долар\w*|доллар\w*|бакс\w*|usd|\$", t.split("в")[0]):
            from_cur = "UAH"
            to_cur = "USD"
        elif re.search(r"\bв\s+(євро\w*|евро\w*|eur\b|€)", t) and \
             not re.search(r"євро\w*|евро\w*|eur\b|€", t.split("в")[0]):
            from_cur = "UAH"
            to_cur = "EUR"
        elif re.search(r"\bв\s+(гривн\w*|грн\b|₴|uah\b)", t):
            to_cur = "UAH"
            if from_cur == "UAH":
                if re.search(r"долар\w*|доллар\w*|бакс\w*|usd|\$", t):
                    from_cur = "USD"
                elif re.search(r"євро\w*|евро\w*|eur\b|€", t):
                    from_cur = "EUR"

        rates = await fetch_nbu_rates()

        if to_cur == "UAH":
            rate = rates.get(from_cur, 1.0)
            result = amount * rate
            from_sym = CURRENCY_SYMBOLS.get(from_cur, from_cur)
            return f"💱 *{fmt(amount)} {from_sym}* = *{fmt(result)} ₴*\n_(курс НБУ: {rate:.2f} ₴/{from_sym})_"
        elif from_cur == "UAH":
            rate = rates.get(to_cur, 1.0)
            if rate == 0: return "❌ Не удалось получить курс."
            result = amount / rate
            to_sym = CURRENCY_SYMBOLS.get(to_cur, to_cur)
            return f"💱 *{fmt(amount)} ₴* = *{result:.2f} {to_sym}*\n_(курс НБУ: {rate:.2f} ₴/{to_sym})_"
        else:
            rate_from = rates.get(from_cur, 1.0)
            rate_to = rates.get(to_cur, 1.0)
            uah = amount * rate_from
            result = uah / rate_to if rate_to else 0
            from_sym = CURRENCY_SYMBOLS.get(from_cur, from_cur)
            to_sym = CURRENCY_SYMBOLS.get(to_cur, to_cur)
            return f"💱 *{fmt(amount)} {from_sym}* = *{result:.2f} {to_sym}*\n_(через гривну по курсу НБУ)_"

    elif action == "category_add":
        name = route.get("name","").strip().capitalize()
        emoji_c = route.get("emoji","")
        if not name: return "🤔 Не понял название категории."
        save_user_category(name, emoji_c)
        em = EMOJI_MAP.get(name, get_category_emoji(name))
        return f"✅ Категория {em} *{name}* добавлена!\nТеперь пиши: «{name} 1500»"


    elif action == "expense_edit":
        old_cat = route.get("old_category","")
        new_cat = route.get("new_category","").strip().capitalize()
        amount = route.get("amount")
        desc_hint = route.get("description","").lower() if route.get("description") else ""
        if new_cat and new_cat not in get_all_categories(): save_user_category(new_cat)
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
            logger.error(f"expense_edit: {e}"); return "❌ Не удалось обновить запись."

    elif action == "debt_remind":
        name = str(route.get("name", conv_ctx.get("last_name",""))).strip().capitalize()
        minutes = float(route.get("minutes", route.get("hours",24)*60))
        did = next((k for k,d in debts.items() if name.lower() in d["name"].lower()), None) if name else None
        if not did and debts: did = list(debts.keys())[-1]
        if not did: return "🤔 Нет активных долгов."
        d = debts[did]
        for job in context.job_queue.get_jobs_by_name(f"debt_{did}"): job.schedule_removal()
        context.job_queue.run_once(send_debt_reminder, when=timedelta(minutes=minutes),
            data={"debt_id":did,"chat_id":chat_id}, name=f"debt_{did}")
        if minutes < 60: label = f"{int(minutes)} минут"
        elif minutes == 60: label = "1 час"
        elif minutes < 1440: label = f"{int(minutes//60)} часов"
        elif minutes == 1440: label = "1 день"
        else: label = f"{int(minutes//1440)} дней"
        set_ctx(chat_id, last_name=d["name"], last_action="debt_remind")
        return f"⏰ Напомню о долге *{d['name']}* через *{label}*."

    elif action == "installment_new":
        name = str(route.get("name","Рассрочка")).strip().capitalize()
        total = float(str(route.get("total",0)).replace(",","."))
        monthly = float(str(route.get("monthly",0)).replace(",","."))
        months = int(route.get("months", 1))
        if total <= 0: return "🤔 Не понял сумму рассрочки. Пример: «Рассрочка Колёса 12000 на 12 месяцев 1000»"
        if monthly <= 0 and months > 0: monthly = round(total / months, 2)
        if months <= 0 and monthly > 0: months = round(total / monthly)
        installment_counter[0] += 1
        iid = str(installment_counter[0])
        date_str = datetime.now(KYIV_TZ).strftime("%d.%m.%Y")
        installments[iid] = {"name":name,"total":total,"monthly":monthly,"paid":0.0,"payments_left":months,"date":date_str}
        save_installment_to_sheet(iid, name, total, monthly, 0, months, date_str)
        save_setting(
          f"last_installment_{chat_id}",
          json.dumps({
           "id": iid,
           "name": name,
           "total": total,
           "monthly": monthly,
           "months": months
          }, ensure_ascii=False)
)
        bar = "░" * 10
        return (f"💳 *Рассрочка: {name}*\n\n[{bar}] 0%\n"
                f"Выплачено: 0 / {fmt(total)} ₴\n"
                f"Ежемесячно: *{fmt(monthly)} ₴* × {months} платежей\n\n"
                f"_Напиши «оплатил рассрочку {name}» когда внесёшь платёж_")
       
    elif action == "installment_pay":
        name_hint = str(route.get("name","")).lower().strip()
        amount = float(str(route.get("amount",0)).replace(",","."))
        # Нечёткий поиск по имени рассрочки
        iid = None
        if name_hint:
            # Пробуем точное совпадение, потом частичное
            for k, v in installments.items():
                if name_hint in v["name"].lower() or v["name"].lower() in name_hint:
                    iid = k; break
            # Если не нашли — пробуем посимвольно (колёс → Колёса)
            if not iid:
                for k, v in installments.items():
                    inst_words = v["name"].lower().split()
                    hint_words = name_hint.split()
                    if any(hw[:4] in iw or iw[:4] in hw for hw in hint_words for iw in inst_words):
                        iid = k; break
        if not iid and installments:
            iid = list(installments.keys())[-1]  # последняя рассрочка как fallback
        if not iid: return "💳 Нет активных рассрочек. Добавь: «Рассрочка Колёса 12000 на 12 месяцев 1000»"
        inst = installments[iid]
        pay = amount if amount > 0 else inst["monthly"]
        inst["paid"] = min(inst["paid"] + pay, inst["total"])
        inst["payments_left"] = max(inst["payments_left"] - 1, 0)
        update_installment_in_sheet(iid, inst["paid"], inst["payments_left"])
        date = datetime.now(KYIV_TZ).strftime("%d.%m.%Y %H:%M")
        save_expense(date, pay, "Рассрочка", f"Рассрочка {inst['name']}", text)
        pct = min(int(inst["paid"] / inst["total"] * 100), 100) if inst["total"] > 0 else 0
        bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
        msg = (f"✅ *Платёж по рассрочке «{inst['name']}»*\n\n"
               f"💳 {fmt(pay)} ₴ внесено\n[{bar}] {pct}%\n"
               f"Выплачено: {fmt(inst['paid'])} / {fmt(inst['total'])} ₴\n"
               f"Осталось платежей: {inst['payments_left']}")
        if inst["paid"] >= inst["total"]:
            update_installment_in_sheet(iid, inst["paid"], 0, "закрыта")
            installments.pop(iid)
            msg += "\n\n🎉 *Рассрочка полностью выплачена!*"
        return msg

    elif action == "recurring_new":
        name = str(route.get("name","Платёж")).strip().capitalize()
        amount = float(str(route.get("amount",0)).replace(",","."))
        day = int(route.get("day", 1))
        category = str(route.get("category","")).strip()
        emoji_r = str(route.get("emoji","")).strip()

if not category or category.lower() == "другое":
    category, auto_emoji = infer_category_from_name(name)
    if not emoji_r:
        emoji_r = auto_emoji

category = fix_cat(category, keep_new=True)
emoji_r = emoji_r or get_category_emoji(category)

if category not in get_all_categories():
    save_user_category(category, emoji_r)
    if not category or category.lower() == "другое":
       category, auto_emoji = infer_category_from_name(name)
       if not emoji_r:
        emoji_r = auto_emoji

    category = fix_cat(category, keep_new=True)
    emoji_r = emoji_r or get_category_emoji(category)
    if amount <= 0: return "🤔 Не понял сумму. Пример: «Учёба каждый месяц 24го 3000»"
    if not 1 <= day <= 31: return "🤔 Не понял день месяца."
    _recurring_counter[0] += 1
    rid = str(_recurring_counter[0])
    recurring[rid] = {"name":name,"amount":amount,"day":day,"category":category,"emoji":emoji_r}
    save_recurring_to_sheet(rid, name, amount, day, category, emoji_r)
    save_setting(
          f"last_recurring_{chat_id}",
          json.dumps({
           "id": rid,
           "name": name,
           "amount": amount,
           "day": day,
           "category": category,
           "emoji": emoji_r
          }, ensure_ascii=False)
)
    return (f"🔄 *Регулярный платёж добавлен!*\n\n"
                f"{emoji_r} *{name}* — {fmt(amount)} ₴\n"
                f"📅 Каждое *{day}-е* число\n"
                f"_{category}_\n\n"
                f"_Буду автоматически записывать трату каждый месяц {day}-го числа_")
       
elif action == "question":
        q = route.get("text", text)
        return await ai_chat_response(chat_id, q)

return None


async def process(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    chat_id = update.effective_chat.id
    conv_ctx = get_ctx(chat_id)
    await update.message.reply_chat_action("typing")

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

    last_msgs = conv_ctx.get("last_messages",[])
    last_msgs.append(text[:100])
    set_ctx(chat_id, last_messages=last_msgs[-10:])

    responses = []
    for route in actions:
        try:
            result = await execute_action(route, update, context, chat_id, text, conv_ctx)
            if result: responses.append(result)
        except Exception as e:
            logger.error(f"execute_action {route.get('action')}: {e}")

    if not responses:
        if NON_EXPENSE_PATTERNS.search(text):
            responses.append("ℹ️ Пополнение счёта или перевод — не трата, ничего не записал.\n\n"
                           "Если хочешь записать трату, напиши например: «Кофе 85»")
        else:
            expenses = parse_expenses(text)
            if expenses:
                date = datetime.now(KYIV_TZ).strftime("%d.%m.%Y %H:%M")
                lines = ["✅ *Записано!*\n"]
                new_cats_fallback = []
                for exp in expenses:
                    amount = float(str(exp.get("amount",0)))
                    if amount <= 0: continue
                    raw_cat = (exp.get("category") or "").strip()
                    cat = fix_cat(raw_cat, keep_new=True) if raw_cat else "Другое"
                    is_new = cat not in get_all_categories() and cat != "Другое"
                    desc = exp.get("description","—")
                    emoji = exp.get("emoji","")
                    save_expense(date, amount, cat, desc, text)
                    lines.append(f"{emoji} *{desc}* — *{fmt(amount)} ₴*\n   _{get_category_emoji(cat)} {cat}_")
                    if is_new and cat not in [c for c,_ in new_cats_fallback]:
                        new_cats_fallback.append((cat, emoji or get_category_emoji(cat)))
                responses.append("\n".join(lines))
                for new_cat, new_emoji in new_cats_fallback:
                    em = new_emoji or get_category_emoji(new_cat)
                    kb = inline_kb([
                        [(f"✅ Добавить «{new_cat}»", f"savecat_{new_cat}")],
                        [("Нет, не нужно", "savecat_skip")],
                    ])
                    await update.message.reply_text(
                        f"💡 Вижу новую категорию {em} *{new_cat}*.\nДобавить её в список?",
                        f"💡 Новая категория {em} *{new_cat}* — добавить в список?",
                        parse_mode="Markdown", reply_markup=kb)
            else:
                responses.append(
                    "🤔 Не понял. Попробуй:\n"
                    "• *Трата:* «Кофе 85» или «Бензин 1200»\n"
                    "• *Долг:* «Дал Саше 500» (Саша должен тебе)\n"
                    "• *Возврат:* «Саша вернул 500»\n"
                    "• *Вопрос:* «Сколько потратил на еду?»"
                )

    combined = "\n\n".join(responses)
    if len(combined) <= 4096:
        await update.message.reply_text(combined, parse_mode="Markdown")
    else:
        for r in responses: await update.message.reply_text(r, parse_mode="Markdown")


# ── CALLBACK ─────────────────────────────────────────────────────────────────
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id

    async def send(text, **kw):
        await context.bot.send_message(chat_id=chat_id, text=text, **kw)

    # ── НОВЫЙ HANDLER: выбор категории для голой суммы ───────────────────────
    # callback_data формат: qcat_{category_index}_{amount}
    if data.startswith("qcat_"):
        parts = data.split("_", 2)  # ["qcat", index, amount]
        if len(parts) == 3:
            try:
                cat_idx = int(parts[1])
                amount = float(parts[2])
                cats = get_all_categories()
                if cat_idx >= len(cats):
                    await query.edit_message_text("❌ Категория не найдена."); return
                cat = cats[cat_idx]
                emoji = get_category_emoji(cat)
                date = datetime.now(KYIV_TZ).strftime("%d.%m.%Y %H:%M")
                # Описание = название категории в нижнем регистре
                desc = cat.lower()
                save_expense(date, amount, cat, desc, str(amount))

                # Считаем итог по категории за месяц
                month_recs = get_current_month_records()
                sk = get_sum_key(month_recs)
                cat_total = sum(
                    float(r[sk]) for r in month_recs
                    if fix_cat(r.get("Категория", "")) == cat and r.get(sk)
                )

                msg = (
                    f"✅ *Записано!*\n\n"
                    f"{emoji} *{cat}* — *{fmt(amount)} ₴*"
                )
                if cat_total > 0:
                    msg += f"\n\n_{emoji} {cat} за месяц: *{fmt(cat_total)} ₴*_"

                # Проверка бюджета
                bs = get_budget_status(chat_id)
                if bs:
                    pct = bs["percent"]
                    if pct >= 90:
                        msg += f"\n🔴 *Бюджет на {pct}%!*"
                    elif pct >= 70:
                        msg += f"\n🟡 Бюджет на {pct}%"

                await query.edit_message_text(msg, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"qcat callback error: {e}")
                await query.edit_message_text("❌ Ошибка при сохранении. Попробуй ещё раз.")
        return

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
            await send("⏳ ИИ анализирует...")
            await send(await build_past_self_ai(chat_id), parse_mode="Markdown")
        elif action == "habits":
            await send("⏳ ИИ ищет паттерны...")
            await send(await build_habits_ai(), parse_mode="Markdown")
        elif action == "rates":
            await send("⏳ Запрашиваю курс...")
            await send(await build_rates_msg(), parse_mode="Markdown")
        elif action == "advice":
            await send("⏳ ИИ готовит советы...")
            await send(await build_advice_ai(chat_id), parse_mode="Markdown")
        elif action == "reminder":
            cur = reminder_label(chat_id)
            await context.bot.send_message(chat_id=chat_id,
                text=f"⏰ *Напоминания*\n\nТекущий: *{cur}*\n\nВыбери:", parse_mode="Markdown", reply_markup=REMINDER_KB)
        elif action == "categories":
            cats = get_all_categories()
            lines = ["🏷 *Категории:*\n"] + [f"{get_category_emoji(c)} {c}" for c in cats]
            if not _user_categories:
                lines.append("\n_Добавь свою: «Добавь категорию Инвестиции»_")
            await send("\n".join(lines), parse_mode="Markdown")
        elif action == "installments":
            msg = build_installments_msg()
            if installments:
                kb = inline_kb([[("💳 Внести платёж","inst_pay_menu"),("🗑 Закрыть","inst_close_menu")]])
                await send(msg, parse_mode="Markdown", reply_markup=kb)
            else:
                await send(msg, parse_mode="Markdown")
        elif action == "recurring":
            msg = build_recurring_msg()
            if recurring:
                kb = inline_kb([[("🗑 Удалить платёж","recur_del_menu")]])
                await send(msg, parse_mode="Markdown", reply_markup=kb)
            else:
                await send(msg, parse_mode="Markdown")
        return

    if data == "goal_deposit":
        if not goals: await query.edit_message_text("🎯 Целей нет. Добавь: «Цель iPhone 25000»"); return
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
        gid = parts[2]; amount = float(parts[3])
        if gid not in goals: return
        g = goals[gid]
        g["saved"] = min(g["saved"] + amount, g["target"])
        update_goal_saved(gid, g["saved"])
        pct = min(int(g["saved"]/g["target"]*100),100)
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
            g = goals.pop(gid); close_goal(gid)
            await query.edit_message_text(f"🗑 Цель *{g['name']}* удалена.", parse_mode="Markdown"); return

    if data == "back_goals":
        await query.edit_message_text(build_goals_msg(), parse_mode="Markdown",
            reply_markup=inline_kb([[("➕ Пополнить цель","goal_deposit"),("🗑 Закрыть цель","goal_close")]])); return

    if data.startswith("reminder_") and not data.startswith("reminder_date"):
        days = int(data[9:])
        set_reminder_interval(chat_id, days)
        label = DAYS_LABELS.get(days, f"{days} дней")
        await query.edit_message_text(f"✅ Интервал установлен: *{label}*", parse_mode="Markdown"); return

    # Старый quick_ handler оставлен для совместимости
    if data.startswith("quick_"):
        parts = data.split("_", 2)
        if len(parts) == 3:
            _, cat, amt_str = parts
            amount = float(amt_str)
            date = datetime.now(KYIV_TZ).strftime("%d.%m.%Y %H:%M")
            emoji = get_category_emoji(cat)
            desc = cat.lower()
            save_expense(date, amount, cat, desc, str(amount))
            await query.edit_message_text(
                f"✅ *{fmt(amount)} ₴* записано!\n{emoji} _{cat}_",
                parse_mode="Markdown")
        return

    if data == "show_debts":
        if not debts: await query.edit_message_text("✅ Никто тебе не должен!"); return
        kb = []
        for did, d in debts.items():
            ams = d.get("amounts",[{"amount":d.get("amount",0),"currency":"UAH"}])
            amt = format_amounts(ams).replace("*","")
            kb.append([(f"👤 {d['name']} — {amt}", f"debt_menu_{did}")])
        kb.append([("← Назад","back")])
        await query.edit_message_text("Выбери долг:", reply_markup=inline_kb(kb)); return

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
            f"👤 *{d['name']}* должен тебе — {format_amounts(ams)}{note}\n📅 {d['date']}\n\nЧто сделать?",
            parse_mode="Markdown", reply_markup=kb); return

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
            parse_mode="Markdown", reply_markup=kb); return

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
        await query.edit_message_text(f"✅ Напомню о *{d['name']}* через *{label}*.", parse_mode="Markdown"); return

    if data.startswith("paid_"):
        did = data[5:]
        if did in debts:
            d = debts.pop(did); mark_paid(did)
            ams = d.get("amounts",[{"amount":d.get("amount",0),"currency":"UAH"}])
            await query.edit_message_text(
                f"✅ *{d['name']}* вернул тебе {format_amounts(ams)}\n\n🎉 Долг закрыт!",
                parse_mode="Markdown")
        else:
            await query.edit_message_text("Долг уже закрыт."); return

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
        await query.edit_message_text("💰 Частичное погашение — выбери валюту:", reply_markup=inline_kb(kb)); return

    if data.startswith("partialcur_"):
        parts = data.split("_")
        did, idx = parts[1], int(parts[2])
        if did not in debts: await query.edit_message_text("Долг уже закрыт."); return
        ams = debts[did].get("amounts",[])
        if idx >= len(ams): return
        sym = CURRENCY_SYMBOLS.get(ams[idx].get("currency","UAH"),"₴")
        context.user_data["partial_debt_id"] = did
        context.user_data["partial_amt_idx"] = idx
        await query.edit_message_text(f"💰 Сколько вернули в {sym}?\n\nНапиши сумму в чат:", parse_mode="Markdown"); return

    if data.startswith("remind_"):
        did = data[7:]
        if did in debts:
            d = debts[did]
            for job in context.job_queue.get_jobs_by_name(f"debt_{did}"): job.schedule_removal()
            context.job_queue.run_once(send_debt_reminder, when=get_reminder_interval(chat_id),
                data={"debt_id":did,"chat_id":chat_id}, name=f"debt_{did}")
            await query.edit_message_text(f"⏰ Напомню о *{d['name']}* через {reminder_label(chat_id)}.", parse_mode="Markdown"); return

    # ── savecat: сохранить новую категорию ──────────────────────────────────
    if data == "savecat_skip":
        await query.edit_message_text("👍 Ок, не добавляю."); return

    if data.startswith("savecat_"):
        cat_name = data[8:].strip()
        if cat_name and cat_name.lower() not in [c.lower() for c in get_all_categories()]:
            save_user_category(cat_name)
            em = get_category_emoji(cat_name)
            await query.edit_message_text(
                f"✅ Категория {em} *{cat_name}* добавлена!\nПиши: «{cat_name} 500»",
                parse_mode="Markdown")
        else:
            await query.edit_message_text(
                f"ℹ️ Категория *{cat_name}* уже есть.", parse_mode="Markdown")
        return

    # ── Рассрочки ────────────────────────────────────────────────────────────
    if data == "inst_pay_menu":
        if not installments: await query.edit_message_text("💳 Рассрочек нет."); return
        kb = [[(f"💳 {v['name']} ({fmt(v['monthly'])} ₴)", f"inst_pay_{k}")] for k,v in installments.items()]
        kb.append([("← Назад","inst_back")])
        await query.edit_message_text("Выбери рассрочку для платежа:", reply_markup=inline_kb(kb)); return

    if data.startswith("inst_pay_"):
        iid = data[9:]
        if iid not in installments: await query.edit_message_text("Рассрочка не найдена."); return
        inst = installments[iid]
        pay = inst["monthly"]
        inst["paid"] = min(inst["paid"] + pay, inst["total"])
        inst["payments_left"] = max(inst["payments_left"] - 1, 0)
        update_installment_in_sheet(iid, inst["paid"], inst["payments_left"])
        date = datetime.now(KYIV_TZ).strftime("%d.%m.%Y %H:%M")
        save_expense(date, pay, "Рассрочка", f"Рассрочка {inst['name']}", f"платёж рассрочка {inst['name']}")
        pct = min(int(inst["paid"] / inst["total"] * 100), 100) if inst["total"] > 0 else 0
        bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
        msg = (f"✅ *Платёж {fmt(pay)} ₴* по «{inst['name']}» записан!\n\n"
               f"[{bar}] {pct}%\nВыплачено: {fmt(inst['paid'])} / {fmt(inst['total'])} ₴\n"
               f"Осталось платежей: {inst['payments_left']}")
        if inst["paid"] >= inst["total"]:
            update_installment_in_sheet(iid, inst["paid"], 0, "закрыта")
            installments.pop(iid)
            msg += "\n\n🎉 *Рассрочка выплачена!*"
        await query.edit_message_text(msg, parse_mode="Markdown"); return

    if data == "inst_close_menu":
        if not installments: await query.edit_message_text("💳 Рассрочек нет."); return
        kb = [[(f"🗑 {v['name']}", f"inst_close_{k}")] for k,v in installments.items()]
        kb.append([("← Назад","inst_back")])
        await query.edit_message_text("Какую рассрочку закрыть?", reply_markup=inline_kb(kb)); return

    if data.startswith("inst_close_"):
        iid = data[11:]
        if iid in installments:
            inst = installments.pop(iid)
            update_installment_in_sheet(iid, inst["paid"], 0, "закрыта")
            await query.edit_message_text(f"🗑 Рассрочка *{inst['name']}* закрыта.", parse_mode="Markdown")
        return

    if data == "inst_back":
        msg = build_installments_msg()
        kb = inline_kb([[("💳 Внести платёж","inst_pay_menu"),("🗑 Закрыть","inst_close_menu")]])
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=kb); return

    # ── Регулярные платежи ───────────────────────────────────────────────────
    if data == "recur_del_menu":
        if not recurring: await query.edit_message_text("🔄 Регулярных платежей нет."); return
        kb = [[(f"🗑 {v['name']} ({fmt(v['amount'])} ₴, {v['day']}-е)", f"recur_del_{k}")] for k,v in recurring.items()]
        await query.edit_message_text("Какой платёж удалить?", reply_markup=inline_kb(kb)); return

    if data.startswith("recur_del_"):
        rid = data[10:]
        if rid in recurring:
            r = recurring.pop(rid)
            delete_recurring_from_sheet(rid)
            await query.edit_message_text(f"🗑 Платёж *{r['name']}* удалён.", parse_mode="Markdown")
        return
def infer_category_from_name(name: str) -> tuple[str, str]:
    desc = name.lower().strip()

    cat_map = [
        (["еда","продукт","кафе","ресторан","доставка","атб","сільпо","сильпо","новус"], "Еда / продукты", "🍔"),
        (["такси","бензин","заправк","uber","bolt","метро","автобус","маршрут"], "Транспорт", "🚗"),
        (["netflix","spotify","youtube","подписк","subscription","iptv","игр","steam"], "Развлечения", "🎮"),
        (["аптек","лекар","лікар","медиц","стоматолог"], "Здоровье / аптека", "💊"),
        (["снюс","сигарет","вейп","zyn","velo","никотин"], "Никотин", "🚬"),
        (["учёб","учеб","курс","школ","универ","репетитор","english"], "Образование", "📚"),
        (["зал","спорт","фитнес","gym","йога","бокс","бассейн"], "Спорт", "💪"),
        (["коммун","жкх","свет","газ","вода","интернет","мобильн","телефон"], "Коммунальные", "🏠"),
        (["одежд","обув","шопинг"], "Одежда", "👕"),
    ]

    for keywords, cat, em in cat_map:
        if any(k in desc for k in keywords):
            return cat, em

    for uc in _user_categories:
        if uc.lower() in desc or desc in uc.lower():
            return uc, get_category_emoji(uc)

    # если ничего не нашли — создаём новую категорию из названия
    new_cat = name.strip().capitalize()
    if len(new_cat) > 24:
        new_cat = new_cat.split()[0].capitalize()

    return new_cat, get_category_emoji(new_cat)
    # ── menu_installments / menu_recurring ────────────────────────────────────
    if data == "back":
        await query.edit_message_text("Выбери раздел:", reply_markup=inline_kb([
            [("💰 Финансы","menu_stats"),("📊 Аналитика","menu_week")],
            [("💸 Долги","show_debts"),("🎯 Цели","back_goals")],
        ])); return


# ── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    for cmd, handler in [
        ("start",cmd_start),("stats",cmd_stats),("week",cmd_week),
        ("month",cmd_month),("budget",cmd_budget),("salary",cmd_salary),
        ("debts",cmd_debts),("reminder",cmd_reminder),("goals",cmd_goals),
        ("rates",cmd_rates),("installments",cmd_installments),("recurring",cmd_recurring),
    ]:
        app.add_handler(CommandHandler(cmd, handler))

    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    import time
    load_settings(); time.sleep(1)
    load_user_categories(); time.sleep(1)
    load_debts(); time.sleep(1)
    load_goals(); time.sleep(1)
    load_installments(); time.sleep(1)
    load_recurring()

    if CHAT_ID and app.job_queue:
        app.job_queue.run_daily(send_weekly_insight, time=dtime(19,0), days=(4,), data={"chat_id":CHAT_ID})
        app.job_queue.run_daily(send_morning_briefing, time=dtime(9,0), data={"chat_id":CHAT_ID})
        app.job_queue.run_daily(fire_recurring_payments, time=dtime(9,5), data={"chat_id":CHAT_ID})

    logger.info("AI-агент запущен! v5.8 🤖")
    app.run_polling()

if __name__ == "__main__":
    main()
