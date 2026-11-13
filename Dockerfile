FROM python:3.11-slim

# Устанавливаем FFmpeg для склейки видео и конвертации музыки в MP3
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# Ставим библиотеки и самую свежую ночную версию yt-dlp с обходом защит TikTok
RUN pip install --no-cache-dir -U -r requirements.txt && \
    pip install --no-cache-dir -U --pre "yt-dlp[default,curl-cffi]"

COPY bot.py .

EXPOSE 10000

CMD ["python", "bot.py"]