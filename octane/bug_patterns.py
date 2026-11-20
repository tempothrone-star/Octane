"""Small reviewed syntax-pattern library; every match needs contract/runtime review."""
from __future__ import annotations

import ast
from .python_syntax import parse_python
import hashlib
from functools import lru_cache

from .ledger import ast_backend
from .localization import source_path

# Compile only engine-owned queries, never queries supplied by the analyzed project.
QUERIES = {
    'python': [('mutable-default', '(default_parameter value: [(list) (dictionary) (set)] @bug)'),
               ('swallowed-exception', '(except_clause (block (pass_statement) @bug))')],
    'javascript': [('empty-catch', '(catch_clause body: (statement_block) @bug)')],
    'typescript': [('empty-catch', '(catch_clause body: (statement_block) @bug)')],
    'java': [('empty-catch', '(catch_clause body: (block) @bug)')],
}


def _python_fallback(text):
    result = []
    try:
        tree = parse_python(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return [], 'syntax_error'
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for value in (*node.args.defaults, *node.args.kw_defaults):
                if isinstance(value, (ast.List, ast.Dict, ast.Set)):
                    result.append(('mutable-default', value.lineno))
        if isinstance(node, ast.ExceptHandler) and len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
            result.append(('swallowed-exception', node.lineno))
    return result, 'python_ast'


@lru_cache(maxsize=32)
def _matches(language, text):
    """Content-keyed, bounded in-process cache: changed bytes cannot reuse old matches."""
    parsed = ast_backend.parse_tree(language, 'source', text)
    if parsed is None:
        return _python_fallback(text) if language == 'python' else ([], 'unavailable')
    tree, _, grammar = parsed
    if tree.root_node.has_error:
        return [], 'syntax_error'
    try:
        results = []
        for rule, query in QUERIES.get(language, []):
            for node, _ in ast_backend.query_captures(grammar, query, tree.root_node):
                if rule == 'empty-catch' and node.named_child_count:
                    continue
                results.append((rule, node.start_point[0] + 1))
        return results, 'tree_sitter_queries'
    except (ImportError, AttributeError, TypeError, ValueError, RuntimeError):
        return _python_fallback(text) if language == 'python' else ([], 'unavailable')


def scan_patterns(path, language, text):
    source_path(path)
    if not isinstance(text, str) or len(text.encode()) > 256000:
        raise ValueError('pattern source exceeds 256 KB')
    matches, backend = _matches(language, text)
    return {'path': path, 'sha256': hashlib.sha256(text.encode()).hexdigest(), 'backend': backend,
            'findings': [{'tool': 'octane-patterns', 'rule': rule, 'path': path, 'line': line,
                          'level': 'note', 'message': rule + ': inspect documented intent and reproduce before changing',
                          'confidence': 0.2} for rule, line in sorted(set(matches))],
            'available': backend not in {'unavailable', 'syntax_error'},
            'basis': 'Low-confidence syntax hypotheses; no control-flow, nullability or exploit proof'}
