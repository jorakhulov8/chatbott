import asyncio
import io
import logging
import os

import google.generativeai as genai
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message
from aiohttp import web
from dotenv import load_dotenv
from PIL import Image

# ==========================================================
# 1) SOZLAMALAR
# ==========================================================

load_dotenv()  # lokal ishga tushirishda .env fayldan o'qiydi

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not TELEGRAM_TOKEN or not GEMINI_API_KEY:
    raise RuntimeError(
        "TELEGRAM_TOKEN yoki GEMINI_API_KEY topilmadi! "
        "Lokalda .env faylga, Render'da esa Environment Variables bo'limiga qo'shing."
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("gemini-bot")

genai.configure(api_key=GEMINI_API_KEY)

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

# Har bir foydalanuvchi uchun suhbat tarixi (RAM ichida, oddiy dict)
user_chats: dict[int, list] = {}

# ==========================================================
# 2) GEMINI MODEL BOSHQARUVI (fallback bilan)
# ==========================================================

# Google modellarni tez-tez o'zgartirib/eskirtirib turadi,
# shuning uchun bir nechta variantni ketma-ket sinaymiz.
CANDIDATE_MODELS = [
    "gemini-1.5-flash",
    "gemini-1.5-pro",
    "gemini-1.0-pro",
]

_working_model_name: str | None = None


def get_model() -> genai.GenerativeModel:
    """Ishlaydigan Gemini modelini qaytaradi.

    Avval oldin muvaffaqiyatli ishlagan modelni ishlatadi.
    Agar hali sinalmagan bo'lsa yoki joriy model ishlamay qolsa,
    ro'yxat bo'yicha birma-bir sinaydi.
    """
    global _working_model_name

    if _working_model_name:
        return genai.GenerativeModel(_working_model_name)

    last_error: Exception | None = None
    for name in CANDIDATE_MODELS:
        try:
            candidate = genai.GenerativeModel(name)
            candidate.generate_content("salom")  # kichik test so'rov
            _working_model_name = name
            log.info("Gemini modeli tanlandi: %s", name)
            return candidate
        except Exception as e:
            last_error = e
            log.warning("Model ishlamadi (%s): %s", name, e)
            continue

    raise RuntimeError(f"Hech qaysi Gemini modeli ishlamadi: {last_error}")


def reset_model() -> None:
    """Joriy model xato bersa, uni tashlab, keyingisini sinash uchun."""
    global _working_model_name
    _working_model_name = None


async def ask_gemini(fn, *args, **kwargs):
    """Gemini chaqiruvini bajaradi, birinchi model ishlamasa keyingisiga o'tadi."""
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except Exception:
        reset_model()
        raise


# ==========================================================
# 3) TELEGRAM HANDLERLAR
# ==========================================================


@dp.message(CommandStart())
async def cmd_start(message: Message):
    user_chats[message.from_user.id] = []
    await message.answer(
        "Salom! Men Gemini AI asosida ishlovchi botman.\n"
        "Menga xohlagan savolingizni yozing yoki rasm yuboring.\n\n"
        "/reset — suhbat tarixini tozalash"
    )


@dp.message(Command("reset"))
async def cmd_reset(message: Message):
    user_chats[message.from_user.id] = []
    await message.answer("Suhbat tarixi tozalandi.")


@dp.message(F.photo)
async def handle_photo(message: Message):
    caption = message.caption or "Bu rasmda nima ko'rinyapti? Batafsil tushuntir."

    await bot.send_chat_action(message.chat.id, "typing")

    try:
        photo = message.photo[-1]  # eng yuqori sifatdagi variant
        file = await bot.get_file(photo.file_id)
        file_bytes = await bot.download_file(file.file_path)
        image = Image.open(io.BytesIO(file_bytes.read()))

        model = get_model()
        try:
            response = await ask_gemini(model.generate_content, [caption, image])
        except Exception:
            model = get_model()
            response = await ask_gemini(model.generate_content, [caption, image])

        answer = response.text or "Kechirasiz, javob bo'sh keldi."
        for i in range(0, len(answer), 4000):
            await message.answer(answer[i : i + 4000])

    except Exception as e:
        log.exception("Gemini rasm xatosi")
        await message.answer(f"Rasmni tahlil qilishda xatolik: {e}")


@dp.message(F.text)
async def handle_text(message: Message):
    user_id = message.from_user.id
    text = message.text

    user_chats.setdefault(user_id, [])

    await bot.send_chat_action(message.chat.id, "typing")

    try:
        model = get_model()
        try:
            chat = model.start_chat(history=user_chats[user_id])
            response = await ask_gemini(chat.send_message, text)
        except Exception:
            model = get_model()
            chat = model.start_chat(history=user_chats[user_id])
            response = await ask_gemini(chat.send_message, text)

        answer = response.text or "Kechirasiz, javob bo'sh keldi."
        user_chats[user_id] = chat.history

        # Telegram xabar uzunligi cheklovi (4096 belgi)
        for i in range(0, len(answer), 4000):
            await message.answer(answer[i : i + 4000])

    except Exception as e:
        log.exception("Gemini matn xatosi")
        await message.answer(f"Xatolik yuz berdi: {e}")


# ==========================================================
# 4) RENDER UCHUN WEB SERVER (uyg'oq turish uchun ping manzili)
# ==========================================================


async def handle_ping(request):
    return web.Response(text="OK")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("Web server ishga tushdi: 0.0.0.0:%s", port)


# ==========================================================
# 5) ISHGA TUSHIRISH
# ==========================================================


async def main():
    await start_web_server()
    log.info("Bot polling boshlandi...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
