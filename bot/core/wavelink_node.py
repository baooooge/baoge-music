import os
import logging
from typing import Optional
import discord
from discord.ext import commands
import wavelink

logger = logging.getLogger("baoge.wavelink")

class WavelinkManager:
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.enabled = os.getenv("LAVALINK_ENABLED", "false").lower() in ("true", "1", "yes")
        self.node_uri = os.getenv("LAVALINK_URI", "http://lavalink:2333")
        self.node_password = os.getenv("LAVALINK_PASSWORD", "youshallnotpass")
        self.node: Optional[wavelink.Node] = None

    async def connect_node(self) -> Optional[wavelink.Node]:
        if not self.enabled:
            return None
        try:
            node = wavelink.Node(
                uri=self.node_uri,
                password=self.node_password,
                inactive_timeout=60
            )
            nodes = [node]
            await wavelink.Pool.connect(nodes=nodes, client=self.bot, cache_capacity=100)
            self.node = node
            logger.info("Successfully connected to Lavalink node: %s", self.node_uri)
            return node
        except Exception as e:
            logger.error("Failed to connect to Lavalink node: %s", e)
            return None

    def is_connected(self) -> bool:
        return self.node is not None and self.node.status is wavelink.NodeStatus.CONNECTED
