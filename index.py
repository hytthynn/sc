"""
Точка входа для Vercel на FastAPI (нативный ASGI).
Заменяет aiohttp-сервер — все эндпоинты перенесены сюда.
"""
import asyncio
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from fastapi import FastAPI, Request, Response
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, FSInputFile, Update
import aiohttp
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1, APIC
from soundcloud import SoundCloud

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Конфиг из env ─────────────────────────────────────────────────────────────
BOT_TOKEN      = os.environ["BOT_TOKEN"]
SC_TOKEN       = os.environ["SC_TOKEN"]
GROUP_CHAT_ID  = int(os.environ["GROUP_CHAT_ID"])
OWNER_ID       = 1822182658
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "sc_secret_42")
CRON_SECRET    = os.environ.get("CRON_SECRET", "cron_secret_42")
UPSTASH_URL    = os.environ["UPSTASH_URL"]
UPSTASH_TOKEN  = os.environ["UPSTASH_TOKEN"]
VERCEL_URL     = os.environ.get("VERCEL_URL", "")
WEBHOOK_PATH   = "/webhook"

# ── Bot + Dispatcher ──────────────────────────────────────────────────────────
bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())

# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI()


# ════════════════════════════════════════════════════════════════════════════
# REDIS
# ════════════════════════════════════════════════════════════════════════════

async def redis_get(key: str) -> str | None:
    async with aiohttp.ClientSession() as s:
        async with s.get(
            f"{UPSTASH_URL}/get/{key}",
            headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"},
        ) as r:
            return (await r.json()).get("result")

async def redis_set(key: str, value: str):
    async with aiohttp.ClientSession() as s:
        await s.get(
            f"{UPSTASH_URL}/set/{key}/{value}",
            headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"},
        )


# ════════════════════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════════════════════

def clean_url(url: str) -> str:
    p = urlparse(url.strip())
    return urlunparse((p.scheme, p.netloc, p.path, "", "", ""))

def build_scdl_cmd(url: str, out: str) -> list[str]:
    return ["scdl", "-l", url, "--path", out, "--overwrite",
            "--no-playlist-folder", "--original-metadata",
            "--original-art", "--onlymp3", "--auth-token", SC_TOKEN]

async def run_scdl(cmd: list[str]) -> tuple[bool, str]:
    loop = asyncio.get_event_loop()
    r = await loop.run_in_executor(
        None, lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=240)
    )
    return r.returncode == 0, r.stdout + r.stderr

def collect_audio(d: str) -> list[Path]:
    return sorted(p for p in Path(d).rglob("*") if p.suffix.lower() in {".mp3", ".m4a"})

def get_meta(filepath: str, d: str) -> dict:
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
                    t = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
                    t.write(tag.data); t.close()
                    meta["thumb_path"] = t.name
    except Exception:
        pass
    if not meta["thumb_path"]:
        for f in list(Path(d).glob("*.jpg")) + list(Path(d).glob("*.png")):
            meta["thumb_path"] = str(f); break
    return meta

async def fetch_latest_like() -> dict | None:
    try:
        loop = asyncio.get_event_loop()
        def _sync():
            sc = SoundCloud(auth_token=SC_TOKEN)
            me = sc.get_me()
            if not me: return None
            for like in sc.get_user_likes(user_id=me.id, limit=1):
                t = getattr(like, "track", None)
                if t:
                    return {
                        "urn": getattr(t, "urn", f"soundcloud:tracks:{t.id}"),
                        "title": getattr(t, "title", "Unknown"),
                        "permalink_url": getattr(t, "permalink_url", ""),
                        "user": getattr(t.user, "username", "") if t.user else "",
                    }
            return None
        return await loop.run_in_executor(None, _sync)
    except Exception as e:
        logger.warning("fetch error: %s", e)
        return None

async def send_track(url: str, caption: str) -> bool:
    with tempfile.TemporaryDirectory() as d:
        ok, out = await run_scdl(build_scdl_cmd(url, d))
        files = collect_audio(d)
        if not files:
            logger.warning("scdl failed: %s", out)
            return False
        meta = get_meta(str(files[0]), d)
        kw = {"title": meta["title"] or files[0].stem, "caption": caption, "parse_mode": "HTML"}
        if meta["artist"]: kw["performer"] = meta["artist"]
        if meta["thumb_path"]: kw["thumbnail"] = FSInputFile(meta["thumb_path"])
        await bot.send_audio(GROUP_CHAT_ID, FSInputFile(str(files[0])), **kw)
        if GROUP_CHAT_ID != OWNER_ID:
            try:
                await bot.send_audio(OWNER_ID, FSInputFile(str(files[0])), **kw)
            except Exception: pass
        return True


