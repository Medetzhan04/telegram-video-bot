import os
import re
import uuid
import shutil
import asyncio
import subprocess
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import (
    FSInputFile,
     InputMediaPhoto,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery
)
from aiogram.utils.chat_action import ChatActionSender
import yt_dlp
from aiohttp import web

TOKEN = os.getenv("BOT_TOKEN")
bot = Bot(token=TOKEN)
dp = Dispatcher()

url_storage = {}

# --- ВЕБ-СЕРВЕР ---
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
        "🚀 <b>Привет! Я твой универсальный загрузчик медиа.</b>\n\n"
        "Поддерживаю <b>1000+ сайтов</b>:\n"
        "• <b>TikTok</b> (Видео + Фото-карусели + Музыка)\n"
        "• <b>Instagram</b> (Reels, Посты, Карусели)\n"
        "• <b>YouTube, VK, Pinterest, X (Twitter), Reddit</b>\n\n"
        "Кидай любую ссылку! 👇",
        parse_mode="HTML"
    )

# --- СКАЧИВАНИЕ ФОТО-КАРУСЕЛЕЙ (ЧЕРЕЗ GALLERY-DL) ---
def download_carousel(url: str, folder_name: str) -> dict:
    os.makedirs(folder_name, exist_ok=True)
    # Вызываем gallery-dl для скачивания всех картинок и звука из поста
    cmd = [
        "gallery-dl",
        "--directory", folder_name,
        "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        url
    ]
    subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)

    images = []
    audios = []
    videos = []

    for root, _, files in os.walk(folder_name):
        for f in sorted(files):
            full_path = os.path.join(root, f)
            ext = f.lower().split('.')[-1]
            if ext in ('jpg', 'jpeg', 'png', 'webp'):
                images.append(full_path)
            elif ext in ('mp3', 'm4a', 'wav', 'ogg'):
                audios.append(full_path)
            elif ext in ('mp4', 'webm', 'mov'):
                videos.append(full_path)

    return {"images": images, "audios": audios, "videos": videos}

# --- СКАЧИВАНИЕ ВИДЕО И АУДИО (ЧЕРЕЗ YT-DLP) ---
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

        final_file = f"{base_filename}.mp3" if mode == "audio" else f"{base_filename}.mp4"
        if not os.path.exists(final_file):
            for f in os.listdir('.'):
                if f.startswith(base_filename):
                    final_file = f
                    break

        return {"file": final_file, "title": title, "uploader": uploader}

# --- ЛОВИМ ССЫЛКУ ---
@dp.message(F.text)
async def handle_link(message: types.Message):
    match = re.search(r'(https?://[^\s]+)', message.text)
    if not match:
        await message.answer("🔗 Отправь мне ссылку на ролик или пост (начинается с http:// или https://)")
        return

    url = match.group(1)
    link_id = str(uuid.uuid4())[:8]
    url_storage[link_id] = url

    if len(url_storage) > 500:
        url_storage.pop(next(iter(url_storage)))

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎬 Видео / 📸 Фото", callback_data=f"dl:video:{link_id}"),
            InlineKeyboardButton(text="🎵 Только звук (MP3)", callback_data=f"dl:audio:{link_id}")
        ]
    ])

    await message.answer(
        "✨ <b>Ссылка принята!</b> Что скачиваем?",
        reply_markup=keyboard,
        parse_mode="HTML"
    )

