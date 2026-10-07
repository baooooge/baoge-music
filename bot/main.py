import os
import json
import asyncio
import random
import time
import logging
from typing import Optional, Dict
import discord
from discord import app_commands
from discord.ext import commands
from core.resolver import UniversalResolver
from core.player import GuildPlayer, QueuePaginator, EQ_LABELS

if os.name != "nt":
    try:
        import uvloop
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    except ImportError:
        pass

logging.getLogger("discord.player").setLevel(logging.WARNING)

LOCALES_DIR = "/app/bot/locales" if os.path.exists("/app/bot/locales") else os.path.join(os.path.dirname(__file__), "locales")
SUPPORT_GUILD_ID = int(os.getenv("SUPPORT_GUILD_ID", 1039860460389941338))
SUPPORT_INVITE_URL = os.getenv("SUPPORT_INVITE_URL", "https://discord.com/invite/92BB9zGRmS")
OFFICIAL_WEBSITE_URL = os.getenv("OFFICIAL_WEBSITE_URL", "https://musicbot.bybaoge.com/")
DONATE_URL = os.getenv("DONATE_URL", "https://donate.bybaoge.com/")

_MEMBERSHIP_CACHE = {}
_MEMBERSHIP_LOCK = asyncio.Lock()
_IN_FLIGHT_AUTH: Dict[int, asyncio.Future] = {}

async def check_official_guild_membership(bot: commands.Bot, user_id: int) -> bool:
    if not SUPPORT_GUILD_ID:
        return True

    now = time.time()
    if user_id in _MEMBERSHIP_CACHE:
        cached_time, is_member = _MEMBERSHIP_CACHE[user_id]
        if is_member and (now - cached_time < 600):
            return True
        if not is_member and (now - cached_time < 60):
            return False

    async with _MEMBERSHIP_LOCK:
        if user_id in _IN_FLIGHT_AUTH:
            return await _IN_FLIGHT_AUTH[user_id]
        fut = asyncio.get_running_loop().create_future()
        _IN_FLIGHT_AUTH[user_id] = fut

    try:
        official_guild = bot.get_guild(SUPPORT_GUILD_ID)
        if not official_guild:
            try:
                official_guild = await bot.fetch_guild(SUPPORT_GUILD_ID)
            except Exception:
                if not fut.done():
                    fut.set_result(True)
                return True

        member = official_guild.get_member(user_id)
        if member:
            _MEMBERSHIP_CACHE[user_id] = (now, True)
            if len(_MEMBERSHIP_CACHE) > 5000:
                _MEMBERSHIP_CACHE.pop(next(iter(_MEMBERSHIP_CACHE)), None)
            if not fut.done():
                fut.set_result(True)
            return True

        try:
            member = await official_guild.fetch_member(user_id)
            if member:
                _MEMBERSHIP_CACHE[user_id] = (now, True)
                if len(_MEMBERSHIP_CACHE) > 5000:
                    _MEMBERSHIP_CACHE.pop(next(iter(_MEMBERSHIP_CACHE)), None)
                if not fut.done():
                    fut.set_result(True)
                return True
        except Exception:
            pass

        _MEMBERSHIP_CACHE[user_id] = (now, False)
        if len(_MEMBERSHIP_CACHE) > 5000:
            _MEMBERSHIP_CACHE.pop(next(iter(_MEMBERSHIP_CACHE)), None)
        if not fut.done():
            fut.set_result(False)
        return False
    finally:
        async with _MEMBERSHIP_LOCK:
            _IN_FLIGHT_AUTH.pop(user_id, None)

class LocalizationManager:
    def __init__(self):
        self.translations = {}
        self.default_locale = "zh_TW"
        self._load_locales()

    def _load_locales(self):
        if not os.path.exists(LOCALES_DIR):
            return
        for file in os.listdir(LOCALES_DIR):
            if file.endswith(".json"):
                lang = file[:-5]
                try:
                    with open(os.path.join(LOCALES_DIR, file), "r", encoding="utf-8") as f:
                        self.translations[lang] = json.load(f)
                except Exception:
                    pass

    def get(self, key: str, locale: str = "zh_TW", **kwargs) -> str:
        target = locale if locale in self.translations else self.default_locale
        text = self.translations.get(target, {}).get(key)
        if not text:
            text = self.translations.get(self.default_locale, {}).get(key, key)
        if kwargs:
            try:
                return text.format(**kwargs)
            except Exception:
                return text
        return text

