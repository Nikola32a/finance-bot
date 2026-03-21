import os
import logging
import tempfile
import json
import re
from datetime import datetime, timedelta
from collections import defaultdict
from dotenv import load_dotenv

load_dotenv()

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (Application, CommandHandler, MessageHandler,
                           filters, ContextTypes, CallbackQueryHandler)
from groq import Groq
import gspread
from google.oauth2.service_account import Credentials

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS")

groq_client = Groq(api_key=GROQ_API_KEY)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CATEGORIES = ["Еда / продукты", "Транспорт", "Развлечения", "Здоровье / аптека", "Никотин", "Другое"]

EMOJI_MAP = {
    "Еда / продукты": "🍔", "Транспорт": "🚗", "Развлечения": "🎮",
    "Здоровье / аптека": "💊", "Никотин": "🚬", "Другое": "📦"
}

CATEGORY_RULES = """
ПРАВИЛА КАТЕГОРИЙ:
🍔 Еда / продукты: продукты, супермаркет, АТБ, Сільпо, Новус, кафе, ресторан, фастфуд, McDonald's, KFC, пицца, суши, Glovo, Bolt Food, кофе, алкоголь, пиво, вино
🚗 Транспорт: бензин, дизель, топливо, заправка, АЗС, ОККО, WOG, мойка машины, автомойка, запчасти, масло, шины, аккумулятор, ремонт авто, СТО, детейлинг, такси, Uber, Bolt, автобус, маршрутка, метро, поезд, парковка, страховка авто
🎮 Развлечения: Steam, игры, донат, кейсы, скины, CS, Dota, AliExpress, Алик, тема, кино, Netflix, Spotify, YouTube Premium, боулинг, квест, ставки, казино, покер, подписки
💊 Здоровье / аптека: аптека, лекарства, таблетки, витамины, врач, стоматолог, клиника, анализы, спортзал, фитнес, массаж, косметолог, парикмахер, стрижка, маникюр
🚬 Никотин: снюс, никотиновые пакетики, ZYN, сигареты, вейп, под, жижа, кальян
📦 Другое: одежда, коммунальные, интернет, телефон, подарки, ремонт дома, всё остальное
"""

CURRENCY_SYMBOLS = {"UAH": "₴", "USD": "$", "EUR": "€"}
MONTH_NAMES = ["Январь","Февраль","Март","Апрель","Май","Июнь",
               "Июль","Август","Сентябрь","Октябрь","Ноябрь","Декабрь"]
MONTH_NAMES_GEN = ["января","февраля","марта","апреля","мая","июня",
                   "июля","августа","сентября","октября","ноября","декабря"]

def month_name(month_num: int, genitive=False) -> str:
    names = MONTH_NAMES_GEN if genitive else MONTH_NAMES
    return names[month_num - 1]

# ============================================================
# КЭШИРОВАНИЕ GOOGLE SHEETS (⚡ ускоряет бота в 3-5 раз)
# ============================================================
_gs_client = None         # единый клиент
_spreadsheet = None       # единый spreadsheet объект
_records_cache = {}       # {sheet_name: (timestamp, records)}
CACHE_TTL = 60            # секунды жизни кэша для чтения

def _get_gs_client():
    global _gs_client
    if _gs_client is None:
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        if GOOGLE_CREDENTIALS:
            creds = Credentials.from_service_account_info(
                json.loads(GOOGLE_CREDENTIALS), scopes=scopes)
        else:
            creds = Credentials.from_service_account_file("credentials.json", scopes=scopes)
        _gs_client = gspread.authorize(creds)
    return _gs_client

def _get_spreadsheet():
    global _spreadsheet
    if _spreadsheet is None:
        _spreadsheet = _get_gs_client().open_by_key(GOOGLE_SHEET_ID)
    return _spreadsheet

def _get_worksheet(name="sheet1"):
    sp = _get_spreadsheet()
    if name == "sheet1":
        return sp.sheet1
    try:
        return sp.worksheet(name)
    except:
        sheet = sp.add_worksheet(title=name, rows=100, cols=6)
        return sheet

def _invalidate_cache(name="sheet1"):
    _records_cache.pop(name, None)

def _cached_records(name="sheet1"):
    now = datetime.now().timestamp()
    if name in _records_cache:
        ts, data = _records_cache[name]
        if now - ts < CACHE_TTL:
            return data
    try:
        sheet = _get_worksheet(name)
        data = sheet.get_all_records()
        _records_cache[name] = (now, data)
        return data
    except Exception as e:
        logger.error(f"Cache read error ({name}): {e}")
        return []

# ============================================================
# НАСТРОЙКИ (сохраняются в листе "Настройки" — не теряются!)
# ============================================================
_settings_cache = {}

def _get_settings_sheet():
    sheet = _get_worksheet("Настройки")
    if not sheet.get_all_values():
        sheet.insert_row(["Ключ", "Значение"], 1)
    return sheet

def load_settings():
    global _settings_cache
    try:
        sheet = _get_settings_sheet()
        records = sheet.get_all_records()
        _settings_cache = {r["Ключ"]: r["Значение"] for r in records if r.get("Ключ")}
    except Exception as e:
        logger.error(f"Load settings error: {e}")

def save_setting(key: str, value: str):
    _settings_cache[key] = value
    try:
        sheet = _get_settings_sheet()
        records = sheet.get_all_records()
        for i, r in enumerate(records, start=2):
            if r.get("Ключ") == key:
                sheet.update_cell(i, 2, value)
                return
        sheet.append_row([key, value])
    except Exception as e:
        logger.error(f"Save setting error: {e}")

def get_setting(key: str, default=None):
    return _settings_cache.get(key, default)

# ============================================================
# ДОЛГИ
# ============================================================
debts = {}
debt_counter = [0]

def get_debts_sheet():
    sheet = _get_worksheet("Долги")
    if not sheet.get_all_values():
        sheet.insert_row(["ID", "Кому", "Сумма", "Дата", "Статус", "Примечание"], 1)
    return sheet

def load_debts_from_sheet():
    try:
        sheet = get_debts_sheet()
        records = sheet.get_all_records()
        for r in records:
            if r.get("Статус") == "активен":
                debt_id = str(r["ID"])
                raw_amount = r["Сумма"]
                # Новый формат: "550 $ + 300 ₴" или старый: число
                try:
                    # Пробуем старый формат (просто число)
                    amounts = [{"amount": float(raw_amount), "currency": "UAH"}]
                except (ValueError, TypeError):
                    # Новый формат — парсим строку "550 $ + 300 ₴"
                    amounts = []
                    sym_map = {"₴": "UAH", "$": "USD", "€": "EUR"}
                    for part in str(raw_amount).split("+"):
                        part = part.strip()
                        for sym, cur in sym_map.items():
                            if sym in part:
                                num = re.findall(r'[\d,.]+', part)
                                if num:
                                    amounts.append({"amount": float(num[0].replace(",", "")), "currency": cur})
                                break
                    if not amounts:
                        amounts = [{"amount": 0, "currency": "UAH"}]

                debts[debt_id] = {
                    "name": r["Кому"],
                    "amounts": amounts,
                    "date": r["Дата"],
                    "note": r.get("Примечание", ""),
                }
                try:
                    debt_counter[0] = max(debt_counter[0], int(r["ID"]))
                except:
                    pass
    except Exception as e:
        logger.error(f"Load debts error: {e}")

def save_debt_to_sheet(debt_id, name, amount, date, note=""):
    try:
        sheet = get_debts_sheet()
        sheet.append_row([debt_id, name, amount, date, "активен", note])
    except Exception as e:
        logger.error(f"Save debt error: {e}")

def mark_debt_paid_in_sheet(debt_id):
    try:
        sheet = get_debts_sheet()
        records = sheet.get_all_records()
        for i, r in enumerate(records, start=2):
            if str(r.get("ID")) == str(debt_id):
                sheet.update_cell(i, 5, "погашен")
                break
    except Exception as e:
        logger.error(f"Mark debt paid error: {e}")

def update_debt_amounts_in_sheet(debt_id, new_amounts_str):
    try:
        sheet = get_debts_sheet()
        records = sheet.get_all_records()
        for i, r in enumerate(records, start=2):
            if str(r.get("ID")) == str(debt_id):
                sheet.update_cell(i, 3, new_amounts_str)  # колонка Сумма
                break
    except Exception as e:
        logger.error(f"Update debt error: {e}")

# ============================================================
# GOOGLE SHEETS — РАСХОДЫ
# ============================================================
def get_sheet():
    sheet = _get_worksheet("sheet1")
    if not sheet.get_all_values():
        sheet.insert_row(["Дата", "Сумма (₴)", "Категория", "Описание", "Исходный текст"], 1)
    return sheet

