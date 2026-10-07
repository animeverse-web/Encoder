import os
import re
import json
import base64
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

# Audio Telegram par wapas bhejna? Default band (slow hota hai). Chahiye to workflow mein SEND_TO_TELEGRAM=1 karo
SEND_TO_TELEGRAM = os.environ.get("SEND_TO_TELEGRAM", "0") == "1"

# Inhi saari qualities mein same audio tracks Firebase mein daale jayenge
QUALITIES = [
    q.strip() for q in os.environ.get("QUALITIES", "1080p,720p,480p").split(",") if q.strip()
]

FB_ROOT = "audio_tracks"  # Firebase mein top-level node (audio)
FB_SUB_ROOT = "subtitles"  # Firebase mein top-level node (subtitles)
SUB_DIR = "subs"           # repo ka folder jahan .vtt files commit hoti hain

# Sirf text wale subtitle .vtt mein badle ja sakte hain (image wale PGS/DVD nahi)
TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text"}
SUB_EXTS = (".srt", ".ass", ".ssa", ".vtt")

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


BY_SHORT = {short: (raw, short, name) for raw, (short, name) in LANGS.items()}
LANG_WORDS = {name.lower(): (raw, short, name) for raw, (short, name) in LANGS.items()}


def detect_lang(*texts):
    """Caption / file ke naam se language pakdo: 'Hindi', 'hin', 'hi', 'english'... Nahi mili to und."""
    for text in texts:
        for tok in re.findall(r"[a-z]+", (text or "").lower()):
            if tok in LANG_WORDS:
                return LANG_WORDS[tok]
            if tok in LANGS:
                short, name = LANGS[tok]
                return tok, short, name
            if tok in BY_SHORT:
                return BY_SHORT[tok]
    return "und", "und", "Subtitle"


def safe_key(v):
    """Firebase key / file naam ke liye safe (sirf a-z0-9)."""
    return re.sub(r"[^a-z0-9]", "", str(v).lower()) or "und"


async def get_subtitle_streams(path):
    code, out, _ = await run(
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-select_streams", "s", "-show_streams", path,
    )
    if code != 0:
        return []
    return json.loads(out).get("streams", [])


def read_text_utf8(path):
    """Subtitle file ko UTF-8 text mein padho (utf-8 / utf-16 / cp1252 sab chalega)."""
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", errors="replace")


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


def fb_save_subtitles(slug, season, episode, subs):
    """subtitles/{slug}/S{n}/E{n}/{lang} = {url, label, lang, format}. Same lang dobara aaye to replace."""
    path = f"{FB_SUB_ROOT}/{slug}/S{season}/E{episode}"
    db.reference(path).update(subs)
    return path


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


_branch = {}


async def default_branch():
    if "name" not in _branch:
        code, out, err = await run("gh", "api", f"repos/{GH_REPO}", "--jq", ".default_branch")
        if code != 0:
            raise RuntimeError(f"Repo ka branch nahi mila: {err.decode().strip()}")
        _branch["name"] = out.decode().strip()
    return _branch["name"]


async def commit_to_repo(local_path, repo_path, message):
    """File ko repo mein commit karo aur raw.githubusercontent.com ka link do.
    (Release ke links par CORS header nahi hota, raw par hota hai — website ko subtitle padhne ke liye chahiye.)"""
    branch = await default_branch()
    api = f"repos/{GH_REPO}/contents/{repo_path}"
    body = {
        "message": message,
        "branch": branch,
        "content": base64.b64encode(open(local_path, "rb").read()).decode(),
    }
    code, out, _ = await run("gh", "api", f"{api}?ref={branch}", "--jq", ".sha")
    if code == 0 and out.strip():
        body["sha"] = out.decode().strip()  # file pehle se hai -> update
    body_path = local_path + ".json"
    with open(body_path, "w") as f:
        json.dump(body, f)
    code, _, err = await run("gh", "api", "--method", "PUT", api, "--input", body_path)
    if code != 0:
        raise RuntimeError(f"Repo mein commit fail: {err.decode().strip()[:200]}")
    return f"https://raw.githubusercontent.com/{GH_REPO}/{branch}/{repo_path}"


async def commit_subtitle(vtt_path, slug, season, episode, key):
    repo_path = f"{SUB_DIR}/{slug}/S{season}E{episode}_{key}.vtt"
    return await commit_to_repo(vtt_path, repo_path, f"subtitle: {slug} S{season}E{episode} {key}")


