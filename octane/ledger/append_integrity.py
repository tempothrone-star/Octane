"""Bounded append verification; full history/replay remain explicit integrity operations."""
import hashlib
import json


def verify_append(conn, metadata_path, project_id):
    from .core import ZERO_HASH, canonical, OctaneError
    head = conn.execute('SELECT seq,hash FROM event_log ORDER BY seq DESC LIMIT 1').fetchone()
    if head is None:
        return
    anchor_seq, previous = 0, ZERO_HASH
    try:
        if metadata_path.exists():
            anchor = json.loads(metadata_path.read_text())['log_anchor']
            anchor_seq, previous = anchor['seq'], anchor['hash']
            if type(anchor_seq) is not int or anchor_seq < 1 or not isinstance(previous, str):
                raise ValueError('invalid anchor')
            anchored = conn.execute('SELECT hash FROM event_log WHERE seq=?', (anchor_seq,)).fetchone()
            if not anchored or anchored['hash'] != previous or head['seq'] < anchor_seq:
                raise ValueError('anchor differs from event log')
        # Include the anchored event itself to detect a mutated tail payload.
        start = max(1, anchor_seq)
        parent = conn.execute('SELECT hash FROM event_log WHERE seq=?', (start - 1,)).fetchone()
        previous = parent['hash'] if parent else ZERO_HASH
        rows = conn.execute('SELECT * FROM event_log WHERE seq>=? ORDER BY seq', (start,))
        for seq, row in enumerate(rows, start):
            core = {key: row[key] for key in ('seq', 'project_id', 'agent_id', 'op', 'payload_json', 'idempotency_key', 'prev_hash', 'timestamp')}
            actual = hashlib.sha256((previous + canonical(core)).encode()).hexdigest()
            if row['seq'] != seq or row['project_id'] != project_id or row['prev_hash'] != previous or actual != row['hash']:
                raise ValueError('append tail hash or sequence mismatch')
            previous = actual
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise OctaneError('refusing to append to a damaged event log: ' + str(exc), 1, 'integrity_error') from exc
