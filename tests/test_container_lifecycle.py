"""Container deadlines survive killed bridges and recover only owned expired jobs."""
from __future__ import annotations

import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from octane.container_lifecycle import register_deadline, finish_interrupted, lifecycle_arguments, owner_key, reap_expired, remove_container
from octane.container_watchdog import enforce
from octane.db import ProjectIntelligence
from octane.workspace import WorkspaceManager

IMAGE = os.environ.get('OCTANE_TEST_DOCKER_IMAGE')


class LifecycleUnitTests(unittest.TestCase):
    def test_removal_errors_and_arguments_are_bounded(self):
        with patch('octane.container_lifecycle.subprocess.run', side_effect=OSError):
            self.assertFalse(remove_container('docker', 'octane-run-test'))
        result = subprocess.CompletedProcess([], 1, b'', b'No such container: octane-run-test')
        with patch('octane.container_lifecycle.subprocess.run', return_value=result):
            self.assertTrue(remove_container('docker', 'octane-run-test'))
        args = lifecycle_arguments('/state', 'RUN-test', 123)
        self.assertIn('io.octane.deadline=123', args)
        self.assertIn('io.octane.owner=' + owner_key('/state'), args)
        self.assertIn('octane-run-test', args)

    def test_recovery_closes_expired_rows_even_when_auto_remove_already_deleted_the_container(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp); database = base / 'state.db'
            with sqlite3.connect(database) as db:
                db.execute('CREATE TABLE execution_runs(id TEXT,status TEXT,result_json TEXT)')
                db.execute("INSERT INTO execution_runs VALUES('RUN-expired','running','{}')")
                db.execute("INSERT INTO execution_runs VALUES('RUN-live','running','{}')")
            register_deadline(database, 'RUN-expired', base, time.time() - 1)
            register_deadline(database, 'RUN-live', base, time.time() + 60)
            def response(args, **kwargs):
                return subprocess.CompletedProcess(args, 0, '', '') if args[1] == 'ps' else subprocess.CompletedProcess(args, 1, b'', b'No such container')
            with patch('octane.container_lifecycle.subprocess.run', side_effect=response):
                self.assertEqual(reap_expired('docker', base, database), ['RUN-expired'])
            with sqlite3.connect(database) as db:
                self.assertEqual(dict(db.execute('SELECT id,status FROM execution_runs')), {'RUN-expired': 'timed_out', 'RUN-live': 'running'})

    def test_watchdog_handles_missing_row_and_already_completed_runs(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'state.db'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE execution_runs(id TEXT,status TEXT,result_json TEXT)')
                db.execute("INSERT INTO execution_runs VALUES('RUN-ok','passed','{}')")
                db.execute("INSERT INTO execution_runs VALUES('RUN-active','running','{}')")
            with patch('octane.container_watchdog.subprocess.run') as run:
                enforce('docker', 'octane-run-ok', time.time() + 1, path, 'RUN-ok')
                run.assert_not_called()
            with patch('octane.container_watchdog.subprocess.run', return_value=subprocess.CompletedProcess([], 0)):
                enforce('docker', 'octane-run-active', time.time() + .02, path, 'RUN-active')
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("SELECT status FROM execution_runs WHERE id='RUN-active'").fetchone()[0], 'timed_out')
            finish_interrupted(path, 'RUN-ok')
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("SELECT status FROM execution_runs WHERE id='RUN-ok'").fetchone()[0], 'passed')


