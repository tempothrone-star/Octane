from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "scripts" / "octance.py"


class PolyglotDependencyGraphTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def write(self, relative: str, text: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def invoke(self, *args: str):
        result = subprocess.run([sys.executable, str(ENTRY), *args], cwd=self.root,
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                                text=True, capture_output=True, timeout=20)
        return result.returncode, json.loads(result.stdout)

    def test_project_internal_dependencies_across_languages(self):
        self.write("py/main.py", "from . import helper\nimport json\n")
        self.write("py/helper.py", "def normalize(value): return value\n")
        self.write("web/app.js", "/* import fake from './missing'; */\nconst text = \"require('./not-a-dependency')\";\nimport {\n  view\n} from './view';\n")
        self.write("web/view.ts", "export function view() {}\n")
        self.write("native/bridge.cpp", '#include "../c/api.h"\n')
        self.write("c/api.h", "int api(void);\n")
        self.write("go.mod", "module example.com/app\n")
        self.write("cmd/main.go", 'package main\nimport "example.com/app/internal/cache"\n')
        self.write("internal/cache/cache.go", "package cache\n")
        self.write("java/Use.java", "package client;\nimport model.Main;\n")
        self.write("java/model/Main.java", "package model;\nclass Main {}\n")
        self.write("rust/lib.rs", "mod util;\nuse crate::util::helper;\n// use crate::missing;\n")
        self.write("rust/util.rs", "pub fn helper() {}\n")
        self.write("node_modules/pkg/internal.js", "// excluded\n")

        sys.path.insert(0, str(ROOT / "octance" / "scripts"))
        from octance.polygraph import build_dependency_graph
        graph = build_dependency_graph(self.root)
        edges = {(edge["source"], edge["target"], edge["source_language"], edge["target_language"])
                 for edge in graph["edges"]}
        self.assertIn(("py/main.py", "py/helper.py", "python", "python"), edges)
        self.assertIn(("web/app.js", "web/view.ts", "javascript", "typescript"), edges)
        self.assertIn(("native/bridge.cpp", "c/api.h", "cpp", "c"), edges)
        self.assertIn(("cmd/main.go", "internal/cache/cache.go", "go", "go"), edges)
        self.assertIn(("java/Use.java", "java/model/Main.java", "java", "java"), edges)
        self.assertIn(("rust/lib.rs", "rust/util.rs", "rust", "rust"), edges)
        self.assertEqual(graph["cross_language_edge_count"], 2)
        self.assertIn("python", graph["parser_coverage"]["full_stdlib_parser"])
        self.assertIn("javascript", graph["parser_coverage"]["heuristic_import_patterns"])
        unresolved_specs = {row["reference"] for row in graph["unresolved_references"]}
        self.assertIn("json", unresolved_specs)
        self.assertNotIn("./missing", unresolved_specs)
        self.assertNotIn("./not-a-dependency", unresolved_specs)
        self.assertFalse(any("node_modules" in edge["source"] for edge in graph["edges"]))

    def test_graph_limits_and_cli_input_errors(self):
        self.write("a.py", "import b\n")
        self.write("b.py", "value = 1\n")
        code, init = self.invoke("init", "--project-root", str(self.root))
        self.assertEqual(code, 0, init)
        code, response = self.invoke("graph", "--max-total-bytes", "1")
        self.assertEqual(code, 0, response)
        self.assertTrue(response["data"]["truncated"])
        self.assertEqual(response["data"]["files_indexed"], 0)
        code, denied = self.invoke("graph", "--max-total-bytes", "0")
        self.assertNotEqual(code, 0)
        self.assertEqual(denied["error"], "invalid_input")


if __name__ == "__main__":
    unittest.main()
