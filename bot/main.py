import os
import json
import asyncio
import random
import time
from typing import Optional
import discord
from discord import app_commands
from discord.ext import commands
from core.resolver import UniversalResolver
from core.player import GuildPlayer, QueuePaginator, EQ_LABELS

LOCALES_DIR = "/app/bot/locales" if os.path.exists("/app/bot/locales") else os.path.join(os.path.dirname(__file__), "locales")
SUPPORT_GUILD_ID = int(os.getenv("SUPPORT_GUILD_ID", 1039860460389941338))
SUPPORT_INVITE_URL = os.getenv("SUPPORT_INVITE_URL", "https://discord.com/invite/92BB9zGRmS")
OFFICIAL_WEBSITE_URL = os.getenv("OFFICIAL_WEBSITE_URL", "https://musicbot.bybaoge.com/")
DONATE_URL = os.getenv("DONATE_URL", "https://donate.bybaoge.com/")

_MEMBERSHIP_CACHE = {}

async def check_official_guild_membership(bot: commands.Bot, user_id: int) -> bool:
    if not SUPPORT_GUILD_ID:
        return True

    now = time.time()
    if user_id in _MEMBERSHIP_CACHE:
        cached_time, is_member = _MEMBERSHIP_CACHE[user_id]
        if now - cached_time < 600:
            return is_member

    official_guild = bot.get_guild(SUPPORT_GUILD_ID)
    if not official_guild:
        try:
            official_guild = await bot.fetch_guild(SUPPORT_GUILD_ID)
        except Exception:
            return True

    member = official_guild.get_member(user_id)
    if member:
        _MEMBERSHIP_CACHE[user_id] = (now, True)
        return True

    try:
        member = await official_guild.fetch_member(user_id)
        is_valid = member is not None
        _MEMBERSHIP_CACHE[user_id] = (now, is_valid)
        return is_valid
    except Exception:
        _MEMBERSHIP_CACHE[user_id] = (now, False)
        return False

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
        self.guild_locales = {}
        self.i18n = i18n

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
            await asyncio.sleep(15)

    async def send_welcome_announcement(self, guild: discord.Guild, inviter: Optional[discord.User] = None, is_startup: bool = False):
        loc = self.get_guild_locale(guild)

        if not is_startup:
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

    async def _broadcast_startup_announcements(self):
        sem = asyncio.Semaphore(5)

        async def _safe_send(guild: discord.Guild):
            async with sem:
                try:
                    await self.send_welcome_announcement(guild, is_startup=True)
                except Exception:
                    pass

        tasks = [_safe_send(g) for g in self.guilds]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

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
            self.status_task = self.loop.create_task(self.dynamic_presence_loop())
        self.loop.create_task(self._broadcast_startup_announcements())
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
            player.voice_client = await voice_channel.connect()
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

    if len(tracks) == 1:
        await interaction.followup.send(bot.i18n.get("ADDED_SINGLE", loc, title=tracks[0]['title']))
    else:
        await interaction.followup.send(bot.i18n.get("ADDED_BATCH", loc, count=len(tracks)))

@bot.tree.command(name="broadcast", description="[Owner Only] Broadcast announcement to all guilds")
@app_commands.describe(message="Announcement text")
async def broadcast(interaction: discord.Interaction, message: Optional[str] = None):
    if not await bot.is_owner(interaction.user):
        return await interaction.response.send_message("Only bot owner can use this.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    success = 0
    failed = 0

    default_desc = (
        "由於近期使用量在短時間內暴增，團隊正在緊急排查並修復各項系統問題，近期後端將會頻繁進行熱修復與重啟維護。\n\n"
        "為避免播歌中斷時無法掌握狀況，請所有使用者務必加入官方 Discord，即時獲取重啟進度與更新通知。"
    )

    embed = discord.Embed(
        title="官方重要維護與更新公告",
        description=message if message else default_desc,
        color=0x3498db
    )
    embed.add_field(name="官方 Discord 社群", value=f"[點擊此處立即加入]({SUPPORT_INVITE_URL})", inline=False)
    embed.add_field(name="音樂機器人官網", value=f"[musicbot.bybaoge.com]({OFFICIAL_WEBSITE_URL})", inline=False)
    embed.add_field(name="贊助支持", value=f"[donate.bybaoge.com]({DONATE_URL})", inline=False)
    embed.set_footer(text="BaoGe Official Announcement • bybaoge.com")

    b_view = discord.ui.View()
    b_view.add_item(discord.ui.Button(label="Discord Community", url=SUPPORT_INVITE_URL, style=discord.ButtonStyle.link))
    b_view.add_item(discord.ui.Button(label="Official Website", url=OFFICIAL_WEBSITE_URL, style=discord.ButtonStyle.link))
    b_view.add_item(discord.ui.Button(label="Donate / 贊助", url=DONATE_URL, style=discord.ButtonStyle.link))

    for guild in bot.guilds:
        target_channel = None
        if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
            target_channel = guild.system_channel
        else:
            candidates = ["公告", "general", "一般", "chat", "大廳", "聊天", "welcome"]
            for name_pattern in candidates:
                for channel in guild.text_channels:
                    if name_pattern in channel.name.lower() and channel.permissions_for(guild.me).send_messages:
                        target_channel = channel
                        break
                if target_channel:
                    break

        if not target_channel:
            for channel in guild.text_channels:
                if channel.permissions_for(guild.me).send_messages:
                    target_channel = channel
                    break

        if target_channel:
            try:
                await target_channel.send(embed=embed, view=b_view)
                success += 1
                await asyncio.sleep(0.5)
            except Exception:
                failed += 1
        else:
            failed += 1

    await interaction.followup.send(f"廣播發送完成。成功: {success} / 失敗: {failed}", ephemeral=True)

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

if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise ValueError("DISCORD_TOKEN environment variable not set.")
    bot.run(token)