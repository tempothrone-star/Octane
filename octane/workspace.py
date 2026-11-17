"""Disposable snapshots; project commands run only through a constrained Docker boundary."""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

from .db import ProjectIntelligence, _id, _scrub_sensitive_value
from .call_plot import call_plot
from .native_trace import load_native_trace, native_trace_command
from .runtime_profiles import load_profile_trace, profile_command


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
        if Path(self.intelligence.database).resolve().is_relative_to(source):
            raise ValueError("store the native PIL database outside the project before creating snapshots")
        target = self.storage_root / workspace_id
        baseline = self.storage_root / ".baselines" / workspace_id
        baseline.parent.mkdir(parents=True, exist_ok=True)
        try:
            source_hash = _tree_hash(source)
            shutil.copytree(source, target, ignore=_copy_ignore, symlinks=False)
            if _tree_hash(target) != source_hash or _tree_hash(source) != source_hash:
                raise RuntimeError("project changed during snapshot creation; retry")
            shutil.copytree(target, baseline, ignore=_copy_ignore, symlinks=False)
            with self.intelligence._transaction() as db:
                db.execute("INSERT INTO workspaces(id,project_id,path,mode,base_revision,created_at,status,baseline_path,snapshot_hash) VALUES(?,?,?,?,?,?,?,?,?)",
                           (workspace_id, project_id, str(target), mode, project["revision"], _now(), "active", str(baseline), source_hash))
                self.intelligence._audit(db, project_id, None, "workspace.create", "workspace", workspace_id,
                                         {"mode": mode, "snapshot_hash": source_hash})
        except Exception:
            if target.exists():
                shutil.rmtree(target)
            if baseline.exists():
                shutil.rmtree(baseline)
            raise
        return {"id": workspace_id, "project_id": project_id, "path": str(target), "mode": mode,
                "base_revision": project["revision"], "snapshot_hash": source_hash, "status": "active"}

    def apply_candidate_patch(self, workspace_id: str, relative_path: str, content: str) -> None:
        workspace = self._get(workspace_id)
        if workspace["mode"] != "experimentation":
            raise PermissionError("candidate patches require experimentation mode")
        path = self._safe_path(Path(workspace["path"]), relative_path)
        if not path.is_file():
            raise FileNotFoundError(relative_path)
        path.write_text(content, encoding="utf-8")

    def run_observation(self, workspace_id: str, command: list[str], image: str, timeout_seconds: int = 30,
                        *, investigation_id: str | None = None, python_trace: bool = False,
                        native_trace_symbols: list[str] | None = None, runtime_trace: str | None = None) -> dict[str, Any]:
        """Run an explicit command in Docker, never as a host subprocess."""
        workspace = self._get(workspace_id)
        if workspace["mode"] != "observation":
            raise PermissionError("observation execution requires observation mode")
        return self._docker_run(workspace, command, image, timeout_seconds, writable=False,
                                investigation_id=investigation_id, python_trace=python_trace,
                                native_trace_symbols=native_trace_symbols, runtime_trace=runtime_trace)

    def run_experiment(self, workspace_id: str, command: list[str], image: str, timeout_seconds: int = 30,
                       *, investigation_id: str | None = None, python_trace: bool = False,
                       native_trace_symbols: list[str] | None = None, runtime_trace: str | None = None) -> dict[str, Any]:
        workspace = self._get(workspace_id)
        if workspace["mode"] != "experimentation":
            raise PermissionError("experiments require experimentation mode")
        return self._docker_run(workspace, command, image, timeout_seconds, writable=True,
                                investigation_id=investigation_id, python_trace=python_trace,
                                native_trace_symbols=native_trace_symbols, runtime_trace=runtime_trace)

    def compare_workspace(self, workspace_id: str) -> dict[str, Any]:
        workspace = self._get(workspace_id)
        root = Path(workspace["path"])
        project_root = Path(self.intelligence.get_project(workspace["project_id"])["root"])
        original = Path(workspace["baseline_path"]) if workspace.get("baseline_path") else project_root
        if workspace.get("snapshot_hash") and _tree_hash(original) != workspace["snapshot_hash"]:
            raise RuntimeError("workspace baseline contents changed")
        paths = sorted({p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_symlink() or p.is_file()} |
                       {p.relative_to(original).as_posix() for p in original.rglob("*") if p.is_file() and not _excluded(p, original)})
        differences = []
        for relative in paths:
            left, right = original / relative, root / relative
            if _has_symlink_parent(left, original) or _has_symlink_parent(right, root):
                differences.append({"path": relative, "kind": "symlink"})
                continue
            if not left.is_file() or not right.is_file():
                differences.append({"path": relative, "kind": "added" if right.is_file() else "removed"})
                continue
            if stat.S_IMODE(left.stat().st_mode) != stat.S_IMODE(right.stat().st_mode):
                differences.append({"path": relative, "kind": "mode_changed"})
                continue
            try:
                a, b = left.read_bytes(), right.read_bytes()
            except OSError as exc:
                raise OSError(f"could not compare workspace file {relative!r}: {exc}") from exc
            if a != b:
                binary = False
                try:
                    if b"\0" in a or b"\0" in b or any(char in relative for char in "\n\r\t"):
                        raise ValueError("path or contents cannot be represented safely in a text patch")
                    lines = difflib.unified_diff(a.decode("utf-8").splitlines(True), b.decode("utf-8").splitlines(True),
                                                 fromfile=f"a/{relative}", tofile=f"b/{relative}")
                    diff = [line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines]
                except ValueError:
                    diff = []
                    binary = True
                differences.append({"path": relative, "kind": "modified", "binary": binary,
                                    "original_sha256": hashlib.sha256(a).hexdigest(),
                                    "workspace_sha256": hashlib.sha256(b).hexdigest(), "diff": "".join(diff)})
        return {"workspace_id": workspace_id, "differences": differences, "patch": "\n".join(change.get("diff", "") for change in differences),
                "review_notes": _header_review_notes(differences),
                "snapshot_hash": workspace.get("snapshot_hash"), "candidate_hash": _tree_hash(root),
                "original_changed": bool(workspace.get("snapshot_hash") and _tree_hash(project_root) != workspace["snapshot_hash"])}

    def destroy_workspace(self, workspace_id: str) -> None:
        workspace = self._get(workspace_id)
        shutil.rmtree(workspace["path"])
        if workspace.get("baseline_path"):
            shutil.rmtree(workspace["baseline_path"])
        with self.intelligence._transaction() as db:
            db.execute("UPDATE workspaces SET status='destroyed' WHERE id=?", (workspace_id,))
            self.intelligence._audit(db, workspace["project_id"], None, "workspace.destroy", "workspace", workspace_id, {})

    def _docker_run(self, workspace: dict[str, Any], command: list[str], image: str, timeout: int, writable: bool,
                    investigation_id: str | None, python_trace: bool,
                    native_trace_symbols: list[str] | None = None, runtime_trace: str | None = None) -> dict[str, Any]:
        if not command or not all(isinstance(part, str) and "\x00" not in part for part in command):
            raise ValueError("command must be a non-empty list of strings")
        if _scrub_sensitive_value(" ".join(command)) != " ".join(command):
            raise ValueError("commands must not contain credentials")
        if not image or image.startswith("-") or any(ch.isspace() for ch in image):
            raise ValueError("image must be a Docker image reference")
        if not 1 <= timeout <= 3600:
            raise ValueError("timeout_seconds must be between 1 and 3600")
        if sum((python_trace, native_trace_symbols is not None, runtime_trace is not None)) > 1:
            raise ValueError("choose one of pythonTrace, nativeTraceSymbols, or runtimeTrace")
        traced_command, runner = None, None
        if python_trace:
            traced_command = _python_trace_command(command)
            runner = ("trace_runner.py", "/octane-tracer.py")
        elif native_trace_symbols is not None:
            traced_command = native_trace_command(command, native_trace_symbols)
            runner = ("native_trace_runner.py", "/octane-tracer.py")
        elif runtime_trace is not None:
            traced_command, runner_name = profile_command(command, runtime_trace)
            if runner_name:
                runner = (runner_name, "/" + runner_name)
        docker = shutil.which("docker")
        if docker is None:
            raise RuntimeError("Docker is required for execution; no host execution fallback is permitted")
        mount_mode = "rw" if writable else "ro"
        cid_fd, cid_name = tempfile.mkstemp(prefix=".octane-cid-", dir=self.storage_root)
        os.close(cid_fd)
        os.unlink(cid_name)
        image_id = subprocess.run([docker, "image", "inspect", "--format", "{{.Id}}", image],
                                  capture_output=True, text=True, timeout=10, check=True).stdout.strip()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise RuntimeError("Docker did not return an immutable local image ID; pull the approved image first")
        args = [docker, "run", "--rm", "--cidfile", cid_name, "--stop-timeout=1", "--network=none", "--memory=512m",
                "--memory-swap=512m", "--cpus=1", "--pids-limit=128", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m", "-v", f"{workspace['path']}:/workspace:{mount_mode}",
                "-w", "/workspace", "-e", "HOME=/tmp", "-e", "PYTHONDONTWRITEBYTECODE=1"]
        if hasattr(os, "getuid"):
            args += ["--user", f"{os.getuid()}:{os.getgid()}"]
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        truncated = {"stdout": False, "stderr": False}
        max_output = 1_000_000
        process = None
        timed_out = False
        cleanup_warning = None
        before_hash = _tree_hash(Path(workspace["path"]))
        run_id = _id("RUN")
        started = time.monotonic()
        with self.intelligence._transaction() as db:
            if investigation_id is not None:
                inv = db.execute("SELECT project_id,revision,status FROM investigations WHERE id=?", (investigation_id,)).fetchone()
                revision = self.intelligence.get_project_revision(workspace["project_id"])
                if (not inv or inv["project_id"] != workspace["project_id"] or inv["status"] in {"completed", "abandoned"}
                        or inv["revision"] != revision or workspace["base_revision"] != revision):
                    raise ValueError("execution needs a current investigation and workspace from the same project revision")
            db.execute("INSERT INTO execution_runs(id,project_id,workspace_id,investigation_id,command_json,image,image_id,before_hash,snapshot_hash,status,result_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (run_id, workspace["project_id"], workspace["id"], investigation_id, json.dumps(command), image, image_id,
                        before_hash, workspace.get("snapshot_hash") or before_hash, "running", "{}", _now()))
        trace_result = None
        trace_file = None
        try:
            if traced_command:
                trace_fd, trace_file = tempfile.mkstemp(prefix=".octane-trace-", dir=self.storage_root)
                os.close(trace_fd)
                args += ["--ulimit", "fsize=524288:524288", "-v", f"{trace_file}:/octane-trace-output:rw"]
                if runner:
                    trace_runner = Path(__file__).with_name(runner[0]).resolve()
                    args += ["-v", f"{trace_runner}:{runner[1]}:ro"]
                if native_trace_symbols is not None:
                    args += ["-e", f"OCTANE_NATIVE_SYMBOLS={json.dumps(native_trace_symbols)}"]
                if runtime_trace == "speedscope":
                    args += ["-e", "OCTANE_TRACE_PATH=/octane-trace-output"]
            args += [image_id, *(traced_command or command)]
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
            if process:
                process.stdout.close()
                process.stderr.close()
            Path(cid_name).unlink(missing_ok=True)
            if trace_file:
                try:
                    if process and not timed_out:
                        if python_trace:
                            trace_result = _load_python_trace(Path(trace_file))
                        elif native_trace_symbols is not None:
                            trace_result = load_native_trace(Path(trace_file), native_trace_symbols)
                        else:
                            trace_result = load_profile_trace(Path(trace_file), Path(workspace["path"]), runtime_trace)
                finally:
                    Path(trace_file).unlink(missing_ok=True)
        output = {"run_id": run_id, "returncode": returncode, "stdout": buffers["stdout"].decode("utf-8", errors="replace"),
                  "stderr": buffers["stderr"].decode("utf-8", errors="replace"), "command": command, "image": image,
                  "network": "disabled", "workspace_mode": workspace["mode"], "timed_out": timed_out,
                  "output_truncated": truncated["stdout"] or truncated["stderr"], "cleanup_warning": cleanup_warning,
                  "image_id": image_id, "before_hash": before_hash, "after_hash": _tree_hash(Path(workspace["path"])),
                  "duration_seconds": round(time.monotonic() - started, 6)}
        if python_trace:
            output["python_trace"] = trace_result or {"available": False, "reason": "trace did not complete or could not be read"}
        if native_trace_symbols is not None:
            output["native_trace"] = trace_result or {"available": False, "symbols": native_trace_symbols,
                                                       "reason": "GDB trace did not complete or could not be read"}
        if runtime_trace is not None:
            output["runtime_trace"] = trace_result or {"available": False, "format": runtime_trace,
                                                        "reason": "Runtime profile did not complete or could not be read"}
        runtime_agent = self.intelligence.get_or_register_agent(workspace["project_id"], "docker-runtime", "runtime observer",
                                                                ["docker-isolated-execution"], ["write_evidence"])
        with self.intelligence._transaction() as db:
            evidence = self.intelligence.submit_evidence(workspace["project_id"], "runtime_observation", output,
                                          "Command executed in constrained Docker workspace", agent_id=runtime_agent["id"],
                                          revision=workspace["base_revision"],
                                          verification_status="observed")
            if timed_out:
                status = "timed_out"
            elif returncode == 0:
                status = "passed"
            elif returncode in {125, 126, 127} or returncode < 0:
                status = "execution_error"
            else:
                status = "failed"
            output["evidence_id"] = evidence["id"]
            db.execute("UPDATE execution_runs SET evidence_id=?,after_hash=?,status=?,result_json=? WHERE id=?",
                       (evidence["id"], output["after_hash"], status, json.dumps(_scrub_sensitive_value(output)), run_id))
            self.intelligence._audit(db, workspace["project_id"], runtime_agent["id"], "workspace.run", "execution", run_id,
                                     {"status": status, "evidence_id": evidence["id"]})
        return _scrub_sensitive_value(output)

    def _get(self, workspace_id: str) -> dict[str, Any]:
        with self.intelligence._session() as db:
            row = db.execute("SELECT * FROM workspaces WHERE id=?", (workspace_id,)).fetchone()
            if not row or row["status"] != "active":
                raise KeyError(f"unknown active workspace: {workspace_id}")
            workspace = dict(row)
        path = Path(workspace["path"]).resolve()
        if path != self.storage_root / workspace_id or not path.is_dir():
            raise RuntimeError("workspace path escaped the configured storage root")
        workspace["path"] = str(path)
        if workspace.get("baseline_path"):
            baseline = Path(workspace["baseline_path"])
            expected = self.storage_root / ".baselines" / workspace_id
            if baseline != expected or baseline.resolve() != expected or not baseline.is_dir():
                raise RuntimeError("workspace baseline is missing or escaped its storage root")
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


