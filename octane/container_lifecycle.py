"""Container ownership, crash deadlines and recovery without touching unrelated jobs."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager

from .host_environment import git_environment

OWNER_LABEL = 'io.octane.owner'
DEADLINE_LABEL = 'io.octane.deadline'
RUN_LABEL = 'io.octane.run'


def engine_environment():
    # Trusted cleanup must reach the same daemon/context as the original engine client.
    keys = ('HOME', 'DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_CONFIG', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH',
            'CONTAINER_HOST', 'CONTAINER_CONNECTION', 'XDG_RUNTIME_DIR', 'XDG_CONFIG_HOME', 'CONTAINERS_CONF',
            'DBUS_SESSION_BUS_ADDRESS', 'SSH_AUTH_SOCK', 'SSL_CERT_FILE', 'SSL_CERT_DIR')
    return {**git_environment(), **{key: os.environ[key] for key in keys if key in os.environ}}


def owner_key(storage):
    return hashlib.sha256(str(Path(storage).resolve()).encode()).hexdigest()[:32]


def lifecycle_arguments(storage, run_id, deadline):
    return ['--name', 'octane-' + run_id.lower(), '--label', OWNER_LABEL + '=' + owner_key(storage),
            '--label', DEADLINE_LABEL + '=' + str(deadline), '--label', RUN_LABEL + '=' + run_id]


def remove_container(engine, name):
    try:
        result = subprocess.run([engine, 'rm', '--force', name], capture_output=True, timeout=5)
        return result.returncode == 0 or b'No such container' in result.stderr or b'no such container' in result.stderr
    except (OSError, subprocess.SubprocessError):
        return False


def finish_interrupted(database, run_id, status='execution_error'):
    """Do not overwrite a normally completed run, even after a watcher/reaper race."""
    with sqlite3.connect(database, timeout=5) as db:
        db.execute("UPDATE execution_runs SET status=?,result_json=? WHERE id=? AND status='running'",
                   (status, json.dumps({'interrupted': True, 'reason': 'container deadline or bridge termination'}), run_id))


def reap_expired(engine, storage, database):
    """Recover expired containers with this exact storage owner; concurrent live jobs survive."""
    result = subprocess.run([engine, 'ps', '-a', '--filter', 'label=' + OWNER_LABEL + '=' + owner_key(storage),
                             '--format', '{{json .}}'], capture_output=True, text=True, timeout=5)
    if result.returncode:
        raise RuntimeError('cannot inspect owned containers for recovery')
    recovered = []
    for line in result.stdout.splitlines():
        row = json.loads(line)
        name = row.get('Names', '')
        if not name.startswith('octane-run-'):
            continue
        details = subprocess.run([engine, 'inspect', name], capture_output=True, text=True, timeout=5)
        if details.returncode:
            continue  # Another owner/watchdog may have completed it.
        labels = json.loads(details.stdout)[0].get('Config', {}).get('Labels') or {}
        if labels.get(OWNER_LABEL) != owner_key(storage):
            continue
        try:
            expired = float(labels[DEADLINE_LABEL]) <= time.time()
            run_id = labels[RUN_LABEL]
        except (KeyError, TypeError, ValueError):
            continue
        if expired and remove_container(engine, name):
            finish_interrupted(database, run_id, 'timed_out')
            recovered.append(run_id)
    recovered.extend(_reap_rows(engine, storage, database, recovered))
    return recovered


def register_deadline(database, run_id, storage, deadline):
    with sqlite3.connect(database, timeout=5) as db:
        db.execute("UPDATE execution_runs SET result_json=? WHERE id=? AND status='running'",
                   (json.dumps({'container_deadline': deadline, 'container_owner': owner_key(storage)}), run_id))


def _reap_rows(engine, storage, database, recovered):
    with sqlite3.connect(database, timeout=5) as db:
        rows = db.execute("SELECT id,result_json FROM execution_runs WHERE status='running' LIMIT 1000").fetchall()
    expired = []
    for run_id, raw in rows:
        if run_id in recovered:
            continue
        data = json.loads(raw)
        if data.get('container_owner') != owner_key(storage):
            continue
        deadline = data.get('container_deadline')
        if type(deadline) not in (int, float) or deadline > time.time():
            continue
        if remove_container(engine, 'octane-' + run_id.lower()):
            finish_interrupted(database, run_id, 'timed_out')
            expired.append(run_id)
    return expired


@contextmanager
def execution_guard(engine, storage, database, run_id, deadline):
    """An independent stdlib watcher survives abrupt SIGKILL of the bridge process."""
    name = 'octane-' + run_id.lower()
    watcher = None
    previous = None
    changed_signal = False
    try:
        # Direct file invocation also works for installed wheels and scrubbed envs.
        watcher = subprocess.Popen([sys.executable, str(Path(__file__).with_name('container_watchdog.py')),
                                    engine, name, str(deadline), str(database), run_id],
                                   env=engine_environment(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, start_new_session=os.name == 'posix')
        import threading
        if threading.current_thread() is threading.main_thread():
            previous = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
            changed_signal = True
        yield
    finally:
        if changed_signal:
            signal.signal(signal.SIGTERM, previous)
        # Idempotent removal by engine-assigned name also covers errors before a cid file exists.
        removed = remove_container(engine, name)
        if watcher is not None and removed:
            watcher.terminate()
            watcher.wait(timeout=5)
        finish_interrupted(database, run_id)
