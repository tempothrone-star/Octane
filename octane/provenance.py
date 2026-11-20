"""In-toto statements and optional offline Ed25519 DSSE signing."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

PAYLOAD_TYPE = "application/vnd.in-toto+json"


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _pae(payload):
    kind = PAYLOAD_TYPE.encode()
    return b"DSSEv1 " + str(len(kind)).encode() + b" " + kind + b" " + str(len(payload)).encode() + b" " + payload


def statement(intelligence, project_id, proposal_evidence_id):
    evidence = intelligence.retrieve_evidence(proposal_evidence_id)
    if evidence is None or evidence["project_id"] != project_id or evidence["evidence_type"] != "patch_proposal":
        raise ValueError("provenance requires a patch proposal in the selected project")
    proposal = evidence["payload"]
    run_ids = [proposal["baseline_run_id"], proposal["candidate_run_id"], *proposal["regression_run_ids"]]
    for pair in proposal.get("preservation_pairs", []):
        run_ids.append(pair["baseline_run_id"])
    runs = []
    with intelligence._session() as db:
        for identifier in sorted(set(run_ids)):
            row = db.execute("SELECT id,command_json,image_id,before_hash,after_hash,status,evidence_id FROM execution_runs WHERE id=? AND project_id=?",
                             (identifier, project_id)).fetchone()
            if row is None:
                raise ValueError("proposal execution provenance is incomplete")
            runs.append({"id": row["id"], "status": row["status"], "image_id": row["image_id"],
                         "command_sha256": hashlib.sha256(row["command_json"].encode()).hexdigest(),
                         "before_hash": row["before_hash"], "after_hash": row["after_hash"],
                         "evidence_id": row["evidence_id"]})
    return {"_type": "https://in-toto.io/Statement/v1",
            "subject": [{"name": "patch.diff", "digest": {"sha256": hashlib.sha256(proposal["patch"].encode()).hexdigest()}}],
            "predicateType": "https://github.com/tempothrone-star/Octane/provenance/v1",
            "predicate": {"project_id": project_id, "revision": evidence["revision"],
                          "proposal_id": proposal["proposal_id"], "proposal_evidence_id": proposal_evidence_id,
                          "snapshot_hash": proposal["snapshot_hash"], "candidate_hash": proposal["candidate_hash"],
                          "image_id": proposal["image_id"], "execution_runs": runs,
                          "evidence_sha256": hashlib.sha256(_canonical(evidence["payload"])).hexdigest()}}


def sign_statement(value, private_key_path: Path, password=None):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = serialization.load_pem_private_key(private_key_path.read_bytes(), password=password)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("provenance signing requires an Ed25519 key")
    payload = _canonical(value)
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return {"payloadType": PAYLOAD_TYPE, "payload": base64.b64encode(payload).decode(),
            "signatures": [{"keyid": hashlib.sha256(public).hexdigest(),
                            "sig": base64.b64encode(key.sign(_pae(payload))).decode()}]}


def verify_envelope(envelope, public_key_path: Path, expected_patch_hash=None):
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if envelope.get("payloadType") != PAYLOAD_TYPE:
        raise ValueError("unsupported DSSE payload type")
    key = serialization.load_pem_public_key(public_key_path.read_bytes())
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("provenance verification requires an Ed25519 public key")
    payload = base64.b64decode(envelope["payload"], validate=True)
    public = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    key_id = hashlib.sha256(public).hexdigest()
    verified = False
    for signature in envelope.get("signatures", []):
        if signature.get("keyid") != key_id:
            continue
        try:
            key.verify(base64.b64decode(signature["sig"], validate=True), _pae(payload))
        except InvalidSignature:
            continue
        verified = True
        break
    if not verified:
        raise ValueError("no valid signature from the selected trusted public key")
    value = json.loads(payload)
    if value.get("_type") != "https://in-toto.io/Statement/v1":
        raise ValueError("signed payload is not an in-toto statement")
    if expected_patch_hash is not None and not any(
        subject.get("digest", {}).get("sha256") == expected_patch_hash for subject in value.get("subject", [])
    ):
        raise ValueError("signed statement does not identify the expected patch")
    return {"signature_verified": True, "key_id": key_id, "statement": value}