i18n = LocalizationManager()

class MusicBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        intents.guilds = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)
        self.resolver = UniversalResolver()
        self.players = {}
        self.status_task = None
        self.watcher_task = None
        self.guild_locales = {}
        self.i18n = i18n

    async def close(self):
        save_playback_state(self)
        if self.watcher_task and not self.watcher_task.done():
            self.watcher_task.cancel()
        if self.status_task and not self.status_task.done():
            self.status_task.cancel()
        await super().close()

    async def auto_hot_reload_loop(self):
        watch_dirs = [
            os.path.join(os.path.dirname(__file__), "core"),
            LOCALES_DIR
        ]
        mtimes = {}
        for d in watch_dirs:
            if os.path.exists(d):
                for f in os.listdir(d):
                    fp = os.path.join(d, f)
                    if os.path.isfile(fp):
                        mtimes[fp] = os.path.getmtime(fp)

        while not self.is_closed():
            await asyncio.sleep(3)
            changed = False
            for d in watch_dirs:
                if os.path.exists(d):
                    for f in os.listdir(d):
                        fp = os.path.join(d, f)
                        if os.path.isfile(fp):
                            current_mtime = os.path.getmtime(fp)
                            if fp in mtimes and current_mtime > mtimes[fp]:
                                changed = True
                            mtimes[fp] = current_mtime
            if changed:
                print("Detected file changes. Auto hot-reloading...")
                success, msg = await perform_hot_reload(self)
                print(f"Auto hot-reload result: {msg}")

    async def setup_hook(self):
        await self.tree.sync()
        print("Global application commands synced successfully.")

    def get_guild_locale(self, guild: Optional[discord.Guild]) -> str:
        if not guild:
            return "zh_TW"
        if guild.id in self.guild_locales:
            return self.guild_locales[guild.id]
        if guild.preferred_locale:
            clean_loc = str(guild.preferred_locale).replace("-", "_")
            if clean_loc in self.i18n.translations:
                return clean_loc
            if "ja" in clean_loc.lower():
                return "ja_JP"
            if "cn" in clean_loc.lower() or "hans" in clean_loc.lower():
                return "zh_CN"
            if "zh" in clean_loc.lower() or "hant" in clean_loc.lower():
                return "zh_TW"
            if "en" in clean_loc.lower():
                return "en_US"
        return "zh_TW"

    async def dynamic_presence_loop(self):
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                active_players = [p for p in self.players.values() if p.voice_client and p.voice_client.is_playing() and p.current]
                active_count = len(active_players)
                guild_count = len(self.guilds)

                if active_count > 0:
                    activity = discord.Activity(
                        type=discord.ActivityType.listening,
                        name=f"/play | 正在 {active_count} 個伺服器播放音樂"
                    )
                else:
                    activity = discord.Activity(
                        type=discord.ActivityType.listening,
                        name=f"/play | 服務於 {guild_count} 個伺服器"
                    )
                await self.change_presence(activity=activity, status=discord.Status.online)
            except Exception:
                pass
            await asyncio.sleep(30)

    async def send_welcome_announcement(self, guild: discord.Guild, inviter: Optional[discord.User] = None):
        loc = self.get_guild_locale(guild)

        target_user = inviter
        if not target_user:
            try:
                target_user = guild.owner or await self.fetch_user(guild.owner_id)
            except Exception:
                target_user = None

        if target_user and not target_user.bot:
            try:
                dm_embed = discord.Embed(
                    title=self.i18n.get("DM_TITLE", loc),
                    description=self.i18n.get("DM_DESC", loc, user=target_user.display_name, guild=guild.name),
                    color=0x3498db
                )
                dm_embed.add_field(name=self.i18n.get("DM_QUICKSTART_TITLE", loc), value=self.i18n.get("DM_QUICKSTART_VAL", loc), inline=False)
                dm_embed.add_field(name=self.i18n.get("DM_PERM_TITLE", loc), value=self.i18n.get("DM_PERM_VAL", loc), inline=False)
                dm_embed.add_field(name=self.i18n.get("DM_OFFICIAL_TITLE", loc), value=self.i18n.get("DM_OFFICIAL_VAL", loc), inline=False)

                view = discord.ui.View()
                view.add_item(discord.ui.Button(label="Discord Community", url=SUPPORT_INVITE_URL, style=discord.ButtonStyle.link))
                view.add_item(discord.ui.Button(label="Official Website", url=OFFICIAL_WEBSITE_URL, style=discord.ButtonStyle.link))
                view.add_item(discord.ui.Button(label="Donate / 贊助", url=DONATE_URL, style=discord.ButtonStyle.link))

                await target_user.send(embed=dm_embed, view=view)
            except Exception:
                pass

        target_channel = None
        if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
            target_channel = guild.system_channel
        else:
            candidates = ["公告", "general", "一般", "chat", "大廳", "聊天", "welcome", "雑談", "メイン"]
            sendable = [c for c in guild.text_channels if c.permissions_for(guild.me).send_messages]
            for pattern in candidates:
                for c in sendable:
                    if pattern in c.name.lower():
                        target_channel = c
                        break
                if target_channel:
                    break
            if not target_channel and sendable:
                target_channel = sendable[0]

        if not target_channel:
            return

        embed = discord.Embed(
            title=self.i18n.get("WELCOME_TITLE", loc),
            description=self.i18n.get("WELCOME_DESC", loc),
            color=0x3498db
        )
        if self.user and self.user.avatar:
            embed.set_thumbnail(url=self.user.avatar.url)

        embed.add_field(name=self.i18n.get("WELCOME_TUTORIAL_TITLE", loc), value=self.i18n.get("WELCOME_TUTORIAL_VAL", loc), inline=False)
        embed.add_field(name=self.i18n.get("WELCOME_AUTH_TITLE", loc), value=self.i18n.get("WELCOME_AUTH_VAL", loc, invite_url=SUPPORT_INVITE_URL), inline=False)
        embed.add_field(name=self.i18n.get("WELCOME_HIGHLIGHT_TITLE", loc), value=self.i18n.get("WELCOME_HIGHLIGHT_VAL", loc), inline=False)
        embed.add_field(name=self.i18n.get("WELCOME_OFFICIAL_TITLE", loc), value=self.i18n.get("WELCOME_OFFICIAL_VAL", loc), inline=False)
        embed.set_footer(text=self.i18n.get("FOOTER_TEXT", loc, guild_id=guild.id))

        guild_view = discord.ui.View()
        guild_view.add_item(discord.ui.Button(label="Discord Community", url=SUPPORT_INVITE_URL, style=discord.ButtonStyle.link))
        guild_view.add_item(discord.ui.Button(label="Official Website", url=OFFICIAL_WEBSITE_URL, style=discord.ButtonStyle.link))
        guild_view.add_item(discord.ui.Button(label="Donate / 贊助", url=DONATE_URL, style=discord.ButtonStyle.link))

        try:
            await target_channel.send(embed=embed, view=guild_view)
        except Exception:
            pass

    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})")
        initial_activity = discord.Activity(
            type=discord.ActivityType.listening,
            name=f"/play | 服務於 {len(self.guilds)} 個伺服器"
        )
        try:
            await self.change_presence(activity=initial_activity, status=discord.Status.online)
        except Exception:
            pass
        if not self.status_task:
            self.status_task = asyncio.create_task(self.dynamic_presence_loop())
        if not self.watcher_task:
            self.watcher_task = asyncio.create_task(self.auto_hot_reload_loop())
        asyncio.create_task(restore_playback_state(self))
        print("Ready and listening!")

    async def on_guild_join(self, guild: discord.Guild):
        try:
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        except Exception:
            pass

        inviter = None
        if guild.me.guild_permissions.view_audit_log:
            try:
                async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.bot_add):
                    if entry.target and entry.target.id == self.user.id:
                        inviter = entry.user
                        break
            except Exception:
                pass

        await self.send_welcome_announcement(guild, inviter=inviter)

    async def on_guild_remove(self, guild: discord.Guild):
        player = self.players.pop(guild.id, None)
        if player:
            player.stop_ticker()
            if player.voice_client:
                try:
                    await player.voice_client.disconnect(force=True)
                except Exception:
                    pass

    async def on_member_join(self, member: discord.Member):
        if member.guild.id == SUPPORT_GUILD_ID:
            _MEMBERSHIP_CACHE[member.id] = (time.time(), True)

    async def on_member_remove(self, member: discord.Member):
        if member.guild.id == SUPPORT_GUILD_ID:
            _MEMBERSHIP_CACHE.pop(member.id, None)

    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if after.guild.id == SUPPORT_GUILD_ID:
            _MEMBERSHIP_CACHE[after.id] = (time.time(), True)

    def get_player(self, guild_id: int) -> GuildPlayer:
        if guild_id not in self.players:
            self.players[guild_id] = GuildPlayer(self, guild_id, self.resolver)
        return self.players[guild_id]

