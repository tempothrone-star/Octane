"""Bounded, read-only multilingual source inventory and failure-frame ranking.

This module intentionally does not parse full language grammars or execute project code.
"""
from __future__ import annotations

import os
import re
import subprocess
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

LANGUAGES = {
    ".py": "python", ".pyi": "python", ".pyx": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".java": "java", ".go": "go", ".rs": "rust",
    ".c": "c", ".h": "c", ".cc": "cpp", ".hh": "cpp", ".cpp": "cpp", ".hpp": "cpp", ".cxx": "cpp", ".hxx": "cpp",
    ".cs": "csharp", ".fs": "fsharp", ".fsx": "fsharp", ".vb": "visual_basic",
    ".php": "php", ".rb": "ruby", ".swift": "swift", ".kt": "kotlin", ".kts": "kotlin",
    ".scala": "scala", ".sc": "scala", ".dart": "dart", ".lua": "lua", ".pl": "perl", ".pm": "perl",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell", ".sql": "sql",
    ".html": "html", ".htm": "html", ".css": "css", ".scss": "css", ".less": "css", ".ipynb": "jupyter_notebook",
    ".vue": "vue", ".svelte": "svelte", ".ex": "elixir", ".exs": "elixir", ".erl": "erlang", ".hrl": "erlang",
    ".clj": "clojure", ".cljs": "clojure", ".cljc": "clojure", ".hs": "haskell", ".lhs": "haskell",
    ".elm": "elm", ".r": "r", ".R": "r", ".jl": "julia", ".m": "objective_c_or_matlab",
    ".mm": "objective_c", ".scala": "scala", ".groovy": "groovy", ".gradle": "groovy",
}
EXCLUDED_DIRS = {
    ".git", ".octance", ".hg", ".svn", ".venv", "venv", "env", "__pycache__", ".tox",
    "node_modules", "vendor", "target", "dist", "build", "coverage", ".next", ".nuxt",
    ".svelte-kit", "out", "bin", "obj", "Pods", "DerivedData", "third_party", "third-party",
}

# Common stack-trace shapes. Patterns capture a source path and line number;
# ranking never treats a frame as proof of causation.
_FRAME_PATTERNS = (
    re.compile(r'File ["\'](?P<path>[^"\']+)["\'], line (?P<line>\d+)(?:, in (?P<symbol>[^\r\n]+))?'),  # Python
    re.compile(r"\bat (?:(?P<symbol>[^\r\n(]+) \()? (?P<path>(?:file://)?[^\s()]+?\.(?:m?js|cjs|jsx|tsx?|mts|cts)):(?P<line>\d+):(?P<column>\d+)\)?", re.X),  # JS/TS
    re.compile(r"\bat [^\r\n(]+\((?P<path>[^()]+?\.java):(?P<line>\d+)\)"),  # Java
    re.compile(r"(?P<path>(?:[A-Za-z]:)?[^\s()]+?\.go):(?P<line>\d+)(?::\d+)?(?:\s|$)"),  # Go
    re.compile(r"\bat (?P<path>(?:\./|/|[^\s]+/)[^\s:]+\.rs):(?P<line>\d+)(?::(?P<column>\d+))?"),  # Rust
    re.compile(r"(?P<path>(?:[A-Za-z]:)?[^\s()]+?\.(?:c|h|cc|hh|cpp|hpp|cxx|hxx)):(?P<line>\d+)(?::\d+)?(?::|\s)"),  # C/C++
)

_FRAME_FORMATS = ("python", "javascript", "java", "go", "rust", "c_cpp")


