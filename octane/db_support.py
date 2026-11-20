"""Shared storage identities, path validation, and credential redaction."""
from __future__ import annotations
import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit, urlunsplit

def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitize_repository_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme and parsed.netloc:
        return urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, "", ""))
    if "@" in value and ":" in value.split("@", 1)[1]:
        return value.split("@", 1)[1]
    return value




def _scrub_sensitive_text(value: str) -> str:
    """Redact common bearer/basic-auth and assignment secrets before persistence."""
    import re
    value = re.sub(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", "[REDACTED PRIVATE KEY]", value, flags=re.S)
    value = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer [REDACTED]", value)
    value = re.sub(r"(?i)(://)[^/@:\s]+:[^/@\s]+@", r"\1[REDACTED]@", value)
    value = re.sub(r"(?i)((?:[A-Za-z0-9_-]*(?:password|passwd|secret|token|api[_-]?key|credential|private[_-]?key)[A-Za-z0-9_-]*)\s*[:=]\s*)([^\s,;]+)", r"\1[REDACTED]", value)
    value = re.sub(r"(?i)\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16})\b", "[REDACTED TOKEN]", value)
    return value

def _git_remote_url(root: Path) -> str | None:
    try:
        result = subprocess.run(["git", "-C", str(root), "remote", "get-url", "origin"],
                                capture_output=True, text=True, timeout=2, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return _sanitize_repository_url(value) if value else None


def _project_relative_path(value: str | None) -> str | None:
    if value is None:
        return None
    path = PurePosixPath(value)
    if (path.is_absolute() or not path.parts or ".." in path.parts
            or (len(path.parts[0]) == 2 and path.parts[0][1] == ":")):
        raise ValueError("source paths must be project-relative")
    return path.as_posix()


def _normalize_languages(languages) -> list[str]:
    if isinstance(languages, str):
        languages = [languages]
    try:
        values = list(languages)
    except TypeError as exc:
        raise TypeError("languages must be a string or iterable of strings") from exc
    if any(not isinstance(language, str) or not language.strip() for language in values):
        raise ValueError("language names must be non-empty strings")
    return sorted({language.strip() for language in values})


def _scrub_sensitive_value(value: Any) -> Any:
    if isinstance(value, str):
        return _scrub_sensitive_text(value)
    if isinstance(value, dict):
        return {key: _scrub_sensitive_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub_sensitive_value(item) for item in value]
    return value


def _reject_secret_keys(value: Any, parent: str = "") -> None:
    sensitive = ("password", "secret", "token", "api_key", "apikey", "credential", "private_key")
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = re.sub(r"[^a-z0-9]+", "_", str(key).casefold()).strip("_")
            if any(term in normalized for term in sensitive):
                raise ValueError(f"project configuration key {parent + str(key)!r} resembles a secret; secrets are not stored")
            _reject_secret_keys(nested, parent + str(key) + ".")
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _reject_secret_keys(nested, parent)
