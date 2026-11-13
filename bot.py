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
    InputMediaVideo,
    InputMediaAudio,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    ChosenInlineResult
)
from aiogram.utils.chat_action import ChatActionSender
import yt_dlp
import aiohttp
from aiohttp import web

TOKEN = os.getenv("BOT_TOKEN")
bot = Bot(token=TOKEN)
dp = Dispatcher()

url_storage = {}

# --- ВЕБ-СЕРВЕР ---
async def health_check(request):
    return web.Response(text="Bot v3.0 (Inline + Carousel) is running 24/7!")

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
    bot_info = await bot.get_me()
    await message.answer(
        "🚀 <b>Привет! Я твой универсальный загрузчик медиа (v3.0).</b>\n\n"
        "📥 <b>Как пользоваться в личке:</b>\n"
        "Просто отправь мне ссылку из TikTok, Instagram, YouTube, VK или Pinterest.\n\n"
        "⚡ <b>Как пользоваться в ЛЮБОМ другом чате (Inline-режим):</b>\n"
        f"Напиши в любом диалоге с другом:\n<code>@{bot_info.username} ссылка_на_ролик</code>\n"
        "и нажми на всплывающую кнопку!",
        parse_mode="HTML"
    )

# --- ПРЯМОЕ СКАЧИВАНИЕ ФОТО-КАРУСЕЛЕЙ TIKTOK ---
async def download_tiktok_carousel(url: str, folder_name: str) -> dict:
    os.makedirs(folder_name, exist_ok=True)
    images = []
    audios = []
    title = "TikTok Фото-карусель"

    api_url = "https://www.tikwm.com/api/"
    async with aiohttp.ClientSession() as session:
        async with session.post(api_url, data={"url": url, "hd": 1}) as resp:
            data = await resp.json()
            if data.get("code") == 0 and "data" in data:
                post_data = data["data"]
                title = post_data.get("title") or title
                img_urls = post_data.get("images", [])

                for idx, img_url in enumerate(img_urls):
                    img_path = os.path.join(folder_name, f"slide_{idx:03d}.jpg")
                    async with session.get(img_url) as img_resp:
                        if img_resp.status == 200:
                            with open(img_path, "wb") as f:
                                f.write(await img_resp.read())
                            images.append(img_path)

                music_url = post_data.get("music")
                if music_url:
                    audio_path = os.path.join(folder_name, "audio.mp3")
                    async with session.get(music_url) as aud_resp:
                        if aud_resp.status == 200:
                            with open(audio_path, "wb") as f:
                                f.write(await aud_resp.read())
                            audios.append(audio_path)

    return {"images": images, "audios": audios, "title": title}

# --- СКАЧИВАНИЕ КАРУСЕЛЕЙ ИЗ ДРУГИХ СОЦСЕТЕЙ ---
def download_other_carousel(url: str, folder_name: str) -> dict:
    os.makedirs(folder_name, exist_ok=True)
    cmd = [
        "gallery-dl",
        "--directory", folder_name,
        "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        url
    ]
    subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)

    images = []
    audios = []
    for root, _, files in os.walk(folder_name):
        for f in sorted(files):
            full_path = os.path.join(root, f)
            ext = f.lower().split('.')[-1]
            if ext in ('jpg', 'jpeg', 'png', 'webp'):
                images.append(full_path)
            elif ext in ('mp3', 'm4a', 'wav', 'ogg'):
                audios.append(full_path)

    return {"images": images, "audios": audios}

# --- СКАЧИВАНИЕ ВИДЕО И АУДИО (YT-DLP) ---
def download_media(url: str, base_filename: str, mode: str) -> dict:
    clean_url = url.replace("/photo/", "/video/") if "tiktok.com" in url else url

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
        info = ydl.extract_info(clean_url, download=True)
        title = info.get('title', 'Медиафайл')
        uploader = info.get('uploader', '')

        final_file = f"{base_filename}.mp3" if mode == "audio" else f"{base_filename}.mp4"
        if not os.path.exists(final_file):
            for f in os.listdir('.'):
                if f.startswith(base_filename):
                    final_file = f
                    break

        return {"file": final_file, "title": title, "uploader": uploader}

