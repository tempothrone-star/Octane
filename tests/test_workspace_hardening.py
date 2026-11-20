"""Filesystem, original-test overlays, and container identity regressions."""
from __future__ import annotations

import io
from contextlib import nullcontext
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from octane.db import ProjectIntelligence
from octane.ledger.core import Project, init_project
from octane.test_protection import baseline_tests, is_test_path
from octane.workspace import WorkspaceManager, _SECRET_DIRS, _SECRET_NAMES, _SECRET_SUFFIXES, _tree_hash


class CompletedProcess:
    def __init__(self):
        self.stdout = io.BytesIO(b"passed\n")
        self.stderr = io.BytesIO()

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0


class WorkspaceHardeningTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "project"
        self.root.mkdir()
        (self.root / "source.py").write_text("VALUE = 1\n")
        self.pil = ProjectIntelligence(self.base / "state/pil.sqlite3")
        self.project = self.pil.create_project("hardening", self.root)["id"]
        self.manager = WorkspaceManager(self.pil, self.base / "workspaces")
        # Lifecycle recovery has separate real-Docker tests; these tests inspect policy arguments.
        self.addCleanup(patch.stopall)
        patch("octane.workspace.reap_expired", return_value=[]).start()
        patch("octane.workspace.execution_guard", side_effect=lambda *args: nullcontext()).start()

    def test_secret_patterns_are_absent_from_snapshots_and_hashes(self):
        original = _tree_hash(self.root)
        names = _SECRET_NAMES | {"CERT" + suffix.upper() for suffix in _SECRET_SUFFIXES} | {".ENV.local", "api.env"}
        for name in names:
            (self.root / name).write_text("private fixture")
        for directory in _SECRET_DIRS:
            folder = self.root / directory
            folder.mkdir()
            (folder / "private.txt").write_text("private fixture")
        self.assertEqual(_tree_hash(self.root), original)
        workspace = self.manager.create_workspace(self.project)
        tree = Path(workspace["path"])
        self.assertEqual([path.name for path in tree.iterdir()], ["source.py"])
        self.assertEqual(_tree_hash(tree), original)

    @unittest.skipUnless(os.name == "posix", "POSIX mode bits")
    def test_state_databases_and_metadata_are_private(self):
        mask = os.umask(0)
        self.addCleanup(os.umask, mask)
        root = self.base / "ledger-project"
        root.mkdir()
        init_project(root)
        project = Project(root)
        self.assertEqual(stat.S_IMODE(project.state.stat().st_mode), 0o700)
        for path in (project.ledger.db_path, project.state / "project.json", project.state / "config.json",
                     Path(self.pil.database)):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path)
        with self.pil._transaction() as db:
            db.execute("INSERT INTO projects(id,name,root,revision,created_at,updated_at) VALUES('perm','p','/p','r','n','n')")
            for suffix in ("-wal", "-shm"):
                path = Path(self.pil.database + suffix)
                if path.exists():
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path)

    @unittest.skipUnless(os.name == "posix", "POSIX mode bits")
    def test_existing_database_permissions_are_tightened_without_chmod_on_project_root(self):
        database = self.root / "external.sqlite3"
        database.touch(mode=0o666)
        root_mode = stat.S_IMODE(self.root.stat().st_mode)
        ProjectIntelligence(database)
        self.assertEqual(stat.S_IMODE(database.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), root_mode)

    @unittest.skipUnless(os.name == "posix", "POSIX mode bits")
    def test_opening_database_tightens_existing_wal_and_shared_memory_files(self):
        from octane.storage import prepare_database
        database = self.root / "existing.sqlite3"
        files = [database, Path(str(database) + "-wal"), Path(str(database) + "-shm")]
        for path in files:
            path.write_bytes(b"existing private state")
            path.chmod(0o666)
        prepare_database(database)
        for path in files:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(path.read_bytes(), b"existing private state")

    def test_missing_host_uid_requires_an_operator_opt_in(self):
        with patch("octane.workspace.os", SimpleNamespace()):
            with self.assertRaisesRegex(RuntimeError, "allowDefaultContainerUser"):
                self.manager._container_user()
            enabled = WorkspaceManager(self.pil, self.base / "enabled", allow_default_container_user=True)
            self.assertIsNone(enabled._container_user())

    def test_missing_docker_image_has_a_friendly_error(self):
        workspace = self.manager.create_workspace(self.project)
        with patch("octane.workspace.shutil.which", return_value="/usr/bin/docker"):
            with patch("octane.workspace.subprocess.run", side_effect=subprocess.CalledProcessError(1, ["docker"])):
                with self.assertRaisesRegex(RuntimeError, "pull the approved image first"):
                    self.manager.run_observation(workspace["id"], ["python", "source.py"], "missing:image")

    def test_candidate_run_mounts_original_checks_and_records_protection(self):
        tests = self.root / "tests"
        tests.mkdir()
        (tests / "test_source.py").write_text("assert VALUE == 2\n")
        workspace = self.manager.create_workspace(self.project, "experimentation")
        self.manager.apply_candidate_patch(workspace["id"], "tests/test_source.py", "assert True\n")
        with patch("octane.workspace.shutil.which", return_value="/usr/bin/docker"):
            with patch("octane.workspace.subprocess.run", return_value=SimpleNamespace(stdout="sha256:" + "a" * 64)):
                with patch("octane.workspace.subprocess.Popen", return_value=CompletedProcess()) as launch:
                    result = self.manager.run_experiment(workspace["id"], ["python", "tests/test_source.py"], "test:image")
        args = launch.call_args.args[0]
        mounts = [args[index + 1] for index, arg in enumerate(args) if arg == "--mount"]
        self.assertEqual(len(mounts), 1)
        self.assertIn("target=/workspace/tests", mounts[0])
        self.assertIn(".baselines", mounts[0])
        self.assertIn("readonly", mounts[0])
        self.assertTrue(result["test_integrity"]["original_tests"])
        self.assertEqual(result["test_integrity"]["file_count"], 1)
        self.assertIn("container_user", result)
        self.assertFalse(result["allow_default_container_user"])
        with self.pil._session() as db:
            recorded = json.loads(db.execute("SELECT result_json FROM execution_runs WHERE id=?", (result["run_id"],)).fetchone()[0])
        self.assertEqual(recorded["test_integrity"], result["test_integrity"])

    def test_rootless_podman_and_gvisor_keep_the_same_constraints(self):
        workspace = self.manager.create_workspace(self.project, "experimentation")
        manager = WorkspaceManager(self.pil, self.manager.storage_root, container_engine="podman", container_runtime="runsc")
        with patch("octane.workspace.shutil.which", return_value="/usr/bin/podman"):
            with patch("octane.workspace.subprocess.run", return_value=SimpleNamespace(stdout='{"host":{"security":{"rootless":false}}}')):
                with self.assertRaisesRegex(RuntimeError, "rootless"):
                    manager.run_experiment(workspace["id"], ["python", "source.py"], "test:image")
            responses = [SimpleNamespace(stdout='{"host":{"security":{"rootless":true}}}'),
                         SimpleNamespace(stdout="sha256:" + "a" * 64)]
            with patch("octane.workspace.subprocess.run", side_effect=responses):
                with patch("octane.workspace.subprocess.Popen", return_value=CompletedProcess()) as launch:
                    result = manager.run_experiment(workspace["id"], ["python", "source.py"], "test:image")
        args = launch.call_args.args[0]
        for option in ("--network=none", "--cap-drop=ALL", "--read-only", "--userns=keep-id", "--runtime"):
            self.assertIn(option, args)
        self.assertEqual(args[args.index("--runtime") + 1], "runsc")
        self.assertEqual(result["container_engine"], "podman")
        self.assertEqual(result["container_runtime"], "runsc")
        with self.assertRaises(ValueError):
            WorkspaceManager(self.pil, self.base / "invalid", container_engine="shell")

    def test_nested_tests_configs_and_fixture_paths_are_classified(self):
        for name in ("test_root.py", "pkg/tests/test_a.py", "pkg/__tests__/file.ts", "pkg/spec/a.rb",
                     "native/a_test.go", "src/FooTest.java", "a.phpt", "fixture/data.bin"):
            if name.startswith("fixture/"):
                continue
            with self.subTest(path=name):
                self.assertTrue(is_test_path(name))
        self.assertFalse(is_test_path("src/main.py"))
        self.assertFalse(is_test_path("pyproject.toml"))
        with self.assertRaisesRegex(ValueError, "symlinks"):
            link = self.root / "test_link.py"
            link.symlink_to(self.root / "source.py")
            baseline_tests(self.root)
