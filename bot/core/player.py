import asyncio
import random
import math
import time
import re
from typing import Optional, List, Dict, Any, Set
import discord

EQ_PRESETS = {
    "flat": "",
    "bass": "bass=g=7:f=110:w=0.6",
    "bass_extreme": "bass=g=13:f=100:w=0.5,equalizer=f=60:width_type=h:width=50:g=6",
    "vocal": "equalizer=f=1000:width_type=h:width=200:g=5,equalizer=f=3000:width_type=h:width=500:g=4,highpass=f=200,lowpass=f=8000",
    "treble": "treble=g=8:f=6000:w=0.6"
}

EQ_LABELS = {
    "zh_TW": {
        "flat": "原音 (關閉 EQ)",
        "bass": "重低音增強 (Bass Boost)",
        "bass_extreme": "極限重低音 (Bass Extreme)",
        "vocal": "人聲清晰 (Vocal Boost)",
        "treble": "高音通透 (Treble Boost)"
    },
    "zh_CN": {
        "flat": "原音 (关闭 EQ)",
        "bass": "重低音增强 (Bass Boost)",
        "bass_extreme": "极限重低音 (Bass Extreme)",
        "vocal": "人声清晰 (Vocal Boost)",
        "treble": "高音通透 (Treble Boost)"
    },
    "en_US": {
        "flat": "Flat (Off)",
        "bass": "Bass Boost",
        "bass_extreme": "Bass Extreme",
        "vocal": "Vocal Boost",
        "treble": "Treble Boost"
    },
    "ja_JP": {
        "flat": "原音 (フラット)",
        "bass": "低音強化 (Bass Boost)",
        "bass_extreme": "超重低音 (Bass Extreme)",
        "vocal": "ボーカル強調 (Vocal Boost)",
        "treble": "高音明瞭 (Treble Boost)"
    }
}

DEFAULT_BEFORE_OPTS = (
    "-loglevel error -nostats -reconnect 1 -reconnect_at_eof 1 -reconnect_streamed 1 "
    "-reconnect_delay_max 5 -rw_timeout 15000000 -probesize 64k -analyzeduration 0"
)

class SafeFFmpegPCMAudio(discord.FFmpegPCMAudio):
    def cleanup(self):
        proc = self._process
        if proc is None:
            return
        self._process = None
        try:
            if proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=0.2)
                except Exception:
                    try:
                        proc.kill()
                        proc.wait(timeout=0.1)
                    except Exception:
                        pass
            if proc.stdin:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
            if proc.stdout:
                try:
                    proc.stdout.close()
                except Exception:
                    pass
            if proc.stderr:
                try:
                    proc.stderr.close()
                except Exception:
                    pass
        except Exception:
            pass

class SkipToModal(discord.ui.Modal):
    def __init__(self, player):
        loc = player.locale
        titles = {
            "zh_TW": "跳至指定曲目",
            "zh_CN": "跳至指定曲目",
            "en_US": "Skip to Track",
            "ja_JP": "指定曲へジャンプ"
        }
        super().__init__(title=titles.get(loc, "Skip to Track"))
        self.player = player
        labels = {
            "zh_TW": "輸入歌曲序號",
            "zh_CN": "输入歌曲序号",
            "en_US": "Enter track number",
            "ja_JP": "曲番号を入力"
        }
        self.index_input = discord.ui.TextInput(
            label=labels.get(loc, "Track Number"),
            placeholder="3",
            min_length=1,
            max_length=4,
            required=True
        )
        self.add_item(self.index_input)

    async def on_submit(self, interaction: discord.Interaction):
        loc = self.player.locale
        try:
            target_index = int(self.index_input.value)
        except ValueError:
            msgs = {
                "zh_TW": "請輸入有效的數字！",
                "zh_CN": "请输入有效的数字！",
                "en_US": "Please enter a valid number!",
                "ja_JP": "有効な数値を入力してください！"
            }
            return await interaction.response.send_message(msgs.get(loc, "Invalid number!"), ephemeral=True)

        if target_index < 1 or target_index > len(self.player.queue):
            msgs = {
                "zh_TW": f"無效序號，目前隊列共有 1 到 {len(self.player.queue)} 首。",
                "zh_CN": f"无效序号，当前队列共有 1 到 {len(self.player.queue)} 首。",
                "en_US": f"Invalid number. Queue range is 1 to {len(self.player.queue)}.",
                "ja_JP": f"無効な番号です。1 〜 {len(self.player.queue)} の範囲で入力してください。"
            }
            return await interaction.response.send_message(msgs.get(loc, "Invalid range."), ephemeral=True)

        self.player.queue = self.player.queue[target_index - 1:]
        if self.player.voice_client:
            self.player.voice_client.stop()
        msgs = {
            "zh_TW": f"已跳至第 {target_index} 首歌曲。",
            "zh_CN": f"已跳至第 {target_index} 首歌曲。",
            "en_US": f"Skipped to track #{target_index}.",
            "ja_JP": f"#{target_index} 曲目へジャンプしました。"
        }
        await interaction.response.send_message(msgs.get(loc, "Skipped."), ephemeral=True)

