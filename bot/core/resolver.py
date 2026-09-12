import asyncio
import json
import re
from typing import Dict, Any, List, Optional
import aiohttp
from bs4 import BeautifulSoup
import yt_dlp

class UniversalResolver:
    def __init__(self):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7"
        }
        self.ydl_opts_meta = {
            "format": "bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "default_search": "ytsearch",
            "extract_flat": "in_playlist",
            "playlistend": 100,
            "http_headers": self.headers
        }
        self.ydl_opts_stream = {
            "format": "bestaudio/best",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
            "http_headers": self.headers
        }

    async def get_search_suggestions(self, current: str) -> List[str]:
        if not current or current.startswith("http"):
            return []
        url = f"https://suggestqueries.google.com/complete/search?client=youtube&ds=yt&q={current}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=2) as resp:
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
        if "streetvoice.com" in query:
            return await self._resolve_streetvoice(query)
        if "kkbox.com" in query:
            return await self._resolve_kkbox(query)
        if "open.spotify.com" in query:
            return await self._resolve_spotify(query)
        if "music.apple.com" in query:
            return await self._resolve_apple_music(query)
        return await self._resolve_raw_search(query)

    async def _resolve_streetvoice(self, url: str) -> List[Dict[str, Any]]:
        clean_url = url.split("?")[0]

        async with aiohttp.ClientSession(headers=self.headers) as session:
            async with session.get(clean_url) as resp:
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

    async def _resolve_kkbox(self, url: str) -> List[Dict[str, Any]]:
        clean_url = url.split("?")[0]
        match = re.search(r"kkbox\.com/(?:[a-z]{2}/[a-z]{2}/)?(track|song|album|playlist)/([a-zA-Z0-9_\-]+)", clean_url)
        if not match:
            return []

        item_type, item_id = match.groups()
        widget_type = "song" if item_type in ["track", "song"] else item_type
        widget_url = f"https://widget.kkbox.com/v1/?id={item_id}&type={widget_type}&terr=TW&lang=TC"

        async with aiohttp.ClientSession(headers=self.headers) as session:
            async with session.get(widget_url) as resp:
                if resp.status != 200:
                    return []
                html = await resp.text()

        results = []
        soup = BeautifulSoup(html, "html.parser")

        script = soup.find("script", id="__NEXT_DATA__")
        if script and script.string:
            try:
                data = json.loads(script.string)
                props = data.get("props", {}).get("pageProps", {})
                init_data = props.get("initData", {})

                if widget_type == "song":
                    song_name = init_data.get("name") or init_data.get("song_name") or ""
                    artist_name = init_data.get("artist_name") or init_data.get("artist", {}).get("name") or ""
                    q = f"{song_name} {artist_name}".strip() if artist_name else song_name
                    if q:
                        return [{"title": q, "search_query": q, "webpage_url": clean_url}]
                else:
                    tracks = init_data.get("tracks", {}).get("data", []) or init_data.get("tracks", []) or []
                    for t in tracks[:100]:
                        s_name = t.get("name") or t.get("song_name") or ""
                        a_name = t.get("artist_name") or t.get("artist", {}).get("name") or ""
                        q = f"{s_name} {a_name}".strip() if a_name else s_name
                        if q and q not in [r["title"] for r in results]:
                            results.append({"title": q, "search_query": q, "webpage_url": clean_url})
                    if results:
                        return results
            except Exception:
                pass

        track_items = soup.find_all(class_=re.compile(r"track|song-item|item", re.IGNORECASE))
        for item in track_items:
            title_el = item.find(class_=re.compile(r"name|title", re.IGNORECASE))
            artist_el = item.find(class_=re.compile(r"artist|singer", re.IGNORECASE))
            if title_el:
                s_name = title_el.get_text().strip()
                a_name = artist_el.get_text().strip() if artist_el else ""
                q = f"{s_name} {a_name}".strip() if a_name else s_name
                if q and "KKBOX" not in q and q not in [r["title"] for r in results]:
                    results.append({"title": q, "search_query": q, "webpage_url": clean_url})

        if results:
            return results[:100]

        title_tag = soup.find("title")
        if title_tag and title_tag.string:
            raw_title = title_tag.string.strip()
            cleaned = re.sub(r"[\s\-\|]+(KKBOX|Widget|線上音樂).*$", "", raw_title, flags=re.IGNORECASE).strip()
            if cleaned and "KKBOX" not in cleaned:
                return [{"title": cleaned, "search_query": cleaned, "webpage_url": clean_url}]

        return []

    async def _resolve_spotify(self, url: str) -> List[Dict[str, Any]]:
        clean_url = url.split("?")[0]
        match = re.search(r"open\.spotify\.com/(track|album|playlist|artist)/([a-zA-Z0-9]+)", clean_url)
        if not match:
            return []
        item_type, item_id = match.groups()
        embed_url = f"https://open.spotify.com/embed/{item_type}/{item_id}"

        async with aiohttp.ClientSession(headers=self.headers) as session:
            async with session.get(embed_url) as resp:
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
        async with aiohttp.ClientSession(headers=self.headers) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return []
                html = await resp.text()

        soup = BeautifulSoup(html, "html.parser")
        og_title = soup.find("meta", property="og:title")
        if not og_title or not og_title.get("content"):
            return []
        title = og_title["content"].split(" - ")[0]
        return [{"title": title, "search_query": title, "webpage_url": url}]

    def _score_music_candidate(self, title: str, uploader: str) -> int:
        score = 0
        t = title.lower()
        u = uploader.lower()

        negatives = [
            "live", "現場", "演唱會", "concert", "fancam", "直拍", "cover", "翻唱",
            "reaction", "反應", "remix", "慢速", "sped up", "slowed", "bass boosted",
            "1小時", "1 hour", "10 hours", "loop", "inst", "instrumental", "伴奏",
            "karaoke", "純音樂"
        ]
        for neg in negatives:
            if neg in t:
                score -= 50

        if "topic" in u:
            score += 80
        if "vevo" in u:
            score += 60
        if "official audio" in t:
            score += 70
        if "official music video" in t or "official mv" in t:
            score += 50
        if "mv" in t:
            score += 30

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
            opts["playlistend"] = 5
            search_target = f"ytsearch5:{query} official audio"

        def _extract():
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(search_target, download=False)

        try:
            info = await loop.run_in_executor(None, _extract)
            if not info:
                if not is_url:
                    search_target = f"ytsearch5:{query}"
                    info = await loop.run_in_executor(None, _extract)
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
                    key=lambda x: self._score_music_candidate(x.get("title", ""), x.get("uploader", "")),
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
        url = f"https://sponsor.ajay.app/api/skipSegments?videoID={video_id}&categories=[\"music_offtopic\"]"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=2) as resp:
                    if resp.status == 200:
                        segments = await resp.json()
                        for seg in segments:
                            segment_range = seg.get("segment", [])
                            if segment_range and len(segment_range) == 2:
                                start, end = segment_range
                                if start <= 2.0 and end > 2.0:
                                    return float(end)
        except Exception:
            pass
        return 0.0

    async def get_live_stream(self, target: str) -> Optional[Dict[str, Any]]:
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
                    async with aiohttp.ClientSession(headers=sv_headers) as session:
                        async with session.post(api_url) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                file_url = data.get("file")
                                if file_url:
                                    user_agent = self.headers["User-Agent"]
                                    before_opts = (
                                        f'-headers "User-Agent: {user_agent}\r\nReferer: {target}\r\nOrigin: https://streetvoice.com\r\n" '
                                        '-reconnect 1 -reconnect_at_eof 1 -reconnect_streamed 1 -reconnect_delay_max 5'
                                    )
                                    return {
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
                query = clean_target if clean_target.startswith("http") else f"ytsearch1:{clean_target} official audio"
                return ydl.extract_info(query, download=False)

        try:
            info = await loop.run_in_executor(None, _extract)
            if not info:
                return None
            if "entries" in info and info["entries"]:
                info = info["entries"][0]

            stream_url = info.get("url")
            if not stream_url:
                formats = info.get("formats", [])
                audio_formats = [f for f in formats if f.get("acodec") != "none"]
                if audio_formats:
                    stream_url = audio_formats[-1].get("url")

            if not stream_url:
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
                    '-reconnect 1 -reconnect_at_eof 1 -reconnect_streamed 1 -reconnect_delay_max 5'
                )
            else:
                before_opts = (
                    f'-headers "User-Agent: {user_agent}\r\n" '
                    '-reconnect 1 -reconnect_at_eof 1 -reconnect_streamed 1 -reconnect_delay_max 5'
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

            return {
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
        except Exception:
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
                info = await loop.run_in_executor(None, _extract_mix)
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
            info = await loop.run_in_executor(None, _extract_search)
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

        return None

    async def get_lyrics(self, title: str, artist: str = "") -> Optional[str]:
        clean_title = re.sub(r"[\(\[].*?[\)\]]", "", title).strip()
        url = f"https://lrclib.net/api/get?track_name={clean_title}&artist_name={artist}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=5) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("plainLyrics") or data.get("syncedLyrics")
        except Exception:
            pass
        return None