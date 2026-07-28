"""Shared helpers used across capabilities: caching, and eventually
logging setup / config loading utilities that don't belong to any one
capability.
"""

from src.utils.cache import CacheManager

__all__ = ["CacheManager"]