class EQSelect(discord.ui.Select):
    def __init__(self, player):
        self.player = player
        loc = player.locale
        labels = EQ_LABELS.get(loc, EQ_LABELS["zh_TW"])
        options = [
            discord.SelectOption(label=labels["flat"], value="flat", default=(player.current_eq == "flat")),
            discord.SelectOption(label=labels["bass"], value="bass", default=(player.current_eq == "bass")),
            discord.SelectOption(label=labels["bass_extreme"], value="bass_extreme", default=(player.current_eq == "bass_extreme")),
            discord.SelectOption(label=labels["vocal"], value="vocal", default=(player.current_eq == "vocal")),
            discord.SelectOption(label=labels["treble"], value="treble", default=(player.current_eq == "treble")),
        ]
        placeholder = player.bot.i18n.get("EQ_PLACEHOLDER", loc)
        super().__init__(placeholder=placeholder, min_values=1, max_values=1, options=options, row=3)

    async def callback(self, interaction: discord.Interaction):
        selected = self.values[0]
        await self.player.set_eq(selected)
        loc = self.player.locale
        label = EQ_LABELS.get(loc, EQ_LABELS["zh_TW"])[selected]
        msgs = {
            "zh_TW": f"等化器效果已切換為：{label}",
            "zh_CN": f"均衡器效果已切换为：{label}",
            "en_US": f"Equalizer preset changed to: {label}",
            "ja_JP": f"イコライザー設定を {label} に変更しました"
        }
        await interaction.response.send_message(msgs.get(loc, "EQ changed."), ephemeral=True)
        await self.player.update_panel_inplace()

