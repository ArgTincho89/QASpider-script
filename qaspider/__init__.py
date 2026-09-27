"""Standalone Playwright-based website inventory crawler."""

from .crawl import build_element_index, crawl_site
from .inventory import INVENTORY_KEYS, empty_inventory
from .urls import normalize_url, same_origin

__all__ = [
    "INVENTORY_KEYS",
    "build_element_index",
    "crawl_site",
    "empty_inventory",
    "normalize_url",
    "same_origin",
]
