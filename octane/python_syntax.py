"""Keep downstream recursive Python visitors inside a deterministic AST budget."""
import ast


def parse_python(source, **options):
    tree = ast.parse(source, **options)
    stack, examined = [(tree, 0)], 0
    while stack:
        node, depth = stack.pop()
        examined += 1
        if depth > 128 or examined > 250000:
            raise ValueError('Python AST exceeds the depth/node analysis budget')
        stack.extend((child, depth + 1) for child in ast.iter_child_nodes(node))
    return tree
