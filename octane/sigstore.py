"""Verify externally keyless-signed statement bundles through operator-installed Cosign."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import re
import tempfile

from .db_support import _scrub_sensitive_text
from .host_environment import language_server_environment
from .process import run_bounded
from .provenance import rebuild_statement


def _read(path):
    with Path(path).open('rb') as stream:
        value=stream.read(4_000_001)
    if len(value)>4_000_000:
        raise ValueError('Sigstore input exceeds the 4 MB limit')
    return value


MIN_COSIGN_VERSION = (3, 0, 5)


def _require_cosign_version(executable):
    result = run_bounded([executable, 'version', '--json'], env=language_server_environment(), timeout=10, limit=16_384)
    try:
        info = json.loads(result.stdout)
        version = info.get('gitVersion', '')
    except (ValueError, AttributeError) as exc:
        raise RuntimeError('Cosign did not report a valid version; require 3.0.5 or newer') from exc
    match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)(?:\+[A-Za-z0-9.-]+)?', version)
    if result.returncode or not match or tuple(map(int, match.groups())) < MIN_COSIGN_VERSION:
        raise RuntimeError('Cosign 3.0.5 or newer is required; prerelease and unknown builds are refused')
    return version


def verify_sigstore(statement_path, bundle_path, identity, issuer, *, intelligence=None, rebuild=False, trusted_root=None):
    if any(not isinstance(value,str) or not value.strip() or len(value)>2048 or '\0' in value for value in (identity,issuer)):
        raise ValueError('Sigstore verification requires an exact certificate identity and OIDC issuer')
    executable=shutil.which('cosign')
    if executable is None:
        raise RuntimeError('Install Cosign to verify a Sigstore bundle')
    version = _require_cosign_version(executable)
    raw, bundle = _read(statement_path), _read(bundle_path)
    value=json.loads(raw)
    if not isinstance(value,dict) or value.get('_type') != 'https://in-toto.io/Statement/v1':
        raise ValueError('Sigstore payload must be an in-toto statement')
    # Verify the same frozen bytes that are subsequently compared with the ledger.
    with tempfile.TemporaryDirectory(prefix='octane-sigstore-') as directory:
        frozen=Path(directory)/'statement.json'; proof=Path(directory)/'bundle.json'
        frozen.write_bytes(raw); proof.write_bytes(bundle)
        options = []
        if trusted_root is not None:
            trusted = Path(directory)/'trusted-root.json'
            trusted.write_bytes(_read(trusted_root))
            options += ['--trusted-root', str(trusted)]
        result=run_bounded([executable,'verify-blob', *options,'--bundle',str(proof),'--certificate-identity',identity,
                            '--certificate-oidc-issuer',issuer,str(frozen)],
                           env=language_server_environment(),timeout=60)
        if result.returncode:
            raise ValueError('Sigstore verification failed: '+_scrub_sensitive_text(result.stderr.decode('utf-8','replace')[:2000]))
    return {'signature_verified':True,'backend':'cosign','cosign_version':version,'certificate_identity':identity,'oidc_issuer':issuer,
            'ledger_verified':rebuild_statement(value,intelligence) if rebuild else None,'statement':value}
