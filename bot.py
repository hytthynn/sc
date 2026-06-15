"""
Telegram бот для скачивания лайкнутых треков с SoundCloud.
Режим: Vercel serverless (webhook) + cron-job.org для слежения за лайками.
Хранилище: Upstash Redis (бесплатно, https://upstash.com)
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import aiohttp
from aiohttp import web
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1, TALB, APIC
from soundcloud import SoundCloud

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, FSInputFile
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

# ── Логирование ───────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── Конфигурация из переменных окружения Vercel ───────────────────────────────
BOT_TOKEN      = os.environ["BOT_TOKEN"]
SC_TOKEN       = os.environ["SC_TOKEN"]          # OAuth-токен SoundCloud
GROUP_CHAT_ID  = int(os.environ["GROUP_CHAT_ID"])  # ID группы/канала куда слать
OWNER_ID       = 1822182658
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "sc_secret_42")
CRON_SECRET    = os.environ.get("CRON_SECRET", "cron_secret_42")

# Upstash Redis (бесплатный https://upstash.com → Create Database → REST API)
UPSTASH_URL    = os.environ["UPSTASH_URL"]   # https://xxx.upstash.io
UPSTASH_TOKEN  = os.environ["UPSTASH_TOKEN"] # Bearer token

# ── Webhook URL (Vercel автоматически даёт домен) ─────────────────────────────
VERCEL_URL     = os.environ.get("VERCEL_URL", "")   # напр. my-bot.vercel.app
WEBHOOK_PATH   = "/webhook"

# ════════════════════════════════════════════════════════════════════════════
# UPSTASH REDIS — хранилище last_track_urn
# ════════════════════════════════════════════════════════════════════════════

async def redis_get(key: str) -> str | None:
    """Получить значение из Upstash Redis."""
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{UPSTASH_URL}/get/{key}",
            headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"},
        ) as resp:
            data = await resp.json()
            return data.get("result")  # None если ключа нет


async def redis_set(key: str, value: str) -> None:
    """Сохранить значение в Upstash Redis."""
    async with aiohttp.ClientSession() as session:
        await session.get(
            f"{UPSTASH_URL}/set/{key}/{value}",
            headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"},
        )


# ════════════════════════════════════════════════════════════════════════════
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ════════════════════════════════════════════════════════════════════════════

def clean_soundcloud_url(url: str) -> str:
    """Убирает UTM-параметры из ссылки SoundCloud."""
    parsed = urlparse(url.strip())
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def build_scdl_command(url: str, output_dir: str) -> list[str]:
    """Собирает команду scdl с оригинальными метаданными."""
    return [
        "scdl", "-l", url,
        "--path", output_dir,
        "--overwrite",
        "--no-playlist-folder",
        "--original-metadata",
        "--original-art",
        "--onlymp3",
        "--auth-token", SC_TOKEN,
    ]


async def run_scdl(cmd: list[str]) -> tuple[bool, str]:
    """Запускает scdl асинхронно."""
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None,
        lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=240),
    )
    return result.returncode == 0, result.stdout + result.stderr


def collect_audio_files(directory: str) -> list[Path]:
    d = Path(directory)
    return sorted(p for p in d.rglob("*") if p.suffix.lower() in {".mp3", ".m4a", ".flac", ".ogg"})


def extract_audio_metadata(filepath: str, download_dir: str) -> dict:
    """Читает title, artist, thumbnail из MP3-тегов."""
    meta = {"title": None, "artist": None, "thumb_path": None}
    try:
        audio = MP3(filepath, ID3=ID3)
        if audio.tags:
            for tag in audio.tags.values():
                if isinstance(tag, TIT2) and tag.text:
                    meta["title"] = tag.text[0]
                elif isinstance(tag, TPE1) and tag.text:
                    meta["artist"] = tag.text[0]
                elif isinstance(tag, APIC) and tag.data:
                    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
                    tmp.write(tag.data)
                    tmp.close()
                    meta["thumb_path"] = tmp.name
    except Exception as e:
        logger.debug("Metadata read error: %s", e)

    if not meta["thumb_path"]:
        for ext in ("*.jpg", "*.jpeg", "*.png"):
            for f in Path(download_dir).glob(ext):
                meta["thumb_path"] = str(f)
                break
        if meta["thumb_path"]:
            pass

    return meta


async def fetch_latest_liked_track() -> dict | None:
    """Получает последний лайкнутый трек через soundcloud-v2."""
    try:
        loop = asyncio.get_event_loop()

        def _sync():
            sc = SoundCloud(auth_token=SC_TOKEN)
            me = sc.get_me()
            if me is None:
                return None
            for like in sc.get_user_likes(user_id=me.id, limit=1):
                track = getattr(like, "track", None)
                if track:
                    return {
                        "urn":           getattr(track, "urn", f"soundcloud:tracks:{track.id}"),
                        "title":         getattr(track, "title", "Unknown"),
                        "permalink_url": getattr(track, "permalink_url", ""),
                        "user":          getattr(track.user, "username", "") if track.user else "",
                    }
            return None

        return await loop.run_in_executor(None, _sync)
    except Exception as e:
        logger.warning("fetch_latest_liked_track error: %s", e)
        return None


async def download_and_send_to_chat(track_url: str, caption: str) -> bool:
    """Скачивает трек и отправляет в GROUP_CHAT_ID и в личку владельцу."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cmd = build_scdl_command(track_url, tmpdir)
        ok, output = await run_scdl(cmd)
        files = collect_audio_files(tmpdir)

        if not files:
            logger.warning("scdl failed: %s", output)
            return False

        audio_path = files[0]
        meta = extract_audio_metadata(str(audio_path), tmpdir)
        title = meta["title"] or audio_path.stem
        artist = meta["artist"]

        send_kwargs = {
            "title": title,
            "caption": caption,
            "parse_mode": "HTML",
        }
        if artist:
            send_kwargs["performer"] = artist
        if meta["thumb_path"]:
            send_kwargs["thumbnail"] = FSInputFile(meta["thumb_path"])

        # Отправляем в группу
        audio_file = FSInputFile(str(audio_path))
        await bot.send_audio(chat_id=GROUP_CHAT_ID, audio=audio_file, **send_kwargs)

        # И дублируем в личку владельцу (если группа отличается)
        if GROUP_CHAT_ID != OWNER_ID:
            audio_file2 = FSInputFile(str(audio_path))
            try:
                await bot.send_audio(chat_id=OWNER_ID, audio=audio_file2, **send_kwargs)
            except Exception as e:
                logger.warning("Owner DM failed: %s", e)

        return True