bot = MusicBot()

@bot.command(name="sync")
@commands.is_owner()
async def sync(ctx):
    for guild in bot.guilds:
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
    global_synced = await bot.tree.sync()
    await ctx.send(f"Synced {len(global_synced)} commands to all {len(bot.guilds)} guilds.")

async def play_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    if not current:
        return []
    suggestions = await bot.resolver.get_search_suggestions(current)
    return [app_commands.Choice(name=s, value=s) for s in suggestions]

@bot.tree.command(name="play", description="播放歌曲 / Play audio / 音楽を再生")
@app_commands.describe(search="歌曲名稱或平台網址 / Track title or URL / 曲名またはURL")
@app_commands.autocomplete(search=play_autocomplete)
async def play(interaction: discord.Interaction, search: str):
    await interaction.response.defer()
    loc = bot.get_guild_locale(interaction.guild)

    is_member = await check_official_guild_membership(bot, interaction.user.id)
    if not is_member:
        embed = discord.Embed(
            title=bot.i18n.get("AUTH_REQUIRED_TITLE", loc),
            description=bot.i18n.get("AUTH_REQUIRED_DESC", loc, invite_url=SUPPORT_INVITE_URL, website_url=OFFICIAL_WEBSITE_URL, donate_url=DONATE_URL),
            color=0xe74c3c
        )
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Discord Community", url=SUPPORT_INVITE_URL, style=discord.ButtonStyle.link))
        view.add_item(discord.ui.Button(label="Official Website", url=OFFICIAL_WEBSITE_URL, style=discord.ButtonStyle.link))
        view.add_item(discord.ui.Button(label="Donate / 贊助", url=DONATE_URL, style=discord.ButtonStyle.link))
        return await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    if not interaction.user.voice or not interaction.user.voice.channel:
        return await interaction.followup.send(bot.i18n.get("JOIN_VOICE_FIRST", loc))

    voice_channel = interaction.user.voice.channel
    player = bot.get_player(interaction.guild_id)
    player.current_text_channel = interaction.channel

    try:
        if not player.voice_client or not player.voice_client.is_connected():
            player.voice_client = await voice_channel.connect(self_deaf=True)
        elif player.voice_client.channel != voice_channel:
            await player.voice_client.move_to(voice_channel)
    except Exception as e:
        return await interaction.followup.send(bot.i18n.get("CONNECT_FAIL", loc, error=e))

    tracks = await bot.resolver.resolve_metadata_batch(search)
    if not tracks:
        return await interaction.followup.send(bot.i18n.get("NO_AUDIO_FOUND", loc))

    for track in tracks:
        track["requester_id"] = interaction.user.id
        track["requester_name"] = interaction.user.display_name
        player.queue.append(track)
    player.ensure_audio_task()

    if len(tracks) == 1:
        await interaction.followup.send(bot.i18n.get("ADDED_SINGLE", loc, title=tracks[0]['title']))
    else:
        await interaction.followup.send(bot.i18n.get("ADDED_BATCH", loc, count=len(tracks)))

