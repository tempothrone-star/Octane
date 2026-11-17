"""Build a project-local call graph from bounded runtime stack observations."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from .call_plot import call_plot

_RUNNERS = {"node": ("node_trace_runner.cjs", "--require"), "ruby": ("ruby_trace_runner.rb", "-r")}
_BASIS = {"node": "V8 CPU stack samples", "ruby": "Ruby TracePoint call stacks",
          "speedscope": "Stacks reported by the profiler run in Docker"}
_MAX_BYTES = 512_000
_MAX_GRAPH_NODES = 100
_MAX_GRAPH_EDGES = 160
_MAX_STACK_DEPTH = 40


def profile_command(command: list[str], kind: str) -> tuple[list[str], str | None]:
    if kind == "speedscope":
        return command, None
    if kind not in _RUNNERS or command[0] != kind:
        raise ValueError("runtimeTrace expects node with a node command, ruby with a ruby command, or speedscope")
    runner, flag = _RUNNERS[kind]
    return [command[0], flag, f"/{runner}", *command[1:]], runner


def _profile_stacks(data):
    """Yield root-first stacks from both Speedscope profile types."""
    if (not isinstance(data, dict) or data.get("$schema") != "https://www.speedscope.app/file-format-schema.json"
            or not isinstance(data.get("shared"), dict) or not isinstance(data.get("profiles"), list)):
        raise ValueError("invalid Speedscope profile")
    frames = data["shared"].get("frames")
    if not isinstance(frames, list) or len(frames) > 10_000:
        raise ValueError("invalid Speedscope frame table")

    def frame(index):
        if type(index) is not int or not 0 <= index < len(frames):
            raise ValueError("invalid Speedscope frame index")
        return frames[index]

    for profile in data["profiles"]:
        if not isinstance(profile, dict):
            raise ValueError("invalid Speedscope profile entry")
        if profile.get("type") == "sampled" and isinstance(profile.get("samples"), list):
            for sample in profile["samples"]:
                if not isinstance(sample, list):
                    raise ValueError("invalid Speedscope stack")
                yield [frame(index) for index in sample[-_MAX_STACK_DEPTH:]], len(sample) > _MAX_STACK_DEPTH
        elif profile.get("type") == "evented" and isinstance(profile.get("events"), list):
            stack = []
            for event in profile["events"]:
                if not isinstance(event, dict):
                    raise ValueError("invalid Speedscope event")
                index = event.get("frame")
                frame(index)
                if event.get("type") == "O":
                    stack.append(index)
                    yield [frames[i] for i in stack[-_MAX_STACK_DEPTH:]], len(stack) > _MAX_STACK_DEPTH
                elif event.get("type") == "C" and stack and stack[-1] == index:
                    stack.pop()
                else:
                    raise ValueError("unbalanced Speedscope events")
            if stack:
                raise ValueError("unclosed Speedscope frames")
        else:
            raise ValueError("unsupported Speedscope profile type")


def _runtime_stacks(data):
    if (not isinstance(data, dict) or type(data.get("truncated")) is not bool
            or not isinstance(data.get("stacks"), list) or len(data["stacks"]) > 256):
        raise ValueError("invalid runtime stack capture")
    for stack in data["stacks"]:
        if not isinstance(stack, list) or len(stack) > _MAX_STACK_DEPTH:
            raise ValueError("invalid runtime stack")
        yield stack, data["truncated"]


def load_profile_trace(path: Path, root: Path, kind: str) -> dict | None:
    try:
        with path.open("rb") as stream:
            data = stream.read(_MAX_BYTES + 1)
        if len(data) > _MAX_BYTES:
            return None
        data = json.loads(data)
        stacks = _profile_stacks(data) if kind == "speedscope" else _runtime_stacks(data)
        return _make_graph(stacks, root, kind)
    except (OSError, ValueError, RecursionError):
        return None


def _make_graph(stacks, root, kind):
    @lru_cache(maxsize=2048)
    def project_path(value):
        try:
            if value.startswith("file:"):
                uri = urlsplit(value)
                if uri.netloc not in {"", "localhost"}:
                    return None
                value = unquote(uri.path)
            relative = PurePosixPath(value)
            if relative.is_absolute():
                if not relative.is_relative_to("/workspace"):
                    return None
                relative = relative.relative_to("/workspace")
            if not relative.parts or ".." in relative.parts or relative.parts[0] in {"node_modules", "vendor", ".git"}:
                return None
            target = (root / relative).resolve()
            if target.is_relative_to(root) and target.is_file():
                return target.relative_to(root).as_posix()
        except (OSError, ValueError, RuntimeError):
            return None
        return None

    def frame(item):
        if not isinstance(item, dict):
            return None
        name, source, line = item.get("name"), item.get("file"), item.get("line")
        if (not isinstance(source, str) or not 1 <= len(source) <= 400 or not source.isprintable()
                or not isinstance(name, str) or not name or not name.isprintable()):
            return None
        relative = project_path(source)
        if relative is None:
            return None
        line = line if type(line) is int and 0 < line <= 10_000_000 else None
        return {"path": relative, "function": name[:160], "line": line}

    entries, calls = {}, {}
    samples, truncated = 0, False
    for index, (stack, clipped) in enumerate(stacks):
        if index >= 4096:
            truncated = True
            break
        truncated |= clipped
        previous = None
        has_frames = False
        for item in stack:
            current = frame(item)
            if current is None:
                previous = None
                continue
            key = (current["path"], current["function"])
            if key not in entries and len(entries) >= _MAX_GRAPH_NODES:
                truncated = True
                previous = None
                continue
            entries.setdefault(key, current)
            has_frames = True
            if previous is not None and previous != key:
                edge = (previous, key, None)
                if edge in calls:
                    calls[edge]["samples"] += 1
                elif len(calls) < _MAX_GRAPH_EDGES:
                    calls[edge] = {"caller": entries[previous], "callee": current, "samples": 1}
                else:
                    truncated = True
            previous = key
        samples += has_frames
    result = {"available": bool(entries), "format": kind, "basis": _BASIS[kind], "samples": samples,
              "truncated": truncated, "entries": list(entries.values()), "calls": list(calls.values()),
              "mermaid": call_plot(entries, calls)}
    if not result["available"]:
        result["reason"] = "No project-local source frames were recorded. Check the probe and profiler source paths."
    return result
