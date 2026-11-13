import os
import re
import uuid
import html
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
URL_REGEX = re.compile(r'(https?://[^\s<>"]+)')

# --- ВЕБ-СЕРВЕР (Использовать только если нужен, на GitHub Actions не мешает) ---
async def health_check(request):
    return web.Response(text="Bot v4.0 is running 24/7!")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

# --- УТИЛИТА: РАЗВОРАЧИВАЕМ КОРОТКИЕ ССЫЛКИ (vt.tiktok.com / vm.tiktok.com) ---
async def resolve_short_url(url: str) -> str:
    if any(short in url for short in ("vt.tiktok.com", "vm.tiktok.com", "tiktok.com/t/", "pin.it")):
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            }
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.head(url, allow_redirects=True, timeout=8) as resp:
                    return str(resp.url)
        except Exception:
            return url
    return url

# --- УТИЛИТА: ВЫТАСКИВАЕМ ССЫЛКУ ИЗ ЛЮБОГО СООБЩЕНИЯ (ТЕКСТ, ПОДПИСЬ, ГИПЕРССЫЛКА) ---
def extract_url_from_message(message: types.Message) -> str | None:
    text = message.text or message.caption or ""
    match = URL_REGEX.search(text)
    if match:
        return match.group(1)

    entities = message.entities or message.caption_entities or []
    for ent in entities:
        if ent.type == "text_link" and ent.url:
            return ent.url
    return None

# --- КОМАНДА /start ---
@dp.message(CommandStart())
async def start_cmd(message: types.Message):
    bot_info = await bot.get_me()
    await message.answer(
        "🚀 <b>Привет! Я твой универсальный загрузчик медиа (v4.0).</b>\n\n"
        "📥 <b>В личке или группе:</b>\n"
        "Просто отправь или перешли ссылку из TikTok, Instagram, YouTube, VK, Pinterest, X или Reddit.\n\n"
        "⚡ <b>В ЛЮБОМ другом чате (Inline-режим):</b>\n"
        f"Напиши в диалоге с другом:\n<code>@{bot_info.username} ссылка_на_ролик</code>",
        parse_mode="HTML"
    )

# --- ПРЯМОЕ СКАЧИВАНИЕ ФОТО-КАРУСЕЛЕЙ И ВИДЕО TIKTOK (ЧЕРЕЗ API) ---
async def download_tiktok_api(url: str, folder_name: str) -> dict:
    os.makedirs(folder_name, exist_ok=True)
    images = []
    audios = []
    videos = []
    title = "TikTok Медиа"
    uploader = ""

    api_url = "https://www.tikwm.com/api/"
    async with aiohttp.ClientSession() as session:
        async with session.post(api_url, data={"url": url, "hd": 1}, timeout=20) as resp:
            data = await resp.json()
            if data.get("code") == 0 and "data" in data:
                post_data = data["data"]
                title = post_data.get("title") or title
                uploader = post_data.get("author", {}).get("nickname", "")
                img_urls = post_data.get("images", [])

                # Если это фото-карусель
                if img_urls:
                    for idx, img_url in enumerate(img_urls):
                        img_path = os.path.join(folder_name, f"slide_{idx:03d}.jpg")
                        async with session.get(img_url) as img_resp:
                            if img_resp.status == 200:
                                with open(img_path, "wb") as f:
                                    f.write(await img_resp.read())
                                images.append(img_path)
                else:
                    # Запасной вариант: если yt-dlp не справился с обычным видео TikTok, качаем через API
                    play_url = post_data.get("hdplay") or post_data.get("play")
                    if play_url:
                        vid_path = os.path.join(folder_name, "tiktok_video.mp4")
                        async with session.get(play_url) as vid_resp:
                            if vid_resp.status == 200:
                                with open(vid_path, "wb") as f:
                                    f.write(await vid_resp.read())
                                videos.append(vid_path)

                music_url = post_data.get("music")
                if music_url:
                    audio_path = os.path.join(folder_name, "audio.mp3")
                    async with session.get(music_url) as aud_resp:
                        if aud_resp.status == 200:
                            with open(audio_path, "wb") as f:
                                f.write(await aud_resp.read())
                            audios.append(audio_path)

    return {"images": images, "videos": videos, "audios": audios, "title": title, "uploader": uploader}