async def _set_language_handler(interaction: discord.Interaction, lang: app_commands.Choice[str]):
    bot.guild_locales[interaction.guild_id] = lang.value
    msg = bot.i18n.get("LANG_SWITCHED", lang.value)
    player = bot.get_player(interaction.guild_id)
    await player.update_panel_inplace()
    await interaction.response.send_message(msg)

@bot.tree.command(name="language", description="設定伺服器語言 / Set language / 言語設定")
@app_commands.choices(lang=[
    app_commands.Choice(name="繁體中文 (Traditional Chinese)", value="zh_TW"),
    app_commands.Choice(name="简体中文 (Simplified Chinese)", value="zh_CN"),
    app_commands.Choice(name="English (US)", value="en_US"),
    app_commands.Choice(name="日本語 (Japanese)", value="ja_JP")
])
@app_commands.default_permissions(manage_guild=True)
async def language(interaction: discord.Interaction, lang: app_commands.Choice[str]):
    await _set_language_handler(interaction, lang)

@bot.tree.command(name="lg", description="設定伺服器語言 (縮寫) / Set language (Alias)")
@app_commands.choices(lang=[
    app_commands.Choice(name="繁體中文 (Traditional Chinese)", value="zh_TW"),
    app_commands.Choice(name="简体中文 (Simplified Chinese)", value="zh_CN"),
    app_commands.Choice(name="English (US)", value="en_US"),
    app_commands.Choice(name="日本語 (Japanese)", value="ja_JP")
])
@app_commands.default_permissions(manage_guild=True)
async def lg(interaction: discord.Interaction, lang: app_commands.Choice[str]):
    await _set_language_handler(interaction, lang)