def source_inventory(root: Path, max_files: int = 100000, max_file_bytes: int = 5 * 1024 * 1024) -> Dict[str, Any]:
    """Classify files by extension with strict walk and per-file budgets."""
    root = root.resolve()
    if not root.is_dir():
        raise ValueError("project root must be an existing directory")
    if not 1 <= max_files <= 1_000_000 or not 1 <= max_file_bytes <= 100 * 1024 * 1024:
        raise ValueError("inventory budgets are outside supported bounds")
    counts: Counter[str] = Counter()
    unsupported: Counter[str] = Counter()
    files: List[Dict[str, Any]] = []
    skipped: Counter[str] = Counter()
    visited = 0
    truncated = False
    for current, dirs, names in os.walk(str(root), topdown=True, followlinks=False):
        base = Path(current)
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED_DIRS and not (base / d).is_symlink())
        for name in sorted(names):
            path = base / name
            if path.is_symlink():
                skipped["symlink"] += 1
                continue
            if not path.is_file():
                continue
            visited += 1
            if visited > max_files:
                truncated = True
                break
            language = LANGUAGES.get(path.suffix.lower())
            # .R and .m are case-sensitive in some ecosystems; retain the explicit map.
            if path.suffix in LANGUAGES:
                language = LANGUAGES[path.suffix]
            if language is None:
                if path.suffix:
                    unsupported[path.suffix.lower()] += 1
                continue
            try:
                size = path.stat().st_size
            except OSError:
                skipped["unreadable"] += 1
                continue
            if size > max_file_bytes:
                skipped["over_file_size_limit"] += 1
                continue
            rel = path.relative_to(root).as_posix()
            counts[language] += 1
            files.append({"path": rel, "language": language, "bytes": size})
        if truncated:
            break
    files.sort(key=lambda item: item["path"])
    return {
        "root": str(root), "languages": dict(sorted(counts.items())), "source_files": len(files),
        "unsupported_extensions": dict(sorted(unsupported.items())),
        "source_bytes": sum(item["bytes"] for item in files), "files": files,
        "visited_files": min(visited, max_files), "truncated": truncated,
        "skipped": dict(sorted(skipped.items())),
        "limits": {"max_files": max_files, "max_file_bytes": max_file_bytes},
        "analysis_level": "extension inventory only; no language parser or execution",
    }


def _normalize_frame_path(raw: str, root: Path) -> Optional[str]:
    value = raw.strip().strip("\"'")
    value = value.replace("webpack://", "").replace("file://", "")
    # Node sometimes prefixes a root marker, while browser bundles use webpack:///src/.
    value = value.lstrip("/") if value.startswith("webpack:/") else value
    candidate = Path(value)
    if candidate.is_absolute():
        try:
            return candidate.resolve().relative_to(root).as_posix()
        except (OSError, ValueError):
            normalized = PurePosixPath(value.replace("\\", "/"))
            if ".." in normalized.parts:
                return None
            external = normalized.as_posix()
            if "/src/" in external:
                return "src/" + external.split("/src/", 1)[1]
            return external
    normalized = PurePosixPath(value.replace("\\", "/"))
    if ".." in normalized.parts:
        return None
    return normalized.as_posix()


def parse_stack_frames(text: str, root: Path) -> List[Dict[str, Any]]:
    """Parse ordered file/line hints from common language runtime traces."""
    root = root.resolve()
    frames: List[Dict[str, Any]] = []
    seen = set()
    for line_text in text.splitlines():
        for pattern_index, pattern in enumerate(_FRAME_PATTERNS):
            match = pattern.search(line_text)
            if not match:
                continue
            relative = _normalize_frame_path(match.group("path"), root)
            if not relative:
                # Runtime paths such as /workspace/app/foo.ts often do not map to
                # this machine; preserve the suffix path for exact project matching.
                raw = match.group("path").replace("\\", "/")
                marker = "/src/" if "/src/" in raw else None
                if marker:
                    relative = raw.split(marker, 1)[1]
                    relative = "src/" + relative
                else:
                    relative = PurePosixPath(raw.replace("\\", "/")).as_posix()
            try:
                line_number = int(match.group("line"))
            except (ValueError, IndexError):
                line_number = None
            symbol = match.groupdict().get("symbol")
            key = (relative, line_number, symbol)
            if key not in seen:
                frames.append({"path": relative, "line": line_number, "symbol": symbol.strip() if symbol else None,
                               "format": _FRAME_FORMATS[pattern_index], "trace_line": line_text.strip()[:500]})
                seen.add(key)
            break
    return frames


