import os
import re
import uuid
import html
import shutil
import logging
import asyncio
import subprocess
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("ОШИБКА: Токен BOT_TOKEN не задан в переменных окружения!")

bot = Bot(token=TOKEN)
dp = Dispatcher()

url_storage = {}
active_tasks = set()
URL_REGEX = re.compile(r'(https?://[^\s<>"]+)')

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


# --- БЕЗОПАСНОЕ РЕДАКТИРОВАНИЕ И УДАЛЕНИЕ СООБЩЕНИЙ ---
async def safe_edit(message: types.Message, text: str):
    try:
        await message.edit_text(text, parse_mode="HTML")
    except Exception:
        pass


async def safe_delete(message: types.Message):
    try:
        await message.delete()
    except Exception:
        pass


def save_url(url: str) -> str:
    link_id = str(uuid.uuid4())[:8]
    url_storage[link_id] = url
    if len(url_storage) > 1000:
        url_storage.pop(next(iter(url_storage)))
    return link_id


# --- РАЗВОРОТ КОРОТКИХ ССЫЛОК ---
async def resolve_short_url(url: str) -> str:
    short_domains = ("vt.tiktok.com", "vm.tiktok.com", "tiktok.com/t/", "pin.it", "youtu.be", "vk.cc", "t.co")
    if any(d in url for d in short_domains):
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            headers = {"User-Agent": USER_AGENT}
            async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
                async with session.get(url, allow_redirects=True) as resp:
                    return str(resp.url)
        except Exception:
            return url
    return url


# --- ПОИСК ССЫЛКИ В ТЕКСТЕ, ОПИСАНИИ ИЛИ ГИПЕРССЫЛКАХ ---
def extract_url_from_message(message: types.Message | None) -> str | None:
    if not message:
        return None
    text = message.text or message.caption or ""
    match = URL_REGEX.search(text)
    if match:
        return match.group(1).rstrip('.,!?;:>\'"')

    entities = message.entities or message.caption_entities or []
    for ent in entities:
        if ent.type == "text_link" and ent.url:
            return ent.url.rstrip('.,!?;:>\'"')
    return None


# --- ОТПРАВКА ПАЧКИ ФОТО/ВИДЕО (С ПОДДЕРЖКОЙ ОДИНОЧНЫХ ФАЙЛОВ И АЛЬБОМОВ) ---
async def send_visual_chunk(message: types.Message, chunk: list, total_count: int, is_first_batch: bool):
    if len(chunk) == 1:
        m_type, m_path = chunk[0]
        if m_type == "photo":
            await message.answer_photo(FSInputFile(m_path), caption="📸 Фото из поста")
        else:
            await message.answer_video(FSInputFile(m_path), caption="🎬 Видео из поста", supports_streaming=True)
    else:
        media_group = []
        for idx, (m_type, m_path) in enumerate(chunk):
            cap = f"📸 Карусель ({total_count} медиа)" if idx == 0 and is_first_batch else None
            if m_type == "photo":
                media_group.append(InputMediaPhoto(media=FSInputFile(m_path), caption=cap))
            else:
                media_group.append(InputMediaVideo(media=FSInputFile(m_path), caption=cap, supports_streaming=True))
        await message.answer_media_group(media_group)


# --- ФИЛЬТР ЗАЩИТЫ ОТ ПРЯМЫХ ЭФИРОВ ---
def _no_live_filter(info, *, incomplete):
    if info.get('is_live') or info.get('live_status') == 'is_live':
        raise yt_dlp.utils.DownloadError("LIVE_STREAM_NOT_SUPPORTED")
    return None


# --- КОМАНДА /start ---
@dp.message(CommandStart())
async def start_cmd(message: types.Message):
    bot_info = await bot.get_me()
    await message.answer(
        "🚀 <b>Привет! Я твой универсальный загрузчик медиа.</b>\n\n"
        "📥 <b>В личке или группе:</b>\n"
        "Отправь или перешли ссылку из TikTok, Instagram, YouTube, VK, Pinterest, X или Reddit.\n\n"
        "⚡ <b>В любом другом чате (Inline-режим):</b>\n"
        f"Напиши в диалоге с другом:\n<code>@{bot_info.username} ссылка_на_ролик</code>",
        parse_mode="HTML"
    )