@unittest.skipUnless(IMAGE and os.name == 'posix', 'Docker and POSIX signals required')
class ContainerCrashTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.base = Path(temp.name); self.root = self.base / 'project'; self.root.mkdir()
        (self.root / 'work.py').write_text('import time\nprint("started",flush=True)\ntime.sleep(60)\n')
        self.database = self.base / 'db.sqlite3'; self.storage = self.base / 'workspaces'
        self.pil = ProjectIntelligence(self.database)
        self.project = self.pil.create_project('crash-test', self.root)['id']
        self.manager = WorkspaceManager(self.pil, self.storage)
        self.names = []
        self.addCleanup(self.clean_containers)

    def clean_containers(self):
        for name in self.names:
            subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=5)

    def test_sigterm_and_sigkill_do_not_leave_a_running_container_or_row(self):
        for termination in (signal.SIGTERM, signal.SIGKILL):
            with self.subTest(termination=termination):
                workspace = self.manager.create_workspace(self.project, 'experimentation')
                code = '''import sys
from octane.db import ProjectIntelligence
from octane.workspace import WorkspaceManager
manager=WorkspaceManager(ProjectIntelligence(sys.argv[1]),sys.argv[2])
manager.run_experiment(sys.argv[3],['python','-B','work.py'],sys.argv[4],2)
'''
                process = subprocess.Popen([sys.executable, '-c', code, str(self.database), str(self.storage), workspace['id'], IMAGE],
                                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                self.addCleanup(lambda p=process: p.kill() if p.poll() is None else None)
                started = time.monotonic(); run_id = None
                while time.monotonic() - started < 5:
                    with self.pil._session() as db:
                        row = db.execute("SELECT id FROM execution_runs WHERE workspace_id=? AND status='running'", (workspace['id'],)).fetchone()
                    if row:
                        name = 'octane-' + row['id'].lower()
                        running = subprocess.run(['docker', 'inspect', '-f', '{{.State.Running}}', name], capture_output=True, text=True)
                        if running.stdout.strip() == 'true':
                            run_id = row['id']; self.names.append(name); break
                    time.sleep(.02)
                self.assertIsNotNone(run_id, 'container did not start')
                process.send_signal(termination)
                process.communicate(timeout=8)
                deadline = time.monotonic() + 17
                while time.monotonic() < deadline:
                    with self.pil._session() as db:
                        status = db.execute('SELECT status FROM execution_runs WHERE id=?', (run_id,)).fetchone()[0]
                    inspect = subprocess.run(['docker', 'inspect', name], capture_output=True)
                    if status != 'running' and inspect.returncode:
                        break
                    time.sleep(.2)
                self.assertNotEqual(status, 'running')
                self.assertNotEqual(inspect.returncode, 0, 'deadline left an orphaned container')
                self.manager.destroy_workspace(workspace['id'])

    def test_tracing_allows_regular_files_larger_than_the_old_512kb_cap(self):
        (self.root / 'work.py').write_text('from pathlib import Path\nPath("build.bin").write_bytes(b"x" * 2000000)\n')
        workspace = self.manager.create_workspace(self.project, 'experimentation')
        result = self.manager.run_experiment(workspace['id'], ['python', '-B', 'work.py'], IMAGE, 10, python_trace=True)
        self.assertEqual(result['returncode'], 0, result)
        self.assertTrue(result['python_trace']['available'])
        self.assertEqual((Path(workspace['path']) / 'build.bin').stat().st_size, 2000000)
        self.manager.destroy_workspace(workspace['id'])

    def test_normal_deadline_removes_container_and_records_timeout(self):
        workspace = self.manager.create_workspace(self.project, 'experimentation')
        result = self.manager.run_experiment(workspace['id'], ['python', '-B', 'work.py'], IMAGE, 1)
        self.assertTrue(result['timed_out'])
        inspect = subprocess.run(['docker', 'inspect', 'octane-' + result['run_id'].lower()], capture_output=True)
        self.assertNotEqual(inspect.returncode, 0)
        self.manager.destroy_workspace(workspace['id'])

    def test_startup_reaper_leaves_other_owners_and_unexpired_jobs_alone(self):
        workspace = self.manager.create_workspace(self.project, 'experimentation')
        run_id = 'RUN-' + workspace['id'][3:]
        name = 'octane-' + run_id.lower()
        self.names.append(name)
        subprocess.run(['docker', 'run', '-d', *lifecycle_arguments(self.storage, run_id, time.time() - 1),
                        IMAGE, 'python', '-c', 'import time;time.sleep(60)'], check=True, capture_output=True)
        live_id = 'RUN-live' + workspace['id'][3:]
        other_id = 'RUN-other' + workspace['id'][3:]
        for key, storage, deadline in ((live_id, self.storage, time.time() + 60),
                                       (other_id, self.base / 'other-owner', time.time() - 1)):
            self.names.append('octane-' + key.lower())
            subprocess.run(['docker', 'run', '-d', *lifecycle_arguments(storage, key, deadline), IMAGE,
                            'python', '-c', 'import time;time.sleep(60)'], check=True, capture_output=True)
        # The reaper is idempotent even if a crash occurred before a run row was committed.
        recovered = reap_expired('docker', self.storage, self.database)
        self.assertEqual(recovered, [run_id])
        self.assertEqual(reap_expired('docker', self.storage, self.database), [])
        for key in (live_id, other_id):
            remaining = subprocess.run(['docker', 'inspect', '-f', '{{.State.Running}}', 'octane-' + key.lower()], capture_output=True, text=True)
            self.assertEqual(remaining.stdout.strip(), 'true')
        self.manager.destroy_workspace(workspace['id'])
