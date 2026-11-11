# Investigation workflow and current limits

The long-term policy is reproducer-first: establish a failing check before claiming a cause, localize ranked suspects, explore the strongest candidates under budgets, and actively seek evidence that contradicts a hypothesis. The current implementation is only the first static triage slice; it has not passed a representative localization benchmark.

`languages` performs bounded extension inventory. `locate --trace-file failure.log` parses common Python, JavaScript/TypeScript, Go, Java, Rust, and C/C++ frame shapes, then ranks source files using frame repetition/order, path query terms, and recent Git changes. It does not parse complete language grammars, execute checks, or label a suspect as a root cause. Keep trace files within the project root; Octance reads them as untrusted text.

There is no predeclared replayable check runner or general `explore` command yet. Run approved checks with the host agent's authorized tools and save their output to a project-local trace file before using `locate`. All resulting evidence is still `unverified`; a trace alone does not verify a claim. The shipped synthetic ranking benchmark is a mechanics regression test, not a real-project evaluation and not a go decision for later features.

Example of supported Phase 1 use:

```sh
python3 "{baseDir}/scripts/octance.py" evidence add \
  --type source_observation --description "parse_input passes the value to normalize_input" \
  --file src/parser.py --symbol parse_input
python3 "{baseDir}/scripts/octance.py" claim add \
  --kind hypothesis --text "Normalization may affect validation" --evidence EV-0001
```

A static observation is evidence that the recorded source structure was seen at a particular revision; it is not proof of runtime behavior or a defect.
