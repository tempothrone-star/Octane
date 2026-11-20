"""Bounded transport for explicitly selected host integration executables."""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time


def run_bounded(arguments, *, timeout=60, limit=4_000_000, cwd=None, env=None):
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    overflow = set()

    def drain(stream, name):
        for chunk in iter(lambda: os.read(stream.fileno(), 65536), b""):
            remaining = limit - len(buffers[name])
            buffers[name].extend(chunk[:max(remaining, 0)])
            if len(chunk) > remaining:
                overflow.add(name)

    process = subprocess.Popen(arguments, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=os.name == "posix")
    readers = [threading.Thread(target=drain, args=(stream, name), daemon=True)
               for name, stream in (("stdout", process.stdout), ("stderr", process.stderr))]
    deadline = time.monotonic() + timeout
    for reader in readers:
        reader.start()
    try:
        code = process.wait(timeout=timeout)
    finally:
        # A finished integration may have left descendants holding its pipes open.
        # Kill its private process group before draining, including on timeout.
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        for reader in readers:
            reader.join(timeout=max(0.01, deadline - time.monotonic()))
        if not any(reader.is_alive() for reader in readers):
            process.stdout.close()
            process.stderr.close()
    if any(reader.is_alive() for reader in readers):
        raise subprocess.TimeoutExpired(arguments, timeout)
    if overflow:
        raise ValueError("integration response exceeds its output budget")
    return subprocess.CompletedProcess(arguments, code, bytes(buffers["stdout"]), bytes(buffers["stderr"]))
