"""Explicit language capabilities and bounded JavaScript/TypeScript AST mutations."""
from __future__ import annotations

from pathlib import Path
import re

from .build_config import is_build_path
from .test_protection import is_test_path

_JS_SUFFIXES = {'.js', '.jsx', '.mjs', '.cjs', '.ts', '.tsx'}


def mutation_support(changes):
    paths = []
    for change in changes:
        path = change['path']
        if is_test_path(path) or is_build_path(path):
            continue
        suffix = Path(path).suffix.casefold()
        available, reason, language = False, 'No mutation operators for this language', suffix.lstrip('.') or 'unknown'
        if suffix == '.py':
            available, reason, language = True, 'Python AST operators and source reversions', 'python'
        elif suffix in _JS_SUFFIXES:
            from .ledger.ast_backend import supported
            language = 'typescript' if suffix in {'.ts', '.tsx'} else 'javascript'
            available = supported(language, path)
            reason = 'Syntax-tree operators and source reversions' if available else 'Install the ast extra for JS/TS mutations'
        paths.append({'path': path, 'language': language, 'available': available, 'reason': reason})
    supported_count = sum(item['available'] for item in paths)
    status = 'available' if paths and supported_count == len(paths) else ('partial' if supported_count else 'unavailable')
    return {'status': status, 'paths': paths, 'reason': None if status == 'available' else
            ('Some changed source languages lack operators' if supported_count else 'No supported mutation language in changed source')}


def javascript_mutants(text, relative, lines):
    from .ledger.ast_backend import parse_tree
    language = 'typescript' if Path(relative).suffix.casefold() in {'.ts', '.tsx'} else 'javascript'
    parsed = parse_tree(language, relative, text)
    if parsed is None:
        return
    tree, raw, _ = parsed
    if tree.root_node.has_error:
        return
    replacements = {'===':'!==', '!==':'===', '==':'!=', '!=':'==', '<':'>=', '<=':'>',
                    '>':'<=', '>=':'<', '&&':'||', '||':'&&', '+':'-', '-':'+', 'true':'false', 'false':'true'}
    stack, examined = [tree.root_node], 0
    while stack and examined < 250_000:
        node = stack.pop()
        examined += 1
        if node.start_point[0] + 1 in lines:
            replacement = replacements.get(node.type)
            if node.type == 'number':
                number = raw[node.start_byte:node.end_byte].decode()
                if re.fullmatch(r'\d{1,9}', number):
                    replacement = str(int(number) + 1)
            if replacement is not None:
                mutated = raw[:node.start_byte] + replacement.encode() + raw[node.end_byte:]
                yield f'{language}:{node.start_byte}:{node.type}', mutated.decode()
        stack.extend(reversed(node.children))
