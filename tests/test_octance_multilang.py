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
sys.path.insert(0, str(ROOT / "octance" / "scripts"))
from octance.source import locate_suspects, parse_stack_frames, source_inventory  # noqa: E402


class MultilingualInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def write(self, relative: str, text: str = "// fixture\n") -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def invoke(self, *args: str):
        result = subprocess.run([sys.executable, str(ENTRY), *args], cwd=self.root,
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                                text=True, capture_output=True, timeout=20)
        payload = json.loads(result.stdout)
        return result.returncode, payload

    def test_inventory_covers_common_languages_and_excludes_generated_trees(self):
        self.write("src/parse.py", "def parse(x): return x\n")
        self.write("web/client.ts", "export function parse(x: string) { return x }\n")
        self.write("web/view.jsx")
        self.write("service/main.go")
        self.write("server/App.java")
        self.write("native/lib.rs")
        self.write("native/calc.cpp")
        self.write("db/schema.sql")
        self.write("docs/api.xml")
        self.write("node_modules/pkg/index.js")
        self.write("target/generated.rs")
        result = source_inventory(self.root)
        self.assertEqual(result["source_files"], 8)
        self.assertEqual(result["languages"]["python"], 1)
        self.assertEqual(result["languages"]["typescript"], 1)
        self.assertEqual(result["languages"]["javascript"], 1)
        self.assertEqual(result["languages"]["go"], 1)
        self.assertEqual(result["languages"]["java"], 1)
        self.assertEqual(result["languages"]["rust"], 1)
        self.assertEqual(result["languages"]["cpp"], 1)
        self.assertEqual(result["languages"]["sql"], 1)
        self.assertEqual(result["unsupported_extensions"][".xml"], 1)
        self.assertEqual(result["analysis_level"], "extension inventory only; no language parser or execution")
        self.assertFalse(any("node_modules" in item["path"] for item in result["files"]))

    def test_inventory_enforces_file_count_and_size_budgets(self):
        self.write("a.py", "12345")
        self.write("b.ts", "x")
        result = source_inventory(self.root, max_files=1, max_file_bytes=4)
        self.assertTrue(result["truncated"])
        self.assertLessEqual(result["visited_files"], 1)
        self.assertEqual(result["source_files"], 0)
        self.assertEqual(result["skipped"].get("over_file_size_limit"), 1)

    def test_stack_trace_formats_resolve_across_languages(self):
        for relative in ("src/py.py", "src/web.ts", "src/app.go", "src/Main.java", "src/lib.rs", "src/calc.cpp"):
            self.write(relative)
        trace = "\n".join([
            '  File "%s", line 3, in parse' % (self.root / "src/py.py"),
            "    at render (%s:8:5)" % (self.root / "src/web.ts"),
            "    at pkg.main(/workspace/src/app.go:12 +0x10)",
            "    at com.example.Main.run(Main.java:42)",
            "    at /workspace/src/lib.rs:17:2",
            "src/calc.cpp:19:4: error: bad value",
        ])
        frames = parse_stack_frames(trace, self.root)
        self.assertEqual([frame["line"] for frame in frames], [3, 8, 12, 42, 17, 19])
        self.assertEqual([frame["path"] for frame in frames], [
            "src/py.py", "src/web.ts", "src/app.go", "Main.java", "src/lib.rs", "src/calc.cpp",
        ])
        ranked = locate_suspects(self.root, trace)
        self.assertEqual(ranked["resolved_frame_count"], 6)
        self.assertEqual(ranked["suspects"][0]["path"], "src/py.py")
        self.assertTrue(any("not a root-cause" in warning for warning in ranked["warnings"]))

    def test_cli_is_read_only_and_rejects_trace_outside_project(self):
        self.write("app.py", "def run(): pass\n")
        code, init = self.invoke("init", "--project-root", str(self.root))
        self.assertEqual(code, 0, init)
        self.write("failure.log", '  File "%s/app.py", line 1, in run\n' % self.root)
        code, inventory = self.invoke("languages")
        self.assertEqual(code, 0, inventory)
        self.assertEqual(inventory["data"]["languages"]["python"], 1)
        code, located = self.invoke("locate", "--trace-file", "failure.log")
        self.assertEqual(code, 0, located)
        self.assertEqual(located["data"]["suspects"][0]["path"], "app.py")
        self.assertTrue((self.root / "app.py").is_file())
        code, denied = self.invoke("locate", "--trace-file", "../outside.log")
        self.assertNotEqual(code, 0)
        self.assertEqual(denied["error"], "invalid_input")
        code, denied = self.invoke("locate", "--trace-file", str(self.root / "failure.log"))
        self.assertNotEqual(code, 0)
        self.assertEqual(denied["error"], "invalid_input")
        code, denied = self.invoke("languages", "--max-files", "0")
        self.assertNotEqual(code, 0)
        self.assertEqual(denied["error"], "invalid_input")
        outside = self.root.parent / "external-trace.log"
        outside.write_text("not used\n", encoding="utf-8")
        try:
            (self.root / "link.log").symlink_to(outside)
        except (OSError, NotImplementedError):
            return
        code, denied = self.invoke("locate", "--trace-file", "link.log")
        self.assertNotEqual(code, 0)
        self.assertEqual(denied["error"], "invalid_input")


if __name__ == "__main__":
    unittest.main()