# ==========================================
# 🔥 INLINE MODE (ВЫЗОВ ЧЕРЕЗ @БОТ В ЧАТАХ)
# ==========================================
@dp.inline_query()
async def inline_link_handler(inline_query: InlineQuery):
    query_text = inline_query.query.strip()
    match = re.search(r'(https?://[^\s]+)', query_text)

    if not match:
        await inline_query.answer(
            results=[],
            switch_pm_text="Вставь ссылку после @имени_бота...",
            switch_pm_parameter="inline_help",
            cache_time=1
        )
        return

    url = match.group(1)
    link_id = str(uuid.uuid4())[:8]
    url_storage[link_id] = url

    # Кнопка-пустышка (обязательна, чтобы Telegram выдал inline_message_id для замены на видео)
    wait_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏳ Скачиваю и прикрепляю...", callback_data="noop")]
    ])

    results = [
        InlineQueryResultArticle(
            id=f"video:{link_id}",
            title="🎬 Отправить Видео сюда",
            description=f"Скачать и вставить ролик: {url[:40]}...",
            input_message_content=InputTextMessageContent(
                message_text="⏳ <b>Загружаю видео по ссылке...</b>\n<i>Подождите пару секунд, сообщение обновится автоматически!</i>",
                parse_mode="HTML"
            ),
            reply_markup=wait_kb
        ),
        InlineQueryResultArticle(
            id=f"audio:{link_id}",
            title="🎵 Отправить только Звук (MP3)",
            description=f"Вырезать аудио из: {url[:40]}...",
            input_message_content=InputTextMessageContent(
                message_text="🎵 <b>Вырезаю аудиодорожку (MP3)...</b>\n<i>Подождите пару секунд!</i>",
                parse_mode="HTML"
            ),
            reply_markup=wait_kb
        )
    ]

    await inline_query.answer(results, cache_time=1, is_personal=True)

@dp.chosen_inline_result()
async def process_inline_download(chosen_result: ChosenInlineResult):
    if not chosen_result.inline_message_id:
        return

    mode, link_id = chosen_result.result_id.split(":")
    url = url_storage.get(link_id)
    user_id = chosen_result.from_user.id
    inline_msg_id = chosen_result.inline_message_id

    if not url:
        await bot.edit_message_text(
            inline_message_id=inline_msg_id,
            text="⚠️ Ссылка устарела, попробуй ещё раз."
        )
        return

    base_filename = f"inline_{user_id}_{link_id}"

    try:
        result = await asyncio.to_thread(download_media, url, base_filename, mode)
        final_path = result["file"]
        title = result["title"][:100]
        uploader = result["uploader"]

        if not os.path.exists(final_path):
            await bot.edit_message_text(
                inline_message_id=inline_msg_id,
                text=f"❌ Не удалось скачать видео (<a href='{url}'>ссылка</a>).",
                parse_mode="HTML"
            )
            return

        # Загружаем файл через личку с пользователем, чтобы получить file_id от Telegram
        if mode == "audio":
            sent = await bot.send_audio(
                chat_id=user_id,
                audio=FSInputFile(final_path),
                title=title,
                performer=uploader or "Audio",
                disable_notification=True
            )
            file_id = sent.audio.file_id
            await sent.delete() # Сразу удаляем техническое сообщение из лички

            await bot.edit_message_media(
                inline_message_id=inline_msg_id,
                media=InputMediaAudio(
                    media=file_id,
                    caption=f"🎵 <b>{title}</b>",
                    parse_mode="HTML"
                )
            )
        else:
            sent = await bot.send_video(
                chat_id=user_id,
                video=FSInputFile(final_path),
                disable_notification=True
            )
            file_id = sent.video.file_id
            await sent.delete() # Сразу удаляем техническое сообщение из лички

            caption = f"🎬 <b>{title}</b>"
            if uploader:
                caption += f"\n👤 Автор: {uploader}"

            await bot.edit_message_media(
                inline_message_id=inline_msg_id,
                media=InputMediaVideo(
                    media=file_id,
                    caption=caption,
                    parse_mode="HTML"
                )
            )

    except Exception as e:
        await bot.edit_message_text(
            inline_message_id=inline_msg_id,
            text=(
                f"❌ <b>Не удалось загрузить в инлайн-режиме.</b>\n"
                f"💡 <i>Важно: чтобы инлайн-режим работал, нужно хотя бы раз нажать /start в личке у бота!</i>\n"
                f"Ссылка: {url}"
            ),
            parse_mode="HTML"
        )
    finally:
        for f in os.listdir('.'):
            if f.startswith(base_filename):
                try:
                    os.remove(f)
                except OSError:
                    pass

