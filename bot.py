"""
ربات مشاور روانشناسی - نسخه Webhook برای Railway
گفتگوی همدلانه با هشدار عدم جایگزینی درمان
"""

import os
import asyncio
import logging
from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from openai import AsyncOpenAI
import aiosqlite

# ============================================================
# ۱. پیکربندی
# ============================================================
# ⚠️ این‌ها را با مقادیر واقعی خودت جایگزین کن
BOT_TOKEN = "8690919773:AAGAN1rWpMZ8Vd2wPQQYqH0-oe84np3Zmlg"
LLM_API_KEY = "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"
LLM_BASE_URL = "https://openrouter.ai/api/v1"
LLM_MODEL = "deepseek/deepseek-r1:free"

# دامنه Railway — بعد از Generate Domain این مقدار به صورت خودکار پر می‌شود
# اگر خالی باشد، از متغیر محیطی RAILWAY_PUBLIC_DOMAIN خوانده می‌شود
RAILWAY_DOMAIN = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
WEBHOOK_URL = f"https://{RAILWAY_DOMAIN}" if RAILWAY_DOMAIN else ""

# مسیر و پورت
WEBHOOK_PATH = "/webhook"
WEB_SERVER_HOST = "0.0.0.0"
WEB_SERVER_PORT = int(os.getenv("PORT", 8080))

DB_PATH = "bot.db"

EMERGENCY_NUMBERS = """
📞 **خطوط اضطراری:**
- اورژانس اجتماعی: **۱۲۳**
- اورژانس: **۱۱۵**
- صدای مشاور: **۱۴۸۰**
"""

DISCLAIMER = (
    "⚠️ من یک دستیار هوش مصنوعی هستم و **جایگزین روانشناس یا روانپزشک نیستم**.\n"
    "هدف من ایجاد یک فضای امن برای گفتگو و ارائه تمرین‌های اولیه است.\n"
    "در مواقع بحرانی، لطفاً با خطوط اضطراری تماس بگیرید."
)

SYSTEM_PROMPT = """تو یک دستیار همدل و حامی هستی که در یک ربات تلگرامی با کاربران گفتگو می‌کنی.

شخصیت تو:
- درک‌کننده، دلسوز، دوستانه، همدل، تشویق‌کننده و کاملاً غیرقضاوت‌گر
- هرگز تشخیص پزشکی نمی‌دهی و دارو تجویز نمی‌کنی
- همیشه به کاربر یادآوری می‌کنی که جایگزین درمانگر واقعی نیستی

اصول گفتگو:
1. ابتدا احساس کاربر را بازتاب بده (Reflection)
2. احساسات او را معتبر بدان (Validation)
3. از سوالات باز استفاده کن
4. از توصیه سریع و کلیشه‌ای مثل «نگران نباش» پرهیز کن
5. پاسخ‌ها کوتاه، گرم و انسانی باشند (حداکثر ۳-۴ پاراگراف)
6. اگر کاربر نشانه‌های بحران داشت، فوراً او را به خطوط اضطراری ارجاع بده

همیشه به فارسی روان و صمیمی پاسخ بده.
"""

CRISIS_KEYWORDS = [
    "خودکشی", "خودم را بکشم", "تمومش کنم", "تمامش کنم",
    "نمیخوام زنده بمونم", "نمی‌خوام زنده بمونم",
    "بهتره بمیرم", "آسیب به خودم", "خودزنی", "خود آزاری"
]

# ============================================================
# ۲. کلاینت مدل زبانی
# ============================================================
llm_client = AsyncOpenAI(
    api_key=LLM_API_KEY,
    base_url=LLM_BASE_URL,
)


async def get_response(history: list[dict]) -> str:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history
    try:
        response = await llm_client.chat.completions.create(
            model=LLM_MODEL,
            messages=messages,
            temperature=0.8,
            max_tokens=500,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logging.error(f"LLM error: {e}")
        return "متأسفم، الان نمی‌توانم پاسخ دهم. لطفاً بعداً تلاش کن. 💙"


# ============================================================
# ۳. دیتابیس
# ============================================================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()


async def save_message(user_id: int, role: str, content: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO messages (user_id, role, content) VALUES (?, ?, ?)",
            (user_id, role, content)
        )
        await db.commit()


async def get_history(user_id: int, limit: int = 10) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT role, content FROM messages WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit)
        )
        rows = await cursor.fetchall()
        return [{"role": r[0], "content": r[1]} for r in reversed(rows)]


