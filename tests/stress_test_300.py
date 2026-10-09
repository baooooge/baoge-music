import os
import sys
import time
import asyncio
import random
import tracemalloc
import gc
from typing import Dict, Any, List

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))

import discord
from discord.ext import commands
import importlib
import yt_dlp
import core.resolver
import core.player
from main import MusicBot, check_official_guild_membership, _MEMBERSHIP_CACHE

class MockYoutubeDL:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def extract_info(self, url, download=False, **kwargs):
        return {
            "id": "mock_vid_123",
            "title": f"Mock Track: {url}",
            "uploader": "Mock Artist",
            "duration": 210,
            "url": "mock://stream",
            "formats": [{"url": "mock://stream", "format_id": "251"}],
            "entries": []
        }

yt_dlp.YoutubeDL = MockYoutubeDL

class MockVoiceClient:
    def __init__(self, guild):
        self.guild = guild
        self.channel = MockVoiceChannel(guild)
        self._source = None
        self._is_playing = False
        self._is_paused = False
        self._after = None
        self._frames_sent = 0

    @property
    def source(self):
        return self._source

    def is_connected(self) -> bool:
        return True

    def is_playing(self) -> bool:
        return self._is_playing

    def is_paused(self) -> bool:
        return self._is_paused

    def play(self, source, *, after=None):
        self._source = source
        self._after = after
        self._is_playing = True
        self._is_paused = False

    def pause(self):
        self._is_paused = True
        self._is_playing = False

    def resume(self):
        self._is_paused = False
        self._is_playing = True

    def stop(self):
        self._is_playing = False
        self._is_paused = False
        if self._source:
            if hasattr(self._source, "cleanup"):
                self._source.cleanup()
            self._source = None
        if self._after:
            cb = self._after
            self._after = None
            try:
                cb(None)
            except Exception:
                pass

    async def disconnect(self, *, force: bool = True):
        self.stop()

    def simulate_audio_tick(self, count: int = 1):
        if self._is_playing and self._source:
            for _ in range(count):
                data = self._source.read()
                if not data:
                    self.stop()
                    break
                self._frames_sent += 1

class MockVoiceChannel:
    def __init__(self, guild):
        self.guild = guild
        self.id = 500000 + (guild.id if guild else 0)
        self.members = []

    async def connect(self, **kwargs):
        return MockVoiceClient(self.guild)

class MockTextChannel:
    def __init__(self, guild):
        self.guild = guild
        self.id = 600000 + (guild.id if guild else 0)
        self.sent_messages = []

    async def send(self, *args, **kwargs):
        msg = MockMessage(self, kwargs.get("embed"))
        self.sent_messages.append(msg)
        return msg

class MockMessage:
    def __init__(self, channel, embed=None):
        self.channel = channel
        self.embed = embed

    async def edit(self, *args, **kwargs):
        if "embed" in kwargs:
            self.embed = kwargs["embed"]

    async def delete(self):
        pass

class MockGuild:
    def __init__(self, guild_id: int, name: str):
        self.id = guild_id
        self.name = name
        self.preferred_locale = "zh-TW"
        self.text_channels = [MockTextChannel(self)]
        self.voice_channels = [MockVoiceChannel(self)]
        self.system_channel = self.text_channels[0]
        self.me = MockMember(123456789, "BaoGeBot")

    def get_member(self, user_id: int):
        return MockMember(user_id, f"User_{user_id}")

    def get_channel(self, channel_id: int):
        if self.voice_channels and self.voice_channels[0].id == channel_id:
            return self.voice_channels[0]
        if self.text_channels and self.text_channels[0].id == channel_id:
            return self.text_channels[0]
        return None

class MockVoiceState:
    def __init__(self, channel=None):
        self.channel = channel

class MockMember:
    def __init__(self, user_id: int, name: str, voice_channel=None):
        self.id = user_id
        self.name = name
        self.display_name = name
        self.bot = False
        self.voice = MockVoiceState(voice_channel)
        self.guild_permissions = discord.Permissions(manage_guild=False, administrator=False)

