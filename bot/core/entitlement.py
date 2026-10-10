import os
import time
import asyncio
from typing import Optional, Dict, Tuple
import discord
from discord.ext import commands

DEFAULT_SUPPORT_GUILD_ID = 1039860460389941338
DEFAULT_PATREON_ROLE_ID = 0

class EntitlementManager:
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.support_guild_id = int(os.getenv("SUPPORT_GUILD_ID", DEFAULT_SUPPORT_GUILD_ID))
        self.patreon_role_id = int(os.getenv("PATREON_ROLE_ID", DEFAULT_PATREON_ROLE_ID))
        self._cache: Dict[int, Tuple[float, bool]] = {}
        self._lock = asyncio.Lock()
        self._in_flight: Dict[int, asyncio.Future] = {}
        self._recon_task: Optional[asyncio.Task] = None
        try:
            loop = asyncio.get_running_loop()
            self._recon_task = loop.create_task(self._reconciliation_loop())
        except RuntimeError:
            pass

    def start_reconciliation(self):
        if self._recon_task is None or self._recon_task.done():
            loop = asyncio.get_running_loop()
            self._recon_task = loop.create_task(self._reconciliation_loop())

    def invalidate_user(self, user_id: int):
        self._cache.pop(user_id, None)

    async def handle_patron_revocation(self, user_id: int):
        self.invalidate_user(user_id)
        if not hasattr(self.bot, "players"):
            return
        for guild_id, player in list(self.bot.players.items()):
            try:
                if not player.auto_disconnect and getattr(player, "mode_247_enabled_by", None) == user_id:
                    player.auto_disconnect = True
                    player.mode_247_enabled_by = None
                    if hasattr(self.bot, "voice_reaper") and player.voice_client and player.voice_client.channel:
                        if self.bot.voice_reaper.is_channel_empty(player.voice_client.channel):
                            guild = self.bot.get_guild(guild_id)
                            if guild:
                                async def _dc(g):
                                    if player.voice_client:
                                        player.stop_current()
                                        await player.voice_client.disconnect(force=True)
                                self.bot.voice_reaper.schedule_reap(guild, _dc)
                    asyncio.create_task(player.update_panel_inplace())

                if player.current_eq != "flat" and getattr(player, "eq_enabled_by", None) == user_id:
                    player.eq_enabled_by = None
                    asyncio.create_task(player.set_eq("flat"))
                    asyncio.create_task(player.update_panel_inplace())
            except Exception:
                pass

    async def handle_member_update(self, before: discord.Member, after: discord.Member):
        if after.guild.id != self.support_guild_id:
            return
        had_role = any(r.id == self.patreon_role_id for r in before.roles)
        has_role = any(r.id == self.patreon_role_id for r in after.roles)
        if had_role and not has_role:
            await self.handle_patron_revocation(after.id)
        elif not had_role and has_role:
            self._cache[after.id] = (time.monotonic(), True)

    async def handle_member_remove(self, member: discord.Member):
        if member.guild.id != self.support_guild_id:
            return
        had_role = any(r.id == self.patreon_role_id for r in member.roles)
        if had_role:
            await self.handle_patron_revocation(member.id)
        else:
            self.invalidate_user(member.id)

    async def _reconciliation_loop(self):
        while not self.bot.is_closed():
            try:
                await asyncio.sleep(60.0)
                if not hasattr(self.bot, "players"):
                    continue
                for guild_id, player in list(self.bot.players.items()):
                    target_247 = getattr(player, "mode_247_enabled_by", None)
                    if not player.auto_disconnect and target_247:
                        is_valid = await self.is_patron(target_247)
                        if not is_valid:
                            await self.handle_patron_revocation(target_247)

                    target_eq = getattr(player, "eq_enabled_by", None)
                    if player.current_eq != "flat" and target_eq:
                        is_valid = await self.is_patron(target_eq)
                        if not is_valid:
                            await self.handle_patron_revocation(target_eq)
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    async def is_patron(self, user_id: int) -> bool:
        if not self.support_guild_id or not self.patreon_role_id:
            return False

        now = time.monotonic()
        if user_id in self._cache:
            cached_time, is_eligible = self._cache[user_id]
            ttl = 300.0 if is_eligible else 60.0
            if now - cached_time < ttl:
                return is_eligible

        async with self._lock:
            if user_id in self._in_flight:
                return await self._in_flight[user_id]
            loop = asyncio.get_running_loop()
            fut = loop.create_future()
            self._in_flight[user_id] = fut

        try:
            guild = self.bot.get_guild(self.support_guild_id)
            if not guild:
                try:
                    guild = await self.bot.fetch_guild(self.support_guild_id)
                except Exception:
                    guild = None

            if not guild:
                self._cache[user_id] = (now, False)
                fut.set_result(False)
                return False

            member = guild.get_member(user_id)
            if not member:
                try:
                    member = await guild.fetch_member(user_id)
                except Exception:
                    member = None

            is_eligible = False
            if member:
                is_eligible = any(r.id == self.patreon_role_id for r in member.roles)

            self._cache[user_id] = (now, is_eligible)
            if len(self._cache) > 10000:
                self._cache.pop(next(iter(self._cache)), None)

            fut.set_result(is_eligible)
            return is_eligible
        except Exception:
            self._cache[user_id] = (now, False)
            if not fut.done():
                fut.set_result(False)
            return False
        finally:
            async with self._lock:
                self._in_flight.pop(user_id, None)

    async def can_use_247(self, user_id: int) -> bool:
        return await self.is_patron(user_id)

    async def can_use_equalizer(self, user_id: int) -> bool:
        return await self.is_patron(user_id)

    async def can_use_premium_source(self, user_id: int) -> bool:
        return await self.is_patron(user_id)

    async def get_queue_limit(self, user_id: int) -> int:
        patron = await self.is_patron(user_id)
        return 200 if patron else 100