def recent_project_changes(root: Path, limit: int = 5) -> Tuple[List[str], Optional[str]]:
    """Return unique paths from recent commits, then worktree/index changes."""
    root = root.resolve()
    try:
        probe = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                               capture_output=True, text=True, timeout=2, check=False)
        if probe.returncode or not probe.stdout.strip():
            return [], "Git unavailable; recent-change baseline omitted"
        git_root = Path(probe.stdout.strip()).resolve()
        prefix = root.relative_to(git_root).as_posix()
        prefix = "" if prefix == "." else prefix.rstrip("/") + "/"
        commands = [
            ["git", "-C", str(git_root), "log", "-n", str(limit), "--format=", "--name-only", "--diff-filter=ACMR"],
            ["git", "-C", str(git_root), "diff", "--name-only", "HEAD"],
            ["git", "-C", str(git_root), "diff", "--cached", "--name-only"],
        ]
        ordered: List[str] = []
        seen = set()
        for command in commands:
            result = subprocess.run(command, capture_output=True, text=True, timeout=3, check=False)
            if result.returncode:
                continue
            for raw in result.stdout.splitlines():
                raw = raw.strip().replace("\\", "/")
                if not raw or (prefix and not raw.startswith(prefix)):
                    continue
                rel = raw[len(prefix):] if prefix else raw
                if rel not in seen and ".." not in PurePosixPath(rel).parts:
                    seen.add(rel); ordered.append(rel)
        # Include untracked files without parsing the ambiguous human status format.
        result = subprocess.run(["git", "-C", str(git_root), "ls-files", "--others", "--exclude-standard", "-z"],
                                capture_output=True, timeout=3, check=False)
        if result.returncode == 0:
            for raw in result.stdout.decode("utf-8", "surrogateescape").split("\0"):
                raw = raw.replace("\\", "/")
                if not raw or (prefix and not raw.startswith(prefix)):
                    continue
                rel = raw[len(prefix):] if prefix else raw
                if rel not in seen and ".." not in PurePosixPath(rel).parts:
                    seen.add(rel); ordered.append(rel)
        return ordered, None
    except (OSError, subprocess.TimeoutExpired):
        return [], "Git inspection failed; recent-change baseline omitted"