def get_all_records():
    return _cached_records("sheet1")

def save_expense(date, amount, category, description, raw_text):
    get_sheet().append_row([date, amount, category, description, raw_text])
    _invalidate_cache("sheet1")  # сбрасываем кэш после записи

def get_sum_key(records):
    if not records:
        return "Сумма (₴)"
    for key in records[0].keys():
        if "умм" in key or "ума" in key.lower() or "сум" in key.lower():
            return key
    return list(records[0].keys())[1]

# ============================================================
# АНАЛИТИКА
# ============================================================
def get_current_month_records():
    records = get_all_records()
    now = datetime.now()
    result = []
    for r in records:
        try:
            date = datetime.strptime(r.get("Дата", "")[:10], "%d.%m.%Y")
            if date.month == now.month and date.year == now.year:
                result.append(r)
        except:
            continue
    return result

def get_week_records():
    records = get_all_records()
    week_ago = datetime.now() - timedelta(days=7)
    result = []
    for r in records:
        try:
            date = datetime.strptime(r.get("Дата", "")[:10], "%d.%m.%Y")
            if date >= week_ago:
                result.append(r)
        except:
            continue
    return result

def analyze_records(records):
    if not records:
        return None
    sum_key = get_sum_key(records)
    total = sum(float(r[sum_key]) for r in records if r[sum_key])
    by_category = defaultdict(float)
    by_day = defaultdict(float)
    by_description = defaultdict(lambda: {"count": 0, "total": 0.0})

    for r in records:
        amt = float(r[sum_key]) if r[sum_key] else 0
        cat = r.get("Категория", "Другое")
        desc = r.get("Описание", "").lower()
        date_str = r.get("Дата", "")
        by_category[cat] += amt
        if date_str:
            try:
                date = datetime.strptime(date_str[:10], "%d.%m.%Y")
                day_names = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
                by_day[day_names[date.weekday()]] += amt
            except:
                pass
        if desc:
            by_description[desc]["count"] += 1
            by_description[desc]["total"] += amt

    leaks = {k: v for k, v in by_description.items() if v["count"] >= 3}

    return {
        "total": total,
        "count": len(records),
        "by_category": dict(by_category),
        "by_day": dict(by_day),
        "leaks": leaks
    }

def get_smart_comment(category, description, amount, month_records):
    if not month_records:
        return ""
    sum_key = get_sum_key(month_records)
    cat_total = sum(
        float(r[sum_key]) for r in month_records
        if r.get("Категория", "") == category and r[sum_key]
    )
    desc_count = sum(
        1 for r in month_records
        if description.lower() in r.get("Описание", "").lower()
    )
    comments = []
    if cat_total > 0:
        comments.append(f"_{EMOJI_MAP.get(category, '📦')} {category} в этом месяце: *{cat_total:,.0f} ₴*_")
    if desc_count >= 4:
        comments.append(f"_Это уже {desc_count}-я покупка «{description}» в этом месяце 😅_")
    if category == "Никотин" and cat_total > 1500:
        comments.append("_💡 На никотин уходит прилично этот месяц_")
    elif category == "Развлечения" and cat_total > 3000:
        comments.append("_🎮 Развлечения бьют по бюджету этот месяц_")
    return "\n".join(comments)

# ============================================================
# КОНТЕКСТНАЯ ПАМЯТЬ (АТБ = продукты автоматически)
# ============================================================
memory = {}  # {keyword: category} — загружается из Настроек

DEFAULT_MEMORY = {
    "атб": "Еда / продукты", "сільпо": "Еда / продукты",
    "новус": "Еда / продукты", "метро": "Еда / продукты",
    "glovo": "Еда / продукты", "bolt food": "Еда / продукты",
    "окко": "Транспорт", "wog": "Транспорт",
    "uber": "Транспорт", "bolt": "Транспорт",
    "аптека": "Здоровье / аптека",
    "зyn": "Никотин", "снюс": "Никотин", "вейп": "Никотин",
    "steam": "Развлечения", "алик": "Развлечения",
    "netflix": "Развлечения", "spotify": "Развлечения",
}

def load_memory_from_settings():
    val = get_setting("user_memory")
    if val:
        try:
            memory.update(json.loads(val))
        except:
            pass

def save_memory_to_settings():
    try:
        save_setting("user_memory", json.dumps(memory))
    except Exception as e:
        logger.error(f"Save memory error: {e}")

def get_memory_category(text: str) -> str | None:
    lower = text.lower()
    for keyword, category in {**DEFAULT_MEMORY, **memory}.items():
        if keyword in lower:
            return category
    return None

def update_memory(keyword: str, category: str):
    if keyword and len(keyword) > 2:
        memory[keyword.lower()] = category
        save_memory_to_settings()

# ============================================================
# БЫСТРЫЙ РЕЖИМ (просто число → бот уточняет категорию)
# ============================================================
pending_quick = {}  # {chat_id: amount}

