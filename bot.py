import asyncio
import os
import discord
from discord.ext import commands
from discord import app_commands
import yt_dlp

# ================== KONFIGURASI ==================
TOKEN = os.environ["DISCORD_TOKEN"]

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

YDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
}

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamer_index 0 -reconnect_delay_max 5",
    "options": "-vn",
}

# Antrian lagu per server (guild_id -> list of dict{title, url, requester})
queues: dict[int, list] = {}


def get_queue(guild_id: int) -> list:
    if guild_id not in queues:
        queues[guild_id] = []
    return queues[guild_id]


async def search_song(query: str) -> dict:
    """Cari lagu di YouTube berdasarkan query/link, return info dasar."""
    loop = asyncio.get_event_loop()

    def _extract():
        with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
            info = ydl.extract_info(query, download=False)
            if "entries" in info:
                info = info["entries"][0]
            return info

    info = await loop.run_in_executor(None, _extract)
    return {
        "title": info.get("title", "Unknown"),
        "url": info.get("url"),
        "webpage_url": info.get("webpage_url", query),
        "duration": info.get("duration", 0),
    }


async def play_next(guild: discord.Guild, text_channel: discord.abc.Messageable):
    guild_queue = get_queue(guild.id)
    voice_client = guild.voice_client

    if not guild_queue:
        return

    song = guild_queue.pop(0)
    source = discord.FFmpegPCMAudio(song["url"], **FFMPEG_OPTIONS)

    def after_playing(error):
        if error:
            print(f"Error saat memutar: {error}")
        fut = asyncio.run_coroutine_threadsafe(
            play_next(guild, text_channel), bot.loop
        )
        try:
            fut.result()
        except Exception as e:
            print(f"Error di after_playing: {e}")

    voice_client.play(source, after=after_playing)
    asyncio.create_task(
        text_channel.send(f"🎶 Sedang memutar: **{song['title']}**")
    )


# ================== EVENTS ==================
@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"Sinkron {len(synced)} slash command.")
    except Exception as e:
        print(f"Gagal sync command: {e}")
    print(f"Bot login sebagai {bot.user}")


# ================== SLASH COMMANDS ==================
@bot.tree.command(name="join", description="Bot join ke voice channel kamu")
async def join(interaction: discord.Interaction):
    if not interaction.user.voice:
        await interaction.response.send_message(
            "❌ Kamu harus join voice channel dulu.", ephemeral=True
        )
        return

    channel = interaction.user.voice.channel
    if interaction.guild.voice_client:
        await interaction.guild.voice_client.move_to(channel)
    else:
        await channel.connect()
    await interaction.response.send_message(f"✅ Bergabung ke **{channel.name}**")


@bot.tree.command(name="play", description="Putar lagu dari YouTube (judul atau link)")
@app_commands.describe(query="Judul lagu atau link YouTube")
async def play(interaction: discord.Interaction, query: str):
    await interaction.response.defer()

    if not interaction.user.voice:
        await interaction.followup.send("❌ Kamu harus join voice channel dulu.")
        return

    voice_client = interaction.guild.voice_client
    if voice_client is None:
        voice_client = await interaction.user.voice.channel.connect()

    try:
        song = await search_song(query)
    except Exception as e:
        await interaction.followup.send(f"❌ Gagal mencari lagu: {e}")
        return

    song["requester"] = interaction.user.display_name
    guild_queue = get_queue(interaction.guild.id)
    guild_queue.append(song)

    if voice_client.is_playing() or voice_client.is_paused():
        await interaction.followup.send(f"➕ Ditambahkan ke antrian: **{song['title']}**")
    else:
        await interaction.followup.send(f"🎶 Memutar sekarang: **{song['title']}**")
        await play_next(interaction.guild, interaction.channel)


@bot.tree.command(name="pause", description="Jeda lagu yang sedang diputar")
async def pause(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await interaction.response.send_message("⏸️ Lagu dijeda.")
    else:
        await interaction.response.send_message("❌ Tidak ada lagu yang sedang diputar.")


@bot.tree.command(name="resume", description="Lanjutkan lagu yang dijeda")
async def resume(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await interaction.response.send_message("▶️ Lagu dilanjutkan.")
    else:
        await interaction.response.send_message("❌ Tidak ada lagu yang dijeda.")


@bot.tree.command(name="skip", description="Lewati lagu yang sedang diputar")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()  # akan otomatis trigger play_next lewat callback 'after'
        await interaction.response.send_message("⏭️ Lagu dilewati.")
    else:
        await interaction.response.send_message("❌ Tidak ada lagu yang sedang diputar.")


@bot.tree.command(name="stop", description="Stop musik dan kosongkan antrian")
async def stop(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc:
        get_queue(interaction.guild.id).clear()
        vc.stop()
        await interaction.response.send_message("⏹️ Musik dihentikan, antrian dikosongkan.")
    else:
        await interaction.response.send_message("❌ Bot tidak sedang di voice channel.")


@bot.tree.command(name="queue", description="Lihat antrian lagu")
async def queue_cmd(interaction: discord.Interaction):
    guild_queue = get_queue(interaction.guild.id)
    if not guild_queue:
        await interaction.response.send_message("📭 Antrian kosong.")
        return

    text = "\n".join(
        f"{i+1}. **{s['title']}** (diminta oleh {s['requester']})"
        for i, s in enumerate(guild_queue)
    )
    await interaction.response.send_message(f"📜 **Antrian lagu:**\n{text}")


@bot.tree.command(name="leave", description="Bot keluar dari voice channel")
async def leave(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc:
        get_queue(interaction.guild.id).clear()
        await vc.disconnect()
        await interaction.response.send_message("👋 Bot keluar dari voice channel.")
    else:
        await interaction.response.send_message("❌ Bot tidak sedang di voice channel.")


if __name__ == "__main__":
    bot.run(TOKEN)