# --- СКАЧИВАНИЕ КАРУСЕЛЕЙ ИЗ ДРУГИХ СОЦСЕТЕЙ (ФОТО + ВИДЕО) ---
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
    videos = []
    audios = []
    for root, _, files in os.walk(folder_name):
        for f in sorted(files):
            full_path = os.path.join(root, f)
            ext = f.lower().split('.')[-1]
            if ext in ('jpg', 'jpeg', 'png', 'webp'):
                images.append(full_path)
            elif ext in ('mp4', 'mov', 'webm'):
                videos.append(full_path)
            elif ext in ('mp3', 'm4a', 'wav', 'ogg'):
                audios.append(full_path)

    return {"images": images, "videos": videos, "audios": audios, "title": "Медиа из поста", "uploader": ""}

# --- СКАЧИВАНИЕ ВИДЕО И АУДИО (YT-DLP) ---
def download_media(url: str, base_filename: str, mode: str) -> dict:
    clean_url = url.replace("/photo/", "/video/") if "tiktok.com" in url else url

    common_opts = {
        'quiet': True,
        'noplaylist': True,
        'no_color': True,
        'geo_bypass': True,
        'socket_timeout': 25,
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
            'format': (
                'best[ext=mp4][filesize<50M]/'
                'bestvideo[ext=mp4][filesize<42M]+bestaudio[ext=m4a]/'
                'best[ext=mp4]/best'
            ),
            'outtmpl': f"{base_filename}.%(ext)s",
            'merge_output_format': 'mp4',
        }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(clean_url, download=True)
        title = info.get('title') or 'Медиафайл'
        uploader = info.get('uploader') or ''
        width = info.get('width')
        height = info.get('height')
        duration = info.get('duration')

        final_file = f"{base_filename}.mp3" if mode == "audio" else f"{base_filename}.mp4"
        if not os.path.exists(final_file):
            for f in os.listdir('.'):
                if f.startswith(base_filename):
                    final_file = f
                    break

        return {
            "file": final_file,
            "title": title,
            "uploader": uploader,
            "width": width,
            "height": height,
            "duration": int(duration) if duration else None
        }