async def handle_quick_mode(update: Update, context: ContextTypes.DEFAULT_TYPE, amount: float):
    """Пользователь написал просто число — спрашиваем категорию"""
    chat_id = update.effective_chat.id
    pending_quick[str(chat_id)] = amount

    keyboard = []
    row = []
    for cat in CATEGORIES:
        emoji = EMOJI_MAP.get(cat, "📦")
        row.append(InlineKeyboardButton(f"{emoji} {cat}", callback_data=f"quick_{cat}_{amount}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    await update.message.reply_text(
        f"⚡ *{amount:,.0f} ₴* — какая категория?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ============================================================
# УМНЫЕ ПРЕДУПРЕЖДЕНИЯ
# ============================================================
def get_smart_warnings(chat_id) -> list:
    """Анализирует темп трат и выдаёт предупреждения"""
    try:
        warnings = []
        records = get_current_month_records()
        if not records:
            return warnings

        stats = analyze_records(records)
        now = datetime.now()
        days_passed = now.day
        days_in_month = 30
        if days_passed == 0:
            return warnings
        daily_avg = stats["total"] / days_passed
        projected = daily_avg * days_in_month

        prev_month = (now.replace(day=1) - timedelta(days=1))
        all_records = get_all_records()
        prev_records = [r for r in all_records if _record_in_month(r, prev_month.month, prev_month.year)]
        if prev_records:
            prev_stats = analyze_records(prev_records)
            if prev_stats and prev_stats["total"] > 0:
                diff_pct = int(((projected - prev_stats["total"]) / prev_stats["total"]) * 100)
                if diff_pct > 20:
                    warnings.append(f"🚨 Идёшь к перерасходу *+{diff_pct}%* vs прошлый месяц")
                elif diff_pct > 10:
                    warnings.append(f"⚠️ Темп трат выше прошлого месяца на *{diff_pct}%*")

        budget_status = get_budget_status(chat_id)
        if budget_status:
            pct = budget_status["percent"]
            days_percent = int((days_passed / days_in_month) * 100)
            if pct > days_percent + 15:
                warnings.append(f"🚨 Потрачено *{pct}%* бюджета за *{days_percent}%* месяца!")

        return warnings
    except Exception as e:
        logger.error(f"Smart warnings error: {e}")
        return []

# ============================================================
# СРАВНЕНИЕ МЕСЯЦЕВ
# ============================================================
def build_months_comparison() -> str:
    all_records = get_all_records()
    now = datetime.now()

    months_data = {}
    for r in all_records:
        try:
            date = datetime.strptime(r.get("Дата", "")[:10], "%d.%m.%Y")
            key = (date.year, date.month)
            if key not in months_data:
                months_data[key] = []
            months_data[key].append(r)
        except:
            continue

    if len(months_data) < 2:
        return "📭 Нужно минимум 2 месяца данных для сравнения."

    sorted_months = sorted(months_data.keys(), reverse=True)[:3]  # последние 3 месяца
    month_names = ["Январь","Февраль","Март","Апрель","Май","Июнь",
                   "Июль","Август","Сентябрь","Октябрь","Ноябрь","Декабрь"]

    lines = ["📊 *Сравнение месяцев*\n"]

    prev_total = None
    for year, month in sorted_months:
        records = months_data[(year, month)]
        stats = analyze_records(records)
        name = f"{month_names[month-1]} {year}"

        if prev_total is not None:
            diff_pct = int(((stats["total"] - prev_total) / prev_total) * 100)
            arrow = "📈" if diff_pct > 0 else "📉"
            sign = "+" if diff_pct > 0 else ""
            lines.append(f"*{name}*: {stats['total']:,.0f} ₴ {arrow} {sign}{diff_pct}%")
        else:
            lines.append(f"*{name}*: {stats['total']:,.0f} ₴")

        # Топ категория
        if stats["by_category"]:
            top_cat = max(stats["by_category"], key=stats["by_category"].get)
            top_amt = stats["by_category"][top_cat]
            lines.append(f"  └ Топ: {EMOJI_MAP.get(top_cat,'📦')} {top_cat} — {top_amt:,.0f} ₴")

        prev_total = stats["total"]

    # Вывод по разнице текущего и прошлого
    if len(sorted_months) >= 2:
        cur = analyze_records(months_data[sorted_months[0]])
        prv = analyze_records(months_data[sorted_months[1]])
        if cur and prv and prv["total"] > 0:
            diff = cur["total"] - prv["total"]
            diff_pct = int((diff / prv["total"]) * 100)
            sign = "+" if diff > 0 else ""
            verdict = "Тратишь больше 📈" if diff > 0 else "Тратишь меньше 📉"
            lines.append(f"\n{verdict}: {sign}{diff:,.0f} ₴ ({sign}{diff_pct}%)")

    return "\n".join(lines)

# ============================================================
# ДЕНЬ ЗАРПЛАТЫ (персистентно через Настройки)
# ============================================================
def get_salary_info(chat_id):
    val = get_setting(f"salary_{chat_id}")
    if not val:
        return None
    try:
        return json.loads(val)
    except:
        return None

def set_salary_info(chat_id, day, amount=None):
    save_setting(f"salary_{chat_id}", json.dumps({"day": day, "amount": amount}))

def build_salary_status(chat_id) -> str:
    info = get_salary_info(chat_id)
    if not info:
        return None
    now = datetime.now()
    salary_day = info["day"]
    amount = info.get("amount")
    if now.day < salary_day:
        days_left = salary_day - now.day
        next_salary = now.replace(day=salary_day)
    else:
        next_month = (now.replace(day=1) + timedelta(days=32)).replace(day=1)
        try:
            next_salary = next_month.replace(day=salary_day)
        except:
            next_salary = next_month.replace(day=28)
        days_left = (next_salary - now).days
    records = get_current_month_records()
    sum_key = get_sum_key(records) if records else "Сумма (₴)"
    spent = sum(float(r[sum_key]) for r in records if r[sum_key]) if records else 0
    lines = [f"💵 *День зарплаты — {salary_day}-е число*\n"]
    if days_left == 0:
        lines.append("🎉 *Сегодня зарплата!*")
    elif days_left == 1:
        lines.append("⏰ *Завтра зарплата!*")
    else:
        lines.append(f"📅 До зарплаты: *{days_left} дней*")
        lines.append(f"   ({next_salary.strftime('%d')} {month_name(next_salary.month, genitive=True)})")
    lines.append(f"\n💸 Потрачено в этом месяце: *{spent:,.0f} ₴*")
    if amount:
        left_after = amount - spent
        per_day = left_after / days_left if days_left > 0 else 0
        lines.append(f"💰 Зарплата: *{amount:,.0f} ₴*")
        lines.append(f"{'🟢' if left_after > 0 else '🔴'} Осталось: *{left_after:,.0f} ₴*")
        if per_day > 0:
            lines.append(f"📊 Можно тратить: *{per_day:,.0f} ₴/день*")
    return "\n".join(lines)

# ============================================================
# БЮДЖЕТ (персистентно через Настройки)
# ============================================================
def get_budget_status(chat_id):
    val = get_setting(f"budget_{chat_id}")
    if not val:
        return None
    try:
        budget = float(val)
    except:
        return None
    records = get_current_month_records()
    sum_key = get_sum_key(records) if records else "Сумма (₴)"
    spent = sum(float(r[sum_key]) for r in records if r[sum_key]) if records else 0
    left = budget - spent
    percent = min(int((spent / budget) * 100), 100)
    return {"budget": budget, "spent": spent, "left": left, "percent": percent}

# ============================================================
# ОТЧЁТЫ
# ============================================================
def build_weekly_report():
    records = get_week_records()
    if not records:
        return "📭 За прошлую неделю трат нет."
    stats = analyze_records(records)
    lines = ["📅 *Отчёт за неделю*\n"]
    lines.append(f"💰 Потрачено: *{stats['total']:,.0f} ₴* ({stats['count']} записей)\n")
    lines.append("*По категориям:*")
    for cat, amt in sorted(stats["by_category"].items(), key=lambda x: -x[1]):
        pct = int((amt / stats["total"]) * 100)
        emoji = EMOJI_MAP.get(cat, "📦")
        lines.append(f"{emoji} {cat}: *{amt:,.0f} ₴* ({pct}%)")
    if stats["by_day"]:
        top_day = max(stats["by_day"], key=stats["by_day"].get)
        lines.append(f"\n📈 Самый дорогой день: *{top_day}* — {stats['by_day'][top_day]:,.0f} ₴")
    if stats["leaks"]:
        lines.append("\n💸 *Частые траты:*")
        for desc, data in list(stats["leaks"].items())[:3]:
            lines.append(f"• {desc}: {data['count']}× = *{data['total']:,.0f} ₴*")
    return "\n".join(lines)

def build_monthly_report():
    records = get_current_month_records()
    if not records:
        return "📭 В этом месяце трат нет."
    stats = analyze_records(records)
    now = datetime.now()
    days_passed = now.day
    daily_avg = stats["total"] / days_passed if days_passed > 0 else 0
    projected = daily_avg * 30
    lines = [f"📆 *Отчёт за {month_name(now.month)} {now.year}*\n"]
    lines.append(f"💰 Потрачено: *{stats['total']:,.0f} ₴* за {days_passed} дней")
    lines.append(f"📊 В среднем: *{daily_avg:,.0f} ₴/день*")
    lines.append(f"📈 Прогноз на месяц: *~{projected:,.0f} ₴*\n")
    lines.append("*Топ категории:*")
    for cat, amt in sorted(stats["by_category"].items(), key=lambda x: -x[1])[:5]:
        pct = int((amt / stats["total"]) * 100)
        emoji = EMOJI_MAP.get(cat, "📦")
        lines.append(f"{emoji} {cat}: *{amt:,.0f} ₴* ({pct}%)")
    if stats["leaks"]:
        lines.append("\n💸 *Частые траты:*")
        for desc, data in list(stats["leaks"].items())[:3]:
            lines.append(f"• {desc}: {data['count']}× = *{data['total']:,.0f} ₴*")
    return "\n".join(lines)

# ============================================================
# ДОЛГИ
# ============================================================
def normalize_currency_text(text: str) -> str:
    """Заменяет текстовые варианты валют на стандартные обозначения"""
    replacements = [
        # Доллары
        (r'долар[ыаіів]*', 'USD'),
        (r'доллар[ыаов]*', 'USD'),
        (r'бакс[ыаов]*', 'USD'),
        (r'\$', 'USD'),
        # Евро
        (r'евро', 'EUR'),
        (r'€', 'EUR'),
        # Гривны
        (r'гривн[яеьи]*', 'UAH'),
        (r'грн', 'UAH'),
        (r'₴', 'UAH'),
    ]
    result = text
    for pattern, replacement in replacements:
        result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)
    return result

def parse_debt(text):
    """Парсит текст с поддержкой нескольких валют: 'Артём 550 долларов и 300 гривен'"""
    # Нормализуем валюты перед отправкой в AI
    normalized = normalize_currency_text(text)

    prompt = f"""Из текста извлеки информацию о долге. Может быть несколько сумм в разных валютах.

Текст: "{normalized}"
Оригинал: "{text}"

Верни ТОЛЬКО JSON без markdown-тиков:
{{
  "name": "<имя человека>",
  "amounts": [
    {{"amount": <число>, "currency": "<UAH или USD или EUR>"}}
  ],
  "note": "<за что, если упомянуто, иначе пустая строка>"
}}

Правила:
- name: только имя
- currency: UAH для гривен/грн/UAH/₴, USD для долларов/USD/$, EUR для евро/EUR/€
- Если валюта не указана — UAH
- amounts: массив всех сумм (может быть несколько)
- note: краткое описание"""

    response = groq_client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=200,
        temperature=0.1
    )
    raw = response.choices[0].message.content.strip().replace("```json", "").replace("```", "").strip()
    bracket_start = raw.find("{")
    bracket_end = raw.rfind("}")
    if bracket_start != -1 and bracket_end != -1:
        raw = raw[bracket_start:bracket_end+1]
    return json.loads(raw)

CURRENCY_NAMES = {"UAH": "гривен", "USD": "долларов", "EUR": "евро"}

def format_debt_amounts(amounts: list) -> str:
    """Форматирует список сумм в строку: 550 $ + 300 ₴"""
    parts = []
    for a in amounts:
        sym = CURRENCY_SYMBOLS.get(a.get("currency", "UAH"), "₴")
        parts.append(f"*{a['amount']:,.0f} {sym}*")
    return " + ".join(parts)

