import os
import re
import json
import asyncio
import aiohttp
import uvicorn

from urllib.parse import urlparse
from typing import Dict, Any

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

from pyrogram import Client, idle
from motor.motor_asyncio import AsyncIOMotorClient
from DvisSearch import FastYoutubeSearch

# ================= CONFIG =================

API_ID = int(os.getenv("API_ID", "123456"))
API_HASH = os.getenv("API_HASH", "API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN", "BOT_TOKEN")
SESSION_STRING = os.getenv("SESSION_STRING", "USER_SESSION_STRING")

MONGO_URL = os.getenv("MONGO_URL", "MONGO_URL")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1001234567890"))
PORT = int(os.getenv("PORT", "2020"))

BOT_LIMIT_MB = 1900

ALL_API = "https://allvideodownloader.cc/wp-json/aio-dl/video-data/"
ALL_TOKEN = "c99f113fab0762d216b4545e5c3d615eefb30f0975fe107caab629d17e51b52d"

ALL_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "User-Agent": "Mozilla/5.0 (Linux; Android 14) Chrome/131 Mobile",
}

os.makedirs("downloads", exist_ok=True)

# ================= TELEGRAM CLIENTS =================

bot = Client(
    "EraBot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

user = Client(
    "EraUser",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
)

# ================= DATABASE =================

mongo = AsyncIOMotorClient(MONGO_URL)
db = mongo.EraApi
videodb = db.videodb
audiodb = db.audiodb

def get_db(media_type: str):
    return videodb if media_type == "video" else audiodb

async def get_cached(video_id: str, media_type: str):
    return await get_db(media_type).find_one({"id": video_id})

async def save_cached(video_id: str, link: str, media_type: str):
    await get_db(media_type).insert_one({"id": video_id, "link": link})

async def delete_cached(video_id: str, media_type: str):
    await get_db(media_type).delete_one({"id": video_id})

# ================= HELPERS =================

def parse_query(q: str) -> str:
    if m := re.search(r'(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})', q):
        return f"https://www.youtube.com/watch?v={m.group(1)}"
    return q

def duration_to_seconds(d: str) -> int:
    if not d or ":" not in d:
        return 0
    s = 0
    for x in d.split(":"):
        s = s * 60 + int(x)
    return s

def file_size_mb(path: str) -> float:
    return os.path.getsize(path) / (1024 * 1024)

async def check_media_exists(tg_link: str) -> bool:
    try:
        parts = urlparse(tg_link).path.strip("/").split("/")
        if parts[0] == "c":
            chat = int(f"-100{parts[1]}")
            msg_id = int(parts[2])
        else:
            chat = parts[0]
            msg_id = int(parts[1])

        msg = await bot.get_messages(chat, msg_id)
        return bool(msg and (msg.document or msg.audio or msg.video))
    except Exception:
        return False

# ================= DOWNLOADER =================

async def fetch_all_downloader(url: str) -> Dict[str, Any]:
    async with aiohttp.ClientSession(headers=ALL_HEADERS) as session:
        async with session.post(
            ALL_API,
            data={"url": url, "token": ALL_TOKEN},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as r:
            r.raise_for_status()
            data = await r.json()
            if not data.get("medias"):
                raise Exception("No media found")
            return data

async def download_file(dl_url: str, path: str):
    async with aiohttp.ClientSession() as session:
        async with session.get(dl_url) as r:
            r.raise_for_status()
            with open(path, "wb") as f:
                async for chunk in r.content.iter_chunked(1024 * 64):
                    f.write(chunk)

# ================= QUALITY PICKER =================

def pick_best_media(medias: list, is_video: bool) -> dict:
    items = []
    for m in medias:
        q = m.get("quality", "").lower()
        if is_video and ("mp4" in q or "webm" in q):
            res = max([int(r) for r in re.findall(r"\d{3,4}", q)] or [0])
            items.append((res, m))
        elif not is_video and ("kb/s" in q or "m4a" in q or "opus" in q):
            br = max([int(b) for b in re.findall(r"\d{2,3}", q)] or [0])
            items.append((br, m))
    items.sort(key=lambda x: x[0], reverse=True)
    return items[0][1]

# ================= DOWNLOAD =================

async def download_media(video_id: str, is_video: bool):
    yt_url = f"https://www.youtube.com/watch?v={video_id}"
    data = await fetch_all_downloader(yt_url)
    media = pick_best_media(data["medias"], is_video)

    ext = "mp4" if is_video else "m4a"
    path = f"downloads/{video_id}.{ext}"

    await download_file(media["url"], path)
    return path, media["url"]

# ================= UPLOAD =================

async def upload_to_channel(path: str, title: str, duration: int, is_video: bool):
    size_mb = file_size_mb(path)
    client = bot if size_mb <= BOT_LIMIT_MB else user

    if is_video:
        return await client.send_document(
            CHANNEL_ID,
            document=path,
            file_name=f"{title[:40]}.mp4"
        )
    else:
        return await client.send_audio(
            CHANNEL_ID,
            audio=path,
            duration=duration,
            title=title,
            performer="EraApi"
        )

async def background_upload(path, title, duration, is_video, video_id, media_type):
    try:
        msg = await upload_to_channel(path, title, duration, is_video)
        link = msg.link or f"https://t.me/c/{str(CHANNEL_ID)[4:]}/{msg.id}"
        await save_cached(video_id, link, media_type)
    finally:
        if os.path.exists(path):
            os.remove(path)

# ================= FASTAPI =================

app = FastAPI(title="EraApi")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/try")
async def get_media(query: str = Query(...), video: bool = Query(False)):
    q = parse_query(query)

    if "youtube.com/watch" in q:
        video_id = q.split("v=")[-1][:11]
        title = "YouTube Media"
        duration = "0:00"
    else:
        search = FastYoutubeSearch(q, max_results=1)
        result = json.loads(search.to_json())["videos"]
        if not result:
            return {"error": "No result found"}
        info = result[0]
        video_id = info["id"]
        title = info["title"]
        duration = info["duration"]

    media_type = "video" if video else "audio"

    cached = await get_cached(video_id, media_type)
    if cached and await check_media_exists(cached["link"]):
        return {"from": "telegram", "download": cached["link"]}
    elif cached:
        await delete_cached(video_id, media_type)

    path, direct_url = await download_media(video_id, video)

    asyncio.create_task(
        background_upload(
            path,
            title,
            duration_to_seconds(duration),
            video,
            video_id,
            media_type
        )
    )

    return {
        "from": "downloader",
        "direct": direct_url,
        "telegram": "uploading"
    }

# ================= MAIN =================

async def main():
    await bot.start()
    await user.start()

    server = uvicorn.Server(
        uvicorn.Config(app, host="0.0.0.0", port=PORT, loop="asyncio")
    )
    await asyncio.gather(server.serve(), idle())

if __name__ == "__main__":
    asyncio.run(main())
