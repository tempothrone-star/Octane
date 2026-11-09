"""Bounded project-local dependency graph extraction using only the Python stdlib.

Python imports are parsed with the stdlib AST. Other language import forms are
recognized with small conservative patterns; this is not a full grammar parser.
"""
from __future__ import annotations

import ast
import posixpath
import re
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .source import LANGUAGES, source_inventory

# Patterns intentionally capture only common static dependency declarations.
_PATTERNS = {
    "javascript": [
        re.compile(r"\b(?:import|export)\s+(?:[^;\n]*?\s+from\s+)?[\"']([^\"']+)[\"']"),
        re.compile(r"\b(?:require|import)\s*\(\s*[\"']([^\"']+)[\"']\s*\)"),
    ],
    "typescript": [
        re.compile(r"\b(?:import|export)\s+(?:[^;\n]*?\s+from\s+)?[\"']([^\"']+)[\"']"),
        re.compile(r"\b(?:require|import)\s*\(\s*[\"']([^\"']+)[\"']\s*\)"),
    ],
    "go": [re.compile(r"(?m)^\s*(?:import\s+)?[\"']([^\"']+)[\"']")],
    "java": [re.compile(r"(?m)^\s*import\s+(?:static\s+)?([A-Za-z_$][\w$]*(?:\.[A-Za-z_$*][\w$]*)*)\s*;")],
    "rust": [
        re.compile(r"(?m)^\s*mod\s+([A-Za-z_][\w]*)\s*;"),
        re.compile(r"(?m)^\s*use\s+(?:crate|self|super)::([A-Za-z_][\w:]*)"),
    ],
    "c": [re.compile(r'(?m)^\s*#\s*include\s*"([^"\n]+)"')],
    "cpp": [re.compile(r'(?m)^\s*#\s*include\s*"([^"\n]+)"')],
    "csharp": [re.compile(r"(?m)^\s*using\s+([A-Za-z_][\w.]*)\s*;")],
    "php": [
        re.compile(r"\b(?:require|include)(?:_once)?\s*(?:\(\s*)?[\"']([^\"']+)[\"']"),
        re.compile(r"(?m)^\s*use\s+([A-Za-z_][\w\\]*)"),
    ],
    "ruby": [
        re.compile(r"\brequire_relative\s+[\"']([^\"']+)[\"']"),
        re.compile(r"\brequire\s+[\"']([^\"']+)[\"']"),
    ],
    "kotlin": [re.compile(r"(?m)^\s*import\s+([A-Za-z_][\w.*]*)")],
    "scala": [re.compile(r"(?m)^\s*import\s+([A-Za-z_][\w.*]*)")],
    "swift": [re.compile(r"(?m)^\s*import\s+[A-Za-z_][\w]*")],
}

_EXTENSIONS = {
    language: sorted(ext for ext, value in LANGUAGES.items() if value == language)
    for language in set(LANGUAGES.values())
}


def _python_imports(path: str, text: str) -> Tuple[List[Tuple[str, int]], int]:
    try:
        tree = ast.parse(text, filename=path)
    except (SyntaxError, ValueError):
        return [], 1
    results: List[Tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            results.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = ("." * node.level) + (node.module or "")
            if base.strip("."):
                results.append((base, node.lineno))
            for alias in node.names:
                if alias.name != "*":
                    separator = "." if node.module else ""
                    results.append((base + separator + alias.name, node.lineno))
    return results, 0


def _go_module(root: Path) -> Optional[str]:
    path = root / "go.mod"
    try:
        if path.is_symlink() or path.stat().st_size > 1024 * 1024:
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r"(?m)^\s*module\s+([^\s]+)", text)
    return match.group(1) if match else None