@bot.tree.command(name="previous", description="播放上一首歌曲 / Previous track / 前の曲を再生")
async def previous(interaction: discord.Interaction):
    player = bot.get_player(interaction.guild_id)
    await player.play_previous(interaction)

@bot.tree.command(name="seek", description="跳轉播放時間進度 / Seek position / 再生位置を指定")
@app_commands.describe(timestamp="時間格式 (1:30 或 90) / Time (1:30 or 90)")
async def seek(interaction: discord.Interaction, timestamp: str):
    player = bot.get_player(interaction.guild_id)
    if not player.current:
        return await interaction.response.send_message("No track playing.", ephemeral=True)

    seconds = 0
    if ":" in timestamp:
        parts = timestamp.split(":")
        try:
            if len(parts) == 2:
                seconds = int(parts[0]) * 60 + int(parts[1])
            elif len(parts) == 3:
                seconds = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        except ValueError:
            return await interaction.response.send_message("Invalid timestamp format.", ephemeral=True)
    else:
        try:
            seconds = int(timestamp)
        except ValueError:
            return await interaction.response.send_message("Please enter valid seconds.", ephemeral=True)

    await player.seek(seconds)
    await interaction.response.send_message(f"{seconds // 60:02d}:{seconds % 60:02d}")

@bot.tree.command(name="autoleave", description="無人時自動離線開關 / Toggle auto-disconnect / 自動退出切り替え")
async def autoleave(interaction: discord.Interaction):
    player = bot.get_player(interaction.guild_id)
    player.auto_disconnect = not player.auto_disconnect
    status = "ON" if player.auto_disconnect else "OFF (24/7)"
    await interaction.response.send_message(f"Auto-leave: {status}")
    await player.update_panel_inplace()

@bot.tree.command(name="skip", description="跳過當前歌曲 / Skip track / 曲をスキップ")
async def skip(interaction: discord.Interaction):
    player = bot.get_player(interaction.guild_id)
    await player.process_skip(interaction)

@bot.tree.command(name="skipto", description="跳轉至指定序號曲目 / Skip to index / 指定番号へスキップ")
@app_commands.describe(index="歌曲序號 / Track number / 曲番号")
async def skipto(interaction: discord.Interaction, index: int):
    player = bot.get_player(interaction.guild_id)
    if not player.queue or index < 1 or index > len(player.queue):
        return await interaction.response.send_message("Invalid index.", ephemeral=True)
    player.queue = player.queue[index - 1:]
    if player.voice_client:
        player.voice_client.stop()
    await interaction.response.send_message(f"#{index}")

