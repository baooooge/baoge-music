import os
import asyncio
import json
import re
import difflib
import time
import threading
import urllib.parse
from typing import Dict, Any, List, Optional
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import aiohttp
from bs4 import BeautifulSoup
import yt_dlp

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

        youtube_extractor_args = {
            "youtube": {
                "player_client": ["android", "web"]
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
            "http_headers": self.headers,
            "extractor_args": youtube_extractor_args,
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
            "http_headers": self.headers,
            "extractor_args": youtube_extractor_args,
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
        self._extract_semaphore = asyncio.Semaphore(16)
        self._flight_lock = asyncio.Lock()
        self._in_flight_searches: Dict[str, asyncio.Future] = {}
        self._in_flight_streams: Dict[str, asyncio.Future] = {}
        self._sponsorblock_cache = OrderedDict()

        self.kkbox_client_id = os.getenv("KKBOX_CLIENT_ID", "")
        self.kkbox_client_secret = os.getenv("KKBOX_CLIENT_SECRET", "")
        self.kkbox_cookie = os.getenv("KKBOX_COOKIE", "")
        self._kkbox_token = None
        self._kkbox_token_expiry = 0
        self.alert_callback = None

    def _acquire_ydl_meta(self) -> yt_dlp.YoutubeDL:
        with self._pool_lock:
            if self._meta_pool:
                return self._meta_pool.pop()
        return yt_dlp.YoutubeDL(self.ydl_opts_meta)

    def _release_ydl_meta(self, ydl_inst: yt_dlp.YoutubeDL):
        with self._pool_lock:
            if len(self._meta_pool) < 16:
                self._meta_pool.append(ydl_inst)

    def _acquire_ydl_stream(self) -> yt_dlp.YoutubeDL:
        with self._pool_lock:
            if self._stream_pool:
                return self._stream_pool.pop()
        return yt_dlp.YoutubeDL(self.ydl_opts_stream)

    def _release_ydl_stream(self, ydl_inst: yt_dlp.YoutubeDL):
        with self._pool_lock:
            if len(self._stream_pool) < 16:
                self._stream_pool.append(ydl_inst)

    async def get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(
                limit=100,
                limit_per_host=30,
                ttl_dns_cache=600,
                keepalive_timeout=60,
                enable_cleanup_closed=True
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

    async def resolve_metadata_batch(self, query: str) -> List[Dict[str, Any]]:
        query = query.strip()
        now = time.time()
        async with self._cache_lock:
            if query in self._meta_cache:
                cached_time, cached_val = self._meta_cache[query]
                if now - cached_time < 7200:
                    return cached_val
                del self._meta_cache[query]

        async with self._flight_lock:
            if query in self._in_flight_searches:
                return await self._in_flight_searches[query]
            fut = asyncio.get_running_loop().create_future()
            self._in_flight_searches[query] = fut

        try:
            res = []
            if "streetvoice.com" in query:
                res = await self._resolve_streetvoice(query)
            elif "kkbox.com" in query:
                res = await self._resolve_kkbox(query)
            elif "open.spotify.com" in query:
                res = await self._resolve_spotify(query)
            elif "music.apple.com" in query:
                res = await self._resolve_apple_music(query)
            elif "bilibili.com" in query or "b23.tv" in query:
                res = await self._resolve_bilibili(query)
            else:
                res = await self._resolve_raw_search(query)

            if res:
                async with self._cache_lock:
                    if len(self._meta_cache) > 2000:
                        self._meta_cache.popitem(last=False)
                    self._meta_cache[query] = (now, res)

                if len(res) == 1 and res[0].get("search_query"):
                    asyncio.create_task(self.get_live_stream(res[0]["search_query"]))

            if not fut.done():
                fut.set_result(res)
            return res
        except Exception:
            if not fut.done():
                fut.set_result([])
            return []
        finally:
            async with self._flight_lock:
                self._in_flight_searches.pop(query, None)

    async def _resolve_streetvoice(self, url: str) -> List[Dict[str, Any]]:
        clean_url = url.split("?")[0]

        session = await self.get_session()
        async with session.get(clean_url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                return []
            html = await resp.text()

        soup = BeautifulSoup(html, "html.parser")

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
                                q = f"{name} {artist}".strip() if artist else name
                                if q:
                                    results.append({"title": q, "search_query": q, "webpage_url": clean_url})
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
                            q = f"{name} {artist}".strip() if artist else name
                            if q:
                                results.append({"title": q, "search_query": q, "webpage_url": clean_url})
                        return results
            elif m_song:
                s_id = m_song.group(1)
                api_url = f"https://api.kkbox.com/v1.1/tracks/{s_id}?territory=TW"
                async with session.get(api_url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        item = await resp.json()
                        name = item.get("name", "")
                        artist = item.get("album", {}).get("artist", {}).get("name", "")
                        q = f"{name} {artist}".strip() if artist else name
                        if q:
                            return [{"title": q, "search_query": q, "webpage_url": clean_url}]
        except Exception:
            pass
        return results

    async def _resolve_kkbox(self, url: str) -> List[Dict[str, Any]]:
        api_results = await self._resolve_kkbox_api(url)
        if api_results:
            return api_results[:100]

        clean_url = url.split("?")[0]
        results = []

        session = await self.get_session()
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

        soup = BeautifulSoup(html, "html.parser")

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
                                results.append({"title": q, "search_query": q, "webpage_url": clean_url})
                    elif node.get("@type") == "MusicRecording":
                        t_name = node.get("name", "").strip()
                        art = node.get("byArtist", {}).get("name", "").strip() if isinstance(node.get("byArtist"), dict) else ""
                        q = f"{t_name} {art}".strip() if art else t_name
                        if q and q not in [r["title"] for r in results]:
                            results.append({"title": q, "search_query": q, "webpage_url": clean_url})
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
                        return [{"title": q, "search_query": q, "webpage_url": clean_url}]

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
                            results.append({"title": q, "search_query": q, "webpage_url": clean_url})
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
                results.append({"title": q, "search_query": q, "webpage_url": clean_url})

        if not results:
            og_title = soup.find("meta", property="og:title")
            if og_title and og_title.get("content"):
                t = og_title["content"].split(" - ")[0].strip()
                if t and "KKBOX" not in t:
                    results.append({"title": t, "search_query": t, "webpage_url": clean_url})

        return results[:100]

    async def _resolve_spotify(self, url: str) -> List[Dict[str, Any]]:
        clean_url = url.split("?")[0]
        match = re.search(r"open\.spotify\.com/(track|album|playlist|artist)/([a-zA-Z0-9]+)", clean_url)
        if not match:
            return []
        item_type, item_id = match.groups()
        embed_url = f"https://open.spotify.com/embed/{item_type}/{item_id}"

        session = await self.get_session()
        async with session.get(embed_url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                return []
            html = await resp.text()

        soup = BeautifulSoup(html, "html.parser")
        script = soup.find("script", id="__NEXT_DATA__")
        if not script or not script.string:
            return []

        data = json.loads(script.string)
        entity = data.get("props", {}).get("pageProps", {}).get("state", {}).get("data", {}).get("entity", {})

        meta_list = []
        if item_type == "track":
            title = entity.get("name", "")
            artists = " ".join([a.get("name", "") for a in entity.get("artists", [])])
            if title:
                meta_list.append({"title": f"{title} - {artists}", "search_query": f"{title} {artists}".strip(), "webpage_url": clean_url})
        elif item_type in ["album", "playlist", "artist"]:
            track_list = entity.get("trackList", [])[:100]
            for t in track_list:
                title = t.get("title", "")
                subtitle = t.get("subtitle", "")
                if title:
                    meta_list.append({"title": f"{title} - {subtitle}", "search_query": f"{title} {subtitle}".strip(), "webpage_url": clean_url})

        return meta_list

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

        soup = BeautifulSoup(html, "html.parser")
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

                        if s_name:
                            disp = f"{art_name} - {s_name}" if art_name else s_name
                            q = f"{art_name} {s_name}".strip() if art_name else s_name
                            return [{"title": disp, "search_query": q, "webpage_url": url}]

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

                            final_art = tr_art or album_artist
                            disp = f"{final_art} - {tr_name}" if final_art else tr_name
                            q = f"{final_art} {tr_name}".strip() if final_art else tr_name
                            item_obj = {"title": disp, "search_query": q, "webpage_url": tr_url or url}

                            if target_id and (target_id in tr_url or str(tr.get("id", "")) == target_id):
                                matched_single = item_obj
                                break
                            elif tr_name:
                                album_results.append(item_obj)

                        if target_id and matched_single:
                            return [matched_single]
                        if not target_id and album_results:
                            return album_results[:100]
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
                        import gzip
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
                soup = BeautifulSoup(html, "html.parser")
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
        "听": "聽", "爱": "愛", "宝": "寶", "国": "國", "语": "語"
    })

    @classmethod
    def _normalize_cjk(cls, text: str) -> str:
        return text.translate(cls._TRANS_TABLE).lower()

    @classmethod
    def _clean_title_for_comparison(cls, title: str) -> str:
        t = cls._normalize_cjk(title)
        t = re.sub(r"[()\[\]【】《》『』〈〉「」|/—\-_]+", " ", t)
        t = re.sub(r"(?i)\b(official\s*(music)?\s*(video|audio|lyric|lyrics)?|mv|hd|4k|audio|lyrics?|full\s*song|hq)\b", " ", t)
        t = re.sub(r"\s+", " ", t).strip()
        return t

    def _score_music_candidate(self, title: str, uploader: str, query: str = "", duration: int = 0, view_count: int = 0) -> int:
        score = 0
        t = self._normalize_cjk(title)
        u = self._normalize_cjk(uploader)
        q = self._normalize_cjk(query).strip() if query else ""

        clean_q = self._clean_title_for_comparison(q)
        clean_t = self._clean_title_for_comparison(title)

        q_parts = [p.strip() for p in re.split(r"\s*[-—/|]\s*", q) if p.strip()]

        if len(q_parts) >= 2:
            p0 = self._clean_title_for_comparison(q_parts[0])
            p1 = self._clean_title_for_comparison(q_parts[1])

            p0_in_t = any(w in clean_t for w in p0.split() if len(w) > 0)
            p1_in_t = any(w in clean_t for w in p1.split() if len(w) > 0)
            p0_in_u = any(w in u for w in p0.split() if len(w) > 1)
            p1_in_u = any(w in u for w in p1.split() if len(w) > 1)

            if p0_in_t and (p1_in_t or p1_in_u):
                score += 4000
            elif p1_in_t and (p0_in_t or p0_in_u):
                score += 4000
            elif p0_in_t or p1_in_t:
                score += 1500
            else:
                return -20000
        else:
            q_tokens = [tok for tok in clean_q.split() if tok]
            if q_tokens:
                matches = sum(1 for tok in q_tokens if tok in clean_t or tok in u)
                ratio = matches / len(q_tokens)
                if ratio < 0.35:
                    return -15000
                score += int(ratio * 2500)

        if " - topic" in u or u.endswith(" topic"):
            score += 3500
        elif "vevo" in u:
            score += 3000
        elif any(k in u for k in ["official", "records", "music", "entertainment", "channel"]):
            score += 1500

        if any(k in t for k in ["official audio", "official track", "audio"]):
            score += 2500
        elif any(k in t for k in ["official lyric", "official lyrics", "lyric video", "lyrics video"]):
            score += 2200
        elif any(k in t for k in ["official music video", "official mv", "music video"]):
            score += 2000
        elif any(k in t for k in ["studio version", "album version", "original version"]):
            score += 1800

        if view_count >= 1_000_000:
            score += 3500
        elif view_count >= 500_000:
            score += 2500
        elif view_count >= 100_000:
            score += 1500
        elif view_count >= 10_000:
            score += 500

        remix_keywords = [
            "remix", "mix", "8d", "slowed", "reverb", "speed up", "sped up",
            "nightcore", "mashup", "dj", "bass boosted", "trap", "lofi", "lo-fi"
        ]
        for rk in remix_keywords:
            if rk in (t + " " + u) and rk not in q:
                score -= 6000

        cover_keywords = [
            "cover", "guitar cover", "piano cover", "drum cover", "dance cover",
            "acoustic cover", "karaoke"
        ]
        for ck in cover_keywords:
            if ck in (t + " " + u) and ck not in q:
                score -= 6000

        spam_keywords = [
            "reaction", "instrumental", "bgm", "podcast", "review",
            "\u7d55\u7f8e\u7684\u756b\u9762", "\u597d\u807d\u7684\u65cb\u5f8b", "\u7d55\u7f8e",
            "\u4e2d\u5b57", "\u7e41\u4e2d", "\u52d5\u614b\u6b4c\u8a5e", "\u526a\u8f2f",
            "\u89e3\u8aaa", "\u96fb\u5f71\u7247\u6bb5", "\u96fb\u8996\u5287", "\u53cd\u61c9",
            "\u7d14\u4eab", "\u7d14\u97f3\u6a02", "\u4f34\u594f", "\u5408\u96c6",
            "\u76e4\u9ede", "\u7cbe\u9078", "\u7ffb\u5531", "\u6539\u7de8",
            "\u65e5\u63a8", "\u6b4c\u55ae", "\u6b4c\u5355", "\u7cbe\u9009", "\u5408\u8f91",
            "\u65e0\u635f", "\u79c1\u85cf", "\u5408\u96c6", "lawpj"
        ]
        for sk in spam_keywords:
            norm_sk = self._normalize_cjk(sk)
            if norm_sk in (t + " " + u) and norm_sk not in q:
                score -= 8000

        live_keywords = ["live", "concert", "fancam"]
        for lk in live_keywords:
            if lk in (t + " " + u) and lk not in q:
                score -= 2500

        if 90 <= duration <= 360:
            score += 150
        elif duration > 540 or (0 < duration < 50):
            score -= 4000

        return score

    async def _resolve_raw_search(self, query: str) -> List[Dict[str, Any]]:
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
                if "confirm your age" in err_msg or "sign in" in err_msg or "format is not available" in err_msg or "requested format" in err_msg:
                    try:
                        retry_opts = dict(opts)
                        retry_opts["format"] = "best/ba/b"
                        retry_opts["extractor_args"] = {"youtube": {"player_client": ["tv_embedded", "tv"]}}
                        with yt_dlp.YoutubeDL(retry_opts) as ydl_retry:
                            return ydl_retry.extract_info(target_query, download=False)
                    except Exception:
                        return None
                return None
            finally:
                self._release_ydl_meta(ydl_inst)

        is_yt_mix = is_url and bool(re.search(r"[?&]list=(?:RD|UL)[0-9A-Za-z_-]+", query))
        if is_yt_mix:
            opts["playlistend"] = 25

        try:
            if is_url:
                if not re.search(r"[?&]list=", query):
                    try:
                        asyncio.create_task(self.get_live_stream(query))
                    except Exception:
                        pass
                info = None
                try:
                    info = await loop.run_in_executor(self.executor, lambda: _extract(query))
                except Exception:
                    info = None

                if not info or not info.get("entries"):
                    v_match = re.search(r"(?:v=|youtu\.be/)([0-9A-Za-z_-]{11})", query)
                    if v_match:
                        single_url = f"https://www.youtube.com/watch?v={v_match.group(1)}"
                        try:
                            info = await loop.run_in_executor(self.executor, lambda: _extract(single_url))
                        except Exception:
                            pass
            else:
                opts["playlistend"] = 3
                search_target = f"ytsearch3:{query}"
                info = await loop.run_in_executor(self.executor, lambda: _extract(search_target))

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
                        view_count=int(x.get("view_count") or 0)
                    ),
                    reverse=True
                )
                chosen = ranked[0]
                vid_id = chosen.get("id", "")
                final_target = f"https://www.youtube.com/watch?v={vid_id}" if re.match(r"^[0-9A-Za-z_-]{11}$", str(vid_id)) else (chosen.get("webpage_url") or chosen.get("url") or query)
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
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=3)) as resp:
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

    async def get_live_stream(self, target: str) -> Optional[Dict[str, Any]]:
        now = time.time()
        async with self._cache_lock:
            if target in self._stream_cache:
                cached_time, cached_val = self._stream_cache[target]
                if now - cached_time < 3600:
                    return cached_val
                del self._stream_cache[target]

        async with self._flight_lock:
            if target in self._in_flight_streams:
                return await self._in_flight_streams[target]
            fut = asyncio.get_running_loop().create_future()
            self._in_flight_streams[target] = fut

        try:
            reconnect_flags = (
                "-loglevel fatal -nostats -reconnect 1 -reconnect_streamed 1 "
                "-reconnect_on_network_error 1 -reconnect_on_http_error 4xx,5xx "
                "-reconnect_at_eof 1 -reconnect_delay_max 5 -probesize 128k -analyzeduration 0"
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
                                            "start_offset": 0.0
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

            def _extract():
                if is_bili:
                    try:
                        with yt_dlp.YoutubeDL(opts) as ydl:
                            if clean_target.startswith("http"):
                                return ydl.extract_info(clean_target, download=False)
                            return ydl.extract_info(f"ytsearch5:{clean_target}", download=False)
                    except Exception:
                        return None

                ydl_inst = self._acquire_ydl_stream()
                try:
                    if clean_target.startswith("http"):
                        return ydl_inst.extract_info(clean_target, download=False)
                    return ydl_inst.extract_info(f"ytsearch5:{clean_target}", download=False)
                except Exception as ex:
                    err_msg = str(ex).lower()
                    if "confirm your age" in err_msg or "sign in" in err_msg:
                        try:
                            retry_opts = dict(opts)
                            retry_opts["format"] = "best/ba/b"
                            retry_opts["extractor_args"] = {"youtube": {"player_client": ["tv_embedded", "tv"]}}
                            with yt_dlp.YoutubeDL(retry_opts) as ydl_retry:
                                if clean_target.startswith("http"):
                                    return ydl_retry.extract_info(clean_target, download=False)
                                return ydl_retry.extract_info(f"ytsearch5:{clean_target}", download=False)
                        except Exception:
                            return None
                    elif "format is not available" in err_msg or "requested format" in err_msg:
                        try:
                            retry_opts = dict(opts)
                            retry_opts["format"] = "best/ba/b"
                            retry_opts["extractor_args"] = {"youtube": {"player_client": ["android", "ios", "mweb", "web"]}}
                            with yt_dlp.YoutubeDL(retry_opts) as ydl_retry:
                                if clean_target.startswith("http"):
                                    return ydl_retry.extract_info(clean_target, download=False)
                                return ydl_retry.extract_info(f"ytsearch5:{clean_target}", download=False)
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
                            duration=int(x.get("duration") or 0)
                        ),
                        reverse=True
                    )
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
                "-reconnect_at_eof 1 -reconnect_delay_max 5 -probesize 32k -analyzeduration 0 "
                "-fflags nobuffer+fastseek -flush_packets 1"
            )

            hdr_lines = [f"User-Agent: {user_agent}"]
            if is_bili:
                referer = http_headers.get("Referer") or (clean_target if clean_target.startswith("http") else "https://www.bilibili.com/")
                hdr_lines.append(f"Referer: {referer}")
                hdr_lines.append("Origin: https://www.bilibili.com")
            else:
                if "referer" in [k.lower() for k in http_headers]:
                    hdr_lines.append(f"Referer: {http_headers.get('Referer')}")

            for hk, hv in http_headers.items():
                if hk.lower() not in ("user-agent", "referer", "origin", "accept-encoding", "host"):
                    clean_hv = str(hv).replace('"', '\\"')
                    hdr_lines.append(f"{hk}: {clean_hv}")

            hdrs_payload = "\r\n".join(hdr_lines) + "\r\n"
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
            async with self._flight_lock:
                self._in_flight_streams.pop(target, None)

    def _clean_title_for_comparison(self, title: str) -> str:
        if not title:
            return ""
        if title.startswith("http://") or title.startswith("https://"):
            return ""
        cleaned = re.sub(r"\[.*?\]|\(.*?\)|【.*?】|（.*?）", "", title)
        cleaned = re.sub(r"(?i)\b(official\s*(music\s*video|mv|audio|video|lyric\s*video)?|full\s*ver|hd|hq|4k|1080p)\b", "", cleaned)
        cleaned = re.sub(r"[\s\-_|/]+", " ", cleaned).strip()
        return cleaned or title.strip()

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
        import urllib.parse
        encoded_title = urllib.parse.quote(title)
        url = f"https://search.bilibili.com/all?keyword={encoded_title}"
        session = await self.get_session()
        headers = {
            **self.headers,
            "Referer": "https://www.bilibili.com/",
            "Origin": "https://www.bilibili.com"
        }
        try:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    bv_match = re.search(r'bvid[:=]["\']?(BV[0-9A-Za-z]{10})', text)
                    if not bv_match:
                        bv_match = re.search(r'/video/(BV[0-9A-Za-z]{10})', text)
                    if bv_match:
                        bvid = bv_match.group(1)
                        target_url = f"https://www.bilibili.com/video/{bvid}/"
                        return await self.get_live_stream(target_url)
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

        bili_stream = await self._search_bilibili_stream(query)
        if bili_stream and bili_stream.get("stream_url"):
            return bili_stream

        sc_stream = await self._search_soundcloud_stream(query)
        if sc_stream and sc_stream.get("stream_url"):
            return sc_stream

        return None

    async def get_autoplay_recommendation(self, current_info: Dict[str, Any], history_ids: List[str] = []) -> Optional[Dict[str, Any]]:
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
        clean_title = re.sub(r"[\(\[].*?[\)\]]", "", title).strip()
        url = f"https://lrclib.net/api/get?track_name={clean_title}&artist_name={artist}"
        try:
            session = await self.get_session()
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("plainLyrics") or data.get("syncedLyrics")
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

        session = await self.get_session()
        try:
            req_headers = dict(http_headers or {})
            if "User-Agent" not in req_headers:
                req_headers["User-Agent"] = self.headers["User-Agent"]
            async with session.get(stream_url, headers=req_headers, timeout=aiohttp.ClientTimeout(total=None, connect=5, sock_read=15)) as resp:
                if resp.status not in (200, 206):
                    return None
                with open(part_path, "wb") as f:
                    while True:
                        chunk = await resp.content.read(256 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                if os.path.exists(part_path) and os.path.getsize(part_path) > 10240:
                    os.replace(part_path, target_path)
                    self._cleanup_audio_cache()
                    return target_path
        except Exception:
            if os.path.exists(part_path):
                try:
                    os.remove(part_path)
                except Exception:
                    pass
        return None