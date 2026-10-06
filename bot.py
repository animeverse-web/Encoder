import os
import re
import json
import time
import shutil
import asyncio
import tempfile
from datetime import datetime, timezone

from pyrogram import Client, filters
import firebase_admin
from firebase_admin import credentials, db


def need(name):
    v = os.environ.get(name, "").strip()
    if not v:
        raise SystemExit(f"Secret '{name}' set nahi hai. Repo Settings > Secrets mein add karo.")
    return v


API_ID = int(need("API_ID"))
API_HASH = need("API_HASH")
BOT_TOKEN = need("BOT_TOKEN")
OWNER_ID = int(need("OWNER_ID"))  # sirf tum use kar sako

# GitHub Actions mein ye dono apne aap mil jate hain (workflow mein GH_TOKEN set hai)
GH_REPO = os.environ.get("GITHUB_REPOSITORY", "").strip()
GH_ENABLED = bool(GH_REPO and os.environ.get("GH_TOKEN"))
MAX_ASSETS_PER_RELEASE = 900  # GitHub limit 1000 hai, thoda margin rakha

# Audio Telegram par bhi bhejna hai? Nahi chahiye to workflow mein SEND_TO_TELEGRAM=0 kar do
SEND_TO_TELEGRAM = os.environ.get("SEND_TO_TELEGRAM", "1") == "1"

# Inhi saari qualities mein same audio tracks Firebase mein daale jayenge
QUALITIES = [
    q.strip() for q in os.environ.get("QUALITIES", "1080p,720p,480p").split(",") if q.strip()
]

FB_ROOT = "audio_tracks"  # Firebase mein top-level node

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

# ffprobe language code -> (short code, naam)
LANGS = {
    "hin": ("hi", "Hindi"), "eng": ("en", "English"), "jpn": ("ja", "Japanese"),
    "tam": ("ta", "Tamil"), "tel": ("te", "Telugu"), "kor": ("ko", "Korean"),
    "chi": ("zh", "Chinese"), "zho": ("zh", "Chinese"), "spa": ("es", "Spanish"),
    "fre": ("fr", "French"), "fra": ("fr", "French"), "ger": ("de", "German"),
    "deu": ("de", "German"), "ita": ("it", "Italian"), "por": ("pt", "Portuguese"),
    "rus": ("ru", "Russian"), "ara": ("ar", "Arabic"), "mal": ("ml", "Malayalam"),
    "kan": ("kn", "Kannada"), "ben": ("bn", "Bengali"), "mar": ("mr", "Marathi"),
    "ind": ("id", "Indonesian"), "tha": ("th", "Thai"),
}

only_owner = filters.user(OWNER_ID)
SETUPS = {}  # user_id -> {"slug", "season"}

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


EP_RE = re.compile(r"\b(?:episode|ep)(?![a-z])\.?\s*[-:#]?\s*(\d{1,4})", re.I)
SE_RE = re.compile(r"\bs\d{1,2}\s*e(\d{1,4})\b", re.I)


def episode_from_caption(caption):
    """'Episode 1', 'Ep 12', 'ep.5', 'Ep1', 'S01E05' sab samajh leta hai."""
    if not caption:
        return None
    m = EP_RE.search(caption) or SE_RE.search(caption)
    return int(m.group(1)) if m else None


def lang_info(stream):
    tags = stream.get("tags", {})
    raw = "".join(c for c in tags.get("language", "und").lower() if c.isalnum()) or "und"
    short, name = LANGS.get(raw, (raw, ""))
    if not name:
        name = tags.get("title", "").strip() or raw.upper()
    return raw, short, name


def build_track(url, stream, is_default):
    """Firebase mein ek track ka format. Format badalna ho to sirf yahi function badlo."""
    _, short, name = lang_info(stream)
    return {
        "audio_link": url,
        "default": is_default,
        "lang": short,
        "name": name,
    }


