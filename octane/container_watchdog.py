"""Independent crash deadline helper; deliberately standard-library only."""
import json
import sqlite3
import subprocess
import sys
import time


def enforce(engine, name, deadline, database, run_id):
    # The calling bridge owns the original execution deadline; this is the crash backstop.
    while time.time() < deadline:
        with sqlite3.connect(database, timeout=5) as db:
            row = db.execute('SELECT status FROM execution_runs WHERE id=?', (run_id,)).fetchone()
        if row is not None and row[0] in {'passed', 'failed', 'timed_out'}:
            return
        time.sleep(min(1, max(0, deadline - time.time())))
    result = subprocess.run([engine, 'rm', '--force', name], capture_output=True, timeout=5)
    if result.returncode == 0 or b'No such container' in result.stderr or b'no such container' in result.stderr:
        with sqlite3.connect(database, timeout=5) as db:
            db.execute("UPDATE execution_runs SET status='timed_out',result_json=? WHERE id=? AND status='running'",
                       (json.dumps({'interrupted': True, 'reason': 'independent container deadline'}), run_id))


if __name__ == '__main__':
    enforce(sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4], sys.argv[5])