@bot.tree.command(name="pause", description="暫停播放 / Pause / 一時停止")
async def pause(interaction: discord.Interaction):
    player = bot.get_player(interaction.guild_id)
    if player.voice_client and player.voice_client.is_playing():
        player.voice_client.pause()
        await interaction.response.send_message("Paused.")
    else:
        await interaction.response.send_message("Not playing.", ephemeral=True)

@bot.tree.command(name="resume", description="繼續播放 / Resume / 再開")
async def resume(interaction: discord.Interaction):
    player = bot.get_player(interaction.guild_id)
    if player.voice_client and player.voice_client.is_paused():
        player.voice_client.resume()
        await interaction.response.send_message("Resumed.")
    else:
        await interaction.response.send_message("Not paused.", ephemeral=True)

@bot.tree.command(name="stop", description="停止播放並清空隊列 / Stop and clear queue / 停止")
async def stop(interaction: discord.Interaction):
    player = bot.get_player(interaction.guild_id)
    player.queue.clear()
    player.history.clear()
    player.autoplay = False
    player.loop_mode = "off"
    if player.voice_client:
        if player.voice_client.is_playing() or player.voice_client.is_paused():
            player.voice_client.stop()
        await player.voice_client.disconnect()
    await interaction.response.send_message("Stopped.")

@bot.tree.command(name="queue", description="查看待播清單 / View queue / キュー確認")
async def queue(interaction: discord.Interaction):
    player = bot.get_player(interaction.guild_id)
    paginator = QueuePaginator(player.queue, player.current, locale=player.locale)
    await interaction.response.send_message(embed=paginator.make_embed(), view=paginator)

@bot.tree.command(name="nowplaying", description="查看當前播放曲目 / View now playing / 再生中の曲")
async def nowplaying(interaction: discord.Interaction):
    player = bot.get_player(interaction.guild_id)
    if not player.current:
        return await interaction.response.send_message("No track playing.", ephemeral=True)
    loc = player.locale
    embed = discord.Embed(
        title=bot.i18n.get("PANEL_TITLE", loc),
        description=f"[{player.current['title']}]({player.current['webpage_url']})",
        color=0x3498db
    )
    embed.set_thumbnail(url=player.current.get("thumbnail", ""))
    duration = f"{player.current.get('duration', 0) // 60:02d}:{player.current.get('duration', 0) % 60:02d}"
    embed.add_field(name=bot.i18n.get("PANEL_UPLOADER", loc), value=player.current.get("uploader", "Unknown"), inline=True)
    embed.add_field(name=bot.i18n.get("PANEL_PROGRESS", loc), value=duration, inline=True)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="volume", description="調整音量 (1 - 200) / Adjust volume / 音量調整")
@app_commands.describe(level="音量百分比 (1 - 200) / Volume percentage (1 - 200)")
async def volume(interaction: discord.Interaction, level: int):
    if level < 1 or level > 200:
        return await interaction.response.send_message("1 - 200 only.", ephemeral=True)
    player = bot.get_player(interaction.guild_id)
    player.volume = level / 100.0
    if player.voice_client and player.voice_client.source:
        player.voice_client.source.volume = player.volume
    await interaction.response.send_message(f"{level}%")

@bot.tree.command(name="equalizer", description="調整等化器 (EQ) / Change equalizer preset / EQ設定")
@app_commands.choices(preset=[
    app_commands.Choice(name="原音 / Flat", value="flat"),
    app_commands.Choice(name="重低音 / Bass Boost", value="bass"),
    app_commands.Choice(name="極限重低音 / Bass Extreme", value="bass_extreme"),
    app_commands.Choice(name="人聲清晰 / Vocal Boost", value="vocal"),
    app_commands.Choice(name="高音通透 / Treble Boost", value="treble")
])
async def equalizer(interaction: discord.Interaction, preset: app_commands.Choice[str]):
    player = bot.get_player(interaction.guild_id)
    await player.set_eq(preset.value)
    await interaction.response.send_message(preset.name)
    await player.update_panel_inplace()

@bot.tree.command(name="shuffle", description="隨機打亂隊列 / Shuffle queue / シャッフル")
async def shuffle(interaction: discord.Interaction):
    player = bot.get_player(interaction.guild_id)
    if len(player.queue) < 2:
        return await interaction.response.send_message("Queue too short.", ephemeral=True)
    random.shuffle(player.queue)
    await interaction.response.send_message("Shuffled.")

