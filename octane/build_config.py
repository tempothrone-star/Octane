"""Review explicit build/config edits while preserving recognizable test settings."""
from __future__ import annotations

import configparser
from fnmatch import fnmatchcase
import json
from pathlib import Path, PurePosixPath
import re
import tomllib

BUILD_NAMES = {'pyproject.toml', 'package.json', 'cargo.toml', 'composer.json', 'pom.xml', 'setup.cfg', 'tox.ini'}


def is_build_path(relative):
    name = PurePosixPath(relative.replace('\\', '/')).name.casefold()
    return name in BUILD_NAMES or fnmatchcase(name, 'build.gradle*')


def _flatten(value, prefix=()):
    if isinstance(value, dict) and value:
        for key, item in value.items():
            yield from _flatten(item, (*prefix, str(key)))
    else:
        yield prefix, value


def _read_config(path):
    if path.stat().st_size > 1_000_000:
        raise ValueError('build configuration exceeds the 1 MB review limit')
    text = path.read_text()
    name = path.name.casefold()
    try:
        if name.endswith('.json'):
            data = json.loads(text)
        elif name.endswith('.toml'):
            data = tomllib.loads(text)
        elif name.endswith(('.ini', '.cfg')):
            parser = configparser.ConfigParser(interpolation=None)
            parser.optionxform = str
            parser.read_string(text)
            data = {section: dict(parser[section]) for section in parser.sections()}
        else:
            return None
    except (ValueError, configparser.Error) as exc:
        raise ValueError('cannot structurally review build configuration: ' + path.name) from exc
    if not isinstance(data, dict):
        raise ValueError('build configuration must contain an object or table')
    flattened = dict(_flatten(data))
    if len(flattened) > 10_000:
        raise ValueError('build configuration exceeds the key budget')
    return flattened


def _test_setting(name, key):
    key = tuple(part.casefold() for part in key)
    if name in {'package.json', 'composer.json'}:
        if key[:1] == ('scripts',):
            return len(key) == 1 or bool(re.search(r'test|spec|check|lint|coverage', key[1]))
        return bool(key and key[0] in {'jest', 'vitest', 'mocha', 'nyc', 'c8', 'ava', 'tap'})
    if name == 'pyproject.toml':
        return key == ('tool',) or (len(key) > 1 and key[0] == 'tool' and key[1] in
                                   {'pytest', 'coverage', 'tox', 'hypothesis', 'ruff', 'mypy', 'pyright'})
    if name == 'cargo.toml':
        return key[:1] in {('test',), ('bench',)} or key[:2] in {
            ('package', 'autotests'), ('package', 'autobenches'), ('lib', 'test'), ('lib', 'doctest')}
    if name.endswith(('.ini', '.cfg')):
        return bool(key and re.search(r'pytest|coverage|flake8|mypy', key[0])) or bool(
            len(key) > 1 and key[0].startswith('testenv') and key[1] in
            {'commands', 'commands_pre', 'commands_post', 'ignore_errors', 'ignore_outcome', 'skip_install', 'setenv'}) or key[:2] == ('tox', 'envlist')
    return False


def build_change_review(baseline: Path, candidate: Path, changed_paths, *, allowed=False, rationale=''):
    paths = [relative for relative in changed_paths if is_build_path(relative)]
    if type(allowed) is not bool:
        raise ValueError('allowBuildChanges must be a boolean')
    if paths and (not allowed or not isinstance(rationale, str) or not rationale.strip()):
        raise ValueError('build/config edits require allowBuildChanges and buildChangesRationale')
    reviews = []
    for relative in paths:
        before, after = _read_config(baseline / relative), _read_config(candidate / relative)
        keys = None
        if before is not None and after is not None:
            changed = sorted(key for key in before.keys() | after.keys()
                             if key not in before or key not in after or before[key] != after[key])
            if any(_test_setting(Path(relative).name.casefold(), key) for key in changed):
                raise ValueError('build override cannot change recognized test settings; establish a reviewed baseline first')
            keys = [{'key': '/' + '/'.join(part.replace('~', '~0').replace('/', '~1') for part in key),
                     'before_present': key in before, 'after_present': key in after,
                     'before': before.get(key), 'after': after.get(key)} for key in changed]
        reviews.append({'path': relative, 'key_diff': keys,
                        'review_basis': 'parsed keys' if keys is not None else 'text diff; independent test-selection review required'})
    return {'allowed': allowed, 'justification': rationale.strip() if paths else '', 'files': reviews}
