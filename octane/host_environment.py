"""Explicit environment boundaries for trusted host integration processes."""
from __future__ import annotations

import os
import re

LS_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TMP", "TEMP", "SYSTEMROOT", "WINDIR")


def language_server_environment(extra_keys=()):
    if (not isinstance(extra_keys, (list, tuple)) or len(extra_keys) > 32
            or any(not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", key)
                   for key in extra_keys)):
        raise ValueError("environmentAllowlist must contain up to 32 environment variable names")
    return {key: os.environ[key] for key in (*LS_ENV_KEYS, *extra_keys) if key in os.environ}


def git_environment():
    keys = ("PATH", "SYSTEMROOT", "WINDIR", "TMP", "TEMP", "TMPDIR")
    return {**{key: os.environ[key] for key in keys if key in os.environ},
            "LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}


def git_arguments(repository, *arguments):
    return ['git', '-C', str(repository), '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=' + os.devnull,
            '-c', 'uploadpack.packObjectsHook=', *arguments]
