"""Prepare GDB invocations and turn recorded stacks into a bounded call graph."""
from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath

from .call_plot import call_plot

_SYMBOL = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:(?:::|\.)[A-Za-z_][A-Za-z0-9_]*)*")


def native_trace_command(command: list[str], symbols: list[str]) -> list[str]:
    if (not isinstance(symbols, list) or not 1 <= len(symbols) <= 8
            or any(not isinstance(name, str) or len(name) > 128 or not _SYMBOL.fullmatch(name) for name in symbols)):
        raise ValueError("nativeTraceSymbols needs one to eight simple or qualified function names")
    if len(set(symbols)) != len(symbols):
        raise ValueError("nativeTraceSymbols must be unique")
    binary = PurePosixPath(command[0])
    if binary.is_absolute() or ".." in binary.parts or not command[0].startswith("./") or not binary.parts:
        raise ValueError("native tracing expects a checkout-relative executable starting with ./")
    return ["gdb", "-nx", "-batch", "-return-child-result", "-iex", "set auto-load off", "-iex",
            "set debuginfod enabled off", "-ex", "set disable-randomization off", "-x", "/octane-tracer.py",
            "--args", *command]


def _valid_frame(frame: object) -> bool:
    if not isinstance(frame, dict) or set(frame) != {"function", "path", "line", "inline"}:
        return False
    function, path = frame["function"], frame["path"]
    if not isinstance(function, str) or not 1 <= len(function) <= 160 or not function.isprintable():
        return False
    if not isinstance(path, str) or not 1 <= len(path) <= 350 or not path.isprintable():
        return False
    relative = PurePosixPath(path)
    return (bool(relative.parts) and not relative.is_absolute() and ".." not in relative.parts
            and type(frame["line"]) is int and 1 <= frame["line"] <= 10_000_000 and type(frame["inline"]) is bool)


def load_native_trace(path: Path, symbols: list[str]) -> dict | None:
    try:
        with path.open("rb") as stream:
            raw = stream.read(160_001)
        if len(raw) > 160_000:
            return None
        data = json.loads(raw)
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or set(data) != {"stacks", "hit_symbols", "unavailable_symbols", "truncated"}:
        return None
    stacks, hits, unresolved = data["stacks"], data["hit_symbols"], data["unavailable_symbols"]
    if type(data["truncated"]) is not bool or not isinstance(stacks, list) or len(stacks) > 12:
        return None
    if any(not isinstance(names, list) or len(names) > len(symbols) or any(name not in symbols for name in names)
           for names in (hits, unresolved)) or any(name in unresolved for name in hits):
        return None
    if any(not isinstance(stack, list) or not 1 <= len(stack) <= 12
           or any(frame is not None and not _valid_frame(frame) for frame in stack) for stack in stacks):
        return None
    entries, calls = {}, {}
    for stack in stacks:
        for frame in stack:
            if frame is not None:
                entries.setdefault((frame["path"], frame["function"]), frame)
        for callee, caller in zip(stack, stack[1:]):
            if caller is None or callee is None:
                continue
            source = (caller["path"], caller["function"])
            target = (callee["path"], callee["function"])
            if source != target:
                calls.setdefault((source, target, caller["line"]), {"caller": caller, "callee": callee})
    result = {"available": bool(entries), "symbols": symbols, "hit_symbols": hits, "unavailable_symbols": unresolved,
              "truncated": data["truncated"], "samples": len(stacks), "entries": list(entries.values()),
              "calls": list(calls.values()), "mermaid": call_plot(entries, calls), "basis": "GDB stack samples at requested breakpoints"}
    if not entries:
        result["reason"] = "No project-local stack reached a requested breakpoint. Check the symbols and debug build path."
    return result
