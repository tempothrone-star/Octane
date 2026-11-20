"""Private local storage shared by the ledger and Gateway database."""
from pathlib import Path
import os


def private_directory(path: Path, *, tighten_existing: bool = False) -> None:
    existed = path.exists()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix" and (not existed or tighten_existing):
        path.chmod(0o700)


def prepare_database(path: Path) -> None:
    private_directory(path.parent)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
    if os.name == "posix":
        for suffix in ("-wal", "-shm"):
            try:
                descriptor = os.open(str(path) + suffix, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
            except FileNotFoundError:
                continue
            try:
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
