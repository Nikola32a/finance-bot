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
    "Еда / продукты": "🍔",
    "Транспорт": "🚗",
    "Развлечения": "🎮",
    "Здоровье / аптека": "💊",
    "Никотин": "🚬",
    "Другое": "📦"
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

# ============================================================
# ХРАНИЛИЩЕ ДОЛГОВ (в памяти, сбрасывается при перезапуске)
# Для постоянного хранения используем отдельный лист Google Sheets
# ============================================================
debts = {}  # {debt_id: {name, amount, date, chat_id, note}}
debt_counter = [0]

def get_debts_sheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    if GOOGLE_CREDENTIALS:
        creds_dict = json.loads(GOOGLE_CREDENTIALS)
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    else:
        creds = Credentials.from_service_account_file("credentials.json", scopes=scopes)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
    try:
        sheet = spreadsheet.worksheet("Долги")
    except:
        sheet = spreadsheet.add_worksheet(title="Долги", rows=100, cols=6)
        sheet.insert_row(["ID", "Кому", "Сумма", "Дата", "Статус", "Примечание"], 1)
    return sheet

def load_debts_from_sheet():
    try:
        sheet = get_debts_sheet()
        records = sheet.get_all_records()
        for r in records:
            if r.get("Статус") == "активен":
                debt_id = str(r["ID"])
                debts[debt_id] = {
                    "name": r["Кому"],
                    "amount": float(r["Сумма"]),
                    "date": r["Дата"],
                    "note": r.get("Примечание", ""),
                    "row": None
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

# ============================================================
# GOOGLE SHEETS — РАСХОДЫ
# ============================================================
def get_sheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    if GOOGLE_CREDENTIALS:
        creds_dict = json.loads(GOOGLE_CREDENTIALS)
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    else:
        creds = Credentials.from_service_account_file("credentials.json", scopes=scopes)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(GOOGLE_SHEET_ID).sheet1
    if not sheet.get_all_values():
        sheet.insert_row(["Дата", "Сумма (₴)", "Категория", "Описание", "Исходный текст"], 1)
    return sheet

def get_all_records():
    sheet = get_sheet()
    return sheet.get_all_records()

def save_expense(date, amount, category, description, raw_text):
    sheet = get_sheet()
    sheet.append_row([date, amount, category, description, raw_text])

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
# БЮДЖЕТ
# ============================================================
budget_storage = {}

def get_budget_status(chat_id):
    budget = budget_storage.get(str(chat_id))
    if not budget:
        return None
    records = get_current_month_records()
    sum_key = get_sum_key(records) if records else "Сумма (₴)"
    spent = sum(float(r[sum_key]) for r in records if r[sum_key]) if records else 0
    left = budget - spent
    percent = int((spent / budget) * 100)
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
    lines = [f"📆 *Отчёт за {now.strftime('%B %Y')}*\n"]
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
def parse_debt(text):
    """Парсит текст типа 'дал в долг Саше 500' или 'долг Вася 1200 за телефон'"""
    prompt = f"""Из текста извлеки информацию о долге.

Текст: "{text}"

Верни ТОЛЬКО JSON без markdown-тиков:
{{"name": "<имя человека>", "amount": <число>, "note": "<за что, если упомянуто, иначе пустая строка>"}}

Правила:
- name: только имя человека
- amount: только число
- note: краткое описание за что дал в долг"""

    response = groq_client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=150,
        temperature=0.1
    )
    raw = response.choices[0].message.content.strip().replace("```json", "").replace("```", "").strip()
    return json.loads(raw)

def build_debts_message():
    active = {k: v for k, v in debts.items()}
    if not active:
        return "✅ Активных долгов нет!"
    lines = ["💸 *Активные долги:*\n"]
    for debt_id, d in active.items():
        days_ago = (datetime.now() - datetime.strptime(d["date"], "%d.%m.%Y")).days
        note_str = f" — _{d['note']}_" if d.get("note") else ""
        lines.append(f"👤 *{d['name']}* — *{d['amount']:,.0f} ₴*{note_str}")
        lines.append(f"   📅 {d['date']} ({days_ago} дн. назад)")
    total = sum(d["amount"] for d in active.values())
    lines.append(f"\n💰 *Итого: {total:,.0f} ₴*")
    return "\n".join(lines)

