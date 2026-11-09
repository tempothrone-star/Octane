"""Disposable snapshots; project commands run only through a constrained Docker boundary."""
from __future__ import annotations

import difflib
import hashlib
import os
import shutil
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from .db import ProjectIntelligence


class WorkspaceManager:
    def __init__(self, intelligence: ProjectIntelligence, storage_root: str | Path | None = None):
        self.intelligence = intelligence
        self.storage_root = (Path(storage_root) if storage_root else Path(tempfile.gettempdir()) / "octane-workspaces").resolve()
        self.storage_root.mkdir(parents=True, exist_ok=True)

    def create_workspace(self, project_id: str, mode: str = "observation") -> dict[str, Any]:
        if mode not in {"observation", "experimentation"}:
            raise ValueError("mode must be observation or experimentation")
        project = self.intelligence.get_project(project_id)
        if not project:
            raise KeyError(project_id)
        source, workspace_id = Path(project["root"]), "WS-" + uuid.uuid4().hex
        if not source.is_dir():
            raise FileNotFoundError(source)
        if source == self.storage_root or source.is_relative_to(self.storage_root) or self.storage_root.is_relative_to(source):
            raise ValueError("workspace storage and project roots must not overlap")
        target = self.storage_root / workspace_id
        try:
            shutil.copytree(source, target, ignore=_copy_ignore, symlinks=False)
            with self.intelligence._transaction() as db:
                db.execute("INSERT INTO workspaces VALUES(?,?,?,?,?,?,?)", (workspace_id, project_id, str(target), mode, project["revision"], _now(), "active"))
        except Exception:
            if target.exists():
                shutil.rmtree(target)
            raise
        return {"id": workspace_id, "project_id": project_id, "path": str(target), "mode": mode, "base_revision": project["revision"], "status": "active"}

    def apply_candidate_patch(self, workspace_id: str, relative_path: str, content: str) -> None:
        workspace = self._get(workspace_id)
        if workspace["mode"] != "experimentation":
            raise PermissionError("candidate patches require experimentation mode")
        path = self._safe_path(Path(workspace["path"]), relative_path)
        if not path.is_file():
            raise FileNotFoundError(relative_path)
        path.write_text(content, encoding="utf-8")

    def run_observation(self, workspace_id: str, command: list[str], image: str, timeout_seconds: int = 30) -> dict[str, Any]:
        """Run an explicit command in Docker, never as a host subprocess."""
        workspace = self._get(workspace_id)
        if workspace["mode"] != "observation":
            raise PermissionError("observation execution requires observation mode")
        return self._docker_run(workspace, command, image, timeout_seconds, writable=False)

    def run_experiment(self, workspace_id: str, command: list[str], image: str, timeout_seconds: int = 30) -> dict[str, Any]:
        workspace = self._get(workspace_id)
        if workspace["mode"] != "experimentation":
            raise PermissionError("experiments require experimentation mode")
        return self._docker_run(workspace, command, image, timeout_seconds, writable=True)

    def compare_workspace(self, workspace_id: str) -> dict[str, Any]:
        workspace = self._get(workspace_id)
        root = Path(workspace["path"])
        original = Path(self.intelligence.get_project(workspace["project_id"])["root"])
        paths = sorted({p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and not p.is_symlink()} |
                       {p.relative_to(original).as_posix() for p in original.rglob("*") if p.is_file() and not _excluded(p, original)})
        differences = []
        for relative in paths:
            left, right = original / relative, root / relative
            if left.is_symlink() or right.is_symlink():
                differences.append({"path": relative, "kind": "symlink"})
                continue
            if not left.exists() or not right.exists():
                differences.append({"path": relative, "kind": "added" if right.exists() else "removed"})
                continue
            try:
                a, b = left.read_bytes(), right.read_bytes()
            except OSError as exc:
                raise OSError(f"could not compare workspace file {relative!r}: {exc}") from exc
            if a != b:
                binary = False
                try:
                    diff = list(difflib.unified_diff(a.decode("utf-8").splitlines(True), b.decode("utf-8").splitlines(True),
                                                     fromfile=f"original/{relative}", tofile=f"workspace/{relative}"))
                except UnicodeDecodeError:
                    diff = []
                    binary = True
                differences.append({"path": relative, "kind": "modified", "binary": binary,
                                    "original_sha256": hashlib.sha256(a).hexdigest(),
                                    "workspace_sha256": hashlib.sha256(b).hexdigest(), "diff": "".join(diff)})
        return {"workspace_id": workspace_id, "differences": differences, "patch": "\n".join(change.get("diff", "") for change in differences)}

    def destroy_workspace(self, workspace_id: str) -> None:
        workspace = self._get(workspace_id)
        shutil.rmtree(workspace["path"])
        with self.intelligence._transaction() as db:
            db.execute("UPDATE workspaces SET status='destroyed' WHERE id=?", (workspace_id,))

    def _docker_run(self, workspace: dict[str, Any], command: list[str], image: str, timeout: int, writable: bool) -> dict[str, Any]:
        if not command or not all(isinstance(part, str) and "\x00" not in part for part in command):
            raise ValueError("command must be a non-empty list of strings")
        if not image or image.startswith("-") or any(ch.isspace() for ch in image):
            raise ValueError("image must be a Docker image reference")
        if not 1 <= timeout <= 3600:
            raise ValueError("timeout_seconds must be between 1 and 3600")
        docker = shutil.which("docker")
        if docker is None:
            raise RuntimeError("Docker is required for execution; no host execution fallback is permitted")
        mount_mode = "rw" if writable else "ro"
        cid_fd, cid_name = tempfile.mkstemp(prefix=".octane-cid-", dir=self.storage_root)
        os.close(cid_fd)
        os.unlink(cid_name)
        args = [docker, "run", "--rm", "--cidfile", cid_name, "--stop-timeout=1", "--network=none", "--memory=512m",
                "--memory-swap=512m", "--cpus=1", "--pids-limit=128", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m", "-v", f"{workspace['path']}:/workspace:{mount_mode}",
                "-w", "/workspace", image, *command]
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        truncated = {"stdout": False, "stderr": False}
        max_output = 1_000_000
        process = None
        timed_out = False
        cleanup_warning = None
        try:
            process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            readers = []
            for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
                thread = threading.Thread(target=_drain_output, args=(stream, buffers[name], truncated, name, max_output), daemon=True)
                thread.start()
                readers.append(thread)
            try:
                returncode = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                cleanup_warning = _stop_container(docker, cid_name)
                returncode = None
            for thread in readers:
                thread.join()
        finally:
            if process and process.poll() is None:
                process.kill()
                process.wait()
            Path(cid_name).unlink(missing_ok=True)
        output = {"returncode": returncode, "stdout": buffers["stdout"].decode("utf-8", errors="replace"),
                  "stderr": buffers["stderr"].decode("utf-8", errors="replace"), "command": command, "image": image,
                  "network": "disabled", "workspace_mode": workspace["mode"], "timed_out": timed_out,
                  "output_truncated": truncated["stdout"] or truncated["stderr"], "cleanup_warning": cleanup_warning}
        runtime_agent = self.intelligence.get_or_register_agent(workspace["project_id"], "docker-runtime", "runtime observer",
                                                                ["docker-isolated-execution"], ["write_evidence"])
        self.intelligence.submit_evidence(workspace["project_id"], "runtime_observation", output,
                                          "Command executed in constrained Docker workspace", agent_id=runtime_agent["id"],
                                          revision=workspace["base_revision"],
                                          verification_status="observed")
        return output

    def _get(self, workspace_id: str) -> dict[str, Any]:
        with self.intelligence._session() as db:
            row = db.execute("SELECT * FROM workspaces WHERE id=?", (workspace_id,)).fetchone()
            if not row or row["status"] != "active":
                raise KeyError(f"unknown active workspace: {workspace_id}")
            workspace = dict(row)
        path = Path(workspace["path"]).resolve()
        if path == self.storage_root or not path.is_relative_to(self.storage_root):
            raise RuntimeError("workspace path escaped the configured storage root")
        workspace["path"] = str(path)
        return workspace

    @staticmethod
    def _safe_path(root: Path, relative_path: str) -> Path:
        relative = PurePosixPath(relative_path)
        if relative.is_absolute() or not relative.parts or any(part in {"..", ""} for part in relative.parts):
            raise ValueError("path must be a safe project-relative path")
        target = root.joinpath(*relative.parts)
        if not target.resolve().is_relative_to(root.resolve()):
            raise ValueError("path escapes the workspace")
        return target


