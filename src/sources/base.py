"""Base crawler interface."""

from abc import ABC, abstractmethod
from typing import List

import aiohttp

from .. import StreamEntry, SourceType


class BaseCrawler(ABC):
    """Abstract base for all source crawlers."""

    name: str = "base"
    source_type: SourceType = SourceType.RAW_M3U

    @abstractmethod
    async def fetch(self, session: aiohttp.ClientSession) -> List[StreamEntry]:
        """Fetch stream entries from this source.

        Args:
            session: aiohttp session to use for HTTP requests.

        Returns:
            List of StreamEntry objects.
        """
        ...