# ════════════════════════════════════════════════════════════════════════════
# AIOGRAM HANDLERS
# ════════════════════════════════════════════════════════════════════════════

def is_owner(m: Message) -> bool:
    return m.from_user and m.from_user.id == OWNER_ID

@dp.message(CommandStart())
async def cmd_start(m: Message):
    if not is_owner(m): return
    await m.answer(
        "🎵 <b>SoundCloud Bot</b>\n\n"
        "Команды:\n/likes — последний лайк\n/track <ссылка> — скачать трек\n/status — статус",
        parse_mode="HTML")

@dp.message(Command("status"))
async def cmd_status(m: Message):
    if not is_owner(m): return
    await m.answer("⏳ Проверяю...")
    t = await fetch_latest_like()
    if t:
        await m.answer(f"✅ Подключено\nПоследний лайк: <b>{t['title']}</b>", parse_mode="HTML")
    else:
        await m.answer("❌ Ошибка подключения к SoundCloud. Проверь SC_TOKEN.")

@dp.message(Command("likes"))
async def cmd_likes(m: Message):
    if not is_owner(m): return
    s = await m.answer("⏳ Ищу последний лайк...")
    t = await fetch_latest_like()
    if not t:
        await s.edit_text("❌ Не удалось получить лайки."); return
    url = clean_url(t["permalink_url"])
    await s.edit_text(f"⏳ Скачиваю: <b>{t['title']}</b>...", parse_mode="HTML")
    ok = await send_track(url, f'🎵 <a href="{t["permalink_url"]}">{t["title"]}</a>')
    if ok: await s.delete()
    else: await s.edit_text("❌ Не удалось скачать.")

@dp.message(Command("track"))
async def cmd_track(m: Message):
    if not is_owner(m): return
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].startswith("http"):
        await m.answer("Пришли ссылку: <code>/track https://soundcloud.com/...</code>", parse_mode="HTML")
        return
    url = clean_url(parts[1])
    s = await m.answer("⏳ Скачиваю...")
    ok = await send_track(url, f'🎵 <a href="{url}">SoundCloud</a>')
    if ok: await s.delete()
    else: await s.edit_text("❌ Не удалось скачать.")

@dp.message(F.text)
async def any_msg(m: Message):
    if not is_owner(m): return
    text = m.text or ""
    if "soundcloud.com/" in text:
        match = re.search(r"https?://(?:www\.)?soundcloud\.com/\S+", text)
        if not match: return
        url = clean_url(match.group(0))
        s = await m.answer("⏳ Скачиваю...")
        ok = await send_track(url, f'🎵 <a href="{url}">SoundCloud</a>')
        if ok: await s.delete()
        else: await s.edit_text("❌ Не удалось скачать.")


# ════════════════════════════════════════════════════════════════════════════
# FASTAPI ROUTES
# ════════════════════════════════════════════════════════════════════════════

@app.on_event("startup")
async def startup():
    webhook_url = f"https://{VERCEL_URL}{WEBHOOK_PATH}"
    await bot.set_webhook(webhook_url, secret_token=WEBHOOK_SECRET)
    logger.info("Webhook set: %s", webhook_url)

@app.post(WEBHOOK_PATH)
async def webhook(request: Request):
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        return Response(status_code=403)
    data = await request.json()
    update = Update.model_validate(data)
    await dp.feed_update(bot, update)
    return Response(status_code=200)

@app.get("/cron")
async def cron(request: Request):
    if request.query_params.get("secret") != CRON_SECRET:
        return Response(status_code=403)
    try:
        track = await fetch_latest_like()
        if not track:
            return Response(content="no track fetched")
        current_urn = track["urn"]
        prev_urn    = await redis_get("last_track_urn")
        if prev_urn is None:
            await redis_set("last_track_urn", current_urn)
            return Response(content=f"init: {current_urn}")
        if current_urn == prev_urn:
            return Response(content="no new likes")
        await redis_set("last_track_urn", current_urn)
        url     = clean_url(track["permalink_url"])
        caption = f'🔔 <a href="{track["permalink_url"]}">{track["title"]}</a>'
        ok      = await send_track(url, caption)
        return Response(content=f"sent: {track['title']}" if ok else "download failed")
    except Exception as e:
        logger.exception("Cron error")
        return Response(status_code=500, content=str(e))

@app.get("/")
async def health():
    return {"status": "ok"}
