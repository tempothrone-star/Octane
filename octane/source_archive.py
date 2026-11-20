"""Read Git revisions into bounded, filtered disposable source trees."""
from __future__ import annotations

import io
import os
from pathlib import Path, PurePosixPath
import re
import tarfile

from .process import run_bounded
from .workspace import excluded_name


def git(repository, *arguments):
    result = run_bounded(["git", "-C", str(repository), *arguments], limit=100_000_000, env={**os.environ, "LC_ALL": "C"})
    if result.returncode:
        if arguments[:1] == ("bisect",) and result.returncode == 2 and b"could be any of" in result.stdout:
            return result.stdout
        raise ValueError("Git operation failed: " + result.stderr.decode("utf-8", "replace")[:1000])
    return result.stdout


def resolve_revision(repository, reference):
    value = git(repository, "rev-parse", "--verify", "--end-of-options", reference + "^{commit}").decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value):
        raise ValueError("revision must resolve to a full Git commit")
    return value


def export_revision(repository, revision, destination: Path):
    archive = git(repository, "archive", "--format=tar", revision)
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    total, files = 0, 0
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as source:
        for member in source:
            relative = PurePosixPath(member.name)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Git archive contains an unsafe path")
            if any(excluded_name(part) for part in relative.parts):
                continue
            if not member.isfile() and not member.isdir():
                raise ValueError("Git archive contains a symlink or special file")
            target = destination.joinpath(*relative.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            total += member.size
            files += 1
            if total > 100_000_000 or files > 100_000:
                raise ValueError("Git archive exceeds the source budget")
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.extractfile(member) as stream:
                with target.open("wb") as output:
                    while chunk := stream.read(65536):
                        output.write(chunk)
            target.chmod(member.mode & 0o777)
    return destination