def _copy_ignore(directory: str, names: list[str]) -> set[str]:
    excluded = {".git", ".venv", "venv", "node_modules", "__pycache__", ".tox"}
    base = Path(directory)
    return {name for name in names if name in excluded or (base / name).is_symlink()
            or not (base / name).is_dir() and not (base / name).is_file()}


def _excluded(path: Path, root: Path) -> bool:
    return path.is_symlink() or any(part in {".git", ".venv", "venv", "node_modules", "__pycache__", ".tox"} for part in path.relative_to(root).parts)


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _drain_output(stream, buffer: bytearray, truncated: dict[str, bool], name: str, limit: int) -> None:
    while chunk := stream.read(65536):
        remaining = limit - len(buffer)
        if remaining > 0:
            buffer.extend(chunk[:remaining])
        if len(chunk) > max(remaining, 0):
            truncated[name] = True


def _stop_container(docker: str, cid_file: str) -> str | None:
    try:
        container_id = Path(cid_file).read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError) as exc:
        return f"could not read Docker container ID for timeout cleanup: {exc}"
    if not container_id or any(character not in "0123456789abcdef" for character in container_id.lower()):
        return "Docker did not provide a valid container ID for timeout cleanup"
    try:
        result = subprocess.run([docker, "stop", "--time", "1", container_id], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"Docker timeout cleanup failed: {exc}"
    if result.returncode != 0:
        return f"Docker timeout cleanup failed: {result.stderr.strip()}"
    return None
