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
from youtubesearchpython import VideosSearch

# ================= CONFIG =================

API_ID = int(os.getenv("API_ID", "14050586"))
API_HASH = os.getenv("API_HASH", "42a60d9c657b106370c79bb0a8ac560c")
BOT_TOKEN = os.getenv("BOT_TOKEN")
SESSION_STRING = os.getenv("SESSION_STRING")

MONGO_URL = os.getenv(
    "MONGO_URL",
    "mongodb+srv://Movieclone:movie12321@cluster0.bsbne.mongodb.net/?retryWrites=true&w=majority"
)

CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1003643287320"))
PORT = int(os.getenv("PORT", "2020"))

BOT_LIMIT_MB = 1900

ALL_API = "https://allvideodownloader.cc/wp-json/aio-dl/video-data/"
ALL_TOKEN = "c99f113fab0762d216b4545e5c3d615eefb30f0975fe107caab629d17e51b52d"

ALL_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "User-Agent": "Mozilla/5.0 (Linux; Android 14)",
}

os.makedirs("downloads", exist_ok=True)

# ================= TELEGRAM =================

bot = Client("EraBot", API_ID, API_HASH, bot_token=BOT_TOKEN)
user = Client("EraUser", API_ID, API_HASH, session_string=SESSION_STRING)

# ================= DATABASE =================

mongo = AsyncIOMotorClient(MONGO_URL)
db = mongo.EraApi
videodb = db.videodb
audiodb = db.audiodb

def get_db(media_type):
    return videodb if media_type == "video" else audiodb

async def get_cached(video_id, media_type):
    return await get_db(media_type).find_one({"id": video_id})

async def save_cached(video_id, link, media_type):
    await get_db(media_type).update_one(
        {"id": video_id},
        {"$set": {"link": link}},
        upsert=True
    )

async def delete_cached(video_id, media_type):
    await get_db(media_type).delete_one({"id": video_id})

# ================= HELPERS =================

def parse_query(q):
    m = re.search(r'(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})', q)
    if m:
        return f"https://www.youtube.com/watch?v={m.group(1)}"
    return q

def duration_to_seconds(d):
    if not d or ":" not in d:
        return 0
    s = 0
    for x in d.split(":"):
        s = s * 60 + int(x)
    return s

def file_size_mb(path):
    return os.path.getsize(path) / (1024 * 1024)

async def check_media_exists(link):
    try:
        parts = urlparse(link).path.strip("/").split("/")
        chat = parts[0]
        msg_id = int(parts[1])
        msg = await bot.get_messages(chat, msg_id)
        return bool(msg)
    except:
        return False

# ================= DOWNLOADER =================

async def fetch_all_downloader(url):
    async with aiohttp.ClientSession(headers=ALL_HEADERS) as session:
        async with session.post(
            ALL_API,
            data={"url": url, "token": ALL_TOKEN},
            timeout=30,
        ) as r:
            r.raise_for_status()
            data = await r.json()
            if not data.get("medias"):
                raise Exception("No media found")
            return data

def pick_best_media(medias, is_video):
    items = []
    for m in medias:
        q = m.get("quality", "").lower()
        if is_video and "mp4" in q:
            res = max([int(x) for x in re.findall(r"\d{3,4}", q)] or [0])
            items.append((res, m))
        elif not is_video and ("kb" in q or "m4a" in q):
            br = max([int(x) for x in re.findall(r"\d{2,3}", q)] or [0])
            items.append((br, m))
    items.sort(key=lambda x: x[0], reverse=True)
    return items[0][1]

async def download_file(url, path):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as r:
            r.raise_for_status()
            with open(path, "wb") as f:
                async for chunk in r.content.iter_chunked(65536):
                    f.write(chunk)

async def download_media(video_id, is_video):
    yt = f"https://www.youtube.com/watch?v={video_id}"
    data = await fetch_all_downloader(yt)
    media = pick_best_media(data["medias"], is_video)
    ext = "mp4" if is_video else "m4a"
    path = f"downloads/{video_id}.{ext}"
    await download_file(media["url"], path)
    return path, media["url"]

# ================= UPLOAD =================

async def upload_to_channel(path, title, duration, is_video):
    client = bot if file_size_mb(path) <= BOT_LIMIT_MB else user
    if is_video:
        return await client.send_document(CHANNEL_ID, path)
    else:
        return await client.send_audio(
            CHANNEL_ID,
            audio=path,
            title=title,
            duration=duration
        )

async def background_upload(path, title, duration, is_video, video_id, media_type):
    try:
        msg = await upload_to_channel(path, title, duration, is_video)
        await save_cached(video_id, msg.link, media_type)
    finally:
        if os.path.exists(path):
            os.remove(path)

# ================= FASTAPI =================

app = FastAPI()

@app.get("/try")
async def get_media(query: str, video: bool = False):
    try:
        q = parse_query(query)

        if "youtube.com/watch" in q:
            video_id = q.split("v=")[-1][:11]
            title = "YouTube Media"
            duration = "0:00"
        else:
            search = VideosSearch(q, limit=1)
            data = search.result().get("result", [])
            if not data:
                return {"error": "No result"}
            info = data[0]
            video_id = info["id"]
            title = info["title"]
            duration = info.get("duration", "0:00")

        media_type = "video" if video else "audio"

        cached = await get_cached(video_id, media_type)
        if cached and await check_media_exists(cached["link"]):
            return {"from": "telegram", "download": cached["link"]}

        path, direct = await download_media(video_id, video)

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

        return {"from": "downloader", "direct": direct, "telegram": "uploading"}

    except Exception as e:
        return {"error": str(e)}

# ================= MAIN =================

async def main():
    await bot.start()
    await user.start()
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=PORT))
    await asyncio.gather(server.serve(), idle())

if __name__ == "__main__":
    asyncio.run(main())