# ----------------------------------------------------------- Firebase
def init_firebase():
    cred = credentials.Certificate(json.loads(need("FIREBASE_CREDENTIALS")))
    firebase_admin.initialize_app(cred, {"databaseURL": need("FIREBASE_DB_URL").rstrip("/")})


def fb_save_tracks(slug, season, episode, qualities, tracks):
    """Same tracks har quality ke neeche save karo. Saved paths ki list wapas do."""
    paths = []
    for q in qualities:
        path = f"{FB_ROOT}/{slug}/S{season}/E{episode}/{q}/tracks"
        db.reference(path).set(tracks)  # list -> keys 0, 1, 2 ...
        paths.append(path)
    return paths


# ----------------------------------------------------- GitHub Releases
async def get_release_tag():
    """Is mahine ka release tag do (audio-YYYY-MM). Bhar jaye to -2, -3 ... banao."""
    base = "audio-" + datetime.now(timezone.utc).strftime("%Y-%m")
    n = 1
    while True:
        tag = base if n == 1 else f"{base}-{n}"
        code, out, _ = await run(
            "gh", "release", "view", tag, "--repo", GH_REPO, "--json", "assets"
        )
        if code != 0:  # release abhi hai nahi, bana do
            code, _, err = await run(
                "gh", "release", "create", tag, "--repo", GH_REPO,
                "--title", tag, "--notes", "Extracted audio files",
            )
            if code != 0:
                raise RuntimeError(f"Release nahi ban saka: {err.decode().strip()}")
            return tag
        if len(json.loads(out).get("assets", [])) < MAX_ASSETS_PER_RELEASE:
            return tag
        n += 1


async def upload_to_github(file_path):
    """File ko GitHub Release mein daalo aur public link wapas do."""
    tag = await get_release_tag()
    code, _, err = await run(
        "gh", "release", "upload", tag, file_path, "--repo", GH_REPO, "--clobber"
    )
    if code != 0:
        raise RuntimeError(f"GitHub upload fail: {err.decode().strip()}")
    name = os.path.basename(file_path)
    return f"https://github.com/{GH_REPO}/releases/download/{tag}/{name}"


# --------------------------------------------------------------- handlers
@app.on_message(filters.command("start") & only_owner)
async def start(client, message):
    await message.reply(
        "👋 **Audio Extractor**\n\n"
        "1️⃣ `/setup anime-slug season`\n"
        "    jaise: `/setup naruto-shippuden 6`\n"
        "2️⃣ Phir ek video bhejo. Caption mein `Episode 1` ya `Ep 1` likha ho.\n\n"
        f"Audio tracks in qualities mein save honge: {', '.join(QUALITIES)}\n"
        "`/cancel` se setup hata sakte ho."
    )


@app.on_message(filters.command("setup") & only_owner)
async def setup(client, message):
    args = message.command[1:]
    usage = "Aise likho: `/setup anime-slug season`\nJaise: `/setup naruto-shippuden 6`"
    if len(args) < 2:
        return await message.reply(usage)

    slug = args[0].lower().strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", slug):
        return await message.reply("❌ Slug mein sirf a-z, 0-9, `-` aur `_` chalega.\n" + usage)

    season = re.search(r"\d+", args[1])
    if not season:
        return await message.reply("❌ Season ek number hona chahiye.\n" + usage)
    season = int(season.group())

    SETUPS[message.from_user.id] = {"slug": slug, "season": season}
    await message.reply(
        f"✅ **Setup ho gaya**\n"
        f"Anime: `{slug}`\nSeason: **S{season}**\n\n"
        f"Ab ek video bhejo (caption mein Episode number ke saath). 🎬"
    )


@app.on_message(filters.command("cancel") & only_owner)
async def cancel(client, message):
    SETUPS.pop(message.from_user.id, None)
    await message.reply("🗑 Setup hata diya.")


