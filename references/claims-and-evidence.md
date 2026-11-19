# Portable Octane claims and evidence

Octane stores observations as immutable evidence and interpretations as separate claims. Evidence carries a type, content hash, agent, UTC timestamp, project revision, method, verification status, and optional relative file, symbol, and line metadata. The portable CLI records evidence as `unverified`, including records with a `test_result` or `runtime_trace` type.

Supported claim kinds are `observation`, `inference`, `hypothesis`, `intent`, `decision`, `requirement`, `limitation`, and `open_question`. Claims cite evidence IDs where relevant, may cite contradicting evidence or dependent claims, and have no numeric confidence score. Claim text is immutable, while status changes are recorded through append-only events; for example, `claim status CL-0001 --status needs_review`. Add a new claim and an explicit link to correct or qualify earlier claim content.

Example:

```sh
python3 "{baseDir}/scripts/octane.py" evidence add \
  --type source_observation \
  --description "The parser calls normalize_input before validation" \
  --file src/parser.py --symbol parse_input
python3 "{baseDir}/scripts/octane.py" claim add \
  --kind hypothesis --text "Normalization may change the value before validation" \
  --evidence EV-0001
```

The `evidence_basis` field expresses an ordered assessment derived from linked record types. Report the actual tests and runtime observations separately, including the behavior the checks exercised.