def _resolve_frame(frame_path: str, root: Path, known: Dict[str, Dict[str, Any]], basename_map: Dict[str, List[str]]) -> Optional[str]:
    if frame_path in known:
        return frame_path
    candidate = Path(frame_path)
    if candidate.is_absolute():
        try:
            rel = candidate.resolve().relative_to(root.resolve()).as_posix()
            if rel in known:
                return rel
        except (OSError, ValueError):
            pass
    if frame_path in known:
        return frame_path
    parts = [part for part in PurePosixPath(frame_path.replace("\\", "/")).parts if part not in ("/", "", ".")]
    for start in range(len(parts)):
        suffix = "/".join(parts[start:])
        if suffix in known:
            return suffix
        matches = [path for path in known if path.endswith("/" + suffix)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            return None
    matches = basename_map.get(PurePosixPath(frame_path).name, [])
    return matches[0] if len(matches) == 1 else None




def _word_tokens(value: str) -> List[str]:
    """Split path punctuation, snake_case, and camelCase into queryable terms."""
    chunks = re.findall(r"[A-Za-z0-9]+", value)
    words: List[str] = []
    for chunk in chunks:
        words.extend(re.findall(r"[A-Z]+(?=[A-Z][a-z]|$)|[A-Z]?[a-z]+|[0-9]+", chunk))
    return [word.casefold() for word in words]

def locate_suspects(root: Path, trace_text: str, query: str = "", limit: int = 20,
                    max_files: int = 100000, max_file_bytes: int = 5 * 1024 * 1024) -> Dict[str, Any]:
    """Rank source-file suspects from stack frames, terms, and recent changes.

    Scores are deterministic triage heuristics, never probabilities or proof.
    """
    root = root.resolve()
    if not 1 <= limit <= 200:
        raise ValueError("limit must be 1..200")
    inventory = source_inventory(root, max_files=max_files, max_file_bytes=max_file_bytes)
    known = {item["path"]: item for item in inventory["files"]}
    basename_map: Dict[str, List[str]] = defaultdict(list)
    for path in known:
        basename_map[PurePosixPath(path).name].append(path)
    frames = parse_stack_frames(trace_text, root)
    resolved: List[Tuple[int, str, Dict[str, Any]]] = []
    for index, frame in enumerate(frames):
        match = _resolve_frame(frame["path"], root, known, basename_map)
        if match:
            resolved.append((index, match, frame))
    python_order = bool(frames) and all(frame.get("format") == "python" for frame in frames)
    if resolved:
        primary_frame_index = max(index for index, _path, _frame in resolved) if python_order else min(index for index, _path, _frame in resolved)
    else:
        primary_frame_index = None
    changed, warning = recent_project_changes(root)
    changed = [path for path in changed if path in known]
    changed_index = {path: index for index, path in enumerate(changed)}
    terms = {term.casefold() for term in _word_tokens(query) if len(term) > 1}
    candidates: Dict[str, Dict[str, Any]] = {}
    for path, item in known.items():
        matching_frames = [(index, frame) for index, frame_path, frame in resolved if frame_path == path]
        frame_hits = len(matching_frames)
        first_frame = min((index for index, _ in matching_frames), default=None)
        last_frame = max((index for index, _ in matching_frames), default=None)
        primary_distance = min((abs(index - primary_frame_index) for index, _ in matching_frames), default=None)
        path_terms = {term.casefold() for term in _word_tokens(path)}
        matched_terms = sorted(terms & path_terms)
        changed_rank = changed_index.get(path)
        score = 0.0
        reasons = []
        if frame_hits:
            proximity = max(0.0, 20.0 - 20.0 * (primary_distance or 0))
            score += 40.0 * frame_hits + proximity
            reasons.append("appears in parsed stack trace (%d frame(s))" % frame_hits)
            if primary_distance == 0:
                reasons.append("closest resolved frame to the likely fault site under this trace format")
        if changed_rank is not None:
            score += 18.0 / (1 + changed_rank)
            reasons.append("changed in the recent project history")
        if matched_terms:
            score += 8.0 * len(matched_terms)
            reasons.append("path matches query term(s): " + ", ".join(matched_terms))
        if score:
            candidates[path] = {"path": path, "language": item["language"], "score": round(score, 3),
                                "frame_hits": frame_hits, "first_frame": first_frame, "last_frame": last_frame,
                                "primary_frame_distance": primary_distance,
                                "recent_change_rank": changed_rank, "matched_terms": matched_terms,
                                "reasons": reasons}
    ranked = sorted(candidates.values(), key=lambda row: (-row["score"], row["path"]))[:limit]
    return {
        "query": query, "suspects": ranked,
        "traceback_frames": frames, "resolved_frame_count": len(resolved),
        "recently_changed_files": changed,
        "inventory": {"source_files": inventory["source_files"], "languages": inventory["languages"],
                      "truncated": inventory["truncated"]},
        "warnings": ([warning] if warning else []) + [
            "Ranking is heuristic triage, not a root-cause diagnosis or calibrated confidence.",
            "Trace data and source files are read only; Octance does not run project code or modify source.",
            "Only extension inventory and common stack-frame formats are supported; framework/vendor mapping is approximate.",
        ],
        "baselines": {
            "traceback_frame_order": list(dict.fromkeys(path for _index, path, _frame in resolved)),
            "traceback_primary_order": list(dict.fromkeys(path for _index, path, _frame in
                (reversed(resolved) if python_order else resolved))),
            "traceback_order_convention": "outer-to-inner; reversed for Python traceback" if python_order else "inner-to-outer or unspecified; display order retained",
            "recent_change_order": changed,
        },
        "analysis_level": "file-level, language-neutral stack/change ranking",
    }
