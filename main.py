from __future__ import annotations

import html
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from Crypto.Cipher import AES
from dotenv import load_dotenv
from telegram import Message, MessageEntity, Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters


load_dotenv()

BOT_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
FASTVIDEOSAVE_AES_KEY = b"qwertyuioplkjhgf"
VIDEODROPPER_API_URL = "https://api.videodropper.app/allinone"
DOWNLOAD_PROXY_URL = "https://dl.videodropper.app/?url={url}"
REELS_URL_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?:reel|reels|share/reel)/[^\s<>()]+",
    re.IGNORECASE,
)
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "49")) * 1024 * 1024

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MediaItem:
    kind: str
    url: str
    thumbnail: str | None = None

    @property
    def proxy_url(self) -> str:
        return DOWNLOAD_PROXY_URL.format(url=quote(self.url, safe=""))


def pkcs7_pad(data: bytes, block_size: int = AES.block_size) -> bytes:
    pad_len = block_size - (len(data) % block_size)
    return data + bytes([pad_len]) * pad_len


def encrypt_fastvideosave_url(url: str) -> str:
    cipher = AES.new(FASTVIDEOSAVE_AES_KEY, AES.MODE_ECB)
    encrypted = cipher.encrypt(pkcs7_pad(url.encode("utf-8")))
    return encrypted.hex()


async def fetch_media(url: str) -> dict[str, Any]:
    encrypted_url = encrypt_fastvideosave_url(url)
    headers = {
        "url": encrypted_url,
        "origin": "https://fastvideosave.net",
        "referer": "https://fastvideosave.net/",
        "user-agent": "Mozilla/5.0 ReelsDownloaderBot/1.0",
    }

    async with httpx.AsyncClient(timeout=45) as client:
        response = await client.get(VIDEODROPPER_API_URL, headers=headers)
        response.raise_for_status()
        data = response.json()

    if data is None or data == "link":
        raise ValueError("API did not return downloadable media")
    if not isinstance(data, dict):
        raise ValueError(f"Unexpected API response: {data!r}")
    return data


async def download_media_file(item: MediaItem, directory: Path) -> Path:
    suffix_by_kind = {
        "video": ".mp4",
        "photo": ".jpg",
        "audio": ".mp3",
    }
    suffix = suffix_by_kind.get(item.kind, ".bin")
    path = directory / f"reels_downloader{suffix}"

    last_error: Exception | None = None
    for url in (item.url, item.proxy_url):
        try:
            await download_url(url, path)
            return path
        except Exception as exc:
            last_error = exc
            logger.warning("Failed to download %s media from %s: %s", item.kind, url, exc)

    raise RuntimeError("Could not download media file") from last_error


async def download_url(url: str, path: Path) -> None:
    downloaded = 0
    headers = {
        "referer": "https://fastvideosave.net/",
        "user-agent": "Mozilla/5.0 ReelsDownloaderBot/1.0",
    }

    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        async with client.stream("GET", url, headers=headers) as response:
            response.raise_for_status()

            content_length = response.headers.get("content-length")
            if content_length and int(content_length) > MAX_UPLOAD_BYTES:
                raise ValueError(f"File is larger than {MAX_UPLOAD_BYTES // 1024 // 1024} MB")

            with path.open("wb") as file:
                async for chunk in response.aiter_bytes(chunk_size=1024 * 256):
                    downloaded += len(chunk)
                    if downloaded > MAX_UPLOAD_BYTES:
                        raise ValueError(f"File is larger than {MAX_UPLOAD_BYTES // 1024 // 1024} MB")
                    file.write(chunk)


def clean_url(url: str) -> str:
    return url.rstrip(".,;!?)\"'")


def first_reels_url(text: str) -> str | None:
    match = REELS_URL_RE.search(text)
    if not match:
        return None
    return clean_url(match.group(0))


def extract_reels_url_from_message(message: Message) -> str | None:
    text = message.text or message.caption or ""
    url = first_reels_url(text)
    if url:
        return url

    entities = []
    if message.entities:
        entities.extend((message.text or "", entity) for entity in message.entities)
    if message.caption_entities:
        entities.extend((message.caption or "", entity) for entity in message.caption_entities)

    for source_text, entity in entities:
        candidate = None
        if entity.type == MessageEntity.TEXT_LINK and entity.url:
            candidate = entity.url
        elif entity.type == MessageEntity.URL:
            candidate = entity.extract_from(source_text)

        if candidate:
            url = first_reels_url(candidate)
            if url:
                return url

    return None


