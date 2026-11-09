# Phase 1: claims and evidence

Octance stores observations as immutable evidence and interpretations as separate claims. Evidence uses one of the CLI's listed types, has a content hash, agent, UTC timestamp, project revision, method, verification status, and optional relative file/symbol/line metadata. Phase 1 always records evidence as `unverified`; merely naming a record `test_result` or `runtime_trace` does not prove it ran.

Supported claim kinds are `observation`, `inference`, `hypothesis`, `intent`, `decision`, `requirement`, `limitation`, and `open_question`. Claims cite evidence IDs where relevant, may cite contradicting evidence or dependent claims, have no numeric confidence score, and cannot be edited in Phase 1. Add a new claim and an explicit link to correct or qualify an earlier one.

Example:

```sh
python3 "{baseDir}/scripts/octance.py" evidence add \
  --type source_observation \
  --description "The parser calls normalize_input before validation" \
  --file src/parser.py --symbol parse_input
python3 "{baseDir}/scripts/octance.py" claim add \
  --kind hypothesis --text "Normalization may change the value before validation" \
  --evidence EV-0001
```

The `evidence_basis` field is ordinal, derived from linked record types, and is not a probability. Phase 1 has no replayable check or route to `verified`; report tests and runtime observations separately in conversation until check replay is implemented.
