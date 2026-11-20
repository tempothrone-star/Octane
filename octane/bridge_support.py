"""Validation helpers shared by the fixed OpenClaw operation handlers."""
from __future__ import annotations
from pathlib import Path
from typing import Any

class BridgeError(ValueError):
    """A safe, user-facing bridge validation error."""


class _CheckoutRequired(BridgeError):
    pass


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _required(params: dict[str, Any], key: str, expected: type) -> Any:
    value = params.get(key)
    if not isinstance(value, expected) or (expected is int and type(value) is not int):
        raise BridgeError(f"{key} must be {expected.__name__}")
    return value