def build_debts_message():
    active = {k: v for k, v in debts.items()}
    if not active:
        return "✅ Активных долгов нет!"
    lines = ["💸 *Активные долги:*\n"]
    uah_total = 0.0
    for debt_id, d in active.items():
        days_ago = (datetime.now() - datetime.strptime(d["date"], "%d.%m.%Y")).days
        note_str = f" — _{d['note']}_" if d.get("note") else ""
        amounts = d.get("amounts", [{"amount": d.get("amount", 0), "currency": "UAH"}])
        amt_str = format_debt_amounts(amounts)
        lines.append(f"👤 *{d['name']}* — {amt_str}{note_str}")
        lines.append(f"   📅 {d['date']} ({days_ago} дн. назад)")
        # Считаем только гривны для итога
        for a in amounts:
            if a.get("currency", "UAH") == "UAH":
                uah_total += float(a["amount"])
    if uah_total > 0:
        lines.append(f"\n💰 *Итого в гривнах: {uah_total:,.0f} ₴*")
    return "\n".join(lines)

async def send_debt_reminder(context: ContextTypes.DEFAULT_TYPE):
    """Напоминание о долге каждые 2 недели"""
    job_data = context.job.data
    debt_id = job_data["debt_id"]
    chat_id = job_data["chat_id"]

    if debt_id not in debts:
        return  # долг уже погашен

    d = debts[debt_id]
    amounts = d.get("amounts", [{"amount": d.get("amount", 0), "currency": "UAH"}])
    amt_str = format_debt_amounts(amounts)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Долг вернули", callback_data=f"paid_{debt_id}"),
        InlineKeyboardButton("⏰ Напомнить ещё", callback_data=f"remind_{debt_id}")
    ]])
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"💸 *Напоминание о долге*\n\n"
             f"👤 *{d['name']}* должен тебе {amt_str}\n"
             f"📅 Дата: {d['date']}\n"
             f"{'📝 ' + d['note'] if d.get('note') else ''}\n\n"
             f"Долг вернули?",
        parse_mode="Markdown",
        reply_markup=keyboard
    )

# ============================================================
# GROQ ПАРСИНГ РАСХОДОВ
# ============================================================
def transcribe_audio(file_path):
    with open(file_path, "rb") as f:
        transcript = groq_client.audio.transcriptions.create(
            model="whisper-large-v3", file=f, language="ru"
        )
    return transcript.text

def parse_expenses(text):
    prompt = f"""Из текста извлеки ВСЕ траты — их может быть несколько.

Текст: "{text}"

Категории: {", ".join(CATEGORIES)}
{CATEGORY_RULES}

Верни ТОЛЬКО JSON массив без markdown-тиков:
[{{"amount": <число>, "category": "<категория>", "description": "<2-5 слов>"}}]

Правила:
- amount: только число, никогда не null
- Если сумма не указана — не включай трату
- Даже одна трата — возвращай массив
- НИКАКОГО текста кроме JSON"""

    response = groq_client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=500,
        temperature=0.1
    )
    raw = response.choices[0].message.content.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()

    # Защита от пустого ответа
    if not raw:
        logger.warning(f"Empty response from Groq for text: {text}")
        # Пробуем извлечь сумму вручную
        numbers = re.findall(r'\d+(?:[.,]\d+)?', text)
        if numbers:
            amount = float(numbers[0].replace(",", "."))
            return [{"amount": amount, "category": "Другое", "description": text[:30]}]
        return []

    # Ищем JSON массив в ответе если есть лишний текст
    bracket_start = raw.find("[")
    bracket_end = raw.rfind("]")
    if bracket_start != -1 and bracket_end != -1:
        raw = raw[bracket_start:bracket_end+1]

    result = json.loads(raw)
    return [result] if isinstance(result, dict) else result

# ============================================================
# ПЕРСОНАЛЬНЫЕ СОВЕТЫ
# ============================================================
def generate_advice(records) -> str:
    """Генерирует персональные советы на основе данных за месяц"""
    if not records or len(records) < 5:
        return ""

    stats = analyze_records(records)
    if not stats:
        return ""

    total = stats["total"]
    by_cat = stats["by_category"]
    advice = []

    # Совет по кафе/еде вне дома
    cafe_keywords = ["кофе", "кафе", "ресторан", "фастфуд", "доставка", "glovo"]
    cafe_total = sum(
        v["total"] for k, v in stats.get("leaks", {}).items()
        if any(kw in k for kw in cafe_keywords)
    )
    food_total = by_cat.get("Еда / продукты", 0)
    if food_total > total * 0.35:
        saving = food_total * 0.25
        advice.append(
            f"🍔 На еду уходит {int(food_total/total*100)}% бюджета. "
            f"Если сократить на 25% — сэкономишь *{saving:,.0f} ₴/мес*"
        )

    # Совет по развлечениям
    ent_total = by_cat.get("Развлечения", 0)
    if ent_total > total * 0.20:
        saving = ent_total * 0.30
        advice.append(
            f"🎮 Развлечения — {int(ent_total/total*100)}% трат. "
            f"Сократить на 30% = *+{saving:,.0f} ₴* в кармане"
        )

    # Совет по никотину
    nic_total = by_cat.get("Никотин", 0)
    if nic_total > 500:
        annual = nic_total * 12
        advice.append(
            f"🚬 На никотин: *{nic_total:,.0f} ₴/мес* = *{annual:,.0f} ₴/год*. "
            f"Есть над чем подумать 💭"
        )

    # Совет по транспорту
    trans_total = by_cat.get("Транспорт", 0)
    if trans_total > total * 0.25:
        advice.append(
            f"🚗 Транспорт съедает {int(trans_total/total*100)}% бюджета ({trans_total:,.0f} ₴). "
            f"Может, иногда такси → маршрутка?"
        )

    # Совет по утечкам
    leaks = stats.get("leaks", {})
    if leaks:
        top_leak = max(leaks.items(), key=lambda x: x[1]["total"])
        name, data = top_leak
        if data["total"] > 300:
            advice.append(
                f"💸 «{name}» — {data['count']} раз = *{data['total']:,.0f} ₴*. "
                f"Самая частая трата месяца"
            )

    # Сравнение с прошлым месяцем
    all_records = get_all_records()
    now = datetime.now()
    prev_month = (now.replace(day=1) - timedelta(days=1))
    prev_records = [
        r for r in all_records
        if _record_in_month(r, prev_month.month, prev_month.year)
    ]
    if prev_records:
        prev_stats = analyze_records(prev_records)
        if prev_stats and prev_stats["total"] > 0:
            diff_pct = int(((total - prev_stats["total"]) / prev_stats["total"]) * 100)
            if diff_pct > 15:
                advice.append(
                    f"📈 Траты выросли на *{diff_pct}%* по сравнению с прошлым месяцем "
                    f"({prev_stats['total']:,.0f} ₴ → {total:,.0f} ₴)"
                )
            elif diff_pct < -10:
                advice.append(
                    f"📉 Молодец! Траты упали на *{abs(diff_pct)}%* по сравнению с прошлым месяцем 🎉"
                )

    if not advice:
        return ""

    lines = ["💡 *Персональные советы:*\n"]
    for i, a in enumerate(advice[:4], 1):
        lines.append(f"{i}. {a}")
    return "\n".join(lines)


def _record_in_month(r, month, year):
    try:
        date = datetime.strptime(r.get("Дата", "")[:10], "%d.%m.%Y")
        return date.month == month and date.year == year
    except:
        return False


# ============================================================
# ИНСАЙТ НЕДЕЛИ
# ============================================================
def build_weekly_insight() -> str:
    """Умный инсайт — бот анализирует паттерны и выдаёт главный вывод"""
    records = get_week_records()
    month_records = get_current_month_records()

    if not records:
        return "📭 За эту неделю данных ещё нет."

    stats = analyze_records(records)
    insights = []

    # Самый дорогой день
    if stats["by_day"]:
        top_day = max(stats["by_day"], key=stats["by_day"].get)
        top_amt = stats["by_day"][top_day]
        avg_day = stats["total"] / 7
        if top_amt > avg_day * 1.5:
            pct = int((top_amt / avg_day - 1) * 100)
            insights.append(f"📅 Самый дорогой день — *{top_day}* (+{pct}% от среднего)")

    # Топ категория недели
    if stats["by_category"]:
        top_cat = max(stats["by_category"], key=stats["by_category"].get)
        top_amt = stats["by_category"][top_cat]
        pct = int((top_amt / stats["total"]) * 100)
        insights.append(
            f"{EMOJI_MAP.get(top_cat, '📦')} Основная трата недели: *{top_cat}* — {pct}% от всех расходов"
        )

    # Утечки недели
    if stats["leaks"]:
        top = max(stats["leaks"].items(), key=lambda x: x[1]["total"])
        insights.append(
            f"💸 Повторяющаяся трата: *{top[0]}* — {top[1]['count']} раза на *{top[1]['total']:,.0f} ₴*"
        )

    # Прогноз месяца на основе недели
    if month_records:
        month_stats = analyze_records(month_records)
        now = datetime.now()
        daily_avg = month_stats["total"] / now.day
        projected = daily_avg * 30
        insights.append(f"📈 По текущему темпу месяц выйдет на *~{projected:,.0f} ₴*")

    # Персональный совет
    advice = generate_advice(month_records)

    lines = ["🧠 *Инсайт недели*\n"]
    lines += insights
    if advice:
        lines.append(f"\n{advice}")

    return "\n".join(lines)