_HEADER_LITERAL = re.compile(
    r"[\"'`](x-content-type-options|content-security-policy|strict-transport-security|x-frame-options|"
    r"referrer-policy|permissions-policy|cross-origin-(?:embedder|opener|resource)-policy)(?=[\"'`:])", re.I,
)


def _header_review_notes(differences):
    notes = []
    for change in differences:
        removed, added = Counter(), Counter()
        for line in change.get("diff", "").splitlines():
            if (line.startswith("-") and not line.startswith("---")) or (line.startswith("+") and not line.startswith("+++")):
                counter = removed if line[0] == "-" else added
                counter.update(name.casefold() for name in _HEADER_LITERAL.findall(line[1:]))
        for header, count in sorted((removed - added).items()):
            notes.append({"kind": "security_header_reference_removed", "path": change["path"],
                          "header": header, "removed_occurrences": count,
                          "basis": "Diff contains fewer header literals. Compare actual before-and-after response headers and check the documented contract."})
    return notes


def _python_trace_command(command: list[str]) -> list[str]:
    if not re.fullmatch(r"python(?:3(?:\.\d+)?)?", command[0]):
        raise ValueError("pythonTrace expects a python, python3, or python3.x command")
    args = command[1:]
    if args and args[0] == "-B":
        args = args[1:]
    prefix = [command[0], "-B", "/octane-tracer.py"]
    if len(args) >= 2 and args[0] == "-m" and re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", args[1]):
        return [*prefix, "module", *args[1:]]
    if args and not args[0].startswith("-") and args[0].endswith(".py"):
        path = PurePosixPath(args[0])
        if not path.is_absolute() and ".." not in path.parts:
            return [*prefix, "script", *args]
    raise ValueError("pythonTrace supports python [-B] -m module or python [-B] project/script.py")