def _java_packages(contents: Dict[str, Tuple[str, str]]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for path, (language, text) in contents.items():
        if language != "java":
            continue
        package = re.search(r"(?m)^\s*package\s+([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*;", text)
        if package:
            class_name = PurePosixPath(path).stem
            result[package.group(1) + "." + class_name] = path
    return result


def _candidate_paths(spec: str, source_path: str, language: str, root: Path,
                     contents: Dict[str, Tuple[str, str]], go_module: Optional[str],
                     java_packages: Dict[str, str]) -> List[str]:
    spec = spec.strip()
    candidates: List[str] = []
    source_dir = PurePosixPath(source_path).parent
    if language == "python":
        module = spec.lstrip(".")
        parts = module.split(".") if module else []
        base = PurePosixPath(*parts).as_posix() if parts else ""
        for ext in (".py",):
            candidates.extend([base + ext, base + "/__init__" + ext])
        # Relative imports resolve from the containing package directory.
        if spec.startswith("."):
            depth = len(spec) - len(spec.lstrip("."))
            prefix = source_dir
            for _ in range(max(0, depth - 1)):
                prefix = prefix.parent
            relative = (prefix / base).as_posix()
            candidates.extend([relative + ".py", relative + "/__init__.py"])
    elif language in ("javascript", "typescript") or (language == "ruby" and spec.startswith(".")):
        base = (source_dir / spec).as_posix()
        candidates.append(base)
        for ext in (".ts", ".tsx", ".mts", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte", ".json", ".sql"):
            candidates.append(base + ext)
            candidates.append((PurePosixPath(base) / ("index" + ext)).as_posix())
    elif language in ("c", "cpp"):
        base = (source_dir / spec).as_posix()
        candidates.append(base)
        candidates.extend([(source_dir / spec).as_posix()])
    elif language == "go":
        module_prefix = (go_module + "/") if go_module else None
        if module_prefix and spec.startswith(module_prefix):
            package_dir = spec[len(module_prefix):]
            candidates.extend(path for path, (lang, _text) in contents.items()
                              if lang == "go" and PurePosixPath(path).parent.as_posix() == package_dir)
    elif language == "java":
        target = java_packages.get(spec)
        if target:
            candidates.append(target)
        elif spec.endswith(".*"):
            package = spec[:-2]
            candidates.extend(path for path in java_packages.values()
                              if re.search(r"(?m)^\s*package\s+" + re.escape(package) + r"\s*;", contents[path][1]))
    elif language == "rust":
        # `mod foo;` refers to foo.rs or foo/mod.rs adjacent to the containing module.
        module = spec.split("::", 1)[0]
        base_dir = source_dir
        if PurePosixPath(source_path).name == "mod.rs":
            base_dir = source_dir
        base = (base_dir / module).as_posix()
        candidates.extend([base + ".rs", (PurePosixPath(base) / "mod.rs").as_posix()])
    elif language == "php":
        base = (source_dir / spec).as_posix()
        candidates.extend([base, base + ".php"])
    elif language == "ruby" and not spec.startswith("."):
        base = spec.replace("::", "/")
        candidates.extend([base + ".rb", (source_dir / (base + ".rb")).as_posix()])
    elif language in ("kotlin", "scala", "csharp"):
        # Match known package/namespace declarations; avoids assuming arbitrary
        # external package names are project files.
        dotted = spec.replace(".*", "")
        for path, (lang, text) in contents.items():
            if lang != language:
                continue
            if re.search(r"(?m)^\s*(?:package|namespace)\s+" + re.escape(dotted) + r"(?:\s|;|$)", text):
                candidates.append(path)
    # Normalize imports such as ../shared/header.h and resolve only files already
    # inside the bounded inventory. Never use a reference to read outside root.
    seen: Set[str] = set()
    resolved: List[str] = []
    for path in candidates:
        normalized = posixpath.normpath(path.replace("\\\\", "/"))
        if normalized.startswith("../") or normalized == ".." or normalized.startswith("/"):
            continue
        if normalized in contents and normalized not in seen:
            seen.add(normalized)
            resolved.append(normalized)
    return resolved


def build_dependency_graph(root: Path, max_files: int = 100000,
                           max_file_bytes: int = 5 * 1024 * 1024,
                           max_total_bytes: int = 100 * 1024 * 1024) -> Dict[str, Any]:
    """Extract a bounded, best-effort internal file dependency graph.

    This deliberately does not attempt complete language parsing. It reports
    unresolved references and never turns a dependency edge into a cause claim.
    """
    root = root.resolve()
    inventory = source_inventory(root, max_files, max_file_bytes)
    if not 1 <= max_total_bytes <= 1024 * 1024 * 1024:
        raise ValueError("max_total_bytes must be 1..1073741824")
    contents: Dict[str, Tuple[str, str]] = {}
    total_bytes = 0
    skipped_total = 0
    for item in inventory["files"]:
        path = root / item["path"]
        size = item["bytes"]
        if total_bytes + size > max_total_bytes:
            skipped_total += 1
            continue
        try:
            if path.is_symlink():
                continue
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        contents[item["path"]] = (item["language"], text)
        total_bytes += size
    module = _go_module(root)
    java_map = _java_packages(contents)
    edges: List[Dict[str, Any]] = []
    unresolved: List[Dict[str, Any]] = []
    parse_errors = 0
    for path, (language, text) in contents.items():
        if language == "python":
            imports, errors = _python_imports(path, text)
            parse_errors += errors
        else:
            imports = []
            for pattern in _PATTERNS.get(language, []):
                imports.extend((match.group(1) if match.lastindex else match.group(0).split()[-1],
                                text.count("\n", 0, match.start()) + 1)
                               for match in pattern.finditer(text))
        seen_specs: Set[Tuple[str, int]] = set()
        for spec, line in imports:
            marker = (spec, line)
            if marker in seen_specs:
                continue
            seen_specs.add(marker)
            targets = _candidate_paths(spec, path, language, root, contents, module, java_map)
            if targets:
                for target in targets:
                    if target == path:
                        continue
                    target_language = contents[target][0]
                    edges.append({
                        "source": path, "target": target, "kind": "imports",
                        "source_language": language, "target_language": target_language,
                        "cross_language": language != target_language,
                        "line": line, "reference": spec,
                        "basis": "Python AST" if language == "python" else "recognized import/include pattern",
                    })
            else:
                unresolved.append({"source": path, "language": language, "line": line, "reference": spec})
    # Remove duplicate equivalent edges while preserving deterministic order.
    deduped: Dict[Tuple[str, str, str, int], Dict[str, Any]] = {}
    for edge in edges:
        key = (edge["source"], edge["target"], edge["reference"], edge["line"])
        deduped[key] = edge
    edges = sorted(deduped.values(), key=lambda edge: (edge["source"], edge["line"], edge["target"], edge["reference"]))
    unresolved.sort(key=lambda item: (item["source"], item["line"], item["reference"]))
    languages = sorted({lang for lang, _text in contents.values()})
    return {
        "root": str(root),
        "languages": languages,
        "files_indexed": len(contents),
        "bytes_read": total_bytes,
        "edges": edges,
        "edge_count": len(edges),
        "cross_language_edge_count": sum(bool(edge["cross_language"]) for edge in edges),
        "unresolved_reference_count": len(unresolved),
        "unresolved_references": unresolved[:1000],
        "parse_error_count": parse_errors,
        "truncated": inventory["truncated"] or skipped_total > 0,
        "skipped_for_total_byte_limit": skipped_total,
        "limits": {"max_files": max_files, "max_file_bytes": max_file_bytes,
                   "max_total_bytes": max_total_bytes},
        "parser_coverage": {
            "full_stdlib_parser": ["python"],
            "heuristic_import_patterns": sorted(_PATTERNS),
            "no_full_parser": sorted(set(languages) - {"python"} - set(_PATTERNS)),
        },
        "warnings": [
            "This is a best-effort file dependency graph, not a full language parser or complete call graph.",
            "Dynamic imports, macros, conditional compilation, generated code, aliases, and unresolved module conventions may be missed or misresolved.",
            "A dependency edge does not establish runtime reachability or defect causation.",
        ],
    }
