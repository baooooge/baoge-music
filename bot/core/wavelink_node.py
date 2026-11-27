import os
import logging
from typing import Optional, Dict, List, Any
from discord.ext import commands
import wavelink

logger = logging.getLogger("baoge.wavelink")

LAVALINK_EQ_PRESETS: Dict[str, List[Dict[str, Any]]] = {
    "flat": [],
    "bass": [
        {"band": 0, "gain": 0.22},
        {"band": 1, "gain": 0.20},
        {"band": 2, "gain": 0.16},
        {"band": 3, "gain": 0.10},
    ],
    "bass_extreme": [
        {"band": 0, "gain": 0.38},
        {"band": 1, "gain": 0.35},
        {"band": 2, "gain": 0.28},
        {"band": 3, "gain": 0.18},
        {"band": 4, "gain": 0.08},
    ],
    "vocal": [
        {"band": 0, "gain": -0.12},
        {"band": 1, "gain": -0.12},
        {"band": 2, "gain": -0.12},
        {"band": 7, "gain": 0.10},
        {"band": 8, "gain": 0.18},
        {"band": 9, "gain": 0.20},
        {"band": 10, "gain": 0.16},
        {"band": 11, "gain": 0.10},
        {"band": 13, "gain": -0.08},
        {"band": 14, "gain": -0.08},
    ],
    "treble": [
        {"band": 10, "gain": 0.08},
        {"band": 11, "gain": 0.14},
        {"band": 12, "gain": 0.20},
        {"band": 13, "gain": 0.24},
        {"band": 14, "gain": 0.24},
    ],
}


class WavelinkManager:
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.enabled = False
        self.node_uri = "http://lavalink:2333"
        self.node_password = "youshallnotpass"
        self.node: Optional[wavelink.Node] = None
        self._refresh_config()

    def _refresh_config(self):
        self.enabled = os.getenv("LAVALINK_ENABLED", "false").lower() in ("true", "1", "yes")
        self.node_uri = os.getenv("LAVALINK_URI", "http://lavalink:2333")
        self.node_password = os.getenv("LAVALINK_PASSWORD", "youshallnotpass")

    async def _try_connect_uri(self, uri: str) -> wavelink.Node:
        node = wavelink.Node(
            uri=uri,
            password=self.node_password,
            resume_timeout=60,
            inactive_player_timeout=None,
        )
        await wavelink.Pool.connect(nodes=[node], client=self.bot, cache_capacity=100)
        return node

    async def connect_node(self) -> Optional[wavelink.Node]:
        self._refresh_config()
        if not self.enabled:
            return None

        uris = [self.node_uri]
        if not os.path.exists("/.dockerenv") and "://lavalink:" in self.node_uri:
            uris.append(self.node_uri.replace("://lavalink:", "://127.0.0.1:", 1))

        for uri in uris:
            try:
                node = await self._try_connect_uri(uri)
                self.node = node
                self.node_uri = uri
                logger.info("Successfully connected to Lavalink node: %s", uri)
                return node
            except Exception as e:
                logger.warning("Failed to connect to Lavalink node (%s): %s", uri, e)
                try:
                    await wavelink.Pool.close()
                except Exception:
                    pass
                self.node = None
        return None

    async def close(self):
        try:
            await wavelink.Pool.close()
        except Exception:
            pass
        self.node = None

    def is_connected(self) -> bool:
        return (
            self.node is not None
            and getattr(self.node, "status", None) is wavelink.NodeStatus.CONNECTED
        )
