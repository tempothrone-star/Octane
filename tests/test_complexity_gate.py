"""The production-wide complexity ratchet rejects new or growing hotspots."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

if importlib.util.find_spec('radon') is None:
    raise unittest.SkipTest('install the dev extra for complexity gate regression tests') from None

spec = importlib.util.spec_from_file_location('complexity_check', Path(__file__).resolve().parents[1]/'scripts/check_complexity.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def branches(count):
    return 'def complicated(value):\n'+''.join(f'    if value == {i}: return {i}\n' for i in range(count))


class ComplexityGateTests(unittest.TestCase):
    def test_new_functions_and_increasing_baseline_scores_are_rejected(self):
        self.assertTrue(gate.violations({'module:new':31}, {}))
        self.assertFalse(gate.violations({'module:old':41}, {'module:old':41}))
        self.assertTrue(gate.violations({'module:old':42}, {'module:old':41}))
        self.assertTrue(gate.violations({'module:old':39}, {'module:old':41}))
        self.assertTrue(gate.violations({}, {'module:old':41}))
        self.assertTrue(gate.violations({'module:old':20}, {'module:old':20}))

    def test_cli_scans_new_production_files_and_methods(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/'octane').mkdir(); (root/'scripts').mkdir()
            baseline=root/'baseline.json'; baseline.write_text(json.dumps({'limit':30,'exceptions':{}}))
            source=root/'octane/new_module.py'; source.write_text(branches(30))
            args=['--root',str(root),'--baseline',str(baseline)]
            self.assertEqual(gate.main(args),1)
            source.write_text(branches(29))
            self.assertEqual(gate.main(args),0)
            source.write_text('class Example:\n'+''.join('    '+line+'\n' for line in branches(30).splitlines()))
            self.assertEqual(set(gate.source_scores(source.read_text())),{'Example.complicated'})
            self.assertEqual(gate.main(args),1)

    def test_nested_functions_are_measured_separately(self):
        source='def wrapper():\n'+''.join('    '+line+'\n' for line in branches(30).splitlines())
        scores=gate.source_scores(source)
        self.assertGreater(scores['wrapper.<locals>.complicated'],30)

    def test_repeated_names_cannot_hide_an_earlier_complex_definition(self):
        scores=gate.source_scores(branches(30)+'\ndef complicated(value): return value\n')
        self.assertEqual(scores['complicated'],31)
        self.assertEqual(scores['complicated#2'],1)
        self.assertTrue(gate.violations(scores,{}))