# --- ОБРАБОТКА НАЖАТИЯ КНОПКИ ---
@dp.callback_query(F.data.startswith("dl:"))
async def process_download(callback: CallbackQuery):
    _, mode, link_id = callback.data.split(":")
    url = url_storage.get(link_id)

    if not url:
        await callback.message.edit_text("⚠️ Ссылка устарела. Отправь её ещё раз!")
        return

    await callback.answer()
    action_text = "🎵 Вырезаю аудио (MP3)..." if mode == "audio" else "⏳ Загружаю медиа (видео или фото-карусель)..."
    await callback.message.edit_text(action_text)

    base_filename = f"media_{callback.from_user.id}_{link_id}"
    carousel_folder = f"carousel_{callback.from_user.id}_{link_id}"

    try:
        # Если ссылка явно ведет на фото-пост TikTok (/photo/) или yt-dlp не справился — качаем карусель
        is_tiktok_photo = "/photo/" in url

        if mode == "video" and not is_tiktok_photo:
            try:
                result = await asyncio.to_thread(download_media, url, base_filename, mode)
                final_path = result["file"]

                # Если yt-dlp скачал только аудио (так бывает на слайд-шоу TikTok), переключаемся на карусель
                if os.path.exists(final_path) and not final_path.endswith(('.mp3', '.m4a')):
                    file_size_mb = os.path.getsize(final_path) / (1024 * 1024)
                    if file_size_mb > 49.5:
                        await callback.message.edit_text(f"⚠️ Файл слишком большой ({file_size_mb:.1f} МБ). Лимит Telegram — 50 МБ.")
                        return

                    await callback.message.edit_text("📤 Отправляю видео в чат...")
                    caption = f"🎬 <b>{result['title'][:100]}</b>"
                    if result["uploader"]:
                        caption += f"\n👤 Автор: {result['uploader']}"

                    async with ChatActionSender.upload_video(bot=bot, chat_id=callback.message.chat.id):
                        await callback.message.answer_video(
                            FSInputFile(final_path),
                            caption=caption,
                            parse_mode="HTML",
                            supports_streaming=True
                        )
                    await callback.message.delete()
                    return
            except Exception:
                # Если yt-dlp выдал ошибку на видео, пробуем вытащить как фото-карусель!
                pass

        # Режим только аудио (MP3)
        if mode == "audio":
            try:
                result = await asyncio.to_thread(download_media, url, base_filename, "audio")
                final_path = result["file"]
                if os.path.exists(final_path):
                    await callback.message.edit_text("📤 Отправляю музыку...")
                    async with ChatActionSender.upload_voice(bot=bot, chat_id=callback.message.chat.id):
                        await callback.message.answer_audio(
                            FSInputFile(final_path),
                            title=result["title"][:100],
                            performer=result["uploader"] or "Audio",
                            caption="🎵 Аудиодорожка готова!"
                        )
                    await callback.message.delete()
                    return
            except Exception:
                pass

        # --- ЗАПУСК ЗАГРУЗЧИКА КАРУСЕЛЕЙ (GALLERY-DL) ---
        await callback.message.edit_text("📸 Обнаружена фото-карусель! Скачиваю слайды...")
        carousel_data = await asyncio.to_thread(download_carousel, url, carousel_folder)
        images = carousel_data["images"]
        audios = carousel_data["audios"]

        if mode == "audio" and audios:
            async with ChatActionSender.upload_voice(bot=bot, chat_id=callback.message.chat.id):
                await callback.message.answer_audio(FSInputFile(audios[0]), caption="🎵 Музыка из слайд-шоу!")
            await callback.message.delete()
            return

        if not images:
            await callback.message.edit_text("❌ Не удалось найти ни видео, ни фото по этой ссылке (возможно, аккаунт закрыт).")
            return

        await callback.message.edit_text(f"📤 Отправляю фото ({len(images)} шт.)...")

        # Telegram разрешает до 10 фото в одном альбоме, поэтому разбиваем по 10 штук
        async with ChatActionSender.upload_photo(bot=bot, chat_id=callback.message.chat.id):
            for i in range(0, len(images), 10):
                chunk = images[i:i + 10]
                if len(chunk) == 1:
                    await callback.message.answer_photo(FSInputFile(chunk[0]), caption="📸 Фото из поста")
                else:
                    media_group = [
                        InputMediaPhoto(
                            media=FSInputFile(img),
                            caption=f"📸 Карусель ({len(images)} фото)" if idx == 0 and i == 0 else None
                        )
                        for idx, img in enumerate(chunk)
                    ]
                    await callback.message.answer_media_group(media_group)

        # Если к карусели в TikTok была прикреплена музыка — отправляем её бонусом!
        if audios:
            async with ChatActionSender.upload_voice(bot=bot, chat_id=callback.message.chat.id):
                await callback.message.answer_audio(
                    FSInputFile(audios[0]),
                    caption="🎵 Фоновый звук из этой карусели"
                )

        await callback.message.delete()

    except Exception as e:
        await callback.message.edit_text(
            f"❌ <b>Ошибка при скачивании:</b>\n<code>{str(e)[:200]}</code>",
            parse_mode="HTML"
        )
    finally:
        # Чистим за собой все временные файлы и папки
        for f in os.listdir('.'):
            if f.startswith(base_filename):
                try:
                    os.remove(f)
                except OSError:
                    pass
        if os.path.exists(carousel_folder):
            shutil.rmtree(carousel_folder, ignore_errors=True)

async def main():
    await start_web_server()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
