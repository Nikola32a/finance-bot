"""
Финансовый Telegram бот v3.1 — оптимизированная версия
"""
import os, logging, tempfile, json, re
from datetime import datetime, timedelta
from collections import defaultdict
from dotenv import load_dotenv

load_dotenv()

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from groq import Groq
import gspread
from google.oauth2.service_account import Credentials

# ── ENV ─────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY     = os.getenv("GROQ_API_KEY")
GOOGLE_SHEET_ID  = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS")
CHAT_ID          = os.getenv("CHAT_ID")

groq_client = Groq(api_key=GROQ_API_KEY)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── КОНСТАНТЫ ────────────────────────────────────────────────────────────────
CATEGORIES = ["Еда / продукты", "Транспорт", "Развлечения", "Здоровье / аптека", "Никотин", "Другое"]

EMOJI_MAP = {
    "Еда / продукты": "🍔", "Транспорт": "🚗", "Развлечения": "🎮",
    "Здоровье / аптека": "💊", "Никотин": "🚬", "Другое": "📦",
}

CURRENCY_SYMBOLS = {"UAH": "₴", "USD": "$", "EUR": "€"}

MONTH_NAMES     = ["Январь","Февраль","Март","Апрель","Май","Июнь",
                   "Июль","Август","Сентябрь","Октябрь","Ноябрь","Декабрь"]
MONTH_NAMES_GEN = ["января","февраля","марта","апреля","мая","июня",
                   "июля","августа","сентября","октября","ноября","декабря"]

CATEGORY_RULES = """
ПРАВИЛА КАТЕГОРИЙ:
🍔 Еда: продукты, АТБ, Сільпо, кафе, ресторан, McDonald's, пицца, суши, Glovo, кофе, алкоголь
🚗 Транспорт: бензин, заправка, АЗС, ОККО, WOG, мойка, запчасти, СТО, такси, Uber, Bolt, метро, парковка
🎮 Развлечения: Steam, игры, донат, кейсы, AliExpress, кино, Netflix, Spotify, боулинг, ставки
💊 Здоровье: аптека, лекарства, врач, стоматолог, спортзал, фитнес, массаж, парикмахер, маникюр
🚬 Никотин: снюс, ZYN, сигареты, вейп, кальян
📦 Другое: одежда, коммунальные, интернет, телефон, подарки, ремонт
"""

DESCRIPTION_EMOJIS = {
    "кофе":"☕","кафе":"☕","ресторан":"🍽","обед":"🍽","ужин":"🍽","завтрак":"🥐",
    "пицца":"🍕","суши":"🍣","фастфуд":"🍟","бургер":"🍔","продукты":"🛒",
    "супермаркет":"🛒","атб":"🛒","магазин":"🛒","алкоголь":"🍺","пиво":"🍺","вино":"🍷",
    "бензин":"⛽","заправка":"⛽","топливо":"⛽","такси":"🚕","uber":"🚕","bolt":"🚕",
    "мойка":"🚿","запчасти":"🔧","ремонт авто":"🔧","сто":"🔧",
    "кино":"🎬","фильм":"🎬","игры":"🎮","steam":"🎮","донат":"🎮","кейсы":"🎮",
    "аптека":"💊","лекарства":"💊","таблетки":"💊","витамины":"💊",
    "врач":"👨‍⚕️","стоматолог":"🦷","клиника":"🏥","спортзал":"💪","фитнес":"💪",
    "парикмахер":"✂️","стрижка":"✂️","маникюр":"💅",
    "снюс":"🚬","вейп":"🚬","сигареты":"🚬","кальян":"🚬",
    "одежда":"👕","телефон":"📱","интернет":"🌐","коммунальные":"🏠","подарок":"🎁",
    "aliexpress":"📦","алик":"📦","netflix":"🎬","spotify":"🎵",
}

DESCRIPTION_CATEGORY_FIX = {
    "ресторан":"Еда / продукты","кафе":"Еда / продукты","кофе":"Еда / продукты",
    "обед":"Еда / продукты","ужин":"Еда / продукты","завтрак":"Еда / продукты",
    "пицца":"Еда / продукты","суши":"Еда / продукты","бургер":"Еда / продукты",
    "фастфуд":"Еда / продукты","доставка":"Еда / продукты","glovo":"Еда / продукты",
    "продукты":"Еда / продукты","магазин":"Еда / продукты",
    "алкоголь":"Еда / продукты","пиво":"Еда / продукты",
    "такси":"Транспорт","uber":"Транспорт","bolt":"Транспорт",
    "бензин":"Транспорт","заправка":"Транспорт","мойка":"Транспорт",
    "снюс":"Никотин","вейп":"Никотин","сигарет":"Никотин",
    "steam":"Развлечения","кино":"Развлечения","netflix":"Развлечения",
    "аптека":"Здоровье / аптека","врач":"Здоровье / аптека",
}

