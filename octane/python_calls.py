"""Conservative Python call candidates with lexical binding and import resolution."""
from __future__ import annotations

import ast
from .python_syntax import parse_python
import symtable
from collections import defaultdict
from pathlib import PurePosixPath

_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
_SCOPES = (*_FUNCTIONS, ast.ClassDef)


class _Body(ast.NodeVisitor):
    """Visit one scope's executable body, leaving nested scopes to their owners."""

    def __init__(self, body):
        self.nodes = []
        for statement in body:
            self.visit(statement)

    def generic_visit(self, node):
        self.nodes.append(node)
        super().generic_visit(node)

    def visit_FunctionDef(self, node):
        self.nodes.append(node)

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_ClassDef = visit_FunctionDef

    def visit_Lambda(self, node):
        pass

    visit_ListComp = visit_SetComp = visit_DictComp = visit_GeneratorExp = visit_Lambda


def _module(path):
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if len(parts) > 1 and parts[0] == "src":
        parts.pop(0)
    if parts[-1] == "__init__":
        parts.pop()
    return tuple(parts)


def _bindings(nodes, package):
    bindings = defaultdict(list)
    for node in nodes:
        if isinstance(node, ast.Import):
            for item in node.names:
                parts = tuple(item.name.split("."))
                bindings[item.asname or parts[0]].append(parts if item.asname else parts[:1])
        elif isinstance(node, ast.ImportFrom) and node.level <= len(package):
            base = package[:len(package) - node.level + 1] if node.level else ()
            module = tuple(node.module.split(".")) if node.module else ()
            for item in node.names:
                if item.name != "*":
                    bindings[item.asname or item.name].append(base + module + (item.name,))
    return {name: paths[0] if len(paths) == 1 else None for name, paths in bindings.items()}


def _writes(nodes):
    names = {node.id for node in nodes if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del))}
    names.update(node.name for node in nodes if isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name)
    names.update(node.rest for node in nodes if isinstance(node, ast.MatchMapping) and node.rest)
    return names


def analyze_python(sources, revision):
    """Return symbols, call candidates, and parse errors across scanned Python files."""
    modules, parsed, symbols, errors = defaultdict(list), {}, [], []
    definitions = defaultdict(dict)
    for path, content in sources.items():
        try:
            tree = parse_python(content, filename=path)
            table = symtable.symtable(content, path, "exec")
        except (SyntaxError, UnicodeError, ValueError, RecursionError, MemoryError) as error:
            errors.append({"path": path, "error": str(error)})
            continue
        module = _module(path)
        package = module if path.endswith("/__init__.py") else module[:-1]
        modules[module].append(path)
        parsed[path] = (tree, table, package)

        def discover(body, prefix=(), path=path):
            for node in _Body(body).nodes:
                if not isinstance(node, _SCOPES):
                    continue
                name = (*prefix, node.name)
                qualified = ".".join(name)
                kind = "class" if isinstance(node, ast.ClassDef) else "function"
                key = f"symbol:{path}:{qualified}"
                definitions[path][name] = key
                symbols.append({"node_key": key, "kind": kind, "name": qualified, "path": path,
                                "line": node.lineno, "language": "python", "revision": revision,
                                "metadata": {"end_line": node.end_lineno, "basis": "Python AST",
                                             "owner": "native-scanner"}})
                discover(node.body, name)
        discover(tree.body)

    def imported_target(parts):
        paths = modules.get(parts[:-1], ())
        if len(paths) == 1:
            return definitions[paths[0]].get(parts[-1:])
        return None

    calls = []
    for path, (tree, table, package) in parsed.items():
        top = _Body(tree.body).nodes
        global_imports = _bindings(top, package)
        wildcard_import = any(isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names)
                              for node in top)
        global_writes = _writes(top)

        def visit(body, lexical, prefix=(), *, class_body=False, receiver=None, path=path, package=package,
                  global_imports=global_imports, global_writes=global_writes, wildcard_import=wildcard_import):
            nodes = _Body(body).nodes
            local_imports = _bindings(nodes, package)
            local_writes = _writes(nodes)
            children = {(child.get_name(), child.get_lineno()): child for child in lexical.get_children()}
            for node in nodes:
                if isinstance(node, _SCOPES):
                    child = children.get((node.name, node.lineno))
                    if child:
                        first_parameter = None
                        if (class_body and isinstance(node, _FUNCTIONS)
                                and not any(isinstance(decorator, ast.Name) and decorator.id == "staticmethod"
                                            for decorator in node.decorator_list)):
                            args = node.args.posonlyargs + node.args.args
                            if args and args[0].arg in {"self", "cls"}:
                                first_parameter = args[0].arg
                        visit(node.body, child, (*prefix, node.name), class_body=isinstance(node, ast.ClassDef),
                              receiver=first_parameter)
                    continue
                if not prefix or not isinstance(node, ast.Call) or class_body:
                    continue
                parts = []
                expression = node.func
                while isinstance(expression, ast.Attribute):
                    parts.insert(0, expression.attr)
                    expression = expression.value
                if not isinstance(expression, ast.Name):
                    continue
                name = expression.id
                try:
                    symbol = lexical.lookup(name)
                except KeyError:
                    continue
                target, basis = None, None
                if symbol.is_free() or name in local_writes:
                    continue
                if symbol.is_imported() and local_imports.get(name) and not symbol.is_assigned():
                    target = imported_target(local_imports[name] + tuple(parts))
                    basis = "local import binding"
                elif symbol.is_global() and not wildcard_import and name not in global_writes:
                    if global_imports.get(name) and (name,) not in definitions[path]:
                        target = imported_target(global_imports[name] + tuple(parts))
                        basis = "module import binding"
                    elif not parts and name not in global_imports:
                        target = definitions[path].get((name,))
                        basis = "module definition"
                elif symbol.is_local() and not parts and symbol.is_namespace() and not symbol.is_imported():
                    target = definitions[path].get((*prefix, name))
                    basis = "nested definition"
                if not target and len(parts) == 1 and symbol.is_parameter() and name == receiver:
                    target = definitions[path].get((*prefix[:-1], parts[0]))
                    basis = "possible receiver method; dispatch can be overridden"
                if target:
                    calls.append({"source": definitions[path][prefix], "target": target, "kind": "calls",
                                  "metadata": {"line": node.lineno, "basis": basis + "; static candidate"},
                                  "confidence": 0.6 if "receiver" in basis else 0.8})
        visit(tree.body, table)
    return symbols, calls, errors