@app.on_message((filters.video | filters.document) & only_owner)
async def handle(client, message):
    uid = message.from_user.id
    cfg = SETUPS.get(uid)
    if not cfg:
        return await message.reply(
            "⚠️ Pehle setup karo:\n`/setup anime-slug season`\nJaise: `/setup naruto-shippuden 6`"
        )

    episode = episode_from_caption(message.caption)
    if episode is None:
        return await message.reply(
            "⚠️ Caption mein episode number nahi mila.\n"
            "Caption mein `Episode 1` ya `Ep 1` likh kar video dobara bhejo."
        )
    if not GH_ENABLED:
        return await message.reply("❌ GitHub token/repo nahi mila. Bot ko GitHub Actions mein chalao.")

    SETUPS.pop(uid, None)  # ek setup = ek video
    slug, season = cfg["slug"], cfg["season"]
    workdir = tempfile.mkdtemp(prefix="audiobot_")
    status = await message.reply(f"⏳ `{slug}` S{season} E{episode} shuru ho raha hai...")
    t0 = time.time()
    tracks = []
    ok = False

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
            raise RuntimeError("Is video mein koi audio track nahi mila.")

        total_tracks = len(streams)
        # default track: jisme "default" flag ho, nahi to pehla
        default_idx = next(
            (i for i, s in enumerate(streams) if s.get("disposition", {}).get("default") == 1), 0
        )

        for n, s in enumerate(streams):
            codec = s.get("codec_name", "")
            ext = EXT.get(codec, "mka")
            raw_lang, _, lang_name = lang_info(s)
            fname = f"{slug}_S{season}E{episode}_t{n + 1}_{raw_lang}.{ext}"
            out = os.path.join(workdir, fname)
            label = f"Track {n + 1}/{total_tracks}"

            # ---- 3. Extract
            await status.edit(f"⚙️ **Extracting**\n{label} • {lang_name} • {codec}")
            code, _, _ = await run(
                "ffmpeg", "-y", "-i", path,
                "-map", f"0:a:{n}", "-vn", "-c", "copy", out,
            )
            if code != 0 or not os.path.exists(out):
                await message.reply(f"⚠️ {label} nikalne mein error aaya.")
                continue

            # ---- 4. GitHub upload
            await status.edit(f"☁️ **GitHub Upload**\n{label} release mein daal raha hoon...")
            try:
                url = await upload_to_github(out)
            except Exception as e:
                await message.reply(f"⚠️ {label} GitHub par upload nahi hua: {e}")
                continue
            tracks.append(build_track(url, s, n == default_idx))

            # ---- 5. Telegram (optional)
            if SEND_TO_TELEGRAM:
                up_start = time.time()
                await message.reply_document(
                    out,
                    caption=f"🎧 {lang_name} | {codec}\n🔗 {url}",
                    progress=progress_cb,
                    progress_args=(status, "📤 Uploading", f"{label} Telegram par bhej raha hoon",
                                   up_start, {"last": 0}),
                )
            os.remove(out)

        if not tracks:
            raise RuntimeError("Koi bhi track GitHub par upload nahi ho paya.")

        # ---- 6. Firebase (har quality mein same tracks)
        await status.edit("🔥 **Firebase**\nData save ho raha hai...")
        await asyncio.to_thread(fb_save_tracks, slug, season, episode, QUALITIES, tracks)

        ok = True
        await status.edit(
            f"✅ **Done!**\n"
            f"`{slug}` • S{season} E{episode}\n"
            f"🎧 {len(tracks)}/{total_tracks} track save hue\n"
            f"🔥 Qualities: {', '.join(QUALITIES)}\n"
            f"📁 `{FB_ROOT}/{slug}/S{season}/E{episode}/<quality>/tracks`\n"
            f"⏱ Total time: {human_time(time.time() - t0)}"
        )

    except Exception as e:
        try:
            await status.edit(f"❌ Error: {e}\n\nSetup wapas laga diya hai, video dobara bhej sakte ho.")
        except Exception:
            pass
    finally:
        if not ok:
            SETUPS[uid] = cfg  # fail hua to dobara /setup nahi karna padega
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    init_firebase()
    app.run()