# --------------------------------------------------------------- handlers
@app.on_message(filters.command("start") & only_owner)
async def start(client, message):
    await message.reply(
        "👋 **Audio Extractor**\n\n"
        "1️⃣ `/setup anime-slug season`\n"
        "    jaise: `/setup naruto-shippuden 6`\n"
        "2️⃣ Phir ya to video bhejo (caption mein `Episode 1`), ya **direct link** bhejo:\n"
        "    `https://.../video.mkv Episode 1`\n"
        "    (link se download Telegram se kaafi tez hota hai)\n\n"
        f"Audio tracks in qualities mein save honge: {', '.join(QUALITIES)}\n"
        "💬 **Subtitle file** (.srt/.ass/.vtt) bhi bhej sakte ho — caption mein `Ep 1 Hindi` likho.\n"
        "Video mein subtitle hon to wo bhi apne aap nikal jaate hain.\n\n"
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


URL_RE = re.compile(r"https?://\S+")
UA = "Mozilla/5.0"


async def url_size(url):
    """Link ki file ka size (HEAD request se). Nahi mila to 0."""
    _, out, _ = await run("curl", "-sIL", "-A", UA, "--max-time", "20", url)
    sizes = re.findall(rb"(?i)content-length:\s*(\d+)", out)
    return int(sizes[-1]) if sizes else 0


async def download_url(url, workdir, status):
    """Direct link se download. aria2c (16 connections) ho to wo, nahi to curl."""
    path = os.path.join(workdir, "input")
    logp = os.path.join(workdir, "dl.log")
    total = await url_size(url)

    if shutil.which("aria2c"):
        cmd = ["aria2c", "-x16", "-s16", "-k1M", "--file-allocation=none",
               "--allow-overwrite=true", "--summary-interval=0",
               "--console-log-level=error", f"--user-agent={UA}",
               "-d", workdir, "-o", "input", url]
    else:
        cmd = ["curl", "-L", "--fail", "-A", UA, "-o", path, url]

    start = time.time()
    state = {"last": 0}
    with open(logp, "wb") as log:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=log, stderr=log)
        while proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=UPDATE_EVERY)
            except asyncio.TimeoutError:
                pass
            if os.path.exists(path):
                # st_blocks = asli disk par likha hua data (multi-connection mein bhi sahi)
                cur = os.stat(path).st_blocks * 512
                if total:
                    cur = min(cur, total)
                await progress_cb(cur, total or cur, status, "📥 Downloading",
                                  "Direct link se download ho raha hai", start, state)

    if proc.returncode != 0 or not os.path.exists(path):
        try:
            lines = open(logp, errors="ignore").read().replace("\r", "\n").splitlines()
            errs = [l.strip() for l in lines if "rror" in l or "curl:" in l]
            tail = errs[-1][:200] if errs else ""
        except Exception:
            tail = ""
        raise RuntimeError(f"Link se download nahi hua. {tail}".strip())
    return path


async def fetch_telegram(message, workdir, status):
    start = time.time()
    return await message.download(
        file_name=os.path.join(workdir, "input"),
        progress=progress_cb,
        progress_args=(status, "📥 Downloading", "Video download ho raha hai",
                       start, {"last": 0}),
    )


async def get_cfg(message):
    cfg = SETUPS.get(message.from_user.id)
    if not cfg:
        await message.reply(
            "⚠️ Pehle setup karo:\n`/setup anime-slug season`\nJaise: `/setup naruto-shippuden 6`"
        )
    return cfg


async def process(message, cfg, episode, fetch):
    """Download -> audio nikalo -> GitHub -> Firebase. `fetch` file ko workdir mein laata hai."""
    uid = message.from_user.id
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
        path = await fetch(workdir, status)

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

            # ---- 5. Telegram (optional, default band)
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

        # ---- 6. Subtitles (text wale -> .vtt), koi bhi fail ho to baaki chalte rehte hain
        subs, skipped = {}, 0
        sub_streams = await get_subtitle_streams(path)
        for n, s in enumerate(sub_streams):
            codec = s.get("codec_name", "")
            if codec not in TEXT_SUB_CODECS:
                skipped += 1  # image wale (PGS/DVD) text nahi ban sakte
                continue
            _, short, name = lang_info(s)
            base = safe_key(short)
            key, i = base, 2
            while key in subs:
                key, i = f"{base}{i}", i + 1
            title = s.get("tags", {}).get("title", "").strip()
            label = name if key == base else (title or f"{name} {i - 1}")
            await status.edit(f"💬 **Subtitle** {n + 1}/{len(sub_streams)}\n{label} nikaal raha hoon...")
            vtt = os.path.join(workdir, f"{key}.vtt")
            code, _, _ = await run("ffmpeg", "-y", "-i", path, "-map", f"0:s:{n}", "-c:s", "webvtt", vtt)
            if code != 0 or not os.path.exists(vtt):
                await message.reply(f"⚠️ Subtitle {label}convert nahi hua.")
                continue
            try:
                url = await commit_subtitle(vtt, slug, season, episode, safe_key(key))
            except Exception as e:
                await message.reply(f"⚠️ Subtitle {label} save nahi hua: {e}")
                continue
            subs[safe_key(key)] = {"url": url, "label": label, "lang": short, "format": "vtt"}
 
        # ---- 7. Firebase (audio har quality mein, subtitle episode ke neeche)
        await status.edit("🔥 **Firebase**\nData save ho raha hai...")
        await asyncio.to_thread(fb_save_tracks, slug, season, episode, QUALITIES, tracks)
        if subs:
            await asyncio.to_thread(fb_save_subtitles, slug, season, episode, subs)
 
        ok = True
        await status.edit(
            f"✅ **Done!**\n"
            f"`{slug}` • S{season} E{episode}\n"
            f"🎧 {len(tracks)}/{total_tracks} track save hue\n"
            + (f"💬 {len(subs)} subtitle save hue" + (f" ({skipped} image-wale chhode)" if skipped else "") + "\n"
               if subs or skipped else "")
            + f"🔥 Qualities: {', '.join(QUALITIES)}\n"
            f"📁 `{FB_ROOT}/{slug}/S{season}/E{episode}/<quality>/tracks`\n"
            f"⏱ Total time: {human_time(time.time() - t0)}"
        )
 
    except Exception as e:
        try:
            await status.edit(f"❌ Error: {e}\n\nSetup wapas laga diya hai, dobara bhej sakte ho.")
        except Exception:
            pass
    finally:
        if not ok:
            SETUPS[uid] = cfg  # fail hua to dobara /setup nahi karna padega
        shutil.rmtree(workdir, ignore_errors=True)
 
 