# --- ЗАГРУЗЧИК TIKTOK API (ФОТО-КАРУСЕЛИ И РЕЗЕРВ ДЛЯ ВИДЕО) ---
async def download_tiktok_api(url: str, folder_name: str) -> dict:
    os.makedirs(folder_name, exist_ok=True)
    images, audios, videos = [], [], []
    title = "TikTok Медиа"
    uploader = ""

    api_url = "https://www.tikwm.com/api/"
    timeout = aiohttp.ClientTimeout(total=30)
    headers = {"User-Agent": USER_AGENT}

    try:
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
            async with session.post(api_url, data={"url": url, "hd": 1}) as resp:
                data = await resp.json(content_type=None)
                if isinstance(data, dict) and data.get("code") == 0 and isinstance(data.get("data"), dict):
                    post_data = data["data"]
                    title = post_data.get("title") or title
                    uploader = (post_data.get("author") or {}).get("nickname") or ""
                    img_urls = post_data.get("images") or []

                    if img_urls:
                        for idx, img_url in enumerate(img_urls):
                            img_path = os.path.join(folder_name, f"slide_{idx:03d}.jpg")
                            async with session.get(img_url) as img_resp:
                                if img_resp.status == 200:
                                    content = await img_resp.read()
                                    if content:
                                        with open(img_path, "wb") as f:
                                            f.write(content)
                                        images.append(img_path)
                    else:
                        play_url = post_data.get("hdplay") or post_data.get("play")
                        if play_url:
                            vid_path = os.path.join(folder_name, "tiktok_video.mp4")
                            async with session.get(play_url) as vid_resp:
                                if vid_resp.status == 200:
                                    content = await vid_resp.read()
                                    if content:
                                        with open(vid_path, "wb") as f:
                                            f.write(content)
                                        videos.append(vid_path)

                    music_url = post_data.get("music")
                    if music_url:
                        audio_path = os.path.join(folder_name, "audio.mp3")
                        async with session.get(music_url) as aud_resp:
                            if aud_resp.status == 200:
                                content = await aud_resp.read()
                                if content:
                                    with open(audio_path, "wb") as f:
                                        f.write(content)
                                    audios.append(audio_path)
    except Exception as e:
        logging.warning(f"TikWM API warning: {e}")

    return {"images": images, "videos": videos, "audios": audios, "title": title, "uploader": uploader}


# --- ЗАГРУЗЧИК КАРУСЕЛЕЙ ДРУГИХ СОЦСЕТЕЙ (GALLERY-DL) ---
def download_other_carousel(url: str, folder_name: str) -> dict:
    os.makedirs(folder_name, exist_ok=True)
    cmd = [
        "gallery-dl",
        "--directory", folder_name,
        "--user-agent", USER_AGENT,
        "--range", "1-30",
        url
    ]
    try:
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    except Exception as e:
        logging.warning(f"gallery-dl warning: {e}")

    images, videos, audios = [], [], []
    for root, _, files in os.walk(folder_name):
        for f in sorted(files):
            if f.endswith(('.part', '.ytdl', '.temp')):
                continue
            full_path = os.path.join(root, f)
            if os.path.getsize(full_path) == 0:
                continue
            ext = f.lower().split('.')[-1]
            if ext in ('jpg', 'jpeg', 'png', 'webp'):
                images.append(full_path)
            elif ext in ('mp4', 'mov', 'webm'):
                videos.append(full_path)
            elif ext in ('mp3', 'm4a', 'wav', 'ogg'):
                audios.append(full_path)

    return {"images": images, "videos": videos, "audios": audios, "title": "Медиа из поста", "uploader": ""}