@bot.tree.command(name="loop", description="切換循環模式 / Set loop mode / ループ設定")
@app_commands.choices(mode=[
    app_commands.Choice(name="關閉 / Off", value="off"),
    app_commands.Choice(name="單曲循環 / Single", value="single"),
    app_commands.Choice(name="佇列循環 / Queue", value="queue")
])
async def loop(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    player = bot.get_player(interaction.guild_id)
    player.loop_mode = mode.value
    await interaction.response.send_message(mode.name)

@bot.tree.command(name="remove", description="刪除隊列歌曲 / Remove song from queue / 曲を削除")
@app_commands.describe(index="歌曲序號 / Track number / 曲番号")
async def remove(interaction: discord.Interaction, index: int):
    player = bot.get_player(interaction.guild_id)
    if index < 1 or index > len(player.queue):
        return await interaction.response.send_message("Invalid index.", ephemeral=True)
    removed = player.queue.pop(index - 1)
    await interaction.response.send_message(f"Removed: {removed['title']}")

@bot.tree.command(name="clear", description="清空隊列 / Clear queue / キューを消去")
async def clear(interaction: discord.Interaction):
    player = bot.get_player(interaction.guild_id)
    count = len(player.queue)
    player.queue.clear()
    await interaction.response.send_message(f"Cleared {count} tracks.")

STATE_FILE = os.path.join("/app/data" if os.path.exists("/app/data") else "data", "state_snapshot.json")

def save_playback_state(bot_instance: commands.Bot):
    state = {}
    for guild_id, player in bot_instance.players.items():
        if player.voice_client and player.voice_client.channel and (player.current or player.queue):
            elapsed = 0
            if player.voice_client.is_playing() and player.track_start_time > 0:
                elapsed = max(0, int(time.time() - player.track_start_time))
            state[str(guild_id)] = {
                "voice_channel_id": player.voice_client.channel.id,
                "text_channel_id": player.current_text_channel.id if player.current_text_channel else None,
                "current": player.current_meta or player.current,
                "elapsed": elapsed,
                "queue": list(player.queue),
                "volume": player.volume,
                "loop_mode": player.loop_mode,
                "autoplay": player.autoplay,
                "current_eq": player.current_eq
            }
    if state:
        try:
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f)
        except Exception:
            pass

async def _delayed_seek(player, pos: int):
    await asyncio.sleep(2.0)
    if player.voice_client and player.voice_client.is_playing():
        await player.seek(pos)

async def restore_playback_state(bot_instance: commands.Bot):
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        os.remove(STATE_FILE)
    except Exception:
        return

    sem = asyncio.Semaphore(5)

    async def _restore_single_guild(guild_id_str, data):
        async with sem:
            guild_id = int(guild_id_str)
            guild = bot_instance.get_guild(guild_id)
            if not guild:
                return
            v_channel = guild.get_channel(data.get("voice_channel_id"))
            if not v_channel:
                return

            player = bot_instance.get_player(guild_id)
            if data.get("text_channel_id"):
                player.current_text_channel = guild.get_channel(data["text_channel_id"])
            player.volume = data.get("volume", 1.0)
            player.loop_mode = data.get("loop_mode", "off")
            player.autoplay = data.get("autoplay", True)
            player.current_eq = data.get("current_eq", "flat")
            player.queue = data.get("queue", [])

            current_item = data.get("current")
            if current_item:
                player.queue.insert(0, current_item)

            try:
                if not player.voice_client or not player.voice_client.is_connected():
                    player.voice_client = await v_channel.connect(self_deaf=True)
                player.ensure_audio_task()
                seek_pos = data.get("elapsed", 0)
                if seek_pos > 0:
                    asyncio.create_task(_delayed_seek(player, seek_pos))
            except Exception:
                pass

    tasks = [_restore_single_guild(gid, d) for gid, d in state.items()]
    await asyncio.gather(*tasks, return_exceptions=True)