async def send_weekly_insight(context: ContextTypes.DEFAULT_TYPE):
    """Авто-отправка инсайта каждую пятницу в 19:00"""
    chat_id = context.job.data.get("chat_id")
    if not chat_id:
        return
    insight = build_weekly_insight()
    await context.bot.send_message(chat_id=chat_id, text=insight, parse_mode="Markdown")


# ============================================================
# HANDLERS
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [KeyboardButton("💰 Финансы"), KeyboardButton("📊 Аналитика")],
        [KeyboardButton("💸 Долги"), KeyboardButton("⚙️ Прочее")]
    ]
    await update.message.reply_text(
        "👋 Привет! Я твой финансовый аналитик.\n\n"
        "🎙 *Как записать трату:*\n"
        "• «Снюс 800»\n"
        "• «Мойка 350, бензин 1200, кофе 90»\n\n"
        "💸 *Как записать долг:*\n"
        "• «Дал в долг Саше 500»\n\n"
        "💰 *Установить бюджет:*\n"
        "• «Бюджет 20000»\n\n"
        "👇 *Используй меню:*\n"
        "💰 Финансы — статистика, бюджет, зарплата\n"
        "📊 Аналитика — отчёты, советы, сравнения\n"
        "💸 Долги — кому дал, напоминания\n"
        "⚙️ Прочее — привычки, прошлое я",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )
    # Загружаем долги из таблицы при старте
    load_debts_from_sheet()

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Анализирую...")
    try:
        records = get_current_month_records()
        if not records:
            await update.message.reply_text("📭 В этом месяце ещё нет записей.")
            return
        stats = analyze_records(records)
        now = datetime.now()
        daily_avg = stats["total"] / now.day if now.day > 0 else 0
        projected = daily_avg * 30
        lines = [f"📊 *Статистика за {month_name(now.month)}* ({stats['count']} записей)\n"]
        for cat, amt in sorted(stats["by_category"].items(), key=lambda x: -x[1]):
            pct = int((amt / stats["total"]) * 100)
            lines.append(f"{EMOJI_MAP.get(cat, '📦')} {cat}: *{amt:,.0f} ₴* ({pct}%)")
        lines.append(f"\n💰 *Итого: {stats['total']:,.0f} ₴*")
        lines.append(f"📈 Прогноз на месяц: *~{projected:,.0f} ₴*")
        budget_status = get_budget_status(update.effective_chat.id)
        if budget_status:
            pct = budget_status["percent"]
            bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
            lines.append(f"\n💰 Бюджет: [{bar}] {pct}%")
            lines.append(f"Осталось: *{budget_status['left']:,.0f} ₴*")
        if stats["leaks"]:
            lines.append("\n💸 *Частые траты:*")
            for desc, data in list(stats["leaks"].items())[:3]:
                lines.append(f"• {desc}: {data['count']}× = *{data['total']:,.0f} ₴*")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Stats error: {e}")
        await update.message.reply_text("❌ Ошибка статистики.")

async def weekly_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Формирую отчёт...")
    try:
        await update.message.reply_text(build_weekly_report(), parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Weekly error: {e}")
        await update.message.reply_text("❌ Ошибка при формировании отчёта.")

async def monthly_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Формирую отчёт...")
    try:
        await update.message.reply_text(build_monthly_report(), parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Monthly error: {e}")
        await update.message.reply_text("❌ Ошибка при формировании отчёта.")

async def budget_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    budget_status = get_budget_status(update.effective_chat.id)
    if not budget_status:
        await update.message.reply_text(
            "💰 Бюджет не установлен.\n\nНапиши: «Бюджет 20000»"
        )
        return
    pct = budget_status["percent"]
    bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
    status = "🟢" if pct < 70 else "🟡" if pct < 90 else "🔴"
    await update.message.reply_text(
        f"💰 *Бюджет на месяц*\n\n"
        f"{status} [{bar}] *{pct}%*\n\n"
        f"Бюджет: *{budget_status['budget']:,.0f} ₴*\n"
        f"Потрачено: *{budget_status['spent']:,.0f} ₴*\n"
        f"Осталось: *{budget_status['left']:,.0f} ₴*",
        parse_mode="Markdown"
    )

async def debts_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = build_debts_message()
    if debts:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Отметить погашенным", callback_data="show_debts")
        ]])
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=keyboard)
    else:
        await update.message.reply_text(msg, parse_mode="Markdown")

async def salary_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    info = get_salary_info(update.effective_chat.id)
    if not info:
        await update.message.reply_text(
            "💵 *День зарплаты не установлен*\n\n"
            "Напиши одним из способов:\n"
            "• «Зарплата 25» — только день\n"
            "• «Зарплата 25 числа 35000» — день и сумма\n"
            "• «Получаю зарплату 10-го 28000»",
            parse_mode="Markdown"
        )
        return
    status = build_salary_status(update.effective_chat.id)
    await update.message.reply_text(status, parse_mode="Markdown")

