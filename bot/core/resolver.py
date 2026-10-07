import os
import asyncio
import json
import re
import time
from typing import Dict, Any, List, Optional
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import aiohttp
from bs4 import BeautifulSoup
import yt_dlp

class UniversalResolver:
    def __init__(self):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7"
        }
        
        cookie_path = "/app/data/cookies.txt" if os.path.exists("/app/data/cookies.txt") else (
            "cookies.txt" if os.path.exists("cookies.txt") else None
        )

        youtube_extractor_args = {
            "youtube": {
                "player_client": ["android", "ios", "mweb"]
            }
        }

        self.ydl_opts_meta = {
            "format": "bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "default_search": "ytsearch",
            "extract_flat": "in_playlist",
            "playlistend": 100,
            "extractor_retries": 3,
            "socket_timeout": 10,
            "http_headers": self.headers,
            "extractor_args": youtube_extractor_args
        }
        if cookie_path:
            self.ydl_opts_meta["cookiefile"] = cookie_path

        self.ydl_opts_stream = {
            "format": "ba[protocol^=http]/ba/b",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
            "extractor_retries": 3,
            "socket_timeout": 10,
            "http_headers": self.headers,
            "extractor_args": youtube_extractor_args
        }
        if cookie_path:
            self.ydl_opts_stream["cookiefile"] = cookie_path

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

    async def get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(
                limit=100,
                limit_per_host=30,
                ttl_dns_cache=300,
                enable_cleanup_closed=True
            )
            self._session = aiohttp.ClientSession(
                connector=connector,
                headers=self.headers,
                timeout=aiohttp.ClientTimeout(total=10, connect=3)
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
        self.executor.shutdown(wait=False)

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
            else:
                res = await self._resolve_raw_search(query)

            if res:
                async with self._cache_lock:
                    if len(self._meta_cache) > 2000:
                        self._meta_cache.popitem(last=False)
                    self._meta_cache[query] = (now, res)

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
        session = await self.get_session()
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                return []
            html = await resp.text()

        soup = BeautifulSoup(html, "html.parser")
        og_title = soup.find("meta", property="og:title")
        if not og_title or not og_title.get("content"):
            return []
        title = og_title["content"].split(" - ")[0]
        return [{"title": title, "search_query": title, "webpage_url": url}]

    def _score_music_candidate(self, title: str, uploader: str, query: str = "", duration: int = 0) -> int:
        score = 0
        t = title.lower()
        u = uploader.lower()
        q = query.lower() if query else ""

        negative_patterns = [
            ("remix", 300),
            ("re-mix", 300),
            ("混音", 300),
            ("重混", 300),
            ("dj", 250),
            ("cover", 250),
            ("翻唱", 250),
            ("fancam", 250),
            ("reaction", 250),
            ("反應", 250),
            ("sped up", 250),
            ("speed up", 250),
            ("slowed", 250),
            ("nightcore", 250),
            ("bass boosted", 250),
            ("8d audio", 250),
            ("1 hour", 250),
            ("10 hour", 250),
            ("1hr", 250),
            ("10hr", 250),
            ("loop", 250),
            ("karaoke", 180),
            ("instrumental", 180),
            ("inst", 150),
            ("伴奏", 180),
            ("純音樂", 180),
            ("live", 140),
            ("concert", 140),
            ("現場", 140),
            ("演唱會", 140)
        ]

        for token, penalty in negative_patterns:
            if token in t and token not in q:
                score -= penalty

        has_lyric_term = any(k in t for k in ["lyric", "lyrics", "歌詞"])
        is_official_source = (
            any(k in t for k in ["official", "官方", "studio", "錄音室", "工作室"]) or
            any(k in u for k in ["topic", "vevo", "official", "官方", "records", "music", "entertainment", "channel"])
        )
        if has_lyric_term and not is_official_source:
            score -= 220

        if " - topic" in u or u.endswith(" topic"):
            score += 160
        elif "topic" in u:
            score += 130

        if "vevo" in u:
            score += 120

        if "official audio" in t or "官方音頻" in t:
            score += 140
        elif "official lyric video" in t or "official lyrics video" in t or "官方歌詞" in t:
            score += 135
        elif "official music video" in t or "official mv" in t or "官方mv" in t or "官方音樂" in t:
            score += 130
        elif "official video" in t or "official visualizer" in t or "官方完整版" in t:
            score += 115
        elif "official" in t or "官方" in t:
            score += 90

        if any(k in t for k in ["studio version", "錄音室", "工作室", "原版", "原唱", "original version"]):
            score += 110

        if "[mv]" in t or "(mv)" in t:
            score += 45

        if q:
            tokens = [tok for tok in re.split(r"[\s\-_/]+", q) if len(tok) >= 2]
            for tok in tokens:
                if tok in u:
                    score += 30
                if tok in t:
                    score += 20

        if duration > 0:
            if 90 <= duration <= 360:
                score += 35
            elif duration < 60:
                score -= 160
            elif duration > 600:
                score -= 200

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

        search_target = query
        if not is_url:
            opts["playlistend"] = 8
            search_target = f"ytsearch8:{query} official"

        def _extract():
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(search_target, download=False)

        try:
            info = await loop.run_in_executor(self.executor, _extract)
            if not info or ("entries" in info and not [e for e in info.get("entries", []) if e]):
                if not is_url:
                    search_target = f"ytsearch8:{query}"
                    info = await loop.run_in_executor(self.executor, _extract)
                if not info:
                    return []

            entries = []
            if "entries" in info:
                entries = [e for e in info["entries"] if e]
            else:
                entries = [info]

            if not entries:
                return []

            if not is_url and len(entries) > 1:
                ranked = sorted(
                    entries,
                    key=lambda x: self._score_music_candidate(
                        x.get("title", ""),
                        x.get("uploader", ""),
                        query=query,
                        duration=int(x.get("duration") or 0)
                    ),
                    reverse=True
                )
                chosen = ranked[0]
                vid_id = chosen.get("id", "")
                final_target = f"https://www.youtube.com/watch?v={vid_id}" if re.match(r"^[0-9A-Za-z_-]{11}$", str(vid_id)) else (chosen.get("webpage_url") or chosen.get("url") or query)
                return [{
                    "title": chosen.get("title", query),
                    "search_query": final_target,
                    "id": vid_id if re.match(r"^[0-9A-Za-z_-]{11}$", str(vid_id)) else None,
                    "duration": int(chosen.get("duration") or 0),
                    "uploader": chosen.get("uploader", "Unknown"),
                    "thumbnail": chosen.get("thumbnail") or "",
                    "webpage_url": final_target
                }]

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
        except Exception:
            return []

    async def _fetch_sponsorblock_offset(self, video_id: str) -> float:
        if not video_id or not re.match(r"^[0-9A-Za-z_-]{11}$", str(video_id)):
            return 0.0
        if video_id in self._sponsorblock_cache:
            return self._sponsorblock_cache[video_id]
        url = f"https://sponsor.ajay.app/api/skipSegments?videoID={video_id}&categories=[\"music_offtopic\"]"
        offset = 0.0
        try:
            session = await self.get_session()
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=2)) as resp:
                if resp.status == 200:
                    segments = await resp.json()
                    for seg in segments:
                        segment_range = seg.get("segment", [])
                        if segment_range and len(segment_range) == 2:
                            start, end = segment_range
                            if start <= 2.0 and end > 2.0:
                                offset = float(end)
                                break
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
                "-loglevel error -nostats -reconnect 1 -reconnect_at_eof 1 -reconnect_streamed 1 "
                "-reconnect_delay_max 5 -rw_timeout 15000000 -probesize 64k -analyzeduration 0"
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
                with yt_dlp.YoutubeDL(opts) as ydl:
                    if clean_target.startswith("http"):
                        return ydl.extract_info(clean_target, download=False)
                    return ydl.extract_info(f"ytsearch5:{clean_target} official", download=False)

            info = None
            async with self._extract_semaphore:
                info = await loop.run_in_executor(self.executor, _extract)

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
            if not stream_url:
                formats = info.get("formats", [])
                audio_formats = [f for f in formats if f.get("acodec") != "none"]
                if audio_formats:
                    stream_url = audio_formats[-1].get("url")

            if not stream_url:
                if not fut.done():
                    fut.set_result(None)
                return None

            raw_id = info.get("id", "")
            video_id = str(raw_id) if re.match(r"^[0-9A-Za-z_-]{11}$", str(raw_id)) else None

            start_offset = 0.0
            if video_id and not is_bili:
                start_offset = await self._fetch_sponsorblock_offset(video_id)

            user_agent = self.headers["User-Agent"]
            if is_bili:
                before_opts = (
                    f'-headers "User-Agent: {user_agent}\r\nReferer: https://www.bilibili.com/\r\nOrigin: https://www.bilibili.com\r\n" '
                    f'{reconnect_flags}'
                )
            else:
                before_opts = (
                    f'-headers "User-Agent: {user_agent}\r\n" '
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
                "start_offset": start_offset
            }
            async with self._cache_lock:
                if len(self._stream_cache) > 2000:
                    self._stream_cache.popitem(last=False)
                self._stream_cache[target] = (now, res_dict)

            if not fut.done():
                fut.set_result(res_dict)
            return res_dict
        except Exception:
            if not fut.done():
                fut.set_result(None)
            return None
        finally:
            async with self._flight_lock:
                self._in_flight_streams.pop(target, None)

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

        clean_seed = re.sub(r"[\(\[].*?[\)\]]", "", query_seed).strip()
        search_query = f"ytsearch5:{clean_seed} official audio"
        opts = dict(self.ydl_opts_meta)
        opts["playlistend"] = 5

        def _extract_search():
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(search_query, download=False)

        try:
            info = await loop.run_in_executor(self.executor, _extract_search)
            if info and "entries" in info:
                valid_entries = [
                    e for e in info["entries"]
                    if e and e.get("id") and re.match(r"^[0-9A-Za-z_-]{11}$", str(e.get("id")))
                    and e.get("id") != video_id and e.get("id") not in history_ids
                ]
                if valid_entries:
                    ranked = sorted(
                        valid_entries,
                        key=lambda x: self._score_music_candidate(
                            x.get("title", ""),
                            x.get("uploader", ""),
                            query=clean_seed,
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