# --- ОСНОВНОЙ ЗАГРУЗЧИК ВИДЕО И АУДИО (YT-DLP) ---
def download_media(url: str, base_filename: str, mode: str) -> dict:
    clean_url = url.replace("/photo/", "/video/") if "tiktok.com" in url else url

    common_opts = {
        'quiet': True,
        'noplaylist': True,
        'playlist_items': '1',
        'no_color': True,
        'geo_bypass': True,
        'socket_timeout': 20,
        'retries': 3,
        'max_filesize': 50 * 1024 * 1024,
        'match_filter': _no_live_filter,
        'http_headers': {'User-Agent': USER_AGENT}
    }

    if mode == "audio":
        ydl_opts = {
            **common_opts,
            'format': 'bestaudio[filesize<?48M]/best[filesize<?48M]',
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
                'bestvideo[vcodec^=avc1][filesize<?42M]+bestaudio[acodec^=mp4a][filesize<?10M]/'
                'bestvideo[vcodec^=avc1][filesize<?42M]+bestaudio[filesize<?10M]/'
                'bestvideo[ext=mp4][filesize<?42M]+bestaudio[filesize<?10M]/'
                'best[ext=mp4][filesize<?49M]/'
                'best[filesize<?49M]/best'
            ),
            'outtmpl': f"{base_filename}.%(ext)s",
            'merge_output_format': 'mp4',
        }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(clean_url, download=True)
        if not info:
            raise ValueError("Не удалось получить данные о медиафайле.")

        if 'entries' in info and info['entries']:
            info = info['entries'][0]

        if mode == "video" and info.get('vcodec') == 'none':
            raise ValueError("Пост не содержит видеопотока (слайд-шоу).")

        title = info.get('title') or 'Медиафайл'
        uploader = info.get('uploader') or ''
        width = info.get('width')
        height = info.get('height')
        duration = info.get('duration')

        final_file = f"{base_filename}.mp3" if mode == "audio" else f"{base_filename}.mp4"
        if not os.path.exists(final_file):
            for f in os.listdir('.'):
                if f.startswith(base_filename) and not f.endswith(('.part', '.ytdl', '.temp')):
                    final_file = f
                    break

        return {
            "file": final_file,
            "title": title,
            "uploader": uploader,
            "width": int(width) if width else None,
            "height": int(height) if height else None,
            "duration": int(duration) if duration else None
        }


# ==========================================
# INLINE MODE (ВЫЗОВ ЧЕРЕЗ @БОТ В ЛЮБОМ ЧАТЕ)
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

    url = match.group(1).rstrip('.,!?;:>\'"')
    link_id = save_url(url)

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
        try:
            await bot.edit_message_text(inline_message_id=inline_msg_id, text="⚠️ Ссылка устарела, попробуй ещё раз.")
        except Exception:
            pass
        return

    unique_run = str(uuid.uuid4())[:6]
    base_filename = f"inline_{user_id}_{unique_run}"
    carousel_folder = f"in_car_{user_id}_{unique_run}"

    url = await resolve_short_url(raw_url)

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
            except Exception as e:
                if "LIVE_STREAM_NOT_SUPPORTED" in str(e):
                    await bot.edit_message_text(inline_message_id=inline_msg_id, text="⚠️ Прямые эфиры (стримы) не поддерживаются.")
                    return
                logging.info(f"Inline yt-dlp fallback triggered: {e}")

        if not final_path and "tiktok.com" in url:
            tk_data = await download_tiktok_api(url, carousel_folder)
            title = tk_data["title"][:100]
            uploader = tk_data["uploader"]
            if mode == "audio" and tk_data["audios"]:
                final_path = tk_data["audios"][0]
            elif tk_data["videos"]:
                final_path = tk_data["videos"][0]
            elif tk_data["images"]:
                sent_photo = await bot.send_photo(chat_id=user_id, photo=FSInputFile(tk_data["images"][0]), disable_notification=True)
                photo_id = sent_photo.photo[-1].file_id
                await safe_delete(sent_photo)

                safe_title = html.escape(title)
                await bot.edit_message_media(
                    inline_message_id=inline_msg_id,
                    media=InputMediaPhoto(
                        media=photo_id,
                        caption=f"📸 <b>{safe_title}</b>\n<i>(Слайд 1 из {len(tk_data['images'])}. Для получения всех фото альбомом отправь ссылку боту в личку!)</i>",
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

        file_size_mb = os.path.getsize(final_path) / (1024 * 1024)
        if file_size_mb > 49.5:
            await bot.edit_message_text(
                inline_message_id=inline_msg_id,
                text=f"⚠️ Файл слишком большой ({file_size_mb:.1f} МБ). Лимит Telegram для ботов — 50 МБ."
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
            await safe_delete(sent)

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
            await safe_delete(sent)

            caption = f"🎬 <b>{safe_title}</b>"
            if safe_uploader:
                caption += f"\n👤 Автор: {safe_uploader}"

            await bot.edit_message_media(
                inline_message_id=inline_msg_id,
                media=InputMediaVideo(media=file_id, caption=caption, parse_mode="HTML")
            )

    except TelegramForbiddenError:
        bot_info = await bot.get_me()
        try:
            await bot.edit_message_text(
                inline_message_id=inline_msg_id,
                text=(
                    f"⚠️ <b>Нужна активация!</b>\n"
                    f"Чтобы отправлять видео через @, открой бота @{bot_info.username} и один раз нажми <b>/start</b>!"
                ),
                parse_mode="HTML"
            )
        except Exception:
            pass
    except Exception as e:
        try:
            await bot.edit_message_text(
                inline_message_id=inline_msg_id,
                text=f"❌ <b>Ошибка загрузки:</b> <code>{html.escape(str(e)[:120])}</code>\nСсылка: {html.escape(url)}",
                parse_mode="HTML"
            )
        except Exception:
            pass
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
# РАБОТА В ЛИЧКЕ И ГРУППОВЫХ ЧАТАХ
# ==========================================
@dp.message(F.text | F.caption)
async def handle_link(message: types.Message):
    url = extract_url_from_message(message)
    if not url:
        if message.chat.type == "private":
            await message.answer("🔗 Отправь или перешли мне сообщение со ссылкой (начинается с http:// или https://)")
        return

    link_id = save_url(url)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎬 Видео / 📸 Фото", callback_data=f"dl:video:{link_id}"),
            InlineKeyboardButton(text="🎵 Только звук (MP3)", callback_data=f"dl:audio:{link_id}")
        ]
    ])

    try:
        await message.reply(
            "✨ <b>Ссылка найдена!</b> Что скачиваем?",
            reply_markup=keyboard,
            parse_mode="HTML"
        )
    except Exception:
        await message.answer(
            "✨ <b>Ссылка найдена!</b> Что скачиваем?",
            reply_markup=keyboard,
            parse_mode="HTML"
        )