async def send_debt_reminder(context: ContextTypes.DEFAULT_TYPE):
    """Напоминание о долге каждые 2 недели"""
    job_data = context.job.data
    debt_id = job_data["debt_id"]
    chat_id = job_data["chat_id"]

    if debt_id not in debts:
        return  # долг уже погашен

    d = debts[debt_id]
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Долг вернули", callback_data=f"paid_{debt_id}"),
        InlineKeyboardButton("⏰ Напомнить ещё", callback_data=f"remind_{debt_id}")
    ]])
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"💸 *Напоминание о долге*\n\n"
             f"👤 *{d['name']}* должен тебе *{d['amount']:,.0f} ₴*\n"
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
- Даже одна трата — возвращай массив"""

    response = groq_client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=500,
        temperature=0.1
    )
    raw = response.choices[0].message.content.strip().replace("```json", "").replace("```", "").strip()
    result = json.loads(raw)
    return [result] if isinstance(result, dict) else result

# ============================================================
# HANDLERS
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [KeyboardButton("📊 Статистика"), KeyboardButton("📅 Отчёт за неделю")],
        [KeyboardButton("📆 Отчёт за месяц"), KeyboardButton("💰 Бюджет")],
        [KeyboardButton("💸 Долги")]
    ]
    await update.message.reply_text(
        "👋 Привет! Я твой финансовый аналитик.\n\n"
        "🎙 *Как записать трату:*\n"
        "• «Снюс 800»\n"
        "• «Мойка 350, бензин 1200, кофе 90»\n\n"
        "💸 *Как записать долг:*\n"
        "• «Дал в долг Саше 500»\n"
        "• «Одолжил Васе 1200 на телефон»\n\n"
        "💰 *Установить бюджет:*\n"
        "• «Бюджет 20000»",
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
        lines = [f"📊 *Статистика за {now.strftime('%B')}* ({stats['count']} записей)\n"]
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

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "show_debts":
        if not debts:
            await query.edit_message_text("✅ Активных долгов нет!")
            return
        keyboard = []
        for debt_id, d in debts.items():
            keyboard.append([InlineKeyboardButton(
                f"✅ {d['name']} — {d['amount']:,.0f} ₴",
                callback_data=f"paid_{debt_id}"
            )])
        keyboard.append([InlineKeyboardButton("← Назад", callback_data="back")])
        await query.edit_message_text(
            "Выбери долг который вернули:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data.startswith("paid_"):
        debt_id = data.replace("paid_", "")
        if debt_id in debts:
            d = debts.pop(debt_id)
            mark_debt_paid_in_sheet(debt_id)
            await query.edit_message_text(
                f"✅ Отлично! *{d['name']}* вернул *{d['amount']:,.0f} ₴*\n\nДолг закрыт 🎉",
                parse_mode="Markdown"
            )
        else:
            await query.edit_message_text("Долг уже закрыт.")

    elif data.startswith("remind_"):
        debt_id = data.replace("remind_", "")
        if debt_id in debts:
            d = debts[debt_id]
            # Планируем ещё одно напоминание через 2 недели
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
    if text == "💸 Долги":
        await debts_command(update, context); return
    await process_message(update, context, text)

async def process_message(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    lower = text.lower()

    # Установка бюджета
    if "бюджет" in lower:
        numbers = re.findall(r'\d+', text)
        if numbers:
            amount = float(numbers[0])
            budget_storage[str(update.effective_chat.id)] = amount
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
            if parsed.get("amount") and parsed.get("name"):
                debt_counter[0] += 1
                debt_id = str(debt_counter[0])
                date_str = datetime.now().strftime("%d.%m.%Y")
                debts[debt_id] = {
                    "name": parsed["name"],
                    "amount": float(parsed["amount"]),
                    "date": date_str,
                    "note": parsed.get("note", "")
                }
                save_debt_to_sheet(debt_id, parsed["name"], float(parsed["amount"]), date_str, parsed.get("note", ""))

                # Планируем напоминание через 2 недели
                chat_id = update.effective_chat.id
                context.job_queue.run_once(
                    send_debt_reminder,
                    when=timedelta(weeks=2),
                    data={"debt_id": debt_id, "chat_id": chat_id},
                    name=f"debt_{debt_id}"
                )

                note_str = f"\n📝 {parsed['note']}" if parsed.get("note") else ""
                await update.message.reply_text(
                    f"💸 *Долг записан!*\n\n"
                    f"👤 Кому: *{parsed['name']}*\n"
                    f"💰 Сумма: *{float(parsed['amount']):,.0f} ₴*{note_str}\n\n"
                    f"⏰ Напомню через 2 недели если не вернут.",
                    parse_mode="Markdown"
                )
                return
        except Exception as e:
            logger.error(f"Debt parse error: {e}")

    # Обычные расходы
    try:
        expenses = parse_expenses(text)
        if not expenses:
            await update.message.reply_text(
                "🤔 Не нашёл сумму.\nПопробуй: «Снюс 800» или «Продукты 500, такси 200»"
            )
            return

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
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    load_debts_from_sheet()
    logger.info("Бот запущен! v2.1")
    app.run_polling()

if __name__ == "__main__":
    main()