async def perform_hot_reload(bot_instance: commands.Bot) -> tuple[bool, str]:
    try:
        import importlib
        import core.resolver
        import core.player
        bot_instance.i18n._load_locales()
        importlib.reload(core.resolver)
        importlib.reload(core.player)
        bot_instance.resolver = core.resolver.UniversalResolver()
        for p in bot_instance.players.values():
            p.__class__ = core.player.GuildPlayer
            p.resolver = bot_instance.resolver
            p.ensure_audio_task()
            if p.panel_message:
                asyncio.create_task(p.update_panel_inplace(force=True))
        return True, "Core modules and locales hot-reloaded successfully. Voice playback uninterrupted."
    except Exception as e:
        return False, f"Hot-reload failed: {e}"

@bot.tree.command(name="reload", description="熱重載核心模組 (管理員專用) / Hot reload modules")
async def _reload_command(interaction: discord.Interaction):
    if not await bot.is_owner(interaction.user):
        return await interaction.response.send_message("Only the bot owner can reload modules.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    success, msg = await perform_hot_reload(bot)
    await interaction.followup.send(msg, ephemeral=True)

async def execute_server_broadcast(bot_instance: commands.Bot, message: str, title: str) -> tuple[int, int]:
    embed = discord.Embed(
        title=title,
        description=message,
        color=0xf1c40f
    )
    if bot_instance.user and bot_instance.user.avatar:
        embed.set_author(name=bot_instance.user.name, icon_url=bot_instance.user.avatar.url)
    embed.set_footer(text="BaoGe Music Official Broadcast")

    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Discord Community", url=SUPPORT_INVITE_URL, style=discord.ButtonStyle.link))
    view.add_item(discord.ui.Button(label="Official Website", url=OFFICIAL_WEBSITE_URL, style=discord.ButtonStyle.link))
    view.add_item(discord.ui.Button(label="Donate / 贊助", url=DONATE_URL, style=discord.ButtonStyle.link))

    sem = asyncio.Semaphore(5)
    success = 0
    failed = 0

    async def _send(guild: discord.Guild):
        nonlocal success, failed
        async with sem:
            target_channel = None
            if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
                target_channel = guild.system_channel
            else:
                candidates = ["公告", "general", "一般", "chat", "大廳", "聊天", "welcome", "雑談", "メイン"]
                sendable = [c for c in guild.text_channels if c.permissions_for(guild.me).send_messages]
                for pattern in candidates:
                    for c in sendable:
                        if pattern in c.name.lower():
                            target_channel = c
                            break
                    if target_channel:
                        break
                if not target_channel and sendable:
                    target_channel = sendable[0]

            if target_channel:
                try:
                    await target_channel.send(embed=embed, view=view)
                    success += 1
                    return
                except Exception:
                    pass
            failed += 1

    tasks = [_send(g) for g in bot_instance.guilds]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    return success, failed

@bot.command(name="broadcast")
@commands.is_owner()
async def cmd_broadcast(ctx: commands.Context, *, message: str):
    status_msg = await ctx.send("Broadcasting message to all servers...")
    success, failed = await execute_server_broadcast(bot, message, "官方系統公告 / Official Announcement")
    await status_msg.edit(content=f"Broadcast complete: Sent to {success} guilds ({failed} failed/skipped).")

@bot.tree.command(name="broadcast", description="[擁有者專用] 向所有伺服器推播公告 / Broadcast announcement to all servers")
@app_commands.describe(message="公告內容 / Message content", title="公告標題 / Title")
async def slash_broadcast(interaction: discord.Interaction, message: str, title: Optional[str] = "官方系統公告 / Official Announcement"):
    if not await bot.is_owner(interaction.user):
        return await interaction.response.send_message("Only the bot owner can use this command.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    success, failed = await execute_server_broadcast(bot, message, title)
    await interaction.followup.send(f"Broadcast complete: Sent to {success} guilds ({failed} failed/skipped).", ephemeral=True)

if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise ValueError("DISCORD_TOKEN environment variable not set.")
    if os.name != "nt":
        try:
            import signal
            signal.signal(signal.SIGTERM, lambda s, f: save_playback_state(bot))
            signal.signal(signal.SIGINT, lambda s, f: save_playback_state(bot))
            signal.signal(signal.SIGHUP, lambda s, f: asyncio.create_task(perform_hot_reload(bot)))
        except Exception:
            pass
    bot.run(token)