class PlayerControls(discord.ui.View):
    def __init__(self, player):
        super().__init__(timeout=None)
        self.player = player
        loc = player.locale
        i18n = player.bot.i18n

        self.prev_btn.label = i18n.get("BTN_PREV", loc)
        self.play_btn.label = i18n.get("BTN_PLAY_PAUSE", loc)
        self.skip_btn.label = i18n.get("BTN_SKIP", loc)
        self.skipto_btn.label = i18n.get("BTN_SKIPTO", loc)
        self.loop_btn.label = i18n.get("BTN_LOOP", loc)
        self.autoplay_btn.label = i18n.get("BTN_AUTOPLAY", loc)
        self.shuffle_btn.label = i18n.get("BTN_SHUFFLE", loc)
        self.queue_btn.label = i18n.get("BTN_QUEUE", loc)
        self.stop_btn.label = i18n.get("BTN_STOP", loc)
        self.voldown_btn.label = i18n.get("BTN_VOL_DOWN", loc)
        self.volup_btn.label = i18n.get("BTN_VOL_UP", loc)
        self.volreset_btn.label = i18n.get("BTN_VOL_RESET", loc)
        self.clear_btn.label = i18n.get("BTN_CLEAR", loc)

        self.add_item(EQSelect(player))

    @discord.ui.button(label="上一首", style=discord.ButtonStyle.secondary, row=0)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.player.play_previous(interaction)

    @discord.ui.button(label="暫停/繼續", style=discord.ButtonStyle.primary, row=0)
    async def play_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        loc = self.player.locale
        if not self.player.voice_client:
            msgs = {
                "zh_TW": "目前沒有連線至語音頻道。",
                "zh_CN": "当前未连接到语音频道。",
                "en_US": "Not connected to any voice channel.",
                "ja_JP": "ボイスチャンネルに接続されていません。"
            }
            return await interaction.response.send_message(msgs.get(loc, "Not connected."), ephemeral=True)
        if self.player.voice_client.is_paused():
            self.player.voice_client.resume()
            self.player.track_start_time += time.time() - self.player.pause_time
            msgs = {
                "zh_TW": "已繼續播放。",
                "zh_CN": "已继续播放。",
                "en_US": "Resumed playback.",
                "ja_JP": "再生を再開しました。"
            }
            await interaction.response.send_message(msgs.get(loc, "Resumed."), ephemeral=True)
        elif self.player.voice_client.is_playing():
            self.player.voice_client.pause()
            self.player.pause_time = time.time()
            msgs = {
                "zh_TW": "已暫停播放。",
                "zh_CN": "已暂停播放。",
                "en_US": "Paused playback.",
                "ja_JP": "再生を一時停止しました。"
            }
            await interaction.response.send_message(msgs.get(loc, "Paused."), ephemeral=True)
        await self.player.update_panel_inplace()

    @discord.ui.button(label="跳過", style=discord.ButtonStyle.secondary, row=0)
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.player.process_skip(interaction)

    @discord.ui.button(label="-15s", style=discord.ButtonStyle.secondary, row=0)
    async def rewind_15(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.player.voice_client or not self.player.current:
            return await interaction.response.send_message("Not playing.", ephemeral=True)
        current_pos = max(0, int(time.time() - self.player.track_start_time))
        new_pos = max(0, current_pos - 15)
        await self.player.seek(new_pos)
        await interaction.response.send_message(f"{new_pos // 60:02d}:{new_pos % 60:02d}", ephemeral=True)

    @discord.ui.button(label="+15s", style=discord.ButtonStyle.secondary, row=0)
    async def fast_forward_15(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.player.voice_client or not self.player.current:
            return await interaction.response.send_message("Not playing.", ephemeral=True)
        current_pos = max(0, int(time.time() - self.player.track_start_time))
        new_pos = current_pos + 15
        await self.player.seek(new_pos)
        await interaction.response.send_message(f"{new_pos // 60:02d}:{new_pos % 60:02d}", ephemeral=True)

    @discord.ui.button(label="跳至指定", style=discord.ButtonStyle.secondary, row=1)
    async def skipto_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        loc = self.player.locale
        if not self.player.queue:
            msgs = {
                "zh_TW": "目前隊列中沒有後續曲目可跳轉。",
                "zh_CN": "当前队列中没有后续曲目可跳转。",
                "en_US": "Queue is empty.",
                "ja_JP": "キューが空です。"
            }
            return await interaction.response.send_message(msgs.get(loc, "Queue empty."), ephemeral=True)
        await interaction.response.send_modal(SkipToModal(self.player))

    @discord.ui.button(label="循環", style=discord.ButtonStyle.secondary, row=1)
    async def loop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        modes = ["off", "single", "queue"]
        current_idx = modes.index(self.player.loop_mode) if self.player.loop_mode in modes else 0
        self.player.loop_mode = modes[(current_idx + 1) % len(modes)]
        loc = self.player.locale
        i18n = self.player.bot.i18n
        labels = {"off": i18n.get("MODE_OFF", loc), "single": i18n.get("MODE_SINGLE", loc), "queue": i18n.get("MODE_QUEUE", loc)}
        msgs = {
            "zh_TW": f"循環模式切換為：{labels[self.player.loop_mode]}",
            "zh_CN": f"循环模式切换为：{labels[self.player.loop_mode]}",
            "en_US": f"Loop mode set to: {labels[self.player.loop_mode]}",
            "ja_JP": f"ループモードを {labels[self.player.loop_mode]} に設定しました"
        }
        await interaction.response.send_message(msgs.get(loc, "Loop mode updated."), ephemeral=True)
        await self.player.update_panel_inplace()

    @discord.ui.button(label="自動續播", style=discord.ButtonStyle.secondary, row=1)
    async def autoplay_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.player.autoplay = not self.player.autoplay
        loc = self.player.locale
        msgs = {
            "zh_TW": f"自動推薦續播模式已{'開啟' if self.player.autoplay else '關閉'}。",
            "zh_CN": f"自动推荐续播模式已{'开启' if self.player.autoplay else '关闭'}。",
            "en_US": f"Autoplay {'enabled' if self.player.autoplay else 'disabled'}.",
            "ja_JP": f"自動連続再生を{'有効' if self.player.autoplay else '無効'}にしました。"
        }
        await interaction.response.send_message(msgs.get(loc, "Autoplay updated."), ephemeral=True)
        await self.player.update_panel_inplace()

    @discord.ui.button(label="隨機打亂", style=discord.ButtonStyle.secondary, row=1)
    async def shuffle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        loc = self.player.locale
        if len(self.player.queue) < 2:
            msgs = {
                "zh_TW": "佇列歌曲不足以打亂。",
                "zh_CN": "队列歌曲不足以打乱。",
                "en_US": "Not enough tracks to shuffle.",
                "ja_JP": "シャッフルできる曲が足りません。"
            }
            return await interaction.response.send_message(msgs.get(loc, "Not enough tracks."), ephemeral=True)
        random.shuffle(self.player.queue)
        msgs = {
            "zh_TW": "已隨機打亂佇列順序。",
            "zh_CN": "已随机打乱队列顺序。",
            "en_US": "Queue shuffled.",
            "ja_JP": "キューの曲順をシャッフルしました。"
        }
        await interaction.response.send_message(msgs.get(loc, "Shuffled."), ephemeral=True)
        await self.player.update_panel_inplace()

    @discord.ui.button(label="待播清單", style=discord.ButtonStyle.secondary, row=1)
    async def queue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        paginator = QueuePaginator(self.player.queue, self.player.current, locale=self.player.locale)
        await interaction.response.send_message(embed=paginator.make_embed(), view=paginator, ephemeral=True)

    @discord.ui.button(label="停止", style=discord.ButtonStyle.danger, row=2)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        loc = self.player.locale
        self.player.queue.clear()
        self.player.history.clear()
        self.player.autoplay = False
        self.player.loop_mode = "off"
        self.player.stop_ticker()
        if self.player.voice_client:
            if self.player.voice_client.is_playing() or self.player.voice_client.is_paused():
                self.player.voice_client.stop()
            await self.player.voice_client.disconnect()
        msgs = {
            "zh_TW": "已清空隊列並離開語音頻道。",
            "zh_CN": "已清空队列并离开语音频道。",
            "en_US": "Stopped playback and disconnected.",
            "ja_JP": "再生を停止し、ボイスチャンネルから退出しました。"
        }
        await interaction.response.send_message(msgs.get(loc, "Stopped."), ephemeral=True)

    @discord.ui.button(label="音量 -10%", style=discord.ButtonStyle.secondary, row=2)
    async def voldown_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        loc = self.player.locale
        new_vol = max(0.1, round(self.player.volume - 0.1, 2))
        self.player.volume = new_vol
        if self.player.voice_client and self.player.voice_client.source:
            self.player.voice_client.source.volume = new_vol
        msgs = {
            "zh_TW": f"音量調整為：{int(new_vol * 100)}%",
            "zh_CN": f"音量调整为：{int(new_vol * 100)}%",
            "en_US": f"Volume set to: {int(new_vol * 100)}%",
            "ja_JP": f"音量を {int(new_vol * 100)}% に変更しました"
        }
        await interaction.response.send_message(msgs.get(loc, f"Volume: {int(new_vol * 100)}%"), ephemeral=True)
        await self.player.update_panel_inplace()

    @discord.ui.button(label="音量 +10%", style=discord.ButtonStyle.secondary, row=2)
    async def volup_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        loc = self.player.locale
        new_vol = min(2.0, round(self.player.volume + 0.1, 2))
        self.player.volume = new_vol
        if self.player.voice_client and self.player.voice_client.source:
            self.player.voice_client.source.volume = new_vol
        msgs = {
            "zh_TW": f"音量調整為：{int(new_vol * 100)}%",
            "zh_CN": f"音量调整为：{int(new_vol * 100)}%",
            "en_US": f"Volume set to: {int(new_vol * 100)}%",
            "ja_JP": f"音量を {int(new_vol * 100)}% に変更しました"
        }
        await interaction.response.send_message(msgs.get(loc, f"Volume: {int(new_vol * 100)}%"), ephemeral=True)
        await self.player.update_panel_inplace()

    @discord.ui.button(label="重設 100%", style=discord.ButtonStyle.secondary, row=2)
    async def volreset_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        loc = self.player.locale
        self.player.volume = 1.0
        if self.player.voice_client and self.player.voice_client.source:
            self.player.voice_client.source.volume = 1.0
        msgs = {
            "zh_TW": "音量已重設為 100%",
            "zh_CN": "音量已重置为 100%",
            "en_US": "Volume reset to 100%",
            "ja_JP": "音量を 100% にリセットしました"
        }
        await interaction.response.send_message(msgs.get(loc, "Volume reset."), ephemeral=True)
        await self.player.update_panel_inplace()

    @discord.ui.button(label="清空佇列", style=discord.ButtonStyle.secondary, row=2)
    async def clear_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        loc = self.player.locale
        count = len(self.player.queue)
        self.player.queue.clear()
        msgs = {
            "zh_TW": f"已清空佇列中的 {count} 首歌曲。",
            "zh_CN": f"已清空队列中的 {count} 首歌曲。",
            "en_US": f"Cleared {count} tracks from queue.",
            "ja_JP": f"キュー内の {count} 曲を消去しました。"
        }
        await interaction.response.send_message(msgs.get(loc, "Cleared queue."), ephemeral=True)
        await self.player.update_panel_inplace()

class QueuePaginator(discord.ui.View):
    def __init__(self, queue: List[Dict[str, Any]], current: Optional[Dict[str, Any]], page: int = 0, locale: str = "zh_TW"):
        super().__init__(timeout=60)
        self.queue = queue
        self.current = current
        self.page = page
        self.locale = locale
        self.per_page = 10
        self.max_page = max(1, (len(queue) + self.per_page - 1) // self.per_page)

        prev_labels = {"zh_TW": "上一頁", "zh_CN": "上一页", "en_US": "Previous", "ja_JP": "前へ"}
        next_labels = {"zh_TW": "下一頁", "zh_CN": "下一页", "en_US": "Next", "ja_JP": "次へ"}
        self.prev_btn.label = prev_labels.get(locale, "Previous")
        self.next_btn.label = next_labels.get(locale, "Next")
        self.update_buttons()

    def update_buttons(self):
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= self.max_page - 1

    def make_embed(self) -> discord.Embed:
        titles = {"zh_TW": "當前播放隊列", "zh_CN": "当前播放队列", "en_US": "Playback Queue", "ja_JP": "現在のキュー"}
        embed = discord.Embed(title=titles.get(self.locale, "Playback Queue"), color=0x9b59b6)
        if self.current:
            now_labels = {"zh_TW": "正在播放", "zh_CN": "正在播放", "en_US": "Now Playing", "ja_JP": "再生中"}
            embed.add_field(name=now_labels.get(self.locale, "Now Playing"), value=f"[{self.current['title']}]({self.current['webpage_url']})", inline=False)
        start = self.page * self.per_page
        end = start + self.per_page
        items = self.queue[start:end]
        if not items:
            empties = {"zh_TW": "隊列目前為空。", "zh_CN": "队列目前为空。", "en_US": "Queue is empty.", "ja_JP": "キューは空です。"}
            embed.description = empties.get(self.locale, "Queue is empty.")
        else:
            req_labels = {"zh_TW": "點播者", "zh_CN": "点播者", "en_US": "Requester", "ja_JP": "リクエスト"}
            r_str = req_labels.get(self.locale, "Requester")
            lines = [f"`{start + i + 1}.` {item['title']} ({r_str}: <@{item.get('requester_id', 'Unknown')}>)" for i, item in enumerate(items)]
            embed.description = "\n".join(lines)

        footers = {
            "zh_TW": f"第 {self.page + 1} / {self.max_page} 頁 • 總計 {len(self.queue)} 首",
            "zh_CN": f"第 {self.page + 1} / {self.max_page} 页 • 总计 {len(self.queue)} 首",
            "en_US": f"Page {self.page + 1} / {self.max_page} • Total {len(self.queue)} tracks",
            "ja_JP": f"{self.page + 1} / {self.max_page} ページ • 合計 {len(self.queue)} 曲"
        }
        embed.set_footer(text=footers.get(self.locale, f"Page {self.page + 1} / {self.max_page}"))
        return embed

    @discord.ui.button(label="上一頁", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page > 0:
            self.page -= 1
            self.update_buttons()
            await interaction.response.edit_message(embed=self.make_embed(), view=self)

    @discord.ui.button(label="下一頁", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page < self.max_page - 1:
            self.page += 1
            self.update_buttons()
            await interaction.response.edit_message(embed=self.make_embed(), view=self)

class GuildPlayer:
    def __init__(self, bot, guild_id: int, resolver):
        self.bot = bot
        self.guild_id = guild_id
        self.resolver = resolver
        self.queue: List[Dict[str, Any]] = []
        self.history: List[Dict[str, Any]] = []
        self.current: Optional[Dict[str, Any]] = None
        self.current_meta: Optional[Dict[str, Any]] = None
        self.last_played_meta: Optional[Dict[str, Any]] = None
        self.voice_client: Optional[discord.VoiceClient] = None
        self.current_text_channel: Optional[discord.TextChannel] = None
        self.panel_message: Optional[discord.Message] = None
        self.play_next_event = asyncio.Event()
        self.queue_event = asyncio.Event()
        self.autoplay = True
        self.loop_mode = "off"
        self.volume = 1.0
        self.current_eq = "flat"
        self.track_start_time = 0.0
        self.pause_time = 0.0
        self.is_restarting_stream = False
        self.auto_disconnect = False
        self.skip_votes: Set[int] = set()
        self.ticker_task: Optional[asyncio.Task] = None
        self.last_panel_update = 0.0
        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = asyncio.get_event_loop()
        self.audio_task = self.loop.create_task(self.audio_loop())

    def ensure_audio_task(self):
        self.queue_event.set()
        if self.audio_task is None or self.audio_task.done():
            self.audio_task = self.loop.create_task(self.audio_loop())

    @property
    def locale(self) -> str:
        guild = self.bot.get_guild(self.guild_id)
        return self.bot.get_guild_locale(guild) if guild else "zh_TW"

    def start_ticker(self):
        pass

    def stop_ticker(self):
        if self.ticker_task and not self.ticker_task.done():
            self.ticker_task.cancel()
        self.ticker_task = None

    async def _ticker_loop(self):
        while not self.bot.is_closed():
            try:
                await asyncio.sleep(25 + random.uniform(0, 10))
                if not self.voice_client or not self.current or not self.panel_message:
                    continue
                if self.voice_client.is_playing():
                    await self.update_panel_inplace()
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    async def seek(self, seconds: int):
        if not self.voice_client or not self.current or not self.current.get("stream_url"):
            return

        self.is_restarting_stream = True
        try:
            if self.voice_client.is_playing() or self.voice_client.is_paused():
                self.voice_client.stop()

            base_opts = "-loglevel error -nostats -vn -nostdin -sn -dn -threads 1 -b:a 64k"
            eq_filter = EQ_PRESETS.get(self.current_eq, "")
            if eq_filter:
                base_opts += f' -af "{eq_filter}"'

            clean_before = re.sub(r"-ss\s+[\d\.]+", "", self.current.get("before_options", DEFAULT_BEFORE_OPTS)).strip()
            before_opts = f"{clean_before} -ss {seconds}"

            raw_source = SafeFFmpegPCMAudio(
                self.current["stream_url"],
                before_options=before_opts,
                options=base_opts
            )
            audio_source = discord.PCMVolumeTransformer(raw_source, volume=self.volume)
            self.track_start_time = time.time() - seconds
            self.voice_client.play(audio_source, after=self._after_playback)
            await asyncio.sleep(0.3)
        finally:
            self.is_restarting_stream = False
        await self.update_panel_inplace(force=True)

    async def set_eq(self, eq_name: str):
        if eq_name not in EQ_PRESETS:
            return
        self.current_eq = eq_name
        elapsed = max(0, int(time.time() - self.track_start_time))
        await self.seek(elapsed)

    def _after_playback(self, error):
        if self.is_restarting_stream:
            return
        self.stop_ticker()
        self.loop.call_soon_threadsafe(self.play_next_event.set)

    async def play_previous(self, interaction: Optional[discord.Interaction] = None):
        loc = self.locale
        if not self.history:
            if interaction:
                msgs = {
                    "zh_TW": "沒有上一首歌曲的播放紀錄。",
                    "zh_CN": "没有上一首歌曲的播放记录。",
                    "en_US": "No previous track history.",
                    "ja_JP": "再生履歴がありません。"
                }
                await interaction.response.send_message(msgs.get(loc, "No history."), ephemeral=True)
            return

        if not self.voice_client:
            if interaction:
                msgs = {
                    "zh_TW": "目前沒有連線至語音頻道。",
                    "zh_CN": "当前未连接至语音频道。",
                    "en_US": "Not connected to a voice channel.",
                    "ja_JP": "ボイスチャンネルに接続されていません。"
                }
                await interaction.response.send_message(msgs.get(loc, "Not connected."), ephemeral=True)
            return

        prev_item = self.history.pop()
        if self.current_meta:
            self.queue.insert(0, self.current_meta)
        self.queue.insert(0, prev_item)

        self.voice_client.stop()
        if interaction:
            msgs = {
                "zh_TW": f"正在切換至上一首：{prev_item['title']}",
                "zh_CN": f"正在切换至上一首：{prev_item['title']}",
                "en_US": f"Playing previous track: {prev_item['title']}",
                "ja_JP": f"前の曲を再生します：{prev_item['title']}"
            }
            await interaction.response.send_message(msgs.get(loc, f"Playing previous: {prev_item['title']}"))

    async def process_skip(self, interaction: discord.Interaction):
        loc = self.locale
        if not self.voice_client or not (self.voice_client.is_playing() or self.voice_client.is_paused()):
            msgs = {
                "zh_TW": "目前沒有正在播放的音樂。",
                "zh_CN": "当前没有正在播放的音乐。",
                "en_US": "No music is currently playing.",
                "ja_JP": "現在再生中の曲はありません。"
            }
            return await interaction.response.send_message(msgs.get(loc, "No music playing."), ephemeral=True)

        user = interaction.user
        is_owner = await self.bot.is_owner(user)
        is_admin = user.guild_permissions.manage_guild or user.guild_permissions.administrator
        is_requester = self.current_meta and user.id == self.current_meta.get("requester_id")

        if is_owner or is_admin or is_requester:
            self.skip_votes.clear()
            self.voice_client.stop()
            roles = {
                "zh_TW": "管理員" if (is_owner or is_admin) else "點播者",
                "zh_CN": "管理员" if (is_owner or is_admin) else "点播者",
                "en_US": "Admin" if (is_owner or is_admin) else "Requester",
                "ja_JP": "管理者" if (is_owner or is_admin) else "リクエスト者"
            }
            r_str = roles.get(loc, "Admin")
            msgs = {
                "zh_TW": f"{r_str} <@{user.id}> 已強制跳過此曲目。",
                "zh_CN": f"{r_str} <@{user.id}> 已强制跳过此曲目。",
                "en_US": f"{r_str} <@{user.id}> force-skipped the track.",
                "ja_JP": f"{r_str} <@{user.id}> が曲をスキップしました。"
            }
            return await interaction.response.send_message(msgs.get(loc, "Skipped track."))

        listeners = [m for m in self.voice_client.channel.members if not m.bot]
        total_listeners = len(listeners)
        if total_listeners <= 1:
            self.skip_votes.clear()
            self.voice_client.stop()
            msgs = {
                "zh_TW": "已跳過此曲目。",
                "zh_CN": "已跳过此曲目。",
                "en_US": "Skipped track.",
                "ja_JP": "曲をスキップしました。"
            }
            return await interaction.response.send_message(msgs.get(loc, "Skipped."))

        required_votes = math.ceil(total_listeners / 2)
        if user.id in self.skip_votes:
            msgs = {
                "zh_TW": f"你已經投過跳過票了！(目前: {len(self.skip_votes)}/{required_votes})",
                "zh_CN": f"你已经投过跳过票了！(当前: {len(self.skip_votes)}/{required_votes})",
                "en_US": f"You already voted! ({len(self.skip_votes)}/{required_votes})",
                "ja_JP": f"既に投票済みです！(現在: {len(self.skip_votes)}/{required_votes})"
            }
            return await interaction.response.send_message(msgs.get(loc, "Already voted."), ephemeral=True)

        self.skip_votes.add(user.id)
        current_votes = len(self.skip_votes)
        if current_votes >= required_votes:
            self.skip_votes.clear()
            self.voice_client.stop()
            msgs = {
                "zh_TW": f"投票通過 ({current_votes}/{required_votes})，已跳過當前歌曲！",
                "zh_CN": f"投票通过 ({current_votes}/{required_votes})，已跳过当前歌曲！",
                "en_US": f"Vote passed! ({current_votes}/{required_votes}) Skipped track.",
                "ja_JP": f"投票が可決されました！({current_votes}/{required_votes}) 曲をスキップします。"
            }
            return await interaction.response.send_message(msgs.get(loc, "Vote passed."))
        else:
            msgs = {
                "zh_TW": f"跳過投票已記錄：目前 {current_votes}/{required_votes} 票。",
                "zh_CN": f"跳过投票已记录：当前 {current_votes}/{required_votes} 票。",
                "en_US": f"Skip vote registered: {current_votes}/{required_votes}.",
                "ja_JP": f"スキップ投票を受け付けました：現在 {current_votes}/{required_votes} 票。"
            }
            return await interaction.response.send_message(msgs.get(loc, "Vote recorded."))

    async def audio_loop(self):
        if hasattr(self.bot, "wait_until_ready"):
            try:
                await self.bot.wait_until_ready()
            except Exception:
                pass
        idle_counter = 0
        while not self.bot.is_closed():
            try:
                self.play_next_event.clear()
                self.skip_votes.clear()

                if self.auto_disconnect and self.voice_client and self.voice_client.is_connected():
                    listeners = [m for m in self.voice_client.channel.members if not m.bot]
                    if not listeners:
                        idle_counter += 1
                        if idle_counter >= 30:
                            await self.voice_client.disconnect()
                            if self.current_text_channel:
                                msgs = {
                                    "zh_TW": "語音頻道內無人收聽，已自動離開。",
                                    "zh_CN": "语音频道内无人收听，已自动离开。",
                                    "en_US": "Voice channel empty. Disconnected.",
                                    "ja_JP": "ボイスチャンネルに誰もいないため自動退出しました。"
                                }
                                await self.current_text_channel.send(msgs.get(self.locale, "Disconnected."))
                            break
                    else:
                        idle_counter = 0

                if self.loop_mode == "single" and self.current_meta:
                    next_item = self.current_meta
                elif not self.queue:
                    if self.loop_mode == "queue" and self.current_meta:
                        self.queue.append(self.current_meta)
                    if not self.queue and self.autoplay:
                        seed_info = self.current or self.current_meta or self.last_played_meta or (self.history[-1] if self.history else None)
                        if seed_info:
                            hist_ids = [str(h.get("id")) for h in self.history if h.get("id")]
                            rec = await self.resolver.get_autoplay_recommendation(seed_info, hist_ids)
                            if rec:
                                rec["requester_id"] = self.bot.user.id
                                ap_names = {
                                    "zh_TW": "自動推薦續播",
                                    "zh_CN": "自动推荐续播",
                                    "en_US": "Autoplay",
                                    "ja_JP": "自動連続再生"
                                }
                                rec["requester_name"] = ap_names.get(self.locale, "Autoplay")
                                self.queue.append(rec)

                if not self.queue and self.loop_mode != "single":
                    self.current = None
                    self.current_meta = None
                    self.queue_event.clear()
                    try:
                        await asyncio.wait_for(self.queue_event.wait(), timeout=2.0)
                    except (asyncio.TimeoutError, TimeoutError):
                        pass
                    continue

                if self.loop_mode != "single":
                    if self.current_meta:
                        self.history.append(self.current_meta)
                        if len(self.history) > 50:
                            self.history.pop(0)

                    next_item = self.queue.pop(0)
                    if self.loop_mode == "queue" and self.current_meta:
                        self.queue.append(self.current_meta)

                track_stream = await self.resolver.get_live_stream(next_item["search_query"])
                if not track_stream or not track_stream.get("stream_url"):
                    track_stream = await self.resolver.get_live_stream(next_item.get("title", next_item["search_query"]))
                    if not track_stream or not track_stream.get("stream_url"):
                        continue

                self.current = track_stream
                self.current_meta = next_item
                self.last_played_meta = next_item

                if next_item.get("title") and (track_stream.get("title") in ["Unknown", "StreetVoice Song", "StreetVoice Track"] or re.match(r"^\d+-\d+-\d+$", str(track_stream.get("title", "")))):
                    self.current["title"] = next_item["title"]

                if next_item.get("uploader") and next_item["uploader"] != "Unknown":
                    self.current["uploader"] = next_item["uploader"]

                if next_item.get("duration", 0) > 0 and self.current.get("duration", 0) == 0:
                    self.current["duration"] = next_item["duration"]

                if next_item.get("thumbnail") and not self.current.get("thumbnail"):
                    self.current["thumbnail"] = next_item["thumbnail"]

                if next_item.get("webpage_url") and not self.current.get("webpage_url", "").startswith("http"):
                    self.current["webpage_url"] = next_item["webpage_url"]

                base_opts = "-loglevel error -nostats -vn -nostdin -sn -dn -threads 1 -b:a 64k"
                eq_filter = EQ_PRESETS.get(self.current_eq, "")
                if eq_filter:
                    base_opts += f' -af "{eq_filter}"'

                raw_source = SafeFFmpegPCMAudio(
                    self.current["stream_url"],
                    before_options=self.current.get("before_options", DEFAULT_BEFORE_OPTS),
                    options=base_opts
                )
                audio_source = discord.PCMVolumeTransformer(raw_source, volume=self.volume)

                if self.voice_client and self.voice_client.is_connected():
                    self.track_start_time = time.time()
                    self.voice_client.play(audio_source, after=self._after_playback)
                    await self.post_new_panel()
                    self.start_ticker()
                    await self.play_next_event.wait()
                    elapsed = time.time() - self.track_start_time
                    if elapsed < 3.0 and not self.is_restarting_stream:
                        if hasattr(self.resolver, "invalidate_stream_cache"):
                            await self.resolver.invalidate_stream_cache(next_item["search_query"])
                else:
                    await asyncio.sleep(2)

            except Exception as e:
                print(f"Audio playback error: {e}")
                await asyncio.sleep(2)

    def _build_embed(self) -> discord.Embed:
        loc = self.locale
        i18n = self.bot.i18n
        target_url = self.current.get("webpage_url", "")
        if not target_url or not target_url.startswith("http"):
            target_url = "https://discord.com"

        embed = discord.Embed(
            title=i18n.get("PANEL_TITLE", loc),
            description=f"[{self.current.get('title', 'Unknown')}]({target_url})",
            color=0x3498db
        )

        thumb = self.current.get("thumbnail")
        if thumb and thumb.startswith("http"):
            embed.set_thumbnail(url=thumb)

        embed.add_field(name=i18n.get("PANEL_UPLOADER", loc), value=self.current.get("uploader", "Unknown"), inline=True)
        embed.add_field(name=i18n.get("PANEL_QUEUE", loc), value=f"{len(self.queue)}", inline=True)

        loop_labels = {"off": i18n.get("MODE_OFF", loc), "single": i18n.get("MODE_SINGLE", loc), "queue": i18n.get("MODE_QUEUE", loc)}
        embed.add_field(name=i18n.get("PANEL_LOOP", loc), value=loop_labels.get(self.loop_mode, "Off"), inline=True)
        embed.add_field(name=i18n.get("PANEL_AUTOPLAY", loc), value=i18n.get("STATUS_ON", loc) if self.autoplay else i18n.get("MODE_OFF", loc), inline=True)
        embed.add_field(name=i18n.get("PANEL_AUTODISCONNECT", loc), value=i18n.get("STATUS_ON", loc) if self.auto_disconnect else i18n.get("STATUS_247", loc), inline=True)
        embed.add_field(name=i18n.get("PANEL_VOLUME", loc), value=f"{int(self.volume * 100)}%", inline=True)
        eq_label = EQ_LABELS.get(loc, EQ_LABELS["zh_TW"]).get(self.current_eq, "Flat")
        embed.add_field(name=i18n.get("PANEL_EQ", loc), value=eq_label, inline=True)

        if self.current_meta and "requester_name" in self.current_meta:
            embed.set_footer(text=i18n.get("PANEL_REQUESTER", loc, user=self.current_meta.get('requester_name', 'Unknown')))
        return embed

    async def post_new_panel(self):
        if not self.current_text_channel or not self.current:
            return

        if self.panel_message:
            try:
                await self.panel_message.delete()
            except Exception:
                pass
            self.panel_message = None

        embed = self._build_embed()
        view = PlayerControls(self)
        try:
            self.panel_message = await self.current_text_channel.send(embed=embed, view=view)
        except Exception as e:
            print(f"Error sending control panel: {e}")

    async def update_panel_inplace(self, force: bool = False):
        if not self.panel_message or not self.current:
            return
        now = time.time()
        if not force and (now - self.last_panel_update < 4.0):
            return
        self.last_panel_update = now
        embed = self._build_embed()
        view = PlayerControls(self)
        try:
            await self.panel_message.edit(embed=embed, view=view)
        except Exception:
            pass