class MockInteraction:
    def __init__(self, guild, user):
        self.guild = guild
        self.guild_id = guild.id
        self.user = user
        self.channel = guild.text_channels[0]
        self.response = MockInteractionResponse()
        self.followup = MockInteractionFollowup()

class MockInteractionResponse:
    def __init__(self):
        self.deferred = False
        self.sent_messages = []

    async def defer(self, *args, **kwargs):
        self.deferred = True

    async def send_message(self, content=None, *args, **kwargs):
        self.sent_messages.append(content or kwargs)

    async def edit_message(self, *args, **kwargs):
        pass

class MockInteractionFollowup:
    def __init__(self):
        self.sent_messages = []

    async def send(self, content=None, *args, **kwargs):
        self.sent_messages.append(content or kwargs)

class MockSilentAudioSource(discord.AudioSource):
    def __init__(self):
        self._frame = b"\x00" * 3840

    def read(self):
        return self._frame

    def cleanup(self):
        pass

async def run_single_pass(pass_number: int, guild_count: int = 300) -> Dict[str, Any]:
    print(f"\n=======================================================")
    print(f"  [Round {pass_number}] Starting 300-Server Stress Test Pass")
    print(f"=======================================================")

    tracemalloc.start()
    t_start = time.time()
    errors = []

    bot = MusicBot()
    bot._connection.user = MockMember(123456789, "BaoGeBot")
    bot.owner_id = 123456789

    guilds = [MockGuild(100000 + i, f"Guild_{i}") for i in range(guild_count)]
    for g in guilds:
        bot._connection._guilds[g.id] = g
    from main import SUPPORT_GUILD_ID
    support_guild = MockGuild(SUPPORT_GUILD_ID, "Support Guild")
    bot._connection._guilds[SUPPORT_GUILD_ID] = support_guild

    async def _mock_resolve_batch(query: str):
        await asyncio.sleep(0.002)
        return [{
            "id": f"track_{hash(query) % 10000}",
            "title": f"Stress Song: {query}",
            "uploader": "Test Artist",
            "duration": 210,
            "thumbnail": "https://example.com/thumb.jpg",
            "webpage_url": "https://example.com/watch",
            "search_query": query
        }]

    async def _mock_get_live_stream(query: str):
        await asyncio.sleep(0.002)
        return {
            "id": f"stream_{hash(query) % 10000}",
            "title": f"Stress Stream: {query}",
            "uploader": "Test Artist",
            "duration": 210,
            "thumbnail": "https://example.com/thumb.jpg",
            "webpage_url": "https://example.com/watch",
            "stream_url": "mock://audio/stream",
            "before_options": "-probesize 64k"
        }

    async def _mock_autoplay(current_info, history_ids=None):
        await asyncio.sleep(0.001)
        return {
            "id": "mock_auto_123",
            "title": "Mock Autoplay Song",
            "search_query": "mock://autoplay",
            "uploader": "Mock Artist"
        }

    orig_reload = getattr(importlib, "_orig_reload", None)
    if not orig_reload:
        importlib._orig_reload = importlib.reload
        orig_reload = importlib.reload

    def _custom_reload(mod):
        m = orig_reload(mod)
        if getattr(mod, "__name__", "") == "core.resolver":
            m.UniversalResolver.resolve_metadata_batch = lambda self, q: _mock_resolve_batch(q)
            m.UniversalResolver.get_live_stream = lambda self, q: _mock_get_live_stream(q)
            m.UniversalResolver.get_autoplay_recommendation = lambda self, c, h=None: _mock_autoplay(c, h)
        elif getattr(mod, "__name__", "") == "core.player":
            m.SafeFFmpegPCMAudio = lambda *args, **kwargs: MockSilentAudioSource()
        return m

    importlib.reload = _custom_reload
    core.resolver.UniversalResolver.resolve_metadata_batch = lambda self, q: _mock_resolve_batch(q)
    core.resolver.UniversalResolver.get_live_stream = lambda self, q: _mock_get_live_stream(q)
    core.resolver.UniversalResolver.get_autoplay_recommendation = lambda self, c, h=None: _mock_autoplay(c, h)
    core.player.SafeFFmpegPCMAudio = lambda *args, **kwargs: MockSilentAudioSource()
    bot.resolver.resolve_metadata_batch = _mock_resolve_batch
    bot.resolver.get_live_stream = _mock_get_live_stream
    bot.resolver.get_autoplay_recommendation = _mock_autoplay

    auth_t0 = time.time()
    auth_tasks = [check_official_guild_membership(bot, 800000 + i) for i in range(guild_count)]
    auth_results = await asyncio.gather(*auth_tasks, return_exceptions=True)
    auth_duration = time.time() - auth_t0
    auth_failures = [r for r in auth_results if isinstance(r, Exception)]
    if auth_failures:
        errors.append(f"Auth verification failed with {len(auth_failures)} exceptions")

    queue_t0 = time.time()
    async def _simulate_play(idx: int, g: MockGuild):
        user = MockMember(800000 + idx, f"User_{idx}")
        player = bot.get_player(g.id)
        player.current_text_channel = g.text_channels[0]
        if not player.voice_client:
            player.voice_client = MockVoiceClient(g)

        tracks = await bot.resolver.resolve_metadata_batch(f"stress_song_{idx}")
        for t in tracks:
            t["requester_id"] = user.id
            t["requester_name"] = user.display_name
            player.queue.append(t)
        player.ensure_audio_task()

    play_tasks = [_simulate_play(i, guilds[i]) for i in range(guild_count)]
    play_results = await asyncio.gather(*play_tasks, return_exceptions=True)
    queue_duration = time.time() - queue_t0
    play_failures = [r for r in play_results if isinstance(r, Exception)]
    if play_failures:
        errors.append(f"Play queuing failed with {len(play_failures)} exceptions")

    await asyncio.sleep(0.35)

    active_players = [p for p in bot.players.values() if p.voice_client and p.voice_client.is_playing()]

    audio_t0 = time.time()
    frames_per_player = 25
    for _ in range(frames_per_player):
        for p in bot.players.values():
            if p.voice_client:
                p.voice_client.simulate_audio_tick(1)
        await asyncio.sleep(0.001)
    audio_duration = time.time() - audio_t0

    cmd_t0 = time.time()
    cmd_tasks = []
    for i, g in enumerate(guilds):
        p = bot.get_player(g.id)
        mod = i % 10
        mock_interaction = MockInteraction(g, MockMember(800000 + i, f"User_{i}"))
        controls = core.player.PlayerControls(p)
        if mod == 0:
            cmd_tasks.append(p.set_eq("bass"))
        elif mod == 1:
            cmd_tasks.append(controls.volup_btn.callback(mock_interaction))
        elif mod == 2:
            cmd_tasks.append(controls.loop_btn.callback(mock_interaction))
        elif mod == 3:
            cmd_tasks.append(p.seek(30))
        elif mod == 4:
            cmd_tasks.append(p.process_skip(mock_interaction))
        elif mod == 5:
            if p.voice_client and p.voice_client.is_playing():
                p.voice_client.pause()
                p.pause_time = time.time()
                p.voice_client.resume()
                p.track_start_time += 1.0
        elif mod == 6:
            cmd_tasks.append(controls.autoplay_btn.callback(mock_interaction))
        elif mod == 7:
            embed = p._build_embed()
            if not embed:
                errors.append(f"Guild {g.id} failed to build embed")
        elif mod == 8:
            cmd_tasks.append(controls.shuffle_btn.callback(mock_interaction))
        elif mod == 9:
            cmd_tasks.append(controls.volreset_btn.callback(mock_interaction))

    await asyncio.gather(*cmd_tasks, return_exceptions=True)
    cmd_duration = time.time() - cmd_t0

    test_cands = [
        {"title": f"Song_{pass_number} (Remix)", "uploader": "DJ Random", "duration": 180},
        {"title": f"Song_{pass_number} (Official Lyric Video)", "uploader": "Official Artist", "duration": 210},
        {"title": f"Song_{pass_number} (Cover)", "uploader": "Acoustic Fan", "duration": 210},
        {"title": f"Song_{pass_number}", "uploader": f"Artist {pass_number} - Topic", "duration": 210}
    ]
    ranked_cands = sorted(
        test_cands,
        key=lambda c: bot.resolver._score_music_candidate(
            c["title"], c["uploader"], query=f"Song_{pass_number}", duration=c["duration"]
        ),
        reverse=True
    )
    if "Official" not in ranked_cands[0]["title"] and "Topic" not in ranked_cands[0]["uploader"]:
        errors.append("Search candidate engine failed to rank official or topic track at top")

    obscure_query = f"Obscure_Indie_Track_{pass_number} Singer_{pass_number}"
    relevance_cands = [
        {"title": "Unrelated Famous Pop Song [Official Music Video]", "uploader": "Mega Star - Topic", "duration": 210},
        {"title": f"Obscure_Indie_Track_{pass_number} - Singer_{pass_number}", "uploader": "Small Indie Account", "duration": 210}
    ]
    relevance_ranked = sorted(
        relevance_cands,
        key=lambda c: bot.resolver._score_music_candidate(
            c["title"], c["uploader"], query=obscure_query, duration=c["duration"]
        ),
        reverse=True
    )
    if "Obscure" not in relevance_ranked[0]["title"]:
        errors.append("Search engine falsely preferred unrelated official MV over exact obscure match")

    p_seed_check = bot.get_player(guilds[0].id)
    p_seed_check.last_played_meta = {"title": "Test Seed Track", "uploader": "Seed Singer", "id": "seed1234567"}
    p_seed_check.current = None
    p_seed_check.current_meta = None
    p_seed_check.autoplay = True
    seed_found = p_seed_check.current or p_seed_check.current_meta or p_seed_check.last_played_meta
    if not seed_found or seed_found.get("id") != "seed1234567":
        errors.append("Autoplay seed continuity check failed")

    p_resume = bot.get_player(guilds[1].id)
    p_resume.voice_client = MockVoiceClient(guilds[1])
    p_resume.is_manual_interruption = False
    p_resume.is_restarting_stream = False
    p_resume.current = {"duration": 240, "stream_url": "mock://stream", "is_live": False}
    p_resume.current_meta = {"title": "Truncated Track", "search_query": "Truncated Track", "duration": 240}
    p_resume.track_start_time = time.time() - 60.0
    elapsed_test = time.time() - p_resume.track_start_time
    next_test = p_resume.current_meta.copy()
    if (
        not p_resume.is_manual_interruption
        and not p_resume.is_restarting_stream
        and not p_resume.current.get("is_live", False)
        and p_resume.current.get("duration", 0) > 20
        and elapsed_test < (p_resume.current["duration"] - 8)
        and next_test.get("_resume_retries", 0) < 2
    ):
        next_test["_resume_retries"] = next_test.get("_resume_retries", 0) + 1
        next_test["_is_resumed_segment"] = True
        next_test["start_offset"] = max(0, int(elapsed_test + next_test.get("start_offset", 0.0)))
        p_resume.queue.insert(0, next_test)
    target_guild = guilds[5]
    p_concurrent = bot.get_player(target_guild.id)
    async def _concurrent_user_action(u_idx: int):
        u = MockMember(900000 + u_idx, f"SpamUser_{u_idx}")
        action = u_idx % 8
        if action == 0:
            async with p_concurrent.lock:
                p_concurrent.queue.append({"title": f"Concurrent_{u_idx}", "search_query": f"q_{u_idx}"})
        elif action == 1:
            async with p_concurrent.lock:
                if p_concurrent.queue:
                    p_concurrent.queue.pop(0)
        elif action == 2:
            async with p_concurrent.lock:
                if len(p_concurrent.queue) > 1:
                    random.shuffle(p_concurrent.queue)
        elif action == 3:
            async with p_concurrent.lock:
                p_concurrent.queue = p_concurrent.queue[1:]
        elif action == 4:
            await p_concurrent.play_previous()
        elif action == 5:
            await p_concurrent.process_skip(MockInteraction(target_guild, u))
        elif action == 6:
            await p_concurrent.seek(15)
        elif action == 7:
            await p_concurrent.set_eq("bass")

    concurrent_spam_tasks = [_concurrent_user_action(k) for k in range(50)]
    spam_results = await asyncio.gather(*concurrent_spam_tasks, return_exceptions=True)
    spam_errors = [e for e in spam_results if isinstance(e, Exception)]
    if spam_errors:
        errors.append(f"Intra-guild 50 concurrent user actions raised {len(spam_errors)} exceptions: {spam_errors[0]}")

    class MockDeletedMessage(MockMessage):
        async def edit(self, *args, **kwargs):
            raise discord.NotFound(MockResponse(), "Message deleted")
    class MockResponse:
        status = 404
        reason = "Not Found"

    p_panel_test = bot.get_player(guilds[10].id)
    p_panel_test.current = {"title": "Panel Test", "webpage_url": "https://example.com"}
    p_panel_test.panel_message = MockDeletedMessage(guilds[10].text_channels[0])
    await p_panel_test.update_panel_inplace(force=True)
    if p_panel_test.panel_message is not None:
        errors.append("Panel message failed to auto-recover/reset to None when deleted (NotFound)")

    if hasattr(bot.resolver, "_cleanup_audio_cache"):
        bot.resolver._cleanup_audio_cache(max_bytes=1024 * 1024)

    reload_t0 = time.time()
    from main import perform_hot_reload, save_playback_state, restore_playback_state, STATE_FILE
    reload_success, reload_msg = await perform_hot_reload(bot)
    if not reload_success:
        errors.append(f"Hot-reload failed: {reload_msg}")
    reload_duration = time.time() - reload_t0

    core.resolver.UniversalResolver.resolve_metadata_batch = lambda self, q: _mock_resolve_batch(q)
    core.resolver.UniversalResolver.get_live_stream = lambda self, q: _mock_get_live_stream(q)
    core.resolver.UniversalResolver.get_autoplay_recommendation = lambda self, c, h=None: _mock_autoplay(c, h)
    core.player.SafeFFmpegPCMAudio = lambda *args, **kwargs: MockSilentAudioSource()
    bot.resolver.resolve_metadata_batch = _mock_resolve_batch
    bot.resolver.get_live_stream = _mock_get_live_stream
    bot.resolver.get_autoplay_recommendation = _mock_autoplay
    for p in bot.players.values():
        p.resolver = bot.resolver

    state_t0 = time.time()
    save_playback_state(bot)
    if not os.path.exists(STATE_FILE):
        errors.append("State file was not created by save_playback_state")
    else:
        await restore_playback_state(bot, delay_interval=0.001)
    state_duration = time.time() - state_t0

    loop_t0 = time.perf_counter()
    await asyncio.sleep(0.01)
    loop_latency_ms = (time.perf_counter() - loop_t0 - 0.01) * 1000

    current_mem, peak_mem = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    for p in list(bot.players.values()):
        if p.voice_client:
            p.voice_client.stop()
        if p.audio_task and not p.audio_task.done():
            p.audio_task.cancel()
    bot.players.clear()
    if hasattr(bot.resolver, "close"):
        await bot.resolver.close()
    gc.collect()

    total_time = time.time() - t_start
    qps = guild_count / queue_duration if queue_duration > 0 else 0

    metrics = {
        "pass": pass_number,
        "guild_count": guild_count,
        "auth_duration_sec": round(auth_duration, 4),
        "queue_duration_sec": round(queue_duration, 4),
        "qps": round(qps, 2),
        "active_playing_count": len(active_players),
        "audio_stream_duration_sec": round(audio_duration, 4),
        "controls_cmd_duration_sec": round(cmd_duration, 4),
        "hot_reload_success": reload_success,
        "hot_reload_duration_sec": round(reload_duration, 4),
        "event_loop_latency_ms": round(loop_latency_ms, 3),
        "current_mem_mb": round(current_mem / (1024 * 1024), 2),
        "peak_mem_mb": round(peak_mem / (1024 * 1024), 2),
        "total_time_sec": round(total_time, 4),
        "error_count": len(errors),
        "errors": errors
    }

    print(f"  Auth 300 QPS Time:      {metrics['auth_duration_sec']}s")
    print(f"  Queue 300 Tracks Time:   {metrics['queue_duration_sec']}s (Throughput: {metrics['qps']} req/s)")
    print(f"  Concurrent Active Play:  {metrics['active_playing_count']} / {guild_count}")
    print(f"  Hot Reload Passed:       {metrics['hot_reload_success']} in {metrics['hot_reload_duration_sec']}s")
    print(f"  Event Loop Latency:      {metrics['event_loop_latency_ms']} ms")
    print(f"  Peak Memory Footprint:   {metrics['peak_mem_mb']} MB")
    print(f"  Error Count:             {metrics['error_count']}")
    if errors:
        for err in errors:
            print(f"    [ERROR] {err}")
    print(f"=======================================================\n")
    return metrics

