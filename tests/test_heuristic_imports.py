"""Language-by-language behavior checks before splitting import heuristics."""
from unittest import TestCase
from unittest.mock import patch

from octane.ledger import polygraph


class HeuristicImportTests(TestCase):
    def test_language_specific_declarations_keep_locations_and_order(self):
        cases = [
            ('c', '\n\n// ignored\n#include "actual.h"\n', [('actual.h',4)]),
            ('c', '// #include "fake.h"\n#include "real.h"\n', [('real.h',2)]),
            ('cpp', '/* ignored\ncomment */\n#include "real.h"\n', [('real.h',3)]),
            ('javascript', 'const x="import fake";\nimport {x} from "./first";\nexport * from "./second"; require("./third"); import("./fourth");',
             [('./first',2),('./second',3),('./third',3),('./fourth',3)]),
            ('typescript','import "./setup";\nimport type {Value} from "./types";', [('./setup',1),('./types',2)]),
            ('go','package x\nimport (\n "first/pkg"\n alias "second/pkg"\n)\n', [('first/pkg',3),('second/pkg',4)]),
            ('java','import static app.Value.run;\nimport other.*;', [('app.Value.run',1),('other.*',2)]),
            ('rust','mod child;\nuse crate::child::run;\n', [('child',1),('child::run',2),('crate::child::run',2)]),
            ('csharp','using Example.Core;', [('Example.Core',1)]),
            ('kotlin','import example.Core;', [('example.Core',1)]),
            ('scala','import example.Core;', [('example.Core',1)]),
            ('swift','import Example;', [('Example',1)]),
            ('php','<?php\ninclude_once("a.php"); require "b.php";', [('a.php',2),('b.php',2)]),
            ('ruby','# require "fake"\nrequire_relative "helper"\nrequire "library"\n', [('helper',2),('library',3)]),
        ]
        for language, source, expected in cases:
            with self.subTest(language=language):
                self.assertEqual(polygraph._heuristic_imports(language,source),expected)

    def test_registered_forms_and_duplicates_preserve_first_occurrence(self):
        with patch.dict(polygraph.LANGUAGE_IMPORT_SYNTAX, {'custom':{'quoted':['fetch'], 'module':['uses'], 'from_import':True}}):
            source='fetch "lib/path"\nuses other/module;\nfrom third.module import value\nfetch "lib/path"\n'
            self.assertEqual(polygraph._heuristic_imports('custom',source),
                             [('lib/path',1),('lib/path',4),('other/module',2),('third.module',3)])