DEFAULT_MEMORY = {
    "атб":"Еда / продукты","сільпо":"Еда / продукты","новус":"Еда / продукты",
    "glovo":"Еда / продукты","bolt food":"Еда / продукты",
    "ресторан":"Еда / продукты","кафе":"Еда / продукты","кофе":"Еда / продукты",
    "обед":"Еда / продукты","ужин":"Еда / продукты","пицца":"Еда / продукты",
    "суши":"Еда / продукты","бургер":"Еда / продукты","фастфуд":"Еда / продукты",
    "окко":"Транспорт","wog":"Транспорт","uber":"Транспорт","bolt":"Транспорт",
    "аптека":"Здоровье / аптека","снюс":"Никотин","вейп":"Никотин",
    "steam":"Развлечения","алик":"Развлечения","netflix":"Развлечения","spotify":"Развлечения",
}

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
    return sp.sheet1 if name == "sheet1" else (sp.worksheet(name) if _worksheet_exists(sp, name) else sp.add_worksheet(title=name, rows=100, cols=6))

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

def fix_cat(cat: str, desc: str) -> str:
    if cat in CATEGORIES: return cat
    low = (desc + " " + cat).lower()
    for kw, c in DESCRIPTION_CATEGORY_FIX.items():
        if kw in low: return c
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

# ── АНАЛИТИКА ────────────────────────────────────────────────────────────────
DAY_NAMES = ["Понедельник","Вторник","Среда","Четверг","Пятница","Суббота","Воскресенье"]

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
    low = cat.lower()
    if any(k in low for k in ["ресторан","кафе","еда","обед","пицца"]): return "🍔"
    if any(k in low for k in ["такси","бензин","транспорт"]): return "🚗"
    if any(k in low for k in ["аптека","врач","здоровье"]): return "💊"
    if any(k in low for k in ["снюс","вейп","сигарет","никотин"]): return "🚬"
    if any(k in low for k in ["игр","кино","steam","развлеч"]): return "🎮"
    return "📦"

def add_emoji_to_desc(desc: str) -> str:
    low = desc.lower()
    for kw, em in DESCRIPTION_EMOJIS.items():
        if kw in low: return f"{em} {desc}"
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

def save_memory():
    try: save_setting("user_memory", json.dumps(memory))
    except: pass

def get_memory_cat(text: str) -> str | None:
    low = text.lower()
    for kw, cat in {**DEFAULT_MEMORY, **memory}.items():
        if kw in low: return cat
    return None

def update_memory(keyword: str, category: str):
    if keyword and len(keyword) > 2:
        memory[keyword.lower()] = category
        save_memory()

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

# ── GROQ ─────────────────────────────────────────────────────────────────────
def transcribe(path: str) -> str:
    with open(path, "rb") as f:
        return groq_client.audio.transcriptions.create(
            model="whisper-large-v3", file=f, language="ru").text

def groq_json(prompt: str, max_tokens=500) -> str:
    r = groq_client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[{"role":"user","content":prompt}],
        max_tokens=max_tokens, temperature=0.1)
    raw = r.choices[0].message.content.strip()
    return raw.replace("```json","").replace("```","").strip()

def parse_expenses(text: str) -> list:
    prompt = f"""Из текста извлеки ВСЕ траты.
Текст: "{text}"
Категории: {", ".join(CATEGORIES)}
{CATEGORY_RULES}
Верни ТОЛЬКО JSON массив без markdown:
[{{"amount":<число>,"category":"<категория>","description":"<2-5 слов>"}}]
Правила: amount только число; без суммы — не включай; всегда массив; НИКАКОГО текста кроме JSON"""
    raw = groq_json(prompt)
    if not raw:
        nums = re.findall(r'\d+(?:[.,]\d+)?', text)
        if nums: return [{"amount":float(nums[0].replace(",",".")), "category":"Другое","description":text[:30]}]
        return []
    s, e = raw.find("["), raw.rfind("]")
    if s != -1 and e != -1: raw = raw[s:e+1]
    try:
        result = json.loads(raw)
        return [result] if isinstance(result, dict) else result
    except: return []

