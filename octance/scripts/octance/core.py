"""Deterministic, standard-library-only ledger used by the Octance skill."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

PACKAGE_ROOT = Path(__file__).resolve().parent
MIGRATIONS = PACKAGE_ROOT / "migrations"
ZERO_HASH = "0" * 64
EVIDENCE_TYPES = {
    "source_observation", "function_io", "runtime_trace", "test_result", "exception",
    "dependency", "file_hash", "config_observation", "execution_result",
    "static_analysis", "user_requirement", "documentation",
}
CLAIM_KINDS = {"observation", "inference", "hypothesis", "intent", "decision", "requirement", "limitation", "open_question"}
CLAIM_STATUSES = {"asserted", "corroborated", "contradicted", "stale", "needs_review", "retracted", "quarantined"}
ROLE_PERMISSIONS = {
    "explorer": {"evidence.add", "evidence.read", "evidence.search", "claim.add", "claim.read", "claim.search", "claim.transition", "brief", "status"},
    "investigator": {"evidence.add", "evidence.read", "evidence.search", "evidence.link", "claim.add", "claim.read", "claim.search", "claim.link", "claim.transition", "brief", "status"},
    "tester": {"evidence.add", "evidence.read", "evidence.search", "claim.read", "claim.search", "claim.transition", "brief", "status"},
    "documenter": {"evidence.add", "evidence.read", "evidence.search", "claim.add", "claim.read", "claim.search", "claim.transition", "brief", "status"},
    "admin": {"*"},
}
_SECRET_KEY = re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|credential|private[_-]?key)")
_SECRET_ASSIGNMENT = re.compile(r"(?i)((?:[A-Za-z0-9_-]*(?:password|passwd|secret|token|api[_-]?key|credential)[A-Za-z0-9_-]*)\s*[:=]\s*)([^\s,;]+)")
_SECRET_TOKEN = re.compile(r"(?i)(?:\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16})\b|\bBearer\s+[A-Za-z0-9._~+/-]+=*|(?<=://)[^/@:\s]+:[^/@\s]+@)")
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)
_EXCLUDED_DIRS = {".git", ".octance", "__pycache__", ".venv", "venv", "node_modules", "dist", "build", ".tox"}


class OctanceError(Exception):
    def __init__(self, message: str, code: int = 1, kind: str = "error"):
        super().__init__(message)
        self.code, self.kind = code, kind


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    if not slug or len(slug) > 48:
        raise OctanceError("agent name must contain 1 to 48 lowercase letters, digits, or hyphens", 2, "invalid_input")
    return slug


def _redact(value: Any, count: List[int], key: str = "") -> Any:
    if _SECRET_KEY.search(key):
        count[0] += 1
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): _redact(v, count, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, count, key) for v in value]
    if not isinstance(value, str):
        return value
    def replace(match: re.Match) -> str:
        count[0] += 1
        return "[REDACTED PRIVATE KEY]"
    value = _PRIVATE_KEY.sub(replace, value)
    def replace_assignment(match: re.Match) -> str:
        count[0] += 1
        return match.group(1) + "[REDACTED]"
    value = _SECRET_ASSIGNMENT.sub(replace_assignment, value)
    def replace_token(_match: re.Match) -> str:
        count[0] += 1
        return "[REDACTED TOKEN]"
    return _SECRET_TOKEN.sub(replace_token, value)


def _atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        temporary.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(str(temporary), str(path))
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def revision_for(root: Path) -> str:
    digest = hashlib.sha256()
    for current, dirs, files in os.walk(str(root), followlinks=False):
        base = Path(current)
        dirs[:] = sorted(d for d in dirs if d not in _EXCLUDED_DIRS and not (base / d).is_symlink())
        for filename in sorted(files):
            path = base / filename
            if path.is_symlink() or not path.is_file():
                continue
            digest.update(path.relative_to(root).as_posix().encode("utf-8", "surrogateescape") + b"\0")
            try:
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
            except OSError:
                digest.update(b"[unreadable]")
            digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def resolve_project_root(start: Optional[Path] = None, explicit: Optional[str] = None) -> Path:
    candidate = Path(explicit).expanduser().resolve() if explicit else (start or Path.cwd()).resolve()
    if not candidate.is_dir():
        raise OctanceError("project root is not an existing directory", 2, "invalid_input")
    try:
        result = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=str(candidate),
                                capture_output=True, text=True, timeout=2, check=False)
        if result.returncode == 0 and result.stdout.strip():
            git_root = Path(result.stdout.strip()).resolve()
            if not explicit or _within(candidate, git_root):
                return git_root
    except (OSError, subprocess.TimeoutExpired):
        pass
    return candidate


def discover_root(start: Optional[Path] = None) -> Path:
    current = (start or Path.cwd()).resolve()
    for candidate in (current,) + tuple(current.parents):
        state = candidate / ".octance"
        if (state / "project.json").is_file() and (state / "octance.db").is_file():
            return candidate
    raise OctanceError("no Octance project found; run `python3 scripts/octance.py init` at the project root", 2, "not_initialized")


def _filesystem_type(path: Path) -> Optional[str]:
    if sys.platform.startswith("linux"):
        try:
            best: Tuple[int, Optional[str]] = (0, None)
            for line in Path("/proc/mounts").read_text(encoding="utf-8").splitlines():
                fields = line.split()
                if len(fields) >= 3:
                    mount = Path(fields[1].replace("\\040", " ")).resolve()
                    if _within(path.resolve(), mount) and len(str(mount)) > best[0]:
                        best = (len(str(mount)), fields[2])
            return best[1]
        except OSError:
            return None
    if sys.platform == "darwin":
        try:
            result = subprocess.run(["mount"], capture_output=True, text=True, timeout=2, check=False)
            for line in result.stdout.splitlines():
                if " on " in line and " (" in line:
                    mount = Path(line.split(" on ", 1)[1].split(" (", 1)[0]).resolve()
                    if _within(path.resolve(), mount):
                        return line.rsplit(" (", 1)[-1].rstrip(")").split(",", 1)[0]
        except (OSError, subprocess.TimeoutExpired):
            pass
    return None


class Ledger:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.fts5 = False
        self._migrate()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _migrate(self) -> None:
        conn = sqlite3.connect(str(self.db_path), timeout=5, isolation_level=None)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA journal_mode=WAL")
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "schema_version" in tables:
                row = conn.execute("SELECT version FROM schema_version").fetchone()
                version = int(row[0]) if row else 0
            else:
                version = 0
            if version > 1:
                raise OctanceError("database schema is newer than this Octance CLI", 1, "schema_too_new")
            for number in range(version + 1, 2):
                migration = MIGRATIONS / ("%03d_initial.sql" % number)
                if not migration.is_file():
                    raise OctanceError("missing database migration: " + str(migration), 1, "migration_missing")
                conn.execute("BEGIN IMMEDIATE")
                try:
                    pending = ""
                    for line in migration.read_text(encoding="utf-8").splitlines(True):
                        pending += line
                        if sqlite3.complete_statement(pending):
                            if pending.strip():
                                conn.execute(pending)
                            pending = ""
                    if pending.strip():
                        raise OctanceError("incomplete SQL migration")
                    if number == 1:
                        conn.execute("INSERT INTO schema_version(version) VALUES(1)")
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("CREATE TABLE IF NOT EXISTS claim_status_events (id INTEGER PRIMARY KEY AUTOINCREMENT, claim_id TEXT NOT NULL REFERENCES claims(claim_id), previous_status TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('asserted','corroborated','contradicted','stale','needs_review','retracted','quarantined')), agent_id TEXT NOT NULL REFERENCES agents(agent_id), created_at TEXT NOT NULL)")
            conn.execute("CREATE INDEX IF NOT EXISTS claim_status_events_claim ON claim_status_events(claim_id,id)")
            conn.execute("CREATE TRIGGER IF NOT EXISTS claim_status_events_no_update BEFORE UPDATE ON claim_status_events BEGIN SELECT RAISE(ABORT, 'claim status events are append-only'); END")
            conn.execute("CREATE TRIGGER IF NOT EXISTS claim_status_events_no_delete BEFORE DELETE ON claim_status_events BEGIN SELECT RAISE(ABORT, 'claim status events are append-only'); END")
            conn.execute("CREATE TABLE IF NOT EXISTS octance_internal_schema(version INTEGER NOT NULL)")
            fts_version = conn.execute("SELECT version FROM octance_internal_schema LIMIT 1").fetchone()
            if not fts_version or int(fts_version[0]) < 2:
                conn.execute("DROP TABLE IF EXISTS evidence_fts")
                conn.execute("DELETE FROM octance_internal_schema")
                conn.execute("INSERT INTO octance_internal_schema(version) VALUES(2)")
            conn.commit()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS evidence_fts USING fts5(evidence_id UNINDEXED,project_id UNINDEXED,type UNINDEXED,description,payload,file,symbol)")
                conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS claims_fts USING fts5(claim_id UNINDEXED,project_id UNINDEXED,kind UNINDEXED,status UNINDEXED,text)")
                conn.execute("INSERT INTO evidence_fts(evidence_id,project_id,type,description,payload,file,symbol) SELECT e.evidence_id,e.project_id,e.type,e.description,e.payload_json,COALESCE(e.file,''),COALESCE(e.symbol,'') FROM evidence e WHERE NOT EXISTS (SELECT 1 FROM evidence_fts f WHERE f.evidence_id=e.evidence_id)")
                conn.execute("INSERT INTO claims_fts(claim_id,project_id,kind,status,text) SELECT c.claim_id,c.project_id,c.kind,c.status,c.text FROM claims c WHERE NOT EXISTS (SELECT 1 FROM claims_fts f WHERE f.claim_id=c.claim_id)")
                conn.commit()
                self.fts5 = True
            except sqlite3.DatabaseError:
                conn.rollback()
                self.fts5 = False
        finally:
            conn.close()

    @staticmethod
    def next_id(conn: sqlite3.Connection, prefix: str) -> str:
        row = conn.execute("SELECT value FROM counters WHERE prefix=?", (prefix,)).fetchone()
        value = (int(row[0]) if row else 0) + 1
        conn.execute("INSERT INTO counters(prefix,value) VALUES(?,?) ON CONFLICT(prefix) DO UPDATE SET value=excluded.value", (prefix, value))
        return "%s-%04d" % (prefix, value)

    def event(self, conn: sqlite3.Connection, project_id: str, agent_id: str, op: str,
              request: Dict[str, Any], builder: Callable[[sqlite3.Connection, str], Dict[str, Any]],
              apply: Callable[[sqlite3.Connection, Dict[str, Any]], None],
              explicit_key: Optional[str] = None) -> Dict[str, Any]:
        count = int(conn.execute("SELECT count(*) FROM event_log").fetchone()[0])
        if count:
            integrity = self.verify_log(project_id)
            if not integrity["valid"]:
                raise OctanceError("refusing to append to a damaged event log: " + integrity.get("reason", "integrity failure"), 1, "integrity_error")
        request_json = canonical(request)
        key = explicit_key or _digest({"agent_id": agent_id, "op": op, "request": request})
        old = conn.execute("SELECT op,payload_json FROM event_log WHERE idempotency_key=?", (key,)).fetchone()
        if old:
            envelope = json.loads(old["payload_json"])
            if old["op"] != op or envelope["request_json"] != request_json:
                raise OctanceError("idempotency key was already used with different content", 2, "idempotency_conflict")
            return envelope["record"]
        record = builder(conn, utc_now())
        envelope = {"request_json": request_json, "record": record}
        payload = canonical(envelope)
        previous_row = conn.execute("SELECT seq,hash FROM event_log ORDER BY seq DESC LIMIT 1").fetchone()
        seq = int(previous_row["seq"]) + 1 if previous_row else 1
        previous = previous_row["hash"] if previous_row else ZERO_HASH
        timestamp = record.get("created_at", utc_now())
        core = {"seq": seq, "project_id": project_id, "agent_id": agent_id, "op": op,
                "payload_json": payload, "idempotency_key": key, "prev_hash": previous, "timestamp": timestamp}
        event_hash = hashlib.sha256((previous + canonical(core)).encode("utf-8")).hexdigest()
        conn.execute("INSERT INTO event_log VALUES(?,?,?,?,?,?,?,?,?)",
                     (seq, project_id, agent_id, op, payload, key, previous, event_hash, timestamp))
        apply(conn, record)
        return record

    @staticmethod
    def agent(conn: sqlite3.Connection, agent_id: str, project_id: str) -> Dict[str, Any]:
        row = conn.execute("SELECT * FROM agents WHERE agent_id=? AND project_id=?", (agent_id, project_id)).fetchone()
        if not row:
            raise OctanceError("agent is not registered in this project", 3, "permission_denied")
        data = dict(row)
        data["permissions"] = json.loads(data.pop("permissions_json"))
        return data

    @staticmethod
    def require(conn: sqlite3.Connection, actor: Dict[str, Any], permission: str) -> None:
        if "*" not in actor["permissions"] and permission not in actor["permissions"]:
            raise OctanceError("agent lacks permission: " + permission, 3, "permission_denied")

    _require = require

    def get_agent(self, agent_id: str, project_id: str) -> Optional[Dict[str, Any]]:
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM agents WHERE agent_id=? AND project_id=?", (agent_id, project_id)).fetchone()
            if not row:
                return None
            data = dict(row)
            data["permissions"] = json.loads(data.pop("permissions_json"))
            return data
        finally:
            conn.close()

    def project_record(self) -> Optional[Dict[str, Any]]:
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM projects LIMIT 1").fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def verify_log(self, project_id: str) -> Dict[str, Any]:
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM event_log ORDER BY seq").fetchall()
            previous = ZERO_HASH
            for index, row in enumerate(rows, 1):
                if row["seq"] != index:
                    return {"valid": False, "first_bad_seq": index, "reason": "sequence gap or reorder"}
                if row["project_id"] != project_id:
                    return {"valid": False, "first_bad_seq": index, "reason": "project id mismatch"}
                if row["prev_hash"] != previous:
                    return {"valid": False, "first_bad_seq": index, "reason": "previous hash mismatch"}
                core = {"seq": int(row["seq"]), "project_id": row["project_id"], "agent_id": row["agent_id"],
                        "op": row["op"], "payload_json": row["payload_json"], "idempotency_key": row["idempotency_key"],
                        "prev_hash": row["prev_hash"], "timestamp": row["timestamp"]}
                actual = hashlib.sha256((previous + canonical(core)).encode("utf-8")).hexdigest()
                if actual != row["hash"]:
                    return {"valid": False, "first_bad_seq": index, "reason": "event hash mismatch"}
                previous = row["hash"]
            metadata_path = self.db_path.parent / "project.json"
            if metadata_path.exists():
                try:
                    anchor = json.loads(metadata_path.read_text(encoding="utf-8")).get("log_anchor", {})
                    anchor_seq, anchor_hash = int(anchor.get("seq", -1)), anchor.get("hash")
                except (OSError, ValueError, TypeError):
                    return {"valid": False, "first_bad_seq": None, "reason": "project log anchor unreadable"}
                if anchor_seq < 1 or not isinstance(anchor_hash, str):
                    return {"valid": False, "first_bad_seq": None, "reason": "project log anchor missing"}
                if anchor_seq > len(rows):
                    return {"valid": False, "first_bad_seq": anchor_seq, "reason": "event log truncated below anchor"}
                if rows[anchor_seq - 1]["hash"] != anchor_hash:
                    return {"valid": False, "first_bad_seq": anchor_seq, "reason": "project anchor hash mismatch"}
            return {"valid": True, "events": len(rows), "head": previous}
        finally:
            conn.close()

    @staticmethod
    def materialized(conn: sqlite3.Connection) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for table in ("projects", "agents", "evidence", "evidence_links", "claims", "claim_links", "claim_status_events"):
            rows = [dict(row) for row in conn.execute("SELECT * FROM " + table + (" ORDER BY id" if table == "claim_status_events" else ""))]
            if table == "claim_status_events":
                for row in rows: row.pop("id", None)
            for row in rows:
                for key in list(row):
                    if key.endswith("_json"):
                        row[key[:-5]] = json.loads(row.pop(key))
            result[table] = rows
        result["counters"] = {row["prefix"]: int(row["value"]) for row in conn.execute("SELECT * FROM counters")}
        return result

    def rebuild_check(self, project_id: str) -> Dict[str, Any]:
        integrity = self.verify_log(project_id)
        if not integrity["valid"]:
            return {"matches": False, "integrity": integrity, "mismatches": ["event log integrity"]}
        expected: Dict[str, Any] = {"projects": [], "agents": [], "evidence": [], "evidence_links": [], "claims": [], "claim_links": [], "claim_status_events": [], "counters": {}}
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM event_log ORDER BY seq").fetchall()
            for row in rows:
                record = json.loads(row["payload_json"])["record"]
                op = row["op"]
                if op == "project.init":
                    expected["projects"] = [{k: record[k] for k in ("project_id", "root", "created_at")}]
                elif op == "agent.register":
                    item = dict(record); item.pop("permissions_json", None); expected["agents"].append(item)
                elif op == "evidence.add":
                    item = dict(record); item["payload"] = json.loads(item.pop("payload_json")); related = item.pop("related", [])
                    expected["evidence"].append(item)
                    for related_id in related:
                        expected["evidence_links"].append({"evidence_id": record["evidence_id"], "related_id": related_id,
                                                           "relation": "related_to", "agent_id": record["agent_id"], "created_at": record["created_at"]})
                    expected["counters"]["EV"] = expected["counters"].get("EV", 0) + 1
                elif op == "evidence.link":
                    item = dict(record); item.pop("project_id", None); expected["evidence_links"].append(item)
                elif op == "claim.add":
                    item = dict(record)
                    for key in ("supporting_evidence_json", "contradicting_evidence_json", "depends_on_claims_json", "footprint_json"):
                        item[key[:-5]] = json.loads(item.pop(key))
                    expected["claims"].append(item)
                    expected["counters"]["CL"] = expected["counters"].get("CL", 0) + 1
                elif op == "claim.link":
                    item = dict(record); item.pop("project_id", None); expected["claim_links"].append(item)
                elif op == "claim.status":
                    expected["claim_status_events"].append({k: record[k] for k in ("claim_id", "previous_status", "status", "agent_id", "created_at")})
                else:
                    return {"matches": False, "mismatches": ["unknown reducer operation: " + op]}
            actual = self.materialized(conn)
            mismatch = [name for name in expected if canonical(expected[name]) != canonical(actual[name])]
            return {"matches": not mismatch, "mismatches": mismatch, "events": len(rows)}
        finally:
            conn.close()

    def _rows(self, query: str, params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
        conn = self.connect()
        try:
            rows = [dict(row) for row in conn.execute(query, tuple(params))]
            for row in rows:
                for key in list(row):
                    if key.endswith("_json"):
                        row[key[:-5]] = json.loads(row.pop(key))
            return rows
        finally:
            conn.close()



def init_project(root: Path, update_gitignore: bool = False, trust_tier: str = "standard") -> Dict[str, Any]:
    root = root.resolve()
    if not root.is_dir():
        raise OctanceError("project root is not a directory", 2, "invalid_input")
    state = root / ".octance"
    state.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(state / "octance.db")
    existing = ledger.project_record()
    if existing:
        project_id = existing["project_id"]
        if existing["root"] != str(root):
            raise OctanceError("stored project root does not match this directory", 1, "project_mismatch")
        metadata_path = state / "project.json"
        if metadata_path.exists():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise OctanceError("project.json is unreadable: " + str(error))
            if metadata.get("project_id") != project_id:
                raise OctanceError("project.json project_id does not match database", 1, "project_mismatch")
    else:
        if trust_tier not in {"low", "standard"}:
            raise OctanceError("default trust tier must be low or standard", 2, "invalid_input")
        project_id, created_at = str(uuid.uuid4()), utc_now()
        permissions = sorted(ROLE_PERMISSIONS["admin"])
        owner = {"agent_id": "AG-owner", "project_id": project_id, "name": "owner", "role": "admin",
                 "trust_tier": trust_tier, "permissions": permissions, "permissions_json": canonical(permissions), "created_at": created_at}
        conn = ledger.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            def apply_project(c: sqlite3.Connection, rec: Dict[str, Any]) -> None:
                c.execute("INSERT INTO projects(project_id,root,created_at) VALUES(?,?,?)", (rec["project_id"], rec["root"], rec["created_at"]))
            ledger.event(conn, project_id, "AG-owner", "project.init", {"project_id": project_id, "root": str(root)},
                         lambda _c, when: {"project_id": project_id, "root": str(root), "created_at": when}, apply_project)
            def apply_agent(c: sqlite3.Connection, rec: Dict[str, Any]) -> None:
                c.execute("INSERT INTO agents(agent_id,project_id,name,role,trust_tier,permissions_json,created_at) VALUES(?,?,?,?,?,?,?)",
                          tuple(rec[k] for k in ("agent_id", "project_id", "name", "role", "trust_tier", "permissions_json", "created_at")))
            ledger.event(conn, project_id, "AG-owner", "agent.register", {"name": "owner", "role": "admin"},
                         lambda _c, _when: owner, apply_agent)
            conn.commit()
        except Exception:
            conn.rollback(); raise
        finally:
            conn.close()
    metadata_path = state / "project.json"
    if not metadata_path.exists():
        conn = ledger.connect()
        try:
            head = conn.execute("SELECT seq,hash FROM event_log ORDER BY seq DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        _atomic_json(metadata_path, {"project_id": project_id, "root": str(root),
                                     "log_anchor": {"seq": int(head["seq"]), "hash": head["hash"]}})
    config_path = state / "config.json"
    if not config_path.exists():
        _atomic_json(config_path, {"schema": 1, "default_trust_tier": trust_tier, "execution_allowlist": []})
    warnings: List[str] = []
    gitignore = root / ".gitignore"
    current = gitignore.read_text(encoding="utf-8", errors="replace") if gitignore.exists() else ""
    if update_gitignore:
        if ".octance/" not in current:
            gitignore.write_text(current + ("\n" if current and not current.endswith("\n") else "") + ".octance/\n", encoding="utf-8")
    elif ".octance/" not in current:
        warnings.append("Consider adding .octance/ to .gitignore; use init --update-gitignore to approve that edit")
    return {"project_id": project_id, "root": str(root), "database": str(state / "octance.db"),
            "created": not bool(existing), "owner_agent": "AG-owner", "warnings": warnings}


class Project:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.state = self.root / ".octance"
        try:
            self.metadata = json.loads((self.state / "project.json").read_text(encoding="utf-8"))
            self.config = json.loads((self.state / "config.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise OctanceError("project metadata/config is missing or invalid: " + str(error))
        if self.metadata.get("root") != str(self.root):
            raise OctanceError("project moved or renamed: stored root does not match current path", 1, "project_mismatch")
        self.project_id = self.metadata.get("project_id")
        self.ledger = Ledger(self.state / "octance.db")
        stored = self.ledger.project_record()
        if not self.project_id or not stored or stored["project_id"] != self.project_id or stored["root"] != str(self.root):
            raise OctanceError("project_id/root mismatch between project.json and database", 1, "project_mismatch")

    def agent_id(self, requested: Optional[str] = None) -> str:
        name = requested or os.environ.get("OCTANCE_AGENT") or "owner"
        agent_id = name if name.startswith("AG-") else ("AG-owner" if name == "owner" else "AG-" + _slug(name))
        if self.ledger.get_agent(agent_id, self.project_id) is None:
            raise OctanceError("agent is not registered in this project: " + agent_id, 3, "permission_denied")
        return agent_id

    def revision(self) -> str:
        return revision_for(self.root)

    def refresh_anchor(self) -> None:
        # Hold SQLite's write reservation while reading the committed head and replacing
        # the external anchor, so concurrent writers cannot overwrite a newer anchor
        # with a stale snapshot.
        conn = self.ledger.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT seq,hash FROM event_log ORDER BY seq DESC LIMIT 1").fetchone()
            metadata = dict(self.metadata)
            metadata["log_anchor"] = {"seq": int(row["seq"]), "hash": row["hash"]}
            _atomic_json(self.state / "project.json", metadata)
            conn.commit()
            self.metadata = metadata
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _require(self, agent: Dict[str, Any], permission: str) -> None:
        conn = self.ledger.connect()
        try:
            Ledger.require(conn, agent, permission)
        finally:
            conn.close()

    def register_agent(self, name: str, role: str, trust_tier: Optional[str], actor_id: str,
                       explicit_key: Optional[str] = None) -> Dict[str, Any]:
        if role not in {"explorer", "investigator", "tester", "documenter"}:
            raise OctanceError("role must be explorer, investigator, tester, or documenter; admin is reserved", 2, "invalid_input")
        actor = self.ledger.get_agent(actor_id, self.project_id)
        if actor is None or actor["role"] != "admin":
            raise OctanceError("only an admin can register agents", 3, "permission_denied")
        name = _slug(name); tier = trust_tier or self.config.get("default_trust_tier", "standard")
        if tier not in {"low", "standard"}:
            raise OctanceError("trust tier must be low or standard", 2, "invalid_input")
        permissions = sorted(ROLE_PERMISSIONS[role])
        request = {"name": name, "role": role, "trust_tier": tier}
        conn = self.ledger.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            def build(_c: sqlite3.Connection, when: str) -> Dict[str, Any]:
                return {"agent_id": "AG-" + name, "project_id": self.project_id, "name": name, "role": role,
                        "trust_tier": tier, "permissions": permissions, "permissions_json": canonical(permissions), "created_at": when}
            def apply(c: sqlite3.Connection, r: Dict[str, Any]) -> None:
                c.execute("INSERT INTO agents(agent_id,project_id,name,role,trust_tier,permissions_json,created_at) VALUES(?,?,?,?,?,?,?)",
                          tuple(r[k] for k in ("agent_id", "project_id", "name", "role", "trust_tier", "permissions_json", "created_at")))
            result = self.ledger.event(conn, self.project_id, actor_id, "agent.register", request, build, apply, explicit_key)
            conn.commit(); self.refresh_anchor()
            result = dict(result); result.pop("permissions_json", None)
            return result
        except sqlite3.IntegrityError as error:
            conn.rollback(); raise OctanceError("agent name already exists: " + str(error), 2, "invalid_input")
        except Exception:
            conn.rollback(); raise
        finally:
            conn.close()

    def add_evidence(self, actor_id: str, evidence_type: str, description: str, payload: Dict[str, Any],
                     file: Optional[str] = None, symbol: Optional[str] = None, line_range: Optional[str] = None,
                     method: str = "agent_reported", related: Optional[List[str]] = None,
                     explicit_key: Optional[str] = None) -> Dict[str, Any]:
        if evidence_type not in EVIDENCE_TYPES or not isinstance(payload, dict):
            raise OctanceError("invalid evidence type or payload; payload must be a JSON object", 2, "invalid_input")
        actor = self.ledger.get_agent(actor_id, self.project_id)
        if actor is None: raise OctanceError("unknown agent", 3, "permission_denied")
        self._require(actor, "evidence.add")
        redactions = [0]
        description = _redact(description, redactions); payload = _redact(payload, redactions)
        file = _redact(file, redactions) if file else None
        symbol = _redact(symbol, redactions) if symbol else None
        method = _redact(method, redactions)
        if line_range is not None and (not isinstance(line_range, str) or not re.fullmatch(r"[1-9][0-9]*(?:-[1-9][0-9]*)?", line_range)):
            raise OctanceError("line range must be a positive line number or start-end range", 2, "invalid_input")
        if line_range and "-" in line_range and int(line_range.split("-", 1)[0]) > int(line_range.split("-", 1)[1]):
            raise OctanceError("line range start must not exceed its end", 2, "invalid_input")
        if file:
            relative = Path(file)
            if relative.is_absolute() or ".." in relative.parts or not _within((self.root / relative).resolve(), self.root):
                raise OctanceError("evidence file must be a project-relative path inside this root", 2, "invalid_input")
        related = sorted(set(related or []))
        if any(not isinstance(item, str) or not item for item in related):
            raise OctanceError("related evidence IDs must be non-empty strings", 2, "invalid_input")
        request = {"type": evidence_type, "description": description, "payload": payload, "file": file,
                   "symbol": symbol, "line_range": line_range, "method": method, "related": related, "revision": self.revision()}
        conn = self.ledger.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            def build(c: sqlite3.Connection, when: str) -> Dict[str, Any]:
                return {"evidence_id": Ledger.next_id(c, "EV"), "project_id": self.project_id, "agent_id": actor_id,
                        "type": evidence_type, "description": description, "payload_json": canonical(payload),
                        "content_hash": _digest({"type": evidence_type, "description": description, "payload": payload,
                                                  "file": file, "symbol": symbol, "line_range": line_range}),
                        "created_at": when, "revision": request["revision"], "file": file, "symbol": symbol,
                        "line_range": line_range, "method": method, "verification_status": "unverified",
                        "redactions": redactions[0], "related": related}
            def apply(c: sqlite3.Connection, r: Dict[str, Any]) -> None:
                c.execute("INSERT INTO evidence(evidence_id,project_id,agent_id,type,description,payload_json,content_hash,created_at,revision,file,symbol,line_range,method,verification_status,redactions) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          tuple(r[k] for k in ("evidence_id","project_id","agent_id","type","description","payload_json","content_hash","created_at","revision","file","symbol","line_range","method","verification_status","redactions")))
                if self.ledger.fts5:
                    c.execute("INSERT INTO evidence_fts VALUES(?,?,?,?,?,?,?)", (r["evidence_id"], self.project_id, evidence_type, description, r["payload_json"], file or "", symbol or ""))
                for target in related:
                    if target == r["evidence_id"]:
                        raise OctanceError("self-referential evidence links are not allowed", 2, "invalid_input")
                    found = c.execute("SELECT project_id FROM evidence WHERE evidence_id=?", (target,)).fetchone()
                    if not found or found[0] != self.project_id: raise OctanceError("related evidence must belong to this project", 2, "invalid_input")
                    c.execute("INSERT INTO evidence_links VALUES(?,?,?,?,?)", (r["evidence_id"], target, "related_to", actor_id, r["created_at"]))
            result = self.ledger.event(conn, self.project_id, actor_id, "evidence.add", request, build, apply, explicit_key)
            conn.commit(); self.refresh_anchor()
            result = dict(result); result["payload"] = json.loads(result.pop("payload_json"))
            return result
        except sqlite3.IntegrityError as error:
            conn.rollback(); raise OctanceError("evidence write rejected: " + str(error), 2, "invalid_input")
        except Exception:
            conn.rollback(); raise
        finally:
            conn.close()

    def add_claim(self, actor_id: str, kind: str, text: str, supporting: Optional[List[str]] = None,
                  contradicting: Optional[List[str]] = None, depends: Optional[List[str]] = None,
                  explicit_key: Optional[str] = None) -> Dict[str, Any]:
        if kind not in CLAIM_KINDS: raise OctanceError("invalid claim kind", 2, "invalid_input")
        actor = self.ledger.get_agent(actor_id, self.project_id)
        if actor is None: raise OctanceError("unknown agent", 3, "permission_denied")
        self._require(actor, "claim.add")
        redactions = [0]; text = _redact(text, redactions)
        support, contradiction, dependencies = sorted(set(supporting or [])), sorted(set(contradicting or [])), sorted(set(depends or []))
        conn = self.ledger.connect()
        try:
            for eid in support + contradiction:
                row = conn.execute("SELECT project_id FROM evidence WHERE evidence_id=?", (eid,)).fetchone()
                if not row or row[0] != self.project_id: raise OctanceError("claim evidence must belong to this project: " + eid, 2, "invalid_input")
            for cid in dependencies:
                row = conn.execute("SELECT project_id FROM claims WHERE claim_id=?", (cid,)).fetchone()
                if not row or row[0] != self.project_id: raise OctanceError("dependent claim must belong to this project: " + cid, 2, "invalid_input")
            evidence_types = {row[0] for eid in support for row in conn.execute("SELECT type FROM evidence WHERE evidence_id=?", (eid,))}
            basis = "static_analysis" if "static_analysis" in evidence_types else ("agent_inference" if support else "unsupported")
            status = "quarantined" if actor["trust_tier"] == "low" else "asserted"
            conn.execute("BEGIN IMMEDIATE")
            revision = self.revision()
            request = {"kind": kind, "text": text, "supporting": support, "contradicting": contradiction,
                       "depends": dependencies, "revision": revision}
            def build(c: sqlite3.Connection, when: str) -> Dict[str, Any]:
                return {"claim_id": Ledger.next_id(c, "CL"), "project_id": self.project_id, "kind": kind, "text": text,
                        "agent_id": actor_id, "revision": revision, "supporting_evidence_json": canonical(support),
                        "contradicting_evidence_json": canonical(contradiction), "depends_on_claims_json": canonical(dependencies),
                        "footprint_json": "{}", "evidence_basis": basis, "status": status, "version": 1,
                        "created_at": when, "redactions": redactions[0]}
            def apply(c: sqlite3.Connection, r: Dict[str, Any]) -> None:
                columns = ("claim_id","project_id","kind","text","agent_id","revision","supporting_evidence_json","contradicting_evidence_json","depends_on_claims_json","footprint_json","evidence_basis","status","version","created_at","redactions")
                c.execute("INSERT INTO claims(" + ",".join(columns) + ") VALUES(" + ",".join("?" for _ in columns) + ")", tuple(r[k] for k in columns))
                if self.ledger.fts5: c.execute("INSERT INTO claims_fts VALUES(?,?,?,?,?)", (r["claim_id"], self.project_id, kind, status, text))
            result = self.ledger.event(conn, self.project_id, actor_id, "claim.add", request, build, apply, explicit_key)
            conn.commit(); self.refresh_anchor()
            return self._claim_record(result)
        except sqlite3.IntegrityError as error:
            conn.rollback(); raise OctanceError("claim write rejected: " + str(error), 2, "invalid_input")
        except Exception:
            conn.rollback(); raise
        finally:
            conn.close()

    @staticmethod
    def _claim_record(record: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(record)
        for key in ("supporting_evidence_json", "contradicting_evidence_json", "depends_on_claims_json", "footprint_json"):
            if key in result: result[key[:-5]] = json.loads(result.pop(key))
        return result

    def transition_claim(self, actor_id: str, claim_id: str, status: str, explicit_key: Optional[str] = None) -> Dict[str, Any]:
        if status not in CLAIM_STATUSES:
            raise OctanceError("invalid claim status", 2, "invalid_input")
        conn = self.ledger.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            actor = Ledger.agent(conn, actor_id, self.project_id)
            Ledger.require(conn, actor, "claim.transition")
            row = conn.execute("SELECT * FROM claims WHERE claim_id=? AND project_id=?", (claim_id, self.project_id)).fetchone()
            if not row:
                raise OctanceError("unknown claim", 2, "not_found")
            previous = conn.execute("SELECT id,previous_status,status,agent_id,created_at FROM claim_status_events WHERE claim_id=? ORDER BY id DESC LIMIT 1", (claim_id,)).fetchone()
            previous_status = previous["status"] if previous else row["status"]
            if previous_status == status and previous:
                conn.commit()
                return {"claim_id": claim_id, "previous_status": previous["previous_status"], "status": status,
                        "agent_id": previous["agent_id"], "created_at": previous["created_at"]}
            if actor["trust_tier"] == "low" and status != previous_status:
                raise OctanceError("low-trust agents cannot transition claim status", 2, "permission_denied")
            request = {"claim_id": claim_id, "status": status, "previous_status": previous_status,
                       "previous_event_id": previous["id"] if previous else None}
            def build(_conn: sqlite3.Connection, when: str) -> Dict[str, Any]:
                return {"claim_id": claim_id, "previous_status": previous_status, "status": status,
                        "agent_id": actor_id, "created_at": when}
            def apply(c: sqlite3.Connection, record: Dict[str, Any]) -> None:
                c.execute("INSERT INTO claim_status_events(claim_id,previous_status,status,agent_id,created_at) VALUES(?,?,?,?,?)",
                          tuple(record[k] for k in ("claim_id", "previous_status", "status", "agent_id", "created_at")))
            result = self.ledger.event(conn, self.project_id, actor_id, "claim.status", request, build, apply, explicit_key)
            conn.commit()
            self.refresh_anchor()
            return result
        except sqlite3.IntegrityError as error:
            conn.rollback(); raise OctanceError("claim transition rejected: " + str(error), 2, "invalid_input")
        except Exception:
            conn.rollback(); raise
        finally:
            conn.close()

    def link_evidence(self, actor_id: str, evidence_id: str, related_id: str, relation: str, explicit_key=None) -> Dict[str, Any]:
        if relation not in {"supports", "contradicts", "context", "supersedes", "verifies", "related_to"}: raise OctanceError("invalid evidence relation", 2, "invalid_input")
        return self._append_link(actor_id, "evidence.link", {"evidence_id": evidence_id, "related_id": related_id, "relation": relation}, explicit_key)

    def link_claim(self, actor_id: str, claim_id: str, related_id: str, relation: str, explicit_key=None) -> Dict[str, Any]:
        if relation not in {"supports", "contradicts", "depends_on", "conflicts_with"}: raise OctanceError("invalid claim relation", 2, "invalid_input")
        return self._append_link(actor_id, "claim.link", {"claim_id": claim_id, "related_id": related_id, "relation": relation}, explicit_key)

    def _append_link(self, actor_id: str, op: str, request: Dict[str, Any], explicit_key: Optional[str]) -> Dict[str, Any]:
        if request["related_id"] == request.get("evidence_id", request.get("claim_id")):
            raise OctanceError("self-referential links are not allowed", 2, "invalid_input")
        permission = "evidence.link" if op == "evidence.link" else "claim.link"
        table, id_key = ("evidence", "evidence_id") if op == "evidence.link" else ("claims", "claim_id")
        right_key = "related_id" if op == "evidence.link" else "related_claim_id"
        conn = self.ledger.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            actor = Ledger.agent(conn, actor_id, self.project_id); Ledger.require(conn, actor, permission)
            for identifier in (request["evidence_id"], request["related_id"]) if op == "evidence.link" else (request["claim_id"], request["related_id"]):
                row = conn.execute("SELECT project_id FROM " + table + " WHERE " + id_key + "=?", (identifier,)).fetchone()
                if not row or row[0] != self.project_id: raise OctanceError("both linked records must belong to this project", 2, "invalid_input")
            def build(_c: sqlite3.Connection, when: str) -> Dict[str, Any]:
                return {"project_id": self.project_id, id_key: request.get(id_key), right_key: request["related_id"],
                        "relation": request["relation"], "agent_id": actor_id, "created_at": when}
            def apply(c: sqlite3.Connection, r: Dict[str, Any]) -> None:
                if op == "evidence.link": c.execute("INSERT INTO evidence_links VALUES(?,?,?,?,?)", (r[id_key], r[right_key], r["relation"], actor_id, r["created_at"]))
                else: c.execute("INSERT INTO claim_links VALUES(?,?,?,?,?)", (r[id_key], r[right_key], r["relation"], actor_id, r["created_at"]))
            result = self.ledger.event(conn, self.project_id, actor_id, op, request, build, apply, explicit_key)
            conn.commit(); self.refresh_anchor(); return result
        except sqlite3.IntegrityError as error:
            conn.rollback(); raise OctanceError("link write rejected: " + str(error), 2, "invalid_input")
        except Exception:
            conn.rollback(); raise
        finally:
            conn.close()

    def _rows(self, query: str, params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
        return self.ledger._rows(query, params)

    def get_evidence(self, evidence_id: str) -> Optional[Dict[str, Any]]:
        rows = self._rows("SELECT * FROM evidence WHERE evidence_id=? AND project_id=?", (evidence_id, self.project_id))
        if rows: rows[0]["links"] = self._rows("SELECT * FROM evidence_links WHERE evidence_id=? ORDER BY created_at", (evidence_id,))
        return rows[0] if rows else None

    def _fts_expression(self, query: str) -> str:
        return " AND ".join('"' + token.replace('"', '""') + '"' for token in re.findall(r"[A-Za-z0-9]+", query))

    def search_evidence(self, query: str = "", evidence_type: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        if not 1 <= limit <= 1000: raise OctanceError("limit must be 1..1000", 2, "invalid_input")
        if len(query) > 4096: raise OctanceError("search query exceeds 4096 characters", 2, "invalid_input")
        if len(re.findall(r"[A-Za-z0-9]+", query)) > 64: raise OctanceError("search query exceeds 64 terms", 2, "invalid_input")
        if query and not re.findall(r"[A-Za-z0-9]+", query): return []
        clauses, params = ["project_id=?"], [self.project_id]
        if evidence_type: clauses.append("type=?"); params.append(evidence_type)
        expr = self._fts_expression(query) if query and self.ledger.fts5 else ""
        fallback_tokens = []
        if expr:
            clauses.append("evidence_id IN (SELECT evidence_id FROM evidence_fts WHERE evidence_fts MATCH ? AND project_id=?)"); params.extend([expr, self.project_id])
        elif query:
            fallback_tokens = [token.casefold() for token in re.findall(r"[A-Za-z0-9]+", query)]
            for token in fallback_tokens:
                clauses.append("(description LIKE ? OR payload_json LIKE ? OR file LIKE ? OR symbol LIKE ?)")
                params.extend(["%" + token + "%"] * 4)
        rows = self._rows("SELECT * FROM evidence WHERE " + " AND ".join(clauses) + " ORDER BY rowid DESC" + (" LIMIT ?" if not fallback_tokens else ""), params + ([limit] if not fallback_tokens else []))
        if fallback_tokens:
            rows = [row for row in rows if set(fallback_tokens) <= set(re.findall(r"[A-Za-z0-9]+", " ".join(str(row.get(k) or "") for k in ("description", "payload_json", "file", "symbol")).casefold()))]
        return rows[:limit]

    def _attach_claim_details(self, claims: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not claims:
            return claims
        ids = [item["claim_id"] for item in claims]
        status_by_claim: Dict[str, str] = {}
        links_by_claim: Dict[str, List[Dict[str, Any]]] = {claim_id: [] for claim_id in ids}
        conn = self.ledger.connect()
        try:
            for offset in range(0, len(ids), 400):
                batch = ids[offset:offset + 400]
                placeholders = ",".join("?" for _ in batch)
                for row in conn.execute("SELECT claim_id,status FROM claim_status_events WHERE claim_id IN (" + placeholders + ") ORDER BY id DESC", batch):
                    status_by_claim.setdefault(row["claim_id"], row["status"])
                for row in conn.execute("SELECT claim_id,related_claim_id,relation,agent_id,created_at FROM claim_links WHERE claim_id IN (" + placeholders + ") OR related_claim_id IN (" + placeholders + ")", batch + batch):
                    record = dict(row)
                    if row["claim_id"] in links_by_claim:
                        links_by_claim[row["claim_id"]].append(dict(record, direction="outgoing"))
                    if row["related_claim_id"] in links_by_claim:
                        links_by_claim[row["related_claim_id"]].append(dict(record, direction="incoming"))
        finally:
            conn.close()
        for item in claims:
            item["status"] = status_by_claim.get(item["claim_id"], item["status"])
            item["links"] = sorted(links_by_claim[item["claim_id"]], key=lambda link: (link["created_at"], link["claim_id"], link["related_claim_id"]))
        return claims

    def get_claim(self, claim_id: str) -> Optional[Dict[str, Any]]:
        rows = self._rows("SELECT * FROM claims WHERE claim_id=? AND project_id=?", (claim_id, self.project_id))
        if not rows: return None
        return self._attach_claim_details([self._claim_record(rows[0])])[0]

    def search_claims(self, query: str = "", status: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        if not 1 <= limit <= 1000: raise OctanceError("limit must be 1..1000", 2, "invalid_input")
        if len(query) > 4096: raise OctanceError("search query exceeds 4096 characters", 2, "invalid_input")
        if len(re.findall(r"[A-Za-z0-9]+", query)) > 64: raise OctanceError("search query exceeds 64 terms", 2, "invalid_input")
        if query and not re.findall(r"[A-Za-z0-9]+", query): return []
        clauses, params = ["project_id=?"], [self.project_id]
        if status:
            if status not in CLAIM_STATUSES: raise OctanceError("invalid claim status", 2, "invalid_input")
            clauses.append("COALESCE((SELECT status FROM claim_status_events e WHERE e.claim_id=claims.claim_id ORDER BY e.id DESC LIMIT 1),claims.status)=?")
            params.append(status)
        expr = self._fts_expression(query) if query and self.ledger.fts5 else ""
        fallback_tokens = []
        if expr:
            clauses.append("claim_id IN (SELECT claim_id FROM claims_fts WHERE claims_fts MATCH ? AND project_id=?)"); params.extend([expr, self.project_id])
        elif query:
            fallback_tokens = [token.casefold() for token in re.findall(r"[A-Za-z0-9]+", query)]
            for token in fallback_tokens:
                clauses.append("text LIKE ?"); params.append("%" + token + "%")
        rows = self._rows("SELECT * FROM claims WHERE " + " AND ".join(clauses) + " ORDER BY rowid DESC" + (" LIMIT ?" if not fallback_tokens else ""), params + ([limit] if not fallback_tokens else []))
        if fallback_tokens:
            rows = [row for row in rows if set(fallback_tokens) <= set(re.findall(r"[A-Za-z0-9]+", row["text"].casefold()))]
        claims = [self._claim_record(row) for row in rows[:limit]]
        return self._attach_claim_details(claims)

    def brief(self, task: str, files: List[str], symbols: List[str], budget_tokens: int) -> Dict[str, Any]:
        if not 1 <= budget_tokens <= 100000: raise OctanceError("budget-tokens must be 1..100000", 2, "invalid_input")
        task_terms = set(re.findall(r"[A-Za-z0-9]+", task.casefold()))
        requested_files = {item.replace("\\", "/").strip("./").casefold() for item in files if item.strip()}
        requested_symbols = {item.strip().casefold() for item in symbols if item.strip()}
        claims = [c for c in self.search_claims(limit=1000) if c["status"] != "quarantined"]
        evidence_ids = sorted({evidence_id for claim in claims
                               for evidence_id in claim["supporting_evidence"] + claim["contradicting_evidence"]})
        evidence_locations: Dict[str, Dict[str, Optional[str]]] = {}
        if evidence_ids:
            conn = self.ledger.connect()
            try:
                for offset in range(0, len(evidence_ids), 400):
                    batch = evidence_ids[offset:offset + 400]
                    placeholders = ",".join("?" for _ in batch)
                    for row in conn.execute("SELECT evidence_id,file,symbol FROM evidence WHERE project_id=? AND evidence_id IN (" + placeholders + ")", [self.project_id] + batch):
                        evidence_locations[row["evidence_id"]] = {"file": row["file"], "symbol": row["symbol"]}
            finally:
                conn.close()
        ranked = []
        for claim in claims:
            text_tokens = set(re.findall(r"[A-Za-z0-9]+", (claim["text"] + " " + claim["kind"]).casefold()))
            evidence_files, evidence_symbols = set(), set()
            ids = set(claim["supporting_evidence"] + claim["contradicting_evidence"])
            for evidence_id in ids:
                location = evidence_locations.get(evidence_id, {})
                if location.get("file"): evidence_files.add(location["file"].replace("\\", "/").strip("./").casefold())
                if location.get("symbol"): evidence_symbols.add(location["symbol"].casefold())
            task_score = len(task_terms & text_tokens)
            file_hit = any(stored == requested or stored.endswith("/" + requested) or requested.endswith("/" + stored)
                           for stored in evidence_files for requested in requested_files)
            symbol_hit = any(stored == requested or stored.rsplit(".", 1)[-1] == requested
                             for stored in evidence_symbols for requested in requested_symbols)
            score = task_score + 3 * int(file_hit or symbol_hit)
            if score or not (task_terms or requested_files or requested_symbols): ranked.append((score, claim))
        ranked.sort(key=lambda pair: (-pair[0], pair[1]["claim_id"]))
        used, items = 0, []
        max_chars = budget_tokens * 4
        for _, claim in ranked:
            item = {k: claim[k] for k in ("claim_id", "kind", "status", "text", "evidence_basis", "revision", "agent_id", "supporting_evidence", "contradicting_evidence", "depends_on_claims", "links")}
            size = len(canonical(item))
            if used + size > max_chars: break
            items.append(item); used += size
        unknowns = [{"claim_id": item["claim_id"], "text": item["text"], "status": item["status"]} for item in items if item["kind"] == "open_question"]
        return {"task": task, "claims": items, "unknowns": unknowns, "open_investigations": [],
                "estimated_tokens": (used + 3) // 4, "budget_tokens": budget_tokens,
                "token_estimate": "characters divided by four"}

    def status(self) -> Dict[str, Any]:
        conn = self.ledger.connect()
        try:
            counts = conn.execute(
                "SELECT COALESCE(SUM(CASE WHEN effective_status IN ('stale','needs_review') THEN 1 ELSE 0 END),0), "
                "COALESCE(SUM(CASE WHEN effective_status='asserted' THEN 1 ELSE 0 END),0) FROM ("
                "SELECT COALESCE((SELECT cse.status FROM claim_status_events cse "
                "WHERE cse.claim_id=claims.claim_id "
                "ORDER BY cse.rowid DESC LIMIT 1), claims.status) AS effective_status "
                "FROM claims WHERE project_id=?)", (self.project_id,)).fetchone()
            stale, asserted = counts[0], counts[1]
            evidence = conn.execute("SELECT count(*) FROM evidence WHERE project_id=? AND verification_status='unverified'", (self.project_id,)).fetchone()[0]
        finally:
            conn.close()
        return {"project_id": self.project_id, "root": str(self.root), "open_investigations": 0,
                "stale_or_needs_review_claims": stale, "asserted_claims": asserted,
                "unverified_evidence": evidence, "log_integrity": self.ledger.verify_log(self.project_id)}


def diagnose_project(root: Path) -> Dict[str, Any]:
    root = root.resolve(); state = root / ".octance"; warnings: List[str] = []
    metadata: Dict[str, Any] = {}; project_id = None; config_valid = False
    try:
        metadata = json.loads((state / "project.json").read_text(encoding="utf-8")); project_id = metadata.get("project_id")
        if metadata.get("root") != str(root): warnings.append("stored project root does not match current directory")
    except (OSError, ValueError, AttributeError) as error: warnings.append("project metadata is invalid: " + str(error))
    try:
        config = json.loads((state / "config.json").read_text(encoding="utf-8"))
        config_valid = isinstance(config, dict) and config.get("schema") == 1 and config.get("default_trust_tier") in {"low", "standard"} and isinstance(config.get("execution_allowlist"), list)
    except (OSError, ValueError) as error: warnings.append("project config is invalid: " + str(error))
    conn = None; schema = None; journal = None; fts = False; db_error = None
    database_path = state / "octance.db"
    if not database_path.is_file():
        db_error = "database file is missing"
    try:
        if database_path.is_file():
            conn = sqlite3.connect(str(database_path), timeout=2, isolation_level=None)
        if conn is None:
            raise sqlite3.DatabaseError(db_error or "database file is missing")
        conn.execute("PRAGMA busy_timeout=2000"); journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "schema_version" in tables:
            row = conn.execute("SELECT version FROM schema_version").fetchone(); schema = int(row[0]) if row else None
        try:
            conn.execute("CREATE VIRTUAL TABLE temp.octance_doctor_fts USING fts5(text)"); fts = True
            conn.execute("DROP TABLE temp.octance_doctor_fts")
        except sqlite3.DatabaseError: fts = False
    except sqlite3.DatabaseError as error:
        db_error = str(error); warnings.append("database could not be inspected: " + db_error)
    finally:
        if conn: conn.close()
    if not config_valid: warnings.append("config.json is missing or invalid")
    if schema is None: warnings.append("schema version is missing")
    elif schema > 1: warnings.append("database schema is newer than this Octance CLI")
    elif schema < 1: warnings.append("database schema needs migration")
    if journal and journal.lower() != "wal": warnings.append("SQLite journal mode is %s, not WAL" % journal)
    fs = _filesystem_type(root)
    if fs is None: warnings.append("filesystem type could not be identified; WAL is unsupported on some network filesystems")
    elif fs.lower() in {"nfs", "nfs4", "cifs", "smbfs", "sshfs", "9p", "afs"}: warnings.append("network filesystem %s detected; SQLite WAL is unsupported here" % fs)
    integrity: Dict[str, Any] = {"valid": False, "reason": "schema unsupported or database unavailable"}
    if schema == 1 and project_id and db_error is None:
        ledger = object.__new__(Ledger); ledger.db_path = state / "octance.db"
        integrity = ledger.verify_log(project_id)
        if not integrity["valid"]: warnings.append("event log integrity check failed: " + integrity.get("reason", "unknown"))
    return {"python": sys.version.split()[0], "python_ok": sys.version_info >= (3, 9), "sqlite": sqlite3.sqlite_version,
            "fts5": fts, "git_available": shutil.which("git") is not None, "filesystem_type": fs,
            "journal_mode": journal, "schema_version": schema, "project_id": project_id,
            "config_valid": config_valid, "log_integrity": integrity, "database_error": db_error, "warnings": warnings}
