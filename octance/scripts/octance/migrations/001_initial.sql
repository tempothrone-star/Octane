CREATE TABLE schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE projects (
    project_id TEXT PRIMARY KEY,
    root TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TRIGGER projects_no_update BEFORE UPDATE ON projects
BEGIN SELECT RAISE(ABORT, 'project identity is immutable'); END;
CREATE TRIGGER projects_no_delete BEFORE DELETE ON projects
BEGIN SELECT RAISE(ABORT, 'project identity is immutable'); END;

CREATE TABLE event_log (
    seq INTEGER PRIMARY KEY,
    project_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    op TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL,
    timestamp TEXT NOT NULL
);
CREATE INDEX event_log_project_seq ON event_log(project_id, seq);
CREATE TRIGGER event_log_no_update BEFORE UPDATE ON event_log
BEGIN SELECT RAISE(ABORT, 'event log is append-only'); END;
CREATE TRIGGER event_log_no_delete BEFORE DELETE ON event_log
BEGIN SELECT RAISE(ABORT, 'event log is append-only'); END;

CREATE TABLE agents (
    agent_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    trust_tier TEXT NOT NULL CHECK (trust_tier IN ('low','standard')),
    permissions_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, name)
);
CREATE TRIGGER agents_no_update BEFORE UPDATE ON agents
BEGIN SELECT RAISE(ABORT, 'agent records are append-only'); END;
CREATE TRIGGER agents_no_delete BEFORE DELETE ON agents
BEGIN SELECT RAISE(ABORT, 'agent records are append-only'); END;

CREATE TABLE evidence (
    evidence_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    agent_id TEXT NOT NULL REFERENCES agents(agent_id),
    type TEXT NOT NULL,
    description TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revision TEXT NOT NULL,
    file TEXT,
    symbol TEXT,
    line_range TEXT,
    method TEXT NOT NULL,
    verification_status TEXT NOT NULL CHECK (verification_status='unverified'),
    redactions INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX evidence_project_type ON evidence(project_id, type);
CREATE INDEX evidence_project_revision ON evidence(project_id, revision);
CREATE TRIGGER evidence_no_update BEFORE UPDATE ON evidence
BEGIN SELECT RAISE(ABORT, 'evidence is immutable'); END;
CREATE TRIGGER evidence_no_delete BEFORE DELETE ON evidence
BEGIN SELECT RAISE(ABORT, 'evidence is immutable'); END;

CREATE TABLE evidence_links (
    evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
    related_id TEXT NOT NULL REFERENCES evidence(evidence_id),
    relation TEXT NOT NULL,
    agent_id TEXT NOT NULL REFERENCES agents(agent_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(evidence_id, related_id, relation)
);
CREATE TRIGGER evidence_links_no_update BEFORE UPDATE ON evidence_links
BEGIN SELECT RAISE(ABORT, 'evidence links are append-only'); END;
CREATE TRIGGER evidence_links_no_delete BEFORE DELETE ON evidence_links
BEGIN SELECT RAISE(ABORT, 'evidence links are append-only'); END;

CREATE TABLE claims (
    claim_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    kind TEXT NOT NULL CHECK (kind IN ('observation','inference','hypothesis','intent','decision','requirement','limitation','open_question')),
    text TEXT NOT NULL,
    agent_id TEXT NOT NULL REFERENCES agents(agent_id),
    revision TEXT NOT NULL,
    supporting_evidence_json TEXT NOT NULL,
    contradicting_evidence_json TEXT NOT NULL,
    depends_on_claims_json TEXT NOT NULL,
    footprint_json TEXT NOT NULL,
    evidence_basis TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('asserted','corroborated','contradicted','stale','needs_review','retracted','quarantined')),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    redactions INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX claims_project_status ON claims(project_id, status);
CREATE INDEX claims_project_kind ON claims(project_id, kind);
CREATE TRIGGER claims_no_update BEFORE UPDATE ON claims
BEGIN SELECT RAISE(ABORT, 'claims are append-only in this schema'); END;
CREATE TRIGGER claims_no_delete BEFORE DELETE ON claims
BEGIN SELECT RAISE(ABORT, 'claims are append-only in this schema'); END;

CREATE TABLE counters (
    prefix TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);

CREATE TABLE claim_links (
    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    related_claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    relation TEXT NOT NULL,
    agent_id TEXT NOT NULL REFERENCES agents(agent_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(claim_id, related_claim_id, relation)
);
CREATE TRIGGER claim_links_no_update BEFORE UPDATE ON claim_links
BEGIN SELECT RAISE(ABORT, 'claim links are append-only'); END;
CREATE TRIGGER claim_links_no_delete BEFORE DELETE ON claim_links
BEGIN SELECT RAISE(ABORT, 'claim links are append-only'); END;
