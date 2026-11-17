"""Bounded, language-specific syntax rules for static call candidates."""
from __future__ import annotations

from collections import Counter, defaultdict
import re


# Keys are bundled grammar IDs, which also distinguishes TSX from JavaScript.
# Each entry gives function definitions and call nodes with their callee field.
_RULES = {
    "bash": ({"function_definition"}, {"command": "name"}),
    "c": ({"function_definition"}, {"call_expression": "function"}),
    "cpp": ({"function_definition"}, {"call_expression": "function"}),
    "c_sharp": ({"method_declaration", "constructor_declaration", "local_function_statement"}, {"invocation_expression": "function"}),
    "erlang": ({"function_clause"}, {"call": "expr"}),
    "fortran": ({"subroutine", "function"}, {"subroutine_call": "subroutine", "call_expression": ""}),
    "fixed_form_fortran": ({"subroutine", "function"}, {"subroutine_call": "subroutine", "call_expression": ""}),
    "go": ({"function_declaration", "method_declaration"}, {"call_expression": "function"}),
    "haskell": ({"function"}, {"exp_apply": ""}),
    "java": ({"method_declaration", "constructor_declaration"}, {"method_invocation": "name"}),
    "javascript": ({"function_declaration", "generator_function_declaration", "method_definition"}, {"call_expression": "function"}),
    "julia": ({"function_definition"}, {"call_expression": ""}),
    "kotlin": ({"function_declaration"}, {"call_expression": ""}),
    "ocaml": ({"let_binding"}, {"application_expression": "function"}),
    "perl": ({"function_definition"}, {"call_expression": "function_name"}),
    "php": ({"function_definition", "method_declaration"}, {"function_call_expression": "function"}),
    "r": (set(), {"call": "function"}),
    "ruby": ({"method", "singleton_method"}, {"call": "method"}),
    "rust": ({"function_item"}, {"call_expression": "function"}),
    "scala": ({"function_definition"}, {"call_expression": "function"}),
    "typescript": ({"function_declaration", "generator_function_declaration", "method_definition"}, {"call_expression": "function"}),
    "tsx": ({"function_declaration", "generator_function_declaration", "method_definition"}, {"call_expression": "function"}),
}
_JS = {"javascript", "typescript", "tsx"}
_C = {"c", "cpp"}
_SCOPE_NODES = {"class_declaration", "class_definition", "class_specifier", "interface_declaration", "namespace_definition",
                "namespace_declaration", "struct_declaration", "struct_specifier", "object_definition",
                "trait_definition", "mod_item", "impl_item", "enum_declaration", "module"}
_NAME_NODES = {"identifier", "simple_identifier", "name", "property_identifier", "variable", "atom", "word",
               "value_name", "namespace_identifier", "type_identifier"}
_NAME = re.compile(r"(?:[A-Za-z_$][\w$]*|[A-Za-z_][\w-]*[!?=]?)\Z")
_MAX_FILE_BYTES = 1024 * 1024
_MAX_NODES = 100_000


def _name(node, content):
    if node is None or node.is_missing:
        return None
    if node.type in {"command_name", "value_path", "exp_name"} and len(node.named_children) == 1:
        node = node.named_children[0]
    if node.type not in _NAME_NODES or node.end_byte - node.start_byte > 128:
        return None
    value = content[node.start_byte:node.end_byte].decode("utf-8", "replace")
    return value if _NAME.fullmatch(value) else None


def _name_parts(node, content, grammar):
    if grammar in _JS and node is not None and node.type == "member_expression":
        receiver = node.child_by_field_name("object")
        member = _name(node.child_by_field_name("property"), content)
        if not member or receiver is None:
            return ()
        if receiver.type == "this":
            return ("this", member)
        if receiver.type == "member_expression" and node.end_byte - node.start_byte <= 128:
            prefix = _name_parts(receiver, content, grammar)
        else:
            name = _name(receiver, content)
            prefix = (name,) if name else ()
        return (*prefix, member) if prefix else ()
    parts = []
    while grammar == "cpp" and node is not None and node.type == "qualified_identifier" and len(parts) < 16:
        scope = _name(node.child_by_field_name("scope"), content)
        if scope is None:
            return ()
        parts.append(scope)
        node = node.child_by_field_name("name")
    name = _name(node, content)
    return (*parts, name) if name else ()


def _definition(node, grammar, kinds):
    """Return the name node and callable body for recognized definitions."""
    if grammar in _JS and node.type == "assignment_expression":
        lhs = node.child_by_field_name("left")
        rhs = node.child_by_field_name("right")
        if (lhs is not None and lhs.type == "member_expression" and rhs is not None
                and rhs.type in {"function", "function_expression", "arrow_function", "generator_function"}):
            return lhs, rhs.child_by_field_name("body")
        return None, None
    if (grammar in _JS and node.type == "variable_declarator") or (grammar == "r" and node.type == "left_assignment"):
        value = node.child_by_field_name("value")
        if value and value.type in {"function", "function_expression", "arrow_function", "generator_function", "function_definition"}:
            body = value.child_by_field_name("body")
            if body is None and grammar == "r" and value.named_children:
                body = value.named_children[-1]
            return node.child_by_field_name("name"), body
        return None, None
    if node.type not in kinds:
        return None, None
    if grammar == "ocaml":
        if any(child.type == "parameter" for child in node.named_children):
            return node.child_by_field_name("pattern"), node.child_by_field_name("body")
        return None, None
    if grammar in _C:
        decl = node.child_by_field_name("declarator")
        while decl is not None and decl.type.endswith("_declarator"):
            decl = decl.child_by_field_name("declarator")
        return decl, node.child_by_field_name("body")
    if grammar in {"fortran", "fixed_form_fortran"} and node.named_children:
        return node.named_children[0].child_by_field_name("name"), node
    body = node.child_by_field_name("body")
    if grammar == "haskell":
        body = node.child_by_field_name("rhs")
    if grammar == "julia" and body is None:
        body = node
    return node.child_by_field_name("name"), body


