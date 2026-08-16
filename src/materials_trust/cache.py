"""Verbatim on-disk cache for API payloads.

Brief rule 5.2: API pulls are slow and rate limited, so cache to disk and make
reruns cheap. Payloads are stored exactly as the API returned them, under
``data/raw``, so that any number in the final report can be traced back to the
bytes that produced it. Parsing happens on read, never on write, which means a
parser bug can be fixed and the benchmark rebuilt without touching the network.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config


def make_key(**params: Any) -> str:
    """A stable hash of the query parameters that produced a payload."""
    canonical = json.dumps(params, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


@dataclass
class DiskCache:
    """A namespaced cache of verbatim API payloads.

    ``enabled=False`` forces every read to miss, which the reconnaissance script
    uses to check live API behaviour rather than yesterday's copy of it.
    """

    namespace: str
    root: Path = config.RAW_DIR
    enabled: bool = True

    def __post_init__(self) -> None:
        self.dir = Path(self.root) / self.namespace
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, kind: str, key: str) -> Path:
        sub = self.dir / kind
        sub.mkdir(parents=True, exist_ok=True)
        return sub / f"{key}.json"

    def get(self, kind: str, key: str) -> Any | None:
        if not self.enabled:
            return None
        path = self._path(kind, key)
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as fh:
                envelope = json.load(fh)
        except (OSError, json.JSONDecodeError):
            # A corrupt cache entry is discarded rather than trusted. It will be
            # refetched, which is always safe because payloads are immutable.
            return None
        return envelope.get("payload")

    def put(self, kind: str, key: str, payload: Any, query: Any = None) -> Path:
        path = self._path(kind, key)
        envelope = {
            "namespace": self.namespace,
            "kind": kind,
            "key": key,
            "query": query,
            "stored_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "payload": payload,
        }
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(envelope, fh, default=str)
        tmp.replace(path)
        return path

    def has(self, kind: str, key: str) -> bool:
        return self.enabled and self._path(kind, key).exists()

    def stats(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        if self.dir.exists():
            for sub in sorted(p for p in self.dir.iterdir() if p.is_dir()):
                counts[sub.name] = len(list(sub.glob("*.json")))
        return counts
