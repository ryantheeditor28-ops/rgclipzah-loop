"""Platform poster registry.

POSTERS maps platform names (config.json "platforms" / meta.json "posts"
keys) to adapter classes. Use get_poster()/get_posters() so callers never
import adapters directly.
"""
from __future__ import annotations

from .base import PosterBase, PosterError
from .facebook import FacebookPoster
from .instagram import InstagramPoster
from .tiktok import TikTokPoster
from .x import XPoster
from .youtube import YouTubePoster

POSTERS: dict[str, type[PosterBase]] = {
    "youtube": YouTubePoster,
    "tiktok": TikTokPoster,
    "instagram": InstagramPoster,
    "facebook": FacebookPoster,
    "x": XPoster,
}


def get_poster(name: str) -> PosterBase:
    """Instantiate the adapter for one platform."""
    try:
        return POSTERS[name]()
    except KeyError:
        raise KeyError(f"unknown platform {name!r}; known: {sorted(POSTERS)}") from None


def get_posters(platforms: list[str]) -> dict[str, PosterBase]:
    """Instantiate adapters for several platforms (duplicates ignored)."""
    return {name: get_poster(name) for name in dict.fromkeys(platforms)}


__all__ = ["POSTERS", "PosterBase", "PosterError", "get_poster", "get_posters"]
