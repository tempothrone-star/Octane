"""Audit regressions: fail closed on malformed inputs and retain useful neighboring results."""
from __future__ import annotations

import csv
import io
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from octane.db import ProjectIntelligence
from octane.exploration import PythonAdapter
from octane.language_server import LanguageServer, semantic_calls
from octane.ledger.core import Ledger, Project, init_project
from octane.ledger.polygraph import _python_imports
from octane.mutation import _python_mutants
from octane.python_calls import analyze_python
from octane.reports import parse_report
from octane.scanner import ProjectScanner
from octane.source_archive import export_revision
from octane.workspace import _private_mount
from tests import test_patch_gate


class AuditInputTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.base = Path(temp.name); self.root = self.base / 'project'; self.root.mkdir()

    def test_resource_exhausted_python_files_do_not_abort_scans(self):
        for error in (RecursionError('too deep'), MemoryError('budget exhausted')):
            with patch('ast.parse', side_effect=error):
                self.assertEqual(_python_imports('odd.py', 'x'), ([], 1))
                symbols, calls, errors = analyze_python({'odd.py': b'x'}, 'rev')
                self.assertEqual((symbols, calls), ([], []))
                self.assertEqual(errors[0]['path'], 'odd.py')
                observed = []
                self.assertEqual(list(_python_mutants('x', {1}, observed.append)), [])
                self.assertEqual(observed, [error])
        (self.root / 'odd.py').write_text('x = ' + '+'.join(['1'] * 60000))
        (self.root / 'good.py').write_text('def useful(): return 1\n')
        for scanner in (ProjectScanner(), PythonAdapter()):
            pil = ProjectIntelligence(self.base / (type(scanner).__name__ + '.db'))
            project = pil.create_project('generated', self.root)['id']
            if isinstance(scanner, ProjectScanner):
                scanner.scan(pil, project, parser='heuristic')
            else:
                scanner.scan(pil, project, self.root)
            self.assertTrue(pil.search_evidence(project, evidence_type='parse_error'))
            self.assertTrue(any(row['name'] == 'useful' for row in pil.query_graph(project)['nodes']))

    def test_xml_entities_are_rejected_in_utf8_utf16_and_utf32(self):
        attack = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e "private">]><testsuite><testcase name="&e;"/></testsuite>'
        for encoding in ('utf-8', 'utf-16', 'utf-16-le', 'utf-16-be', 'utf-32'):
            with self.subTest(encoding=encoding), self.assertRaises(ValueError):
                parse_report(attack.encode(encoding), 'junit', self.root)
        for text in ('<?xml version="1.0" encoding="iso-8859-1"?><testsuite/>', '\0<testsuite/>'):
            with self.assertRaises(ValueError):
                parse_report(text.encode(), 'junit', self.root)
        self.assertEqual(parse_report(b'<testsuite><testcase name="encoding failure"/></testsuite>', 'junit', self.root)['tests'][0]['status'], 'passed')
        self.assertEqual(parse_report(b'<?xml version="1.0" encoding="UTF-8"?><testsuite/>', 'junit', self.root)['tests'], [])

    def test_untrusted_lsp_definition_never_spawns_a_host_server(self):
        for definition in ([sys.executable], {'command': [sys.executable]}, {'command': [sys.executable], 'trustedProject': False}):
            with patch('octane.language_server.LanguageServer') as provider:
                _, _, coverage = semantic_calls(self.root, [{'path': 'odd.py', 'language': 'python'}],
                                                  {'odd.py': b'x'}, 'r', [], {'python': definition})
            provider.assert_not_called()
            self.assertIn('fallback', coverage['python'])

    def test_lsp_queue_overflow_is_visible_without_waiting_for_the_deadline(self):
        script = 'import sys,time,json\nfor i in range(600):\n b=json.dumps({"jsonrpc":"2.0","method":"noise"}).encode();sys.stdout.buffer.write(f"Content-Length: {len(b)}\\r\\n\\r\\n".encode()+b);sys.stdout.buffer.flush()\ntime.sleep(30)'
        server = LanguageServer([sys.executable, '-c', script], self.root, timeout=45)
        self.addCleanup(server.close)
        deadline = time.monotonic() + 5
        while not isinstance(server.reader_error, queue.Full) and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertIsInstance(server.reader_error, queue.Full)
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, 'queue overflow'):
            server.request('initialize', {})
        self.assertLess(time.monotonic() - started, 1)

    def test_holdout_mount_escapes_commas_in_operator_paths(self):
        suite = self.base / 'oracle,private'
        option = _private_mount(suite)
        self.assertEqual(option[0], '--mount')
        self.assertEqual(next(csv.reader(io.StringIO(option[1]))),
                         ['type=bind', 'source=' + str(suite), 'target=/octane-holdout', 'readonly'])

    def test_archives_skip_symlinks_without_following_them_and_record_omissions(self):
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        (self.root / 'app.py').write_text('VALUE = 1\n')
        (self.root / 'external').symlink_to('/etc/passwd')
        (self.root / 'local').symlink_to('app.py')
        subprocess.run(['git', '-C', str(self.root), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Test', '-c', 'user.email=t@example.invalid', 'commit', '-qm', 'fixture'], check=True)
        skipped = []
        target = export_revision(self.root, 'HEAD', self.base / 'archive', skipped_paths=skipped)
        self.assertEqual({r['path'] for r in skipped}, {'external', 'local'})
        self.assertEqual((target / 'app.py').read_text(), 'VALUE = 1\n')
        self.assertFalse((target / 'external').exists())
        self.assertFalse((target / 'local').exists())

    def test_existing_databases_open_without_schema_write_transactions(self):
        pil = ProjectIntelligence(self.base / 'native.db')
        statements = []
        original = pil._connect
        def connection():
            db = original(); db.set_trace_callback(statements.append); return db
        with patch.object(pil, '_connect', side_effect=connection):
            pil._initialize()
        self.assertFalse(any('BEGIN' in row or 'CREATE' in row or 'INSERT' in row for row in statements))
        init_project(self.root)
        ledger = Ledger(self.root / '.octane/ledger.db')
        self.assertTrue(ledger.fts5)
        with patch.object(ledger, 'verify_log', side_effect=AssertionError('full history read during append')):
            project = Project(self.root)
            project.ledger = ledger
            project.add_evidence('AG-owner', 'source_observation', 'append uses tail', {})
        self.assertTrue(ledger.verify_log(project.project_id)['valid'])


class AddedDeletedPatchTests(unittest.TestCase):
    def setUp(self):
        self.gate = test_patch_gate.PatchGateTests(); self.gate.setUp(); self.addCleanup(self.gate.doCleanups)

    def test_new_and_removed_regular_files_survive_comparison_and_gate(self):
        gate = self.gate
        manager = gate.manager
        manager.apply_candidate_patch(gate.candidate['id'], 'new/helper file.py', 'VALUE = 3\n')
        manager.remove_candidate_file(gate.candidate['id'], 'source.py')
        comparison = manager.compare_workspace(gate.candidate['id'])
        self.assertEqual({row['kind'] for row in comparison['differences']}, {'added', 'removed'})
        self.assertIn('/dev/null', comparison['patch'])
        self.assertIn('new file mode', comparison['patch'])
        self.assertIn('deleted file mode', comparison['patch'])
        (gate.root / 'candidate.diff').write_text(comparison['patch'])
        subprocess.run(['git', 'apply', '--check', 'candidate.diff'], cwd=gate.root, check=True, capture_output=True)
        (gate.root / 'candidate.diff').unlink()
        gate.fixed = gate.record(gate.candidate, gate.focus, 'passed')
        gate.regression = gate.record(gate.candidate, gate.preserve, 'passed')
        result = gate.propose()
        self.assertIn('new file mode', result['patch'])
        with self.assertRaises(ValueError):
            manager.remove_candidate_file(gate.candidate['id'], 'missing.py')
        with self.assertRaises(PermissionError):
            manager.remove_candidate_file(gate.original['id'], 'source.py')
        with self.assertRaises(ValueError):
            manager.apply_candidate_patch(gate.candidate['id'], '../escape.py', 'x')

    def test_empty_and_unicode_additions_make_git_apply_compatible_patches(self):
        gate = self.gate
        for name, content in (('empty.py', ''), ('café helper.py', 'VALUE=3')):
            gate.manager.apply_candidate_patch(gate.candidate['id'], name, content)
        comparison = gate.manager.compare_workspace(gate.candidate['id'])
        patch_file = gate.root / 'candidate.diff'
        patch_file.write_text(comparison['patch'])
        subprocess.run(['git', 'apply', '--check', 'candidate.diff'], cwd=gate.root, check=True, capture_output=True)
        patch_file.unlink()
        gate.manager.remove_candidate_file(gate.candidate['id'], 'empty.py')
        self.assertFalse(any(row['path'] == 'empty.py' for row in gate.manager.compare_workspace(gate.candidate['id'])['differences']))

    def test_mutation_resource_errors_are_recorded_as_parse_evidence(self):
        from octane.mutation import mutation_candidates
        gate = self.gate
        with patch('ast.parse', side_effect=MemoryError('synthetic resource failure')):
            mutation_candidates(gate.manager, gate.candidate['id'], 5)
        records = gate.pil.search_evidence(gate.project, evidence_type='parse_error')
        self.assertEqual(records[0]['payload']['stage'], 'mutation_generation')
        self.assertEqual(records[0]['source_path'], 'source.py')

    def test_added_binary_and_symlink_cannot_be_proposed(self):
        gate = self.gate
        (Path(gate.candidate['path']) / 'binary.dat').write_bytes(b'\0binary')
        with self.assertRaisesRegex(ValueError, 'text-only'):
            gate.propose()
        (Path(gate.candidate['path']) / 'link.py').symlink_to(gate.root / 'source.py')
        with self.assertRaises(ValueError):
            gate.manager.apply_candidate_patch(gate.candidate['id'], 'link.py', 'x')


class IntegrityAndProcessTests(unittest.TestCase):
    def test_append_rejects_bad_anchors_and_modified_tail(self):
        import json
        from octane.ledger.core import OctaneError
        for operation in ('invalid-anchor', 'missing-anchor-row', 'tail-payload'):
            with tempfile.TemporaryDirectory() as directory, self.subTest(operation=operation):
                root = Path(directory); init_project(root); project = Project(root)
                metadata_path = root / '.octane/project.json'
                metadata = json.loads(metadata_path.read_text())
                if operation == 'invalid-anchor':
                    metadata['log_anchor']['seq'] = False
                elif operation == 'missing-anchor-row':
                    metadata['log_anchor']['seq'] = 1000
                else:
                    with project.ledger.connect() as db:
                        db.execute('DROP TRIGGER event_log_no_update')
                        db.execute("UPDATE event_log SET payload_json='{}' WHERE seq=(SELECT MAX(seq) FROM event_log)")
                metadata_path.write_text(json.dumps(metadata))
                with self.assertRaisesRegex(OctaneError, 'damaged event log'):
                    project.add_evidence('AG-owner', 'source_observation', 'must reject', {})

    def test_wait_fallback_and_finished_group_disappearance_are_safe(self):
        from types import SimpleNamespace
        from octane.process import wait_unreaped, run_bounded
        from unittest.mock import Mock
        process = Mock()
        process.wait.return_value = 0
        with patch('octane.process.os', SimpleNamespace(name='nt')):
            self.assertEqual(wait_unreaped(process, 1), 0)
            process.wait.assert_called_once_with(timeout=1)
        if sys.platform != 'win32':
            # A separately collected/removed group is a harmless cleanup race.
            with patch('octane.process.os.killpg', side_effect=ProcessLookupError):
                result = run_bounded([sys.executable, '-c', 'print("finished")'])
            self.assertEqual(result.stdout, b'finished\n')


class PlatformRegressionTests(unittest.TestCase):
    def test_ast_budget_applies_even_when_the_interpreter_accepts_a_deep_tree(self):
        import ast
        from octane.python_syntax import parse_python
        tree = ast.Constant(value=1)
        for _ in range(200):
            tree = ast.BinOp(left=tree, op=ast.Add(), right=ast.Constant(value=1))
        with patch('octane.python_syntax.ast.parse', return_value=tree):
            with self.assertRaisesRegex(ValueError, 'depth/node'):
                parse_python('generated')
        broad = ast.Module(body=[ast.Pass()] * 250001, type_ignores=[])
        with patch('octane.python_syntax.ast.parse', return_value=broad):
            with self.assertRaisesRegex(ValueError, 'depth/node'):
                parse_python('generated')

    def test_resolved_alias_roots_work_for_property_commands_and_lsp_locations(self):
        from octane.property_checks import property_command
        from octane.language_server import _node
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); root = base / 'real'; root.mkdir()
            (root / 'app.py').write_text('def entry(x): return x\n')
            alias = base / 'alias'; alias.symlink_to(root, target_is_directory=True)
            self.assertEqual(property_command(alias, {'module': 'app', 'function': 'entry', 'rule': 'idempotent'})[0], 'python')
            location = {'uri': (alias / 'app.py').as_uri(), 'name': 'entry',
                        'range': {'start': {'line': 0, 'character': 0}}}
            self.assertEqual(_node(location, 'python', alias, 'r', [])['path'], 'app.py')

    def test_kqueue_observes_exit_without_reaping_and_reports_timeout(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        from octane.process import _wait_kqueue, stop_process_group
        events = MagicMock(); events.__enter__.return_value = events; events.control.return_value = [object()]
        backend = SimpleNamespace(kqueue=lambda: events, kevent=lambda *a, **k: object(), KQ_FILTER_PROC=1,
                                  KQ_EV_ADD=2, KQ_EV_ONESHOT=4, KQ_NOTE_EXIT=8)
        child = SimpleNamespace(pid=123, args=['probe'])
        with patch('octane.process.select', backend):
            self.assertIsNone(_wait_kqueue(child, 1))
            events.control.return_value = []
            with self.assertRaises(subprocess.TimeoutExpired):
                _wait_kqueue(child, 1)
            events.control.side_effect = ProcessLookupError
            self.assertIsNone(_wait_kqueue(child, 1))
        child = MagicMock(returncode=None)
        with patch('octane.process.os.killpg', side_effect=PermissionError), patch('octane.process.sys.platform', 'darwin'):
            stop_process_group(child)
            child.kill.assert_called_once()