async def compare_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Сравниваю месяцы...")
    try:
        await update.message.reply_text(build_months_comparison(), parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Compare error: {e}")
        await update.message.reply_text("❌ Ошибка при сравнении.")


# ============================================================
# СРАВНЕНИЕ С ПРОШЛЫМ "Я"
# ============================================================
def build_past_self_comparison() -> str:
    all_records = get_all_records()
    now = datetime.now()

    def get_month_stats(months_ago):
        target = now.replace(day=1)
        for _ in range(months_ago):
            target = (target - timedelta(days=1)).replace(day=1)
        recs = [r for r in all_records if _record_in_month(r, target.month, target.year)]
        return analyze_records(recs) if recs else None, target

    cur_stats = analyze_records(get_current_month_records())
    stats_1, date_1 = get_month_stats(1)
    stats_2, date_2 = get_month_stats(2)
    stats_3, date_3 = get_month_stats(3)

    if not cur_stats:
        return "📭 Недостаточно данных для сравнения."

    lines = ["🪞 *Сравнение с прошлым «я»*\n"]
    cur_total = cur_stats["total"]

    comparisons = [
        (stats_1, date_1, "1 месяц назад"),
        (stats_2, date_2, "2 месяца назад"),
        (stats_3, date_3, "3 месяца назад"),
    ]

    has_data = False
    for stats, date, label in comparisons:
        if not stats or stats["total"] == 0:
            continue
        has_data = True
        diff = cur_total - stats["total"]
        diff_pct = int((diff / stats["total"]) * 100)
        arrow = "📈" if diff > 0 else "📉"
        sign = "+" if diff > 0 else ""
        verdict = "больше" if diff > 0 else "меньше"
        lines.append(
            f"{arrow} *{label}* ({month_name(date.month)}):\n"
            f"   {sign}{diff_pct}% — тратишь на *{abs(diff):,.0f} ₴ {verdict}*"
        )

    if not has_data:
        return "📭 Нужно минимум 2 месяца данных."

    # Сравнение по категориям с самым давним месяцем
    oldest_stats = None
    for stats, date, label in reversed(comparisons):
        if stats:
            oldest_stats = (stats, date, label)
            break

    if oldest_stats:
        stats, date, label = oldest_stats
        lines.append(f"\n📊 *Изменения по категориям vs {label}:*")
        for cat in CATEGORIES:
            cur_amt = cur_stats["by_category"].get(cat, 0)
            old_amt = stats["by_category"].get(cat, 0)
            if old_amt == 0 and cur_amt == 0:
                continue
            diff = cur_amt - old_amt
            if abs(diff) < 100:
                continue
            emoji = EMOJI_MAP.get(cat, "📦")
            sign = "+" if diff > 0 else ""
            arrow = "📈" if diff > 0 else "📉"
            lines.append(f"{emoji} {cat}: {arrow} {sign}{diff:,.0f} ₴")

    # Итоговый вывод
    if stats_2 and stats_2["total"] > 0:
        long_diff_pct = int(((cur_total - stats_2["total"]) / stats_2["total"]) * 100)
        if long_diff_pct <= -15:
            lines.append(f"\n🏆 *Ты стал тратить на {abs(long_diff_pct)}% меньше, чем 2 месяца назад! Отличный прогресс!*")
        elif long_diff_pct >= 20:
            lines.append(f"\n⚠️ *Траты выросли на {long_diff_pct}% за 2 месяца — стоит разобраться почему*")

    return "\n".join(lines)


# ============================================================
# АНАЛИЗ СТОИМОСТИ ПРИВЫЧЕК
# ============================================================

# Что можно купить за разные суммы
EQUIVALENTS = [
    (2000,   "🍕 100 пицц"),
    (3000,   "🎮 3 новые игры в Steam"),
    (5000,   "✈️ билет в Европу"),
    (8000,   "📱 бюджетный смартфон"),
    (15000,  "💻 неплохой ноутбук"),
    (25000,  "📱 iPhone"),
    (40000,  "🏖 неделя на море на двоих"),
    (60000,  "🚗 первый взнос на авто"),
    (100000, "🌍 отпуск мечты за границей"),
]

def get_equivalent(amount: float) -> str:
    best = None
    for threshold, label in EQUIVALENTS:
        if amount >= threshold * 0.7:
            best = label
    return best

def build_habit_cost_analysis() -> str:
    all_records = get_all_records()
    now = datetime.now()

    # Берём последние 3 месяца для среднего
    months_data = defaultdict(list)
    for r in all_records:
        try:
            date = datetime.strptime(r.get("Дата", "")[:10], "%d.%m.%Y")
            months_data[(date.year, date.month)].append(r)
        except:
            continue

    if len(months_data) < 1:
        return "📭 Недостаточно данных."

    # Среднемесячные траты по описаниям
    desc_totals = defaultdict(lambda: {"total": 0.0, "count": 0, "months": set()})
    for (year, month), records in months_data.items():
        sum_key = get_sum_key(records)
        for r in records:
            desc = r.get("Описание", "").lower().strip()
            amt = float(r[sum_key]) if r[sum_key] else 0
            if desc and amt > 0:
                desc_totals[desc]["total"] += amt
                desc_totals[desc]["count"] += 1
                desc_totals[desc]["months"].add((year, month))

    num_months = max(len(months_data), 1)

    # Находим повторяющиеся привычки (минимум в 2 месяцах)
    habits = {
        k: v for k, v in desc_totals.items()
        if len(v["months"]) >= 2 and v["total"] / num_months >= 200
    }

    if not habits:
        return "📭 Пока недостаточно повторяющихся трат для анализа.\nЗаписывай траты ещё несколько недель!"

    lines = ["💸 *Анализ стоимости привычек*\n"]
    lines.append(f"_За {num_months} мес. данных_\n")

    sorted_habits = sorted(habits.items(), key=lambda x: -x[1]["total"])[:6]

    for desc, data in sorted_habits:
        monthly_avg = data["total"] / num_months
        annual = monthly_avg * 12
        equiv = get_equivalent(annual)

        lines.append(f"*{desc.capitalize()}*")
        lines.append(f"  📅 В месяц: *{monthly_avg:,.0f} ₴*")
        lines.append(f"  📆 В год: *{annual:,.0f} ₴*")
        if equiv:
            lines.append(f"  💡 Это = {equiv}")
        lines.append("")

    # Топ привычка
    if sorted_habits:
        top_desc, top_data = sorted_habits[0]
        top_annual = (top_data["total"] / num_months) * 12
        equiv = get_equivalent(top_annual)
        if equiv:
            lines.append(f"🏆 *Самая дорогая привычка — «{top_desc}»*")
            lines.append(f"За год уходит *{top_annual:,.0f} ₴* — это {equiv}")

    return "\n".join(lines)


async def past_self_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Анализирую твою историю...")
    try:
        await update.message.reply_text(build_past_self_comparison(), parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Past self error: {e}")
        await update.message.reply_text("❌ Ошибка при анализе.")


async def habits_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Считаю стоимость привычек...")
    try:
        await update.message.reply_text(build_habit_cost_analysis(), parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Habits error: {e}")
        await update.message.reply_text("❌ Ошибка при анализе привычек.")


async def advice_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Анализирую твои траты...")
    try:
        records = get_current_month_records()
        advice = generate_advice(records)
        insight = build_weekly_insight()
        msg = ""
        if advice:
            msg += advice
        if insight:
            msg += f"\n\n{insight}"
        if not msg:
            msg = "📭 Пока недостаточно данных для советов. Записывай траты несколько дней!"
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Advice error: {e}")
        await update.message.reply_text("❌ Ошибка при формировании советов.")


    msg = build_debts_message()
    if debts:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Отметить погашенным", callback_data="show_debts")
        ]])
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=keyboard)
    else:
        await update.message.reply_text(msg, parse_mode="Markdown")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("menu_"):
        action = data.replace("menu_", "")
        await query.answer()
        chat_id = query.message.chat_id

        async def send(text, **kwargs):
            await context.bot.send_message(chat_id=chat_id, text=text, **kwargs)

        try:
            if action == "stats":
                records = get_current_month_records()
                if not records:
                    await send("📭 В этом месяце ещё нет записей.")
                    return
                stats = analyze_records(records)
                now = datetime.now()
                daily_avg = stats["total"] / now.day if now.day > 0 else 0
                projected = daily_avg * 30
                lines = [f"📊 *Статистика за {month_name(now.month)}* ({stats['count']} записей)\n"]
                for cat, amt in sorted(stats["by_category"].items(), key=lambda x: -x[1]):
                    pct = int((amt / stats["total"]) * 100)
                    lines.append(f"{EMOJI_MAP.get(cat, '📦')} {cat}: *{amt:,.0f} ₴* ({pct}%)")
                lines.append(f"\n💰 *Итого: {stats['total']:,.0f} ₴*")
                lines.append(f"📈 Прогноз на месяц: *~{projected:,.0f} ₴*")
                budget_status = get_budget_status(chat_id)
                if budget_status:
                    pct = budget_status["percent"]
                    bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
                    lines.append(f"\n💰 Бюджет: [{bar}] {pct}%")
                    lines.append(f"Осталось: *{budget_status['left']:,.0f} ₴*")
                await send("\n".join(lines), parse_mode="Markdown")

            elif action == "budget":
                budget_status = get_budget_status(chat_id)
                if not budget_status:
                    await send("💰 Бюджет не установлен.\n\nНапиши: «Бюджет 20000»")
                    return
                pct = budget_status["percent"]
                bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
                status = "🟢" if pct < 70 else "🟡" if pct < 90 else "🔴"
                await send(
                    f"💰 *Бюджет на месяц*\n\n{status} [{bar}] *{pct}%*\n\n"
                    f"Бюджет: *{budget_status['budget']:,.0f} ₴*\n"
                    f"Потрачено: *{budget_status['spent']:,.0f} ₴*\n"
                    f"Осталось: *{budget_status['left']:,.0f} ₴*",
                    parse_mode="Markdown"
                )

            elif action == "salary":
                status = build_salary_status(chat_id)
                if not status:
                    await send(
                        "💵 *День зарплаты не установлен*\n\n"
                        "Напиши:\n• «Зарплата 25» — только день\n"
                        "• «Зарплата 25 числа 35000» — день и сумма"
                    )
                else:
                    await send(status, parse_mode="Markdown")

            elif action == "compare":
                await send("⏳ Сравниваю месяцы...")
                await send(build_months_comparison(), parse_mode="Markdown")

            elif action == "week":
                await send("⏳ Формирую отчёт...")
                await send(build_weekly_report(), parse_mode="Markdown")

            elif action == "month":
                await send("⏳ Формирую отчёт...")
                await send(build_monthly_report(), parse_mode="Markdown")

            elif action == "past":
                await send("⏳ Анализирую твою историю...")
                await send(build_past_self_comparison(), parse_mode="Markdown")

            elif action == "habits":
                await send("⏳ Считаю стоимость привычек...")
                await send(build_habit_cost_analysis(), parse_mode="Markdown")

            elif action == "advice":
                await send("⏳ Анализирую твои траты...")
                records = get_current_month_records()
                advice = generate_advice(records)
                insight = build_weekly_insight()
                msg = ""
                if advice:
                    msg += advice
                if insight:
                    msg += f"\n\n{insight}"
                if not msg:
                    msg = "📭 Пока недостаточно данных для советов."
                await send(msg, parse_mode="Markdown")

        except Exception as e:
            logger.error(f"Menu callback error: {e}")
            await send("❌ Ошибка. Попробуй ещё раз.")
        return

    if data.startswith("quick_"):
        parts = data.split("_", 2)
        if len(parts) == 3:
            _, category, amount_str = parts
            amount = float(amount_str)
            date = datetime.now().strftime("%d.%m.%Y %H:%M")
            save_expense(date, amount, category, "быстрая запись", str(amount))
            update_memory("", category)
            emoji = EMOJI_MAP.get(category, "📦")
            await query.edit_message_text(
                f"⚡ *{amount:,.0f} ₴* → {emoji} {category}\n✅ Записано!",
                parse_mode="Markdown"
            )
        return

    if data == "show_debts":
        if not debts:
            await query.edit_message_text("✅ Активных долгов нет!")
            return
        keyboard = []
        for debt_id, d in debts.items():
            amounts = d.get("amounts", [{"amount": d.get("amount", 0), "currency": "UAH"}])
            amt_str = format_debt_amounts(amounts)
            keyboard.append([InlineKeyboardButton(
                f"👤 {d['name']} — {amt_str.replace('*', '')}",
                callback_data=f"debt_menu_{debt_id}"
            )])
        keyboard.append([InlineKeyboardButton("← Назад", callback_data="back")])
        await query.edit_message_text(
            "Выбери долг:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data.startswith("debt_menu_"):
        debt_id = data.replace("debt_menu_", "")
        if debt_id not in debts:
            await query.edit_message_text("Долг уже закрыт.")
            return
        d = debts[debt_id]
        amounts = d.get("amounts", [{"amount": d.get("amount", 0), "currency": "UAH"}])
        amt_str = format_debt_amounts(amounts)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Вернули полностью", callback_data=f"paid_{debt_id}")],
            [InlineKeyboardButton("💰 Частичное погашение", callback_data=f"partial_{debt_id}")],
            [InlineKeyboardButton("⏰ Напомнить через 2 нед.", callback_data=f"remind_{debt_id}")],
            [InlineKeyboardButton("← Назад", callback_data="show_debts")],
        ])
        note_str = f"\n📝 {d['note']}" if d.get("note") else ""
        await query.edit_message_text(
            f"👤 *{d['name']}* — {amt_str}{note_str}\n📅 {d['date']}\n\nЧто сделать?",
            parse_mode="Markdown",
            reply_markup=keyboard
        )

    elif data.startswith("partial_"):
        debt_id = data.replace("partial_", "")
        if debt_id not in debts:
            await query.edit_message_text("Долг уже закрыт.")
            return
        d = debts[debt_id]
        amounts = d.get("amounts", [{"amount": d.get("amount", 0), "currency": "UAH"}])
        # Показываем кнопки для каждой валюты
        keyboard = []
        for i, a in enumerate(amounts):
            sym = CURRENCY_SYMBOLS.get(a.get("currency", "UAH"), "₴")
            cur = a.get("currency", "UAH")
            keyboard.append([InlineKeyboardButton(
                f"💰 Частично в {sym} ({a['amount']:,.0f} {sym} осталось)",
                callback_data=f"partialcur_{debt_id}_{i}"
            )])
        keyboard.append([InlineKeyboardButton("← Назад", callback_data=f"debt_menu_{debt_id}")])
        await query.edit_message_text(
            f"💰 *Частичное погашение*\nВыбери валюту:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data.startswith("partialcur_"):
        # partialcur_{debt_id}_{amount_index}
        parts = data.split("_")
        debt_id = parts[1]
        amt_idx = int(parts[2])
        if debt_id not in debts:
            await query.edit_message_text("Долг уже закрыт.")
            return
        d = debts[debt_id]
        amounts = d.get("amounts", [])
        if amt_idx >= len(amounts):
            return
        a = amounts[amt_idx]
        sym = CURRENCY_SYMBOLS.get(a.get("currency", "UAH"), "₴")
        # Сохраняем контекст в user_data для следующего шага
        context.user_data["partial_debt_id"] = debt_id
        context.user_data["partial_amt_idx"] = amt_idx
        await query.edit_message_text(
            f"💰 Сколько вернули в {sym}?\n\nНапиши сумму в чат (например: `500`)",
            parse_mode="Markdown"
        )

    elif data.startswith("paid_"):
        debt_id = data.replace("paid_", "")
        if debt_id in debts:
            d = debts.pop(debt_id)
            mark_debt_paid_in_sheet(debt_id)
            amounts = d.get("amounts", [{"amount": d.get("amount", 0), "currency": "UAH"}])
            amt_str = format_debt_amounts(amounts)
            await query.edit_message_text(
                f"✅ Отлично! *{d['name']}* вернул {amt_str}\n\nДолг закрыт 🎉",
                parse_mode="Markdown"
            )
        else:
            await query.edit_message_text("Долг уже закрыт.")

    elif data.startswith("remind_"):
        debt_id = data.replace("remind_", "")
        if debt_id in debts:
            d = debts[debt_id]
            chat_id = query.message.chat_id
            context.job_queue.run_once(
                send_debt_reminder,
                when=timedelta(weeks=2),
                data={"debt_id": debt_id, "chat_id": chat_id},
                name=f"debt_{debt_id}"
            )
            await query.edit_message_text(
                f"⏰ Напомню о долге *{d['name']}* через 2 недели.",
                parse_mode="Markdown"
            )

    elif data == "back":
        await query.edit_message_text(build_debts_message(), parse_mode="Markdown")

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎙 Распознаю...")
    try:
        voice = update.message.voice
        file = await context.bot.get_file(voice.file_id)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            await file.download_to_drive(tmp.name)
            tmp_path = tmp.name
        text = transcribe_audio(tmp_path)
        os.unlink(tmp_path)
        await update.message.reply_text(f"📝 Распознал: _{text}_", parse_mode="Markdown")
        await process_message(update, context, text)
    except Exception as e:
        logger.error(f"Voice error: {e}")
        await update.message.reply_text("❌ Не удалось распознать. Попробуй ещё раз.")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "📊 Статистика":
        await stats_command(update, context); return
    if text == "📅 Отчёт за неделю":
        await weekly_report_command(update, context); return
    if text == "📆 Отчёт за месяц":
        await monthly_report_command(update, context); return
    if text == "💰 Бюджет":
        await budget_command(update, context); return
    if text == "💵 Зарплата":
        await salary_command(update, context); return
    if text == "🪞 Прошлое я":
        await past_self_command(update, context); return
    if text == "💸 Привычки":
        await habits_command(update, context); return
    if text == "📊 Сравнение месяцев":
        await compare_command(update, context); return
    if text == "💡 Советы":
        await advice_command(update, context); return
    if text == "💰 Финансы":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Статистика", callback_data="menu_stats"),
             InlineKeyboardButton("💰 Бюджет", callback_data="menu_budget")],
            [InlineKeyboardButton("💵 Зарплата", callback_data="menu_salary"),
             InlineKeyboardButton("📊 Сравнение", callback_data="menu_compare")],
        ])
        await update.message.reply_text("💰 *Финансы* — выбери:", parse_mode="Markdown", reply_markup=keyboard)
        return

    if text == "📊 Аналитика":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📅 Неделя", callback_data="menu_week"),
             InlineKeyboardButton("📆 Месяц", callback_data="menu_month")],
            [InlineKeyboardButton("🪞 Прошлое я", callback_data="menu_past"),
             InlineKeyboardButton("💸 Привычки", callback_data="menu_habits")],
            [InlineKeyboardButton("💡 Советы", callback_data="menu_advice")],
        ])
        await update.message.reply_text("📊 *Аналитика* — выбери:", parse_mode="Markdown", reply_markup=keyboard)
        return

    if text == "⚙️ Прочее":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💡 Советы", callback_data="menu_advice"),
             InlineKeyboardButton("💵 Зарплата", callback_data="menu_salary")],
            [InlineKeyboardButton("🪞 Прошлое я", callback_data="menu_past"),
             InlineKeyboardButton("💸 Привычки", callback_data="menu_habits")],
        ])
        await update.message.reply_text("⚙️ *Прочее* — выбери:", parse_mode="Markdown", reply_markup=keyboard)
        return

    if text == "💸 Долги":
        await debts_command(update, context); return
    await process_message(update, context, text)

