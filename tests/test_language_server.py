"""Semantic provider integration and labeled fallback behavior."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
import tempfile
import unittest

from octane.db import ProjectIntelligence
from octane.language_server import LanguageServer, semantic_calls
from octane.scanner import ProjectScanner


class LanguageServerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "project"
        self.root.mkdir()
        self.sources = {
            "main.py": "from helper import helper\ndef entry(value):\n    return helper(value)\n",
            "helper.py": "def helper(value):\n    return value + 1\n",
            "main.ts": 'import {helper} from "./helper";\nexport function entry(value: number) {return helper(value);}\n',
            "helper.ts": "export function helper(value: number) {return value + 1;}\n",
        }
        for name, text in self.sources.items():
            (self.root / name).write_text(text)

    def test_unavailable_provider_preserves_static_fallback(self):
        nodes, edges, coverage = semantic_calls(self.root, [{"path": "main.py", "language": "python"}],
            {"main.py": self.sources["main.py"].encode()}, "revision", [], {"python": ["/missing/lsp"]})
        self.assertEqual((nodes, edges), ([], []))
        self.assertIn("fallback", coverage["python"])
        self.assertIn("Static", coverage["python"]["basis"])

    def test_rejects_invalid_commands_and_languages(self):
        with self.assertRaisesRegex(ValueError, "argument arrays"):
            LanguageServer("shell-command", self.root)
        with self.assertRaisesRegex(ValueError, "python and typescript"):
            semantic_calls(self.root, [], {}, "revision", [], {"unknown": ["server"]})

    def test_unresponsive_server_cannot_block_large_request_writes(self):
        server = LanguageServer([sys.executable, "-c", "import time; time.sleep(60)"], self.root, timeout=0.1)
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(TimeoutError, "stopped reading"):
                server.send("textDocument/didOpen", {"text": "x" * 1_000_000})
        finally:
            server.close()
        self.assertLess(time.monotonic() - started, 5)

    @unittest.skipUnless(os.environ.get("OCTANE_LSP_TEST_TOOLS"), "set OCTANE_LSP_TEST_TOOLS to installed language server tools")
    def test_real_pyright_and_typescript_resolve_calls_through_imports(self):
        prefix = Path(os.environ["OCTANE_LSP_TEST_TOOLS"]) / "node_modules"
        servers = {
            "python": [str(prefix / ".bin/pyright-langserver"), "--stdio"],
            "typescript": {
                "command": [str(prefix / ".bin/typescript-language-server"), "--stdio"],
                "initializationOptions": {"tsserver": {"path": str(prefix / "typescript/lib/tsserver.js")}},
            },
        }
        pil = ProjectIntelligence(self.base / "pil.sqlite3")
        project = pil.create_project("semantic", self.root)["id"]
        scan = ProjectScanner().scan(pil, project, language_servers=servers)
        coverage = scan["parser_coverage"]["language_servers"]
        self.assertTrue(all(coverage[name]["calls"] >= 1 for name in servers), coverage)
        graph = pil.query_graph(project)
        semantic = [edge for edge in graph["edges"] if edge["metadata"].get("basis") == "LSP call hierarchy"]
        providers = {edge["metadata"]["provider"] for edge in semantic}
        self.assertEqual(providers, {"python", "typescript"})
