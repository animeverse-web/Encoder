import os
import json
import shutil
import asyncio
import tempfile

from pyrogram import Client, filters

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])  # sirf tum use kar sako

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


async def run(*cmd):
    """Command chalao aur (returncode, stdout, stderr) wapas do."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode, out, err


async def get_audio_streams(path):
    code, out, err = await run(
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-select_streams", "a", "-show_streams", path,
    )
    if code != 0:
        raise RuntimeError("ffprobe fail hua, file video nahi lag rahi.")
    return json.loads(out).get("streams", [])


@app.on_message(filters.command("start") & only_owner)
async def start(client, message):
    await message.reply(
        "Mujhe koi video bhejo, main uske saare audio tracks alag-alag nikaal dunga."
    )


@app.on_message((filters.video | filters.document) & only_owner)
async def handle(client, message):
    workdir = tempfile.mkdtemp(prefix="audiobot_")
    status = await message.reply("Download ho raha hai...")

    try:
        path = await message.download(file_name=os.path.join(workdir, "input"))

        streams = await get_audio_streams(path)
        if not streams:
            await status.edit("Is video mein koi audio track nahi mila.")
            return

        await status.edit(f"{len(streams)} audio track mile, nikaal raha hoon...")

        for n, s in enumerate(streams):
            codec = s.get("codec_name", "")
            ext = EXT.get(codec, "mka")
            tags = s.get("tags", {})
            lang = tags.get("language", "und")
            title = tags.get("title", "")
            out = os.path.join(workdir, f"audio_{n + 1}_{lang}.{ext}")

            code, _, err = await run(
                "ffmpeg", "-y", "-i", path,
                "-map", f"0:a:{n}", "-vn", "-c", "copy", out,
            )
            if code != 0 or not os.path.exists(out):
                await message.reply(f"Track {n + 1} nikalne mein error aaya.")
                continue

            caption = f"Track {n + 1} | {lang} | {codec}"
            if title:
                caption += f" | {title}"
            await message.reply_document(out, caption=caption)
            os.remove(out)

        await status.delete()

    except Exception as e:
        await status.edit(f"Error: {e}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    app.run()
