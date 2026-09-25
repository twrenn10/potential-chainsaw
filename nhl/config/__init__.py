"""JSON config loader. Configs ship with the package and are versioned by content hash."""

from __future__ import annotations

import json
from functools import lru_cache
from hashlib import sha256
from importlib import resources
from typing import Any


@lru_cache(maxsize=None)
def _raw(name: str) -> str:
    return resources.files(__name__).joinpath(f"{name}.json").read_text(encoding="utf-8")


def load(name: str) -> dict[str, Any]:
    """Return a fresh copy so callers can't mutate the cached config."""

    return json.loads(_raw(name))


def config_hash(name: str) -> str:
    return sha256(_raw(name).encode("utf-8")).hexdigest()[:12]
