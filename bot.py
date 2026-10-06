import os
import json
import time
import shutil
import asyncio
import tempfile

from pyrogram import Client, filters


def need(name):
    v = os.environ.get(name, "").strip()
    if not v:
        raise SystemExit(f"Secret '{name}' set nahi hai. Repo Settings > Secrets mein add karo.")
    return v


API_ID = int(need("API_ID"))
API_HASH = need("API_HASH")
BOT_TOKEN = need("BOT_TOKEN")
OWNER_ID = int(need("OWNER_ID"))  # sirf tum use kar sako

app = Client(
    "audio_extractor",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True,
)

# codec -> file extension (original quality mein copy hota hai)
EXT = {
    "aac": "aac", "ac3": "ac3", "eac3": "eac3", "opus": "opus",
    "mp3": "mp3", "flac": "flac", "vorbis": "ogg", "dts": "dts",
    "truehd": "thd", "pcm_s16le": "wav",
}

only_owner = filters.user(OWNER_ID)

BAR_LEN = 12
UPDATE_EVERY = 3  # seconds (Telegram flood se bachne ke liye)


# ---------------------------------------------------------------- helpers
def human_size(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def human_time(sec):
    sec = int(max(sec, 0))
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def bar(percent):
    filled = int(BAR_LEN * percent / 100)
    return "█" * filled + "░" * (BAR_LEN - filled)


def render(title, step, current, total, start):
    percent = (current * 100 / total) if total else 0
    elapsed = max(time.time() - start, 0.001)
    speed = current / elapsed
    eta = (total - current) / speed if speed > 0 else 0
    return (
        f"**{title}**\n"
        f"{step}\n\n"
        f"`[{bar(percent)}]` **{percent:.1f}%**\n\n"
        f"📦 {human_size(current)} / {human_size(total)}\n"
        f"⚡ {human_size(speed)}/s\n"
        f"⏳ ETA: {human_time(eta)}"
    )


async def progress_cb(current, total, status, title, step, start, state):
    """Pyrogram ka progress callback (download/upload dono ke liye)."""
    now = time.time()
    if current != total and now - state["last"] < UPDATE_EVERY:
        return
    state["last"] = now
    try:
        await status.edit(render(title, step, current, total, start))
    except Exception:
        pass  # FloodWait / MessageNotModified ignore


async def run(*cmd):
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode, out, err


async def get_audio_streams(path):
    code, out, _ = await run(
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-select_streams", "a", "-show_streams", path,
    )
    if code != 0:
        raise RuntimeError("ffprobe fail hua, file video nahi lag rahi.")
    return json.loads(out).get("streams", [])


# --------------------------------------------------------------- handlers
@app.on_message(filters.command("start") & only_owner)
async def start(client, message):
    await message.reply(
        "👋 Mujhe koi video bhejo, main uske saare audio tracks alag-alag nikaal dunga."
    )


@app.on_message((filters.video | filters.document) & only_owner)
async def handle(client, message):
    workdir = tempfile.mkdtemp(prefix="audiobot_")
    status = await message.reply("⏳ Shuru ho raha hai...")
    t0 = time.time()

    try:
        # ---- 1. Download
        start = time.time()
        path = await message.download(
            file_name=os.path.join(workdir, "input"),
            progress=progress_cb,
            progress_args=(status, "📥 Downloading", "Video download ho raha hai",
                           start, {"last": 0}),
        )

        # ---- 2. Analyze
        await status.edit("🔍 **Analyzing**\nAudio tracks check ho rahe hain...")
        streams = await get_audio_streams(path)
        if not streams:
            await status.edit("❌ Is video mein koi audio track nahi mila.")
            return

        total_tracks = len(streams)
        sent = 0

        for n, s in enumerate(streams):
            codec = s.get("codec_name", "")
            ext = EXT.get(codec, "mka")
            tags = s.get("tags", {})
            lang = tags.get("language", "und")
            title = tags.get("title", "")
            out = os.path.join(workdir, f"audio_{n + 1}_{lang}.{ext}")
            label = f"Track {n + 1}/{total_tracks}"

            # ---- 3. Extract
            await status.edit(
                f"⚙️ **Extracting**\n{label} ({lang}, {codec}) nikaal raha hoon..."
            )
            code, _, _ = await run(
                "ffmpeg", "-y", "-i", path,
                "-map", f"0:a:{n}", "-vn", "-c", "copy", out,
            )
            if code != 0 or not os.path.exists(out):
                await message.reply(f"⚠️ {label} nikalne mein error aaya.")
                continue

            # ---- 4. Upload
            caption = f"🎧 Track {n + 1} | {lang} | {codec}"
            if title:
                caption += f" | {title}"

            up_start = time.time()
            await message.reply_document(
                out,
                caption=caption,
                progress=progress_cb,
                progress_args=(status, "📤 Uploading", f"{label} upload ho raha hai",
                               up_start, {"last": 0}),
            )
            os.remove(out)
            sent += 1

        await status.edit(
            f"✅ **Done!**\n{sent}/{total_tracks} audio track bhej diye.\n"
            f"⏱ Total time: {human_time(time.time() - t0)}"
        )

    except Exception as e:
        try:
            await status.edit(f"❌ Error: {e}")
        except Exception:
            pass
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    app.run()
