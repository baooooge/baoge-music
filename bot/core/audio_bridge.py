import os
import re
import sys
import time
import asyncio
import logging
import secrets
from typing import Optional, Dict, Any
from aiohttp import web

logger = logging.getLogger("baoge.audio_bridge")


class AudioBridge:
    """Lightweight internal HTTP bridge that transcodes custom-header/HLS streams to OGG/Opus for Lavalink."""

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        public_base_url: Optional[str] = None,
        token_ttl: float = 900.0,
    ):
        self.host = host or os.getenv("AUDIO_BRIDGE_HOST", "0.0.0.0")
        self.port = int(port or os.getenv("AUDIO_BRIDGE_PORT", "8765"))
        self.token_ttl = token_ttl
        self.public_base_url = (public_base_url or self._detect_public_base_url(self.port)).rstrip("/")
        self._tokens: Dict[str, Dict[str, Any]] = {}
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._cache_dir = os.path.join(
            "/app/uploads" if os.path.exists("/app/uploads") else "uploads",
            "cache",
        )

    @staticmethod
    def _detect_public_base_url(port: int) -> str:
        env_url = os.getenv("AUDIO_BRIDGE_PUBLIC_URL", "").strip()
        if env_url:
            return env_url
        if os.path.exists("/.dockerenv"):
            return f"http://baoge-music:{port}"
        return f"http://127.0.0.1:{port}"

    @property
    def is_running(self) -> bool:
        return self._runner is not None and self._site is not None

    @staticmethod
    def _find_ffmpeg() -> str:
        if sys.platform == "win32":
            repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            for c in (
                os.path.join(os.getcwd(), "ffmpeg.exe"),
                os.path.join(repo_root, "ffmpeg.exe"),
                "ffmpeg.exe",
                "ffmpeg",
            ):
                if os.path.isfile(c):
                    return c
        return "ffmpeg"

    def _prune_tokens(self):
        now = time.monotonic()
        expired = [k for k, v in self._tokens.items() if (now - v["created_at"]) > self.token_ttl]
        for k in expired:
            self._tokens.pop(k, None)
        while len(self._tokens) > 500:
            oldest_key = next(iter(self._tokens))
            self._tokens.pop(oldest_key, None)

    def register_stream(
        self,
        stream_url: str,
        http_headers: Optional[Dict[str, str]] = None,
        track_id: Optional[str] = None,
    ) -> str:
        self._prune_tokens()
        token = secrets.token_urlsafe(24)
        self._tokens[token] = {
            "stream_url": stream_url,
            "http_headers": dict(http_headers or {}),
            "track_id": str(track_id) if track_id else None,
            "created_at": time.monotonic(),
        }
        return f"{self.public_base_url}/stream/{token}.ogg"

    def _cleanup_bridge_cache(self, max_bytes: int = 500 * 1024 * 1024):
        if not os.path.isdir(self._cache_dir):
            return
        try:
            entries = []
            total_size = 0
            for f in os.listdir(self._cache_dir):
                if not (f.startswith("bridge_") and f.endswith(".ogg")):
                    continue
                fp = os.path.join(self._cache_dir, f)
                if os.path.isfile(fp):
                    st = os.stat(fp)
                    total_size += st.st_size
                    entries.append((st.st_mtime, st.st_size, fp))
            if total_size > max_bytes:
                entries.sort(key=lambda x: x[0])
                target_bytes = int(max_bytes * 0.75)
                for _, size, path in entries:
                    try:
                        os.remove(path)
                        total_size -= size
                    except Exception:
                        pass
                    if total_size <= target_bytes:
                        break
        except Exception:
            pass

    def _get_cache_paths(self, track_id: Optional[str]) -> tuple[Optional[str], Optional[str]]:
        if not track_id:
            return None, None
        safe_id = re.sub(r"[^0-9A-Za-z_-]", "_", str(track_id))[:80]
        if not safe_id:
            return None, None
        cache_path = os.path.join(self._cache_dir, f"bridge_{safe_id}.ogg")
        part_path = os.path.join(self._cache_dir, f"bridge_{safe_id}_{secrets.token_hex(4)}.ogg.part")
        return cache_path, part_path

    def _build_ffmpeg_args(self, stream_url: str, http_headers: Dict[str, str]) -> list[str]:
        args = [self._find_ffmpeg(), "-loglevel", "fatal", "-nostats"]
        hdr_lines = []
        for k, v in (http_headers or {}).items():
            kl = str(k).strip().lower()
            if kl in ("host", "accept-encoding", "content-length"):
                continue
            clean_k = re.sub(r"[\r\n:]", "", str(k)).strip()
            clean_v = re.sub(r"[\r\n]", "", str(v)).strip()
            if clean_k and clean_v:
                hdr_lines.append(f"{clean_k}: {clean_v}")
        if hdr_lines and stream_url.startswith(("http://", "https://")):
            args.extend(["-headers", "\r\n".join(hdr_lines) + "\r\n"])

        if stream_url.startswith(("http://", "https://")):
            args.extend([
                "-reconnect", "1",
                "-reconnect_streamed", "1",
                "-reconnect_on_network_error", "1",
                "-reconnect_on_http_error", "4xx,5xx",
                "-reconnect_delay_max", "5",
            ])

        args.extend([
            "-i", stream_url,
            "-vn", "-sn", "-dn",
            "-c:a", "libopus",
            "-b:a", "128k",
            "-ar", "48000",
            "-ac", "2",
            "-f", "ogg",
            "pipe:1",
        ])
        return args

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "tokens": len(self._tokens)})

    async def _handle_stream(self, request: web.Request) -> web.StreamResponse:
        raw_token = request.match_info.get("token", "")
        token = raw_token[:-4] if raw_token.endswith(".ogg") else raw_token
        entry = self._tokens.get(token)
        if not entry or (time.monotonic() - entry["created_at"]) > self.token_ttl:
            self._tokens.pop(token, None)
            return web.Response(status=404, text="Stream token not found or expired")

        cache_path, part_path = self._get_cache_paths(entry.get("track_id"))
        if cache_path and os.path.isfile(cache_path) and os.path.getsize(cache_path) > 4096:
            return web.FileResponse(cache_path, headers={"Content-Type": "audio/ogg"})

        if request.method == "HEAD":
            return web.Response(
                status=200,
                headers={"Content-Type": "audio/ogg", "Cache-Control": "no-store"},
            )

        args = self._build_ffmpeg_args(entry["stream_url"], entry["http_headers"])
        proc = None
        fh = None
        resp = None
        completed_cleanly = False

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )

            assert proc.stdout is not None
            first_chunk = await proc.stdout.read(65536)
            if not first_chunk:
                await proc.wait()
                return web.Response(status=502, text="Upstream transcode returned empty stream")

            resp = web.StreamResponse(
                status=200,
                headers={"Content-Type": "audio/ogg", "Cache-Control": "no-store"},
            )
            await resp.prepare(request)

            if part_path:
                try:
                    os.makedirs(self._cache_dir, exist_ok=True)
                    fh = open(part_path, "wb")
                except Exception:
                    fh = None

            chunk = first_chunk
            while chunk:
                if fh is not None:
                    try:
                        fh.write(chunk)
                    except Exception:
                        try:
                            fh.close()
                        except Exception:
                            pass
                        fh = None
                await resp.write(chunk)
                chunk = await proc.stdout.read(65536)

            rc = await proc.wait()
            completed_cleanly = (rc == 0)
            await resp.write_eof()
            return resp
        except (asyncio.CancelledError, ConnectionError, BrokenPipeError):
            return resp if resp is not None else web.Response(status=499)
        except Exception as e:
            err_lower = str(e).lower()
            err_type = type(e).__name__.lower()
            if (
                "connection lost" in err_lower
                or "closing transport" in err_lower
                or "clientconnectionreseterror" in err_type
            ):
                return resp if resp is not None else web.Response(status=499)
            logger.warning("AudioBridge stream error: %s", e)
            return web.Response(status=502, text="Upstream transcode error")
        finally:
            if proc is not None and proc.returncode is None:
                try:
                    proc.terminate()
                    await asyncio.wait_for(proc.wait(), timeout=1.0)
                except Exception:
                    try:
                        proc.kill()
                        await asyncio.wait_for(proc.wait(), timeout=1.0)
                    except Exception:
                        pass
            if proc is not None:
                try:
                    if getattr(proc, "_transport", None) is not None:
                        proc._transport.close()
                except Exception:
                    pass
            if fh is not None:
                try:
                    fh.close()
                except Exception:
                    pass
            if part_path and os.path.isfile(part_path):
                if completed_cleanly and cache_path and os.path.getsize(part_path) > 4096:
                    try:
                        os.replace(part_path, cache_path)
                        asyncio.create_task(asyncio.to_thread(self._cleanup_bridge_cache))
                    except Exception:
                        try:
                            os.remove(part_path)
                        except Exception:
                            pass
                else:
                    try:
                        os.remove(part_path)
                    except Exception:
                        pass

    async def start(self) -> bool:
        if self.is_running:
            return True
        try:
            app = web.Application()
            app.router.add_get("/health", self._handle_health)
            app.router.add_get("/stream/{token}", self._handle_stream)
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            site = web.TCPSite(runner, self.host, self.port)
            await site.start()
            self._runner = runner
            self._site = site
            logger.info("AudioBridge listening on %s:%s (public: %s)", self.host, self.port, self.public_base_url)
            return True
        except Exception as e:
            logger.error("Failed to start AudioBridge: %s", e)
            await self.stop()
            return False

    async def stop(self):
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:
                pass
        self._runner = None
        self._site = None
        self._tokens.clear()
