from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TreeLimits:
    max_depth: int
    max_pages: int
    max_files: int


# Production-oriented defaults for bounded whole-site monitoring from a
# homepage or top-level section seed. These are intentionally larger than
# the earlier smoke-style limits so the crawler can cover navigation hubs
# and discover deeper document pages before later incremental runs.
PRODUCTION_TREE_LIMITS = TreeLimits(
    max_depth=4,
    max_pages=120,
    max_files=40,
)