@dp.callback_query(F.data.startswith("dl:"))
async def process_download(callback: CallbackQuery):
    if not callback.message:
        await callback.answer()
        return

    _, mode, link_id = callback.data.split(":")
    raw_url = url_storage.get(link_id)

    if not raw_url and callback.message.reply_to_message:
        raw_url = extract_url_from_message(callback.message.reply_to_message)

    if not raw_url:
        await safe_edit(callback.message, "⚠️ Ссылка устарела. Отправь её ещё раз!")
        return

    task_key = f"{callback.message.chat.id}:{callback.message.message_id}"
    if task_key in active_tasks:
        await callback.answer("⏳ Уже скачиваю, подожди пару секунд!", show_alert=False)
        return
    active_tasks.add(task_key)

    try:
        await callback.answer()
    except Exception:
        pass

    action_text = "🎵 Вырезаю аудио (MP3)..." if mode == "audio" else "⏳ Проверяю ссылку и загружаю медиа..."
    await safe_edit(callback.message, action_text)

    unique_run = str(uuid.uuid4())[:6]
    base_filename = f"media_{callback.from_user.id}_{unique_run}"
    carousel_folder = f"carousel_{callback.from_user.id}_{unique_run}"

    try:
        url = await resolve_short_url(raw_url)
        is_tiktok_photo = "tiktok.com" in url and "/photo/" in url

        # 1. СКАЧИВАЕМ ВИДЕО ЧЕРЕЗ YT-DLP
        if mode == "video" and not is_tiktok_photo:
            try:
                result = await asyncio.to_thread(download_media, url, base_filename, mode)
                final_path = result["file"]

                if os.path.exists(final_path) and not final_path.endswith(('.mp3', '.m4a')):
                    file_size_mb = os.path.getsize(final_path) / (1024 * 1024)
                    if file_size_mb > 49.5:
                        await safe_edit(callback.message, f"⚠️ Файл слишком большой ({file_size_mb:.1f} МБ). Лимит — 50 МБ.")
                        return

                    await safe_edit(callback.message, "📤 Отправляю видео в чат...")
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
                    await safe_delete(callback.message)
                    return
            except Exception as e:
                if "LIVE_STREAM_NOT_SUPPORTED" in str(e):
                    await safe_edit(callback.message, "⚠️ Прямые эфиры (стримы) не поддерживаются.")
                    return
                logging.info(f"Video yt-dlp fallback triggered: {e}")

        # 2. СКАЧИВАЕМ АУДИО ЧЕРЕЗ YT-DLP
        if mode == "audio" and not is_tiktok_photo:
            try:
                result = await asyncio.to_thread(download_media, url, base_filename, "audio")
                final_path = result["file"]
                if os.path.exists(final_path):
                    file_size_mb = os.path.getsize(final_path) / (1024 * 1024)
                    if file_size_mb > 49.5:
                        await safe_edit(callback.message, f"⚠️ Аудиофайл слишком большой ({file_size_mb:.1f} МБ).")
                        return

                    await safe_edit(callback.message, "📤 Отправляю музыку...")
                    async with ChatActionSender.upload_voice(bot=bot, chat_id=callback.message.chat.id):
                        await callback.message.answer_audio(
                            FSInputFile(final_path),
                            title=result["title"][:100],
                            performer=result["uploader"] or "Audio",
                            caption="🎵 Аудиодорожка готова!"
                        )
                    await safe_delete(callback.message)
                    return
            except Exception as e:
                if "LIVE_STREAM_NOT_SUPPORTED" in str(e):
                    await safe_edit(callback.message, "⚠️ Прямые эфиры (стримы) не поддерживаются.")
                    return
                logging.info(f"Audio yt-dlp fallback triggered: {e}")

        # 3. КАРУСЕЛИ И РЕЗЕРВНЫЙ КАНАЛ (TIKTOK API / GALLERY-DL)
        await safe_edit(callback.message, "📸 Скачиваю файлы из поста/карусели...")
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
            await safe_delete(callback.message)
            return

        all_visual_media = []
        for img in images:
            if os.path.getsize(img) < 10 * 1024 * 1024:
                all_visual_media.append(("photo", img))
        for vid in videos:
            if os.path.getsize(vid) < 49.5 * 1024 * 1024:
                all_visual_media.append(("video", vid))

        if not all_visual_media:
            await safe_edit(callback.message, "❌ Не удалось найти доступные медиа по этой ссылке (возможно, аккаунт закрыт).")
            return

        await safe_edit(callback.message, f"📤 Отправляю файлы ({len(all_visual_media)} шт.)...")

        async with ChatActionSender.upload_photo(bot=bot, chat_id=callback.message.chat.id):
            for i in range(0, len(all_visual_media), 10):
                if i > 0:
                    await asyncio.sleep(1.5)
                chunk = all_visual_media[i:i + 10]
                try:
                    await send_visual_chunk(callback.message, chunk, len(all_visual_media), is_first_batch=(i == 0))
                except TelegramRetryAfter as e:
                    await asyncio.sleep(e.retry_after + 1)
                    await send_visual_chunk(callback.message, chunk, len(all_visual_media), is_first_batch=(i == 0))

        if audios and images:
            async with ChatActionSender.upload_voice(bot=bot, chat_id=callback.message.chat.id):
                await callback.message.answer_audio(
                    FSInputFile(audios[0]),
                    caption="🎵 Фоновый трек из этой карусели"
                )

        await safe_delete(callback.message)

    except Exception as e:
        safe_err = html.escape(str(e)[:200])
        await safe_edit(
            callback.message,
            f"❌ <b>Ошибка при обработке:</b>\n<code>{safe_err}</code>"
        )
    finally:
        active_tasks.discard(task_key)
        for f in os.listdir('.'):
            if f.startswith(base_filename):
                try:
                    os.remove(f)
                except OSError:
                    pass
        if os.path.exists(carousel_folder):
            shutil.rmtree(carousel_folder, ignore_errors=True)


async def main():
    logging.info("Очистка очереди обновлений и запуск бота...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(
        bot,
        polling_timeout=30,
        allowed_updates=dp.resolve_used_update_types()
    )


if __name__ == "__main__":
    asyncio.run(main())
