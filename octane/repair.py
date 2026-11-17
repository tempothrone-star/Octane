"""Find a small, read-only starting point for a repair in a known checkout."""
from __future__ import annotations

import configparser
import json
import os
import shlex
import tomllib
from itertools import islice
from pathlib import Path
from typing import Any

_CONFIG_BYTES = 64 * 1024


def _inside(root: Path, path: str) -> Path:
    candidate = (root / path).resolve()
    if not candidate.is_relative_to(root):
        raise PermissionError("repair path is outside the selected checkout")
    return candidate


def _present(root: Path, path: str, *, directory: bool = False) -> bool:
    try:
        candidate = _inside(root, path)
        return candidate.is_dir() if directory else candidate.is_file()
    except (OSError, RuntimeError):
        return False


def _config(root: Path, path: str) -> str:
    try:
        candidate = _inside(root, path)
        if candidate.is_file():
            with candidate.open("rb") as file:
                data = file.read(_CONFIG_BYTES + 1)
            if len(data) <= _CONFIG_BYTES:
                return data.decode("utf-8-sig")
    except (OSError, RuntimeError, UnicodeError):
        pass
    return ""


def _pytest_config(root: Path) -> str | None:
    for path in ("pytest.ini", ".pytest.ini"):
        if _present(root, path):
            return path
    try:
        tool = tomllib.loads(_config(root, "pyproject.toml")).get("tool", {})
        if isinstance(tool, dict) and isinstance(tool.get("pytest"), dict):
            return "pyproject.toml"
    except (ValueError, RecursionError):
        pass
    for path, section in (("tox.ini", "pytest"), ("setup.cfg", "tool:pytest")):
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read_string(_config(root, path))
        except configparser.Error:
            continue
        if parser.has_section(section):
            return path
    return None


def _package_check(root: Path) -> dict[str, Any] | None:
    try:
        package = json.loads(_config(root, "package.json"))
    except (ValueError, RecursionError):
        return None
    scripts = package.get("scripts") if isinstance(package, dict) else None
    test = scripts.get("test") if isinstance(scripts, dict) else None
    if not isinstance(test, str) or not test.strip() or "no test specified" in test.lower():
        return None
    manager = str(package.get("packageManager", "")).split("@", 1)[0]
    if manager in {"pnpm", "yarn", "bun"}:
        return {"command": [manager, "run", "test"], "source": "package.json"}
    command = {"command": ["npm", "test"], "source": "package.json"}
    try:
        args = shlex.split(test)
    except ValueError:
        args = []
    if args[:1] == ["mocha"] and "--" not in args and not any(char in test for char in "&;|<>`$\n"):
        command["quiet_command"] = ["npm", "test", "--", "--reporter", "dot"]
    return command


def _ecosystem_checks(root: Path) -> list[dict[str, Any]]:
    checks = []
    runners = (
        (("pom.xml",), ["mvn", "test"], "mvnw"),
        (("build.gradle", "build.gradle.kts"), ["gradle", "test"], "gradlew"),
        (("build.sbt",), ["sbt", "test"], None),
        (("Package.swift",), ["swift", "test"], None),
        (("mix.exs",), ["mix", "test"], None),
        (("cabal.project",), ["cabal", "test"], None),
    )
    for markers, command, wrapper in runners:
        source = next((path for path in markers if _present(root, path)), None)
        if source:
            if wrapper and _present(root, wrapper) and os.access(_inside(root, wrapper), os.X_OK):
                command = [f"./{wrapper}", *command[1:]]
            checks.append({"command": command, "source": source})

    if _present(root, "Gemfile") and _present(root, "Rakefile"):
        checks.append({"command": ["bundle", "exec", "rake", "test"], "source": "Rakefile"})
    try:
        composer = json.loads(_config(root, "composer.json"))
    except (ValueError, RecursionError):
        composer = None
    scripts = composer.get("scripts") if isinstance(composer, dict) else None
    test = scripts.get("test") if isinstance(scripts, dict) else None
    if (isinstance(test, str) and test.strip()) or (isinstance(test, list) and test and all(isinstance(item, str) for item in test)):
        checks.append({"command": ["composer", "run-script", "test"], "source": "composer.json"})

    try:
        names = [entry.name for entry in islice(root.iterdir(), 257)]
    except OSError:
        names = []
    if len(names) <= 256:
        solutions = sorted(name for name in names if name.endswith((".sln", ".slnx")) and _present(root, name))
        projects = sorted(name for name in names if name.endswith((".csproj", ".fsproj", ".vbproj")) and _present(root, name))
        candidates = solutions or projects
        if len(candidates) == 1:
            checks.append({"command": ["dotnet", "test", f"./{candidates[0]}"], "source": candidates[0]})
    return checks


