import asyncio
from typing import Dict, Optional, Callable, Awaitable
import discord

REAP_TIMEOUT_SECONDS = 60.0

class VoiceReaper:
    def __init__(self, is_247_predicate: Optional[Callable[[int], bool]] = None):
        self._timers: Dict[int, asyncio.Task] = {}
        self._is_247_predicate = is_247_predicate

    def is_channel_empty(self, voice_channel: discord.VoiceChannel) -> bool:
        human_members = [m for m in voice_channel.members if not m.bot]
        return len(human_members) == 0

    def cancel_timer(self, guild_id: int):
        task = self._timers.pop(guild_id, None)
        if task and not task.done():
            task.cancel()

    def schedule_reap(
        self,
        guild: discord.Guild,
        disconnect_callback: Callable[[discord.Guild], Awaitable[None]]
    ):
        guild_id = guild.id
        if self._is_247_predicate and self._is_247_predicate(guild_id):
            return

        if guild_id in self._timers and not self._timers[guild_id].done():
            return

        async def _countdown():
            try:
                await asyncio.sleep(REAP_TIMEOUT_SECONDS)
                if self._is_247_predicate and self._is_247_predicate(guild_id):
                    return
                await disconnect_callback(guild)
            except asyncio.CancelledError:
                pass
            finally:
                self._timers.pop(guild_id, None)

        self._timers[guild_id] = asyncio.create_task(_countdown())

    async def handle_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
        disconnect_callback: Callable[[discord.Guild], Awaitable[None]]
    ):
        guild = member.guild
        voice_client = guild.voice_client

        if not voice_client or not voice_client.channel:
            self.cancel_timer(guild.id)
            return

        bot_channel = voice_client.channel

        if before.channel == bot_channel or after.channel == bot_channel:
            if self.is_channel_empty(bot_channel):
                self.schedule_reap(guild, disconnect_callback)
            else:
                self.cancel_timer(guild.id)