# ════════════════════════════════════════════════════════════════════════════
# БОТ
# ════════════════════════════════════════════════════════════════════════════

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())


def is_owner(message: Message) -> bool:
    return message.from_user and message.from_user.id == OWNER_ID


@dp.message(CommandStart())
async def cmd_start(message: Message):
    if not is_owner(message):
        return
    await message.answer(
        "🎵 <b>SoundCloud Bot</b>\n\n"
        "Бот работает в автоматическом режиме на Vercel.\n"
        "Cron-job.org проверяет новые лайки каждую минуту.\n\n"
        "<b>Команды:</b>\n"
        "/likes — скачать последний лайкнутый трек прямо сейчас\n"
        "/track <ссылка> — скачать любой трек по ссылке\n"
        "/status — проверить подключение к SoundCloud",
        parse_mode="HTML",
    )


@dp.message(Command("status"))
async def cmd_status(message: Message):
    if not is_owner(message):
        return
    await message.answer("⏳ Проверяю...")
    track = await fetch_latest_liked_track()
    if track:
        await message.answer(
            f"✅ Подключено к SoundCloud\n"
            f"Последний лайк: <b>{track['title']}</b> — {track['user']}\n"
            f"URN: <code>{track['urn']}</code>",
            parse_mode="HTML",
        )
    else:
        await message.answer(
            "❌ Не удалось получить данные с SoundCloud.\n"
            "Проверь SC_TOKEN в переменных окружения Vercel."
        )


@dp.message(Command("likes"))
async def cmd_likes(message: Message):
    if not is_owner(message):
        return
    status = await message.answer("⏳ Ищу последний лайкнутый трек...")
    track = await fetch_latest_liked_track()
    if not track:
        await status.edit_text("❌ Не удалось получить лайки. Проверь SC_TOKEN.")
        return

    url = clean_soundcloud_url(track["permalink_url"])
    caption = f'🎵 <a href="{track["permalink_url"]}">{track["title"]}</a>'
    await status.edit_text(f"⏳ Скачиваю: <b>{track['title']}</b>...", parse_mode="HTML")

    ok = await download_and_send_to_chat(url, caption)
    if ok:
        await status.delete()
    else:
        await status.edit_text("❌ Не удалось скачать трек.")