def _scan_tree(tree, source, grammar):
    definitions, calls = [], []
    definition_kinds, call_fields = _RULES[grammar]
    stack = [(tree.root_node, (), None)]
    visited = 0
    while stack and visited < _MAX_NODES:
        node, scope, caller = stack.pop()
        visited += 1
        if node.type == "ERROR" or node.is_missing:
            continue
        name_node, body = _definition(node, grammar, definition_kinds)
        name = _name_parts(name_node, source, grammar)
        if name and body is not None:
            line = int(name_node.start_point[0]) + 1
            qualified = (*scope, *name)
            caller = len(definitions)
            internal = grammar in _C and any(child.type == "storage_class_specifier" and
                         source[child.start_byte:child.end_byte] == b"static" for child in node.named_children)
            definitions.append((qualified, line, int(name_node.start_point[1]) + 1, int(node.end_point[0]) + 1, internal))
            if body != node:
                stack.append((body, qualified, caller))
                continue
            scope = qualified
        elif node.type in {"arrow_function", "function_expression", "lambda_expression", "anonymous_function"}:
            caller = None
        elif node.type in _SCOPE_NODES:
            scope_name = _name(node.child_by_field_name("name") or node.child_by_field_name("type"), source)
            if scope_name:
                scope = (*scope, scope_name)
            else:
                scope = (*scope, f"scope@{int(node.start_point[0]) + 1}:{int(node.start_point[1]) + 1}")
        elif caller is not None and node.type in call_fields and not (
                node.child_by_field_name("receiver") or node.child_by_field_name("object")):
            field = call_fields[node.type]
            callee = node.child_by_field_name(field) if field else next(iter(node.named_children), None)
            callee_name = _name_parts(callee, source, grammar)
            if callee_name and callee_name[0] == "this":
                receiver = definitions[caller][0][:-1]
                callee_name = (*receiver, *callee_name[1:]) if receiver else ()
                if callee_name:
                    calls.append((caller, (), callee_name, int(callee.start_point[0]) + 1))
            elif callee_name:
                calls.append((caller, scope, callee_name, int(callee.start_point[0]) + 1))
        stack.extend((child, scope, caller) for child in reversed(node.named_children))
    return definitions, calls, bool(stack)


def analyze_syntax_calls(backend, files, contents, revision, import_edges, enabled=True):
    """Index recognized definitions and direct-name candidates from the same scanned bytes."""
    symbols, edges, scanned = [], [], {}
    by_file = defaultdict(lambda: defaultdict(list))
    coverage = defaultdict(Counter)
    for item in files:
        path, language = item["path"], item["language"]
        grammar = backend.grammar_for(language, path)
        if language == "python" or not grammar or grammar not in _RULES:
            continue
        if not enabled or not backend.supported(language, path):
            coverage[language]["parser_unavailable"] += 1
            continue
        content = contents[path]
        if len(content) > _MAX_FILE_BYTES:
            coverage[language]["size_skipped"] += 1
            continue
        parsed = backend.parse_tree(language, path, content.decode("utf-8", "replace"))
        if parsed is None:
            coverage[language]["parse_failed"] += 1
            continue
        tree, source, _ = parsed
        definitions, calls, truncated = _scan_tree(tree, source, grammar)
        coverage[language]["files_parsed"] += 1
        if tree.root_node.has_error:
            coverage[language]["files_with_syntax_errors"] += 1
        if truncated:
            coverage[language]["node_budget_skipped"] += 1
            continue
        coverage[language]["symbols"] += len(definitions)
        scanned[path] = (language, calls, [])
        duplicates = Counter(parts for parts, *_ in definitions)
        for parts, line, column, end, internal in definitions:
            qualified = ".".join(parts)
            key = f"symbol:{path}:{qualified}" + (f"@{line}:{column}" if duplicates[parts] > 1 else "")
            scanned[path][2].append(key)
            by_file[path][parts].append((key, internal))
            symbols.append({"node_key": key, "kind": "function", "name": qualified, "path": path, "line": line,
                            "language": language, "revision": revision,
                            "metadata": {"owner": "native-scanner", "basis": "Tree-sitter syntax", "end_line": end}})
    dependencies = defaultdict(set)
    for edge in import_edges:
        dependencies[edge["source"]].add(edge["target"])
    for path, (language, calls, keys) in scanned.items():
        for caller, scope, name, line in calls:
            candidates = []
            for depth in range(len(scope), -1, -1):
                candidates = by_file[path].get((*scope[:depth], *name), [])
                if candidates:
                    break
            basis = "Tree-sitter direct name in enclosing scope"
            confidence = 0.55
            if not candidates and len(name) == 1 and language in {"c", "cpp", "javascript", "typescript", "php", "ruby"}:
                candidates = sorted({entry for dependency in dependencies[path]
                                     if dependency in scanned and scanned[dependency][0] == language
                                     for entry in by_file[dependency].get(name, []) if not entry[1]})
                basis = "Tree-sitter name candidate in directly linked source"
                confidence = 0.35
            if len(candidates) == 1:
                edges.append({"source": keys[caller], "target": candidates[0][0], "kind": "calls",
                              "confidence": confidence, "metadata": {"line": line, "basis": basis, "static": True}})
                coverage[language]["call_candidates"] += 1
    return symbols, edges, {language: dict(counts) for language, counts in sorted(coverage.items())}
