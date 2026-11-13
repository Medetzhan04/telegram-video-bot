import os
import re
import uuid
import asyncio
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import (
    FSInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery
)
from aiogram.utils.chat_action import ChatActionSender
import yt_dlp
from aiohttp import web

# Токен берется автоматически из секретного сейфа сервера
TOKEN = os.getenv("BOT_TOKEN")
bot = Bot(token=TOKEN)
dp = Dispatcher()

# Временная память для ссылок, пока пользователь выбирает кнопку
url_storage = {}

# --- ВЕБ-СЕРВЕР ДЛЯ ОБЛАКА (чтобы Render видел, что бот жив) ---
async def health_check(request):
    return web.Response(text="Bot is alive and running 24/7!")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

# --- КОМАНДА /start ---
@dp.message(CommandStart())
async def start_cmd(message: types.Message):
    await message.answer(
        "🚀 <b>Привет! Я твой личный загрузчик медиа.</b>\n\n"
        "Просто отправь мне ссылку из:\n"
        "• <b>TikTok</b> (без водяного знака)\n"
        "• <b>Instagram</b> (Reels / Посты)\n"
        "• <b>YouTube</b> (Shorts и обычные видео)\n"
        "• <b>Pinterest, X (Twitter), Reddit, VK</b>\n\n"
        "Я предложу скачать <b>Видео (MP4)</b> или вырезать <b>Музыку (MP3)</b>! 👇",
        parse_mode="HTML"
    )

# --- ФУНКЦИЯ СКАЧИВАНИЯ (ВИДЕО ИЛИ АУДИО) ---
def download_media(url: str, base_filename: str, mode: str) -> dict:
    common_opts = {
        'quiet': True,
        'noplaylist': True,
        'no_color': True,
        'geo_bypass': True,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        }
    }

    if mode == "audio":
        ydl_opts = {
            **common_opts,
            'format': 'bestaudio/best',
            'outtmpl': f"{base_filename}.%(ext)s",
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
        }
    else:
        ydl_opts = {
            **common_opts,
            'format': 'best[ext=mp4][filesize<50M]/bestvideo[ext=mp4][filesize<40M]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            'outtmpl': f"{base_filename}.%(ext)s",
            'merge_output_format': 'mp4',
        }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        title = info.get('title', 'Медиафайл')
        uploader = info.get('uploader', '')

        # Находим точное имя итогового файла
        final_file = f"{base_filename}.mp3" if mode == "audio" else f"{base_filename}.mp4"
        if not os.path.exists(final_file):
            for f in os.listdir('.'):
                if f.startswith(base_filename):
                    final_file = f
                    break

        return {"file": final_file, "title": title, "uploader": uploader}

# --- ЛОВИМ ССЫЛКУ И ПОКАЗЫВАЕМ КНОПКИ ---
@dp.message(F.text)
async def handle_link(message: types.Message):
    # Ищем ссылку даже внутри длинного текста
    match = re.search(r'(https?://[^\s]+)', message.text)
    if not match:
        await message.answer("🔗 Отправь мне ссылку на ролик (начинается с http:// или https://)")
        return

    url = match.group(1)
    link_id = str(uuid.uuid4())[:8]
    url_storage[link_id] = url

    # Очистка памяти, если накопилось больше 500 ссылок
    if len(url_storage) > 500:
        url_storage.pop(next(iter(url_storage)))

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎬 Видео (MP4)", callback_data=f"dl:video:{link_id}"),
            InlineKeyboardButton(text="🎵 Звук (MP3)", callback_data=f"dl:audio:{link_id}")
        ]
    ])

    await message.answer(
        "✨ <b>Ссылка найдена!</b> В каком формате скачиваем?",
        reply_markup=keyboard,
        parse_mode="HTML"
    )

# --- НАЖАТИЕ НА КНОПКУ ---
@dp.callback_query(F.data.startswith("dl:"))
async def process_download(callback: CallbackQuery):
    _, mode, link_id = callback.data.split(":")
    url = url_storage.get(link_id)

    if not url:
        await callback.message.edit_text("⚠️ Ссылка устарела. Отправь её ещё раз!")
        return

    await callback.answer()
    action_text = "🎵 Вырезаю аудио (MP3)..." if mode == "audio" else "⏳ Скачиваю видео в лучшем качестве..."
    await callback.message.edit_text(action_text)

    base_filename = f"media_{callback.from_user.id}_{link_id}"
    final_path = None

    try:
        result = await asyncio.to_thread(download_media, url, base_filename, mode)
        final_path = result["file"]
        title = result["title"][:100]
        uploader = result["uploader"]

        if not os.path.exists(final_path):
            await callback.message.edit_text("❌ Не удалось получить файл по этой ссылке.")
            return

        # Проверка лимита Telegram (50 МБ)
        file_size_mb = os.path.getsize(final_path) / (1024 * 1024)
        if file_size_mb > 49.5:
            await callback.message.edit_text(
                f"⚠️ Файл слишком большой ({file_size_mb:.1f} МБ).\n"
                "Telegram разрешает ботам отправлять файлы только до 50 МБ."
            )
            return

        await callback.message.edit_text("📤 Отправляю в чат...")
        media_file = FSInputFile(final_path)

        caption = f"🎬 <b>{title}</b>"
        if uploader:
            caption += f"\n👤 Автор: {uploader}"

        if mode == "audio":
            async with ChatActionSender.upload_voice(bot=bot, chat_id=callback.message.chat.id):
                await callback.message.answer_audio(
                    media_file,
                    title=title,
                    performer=uploader or "Audio",
                    caption="🎵 Аудиодорожка готова!"
                )
        else:
            async with ChatActionSender.upload_video(bot=bot, chat_id=callback.message.chat.id):
                await callback.message.answer_video(
                    media_file,
                    caption=caption,
                    parse_mode="HTML",
                    supports_streaming=True
                )

        await callback.message.delete()

    except Exception as e:
        error_short = str(e)[:250]
        await callback.message.edit_text(
            f"❌ <b>Не удалось скачать.</b>\n"
            f"Возможно, аккаунт закрыт или видео недоступно.\n\n"
            f"<code>{error_short}</code>",
            parse_mode="HTML"
        )
    finally:
        # Удаляем за собой все временные файлы с диска
        for f in os.listdir('.'):
            if f.startswith(base_filename):
                try:
                    os.remove(f)
                except OSError:
                    pass

async def main():
    await start_web_server()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())