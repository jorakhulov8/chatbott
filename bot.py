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

# .env faylidan tokenlarni o'qish
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not TELEGRAM_TOKEN or not GEMINI_API_KEY:
    raise RuntimeError("TELEGRAM_TOKEN yoki GEMINI_API_KEY .env faylida topilmadi!")

# Gemini sozlash
genai.configure(api_key=GEMINI_API_KEY)

# Google modellarni tez-tez o'zgartirib/eskirtirib turadi.
# Shuning uchun bir nechta nomni ro'yxat qilib qo'yamiz — birinchisi
# ishlamay qolsa (404/eskirgan bo'lsa), bot avtomatik keyingisiga o'tadi.
CANDIDATE_MODELS = [
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash-lite",
    "gemini-flash-latest",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
]

# Hozircha "ishlayotgan" deb topilgan model shu yerda saqlanadi
_working_model_name: str | None = None


def get_model():
    """Ishlaydigan Gemini modelini qaytaradi.

    Avval oldin muvaffaqiyatli ishlagan modelni ishlatadi.
    Agar hali hech qaysi model sinalmagan bo'lsa yoki
    joriy model ishlamay qolsa, ro'yxat bo'yicha birma-bir sinaydi.
    """
    global _working_model_name

    if _working_model_name:
        return genai.GenerativeModel(_working_model_name)

    last_error = None
    for name in CANDIDATE_MODELS:
        try:
            candidate = genai.GenerativeModel(name)
            # Kichik test so'rov bilan model ishlashini tekshiramiz
            candidate.generate_content("salom")
            _working_model_name = name
            logging.info(f"Gemini modeli tanlandi: {name}")
            return candidate
        except Exception as e:
            last_error = e
            logging.warning(f"Model ishlamadi ({name}): {e}")
            continue

    raise RuntimeError(f"Hech qaysi Gemini modeli ishlamadi: {last_error}")


def reset_model():
    """Joriy model xato bersa, uni ro'yxatdan o'chirib, keyingisini sinash uchun."""
    global _working_model_name
    _working_model_name = None

# Har bir foydalanuvchi uchun suhbat tarixini saqlash (oddiy dict, RAM ichida)
user_chats: dict[int, list] = {}

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()


@dp.message(CommandStart())
async def cmd_start(message: Message):
    user_chats[message.from_user.id] = []
    await message.answer(
        "Salom! Men Gemini AI asosida ishlovchi botman.\n"
        "Menga xohlagan savolingizni yozing.\n\n"
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
        # Eng yuqori sifatdagi rasmni olish
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        file_bytes = await bot.download_file(file.file_path)
        image = Image.open(io.BytesIO(file_bytes.read()))

        try:
            model = get_model()
            response = await asyncio.to_thread(
                model.generate_content, [caption, image]
            )
        except Exception:
            # Model ishlamadi — keyingisini sinaymiz
            reset_model()
            model = get_model()
            response = await asyncio.to_thread(
                model.generate_content, [caption, image]
            )

        answer = response.text

        for i in range(0, len(answer), 4000):
            await message.answer(answer[i:i + 4000])

    except Exception as e:
        logging.exception("Gemini rasm xatosi")
        await message.answer(f"Rasmni tahlil qilishda xatolik: {e}")


@dp.message(F.text)
async def handle_text(message: Message):
    user_id = message.from_user.id
    text = message.text

    if user_id not in user_chats:
        user_chats[user_id] = []

    # "yozmoqda..." holatini ko'rsatish
    await bot.send_chat_action(message.chat.id, "typing")

    try:
        try:
            model = get_model()
            chat = model.start_chat(history=user_chats[user_id])
            response = await asyncio.to_thread(chat.send_message, text)
        except Exception:
            # Model ishlamadi — keyingisini sinaymiz, tarixni saqlagan holda
            reset_model()
            model = get_model()
            chat = model.start_chat(history=user_chats[user_id])
            response = await asyncio.to_thread(chat.send_message, text)

        answer = response.text

        # Tarixni yangilash (keyingi savollarda kontekst saqlanishi uchun)
        user_chats[user_id] = chat.history

        # Telegram xabar uzunligi cheklovi (4096 belgi)
        for i in range(0, len(answer), 4000):
            await message.answer(answer[i:i + 4000])

    except Exception as e:
        logging.exception("Gemini xatosi")
        await message.answer(f"Xatolik yuz berdi: {e}")


async def handle_ping(request):
    # UptimeRobot shu manzilga ping yuborib, botni "uyg'oq" ushlab turadi
    return web.Response(text="OK")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()


async def main():
    await start_web_server()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
