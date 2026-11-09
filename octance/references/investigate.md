# Investigation workflow and current limits

The long-term policy is reproducer-first: establish a failing check before claiming a cause, localize ranked suspects, explore the strongest candidates under budgets, and actively seek evidence that contradicts a hypothesis. This protocol is not yet available as Octance commands.

**Phase 1 limitation:** `check`, `explore`, `locate`, benchmark, and investigation commands are not implemented. Use the host agent's existing approved repository/test tools; record direct observations in `evidence` and keep interpretations in `claim`. Do not call heuristic ranking or automatic fault localization a working feature yet.

Example of supported Phase 1 use:

```sh
python3 "{baseDir}/scripts/octance.py" evidence add \
  --type source_observation --description "parse_input passes the value to normalize_input" \
  --file src/parser.py --symbol parse_input
python3 "{baseDir}/scripts/octance.py" claim add \
  --kind hypothesis --text "Normalization may affect validation" --evidence EV-0001
```

A static observation is evidence that the recorded source structure was seen at a particular revision; it is not proof of runtime behavior or a defect.
