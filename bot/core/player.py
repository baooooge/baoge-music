import os
import sys
import logging
import asyncio
import random
import math
import time
import re
from typing import Optional, List, Dict, Any, Set
import discord
try:
    import wavelink
except ImportError:
    wavelink = None
try:
    from core.wavelink_node import LAVALINK_EQ_PRESETS
except ImportError:
    LAVALINK_EQ_PRESETS = {
        "flat": [],
        "bass": [{"band": 0, "gain": 0.22}, {"band": 1, "gain": 0.20}, {"band": 2, "gain": 0.16}, {"band": 3, "gain": 0.10}],
        "bass_extreme": [{"band": 0, "gain": 0.38}, {"band": 1, "gain": 0.35}, {"band": 2, "gain": 0.28}, {"band": 3, "gain": 0.18}, {"band": 4, "gain": 0.08}],
        "vocal": [{"band": 0, "gain": -0.12}, {"band": 1, "gain": -0.12}, {"band": 2, "gain": -0.12}, {"band": 7, "gain": 0.10}, {"band": 8, "gain": 0.18}, {"band": 9, "gain": 0.20}, {"band": 10, "gain": 0.16}, {"band": 11, "gain": 0.10}, {"band": 13, "gain": -0.08}, {"band": 14, "gain": -0.08}],
        "treble": [{"band": 10, "gain": 0.08}, {"band": 11, "gain": 0.14}, {"band": 12, "gain": 0.20}, {"band": 13, "gain": 0.24}, {"band": 14, "gain": 0.24}],
    }

if not discord.opus.is_loaded():
    candidates = [
        "libopus.so.0",
        "libopus.so",
        "opus",
        "opus.dll",
        "libopus-0.x64.dll",
        "libopus-0.dll",
        os.path.join(os.getcwd(), "opus.dll"),
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "opus.dll")
    ]
    if hasattr(discord, "__file__") and discord.__file__:
        candidates.append(os.path.join(os.path.dirname(discord.__file__), "bin", "libopus-0.x64.dll"))
    for opus_candidate in candidates:
        try:
            discord.opus.load_opus(opus_candidate)
            if discord.opus.is_loaded():
                break
        except Exception:
            pass

_DYING_TASKS: Set[asyncio.Task] = set()

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
    "-loglevel fatal -nostats -reconnect 1 -reconnect_streamed 1 "
    "-reconnect_on_network_error 1 -reconnect_on_http_error 4xx,5xx "
    "-reconnect_at_eof 1 -reconnect_delay_max 5 -probesize 512k -analyzeduration 100000 "
    "-fflags nobuffer+fastseek -flush_packets 1"
)