def _php_source_check(root: Path, target: Path | None) -> dict[str, Any] | None:
    if not all(_present(root, path) for path in ("buildconf", "configure.ac", "main/php.h", "run-tests.php")):
        return None
    if target is not None and target.suffix == ".phpt":
        scope = target.relative_to(root).as_posix()
    else:
        scopes = ["tests/lang", "Zend/tests"]
        if target is not None:
            parts = target.relative_to(root).parts
            if len(parts) >= 3 and parts[0] == "ext":
                scopes.insert(0, f"ext/{parts[1]}/tests")
            elif parts[0] == "Zend":
                scopes.reverse()
        scope = next((path for path in scopes if _present(root, path, directory=True)), None)
    if scope is None:
        return None

    setup = []
    cli = "sapi/cli/php"
    if not (_present(root, cli) and os.access(_inside(root, cli), os.X_OK)):
        if not _present(root, "Makefile"):
            if not _present(root, "configure"):
                setup.append(["./buildconf"])
            setup.append(["./configure", "--disable-all", "--enable-cli", "--disable-cgi", "--disable-phpdbg"])
        setup.append(["make", "-j2", cli])
    return {"command": [f"./{cli}", "-n", "run-tests.php", "-P", "-q", "-j2", scope],
            "source": "run-tests.php", "scope": scope, "setup_commands": setup}


def repair_triage(root: Path, path: str | None) -> dict[str, Any]:
    target = None
    if path is not None:
        target = _inside(root, path)
        if not target.is_file():
            raise ValueError("repair path must name an existing file in the selected checkout")

    checks = []
    php_check = _php_source_check(root, target)
    if php_check is not None:
        checks.append(php_check)
    pytest_config = _pytest_config(root)
    if pytest_config:
        checks.append({"command": ["python", "-m", "pytest", "-q"], "source": pytest_config})
    package_check = _package_check(root)
    if package_check:
        checks.append(package_check)
    if _present(root, "go.mod"):
        checks.append({"command": ["go", "test", "./..."], "source": "go.mod"})
    if _present(root, "Cargo.toml"):
        checks.append({"command": ["cargo", "test"], "source": "Cargo.toml"})
    checks.extend(_ecosystem_checks(root))

    related_tests = []
    if target is not None and target.suffix == ".py":
        for directory in ("tests", "test", target.parent.relative_to(root).as_posix()):
            for name in (f"test_{target.stem}.py", f"{target.stem}_test.py"):
                candidate = (Path(directory) / name).as_posix()
                if candidate not in related_tests and _present(root, candidate) and _inside(root, candidate) != target:
                    related_tests.append(candidate)

    if php_check and php_check["setup_commands"]:
        next_action = ("Run setup_commands in order in an isolated writable build copy, then run the PHPT baseline with "
                       "the CLI built from that source. A failed build is a setup result. The minimal configuration may "
                       "skip tests that require additional extensions.")
    elif checks:
        next_action = "Run the relevant baseline command in the isolated checkout's working directory before reading source."
    else:
        next_action = "Look for one supported check in the project's test or contract locations before reading source."
    return {
        "next_action": next_action + " Use checkout-relative paths and the current working directory in a sandbox. The root is for Octane's host-side tools.",
        "execution_directory": ".",
        "if_baseline_passes_without_a_reported_symptom": (
            "Read one relevant test, entry point, and contract source. Run one probe of an uncovered behavior. "
            "If it does not reproduce a defect, report the checks and stop."
        ),
        "target": target.relative_to(root).as_posix() if target else None,
        "baseline_candidates": checks,
        "related_test_candidates": related_tests,
        "test_locations": [path for path in ("tests", "test", "spec", "__tests__")
                           if _present(root, path, directory=True)],
        "contract_locations": [path for path in ("README.md", "Readme.md", "readme.md", "README.rst", "CHANGELOG.md",
                                                   "HISTORY.md", "History.md", "CHANGES.rst", "NEWS", "UPGRADING",
                                                   "CONTRIBUTING.md", "SECURITY.md", "docs")
                               if _present(root, path, directory=path == "docs")],
    }