async def clear_history(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM messages WHERE user_id = ?", (user_id,))
        await db.commit()


# ============================================================
# ۴. تشخیص بحران
# ============================================================
def is_crisis(text: str) -> bool:
    normalized = text.replace(" ", "").replace("\u200c", "")
    return any(kw.replace(" ", "") in normalized for kw in CRISIS_KEYWORDS)


# ============================================================
# ۵. ربات و هندلرها
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        f"سلام 🌱\n\n"
        f"من اینجا هستم تا در یک فضای امن و بدون قضاوت به حرفت گوش بدم.\n\n"
        f"{DISCLAIMER}\n\n"
        f"هر وقت آماده بودی، فقط بنویس چه چیزی ذهنت را مشغول کرده."
    )


@dp.message(Command("clear"))
async def cmd_clear(message: Message):
    await clear_history(message.from_user.id)
    await message.answer("تاریخچه گفتگوی ما پاک شد. از نو شروع می‌کنیم. 🌿")


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "دستورات موجود:\n"
        "/start - شروع مجدد\n"
        "/clear - پاک کردن تاریخچه\n"
        "/help - راهنما\n\n"
        "فقط کافیه احساسات یا افکارت رو بنویسی. من اینجام تا گوش بدم. 💙"
    )


@dp.message(F.text)
async def handle_message(message: Message):
    user_id = message.from_user.id
    text = message.text.strip()

    # ۱. بررسی بحران
    if is_crisis(text):
        await save_message(user_id, "user", text)
        crisis_reply = (
            "💙 می‌شنوم که الان خیلی سختی می‌کشی و این خیلی سنگین است.\n"
            "تو تنها نیستی و ارزشمند هستی. لطفاً همین الان با یکی از این شماره‌ها تماس بگیر:\n\n"
            f"{EMERGENCY_NUMBERS}\n\n"
            "من اینجا هستم، اما یک انسان واقعی می‌تواند بهتر از من کنارت باشد."
        )
        await message.answer(crisis_reply)
        await save_message(user_id, "assistant", crisis_reply)
        return

    # ۲. ذخیره پیام کاربر
    await save_message(user_id, "user", text)

    # ۳. ارسال "در حال تایپ" و گرفتن پاسخ
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    history = await get_history(user_id, limit=10)
    reply = await get_response(history)

    # ۴. ذخیره و ارسال پاسخ
    await save_message(user_id, "assistant", reply)
    await message.answer(reply)


# ============================================================
# ۶. Webhook و سرور
# ============================================================
async def on_startup(bot: Bot):
    if WEBHOOK_URL:
        await bot.set_webhook(f"{WEBHOOK_URL}{WEBHOOK_PATH}")
        logging.info(f"✅ Webhook set to {WEBHOOK_URL}{WEBHOOK_PATH}")
    else:
        logging.warning("⚠️ RAILWAY_PUBLIC_DOMAIN تنظیم نشده. Webhook ثبت نشد.")


async def on_shutdown(bot: Bot):
    await bot.delete_webhook()
    logging.info("Webhook deleted.")


def main():
    # مقداردهی دیتابیس
    asyncio.run(init_db())

    # ساخت اپلیکیشن aiohttp
    app = web.Application()

    # ثبت هندلر Webhook
    webhook_requests_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
    )
    webhook_requests_handler.register(app, path=WEBHOOK_PATH)

    # مسیر سلامت (برای Railway)
    async def health(request):
        return web.Response(text="OK")

    app.router.add_get("/", health)

    # ثبت رویدادهای startup و shutdown
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    # اجرا
    setup_application(app, dp, bot=bot)
    logging.info(f"🚀 Starting web server on {WEB_SERVER_HOST}:{WEB_SERVER_PORT}")
    web.run_app(app, host=WEB_SERVER_HOST, port=WEB_SERVER_PORT)


if __name__ == "__main__":
    main()
