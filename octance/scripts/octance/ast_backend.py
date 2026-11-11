"""Optional, offline Tree-sitter syntax-tree backend for Octance.

The default CLI does not require third-party packages. Installing the optional
``ast`` extra bundles a set of grammars locally; parsing never fetches grammars
or executes project code. Grammar coverage depends on the bundled wheel.
"""
from __future__ import annotations

import re
import warnings
from functools import lru_cache
from typing import List, Optional, Tuple

try:
    from tree_sitter_languages import get_parser as _get_parser
except ImportError:  # The optional parser extra is intentionally not required.
    _get_parser = None

# This wheel bundles compiled grammars. Keep this mapping explicit rather than
# loading parser code, grammar paths, or plugins supplied by analyzed projects.
_GRAMMARS = {
    "bash": "bash", "shell": "bash", "c": "c", "cpp": "cpp", "csharp": "c_sharp",
    "css": "css", "dockerfile": "dockerfile", "elisp": "elisp", "elixir": "elixir",
    "elm": "elm", "embedded_template": "embedded_template", "erlang": "erlang", "fortran": "fortran",
    "go": "go", "haskell": "haskell", "hack": "hack", "hcl": "hcl", "html": "html", "java": "java",
    "javascript": "javascript", "julia": "julia", "json": "json", "kotlin": "kotlin",
    "lisp": "commonlisp", "lua": "lua", "make": "make", "markdown": "markdown", "ocaml": "ocaml",
    "objective_c": "objc", "perl": "perl", "php": "php", "python": "python", "ql": "ql", "r": "r",
    "restructuredtext": "rst", "ruby": "ruby", "rust": "rust", "scala": "scala",
    "sql": "sql", "sqlite": "sqlite", "toml": "toml", "tsq": "tsq", "typescript": "typescript", "yaml": "yaml",
}
_IMPORT_NODE_HINTS = ("import", "include", "require")
_SPECIAL_IMPORT_NODES = {
    "use_declaration", "use_statement", "mod_item", "extern_crate_declaration",
    "namespace_use_declaration", "preproc_include", "include_directive", "using_directive",
}
_QUOTED = re.compile(r"[\"']([^\"'\r\n]+)[\"']")
_MODULE = re.compile(
    r"(?is)\b(?:import|use|mod|using|require|include|extern\s+crate)\s+"
    r"(?:static\s+)?([A-Za-z_$][A-Za-z0-9_.$:/\\*-]*)"
)


def available() -> bool:
    """Whether the optional package can construct a known bundled parser."""
    return _get_parser is not None and _parser("javascript") is not None


def grammar_for(language: str, source_path: str = "") -> Optional[str]:
    """Return a bundled grammar identifier, accounting for TSX files."""
    suffix = source_path.lower().rsplit(".", 1)[-1] if "." in source_path else ""
    if language == "typescript" and suffix == "tsx":
        return "tsx"
    if language == "fortran" and suffix in {"f", "for", "f77"}:
        return "fixed_form_fortran"
    return _GRAMMARS.get(language)


@lru_cache(maxsize=48)
def _parser(grammar: str):
    if _get_parser is None:
        return None
    try:
        # tree-sitter-languages 1.10.x uses the older Language constructor API.
        # Suppress only its deprecation warning; incompatibility falls back cleanly.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)
            return _get_parser(grammar)
    except Exception:
        return None


def supported(language: str, source_path: str = "") -> bool:
    grammar = grammar_for(language, source_path)
    return bool(grammar and _parser(grammar) is not None)


def _is_import_node(node_type: str) -> bool:
    lowered = node_type.lower()
    if lowered in _SPECIAL_IMPORT_NODES:
        return True
    return any(hint in lowered for hint in _IMPORT_NODE_HINTS) and lowered.endswith((
        "_statement", "_declaration", "_directive", "_spec", "_item", "_header", "_list", "_expression"))


def parse_tree(language: str, source_path: str, text: str):
    """Return the full bundled Tree-sitter tree, source bytes, and grammar ID.

    ``None`` means the optional dependency or requested grammar is unavailable.
    The returned object is a concrete syntax tree, not a semantic/compiler AST.
    """
    grammar = grammar_for(language, source_path)
    parser = _parser(grammar) if grammar else None
    if parser is None:
        return None
    source = text.encode("utf-8", "replace")
    try:
        tree = parser.parse(source)
        return tree, source, grammar
    except Exception:
        return None


def parse_imports(language: str, source_path: str, text: str,
                  max_nodes: int = 250000) -> Optional[Tuple[List[Tuple[str, int]], bool, str]]:
    """Parse imports from named syntax nodes and report root syntax errors.

    ``None`` means no bundled grammar is available or the optional package is
    incompatible. Imports are extracted from the full concrete syntax tree, not
    by scanning the entire file for keywords. No runtime semantics are inferred.
    """
    parsed = parse_tree(language, source_path, text)
    if parsed is None:
        return None
    tree, source, _grammar = parsed
    try:
        root = tree.root_node
        imports: List[Tuple[str, int]] = []
        seen = set()
        stack = [root]
        visited = 0
        while stack and visited < max_nodes:
            node = stack.pop()
            visited += 1
            if getattr(node, "is_named", True) and _is_import_node(node.type):
                fragment = source[node.start_byte:node.end_byte].decode("utf-8", "replace")
                references = [value.strip() for value in _QUOTED.findall(fragment)]
                if not references:
                    match = _MODULE.search(fragment)
                    if match:
                        references = [match.group(1).rstrip(":")]
                if language == "rust":
                    references = [reference.removeprefix("crate::").removeprefix("self::").removeprefix("super::")
                                  for reference in references]
                line = int(node.start_point[0]) + 1
                for reference in references:
                    marker = (reference, line)
                    if reference and marker not in seen:
                        seen.add(marker)
                        imports.append(marker)
                # A parent import node contains its nested names. Do not turn
                # imported identifiers into separate file references.
                if references:
                    continue
            stack.extend(reversed(node.children))
        return imports, bool(root.has_error), root.type
    except Exception:
        # Parsing a file must never prevent read-only heuristic analysis of the
        # rest of the project (malformed files and parser ABI mismatches included).
        return None


def bundled_grammars() -> List[str]:
    """Language IDs with a configured grammar mapping (not a promise all are installed)."""
    return sorted(_GRAMMARS)
