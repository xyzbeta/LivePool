"""Generic M3U URL crawler — handles any IPTV m3u8 source type."""

import asyncio
import logging
from typing import List, Optional

import aiohttp

from .base import BaseCrawler
from .. import StreamEntry, SourceType
from ..parser import parse_m3u8_content

logger = logging.getLogger(__name__)


class M3UCrawler(BaseCrawler):
    """Fetches m3u8 files from raw URLs (GitHub, CDN, or any HTTP host).

    The *source_type* parameter tags the fetched entries so downstream
    stages (filter / classify) can distinguish origins.
    """

    source_type = SourceType.RAW_M3U

    def __init__(self, name: str, urls: List[str], timeout: int = 30,
                 source_type: Optional[SourceType] = None):
        self.name = name
        self.urls = urls
        self.timeout = timeout
        if source_type is not None:
            self.source_type = source_type

    async def fetch(self, session: aiohttp.ClientSession) -> List[StreamEntry]:
        entries: List[StreamEntry] = []
        for url in self.urls:
            try:
                logger.info(f"[{self.name}] Fetching: {url}")
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=self.timeout)) as resp:
                    if resp.status != 200:
                        logger.warning(
                            f"[{self.name}] HTTP {resp.status} fetching {url}"
                        )
                        continue
                    content = await resp.text()
                    parsed = parse_m3u8_content(content, source=url, source_type=self.source_type)
                    logger.info(f"[{self.name}] Got {len(parsed)} entries from {url}")
                    entries.extend(parsed)
            except asyncio.TimeoutError:
                logger.error(f"[{self.name}] Timeout fetching {url}")
            except Exception as e:
                logger.error(f"[{self.name}] Error fetching {url}: {e}")
        return entries


# Backward compatible alias
RawM3UCrawler = M3UCrawler