def normalize_currency(text: str) -> str:
    patterns = [
        (r'долар[ыаіів]*|доллар[ыаов]*|бакс[ыаов]*|\$','USD'),
        (r'евро|€','EUR'),
        (r'гривн[яеьи]*|грн|₴','UAH'),
    ]
    for pat, rep in patterns:
        text = re.sub(pat, rep, text, flags=re.IGNORECASE)
    return text

def parse_debt(text: str) -> dict:
    norm = normalize_currency(text)
    prompt = f"""Извлеки долг из текста.
Текст: "{norm}" (оригинал: "{text}")
Верни ТОЛЬКО JSON:
{{"name":"<имя>","amounts":[{{"amount":<число>,"currency":"<UAH|USD|EUR>"}}],"note":"<за что>"}}
Правила: name только имя; currency из слов UAH/USD/EUR; без валюты — UAH; amounts массив"""
    raw = groq_json(prompt, 200)
    s, e = raw.find("{"), raw.rfind("}")
    if s != -1 and e != -1: raw = raw[s:e+1]
    return json.loads(raw)

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

async def send_weekly_insight(context: ContextTypes.DEFAULT_TYPE):
    cid = (context.job.data or {}).get("chat_id") or CHAT_ID
    if cid: await context.bot.send_message(chat_id=cid, text=build_insight(), parse_mode="Markdown")

# ── КЛАВИАТУРА ───────────────────────────────────────────────────────────────
MAIN_KB = ReplyKeyboardMarkup([
    [KeyboardButton("💰 Финансы"), KeyboardButton("📊 Аналитика")],
    [KeyboardButton("💸 Долги"),   KeyboardButton("⚙️ Прочее")],
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
    [("⏰ Напоминания","menu_reminder")],
])
REMINDER_KB = inline_kb([
    [("1 день","reminder_1"),("3 дня","reminder_3")],
    [("1 неделю","reminder_7"),("2 недели","reminder_14")],
    [("3 недели","reminder_21"),("1 месяц","reminder_30")],
])

