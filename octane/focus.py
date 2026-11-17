"""Read-only guidance for a failing symbol or file without project registration."""
from __future__ import annotations

from pathlib import Path

from .analysis import analyzer
from .call_plot import call_plot
from .python_calls import analyze_python
from .syntax_calls import analyze_syntax_calls

_MAX_FILE = 256 * 1024
_MAX_BYTES = 2 * 1024 * 1024
_MAX_FILES = 64


def _source_scope(root, path, source):
    if path is not None:
        requested = root / path
        resolved = requested.resolve()
        if not resolved.is_relative_to(root):
            raise PermissionError("focus path is outside the selected checkout")
        relative = resolved.relative_to(root)
        if (requested.is_symlink() or requested != resolved or not resolved.is_file()
                or any(part in source.EXCLUDED_DIRS for part in relative.parts)):
            raise ValueError("focus path must name a regular project source file")
        language = next((value for suffix, value in sorted(source.LANGUAGES.items(), key=lambda entry: -len(entry[0]))
                         if resolved.name.lower().endswith(suffix.lower())), None)
        if language is None:
            raise ValueError("focus path has no recognized source language")
        size = resolved.stat().st_size
        if size > _MAX_FILE:
            raise ValueError("focus path exceeds the 256 KiB per-file limit")
        return [{"path": relative.as_posix(), "language": language, "bytes": size}], False

    inventory = source.source_inventory(root, max_files=1200, max_file_bytes=_MAX_FILE)
    files = [item for item in inventory["files"] if item["language"] not in {"markdown", "json", "yaml", "toml", "restructuredtext"}]
    files.sort(key=lambda item: (item["path"].split("/", 1)[0] in {"test", "tests", "examples", "docs", "benchmarks"}, item["path"]))
    partial = inventory["truncated"] or bool(inventory["skipped"].get("over_file_size_limit")) or len(files) > _MAX_FILES
    return files[:_MAX_FILES], partial


def repair_focus(root: Path, path: str | None, query: str | None, limit: int) -> dict:
    if query is not None and (not isinstance(query, str) or not 1 <= len(query.strip()) <= 160):
        raise ValueError("focus query must be a symbol name of 1 to 160 characters")
    if path is None and query is None:
        raise ValueError("focus requires a source path or symbol name")
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("focus limit must be an integer from 1 to 20")
    query = query.strip().replace("::", ".").casefold() if query else None
    source, polygraph = analyzer()
    items, truncated = _source_scope(root, path, source)
    total, examined, contents, files = 0, 0, {}, []
    marker = query.rsplit(".", 1)[-1] if query and path is None else ""
    ascii_marker = marker.encode("ascii") if marker.isascii() else None
    for item in items:
        candidate = root / item["path"]
        if candidate.is_symlink() or candidate.resolve() != candidate:
            truncated = True
            continue
        if total + item["bytes"] > _MAX_BYTES:
            truncated = True
            break
        try:
            with candidate.open("rb") as stream:
                raw = stream.read(min(_MAX_FILE, _MAX_BYTES - total) + 1)
        except OSError:
            truncated = True
            continue
        examined += 1
        total += len(raw)
        if len(raw) != item["bytes"]:
            truncated = True
            continue
        if marker and (ascii_marker not in raw.lower() if ascii_marker is not None
                       else marker not in raw.decode("utf-8", "replace").casefold()):
            continue
        contents[item["path"]] = raw
        files.append(item)

    graph = polygraph.build_dependency_graph(root, snapshot=({"files": files, "truncated": False}, contents))
    python = {name: raw for name, raw in contents.items() if name.lower().endswith((".py", ".pyi"))}
    symbols, calls, errors = analyze_python(python, "working-copy")
    extra, extra_calls, coverage = analyze_syntax_calls(polygraph.ast_backend, files, contents, "working-copy", graph["edges"])
    symbols += extra
    calls += extra_calls
    matches = [node for node in symbols if query is None or query in node["name"].casefold()]
    matches.sort(key=lambda node: (node["name"].casefold() != query,
                                   node["name"].casefold().rsplit(".", 1)[-1] != query,
                                   node["path"], node["line"], node["node_key"]))
    by_key = {node["node_key"]: node for node in symbols}
    compact = lambda node: {key: node[key] for key in ("node_key", "name", "path", "line", "language")}
    selected = {node["node_key"]: compact(node) for node in matches[:limit]}
    related, diagram, connections = dict(selected), {}, []
    for edge in calls:
        begin, end = edge["source"], edge["target"]
        if (begin not in selected and end not in selected) or begin not in by_key or end not in by_key:
            continue
        key = (begin, end, edge["metadata"]["line"])
        if key in diagram:
            continue
        if len(connections) >= 2 * limit:
            truncated = True
            break
        caller, callee = compact(by_key[begin]), compact(by_key[end])
        related[begin], related[end] = caller, callee
        diagram[key] = None
        connections.append({"caller": caller, "callee": callee, "line": key[2], "basis": edge["metadata"]["basis"]})
    return {"status": "ready" if selected else "no_matching_symbol", "files_examined": examined,
            "files_analyzed": len(contents), "bytes_read": total,
            "symbols": list(selected.values()), "connections": connections, "mermaid": call_plot(related, diagram),
            "coverage": coverage, "python_parse_errors": len(errors), "truncated": truncated or len(matches) > limit,
            "basis": "Bounded static candidates from the working copy. Confirm the path with a focused reproduction."}
