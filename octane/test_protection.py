"""Identify and fingerprint the baseline checks used for candidate executions."""
from __future__ import annotations

from fnmatch import fnmatchcase
import hashlib
import json
from pathlib import Path, PurePosixPath


TEST_DIRECTORIES = {"test", "tests", "spec", "specs", "__tests__", "fixtures", "__fixtures__", "testdata"}
_CONFIG_NAMES = {"conftest.py", "pytest.ini", "phpunit.xml", "phpunit.xml.dist", ".rspec"}
_PATTERNS = ("test_*", "*_test.*", "*.test.*", "*.spec.*", "*Test.java", "*Tests.cs", "*.phpt",
             "jest.config.*", "vitest.config.*", ".mocharc.*")


def is_test_path(path: str) -> bool:
    relative = PurePosixPath(path.replace("\\", "/"))
    return (any(part.casefold() in TEST_DIRECTORIES for part in relative.parts[:-1])
            or relative.name.casefold() in _CONFIG_NAMES
            or any(fnmatchcase(relative.name.casefold(), pattern.casefold()) for pattern in _PATTERNS))


def baseline_tests(root: Path) -> tuple[dict, list[str]]:
    """Return a content fingerprint and minimal read-only overlay roots."""
    digest = hashlib.sha256()
    mounts = set()
    count = 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if path.is_symlink():
            raise ValueError("baseline tests must not contain symlinks")
        if not path.is_file() or not is_test_path(relative.as_posix()):
            continue
        count += 1
        content = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                content.update(chunk)
        digest.update(json.dumps([relative.as_posix(), path.stat().st_mode & 0o777,
                                  content.hexdigest()]).encode())
        directory = next((index for index, part in enumerate(relative.parts[:-1])
                          if part.casefold() in TEST_DIRECTORIES), None)
        mount = Path(*relative.parts[:directory + 1]) if directory is not None else relative
        mounts.add(mount.as_posix())
    return {"baseline_hash": digest.hexdigest(), "file_count": count, "original_tests": True}, sorted(mounts)