# ── HANDLERS ─────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    load_debts()  # обновляем долги при старте
    await update.message.reply_text(
        "👋 Привет! Я твой финансовый аналитик.\n\n"
        "🎙 *Запись трат:* «Снюс 800» или «Мойка 350, бензин 1200»\n"
        "💸 *Долг:* «Дал в долг Саше 500 долларов»\n"
        "💰 *Бюджет:* «Бюджет 20000»\n"
        "💵 *Зарплата:* «Зарплата 25 числа 35000»",
        parse_mode="Markdown", reply_markup=MAIN_KB)

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Анализирую...")
    recs = get_current_month_records()
    if not recs:
        await update.message.reply_text("📭 В этом месяце ещё нет записей."); return
    s = analyze_records(recs)
    now = datetime.now()
    avg = s["total"]/now.day if now.day else 0
    lines = [f"📊 *Статистика за {month_name(now.month)}* ({s['count']} записей)\n"]
    lines += _cat_lines(s)
    lines += [f"\n💰 *Итого: {fmt(s['total'])} ₴*", f"📈 Прогноз: *~{fmt(avg*30)} ₴*"]
    bs = get_budget_status(update.effective_chat.id)
    if bs:
        bar = "█"*(bs["percent"]//10) + "░"*(10-bs["percent"]//10)
        lines += [f"\n💰 Бюджет: [{bar}] {bs['percent']}%", f"Осталось: *{fmt(bs['left'])} ₴*"]
    lines += _leak_lines(s)
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bs = get_budget_status(update.effective_chat.id)
    if not bs:
        await update.message.reply_text("💰 Бюджет не установлен.\n\nНапиши: «Бюджет 20000»"); return
    pct = bs["percent"]
    bar = "█"*(pct//10) + "░"*(10-pct//10)
    status = "🟢" if pct<70 else "🟡" if pct<90 else "🔴"
    await update.message.reply_text(
        f"💰 *Бюджет на месяц*\n\n{status} [{bar}] *{pct}%*\n\n"
        f"Бюджет: *{fmt(bs['budget'])} ₴*\nПотрачено: *{fmt(bs['spent'])} ₴*\nОсталось: *{fmt(bs['left'])} ₴*",
        parse_mode="Markdown")

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
    routes = {
        "📊 Статистика": cmd_stats, "📅 Отчёт за неделю": cmd_week,
        "📆 Отчёт за месяц": cmd_month, "💰 Бюджет": cmd_budget,
        "💸 Долги": cmd_debts, "💵 Зарплата": cmd_salary,
    }
    if text in routes:
        await routes[text](update, context); return
    if text == "💰 Финансы":
        await update.message.reply_text("💰 *Финансы*:", parse_mode="Markdown", reply_markup=FINANCE_KB); return
    if text == "📊 Аналитика":
        await update.message.reply_text("📊 *Аналитика*:", parse_mode="Markdown", reply_markup=ANALYTICS_KB); return
    if text == "⚙️ Прочее":
        await update.message.reply_text("⚙️ *Прочее*:", parse_mode="Markdown", reply_markup=OTHER_KB); return
    await process(update, context, text)

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
        return

    # ── ИНТЕРВАЛ НАПОМИНАНИЙ (глобальный) ──
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

    # ── ДОЛГИ — МЕНЮ КОНКРЕТНОГО ──
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

    # ── ДОЛГИ — НАСТРОЙКА НАПОМИНАНИЯ ──
    if data.startswith("debt_remind_settings_"):
        did = data[21:]
        if did not in debts: await query.edit_message_text("Долг уже закрыт."); return
        d = debts[did]
        cur = get_setting(f"debt_reminder_{did}") or reminder_label(chat_id)
        kb = inline_kb([
            [("1 день",f"dremind_{did}_1"),("3 дня",f"dremind_{did}_3")],
            [("1 неделю",f"dremind_{did}_7"),("2 недели",f"dremind_{did}_14")],
            [("3 недели",f"dremind_{did}_21"),("1 месяц",f"dremind_{did}_30")],
            [("📅 Конкретная дата",f"dremind_date_{did}")],
            [("← Назад",f"debt_menu_{did}")],
        ])
        await query.edit_message_text(
            f"⏰ *Напоминание для {d['name']}*\n\nТекущее: *{cur}*\n\nВыбери:",
            parse_mode="Markdown", reply_markup=kb)
        return

    # ── ДОЛГИ — КОНКРЕТНАЯ ДАТА НАПОМИНАНИЯ ──
    if data.startswith("dremind_date_"):
        did = data[13:]
        if did not in debts: await query.edit_message_text("Долг уже закрыт."); return
        context.user_data["debt_date_reminder_id"] = did
        await query.edit_message_text("📅 Напиши дату: `ДД.ММ.ГГГГ`\nНапример: `25.04.2025`", parse_mode="Markdown")
        return

    # ── ДОЛГИ — ИНТЕРВАЛ ДЛЯ КОНКРЕТНОГО ──
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

    # ── ДОЛГИ — ПОЛНОЕ ПОГАШЕНИЕ ──
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

    # ── ДОЛГИ — ЧАСТИЧНОЕ ПОГАШЕНИЕ ──
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

    # ── НАПОМИНАНИЕ — НАПОМНИТЬ ЕЩЁ ──
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
        await query.edit_message_text(build_debts_msg(), parse_mode="Markdown")

# ── INLINE STATS HELPERS ──────────────────────────────────────────────────────
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
    await context.bot.send_message(chat_id=chat_id,
        text=f"💰 *Бюджет на месяц*\n\n{status} [{bar}] *{pct}%*\n\n"
             f"Бюджет: *{fmt(bs['budget'])} ₴*\nПотрачено: *{fmt(bs['spent'])} ₴*\nОсталось: *{fmt(bs['left'])} ₴*",
        parse_mode="Markdown")

# ── НАПОМИНАНИЯ О ДОЛГАХ ─────────────────────────────────────────────────────
async def send_debt_reminder(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    did, cid = data["debt_id"], data["chat_id"]
    if did not in debts: return
    d = debts[did]
    ams = d.get("amounts",[{"amount":d.get("amount",0),"currency":"UAH"}])
    kb = inline_kb([[("✅ Долг вернули",f"paid_{did}"),("⏰ Напомнить ещё",f"remind_{did}")]])
    note = f"\n📝 {d['note']}" if d.get("note") else ""
    await context.bot.send_message(chat_id=cid,
        text=f"💸 *Напоминание о долге*\n\n👤 *{d['name']}* должен {format_amounts(ams)}{note}\n📅 {d['date']}\n\nДолг вернули?",
        parse_mode="Markdown", reply_markup=kb)

# ── PROCESS MESSAGE ───────────────────────────────────────────────────────────
async def process(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    lower = text.lower()
    chat_id = update.effective_chat.id

    # Конкретная дата напоминания для долга
    if "debt_date_reminder_id" in context.user_data:
        did = context.user_data.pop("debt_date_reminder_id")
        try:
            rd = datetime.strptime(text.strip(), "%d.%m.%Y")
            if rd <= datetime.now():
                await update.message.reply_text("❌ Дата уже прошла. Введи будущую дату.")
                context.user_data["debt_date_reminder_id"] = did; return
            if did in debts:
                d = debts[did]
                for job in context.job_queue.get_jobs_by_name(f"debt_{did}"): job.schedule_removal()
                context.job_queue.run_once(send_debt_reminder, when=rd-datetime.now(),
                    data={"debt_id":did,"chat_id":chat_id}, name=f"debt_{did}")
                label = rd.strftime("%d.%m.%Y")
                save_setting(f"debt_reminder_{did}", label)
                await update.message.reply_text(f"✅ Напомню о долге *{d['name']}* {label}.", parse_mode="Markdown")
        except ValueError:
            await update.message.reply_text("❌ Формат: `ДД.ММ.ГГГГ`\nНапример: `25.04.2025`", parse_mode="Markdown")
            context.user_data["debt_date_reminder_id"] = did
        return

    # Частичное погашение
    if "partial_debt_id" in context.user_data:
        did = context.user_data.get("partial_debt_id")
        idx = context.user_data.get("partial_amt_idx", 0)
        nums = re.findall(r'[\d]+(?:[.,]\d+)?', text)
        if nums and did in debts:
            partial = float(nums[0].replace(",","."))
            d = debts[did]
            ams = d.get("amounts",[])
            if idx < len(ams):
                a = ams[idx]
                cur = a.get("currency","UAH")
                sym = CURRENCY_SYMBOLS.get(cur,"₴")
                new_amt = float(a["amount"]) - partial
                lines = [f"💰 *{d['name']}* вернул:\n"]
                if new_amt <= 0:
                    ams.pop(idx)
                    lines.append(f"✅ {sym}: закрыто полностью")
                else:
                    a["amount"] = new_amt
                    lines.append(f"💸 {sym}: {fmt(partial)} → остаток *{fmt(new_amt)} {sym}*")
                if not ams:
                    debts.pop(did); mark_paid(did)
                    lines.append("\n🎉 *Долг полностью закрыт!*")
                else:
                    debts[did]["amounts"] = ams
                    update_debt_amounts(did, ams)
                    lines.append(f"\n📊 Остаток: {format_amounts(ams)}")
                await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        del context.user_data["partial_debt_id"]
        del context.user_data["partial_amt_idx"]
        return

    # Бюджет
    if "бюджет" in lower:
        nums = re.findall(r'\d+', text)
        if nums:
            amount = float(nums[0])
            save_setting(f"budget_{chat_id}", str(amount))
            await update.message.reply_text(f"✅ Бюджет: *{fmt(amount)} ₴/месяц*", parse_mode="Markdown"); return

    # Зарплата
    if any(kw in lower for kw in ["зарплата","зарплату","получаю","получу","аванс","зп"]):
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

    # Возврат долга
    if any(kw in lower for kw in ["отдал","вернул","отдала","вернула","погасил","расплатился"]):
        try:
            parsed = parse_debt(text)
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
                await update.message.reply_text("\n".join(lines), parse_mode="Markdown"); return
        except Exception as e: logger.error(f"debt_return: {e}")

    # Добавление к долгу
    if any(kw in lower for kw in ["ещё","еще","плюс","добав","доплат"]):
        try:
            parsed = parse_debt(text)
            ams = parsed.get("amounts",[])
            if ams and parsed.get("name"):
                name = parsed["name"].lower()
                did = next((k for k,d in debts.items() if name in d["name"].lower()), None)
                if did:
                    ex_ams = debts[did].get("amounts",[])
                    for na in ams:
                        cur = na.get("currency","UAH")
                        found = next((a for a in ex_ams if a.get("currency","UAH")==cur), None)
                        if found: found["amount"] = float(found["amount"]) + float(na["amount"])
                        else: ex_ams.append(na)
                    debts[did]["amounts"] = ex_ams
                    update_debt_amounts(did, ex_ams)
                    await update.message.reply_text(
                        f"➕ Добавлено к *{debts[did]['name']}*\n💰 Итого: {format_amounts(ex_ams)}",
                        parse_mode="Markdown"); return
        except Exception as e: logger.error(f"debt_add: {e}")

    # Новый долг
    if any(kw in lower for kw in ["дал в долг","одолжил","дала в долг","долг"]):
        try:
            parsed = parse_debt(text)
            ams = parsed.get("amounts",[])
            if not ams and parsed.get("amount"):
                ams = [{"amount":float(parsed["amount"]),"currency":"UAH"}]
            if ams and parsed.get("name"):
                debt_counter[0] += 1
                did = str(debt_counter[0])
                date_str = datetime.now().strftime("%d.%m.%Y")
                debts[did] = {"name":parsed["name"],"amounts":ams,"date":date_str,"note":parsed.get("note","")}
                save_debt(did, parsed["name"], ams, date_str, parsed.get("note",""))
                interval = get_reminder_interval(chat_id)
                context.job_queue.run_once(send_debt_reminder, when=interval,
                    data={"debt_id":did,"chat_id":chat_id}, name=f"debt_{did}")
                note = f"\n📝 {parsed['note']}" if parsed.get("note") else ""
                await update.message.reply_text(
                    f"💸 *Долг записан!*\n\n👤 *{parsed['name']}*\n💰 {format_amounts(ams)}{note}\n\n⏰ Напомню через {reminder_label(chat_id)}.",
                    parse_mode="Markdown"); return
        except Exception as e: logger.error(f"debt_new: {e}")

    # Быстрый режим (просто число)
    stripped = text.strip().replace(",",".").replace(" ","")
    if re.fullmatch(r'\d+(\.\d+)?', stripped):
        amount = float(stripped)
        mem_cat = get_memory_cat(text)
        if mem_cat:
            date = datetime.now().strftime("%d.%m.%Y %H:%M")
            save_expense(date, amount, mem_cat, "быстрая запись", text)
            await update.message.reply_text(
                f"⚡ *{fmt(amount)} ₴* → {get_category_emoji(mem_cat)} {mem_cat}\n_Записано!_",
                parse_mode="Markdown")
        else:
            kb = []
            row = []
            for cat in CATEGORIES:
                row.append((f"{get_category_emoji(cat)} {cat}", f"quick_{cat}_{amount}"))
                if len(row) == 2: kb.append(row); row = []
            if row: kb.append(row)
            await update.message.reply_text(f"⚡ *{fmt(amount)} ₴* — категория?",
                parse_mode="Markdown", reply_markup=inline_kb(kb))
        return

    # Обычные расходы
    try:
        expenses = parse_expenses(text)
        if not expenses:
            await update.message.reply_text("🤔 Не нашёл сумму.\nПопробуй: «Снюс 800» или «Продукты 500, такси 200»"); return
        date = datetime.now().strftime("%d.%m.%Y %H:%M")
        month_recs = get_current_month_records()
        lines = ["✅ *Записано!*\n"]
        for exp in expenses:
            amount = float(exp["amount"])
            cat = validate_category(exp.get("category","Другое"), exp.get("description",""))
            desc = exp.get("description","—")
            save_expense(date, amount, cat, desc, text)
            lines.append(f"{add_emoji_to_desc(desc)} — *{fmt(amount)} ₴* ({cat})")
            update_memory(desc, cat)
        if len(expenses) > 1:
            lines.append(f"\n💰 *Итого: {fmt(sum(float(e['amount']) for e in expenses))} ₴*")
        # Умный комментарий
        sk = get_sum_key(month_recs)
        cat0 = validate_category(expenses[0].get("category","Другое"), expenses[0].get("description",""))
        cat_total = sum(float(r[sk]) for r in month_recs if fix_cat(r.get("Категория",""),r.get("Описание",""))==cat0 and r.get(sk))
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

# ── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    for cmd, handler in [
        ("start",cmd_start), ("stats",cmd_stats), ("week",cmd_week),
        ("month",cmd_month), ("budget",cmd_budget), ("salary",cmd_salary),
        ("debts",cmd_debts), ("reminder",cmd_reminder),
    ]:
        app.add_handler(CommandHandler(cmd, handler))

    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    load_settings()
    load_memory()
    load_debts()

    if CHAT_ID and app.job_queue:
        app.job_queue.run_daily(
            send_weekly_insight,
            time=datetime.strptime("19:00","%H:%M").time(),
            days=(4,), data={"chat_id":CHAT_ID})

    logger.info("Бот запущен! v3.1")
    app.run_polling()

if __name__ == "__main__":
    main()
