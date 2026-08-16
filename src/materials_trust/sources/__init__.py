"""Data source adapters.

Each module here talks to exactly one source and returns ``PropertyRecord``
objects with complete provenance. Comparison logic lives elsewhere: a source
module never decides whether two values agree, and never converts a value it
cannot justify converting.
"""

from __future__ import annotations

__all__ = ["materials_project", "oqmd", "experimental"]
