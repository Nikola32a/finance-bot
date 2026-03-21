import os
import logging
import tempfile
import json
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
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


def save_expense(date, amount, category, description, raw_text):
    sheet = get_sheet()
    sheet.append_row([date, amount, category, description, raw_text])


def get_stats():
    sheet = get_sheet()
    records = sheet.get_all_records()
    if not records:
        return None
    total = sum(float(r["Сумма (₴)"]) for r in records if r["Сумма (₴)"])
    by_category = {}
    for r in records:
        cat = r["Категория"]
        amt = float(r["Сумма (₴)"]) if r["Сумма (₴)"] else 0
        by_category[cat] = by_category.get(cat, 0) + amt
    return {"total": total, "by_category": by_category, "count": len(records)}


def transcribe_audio(file_path: str) -> str:
    with open(file_path, "rb") as audio_file:
        transcript = groq_client.audio.transcriptions.create(
            model="whisper-large-v3",
            file=audio_file,
            language="ru"
        )
    return transcript.text


def parse_expenses(text: str) -> list:
    prompt = f"""Ты помощник по финансовому учёту. Из текста извлеки ВСЕ траты — их может быть одна или несколько.

Текст: "{text}"

Категории: {", ".join(CATEGORIES)}

Верни ТОЛЬКО JSON массив без лишнего текста и без markdown-тиков. Даже если трата одна — верни массив с одним элементом:
[
  {{"amount": <число>, "category": "<одна из категорий>", "description": "<краткое описание 2-5 слов>"}},
  {{"amount": <число>, "category": "<одна из категорий>", "description": "<краткое описание 2-5 слов>"}}
]

Правила:
- amount: только число без знаков валюты, никогда не null
- Если сумма не указана — не включай эту трату
- description: очень кратко суть покупки
- Топливо, бензин, заправка → категория Транспорт
- Аптека, лекарства → категория Здоровье / аптека
- Снюс, сигареты, вейп → категория Никотин"""

    response = groq_client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=500,
        temperature=0.1
    )
    raw = response.choices[0].message.content.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    result = json.loads(raw)
    if isinstance(result, dict):
        result = [result]
    return result


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[KeyboardButton("📊 Статистика")]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(
        "👋 Привет! Я твій *бесплатный* фінансовий бот.\n\n"
        "🎙 *Як користуватись:*\n"
        "Відправ голосове або напиши текстом — одну або кілька трат одразу:\n"
        "• «Снюс 800»\n"
        "• «Аптека 1400, топливо 650»\n"
        "• «Продукти 320, таксі 180, кіно 250»\n\n"
        "📊 Натисни *Статистика* щоб побачити зведення.",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Завантажую статистику...")
    try:
        stats = get_stats()
        if not stats:
            await update.message.reply_text("📭 Поки немає записів. Відправ голосове про витрату!")
            return
        lines = [f"📊 *Статистика витрат* ({stats['count']} записів)\n"]
        for cat, amt in sorted(stats["by_category"].items(), key=lambda x: -x[1]):
            emoji = EMOJI_MAP.get(cat, "📦")
            lines.append(f"{emoji} {cat}: *{amt:,.0f} ₴*")
        lines.append(f"\n💰 *Разом: {stats['total']:,.0f} ₴*")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Stats error: {e}")
        await update.message.reply_text("❌ Помилка при завантаженні статистики.")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎙 Розпізнаю голосове...")
    try:
        voice = update.message.voice
        file = await context.bot.get_file(voice.file_id)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            await file.download_to_drive(tmp.name)
            tmp_path = tmp.name
        text = transcribe_audio(tmp_path)
        os.unlink(tmp_path)
        await update.message.reply_text(f"📝 Розпізнав: _{text}_", parse_mode="Markdown")
        await process_expense_text(update, text)
    except Exception as e:
        logger.error(f"Voice error: {e}")
        await update.message.reply_text("❌ Не вдалось розпізнати голосове. Спробуй ще раз.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "📊 Статистика":
        await stats_command(update, context)
        return
    await process_expense_text(update, text)


async def process_expense_text(update: Update, text: str):
    try:
        expenses = parse_expenses(text)

        if not expenses:
            await update.message.reply_text(
                "🤔 Не знайшов суму в повідомленні.\n"
                "Спробуй: «Снюс 800» або «Продукти 500, таксі 200»"
            )
            return

        date = datetime.now().strftime("%d.%m.%Y %H:%M")
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
            lines.append(f"\n💰 *Разом: {total:,.0f} ₴*")

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Process error: {e}")
        await update.message.reply_text("❌ Помилка при збереженні. Спробуй ще раз.")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("Бот запущен! (бесплатная версия)")
    app.run_polling()


if __name__ == "__main__":
    main()
