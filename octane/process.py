"""Bounded transport for explicitly selected host integration executables."""
from __future__ import annotations

import os
import signal
import select
import sys
import subprocess
import threading
import time


def wait_unreaped(process, timeout):
    """Keep the leader PID reserved until its private group has been signaled."""
    if os.name != 'posix':
        return process.wait(timeout=timeout)
    if not hasattr(os, 'waitid'):
        return _wait_kqueue(process, timeout)
    deadline = time.monotonic() + timeout
    while True:
        state = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        if state is not None:
            return state.si_status if state.si_code == os.CLD_EXITED else -state.si_status
        if time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired(process.args, timeout)
        time.sleep(0.005)


def _wait_kqueue(process, timeout):
    # macOS Python 3.12 lacks waitid; NOTE_EXIT observes without collecting the PID.
    with select.kqueue() as events:
        change = select.kevent(process.pid, filter=select.KQ_FILTER_PROC,
                               flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT, fflags=select.KQ_NOTE_EXIT)
        try:
            ready = events.control([change], 1, timeout)
        except ProcessLookupError:
            return  # Exited before registration; the unreaped child still reserves its PID.
        if not ready:
            raise subprocess.TimeoutExpired(process.args, timeout)


def stop_process_group(process):
    if os.name == 'posix' and process.returncode is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            if sys.platform != 'darwin':
                raise
            # Darwin returns EPERM for a group containing only its zombie leader.
            # Popen.kill safely checks the directly owned child before signaling it.
            process.kill()
    elif process.poll() is None:
        process.kill()


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
        wait_unreaped(process, timeout)
    finally:
        # A finished integration may have left descendants holding its pipes open.
        # Kill its private process group before draining, including on timeout.
        stop_process_group(process)
        code = process.wait(timeout=5)
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
