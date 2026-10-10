import re
import time
from urllib.parse import urlparse
from typing import Tuple, Optional, Dict

ALLOWED_DOMAINS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "bilibili.com",
    "www.bilibili.com",
    "b23.tv",
    "soundcloud.com",
    "www.soundcloud.com",
    "m.soundcloud.com",
    "on.soundcloud.com",
    "spotify.com",
    "open.spotify.com",
    "apple.com",
    "music.apple.com",
    "streetvoice.com",
    "kkbox.com",
    "play.kkbox.com"
}

MAX_QUERY_LENGTH = 150
GUILD_QUEUE_LIMIT = 100
DEFAULT_USER_QUEUE_LIMIT = 100
PATRON_USER_QUEUE_LIMIT = 200
COOLDOWN_WINDOW_SECONDS = 3.0

class SecurityGateway:
    def __init__(self):
        self._user_last_action: Dict[Tuple[int, str], float] = {}
        self._spam_strike: Dict[int, Tuple[float, int]] = {}

    def enforce_cooldown(
        self,
        user_id: int,
        action_type: str = "default",
        window: float = COOLDOWN_WINDOW_SECONDS
    ) -> Tuple[bool, float]:
        now = time.monotonic()
        key = (user_id, action_type)

        strike_time, strike_count = self._spam_strike.get(user_id, (0.0, 0))
        if now - strike_time > 10.0:
            strike_count = 0

        if strike_count >= 5:
            penalty_remaining = 10.0 - (now - strike_time)
            if penalty_remaining > 0:
                return False, penalty_remaining
            strike_count = 0

        last_time = self._user_last_action.get(key, 0.0)
        elapsed = now - last_time

        if elapsed < window:
            strike_count += 1
            self._spam_strike[user_id] = (now, strike_count)
            return False, window - elapsed

        self._user_last_action[key] = now
        if len(self._user_last_action) > 10000:
            self._user_last_action.pop(next(iter(self._user_last_action)), None)
        return True, 0.0

    def sanitize_and_truncate(self, query: str) -> str:
        clean = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", query).strip()
        if len(clean) > MAX_QUERY_LENGTH:
            return clean[:MAX_QUERY_LENGTH].rstrip()
        return clean

    def is_url(self, query: str) -> bool:
        return bool(re.match(r"^https?://", query, re.IGNORECASE))

    def validate_url(self, url: str) -> Tuple[bool, Optional[str]]:
        try:
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https"):
                return False, "INVALID_SCHEME"
            netloc = (parsed.netloc or "").lower().split(":")[0]
            if not netloc:
                return False, "EMPTY_HOST"
            for domain in ALLOWED_DOMAINS:
                if netloc == domain or netloc.endswith("." + domain):
                    return True, None
            return False, "DISALLOWED_DOMAIN"
        except Exception:
            return False, "PARSE_ERROR"

    def check_queue_quota(
        self,
        guild_total_count: int,
        user_track_count: int,
        is_patron: bool
    ) -> Tuple[bool, str]:
        if guild_total_count >= GUILD_QUEUE_LIMIT:
            return False, "GUILD_QUEUE_FULL"
        max_user_limit = PATRON_USER_QUEUE_LIMIT if is_patron else DEFAULT_USER_QUEUE_LIMIT
        if user_track_count >= max_user_limit:
            return False, "USER_QUOTA_EXCEEDED"
        return True, "OK"
