# Portable Octane triage

Establish a failing check before claiming a cause. Use the portable CLI to narrow what to inspect, then seek evidence that could disprove the hypothesis.

`languages` performs bounded extension inventory. `locate --trace-file failure.log` parses common Python, JavaScript/TypeScript, Go, Java, Rust, and C/C++ frame shapes. It ranks source files using frame repetition and order, path query terms, and recent Git changes. Run approved checks through the host's authorized tools and save the trace as project-local text.

`graph` returns candidate file dependencies. It combines Python AST, language-specific recognizers, generic import patterns, and optional Tree-sitter syntax trees. Install the parser extra with `python3 -m pip install '.[ast]'` from the full repository checkout using Python 3.11+. Use `graph --parser tree-sitter` to require that backend. Check parser coverage and unresolved edges before relying on the graph.

To add project-specific suffixes or import rules, use a data-only `.octane/languages.json` and pass `--language-config .octane/languages.json` to `languages`, `graph`, or `locate`. Entries support `extensions`, `quoted_import_keywords`, `module_import_keywords`, and `from_import`. For example, this entry recognizes `.qz` files and `fetch "helper"` imports.

```json
{"languages": {"quartz": {"extensions": [".qz"], "quoted_import_keywords": ["fetch"]}}}
```

Record the source observation and your interpretation separately.

```sh
python3 "{baseDir}/scripts/octane.py" evidence add \
  --type source_observation --description "parse_input passes the value to normalize_input" \
  --file src/parser.py --symbol parse_input
python3 "{baseDir}/scripts/octane.py" claim add \
  --kind hypothesis --text "Normalization may affect validation" --evidence EV-0001
```

Use the revision and path to revisit the observation. Use a focused reproduction or runtime trace to check how the code behaves.