async def process_message(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    lower = text.lower()

    # Установка зарплаты
    salary_keywords = ["зарплата", "зарплату", "получаю", "получу", "аванс", "зп"]
    if any(kw in lower for kw in salary_keywords):
        numbers = re.findall(r'\d+', text)
        if numbers:
            day = int(numbers[0])
            amount = float(numbers[1]) if len(numbers) > 1 else None
            if 1 <= day <= 31:
                set_salary_info(update.effective_chat.id, day, amount)
                amount_str = f" — *{amount:,.0f} ₴*" if amount else ""
                await update.message.reply_text(
                    f"💵 *Зарплата установлена!*\n\n"
                    f"📅 День: *{day}-е число*{amount_str}\n\n"
                    f"Теперь нажми кнопку *💵 Зарплата* чтобы видеть сколько можно тратить в день.",
                    parse_mode="Markdown"
                )
                return

    # Частичное погашение долга — ожидаем число
    if "partial_debt_id" in context.user_data:
        debt_id = context.user_data.get("partial_debt_id")
        amt_idx = context.user_data.get("partial_amt_idx", 0)
        numbers = re.findall(r'[\d]+(?:[.,]\d+)?', text)
        if numbers and debt_id in debts:
            partial_amount = float(numbers[0].replace(",", "."))
            d = debts[debt_id]
            amounts = d.get("amounts", [])
            if amt_idx < len(amounts):
                old_amount = float(amounts[amt_idx]["amount"])
                currency = amounts[amt_idx].get("currency", "UAH")
                sym = CURRENCY_SYMBOLS.get(currency, "₴")
                new_amount = old_amount - partial_amount
                if new_amount <= 0:
                    # Полностью погашена эта валюта
                    amounts.pop(amt_idx)
                    if not amounts:
                        # Все валюты погашены — закрываем долг
                        debts.pop(debt_id)
                        mark_debt_paid_in_sheet(debt_id)
                        del context.user_data["partial_debt_id"]
                        del context.user_data["partial_amt_idx"]
                        await update.message.reply_text(
                            f"✅ *{d['name']}* полностью погасил долг! 🎉",
                            parse_mode="Markdown"
                        )
                        return
                    else:
                        msg = f"✅ Погашено *{partial_amount:,.0f} {sym}* в этой валюте — эта часть закрыта!"
                else:
                    amounts[amt_idx]["amount"] = new_amount
                    msg = (f"💰 Частично погашено: *{partial_amount:,.0f} {sym}*\n"
                           f"Остаток по {sym}: *{new_amount:,.0f} {sym}*")

                debts[debt_id]["amounts"] = amounts
                new_amounts_str = " + ".join(
                    f"{a['amount']} {CURRENCY_SYMBOLS.get(a.get('currency','UAH'),'₴')}"
                    for a in amounts
                )
                update_debt_amounts_in_sheet(debt_id, new_amounts_str)
                del context.user_data["partial_debt_id"]
                del context.user_data["partial_amt_idx"]
                await update.message.reply_text(msg, parse_mode="Markdown")
                return
        else:
            del context.user_data["partial_debt_id"]
            del context.user_data["partial_amt_idx"]

    # Установка бюджета
    if "бюджет" in lower:
        numbers = re.findall(r'\d+', text)
        if numbers:
            amount = float(numbers[0])
            save_setting(f"budget_{update.effective_chat.id}", str(amount))
            await update.message.reply_text(
                f"✅ Бюджет установлен: *{amount:,.0f} ₴/месяц*",
                parse_mode="Markdown"
            )
            return

    # Запись долга
    debt_keywords = ["дал в долг", "одолжил", "дала в долг", "дав в борг", "позичив", "долг"]
    if any(kw in lower for kw in debt_keywords):
        try:
            parsed = parse_debt(text)
            amounts = parsed.get("amounts", [])
            # Поддержка старого формата
            if not amounts and parsed.get("amount"):
                amounts = [{"amount": float(parsed["amount"]), "currency": "UAH"}]

            if amounts and parsed.get("name"):
                debt_counter[0] += 1
                debt_id = str(debt_counter[0])
                date_str = datetime.now().strftime("%d.%m.%Y")
                debts[debt_id] = {
                    "name": parsed["name"],
                    "amounts": amounts,
                    "date": date_str,
                    "note": parsed.get("note", "")
                }
                # Для совместимости с таблицей сохраняем строку сумм
                amounts_str = " + ".join(
                    f"{a['amount']} {CURRENCY_SYMBOLS.get(a.get('currency','UAH'),'₴')}"
                    for a in amounts
                )
                save_debt_to_sheet(debt_id, parsed["name"], amounts_str, date_str, parsed.get("note", ""))

                chat_id = update.effective_chat.id
                context.job_queue.run_once(
                    send_debt_reminder,
                    when=timedelta(weeks=2),
                    data={"debt_id": debt_id, "chat_id": chat_id},
                    name=f"debt_{debt_id}"
                )
                note_str = f"\n📝 {parsed['note']}" if parsed.get("note") else ""
                amt_str = format_debt_amounts(amounts)
                await update.message.reply_text(
                    f"💸 *Долг записан!*\n\n"
                    f"👤 Кому: *{parsed['name']}*\n"
                    f"💰 Сумма: {amt_str}{note_str}\n\n"
                    f"⏰ Напомню через 2 недели если не вернут.",
                    parse_mode="Markdown"
                )
                return
        except Exception as e:
            logger.error(f"Debt parse error: {e}")

    # ⚡ Быстрый режим — просто число
    stripped = text.strip().replace(",", ".").replace(" ", "")
    if re.fullmatch(r'\d+(\.\d+)?', stripped):
        amount = float(stripped)
        # Проверяем память — может уже знаем категорию
        mem_cat = get_memory_category(text)
        if mem_cat:
            # Сразу записываем без уточнения
            date = datetime.now().strftime("%d.%m.%Y %H:%M")
            save_expense(date, amount, mem_cat, "быстрая запись", text)
            await update.message.reply_text(
                f"⚡ *{amount:,.0f} ₴* → {EMOJI_MAP.get(mem_cat,'📦')} {mem_cat}\n_Записано!_",
                parse_mode="Markdown"
            )
        else:
            await handle_quick_mode(update, context, amount)
        return

    # Обычные расходы
    try:
        # Проверяем память для автокатегоризации
        mem_cat = get_memory_category(text)

        expenses = parse_expenses(text)
        if not expenses:
            await update.message.reply_text(
                "🤔 Не нашёл сумму.\nПопробуй: «Снюс 800» или «Продукты 500, такси 200»"
            )
            return

        # Применяем память если AI не уверен
        for exp in expenses:
            if mem_cat and exp.get("category") == "Другое":
                exp["category"] = mem_cat

        date = datetime.now().strftime("%d.%m.%Y %H:%M")
        month_records = get_current_month_records()
        lines = ["✅ *Записано!*\n"]

        for exp in expenses:
            amount = float(exp["amount"])
            category = exp.get("category", "Другое")
            description = exp.get("description", "—")
            save_expense(date, amount, category, description, text)
            emoji = EMOJI_MAP.get(category, "📦")
            lines.append(f"{emoji} {description} — *{amount:,.0f} ₴* ({category})")
            # Обновляем память
            update_memory(description, category)

        if len(expenses) > 1:
            total = sum(float(e["amount"]) for e in expenses)
            lines.append(f"\n💰 *Итого: {total:,.0f} ₴*")

        # Умный комментарий
        comment = get_smart_comment(
            expenses[0].get("category", "Другое"),
            expenses[0].get("description", ""),
            float(expenses[0]["amount"]),
            month_records
        )
        if comment:
            lines.append(f"\n{comment}")

        # 🚨 Умные предупреждения
        warnings = get_smart_warnings(update.effective_chat.id)
        for w in warnings:
            lines.append(f"\n{w}")

        # Предупреждение бюджета
        budget_status = get_budget_status(update.effective_chat.id)
        if budget_status:
            pct = budget_status["percent"]
            if pct >= 90:
                lines.append(f"\n🔴 *Внимание! Бюджет использован на {pct}%!*")
            elif pct >= 70:
                lines.append(f"\n🟡 Бюджет использован на {pct}% — осторожно!")

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Process error: {e}")
        await update.message.reply_text("❌ Ошибка при сохранении. Попробуй ещё раз.")

# ============================================================
# ЗАПУСК
# ============================================================
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("week", weekly_report_command))
    app.add_handler(CommandHandler("month", monthly_report_command))
    app.add_handler(CommandHandler("budget", budget_command))
    app.add_handler(CommandHandler("debts", debts_command))
    app.add_handler(CommandHandler("past", past_self_command))
    app.add_handler(CommandHandler("habits", habits_command))
    app.add_handler(CommandHandler("compare", compare_command))
    app.add_handler(CommandHandler("advice", advice_command))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    load_debts_from_sheet()
    load_settings()         # 💾 бюджет, зарплата
    load_memory_from_settings()  # 💾 контекстная память

    # Авто-инсайт каждую пятницу в 19:00
    chat_id = os.getenv("CHAT_ID")
    if chat_id and app.job_queue:
        app.job_queue.run_daily(
            send_weekly_insight,
            time=datetime.strptime("19:00", "%H:%M").time(),
            days=(4,),  # 4 = пятница
            data={"chat_id": chat_id}
        )

    logger.info("Бот запущен! v3.0 — оптимизирован")
    app.run_polling()

if __name__ == "__main__":
    main()