@dp.message(Command("track"))
async def cmd_track(message: Message):
    if not is_owner(message):
        return
    parts = message.text.split(maxsplit=1) if message.text else []
    if len(parts) < 2 or not parts[1].startswith("http"):
        await message.answer(
            "Пришли ссылку после команды:\n"
            "<code>/track https://soundcloud.com/artist/song</code>",
            parse_mode="HTML",
        )
        return

    url = clean_soundcloud_url(parts[1])
    status = await message.answer("⏳ Скачиваю трек...")
    caption = f'🎵 <a href="{url}">SoundCloud</a>'
    ok = await download_and_send_to_chat(url, caption)
    if ok:
        await status.delete()
    else:
        await status.edit_text("❌ Не удалось скачать.")


@dp.message(F.text)
async def any_message(message: Message):
    if not is_owner(message):
        return
    text = message.text or ""
    if "soundcloud.com/" in text:
        match = re.search(r"https?://(?:www\.)?soundcloud\.com/\S+", text)
        if not match:
            return
        url = clean_soundcloud_url(match.group(0))
        status = await message.answer("⏳ Нашёл ссылку SoundCloud, скачиваю...")
        caption = f'🎵 <a href="{url}">SoundCloud</a>'
        ok = await download_and_send_to_chat(url, caption)
        if ok:
            await status.delete()
        else:
            await status.edit_text("❌ Не удалось скачать.")


# ════════════════════════════════════════════════════════════════════════════
# CRON ENDPOINT — вызывается cron-job.org каждую минуту
# GET /cron?secret=<CRON_SECRET>
# ════════════════════════════════════════════════════════════════════════════

async def handle_cron(request: web.Request) -> web.Response:
    """Проверяет новые лайки и присылает трек если появился новый."""
    if request.rel_url.query.get("secret") != CRON_SECRET:
        return web.Response(status=403, text="Forbidden")

    try:
        track = await fetch_latest_liked_track()
        if not track:
            return web.Response(text="no track fetched")

        current_urn = track["urn"]
        prev_urn    = await redis_get("last_track_urn")

        if prev_urn is None:
            # Первый запуск — просто запоминаем, ничего не шлём
            await redis_set("last_track_urn", current_urn)
            logger.info("Cron: initial urn saved: %s", current_urn)
            return web.Response(text=f"init: {current_urn}")

        if current_urn == prev_urn:
            return web.Response(text="no new likes")

        # Новый лайк!
        logger.info("Cron: new like detected: %s", track["title"])
        await redis_set("last_track_urn", current_urn)

        url     = clean_soundcloud_url(track["permalink_url"])
        caption = f'🔔 <a href="{track["permalink_url"]}">{track["title"]}</a>'
        ok      = await download_and_send_to_chat(url, caption)

        return web.Response(text=f"sent: {track['title']}" if ok else "download failed")

    except Exception as e:
        logger.exception("Cron error: %s", e)
        return web.Response(status=500, text=str(e))


# ════════════════════════════════════════════════════════════════════════════
# ЗАПУСК (локально через polling, на Vercel — через webhook)
# ════════════════════════════════════════════════════════════════════════════

async def on_startup(app: web.Application):
    webhook_url = f"https://{VERCEL_URL}{WEBHOOK_PATH}"
    await bot.set_webhook(webhook_url, secret_token=WEBHOOK_SECRET)
    logger.info("Webhook set: %s", webhook_url)


async def on_shutdown(app: web.Application):
    await bot.delete_webhook()
    await bot.session.close()


def make_app() -> web.Application:
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    # Telegram webhook
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET).register(
        app, path=WEBHOOK_PATH
    )
    setup_application(app, dp, bot=bot)

    # Cron endpoint
    app.router.add_get("/cron", handle_cron)

    return app


# Точка входа для Vercel (WSGI/ASGI через aiohttp)
app = make_app()


if __name__ == "__main__":
    # Локальный запуск (polling вместо webhook для тестирования)
    async def _local():
        await bot.delete_webhook()
        logger.info("Local polling mode...")
        await dp.start_polling(bot, skip_updates=True)
    asyncio.run(_local())