class SafeFFmpegPCMAudio(discord.FFmpegPCMAudio):
    def __init__(self, source, *args, **kwargs):
        if "executable" not in kwargs:
            if sys.platform == "win32":
                repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                candidates = [
                    os.path.join(os.getcwd(), "ffmpeg.exe"),
                    os.path.join(repo_root, "ffmpeg.exe"),
                    "ffmpeg.exe",
                    "ffmpeg"
                ]
                for c in candidates:
                    if os.path.isfile(c):
                        kwargs["executable"] = c
                        break
            else:
                kwargs["executable"] = "ffmpeg"
        super().__init__(source, *args, **kwargs)

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

        async with self.player.lock:
            if target_index < 1 or target_index > len(self.player.queue):
                msgs = {
                    "zh_TW": f"無效序號，目前隊列共有 1 到 {len(self.player.queue)} 首。",
                    "zh_CN": f"无效序号，当前队列共有 1 到 {len(self.player.queue)} 首。",
                    "en_US": f"Invalid number. Queue range is 1 to {len(self.player.queue)}.",
                    "ja_JP": f"無効な番号です。1 〜 {len(self.player.queue)} の範囲で入力してください。"
                }
                return await interaction.response.send_message(msgs.get(loc, "Invalid range."), ephemeral=True)

            self.player.queue = self.player.queue[target_index - 1:]
            self.player.stop_current()
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
        if selected != "flat" and hasattr(self.player.bot, "entitlement_manager"):
            is_patron = await self.player.bot.entitlement_manager.can_use_equalizer(interaction.user.id)
            if not is_patron:
                loc = self.player.locale
                patreon_url = os.getenv("PATREON_URL", "https://patreon.bybaoge.com/")
                embed = discord.Embed(
                    title=self.player.bot.i18n.get("PATREON_TITLE", loc),
                    description=self.player.bot.i18n.get("PATREON_EQ_DESC", loc),
                    color=0xf39c12
                )
                view = discord.ui.View()
                view.add_item(discord.ui.Button(label=self.player.bot.i18n.get("PATREON_BTN_LABEL", loc), url=patreon_url, style=discord.ButtonStyle.link))
                return await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

        await self.player.set_eq(selected)
        if selected != "flat":
            self.player.eq_enabled_by = interaction.user.id
        else:
            self.player.eq_enabled_by = None
        loc = self.player.locale
        label = EQ_LABELS.get(loc, EQ_LABELS["zh_TW"])[selected]
        msgs = {
            "zh_TW": f"等化器效果已切換為：{label}",
            "zh_CN": f"均衡器效果已切换为：{label}",
            "en_US": f"Equalizer preset changed to: {label}",
            "ja_JP": f"イコライザー設定を {label} に変更しました"
        }
        await interaction.response.send_message(msgs.get(loc, "EQ changed."), ephemeral=True)
        asyncio.create_task(self.player.update_panel_inplace())

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
        self.lyrics_btn.label = i18n.get("BTN_LYRICS", loc)

        self.add_item(EQSelect(player))

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        if isinstance(error, (discord.NotFound, discord.HTTPException)):
            if getattr(error, "code", None) == 10062 or "10062" in str(error):
                return
        await super().on_error(interaction, error, item)

    async def _safe_send(self, interaction: discord.Interaction, content: str = None, embed: discord.Embed = None, view: discord.ui.View = None, ephemeral: bool = True):
        try:
            kwargs = {"ephemeral": ephemeral}
            if content is not None:
                kwargs["content"] = content
            if embed is not None:
                kwargs["embed"] = embed
            if view is not None:
                kwargs["view"] = view
            if not interaction.response.is_done():
                await interaction.response.send_message(**kwargs)
            else:
                await interaction.followup.send(**kwargs)
        except (discord.NotFound, discord.HTTPException):
            pass

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        loc = self.player.locale
        if self.player.voice_client and self.player.voice_client.channel:
            listeners = [m for m in self.player.voice_client.channel.members if not m.bot]
            if listeners:
                if not interaction.user.voice or interaction.user.voice.channel != self.player.voice_client.channel:
                    msgs = {
                        "zh_TW": "你必須與機器人在同一個語音頻道中才能操作面板。",
                        "zh_CN": "你必须与机器人在同一个语音频道中才能操作面板。",
                        "en_US": "You must be in the same voice channel as the bot to control playback.",
                        "ja_JP": "再生を操作するにはボットと同じボイスチャンネルにいる必要があります。"
                    }
                    await interaction.response.send_message(msgs.get(loc, "You must be in the bot's voice channel."), ephemeral=True)
                    return False

        if hasattr(self.player.bot, "security_gateway"):
            ok, remaining = self.player.bot.security_gateway.enforce_cooldown(
                interaction.user.id, action_type="button", window=1.2
            )
            if not ok:
                i18n = self.player.bot.i18n
                msg = i18n.get("RATE_LIMITED", loc, time=f"{remaining:.1f}")
                await interaction.response.send_message(msg, ephemeral=True)
                return False
        return True

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
        if self.player.vc_is_paused():
            await self.player.resume_async()
            msgs = {
                "zh_TW": "已繼續播放。",
                "zh_CN": "已继续播放。",
                "en_US": "Resumed playback.",
                "ja_JP": "再生を再開しました。"
            }
            await interaction.response.send_message(msgs.get(loc, "Resumed."), ephemeral=True)
        elif self.player.vc_is_playing():
            await self.player.pause_async()
            msgs = {
                "zh_TW": "已暫停播放。",
                "zh_CN": "已暂停播放。",
                "en_US": "Paused playback.",
                "ja_JP": "再生を一時停止しました。"
            }
            await interaction.response.send_message(msgs.get(loc, "Paused."), ephemeral=True)
        asyncio.create_task(self.player.update_panel_inplace())

    @discord.ui.button(label="跳過", style=discord.ButtonStyle.secondary, row=0)
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.player.process_skip(interaction)

    @discord.ui.button(label="-15s", style=discord.ButtonStyle.secondary, row=0)
    async def rewind_15(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.player.voice_client or not self.player.current:
            return await interaction.response.send_message("Not playing.", ephemeral=True)
        current_pos = self.player.get_elapsed_seconds()
        new_pos = max(0, current_pos - 15)
        await interaction.response.send_message(f"{new_pos // 60:02d}:{new_pos % 60:02d}", ephemeral=True)
        asyncio.create_task(self.player.seek(new_pos))

    @discord.ui.button(label="+15s", style=discord.ButtonStyle.secondary, row=0)
    async def fast_forward_15(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.player.voice_client or not self.player.current:
            return await interaction.response.send_message("Not playing.", ephemeral=True)
        current_pos = self.player.get_elapsed_seconds()
        new_pos = current_pos + 15
        await interaction.response.send_message(f"{new_pos // 60:02d}:{new_pos % 60:02d}", ephemeral=True)
        asyncio.create_task(self.player.seek(new_pos))

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
        asyncio.create_task(self.player.update_panel_inplace())

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
        asyncio.create_task(self.player.update_panel_inplace())

    @discord.ui.button(label="隨機打亂", style=discord.ButtonStyle.secondary, row=1)
    async def shuffle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        loc = self.player.locale
        async with self.player.lock:
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
        asyncio.create_task(self.player.update_panel_inplace())

    @discord.ui.button(label="待播清單", style=discord.ButtonStyle.secondary, row=1)
    async def queue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        paginator = QueuePaginator(self.player.queue, self.player.current, locale=self.player.locale)
        await interaction.response.send_message(embed=paginator.make_embed(), view=paginator, ephemeral=True)

    @discord.ui.button(label="停止", style=discord.ButtonStyle.danger, row=2)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        loc = self.player.locale
        async with self.player.lock:
            self.player.queue.clear()
            self.player.history.clear()
            self.player.autoplay = False
            self.player.loop_mode = "off"
            self.player.stop_ticker()
            self.player.stop_current()
        if hasattr(self.player.bot, "players"):
            self.player.bot.players.pop(self.player.guild_id, None)
        msgs = {
            "zh_TW": "已清空隊列並離開語音頻道。",
            "zh_CN": "已清空队列并离开语音频道。",
            "en_US": "Stopped playback and disconnected.",
            "ja_JP": "再生を停止し、ボイスチャンネルから退出しました。"
        }
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(msgs.get(loc, "Stopped."), ephemeral=True)
            else:
                await interaction.followup.send(msgs.get(loc, "Stopped."), ephemeral=True)
        except (discord.NotFound, discord.HTTPException):
            pass
        await self.player.disconnect_voice()

    @discord.ui.button(label="音量 -10%", style=discord.ButtonStyle.secondary, row=2)
    async def voldown_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        loc = self.player.locale
        new_vol = max(0.1, round(self.player.volume - 0.1, 2))
        await self.player.set_volume(new_vol)
        msgs = {
            "zh_TW": f"音量調整為：{int(new_vol * 100)}%",
            "zh_CN": f"音量调整为：{int(new_vol * 100)}%",
            "en_US": f"Volume set to: {int(new_vol * 100)}%",
            "ja_JP": f"音量を {int(new_vol * 100)}% に変更しました"
        }
        await self._safe_send(interaction, msgs.get(loc, f"Volume: {int(new_vol * 100)}%"), ephemeral=True)
        asyncio.create_task(self.player.update_panel_inplace())

    @discord.ui.button(label="音量 +10%", style=discord.ButtonStyle.secondary, row=2)
    async def volup_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        loc = self.player.locale
        new_vol = min(2.0, round(self.player.volume + 0.1, 2))
        await self.player.set_volume(new_vol)
        msgs = {
            "zh_TW": f"音量調整為：{int(new_vol * 100)}%",
            "zh_CN": f"音量调整为：{int(new_vol * 100)}%",
            "en_US": f"Volume set to: {int(new_vol * 100)}%",
            "ja_JP": f"音量を {int(new_vol * 100)}% に変更しました"
        }
        await self._safe_send(interaction, msgs.get(loc, f"Volume: {int(new_vol * 100)}%"), ephemeral=True)
        asyncio.create_task(self.player.update_panel_inplace())

    @discord.ui.button(label="重設 100%", style=discord.ButtonStyle.secondary, row=2)
    async def volreset_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        loc = self.player.locale
        await self.player.set_volume(1.0)
        msgs = {
            "zh_TW": "音量已重設為 100%",
            "zh_CN": "音量已重置为 100%",
            "en_US": "Volume reset to 100%",
            "ja_JP": "音量を 100% にリセットしました"
        }
        await self._safe_send(interaction, msgs.get(loc, "Volume reset."), ephemeral=True)
        asyncio.create_task(self.player.update_panel_inplace())

    @discord.ui.button(label="清空佇列", style=discord.ButtonStyle.secondary, row=2)
    async def clear_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        loc = self.player.locale
        async with self.player.lock:
            count = len(self.player.queue)
            self.player.queue.clear()
        msgs = {
            "zh_TW": f"已清空佇列中的 {count} 首歌曲。",
            "zh_CN": f"已清空队列中的 {count} 首歌曲。",
            "en_US": f"Cleared {count} tracks from queue.",
            "ja_JP": f"キュー内の {count} 曲を消去しました。"
        }
        await self._safe_send(interaction, msgs.get(loc, "Cleared queue."), ephemeral=True)
        asyncio.create_task(self.player.update_panel_inplace())

    @discord.ui.button(label="歌詞", style=discord.ButtonStyle.secondary, row=4)
    async def lyrics_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        loc = self.player.locale
        i18n = self.player.bot.i18n
        if not self.player.current and not self.player.current_meta:
            return await self._safe_send(interaction, i18n.get("LYRICS_NO_TRACK", loc), ephemeral=True)
        try:
            await interaction.response.defer(ephemeral=True)
        except (discord.NotFound, discord.HTTPException):
            return
        meta = self.player.current_meta or {}
        curr = self.player.current or {}
        track_title = meta.get("track_name") or curr.get("title") or meta.get("title") or ""
        track_artist = meta.get("artist_name") or curr.get("uploader") or meta.get("uploader") or ""
        if track_artist in ("Unknown", "YouTube", "StreetVoice", "Bilibili", "SoundCloud"):
            track_artist = ""
        lyrics = await self.player.bot.resolver.get_lyrics(track_title, track_artist)
        if not lyrics:
            return await self._safe_send(interaction, i18n.get("LYRICS_NOT_FOUND", loc, title=track_title), ephemeral=True)
        display_lyrics = lyrics if len(lyrics) <= 3950 else lyrics[:3947] + "..."
        embed = discord.Embed(
            title=i18n.get("LYRICS_TITLE", loc, title=track_title)[:256],
            description=display_lyrics,
            color=0x3498db,
        )
        embed.set_footer(text=i18n.get("LYRICS_FOOTER", loc))
        await self._safe_send(interaction, embed=embed, ephemeral=True)

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

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        client = interaction.client
        if hasattr(client, "security_gateway"):
            ok, remaining = client.security_gateway.enforce_cooldown(
                interaction.user.id, action_type="button", window=0.8
            )
            if not ok:
                msg = client.i18n.get("RATE_LIMITED", self.locale, time=f"{remaining:.1f}") if hasattr(client, "i18n") else f"Please wait {remaining:.1f}s."
                await interaction.response.send_message(msg, ephemeral=True)
                return False
        return True

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
            lines = []
            for i, item in enumerate(items):
                raw_title = str(item.get('title', 'Unknown'))
                title_clean = raw_title[:70] + ("..." if len(raw_title) > 70 else "")
                lines.append(f"`{start + i + 1}.` {title_clean} ({r_str}: <@{item.get('requester_id', 'Unknown')}>)")
            embed.description = "\n".join(lines)[:4000]

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
        self.lock = asyncio.Lock()
        self.queue: List[Dict[str, Any]] = []
        self.history: List[Dict[str, Any]] = []
        self.current: Optional[Dict[str, Any]] = None
        self.current_meta: Optional[Dict[str, Any]] = None
        self.last_played_meta: Optional[Dict[str, Any]] = None
        self.voice_client: Optional[discord.VoiceClient] = None
        self.current_text_channel: Optional[discord.TextChannel] = None
        self.panel_message: Optional[discord.Message] = None
        self.panel_view: Optional[discord.ui.View] = None
        self.connect_lock = asyncio.Lock()
        self.stream_token: int = 0
        self.play_next_event = asyncio.Event()
        self.queue_event = asyncio.Event()
        self.autoplay = True
        self.loop_mode = "off"
        self.volume = 1.0
        self.current_eq = "flat"
        self.track_start_time = 0.0
        self.pause_time = 0.0
        self.is_restarting_stream = False
        self.is_manual_interruption = False
        self.auto_disconnect = True
        self.mode_247_enabled_by: Optional[int] = None
        self.eq_enabled_by: Optional[int] = None
        self.skip_votes: Set[int] = set()
        self.last_panel_update = 0.0
        self._pre_resolved_cache: Dict[str, tuple[float, Any, Dict[str, Any]]] = {}
        self._consecutive_lavalink_failures: int = 0
        self._last_track_failed: bool = False
        self._track_ended_with_error: bool = False
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
        pass

    def destroy(self):
        self.queue.clear()
        self.history.clear()
        self._pre_resolved_cache.clear()
        self.stop_current()
        task = self.audio_task
        self.audio_task = None
        try:
            curr = asyncio.current_task()
        except RuntimeError:
            curr = None
        if task and not task.done() and task is not curr:
            _DYING_TASKS.add(task)
            task.add_done_callback(_DYING_TASKS.discard)
            task.cancel()

    def is_lavalink_vc(self, vc=None) -> bool:
        target = vc if vc is not None else self.voice_client
        return wavelink is not None and isinstance(target, wavelink.Player)

    def vc_is_connected(self, vc=None) -> bool:
        target = vc if vc is not None else self.voice_client
        if not target:
            return False
        if self.is_lavalink_vc(target):
            return bool(getattr(target, "connected", False))
        return bool(target.is_connected())

    def vc_is_playing(self, vc=None) -> bool:
        target = vc if vc is not None else self.voice_client
        if not target:
            return False
        if self.is_lavalink_vc(target):
            return bool(getattr(target, "playing", False) and not getattr(target, "paused", False))
        return bool(target.is_playing())

    def vc_is_paused(self, vc=None) -> bool:
        target = vc if vc is not None else self.voice_client
        if not target:
            return False
        if self.is_lavalink_vc(target):
            return bool(getattr(target, "paused", False))
        return bool(target.is_paused())

    def get_elapsed_seconds(self) -> int:
        if self.is_lavalink_vc():
            return max(0, int(getattr(self.voice_client, "position", 0) // 1000))
        if self.pause_time > 0.0:
            return max(0, int(self.pause_time - self.track_start_time))
        return max(0, int(time.time() - self.track_start_time))

    async def set_volume(self, new_vol: float):
        new_vol = max(0.01, min(2.0, float(new_vol)))
        self.volume = new_vol
        vc = self.voice_client
        if not vc:
            return
        if self.is_lavalink_vc(vc):
            try:
                await vc.set_volume(int(round(new_vol * 100)))
            except Exception:
                pass
        elif getattr(vc, "source", None):
            vc.source.volume = new_vol

    async def apply_lavalink_eq(self, eq_name: str):
        vc = self.voice_client
        if not self.is_lavalink_vc(vc) or not self.vc_is_connected(vc):
            return
        filters = vc.filters
        bands = LAVALINK_EQ_PRESETS.get(eq_name, [])
        if bands:
            filters.equalizer.set(bands=bands)
        else:
            filters.equalizer.reset()
        await vc.set_filters(filters)

    async def pause_async(self) -> bool:
        if self.is_lavalink_vc() and self.vc_is_playing():
            await self.voice_client.pause(True)
            self.pause_time = time.time()
            return True
        return self.pause()

    async def resume_async(self) -> bool:
        if self.is_lavalink_vc() and self.vc_is_paused():
            await self.voice_client.pause(False)
            if self.pause_time > 0.0:
                self.track_start_time += time.time() - self.pause_time
                self.pause_time = 0.0
            return True
        return self.resume()

    async def disconnect_voice(self):
        vc = self.voice_client
        self.voice_client = None
        if not vc:
            return
        try:
            if self.is_lavalink_vc(vc):
                try:
                    await vc.stop()
                except Exception:
                    pass
                await vc.disconnect()
            else:
                try:
                    vc.stop()
                except Exception:
                    pass
                await vc.disconnect(force=True)
                if hasattr(vc, "cleanup"):
                    vc.cleanup()
        except Exception:
            pass

    def handle_lavalink_track_end(self, payload):
        reason = str(getattr(payload, "reason", "")).lower()
        if reason == "replaced" or self.is_restarting_stream:
            return
        if reason in ("load_failed", "loadfailed") or getattr(self, "_last_track_failed", False):
            self._consecutive_lavalink_failures = getattr(self, "_consecutive_lavalink_failures", 0) + 1
            self._last_track_failed = False
            self._track_ended_with_error = True
        elif reason in ("finished", "stopped"):
            self._consecutive_lavalink_failures = 0
            self._track_ended_with_error = False
        self.stop_ticker()
        self.play_next_event.set()

    def handle_lavalink_track_exception(self, payload):
        exc = getattr(payload, "exception", None)
        self._last_track_failed = True
        logging.getLogger("discord.player").error("Lavalink track exception in guild %s: %s", self.guild_id, exc)

    def handle_lavalink_track_stuck(self, payload):
        threshold = getattr(payload, "threshold", 0)
        logging.getLogger("discord.player").warning("Lavalink track stuck (%sms) in guild %s", threshold, self.guild_id)
        self.stop_ticker()
        self.play_next_event.set()

    def pause(self) -> bool:
        if self.vc_is_playing():
            if self.is_lavalink_vc():
                asyncio.create_task(self.voice_client.pause(True))
            else:
                self.voice_client.pause()
            self.pause_time = time.time()
            return True
        return False

    def resume(self) -> bool:
        if self.vc_is_paused():
            if self.is_lavalink_vc():
                asyncio.create_task(self.voice_client.pause(False))
            else:
                self.voice_client.resume()
            if self.pause_time > 0.0:
                self.track_start_time += time.time() - self.pause_time
                self.pause_time = 0.0
            return True
        return False

    def stop_current(self):
        self.is_manual_interruption = True
        if self.panel_view and not self.panel_view.is_finished():
            try:
                self.panel_view.stop()
            except Exception:
                pass
        if self.vc_is_playing() or self.vc_is_paused():
            if self.is_lavalink_vc():
                asyncio.create_task(self.voice_client.stop())
            else:
                self.voice_client.stop()

    async def seek(self, seconds: int):
        if not self.voice_client or not self.current:
            return
        seconds = max(0, int(seconds))
        if self.is_lavalink_vc() and self.vc_is_connected():
            try:
                await self.voice_client.seek(seconds * 1000)
                self.track_start_time = time.time() - seconds
                await self.update_panel_inplace(force=True)
            except Exception as ex:
                logging.getLogger("discord.player").error("Lavalink seek failed: %s", ex)
            return
        if not self.current.get("stream_url"):
            return

        self.stream_token += 1
        curr_token = self.stream_token
        self.is_restarting_stream = True
        try:
            if self.voice_client.is_playing() or self.voice_client.is_paused():
                self.voice_client.stop()

            base_opts = "-loglevel fatal -nostats -vn -nostdin -sn -dn -threads 1"
            eq_filter = EQ_PRESETS.get(self.current_eq, "")
            if eq_filter:
                base_opts += f' -af "{eq_filter}"'

            play_target = self.current["stream_url"]
            if not play_target or not isinstance(play_target, str) or not play_target.strip():
                return

            before_opts = None
            if not self.current.get("is_live", False) and hasattr(self.resolver, "get_cached_track_path"):
                track_uid = self.current.get("id") or (self.current_meta.get("id") if self.current_meta else None) or str(abs(hash(play_target)))
                cached_file = self.resolver.get_cached_track_path(track_uid)
                if cached_file:
                    play_target = cached_file
                    before_opts = f"-loglevel fatal -nostats -ss {seconds}"

            if before_opts is None:
                raw_before = self.current.get("before_options", DEFAULT_BEFORE_OPTS)
                clean_before = re.sub(r"-ss\s+[\d\.]+", "", raw_before).strip()
                clean_before = re.sub(r"-rw_timeout\s+\d+|-timeout\s+\d+", "", clean_before)
                if "-reconnect_streamed 1" not in clean_before:
                    clean_before = f"{clean_before} -reconnect_streamed 1"
                if "-reconnect_at_eof 1" not in clean_before:
                    clean_before = f"{clean_before} -reconnect_at_eof 1"
                before_opts = f"{clean_before.strip()} -ss {seconds}"

            try:
                raw_source = SafeFFmpegPCMAudio(
                    play_target,
                    before_options=before_opts,
                    options=base_opts
                )
                audio_source = discord.PCMVolumeTransformer(raw_source, volume=self.volume)
                self.track_start_time = time.time() - seconds
                self.voice_client.play(audio_source, after=self._make_after_callback(curr_token))
                await asyncio.sleep(0.3)
            except Exception:
                self.play_next_event.set()
        finally:
            self.is_restarting_stream = False
        await self.update_panel_inplace(force=True)

    async def set_eq(self, eq_name: str):
        if eq_name not in EQ_PRESETS:
            return
        self.current_eq = eq_name
        if self.is_lavalink_vc() and self.vc_is_connected():
            await self.apply_lavalink_eq(eq_name)
            await self.update_panel_inplace(force=True)
            return
        elapsed = self.get_elapsed_seconds()
        await self.seek(elapsed)

    def _make_after_callback(self, token: int):
        def _cb(error):
            if error:
                logging.getLogger("discord.player").error("Audio playback callback error: %s", error, exc_info=error)
            if token != self.stream_token:
                return
            self.stop_ticker()
            self.loop.call_soon_threadsafe(self.play_next_event.set)
        return _cb

    def _after_playback(self, error):
        if error:
            logging.getLogger("discord.player").error("Audio playback callback error: %s", error, exc_info=error)
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

        async with self.lock:
            if not self.history:
                return
            prev_item = self.history.pop()
            if self.current_meta:
                self.queue.insert(0, self.current_meta)
            self.queue.insert(0, prev_item)
            self.stop_current()

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
        if not self.voice_client or not (self.vc_is_playing() or self.vc_is_paused()):
            msgs = {
                "zh_TW": "目前沒有正在播放的音樂。",
                "zh_CN": "当前没有正在播放的音乐。",
                "en_US": "No music is currently playing.",
                "ja_JP": "現在再生中の曲はありません。"
            }
            return await interaction.response.send_message(msgs.get(loc, "No music playing."), ephemeral=True)

        user = interaction.user
        is_owner = False
        try:
            owner_id = getattr(self.bot, "owner_id", None)
            owner_ids = getattr(self.bot, "owner_ids", None)
            if owner_id is not None:
                is_owner = (user.id == owner_id)
            elif owner_ids:
                is_owner = (user.id in owner_ids)
            else:
                is_owner = await self.bot.is_owner(user)
        except Exception:
            is_owner = False

        user_perms = getattr(user, "guild_permissions", None)
        is_admin = bool(user_perms and (user_perms.manage_guild or user_perms.administrator))
        is_requester = bool(self.current_meta and user.id == self.current_meta.get("requester_id"))

        if is_owner or is_admin or is_requester:
            self.skip_votes.clear()
            if self.loop_mode == "single":
                self.loop_mode = "off"
            self.stop_current()
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
        if listeners and (not user.voice or user.voice.channel != self.voice_client.channel):
            msgs = {
                "zh_TW": "你必須在同一個語音頻道中才能執行跳過或參與投票。",
                "zh_CN": "你必须在同一个语音頻道中才能执行跳过或参与投票。",
                "en_US": "You must be in the voice channel to skip or vote.",
                "ja_JP": "スキップまたは投票を行うには同じボイスチャンネルにいる必要があります。"
            }
            return await interaction.response.send_message(msgs.get(loc, "You must be in the voice channel to skip."), ephemeral=True)

        total_listeners = len(listeners)
        if total_listeners <= 1:
            self.skip_votes.clear()
            if self.loop_mode == "single":
                self.loop_mode = "off"
            self.stop_current()
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
            if self.loop_mode == "single":
                self.loop_mode = "off"
            self.stop_current()
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

    def _rank_lavalink_candidates(
        self,
        candidates: List[Any],
        query: str,
        expected_duration: int,
        track_name: str,
        artist_name: str,
    ):
        if not candidates:
            return None, -999999
        scored = []
        for tr in candidates[:8]:
            sc = self.resolver._score_music_candidate(
                tr.title or "",
                tr.author or "",
                query=query,
                duration=int((tr.length or 0) // 1000),
                expected_duration=expected_duration,
                track_name=track_name,
                artist_name=artist_name,
            )
            scored.append((sc, tr))
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[0][1], scored[0][0]

    @staticmethod
    def _make_lavalink_cache_key(item: Dict[str, Any]) -> str:
        sq = (item.get("search_query") or "").strip()
        track_name = (item.get("track_name") or "").strip()
        artist_name = (item.get("artist_name") or "").strip()
        isrc = (item.get("isrc") or "").strip()
        return f"{sq}|{track_name}|{artist_name}|{isrc}"

    def _store_pre_resolved(self, cache_key: str, playable: Any, track_info: Dict[str, Any]):
        while len(self._pre_resolved_cache) >= 20:
            self._pre_resolved_cache.pop(next(iter(self._pre_resolved_cache)), None)
        self._pre_resolved_cache[cache_key] = (time.monotonic(), playable, dict(track_info))

    def _build_lavalink_entry(self, playable: Any, fallback_query: str = "") -> tuple[Dict[str, Any], Dict[str, Any]]:
        vid_id = getattr(playable, "identifier", None)
        title_val = getattr(playable, "title", None) or fallback_query or "Unknown"
        uploader_val = getattr(playable, "author", None) or "Unknown"
        dur_sec = int((getattr(playable, "length", 0) or 0) // 1000)
        thumb_val = getattr(playable, "artwork", None) or ""
        web_val = getattr(playable, "uri", None) or fallback_query
        if not thumb_val and vid_id and re.match(r"^[0-9A-Za-z_-]{11}$", str(vid_id)):
            thumb_val = f"https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg"
        is_live = bool(getattr(playable, "is_stream", False))
        cached_offset = 0.0
        if vid_id and re.match(r"^[0-9A-Za-z_-]{11}$", str(vid_id)):
            cached_offset = float(self.resolver._sponsorblock_cache.get(str(vid_id), 0.0))

        queue_item = {
            "id": vid_id if (vid_id and re.match(r"^[0-9A-Za-z_-]{11}$", str(vid_id))) else None,
            "title": title_val,
            "search_query": web_val,
            "duration": dur_sec,
            "uploader": uploader_val,
            "thumbnail": thumb_val,
            "webpage_url": web_val,
        }
        track_info = {
            "id": vid_id,
            "title": title_val,
            "uploader": uploader_val,
            "duration": dur_sec,
            "thumbnail": thumb_val,
            "webpage_url": web_val,
            "stream_url": web_val,
            "is_live": is_live,
            "start_offset": cached_offset,
        }
        return queue_item, track_info

    @staticmethod
    def _extract_lavalink_candidates(found: Any) -> List[Any]:
        if isinstance(found, Exception) or not found:
            return []
        if wavelink is not None and isinstance(found, wavelink.Playlist):
            return list(found.tracks or [])
        if hasattr(found, "tracks"):
            return list(getattr(found, "tracks", None) or [])
        return list(found)

    def _is_lavalink_yt_playback_blocked(self) -> bool:
        if os.getenv("LAVALINK_YT_DIRECT_HTTP", "1").lower() in ("1", "true", "yes"):
            return True
        return time.monotonic() < float(getattr(self.bot, "_lavalink_yt_direct_blocked_until", 0.0))

    async def _load_youtube_via_lavalink_http(
        self,
        yt_url: str,
        fallback_item: Optional[Dict[str, Any]] = None,
    ) -> Optional[tuple[Any, Dict[str, Any]]]:
        if wavelink is None or not yt_url or not hasattr(self.resolver, "get_youtube_direct_stream"):
            return None
        stream_meta = await self.resolver.get_youtube_direct_stream(yt_url)
        if not stream_meta or not stream_meta.get("stream_url"):
            return None
        try:
            found = await wavelink.Playable.search(stream_meta["stream_url"])
        except Exception:
            return None
        cands = self._extract_lavalink_candidates(found)
        if not cands:
            return None

        playable = cands[0]
        fb = fallback_item or {}
        vid_id = stream_meta.get("id") or fb.get("id")
        title_val = fb.get("title") or stream_meta.get("title") or "Unknown"
        uploader_val = fb.get("uploader") or stream_meta.get("uploader") or "Unknown"
        dur_sec = int((getattr(playable, "length", 0) or 0) // 1000) or int(stream_meta.get("duration") or fb.get("duration") or 0)
        thumb_val = fb.get("thumbnail") or stream_meta.get("thumbnail") or (
            f"https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg" if vid_id else ""
        )
        web_val = fb.get("webpage_url") or stream_meta.get("webpage_url") or yt_url
        start_off = float(fb.get("start_offset") or 0.0)
        if start_off <= 0.0 and vid_id and re.match(r"^[0-9A-Za-z_-]{11}$", str(vid_id)):
            start_off = float(self.resolver._sponsorblock_cache.get(str(vid_id), 0.0))

        track_info = {
            "id": vid_id,
            "title": title_val,
            "uploader": uploader_val,
            "duration": dur_sec,
            "thumbnail": thumb_val,
            "webpage_url": web_val,
            "stream_url": stream_meta["stream_url"],
            "is_live": bool(stream_meta.get("is_live", False)),
            "start_offset": start_off,
        }
        return playable, track_info

    async def fast_resolve_lavalink_query(self, query: str) -> Optional[List[Dict[str, Any]]]:
        if wavelink is None:
            return None
        wl_mgr = getattr(self.bot, "wavelink_manager", None)
        if not wl_mgr or not wl_mgr.is_connected():
            return None

        q = (query or "").strip()
        if not q:
            return None

        yt_blocked = self._is_lavalink_yt_playback_blocked()
        preload_first_http = yt_blocked and (self.current is None and not self.queue)
        is_url = q.startswith("http://") or q.startswith("https://")
        if is_url:
            if not any(d in q.lower() for d in ("youtube.com", "youtu.be", "soundcloud.com")):
                return None
            try:
                found = await wavelink.Playable.search(q)
            except Exception:
                return None

            if isinstance(found, wavelink.Playlist):
                tracks_list = list(found.tracks or [])
                if not tracks_list:
                    return None
                selected = getattr(found, "selected", None)
                if isinstance(selected, int) and 0 < selected < len(tracks_list):
                    tracks_list = tracks_list[selected:] + tracks_list[:selected]
                limit = 25 if re.search(r"[?&]list=(?:RD|UL)[0-9A-Za-z_-]+", q) else 100
                results = []
                for idx, tr in enumerate(tracks_list[:limit]):
                    q_item, t_info = self._build_lavalink_entry(tr, fallback_query=q)
                    results.append(q_item)
                    is_yt_tr = getattr(tr, "source", "") == "youtube"
                    if idx == 0 and is_yt_tr and preload_first_http:
                        direct_res = await self._load_youtube_via_lavalink_http(q_item["search_query"], q_item)
                        if direct_res:
                            self._store_pre_resolved(self._make_lavalink_cache_key(q_item), direct_res[0], direct_res[1])
                    elif idx < 2 and not (yt_blocked and is_yt_tr):
                        self._store_pre_resolved(self._make_lavalink_cache_key(q_item), tr, t_info)
                return results or None

            cands = self._extract_lavalink_candidates(found)
            if not cands:
                return None
            tr = cands[0]
            q_item, t_info = self._build_lavalink_entry(tr, fallback_query=q)
            is_yt_tr = getattr(tr, "source", "") == "youtube"
            if is_yt_tr and preload_first_http:
                direct_res = await self._load_youtube_via_lavalink_http(q_item["search_query"], q_item)
                if direct_res:
                    self._store_pre_resolved(self._make_lavalink_cache_key(q_item), direct_res[0], direct_res[1])
            elif not (yt_blocked and is_yt_tr):
                self._store_pre_resolved(self._make_lavalink_cache_key(q_item), tr, t_info)
            return [q_item]

        clean_q = self.resolver.clean_for_search(q) or q
        ytm_res, yt_res = await asyncio.gather(
            wavelink.Playable.search(clean_q, source=wavelink.TrackSource.YouTubeMusic),
            wavelink.Playable.search(clean_q, source=wavelink.TrackSource.YouTube),
            return_exceptions=True,
        )

        best_cand = None
        best_score = -999999
        for res_pool in (ytm_res, yt_res):
            cands = self._extract_lavalink_candidates(res_pool)
            cand, sc = self._rank_lavalink_candidates(cands, q, 0, "", "")
            if cand is not None and sc > best_score:
                best_cand, best_score = cand, sc

        if best_cand is None or best_score < 0:
            try:
                retry_found = await wavelink.Playable.search(
                    f"{clean_q} official audio",
                    source=wavelink.TrackSource.YouTubeMusic,
                )
                retry_cands = self._extract_lavalink_candidates(retry_found)
                cand, sc = self._rank_lavalink_candidates(retry_cands, q, 0, "", "")
                if cand is not None and sc > best_score:
                    best_cand, best_score = cand, sc
            except Exception:
                pass

        if best_cand is None:
            return None
        q_item, t_info = self._build_lavalink_entry(best_cand, fallback_query=q)
        is_yt_cand = getattr(best_cand, "source", "") == "youtube"
        if is_yt_cand and preload_first_http:
            direct_res = await self._load_youtube_via_lavalink_http(q_item["search_query"], q_item)
            if direct_res:
                self._store_pre_resolved(self._make_lavalink_cache_key(q_item), direct_res[0], direct_res[1])
        elif not (yt_blocked and is_yt_cand):
            self._store_pre_resolved(self._make_lavalink_cache_key(q_item), best_cand, t_info)
        return [q_item]

    async def _pre_resolve_lavalink_next(self, next_track: Dict[str, Any]):
        try:
            if not next_track or not self.is_lavalink_vc():
                return
            await self._resolve_lavalink_playable(next_track, store_cache=True)
        except Exception:
            pass

    async def _resolve_lavalink_playable(self, next_item: Dict[str, Any], store_cache: bool = False):
        if wavelink is None:
            return None
        sq = (next_item.get("search_query") or "").strip()
        track_name = (next_item.get("track_name") or "").strip()
        artist_name = (next_item.get("artist_name") or "").strip()
        isrc = (next_item.get("isrc") or "").strip()
        is_url = sq.startswith("http://") or sq.startswith("https://")
        exp_dur = int(next_item.get("expected_duration") or 0)

        cache_key = self._make_lavalink_cache_key(next_item)
        now = time.monotonic()
        cached = self._pre_resolved_cache.get(cache_key)
        if cached and (now - cached[0]) < 1200.0:
            if not store_cache:
                self._pre_resolved_cache.pop(cache_key, None)
            return cached[1], dict(cached[2])

        playable = None
        stream_meta = None

        if next_item.get("_lavalink_http_fallback"):
            yt_fb_url = sq if (is_url and ("youtube.com" in sq or "youtu.be" in sq)) else (next_item.get("webpage_url") or "")
            if yt_fb_url and ("youtube.com" in yt_fb_url or "youtu.be" in yt_fb_url):
                direct_res = await self._load_youtube_via_lavalink_http(yt_fb_url, next_item)
                if direct_res:
                    if store_cache:
                        self._store_pre_resolved(cache_key, direct_res[0], direct_res[1])
                    return direct_res

        if is_url and any(d in sq for d in ("youtube.com", "youtu.be", "soundcloud.com")):
            try:
                found = await wavelink.Playable.search(sq)
                cands = self._extract_lavalink_candidates(found)
                playable = cands[0] if cands else None
            except Exception:
                playable = None

        if not playable and not is_url and isrc:
            raw_q = f"{track_name} {artist_name}".strip() if track_name else sq
            clean_q = self.resolver.clean_for_search(raw_q) or raw_q
            isrc_res, ytm_res = await asyncio.gather(
                wavelink.Playable.search(isrc, source=wavelink.TrackSource.YouTubeMusic),
                wavelink.Playable.search(clean_q, source=wavelink.TrackSource.YouTubeMusic) if clean_q else asyncio.sleep(0, result=[]),
                return_exceptions=True,
            )
            isrc_cands = self._extract_lavalink_candidates(isrc_res)
            if isrc_cands:
                best_isrc, best_isrc_score = self._rank_lavalink_candidates(
                    isrc_cands, sq, exp_dur, track_name, artist_name
                )
                if best_isrc is not None and best_isrc_score >= 0:
                    playable = best_isrc

            if not playable:
                best_cand = None
                best_score = -999999
                ytm_cands = self._extract_lavalink_candidates(ytm_res)
                if ytm_cands:
                    cand, sc = self._rank_lavalink_candidates(ytm_cands, sq, exp_dur, track_name, artist_name)
                    if cand is not None and sc > best_score:
                        best_cand, best_score = cand, sc

                yt_threshold = 1800 if track_name else 800
                if (best_cand is None or best_score < yt_threshold) and clean_q:
                    try:
                        yt_found = await wavelink.Playable.search(clean_q, source=wavelink.TrackSource.YouTube)
                        yt_cands = self._extract_lavalink_candidates(yt_found)
                        cand, sc = self._rank_lavalink_candidates(yt_cands, sq, exp_dur, track_name, artist_name)
                        if cand is not None and sc > best_score:
                            best_cand, best_score = cand, sc
                    except Exception:
                        pass

                if (best_cand is None or best_score < 0) and clean_q:
                    try:
                        retry_found = await wavelink.Playable.search(
                            f"{clean_q} official audio",
                            source=wavelink.TrackSource.YouTubeMusic,
                        )
                        retry_cands = self._extract_lavalink_candidates(retry_found)
                        cand, sc = self._rank_lavalink_candidates(retry_cands, sq, exp_dur, track_name, artist_name)
                        if cand is not None and sc > best_score:
                            best_cand, best_score = cand, sc
                    except Exception:
                        pass

                playable = best_cand

        if not playable and not is_url and (sq or track_name):
            raw_q = f"{track_name} {artist_name}".strip() if track_name else sq
            clean_q = self.resolver.clean_for_search(raw_q) or raw_q
            best_cand = None
            best_score = -999999

            ytm_res, yt_res = await asyncio.gather(
                wavelink.Playable.search(clean_q, source=wavelink.TrackSource.YouTubeMusic),
                wavelink.Playable.search(clean_q, source=wavelink.TrackSource.YouTube),
                return_exceptions=True,
            )
            for res_pool in (ytm_res, yt_res):
                cands = self._extract_lavalink_candidates(res_pool)
                cand, sc = self._rank_lavalink_candidates(cands, sq, exp_dur, track_name, artist_name)
                if cand is not None and sc > best_score:
                    best_cand, best_score = cand, sc

            if best_cand is None or best_score < 0:
                try:
                    retry_found = await wavelink.Playable.search(
                        f"{clean_q} official audio",
                        source=wavelink.TrackSource.YouTubeMusic,
                    )
                    retry_cands = self._extract_lavalink_candidates(retry_found)
                    cand, sc = self._rank_lavalink_candidates(retry_cands, sq, exp_dur, track_name, artist_name)
                    if cand is not None and sc > best_score:
                        best_cand, best_score = cand, sc
                except Exception:
                    pass

            playable = best_cand

        used_bridge = False
        if not playable:
            stream_meta = await self.resolver.get_live_stream(
                sq or next_item.get("title", ""),
                expected_duration=exp_dur,
            )
            if not stream_meta or not stream_meta.get("stream_url"):
                track_title = next_item.get("title") or sq
                stream_meta = await self.resolver.get_fallback_stream(track_title)
            if stream_meta and stream_meta.get("stream_url"):
                raw_stream_url = stream_meta["stream_url"]
                hdrs = stream_meta.get("http_headers") or {}
                needs_bridge = (
                    "streetvoice.com" in sq
                    or "streetvoice.com" in str(next_item.get("webpage_url", ""))
                    or "bilibili.com" in sq
                    or "b23.tv" in sq
                    or "bilibili.com" in str(next_item.get("webpage_url", ""))
                    or ".m3u8" in raw_stream_url.lower()
                    or any(k.lower() in ("referer", "origin") for k in hdrs)
                )
                if not needs_bridge:
                    try:
                        found = await wavelink.Playable.search(raw_stream_url)
                        cands = self._extract_lavalink_candidates(found)
                        playable = cands[0] if cands else None
                    except Exception:
                        playable = None

                if playable is None:
                    bridge = getattr(self.bot, "audio_bridge", None)
                    if bridge and bridge.is_running:
                        try:
                            bridge_url = bridge.register_stream(
                                raw_stream_url,
                                http_headers=hdrs,
                                track_id=stream_meta.get("id") or next_item.get("id"),
                            )
                            found = await wavelink.Playable.search(bridge_url)
                            cands = self._extract_lavalink_candidates(found)
                            playable = cands[0] if cands else None
                            if playable is not None:
                                used_bridge = True
                        except Exception:
                            playable = None

        if not playable:
            return None

        if getattr(playable, "source", "") == "youtube" and self._is_lavalink_yt_playback_blocked():
            yt_url = getattr(playable, "uri", None) or (sq if is_url else "")
            if yt_url and ("youtube.com" in yt_url or "youtu.be" in yt_url):
                merged_fb = {
                    "id": next_item.get("id") or getattr(playable, "identifier", None),
                    "title": next_item.get("title") or getattr(playable, "title", None) or "Unknown",
                    "uploader": next_item.get("uploader") or getattr(playable, "author", None) or "Unknown",
                    "duration": int(next_item.get("duration") or 0) or int((getattr(playable, "length", 0) or 0) // 1000),
                    "thumbnail": next_item.get("thumbnail") or getattr(playable, "artwork", None) or "",
                    "webpage_url": next_item.get("webpage_url") or yt_url,
                    "start_offset": next_item.get("start_offset") or 0.0,
                }
                direct_res = await self._load_youtube_via_lavalink_http(yt_url, merged_fb)
                if direct_res:
                    if store_cache:
                        self._store_pre_resolved(cache_key, direct_res[0], direct_res[1])
                    return direct_res

        vid_id = (stream_meta or {}).get("id") or next_item.get("id") or getattr(playable, "identifier", None)
        dur_sec = int((getattr(playable, "length", 0) or 0) // 1000) or int((stream_meta or {}).get("duration") or next_item.get("duration") or 0)
        start_off = float(next_item.get("start_offset") or (stream_meta or {}).get("start_offset") or 0.0)
        if start_off <= 0.0 and vid_id and re.match(r"^[0-9A-Za-z_-]{11}$", str(vid_id)):
            if str(vid_id) in self.resolver._sponsorblock_cache:
                start_off = float(self.resolver._sponsorblock_cache[str(vid_id)])
            elif store_cache:
                start_off = await self.resolver._fetch_sponsorblock_offset(str(vid_id))

        is_http_playable = used_bridge or getattr(playable, "source", "") == "http" or getattr(playable, "title", None) in (None, "", "Unknown title")
        if is_http_playable:
            title_val = next_item.get("title") or (stream_meta or {}).get("title") or "Unknown"
            uploader_val = next_item.get("uploader") or (stream_meta or {}).get("uploader") or "Unknown"
            thumb_val = next_item.get("thumbnail") or (stream_meta or {}).get("thumbnail") or ""
            web_val = next_item.get("webpage_url") or (stream_meta or {}).get("webpage_url") or ""
            stream_val = (stream_meta or {}).get("stream_url") or web_val
        else:
            title_val = getattr(playable, "title", None) or (stream_meta or {}).get("title") or next_item.get("title") or "Unknown"
            uploader_val = getattr(playable, "author", None) or (stream_meta or {}).get("uploader") or next_item.get("uploader") or "Unknown"
            thumb_val = getattr(playable, "artwork", None) or (stream_meta or {}).get("thumbnail") or next_item.get("thumbnail") or ""
            web_val = getattr(playable, "uri", None) or (stream_meta or {}).get("webpage_url") or next_item.get("webpage_url") or ""
            stream_val = getattr(playable, "uri", None) or (stream_meta or {}).get("stream_url") or ""

        track_info = {
            "id": vid_id,
            "title": title_val,
            "uploader": uploader_val,
            "duration": dur_sec,
            "thumbnail": thumb_val,
            "webpage_url": web_val,
            "stream_url": stream_val,
            "is_live": bool(getattr(playable, "is_stream", False)) if not used_bridge else bool((stream_meta or {}).get("is_live", False)),
            "start_offset": start_off,
        }
        if store_cache and not used_bridge:
            self._store_pre_resolved(cache_key, playable, track_info)
        return playable, track_info

    async def _apply_background_sponsorblock(self, vid_id: str, expected_token: int):
        try:
            offset = await self.resolver._fetch_sponsorblock_offset(vid_id)
            if offset < 3.0:
                return
            if self.stream_token != expected_token or self.is_manual_interruption:
                return
            if not self.is_lavalink_vc() or not self.vc_is_playing():
                return
            elapsed = self.get_elapsed_seconds()
            if elapsed < (offset - 1.0):
                await self.voice_client.seek(int(offset * 1000))
                self.track_start_time = time.time() - offset
                if self.current:
                    self.current["start_offset"] = offset
        except Exception:
            pass

    async def audio_loop(self):
        if hasattr(self.bot, "wait_until_ready"):
            try:
                await self.bot.wait_until_ready()
            except Exception:
                pass
        idle_counter = 0
        idle_playback_counter = 0
        while not self.bot.is_closed():
            try:
                self.play_next_event.clear()
                self.skip_votes.clear()

                if self.auto_disconnect and not hasattr(self.bot, "voice_reaper") and self.vc_is_connected() and getattr(self.voice_client, "channel", None):
                    listeners = [m for m in self.voice_client.channel.members if not m.bot]
                    if not listeners:
                        idle_counter += 1
                        if idle_counter >= 30:
                            await self.disconnect_voice()
                            if self.current_text_channel:
                                msgs = {
                                    "zh_TW": "語音頻道內無人收聽，已自動離開。",
                                    "zh_CN": "语音频道内无人收听，已自动离开。",
                                    "en_US": "Voice channel empty. Disconnected.",
                                    "ja_JP": "ボイスチャンネルに誰もいないため自動退出しました。"
                                }
                                try:
                                    await self.current_text_channel.send(msgs.get(self.locale, "Disconnected."))
                                except Exception:
                                    pass
                            self.destroy()
                            self.bot.players.pop(self.guild_id, None)
                            break
                    else:
                        idle_counter = 0

                seed_info = None
                async with self.lock:
                    if not self.queue and self.autoplay:
                        if getattr(self, "_consecutive_lavalink_failures", 0) >= 3:
                            logging.getLogger("discord.player").warning(
                                "Paused Autoplay in guild %s after %s consecutive Lavalink load failures.",
                                self.guild_id,
                                self._consecutive_lavalink_failures,
                            )
                            self._consecutive_lavalink_failures = 0
                        else:
                            seed_info = self.current or self.current_meta or self.last_played_meta or (self.history[-1] if self.history else None)

                if seed_info and not self.queue:
                    hist_ids = [str(h.get("id")) for h in self.history if h.get("id")]
                    rec = await self.resolver.get_autoplay_recommendation(seed_info, hist_ids)
                    if rec:
                        rec["requester_id"] = self.bot.user.id
                        ap_names = {
                            "zh_TW": "自動推薦續播",
                            "zh_CN": "自动推荐续播",
                            "en_US": "Autoplay",
                            "ja_JP": "自動連續再生"
                        }
                        rec["requester_name"] = ap_names.get(self.locale, "Autoplay")
                        async with self.lock:
                            self.queue.append(rec)

                next_item = None
                async with self.lock:
                    if self.loop_mode == "single" and self.current_meta:
                        next_item = self.current_meta
                    elif self.queue:
                        next_item = self.queue.pop(0)
                        if next_item.get("requester_id") != getattr(getattr(self.bot, "user", None), "id", None):
                            self._consecutive_lavalink_failures = 0
                        if self.current_meta and self.loop_mode != "single" and not next_item.get("_is_resumed_segment"):
                            self.history.append(self.current_meta)
                            if len(self.history) > 50:
                                self.history.pop(0)
                        if self.loop_mode == "queue" and self.current_meta:
                            self.queue.append(self.current_meta)

                    if not next_item:
                        self.current = None
                        self.current_meta = None
                        self.queue_event.clear()

                if not next_item:
                    if self.auto_disconnect and self.vc_is_connected():
                        idle_playback_counter += 1
                        if idle_playback_counter >= 90:
                            await self.disconnect_voice()
                            if self.current_text_channel:
                                msgs = {
                                    "zh_TW": "播放已結束且閒置超過 3 分鐘，已自動離開語音頻道。",
                                    "zh_CN": "播放已结束且闲置超过 3 分钟，已自动离开语音频道。",
                                    "en_US": "Playback finished and idle for 3 minutes. Disconnected.",
                                    "ja_JP": "再生が終了し、3分間待機したため自動退出しました。"
                                }
                                try:
                                    await self.current_text_channel.send(msgs.get(self.locale, "Disconnected due to inactivity."))
                                except Exception:
                                    pass
                            self.destroy()
                            self.bot.players.pop(self.guild_id, None)
                            break
                    try:
                        await asyncio.wait_for(self.queue_event.wait(), timeout=2.0)
                    except (asyncio.TimeoutError, TimeoutError):
                        pass
                    continue
                else:
                    idle_playback_counter = 0

                if self.is_lavalink_vc():
                    if not self.vc_is_connected():
                        if not self.voice_client:
                            self.current = None
                            self.current_meta = None
                            self.destroy()
                            self.bot.players.pop(self.guild_id, None)
                            break
                        async with self.lock:
                            self.queue.insert(0, next_item)
                        await asyncio.sleep(1.0)
                        continue

                    resolved = await self._resolve_lavalink_playable(next_item)
                    if not resolved:
                        self.current = None
                        self.current_meta = None
                        if next_item.get("id"):
                            self.history.append({"id": next_item["id"]})
                        continue

                    playable, track_stream = resolved
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

                    self.stream_token += 1
                    curr_token = self.stream_token
                    self.play_next_event.clear()
                    self.is_manual_interruption = False
                    self._last_track_failed = False
                    self._track_ended_with_error = False
                    start_off = float(self.current.get("start_offset") or next_item.get("start_offset") or 0.0)
                    self.track_start_time = time.time() - start_off
                    self.pause_time = 0.0
                    try:
                        await self.voice_client.play(
                            playable,
                            start=max(0, int(start_off * 1000)),
                            volume=int(round(self.volume * 100)),
                        )
                        if self.current_eq != "flat":
                            await self.apply_lavalink_eq(self.current_eq)
                    except Exception as ex:
                        logging.getLogger("discord.player").error("Lavalink play failed: %s", ex, exc_info=True)
                        self.current = None
                        self.current_meta = None
                        continue

                    vid_id = self.current.get("id")
                    if start_off <= 0.0 and vid_id and re.match(r"^[0-9A-Za-z_-]{11}$", str(vid_id)) and str(vid_id) not in self.resolver._sponsorblock_cache:
                        asyncio.create_task(self._apply_background_sponsorblock(str(vid_id), curr_token))

                    if not (next_item.get("_lavalink_http_fallback") and self.panel_message):
                        asyncio.create_task(self.post_new_panel())
                    self.start_ticker()
                    if self.queue:
                        asyncio.create_task(self._pre_resolve_lavalink_next(self.queue[0]))
                    await self.play_next_event.wait()

                    if (
                        getattr(self, "_track_ended_with_error", False)
                        and not self.is_manual_interruption
                        and not next_item.get("_lavalink_http_fallback")
                        and getattr(playable, "source", "") == "youtube"
                    ):
                        self._track_ended_with_error = False
                        fail_cnt = getattr(self.bot, "_lavalink_yt_direct_fail_count", 0) + 1
                        self.bot._lavalink_yt_direct_fail_count = fail_cnt
                        if fail_cnt >= 2:
                            self.bot._lavalink_yt_direct_blocked_until = time.monotonic() + 1800.0
                            logging.getLogger("discord.player").warning(
                                "YouTube plugin clients failed %s times; routing YouTube playback via direct HTTP into Lavalink for 30 minutes.",
                                fail_cnt,
                            )

                        fb_url = (self.current or {}).get("webpage_url") or next_item.get("webpage_url") or ""
                        if fb_url and ("youtube.com" in fb_url or "youtu.be" in fb_url):
                            retry_item = dict(next_item)
                            if self.current:
                                retry_item["id"] = self.current.get("id") or retry_item.get("id")
                                retry_item["title"] = self.current.get("title") or retry_item.get("title")
                                retry_item["uploader"] = self.current.get("uploader") or retry_item.get("uploader")
                                retry_item["duration"] = self.current.get("duration") or retry_item.get("duration")
                                retry_item["thumbnail"] = self.current.get("thumbnail") or retry_item.get("thumbnail")
                                retry_item["webpage_url"] = fb_url
                            retry_item["search_query"] = fb_url
                            retry_item["_lavalink_http_fallback"] = True
                            retry_item["_is_resumed_segment"] = True
                            self._consecutive_lavalink_failures = max(0, getattr(self, "_consecutive_lavalink_failures", 1) - 1)
                            async with self.lock:
                                self.queue.insert(0, retry_item)
                            continue
                    elif not getattr(self, "_track_ended_with_error", False) and getattr(playable, "source", "") == "youtube":
                        self.bot._lavalink_yt_direct_fail_count = 0
                    continue

                track_stream = await self.resolver.get_live_stream(
                    next_item["search_query"],
                    expected_duration=int(next_item.get("expected_duration") or 0)
                )
                if track_stream and track_stream.get("stream_url", "").startswith("https://b23.tv"):
                    track_stream = None

                if not track_stream or not track_stream.get("stream_url"):
                    is_third_party = any(
                        domain in next_item.get("search_query", "") or domain in next_item.get("webpage_url", "")
                        for domain in ("bilibili.com", "b23.tv", "streetvoice.com", "kkbox.com", "spotify.com", "apple.com", "spotify.link")
                    )
                    if not is_third_party:
                        track_stream = await self.resolver.get_live_stream(
                            next_item.get("title", next_item["search_query"]),
                            expected_duration=int(next_item.get("expected_duration") or 0)
                        )
                    if not track_stream or not track_stream.get("stream_url"):
                        track_title = next_item.get("title") or next_item.get("search_query", "")
                        track_stream = await self.resolver.get_fallback_stream(track_title)

                    if not track_stream or not track_stream.get("stream_url"):
                        self.current = None
                        self.current_meta = None
                        if next_item.get("id"):
                            self.history.append({"id": next_item["id"]})
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

                base_opts = "-loglevel fatal -nostats -vn -nostdin -sn -dn -threads 1"
                eq_filter = EQ_PRESETS.get(self.current_eq, "")
                if eq_filter:
                    base_opts += f' -af "{eq_filter}"'

                self.is_manual_interruption = False
                play_target = self.current["stream_url"]
                if not play_target or not isinstance(play_target, str) or not play_target.strip():
                    self.current = None
                    self.current_meta = None
                    continue

                before_opts = self.current.get("before_options", DEFAULT_BEFORE_OPTS)
                start_off = float(self.current.get("start_offset") or next_item.get("start_offset") or 0.0)

                if not self.current.get("is_live", False) and hasattr(self.resolver, "get_cached_track_path"):
                    track_uid = self.current.get("id") or next_item.get("id") or str(abs(hash(self.current["stream_url"])))
                    cached_file = self.resolver.get_cached_track_path(track_uid)
                    if cached_file:
                        play_target = cached_file
                        before_opts = "-loglevel fatal -nostats"

                if start_off > 0.0 and "-ss" not in before_opts:
                    before_opts = f"{before_opts} -ss {start_off}"

                try:
                    raw_source = SafeFFmpegPCMAudio(
                        play_target,
                        before_options=before_opts,
                        options=base_opts
                    )
                    audio_source = discord.PCMVolumeTransformer(raw_source, volume=self.volume)
                except Exception as ex:
                    logging.getLogger("discord.player").error("SafeFFmpegPCMAudio initialization failed: %s", ex, exc_info=True)
                    self.current = None
                    self.current_meta = None
                    continue

                if self.voice_client and self.vc_is_connected():
                    self.track_start_time = time.time() - start_off
                    self.pause_time = 0.0
                    self.stream_token += 1
                    curr_token = self.stream_token
                    try:
                        self.voice_client.play(audio_source, after=self._make_after_callback(curr_token))
                    except Exception as ex:
                        logging.getLogger("discord.player").error("voice_client.play failed: %s", ex, exc_info=True)
                        if audio_source:
                            try:
                                audio_source.cleanup()
                            except Exception:
                                pass
                        self.current = None
                        self.current_meta = None
                        continue
                    await self.post_new_panel()
                    self.start_ticker()
                    if self.queue:
                        asyncio.create_task(self._preload_upcoming_track(self.queue[0]))
                    await self.play_next_event.wait()
                    elapsed = time.time() - self.track_start_time
                    exp_dur = self.current.get("duration", 0)
                    if (
                        not self.is_manual_interruption
                        and not self.is_restarting_stream
                        and not self.current.get("is_live", False)
                        and exp_dur > 20
                        and elapsed >= 3.0
                        and elapsed < (exp_dur - 8)
                        and next_item.get("_resume_retries", 0) < 2
                    ):
                        next_item["_resume_retries"] = next_item.get("_resume_retries", 0) + 1
                        next_item["_is_resumed_segment"] = True
                        next_item["start_offset"] = max(0, int(elapsed))
                        if hasattr(self.resolver, "invalidate_stream_cache"):
                            await self.resolver.invalidate_stream_cache(next_item["search_query"])
                        async with self.lock:
                            self.queue.insert(0, next_item)
                        await asyncio.sleep(1.0)
                        continue

                    if elapsed < 3.0 and not self.is_restarting_stream and not self.is_manual_interruption:
                        if next_item.get("id"):
                            self.history.append({
                                "id": next_item["id"],
                                "title": next_item.get("title", "Unknown"),
                                "uploader": next_item.get("uploader", "Unknown"),
                                "webpage_url": next_item.get("webpage_url", ""),
                                "search_query": next_item.get("search_query", "")
                            })
                        if hasattr(self.resolver, "invalidate_stream_cache"):
                            await self.resolver.invalidate_stream_cache(next_item["search_query"])
                else:
                    if not self.voice_client:
                        self.current = None
                        self.current_meta = None
                        self.destroy()
                        self.bot.players.pop(self.guild_id, None)
                        break
                    async with self.lock:
                        self.queue.insert(0, next_item)
                    await asyncio.sleep(1.0)

            except Exception as e:
                print(f"Audio playback error: {e}")
                await asyncio.sleep(2)

    async def _preload_upcoming_track(self, next_track: Dict[str, Any]):
        try:
            if not next_track or self.is_lavalink_vc():
                return
            sq = next_track.get("search_query", "")
            if not sq:
                return
            exp_dur = int(next_track.get("expected_duration") or 0)
            stream_info = await self.resolver.get_live_stream(sq, expected_duration=exp_dur)
            if stream_info and stream_info.get("stream_url") and hasattr(self.resolver, "preload_track_audio"):
                t_id = stream_info.get("id") or next_track.get("id") or str(abs(hash(stream_info["stream_url"])))
                await self.resolver.preload_track_audio(
                    stream_info["stream_url"],
                    stream_info.get("http_headers", {}),
                    t_id
                )
        except Exception:
            pass

    def _build_embed(self) -> discord.Embed:
        loc = self.locale
        i18n = self.bot.i18n
        curr = self.current or {}
        target_url = curr.get("webpage_url", "")
        if not target_url or not target_url.startswith("http"):
            target_url = "https://discord.com"

        embed = discord.Embed(
            title=i18n.get("PANEL_TITLE", loc),
            description=f"[{curr.get('title', 'Unknown')}]({target_url})",
            color=0x3498db
        )

        thumb = curr.get("thumbnail")
        if thumb and thumb.startswith("http"):
            embed.set_thumbnail(url=thumb)

        embed.add_field(name=i18n.get("PANEL_UPLOADER", loc), value=curr.get("uploader", "Unknown"), inline=True)
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
                await self.panel_message.edit(view=None)
            except Exception:
                pass
            self.panel_message = None

        if self.panel_view and not self.panel_view.is_finished():
            try:
                self.panel_view.stop()
            except Exception:
                pass

        embed = self._build_embed()
        view = PlayerControls(self)
        self.panel_view = view
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
        if self.panel_view and not self.panel_view.is_finished():
            try:
                self.panel_view.stop()
            except Exception:
                pass
        view = PlayerControls(self)
        self.panel_view = view
        try:
            await self.panel_message.edit(embed=embed, view=view)
        except discord.NotFound:
            self.panel_message = None
        except discord.HTTPException as e:
            if getattr(e, "status", None) == 404:
                self.panel_message = None
        except Exception:
            pass