# ==========================================
# 🔥 INLINE MODE (ВЫЗОВ ЧЕРЕЗ @БОТ В ЧАТАХ)
# ==========================================
@dp.inline_query()
async def inline_link_handler(inline_query: InlineQuery):
    query_text = inline_query.query.strip()
    match = URL_REGEX.search(query_text)

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

    wait_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏳ Скачиваю и прикрепляю...", callback_data="noop")]
    ])

    results = [
        InlineQueryResultArticle(
            id=f"video:{link_id}",
            title="🎬 Отправить Видео / Фото сюда",
            description=f"Скачать и вставить: {url[:40]}...",
            input_message_content=InputTextMessageContent(
                message_text="⏳ <b>Загружаю медиа по ссылке...</b>\n<i>Подождите пару секунд, сообщение обновится автоматически!</i>",
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
    raw_url = url_storage.get(link_id)
    user_id = chosen_result.from_user.id
    inline_msg_id = chosen_result.inline_message_id

    if not raw_url:
        await bot.edit_message_text(inline_message_id=inline_msg_id, text="⚠️ Ссылка устарела, попробуй ещё раз.")
        return

    url = await resolve_short_url(raw_url)
    base_filename = f"inline_{user_id}_{link_id}"
    carousel_folder = f"in_car_{user_id}_{link_id}"

    try:
        is_tiktok_photo = "tiktok.com" in url and "/photo/" in url
        final_path = None
        title = "Медиафайл"
        uploader = ""
        width = height = duration = None

        if not is_tiktok_photo:
            try:
                result = await asyncio.to_thread(download_media, url, base_filename, mode)
                if os.path.exists(result["file"]) and not (mode == "video" and result["file"].endswith(('.mp3', '.m4a'))):
                    final_path = result["file"]
                    title = result["title"][:100]
                    uploader = result["uploader"]
                    width, height, duration = result["width"], result["height"], result["duration"]
            except Exception:
                pass

        # Если это карусель или yt-dlp не справился в Инлайне
        if not final_path and "tiktok.com" in url:
            tk_data = await download_tiktok_api(url, carousel_folder)
            title = tk_data["title"][:100]
            uploader = tk_data["uploader"]
            if mode == "audio" and tk_data["audios"]:
                final_path = tk_data["audios"][0]
            elif tk_data["videos"]:
                final_path = tk_data["videos"][0]
            elif tk_data["images"]:
                # В инлайне нельзя заменить текст на альбом, поэтому отправляем обложку карусели, а все фото — в личку!
                sent_photo = await bot.send_photo(chat_id=user_id, photo=FSInputFile(tk_data["images"][0]), disable_notification=True)
                photo_id = sent_photo.photo[-1].file_id
                await sent_photo.delete()

                safe_title = html.escape(title)
                await bot.edit_message_media(
                    inline_message_id=inline_msg_id,
                    media=InputMediaPhoto(
                        media=photo_id,
                        caption=f"📸 <b>{safe_title}</b>\n<i>(Фото-карусель из {len(tk_data['images'])} слайдов)</i>",
                        parse_mode="HTML"
                    )
                )
                return

        if not final_path or not os.path.exists(final_path):
            await bot.edit_message_text(
                inline_message_id=inline_msg_id,
                text=f"❌ Не удалось скачать медиа (<a href='{html.escape(url)}'>ссылка</a>).",
                parse_mode="HTML"
            )
            return

        safe_title = html.escape(title)
        safe_uploader = html.escape(uploader)

        if mode == "audio":
            sent = await bot.send_audio(
                chat_id=user_id,
                audio=FSInputFile(final_path),
                title=title,
                performer=uploader or "Audio",
                disable_notification=True
            )
            file_id = sent.audio.file_id
            await sent.delete()

            await bot.edit_message_media(
                inline_message_id=inline_msg_id,
                media=InputMediaAudio(media=file_id, caption=f"🎵 <b>{safe_title}</b>", parse_mode="HTML")
            )
        else:
            sent = await bot.send_video(
                chat_id=user_id,
                video=FSInputFile(final_path),
                width=width,
                height=height,
                duration=duration,
                disable_notification=True
            )
            file_id = sent.video.file_id
            await sent.delete()

            caption = f"🎬 <b>{safe_title}</b>"
            if safe_uploader:
                caption += f"\n👤 Автор: {safe_uploader}"

            await bot.edit_message_media(
                inline_message_id=inline_msg_id,
                media=InputMediaVideo(media=file_id, caption=caption, parse_mode="HTML")
            )

    except Exception:
        await bot.edit_message_text(
            inline_message_id=inline_msg_id,
            text=(
                f"❌ <b>Не удалось загрузить в инлайн-режиме.</b>\n"
                f"💡 <i>Важно: чтобы инлайн-режим работал, нажми /start в личке у бота!</i>\n"
                f"Ссылка: {html.escape(url)}"
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
        if os.path.exists(carousel_folder):
            shutil.rmtree(carousel_folder, ignore_errors=True)

@dp.callback_query(F.data == "noop")
async def noop_cb(callback: CallbackQuery):
    await callback.answer("⏳ Медиа уже качается, подожди пару секунд!", show_alert=False)

# ==========================================
# ОБЫЧНЫЙ РЕЖИМ В ЛИЧКЕ И ГРУППАХ (ВИДЕО + КАРУСЕЛИ)
# ==========================================
@dp.message(F.text | F.caption)
async def handle_link(message: types.Message):
    url = extract_url_from_message(message)
    if not url:
        # В личке подсказываем, а в групповых чатах с друзьями не спамим на обычные сообщения
        if message.chat.type == "private":
            await message.answer("🔗 Отправь или перешли мне сообщение со ссылкой (начинается с http:// или https://)")
        return

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

    await message.reply(
        "✨ <b>Ссылка найдена!</b> Что скачиваем?",
        reply_markup=keyboard,
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("dl:"))
async def process_download(callback: CallbackQuery):
    _, mode, link_id = callback.data.split(":")
    raw_url = url_storage.get(link_id)

    if not raw_url:
        await callback.message.edit_text("⚠️ Ссылка устарела. Отправь её ещё раз!")
        return

    await callback.answer()
    action_text = "🎵 Вырезаю аудио (MP3)..." if mode == "audio" else "⏳ Проверяю ссылку и загружаю медиа..."
    await callback.message.edit_text(action_text)

    # Разворачиваем короткие ссылки (vt.tiktok.com и др.), чтобы точно видеть /photo/
    url = await resolve_short_url(raw_url)

    base_filename = f"media_{callback.from_user.id}_{link_id}"
    carousel_folder = f"carousel_{callback.from_user.id}_{link_id}"

    try:
        is_tiktok_photo = "tiktok.com" in url and "/photo/" in url

        # 1. СКАЧИВАЕМ ВИДЕО ЧЕРЕЗ YT-DLP
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
                    safe_title = html.escape(result['title'][:100])
                    safe_uploader = html.escape(result['uploader'])
                    caption = f"🎬 <b>{safe_title}</b>"
                    if safe_uploader:
                        caption += f"\n👤 Автор: {safe_uploader}"

                    async with ChatActionSender.upload_video(bot=bot, chat_id=callback.message.chat.id):
                        await callback.message.answer_video(
                            FSInputFile(final_path),
                            caption=caption,
                            width=result["width"],
                            height=result["height"],
                            duration=result["duration"],
                            parse_mode="HTML",
                            supports_streaming=True
                        )
                    await callback.message.delete()
                    return
            except Exception:
                pass

        # 2. СКАЧИВАЕМ АУДИО ЧЕРЕЗ YT-DLP
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

        # 3. КАРУСЕЛИ И ЗАПАСНОЙ ЗАГРУЗЧИК (TIKTOK API / GALLERY-DL)
        await callback.message.edit_text("📸 Скачиваю файлы из поста/карусели...")
        if "tiktok.com" in url:
            carousel_data = await download_tiktok_api(url, carousel_folder)
        else:
            carousel_data = await asyncio.to_thread(download_other_carousel, url, carousel_folder)

        images = carousel_data["images"]
        videos = carousel_data["videos"]
        audios = carousel_data["audios"]

        if mode == "audio" and audios:
            async with ChatActionSender.upload_voice(bot=bot, chat_id=callback.message.chat.id):
                await callback.message.answer_audio(FSInputFile(audios[0]), caption="🎵 Музыка из поста!")
            await callback.message.delete()
            return

        # Собираем все фото и видео из карусели в единый список медиа
        all_visual_media = [("photo", img) for img in images] + [("video", vid) for vid in videos]

        if not all_visual_media:
            await callback.message.edit_text("❌ Не удалось найти медиа по этой ссылке (возможно, аккаунт закрыт).")
            return

        await callback.message.edit_text(f"📤 Отправляю файлы ({len(all_visual_media)} шт.)...")

        async with ChatActionSender.upload_photo(bot=bot, chat_id=callback.message.chat.id):
            for i in range(0, len(all_visual_media), 10):
                chunk = all_visual_media[i:i + 10]
                if len(chunk) == 1:
                    m_type, m_path = chunk[0]
                    if m_type == "photo":
                        await callback.message.answer_photo(FSInputFile(m_path), caption="📸 Фото из поста")
                    else:
                        await callback.message.answer_video(FSInputFile(m_path), caption="🎬 Видео из поста", supports_streaming=True)
                else:
                    media_group = []
                    for idx, (m_type, m_path) in enumerate(chunk):
                        cap = f"📸 Карусель ({len(all_visual_media)} медиа)" if idx == 0 and i == 0 else None
                        if m_type == "photo":
                            media_group.append(InputMediaPhoto(media=FSInputFile(m_path), caption=cap))
                        else:
                            media_group.append(InputMediaVideo(media=FSInputFile(m_path), caption=cap, supports_streaming=True))
                    await callback.message.answer_media_group(media_group)

        if audios:
            async with ChatActionSender.upload_voice(bot=bot, chat_id=callback.message.chat.id):
                await callback.message.answer_audio(
                    FSInputFile(audios[0]),
                    caption="🎵 Фоновый трек из этой карусели"
                )

        await callback.message.delete()

    except Exception as e:
        safe_err = html.escape(str(e)[:200])
        await callback.message.edit_text(
            f"❌ <b>Ошибка при обработке (v4.0):</b>\n<code>{safe_err}</code>",
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