@dp.callback_query(F.data == "noop")
async def noop_cb(callback: CallbackQuery):
    await callback.answer("⏳ Видео уже качается, подожди пару секунд!", show_alert=False)

# ==========================================
# ОБЫЧНЫЙ РЕЖИМ В ЛИЧКЕ (ВИДЕО + КАРУСЕЛИ)
# ==========================================
@dp.message(F.text)
async def handle_link(message: types.Message):
    match = re.search(r'(https?://[^\s]+)', message.text)
    if not match:
        await message.answer("🔗 Отправь мне ссылку на ролик или фото-пост (начинается с http:// или https://)")
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

@dp.callback_query(F.data.startswith("dl:"))
async def process_download(callback: CallbackQuery):
    _, mode, link_id = callback.data.split(":")
    url = url_storage.get(link_id)

    if not url:
        await callback.message.edit_text("⚠️ Ссылка устарела. Отправь её ещё раз!")
        return

    await callback.answer()
    action_text = "🎵 Вырезаю аудио (MP3)..." if mode == "audio" else "⏳ Загружаю медиа..."
    await callback.message.edit_text(action_text)

    base_filename = f"media_{callback.from_user.id}_{link_id}"
    carousel_folder = f"carousel_{callback.from_user.id}_{link_id}"

    try:
        is_tiktok_photo = "tiktok.com" in url and "/photo/" in url

        if mode == "video" and not is_tiktok_photo:
            try:
                result = await asyncio.to_thread(download_media, url, base_filename, mode)
                final_path = result["file"]

                if os.path.exists(final_path) and not final_path.endswith(('.mp3', '.m4a')):
                    file_size_mb = os.path.getsize(final_path) / (1024 * 1024)
                    if file_size_mb > 49.5:
                        await callback.message.edit_text(f"⚠️ Файл слишком большой ({file_size_mb:.1f} МБ). Лимит — 50 МБ.")
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
                pass

        if mode == "audio" and not is_tiktok_photo:
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

        await callback.message.edit_text("📸 Скачиваю слайды из фото-карусели...")
        if "tiktok.com" in url:
            carousel_data = await download_tiktok_carousel(url, carousel_folder)
        else:
            carousel_data = await asyncio.to_thread(download_other_carousel, url, carousel_folder)

        images = carousel_data["images"]
        audios = carousel_data["audios"]

        if mode == "audio" and audios:
            async with ChatActionSender.upload_voice(bot=bot, chat_id=callback.message.chat.id):
                await callback.message.answer_audio(FSInputFile(audios[0]), caption="🎵 Музыка из слайд-шоу!")
            await callback.message.delete()
            return

        if not images:
            await callback.message.edit_text("❌ Не удалось найти ни видео, ни фото по этой ссылке.")
            return

        await callback.message.edit_text(f"📤 Отправляю фото ({len(images)} шт.)...")

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

        if audios:
            async with ChatActionSender.upload_voice(bot=bot, chat_id=callback.message.chat.id):
                await callback.message.answer_audio(
                    FSInputFile(audios[0]),
                    caption="🎵 Фоновый трек из этой карусели"
                )

        await callback.message.delete()

    except Exception as e:
        await callback.message.edit_text(
            f"❌ <b>Ошибка при обработке (v3.0):</b>\n<code>{str(e)[:200]}</code>",
            parse_mode="HTML"
        )
    finally:
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