def is_group_chat(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.type in {"group", "supergroup"})


def extract_media(data: Any) -> list[MediaItem]:
    items: list[MediaItem] = []
    seen: set[str] = set()

    def add(kind: str, url: str, thumbnail: str | None = None) -> None:
        if url in seen:
            return
        seen.add(url)
        items.append(MediaItem(kind=kind, url=url, thumbnail=thumbnail))

    def walk(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                walk(item)
            return

        if not isinstance(value, dict):
            return

        video = value.get("video")
        image = value.get("image") or value.get("photo")
        audio = value.get("audio") or value.get("mp3")
        thumbnail = value.get("thumbnail")

        if isinstance(video, str) and video.startswith("http"):
            add("video", video, thumbnail if isinstance(thumbnail, str) else None)
        if isinstance(image, str) and image.startswith("http"):
            add("photo", image)
        if isinstance(audio, str) and audio.startswith("http"):
            add("audio", audio)

        for child in value.values():
            walk(child)

    walk(data)
    return items


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if update.message:
        await update.message.reply_text(
            "Пришли ссылку на публичный Instagram Reels, и я отправлю видео файлом.\n\n"
            "В группе просто отправьте Reels-ссылку обычным сообщением."
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if update.message:
        await update.message.reply_text(
            "Команды:\n"
            "/start - инструкция\n"
            "/help - помощь\n\n"
            "Просто отправь ссылку вида https://www.instagram.com/reel/... или https://www.instagram.com/reels/..."
        )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not update.message:
        return

    url = extract_reels_url_from_message(update.message)
    if not url:
        if not is_group_chat(update):
            await update.message.reply_text("Не вижу ссылку на Instagram Reels. Пришли URL вида https://www.instagram.com/reel/...")
        return

    await update.message.chat.send_action(ChatAction.TYPING)

    try:
        data = await fetch_media(url)
        media_items = extract_media(data)
    except Exception:
        logger.exception("Failed to fetch media for %s", url)
        if not is_group_chat(update):
            await update.message.reply_text(
                "Не получилось получить медиа. Частые причины: приватный аккаунт, удаленный ролик или временная ошибка API."
            )
        return

    if not media_items:
        logger.info("Response without media for %s: %r", url, data)
        if not is_group_chat(update):
            await update.message.reply_text("API ответил, но я не нашел в ответе ссылок на медиа.")
        return

    await send_media(update, media_items)


async def send_media(update: Update, media_items: list[MediaItem]) -> None:
    assert update.message is not None

    for item in media_items[:5]:
        try:
            with tempfile.TemporaryDirectory(prefix="reels_downloader_") as temp_dir:
                file_path = await download_media_file(item, Path(temp_dir))

                if item.kind == "video":
                    await update.message.chat.send_action(ChatAction.UPLOAD_VIDEO)
                    with file_path.open("rb") as video:
                        await update.message.reply_video(video=video)
                elif item.kind == "photo":
                    await update.message.chat.send_action(ChatAction.UPLOAD_PHOTO)
                    with file_path.open("rb") as photo:
                        await update.message.reply_photo(photo=photo)
                else:
                    await update.message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
                    with file_path.open("rb") as document:
                        await update.message.reply_document(document=document)
        except Exception:
            logger.exception("Could not send %s as a downloaded file", item.kind)
            if is_group_chat(update):
                continue
            escaped_url = html.escape(item.proxy_url)
            await update.message.reply_text(
                f"Не смог отправить файлом. Возможно, ролик слишком большой или CDN временно не отдает файл.\n{escaped_url}",
                disable_web_page_preview=True,
            )


async def post_init(application: Application) -> None:
    bot = await application.bot.get_me()
    logger.info(
        "Bot started as @%s; can_join_groups=%s; can_read_all_group_messages=%s",
        bot.username,
        bot.can_join_groups,
        bot.can_read_all_group_messages,
    )


def build_app() -> Application:
    token = os.getenv(BOT_TOKEN_ENV)
    if not token:
        raise RuntimeError(f"Set {BOT_TOKEN_ENV} environment variable")

    app = Application.builder().token(token).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler((filters.TEXT | filters.Caption()) & ~filters.COMMAND, handle_message))
    return app


def main() -> None:
    app = build_app()
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        pass