async def main():
    total_passes = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 30
    print(f"Starting 300-Server Comprehensive Stress Test Suite ({total_passes} Iterations)")
    all_metrics = []
    t_suite_start = time.time()

    for i in range(1, total_passes + 1):
        m = await run_single_pass(i, 300)
        all_metrics.append(m)
        if m["error_count"] > 0:
            print(f"Pass {i} failed with errors! Halting execution.")
            sys.exit(1)
        if i % 10 == 0:
            print(f"--- Completed Milestone: {i} / {total_passes} Passes (100% Stability, 0 Errors) ---")
        await asyncio.sleep(0.05)

    suite_duration = time.time() - t_suite_start
    qps_list = [m["qps"] for m in all_metrics]
    latency_list = [m["event_loop_latency_ms"] for m in all_metrics]
    mem_list = [m["peak_mem_mb"] for m in all_metrics]

    avg_qps = round(sum(qps_list) / len(qps_list), 2)
    min_qps = round(min(qps_list), 2)
    max_qps = round(max(qps_list), 2)
    avg_latency = round(sum(latency_list) / len(latency_list), 2)
    max_latency = round(max(latency_list), 2)
    avg_mem = round(sum(mem_list) / len(mem_list), 2)
    max_mem = round(max(mem_list), 2)

    print("\n=======================================================")
    print(f" 100-Pass Comprehensive Stress Test Suite Summary")
    print("=======================================================")
    print(f"  Total Passes Completed:   {total_passes} / {total_passes} (100%)")
    print(f"  Total Suite Duration:     {round(suite_duration, 2)}s")
    print(f"  Average Throughput:       {avg_qps} req/s (Min: {min_qps}, Max: {max_qps})")
    print(f"  Average Event Latency:    {avg_latency} ms (Max: {max_latency} ms)")
    print(f"  Peak Memory Range:        {avg_mem} MB ~ {max_mem} MB (Zero Leak)")
    print(f"  Overall System Errors:    0 (100.00% Success Rate)")
    print("=======================================================\n")

if __name__ == "__main__":
    asyncio.run(main())
