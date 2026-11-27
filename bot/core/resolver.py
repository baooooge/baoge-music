import os
import asyncio
import json
import re
import gzip
import time
import threading
import socket
import logging
from logging.handlers import RotatingFileHandler
import urllib.parse
import urllib.request
from typing import Dict, Any, List, Optional
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import aiohttp
import unicodedata
from bs4 import BeautifulSoup
import yt_dlp
import html as html_lib
import base64
try:
    import zhconv
except ImportError:
    zhconv = None

DEFAULT_BEFORE_OPTS = (
    "-loglevel fatal -nostats -reconnect 1 -reconnect_streamed 1 "
    "-reconnect_on_network_error 1 -reconnect_on_http_error 4xx,5xx "
    "-reconnect_at_eof 1 -reconnect_delay_max 5 -probesize 512k -analyzeduration 100000 "
    "-fflags nobuffer+fastseek -flush_packets 1"
)

class YtdlSilentLogger:
    def debug(self, msg):
        pass
    def warning(self, msg):
        pass
    def error(self, msg):
        pass

class UniversalResolver:
    @staticmethod
    def _is_valid_cookie_file(path: Optional[str]) -> bool:
        if not path or not os.path.isfile(path) or os.path.getsize(path) < 10:
            return False
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("#") or not line:
                        continue
                    if len(line.split("\t")) == 7:
                        return True
            return False
        except Exception:
            return False

    def __init__(self):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7"
        }
        
        possible_cookie_paths = [
            "/app/data/cookies.txt",
            os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "cookies.txt"),
            os.path.join(os.getcwd(), "data", "cookies.txt")
        ]
        cookie_path = next((p for p in possible_cookie_paths if self._is_valid_cookie_file(p)), None)
        self.cookie_path = cookie_path

        youtube_meta_extractor_args = {
            "youtube": {
                "player_client": ["android", "ios", "mweb", "web"]
            }
        }
        youtube_stream_extractor_args = {
            "youtube": {
                "player_client": ["android", "ios", "mweb", "web"]
            }
        }

        self.ydl_opts_meta = {
            "format": "bestaudio/ba/best[height<=720]/b",
            "extract_flat": True,
            "skip_download": True,
            "quiet": True,
            "no_warnings": True,
            "logger": YtdlSilentLogger(),
            "default_search": "ytsearch",
            "playlistend": 100,
            "ignoreerrors": True,
            "extractor_retries": 1,
            "socket_timeout": 5,
            "source_address": "0.0.0.0",
            "http_headers": self.headers,
            "extractor_args": youtube_meta_extractor_args,
            "youtube_include_dash_manifest": False,
            "youtube_include_hls_manifest": False
        }
        if cookie_path:
            self.ydl_opts_meta["cookiefile"] = cookie_path

        self.ydl_opts_stream = {
            "format": "bestaudio/ba/best[height<=720]/b",
            "noplaylist": True,
            "skip_download": True,
            "quiet": True,
            "no_warnings": True,
            "logger": YtdlSilentLogger(),
            "extract_flat": False,
            "extractor_retries": 1,
            "socket_timeout": 5,
            "source_address": "0.0.0.0",
            "http_headers": self.headers,
            "extractor_args": youtube_stream_extractor_args,
            "youtube_include_dash_manifest": False,
            "youtube_include_hls_manifest": False
        }
        if cookie_path:
            self.ydl_opts_stream["cookiefile"] = cookie_path

        self._pool_lock = threading.Lock()
        self._meta_pool = []
        self._stream_pool = []

        self.executor = ThreadPoolExecutor(max_workers=min(32, max(8, (os.cpu_count() or 4) * 4)))
        self._meta_cache = OrderedDict()
        self._stream_cache = OrderedDict()
        self._cache_lock = asyncio.Lock()
        self._session: Optional[aiohttp.ClientSession] = None
        self._extract_semaphore = asyncio.Semaphore(min(24, max(8, (os.cpu_count() or 4) * 3)))
        self._flight_lock = asyncio.Lock()
        self._in_flight_searches: Dict[str, asyncio.Future] = {}
        self._in_flight_streams: Dict[str, asyncio.Future] = {}
        self._preload_locks: Dict[str, asyncio.Lock] = {}
        self._sponsorblock_cache = OrderedDict()

        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        possible_env_paths = [
            os.path.join(project_root, ".env"),
            "/app/.env",
            os.path.join(os.getcwd(), ".env")
        ]
        for env_path in possible_env_paths:
            if os.path.isfile(env_path):
                try:
                    with open(env_path, "r", encoding="utf-8", errors="ignore") as f:
                        for line in f:
                            line = line.strip()
                            if line and not line.startswith("#") and "=" in line:
                                k, v = line.split("=", 1)
                                k, v = k.strip(), v.strip().strip("'\"")
                                if not os.environ.get(k):
                                    os.environ[k] = v
                except Exception:
                    pass
                break

        self.kkbox_client_id = os.getenv("KKBOX_CLIENT_ID", "")
        self.kkbox_client_secret = os.getenv("KKBOX_CLIENT_SECRET", "")
        self.kkbox_cookie = os.getenv("KKBOX_COOKIE", "")
        self._kkbox_token = None
        self._kkbox_token_expiry = 0
        self.spotify_client_id = os.getenv("SPOTIFY_CLIENT_ID", "")
        self.spotify_client_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "")
        self._spotify_token = None
        self._spotify_token_expiry = 0
        self.alert_callback = None
        self._cookie_rate_limited_until = 0.0
        self._debug_logger = self._init_debug_logger()

    def _init_debug_logger(self) -> Optional[logging.Logger]:
        logger = logging.getLogger("baoge.resolver")
        if logger.handlers:
            return logger
        logger.setLevel(logging.INFO)
        logger.propagate = False
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        log_paths = [
            os.path.join(project_root, "data", "spotify_debug.log"),
            "/app/data/spotify_debug.log",
            os.path.join(os.getcwd(), "data", "spotify_debug.log")
        ]
        for lp in log_paths:
            try:
                os.makedirs(os.path.dirname(lp), exist_ok=True)
                handler = RotatingFileHandler(lp, maxBytes=1024 * 1024, backupCount=2, encoding="utf-8")
                handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
                logger.addHandler(handler)
                return logger
            except Exception:
                continue
        return None

    def _debug_log(self, msg: str):
        if self._debug_logger:
            try:
                self._debug_logger.info(msg)
            except Exception:
                pass

    async def _parse_html(self, html: str) -> BeautifulSoup:
        return await asyncio.to_thread(BeautifulSoup, html, "html.parser")

    def _acquire_ydl_meta(self) -> yt_dlp.YoutubeDL:
        is_cookie_blocked = time.time() < self._cookie_rate_limited_until
        with self._pool_lock:
            if is_cookie_blocked:
                opts = dict(self.ydl_opts_meta)
                opts.pop("cookiefile", None)
                return yt_dlp.YoutubeDL(opts)
            if self._meta_pool:
                return self._meta_pool.pop()
        return yt_dlp.YoutubeDL(self.ydl_opts_meta)

    def _release_ydl_meta(self, ydl_inst: yt_dlp.YoutubeDL):
        if time.time() < self._cookie_rate_limited_until:
            return
        max_pool = min(24, max(8, (os.cpu_count() or 4) * 2))
        with self._pool_lock:
            if len(self._meta_pool) < max_pool:
                self._meta_pool.append(ydl_inst)

    def _acquire_ydl_stream(self) -> yt_dlp.YoutubeDL:
        is_cookie_blocked = time.time() < self._cookie_rate_limited_until
        with self._pool_lock:
            if is_cookie_blocked:
                opts = dict(self.ydl_opts_stream)
                opts.pop("cookiefile", None)
                return yt_dlp.YoutubeDL(opts)
            if self._stream_pool:
                return self._stream_pool.pop()
        return yt_dlp.YoutubeDL(self.ydl_opts_stream)

    def _release_ydl_stream(self, ydl_inst: yt_dlp.YoutubeDL):
        if time.time() < self._cookie_rate_limited_until:
            return
        max_pool = min(24, max(8, (os.cpu_count() or 4) * 2))
        with self._pool_lock:
            if len(self._stream_pool) < max_pool:
                self._stream_pool.append(ydl_inst)

    async def get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(
                limit=150,
                limit_per_host=30,
                ttl_dns_cache=600,
                keepalive_timeout=60,
                enable_cleanup_closed=True,
                family=socket.AF_INET
            )
            self._session = aiohttp.ClientSession(
                connector=connector,
                headers=self.headers,
                timeout=aiohttp.ClientTimeout(total=8, connect=2.5)
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
            await asyncio.sleep(0.01)
        self.executor.shutdown(wait=False)

    async def graceful_close(self, drain_timeout: float = 30.0):
        t0 = time.time()
        while time.time() - t0 < drain_timeout:
            async with self._flight_lock:
                if not self._in_flight_searches and not self._in_flight_streams:
                    break
            await asyncio.sleep(0.5)
        await self.close()

    async def invalidate_stream_cache(self, target: str):
        async with self._cache_lock:
            self._stream_cache.pop(target, None)

    async def get_search_suggestions(self, current: str) -> List[str]:
        if not current or current.startswith("http"):
            return []
        url = f"https://suggestqueries.google.com/complete/search?client=youtube&ds=yt&q={current}"
        try:
            session = await self.get_session()
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=2)) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    match = re.search(r"window\.google\.ac\.h\((.*)\)", text)
                    if match:
                        data = json.loads(match.group(1))
                        return [item[0] for item in data[1][:5]]
        except Exception:
            pass
        return []

    async def resolve_metadata_batch(self, query: str, preload_stream: bool = True) -> List[Dict[str, Any]]:
        query = query.strip()
        now = time.time()
        async with self._cache_lock:
            if query in self._meta_cache:
                cached_time, cached_val = self._meta_cache[query]
                if now - cached_time < 7200:
                    return [dict(x) for x in cached_val]
                del self._meta_cache[query]

        is_owner = False
        async with self._flight_lock:
            if query in self._in_flight_searches:
                fut = self._in_flight_searches[query]
            else:
                fut = asyncio.get_running_loop().create_future()
                self._in_flight_searches[query] = fut
                is_owner = True

        if not is_owner:
            res = await fut
            return [dict(x) for x in res] if res else []

        try:
            res = []
            if "streetvoice.com" in query:
                res = await self._resolve_streetvoice(query)
            elif "kkbox.com" in query:
                res = await self._resolve_kkbox(query)
            elif "open.spotify.com" in query or "spotify.link" in query or "spotify.app.link" in query:
                res = await self._resolve_spotify(query)
            elif "music.apple.com" in query:
                res = await self._resolve_apple_music(query)
            elif "bilibili.com" in query or "b23.tv" in query:
                res = await self._resolve_bilibili(query)
            else:
                res = await self._resolve_raw_search(query, preload_stream=preload_stream)

            if res:
                async with self._cache_lock:
                    if len(self._meta_cache) > 2000:
                        self._meta_cache.popitem(last=False)
                    self._meta_cache[query] = (now, res)

                if preload_stream and len(res) == 1 and res[0].get("search_query"):
                    asyncio.create_task(
                        self.get_live_stream(
                            res[0]["search_query"],
                            expected_duration=int(res[0].get("expected_duration") or 0)
                        )
                    )

            if not fut.done():
                fut.set_result(res)
            return res
        except Exception:
            if not fut.done():
                fut.set_result([])
            return []
        finally:
            if not fut.done():
                fut.set_result([])
            async with self._flight_lock:
                self._in_flight_searches.pop(query, None)

    async def _resolve_streetvoice(self, url: str) -> List[Dict[str, Any]]:
        clean_url = url.split("?")[0]

        session = await self.get_session()
        async with session.get(clean_url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                return []
            html = await resp.text()

        soup = await self._parse_html(html)

        if "/songs/album/" in clean_url or "/playlists/" in clean_url:
            results = []
            seen_ids = set()
            song_links = soup.find_all("a", href=re.compile(r"/songs/(\d+)"))
            for a in song_links:
                href = a.get("href", "")
                m = re.search(r"/songs/(\d+)", href)
                if not m:
                    continue
                song_id = m.group(1)
                if song_id in seen_ids:
                    continue
                seen_ids.add(song_id)

                title = a.get_text().strip()
                if not title:
                    parent_row = a.find_parent(["tr", "li", "div"])
                    if parent_row:
                        title_el = parent_row.find(class_=re.compile(r"title|name", re.IGNORECASE))
                        if title_el:
                            title = title_el.get_text().strip()

                if not title:
                    title = f"StreetVoice Track {song_id}"

                full_url = f"https://streetvoice.com{href}" if href.startswith("/") else href
                results.append({
                    "title": title,
                    "search_query": full_url,
                    "source_type": "streetvoice",
                    "song_id": song_id,
                    "webpage_url": full_url
                })
                if len(results) >= 100:
                    break
            return results

        song_match = re.search(r"/songs/(\d+)", clean_url)
        if song_match:
            song_id = song_match.group(1)
            og_title = soup.find("meta", property="og:title")
            raw_title = og_title["content"].strip() if (og_title and og_title.get("content")) else "StreetVoice Track"
            cleaned_title = re.sub(r"[\s\-\|]+(StreetVoice|街聲).*$", "", raw_title, flags=re.IGNORECASE).strip()

            return [{
                "title": cleaned_title,
                "search_query": clean_url,
                "source_type": "streetvoice",
                "song_id": song_id,
                "webpage_url": clean_url
            }]

        return []

    async def _get_kkbox_token(self) -> Optional[str]:
        now = time.time()
        if self._kkbox_token and now < (self._kkbox_token_expiry - 60):
            return self._kkbox_token
        if not self.kkbox_client_id or not self.kkbox_client_secret:
            return None
        url = "https://account.kkbox.com/oauth2/token"
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        data = {
            "grant_type": "client_credentials",
            "client_id": self.kkbox_client_id,
            "client_secret": self.kkbox_client_secret
        }
        try:
            session = await self.get_session()
            async with session.post(url, data=data, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    payload = await resp.json()
                    self._kkbox_token = payload.get("access_token")
                    expires_in = int(payload.get("expires_in", 3600))
                    self._kkbox_token_expiry = now + expires_in
                    return self._kkbox_token
        except Exception:
            pass
        return None

    async def _resolve_kkbox_api(self, url: str) -> List[Dict[str, Any]]:
        token = await self._get_kkbox_token()
        if not token:
            return []
        headers = {"Authorization": f"Bearer {token}"}
        clean_url = url.split("?")[0].rstrip("/")
        m_pl = re.search(r"/playlist/([a-zA-Z0-9_\-]+)", clean_url)
        m_album = re.search(r"/album/([a-zA-Z0-9_\-]+)", clean_url)
        m_song = re.search(r"/song/([a-zA-Z0-9_\-]+)", clean_url)
        results = []

        try:
            session = await self.get_session()
            if m_pl:
                p_id = m_pl.group(1)
                api_urls = [
                    f"https://api.kkbox.com/v1.1/shared-playlists/{p_id}/tracks?territory=TW&limit=100",
                    f"https://api.kkbox.com/v1.1/featured-playlists/{p_id}/tracks?territory=TW&limit=100"
                ]
                for a_url in api_urls:
                    async with session.get(a_url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        if resp.status == 200:
                            payload = await resp.json()
                            for item in payload.get("data", []):
                                name = item.get("name", "")
                                artist = item.get("album", {}).get("artist", {}).get("name", "")
                                dur_ms = item.get("duration", 0)
                                exp_dur = int(dur_ms / 1000) if dur_ms > 0 else 0
                                q = f"{name} {artist}".strip() if artist else name
                                if q:
                                    results.append({
                                        "title": q,
                                        "search_query": q,
                                        "webpage_url": clean_url,
                                        "expected_duration": exp_dur,
                                        "track_name": name,
                                        "artist_name": artist
                                    })
                            if results:
                                return results
            elif m_album:
                a_id = m_album.group(1)
                api_url = f"https://api.kkbox.com/v1.1/albums/{a_id}/tracks?territory=TW&limit=100"
                async with session.get(api_url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        payload = await resp.json()
                        for item in payload.get("data", []):
                            name = item.get("name", "")
                            artist = item.get("album", {}).get("artist", {}).get("name", "")
                            dur_ms = item.get("duration", 0)
                            exp_dur = int(dur_ms / 1000) if dur_ms > 0 else 0
                            q = f"{name} {artist}".strip() if artist else name
                            if q:
                                results.append({
                                    "title": q,
                                    "search_query": q,
                                    "webpage_url": clean_url,
                                    "expected_duration": exp_dur,
                                    "track_name": name,
                                    "artist_name": artist
                                })
                        return results
            elif m_song:
                s_id = m_song.group(1)
                api_url = f"https://api.kkbox.com/v1.1/tracks/{s_id}?territory=TW"
                async with session.get(api_url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        item = await resp.json()
                        name = item.get("name", "")
                        artist = item.get("album", {}).get("artist", {}).get("name", "")
                        dur_ms = item.get("duration", 0)
                        exp_dur = int(dur_ms / 1000) if dur_ms > 0 else 0
                        q = f"{name} {artist}".strip() if artist else name
                        if q:
                            return [{
                                "title": q,
                                "search_query": q,
                                "webpage_url": clean_url,
                                "expected_duration": exp_dur,
                                "track_name": name,
                                "artist_name": artist
                            }]
        except Exception:
            pass
        return results

    async def _resolve_kkbox(self, url: str) -> List[Dict[str, Any]]:
        api_results = await self._resolve_kkbox_api(url)
        if api_results:
            return api_results[:100]

        clean_url = url.split("?")[0].rstrip("/")
        results = []
        session = await self.get_session()

        m_pl = re.search(r"/playlist/([a-zA-Z0-9_\-]+)", clean_url)
        m_album = re.search(r"/album/([a-zA-Z0-9_\-]+)", clean_url)
        m_song = re.search(r"/song/([a-zA-Z0-9_\-]+)", clean_url)

        item_id = None
        item_type = None
        if m_pl:
            item_id = m_pl.group(1)
            item_type = "playlist"
        elif m_album:
            item_id = m_album.group(1)
            item_type = "album"
        elif m_song:
            item_id = m_song.group(1)
            item_type = "song"

        if item_id and item_type:
            widget_url = f"https://widget.kkbox.com/v1/?id={item_id}&type={item_type}&terr=TW&lang=tc"
            widget_headers = {
                "User-Agent": self.headers["User-Agent"],
                "Referer": "https://www.kkbox.com/"
            }
            try:
                async with session.get(widget_url, headers=widget_headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status == 200:
                        w_html = await resp.text()
                        m_state = re.search(r'data-state="([^"]+)"', w_html)
                        if m_state:
                            state_data = json.loads(html_lib.unescape(m_state.group(1)))
                            feed = state_data.get("feed", {})
                            feed_data = feed.get("data", {})

                            songlist = feed_data.get("songlist", [])
                            if isinstance(songlist, list) and songlist:
                                for item in songlist:
                                    s_name = (item.get("song_name") or item.get("name") or "").strip()
                                    a_name = (item.get("artist_name") or item.get("artist") or "").strip()
                                    if s_name:
                                        q = f"{s_name} {a_name}".strip() if a_name else s_name
                                        if q and q not in [r["title"] for r in results]:
                                            results.append({"title": q, "search_query": q, "webpage_url": clean_url, "track_name": s_name, "artist_name": a_name})
                                if results:
                                    return results[:100]

                            if "song_name" in feed_data:
                                s_name = feed_data.get("song_name", "").strip()
                                a_name = feed_data.get("artist_name", "").strip()
                                if s_name:
                                    q = f"{s_name} {a_name}".strip() if a_name else s_name
                                    return [{"title": q, "search_query": q, "webpage_url": clean_url, "track_name": s_name, "artist_name": a_name}]

                            token = feed.get("accessToken")
                            if token and item_type == "playlist":
                                api_urls = [
                                    f"https://api.kkbox.com/v1.1/featured-playlists/{item_id}/tracks?territory=TW&limit=100",
                                    f"https://api.kkbox.com/v1.1/charts/{item_id}/tracks?territory=TW&limit=100",
                                    f"https://api.kkbox.com/v1.1/shared-playlists/{item_id}/tracks?territory=TW&limit=100"
                                ]
                                for a_url in api_urls:
                                    try:
                                        async with session.get(a_url, headers={"Authorization": f"Bearer {token}", "User-Agent": self.headers["User-Agent"]}, timeout=aiohttp.ClientTimeout(total=5)) as api_resp:
                                            if api_resp.status == 200:
                                                payload = await api_resp.json()
                                                for it in payload.get("data", []):
                                                    t_name = it.get("name", "").strip()
                                                    t_art = it.get("album", {}).get("artist", {}).get("name", "").strip()
                                                    q = f"{t_name} {t_art}".strip() if t_art else t_name
                                                    if q and q not in [r["title"] for r in results]:
                                                        results.append({"title": q, "search_query": q, "webpage_url": clean_url, "track_name": t_name, "artist_name": t_art})
                                                if results:
                                                    return results[:100]
                                    except Exception:
                                        pass
            except Exception:
                pass

        req_headers = {
            "User-Agent": self.headers["User-Agent"],
            "Accept-Language": self.headers["Accept-Language"],
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://www.kkbox.com/"
        }
        if self.kkbox_cookie:
            req_headers["Cookie"] = self.kkbox_cookie

        html = ""
        try:
            async with session.get(clean_url, headers=req_headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    html = await resp.text()
        except Exception:
            pass

        if not html:
            return []

        soup = await self._parse_html(html)

        for s in soup.find_all("script", type="application/ld+json"):
            if not s.string:
                continue
            try:
                data = json.loads(s.string)
                items = data if isinstance(data, list) else [data]
                for node in items:
                    if node.get("@type") == "MusicPlaylist" and "track" in node:
                        for tr in node["track"]:
                            t_name = tr.get("name", "").strip()
                            art = tr.get("byArtist", {}).get("name", "").strip() if isinstance(tr.get("byArtist"), dict) else ""
                            q = f"{t_name} {art}".strip() if art else t_name
                            if q and q not in [r["title"] for r in results]:
                                results.append({"title": q, "search_query": q, "webpage_url": clean_url, "track_name": t_name, "artist_name": art})
                    elif node.get("@type") == "MusicRecording":
                        t_name = node.get("name", "").strip()
                        art = node.get("byArtist", {}).get("name", "").strip() if isinstance(node.get("byArtist"), dict) else ""
                        q = f"{t_name} {art}".strip() if art else t_name
                        if q and q not in [r["title"] for r in results]:
                            results.append({"title": q, "search_query": q, "webpage_url": clean_url, "track_name": t_name, "artist_name": art})
            except Exception:
                pass

        if results:
            return results[:100]

        script = soup.find("script", id="__NEXT_DATA__")
        if script and script.string:
            try:
                data = json.loads(script.string)
                props = data.get("props", {}).get("pageProps", {})
                init_data = props.get("initData", {}) or props.get("playlist", {}) or props.get("album", {}) or props

                if "song_name" in init_data or ("name" in init_data and "tracks" not in init_data):
                    s_name = init_data.get("name") or init_data.get("song_name") or ""
                    artist = init_data.get("artist_name") or init_data.get("artist", {}).get("name") or ""
                    q = f"{s_name} {artist}".strip() if artist else s_name
                    if q:
                        return [{"title": q, "search_query": q, "webpage_url": clean_url, "track_name": s_name, "artist_name": artist}]

                track_items = []
                for key in ["tracks", "trackList", "songs", "data"]:
                    val = init_data.get(key)
                    if isinstance(val, dict):
                        track_items = val.get("data", []) or val.get("items", [])
                        if track_items:
                            break
                    elif isinstance(val, list):
                        track_items = val
                        break

                if isinstance(track_items, list):
                    for item in track_items:
                        if not isinstance(item, dict):
                            continue
                        s_name = item.get("name") or item.get("song_name") or item.get("title") or ""
                        artist_raw = item.get("artist_name") or item.get("artist") or {}
                        a_name = artist_raw.get("name", "") if isinstance(artist_raw, dict) else str(artist_raw)
                        q = f"{s_name} {a_name}".strip() if a_name else s_name
                        if q and q not in [r["title"] for r in results]:
                            results.append({"title": q, "search_query": q, "webpage_url": clean_url, "track_name": s_name, "artist_name": a_name})
            except Exception:
                pass

        if results:
            return results[:100]

        song_elements = soup.find_all("a", href=re.compile(r"/song/"))
        for el in song_elements:
            s_name = el.get_text().strip()
            if not s_name:
                continue
            parent = el.find_parent(["li", "tr", "div"])
            a_name = ""
            if parent:
                art_el = parent.find("a", href=re.compile(r"/artist/"))
                if art_el:
                    a_name = art_el.get_text().strip()
            q = f"{s_name} {a_name}".strip() if a_name else s_name
            if q and q not in [r["title"] for r in results]:
                results.append({"title": q, "search_query": q, "webpage_url": clean_url, "track_name": s_name, "artist_name": a_name})

        if not results:
            og_title = soup.find("meta", property="og:title")
            if og_title and og_title.get("content"):
                t = og_title["content"].split(" - ")[0].strip()
                if t and "KKBOX" not in t:
                    results.append({"title": t, "search_query": t, "webpage_url": clean_url})

        return results[:100]

    async def _get_spotify_token(self) -> Optional[str]:
        now = time.time()
        if self._spotify_token and now < (self._spotify_token_expiry - 60):
            return self._spotify_token
        if not self.spotify_client_id or not self.spotify_client_secret:
            return None
        url = "https://accounts.spotify.com/api/token"
        auth_bytes = f"{self.spotify_client_id}:{self.spotify_client_secret}".encode("utf-8")
        auth_b64 = base64.b64encode(auth_bytes).decode("ascii")
        headers = {
            "Authorization": f"Basic {auth_b64}",
            "Content-Type": "application/x-www-form-urlencoded"
        }
        data = {"grant_type": "client_credentials"}
        try:
            session = await self.get_session()
            async with session.post(url, data=data, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    payload = await resp.json()
                    self._spotify_token = payload.get("access_token")
                    expires_in = int(payload.get("expires_in", 3600))
                    self._spotify_token_expiry = now + expires_in
                    return self._spotify_token
        except Exception:
            pass
        return None

    async def _resolve_spotify_api(self, item_type: str, item_id: str, canonical_url: str) -> List[Dict[str, Any]]:
        token = await self._get_spotify_token()
        if not token:
            return []
        session = await self.get_session()
        headers = {"Authorization": f"Bearer {token}"}
        try:
            if item_type in ("track", "episode"):
                api_url = f"https://api.spotify.com/v1/{item_type}s/{item_id}"
                async with session.get(api_url, headers=headers, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        title = (data.get("name") or "").strip()
                        artists = [a.get("name", "") for a in data.get("artists", []) if a.get("name")]
                        primary = artists[0] if artists else ""
                        all_art = " ".join(artists)
                        dur_ms = data.get("duration_ms") or 0
                        exp_dur = int(dur_ms / 1000) if dur_ms > 0 else 0
                        if title:
                            disp = f"{title} - {all_art}".strip(" -")
                            sq = f"{title} {primary}".strip() if primary else title
                            return [{
                                "title": disp,
                                "search_query": sq,
                                "webpage_url": canonical_url,
                                "expected_duration": exp_dur,
                                "track_name": title,
                                "artist_name": primary,
                                "isrc": (data.get("external_ids") or {}).get("isrc", "")
                            }]
            elif item_type == "album":
                api_url = f"https://api.spotify.com/v1/albums/{item_id}"
                async with session.get(api_url, headers=headers, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        track_items = data.get("tracks", {}).get("items", [])[:100]
                        res = []
                        for t in track_items:
                            title = (t.get("name") or "").strip()
                            artists = [a.get("name", "") for a in t.get("artists", []) if a.get("name")]
                            primary = artists[0] if artists else ""
                            all_art = " ".join(artists)
                            dur_ms = t.get("duration_ms") or 0
                            exp_dur = int(dur_ms / 1000) if dur_ms > 0 else 0
                            if title:
                                res.append({
                                    "title": f"{title} - {all_art}".strip(" -"),
                                    "search_query": f"{title} {primary}".strip() if primary else title,
                                    "webpage_url": canonical_url,
                                    "expected_duration": exp_dur,
                                    "track_name": title,
                                    "artist_name": primary,
                                    "isrc": (t.get("external_ids") or {}).get("isrc", "")
                                })
                        if res:
                            return res
            elif item_type == "playlist":
                api_url = f"https://api.spotify.com/v1/playlists/{item_id}/tracks?limit=100"
                async with session.get(api_url, headers=headers, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        track_items = data.get("items", [])[:100]
                        res = []
                        for item in track_items:
                            t = item.get("track") or {}
                            if not t or not isinstance(t, dict):
                                continue
                            title = (t.get("name") or "").strip()
                            artists = [a.get("name", "") for a in t.get("artists", []) if a.get("name")]
                            primary = artists[0] if artists else ""
                            all_art = " ".join(artists)
                            dur_ms = t.get("duration_ms") or 0
                            exp_dur = int(dur_ms / 1000) if dur_ms > 0 else 0
                            if title:
                                res.append({
                                    "title": f"{title} - {all_art}".strip(" -"),
                                    "search_query": f"{title} {primary}".strip() if primary else title,
                                    "webpage_url": canonical_url,
                                    "expected_duration": exp_dur,
                                    "track_name": title,
                                    "artist_name": primary,
                                    "isrc": (t.get("external_ids") or {}).get("isrc", "")
                                })
                        if res:
                            return res
            elif item_type == "artist":
                api_url = f"https://api.spotify.com/v1/artists/{item_id}/top-tracks?market=TW"
                async with session.get(api_url, headers=headers, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        track_items = data.get("tracks", [])[:100]
                        res = []
                        for t in track_items:
                            title = (t.get("name") or "").strip()
                            artists = [a.get("name", "") for a in t.get("artists", []) if a.get("name")]
                            primary = artists[0] if artists else ""
                            all_art = " ".join(artists)
                            dur_ms = t.get("duration_ms") or 0
                            exp_dur = int(dur_ms / 1000) if dur_ms > 0 else 0
                            if title:
                                res.append({
                                    "title": f"{title} - {all_art}".strip(" -"),
                                    "search_query": f"{title} {primary}".strip() if primary else title,
                                    "webpage_url": canonical_url,
                                    "expected_duration": exp_dur,
                                    "track_name": title,
                                    "artist_name": primary,
                                    "isrc": (t.get("external_ids") or {}).get("isrc", "")
                                })
                        if res:
                            return res
        except Exception:
            pass
        return []

    async def _resolve_spotify(self, url: str) -> List[Dict[str, Any]]:
        clean_url = url.split("?")[0].strip()
        session = await self.get_session()

        if "spotify.link" in url or "spotify.app.link" in url:
            try:
                async with session.get(url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                    clean_url = str(resp.url).split("?")[0].strip()
            except Exception:
                pass

        match = re.search(
            r"open\.spotify\.com/(?:intl-[a-zA-Z0-9_-]+/)?(?:user/[^/]+/)?(track|album|playlist|artist|episode|show)/([a-zA-Z0-9]+)",
            clean_url
        )
        if not match:
            self._debug_log(f"Spotify regex failed for: {clean_url}")
            return []

        item_type, item_id = match.groups()
        canonical_url = f"https://open.spotify.com/{item_type}/{item_id}"
        embed_url = f"https://open.spotify.com/embed/{item_type}/{item_id}"
        self._debug_log(f"Processing Spotify item_type={item_type}, item_id={item_id}")

        if self.spotify_client_id and self.spotify_client_secret:
            try:
                api_tracks = await self._resolve_spotify_api(item_type, item_id, canonical_url)
                if api_tracks:
                    self._debug_log(f"Spotify API success: {len(api_tracks)} items")
                    return api_tracks
                self._debug_log(f"Spotify API empty/failed for {item_id}, falling back to scraper")
            except Exception as e:
                self._debug_log(f"Spotify API exception for {item_id}: {e}")

        self._debug_log(f"Scraper processing {item_type}/{item_id}")

        spider_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate"
        }

        html = ""
        try:
            async with session.get(embed_url, headers=spider_headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    html = await resp.text()
                else:
                    self._debug_log(f"Embed HTTP status {resp.status} for {embed_url}")
        except Exception as e:
            self._debug_log(f"Async fetch failed for {embed_url}: {e}")

        if not html:
            try:
                def _fetch_sync():
                    req = urllib.request.Request(embed_url, headers=spider_headers)
                    with urllib.request.urlopen(req, timeout=6) as sync_resp:
                        raw = sync_resp.read()
                        if sync_resp.info().get("Content-Encoding") == "gzip":
                            raw = gzip.decompress(raw)
                        return raw.decode("utf-8", errors="ignore")
                html = await asyncio.to_thread(_fetch_sync)
                self._debug_log(f"Sync fallback fetch succeeded, length={len(html)}")
            except Exception as e:
                self._debug_log(f"Sync fallback fetch failed: {e}")

        if html:
            soup = await self._parse_html(html)
            script = soup.find("script", id="__NEXT_DATA__")
            raw_json = (script.string or script.get_text()) if script else ""
            if raw_json:
                try:
                    data = json.loads(raw_json)
                    entity = data.get("props", {}).get("pageProps", {}).get("state", {}).get("data", {}).get("entity", {})
                    if not entity or not isinstance(entity, dict):
                        def _search_entity(node):
                            if isinstance(node, dict):
                                if "trackList" in node or ("name" in node and ("artists" in node or "uri" in node)):
                                    return node
                                for sub in node.values():
                                    found = _search_entity(sub)
                                    if found:
                                        return found
                            return None
                        entity = _search_entity(data) or {}

                    if item_type in ("track", "episode"):
                        raw_title = (entity.get("name") or entity.get("title") or "").strip()
                        raw_title = unicodedata.normalize("NFKC", raw_title.replace("\xa0", " ")).strip()
                        artist_list = [unicodedata.normalize("NFKC", a.get("name", "").replace("\xa0", " ")).strip() for a in entity.get("artists", []) if a.get("name")]
                        subtitle = unicodedata.normalize("NFKC", (entity.get("subtitle") or "").replace("\xa0", " ")).strip()
                        if not artist_list and subtitle:
                            artist_list = [subtitle]
                        primary_artist = artist_list[0] if artist_list else ""
                        all_artists = " ".join(artist_list)
                        dur_ms = entity.get("duration_ms") or entity.get("duration") or 0
                        exp_dur = int(dur_ms / 1000) if dur_ms > 0 else 0
                        if raw_title:
                            disp = f"{raw_title} - {all_artists}".strip(" -")
                            search_q = f"{raw_title} {primary_artist}".strip() if primary_artist else raw_title
                            self._debug_log(f"Scraper extracted track: {disp}")
                            return [{
                                "title": disp,
                                "search_query": search_q,
                                "webpage_url": canonical_url,
                                "expected_duration": exp_dur,
                                "track_name": raw_title,
                                "artist_name": primary_artist
                            }]
                    elif item_type in ("album", "playlist", "artist", "show"):
                        track_list = entity.get("trackList", [])[:100]
                        meta_list = []
                        for t in track_list:
                            raw_t = (t.get("title") or t.get("name") or "").strip()
                            raw_t = unicodedata.normalize("NFKC", raw_t.replace("\xa0", " ")).strip()
                            raw_sub = (t.get("subtitle") or "").strip()
                            raw_sub = unicodedata.normalize("NFKC", raw_sub.replace("\xa0", " ")).strip()
                            primary_sub = raw_sub.split(",")[0].strip() if raw_sub else ""
                            dur_ms = t.get("duration") or t.get("duration_ms") or 0
                            exp_dur = int(dur_ms / 1000) if dur_ms > 0 else 0
                            if raw_t:
                                disp = f"{raw_t} - {raw_sub}".strip(" -")
                                search_q = f"{raw_t} {primary_sub}".strip() if primary_sub else raw_t
                                meta_list.append({
                                    "title": disp,
                                    "search_query": search_q,
                                    "webpage_url": canonical_url,
                                    "expected_duration": exp_dur,
                                    "track_name": raw_t,
                                    "artist_name": primary_sub
                                })
                        if meta_list:
                            self._debug_log(f"Scraper extracted {len(meta_list)} tracks from {item_type}")
                            return meta_list
                except Exception as e:
                    self._debug_log(f"JSON parsing error: {e}")

            if item_type in ("track", "episode"):
                links = soup.find_all("a")
                link_texts = [a.get_text().strip() for a in links if a.get_text().strip()]
                if len(link_texts) >= 2:
                    dom_title = link_texts[0]
                    dom_artist = link_texts[1]
                    disp = f"{dom_title} - {dom_artist}".strip(" -")
                    self._debug_log(f"DOM fallback extracted: {disp}")
                    return [{
                        "title": disp,
                        "search_query": disp,
                        "webpage_url": canonical_url,
                        "expected_duration": 0
                    }]

        try:
            oembed_url = f"https://open.spotify.com/oembed?url={urllib.parse.quote(canonical_url)}"
            async with session.get(oembed_url, timeout=aiohttp.ClientTimeout(total=5)) as oresp:
                if oresp.status == 200:
                    odata = await oresp.json()
                    otitle = (odata.get("title") or "").strip()
                    if otitle:
                        self._debug_log(f"oEmbed fallback extracted: {otitle}")
                        return [{
                            "title": otitle,
                            "search_query": otitle,
                            "webpage_url": canonical_url,
                            "expected_duration": 0
                        }]
        except Exception as e:
            self._debug_log(f"oEmbed fallback failed: {e}")

        if html:
            soup = await self._parse_html(html)
            og_title = soup.find("meta", property="og:title")
            og_desc = soup.find("meta", property="og:description")
            if og_title and og_title.get("content"):
                title = og_title["content"].strip()
                desc = og_desc.get("content", "").strip() if og_desc else ""
                q = f"{title} {desc}".strip() if desc else title
                self._debug_log(f"OpenGraph fallback extracted: {title}")
                return [{
                    "title": title,
                    "search_query": q,
                    "webpage_url": canonical_url,
                    "expected_duration": 0
                }]

        self._debug_log(f"Scraper returned empty for {url}")
        return []

    async def _resolve_apple_music(self, url: str) -> List[Dict[str, Any]]:
        unquoted_url = urllib.parse.unquote(url)
        target_id_m = re.search(r"[?&]i=(\d+)", unquoted_url)
        target_id = target_id_m.group(1) if target_id_m else None

        session = await self.get_session()
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status != 200:
                    return []
                html = await resp.text()
        except Exception:
            return []

        soup = await self._parse_html(html)
        results = []

        for s in soup.find_all("script", type=re.compile(r"application/ld\+json", re.I)):
            raw_text = s.string or s.get_text() or ""
            if not raw_text.strip():
                continue
            try:
                data = json.loads(raw_text.strip())
                items = data if isinstance(data, list) else [data]
                for node in items:
                    ntype = node.get("@type", "")

                    if ntype in ("MusicComposition", "MusicRecording"):
                        s_name = node.get("name", "").strip()
                        art_node = node.get("byArtist") or node.get("audio", {}).get("byArtist") or []
                        art_name = ""
                        if isinstance(art_node, list) and art_node:
                            art_name = art_node[0].get("name", "").strip()
                        elif isinstance(art_node, dict):
                            art_name = art_node.get("name", "").strip()

                        dur_s = self._parse_iso_duration(node.get("duration", ""))
                        if s_name:
                            disp = f"{art_name} - {s_name}" if art_name else s_name
                            q = f"{art_name} {s_name}".strip() if art_name else s_name
                            return [{
                                "title": disp,
                                "search_query": q,
                                "webpage_url": url,
                                "expected_duration": dur_s,
                                "track_name": s_name,
                                "artist_name": art_name
                            }]

                    elif ntype in ("MusicAlbum", "MusicPlaylist"):
                        album_art_node = node.get("byArtist", [])
                        album_artist = ""
                        if isinstance(album_art_node, list) and album_art_node:
                            album_artist = album_art_node[0].get("name", "").strip()
                        elif isinstance(album_art_node, dict):
                            album_artist = album_art_node.get("name", "").strip()

                        tracks = node.get("tracks") or node.get("track") or []
                        matched_single = None
                        album_results = []

                        for tr in tracks:
                            tr_name = tr.get("name", "").strip()
                            tr_url = tr.get("url", "")
                            tr_art_node = tr.get("byArtist", [])
                            tr_art = ""
                            if isinstance(tr_art_node, list) and tr_art_node:
                                tr_art = tr_art_node[0].get("name", "").strip()
                            elif isinstance(tr_art_node, dict):
                                tr_art = tr_art_node.get("name", "").strip()

                            tr_dur = self._parse_iso_duration(tr.get("duration", ""))
                            final_art = tr_art or album_artist
                            disp = f"{final_art} - {tr_name}" if final_art else tr_name
                            q = f"{final_art} {tr_name}".strip() if final_art else tr_name
                            item_obj = {
                                "title": disp,
                                "search_query": q,
                                "webpage_url": tr_url or url,
                                "expected_duration": tr_dur,
                                "track_name": tr_name,
                                "artist_name": final_art
                            }

                            if target_id and (target_id in tr_url or str(tr.get("id", "")) == target_id):
                                matched_single = item_obj
                                break
                            elif tr_name:
                                album_results.append(item_obj)

                        if target_id and matched_single:
                            return [matched_single]
                        if not target_id and album_results:
                            return album_results[:100]

                    elif ntype in ("MusicGroup", "Person"):
                        group_art = node.get("name", "").strip()
                        tracks = node.get("tracks") or node.get("track") or []
                        group_results = []
                        for tr in tracks:
                            tr_name = tr.get("name", "").strip()
                            tr_dur = self._parse_iso_duration(tr.get("duration", ""))
                            if tr_name:
                                disp = f"{group_art} - {tr_name}" if group_art else tr_name
                                q = f"{group_art} {tr_name}".strip() if group_art else tr_name
                                group_results.append({
                                    "title": disp,
                                    "search_query": q,
                                    "webpage_url": url,
                                    "expected_duration": tr_dur,
                                    "track_name": tr_name,
                                    "artist_name": group_art
                                })
                        if not target_id and group_results:
                            return group_results[:100]
            except Exception:
                pass

        server_script = soup.find("script", id="serialized-server-data")
        if server_script:
            raw_server = server_script.string or server_script.get_text() or ""
            if raw_server.strip():
                try:
                    s_data = json.loads(raw_server.strip())
                    sections = s_data.get("data", [{}])[0].get("data", {}).get("sections", [])

                    artist_name = ""
                    for sec in sections:
                        if sec.get("kind") == "artistDetailHeader" or sec.get("itemKind") == "artistDetailHeader":
                            for it in sec.get("items", []):
                                artist_name = (it.get("title") or it.get("name") or "").strip()
                                if artist_name:
                                    break
                        if artist_name:
                            break

                    artist_tracks = []
                    for sec in sections:
                        for it in sec.get("items", []):
                            for tr in it.get("tracks", []):
                                t_title = tr.get("title", "").strip()
                                t_id = str(tr.get("id", ""))
                                if not t_title:
                                    continue

                                t_artist = artist_name
                                sub_links = tr.get("subtitleLinks", [])
                                if not t_artist and sub_links:
                                    t_artist = sub_links[0].get("title", "")

                                disp = f"{t_artist} - {t_title}" if t_artist else t_title
                                q = f"{t_artist} {t_title}".strip() if t_artist else t_title
                                item_obj = {"title": disp, "search_query": q, "webpage_url": url, "track_name": t_title, "artist_name": t_artist}

                                if target_id and target_id == t_id:
                                    return [item_obj]

                                if not target_id and item_obj not in artist_tracks:
                                    artist_tracks.append(item_obj)

                    if artist_tracks:
                        return artist_tracks[:100]
                except Exception:
                    pass

        if target_id:
            apple_title_meta = soup.find("meta", attrs={"name": "apple:title"})
            song_title = apple_title_meta.get("content", "").strip() if apple_title_meta else ""

            artist_name = ""
            artist_link = soup.find("a", href=re.compile(r"/artist/"))
            if artist_link:
                artist_name = artist_link.get_text().strip()

            og_title_meta = soup.find("meta", property="og:title")
            raw_og = og_title_meta.get("content", "").strip() if og_title_meta else ""

            if not song_title and raw_og:
                raw_og = urllib.parse.unquote(raw_og)
                m = re.search(r"^(.*?)(?:在\s*Apple\s*Music\s*上的《(.*?)》|on Apple Music.*$)", raw_og)
                if m:
                    if m.group(2):
                        artist_name = artist_name or m.group(1).strip()
                        song_title = m.group(2).strip()
                    else:
                        song_title = m.group(1).strip()

            if song_title:
                disp = f"{artist_name} - {song_title}" if artist_name else song_title
                q = f"{artist_name} {song_title}".strip() if artist_name else song_title
                return [{"title": disp, "search_query": q, "webpage_url": url}]

        song_metas = soup.find_all("meta", property="music:song")
        if not target_id and song_metas:
            artist_name = ""
            artist_link = soup.find("a", href=re.compile(r"/artist/"))
            if artist_link:
                artist_name = artist_link.get_text().strip()

            album_songs = []
            for sm in song_metas:
                s_url = sm.get("content", "").strip()
                if not s_url:
                    continue
                m = re.search(r"/song/([^/]+)/", s_url)
                slug = m.group(1) if m else ""
                clean_name = slug.replace("-", " ").title() if slug else "Track"
                disp = f"{artist_name} - {clean_name}" if artist_name else clean_name
                q = f"{artist_name} {clean_name}".strip() if artist_name else clean_name
                album_songs.append({"title": disp, "search_query": q, "webpage_url": s_url})
                if len(album_songs) >= 100:
                    break
            if album_songs:
                return album_songs

        og_title_meta = soup.find("meta", property="og:title")
        raw_og = og_title_meta.get("content", "").strip() if og_title_meta else ""
        if raw_og:
            raw_og = urllib.parse.unquote(raw_og)
            m1 = re.search(r"^(.*?)(?:在\s*Apple\s*Music\s*上的《(.*?)》|在\s*Apple\s*Music\s*上的\s*(.*?)$)", raw_og)
            m2 = re.search(r"^《(.*?)》\s*(?:by|—|-)\s*(.*?)(?:\s+on\s+Apple\s+Music|$)", raw_og, re.IGNORECASE)
            if m1:
                art = m1.group(1).strip()
                sng = (m1.group(2) or m1.group(3) or "").strip()
                return [{"title": f"{art} - {sng}" if (art and sng) else (art or sng), "search_query": f"{art} {sng}".strip(), "webpage_url": url}]
            elif m2:
                sng = m2.group(1).strip()
                art = m2.group(2).strip()
                return [{"title": f"{art} - {sng}", "search_query": f"{art} {sng}".strip(), "webpage_url": url}]
            else:
                clean = re.sub(r"(?i)\s*(?:-|—|on)\s*Apple\s*Music.*$", "", raw_og).strip()
                clean = re.sub(r"[\(\[【《『].*?[\)\]】》』]", "", clean).strip()
                return [{"title": clean, "search_query": clean, "webpage_url": url}]

        return []

    async def _resolve_bilibili(self, url: str) -> List[Dict[str, Any]]:
        target_url = url.strip()
        url_match = re.search(r"https?://[^\s]+", target_url)
        if url_match:
            target_url = url_match.group(0)

        if "b23.tv" in target_url:
            session = await self.get_session()
            try:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
                }
                async with session.get(target_url, allow_redirects=True, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        body_sample = await resp.text()
                        if '"code":-404' in body_sample or '"code": -404' in body_sample:
                            return []
                    target_url = str(resp.url)
            except Exception:
                pass

        bv_match = re.search(r"(BV[0-9A-Za-z]{10})", target_url)
        if not bv_match:
            return []

        bvid = bv_match.group(1)
        video_page_url = f"https://www.bilibili.com/video/{bvid}/"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            "Referer": "https://www.bilibili.com/",
            "Accept-Encoding": "gzip, deflate",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        }

        session = await self.get_session()
        html = ""
        try:
            async with session.get(video_page_url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    raw = await resp.read()
                    try:
                        html = gzip.decompress(raw).decode("utf-8", errors="ignore")
                    except Exception:
                        html = raw.decode("utf-8", errors="ignore")
        except Exception:
            pass

        if html:
            m = re.search(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\});", html)
            if m:
                try:
                    state = json.loads(m.group(1))
                    video_data = state.get("videoData", {})
                    results = []

                    ugc_season = video_data.get("ugc_season", {})
                    if ugc_season and "sections" in ugc_season:
                        all_eps = []
                        for sec in ugc_season.get("sections", []):
                            for ep in sec.get("episodes", []):
                                ep_bvid = ep.get("bvid") or bvid
                                ep_title = ep.get("title") or video_data.get("title") or "Bilibili Track"
                                ep_url = f"https://www.bilibili.com/video/{ep_bvid}/"
                                all_eps.append({
                                    "title": ep_title,
                                    "search_query": ep_url,
                                    "id": ep_bvid,
                                    "duration": int(ep.get("arc", {}).get("duration") or 0),
                                    "uploader": video_data.get("owner", {}).get("name", "Bilibili"),
                                    "thumbnail": ep.get("arc", {}).get("pic") or video_data.get("pic") or "",
                                    "webpage_url": ep_url
                                })
                        if all_eps:
                            target_idx = next((i for i, item in enumerate(all_eps) if item["id"] == bvid), 0)
                            ordered_eps = all_eps[target_idx:] + all_eps[:target_idx]
                            return ordered_eps[:100]

                    pages = video_data.get("pages", [])
                    if len(pages) > 1:
                        all_pages = []
                        for p in pages:
                            p_num = p.get("page", 1)
                            p_title = p.get("part") or f"{video_data.get('title')} P{p_num}"
                            p_url = f"https://www.bilibili.com/video/{bvid}/?p={p_num}"
                            all_pages.append({
                                "title": p_title,
                                "search_query": p_url,
                                "id": f"{bvid}_p{p_num}",
                                "duration": int(p.get("duration") or 0),
                                "uploader": video_data.get("owner", {}).get("name", "Bilibili"),
                                "thumbnail": video_data.get("pic") or "",
                                "webpage_url": p_url,
                                "_p_num": p_num
                            })
                        if all_pages:
                            p_match = re.search(r"[?&]p=(\d+)", target_url)
                            target_p = int(p_match.group(1)) if p_match else 1
                            target_idx = next((i for i, item in enumerate(all_pages) if item.get("_p_num") == target_p), 0)
                            ordered_pages = all_pages[target_idx:] + all_pages[:target_idx]
                            for item in ordered_pages:
                                item.pop("_p_num", None)
                            return ordered_pages[:100]

                    single_title = video_data.get("title") or "Bilibili Track"
                    return [{
                        "title": single_title,
                        "search_query": video_page_url,
                        "id": bvid,
                        "duration": int(video_data.get("duration") or 0),
                        "uploader": video_data.get("owner", {}).get("name", "Bilibili"),
                        "thumbnail": video_data.get("pic") or "",
                        "webpage_url": video_page_url
                    }]
                except Exception:
                    pass

            try:
                soup = await self._parse_html(html)
                og_title = soup.find("meta", property="og:title")
                if og_title and og_title.get("content"):
                    t_str = og_title["content"].split("_哔哩哔哩")[0].strip()
                    og_pic = soup.find("meta", property="og:image")
                    pic_url = og_pic.get("content", "") if og_pic else ""
                    author_meta = soup.find("meta", attrs={"name": "author"})
                    u_name = author_meta.get("content", "Bilibili") if author_meta else "Bilibili"
                    return [{
                        "title": t_str,
                        "search_query": video_page_url,
                        "id": bvid,
                        "duration": 0,
                        "uploader": u_name,
                        "thumbnail": pic_url,
                        "webpage_url": video_page_url
                    }]
            except Exception:
                pass

        loop = asyncio.get_running_loop()
        opts = dict(self.ydl_opts_meta)
        opts["http_headers"] = {
            **self.headers,
            "Referer": "https://www.bilibili.com/",
            "Origin": "https://www.bilibili.com"
        }
        try:
            info = await loop.run_in_executor(self.executor, lambda: yt_dlp.YoutubeDL(opts).extract_info(video_page_url, download=False))
            if info:
                return [{
                    "title": info.get("title", "Bilibili Track"),
                    "search_query": video_page_url,
                    "id": bvid,
                    "duration": int(info.get("duration") or 0),
                    "uploader": info.get("uploader", "Bilibili"),
                    "thumbnail": info.get("thumbnail") or "",
                    "webpage_url": video_page_url
                }]
        except Exception:
            pass

        return []

    _TRANS_TABLE = str.maketrans({
        "喜": "喜", "欢": "歡", "酱": "醬", "单": "單", "发": "發",
        "行": "行", "輯": "輯", "辑": "輯", "选": "選", "選": "選",
        "听": "聽", "爱": "愛", "宝": "寶", "国": "國", "语": "語",
        "乐": "樂", "响": "響", "声": "聲", "录": "錄", "风": "風",
        "华": "華", "恋": "戀", "伤": "傷", "梦": "夢", "话": "話",
        "传": "傳", "变": "變", "专": "專", "编": "編", "续": "續",
        "独": "獨", "绝": "絕", "剧": "劇", "门": "門", "间": "間",
        "关": "關", "现": "現", "点": "點", "词": "詞", "蓝": "藍",
        "黄": "黃", "红": "紅", "绿": "綠", "线": "線", "给": "給"
    })

    @staticmethod
    def _parse_iso_duration(dur_str: str) -> int:
        if not dur_str or not isinstance(dur_str, str):
            return 0
        m = re.match(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$", dur_str.strip(), re.I)
        if m:
            hours = int(m.group(1) or 0)
            minutes = int(m.group(2) or 0)
            seconds = int(m.group(3) or 0)
            return hours * 3600 + minutes * 60 + seconds
        return 0

    @staticmethod
    def _kw_hit(kw: str, text: str) -> bool:
        if not kw or not text:
            return False
        if re.search(r"[\u4e00-\u9fff]", kw):
            return kw in text
        return re.search(rf"(?<![a-z0-9]){re.escape(kw)}(?![a-z0-9])", text) is not None

    @classmethod
    def clean_for_search(cls, t: str) -> str:
        if not t:
            return ""
        norm = unicodedata.normalize("NFKC", str(t))
        norm = re.sub(r"\s*[\(\[（【](feat|ft|with)\.?[^\)\]）】]*[\)\]）】]", "", norm, flags=re.I)
        norm = re.sub(r"\s*[\(\[（【](official\s*(music\s*video|mv|audio|video|lyric\s*video|lyrics)?|mv|audio|lyrics?|full\s*song|hq|hd|4k)[^\)\]）】]*[\)\]）】]", "", norm, flags=re.I)
        norm = re.sub(r"\s*-\s*(\d{4}\s*)?(remaster(ed)?|radio edit|single version|mono|stereo|album version).*$", "", norm, flags=re.I)
        norm = re.sub(r"\s+", " ", norm).strip()
        return norm

    @classmethod
    def _normalize_cjk(cls, text: str) -> str:
        if not text:
            return ""
        norm = unicodedata.normalize("NFKC", str(text))
        if zhconv is not None:
            try:
                return zhconv.convert(norm, "zh-hant").lower()
            except Exception:
                pass
        return norm.translate(cls._TRANS_TABLE).lower()

    @classmethod
    def _clean_title_for_comparison(cls, title: str) -> str:
        if not title:
            return ""
        if title.startswith("http://") or title.startswith("https://"):
            return ""
        t = cls._normalize_cjk(title)
        t = re.sub(r"[()\[\]【】《》『』〈〉「」|/—\-_]+", " ", t)
        t = re.sub(r"(?i)\b(official\s*(music\s*video|mv|audio|video|lyric\s*video|lyrics)?|full\s*(ver|version|song)|mv|hd|hq|4k|1080p|audio|lyrics?)\b", " ", t)
        t = re.sub(r"\s+", " ", t).strip()
        return t

    @classmethod
    def _text_match_ratio(cls, needle_clean: str, haystack_clean: str) -> float:
        if not needle_clean or not haystack_clean:
            return 0.0
        if needle_clean in haystack_clean:
            return 1.0
        cjk_chars = re.findall(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", needle_clean)
        latin_all = re.findall(r"[a-z0-9]+", needle_clean)
        latin_words = [w for w in latin_all if len(w) > 1] or latin_all
        total = len(cjk_chars) + len(latin_words)
        if total == 0:
            return 1.0 if needle_clean in haystack_clean else 0.0
        matched = sum(1 for ch in cjk_chars if ch in haystack_clean)
        hay_words = set(re.findall(r"[a-z0-9]+", haystack_clean))
        for w in latin_words:
            if w in hay_words or (len(w) >= 3 and w in haystack_clean):
                matched += 1
        return matched / total

    def _score_music_candidate(
        self,
        title: str,
        uploader: str,
        query: str = "",
        duration: int = 0,
        view_count: int = 0,
        expected_duration: int = 0,
        track_name: str = "",
        artist_name: str = "",
    ) -> int:
        score = 0
        t = self._normalize_cjk(title)
        u = self._normalize_cjk(uploader)
        q = self._normalize_cjk(query).strip() if query else ""
        combined_q = f"{q} {self._normalize_cjk(track_name)} {self._normalize_cjk(artist_name)}".strip()

        clean_q = self._clean_title_for_comparison(q)
        clean_t = self._clean_title_for_comparison(title)

        if clean_q and clean_t and clean_q == clean_t:
            score += 5000

        if track_name:
            clean_track = self._clean_title_for_comparison(track_name) or self._normalize_cjk(track_name)
            clean_artist = self._clean_title_for_comparison(artist_name) or self._normalize_cjk(artist_name)
            title_ratio = self._text_match_ratio(clean_track, clean_t)
            if title_ratio < 0.5:
                return -25000
            score += int(title_ratio * 5500)
            if clean_track and clean_track == clean_t:
                score += 2000
            if clean_artist:
                u_clean = self._clean_title_for_comparison(uploader) or u
                art_in_u = self._text_match_ratio(clean_artist, u_clean)
                art_in_t = self._text_match_ratio(clean_artist, clean_t)
                if art_in_u >= 0.6:
                    score += 3500
                elif art_in_t >= 0.6:
                    score += 2500
                else:
                    score -= 6000
        else:
            q_parts = [p.strip() for p in re.split(r"\s*[-—/|]\s*", q) if p.strip()]

            if len(q_parts) >= 2:
                p0 = self._clean_title_for_comparison(q_parts[0])
                p1 = self._clean_title_for_comparison(q_parts[1])

                def _valid_token(tok: str) -> bool:
                    return len(tok) > 1 or bool(re.search(r"[\u4e00-\u9fff]", tok))

                p0_in_t = any(w in clean_t for w in p0.split() if _valid_token(w)) or self._text_match_ratio(p0, clean_t) >= 0.6
                p1_in_t = any(w in clean_t for w in p1.split() if _valid_token(w)) or self._text_match_ratio(p1, clean_t) >= 0.6
                p0_in_u = any(w in u for w in p0.split() if _valid_token(w)) or self._text_match_ratio(p0, u) >= 0.6
                p1_in_u = any(w in u for w in p1.split() if _valid_token(w)) or self._text_match_ratio(p1, u) >= 0.6

                if p0_in_t and (p1_in_t or p1_in_u):
                    score += 4000
                    if p1_in_u:
                        score += 1500
                elif p1_in_t and (p0_in_t or p0_in_u):
                    score += 4000
                    if p0_in_u:
                        score += 1500
                elif p0_in_t or p1_in_t:
                    score += 1500
                else:
                    return -20000
            else:
                ratio = self._text_match_ratio(clean_q, f"{clean_t} {u}".strip())
                if clean_q and ratio < 0.35:
                    return -15000
                score += int(ratio * 3000)

        if " - topic" in u or u.endswith(" topic"):
            score += 4000
        elif "vevo" in u:
            score += 3200
        elif any(k in u for k in ["official", "records", "music", "entertainment", "channel", "sony", "universal", "warner", "avex", "king records", "lantis", "jyp", "hybe", "smtown", "yg"]):
            score += 2000

        if any(k in t for k in ["official audio", "official track", "audio"]):
            score += 3000
        elif any(k in t for k in ["official lyric", "official lyrics", "lyric video", "lyrics video"]):
            score += 2400
        elif any(k in t for k in ["official music video", "official mv", "music video"]):
            score += 2200
        elif any(k in t for k in ["studio version", "album version", "original version"]):
            score += 1800

        if view_count >= 1_000_000:
            score += 2500
        elif view_count >= 500_000:
            score += 1800
        elif view_count >= 100_000:
            score += 1200
        elif view_count >= 10_000:
            score += 400

        remix_words = [
            "remix", "mix", "8d", "slowed", "reverb", "speed up", "sped up",
            "nightcore", "mashup", "dj", "bass boosted", "trap", "lofi", "lo-fi", "edit"
        ]
        for rk in remix_words:
            if self._kw_hit(rk, t) and not self._kw_hit(rk, combined_q):
                score -= 50000

        cover_keywords = [
            "cover", "guitar cover", "piano cover", "drum cover", "dance cover",
            "acoustic cover", "karaoke", "flip"
        ]
        for ck in cover_keywords:
            if self._kw_hit(ck, t) and not self._kw_hit(ck, combined_q):
                score -= 50000

        spam_keywords = [
            "reaction", "instrumental", "bgm", "podcast", "review",
            "\u7d55\u7f8e\u7684\u756b\u9762", "\u597d\u807d\u7684\u65cb\u5f8b", "\u7d55\u7f8e",
            "\u4e2d\u5b57", "\u7e41\u4e2d", "\u52d5\u614b\u6b4c\u8a5e", "\u526a\u8f2f",
            "\u89e3\u8aaa", "\u96fb\u5f71\u7247\u6bb5", "\u96fb\u8996\u5287", "\u53cd\u61c9",
            "\u7d14\u4eab", "\u7d14\u97f3\u6a02", "\u4f34\u594f", "\u5408\u96c6",
            "\u76e4\u9ede", "\u7cbe\u9078", "\u7ffb\u5531", "\u6539\u7de8",
            "\u65e5\u63a8", "\u6b4c\u55ae", "\u6b4c\u5355", "\u7cbe\u9009", "\u5408\u8f91",
            "\u65e0\u635f", "\u79c1\u85cf", "\u5408\u96c6", "lawpj", "1 hour", "10 hours", "loop",
            "compilation", "greatest hits", "full album", "album mix", "\u4e32\u71d2", "\u4e32\u70e7"
        ]
        for sk in spam_keywords:
            norm_sk = self._normalize_cjk(sk)
            if self._kw_hit(norm_sk, t) and not self._kw_hit(norm_sk, combined_q):
                score -= 60000

        live_keywords = ["live", "concert", "fancam"]
        for lk in live_keywords:
            if self._kw_hit(lk, t) and not self._kw_hit(lk, combined_q):
                score -= 2500

        if expected_duration > 0 and duration > 0:
            d = abs(duration - expected_duration)
            if d <= 3:
                score += 5000
            elif d <= 8:
                score += 2500
            elif d > 60:
                score -= 20000
            elif d > 25:
                score -= 8000
            elif d > 15:
                score -= 3000
        elif 90 <= duration <= 480:
            score += 200
        elif 481 <= duration <= 600:
            score += 50
        elif duration > 720 or (0 < duration < 45):
            score -= 5000

        return score

    async def _resolve_raw_search(self, query: str, expected_duration: int = 0, preload_stream: bool = True) -> List[Dict[str, Any]]:
        loop = asyncio.get_running_loop()
        is_url = query.startswith("http://") or query.startswith("https://")
        is_bili = "bilibili.com" in query or "b23.tv" in query
        opts = dict(self.ydl_opts_meta)

        if is_bili:
            opts["http_headers"] = {
                **self.headers,
                "Referer": "https://www.bilibili.com/",
                "Origin": "https://www.bilibili.com"
            }

        def _extract(target_query):
            if is_bili:
                try:
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        return ydl.extract_info(target_query, download=False)
                except Exception:
                    return None
            ydl_inst = self._acquire_ydl_meta()
            try:
                return ydl_inst.extract_info(target_query, download=False)
            except Exception as ex:
                err_msg = str(ex).lower()
                if any(k in err_msg for k in ["confirm you're not a bot", "sign in to confirm", "rate-limit", "too many requests", "429"]):
                    self._cookie_rate_limited_until = time.time() + 1800
                try:
                    retry_opts = dict(self.ydl_opts_meta)
                    retry_opts.pop("cookiefile", None)
                    retry_opts["source_address"] = "0.0.0.0"
                    retry_opts["extractor_args"] = {"youtube": {"player_client": ["android", "ios", "mweb", "web"]}}
                    with yt_dlp.YoutubeDL(retry_opts) as ydl_retry:
                        return ydl_retry.extract_info(target_query, download=False)
                except Exception:
                    pass
                if "confirm your age" in err_msg or "sign in" in err_msg or "format is not available" in err_msg or "requested format" in err_msg:
                    try:
                        retry_opts = dict(opts)
                        retry_opts.pop("cookiefile", None)
                        retry_opts["source_address"] = "0.0.0.0"
                        retry_opts["format"] = "best/ba/b"
                        retry_opts["extractor_args"] = {"youtube": {"player_client": ["tv_embedded", "tv", "android"]}}
                        with yt_dlp.YoutubeDL(retry_opts) as ydl_retry:
                            return ydl_retry.extract_info(target_query, download=False)
                    except Exception:
                        return None
                return None
            finally:
                self._release_ydl_meta(ydl_inst)

        async def _safe_extract(target_query: str):
            async with self._extract_semaphore:
                try:
                    return await loop.run_in_executor(self.executor, lambda: _extract(target_query))
                except Exception:
                    return None

        is_yt_mix = is_url and bool(re.search(r"[?&]list=(?:RD|UL)[0-9A-Za-z_-]+", query))
        if is_yt_mix:
            opts["playlistend"] = 25

        try:
            if is_url:
                if preload_stream and not re.search(r"[?&]list=", query):
                    try:
                        asyncio.create_task(self.get_live_stream(query))
                    except Exception:
                        pass
                info = await _safe_extract(query)
                if not info or not info.get("entries"):
                    v_match = re.search(r"(?:v=|youtu\.be/)([0-9A-Za-z_-]{11})", query)
                    if v_match:
                        single_url = f"https://www.youtube.com/watch?v={v_match.group(1)}"
                        info = await _safe_extract(single_url)
            else:
                opts["playlistend"] = 8
                search_q = self.clean_for_search(query) or query
                search_target = f"ytsearch8:{search_q}"
                info = await _safe_extract(search_target)

            if not info:
                if is_url and ("youtube.com" in query or "youtu.be" in query):
                    fallback_title = await self._fetch_title_from_oembed(query)
                    if fallback_title:
                        v_match = re.search(r"(?:v=|youtu\.be/)([0-9A-Za-z_-]{11})", query)
                        vid_id = v_match.group(1) if v_match else None
                        return [{
                            "title": fallback_title,
                            "search_query": query,
                            "id": vid_id,
                            "duration": 0,
                            "uploader": "YouTube",
                            "thumbnail": f"https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg" if vid_id else "",
                            "webpage_url": query
                        }]
                return []

            entries = [e for e in info.get("entries", [info]) if e]
            if not entries:
                return []

            if not is_url and len(entries) > 1:
                ranked = sorted(
                    entries,
                    key=lambda x: self._score_music_candidate(
                        x.get("title", ""),
                        x.get("uploader", ""),
                        query=query,
                        duration=int(x.get("duration") or 0),
                        view_count=int(x.get("view_count") or 0),
                        expected_duration=expected_duration
                    ),
                    reverse=True
                )
                if ranked and self._score_music_candidate(
                    ranked[0].get("title", ""),
                    ranked[0].get("uploader", ""),
                    query=query,
                    duration=int(ranked[0].get("duration") or 0),
                    view_count=int(ranked[0].get("view_count") or 0),
                    expected_duration=expected_duration
                ) < 0:
                    search_q = self.clean_for_search(query) or query
                    retry_targets = [
                        f"ytsearch8:{search_q} official audio",
                        f"ytsearch8:{search_q} topic"
                    ]
                    for r_tgt in retry_targets:
                        retry_info = await _safe_extract(r_tgt)
                        if retry_info and retry_info.get("entries"):
                            retry_entries = [e for e in retry_info["entries"] if e]
                            if retry_entries:
                                ranked_retry = sorted(
                                    retry_entries,
                                    key=lambda x: self._score_music_candidate(
                                        x.get("title", ""),
                                        x.get("uploader", ""),
                                        query=query,
                                        duration=int(x.get("duration") or 0),
                                        view_count=int(x.get("view_count") or 0),
                                        expected_duration=expected_duration
                                    ),
                                    reverse=True
                                )
                                if ranked_retry and self._score_music_candidate(
                                    ranked_retry[0].get("title", ""),
                                    ranked_retry[0].get("uploader", ""),
                                    query=query,
                                    duration=int(ranked_retry[0].get("duration") or 0),
                                    view_count=int(ranked_retry[0].get("view_count") or 0),
                                    expected_duration=expected_duration
                                ) > self._score_music_candidate(
                                    ranked[0].get("title", ""),
                                    ranked[0].get("uploader", ""),
                                    query=query,
                                    duration=int(ranked[0].get("duration") or 0),
                                    view_count=int(ranked[0].get("view_count") or 0),
                                    expected_duration=expected_duration
                                ):
                                    ranked = ranked_retry
                                    break
                chosen = ranked[0]
                vid_id = chosen.get("id", "")
                final_target = f"https://www.youtube.com/watch?v={vid_id}" if re.match(r"^[0-9A-Za-z_-]{11}$", str(vid_id)) else (chosen.get("webpage_url") or chosen.get("url") or query)
                if preload_stream:
                    try:
                        asyncio.create_task(self.get_live_stream(final_target))
                    except Exception:
                        pass
                return [{
                    "title": chosen.get("title", query),
                    "search_query": final_target,
                    "id": vid_id if re.match(r"^[0-9A-Za-z_-]{11}$", str(vid_id)) else None,
                    "duration": int(chosen.get("duration") or 0),
                    "uploader": chosen.get("uploader", "Unknown"),
                    "thumbnail": chosen.get("thumbnail") or "",
                    "webpage_url": final_target
                }]

            if is_url and len(entries) > 1:
                target_idx = None
                v_m = re.search(r"[?&]v=([0-9A-Za-z_-]{11})", query)
                if v_m:
                    target_vid = v_m.group(1)
                    target_idx = next((i for i, item in enumerate(entries) if item.get("id") == target_vid), None)
                if target_idx is None:
                    idx_m = re.search(r"[?&]index=(\d+)", query)
                    if idx_m:
                        val = int(idx_m.group(1)) - 1
                        if 0 <= val < len(entries):
                            target_idx = val
                if target_idx is None:
                    p_m = re.search(r"[?&]p=(\d+)", query)
                    if p_m:
                        val = int(p_m.group(1)) - 1
                        if 0 <= val < len(entries):
                            target_idx = val
                if target_idx is not None and target_idx > 0:
                    entries = entries[target_idx:] + entries[:target_idx]

            results = []
            for item in entries[:100]:
                vid_id = item.get("id", "")
                web_url = item.get("webpage_url") or item.get("url") or query

                if is_bili:
                    target = web_url if web_url.startswith("http") else f"https://www.bilibili.com/video/{vid_id}"
                elif vid_id and re.match(r"^[0-9A-Za-z_-]{11}$", str(vid_id)):
                    target = f"https://www.youtube.com/watch?v={vid_id}"
                else:
                    target = web_url

                results.append({
                    "title": item.get("title", "Unknown"),
                    "search_query": target,
                    "id": vid_id if re.match(r"^[0-9A-Za-z_-]{11}$", str(vid_id)) else None,
                    "duration": int(item.get("duration") or 0),
                    "uploader": item.get("uploader", "Unknown"),
                    "thumbnail": item.get("thumbnail") or "",
                    "webpage_url": target
                })
            return results
        except Exception as e:
            err_str = str(e)
            if "confirm you're not a bot" in err_str.lower() or "sign in" in err_str.lower():
                print("[WARNING] YouTube bot detection triggered. Cookies may be missing or expired in /app/data/cookies.txt!")
                if self.alert_callback:
                    try:
                        self.alert_callback("YouTube bot detection triggered (Sign in to confirm you're not a bot). Cookies may be missing or expired in `/app/data/cookies.txt`.")
                    except Exception:
                        pass
            return []

    async def _fetch_sponsorblock_offset(self, video_id: str) -> float:
        if not video_id or not re.match(r"^[0-9A-Za-z_-]{11}$", str(video_id)):
            return 0.0
        if video_id in self._sponsorblock_cache:
            return self._sponsorblock_cache[video_id]

        url = f"https://sponsor.ajay.app/api/skipSegments?videoID={video_id}&categories=[\"music_offtopic\",\"intro\",\"preview\",\"filler\"]"
        offset = 0.0
        try:
            session = await self.get_session()
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=0.8)) as resp:
                if resp.status == 200:
                    segments = await resp.json()
                    sorted_segs = sorted(
                        [s.get("segment", []) for s in segments if len(s.get("segment", [])) == 2],
                        key=lambda x: x[0]
                    )
                    curr_pos = 0.0
                    for start, end in sorted_segs:
                        if start <= (curr_pos + 6.0) and end > curr_pos:
                            curr_pos = float(end)
                    if curr_pos >= 3.0:
                        offset = curr_pos
        except Exception:
            pass

        if len(self._sponsorblock_cache) > 2000:
            self._sponsorblock_cache.popitem(last=False)
        self._sponsorblock_cache[video_id] = offset
        return offset

    async def get_youtube_direct_stream(self, target_url: str) -> Optional[Dict[str, Any]]:
        clean_target = (target_url or "").strip()
        if not clean_target:
            return None
        v_match = re.search(r"(?:v=|youtu\.be/)([0-9A-Za-z_-]{11})", clean_target)
        vid_id = v_match.group(1) if v_match else None
        if vid_id:
            clean_target = f"https://www.youtube.com/watch?v={vid_id}"

        cache_key = f"direct_yt:{clean_target}"
        now = time.time()
        async with self._cache_lock:
            if cache_key in self._stream_cache:
                cached_time, cached_val = self._stream_cache[cache_key]
                if now - cached_time < 3600:
                    return dict(cached_val)
                del self._stream_cache[cache_key]

        is_owner = False
        async with self._flight_lock:
            if cache_key in self._in_flight_streams:
                fut = self._in_flight_streams[cache_key]
            else:
                fut = asyncio.get_running_loop().create_future()
                self._in_flight_streams[cache_key] = fut
                is_owner = True

        if not is_owner:
            res = await fut
            return dict(res) if res else None

        try:
            loop = asyncio.get_running_loop()

            def _extract_direct() -> Optional[Dict[str, Any]]:
                strategies = [
                    {
                        "format": "bestaudio[acodec=opus]/bestaudio/best",
                        "noplaylist": True,
                        "skip_download": True,
                        "quiet": True,
                        "no_warnings": True,
                        "logger": YtdlSilentLogger(),
                        "socket_timeout": 6,
                        "source_address": "0.0.0.0",
                    },
                    {
                        "format": "bestaudio/best",
                        "noplaylist": True,
                        "skip_download": True,
                        "quiet": True,
                        "no_warnings": True,
                        "logger": YtdlSilentLogger(),
                        "socket_timeout": 5,
                        "source_address": "0.0.0.0",
                        "extractor_args": {
                            "youtube": {
                                "player_client": ["android"],
                                "player_skip": ["webpage", "configs", "js"],
                            }
                        },
                    },
                ]
                if self.cookie_path and now >= getattr(self, "_cookie_rate_limited_until", 0.0):
                    strategies.append({
                        "format": "bestaudio[acodec=opus]/bestaudio/best",
                        "noplaylist": True,
                        "skip_download": True,
                        "quiet": True,
                        "no_warnings": True,
                        "logger": YtdlSilentLogger(),
                        "socket_timeout": 6,
                        "source_address": "0.0.0.0",
                        "cookiefile": self.cookie_path,
                    })

                for opts in strategies:
                    try:
                        with yt_dlp.YoutubeDL(opts) as ydl:
                            info = ydl.extract_info(clean_target, download=False)
                        if info and "entries" in info and info["entries"]:
                            entries = [e for e in info["entries"] if e]
                            info = entries[0] if entries else None
                        if info and info.get("url"):
                            return info
                    except Exception:
                        continue
                return None

            async with self._extract_semaphore:
                info = await loop.run_in_executor(self.executor, _extract_direct)

            if not info or not info.get("url"):
                if not fut.done():
                    fut.set_result(None)
                return None

            res_vid = info.get("id") or vid_id
            res_dict = {
                "id": res_vid,
                "title": info.get("title") or "Unknown",
                "uploader": info.get("uploader") or info.get("channel") or "Unknown",
                "duration": int(info.get("duration") or 0),
                "thumbnail": info.get("thumbnail") or (f"https://i.ytimg.com/vi/{res_vid}/hqdefault.jpg" if res_vid else ""),
                "webpage_url": info.get("webpage_url") or clean_target,
                "stream_url": info["url"],
                "is_live": bool(info.get("is_live", False)),
                "start_offset": 0.0,
                "http_headers": {},
            }
            async with self._cache_lock:
                if len(self._stream_cache) > 2000:
                    self._stream_cache.popitem(last=False)
                self._stream_cache[cache_key] = (time.time(), res_dict)
            if not fut.done():
                fut.set_result(res_dict)
            return dict(res_dict)
        except Exception:
            if not fut.done():
                fut.set_result(None)
            return None
        finally:
            if not fut.done():
                fut.set_result(None)
            async with self._flight_lock:
                self._in_flight_streams.pop(cache_key, None)

    async def get_live_stream(self, target: str, expected_duration: int = 0) -> Optional[Dict[str, Any]]:
        now = time.time()
        async with self._cache_lock:
            if target in self._stream_cache:
                cached_time, cached_val = self._stream_cache[target]
                if now - cached_time < 3600:
                    return dict(cached_val)
                del self._stream_cache[target]

        is_owner = False
        async with self._flight_lock:
            if target in self._in_flight_streams:
                fut = self._in_flight_streams[target]
            else:
                fut = asyncio.get_running_loop().create_future()
                self._in_flight_streams[target] = fut
                is_owner = True

        if not is_owner:
            res = await fut
            return dict(res) if res else None

        try:
            reconnect_flags = (
                "-loglevel fatal -nostats -reconnect 1 -reconnect_streamed 1 "
                "-reconnect_on_network_error 1 -reconnect_on_http_error 4xx,5xx "
                "-reconnect_at_eof 1 -reconnect_delay_max 5 -probesize 512k -analyzeduration 100000"
            )

            if "streetvoice.com" in target:
                song_match = re.search(r"/songs/(\d+)", target)
                if song_match:
                    song_id = song_match.group(1)
                    api_url = f"https://streetvoice.com/api/v3/songs/{song_id}/hls/"
                    sv_headers = {
                        **self.headers,
                        "Referer": target,
                        "Origin": "https://streetvoice.com"
                    }
                    try:
                        session = await self.get_session()
                        async with session.post(api_url, headers=sv_headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                                if resp.status == 200:
                                    data = await resp.json()
                                    file_url = data.get("file")
                                    if file_url:
                                        user_agent = self.headers["User-Agent"]
                                        before_opts = (
                                            f'-headers "User-Agent: {user_agent}\r\nReferer: {target}\r\nOrigin: https://streetvoice.com\r\n" '
                                            f'{reconnect_flags}'
                                        )
                                        res_dict = {
                                            "id": song_id,
                                            "title": "StreetVoice Track",
                                            "uploader": "StreetVoice",
                                            "duration": 0,
                                            "thumbnail": "",
                                            "webpage_url": target,
                                            "stream_url": file_url,
                                            "is_live": False,
                                            "before_options": before_opts,
                                            "start_offset": 0.0,
                                            "http_headers": sv_headers
                                        }
                                        async with self._cache_lock:
                                            if len(self._stream_cache) > 2000:
                                                self._stream_cache.popitem(last=False)
                                            self._stream_cache[target] = (now, res_dict)
                                        if not fut.done():
                                            fut.set_result(res_dict)
                                        return res_dict
                    except Exception:
                        pass

            loop = asyncio.get_running_loop()
            clean_target = target
            if "youtube.com" in target or "youtu.be" in target:
                match = re.search(r"(?:v=|\/)([0-9A-Za-z_-]{11})", target)
                if match:
                    clean_target = f"https://www.youtube.com/watch?v={match.group(1)}"

            is_bili = "bilibili.com" in clean_target or "b23.tv" in clean_target
            opts = dict(self.ydl_opts_stream)
            if is_bili:
                opts["http_headers"] = {
                    **self.headers,
                    "Referer": "https://www.bilibili.com/",
                    "Origin": "https://www.bilibili.com"
                }

            search_q = self.clean_for_search(clean_target) or clean_target
            search_target = f"ytsearch2:{search_q}"

            def _extract():
                if is_bili:
                    try:
                        with yt_dlp.YoutubeDL(opts) as ydl:
                            if clean_target.startswith("http"):
                                return ydl.extract_info(clean_target, download=False)
                            return ydl.extract_info(search_target, download=False)
                    except Exception:
                        return None

                ydl_inst = self._acquire_ydl_stream()
                try:
                    if clean_target.startswith("http"):
                        return ydl_inst.extract_info(clean_target, download=False)
                    return ydl_inst.extract_info(search_target, download=False)
                except Exception as ex:
                    err_msg = str(ex).lower()
                    if any(k in err_msg for k in ["confirm you're not a bot", "sign in to confirm", "rate-limit", "too many requests", "429"]):
                        self._cookie_rate_limited_until = time.time() + 1800

                    try:
                        retry_opts = dict(self.ydl_opts_stream)
                        retry_opts.pop("cookiefile", None)
                        retry_opts["source_address"] = "0.0.0.0"
                        retry_opts["format"] = "best/ba/b"
                        retry_opts["extractor_args"] = {"youtube": {"player_client": ["android", "ios", "mweb", "web"]}}
                        with yt_dlp.YoutubeDL(retry_opts) as ydl_retry:
                            if clean_target.startswith("http"):
                                return ydl_retry.extract_info(clean_target, download=False)
                            return ydl_retry.extract_info(search_target, download=False)
                    except Exception:
                        pass

                    if "confirm your age" in err_msg or "sign in" in err_msg:
                        try:
                            retry_opts = dict(opts)
                            retry_opts.pop("cookiefile", None)
                            retry_opts["source_address"] = "0.0.0.0"
                            retry_opts["format"] = "best/ba/b"
                            retry_opts["extractor_args"] = {"youtube": {"player_client": ["tv_embedded", "tv", "android"]}}
                            with yt_dlp.YoutubeDL(retry_opts) as ydl_retry:
                                if clean_target.startswith("http"):
                                    return ydl_retry.extract_info(clean_target, download=False)
                                return ydl_retry.extract_info(search_target, download=False)
                        except Exception:
                            return None
                    elif "format is not available" in err_msg or "requested format" in err_msg:
                        try:
                            retry_opts = dict(opts)
                            retry_opts.pop("cookiefile", None)
                            retry_opts["source_address"] = "0.0.0.0"
                            retry_opts["format"] = "best/ba/b"
                            retry_opts["extractor_args"] = {"youtube": {"player_client": ["android", "ios", "mweb", "web"]}}
                            with yt_dlp.YoutubeDL(retry_opts) as ydl_retry:
                                if clean_target.startswith("http"):
                                    return ydl_retry.extract_info(clean_target, download=False)
                                return ydl_retry.extract_info(search_target, download=False)
                        except Exception:
                            return None
                    return None
                finally:
                    self._release_ydl_stream(ydl_inst)

            info = None
            async with self._extract_semaphore:
                try:
                    info = await loop.run_in_executor(self.executor, _extract)
                except Exception:
                    info = None

            if not info:
                if not fut.done():
                    fut.set_result(None)
                return None
            if "entries" in info and info["entries"]:
                valid_entries = [e for e in info["entries"] if e]
                if len(valid_entries) > 1 and not clean_target.startswith("http"):
                    ranked = sorted(
                        valid_entries,
                        key=lambda x: self._score_music_candidate(
                            x.get("title", ""),
                            x.get("uploader", ""),
                            query=clean_target,
                            duration=int(x.get("duration") or 0),
                            view_count=int(x.get("view_count") or 0),
                            expected_duration=expected_duration
                        ),
                        reverse=True
                    )
                    if ranked and self._score_music_candidate(
                        ranked[0].get("title", ""),
                        ranked[0].get("uploader", ""),
                        query=clean_target,
                        duration=int(ranked[0].get("duration") or 0),
                        view_count=int(ranked[0].get("view_count") or 0),
                        expected_duration=expected_duration
                    ) < 0:
                        retry_audio_q = f"ytsearch8:{search_q} official audio"
                        def _extract_audio_retry():
                            ydl_inst = self._acquire_ydl_stream()
                            try:
                                return ydl_inst.extract_info(retry_audio_q, download=False)
                            except Exception:
                                return None
                            finally:
                                self._release_ydl_stream(ydl_inst)
                        try:
                            retry_info = await loop.run_in_executor(self.executor, _extract_audio_retry)
                            if retry_info and retry_info.get("entries"):
                                r_entries = [e for e in retry_info["entries"] if e]
                                if r_entries:
                                    ranked_retry = sorted(
                                        r_entries,
                                        key=lambda x: self._score_music_candidate(
                                            x.get("title", ""),
                                            x.get("uploader", ""),
                                            query=clean_target,
                                            duration=int(x.get("duration") or 0),
                                            view_count=int(x.get("view_count") or 0),
                                            expected_duration=expected_duration
                                        ),
                                        reverse=True
                                    )
                                    if ranked_retry and self._score_music_candidate(
                                        ranked_retry[0].get("title", ""),
                                        ranked_retry[0].get("uploader", ""),
                                        query=clean_target,
                                        duration=int(ranked_retry[0].get("duration") or 0),
                                        view_count=int(ranked_retry[0].get("view_count") or 0),
                                        expected_duration=expected_duration
                                    ) > self._score_music_candidate(
                                        ranked[0].get("title", ""),
                                        ranked[0].get("uploader", ""),
                                        query=clean_target,
                                        duration=int(ranked[0].get("duration") or 0),
                                        view_count=int(ranked[0].get("view_count") or 0),
                                        expected_duration=expected_duration
                                    ):
                                        ranked = ranked_retry
                        except Exception:
                            pass
                    info = ranked[0]
                elif valid_entries:
                    info = valid_entries[0]
                else:
                    info = None

            if not info:
                if not fut.done():
                    fut.set_result(None)
                return None

            stream_url = info.get("url")
            chosen_format = None
            if not stream_url:
                formats = info.get("formats", [])
                audio_formats = [f for f in formats if f.get("acodec") != "none"]
                if audio_formats:
                    chosen_format = audio_formats[-1]
                    stream_url = chosen_format.get("url")

            if not stream_url:
                try:
                    retry_opts = dict(self.ydl_opts_stream)
                    retry_opts.pop("cookiefile", None)
                    retry_opts["format"] = "best/ba/b"
                    retry_opts["socket_timeout"] = 5
                    retry_opts["extractor_args"] = {"youtube": {"player_client": ["android"]}}
                    def _extract_retry():
                        with yt_dlp.YoutubeDL(retry_opts) as ydl_retry:
                            return ydl_retry.extract_info(clean_target, download=False)
                    retry_info = await loop.run_in_executor(self.executor, _extract_retry)
                    if retry_info:
                        stream_url = retry_info.get("url")
                        if not stream_url:
                            afs = [f for f in retry_info.get("formats", []) if f.get("acodec") != "none"]
                            if afs:
                                chosen_format = afs[-1]
                                stream_url = chosen_format.get("url")
                                info = retry_info
                except Exception:
                    pass

            if not stream_url:
                if not fut.done():
                    fut.set_result(None)
                return None

            raw_id = info.get("id", "")
            video_id = str(raw_id) if re.match(r"^[0-9A-Za-z_-]{11}$", str(raw_id)) else None

            start_offset = 0.0
            if video_id and not is_bili:
                start_offset = await self._fetch_sponsorblock_offset(video_id)
                if start_offset <= 0.0 and info.get("chapters"):
                    chaps = info.get("chapters", [])
                    if len(chaps) >= 2 and float(chaps[0].get("start_time", 0.0)) <= 3.0:
                        chap_title = str(chaps[0].get("title", "")).lower()
                        if any(k in chap_title for k in ["intro", "prologue", "plot", "story", "drama", "scene", "opening", "preview"]):
                            start_offset = float(chaps[1].get("start_time", 0.0))

            http_headers = (chosen_format.get("http_headers") if chosen_format else None) or info.get("http_headers") or {}
            user_agent = http_headers.get("User-Agent") or self.headers["User-Agent"]

            reconnect_flags = (
                "-loglevel fatal -nostats -reconnect 1 -reconnect_streamed 1 "
                "-reconnect_on_network_error 1 -reconnect_on_http_error 4xx,5xx "
                "-reconnect_at_eof 1 -reconnect_delay_max 5 -probesize 512k -analyzeduration 100000 "
                "-fflags nobuffer+fastseek -flush_packets 1"
            )

            hdr_lines = [f"User-Agent: {user_agent}"]
            if is_bili:
                bili_ref = f"https://www.bilibili.com/video/{video_id}/" if (video_id and str(video_id).startswith("BV")) else (clean_target if clean_target.startswith("http") else "https://www.bilibili.com/")
                hdr_lines.append(f"Referer: {bili_ref}")
                hdr_lines.append("Origin: https://www.bilibili.com")
            else:
                if "referer" in [k.lower() for k in http_headers]:
                    hdr_lines.append(f"Referer: {http_headers.get('Referer')}")

            for hk, hv in http_headers.items():
                if hk.lower() not in ("user-agent", "referer", "origin", "accept-encoding", "host"):
                    clean_hv = str(hv).replace('"', '\\"')
                    hdr_lines.append(f"{hk}: {clean_hv}")

            hdrs_payload = "\r\n".join(hdr_lines) + "\r\n"
            if "googlevideo.com" in stream_url or "youtube.com" in stream_url:
                before_opts = reconnect_flags
            else:
                before_opts = (
                    f'-headers "{hdrs_payload}" '
                    f'{reconnect_flags}'
                )

            if start_offset > 0.0:
                before_opts += f" -ss {start_offset}"

            raw_title = info.get("title")
            if raw_title and (raw_title == "videoplayback" or re.match(r"^\d+-\d+-\d+$", str(raw_title))):
                title = "Unknown"
            else:
                title = raw_title or "Unknown"

            web_url = info.get("webpage_url") or clean_target
            if is_bili and ("bilivideo.com" in web_url or "akamaized.net" in web_url):
                web_url = clean_target

            duration = int(info.get("duration") or 0)

            res_dict = {
                "id": video_id,
                "title": title,
                "uploader": info.get("uploader", "Unknown"),
                "duration": duration,
                "thumbnail": info.get("thumbnail") or "",
                "webpage_url": web_url,
                "stream_url": stream_url,
                "is_live": False if duration > 0 else info.get("is_live", False),
                "before_options": before_opts,
                "start_offset": start_offset,
                "http_headers": http_headers
            }
            async with self._cache_lock:
                if len(self._stream_cache) > 2000:
                    self._stream_cache.popitem(last=False)
                self._stream_cache[target] = (now, res_dict)

            if not fut.done():
                fut.set_result(res_dict)
            return res_dict
        except Exception as e:
            err_str = str(e)
            if "confirm you're not a bot" in err_str.lower() or "sign in" in err_str.lower():
                print("[WARNING] YouTube bot detection triggered. Cookies may be missing or expired in /app/data/cookies.txt!")
                if self.alert_callback:
                    try:
                        self.alert_callback("YouTube bot detection triggered (Sign in to confirm you're not a bot). Cookies may be missing or expired in `/app/data/cookies.txt`.")
                    except Exception:
                        pass
            if not fut.done():
                fut.set_result(None)
            return None
        finally:
            if not fut.done():
                fut.set_result(None)
            async with self._flight_lock:
                self._in_flight_streams.pop(target, None)

    def _clean_title_for_fallback(self, title: str) -> str:
        return self._clean_title_for_comparison(title)

    async def _fetch_title_from_oembed(self, url: str) -> str:
        try:
            session = await self.get_session()
            oembed_url = f"https://www.youtube.com/oembed?url={url}&format=json"
            async with session.get(oembed_url, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("title", "")
        except Exception:
            pass
        return ""

    async def _search_bilibili_stream(self, title: str) -> Optional[Dict[str, Any]]:
        encoded_title = urllib.parse.quote(title)
        url = f"https://search.bilibili.com/all?keyword={encoded_title}"
        session = await self.get_session()
        headers = {
            **self.headers,
            "Referer": "https://www.bilibili.com/",
            "Origin": "https://www.bilibili.com"
        }

        blacklist = [
            "cover", "翻唱", "翻弹", "翻彈", "demo", "remix", "二创", "二創",
            "剪辑", "剪輯", "反应", "反應", "reaction", "吉他", "钢琴", "鋼琴",
            "伴奏", "纯音", "自制", "自製", "手书", "手書", "mad", "amv", "mmd",
            "舞蹈", "教学", "教學", "游戏", "遊戲", "解说", "解說", "鬼畜", "试听",
            "試聽", "片段", "卡拉ok", "karaoke"
        ]

        q_lower = title.lower()
        q_words = [w for w in re.split(r'[\s\-_|/]+', q_lower) if len(w) > 0]

        try:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    html = await resp.text()
                    cards = re.findall(r'<div class="bili-video-card__wrap"[^>]*>(.*?)</div>\s*</div>\s*</div>', html, re.DOTALL)
                    candidates = []
                    for c in cards:
                        bv_m = re.search(r'/video/(BV[0-9A-Za-z]{10})', c)
                        t_m = re.search(r'alt=[\'"]([^\'"]+)[\'"]', c) or re.search(r'<h3[^>]*title=[\'"]([^\'"]+)[\'"]', c)
                        dur_m = re.search(r'class="bili-video-card__stats__duration"[^>]*>([^<]+)</span>', c)
                        if not bv_m or not t_m:
                            continue
                        bvid = bv_m.group(1)
                        clean_t = re.sub(r'<[^>]+>', '', t_m.group(1)).strip()
                        t_lower = clean_t.lower()

                        if any(bad in t_lower and bad not in q_lower for bad in blacklist):
                            continue

                        if q_words:
                            matched = sum(1 for w in q_words if w in t_lower)
                            if matched < min(len(q_words), 2):
                                continue

                        dur_str = dur_m.group(1) if dur_m else "0:00"
                        dur_parts = dur_str.split(":")
                        dur_sec = 0
                        if len(dur_parts) == 2:
                            dur_sec = int(dur_parts[0]) * 60 + int(dur_parts[1])
                        elif len(dur_parts) == 3:
                            dur_sec = int(dur_parts[0]) * 3600 + int(dur_parts[1]) * 60 + int(dur_parts[2])

                        if dur_sec > 0 and (dur_sec < 45 or dur_sec > 900):
                            continue

                        score = 0
                        if any(k in t_lower for k in ["official", "music video", "mv", "原版", "正版"]):
                            score += 5000
                        if "live" in t_lower:
                            score -= 500

                        candidates.append((score, bvid))

                    if candidates:
                        candidates.sort(key=lambda x: x[0], reverse=True)
                        for _, best_bvid in candidates[:3]:
                            target_url = f"https://www.bilibili.com/video/{best_bvid}/"
                            res = await self.get_live_stream(target_url)
                            if res and res.get("stream_url"):
                                return res
        except Exception:
            pass
        return None

    async def _search_soundcloud_stream(self, title: str) -> Optional[Dict[str, Any]]:
        loop = asyncio.get_running_loop()
        opts = dict(self.ydl_opts_stream)
        def _extract():
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(f"scsearch1:{title}", download=False)
        try:
            info = await loop.run_in_executor(self.executor, _extract)
            if info:
                entry = info["entries"][0] if "entries" in info and info["entries"] else info
                stream_url = entry.get("url")
                if not stream_url:
                    formats = [f for f in entry.get("formats", []) if f.get("acodec") != "none"]
                    if formats:
                        stream_url = formats[-1].get("url")
                if stream_url:
                    http_headers = entry.get("http_headers", {})
                    user_agent = http_headers.get("User-Agent") or self.headers["User-Agent"]
                    before_opts = f'-headers "User-Agent: {user_agent}\r\n" {DEFAULT_BEFORE_OPTS}'
                    return {
                        "id": str(entry.get("id")),
                        "title": entry.get("title", title),
                        "uploader": entry.get("uploader", "SoundCloud"),
                        "duration": int(entry.get("duration") or 0),
                        "thumbnail": entry.get("thumbnail") or "",
                        "webpage_url": entry.get("webpage_url") or "",
                        "stream_url": stream_url,
                        "is_live": False,
                        "before_options": before_opts,
                        "start_offset": 0.0,
                        "http_headers": http_headers
                    }
        except Exception:
            pass
        return None

    async def get_fallback_stream(self, title_or_url: str) -> Optional[Dict[str, Any]]:
        target_title = title_or_url
        if target_title.startswith("http://") or target_title.startswith("https://"):
            oembed_title = await self._fetch_title_from_oembed(target_title)
            if oembed_title:
                target_title = oembed_title
            else:
                return None

        clean_title = self._clean_title_for_comparison(target_title)
        query = clean_title or target_title

        original_vid = None
        if "youtube.com" in title_or_url or "youtu.be" in title_or_url:
            v_m = re.search(r"(?:v=|youtu\.be/)([0-9A-Za-z_-]{11})", title_or_url)
            if v_m:
                original_vid = v_m.group(1)

        try:
            yt_candidates = await self._resolve_raw_search(query)
            if yt_candidates:
                for cand in yt_candidates:
                    cand_id = cand.get("id")
                    if original_vid and cand_id == original_vid:
                        continue
                    cand_url = cand.get("webpage_url") or (f"https://www.youtube.com/watch?v={cand_id}" if cand_id else None)
                    if cand_url:
                        res = await self.get_live_stream(cand_url)
                        if res and res.get("stream_url"):
                            return res
        except Exception:
            pass

        sc_stream = await self._search_soundcloud_stream(query)
        if sc_stream and sc_stream.get("stream_url"):
            return sc_stream

        if os.getenv("ENABLE_BILI_FALLBACK", "0") == "1":
            bili_stream = await self._search_bilibili_stream(query)
            if bili_stream and bili_stream.get("stream_url"):
                return bili_stream

        return None

    async def get_autoplay_recommendation(self, current_info: Dict[str, Any], history_ids: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
        if history_ids is None:
            history_ids = []
        loop = asyncio.get_running_loop()
        video_id = current_info.get("id")

        if video_id and re.match(r"^[0-9A-Za-z_-]{11}$", str(video_id)):
            mix_url = f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}"
            opts = dict(self.ydl_opts_meta)
            opts["playlistend"] = 15

            def _extract_mix():
                with yt_dlp.YoutubeDL(opts) as ydl:
                    return ydl.extract_info(mix_url, download=False)

            try:
                info = await loop.run_in_executor(self.executor, _extract_mix)
                if info and "entries" in info:
                    for entry in info["entries"]:
                        if not entry:
                            continue
                        e_id = entry.get("id")
                        if e_id and re.match(r"^[0-9A-Za-z_-]{11}$", str(e_id)) and e_id != video_id and e_id not in history_ids:
                            return {
                                "title": entry.get("title", "Recommended Track"),
                                "search_query": f"https://www.youtube.com/watch?v={e_id}",
                                "id": e_id
                            }
            except Exception:
                pass

        query_seed = current_info.get("title", "")
        if current_info.get("uploader") and current_info["uploader"] != "Unknown":
            query_seed = f"{query_seed} {current_info['uploader']}"

        clean_seed = re.sub(r"[\(\[【《『].*?[\)\]】》』]", "", query_seed).strip()
        search_query = f"ytsearch5:{clean_seed} official audio"
        opts = dict(self.ydl_opts_meta)
        opts["playlistend"] = 5

        def _extract_search(q_target):
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(q_target, download=False)

        try:
            info = await loop.run_in_executor(self.executor, lambda: _extract_search(search_query))
            valid_entries = []
            if info and "entries" in info:
                valid_entries = [
                    e for e in info["entries"]
                    if e and e.get("id") and re.match(r"^[0-9A-Za-z_-]{11}$", str(e.get("id")))
                    and e.get("id") != video_id and e.get("id") not in history_ids
                ]

            if not valid_entries:
                simple_title = re.sub(r"[【】《》『』\[\]\(\)\-_/]+", " ", current_info.get("title", "")).strip()
                if simple_title:
                    fallback_query = f"ytsearch5:{simple_title} official"
                    fallback_info = await loop.run_in_executor(self.executor, lambda: _extract_search(fallback_query))
                    if fallback_info and "entries" in fallback_info:
                        valid_entries = [
                            e for e in fallback_info["entries"]
                            if e and e.get("id") and re.match(r"^[0-9A-Za-z_-]{11}$", str(e.get("id")))
                            and e.get("id") != video_id and e.get("id") not in history_ids
                        ]

            if valid_entries:
                ranked = sorted(
                    valid_entries,
                    key=lambda x: self._score_music_candidate(
                        x.get("title", ""),
                        x.get("uploader", ""),
                        query=clean_seed or current_info.get("title", ""),
                        duration=int(x.get("duration") or 0)
                    ),
                    reverse=True
                )
                best = ranked[0]
                return {
                    "title": best.get("title", "Recommended Track"),
                    "search_query": f"https://www.youtube.com/watch?v={best.get('id')}",
                    "id": best.get("id")
                }
        except Exception:
            pass

        return None

    async def get_lyrics(self, title: str, artist: str = "") -> Optional[str]:
        clean_title = self.clean_for_search(title or "")
        clean_title = re.sub(r"[\(\[（【].*?[\)\]）】]", "", clean_title).strip()
        if " - " in clean_title and not artist:
            parts = clean_title.split(" - ", 1)
            artist = parts[0].strip()
            clean_title = parts[1].strip()
        if not clean_title:
            return None

        clean_artist = re.sub(r"\s*-\s*Topic$", "", artist or "", flags=re.I)
        clean_artist = re.sub(r"\b(VEVO|Official)\b", "", clean_artist, flags=re.I).strip()

        def _clean_lrc(raw: Optional[str]) -> Optional[str]:
            if not raw:
                return None
            return re.sub(r"\[\d{1,2}:\d{2}(?:\.\d{1,3})?\]\s*", "", raw).strip() or None

        session = await self.get_session()
        if clean_artist:
            url = (
                f"https://lrclib.net/api/get?track_name={urllib.parse.quote(clean_title)}"
                f"&artist_name={urllib.parse.quote(clean_artist)}"
            )
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        lyrics = (data.get("plainLyrics") or "").strip() or _clean_lrc(data.get("syncedLyrics"))
                        if lyrics:
                            return lyrics
            except Exception:
                pass

        queries = [f"{clean_title} {clean_artist}".strip()] if clean_artist else [clean_title]
        if clean_artist:
            queries.append(clean_title)

        for q in queries:
            search_url = f"https://lrclib.net/api/search?q={urllib.parse.quote(q)}"
            try:
                async with session.get(search_url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        items = await resp.json()
                        if isinstance(items, list):
                            for item in items:
                                lyrics = (item.get("plainLyrics") or "").strip() or _clean_lrc(item.get("syncedLyrics"))
                                if lyrics:
                                    return lyrics
            except Exception:
                pass
        return None

    async def verify_cookies_task(self):
        if not getattr(self, "cookie_path", None):
            if self.alert_callback:
                try:
                    self.alert_callback("System Alert: cookies.txt was not detected. YouTube streams may face rate limits.")
                except Exception:
                    pass
            return

        loop = asyncio.get_running_loop()

        def _check():
            opts = dict(self.ydl_opts_meta)
            opts["playlistend"] = 1
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info("https://www.youtube.com/watch?v=dQw4w9WgXcQ", download=False)

        try:
            await loop.run_in_executor(self.executor, _check)
        except Exception as e:
            err_msg = str(e)
            if any(k in err_msg.lower() for k in ("bot", "sign in", "login", "confirm")):
                if self.alert_callback:
                    try:
                        self.alert_callback(f"YouTube Cookie Alert: Verification triggered or cookies expired: {err_msg[:120]}")
                    except Exception:
                        pass

    def _cleanup_audio_cache(self, max_bytes: int = 1024 * 1024 * 1024):
        cache_dir = os.path.join("/app/uploads" if os.path.exists("/app/uploads") else "uploads", "cache")
        if not os.path.exists(cache_dir):
            return
        try:
            entries = []
            total_size = 0
            for f in os.listdir(cache_dir):
                fp = os.path.join(cache_dir, f)
                if os.path.isfile(fp):
                    st = os.stat(fp)
                    total_size += st.st_size
                    entries.append((st.st_mtime, st.st_size, fp))
            if total_size > max_bytes:
                entries.sort(key=lambda x: x[0])
                for _, size, path in entries:
                    try:
                        os.remove(path)
                        total_size -= size
                    except Exception:
                        pass
                    if total_size <= int(max_bytes * 0.75):
                        break
        except Exception:
            pass

    def get_cached_track_path(self, track_id: str) -> Optional[str]:
        if not track_id:
            return None
        safe_id = re.sub(r"[^0-9A-Za-z_-]", "_", str(track_id))
        cache_dir = os.path.join("/app/uploads" if os.path.exists("/app/uploads") else "uploads", "cache")
        target_path = os.path.join(cache_dir, f"{safe_id}.audio")
        if os.path.exists(target_path) and os.path.getsize(target_path) > 10240:
            return target_path
        return None

    async def preload_track_audio(self, stream_url: str, http_headers: Dict[str, Any], track_id: str) -> Optional[str]:
        if not stream_url or not track_id:
            return None
        safe_id = re.sub(r"[^0-9A-Za-z_-]", "_", str(track_id))
        cache_dir = os.path.join("/app/uploads" if os.path.exists("/app/uploads") else "uploads", "cache")
        try:
            os.makedirs(cache_dir, exist_ok=True)
        except Exception:
            return None

        target_path = os.path.join(cache_dir, f"{safe_id}.audio")
        part_path = os.path.join(cache_dir, f"{safe_id}.part")
        if os.path.exists(target_path) and os.path.getsize(target_path) > 10240:
            return target_path

        if len(self._preload_locks) > 200:
            for k in list(self._preload_locks.keys())[:-100]:
                self._preload_locks.pop(k, None)

        track_lock = self._preload_locks.setdefault(safe_id, asyncio.Lock())
        async with track_lock:
            if os.path.exists(target_path) and os.path.getsize(target_path) > 10240:
                return target_path

            session = await self.get_session()
            loop = asyncio.get_running_loop()
            fh = None
            try:
                req_headers = dict(http_headers or {})
                if "User-Agent" not in req_headers:
                    req_headers["User-Agent"] = self.headers["User-Agent"]
                async with session.get(stream_url, headers=req_headers, timeout=aiohttp.ClientTimeout(total=45, connect=5, sock_read=15)) as resp:
                    if resp.status not in (200, 206):
                        return None
                    content_length = resp.headers.get("Content-Length")
                    expected_bytes = int(content_length) if content_length and content_length.isdigit() else None
                    if expected_bytes and expected_bytes > 60 * 1024 * 1024:
                        return None

                    fh = await loop.run_in_executor(self.executor, open, part_path, "wb")
                    downloaded_bytes = 0
                    while True:
                        chunk = await resp.content.read(512 * 1024)
                        if not chunk:
                            break
                        downloaded_bytes += len(chunk)
                        if downloaded_bytes > 60 * 1024 * 1024:
                            return None
                        await loop.run_in_executor(self.executor, fh.write, chunk)

                    await loop.run_in_executor(self.executor, fh.close)
                    fh = None

                    if expected_bytes and downloaded_bytes < expected_bytes:
                        return None

                    if os.path.exists(part_path) and os.path.getsize(part_path) > 10240:
                        os.replace(part_path, target_path)
                        await loop.run_in_executor(self.executor, self._cleanup_audio_cache)
                        return target_path
            except Exception:
                pass
            finally:
                if fh is not None:
                    try:
                        await loop.run_in_executor(self.executor, fh.close)
                    except Exception:
                        pass
                if os.path.exists(part_path):
                    try:
                        os.remove(part_path)
                    except Exception:
                        pass
            return None