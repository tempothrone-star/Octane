from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "octance" / "scripts"))

try:
    import tree_sitter_languages  # noqa: F401
except ImportError:
    raise unittest.SkipTest("optional Tree-sitter extra is not installed")

from octance import ast_backend  # noqa: E402
from octance.polygraph import build_dependency_graph  # noqa: E402


class TreeSitterGraphTests(unittest.TestCase):
    def test_full_concrete_syntax_tree_is_available_to_python_callers(self):
        parsed = ast_backend.parse_tree("javascript", "main.js", 'const answer = 42;')
        self.assertIsNotNone(parsed)
        tree, source, grammar = parsed
        self.assertEqual(grammar, "javascript")
        self.assertEqual(tree.root_node.type, "program")
        self.assertGreater(len(tree.root_node.children), 0)
        self.assertEqual(source.decode("utf-8"), 'const answer = 42;')

    def test_tree_sitter_parses_and_resolves_multiple_grammar_families(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = {
                "web/main.js": 'import { run } from "./helper.js";\n',
                "web/helper.js": "export function run() {}\n",
                "web/main.tsx": 'import View from "./View";\n',
                "web/View.tsx": "export default function View() { return null; }\n",
                "native/main.rs": "mod helper;\nuse crate::helper::run;\n",
                "native/helper.rs": "pub fn run() {}\n",
                "native/bad.js": 'import { from "./helper.js";\n',
            }
            for relative, text in files.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            graph = build_dependency_graph(root, parser="tree-sitter")
            edges = {(edge["source"], edge["target"]) for edge in graph["edges"]}
            self.assertIn(("web/main.js", "web/helper.js"), edges)
            self.assertIn(("web/main.tsx", "web/View.tsx"), edges)
            self.assertIn(("native/main.rs", "native/helper.rs"), edges)
            coverage = graph["parser_coverage"]["tree_sitter"]
            self.assertGreaterEqual(coverage["files_parsed"], 5)
            self.assertTrue({"javascript", "typescript", "rust"} <= set(coverage["languages_used"]))
            self.assertGreaterEqual(coverage["parse_error_files"], 1)
            self.assertTrue(any(edge["basis"] == "Tree-sitter syntax tree" for edge in graph["edges"]))

    def test_explicit_tree_sitter_mode_requires_the_optional_dependency(self):
        from octance import ast_backend
        if ast_backend.available():
            self.skipTest("optional Tree-sitter package is available")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "optional"):
                build_dependency_graph(Path(directory), parser="tree-sitter")


if __name__ == "__main__":
    unittest.main()
