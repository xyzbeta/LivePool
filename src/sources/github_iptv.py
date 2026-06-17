"""GitHub-hosted IPTV m3u8 crawler — thin wrapper around M3UCrawler."""

from .. import SourceType
from .common_sites import M3UCrawler


class GitHubM3UCrawler(M3UCrawler):
    """Fetches m3u8 files from GitHub raw URLs or similar static hosts."""

    source_type = SourceType.GITHUB_M3U