def _is_sub_doc(_, __, m):
    d = m.document
    return bool(d and d.file_name and d.file_name.lower().endswith(SUB_EXTS))
 
 
sub_doc_filter = filters.create(_is_sub_doc)
 
 
@app.on_message(filters.document & sub_doc_filter & only_owner)
async def handle_sub_file(client, message):
    """Seedhi subtitle file (.srt/.ass/.vtt) -> .vtt -> repo -> Firebase. Setup ussi chalu rehta hai."""
    cfg = await get_cfg(message)
    if not cfg:
        return
    fname = message.document.file_name
    episode = episode_from_caption(message.caption)
    if episode is None:
        episode = episode_from_caption(fname)
    if episode is None:
        return await message.reply(
            "⚠️ Episode number nahi mila.\nCaption mein likho: `Ep 1 Hindi`"
        )
    if not GH_ENABLED:
        return await message.reply("❌ GitHub token/repo nahi mila. Bot ko GitHub Actions mein chalao.")
 
    slug, season = cfg["slug"], cfg["season"]
    raw3, short, name = detect_lang(message.caption, fname)
    key = safe_key(short)
    workdir = tempfile.mkdtemp(prefix="subbot_")
    status = await message.reply(f"💬 `{slug}` S{season} E{episode} • {name} subtitle...")
    try:
        ext = os.path.splitext(fname)[1].lower()
        src = os.path.join(workdir, "in" + ext)
        await message.download(file_name=src)
 
        # UTF-8 mein badlo (Hindi/Unicode wali files ke liye zaroori)
        text = read_text_utf8(src)
        with open(src, "w", encoding="utf-8") as f:
            f.write(text)
 
        vtt = os.path.join(workdir, f"{key}.vtt")
        if ext == ".vtt" and text.lstrip().startswith("WEBVTT"):
            shutil.copy(src, vtt)
        else:
            code, _, _ = await run("ffmpeg", "-y", "-i", src, "-c:s", "webvtt", vtt)
            if code != 0 or not os.path.exists(vtt):
                raise RuntimeError("Subtitle file convert nahi hui (format sahi hai?).")
 
        url = await commit_subtitle(vtt, slug, season, episode, key)
        entry = {"url": url, "label": name, "lang": short, "format": "vtt"}
        path = await asyncio.to_thread(fb_save_subtitles, slug, season, episode, {key: entry})
        await status.edit(
            f"✅ **Subtitle save ho gaya**\n"
            f"`{slug}` • S{season} E{episode} • {name} (`{short}`)\n"
            f"📁 `{path}/{key}`\n\n"
            f"Setup abhi chalu hai — aur subtitle ya video bhej sakte ho."
        )
    except Exception as e:
        try:
            await status.edit(f"❌ Error: {e}")
        except Exception:
            pass
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
 
 
@app.on_message((filters.video | filters.document) & only_owner)
async def handle_media(client, message):
    cfg = await get_cfg(message)
    if not cfg:
        return
    episode = episode_from_caption(message.caption)
    if episode is None:
        return await message.reply(
            "⚠️ Caption mein episode number nahi mila.\n"
            "Caption mein `Episode 1` ya `Ep 1` likh kar video dobara bhejo."
        )
    await process(message, cfg, episode, lambda wd, st: fetch_telegram(message, wd, st))
 
 
@app.on_message(
    filters.text & filters.regex(r"https?://\S+")
    & ~filters.command(["start", "setup", "cancel"]) & only_owner
)
async def handle_link(client, message):
    cfg = await get_cfg(message)
    if not cfg:
        return
    url = URL_RE.search(message.text).group(0)
    episode = episode_from_caption(URL_RE.sub(" ", message.text))  # pehle link ke bahar ka text
    if episode is None:
        episode = episode_from_caption(url)                          # nahi mila to link ke andar dekho
    if episode is None:
        return await message.reply(
            "⚠️ Episode number nahi mila.\nAise bhejo: `https://.../video.mkv Episode 1`"
        )
    await process(message, cfg, episode, lambda wd, st: download_url(url, wd, st))
 
 
if __name__ == "__main__":
    init_firebase()
    app.run()
