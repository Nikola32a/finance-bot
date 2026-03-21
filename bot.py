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


def parse_expense(text: str) -> dict:
    prompt = f"""Ты помощник по финансовому учёту. Из текста извлеки информацию о трате.

Текст: "{text}"

Категории: {", ".join(CATEGORIES)}

Верни ТОЛЬКО JSON без лишнего текста и без markdown-тиков:
{{"amount": <число или null>, "category": "<одна из категорий>", "description": "<краткое описание 2-5 слов>"}}

Правила:
- amount: только число без знаков валюты
- Если сумма не упомянута — null
- description: очень кратко суть покупки"""

    response = groq_client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=200,
        temperature=0.1
    )
    raw = response.choices[0].message.content.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(raw)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[KeyboardButton("📊 Статистика")]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(
        "👋 Привет! Я твой *бесплатный* финансовый бот.\n\n"
        "🎙 *Как пользоваться:*\n"
        "Отправь голосовое сообщение или напиши текстом:\n"
        "• «Потратил 350 рублей на обед»\n"
        "• «Такси 280 рублей»\n"
        "• «Аптека, купил витамины за 600»\n\n"
        "📊 Нажми *Статистика* чтобы увидеть сводку.",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )


 async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❓ *Помощь*\n\n"
        "Говори или пиши о своих тратах:\n"
        "• «Купил продукты на 1200»\n"
        "• «Бензин 2500 рублей»\n"
        "• «Кино с друзьями 800 р»\n\n"
        "Категории:\n"
        "🍔 Еда / продукты\n"
        "🚗 Транспорт\n"
        "🎮 Развлечения\n"
        "💊 Здоровье / аптека\n"
        "📦 Другое",
        parse_mode="Markdown"
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Загружаю статистику...")
    try:
        stats = get_stats()
        if not stats:
            await update.message.reply_text("📭 Пока нет записей. Отправь голосовое о трате!")
            return
        emoji_map = {
            "Еда / продукты": "🍔", "Транспорт": "🚗",
            "Развлечения": "🎮", "Здоровье / аптека": "💊", "Никотин": "🚬", "Другое": "📦"
        }
        lines = [f"📊 *Статистика трат* ({stats['count']} записей)\n"]
        for cat, amt in sorted(stats["by_category"].items(), key=lambda x: -x[1]):
            emoji = emoji_map.get(cat, "📦")
            lines.append(f"{emoji} {cat}: *{amt:,.0f} ₴*")
        lines.append(f"\n💰 *Итого: {stats['total']:,.0f} ₴*")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Stats error: {e}")
        await update.message.reply_text("❌ Ошибка при загрузке статистики.")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎙 Распознаю голосовое...")
    try:
        voice = update.message.voice
        file = await context.bot.get_file(voice.file_id)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            await file.download_to_drive(tmp.name)
            tmp_path = tmp.name
        text = transcribe_audio(tmp_path)
        os.unlink(tmp_path)
        await update.message.reply_text(f"📝 Распознал: _{text}_", parse_mode="Markdown")
        await process_expense_text(update, text)
    except Exception as e:
        logger.error(f"Voice error: {e}")
        await update.message.reply_text("❌ Не удалось распознать голосовое. Попробуй ещё раз.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "📊 Статистика":
        await stats_command(update, context)
        return
    if text == "❓ Помощь":
        await help_command(update, context)
        return
    await process_expense_text(update, text)


async def process_expense_text(update: Update, text: str):
    try:
        parsed = parse_expense(text)
        if not parsed.get("amount"):
            await update.message.reply_text(
                "🤔 Не нашёл сумму в сообщении.\n"
                "Попробуй: «Потратил 500 рублей на продукты»"
            )
            return
        date = datetime.now().strftime("%d.%m.%Y %H:%M")
        amount = float(parsed["amount"])
        category = parsed.get("category", "Другое")
        description = parsed.get("description", "—")
        save_expense(date, amount, category, description, text)
        emoji_map = {
            "Еда / продукты": "🍔", "Транспорт": "🚗",
            "Развлечения": "🎮", "Здоровье / аптека": "💊", "Другое": "📦"
        }
        emoji = emoji_map.get(category, "📦")
        await update.message.reply_text(
            f"✅ *Записано!*\n\n"
            f"{emoji} {category}\n"
            f"💸 *{amount:,.0f} ₴*\n"
            f"📌 {description}",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Process error: {e}")
        await update.message.reply_text("❌ Ошибка при сохранении. Попробуй ещё раз.")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("Бот запущен! (бесплатная версия)")
    app.run_polling()


if __name__ == "__main__":
    main()