def _load_python_trace(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("rb") as stream:
            raw = stream.read(400_001)
        if len(raw) > 400_000:
            return None
        data = json.loads(raw)
    except (OSError, ValueError, RecursionError):
        return None
    if (not isinstance(data, dict) or set(data) != {"calls", "entries", "exceptions", "events", "truncated"}
            or type(data["events"]) is not int or not 0 <= data["events"] <= 50000 or type(data["truncated"]) is not bool
            or any(not isinstance(data[field], list) or len(data[field]) > 150
                   for field in ("calls", "entries", "exceptions"))):
        return None
    try:
        for row in data["entries"]:
            if not _valid_trace_symbol({key: row[key] for key in ("path", "function", "definition_line")}):
                return None
        for row in data["calls"]:
            if not all(_valid_trace_symbol(row[side]) for side in ("caller", "callee")):
                return None
        for row in data["exceptions"]:
            if not _valid_trace_symbol(row["function"]):
                return None
    except (TypeError, KeyError):
        return None
    nodes, edges = {}, {}
    for frame in data["entries"]:
        key = (frame["path"], frame["function"], frame["definition_line"])
        nodes[key] = {**frame, "line": frame["definition_line"]}
    for edge in data["calls"]:
        keys = []
        for frame in (edge["caller"], edge["callee"]):
            key = (frame["path"], frame["function"], frame["definition_line"])
            nodes[key] = {**frame, "line": frame["definition_line"]}
            keys.append(key)
        line = edge.get("call_line")
        if type(line) is not int:
            line = None
        edges[(*keys, line)] = None
    return {"available": True, **data, "mermaid": call_plot(nodes, edges)}


def _valid_trace_symbol(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {"path", "function", "definition_line"}:
        return False
    path = value["path"]
    if not isinstance(path, str) or not path or len(path) > 350:
        return False
    relative = PurePosixPath(path)
    return (not relative.is_absolute() and ".." not in relative.parts
            and isinstance(value["function"], str) and 0 < len(value["function"]) <= 200
            and type(value["definition_line"]) is int and value["definition_line"] >= 0)


def _copy_ignore(directory: str, names: list[str]) -> set[str]:
    excluded = {".git", ".venv", "venv", "node_modules", "__pycache__", ".tox", ".octane", ".octance", ".ssh", ".aws"}
    base = Path(directory)
    return {name for name in names if name in excluded or name.startswith(".env") or name.endswith(".env")
            or name in {"id_rsa", "id_ed25519", ".npmrc", ".netrc", ".pypirc"} or (base / name).is_symlink()
            or not (base / name).is_dir() and not (base / name).is_file()}


def _excluded(path: Path, root: Path) -> bool:
    current = root
    for part in path.relative_to(root).parts:
        if part in _copy_ignore(str(current), [part]):
            return True
        current /= part
    return False


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for directory, dirs, files in os.walk(root, followlinks=False):
        ignored = _copy_ignore(directory, dirs + files)
        dirs[:] = sorted(name for name in dirs if name not in ignored)
        for name in sorted(files):
            if name in ignored:
                continue
            path = Path(directory) / name
            file_hash = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    file_hash.update(chunk)
            digest.update(json.dumps([path.relative_to(root).as_posix(), stat.S_IMODE(path.stat().st_mode),
                                      file_hash.hexdigest()]).encode())
    return digest.hexdigest()


def _has_symlink_parent(path: Path, root: Path) -> bool:
    """Check every component before reading a counterpart from the other tree."""
    current = root
    if current.is_symlink():
        return True
    for part in path.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


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
